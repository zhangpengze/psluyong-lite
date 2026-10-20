FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    TZ=Asia/Shanghai \
    DATA_DIR=/app/data

WORKDIR /app

# Pillow 生成缩略图需要 libjpeg / zlib；其余依赖都是纯 Python
RUN apt-get update && apt-get install -y --no-install-recommends \
        libjpeg62-turbo zlib1g curl tzdata \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY server ./server
COPY bot ./bot
COPY core ./core

# 非 root 运行
RUN useradd -m -u 1000 appuser \
    && mkdir -p /app/data \
    && chown -R appuser:appuser /app
USER appuser

EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=10s --start-period=15s --retries=3 \
    CMD curl -fsS http://127.0.0.1:8080/health/ready || exit 1

CMD ["python", "-m", "server.run"]
