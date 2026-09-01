# Docker 部署

推荐的部署方式会将公开源代码与私有运行状态在物理路径上分离。请从干净的源码检出目录运行初始化脚本；不要把数据库、密钥或真实环境变量文件复制进 Git 仓库。

## 环境要求

- Linux 主机，安装 Python 3.10 或更高版本。
- Docker Engine 和 Docker Compose 插件。
- 为 Chromium、SQLite、容器镜像和备份预留足够的内存与磁盘空间。
- 任何可从远程访问的部署都必须配置 HTTPS 反向代理。

应用镜像以 UID/GID `10001:10001` 运行，使用只读根文件系统、丢弃全部 Linux capabilities，并且必须设置 `WEB_CONCURRENCY=1`，因为调度器当前嵌入在 Web 进程中。

## 初始化部署目录

在源码检出目录中运行：

```bash
python scripts/init_deployment.py /srv/manuscript-desk \
  --public-base-url https://desk.example.com \
  --admin-email admin@example.com
```

生成的 Compose 文件包含一个短暂运行且禁用网络的 `state-init` 服务。该服务只使用为私有 bind mount 状态设置 UID/GID 10001 所必需的主机权限，完成后即退出，随后应用服务才会启动。这样无需针对不同主机手动执行 `chown`，也不会把状态目录改成所有用户可读或可写。

命令会创建：

```text
/srv/manuscript-desk/
├── compose.yml
├── production.env
├── bootstrap.txt
├── data/
│   ├── runtime/
│   └── secrets/
│       ├── data/
│       ├── keys/{otp.key,rate.key,captcha.key}
│       └── bootstrap/initial-admin-password
└── backups/
```

三个长期密钥会分别独立生成。私有文件权限为 `0600`，敏感目录权限为 `0700`。`data/` 边界目录仅允许路径穿越（`0711`），部署根目录仍保持私有。明文初始管理员密码只会临时存在于私有的 `bootstrap.txt` 和挂载的 bootstrap 密码文件中，首次登录收尾后会被删除。

## 检查并启动

```bash
cd /srv/manuscript-desk
docker compose config --quiet
docker compose up -d --build
docker compose ps
curl --fail http://127.0.0.1:8008/api/health
```

如果容器没有进入 `healthy` 状态，可查看：

```bash
docker compose logs manuscript-desk
```

在把日志发到公开渠道前，必须先检查其中是否包含敏感数据。

生产环境应先配置 HTTPS，然后使用初始化时传给 `--public-base-url` 的完全相同 origin 访问。仅进行本机演练时，可以使用 `http://127.0.0.1:8008` 初始化；这会刻意关闭 Secure Cookie，不适合任何远程暴露场景。

## 完成首次启动

读取 `/srv/manuscript-desk/bootstrap.txt`，登录并确认管理员账号可正常使用，然后运行：

```bash
python /path/to/source/scripts/finalize_bootstrap.py /srv/manuscript-desk
cd /srv/manuscript-desk
docker compose config --quiet
docker compose up -d
```

收尾脚本会：

- 将 `MANUSCRIPT_DESK_CREATE_EMPTY_DB` 从 `1` 改为 `0`；
- 删除初始管理员用户名和邮箱配置；
- 删除初始密码环境设置及其 bind mount；
- 删除初始密码文件和明文 bootstrap 记录。

容器重新创建后，请再次检查健康状态并重新登录。关闭空数据库自动创建后，如果数据库意外丢失，应用会安全失败，而不是静默创建一套全新的空系统。

## 上线前检查

在录入真实数据前：

1. 确认 `docker compose ps` 显示服务为 `healthy`。
2. 确认服务只通过预期的网络接口访问。
3. 确认 HTTPS origin 与 `MANUSCRIPT_DESK_PUBLIC_BASE_URL` 完全一致。
4. 重启并重新创建容器，确认管理员账号仍然存在。
5. 按照 [备份与恢复](backup-restore.md) 创建并演练一次数据库与 SecretStore 配对备份。
6. 如果有测试账号，执行一次受控更新，确认 Chromium 和 ChromeDriver 工作正常。

不要让两个生产容器或两个调度器同时读写同一套状态目录。
