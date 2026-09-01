# 反向代理与 TLS

Manuscript Desk 默认监听 `127.0.0.1:8008`。建议保持此绑定，并在同一台主机上通过反向代理终止 HTTPS。不要把 8008 端口直接暴露到互联网。

将 `MANUSCRIPT_DESK_PUBLIC_BASE_URL` 设置为准确的外部 origin，不能包含路径，例如 `https://desk.example.com`。生产环境必须保持：

```text
MANUSCRIPT_DESK_COOKIE_SECURE=1
```

## Nginx 示例

```nginx
server {
    listen 443 ssl http2;
    server_name desk.example.com;

    ssl_certificate /etc/letsencrypt/live/desk.example.com/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/desk.example.com/privkey.pem;

    client_max_body_size 2m;

    location / {
        proxy_pass http://127.0.0.1:8008;
        proxy_http_version 1.1;
        proxy_set_header Host $host;
        proxy_set_header X-Forwarded-Host $host;
        proxy_set_header X-Forwarded-Proto https;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_read_timeout 180s;
    }
}

server {
    listen 80;
    server_name desk.example.com;
    return 301 https://$host$request_uri;
}
```

应用只信任来自 `127.0.0.1` 的转发请求头。因此，除非你明确修改并重新审查这条信任边界，否则反向代理应与应用运行在同一台主机上。

## 检查反向代理

```bash
curl --fail https://desk.example.com/api/health
curl -I http://desk.example.com/
```

请确认：

- HTTP 会重定向到 HTTPS；
- TLS 证书有效且证书链完整；
- 身份验证 Cookie 带有 `Secure` 属性；
- 后端 8008 端口无法从公网直接访问；
- 代理传入的 `Host` 和 `X-Forwarded-Proto` 与实际外部地址一致。

用户在快捷指令页面配置的基础网址只用于显示和生成链接。系统不得把该网址用于请求 origin 验证、回调信任、Provider 访问、邀请邮件来源或其他服务端出站请求。
