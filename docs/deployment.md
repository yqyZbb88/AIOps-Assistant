# 部署与升级

推荐用 Docker Compose 部署完整监控栈。宿主机只需 Docker Engine / Docker Desktop 与
Compose 2.24+；不需要安装 Python、Conda 或项目依赖。Docker Desktop 使用 Linux 容器。
本项目仍采用单 Assistant 进程、单 worker 和 SQLite。

## 首次启动

首次部署的启动命令、状态判断、管理员 Token 获取、控制台连接和第一次问答，
统一按 [快速入门](quickstart.md) 操作。本页负责可选配置、重建、升级和数据保留。
旧部署首次启动前先阅读 [升级步骤](#从旧开发栈升级)，不要覆盖原配置或删除凭据。

Grafana 位于 `http://127.0.0.1:3000`，新部署账号为 `admin`。初始密码：

```bash
docker compose run --rm --no-deps init show-grafana-password
```

该命令显示初始化密码；在 Grafana 页面修改密码后，以 Grafana 数据库中的密码为准。
密钥只在显式查看命令中输出，初始化日志不打印密钥。

## 可选配置：模型、端口与 SSH

不提供模型密钥也能使用监控、巡检、控制台和关键词问答。需要 AI 时，可在“系统管理 → 模型配置”保存 DeepSeek 参数并显式测试连接，按需打开 LLM/Agent 开关。
也可沿用环境配置，在根目录创建 `.env`：

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
会一并传入初始化服务。
模型参数默认值与约束：`DEEPSEEK_TIMEOUT=30`（有限正数，单位秒）、
`DEEPSEEK_MAX_RETRIES=2`（非负整数，0 表示不重试）、`DEEPSEEK_MAX_TOKENS=2048`
（正整数）。非法参数会在模型初始化时被拒绝，按现有降级路径关闭模型能力。
SDK 请求超时与重试不构成整轮 Agent 的总时间上限；一轮可能包含多个模型及工具调用。

新模板根据密钥是否存在启用 AI；若管理员已经把开关保存为固定
`false`，添加密钥后还需在管理页启用 Agent / 告警 AI 分析。

修改 `.env` 后重新创建服务，使初始化保存的环境配置和 Assistant 进程一致：

```bash
docker compose up -d --build --force-recreate --wait --wait-timeout 180
```

非空的模型/SSH 环境值更新保存的值；留空不清除已保存的密钥。需要撤销密钥时，同时移除
`.env` / 进程中的值和 `.runtime/compose.env` 中对应字段，再执行上面的重建命令。
要临时关闭 AI，直接使用管理页开关即可。

如果部署在远程服务器上，工作站浏览器的 `127.0.0.1` 指向工作站自身。
可在工作站终端建立 SSH 隧道，把地址替换为有权限登录的测试或部署服务器：

```bash
ssh -L 8000:127.0.0.1:8000 ops@server.example
```

保持该终端连接，再从工作站打开 `http://127.0.0.1:8000/console`。
如果调整了 Assistant 发布端口，隧道的远程目标端口也要相应调整。

所有发布端口绑定 `127.0.0.1`。需要对外提供长期访问时应通过带 TLS 的反向代理；不要仅为了连通
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
| `.runtime/assistant-data/` | SQLite、WAL、配置备份与 admin-credentials 页面凭据 |
| `.runtime/assistant-logs/` | 应用审计与巡检日志 |
| `.runtime/prometheus-targets/` | 动态发现目标；Prometheus 只读，Assistant 可写 |
| `.runtime/textfile/` | 安全演示指标，重复启动不重置已有值 |
| Docker 命名卷 | Prometheus、Alertmanager、Grafana 持久化数据 |

可用 `AIOPS_RUNTIME_DIR` 指定部署目录；所有命令必须使用同一个值。并行部署还应使用不同
Compose 项目名（`-p`）、端口和目录，避免共享数据库与凭据。

Prometheus 主配置和动态目标列表使用不同的 YAML 格式与生效方式；路径映射、示例、
重载命令与新增主机流程见 [Prometheus 主配置与目标列表](configuration.md#compose-的-prometheus-主配置与目标列表)。

初始化仅补充缺失文件，不覆盖已有配置、不轮换有效 Token、不清空数据库。派生凭据丢失
可在保留 `compose.env` 的情况下重新初始化恢复；凭据写入后即记录检查点，初始化中断也可保留同一套凭据重试。
已记录凭据或旧部署配置的目录丢失 `compose.env` 会报错，必须恢复备份，不能静默生成另一套密钥。

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

## 本地 Python 启动

本地 Python 适合开发或使用已有 Prometheus，不会自动启动监控栈。
如果只是首次体验，请优先使用 [快速入门](quickstart.md) 的 Compose 路径。
使用 Python 3.13，在项目根目录安装开发依赖：

```bash
python -m pip install -r requirements.txt -c requirements-lock.txt
python -m pip check
```

建议使用独立虚拟环境。已有 `langchain1.2` Conda 环境时可先激活，再执行上述命令。
Linux 若没有 `python` 命令，可以使用 `python3`。

仅在根目录还没有 `.env` 时复制样例，已有文件保留并按需补充字段：

```bash
cp .env.example .env
```

PowerShell 使用 `Copy-Item .env.example .env`。填写 `PROMETHEUS_URL`，并为本地服务设置
有效的 `OPS_API_TOKENS` 身份映射和管理员身份；随机密钥生成与映射写法见
[本地鉴权配置](auth.md#四配置-api-鉴权)。模型密钥与 SSH 凭据按需要填写，留空不会自动获得这些能力。

本地服务读取根 `config/`。核对 `settings.yaml` 的开关、`hosts.yaml` 的 ID 与
Prometheus `job`/`instance`、`metrics.yaml` 的指标范围。SSH 诊断/写操作还需命令目录和
逐主机授权。普通鉴权是否开启按现有配置决定；管理接口始终要求管理员身份，
不能因为 `auth.enabled=false` 就留空管理员 Token 映射。

以单 worker 启动：

```bash
uvicorn src.main:app --host 127.0.0.1 --port 8000 --workers 1
```

也可执行 `python src/main.py`，此入口使用配置文件中的监听地址与端口。
打开 `http://127.0.0.1:8000/console`，使用本地环境的管理员 Token 连接，并选择实际登记的主机。
本地 Python 不使用 Compose 自动生成的 Token；若不记得本地映射中的管理员 Token，可显式查看：

```bash
python scripts/dev_stack.py show-token --source local
```

在部署机上检查 `/ready`；PowerShell 使用 `curl.exe`，Bash 使用 `curl`：

```bash
curl http://127.0.0.1:8000/ready
```

就绪失败时核对外部 Prometheus 地址和日志，不要把 `/health` 返回 200 当成依赖可用。
本地 `.env` 密钥或进程环境变化后重启 Python 服务；YAML 热应用和重启边界见
[配置生效说明](configuration.md#十一字段生效与重启)。

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

## 页面凭据持久化与 Linux 宿主机监控

页面配置保存到 `.runtime/assistant-data/admin-credentials/`，重建 Assistant 或重跑 init
均保留。该目录包含敏感 state.json 和 SSH 版本资产，不进入普通 YAML 备份或数据库历史；
随整个运行目录通过受控加密渠道备份。恢复必须同时恢复 state.json 与 assets，并在停机
状态下操作；不要将凭据目录放入代码仓库。旧资产为旧请求保留，清理须在服务停止并核对
当前引用后进行。容器权限仍使用原部署边界，未增加默认 host PID/network 或根目录挂载。

Compose 的 `local-node` 继续代表容器环境。要监控独立 Linux 宿主机，在该宿主机部署
node_exporter（可信来源、固定版本），以独立 systemd 服务运行，例如二进制位于
`/usr/local/bin/node_exporter`：

```ini
[Unit]
Description=Linux node_exporter
After=network.target
[Service]
User=node_exporter
Group=node_exporter
ExecStart=/usr/local/bin/node_exporter --web.listen-address=:9100
Restart=on-failure
NoNewPrivileges=true
ProtectSystem=strict
ProtectHome=true
[Install]
WantedBy=multi-user.target
```

由宿主机管理员安装服务账户、核验二进制并启用服务；9100 仅允许 Prometheus 访问，
按实际安全边界配置 TLS/认证或可信内网。确保 Prometheus 容器可访问宿主机的实际网络
地址；不要将容器内 127.0.0.1 当作宿主机。自管模式在“监控主机”填写可达地址:9100 并
登记；外部模式先在外部 Prometheus 配置 scrape target，等待发现后再选目标登记。
核对 /targets 的 up、标签 job/instance 和 17 项即时/趋势状态，再关联 SSH 并单独授权。
本次不自动安装远程 Exporter，也不扩大 Compose 容器权限。
