"""
定期清理命令：物理删除过期或读取次数已耗尽的分享文本，并清理过期的频率计数。

用法（建议由系统 cron 每小时执行一次）：
    python manage.py cleanup_pastes

安全说明：
- 本命令仅用于回收存储空间，不承担任何访问控制职责。
  过期/耗尽文本即使未被本命令删除，也始终无法被访问（每次读取时都会实时检查）。
- IP 封禁规则不会被本命令自动删除（由运营者在 Admin 后台显式管理）。
"""
from datetime import timedelta

from django.core.management.base import BaseCommand
from django.db.models import F, Q
from django.utils import timezone

from ...models import HourCounter, Paste, SiteConfig


class Command(BaseCommand):
    help = (
        "物理删除已过期或读取次数已耗尽的分享文本，"
        "并按保留时长清理过期的频率计数记录（仅回收空间，不承担访问控制）"
    )

    def handle(self, *args, **options):
        now = timezone.now()

        # 1. 删除过期或已耗尽的文本（两条件任一满足即删除）
        stale = Paste.objects.filter(
            Q(expires_at__lte=now)
            | Q(max_reads__isnull=False, read_count__gte=F("max_reads"))
        )
        paste_total, _ = stale.delete()

        # 2. 清理超过保留时长的频率计数（按自然小时粒度）
        config = SiteConfig.load()
        cutoff = (now - timedelta(hours=config.rate_retention_hours)).replace(
            minute=0, second=0, microsecond=0
        )
        counters_total, _ = HourCounter.objects.filter(hour_bucket__lt=cutoff).delete()

        self.stdout.write(
            self.style.SUCCESS(
                f"清理完成：删除 {paste_total} 条过期/耗尽文本记录，"
                f"清理 {counters_total} 条过期频率计数（保留时长 {config.rate_retention_hours} 小时）。"
            )
        )
