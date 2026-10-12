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
from django.http import HttpResponse

# 极简错误页模板（骨架阶段模板尚未就绪；模板提交后统一切换为 render 渲染 status.html）
_ERROR_PAGE = """<!DOCTYPE html>
<html lang="zh-Hans">
<head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>{title} - Pastebin</title></head>
<body style="font-family: system-ui, sans-serif; margin: 2rem auto; max-width: 32rem; padding: 0 1rem;">
<h1>{title}</h1><p>{message}</p><p><a href="/">返回首页</a></p></body></html>"""


def _plain_error(status: int, title: str, message: str) -> HttpResponse:
    """返回不依赖模板引擎的极简错误页，避免错误页本身再引入故障点。"""
    html = _ERROR_PAGE.format(title=title, message=message)
    return HttpResponse(html, status=status, content_type="text/html; charset=utf-8")


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
                    return _plain_error(413, "请求被拒绝", "请使用标准表单方式提交。")
            else:
                try:
                    length = int(raw_length)
                except (TypeError, ValueError):
                    return _plain_error(400, "请求无效", "请求头格式非法。")
                if length < 0:
                    return _plain_error(400, "请求无效", "请求头格式非法。")
                if length > settings.PASTE_MAX_REQUEST_BODY_BYTES:
                    return _plain_error(413, "请求体过大", "请求体超出服务器允许的大小上限。")
        return self.get_response(request)


class SecurityHeadersMiddleware:
    """为所有响应统一设置安全响应头。

    - Cache-Control: no-store          —— 禁止任何缓存保存页面（含正文与错误页）
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
        headers["Cache-Control"] = "no-store"
        headers["X-Content-Type-Options"] = "nosniff"
        headers["Referrer-Policy"] = "no-referrer"
        headers["X-Robots-Tag"] = "noindex, nofollow, noarchive, nosnippet"
        headers["Content-Security-Policy"] = self.CSP_POLICY
        return response
