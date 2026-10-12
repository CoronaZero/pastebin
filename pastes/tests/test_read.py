"""阅读流程、密码访问与读取次数/有效期测试。"""
from datetime import timedelta

from django.contrib.auth.hashers import make_password
from django.utils import timezone

from pastes.models import Paste, SiteConfig
from pastes.services import consume_read_quota

from .base import PasteTestBase


class PlainReadTests(PasteTestBase):
    """无密码文本阅读。"""

    def test_unprotected_paste_reads_directly(self):
        """无密码文本 GET 直接返回正文。"""
        paste = self.make_paste(content="plain content \n line2")
        response = self.client.get(f"/p/{paste.id}/")
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "plain content")
        self.assertContains(response, " line2")
        paste.refresh_from_db()
        self.assertEqual(paste.read_count, 1)  # 每次成功读取都计数

    def test_read_counting_accumulates_on_refresh(self):
        """浏览器刷新再次成功获取正文，也会再次计数。"""
        paste = self.make_paste(max_reads=3)
        for expected in range(1, 4):
            response = self.client.get(f"/p/{paste.id}/")
            self.assertEqual(response.status_code, 200)
            paste.refresh_from_db()
            self.assertEqual(paste.read_count, expected)
        # 第 4 次达到上限：410，不再返回正文
        response = self.client.get(f"/p/{paste.id}/")
        self.assertEqual(response.status_code, 410)
        self.assertNotIn("hello pastebin", response.content.decode())

    def test_unlimited_paste_reads_repeatedly(self):
        """无读取上限的文本可反复读取。"""
        paste = self.make_paste()
        for _ in range(10):
            response = self.client.get(f"/p/{paste.id}/")
            self.assertEqual(response.status_code, 200)
        paste.refresh_from_db()
        self.assertEqual(paste.read_count, 10)

    def test_nonexistent_paste_returns_404(self):
        """不存在的分享 ID 返回 404。"""
        response = self.client.get("/p/00000000-0000-0000-0000-000000000000/")
        self.assertEqual(response.status_code, 404)
        self.assertNotIn("hello pastebin", response.content.decode())

    def test_invalid_uuid_returns_404(self):
        """非法 UUID 格式返回 404，不触发异常堆栈。"""
        response = self.client.get("/p/not-a-uuid/")
        self.assertEqual(response.status_code, 404)


class ExpiryTests(PasteTestBase):
    """有效期检查（每次读取时实时执行）。"""

    def test_expired_paste_rejected(self):
        """已过期文本不能读取：410。"""
        paste = self.make_paste(expires_at=timezone.now() - timedelta(seconds=1))
        response = self.client.get(f"/p/{paste.id}/")
        self.assertEqual(response.status_code, 410)
        self.assertContains(response, "已过期", status_code=410)
        self.assertNotIn("hello pastebin", response.content.decode())

    def test_expiry_boundary_is_inclusive(self):
        """当前时间恰好达到过期时间即拒绝（>= 语义）。"""
        paste = self.make_paste(expires_at=timezone.now())
        response = self.client.get(f"/p/{paste.id}/")
        self.assertEqual(response.status_code, 410)

    def test_unexpired_paste_readable(self):
        """未过期文本可正常读取。"""
        paste = self.make_paste(expires_at=timezone.now() + timedelta(seconds=5))
        self.assertEqual(self.client.get(f"/p/{paste.id}/").status_code, 200)

    def test_expiry_not_extended_by_access(self):
        """有效期不因访问而延长：距过期 2 秒的文本访问一次后，稍后仍按原时间过期。"""
        paste = self.make_paste(expires_at=timezone.now() + timedelta(seconds=0.5))
        self.assertEqual(self.client.get(f"/p/{paste.id}/").status_code, 200)
        # 伪造时间前进：直接把过期时间改到过去（等效于真实等待）
        Paste.objects.filter(pk=paste.pk).update(expires_at=timezone.now() - timedelta(seconds=1))
        self.assertEqual(self.client.get(f"/p/{paste.id}/").status_code, 410)


class MaxReadsTests(PasteTestBase):
    """读取次数限制。"""

    def test_exhausted_paste_rejected(self):
        """达到读取上限的文本不能再读取：410。"""
        paste = self.make_paste(max_reads=1, read_count=1)
        response = self.client.get(f"/p/{paste.id}/")
        self.assertEqual(response.status_code, 410)
        self.assertContains(response, "读取次数已达上限", status_code=410)

    def test_consume_read_quota_atomic_single(self):
        """consume_read_quota：max_reads=1 时第二次调用被原子拒绝。"""
        paste = self.make_paste(max_reads=1)
        granted, reason = consume_read_quota(paste.id)
        self.assertIsNotNone(granted)
        self.assertEqual(granted.content, "hello pastebin")
        granted, reason = consume_read_quota(paste.id)
        self.assertIsNone(granted)
        self.assertEqual(reason, "exhausted")

    def test_consume_read_quota_rejects_expired(self):
        """consume_read_quota 对过期文本拒绝。"""
        paste = self.make_paste(expires_at=timezone.now() - timedelta(seconds=1))
        granted, reason = consume_read_quota(paste.id)
        self.assertIsNone(granted)
        self.assertEqual(reason, "expired")

    def test_post_method_not_allowed_on_unprotected_paste(self):
        """无密码文本不接受 POST（防止误用消耗配额）。"""
        paste = self.make_paste()
        response = self.client.post(f"/p/{paste.id}/", {"password": "x"})
        self.assertEqual(response.status_code, 405)


