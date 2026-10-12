"""应用配置。"""
from django.apps import AppConfig


class PastesConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "pastes"
    verbose_name = "文本分享"
