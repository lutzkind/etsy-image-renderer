FROM node:22-bookworm-slim

ARG CODEX_VERSION=0.145.0
ENV PYTHONUNBUFFERED=1 \
    CODEX_HOME=/tmp/etsy-codex-home \
    CODEX_AUTH_SOURCE=/run/secrets/codex-auth.json \
    RENDER_DATA_DIR=/data

RUN apt-get update \
    && apt-get install -y --no-install-recommends python3 python3-venv ca-certificates curl util-linux \
    && rm -rf /var/lib/apt/lists/* \
    && npm install -g "@openai/codex@${CODEX_VERSION}" \
    && python3 -m venv /opt/venv

ENV PATH="/opt/venv/bin:${PATH}"
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY app.py openai_fallback.py ./
COPY runtime-entrypoint.sh /usr/local/bin/etsy-renderer-entrypoint
COPY codex-skills/imagegen /opt/codex-system-skills/imagegen
RUN groupadd --system --gid 10001 etsy-renderer \
    && useradd --system --uid 10001 --gid 10001 --home-dir /home/etsy-renderer --create-home --shell /usr/sbin/nologin etsy-renderer \
    && mkdir -p /data /home/etsy-renderer /tmp/etsy-codex-home \
    && chown -R 10001:10001 /data /home/etsy-renderer /tmp/etsy-codex-home \
    && chown 10001:0 /tmp/etsy-codex-home \
    && chmod 0770 /tmp/etsy-codex-home \
    && chmod 0555 /usr/local/bin/etsy-renderer-entrypoint

EXPOSE 8080
HEALTHCHECK --interval=15s --timeout=8s --start-period=20s --retries=5 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8080/health', timeout=5)"
ENTRYPOINT ["/usr/local/bin/etsy-renderer-entrypoint"]
CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8080", "--workers", "1"]