class PasswordTests(PasteTestBase):
    """密码保护文本。"""

    def make_protected(self, password="s3cret-pw", **fields) -> Paste:
        return self.make_paste(
            password_hash=make_password(password), **fields
        )

    def test_protected_get_shows_password_form_only(self):
        """GET 只显示密码页，绝不返回正文。"""
        paste = self.make_protected(content="TOP SECRET CONTENT")
        response = self.client.get(f"/p/{paste.id}/")
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "密码")
        self.assertNotIn("TOP SECRET CONTENT", response.content.decode())
        # 密码哈希也不得出现在页面
        self.assertNotIn(paste.password_hash, response.content.decode())

    def test_correct_password_reads_content(self):
        """正确密码返回正文并消耗一次读取次数。"""
        paste = self.make_protected(content="SECRET BODY")
        response = self.client.post(
            f"/p/{paste.id}/", {"password": "s3cret-pw"}, REMOTE_ADDR="127.0.0.1"
        )
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "SECRET BODY")
        paste.refresh_from_db()
        self.assertEqual(paste.read_count, 1)

    def test_wrong_password_shows_generic_error_no_content(self):
        """错误密码：通用提示、不返回正文、不消耗读取次数。"""
        paste = self.make_protected(content="SECRET BODY", max_reads=2)
        for _ in range(3):
            response = self.client.post(
                f"/p/{paste.id}/", {"password": "wrong"}, REMOTE_ADDR="127.0.0.1"
            )
            self.assertEqual(response.status_code, 200)
            self.assertContains(response, "密码错误")
            self.assertNotIn("SECRET BODY", response.content.decode())
        paste.refresh_from_db()
        self.assertEqual(paste.read_count, 0)  # 错误密码不消耗读取次数

        # 正确密码仍可成功读取
        response = self.client.post(
            f"/p/{paste.id}/", {"password": "s3cret-pw"}, REMOTE_ADDR="127.0.0.1"
        )
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "SECRET BODY")

    def test_password_not_stored_in_plaintext(self):
        """数据库不保存明文密码。"""
        self.post_create(content="with password", password="plainpw")
        paste = Paste.objects.get()
        self.assertNotEqual(paste.password_hash, "plainpw")
        self.assertTrue(paste.password_hash.startswith("pbkdf2_sha256$"))
        self.assertNotIn("plainpw", paste.password_hash)

    def test_empty_password_submission_rejected(self):
        """POST 空密码：表单错误页，不消耗读取次数。"""
        paste = self.make_protected(max_reads=1)
        response = self.client.post(
            f"/p/{paste.id}/", {"password": ""}, REMOTE_ADDR="127.0.0.1"
        )
        self.assertEqual(response.status_code, 200)
        self.assertNotIn("hello pastebin", response.content.decode())
        paste.refresh_from_db()
        self.assertEqual(paste.read_count, 0)

    def test_password_attempt_limit_enforced(self):
        """密码尝试限制：每个网络每小时达到上限后拒绝后续验证（429）。"""
        paste = self.make_protected(password="real-pw")
        SiteConfig.objects.update(max_password_attempts_per_hour=3)
        # 3 次尝试（无论对错）后，第 4 次即使密码正确也被拒绝
        for _ in range(3):
            response = self.client.post(
                f"/p/{paste.id}/", {"password": "wrong"}, REMOTE_ADDR="127.0.0.1"
            )
            self.assertEqual(response.status_code, 200)
        response = self.client.post(
            f"/p/{paste.id}/", {"password": "real-pw"}, REMOTE_ADDR="127.0.0.1"
        )
        self.assertEqual(response.status_code, 429)
        self.assertNotIn("hello pastebin", response.content.decode())

    def test_expired_protected_paste_shows_gone_not_password_form(self):
        """过期/耗尽的受保护文本 GET 直接显示错误页，而非密码页。"""
        paste = self.make_protected(expires_at=timezone.now() - timedelta(seconds=1))
        response = self.client.get(f"/p/{paste.id}/")
        self.assertEqual(response.status_code, 410)
        self.assertContains(response, "已过期", status_code=410)
        # 不再展示密码输入框
        self.assertNotIn('type="password"', response.content.decode())

    def test_exhausted_protected_paste_shows_gone(self):
        """耗尽的受保护文本 GET 直接显示错误页。"""
        paste = self.make_protected(max_reads=1, read_count=1)
        response = self.client.get(f"/p/{paste.id}/")
        self.assertEqual(response.status_code, 410)
        self.assertContains(response, "读取次数已达上限", status_code=410)

    def test_correct_password_on_exhausted_paste_shows_gone(self):
        """密码正确但已并发耗尽：显示耗尽错误（原子条件更新兜底）。"""
        paste = self.make_protected(max_reads=1, read_count=1)
        response = self.client.post(
            f"/p/{paste.id}/", {"password": "s3cret-pw"}, REMOTE_ADDR="127.0.0.1"
        )
        self.assertEqual(response.status_code, 410)
        self.assertNotIn("hello pastebin", response.content.decode())
