FROM node:22-bookworm-slim AS node-tools

FROM python:3.13.13-slim-bookworm AS application
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1
WORKDIR /app
COPY requirements-runtime.txt requirements-lock.txt ./
RUN python -m pip install -r requirements-runtime.txt -c requirements-lock.txt && python -m pip check
# 生产镜像仅复制发布文件，运行数据和密钥由 Compose 挂载。
COPY src ./src
COPY init.py ./
COPY scripts ./scripts
COPY deploy ./deploy
COPY deploy/config ./config
RUN mkdir -p /app/data /app/logs
EXPOSE 8000
HEALTHCHECK --interval=10s --timeout=8s --start-period=30s --retries=6 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/ready', timeout=7)"
ENTRYPOINT ["python", "scripts/deployment.py"]
CMD ["serve"]

# 独立回归镜像，测试运行时可禁用网络，无需宿主机 Python/Node 依赖。
FROM application AS test
COPY --from=node-tools /usr/local/bin/node /usr/local/bin/node
COPY requirements.txt ./
RUN python -m pip install -r requirements.txt -c requirements-lock.txt && python -m pip check
COPY tests ./tests
ENV LANGSMITH_TRACING=false LANGCHAIN_TRACING=false LANGCHAIN_TRACING_V2=false
HEALTHCHECK NONE
ENTRYPOINT ["python", "-m", "pytest"]
CMD ["-q", "--basetemp=/tmp/aiops-tests"]

# 默认构建目标始终为生产镜像。
FROM application AS runtime
