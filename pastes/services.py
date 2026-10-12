"""
业务服务层。

职责：
- 客户端 IP 提取与信任边界（可信代理跳数）；
- 网络键规范化（IPv4 /32、IPv6 /64，防格式差异绕过）；
- IP 封禁检查；
- 自然小时频率配额的原子消耗（并发安全，SQLite 写事务串行化）；
- 读取次数的原子条件消耗（并发请求无法突破上限）；
- 分享 ID 解析。

安全要点：
- 所有配额与读取消耗都使用"条件 UPDATE + 数据库端自增"的原子操作，
  绝不做"先查询再在 Python 中加一"的非原子逻辑；
- SQLite 以 BEGIN IMMEDIATE 开启写事务，并发写请求在 busy timeout 内排队，
  事务串行执行，因此并发下计数不可能被突破（超时则该请求报 database is locked，
  由 Django 转为 500，属于拒绝服务而非超发，可接受）。
- 本模块不记录任何密码、正文、哈希到日志。
"""
import ipaddress
import uuid
from datetime import datetime

from django.conf import settings
from django.db import IntegrityError, transaction
from django.db.models import F, Q
from django.utils import timezone

from .models import HourCounter, IpBan, Paste

# 无效 / 缺失读取原因
REASON_NOT_FOUND = "not_found"
REASON_EXPIRED = "expired"
REASON_EXHAUSTED = "exhausted"


# ---------------------------------------------------------------------------
# 客户端 IP：获取与信任边界
# ---------------------------------------------------------------------------

def get_client_ip(request):
    """提取客户端 IP，返回 ipaddress.IPv4Address / IPv6Address，无法解析时返回 None。

    信任边界：
    - PASTE_PROXY_HOPS == 0（默认）：完全忽略 X-Forwarded-For，使用 TCP 连接对端
      地址（REMOTE_ADDR）。未经代理的客户端伪造任何代理头都无效；
    - PASTE_PROXY_HOPS == N：仅在存在 N 层可信代理的部署下有效，取 XFF 从右数
      第 N 个地址。右侧 N 个地址由可信代理写入，再左侧的内容视为不可信被丢弃；
      XFF 缺失、不足或非法时回退 REMOTE_ADDR。
    - IPv4-mapped IPv6 地址统一归并为 IPv4，防止映射形式绕过 v4 计数与封禁。
    """
    remote_addr = (request.META.get("REMOTE_ADDR") or "").strip()
    hops = settings.PASTE_PROXY_HOPS
    candidate = remote_addr
    if hops > 0:
        forwarded = (request.META.get("HTTP_X_FORWARDED_FOR") or "").strip()
        parts = [p.strip() for p in forwarded.split(",") if p.strip()]
        if len(parts) >= hops:
            candidate = parts[-hops]
        # XFF 不足 hops 时 candidate 保持 REMOTE_ADDR，不信客户端可伪造的左侧内容
    try:
        addr = ipaddress.ip_address(candidate)
    except ValueError:
        return None
    if isinstance(addr, ipaddress.IPv6Address) and addr.ipv4_mapped is not None:
        addr = addr.ipv4_mapped
    return addr


# ---------------------------------------------------------------------------
# 网络键规范化
# ---------------------------------------------------------------------------

def network_key(addr) -> str:
    """把 IP 地址规范化为频率计数的网络键。

    IPv4 → /32，即地址本身；IPv6 → /64 前缀的网络地址。
    同一网络无论书写形式如何（如 2001:DB8::1 与 2001:0db8:0000::1），
    键均一致，无法通过大小写/压缩格式差异绕过限制。
    """
    if addr.version == 4:
        return f"v4:{addr}"
    net = ipaddress.ip_network(f"{addr}/64", strict=False)
    return f"v6:{net.network_address}"


# ---------------------------------------------------------------------------
# 封禁检查
# ---------------------------------------------------------------------------

