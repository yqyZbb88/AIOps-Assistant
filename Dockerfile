FROM node:22-bookworm-slim AS node-tools

FROM python:3.13.13-slim-bookworm AS dependencies
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1
WORKDIR /app
COPY requirements-runtime.txt requirements-lock.txt ./
# 下载缓存仅存在于 BuildKit，不进入镜像；源码变更不触发依赖安装。
# 容器禁止写入字节码，安装时也跳过预编译，减少构建耗时和发布体积。
RUN --mount=type=cache,target=/root/.cache/pip,sharing=locked \
    python -m pip install --no-compile -r requirements-runtime.txt -c requirements-lock.txt && python -m pip check

# 测试依赖也在复制源码之前安装，不随业务代码变更失效。
FROM dependencies AS test-dependencies
COPY requirements.txt ./
RUN --mount=type=cache,target=/root/.cache/pip,sharing=locked \
    python -m pip install --no-compile -r requirements.txt -c requirements-lock.txt && python -m pip check

FROM dependencies AS application
# 生产镜像仅复制发布文件，运行数据和密钥由 Compose 挂载。
COPY src ./src
COPY scripts ./scripts
COPY deploy ./deploy
COPY deploy/templates/assistant ./config
RUN mkdir -p /app/data /app/logs
EXPOSE 8000
HEALTHCHECK --interval=10s --timeout=8s --start-period=30s --retries=6 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/ready', timeout=7)"
ENTRYPOINT ["python", "-m", "scripts.deployment"]
CMD ["serve"]

# 独立回归镜像，测试运行时可禁用网络，无需宿主机 Python/Node 依赖。
FROM test-dependencies AS test
COPY --from=node-tools /usr/local/bin/node /usr/local/bin/node
COPY --from=application /app/ /app/
COPY tests ./tests
ENV LANGSMITH_TRACING=false LANGCHAIN_TRACING=false LANGCHAIN_TRACING_V2=false
HEALTHCHECK NONE
ENTRYPOINT ["python", "-m", "pytest"]
CMD ["-q", "--basetemp=/tmp/aiops-tests"]

# 默认构建目标始终为生产镜像。
FROM application AS runtime
