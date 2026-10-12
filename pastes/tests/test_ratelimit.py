"""创建频率限制、IP 封禁与网络键规范化测试。"""
import ipaddress
from datetime import timedelta

from django.test import RequestFactory, override_settings
from django.utils import timezone

from pastes.models import HourCounter, IpBan, SiteConfig
from pastes.services import (
    consume_hour_quota,
    current_hour_bucket,
    get_client_ip,
    is_banned,
    network_key,
)

from .base import PasteTestBase


class NetworkKeyTests(PasteTestBase):
    """网络键规范化：同一网络不同书写形式必须得到同一键。"""

    def test_ipv4_key(self):
        """IPv4 按 /32 处理：键为地址本身。"""
        self.assertEqual(network_key(ipaddress.ip_address("192.0.2.1")), "v4:192.0.2.1")
        self.assertEqual(network_key(ipaddress.ip_address("0.0.0.0")), "v4:0.0.0.0")

    def test_ipv6_normalized_to_64_prefix(self):
        """IPv6 按 /64 前缀聚合，且不同书写形式/主机地址得到相同键。"""
        key_a = network_key(ipaddress.ip_address("2001:0DB8:0001:0002:0000:0000:0000:0001"))
        key_b = network_key(ipaddress.ip_address("2001:db8:1:2::ffff:abcd"))
        key_c = network_key(ipaddress.ip_address("2001:DB8:0001:0002::1"))
        self.assertEqual(key_a, "v6:2001:db8:1:2::")
        self.assertEqual(key_a, key_b)
        self.assertEqual(key_a, key_c)

    def test_ipv6_different_64_networks_have_distinct_keys(self):
        """不同 /64 网络的键不同。"""
        key_1 = network_key(ipaddress.ip_address("2001:db8:1:2::a"))
        key_2 = network_key(ipaddress.ip_address("2001:db8:1:3::a"))
        self.assertNotEqual(key_1, key_2)

    def test_ipv4_mapped_ipv6_collapsed_to_ipv4(self):
        """IPv4-mapped IPv6 地址归并为 IPv4，防止用映射形式绕过 v4 计数与封禁。"""
        request = RequestFactory().get("/", REMOTE_ADDR="::ffff:192.0.2.1")
        addr = get_client_ip(request)
        self.assertEqual(network_key(addr), "v4:192.0.2.1")


class ClientIpTrustTests(PasteTestBase):
    """客户端 IP 获取与代理头信任边界。"""

    def test_default_hops_zero_ignores_forwarded_header(self):
        """默认（0 跳）：完全忽略 X-Forwarded-For，使用 REMOTE_ADDR。"""
        request = RequestFactory().get(
            "/", REMOTE_ADDR="198.51.100.7", HTTP_X_FORWARDED_FOR="8.8.8.8, 1.2.3.4"
        )
        self.assertEqual(str(get_client_ip(request)), "198.51.100.7")

    def test_hops_one_takes_rightmost_entry(self):
        """1 跳可信代理：取 XFF 最右侧第 1 个（代理记录的真实对端）。"""
        with override_settings(PASTE_PROXY_HOPS=1):
            request = RequestFactory().get(
                "/",
                REMOTE_ADDR="127.0.0.1",
                HTTP_X_FORWARDED_FOR="8.8.8.8, 203.0.113.9",
            )
            self.assertEqual(str(get_client_ip(request)), "203.0.113.9")

    def test_hops_two_takes_second_from_right(self):
        """2 跳可信代理：取从右数第 2 个。"""
        with override_settings(PASTE_PROXY_HOPS=2):
            request = RequestFactory().get(
                "/",
                REMOTE_ADDR="127.0.0.1",
                HTTP_X_FORWARDED_FOR="8.8.8.8, 203.0.113.9, 10.0.0.1",
            )
            self.assertEqual(str(get_client_ip(request)), "203.0.113.9")

    def test_hops_with_insufficient_entries_falls_back(self):
        """XFF 条目不足时回退 REMOTE_ADDR，不信任客户端可伪造的左侧内容。"""
        with override_settings(PASTE_PROXY_HOPS=3):
            request = RequestFactory().get(
                "/", REMOTE_ADDR="198.51.100.7", HTTP_X_FORWARDED_FOR="8.8.8.8"
            )
            self.assertEqual(str(get_client_ip(request)), "198.51.100.7")

    def test_invalid_remote_addr_yields_none(self):
        """无法解析的 REMOTE_ADDR 返回 None（不抛异常）。"""
        request = RequestFactory().get("/", REMOTE_ADDR="not-an-ip")
        self.assertIsNone(get_client_ip(request))

    def test_forged_forwarded_header_cannot_bypass_ratelimit(self):
        """集成：hops=0 时伪造 XFF 不能绕过创建频率限制（按真实对端计数）。"""
        SiteConfig.objects.update(max_creates_per_hour=2)
        addr = "203.0.113.50"
        statuses = []
        for i in range(3):
            response = self.client.post(
                "/",
                {"content": f"attempt {i}", "expiry": "1d"},
                REMOTE_ADDR=addr,
                HTTP_X_FORWARDED_FOR=f"8.8.8.{i}",
            )
            statuses.append(response.status_code)
        self.assertEqual(statuses[:2], [302, 302])
        self.assertEqual(statuses[2], 429)  # 第三次按真实 IP 计数被拒


