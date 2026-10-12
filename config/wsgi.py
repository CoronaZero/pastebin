"""WSGI 入口（Gunicorn / uWSGI 等生产服务器使用）。"""
import os

from django.core.wsgi import get_wsgi_application

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")

application = get_wsgi_application()
