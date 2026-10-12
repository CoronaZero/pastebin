# Pastebin — 轻量级临时文本分享服务

一个可自托管的临时纯文本分享网站（类似 [fars.ee](https://fars.ee/)）：粘贴文本、拿到链接、到点自动失效。无需注册账号，单服务器即可运行。

## 功能清单

- **创建分享**：网页粘贴纯文本，获得 `https://你的域名/p/<UUIDv4>/` 形式的分享链接
- **大小限制**：正文最大 256 KiB（按 UTF-8 编码后的实际字节数计算，而非字符数）
- **访问密码**（可选）：密码以 PBKDF2-SHA256 哈希存储，数据库不保存明文
- **读取次数限制**（可选）：达到最大成功读取次数后文本自动失效；并发请求无法突破上限
- **有效期**：12 小时 / 1 天 / 7 天 / 30 天（默认 1 天），从创建时刻起算，不因访问延长
- **创建频率限制**：每 IP 网络每自然 UTC 小时最多 20 次（IPv4 /32、IPv6 /64，运营者可在后台调整）
- **IP 封禁**：运营者可在后台添加单 IP 或任意前缀网段（IPv4/IPv6）封禁规则
- **运营后台**：Django Admin 管理频率限额、密码尝试上限、计数保留时长等配置，修改即时生效
- **自动清理**：management command 物理删除过期与耗尽记录（仅回收空间，不承担访问控制）
- **安全响应头**：`Cache-Control: no-store`、`X-Content-Type-Options: nosniff`、`X-Frame-Options: DENY`、`Referrer-Policy: no-referrer`、严格 CSP、`X-Robots-Tag: noindex, nofollow, noarchive, nosnippet`
- **robots.txt**：劝阻遵守规则的爬虫抓取全站

## 技术栈与设计说明

| 组件 | 选择 |
|------|------|
| 语言 | Python 3.10+（开发与测试环境为 3.12.13） |
| 框架 | Django 6.1（服务端渲染，Django Templates） |
| 数据库 | SQLite（唯一数据库，文件持久化） |
| 前端 | 原生 HTML + CSS + 约 50 行 JavaScript（仅用于复制链接） |
| 生产部署 | Gunicorn（WSGI）+ Nginx（反向代理与静态文件） |

不使用 DRF、React/Vue、Redis 等非必要依赖；不要求注册登录即可创建与阅读。

### 架构

```
请求 → Nginx（TLS 终止 / 静态文件 / client_max_body_size）
     → Gunicorn → Django
                  ├─ config/middleware.py    请求体大小预检 + 统一安全响应头
                  ├─ pastes/views.py         视图（创建 / 阅读 / 密码验证）
                  ├─ pastes/forms.py         服务端输入验证（UTF-8 字节数限制）
                  ├─ pastes/services.py      IP 信任边界、网络键规范化、原子配额
                  ├─ pastes/models.py        Paste / SiteConfig / IpBan / HourCounter
                  └─ SQLite（BEGIN IMMEDIATE 写事务 + busy timeout 排队）
```

### 并发正确性（SQLite）

所有涉及“计数”的操作都使用 **数据库端原子操作**，绝不是“先查询、Python 里加一再写回”：

- 读取次数：单条条件 `UPDATE ... SET read_count = read_count + 1 WHERE 未过期 AND 未达上限`，恰好一个请求能更新成功；
- 频率配额：`BEGIN IMMEDIATE` 写事务 + 数据库端自增 + 事务内回读判断。

SQLite 连接配置了 `transaction_mode: IMMEDIATE`（事务开始即取写锁，避免锁升级死锁）与 `timeout: 20`（秒，写冲突在超时内排队）。已通过多线程文件库测试验证：8 线程并发读 `max_reads=1` 的文本，恰好 1 个成功；10 线程并发创建，成功数不超过配额。

## 环境准备

### Python 版本要求

Python **3.10 或更高**（开发与测试于 3.12.13，Django 6.1 最低要求 3.10）。

### 创建虚拟环境并安装依赖

Linux / macOS：

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

Windows (PowerShell)：

```powershell
python -m venv .venv
.venv\Scripts\Activate.ps1
pip install -r requirements.txt
```

> 生产服务器（Linux）还需安装 Gunicorn：`pip install gunicorn`（Windows 无法构建 Gunicorn，故未写入 requirements.txt）。

### 数据库迁移

```bash
python manage.py migrate
```

首次迁移会创建全部数据表（含 Admin 后台表），无需任何手工建表步骤。

### 创建运营者账号（可选，用于后台管理）

```bash
python manage.py createsuperuser
```

普通用户创建/阅读文本完全不需要账号；超级用户仅用于 `/admin/` 运营后台。

## 本地开发

```bash
export PASTE_DEBUG=true        # Windows: $env:PASTE_DEBUG="true"
python manage.py runserver
```

访问 <http://127.0.0.1:8000/> 创建文本，`/admin/` 进入后台。

开发模式下自动追加 `localhost` 到 `ALLOWED_HOSTS`，其余安全配置与生产一致（安全响应头、CSRF、频率限制全部生效）。

## 运行自动化测试

```bash
python manage.py test
```

测试使用独立的文件型 SQLite 测试库（`.temp/test-db.sqlite3`，运行后自动销毁），不会触碰真实数据库；无需设置任何环境变量。当前共 102 个测试，覆盖创建与输入验证、密码、有效期与读取次数、频率限制、IP 封禁、Web 安全（XSS 转义 / CSRF / 请求体上限 / 安全响应头）、Admin 后台与多线程并发行为。

## 运营后台（Django Admin）

登录 `/admin/` 后可管理：

| 后台页面 | 作用 |
|----------|------|
| **站点配置**（单例） | 单网络每小时创建上限（默认 20）、单网络每小时密码尝试上限（默认 30）、频率计数保留时长（默认 48 小时）、密码最大长度（默认 128 字符）。修改后**立即生效**，无需重启 |
| **IP 封禁** | 添加/停用封禁规则：单个 IP（`203.0.113.10`）或 CIDR 网段（`203.0.113.0/24`、`2001:db8::/32`），可设过期时间与备注；保存时自动规范化格式 |
| **分享文本** | 只读审计视图（不展示正文与密码哈希），可删除单条记录用于紧急下线 |
| **小时频率计数** | 只读查看各网络的小时用量，用于人工确认滥用来源后去 IP 封禁添加规则 |

**封禁规则的适用范围**：封禁检查与频率限制作用于**创建文本**和**密码验证尝试**两处（且封禁检查在配额计数之前）；不影响对无密码文本的正常阅读。

**修改创建频率限制的方法**：后台 → 站点配置 → 修改“单网络每小时创建上限” → 保存。按 IP 网络统计（IPv4 单地址、IPv6 /64 前缀），自然 UTC 小时切换自动重置；被拒绝的尝试同样计数。

## 生产环境部署

以下示例假设部署目录为 `/opt/pastebin`，服务用户 `paste`，域名 `paste.example.com`。与本地开发一致使用项目内虚拟环境 `.venv`。

### 1. 准备代码与环境变量

```bash
cd /opt/pastebin
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt gunicorn

cp .env.example .env
# 编辑 .env（见下一节生产配置说明）
python manage.py migrate
python manage.py collectstatic --noinput
python manage.py createsuperuser   # 可选
```

### 2. 生产配置说明（`.env`）

```ini
# 必填。生成方式：python -c "import secrets; print(secrets.token_urlsafe(64))"
PASTE_SECRET_KEY=<64+ 位随机串>

# 调试模式，生产必须 false
PASTE_DEBUG=false

# 允许的主机名（逗号分隔）
PASTE_ALLOWED_HOSTS=paste.example.com

# 可信反向代理跳数：前置一层 Nginx 取 1；无代理（本地开发）取 0
PASTE_PROXY_HOPS=1

# HTTPS 三件套（Nginx 终止 TLS 的标准部署）
PASTE_SECURE_SSL_REDIRECT=true
PASTE_SECURE_COOKIES=true
PASTE_PROXY_SSL_HEADER=true
```

关键项说明：

- **`PASTE_SECRET_KEY`**：用于会话与 CSRF 签名。多 Worker 部署必须所有进程使用同一值，否则 CSRF 验证会被随机击穿。缺失时服务拒绝启动（防误部署）。
- **`PASTE_DEBUG`**：生产必须为 `false`，否则任何异常都会向访问者暴露完整堆栈与配置。
- **`PASTE_ALLOWED_HOSTS`**：防止 Host 头伪造。
- **`PASTE_PROXY_HOPS`**（客户端 IP 获取方式）：`0` = 完全忽略 `X-Forwarded-For`，直接用 TCP 对端地址；`N` = 信任 N 层可信代理，取 XFF 从右数第 N 个地址。**只应与真实代理层数一致**——设大了客户端就能伪造来源 IP 绕过频率限制。开发环境保持 0。
- **`PASTE_DB_PATH`**：SQLite 文件路径（默认项目根 `db.sqlite3`），可指向独立数据目录。

### 3. Gunicorn + systemd

创建 `/etc/systemd/system/pastebin.service`：

```ini
[Unit]
Description=Pastebin (Django + Gunicorn)
After=network.target

[Service]
User=paste
Group=paste
WorkingDirectory=/opt/pastebin
EnvironmentFile=/opt/pastebin/.env
ExecStart=/opt/pastebin/.venv/bin/gunicorn config.wsgi:application \
    --workers 3 \
    --threads 2 \
    --bind 127.0.0.1:8000 \
    --access-logfile - \
    --error-logfile -
Restart=always
RestartSec=3

[Install]
WantedBy=multi-user.target
```

```bash
systemctl daemon-reload
systemctl enable --now pastebin
```

> Worker 数量说明：SQLite 写操作在同一时刻只允许一个写者（BEGIN IMMEDIATE 串行排队，busy timeout 20 秒）。2–4 个 Worker 足以支撑本服务的典型负载；盲目调大 Worker 数只会增加写锁排队，不会提高写入吞吐。

### 4. Nginx 配置示例

`/etc/nginx/sites-available/pastebin.conf`：

```nginx
# HTTP 一律重定向到 HTTPS
server {
    listen 80;
    listen [::]:80;
    server_name paste.example.com;
    return 301 https://$host$request_uri;
}

server {
    listen 443 ssl;
    listen [::]:443 ssl;
    http2 on;
    server_name paste.example.com;

    # 证书（Let's Encrypt 示例；按实际路径修改）
    ssl_certificate     /etc/letsencrypt/live/paste.example.com/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/paste.example.com/privkey.pem;
    ssl_protocols TLSv1.2 TLSv1.3;

    # 请求体上限：略高于应用层的 4 MiB（覆盖 256 KiB 文本最大 URL 编码膨胀）
    client_max_body_size 5m;

    access_log /var/log/nginx/pastebin.access.log;
    error_log  /var/log/nginx/pastebin.error.log;

    # 静态文件由 Nginx 直接服务（collectstatic 输出目录）
    location /static/ {
        alias /opt/pastebin/staticfiles/;
        access_log off;
    }

    location / {
        proxy_pass http://127.0.0.1:8000;
        proxy_set_header Host $host;
        # 每层代理把自己看到的对端追加到 XFF 末尾；应用按 PASTE_PROXY_HOPS 取值
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
    }
}
```

```bash
ln -s /etc/nginx/sites-available/pastebin.conf /etc/nginx/sites-enabled/
nginx -t && systemctl reload nginx
```

### 5. HTTPS 配置建议

- 使用 Let's Encrypt：`certbot --nginx -d paste.example.com`（自动续期）。
- 本项目 `.env` 开启 `PASTE_SECURE_SSL_REDIRECT` + `PASTE_PROXY_SSL_HEADER` 后，Django 自行把 http 请求重定向到 https，并输出 `Strict-Transport-Security`（HSTS，1 年，含子域）。也可以改为只在 Nginx 层加 HSTS 头，二选一即可，不要同时配置造成重复。
- 会话与 CSRF Cookie 已通过 `PASTE_SECURE_COOKIES=true` 限定仅经 HTTPS 传输。

## 定时清理任务

清理命令回收存储空间（**过期/耗尽文本即使未被清理也无法访问**——每次读取时都会实时校验有效期与读取次数）：

```bash
python manage.py cleanup_pastes
```

cron 示例（每小时第 17 分钟执行，`.env` 提供运行所需环境变量）：

```cron
17 * * * * paste cd /opt/pastebin && set -a && . ./.env && set +a && .venv/bin/python manage.py cleanup_pastes >> /var/log/pastebin-cleanup.log 2>&1
```

输出示例：`清理完成：删除 12 条过期/耗尽文本记录，清理 37 条过期频率计数（保留时长 48 小时）。`

- 过期或已耗尽的文本记录被物理删除；
- 频率计数按站点配置的保留时长（默认 48 小时）清理；
- IP 封禁规则**不会**被自动删除，只能由运营者在后台处理。

## SQLite 运维

**文件权限**：

```bash
chown paste:paste /opt/pastebin/db.sqlite3
chmod 600 /opt/pastebin/db.sqlite3
# 数据库所在目录需要可写（SQLite 会创建 -journal/-wal 临时文件）
```

**持久化**：SQLite 数据库就是单个文件，`PASTE_DB_PATH` 指向的路径必须位于持久化卷上且被包含在备份策略内。

**备份**（在线安全备份，服务运行中即可执行）：

```bash
sqlite3 /opt/pastebin/db.sqlite3 ".backup /backup/pastebin-$(date +%F).sqlite3"
```

**与迁移的顺序**：升级代码后先 `python manage.py migrate` 再重启服务；备份先行。

## 日志、故障排查与数据库迁移

- **日志位置**：应用 INFO/ERROR 日志输出到 stderr，由 systemd journal 收集（`journalctl -u pastebin -f`）；Nginx 访问/错误日志见上方配置；清理任务日志见 cron 重定向文件。日志刻意不包含任何密码、正文或密码哈希。
- **`ImproperlyConfigured: 生产环境必须通过环境变量 PASTE_SECRET_KEY...`**：`.env` 未被加载（systemd 的 `EnvironmentFile` 路径错误，或手工启动时未 source）。
- **`DisallowedHost`**：`PASTE_ALLOWED_HOSTS` 缺少访问所用域名。
- **CSRF 验证失败（403，偶发）**：多进程部署时各进程 `PASTE_SECRET_KEY` 不一致，或 Nginx 未正确转发 `Host` 头。
- **登录后台会话频繁丢失**：跨 Worker 会话不一致，同上检查密钥统一性。
- **`database is locked`（HTTP 500）**：写并发超过 busy timeout 的极端情况，属拒绝服务而非数据超发（计数上限不会被突破）。确认定时清理是否在高峰执行、Worker 数是否合理；如频繁出现可适当调小 Worker 数或增加 `settings.py` 中 SQLite 的 `timeout`。
- **数据库迁移**：所有表结构变更都通过 Django migrations 管理——拉取新版本后执行 `python manage.py migrate` 即可；迁移文件位于 `pastes/migrations/`，随仓库版本化。

## 数据保留、备份与删除行为

- 文本记录在**过期或读取次数耗尽**后，于下一次清理任务运行时被物理删除；在此之前一直不可访问。
- **删除数据库记录不等于立即销毁所有历史副本**：操作系统或云平台的磁盘快照、数据库备份文件、日志中的引用等可能仍残留数据。需要彻底清除时应同时处置备份。
- **访问密码不是内容加密**：服务器与能够访问数据库或备份的管理员仍可读取明文正文。密码仅是一道访问门，不能防止服务器被攻破后的内容泄露；敏感内容请自行在客户端加密后再分享。
- 分享链接不是密码：任何知道链接（且未被密码保护）的人都可以阅读内容，且知道链接的人可能通过历史记录（浏览器、聊天工具）留存它。

## 已知限制

1. **SQLite 极端并发**：写事务串行执行；并发压力超过 busy timeout 时个别请求返回 500（拒绝该请求，不会多发）。已通过多线程测试验证上限不被突破，但未宣称支持高并发写入场景。
2. **清理任务是尽力而为**：crontab 停摆只会让过期数据多占空间，不影响访问控制。
3. **频率限制的公平性**：IPv6 /64 会把同一前缀下的所有用户合并计数，NAT 后的 IPv4 用户同理共享配额；这是防滥用设计与误伤之间的折中。
4. **`X-Robots-Tag` 与 robots.txt 只对遵守规范的爬虫有效**，不构成强制性访问控制。
5. **Web 服务器日志会记录访问 IP 与分享 ID**（Nginx access log），运营者需自行决定保留策略。

## 开发参考

```
pastebin/
├── manage.py                     # 管理入口
├── requirements.txt              # 运行依赖
├── .env.example                  # 环境变量示例
├── config/                       # Django 项目配置
│   ├── settings.py               # 环境变量驱动的全部安全边界
│   ├── middleware.py             # 请求体大小预检 + 统一安全响应头
│   ├── urls.py / wsgi.py
├── pastes/                       # 业务应用
│   ├── models.py                 # Paste / SiteConfig / IpBan / HourCounter
│   ├── services.py               # IP 信任边界 / 网络键规范化 / 原子配额
│   ├── forms.py                  # 创建与密码表单验证
│   ├── views.py                  # 视图（创建 / 阅读 / 密码验证 / robots）
│   ├── admin.py                  # 运营后台
│   ├── urls.py
│   ├── management/commands/cleanup_pastes.py
│   ├── migrations/               # Django 迁移（勿手工修改数据库）
│   ├── templates/pastes/         # 页面模板（自动转义，纯文本展示）
│   ├── static/pastes/            # CSS 与复制链接 JS
│   └── tests/                    # 102 个自动化测试
└── templates/                    # 400/403/404/500 错误页
```

## 许可证

见 [LICENSE](LICENSE)。
