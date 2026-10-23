# 精简版 Dockerfile：不跑 apt-get，pip 只用清华源（不走 pypi.org）。
#
# 为什么去掉 apt 依赖：
#   libjpeg62-turbo / zlib1g —— Pillow 的 manylinux wheel 自带 libjpeg/zlib
#   tzdata                   —— requirements.txt 里的 tzdata 包提供 zoneinfo 数据
#   curl                     —— 只被健康检查用，改用 python 自带 urllib
#
# 为什么不加 --extra-index-url https://pypi.org/simple：
#   加了之后 pip 会把 pypi.org 的候选也算进来，最终从 files.pythonhosted.org 下载，
#   国内实测只有 13.8 kB/s，4.6MB 的 SQLAlchemy 直接读超时（exit code 2）。
#   只留清华源，并在清华源缺包时用重试兜底。

FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    TZ=Asia/Shanghai \
    DATA_DIR=/app/data \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_DEFAULT_TIMEOUT=120

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir \
        --retries 10 \
        -i https://pypi.tuna.tsinghua.edu.cn/simple \
        -r requirements.txt

COPY server ./server
COPY bot ./bot
COPY core ./core

RUN useradd -m -u 1000 appuser \
    && mkdir -p /app/data \
    && chown -R appuser:appuser /app
USER appuser

EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
  CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8080/health/ready',timeout=3).status==200 else 1)"

CMD ["python", "-m", "server.run"]
