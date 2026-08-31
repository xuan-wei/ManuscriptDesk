# Manuscript Desk

Manuscript Desk 是一个自托管、多用户的期刊稿件管理面板，可用于追踪投稿状态、保留历史记录、合并跨期刊投稿历程，并通过受控的浏览器自动化更新稿件信息。

> Manuscript Desk 是独立的社区项目，与 Clarivate、ScholarOne、Aries Systems 或 Editorial Manager 均无隶属、授权、背书或赞助关系。

## 发布状态

当前版本为 `0.1.0`，属于 Alpha 测试版。在录入真实期刊账号、凭据或稿件数据前，建议先使用模拟数据演练部署、备份和恢复流程。

## 主要功能

- 多用户账号与管理员邀请机制。
- 按用户隔离稿件、期刊配置、自动化任务和快捷指令。
- 保存稿件状态历史和完整时间轴。
- 手动合并跨期刊、跨轮次的投稿历程。
- 支持全部、进行中、标星和隐藏稿件视图。
- 支持 ScholarOne 等投稿系统的受控浏览器自动更新。
- 支持定时更新和全部、标星、进行中稿件的邮件通知。
- 支持 Star、Update Star、Working 和 Update Working 快捷指令链接。
- SQLite 数据库与 SecretStore 配对备份。

## 使用 Docker 快速部署

环境要求：

- Linux
- Python 3.10 或更高版本
- Docker Engine
- Docker Compose 插件

部署数据应保存在源码仓库之外。

```bash
git clone https://github.com/xuan-wei/ManuscriptDesk.git
cd ManuscriptDesk

python scripts/init_deployment.py /srv/manuscript-desk \
  --public-base-url https://desk.example.com \
  --admin-email admin@example.com

cd /srv/manuscript-desk
docker compose config --quiet
docker compose up -d --build
docker compose ps
```

请将以下参数替换为自己的配置：

- `https://desk.example.com`：用户最终访问系统的 HTTPS 地址。
- `admin@example.com`：首位管理员的邮箱地址。
- `/srv/manuscript-desk`：私有部署目录，可按实际情况调整。

初始化脚本会自动生成：

- 三个相互独立的应用密钥；
- 一个高强度的一次性管理员密码；
- Docker Compose 配置；
- 私有环境变量文件；
- 数据、密钥和备份目录。

首次启动后，从以下文件读取管理员登录信息：

```text
/srv/manuscript-desk/bootstrap.txt
```

使用管理员账号成功登录后，执行：

```bash
python /path/to/ManuscriptDesk/scripts/finalize_bootstrap.py \
  /srv/manuscript-desk

cd /srv/manuscript-desk
docker compose up -d
```

该命令会删除一次性明文密码和初始化资料，并关闭空数据库自动创建功能，避免数据目录意外丢失后静默生成一个全新系统。

服务默认监听：

```text
127.0.0.1:8008
```

如需远程访问，请在服务前配置 HTTPS 反向代理。仅进行本机测试时，可以使用：

```bash
--public-base-url http://127.0.0.1:8008
```

此模式会关闭安全 Cookie，仅适合本机演练，不适合远程公开部署。

更多说明：

- [Docker 部署](docs/docker-deployment.md)
- [反向代理](docs/reverse-proxy.md)
- [备份与恢复](docs/backup-restore.md)
- [版本升级](docs/upgrading.md)
- [配置说明](docs/configuration.md)

## 源码与私有数据边界

本 Git 仓库只包含可公开发布的源码。以下内容不得放入 Git：

- 生产数据库以及 SQLite WAL/SHM 文件；
- SecretStore 数据和长期密钥；
- 真实的 `production.env`；
- 期刊账号或密码；
- Shortcut Token；
- 浏览器配置文件、日志和截图；
- 未公开稿件数据；
- 生产备份。

推荐结构：

```text
ManuscriptDesk/              # 从 GitHub 克隆的公开源码
/srv/manuscript-desk/
├── compose.yml              # 私有 Docker Compose 配置
├── production.env           # 私有环境变量
├── data/                    # 数据库、SecretStore 和长期密钥
└── backups/                 # 数据库与 SecretStore 配对备份
```

只有源码目录属于 Git。初始化脚本会拒绝在源码 Git 工作树内创建部署状态，也不会覆盖一个非空的部署目录。

## 备份原则

Manuscript Desk 的业务状态由以下两部分共同组成：

1. SQLite 数据库；
2. 与该数据库匹配的动态 SecretStore。

两者必须成对备份和恢复。不要直接复制正在运行的 SQLite 数据库文件，因为数据库可能处于 WAL 模式。请使用项目提供的 `scripts/backup_state.py` 在线备份脚本。

长期应用密钥也必须单独安全保存，但不得上传到 Git。

## 本地开发

需要 Python 3.10 或更高版本：

```bash
python -m venv .venv
. .venv/bin/activate
python -m pip install -e '.[dev]'
pytest -q
```

支持以下启动方式：

```bash
python -m manuscript_desk --port 8008
```

或：

```bash
manuscript-desk --port 8008
```

应用启动时需要：

- 三个分别包含至少 32 字节随机内容的独立密钥；
- 一个可写的 SecretStore 目录；
- 一个已有数据库，或者明确启用首次空数据库创建。

具体配置请参阅 [`.env.example`](.env.example) 和 [`docs/configuration.md`](docs/configuration.md)。

## 安全模型

- 所有租户数据访问均绑定 `owner_user_id`。
- 数据库会话只保存哈希值。
- 密码使用 Argon2id 哈希。
- Cookie 登录状态下的修改操作均受 CSRF 保护。
- 原始 Shortcut Token 保存在 SecretStore 中，SQLite 只保存 Token 哈希及必要元数据。
- 期刊凭据仅会提交到经过严格验证的投稿系统来源。
- 数据库和动态 SecretStore 必须配对备份与恢复。
- 当前内嵌调度器部署仅支持一个 Web Worker：

```text
WEB_CONCURRENCY=1
```

如需报告安全问题，请按照 [`SECURITY.md`](SECURITY.md) 中的流程私下提交。报告中不得包含真实凭据、Token、未公开稿件内容或数据库文件。

## 许可证

本项目采用 MIT License，详见 [`LICENSE`](LICENSE)。
