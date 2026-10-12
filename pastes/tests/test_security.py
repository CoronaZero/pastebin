"""Web 安全测试：转义、CSRF、请求体限制、安全响应头、错误页不泄露内部信息。"""
from datetime import timedelta

from django.contrib.auth.hashers import make_password
from django.http import HttpResponse
from django.test import Client, RequestFactory, TestCase, override_settings
from django.utils import timezone

from pastes.models import Paste, SiteConfig

from .base import PasteTestBase


class ContentEscapeTests(PasteTestBase):
    """正文必须按纯文本显示，不能被解释为 HTML 或执行脚本。"""

    PAYLOAD = (
        '<script>alert("xss")</script>\n'
        '<img src=x onerror=alert(1)>\n'
        '<iframe src="javascript:alert(2)"></iframe>\n'
        '"><svg onload=alert(3)> & < > " \' ` ${7*7}\n'
        "line break\ttab 韩文한글 emoji 🌏"
    )

    def test_html_and_script_not_executed(self):
        """读取页包含转义后的载荷，不含可执行的原始标签。"""
        self.post_create(content=self.PAYLOAD)
        paste = Paste.objects.get()
        response = self.client.get(f"/p/{paste.id}/")
        self.assertEqual(response.status_code, 200)
        body = response.content.decode()
        # 原始标签不得出现（会被浏览器解释）
        self.assertNotIn("<script>", body)
        self.assertNotIn("<img src=x", body)
        self.assertNotIn("<iframe", body)
        self.assertNotIn("<svg", body)
        # 转义形式必须出现
        self.assertIn("&lt;script&gt;", body)
        self.assertIn("&lt;img", body)
        # 换行、Tab、Unicode 必须保留展示
        self.assertIn("line break\ttab", body)
        self.assertIn("韩文한글", body)
        self.assertIn("🌏", body)

    def test_content_displayed_in_pre_block(self):
        """正文渲染在 <pre> 中以保留原始换行与空格。"""
        self.post_create(content="first\n  second with spaces")
        paste = Paste.objects.get()
        response = self.client.get(f"/p/{paste.id}/")
        self.assertContains(response, "<pre")
        self.assertIn("first\n  second with spaces", response.content.decode())


class CsrfTests(PasteTestBase):
    """CSRF 防护（全局未关闭 CsrfViewMiddleware）。"""

    def test_create_without_token_rejected(self):
        """无 CSRF token 的创建请求被 403 拒绝，不产生记录。"""
        client = Client(enforce_csrf_checks=True)
        response = client.post(
            "/", {"content": "csrf-less", "expiry": "1d"}, REMOTE_ADDR="127.0.0.1"
        )
        self.assertEqual(response.status_code, 403)
        self.assertEqual(Paste.objects.count(), 0)

    def test_password_submit_without_token_rejected(self):
        """无 CSRF token 的密码提交被 403 拒绝，正文不可达。"""
        from django.contrib.auth.hashers import make_password

        paste = self.make_paste(
            content="CSRF SECRET", password_hash=make_password("pw")
        )
        client = Client(enforce_csrf_checks=True)
        response = client.post(
            f"/p/{paste.id}/", {"password": "pw"}, REMOTE_ADDR="127.0.0.1"
        )
        self.assertEqual(response.status_code, 403)
        self.assertNotIn("CSRF SECRET", response.content.decode())
        paste.refresh_from_db()
        self.assertEqual(paste.read_count, 0)

    def test_create_with_token_succeeds(self):
        """正常流程（带 token）不受影响。"""
        response = self.post_create(content="with token")
        self.assertEqual(response.status_code, 302)


