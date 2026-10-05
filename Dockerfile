FROM python:3.13.13-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1
WORKDIR /app
COPY requirements-runtime.txt requirements-lock.txt ./
RUN python -m pip install -r requirements-runtime.txt -c requirements-lock.txt && python -m pip check
# 仅复制发布文件，不把本机配置和凭据带入镜像。
COPY src ./src
COPY init.py ./
COPY scripts ./scripts
COPY deploy ./deploy
COPY deploy/config ./config
RUN mkdir -p /app/data /app/logs
EXPOSE 8000
HEALTHCHECK --interval=10s --timeout=8s --start-period=30s --retries=6 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/ready', timeout=7)"
CMD ["uvicorn", "src.main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1"]
