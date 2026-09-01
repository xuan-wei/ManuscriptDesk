# 配置说明

Docker 部署应使用 `scripts/init_deployment.py` 和脚本生成的私有 `production.env`，详见 [Docker 部署](docker-deployment.md)。下面说明相同的应用环境变量，也适用于非容器开发环境。

Manuscript Desk 从环境变量读取配置。真实环境文件必须保存在源码仓库之外，并设置为 `0600`。

## 必需的状态配置

- `MANUSCRIPT_DESK_DB_PATH`：SQLite 数据库路径。应挂载数据库所在目录，而不是只挂载 `.db` 文件，以便 WAL 和 SHM 辅助文件正常工作。
- `MANUSCRIPT_DESK_SECRET_ROOT`：可写的 SecretStore 数据目录，权限应为 `0700`。
- `MANUSCRIPT_DESK_OTP_KEY_FILE`：邮箱验证码 HMAC 长期密钥文件。
- `MANUSCRIPT_DESK_RATE_LIMIT_KEY_FILE`：限流标识 HMAC 长期密钥文件。
- `MANUSCRIPT_DESK_CAPTCHA_KEY_FILE`：验证码签名长期密钥文件。
- `MANUSCRIPT_DESK_PUBLIC_BASE_URL`：由管理员控制的 HTTPS origin，用于应用生成的链接。

三个长期密钥文件必须相互独立，每个至少包含 32 字节随机数据，并以只读方式挂载。不要复用同一个值。

## 首次安装

只有在数据目录确实为空时，才可设置：

```text
MANUSCRIPT_DESK_CREATE_EMPTY_DB=1
```

初始管理员用户名、邮箱和密码文件必须成套配置。优先使用密码文件，不要把明文密码直接写入 Compose 或命令历史。

管理员账号创建且首次登录成功后，运行：

```bash
python scripts/finalize_bootstrap.py /srv/manuscript-desk
```

该脚本会删除部署根目录中的 bootstrap 明文记录，以及 `data/secrets/bootstrap` 中的初始密码文件，并关闭空数据库自动创建。

## Web 与 Cookie

- `MANUSCRIPT_DESK_PUBLIC_BASE_URL` 必须是 origin，不应包含路径、查询参数、片段或用户信息。
- 生产环境应使用 HTTPS，并设置 `MANUSCRIPT_DESK_COOKIE_SECURE=1`。
- 仅本机开发环境可使用 HTTP 和 `MANUSCRIPT_DESK_COOKIE_SECURE=0`。
- 默认监听地址应保持 `127.0.0.1`；如需远程访问，推荐通过同机 HTTPS 反向代理暴露。

## Worker 约束

必须设置：

```text
WEB_CONCURRENCY=1
```

当前调度器和自动化队列 Worker 嵌入 Web 进程。Schema fencing 能提高任务领取正确性，但并不意味着系统支持多 Web Worker 或多个生产调度器。

## Chromium 配置

标准 Docker 镜像已经包含 Chromium 和 ChromeDriver。部署环境可显式设置：

```text
MANUSCRIPT_DESK_CHROMIUM=/usr/bin/chromium
MANUSCRIPT_DESK_CHROMEDRIVER=/usr/bin/chromedriver
HOME=/tmp/manuscript-desk-home
XDG_CONFIG_HOME=/tmp/manuscript-desk-config
XDG_CACHE_HOME=/tmp/manuscript-desk-cache
```

应用会为每次浏览器会话在 `/tmp` 创建隔离且可写的 profile，并在退出后清理。不要为了修复浏览器启动而关闭只读根文件系统、启用 privileged 模式或增加宽泛的宿主机可写挂载。

## SMTP 配置

- 系统 SMTP 由管理员维护，用于账号邀请、验证码和恢复邮件，也可供用户选择用于自动化摘要邮件。
- 用户个人 SMTP 属于各自租户，只能由该用户查看和修改。
- SMTP 密码只写入 SecretStore，SQLite 仅保存秘密引用；页面和 API 不会回显明文密码。
- 普通用户只有在管理员对当前系统 SMTP 配置完成真实成功测试后，才能新选择系统发送通道。
- 修改系统 SMTP 配置会使之前的成功状态变为“需要重新测试”。
- 系统 SMTP 不可用时，自动化任务不会静默回退到个人 SMTP。

## 快捷指令基础网址

每位用户可以在快捷指令页面单独设置一个外部可访问的基础网址。它只用于生成快捷指令链接；服务器绝不能把它当作可信回调地址、Provider 目标、请求 origin、邀请邮件来源或其他服务端出站请求目标。

## 敏感信息边界

以下内容不得加入 Git，也不应粘贴到公开 Issue 或日志中：

- 生产数据库及 WAL/SHM 文件；
- SecretStore 文件与长期密钥；
- 真实 `production.env`；
- SMTP、期刊账号或其他明文凭据；
- 稿件数据、个人邮箱和内部域名；
- 未经检查的生产日志和备份 manifest。
