"""
Django 全局配置。

配置双通道（优先级从高到低）：
1. 真实环境变量 —— Docker（docker run -e / --env-file / compose）与 systemd
   EnvironmentFile 注入的标准方式；
2. `.env` 配置文件（项目根目录，或 PASTE_ENV_FILE 指定的路径）—— 面向直接部署，
   未通过环境变量提供的项才会从文件读取，文件中同样支持 `KEY=VALUE` 行与 # 注释。

设计约定：
- 部署级安全边界（SECRET_KEY / DEBUG / ALLOWED_HOSTS / 可信代理跳数）必须通过
  环境变量或 .env 文件提供，不能放进 Admin 后台，否则会形成“用后台改后台安全边界”的循环依赖。
- 运营者日常可调参数（创建频率限额、密码尝试限额、计数保留时长、密码长度上限、
  IP 封禁规则）存放在数据库中，通过 Django Admin 后台修改，即时生效。
"""
import os
import secrets
import sys
from pathlib import Path

from django.core.exceptions import ImproperlyConfigured

BASE_DIR = Path(__file__).resolve().parent.parent


def _load_env_file() -> None:
    """从 .env 文件加载配置项到环境变量（不覆盖已存在的值）。

    仅支持最基础的 `KEY=VALUE` 格式：每行一条，# 开头为注释，忽略空行；
    值可用成对的单引号或双引号包裹（包裹时去除引号）。刻意不实现变量展开、
    多行等高级语法，避免引入 python-dotenv 依赖与解析歧义。

    查找路径：环境变量 PASTE_ENV_FILE 指定的文件；否则项目根目录的 .env。
    文件不存在时静默跳过（本地开发可不提供）。
    """
    path = os.environ.get("PASTE_ENV_FILE") or str(BASE_DIR / ".env")
    try:
        with open(path, encoding="utf-8") as handle:
            lines = handle.readlines()
    except OSError:
        return  # 不存在或不可读：保持纯环境变量行为
    for raw in lines:
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
            value = value[1:-1]
        if key and key not in os.environ:
            os.environ[key] = value


_load_env_file()


def _env_str(name: str, default: str | None = None) -> str | None:
    """读取环境变量，空白值视为未设置。"""
    value = os.environ.get(name)
    if value is None or value.strip() == "":
        return default
    return value


def _env_bool(name: str, default: bool = False) -> bool:
    value = _env_str(name)
    if value is None:
        return default
    return value.strip().lower() in ("1", "true", "yes", "on")


def _env_int(name: str, default: int) -> int:
    value = _env_str(name)
    if value is None:
        return default
    try:
        return int(value)
    except ValueError as exc:
        raise ImproperlyConfigured(f"环境变量 {name} 必须是整数，当前值: {value!r}") from exc


# ---------------------------------------------------------------------------
# 核心安全边界
# ---------------------------------------------------------------------------

DEBUG = _env_bool("PASTE_DEBUG", default=False)

# 测试模式（manage.py test）：自动生成随机密钥，免设环境变量。
# 测试无需跨进程稳定的 SECRET_KEY（无会话/CSRF 跨重启状态），
# 而生产环境仍强制要求显式提供 PASTE_SECRET_KEY。
_RUNNING_TESTS = len(sys.argv) > 1 and sys.argv[1] == "test"

SECRET_KEY = _env_str("PASTE_SECRET_KEY")
if not SECRET_KEY:
    if DEBUG or _RUNNING_TESTS:
        # 开发模式：每次启动随机生成，本服务不依赖跨重启的会话/CSRF 状态，可接受。
        SECRET_KEY = secrets.token_urlsafe(64)
    else:
        raise ImproperlyConfigured(
            "生产环境必须通过环境变量 PASTE_SECRET_KEY 提供随机密钥，"
            "生成方式: python -c \"import secrets; print(secrets.token_urlsafe(64))\""
        )
ALLOWED_HOSTS = [
    host.strip() for host in (_env_str("PASTE_ALLOWED_HOSTS", "") or "").split(",") if host.strip()
]
if DEBUG:
    ALLOWED_HOSTS += ["localhost", "127.0.0.1", "[::1]"]

# ---------------------------------------------------------------------------
# 应用定义
# ---------------------------------------------------------------------------

INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "pastes",
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    # WhiteNoise：让应用自身也能服务静态文件（单容器/无 Nginx 部署时 Admin 资源可用；
    # 若前置 Nginx 服务静态文件，则由 Nginx 优先处理，互不冲突）
    "whitenoise.middleware.WhiteNoiseMiddleware",
    # 请求体大小预检：在最早期拒绝超大请求，避免后续中间件读入内存
    "config.middleware.RequestBodySizeMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    # 统一设置安全响应头（Cache-Control / nosniff / CSP / X-Robots-Tag 等）
    "config.middleware.SecurityHeadersMiddleware",
    # X-Frame-Options: DENY 由 Django 内置中间件负责
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]

ROOT_URLCONF = "config.urls"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [BASE_DIR / "templates"],  # 放置 400/403/404/500 等错误页覆盖模板
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.debug",
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
            ],
        },
    },
]

WSGI_APPLICATION = "config.wsgi.application"

