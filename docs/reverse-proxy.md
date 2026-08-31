# Reverse proxy and TLS

Manuscript Desk binds to `127.0.0.1:8008` by default. Keep that binding and terminate HTTPS in a reverse proxy on the same host. Do not expose port 8008 directly to the internet.

Set `MANUSCRIPT_DESK_PUBLIC_BASE_URL` to the exact external origin, without a path, for example `https://desk.example.com`. Production deployments must keep `MANUSCRIPT_DESK_COOKIE_SECURE=1`.

## Nginx example

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

The application trusts forwarded headers only from `127.0.0.1`, so keep the proxy on the same host unless you deliberately change and review that trust boundary.

## Verification

```bash
curl --fail https://desk.example.com/api/health
curl -I http://desk.example.com/
```

Verify that HTTP redirects to HTTPS, the certificate is valid, authentication cookies carry `Secure`, and the backend port is not publicly reachable. User-configured Shortcut base URLs are display/link-generation settings only; they must not be treated as request-origin or callback trust.
