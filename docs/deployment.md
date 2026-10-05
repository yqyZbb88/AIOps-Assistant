# 部署与升级

推荐用 Docker Compose 部署完整监控栈。宿主机只需 Docker Engine / Docker Desktop 与
Compose 2.24+；不需要安装 Python、Conda 或项目依赖。Docker Desktop 使用 Linux 容器。
本项目仍采用单 Assistant 进程、单 worker 和 SQLite。

## 首次启动

在项目根目录运行：

```bash
docker compose up -d --build --wait --wait-timeout 180
```

首次需要联网下载镜像和 Python 依赖；180 秒是容器就绪等待时间，不包括镜像构建。
初始化服务 `init` 成功退出后，五个常驻服务按依赖启动。只有健康检查通过才返回成功。
`init` 显示 `Exited (0)` 是正常状态。

打开 `http://127.0.0.1:8000/console`。查看自动生成的管理员 Token：

```bash
docker compose run --rm --no-deps init show-token
```

新部署默认开启普通接口鉴权，Alertmanager 使用独立 Token；SSH 写操作和自动处置不启用。
默认登记 `local-node`，对应栈内 `node-exporter:9100`，不需要填写本机 IP。
它采集的是 Docker Linux 环境，不代表 Windows/macOS 宿主机；实际 Linux 主机请独立部署
Node Exporter，再从管理员页面登记。

Grafana 位于 `http://127.0.0.1:3000`，新部署账号为 `admin`。初始密码：

```bash
docker compose run --rm --no-deps init show-grafana-password
```

该命令显示初始化密码；在 Grafana 页面修改密码后，以 Grafana 数据库中的密码为准。
密钥只在显式查看命令中输出，初始化日志不打印密钥。

## 可选配置：模型、端口与 SSH

不提供模型密钥也能使用监控、巡检、控制台和关键词问答。需要 AI 时，在根目录创建 `.env`：

```env
DEEPSEEK_API_KEY=填写模型密钥
DEEPSEEK_MODEL=deepseek-v4-flash
# 端口冲突时按需修改，不必编辑 compose.yaml。
ASSISTANT_HOST_PORT=8000
GRAFANA_HOST_PORT=3000
PROMETHEUS_HOST_PORT=9090
ALERTMANAGER_HOST_PORT=9093
NODE_EXPORTER_HOST_PORT=9100
```

也可复制 `.env.example` 后填写。Compose 读取根 `.env`，同名进程环境变量优先。
`DEEPSEEK_BASE_URL`、`DEEPSEEK_TIMEOUT`、`DEEPSEEK_MAX_RETRIES`、`DEEPSEEK_MAX_TOKENS`
会一并传入初始化服务。新模板根据密钥是否存在启用 AI；若管理员已经把开关保存为固定
`false`，添加密钥后还需在管理页启用 Agent / 告警 AI 分析。

修改 `.env` 后重新创建服务，使初始化保存的环境配置和 Assistant 进程一致：

```bash
docker compose up -d --build --force-recreate --wait --wait-timeout 180
```

非空的模型/SSH 环境值更新保存的值；留空不清除已保存的密钥。需要撤销密钥时，同时移除
`.env` / 进程中的值和 `.runtime/compose.env` 中对应字段，再执行上面的重建命令。
要临时关闭 AI，直接使用管理页开关即可。

所有发布端口绑定 `127.0.0.1`。远程用户访问应通过带 TLS 的反向代理；不要仅为了连通
而关闭鉴权。`OPS_API_TOKENS` 等根 `.env` 身份配置供本地 Python 模式使用，Compose
使用独立自动生成的身份，不会混用本地 Python 的 Token。

SSH 是可选能力。把私钥和 known_hosts 放入 `.runtime/ssh/`，容器只读路径为 `/run/ssh/`。
在 `.env` 中使用容器路径，例如 `SSH_KEY_FILENAME=/run/ssh/id_ed25519`、
`SSH_KNOWN_HOSTS=/run/ssh/known_hosts`。再在 `.runtime/config/hosts.yaml` 配置凭据引用，
由管理员应用配置并授予必要命令；部署本身不安装远程 Exporter、不授予远程写权限。
Linux 上初始化由容器 root 执行，部署目录可能需要 `sudo` 才能手工编辑；普通使用可通过
控制台和上述 Docker 凭据查看命令完成。不要把私钥目录改成所有人可读。

## 配置与数据位置

| 位置 | 用途 |
|---|---|
| `deploy/config/` | 随版本发布的首次部署模板，不是正在运行的配置 |
| `.runtime/config/` | Compose 实际配置；管理页保存和热更新写这里 |
| `config/` | 本地 Python 开发配置；仅旧部署第一次迁移时复制 |
| `.runtime/compose.env` | 持久化部署凭据和可选模型/SSH 环境值，不再用作 Compose 的 `--env-file` |
| `.runtime/deployment.json` | 初始化来源与完成标记，升级时保留 |
| `.runtime/secrets/` | Assistant 专用派生凭据，仅挂入 Assistant |
| `.runtime/alertmanager/`、`.runtime/grafana/` | 对应服务专用凭据，只读挂载 |
| `.runtime/assistant-data/` | SQLite、WAL 和配置备份 |
| `.runtime/assistant-logs/` | 应用审计与巡检日志 |
| `.runtime/prometheus-targets/` | 动态发现目标；Prometheus 只读，Assistant 可写 |
| `.runtime/textfile/` | 安全演示指标，重复启动不重置已有值 |
| Docker 命名卷 | Prometheus、Alertmanager、Grafana 持久化数据 |