class SecurityHeadersTests(PasteTestBase):
    """安全响应头在所有页面（含错误页）统一设置。"""

    def assert_common_headers(self, response):
        headers = response.headers
        self.assertEqual(headers["Cache-Control"], "no-store")
        self.assertEqual(headers["X-Content-Type-Options"], "nosniff")
        self.assertEqual(headers["Referrer-Policy"], "no-referrer")
        self.assertEqual(headers["X-Robots-Tag"], "noindex, nofollow, noarchive, nosnippet")
        self.assertEqual(headers["X-Frame-Options"], "DENY")
        self.assertIn("default-src 'none'", headers["Content-Security-Policy"])
        self.assertIn("script-src 'self'", headers["Content-Security-Policy"])

    def test_headers_on_create_page(self):
        self.assert_common_headers(self.client.get("/"))

    def test_headers_on_content_page(self):
        paste = self.make_paste()
        self.assert_common_headers(self.client.get(f"/p/{paste.id}/"))

    def test_headers_on_error_pages(self):
        # 404 / 410 / 429 / 403 均带安全头
        self.assert_common_headers(self.client.get("/p/not-a-uuid/"))
        expired = self.make_paste(expires_at=timezone.now() - timedelta(seconds=1))
        self.assert_common_headers(self.client.get(f"/p/{expired.id}/"))
        SiteConfig.objects.update(max_creates_per_hour=1)
        self.post_create(content="a")
        rate_limited = self.post_create(content="b")
        self.assertEqual(rate_limited.status_code, 429)
        headers = rate_limited.headers
        self.assertEqual(headers["Cache-Control"], "no-store")
        self.assertEqual(headers["X-Content-Type-Options"], "nosniff")

    def test_content_type_is_html_with_charset(self):
        paste = self.make_paste()
        response = self.client.get(f"/p/{paste.id}/")
        self.assertEqual(response.headers["Content-Type"], "text/html; charset=utf-8")

    def test_robots_txt_content_type(self):
        response = self.client.get("/robots.txt")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers["Content-Type"], "text/plain; charset=utf-8")
        self.assertIn("Disallow: /", response.content.decode())

    def test_no_inline_script_or_style_in_pages(self):
        """CSP 为无内联模式：页面源码不应包含内联 <script>/<style>（配合 script-src 'self'）。"""
        paste = self.make_paste()
        for path in ["/", f"/p/{paste.id}/", "/robots.txt"]:
            body = self.client.get(path).content.decode()
            self.assertNotIn("<script>", body)
            self.assertNotIn("style=", body, path)


class RequestBodyLimitTests(TestCase):
    """请求体大小限制：预检 + 无界读取防护。"""

    def make_request(self, data: bytes, **meta):
        request = RequestFactory().post(
            "/", data=data, content_type="application/x-www-form-urlencoded"
        )
        for key, value in meta.items():
            request.META[key] = value
        return request

    def call_middleware(self, request):
        from config.middleware import RequestBodySizeMiddleware

        def get_response(_):
            return HttpResponse("should not reach", status=200)

        return RequestBodySizeMiddleware(get_response)(request)

    def test_oversized_content_length_rejected_413(self):
        """Content-Length 超过上限：413，请求不会到达视图。"""
        request = self.make_request(b"a=1", CONTENT_LENGTH=str(5 * 1024 * 1024))
        response = self.call_middleware(request)
        self.assertEqual(response.status_code, 413)

    def test_invalid_content_length_rejected_400(self):
        """Content-Length 非整数：400。"""
        request = self.make_request(b"a=1", CONTENT_LENGTH="abc")
        response = self.call_middleware(request)
        self.assertEqual(response.status_code, 400)

    def test_negative_content_length_rejected_400(self):
        request = self.make_request(b"a=1", CONTENT_LENGTH="-5")
        response = self.call_middleware(request)
        self.assertEqual(response.status_code, 400)

    def test_chunked_without_length_rejected_413(self):
        """无 Content-Length 的 chunked 声明：413（避免对无界流的无界读取）。"""
        request = self.make_request(b"a=1", HTTP_TRANSFER_ENCODING="chunked")
        del request.META["CONTENT_LENGTH"]
        response = self.call_middleware(request)
        self.assertEqual(response.status_code, 413)

    def test_normal_body_passes_middleware(self):
        """正常大小请求通过预检。"""
        request = self.make_request(b"content=hello&expiry=1d")
        request.META["CONTENT_LENGTH"] = str(len(b"content=hello&expiry=1d"))
        response = self.call_middleware(request)
        self.assertEqual(response.status_code, 200)

    def test_huge_real_body_rejected_end_to_end(self):
        """端到端：5 MiB 真实请求体在中间件层被 413 拒绝，不进入视图。"""
        client = Client()
        big = "a" * (5 * 1024 * 1024)
        response = client.post(
            "/", {"content": big, "expiry": "1d"}, REMOTE_ADDR="127.0.0.1"
        )
        self.assertEqual(response.status_code, 413)
        self.assertEqual(Paste.objects.count(), 0)


class DebugLeakageTests(TestCase):
    """生产模式下不向用户泄露内部信息。"""

    @override_settings(DEBUG=False)
    def test_404_page_contains_no_debug_info(self):
        response = self.client.get("/p/not-a-uuid/")
        body = response.content.decode()
        self.assertNotIn("Traceback", body)
        self.assertNotIn("DJANGO_SETTINGS_MODULE", body)
        self.assertNotIn("<html lang=\"en\"", body)  # 非英文技术页

    def test_500_page_template_contains_no_stacktrace(self):
        """500 模板渲染不含任何堆栈信息。"""
        from django.views.defaults import server_error

        request = RequestFactory().get("/whatever/")
        response = server_error(request)
        body = response.content.decode()
        self.assertNotIn("Traceback", body)
        self.assertNotIn("Exception", body)
        self.assertIn("内部错误", body)
