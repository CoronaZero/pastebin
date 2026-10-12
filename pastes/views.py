"""
视图层。

路由：
- GET  /                    创建页
- POST /                    创建文本（封禁/频率检查 → 表单验证 → 哈希 → 入库）
- GET  /created/<id>/       创建成功页（PRG 模式，刷新不重复创建）
- GET  /p/<id>/             阅读文本（无密码直接显示；有密码显示密码表单）
- POST /p/<id>/             提交访问密码验证
- GET  /robots.txt          爬虫劝阻声明

安全顺序约定（每一步都在前一步通过后执行）：
  封禁检查 → 频率配额（原子）→ 输入验证 → 密码哈希（昂贵操作放最后）
错误密码不消耗读取次数；读取次数只在密码验证成功后由原子条件更新消耗。
"""
from django.contrib.auth.hashers import check_password, make_password
from django.http import HttpResponse
from django.shortcuts import redirect, render
from django.urls import reverse
from django.utils import timezone

from .forms import MAX_CONTENT_BYTES, PasteCreateForm, PastePasswordForm
from .models import HourCounter, Paste, SiteConfig
from .services import (
    REASON_EXPIRED,
    REASON_EXHAUSTED,
    REASON_NOT_FOUND,
    consume_hour_quota,
    consume_read_quota,
    get_client_ip,
    is_banned,
    network_key,
    parse_paste_id,
)

# 状态页文案：kind -> (HTTP 状态码, 标题, 说明)
STATUS_INFO = {
    "not_found": (404, "文本不存在", "该链接无效，或文本从未存在。"),
    "expired": (410, "文本已过期", "该文本已超过有效期，无法再访问。"),
    "exhausted": (410, "读取次数已达上限", "该文本的读取次数已用完，无法再访问。"),
    "method": (405, "方法不允许", "该页面不支持此请求方法。"),
    "banned": (403, "访问被拒绝", "当前网络被禁止执行此操作。"),
    "rate_limited_create": (429, "创建过于频繁", "当前网络在本小时内的创建次数已达上限，请一小时后再试。"),
    "rate_limited_password": (429, "尝试过于频繁", "当前网络在本小时内的密码验证次数已达上限，请一小时后再试。"),
}

REASON_TO_KIND = {
    REASON_NOT_FOUND: "not_found",
    REASON_EXPIRED: "expired",
    REASON_EXHAUSTED: "exhausted",
}


def _status_page(request, kind: str):
    status, title, message = STATUS_INFO[kind]
    return render(request, "pastes/status.html", {"title": title, "message": message}, status=status)


def _check_ban_and_quota(request, scope: str, limit: int, denied_kind: str):
    """封禁与频率配额检查（原子消耗），被拒时返回状态页，通过时返回 None。

    注意：任何进入视图的写请求都会消耗对应配额（即使表单验证失败），
    以防止垃圾请求绕过计数持续冲击数据库。
    """
    ip = get_client_ip(request)
    if ip is not None and is_banned(ip):
        return _status_page(request, "banned")
    key = network_key(ip) if ip is not None else "unknown"
    if not consume_hour_quota(scope, key, limit):
        return _status_page(request, denied_kind)
    return None


def index(request):
    """创建页：GET 渲染表单；POST 创建文本。"""
    config = SiteConfig.load()
    if request.method == "POST":
        denied = _check_ban_and_quota(
            request,
            HourCounter.Scope.CREATE,
            config.max_creates_per_hour,
            "rate_limited_create",
        )
        if denied is not None:
            return denied
        form = PasteCreateForm(request.POST, config=config)
        if form.is_valid():
            password = form.cleaned_data["password"]
            paste = Paste(
                content=form.cleaned_data["content"],
                password_hash=make_password(password) if password else "",
                expires_at=timezone.now() + form.expiry_timedelta,
                max_reads=form.cleaned_data["max_reads"],
            )
            paste.save()
            # PRG：成功后重定向，刷新不会重复创建
            return redirect("pastes:created", paste_id=paste.id)
    else:
        form = PasteCreateForm(config=config)
    return render(
        request,
        "pastes/create.html",
        {"form": form, "max_bytes": MAX_CONTENT_BYTES},
    )


