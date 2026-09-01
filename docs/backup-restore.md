# 备份与恢复

一套完整的 Manuscript Desk 运行状态由 SQLite 数据库和与之匹配的动态 SecretStore 组成。两者必须一起备份、一起恢复。长期应用密钥也必须单独安全保管，但它们属于部署配置，因此不会包含在本脚本生成的动态 SecretStore 备份中。

不要直接复制正在运行的 SQLite `.db` 文件。SQLite 可能处于 WAL 模式，直接复制可能得到不一致的数据库；请使用项目提供的在线备份脚本。

## 创建备份

```bash
cd /srv/manuscript-desk
docker compose stop manuscript-desk
docker run --rm \
  --user "10001:10001" \
  -v /srv/manuscript-desk/data/runtime:/runtime \
  -v /srv/manuscript-desk/data/secrets/data:/secrets:ro \
  -v /srv/manuscript-desk/backups:/backups \
  -v /path/to/source/scripts/backup_state.py:/backup_state.py:ro \
  manuscript-desk:local python /backup_state.py \
  --database /runtime/manuscripts.db \
  --secrets /secrets \
  --output /backups \
  --label production
docker compose up -d
```

备份脚本使用 SQLite 在线备份 API，因此技术上可以在服务运行时安全备份；上述流程仍建议先停止应用，以减少状态变化，并获得更容易理解的即时一致运维快照。

辅助容器应使用能够读取私有状态的 UID/GID（标准部署中为 `10001:10001`）。如果宿主机当前用户无法读取这些目录，请不要放宽文件权限；应改用受控的辅助容器，以正确 UID/GID 执行备份。

备份脚本会：

- 使用 SQLite 在线备份 API；
- 执行 `integrity_check` 和外键检查；
- 拒绝 SecretStore 中的符号链接、不支持的文件类型，以及权限过宽的秘密文件；
- 在私有 manifest 中记录 SHA-256、Schema 信息和秘密文件清单。

还应使用加密且访问受控的系统，单独备份：

```text
/srv/manuscript-desk/data/secrets/keys
```

不要把长期密钥、数据库、SecretStore 或任何备份加入 Git。

## 恢复演练

1. 停止所有可能写入恢复目标的服务。
2. 在另一个仅本机监听的端口上创建全新、空白且私有的部署目录。
3. 把同一个备份目录中的 `manuscripts.db` 复制到目标的 `data/runtime/`。
4. 把该备份目录中的 `secrets/` 树复制到目标的 `data/secrets/data/`。
5. 安装与该状态集匹配的长期密钥。
6. 将所有权设置为 `10001:10001`，目录权限设置为 `0700`，私有文件权限设置为 `0600`。
7. 保持 `MANUSCRIPT_DESK_CREATE_EMPTY_DB=0`。
8. 启动恢复服务，检查健康状态、登录、稿件记录和依赖 SecretStore 的配置。

## 恢复原则

- 不要把一次备份的数据库与另一次备份的 SecretStore 混用。
- 不要只恢复数据库而遗漏 SecretStore 或长期密钥。
- 不要让旧版本应用读取已经迁移到更新 Schema 的数据库。
- 正式恢复前应保留故障状态副本，以便后续诊断。
- 恢复成功后，应再次执行数据库完整性和外键检查，并验证至少一个依赖秘密配置的受控操作。
