"""
表单与服务端输入验证。

所有输入（正文、密码、读取次数、有效期）都在服务端重新验证，
不依赖前端 HTML 校验。大小限制按 UTF-8 编码后的实际字节数计算，
而不是 Python 字符串长度。
"""
from datetime import timedelta

from django import forms
from django.core.exceptions import ValidationError

from .models import SiteConfig

# 文本内容上限：256 KiB = 262144 字节（UTF-8 编码后）
MAX_CONTENT_BYTES = 256 * 1024

# 读取次数上限（防荒谬数值，如超长整型消耗存储）
MAX_MAX_READS = 10_000

# 有效期选项：键 → 说明；timedelta 映射见 EXPIRY_DELTAS
EXPIRY_CHOICES = [
    ("12h", "12 小时"),
    ("1d", "1 天"),
    ("7d", "7 天"),
    ("30d", "30 天"),
]
EXPIRY_DELTAS = {
    "12h": timedelta(hours=12),
    "1d": timedelta(days=1),
    "7d": timedelta(days=7),
    "30d": timedelta(days=30),
}
DEFAULT_EXPIRY = "1d"


def _check_utf8_encodable(value: str, field_label: str) -> None:
    """拒绝包含无法编码为 UTF-8 的非法字符（如孤立代理项）。"""
    try:
        value.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise ValidationError(f"{field_label}包含无效的字符。") from exc


class PasteCreateForm(forms.Form):
    """创建文本表单。"""

    content = forms.CharField(
        label="文本内容",
        widget=forms.Textarea(
            attrs={
                "rows": 14,
                "placeholder": "在此粘贴或输入要分享的纯文本…",
                "aria-describedby": "content-help",
            }
        ),
        required=True,
        strip=False,  # 保留用户输入的原始换行与空白，不能被自动修剪
        error_messages={"required": "请输入要分享的文本内容。"},
    )
    password = forms.CharField(
        label="访问密码（可选）",
        widget=forms.PasswordInput(
            attrs={"autocomplete": "new-password", "placeholder": "留空表示无需密码"}
        ),
        required=False,
        error_messages={"required": "请输入访问密码。"},
    )
    max_reads = forms.IntegerField(
        label="最大成功读取次数（可选）",
        min_value=1,
        max_value=MAX_MAX_READS,
        required=False,
        widget=forms.NumberInput(attrs={"min": 1, "max": MAX_MAX_READS, "placeholder": "留空不限制"}),
        error_messages={
            "min_value": "读取次数至少为 1。",
            "max_value": f"读取次数过大，最大 {MAX_MAX_READS}。",
            "invalid": "读取次数必须是整数。",
        },
    )
    expiry = forms.ChoiceField(
        label="有效期",
        choices=EXPIRY_CHOICES,
        initial=DEFAULT_EXPIRY,
        widget=forms.Select,
        error_messages={"required": "请选择有效期。", "invalid_choice": "无效的有效期选项。"},
    )

    def __init__(self, *args, config: SiteConfig | None = None, **kwargs):
        super().__init__(*args, **kwargs)
        # 密码长度上限由站点配置驱动（Admin 后台可调）
        config = config or SiteConfig.load()
        self.fields["password"].max_length = config.password_max_length

    def clean_content(self) -> str:
        content: str = self.cleaned_data.get("content", "")
        if not content.strip():
            raise ValidationError("文本内容不能为空。")
        _check_utf8_encodable(content, "文本内容")
        # 按 UTF-8 编码后的实际字节数限制，而非字符数
        if len(content.encode("utf-8")) > MAX_CONTENT_BYTES:
            raise ValidationError(
                f"文本过大：UTF-8 编码后不能超过 {MAX_CONTENT_BYTES // 1024} KiB。"
            )
        return content

    def clean_password(self) -> str:
        password: str = self.cleaned_data.get("password", "")
        if password:
            _check_utf8_encodable(password, "密码")
        return password

    @property
    def expiry_timedelta(self) -> timedelta:
        return EXPIRY_DELTAS[self.cleaned_data["expiry"]]


class PastePasswordForm(forms.Form):
    """密码验证表单。"""

    password = forms.CharField(
        label="访问密码",
        widget=forms.PasswordInput(
            attrs={"autocomplete": "off", "autofocus": "autofocus"}
        ),
        required=True,
        error_messages={"required": "请输入访问密码。"},
    )

    def __init__(self, *args, config: SiteConfig | None = None, **kwargs):
        super().__init__(*args, **kwargs)
        config = config or SiteConfig.load()
        self.fields["password"].max_length = config.password_max_length

    def clean_password(self) -> str:
        password: str = self.cleaned_data.get("password", "")
        _check_utf8_encodable(password, "密码")
        return password
