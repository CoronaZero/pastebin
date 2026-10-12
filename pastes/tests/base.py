"""测试公共工具。"""
from datetime import timedelta

from django.test import TestCase
from django.utils import timezone

from pastes.models import Paste, SiteConfig


class PasteTestBase(TestCase):
    """通用工具：默认创建数据与配置读取。"""

    def setUp(self):
        # 确保配置单例存在（各测试独立事务内重建）
        self.config = SiteConfig.load()

    def make_paste(self, **fields) -> Paste:
        """直接经 ORM 构造一条文本记录。"""
        defaults = {
            "content": "hello pastebin",
            "expires_at": timezone.now() + timedelta(days=1),
            "max_reads": None,
            "password_hash": "",
        }
        defaults.update(fields)
        return Paste.objects.create(**defaults)

    def post_create(self, content="hello pastebin", **extra):
        """向创建接口提交表单，返回响应。"""
        data = {"content": content, "expiry": "1d"}
        data.update(extra)
        return self.client.post("/", data, REMOTE_ADDR="127.0.0.1")
