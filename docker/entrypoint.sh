#!/bin/sh
# 容器入口：准备数据目录 → 幂等数据库迁移 → 启动主进程。
#
# 主进程默认为 gunicorn（见 Dockerfile CMD），也可替换为管理命令，例如：
#   docker run --rm ghcr.io/coronazero/pastebin:latest python manage.py cleanup_pastes
set -e

# 数据目录（SQLite 持久化卷挂载点，PASTE_DB_PATH 可覆盖默认 /data/db.sqlite3）
mkdir -p "$(dirname "${PASTE_DB_PATH:-/data/db.sqlite3}")"

# 数据库迁移：幂等（已是最新时无操作）。需在运行期执行——写权限属于运行用户，
# 且卷挂载发生在容器启动之后。
python manage.py migrate --noinput

exec "$@"