def created(request, paste_id):
    """创建成功页：展示完整分享链接，方便复制。"""
    uid = parse_paste_id(paste_id)
    if uid is None:
        return _status_page(request, "not_found")
    # 仅确认存在（不读取正文，成功页无需也不应展示正文）
    if not Paste.objects.filter(id=uid).exists():
        return _status_page(request, "not_found")
    share_url = request.build_absolute_uri(reverse("pastes:view", args=[str(uid)]))
    return render(request, "pastes/created.html", {"share_url": share_url, "paste_id": str(uid)})


def view(request, paste_id):
    """阅读页：无密码直接展示正文；有密码先验证密码。"""
    uid = parse_paste_id(paste_id)
    if uid is None:
        return _status_page(request, "not_found")
    config = SiteConfig.load()

    # 元数据查询：刻意不含正文 content
    meta = (
        Paste.objects.filter(id=uid)
        .only("password_hash", "expires_at", "read_count", "max_reads")
        .first()
    )
    if meta is None:
        return _status_page(request, "not_found")

    # 每次读取时执行有效性检查：过期或已达读取上限的文本（无论有无密码）
    # 一律显示错误页，不再进入正文获取流程
    if meta.is_expired:
        return _status_page(request, "expired")
    if meta.is_read_exhausted:
        return _status_page(request, "exhausted")

    if not meta.has_password:
        if request.method != "GET":
            return _status_page(request, "method")
        paste, reason = consume_read_quota(uid)
        if paste is None:
            # 并发竞态兜底：查询与消耗之间恰好到期/耗尽
            return _status_page(request, REASON_TO_KIND[reason])
        return render(request, "pastes/view.html", {"paste": paste})

    # —— 密码保护文本 ——
    if request.method != "POST":
        # GET（及其他方法）只显示密码输入页，绝不返回正文
        form = PastePasswordForm(config=config)
        return render(
            request,
            "pastes/password.html",
            {"form": form, "paste_id": str(uid)},
        )

    # POST：封禁检查 + 尝试配额（在昂贵的密码哈希验证之前）
    denied = _check_ban_and_quota(
        request,
        HourCounter.Scope.PASSWORD,
        config.max_password_attempts_per_hour,
        "rate_limited_password",
    )
    if denied is not None:
        return denied

    form = PastePasswordForm(request.POST, config=config)
    if not form.is_valid():
        return render(
            request,
            "pastes/password.html",
            {"form": form, "paste_id": str(uid)},
        )

    if not check_password(form.cleaned_data["password"], meta.password_hash):
        # 通用错误提示：不区分具体失败原因，不泄露任何正文信息；
        # 错误密码不消耗读取次数（读取配额仅在验证成功后消耗）
        form.add_error(None, "密码错误。")
        return render(
            request,
            "pastes/password.html",
            {"form": form, "paste_id": str(uid)},
        )

    # 密码正确：原子消耗一次读取次数（并发耗尽/过期在此兜底）
    paste, reason = consume_read_quota(uid)
    if paste is None:
        return _status_page(request, REASON_TO_KIND[reason])
    return render(request, "pastes/view.html", {"paste": paste})


def robots(request):
    """robots.txt：劝阻遵守规则的爬虫抓取全站。

    该声明仅对遵守 robots 协议的爬虫有效，不能保证所有爬虫都不抓取。
    """
    return HttpResponse("User-agent: *\nDisallow: /\n", content_type="text/plain; charset=utf-8")


def healthz(request):
    """健康检查端点：验证 HTTP 栈与数据库读路径（仅 GET，无敏感信息）。

    供容器 HEALTHCHECK 与负载均衡探针使用；异常时由 Django 转为 5xx。
    """
    SiteConfig.objects.exists()  # 任何数据库读失败都会抛异常 → 非 200
    return HttpResponse("ok\n", content_type="text/plain; charset=utf-8")
