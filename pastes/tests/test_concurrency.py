"""
并发行为测试（SQLite 文件库 + 多线程）。

验证目标（任务要求）：
- 并发请求不能突破读取次数上限（max_reads=1 时 8 线程并发仅 1 个成功）；
- 并发创建不能突破频率配额；
- 并发密码验证不能突破尝试配额。

测试方式说明：
- 使用 TransactionTestCase（无事务包裹，真实提交）+ threading.Barrier 同步起点；
- 测试数据库为文件型 SQLite（settings.TEST.NAME 指向 .temp/test-db.sqlite3），
  与生产存储引擎一致；内存库无法暴露真实写锁排队行为，故不使用内存库；
- 每个工作线程使用独立 Client 与数据库连接，结束时关闭连接。

已知限制（如实说明，不作强一致性夸大）：
- SQLite 的写事务通过 BEGIN IMMEDIATE + busy timeout 排队执行；
  若并发压力超过 busy timeout（默认 20 秒），个别请求可能报
  database is locked（HTTP 500），属于拒绝服务而非超发——
  计数上限仍然不会被突破，本测试同时断言这一点。
"""
import threading

from django.contrib.auth.hashers import make_password
from django.db import connection
from django.test import Client, TransactionTestCase
from django.utils import timezone
from datetime import timedelta

from pastes.models import Paste, SiteConfig

WORKERS = 8


def _run_threads(worker, count=WORKERS):
    """并发执行 worker，barrier 保证线程尽量同时发出请求。"""
    barrier = threading.Barrier(count)
    results = []
    results_lock = threading.Lock()

    def wrapped(i):
        barrier.wait()
        try:
            status = worker(i)
        except Exception as exc:  # noqa: BLE001 - 记录异常供断言
            status = f"exception:{exc}"
        finally:
            with results_lock:
                results.append(status)
            connection.close()

    threads = [threading.Thread(target=wrapped, args=(i,)) for i in range(count)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=120)
    return results


class ConcurrentReadTests(TransactionTestCase):
    """并发读取不突破 max_reads。"""

    def test_concurrent_reads_cannot_exceed_max_reads_of_one(self):
        """max_reads=1，8 线程并发 GET：恰好 1 个 200，其余 410；读计数=1。"""
        paste = Paste.objects.create(
            content="race condition payload",
            expires_at=timezone.now() + timedelta(hours=1),
            max_reads=1,
        )

        def worker(_):
            client = Client()
            response = client.get(f"/p/{paste.id}/")
            return response.status_code

        results = _run_threads(worker)
        # 无论如何，成功获取正文的请求不能超过 1 个
        self.assertEqual(results.count(200), 1, results)
        # 剩余请求必须全部被明确拒绝（410 耗尽），不得出现成功或异常
        self.assertEqual(results.count(410), WORKERS - 1, results)
        paste.refresh_from_db()
        self.assertEqual(paste.read_count, 1)

    def test_concurrent_reads_within_larger_limit(self):
        """max_reads=3，8 线程并发：成功数恰好 3，读计数=3。"""
        paste = Paste.objects.create(
            content="bounded concurrency",
            expires_at=timezone.now() + timedelta(hours=1),
            max_reads=3,
        )

        def worker(_):
            return Client().get(f"/p/{paste.id}/").status_code

        results = _run_threads(worker)
        self.assertEqual(results.count(200), 3, results)
        self.assertEqual(results.count(410), WORKERS - 3, results)
        paste.refresh_from_db()
        self.assertEqual(paste.read_count, 3)


class ConcurrentCreateTests(TransactionTestCase):
    """并发创建不突破小时配额。"""

    def setUp(self):
        # TransactionTestCase 不经 PasteTestBase.setUp()，必须先确保单例存在，
        # 否则 update() 命中 0 行、限额仍是默认值，测试会伪失败
        config = SiteConfig.load()
        SiteConfig.objects.update(max_creates_per_hour=5)

    def test_concurrent_creates_cannot_exceed_quota(self):
        """限额 5，10 线程同时创建：成功入库的记录数不超过 5。"""

        def worker(i):
            return Client().post(
                "/",
                {"content": f"concurrent-{i}", "expiry": "1d"},
                REMOTE_ADDR="203.0.113.77",
            ).status_code

        results = _run_threads(worker, count=10)
        succeeded = [r for r in results if r == 302]
        self.assertLessEqual(len(succeeded), 5, results)
        self.assertEqual(Paste.objects.count(), len(succeeded))
        # 其余全部被 429 拒绝
        self.assertEqual(results.count(429), 10 - len(succeeded), results)


class ConcurrentPasswordTests(TransactionTestCase):
    """并发密码验证不突破尝试配额，成功验证后按 max_reads 计数。"""

    def setUp(self):
        SiteConfig.load()
        SiteConfig.objects.update(max_password_attempts_per_hour=2)

    def test_concurrent_password_attempts_cannot_exceed_quota(self):
        """尝试配额=2，6 线程并发提交正确密码：成功读正文的次数 ≤2。"""
        paste = Paste.objects.create(
            content="protected race payload",
            password_hash=make_password("race-pw"),
            expires_at=timezone.now() + timedelta(hours=1),
            max_reads=None,
        )

        def worker(_):
            return Client().post(
                f"/p/{paste.id}/",
                {"password": "race-pw"},
                REMOTE_ADDR="203.0.113.88",
            ).status_code

        results = _run_threads(worker, count=6)
        succeeded = results.count(200)
        rejected_429 = results.count(429)
        self.assertLessEqual(succeeded, 2, results)
        self.assertEqual(succeeded + rejected_429, 6, results)
        paste.refresh_from_db()
        self.assertEqual(paste.read_count, succeeded)
