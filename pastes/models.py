"""
数据模型。

- Paste        分享文本（UUIDv4 主键、正文、密码哈希、有效期、读取计数）
- SiteConfig   运营配置单例（Admin 后台可改，即时生效）
- IpBan        IP 封禁规则（运营者在 Admin 后台手动维护）
- HourCounter  自然 UTC 小时频率计数（创建频率 / 密码尝试限速共用）

设计要点：
- 访问密码只存哈希（Django PBKDF2-SHA256），绝不存明文；
- 文本记录中不保存创建者 IP，IP 计数独立存储（HourCounter）；
- 频率计数按规范化网络键（IPv4 /32、IPv6 /64）聚合，天然小时窗口切换即重置。
"""
import ipaddress
import uuid

from django.core.exceptions import ValidationError
from django.core.validators import MaxValueValidator, MinValueValidator
from django.db import models
from django.utils import timezone


class Paste(models.Model):
    """一份临时分享文本。"""

    id = models.UUIDField(
        primary_key=True,
        default=uuid.uuid4,  # 密码学安全随机源（os.urandom）生成 UUIDv4
        editable=False,
        verbose_name="分享 ID",
    )
    content = models.TextField(verbose_name="正文内容")
    password_hash = models.CharField(
        max_length=255,
        blank=True,
        default="",
        verbose_name="访问密码哈希",
        help_text="仅存 PBKDF2 哈希，不存明文；空表示无需密码",
    )
    created_at = models.DateTimeField(auto_now_add=True, verbose_name="创建时间")
    expires_at = models.DateTimeField(db_index=True, verbose_name="过期时间")
    read_count = models.PositiveIntegerField(
        default=0, verbose_name="成功读取次数"
    )
    max_reads = models.PositiveIntegerField(
        null=True,
        blank=True,
        verbose_name="最大成功读取次数",
        help_text="留空表示不限制",
    )

    class Meta:
        verbose_name = "分享文本"
        verbose_name_plural = "分享文本"

    def __str__(self) -> str:
        return f"Paste {self.id}"

    @property
    def has_password(self) -> bool:
        return bool(self.password_hash)

    @property
    def is_expired(self) -> bool:
        """有效期到点即失效：当前时间达到或超过过期时间时不可访问。"""
        return timezone.now() >= self.expires_at

    @property
    def is_read_exhausted(self) -> bool:
        return self.max_reads is not None and self.read_count >= self.max_reads


class SiteConfig(models.Model):
    """运营配置（全局唯一单例）。

    所有字段均由 Django Admin 后台维护，修改后立即生效（每次请求读取）。
    部署级安全边界（SECRET_KEY、DEBUG、ALLOWED_HOSTS、代理跳数）不在此处，
    由环境变量控制，见 config/settings.py。
    """

    # 单例哨兵：唯一约束保证全表仅一行
    singleton = models.BooleanField(default=True, unique=True, editable=False)

    max_creates_per_hour = models.PositiveIntegerField(
        default=20,
        validators=[MinValueValidator(1), MaxValueValidator(10_000)],
        verbose_name="单网络每小时创建上限",
        help_text="每个 IP 网络（IPv4 /32、IPv6 /64）在每个自然 UTC 小时内允许的最大创建次数",
    )
    max_password_attempts_per_hour = models.PositiveIntegerField(
        default=30,
        validators=[MinValueValidator(1), MaxValueValidator(10_000)],
        verbose_name="单网络每小时密码尝试上限",
        help_text="每个 IP 网络在每个自然 UTC 小时内允许的密码验证请求数（无论成败均计数），用于防暴力猜测与 CPU 耗尽",
    )
    rate_retention_hours = models.PositiveIntegerField(
        default=48,
        validators=[MinValueValidator(1), MaxValueValidator(24 * 365)],
        verbose_name="频率计数保留时长（小时）",
        help_text="超过该时长的频率计数记录将被清理命令物理删除",
    )
    password_max_length = models.PositiveIntegerField(
        default=128,
        validators=[MinValueValidator(16), MaxValueValidator(1024)],
        verbose_name="访问密码最大长度（字符）",
        help_text="服务端验证密码长度上限，防止超长输入消耗哈希资源",
    )
    updated_at = models.DateTimeField(auto_now=True, verbose_name="更新时间")

    class Meta:
        verbose_name = "站点配置"
        verbose_name_plural = "站点配置"

    def __str__(self) -> str:
        return "站点配置"

    @classmethod
    def load(cls) -> "SiteConfig":
        """读取全局配置单例，不存在时以默认值创建。

        get_or_create 内部使用保存点处理并发插入冲突，多进程安全。
        """
        config, _ = cls.objects.get_or_create(singleton=True)
        return config