class CreateRateLimitTests(PasteTestBase):
    """创建频率限制（每 IP 网络每自然 UTC 小时）。"""

    def test_same_ipv4_shares_quota(self):
        """同一 IPv4 地址共享同一限额，达到上限后 429。"""
        SiteConfig.objects.update(max_creates_per_hour=3)
        statuses = [
            self.post_create(content=f"c{i}", REMOTE_ADDR="203.0.113.10").status_code
            for i in range(4)
        ]
        self.assertEqual(statuses, [302, 302, 302, 429])
        counter = HourCounter.objects.get(
            scope=HourCounter.Scope.CREATE, network_key="v4:203.0.113.10"
        )
        self.assertEqual(counter.count, 4)  # 被拒尝试同样计数

    def test_different_ipv4_addresses_independent_quota(self):
        """不同 IPv4 地址各自独立计数。"""
        SiteConfig.objects.update(max_creates_per_hour=1)
        first = self.post_create(content="a", REMOTE_ADDR="203.0.113.1")
        second = self.post_create(content="b", REMOTE_ADDR="203.0.113.2")
        self.assertEqual(first.status_code, 302)
        self.assertEqual(second.status_code, 302)

    def test_same_ipv6_64_network_shares_quota(self):
        """同一 IPv6 /64 网络内的不同主机共享限额。"""
        SiteConfig.objects.update(max_creates_per_hour=2)
        hosts = ["2001:db8:1:2::a", "2001:db8:1:2::b", "2001:db8:1:2::c"]
        statuses = [
            self.post_create(content=f"h{i}", REMOTE_ADDR=host).status_code
            for i, host in enumerate(hosts)
        ]
        self.assertEqual(statuses, [302, 302, 429])  # 第三台同 /64 主机被拒

    def test_different_ipv6_64_networks_independent(self):
        """不同 IPv6 /64 网络独立计数。"""
        SiteConfig.objects.update(max_creates_per_hour=1)
        first = self.post_create(content="x", REMOTE_ADDR="2001:db8:1:2::a")
        second = self.post_create(content="y", REMOTE_ADDR="2001:db8:1:3::a")
        self.assertEqual(first.status_code, 302)
        self.assertEqual(second.status_code, 302)

    def test_natural_hour_rollover_resets_quota(self):
        """自然 UTC 小时切换后限额自动重置。"""
        SiteConfig.objects.update(max_creates_per_hour=1)
        self.assertEqual(self.post_create(content="first").status_code, 302)
        self.assertEqual(self.post_create(content="second").status_code, 429)
        # 将已有计数挪到上一自然小时（等效于真实跨小时）
        HourCounter.objects.update(hour_bucket=current_hour_bucket() - timedelta(hours=1))
        self.assertEqual(self.post_create(content="third").status_code, 302)

    def test_config_change_takes_effect_immediately(self):
        """Admin 后台修改限额即时生效（无需重启）。"""
        self.assertEqual(self.post_create(content="a").status_code, 302)
        # 运营者把本小时剩余额度收紧为 1
        SiteConfig.objects.update(max_creates_per_hour=1)
        self.assertEqual(self.post_create(content="b").status_code, 429)
        # 放宽后立即恢复
        SiteConfig.objects.update(max_creates_per_hour=100)
        self.assertEqual(self.post_create(content="c").status_code, 302)

    def test_rate_limit_page_is_429_with_message(self):
        """429 响应包含明确说明。"""
        SiteConfig.objects.update(max_creates_per_hour=1)
        self.post_create(content="a")
        response = self.post_create(content="b")
        self.assertEqual(response.status_code, 429)
        self.assertContains(response, "创建过于频繁", status_code=429)


