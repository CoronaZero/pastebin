"""Django Admin 后台测试：安全边界与运营配置生效。"""
from django.contrib.auth.models import User
from django.urls import reverse

from pastes.models import Paste, SiteConfig

from .base import PasteTestBase


class AdminAccessTests(PasteTestBase):
    """后台访问控制。"""

    def setUp(self):
        super().setUp()
        self.admin_user = User.objects.create_superuser(
            username="boss", email="boss@example.com", password="strong-pass-123"
        )

    def login(self):
        self.client.force_login(self.admin_user)

    def test_admin_requires_login(self):
        """未登录访问后台：重定向到登录页。"""
        response = self.client.get("/admin/")
        self.assertEqual(response.status_code, 302)
        self.assertIn("login", response.url)

    def test_admin_index_accessible_under_csp(self):
        """登录后后台首页可用（验证严格 CSP 与 Admin 兼容）。"""
        self.login()
        response = self.client.get("/admin/")
        self.assertEqual(response.status_code, 200)
        # CSP 头依然全局生效
        self.assertIn("default-src 'none'", response.headers["Content-Security-Policy"])

    def test_paste_admin_detail_does_not_expose_content(self):
        """后台文本详情页不展示正文与密码哈希。"""
        self.login()
        paste = self.make_paste(content="ADMIN-VIEW-SECRET-BODY", password_hash="pbkdf2_sha256$abc$xyz")
        response = self.client.get(f"/admin/pastes/paste/{paste.pk}/change/")
        self.assertEqual(response.status_code, 200)
        body = response.content.decode()
        self.assertNotIn("ADMIN-VIEW-SECRET-BODY", body)
        self.assertNotIn("pbkdf2_sha256$abc$xyz", body)

    def test_paste_admin_is_readonly(self):
        """后台文本对象不可编辑（只读审计 + 允许删除）。"""
        self.login()
        paste = self.make_paste()
        response = self.client.post(
            f"/admin/pastes/paste/{paste.pk}/change/",
            {"content": "tampered"},
        )
        # 无修改权限 → 403（Django admin 对无权限操作返回禁止页）
        self.assertEqual(response.status_code, 403)
        paste.refresh_from_db()
        self.assertEqual(paste.content, "hello pastebin")

    def test_paste_admin_add_disabled(self):
        """后台不允许直接新建文本（业务数据只来自前台流程）。"""
        self.login()
        response = self.client.get("/admin/pastes/paste/add/")
        self.assertEqual(response.status_code, 403)

    def test_siteconfig_add_blocked_when_exists(self):
        """站点配置为单例：已存在实例时禁止再添加。"""
        self.login()
        self.assertEqual(SiteConfig.objects.count(), 1)
        response = self.client.get("/admin/pastes/siteconfig/add/")
        self.assertEqual(response.status_code, 403)

    def test_siteconfig_edit_via_admin_takes_effect(self):
        """通过后台表单修改配置后，频率限额立即生效。"""
        self.login()
        config = SiteConfig.load()
        url = reverse("admin:pastes_siteconfig_change", args=[config.pk])
        response = self.client.post(
            url,
            {
                "max_creates_per_hour": 2,
                "max_password_attempts_per_hour": 30,
                "rate_retention_hours": 48,
                "password_max_length": 128,
            },
        )
        self.assertEqual(response.status_code, 302, response.context["adminform"].errors if response.context else "")
        config.refresh_from_db()
        self.assertEqual(config.max_creates_per_hour, 2)
        # 新限额立即约束业务行为
        self.assertEqual(self.post_create(content="a").status_code, 302)
        self.assertEqual(self.post_create(content="b").status_code, 302)
        self.assertEqual(self.post_create(content="c").status_code, 429)

    def test_ipban_admin_add_and_effect(self):
        """通过后台添加封禁规则后立即生效。"""
        self.login()
        response = self.client.post(
            "/admin/pastes/ipban/add/",
            {
                "network": "127.0.0.0/8",
                "reason": "测试封禁",
                "active": "on",
            },
        )
        self.assertEqual(response.status_code, 302)
        self.assertEqual(self.post_create(content="blocked now").status_code, 403)
