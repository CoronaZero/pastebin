"""
Django Admin 后台。

后台是运营者管理界面，需先用 `python manage.py createsuperuser` 创建账号。
普通用户创建/阅读文本无需任何账号，Admin 与业务功能完全解耦。

注册内容：
- SiteConfig   站点配置单例：频率限额、密码尝试上限、计数保留时长等，改后即时生效
- IpBan        IP 封禁规则的增删改查
- Paste        分享记录只读审计（不展示正文与密码哈希），允许删除用于紧急下线
- HourCounter  频率计数只读查看，便于运营者确认需要封禁的网络

安全约定：
- 后台不展示、不提供编辑正文内容（content）与密码哈希（password_hash），
  避免运营侧泄露与 CSRF/日志等次生风险；
- 管理员仍可经数据库直接查看存储内容（服务不对正文做端到端加密，README 已说明）。
"""
from django.contrib import admin
from django.urls import path
from django.http import HttpResponseRedirect
from django.utils.html import format_html

from .models import HourCounter, IpBan, Paste, SiteConfig


@admin.register(SiteConfig)
class SiteConfigAdmin(admin.ModelAdmin):
    """站点配置：全局单例，仅允许编辑既有的唯一记录。"""

    list_display = (
        "max_creates_per_hour",
        "max_password_attempts_per_hour",
        "rate_retention_hours",
        "password_max_length",
        "updated_at",
    )

    def has_add_permission(self, request) -> bool:
        # 已存在唯一实例时禁止新增，避免多行配置导致读取歧义
        return not SiteConfig.objects.exists()

    def has_delete_permission(self, request, obj=None) -> bool:
        return False

    def changelist_view(self, request, extra_context=None):
        """唯一记录场景：列表页直接跳到编辑页。"""
        if request.method == "GET" and not request.GET:
            obj = SiteConfig.objects.first()
            if obj is not None:
                return HttpResponseRedirect(f"change/{obj.pk}/")
        return super().changelist_view(request, extra_context)


@admin.register(IpBan)
class IpBanAdmin(admin.ModelAdmin):
    """IP 封禁：运营者手动添加规则，保存时自动规范化网络格式。"""

    list_display = ("network", "reason", "active", "is_active_now", "created_at", "expires_at")
    list_filter = ("active",)
    search_fields = ("network", "reason")
    readonly_fields = ("created_at",)
    list_per_page = 50

    @admin.display(boolean=True, description="当前生效")
    def is_active_now(self, obj: IpBan) -> bool:
        return obj.is_active_now


@admin.register(Paste)
class PasteAdmin(admin.ModelAdmin):
    """分享记录：只读审计视图，允许删除（紧急下线），不暴露正文与密码哈希。"""

    list_display = (
        "link",
        "created_at",
        "expires_at",
        "read_count",
        "max_reads",
        "has_password",
    )
    list_filter = ("created_at",)
    search_fields = ("id",)
    list_per_page = 50
    actions = None  # 禁用批量操作，避免误删

    fields = (  # 详情页字段：刻意不含 content 与 password_hash
        "id",
        "created_at",
        "expires_at",
        "read_count",
        "max_reads",
        "has_password",
    )
    readonly_fields = fields

    @admin.display(description="分享 ID")
    def link(self, obj: Paste):
        return format_html("<code>{}</code>", obj.id)

    @admin.display(boolean=True, description="有密码")
    def has_password(self, obj: Paste) -> bool:
        return obj.has_password

    def has_add_permission(self, request) -> bool:
        return False  # 后台不创建业务数据

    def has_change_permission(self, request, obj=None) -> bool:
        return False  # 全只读，仅允许查看详情

    def has_delete_permission(self, request, obj=None) -> bool:
        return True  # 允许紧急下线


@admin.register(HourCounter)
class HourCounterAdmin(admin.ModelAdmin):
    """频率计数：只读查看，用于人工确认滥用网络后到 IpBan 中添加封禁。"""

    list_display = ("scope", "network_key", "hour_bucket", "count")
    list_filter = ("scope",)
    search_fields = ("network_key",)
    list_per_page = 50

    def has_add_permission(self, request) -> bool:
        return False

    def has_change_permission(self, request, obj=None) -> bool:
        return False

    def has_delete_permission(self, request, obj=None) -> bool:
        return True  # 允许运营者手工清理异常计数