class IpBanTests(PasteTestBase):
    """IP 封禁规则。"""

    def test_banned_ip_cannot_create(self):
        """被封禁网络创建文本：403。"""
        IpBan.objects.create(network="127.0.0.0/8", reason="测试封禁")
        response = self.post_create(content="blocked")
        self.assertEqual(response.status_code, 403)
        self.assertEqual(
            HourCounter.objects.filter(scope=HourCounter.Scope.CREATE).count(), 0,
            "封禁请求不应进入频率计数（未执行后续逻辑）",
        )

    def test_ban_scope_covers_password_but_not_reading(self):
        """封禁范围：阻止创建与密码验证尝试，但不影响无密码阅读。"""
        IpBan.objects.create(network="127.0.0.0/8")
        # 创建被拒
        self.assertEqual(self.post_create(content="blocked").status_code, 403)
        # 无密码阅读不受封禁影响
        paste = self.make_paste(content="public readable")
        response = self.client.get(f"/p/{paste.id}/", REMOTE_ADDR="127.0.0.1")
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "public readable")

    def test_banned_ip_cannot_attempt_password(self):
        """被封禁网络的密码验证尝试：403，不进入密码验证。"""
        from django.contrib.auth.hashers import make_password

        IpBan.objects.create(network="127.0.0.0/8")
        paste = self.make_paste(
            content="secret content", password_hash=make_password("pw")
        )
        response = self.client.post(
            f"/p/{paste.id}/", {"password": "pw"}, REMOTE_ADDR="127.0.0.1"
        )
        self.assertEqual(response.status_code, 403)
        self.assertNotIn("secret content", response.content.decode())
        paste.refresh_from_db()
        self.assertEqual(paste.read_count, 0)

    def test_cidr_network_ban_matches_members(self):
        """CIDR 网段封禁覆盖网段内任意地址（IPv4 与 IPv6）。"""
        IpBan.objects.create(network="203.0.113.0/25")
        IpBan.objects.create(network="2001:db8:42::/48")
        self.assertTrue(is_banned(ipaddress.ip_address("203.0.113.77")))
        self.assertFalse(is_banned(ipaddress.ip_address("203.0.113.200")))  # 网段外
        self.assertTrue(is_banned(ipaddress.ip_address("2001:db8:42:1::dead")))
        self.assertFalse(is_banned(ipaddress.ip_address("2001:db8:43:1::dead")))

    def test_ban_normalizes_input_network(self):
        """封禁规则保存时自动规范化网络格式（大小写/零压缩/主机位）。"""
        ban = IpBan(network="2001:0DB8:0000:0000:0000:0000:0000:0000/64")
        ban.full_clean()  # 触发 clean() 规范化
        ban.save()
        self.assertEqual(ban.network, "2001:db8::/64")

    def test_ban_invalid_network_rejected_by_clean(self):
        """非法网络格式被 clean() 拒绝。"""
        from django.core.exceptions import ValidationError as ModelValidationError

        ban = IpBan(network="999.999.999.999")
        with self.assertRaises(ModelValidationError):
            ban.full_clean()

    def test_inactive_or_expired_ban_ignored(self):
        """停用或已过期的封禁不再生效。"""
        IpBan.objects.create(network="127.0.0.1", active=False)
        IpBan.objects.create(
            network="10.0.0.1", expires_at=timezone.now() - timedelta(hours=1)
        )
        self.assertFalse(is_banned(ipaddress.ip_address("127.0.0.1")))
        self.assertFalse(is_banned(ipaddress.ip_address("10.0.0.1")))

    def test_ban_read_check_before_rate_limit(self):
        """封禁检查在频率计数之前：被封禁请求不消耗任何配额。"""
        SiteConfig.objects.update(max_creates_per_hour=5)
        IpBan.objects.create(network="127.0.0.1")
        for _ in range(3):
            self.assertEqual(self.post_create(content="x").status_code, 403)
        # 未封禁网络仍可正常用满 5 次
        statuses = [
            self.post_create(content=f"ok{i}", REMOTE_ADDR="192.0.2.9").status_code
            for i in range(5)
        ]
        self.assertEqual(statuses, [302] * 5)


class QuotaCounterTests(PasteTestBase):
    """配额计数器行为。"""

    def test_consume_hour_quota_increments_atomically(self):
        """consume_hour_quota 单线程语义：正确递增并按限额拒绝。"""
        key = "v4:192.0.2.1"
        self.assertTrue(consume_hour_quota("create", key, 2))
        self.assertTrue(consume_hour_quota("create", key, 2))
        self.assertFalse(consume_hour_quota("create", key, 2))
        self.assertEqual(
            HourCounter.objects.get(network_key=key).count, 3
        )

    def test_counter_scopes_are_independent(self):
        """create 与 password 两个 scope 相互独立计数。"""
        key = "v4:192.0.2.1"
        self.assertTrue(consume_hour_quota("create", key, 1))
        self.assertFalse(consume_hour_quota("create", key, 1))
        # password scope 不受影响
        self.assertTrue(consume_hour_quota("password", key, 1))
