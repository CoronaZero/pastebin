# syntax=docker/dockerfile:1

# ============================================================================
# 构建阶段：在独立层安装 Python 依赖
# 最终镜像不携带 pip 缓存与安装过程残留
# ============================================================================
# 基础镜像选型说明：
# - 不用 debian:latest —— 不含 Python，需 apt 安装（体积相当但版本随 Debian 漂移，
#   且 python3-venv/pip 需另装，构建不可复现）；
# - 不用 python:3.12-alpine —— musl libc 与生产常用 glibc 环境存在行为差异，
#   且对 SQLite 并发写锁行为的验证均在 glibc 上完成；
# - python:3.12-slim = Debian slim 底座 + 官方 CPython 3.12（与开发环境一致），
#   轻量与一致性兼顾。
FROM python:3.12-slim AS builder

ENV PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

COPY requirements.txt .
# gunicorn 仅在 Linux/容器可用，与 requirements.txt 分开固定版本
RUN pip install -r requirements.txt gunicorn==23.0.0

# ============================================================================
# 运行阶段
# ============================================================================
FROM python:3.12-slim

LABEL org.opencontainers.image.title="pastebin" \
      org.opencontainers.image.description="轻量级临时文本分享服务（Django + SQLite）" \
      org.opencontainers.image.source="https://github.com/CoronaZero/pastebin" \
      org.opencontainers.image.licenses="MIT"

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PATH="/opt/venv/bin:$PATH" \
    # SQLite 默认落在持久化卷（可用 PASTE_DB_PATH 覆盖）
    PASTE_DB_PATH=/data/db.sqlite3

# 非 root 运行用户（固定 uid 1000，便于宿主机对挂载卷 chown 1000:1000）
RUN useradd --system --uid 1000 --user-group appuser

WORKDIR /app

COPY --from=builder /opt/venv /opt/venv
COPY manage.py .
COPY config config/
COPY pastes pastes/
COPY templates templates/
COPY LICENSE README.md ./
COPY docker/entrypoint.sh /usr/local/bin/entrypoint.sh

# 构建期收集静态文件（gzip/br 预压缩，运行期由 WhiteNoise 直接服务，
# 单容器部署无需 Nginx）。
# SECRET_KEY 仅为通过启动校验的临时占位值，collectstatic 不产生任何需签名的状态。
RUN PASTE_SECRET_KEY=collectstatic-only \
    python manage.py collectstatic --noinput \
 && chmod +x /usr/local/bin/entrypoint.sh \
 && mkdir -p /data && chown appuser:appuser /data

USER appuser
VOLUME /data
EXPOSE 8000

# 健康检查：HTTP 栈 + SQLite 读路径（/healthz 访问数据库），不依赖 curl
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD ["python", "-c", "import sys,urllib.request; r=urllib.request.urlopen('http://127.0.0.1:8000/healthz', timeout=4); sys.exit(0 if r.status==200 else 1)"]

ENTRYPOINT ["/usr/local/bin/entrypoint.sh"]
# gunicorn 参数可用 GUNICORN_CMD_ARGS 环境变量覆盖（gunicorn 原生支持），如：
#   -e GUNICORN_CMD_ARGS="--workers 4 --threads 4"
# SQLite 写事务串行化（BEGIN IMMEDIATE + busy timeout），少量 worker 即可
CMD ["gunicorn", "config.wsgi:application", "--bind", "0.0.0.0:8000", "--workers", "2", "--threads", "2", "--access-logfile", "-", "--error-logfile", "-"]