# ---------------------------------------------------------------------------
# 数据库（SQLite，唯一数据库）
# ---------------------------------------------------------------------------

# 测试专用数据库固定使用文件型 SQLite（放在 .temp/ 下），
# 而不是默认的内存库：内存库无法暴露真实文件库的写锁排队与并发行为，
# 并发正确性必须在与生产一致的存储引擎上验证。
TEST_DB_DIR = BASE_DIR / ".temp"
os.makedirs(TEST_DB_DIR, exist_ok=True)

DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.sqlite3",
        "NAME": _env_str("PASTE_DB_PATH", str(BASE_DIR / "db.sqlite3")),
        "OPTIONS": {
            # BEGIN IMMEDIATE：事务一开始即获取写锁，
            # 避免多个连接"先读后写"时锁升级造成 database is locked 死锁
            "transaction_mode": "IMMEDIATE",
            # SQLite busy 超时（秒）：并发写请求在此时间内排队等待，而不是立刻失败
            "timeout": 20,
        },
        "TEST": {
            "NAME": str(TEST_DB_DIR / "test-db.sqlite3"),
        },
    }
}

# ---------------------------------------------------------------------------
# 时间：全部使用 UTC
# ---------------------------------------------------------------------------

TIME_ZONE = "UTC"
USE_TZ = True

# ---------------------------------------------------------------------------
# 输入大小限制
# ---------------------------------------------------------------------------

# 应用层请求体上限（原始字节）。
# 背景：256 KiB 的 UTF-8 文本经 URL 编码提交时体积最多膨胀约 3 倍（约 0.75 MiB），
# 4 MiB 可覆盖最坏情况并留足余量；配合 Nginx client_max_body_size 使用。
PASTE_MAX_REQUEST_BODY_BYTES = 4 * 1024 * 1024
DATA_UPLOAD_MAX_MEMORY_SIZE = PASTE_MAX_REQUEST_BODY_BYTES

# ---------------------------------------------------------------------------
# 客户端 IP 信任边界
# ---------------------------------------------------------------------------

# 0 = 不信任任何 X-Forwarded-For；N = 经 N 层可信代理时取 XFF 从右数第 N 个地址
PASTE_PROXY_HOPS = _env_int("PASTE_PROXY_HOPS", default=0)

# ---------------------------------------------------------------------------
# HTTPS 与 Cookie 安全（生产建议开启）
# ---------------------------------------------------------------------------

SECURE_SSL_REDIRECT = _env_bool("PASTE_SECURE_SSL_REDIRECT", default=False)
PASTE_SECURE_COOKIES = _env_bool("PASTE_SECURE_COOKIES", default=False)
SESSION_COOKIE_SECURE = PASTE_SECURE_COOKIES
CSRF_COOKIE_SECURE = PASTE_SECURE_COOKIES
SECURE_HSTS_SECONDS = 31536000 if PASTE_SECURE_COOKIES else 0
SECURE_HSTS_INCLUDE_SUBDOMAINS = True
SECURE_HSTS_PRELOAD = False

# TLS 由前置 Nginx 终止时，需信任其传递的 X-Forwarded-Proto 以识别 HTTPS。
# 置为 true 时启用 SECURE_PROXY_SSL_HEADER（配合 PASTE_SECURE_SSL_REDIRECT 与 HSTS）。
if _env_bool("PASTE_PROXY_SSL_HEADER", default=False):
    SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")

# ---------------------------------------------------------------------------
# 日志：统一输出到 stderr，由 Gunicorn 捕获；不记录密码、正文或哈希
# ---------------------------------------------------------------------------

LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "formatters": {
        "simple": {"format": "{asctime} {levelname} {name} {message}", "style": "{"},
    },
    "handlers": {
        "console": {
            "class": "logging.StreamHandler",
            "formatter": "simple",
        },
    },
    "root": {"handlers": ["console"], "level": "INFO"},
}

# ---------------------------------------------------------------------------
# 密码哈希：使用 Django 内置 PBKDF2-SHA256（无需第三方依赖）
# ---------------------------------------------------------------------------

PASSWORD_HASHERS = [
    "django.contrib.auth.hashers.PBKDF2PasswordHasher",
]

# ---------------------------------------------------------------------------
# 静态文件
# ---------------------------------------------------------------------------

STATIC_URL = "static/"
STATIC_ROOT = BASE_DIR / "staticfiles"  # collectstatic 输出目录

# 静态文件收集时生成 gzip/br 预压缩副本，配合 WhiteNoise 直接服务（无需 Nginx）
STORAGES = {
    "default": {
        "BACKEND": "django.core.files.storage.FileSystemStorage",
    },
    "staticfiles": {
        "BACKEND": "whitenoise.storage.CompressedStaticFilesStorage",
    },
}

# ---------------------------------------------------------------------------
# 国际化与语言（界面为中文）
# ---------------------------------------------------------------------------

LANGUAGE_CODE = "zh-hans"

# ---------------------------------------------------------------------------
# 默认主键
# ---------------------------------------------------------------------------

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

# ---------------------------------------------------------------------------
# 响应头：禁止浏览器嗅探与被嵌入（X-Frame-Options 由内置中间件设置）
# ---------------------------------------------------------------------------

X_FRAME_OPTIONS = "DENY"
