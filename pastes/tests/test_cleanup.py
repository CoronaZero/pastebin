"""cleanup_pastes 管理命令测试。"""
import io
from datetime import timedelta

from django.core.management import call_command
from django.utils import timezone

from pastes.models import HourCounter, Paste, SiteConfig
from pastes.services import current_hour_bucket

from .base import PasteTestBase


class CleanupCommandTests(PasteTestBase):
    """清理命令：只回收空间，不承担访问控制。"""

    def run_cleanup(self) -> str:
        out = io.StringIO()
        call_command("cleanup_pastes", stdout=out)
        return out.getvalue()

    def test_cleanup_deletes_expired_pastes(self):
        """过期文本被物理删除。"""
        expired = self.make_paste(expires_at=timezone.now() - timedelta(seconds=1))
        alive = self.make_paste(expires_at=timezone.now() + timedelta(days=1))
        self.assertIn("删除 1 条", self.run_cleanup())
        self.assertFalse(Paste.objects.filter(pk=expired.pk).exists())
        self.assertTrue(Paste.objects.filter(pk=alive.pk).exists())

    def test_cleanup_deletes_exhausted_pastes(self):
        """读取次数已耗尽的文本被物理删除（即使未过期）。"""
        exhausted = self.make_paste(max_reads=1, read_count=1)
        untouched = self.make_paste(max_reads=1, read_count=0)
        self.assertIn("删除 1 条", self.run_cleanup())
        self.assertFalse(Paste.objects.filter(pk=exhausted.pk).exists())
        self.assertTrue(Paste.objects.filter(pk=untouched.pk).exists())

    def test_cleanup_stale_counters_only(self):
        """仅清理超过保留时长的频率计数，近期计数保留。"""
        stale_bucket = current_hour_bucket() - timedelta(hours=SiteConfig.load().rate_retention_hours + 1)
        fresh_bucket = current_hour_bucket()
        HourCounter.objects.create(
            scope="create", network_key="v4:192.0.2.1", hour_bucket=stale_bucket, count=10
        )
        HourCounter.objects.create(
            scope="create", network_key="v4:192.0.2.2", hour_bucket=fresh_bucket, count=3
        )
        self.assertIn("清理 1 条", self.run_cleanup())
        self.assertFalse(
            HourCounter.objects.filter(hour_bucket=stale_bucket).exists()
        )
        self.assertTrue(HourCounter.objects.filter(hour_bucket=fresh_bucket).exists())

    def test_cleanup_does_not_delete_ip_bans(self):
        """封禁规则不受清理命令影响（由运营者显式管理）。"""
        from pastes.models import IpBan

        IpBan.objects.create(network="203.0.113.5")
        self.run_cleanup()
        self.assertEqual(IpBan.objects.count(), 1)

    def test_cleanup_idempotent(self):
        """重复执行安全幂等。"""
        self.make_paste(expires_at=timezone.now() - timedelta(seconds=1))
        self.run_cleanup()
        output = self.run_cleanup()
        self.assertIn("删除 0 条", output)
