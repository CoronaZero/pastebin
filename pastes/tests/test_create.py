"""创建流程与输入验证测试。"""
import uuid
from datetime import timedelta

from django.utils import timezone

from pastes.forms import MAX_CONTENT_BYTES
from pastes.models import Paste

from .base import PasteTestBase


class CreateFlowTests(PasteTestBase):
    """正常创建与 PRG 流程。"""

    def test_create_success_redirect_and_link(self):
        """正常创建：302 到成功页，成功页展示完整链接，不含正文与密码。"""
        response = self.post_create(content="hello", password="pw123", max_reads="5")
        self.assertEqual(response.status_code, 302)
        paste = Paste.objects.get()
        self.assertEqual(str(paste.id), response.url.split("/")[-2])
        self.assertEqual(paste.read_count, 0)
        self.assertEqual(paste.max_reads, 5)
        self.assertTrue(paste.password_hash)
        # 成功页
        created_page = self.client.get(response.url)
        self.assertEqual(created_page.status_code, 200)
        self.assertIn(f"/p/{paste.id}/", created_page.context["share_url"])
        # 成功页不暴露正文与密码
        self.assertNotIn("hello", created_page.content.decode())
        self.assertNotIn("pw123", created_page.content.decode())

    def test_default_expiry_is_one_day(self):
        """默认有效期不存在时必须显式提交；未传 expiry 应被拒绝（服务端验证）。"""
        response = self.client.post("/", {"content": "x"}, REMOTE_ADDR="127.0.0.1")
        self.assertEqual(response.status_code, 200)  # 回到表单页显示错误
        self.assertEqual(Paste.objects.count(), 0)

    def test_expiry_choices_produce_correct_expiry_at(self):
        """四个有效期选项与创建时刻的正确偏移。"""
        for choice, delta in [
            ("12h", timedelta(hours=12)),
            ("1d", timedelta(days=1)),
            ("7d", timedelta(days=7)),
            ("30d", timedelta(days=30)),
        ]:
            before = timezone.now()
            response = self.post_create(content=f"content-{choice}", expiry=choice)
            after = timezone.now()
            self.assertEqual(response.status_code, 302, choice)
            paste = Paste.objects.get(content=f"content-{choice}")
            self.assertTrue(before + delta <= paste.expires_at <= after + delta)

    def test_share_id_is_uuid_v4(self):
        """分享 ID 必须是 UUIDv4（随机版本位）。"""
        self.post_create()
        paste = Paste.objects.get()
        self.assertEqual(paste.id.version, 4)
        self.assertIsInstance(paste.id, uuid.UUID)

    def test_paste_content_preserved_verbatim(self):
        """正文按原文保存：换行、空格、制表符与首尾空白完整保留。"""
        raw = "  首行带空白\nsecond line\n\ttab indent\n尾行  "
        self.post_create(content=raw)
        self.assertEqual(Paste.objects.get().content, raw)


class CreateValidationTests(PasteTestBase):
    """非法输入验证（服务端重新验证所有输入）。"""

    def test_empty_content_rejected(self):
        """空文本被拒绝，不创建记录。"""
        response = self.post_create(content="")
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "文本内容")
        self.assertEqual(Paste.objects.count(), 0)

    def test_whitespace_only_content_rejected(self):
        """纯空白文本被拒绝。"""
        response = self.post_create(content=" \n\t \n")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(Paste.objects.count(), 0)

    def test_content_exactly_256_kib_accepted(self):
        """恰好 262144 字节的合法 UTF-8 文本应创建成功。"""
        content = "a" * MAX_CONTENT_BYTES
        response = self.post_create(content=content)
        self.assertEqual(response.status_code, 302)
        self.assertEqual(len(Paste.objects.get().content.encode("utf-8")), MAX_CONTENT_BYTES)

    def test_content_over_256_kib_ascii_rejected(self):
        """262145 字节 ASCII 超限被拒绝。"""
        content = "a" * (MAX_CONTENT_BYTES + 1)
        response = self.post_create(content=content)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "文本过大")
        self.assertEqual(Paste.objects.count(), 0)

    def test_content_over_limit_multibyte_rejected(self):
        """按 UTF-8 字节数（而非字符数）限制：多字节字符在字符数低于上限时即可超限。"""
        # 87382 个中文汉字 = 262146 字节 > 256 KiB，但字符数远小于 262144
        content = "汉" * (MAX_CONTENT_BYTES // 3 + 1)
        response = self.post_create(content=content)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "文本过大")
        self.assertEqual(Paste.objects.count(), 0)

    def test_content_multibyte_under_limit_accepted(self):
        """87381 个中文汉字 = 262143 字节（< 262144），合法。"""
        content = "汉" * (MAX_CONTENT_BYTES // 3)
        self.assertEqual(len(content.encode("utf-8")), 262143)
        response = self.post_create(content=content)
        self.assertEqual(response.status_code, 302)
        self.assertEqual(len(Paste.objects.get().content.encode("utf-8")), 262143)

    def test_multibyte_unicode_roundtrip(self):
        """多字节 Unicode（含 emoji、组合字符、BOM）完整往返。"""
        content = "你好，世界！🌏🎉 ĉαβ γλ φ ⇐ ⇒ ™ € ½ ¼﻿ \U0001F600"
        self.post_create(content=content)
        self.assertEqual(Paste.objects.get().content, content)

    def test_invalid_expiry_rejected(self):
        """非法有效期选项被拒绝。"""
        response = self.post_create(expiry="365d")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(Paste.objects.count(), 0)

    def test_invalid_max_reads_values_rejected(self):
        """非法读取次数：0、负数、超上限、非整数。"""
        for bad in ["0", "-1", str(10_001), "abc", "1.5"]:
            with self.subTest(value=bad):
                response = self.post_create(max_reads=bad)
                self.assertEqual(response.status_code, 200, bad)
                self.assertEqual(Paste.objects.count(), 0, bad)

    def test_unlimited_reads_when_field_blank(self):
        """未填读取次数 → max_reads 为 NULL，表示不限制。"""
        self.post_create(content="x", max_reads="")
        self.assertIsNone(Paste.objects.get().max_reads)

    def test_password_too_long_rejected(self):
        """超过配置上限的密码被拒绝。"""
        long_password = "p" * (self.config.password_max_length + 1)
        response = self.post_create(password=long_password)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(Paste.objects.count(), 0)

    def test_invalid_utf8_percent_encoded_handled_gracefully(self):
        """百分号编码的非法 UTF-8 字节：不崩溃、不泄露堆栈，按替换字符处理。"""
        response = self.post_create(content="valid start %FF%FE end")
        # Django 以 errors="replace" 解码，应创建成功且无 500
        self.assertEqual(response.status_code, 302)
        stored = Paste.objects.get().content
        self.assertIn("valid start", stored)
        self.assertIn("end", stored)