def is_banned(addr) -> bool:
    """判断地址是否命中任一生效中的封禁规则。

    封禁范围由视图层决定：仅应用于创建文本与密码验证尝试，
    不影响已知晓链接的正常阅读（见 README「IP 封禁」一节）。
    """
    if addr is None:
        return False
    now = timezone.now()
    for ban in IpBan.objects.filter(active=True):
        if ban.expires_at is not None and now >= ban.expires_at:
            continue
        if ban.matches(addr):
            return True
    return False


# ---------------------------------------------------------------------------
# 自然小时频率配额（原子消耗）
# ---------------------------------------------------------------------------

def current_hour_bucket(now: datetime | None = None) -> datetime:
    """当前自然 UTC 小时的起点（整点）。窗口切换即自动重置。"""
    now = now or timezone.now()
    return now.replace(minute=0, second=0, microsecond=0)


def consume_hour_quota(scope: str, key: str, limit: int) -> bool:
    """原子消耗一个自然小时配额，返回本次是否被允许。

    实现说明（并发安全）：
    - 整体在 BEGIN IMMEDIATE 写事务中执行，SQLite 写锁串行化并发事务；
    - 先尝试原子 UPDATE（数据库端自增），行不存在时 INSERT（保存点保护），
      并发 INSERT 冲突（唯一约束）时改回原子 UPDATE 重试；
    - 事务内回读计数判断是否超限，判断依据与自增处于同一事务，不存在 TOCTOU；
    - 被拒绝的尝试同样计数（留下痕迹，且下小时前不会因拒绝而放开）。
    """
    bucket = current_hour_bucket()
    filters = {"scope": scope, "network_key": key, "hour_bucket": bucket}
    with transaction.atomic():
        updated = HourCounter.objects.filter(**filters).update(count=F("count") + 1)
        if updated:
            row = HourCounter.objects.filter(**filters).values_list("count", flat=True).first()
            count = row or 1
        else:
            try:
                with transaction.atomic():  # 保存点：冲突时仅回滚插入
                    HourCounter.objects.create(scope=scope, network_key=key, hour_bucket=bucket, count=1)
                count = 1
            except IntegrityError:
                # 并发插入已建立该行：原子递增并回读
                HourCounter.objects.filter(**filters).update(count=F("count") + 1)
                row = HourCounter.objects.filter(**filters).values_list("count", flat=True).first()
                count = row or 1
        return count <= limit


# ---------------------------------------------------------------------------
# 读取次数的原子消耗
# ---------------------------------------------------------------------------

def consume_read_quota(paste_id) -> tuple[Paste | None, str | None]:
    """原子消耗一次读取次数并返回正文记录。

    返回 (paste, None)       —— 本次读取被授权，paste 含正文；
    返回 (None, reason)      —— 拒绝，reason 为 not_found / expired / exhausted。

    原子性：单条条件 UPDATE（有效期未到 且 未达读取上限）在数据库端自增，
    并发请求串行执行，rows==0 的请求不会获得正文，因此无法突破上限。
    """
    now = timezone.now()
    with transaction.atomic():
        rows = Paste.objects.filter(
            id=paste_id,
            expires_at__gt=now,  # 当前时间达到或超过过期时间即拒绝
        ).filter(
            Q(max_reads__isnull=True) | Q(read_count__lt=F("max_reads"))
        ).update(read_count=F("read_count") + 1)
        if rows == 0:
            meta = (
                Paste.objects.filter(id=paste_id)
                .only("expires_at", "read_count", "max_reads")
                .first()
            )
            if meta is None:
                return None, REASON_NOT_FOUND
            if meta.expires_at <= now:
                return None, REASON_EXPIRED
            return None, REASON_EXHAUSTED
        paste = Paste.objects.get(id=paste_id)
        return paste, None


# ---------------------------------------------------------------------------
# 分享 ID 解析
# ---------------------------------------------------------------------------

def parse_paste_id(value) -> uuid.UUID | None:
    """解析分享 ID；非法格式返回 None（视图按不存在处理）。"""
    try:
        return uuid.UUID(str(value))
    except (ValueError, TypeError, AttributeError):
        return None