可用 `AIOPS_RUNTIME_DIR` 指定部署目录；所有命令必须使用同一个值。并行部署还应使用不同
Compose 项目名（`-p`）、端口和目录，避免共享数据库与凭据。

初始化仅补充缺失文件，不覆盖已有配置、不轮换有效 Token、不清空数据库。派生凭据丢失
可在保留 `compose.env` 的情况下重新初始化恢复；已完成部署却丢失 `compose.env` 会报错，
必须恢复备份，不能静默生成另一套密钥。

## 日常操作

```bash
docker compose ps --all
docker compose logs --tail 200 assistant init
docker compose down
docker compose up -d --wait --wait-timeout 180
```

`down` 保留部署目录和命名卷。不要把 `down -v` 当成普通停止命令，它会删除监控数据卷。
直接修改 `.runtime/config/*.yaml` 后，在管理员页面点击“应用磁盘配置”；路径、密钥等
需重启字段使用重建命令。模板更新不会覆盖已有部署配置，新版本新增配置应按发布说明合并。

已有 Python 时也可使用便捷入口：

```bash
python scripts/dev_stack.py doctor
python scripts/dev_stack.py up
python scripts/dev_stack.py status
python scripts/dev_stack.py logs
python scripts/dev_stack.py show-token
python scripts/dev_stack.py show-grafana-password
python scripts/dev_stack.py down
```

`doctor` 检查 Docker、Compose 版本、引擎连接和 Compose 配置；不会验证真实 SSH 或模型。
`up` 等待健康检查，失败时返回非零状态并保留数据供排查。查看状态或 Token 不会初始化部署。
`show-token --source local` 仍用于本地 Python 服务，需要该开发环境的 `python-dotenv`。

## 从旧开发栈升级

升级前用旧版本停止服务，备份 `config/`、整个 `.runtime/` 与三个命名卷。备份和恢复细节见
[数据库说明](database.md)。保持原 Compose 项目名，才能继续使用原命名卷。

旧版已存在 `.runtime/compose.env`、但没有初始化标记时，首次启动会：

1. 保留原管理员 Token、Alertmanager Token、Grafana 初始密码及已有数据。
2. 将根 `config/` 的七个 YAML 复制到 `.runtime/config/`，原文件保持不变。
3. 生成各服务专用凭据文件，后续管理页只修改部署副本。

旧配置的鉴权和功能开关保持原值，不会因升级自动开启写操作；请核对 `auth.enabled`。
旧 `.runtime/compose.env` 中自定义的发布端口需搬到根 `.env`；旧 SSH 主机路径需改为容器内
`/run/ssh/` 路径并准备对应文件。不要删除旧凭据来“重试初始化”。

回退应用代码前先停止新版本；若需要恢复旧的挂载方式，应把部署副本中需要保留的变更
合并回旧配置。数据库 schema 不变；更早版本的数据库兼容性仍须按数据库文档核对。

## 部署失败排查

| 现象 | 处理 |
|---|---|
| 无法连接 Docker 引擎 | 启动 Docker Desktop / Docker 服务；Windows 确认 Linux 容器模式 |
| `--wait` 不识别 | 升级 Compose 到 2.24+ |
| 镜像拉取或依赖下载失败 | 检查 Docker 网络、代理和仓库访问；修复后重复原命令 |
| 端口被占用 | 在 `.env` 调整对应 `*_HOST_PORT` 后重新 up |
| `init` 非零退出 | 查看 `docker compose logs init`，修复配置来源或恢复凭据，不要删数据目录 |
| Assistant 健康检查失败 | 查看 Assistant 日志、配置校验和 Prometheus `/ready` 状态 |
| 新主机长时间未被抓取 | 检查动态目标文件、Prometheus Targets 和网络；文件需允许 Prometheus 用户读取 |
| Token 不匹配 | 区分 Compose 与本地 Python 来源；确认终端使用相同项目、端口和部署目录 |

镜像基础版本和第三方服务版本固定；Python 运行依赖通过 `requirements-lock.txt` 约束。
更新依赖时重新运行离线测试及整栈启动检查。构建镜像不包含开发机 `.env`、根 `config/`、
数据库或测试代码；运行镜像不安装 pytest。

实现使用 Compose 的 [等待服务就绪](https://docs.docker.com/reference/cli/docker/compose/up/)
和 Grafana 的 [文件形式凭据](https://grafana.com/docs/grafana/latest/setup-grafana/configure-docker/)。
