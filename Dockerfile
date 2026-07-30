FROM node:22-bookworm-slim

ARG CODEX_VERSION=0.145.0
ENV PYTHONUNBUFFERED=1 \
    CODEX_HOME=/root/.codex \
    RENDER_DATA_DIR=/data

RUN apt-get update \
    && apt-get install -y --no-install-recommends python3 python3-venv ca-certificates \
    && rm -rf /var/lib/apt/lists/* \
    && npm install -g "@openai/codex@${CODEX_VERSION}" \
    && python3 -m venv /opt/venv

ENV PATH="/opt/venv/bin:${PATH}"
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY app.py .
RUN mkdir -p /data /root/.codex

EXPOSE 8080
HEALTHCHECK --interval=15s --timeout=8s --start-period=20s --retries=5 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8080/health', timeout=5)"
CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8080", "--workers", "1"]
