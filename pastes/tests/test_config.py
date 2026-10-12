"""配置层测试：.env 文件加载、静态资源缓存例外、健康检查端点。"""
import os
import uuid
from pathlib import Path

from django.conf import settings
from django.http import HttpResponse
from django.test import RequestFactory, TestCase

from config.middleware import SecurityHeadersMiddleware


class EnvFileLoaderTests(TestCase):
    """.env 配置文件加载器（config.settings._load_env_file）行为。

    优先级约定：真实环境变量 > .env 文件值 > 代码默认值。
    """

    def setUp(self):
        from config import settings as settings_module

        self.loader = settings_module._load_env_file
        self.env_file = Path(settings.BASE_DIR) / ".temp" / f"test-env-{uuid.uuid4().hex}.env"
        self.env_file.parent.mkdir(exist_ok=True)
        self._keys = []

    def tearDown(self):
        for key in self._keys:
            os.environ.pop(key, None)
        os.environ.pop("PASTE_ENV_FILE", None)
        self.env_file.unlink(missing_ok=True)

    def track(self) -> str:
        key = f"PASTE_TEST_UNIQ_{uuid.uuid4().hex[:8]}"
        self._keys.append(key)
        return key

    def write(self, content: str):
        self.env_file.write_text(content, encoding="utf-8")
        os.environ["PASTE_ENV_FILE"] = str(self.env_file)

    def test_file_value_used_when_env_absent(self):
        """未通过环境变量设置时，从 .env 文件读取。"""
        key = self.track()
        self.write(f"{key}=from-file\n")
        self.loader()
        self.assertEqual(os.environ.get(key), "from-file")

    def test_existing_env_wins_over_file(self):
        """已存在的环境变量不被 .env 文件覆盖（环境变量优先）。"""
        key = self.track()
        os.environ[key] = "from-env"
        self.write(f"{key}=from-file\n")
        self.loader()
        self.assertEqual(os.environ[key], "from-env")

    def test_quotes_stripped_and_comments_ignored(self):
        """引号包裹的值去除引号、内部空白字面保留；无引号值去首尾空白；注释/空行忽略。"""
        quoted = self.track()
        bare = self.track()
        spaces = self.track()
        self.write(
            f"{quoted}='single quoted'\n"
            f"  {bare}=\"inner  spaces kept\"\n"
            f"{spaces}=  plain value  \n"
            "\n"
            "# 这是注释行\n"
            "PASTE_TEST_MALFORMED_NO_EQUALS\n"
        )
        self.loader()
        self.assertEqual(os.environ.get(quoted), "single quoted")
        # 引号内空白按字面保留（dotenv 语义：引号 = 取字面值）
        self.assertEqual(os.environ.get(bare), "inner  spaces kept")
        # 无引号值两侧空白被去除
        self.assertEqual(os.environ.get(spaces), "plain value")
        # 注释与无等号行不产生任何键
        self.assertNotIn("PASTE_TEST_MALFORMED_NO_EQUALS", os.environ)

    def test_missing_file_is_silent_noop(self):
        """PASTE_ENV_FILE 指向不存在的文件时静默跳过，不抛异常。"""
        os.environ["PASTE_ENV_FILE"] = str(self.env_file)  # 尚未写入，不存在
        self.loader()  # 不应抛出异常
        self.assertFalse(self.env_file.exists())


class StaticCacheHeaderTests(TestCase):
    """安全响应头中间件对静态资源路径的缓存策略例外。"""

    def _middleware(self):
        def get_response(request):
            response = HttpResponse("body")
            response.headers["Cache-Control"] = "public, max-age=315360000, immutable"
            return response

        return SecurityHeadersMiddleware(get_response)

    def test_static_path_keeps_upstream_cache_control(self):
        """/static/** 保留 WhiteNoise/上游设置的 Cache-Control，不被强制 no-store。"""
        request = RequestFactory().get("/static/pastes/style.css")
        response = self._middleware()(request)
        self.assertEqual(response.headers["Cache-Control"], "public, max-age=315360000, immutable")
        # 其余安全头依然生效
        self.assertEqual(response.headers["X-Content-Type-Options"], "nosniff")

    def test_page_paths_forced_no_store(self):
        """非静态路径（含 404 等）必须 no-store。"""
        for path in ["/", "/p/00000000-0000-0000-0000-000000000000/", "/admin/login/"]:
            request = RequestFactory().get(path)
            response = self._middleware()(request)
            self.assertEqual(response.headers["Cache-Control"], "no-store", path)


class HealthEndpointTests(TestCase):
    """/healthz 健康检查端点。"""

    def test_healthz_returns_ok_plain_text(self):
        response = self.client.get("/healthz")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.content, b"ok\n")
        self.assertEqual(response.headers["Content-Type"], "text/plain; charset=utf-8")

    def test_healthz_carries_security_headers(self):
        """健康检查响应同样带安全头（无缓存、不索引）。"""
        response = self.client.get("/healthz")
        self.assertEqual(response.headers["Cache-Control"], "no-store")
        self.assertEqual(response.headers["X-Robots-Tag"], "noindex, nofollow, noarchive, nosnippet")
        self.assertEqual(response.headers["X-Frame-Options"], "DENY")
