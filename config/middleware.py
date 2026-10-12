"""
全局中间件。

包含两个中间件：
1. RequestBodySizeMiddleware —— 请求体大小预检；
2. SecurityHeadersMiddleware —— 统一安全响应头。

说明：
- 大小预检只是"快速通道"，用于在早期拒绝明显超大的请求以节省资源；
  真实读入限制由 settings.DATA_UPLOAD_MAX_MEMORY_SIZE 兜底（不能只依赖
  Content-Length 头，头可以被伪造），最终内容限制由表单层按 UTF-8 字节数验证。
- 任何中间件与视图都不记录密码、正文或密码哈希到日志。
"""
from django.conf import settings
from django.shortcuts import render

# 状态码 → (标题, 说明)；与 pastes 视图的状态页共用同一模板
_BODY_LIMIT_PAGES = {
    400: ("请求无效", "请求头格式非法。"),
    413: ("请求体过大", "请求体超出服务器允许的大小上限，或使用了不受支持的传输方式。"),
}


def _plain_error(request, status: int):
    """渲染统一状态页。"""
    title, message = _BODY_LIMIT_PAGES[status]
    return render(
        request,
        "pastes/status.html",
        {"title": title, "message": message},
        status=status,
    )


class RequestBodySizeMiddleware:
    """按 Content-Length 或 Transfer-Encoding 对请求体做早期预检。

    规则：
    - Content-Length 非法整数或负数 → 400；
    - Content-Length 超过上限 → 413；
    - 无 Content-Length 但声明 chunked 传输 → 413（本站表单请求始终带 Content-Length，
      经可信 Nginx 代理的请求同样如此；拒绝可避免对无界流的无界读取）；
    - 其余情况放行，由 DATA_UPLOAD_MAX_MEMORY_SIZE 与表单验证兜底。
    """

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        if request.method in ("POST", "PUT", "PATCH"):
            raw_length = request.META.get("CONTENT_LENGTH") or ""
            if raw_length.strip() == "":
                # 无 Content-Length：若有任何分块传输声明则直接拒绝
                transfer_encoding = (request.META.get("HTTP_TRANSFER_ENCODING") or "").lower()
                if "chunked" in transfer_encoding:
                    return _plain_error(request, 413)
            else:
                try:
                    length = int(raw_length)
                except (TypeError, ValueError):
                    return _plain_error(request, 400)
                if length < 0:
                    return _plain_error(request, 400)
                if length > settings.PASTE_MAX_REQUEST_BODY_BYTES:
                    return _plain_error(request, 413)
        return self.get_response(request)


class SecurityHeadersMiddleware:
    """为所有响应统一设置安全响应头。

    - Cache-Control: no-store          —— 页面（含正文与错误页）禁止任何缓存保存；
                                          静态资源例外，保留 WhiteNoise 的长缓存头
    - X-Content-Type-Options: nosniff —— 禁止浏览器嗅探 MIME 类型
    - Referrer-Policy: no-referrer    —— 不向外站泄露来源
    - X-Robots-Tag                    —— 劝阻搜索引擎索引/摘要（不保证所有爬虫遵守）
    - Content-Security-Policy          —— 仅允许本站静态资源，禁内联脚本与外联
    """

    CSP_POLICY = (
        "default-src 'none'; "
        "style-src 'self'; "
        "script-src 'self'; "
        "img-src 'self' data:; "
        "font-src 'self'; "
        "connect-src 'self'; "
        "form-action 'self'; "
        "base-uri 'none'; "
        "frame-ancestors 'none'"
    )

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        response = self.get_response(request)
        headers = response.headers
        # 静态资源（WhiteNoise / Nginx 服务）保留其自身缓存策略；
        # 页面响应一律 no-store（含正文与错误页）。
        # 注意补前导斜杠：request.path 形如 /static/...，而 STATIC_URL 默认无斜杠。
        static_prefix = "/" + settings.STATIC_URL.lstrip("/")
        if not request.path.startswith(static_prefix):
            headers["Cache-Control"] = "no-store"
        headers["X-Content-Type-Options"] = "nosniff"
        headers["Referrer-Policy"] = "no-referrer"
        headers["X-Robots-Tag"] = "noindex, nofollow, noarchive, nosnippet"
        headers["Content-Security-Policy"] = self.CSP_POLICY
        return response