class IpBan(models.Model):
    """IP 封禁规则（运营者在 Admin 后台手动添加）。

    network 存储规范化网络（任意前缀长度），如:
      192.0.2.1/32（单 IPv4）、203.0.113.0/24（IPv4 段）、
      2001:db8::/32（IPv6 段）、2001:db8:1:2::/64（IPv6 /64 段）
    封禁范围：创建文本与密码验证尝试；不影响已知晓链接的正常阅读。
    """

    network = models.CharField(
        max_length=64,
        unique=True,
        verbose_name="IP / 网段",
        help_text="单个 IP 或 CIDR 网段，如 203.0.113.10 或 2001:db8::/32，保存时自动规范化",
    )
    reason = models.CharField(max_length=200, blank=True, default="", verbose_name="备注")
    active = models.BooleanField(default=True, verbose_name="启用")
    created_at = models.DateTimeField(auto_now_add=True, verbose_name="创建时间")
    expires_at = models.DateTimeField(
        null=True,
        blank=True,
        verbose_name="过期时间",
        help_text="留空表示长期有效，直到手动删除或停用",
    )

    class Meta:
        verbose_name = "IP 封禁"
        verbose_name_plural = "IP 封禁"
        ordering = ["-created_at"]

    def __str__(self) -> str:
        return self.network

    def clean(self) -> None:
        """保存前规范化并校验网络格式。"""
        try:
            net = ipaddress.ip_network(self.network.strip(), strict=False)
        except ValueError as exc:
            raise ValidationError({"network": f"无效的 IP 或网段: {exc}"}) from exc
        self.network = str(net)

    def matches(self, addr) -> bool:
        """判断给定 ipaddress 地址对象是否命中本规则。"""
        try:
            net = ipaddress.ip_network(self.network)
        except ValueError:
            return False
        return addr.version == net.version and addr in net

    @property
    def is_active_now(self) -> bool:
        if not self.active:
            return False
        if self.expires_at is not None and timezone.now() >= self.expires_at:
            return False
        return True


class HourCounter(models.Model):
    """自然 UTC 小时频率计数。

    scope:
      - create   创建文本尝试
      - password 密码验证尝试
    network_key: 规范化网络键（v4:1.2.3.4 / v6:2001:db8:abcd:5678::）
    hour_bucket: 所属自然 UTC 小时的起点（整点），窗口切换即自然重置
    """

    class Scope(models.TextChoices):
        CREATE = "create", "创建文本"
        PASSWORD = "password", "密码验证"

    scope = models.CharField(max_length=16, choices=Scope.choices)
    network_key = models.CharField(max_length=64, db_index=True)
    hour_bucket = models.DateTimeField(db_index=True)
    count = models.PositiveIntegerField(default=0)

    class Meta:
        verbose_name = "小时频率计数"
        verbose_name_plural = "小时频率计数"
        constraints = [
            models.UniqueConstraint(
                fields=["scope", "network_key", "hour_bucket"],
                name="uniq_scope_network_hour",
            )
        ]
        indexes = [
            models.Index(fields=["hour_bucket"]),  # 加速清理命令按时间批量删除
        ]

    def __str__(self) -> str:
        return f"{self.scope}/{self.network_key}@{self.hour_bucket:%Y-%m-%dT%H}={self.count}"
