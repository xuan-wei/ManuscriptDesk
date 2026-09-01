FROM python:3.12-slim-bookworm AS builder

ENV PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1
WORKDIR /build
COPY pyproject.toml README.md LICENSE CHANGELOG.md SECURITY.md CONTRIBUTING.md THIRD_PARTY_NOTICES.md ./
COPY src ./src
RUN python -m pip install --upgrade pip build && python -m build --wheel

FROM python:3.12-slim-bookworm AS runtime

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    HOME=/tmp/manuscript-desk-home \
    XDG_CONFIG_HOME=/tmp/manuscript-desk-config \
    XDG_CACHE_HOME=/tmp/manuscript-desk-cache \
    MANUSCRIPT_DESK_CHROMIUM=/usr/bin/chromium \
    MANUSCRIPT_DESK_CHROMEDRIVER=/usr/bin/chromedriver \
    WEB_CONCURRENCY=1

RUN apt-get update \
    && apt-get install --no-install-recommends -y chromium chromium-driver ca-certificates tini \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd --system --gid 10001 manuscript-desk \
    && useradd --system --uid 10001 --gid manuscript-desk --create-home manuscript-desk

COPY --from=builder /build/dist/*.whl /tmp/
RUN python -m pip install --no-cache-dir /tmp/*.whl && rm -f /tmp/*.whl

USER manuscript-desk:manuscript-desk
WORKDIR /home/manuscript-desk
EXPOSE 8008
ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["python", "-m", "manuscript_desk", "--host", "0.0.0.0", "--port", "8008"]
