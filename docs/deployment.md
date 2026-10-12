# 部署与升级

0.6.2 仅提供容器运行方式。宿主机需要 Docker Engine / Docker Desktop 与
Compose 2.24+；Docker Desktop 使用 Linux 容器。无需宿主机 Python、Conda 或项目依赖。
Assistant 保持单进程、单 worker、SQLite schema 7。

## 首次启动

首次连接与问答按 [快速入门](quickstart.md) 操作。在项目根目录执行：

```bash
docker compose up -d --build --wait --wait-timeout 180
docker compose ps --all
docker compose run --rm --no-deps init show-token
```

控制台为 `http://127.0.0.1:8000/console`。初始化生成管理员、Alertmanager Token
和 Grafana 初始密码；初始化日志不输出密钥。Grafana 为 `http://127.0.0.1:3000`，
新部署账号为 `admin`，显式查看初始密码：

```bash
docker compose run --rm --no-deps init show-grafana-password
```

Grafana 页面改密后以其数据库为准。重复初始化保留有效 Token、已有配置、业务数据和演示指标。

## 可选配置：模型与端口

管理员在“系统管理”配置 SSH、用户 Token 和通用模型。SSH 只在该页面录入，支持多份凭据；
已登记主机选择凭据后，用户按照主机访问授权使用它。密码和私钥认证都需要经核验的
known_hosts，每条主机公钥占一行；认证测试不执行命令，也不会自动授予诊断或写权限。

模型支持 OpenAI 兼容的 Chat Completions 接口，完整 Agent 要求标准工具调用。
保存只校验参数，管理员点击“测试已保存的模型连接”才发出请求。
无完整模型配置仍可监控、巡检和关键词问答。

复制 `.env.example` 后可填写以下 Compose 输入，同名进程环境变量优先：

```env
ASSISTANT_HOST_PORT=8000
GRAFANA_HOST_PORT=3000
PROMETHEUS_HOST_PORT=9090
ALERTMANAGER_HOST_PORT=9093
NODE_EXPORTER_HOST_PORT=9100
MODEL_API_KEY=
MODEL_BASE_URL=
MODEL_NAME=
MODEL_TIMEOUT=30
MODEL_MAX_RETRIES=2
MODEL_MAX_TOKENS=2048
MODEL_TOKEN_LIMIT_PARAM=max_tokens
```

Base URL 是接口根地址（自建服务例如 `http://model.example/v1`），不要追加 `/chat/completions`。
输出长度高级选项支持 `max_tokens` 或 `max_completion_tokens`，只发送所选字段。
新部署没有默认模型地址或名称，不会自动请求公共服务。

页面模型配置整组优先于环境配置；清除页面密钥后也不回退环境密钥。
环境模型参数由初始化服务持久化，非空环境值覆盖持久部署值，空值不清除旧值；
撤销环境密钥时同时删除根 `.env`、进程环境和 `.runtime/compose.env` 中的对应值。
页面 Token 轮换和撤销对后续请求生效，身份历史保留。
已有固定关闭开关需在“安全与开关”按需开启 Agent 和 LLM。

修改 Compose 输入或持久部署凭据后重建，普通 restart 不会重新派生凭据：

```bash
docker compose up -d --build --force-recreate --wait --wait-timeout 180
```

发布端口默认绑定部署机 `127.0.0.1`。远程浏览器可使用 SSH 隧道：

```bash
ssh -L 8000:127.0.0.1:8000 ops@server.example
```

按实际发布端口调整目标；长期访问使用带 TLS 的反向代理并保留鉴权。

## 配置与数据位置

| 位置 | 用途 |
|---|---|
| `deploy/templates/assistant/` | 七份首次部署模板，修改模板不覆盖运行配置 |
| `deploy/` 下监控组件目录 | Compose 直接挂载的组件配置 |
| `.runtime/config/` | 实际运行 YAML；管理页保存写这里 |
| 根 `.env` | 可选初始化输入及端口、运行目录；不提供业务登录 Token |
| `.runtime/compose.env` | 持久化部署 Token、角色及可选环境值，不作 Compose 的 --env-file |
| `.runtime/deployment.json` | 初始化状态和配置迁移版本；不是当前配置内容 |
| `.runtime/secrets/` | Assistant 专用派生凭据 |
| `.runtime/alertmanager/`、`.runtime/grafana/` | 各服务专用派生凭据 |
| `.runtime/assistant-data/` | SQLite、配置备份和 admin-credentials 页面凭据 |
| `.runtime/assistant-logs/` | 审计与巡检日志 |
| `.runtime/prometheus-targets/` | 动态发现目标 |
| `.runtime/textfile/` | 容器演示告警合成指标 |
| Docker 命名卷 | Prometheus、Alertmanager、Grafana 数据 |

`AIOPS_RUNTIME_DIR` 可指定目录；全部管理命令必须保持相同目录与项目名。
独立栈同时隔离项目名、端口和运行目录。Prometheus 配置与目标格式见
[配置指南](configuration.md#compose-的-prometheus-主配置与目标列表)。

新部署从模板初始化。已完成部署缺少任何一份 YAML 会拒绝启动，需恢复备份；
已有部署丢失 compose.env 也不会生成新 Token。仅派生凭据丢失可在保留 compose.env
时重新初始化修复。初始化中断保留凭据检查点，可修复原因后重试。

三类文件职责不同：模板只用于首次复制；持久配置是当前实际来源；派生文件由初始化服务
从 `.runtime/compose.env` 单向生成，不能反向同步。手工修改派生文件不会更新来源，
下次初始化还会覆盖派生内容。Grafana 初始密码只在空数据库第一次启动时使用，
页面改密后实际密码在 Grafana 数据库里，更改上述文件不会自动同步数据库。

## 日常操作

```bash
docker compose ps --all
docker compose logs --tail 200 assistant init
docker compose restart assistant
docker compose down
docker compose up -d --wait --wait-timeout 180
```

代码更新后用 `up -d --build --wait`。普通 down 保留数据，`down -v` 会删除监控卷。
手工改 YAML 后由管理员“应用磁盘配置”；数据库/日志路径等启动字段需要重建。
已有 Python 时可选用标准库包装脚本 `python scripts/stack.py`，
支持 doctor、up、down、status、logs、show-token、show-grafana-password、migrate-config；
该脚本只操作 Docker，不能直接启动宿主机服务。doctor 仅检查 Docker/Compose 与部署配置。

## 一次性配置迁移

升级前停止业务栈，备份完整运行目录和监控命名卷，保持原 Compose 项目名、
`AIOPS_RUNTIME_DIR` 及端口输入。宿主机运行包装脚本仅需标准库 Python；
YAML、凭据校验由新镜像中的迁移程序完成，不需要在宿主机安装项目依赖。

```bash
python scripts/stack.py down
python scripts/stack.py migrate-config --dry-run
python scripts/stack.py migrate-config
python scripts/stack.py up
```

命令先检查该项目没有运行服务，构建初始化镜像，再在容器内处理根 `.env`、
`.runtime/compose.env`、七份运行 YAML 和页面凭据。dry-run 只报告候选文件名，不输出值。
正常启动发现旧字段会明确失败并提示该命令，不会隐式升级。
初始化只读挂载项目目录检查根 .env 的旧字段，业务服务不挂载项目源码或自动加载该文件。

迁移映射 `DEEPSEEK_API_KEY/BASE_URL/TIMEOUT/MAX_RETRIES/MAX_TOKENS` 到对应 `MODEL_*`，
`DEEPSEEK_MODEL` 到 `MODEL_NAME`，供应商名改为 `openai_compatible`。
旧版本隐式使用的 DeepSeek 地址和名称转成显式配置；已有页面模型整组保留。
所有迁移输入中的新旧同义字段值相同则合并，不同则停止，只报告字段名；请人工选择后重试。
根 `.env` 与持久环境仍各自保留原来的配置层级，根输入在重新初始化时生效。

旧 `SSH_*` 和命令目录全局 `target` 被删除。已明确登记的连接目标保留；
引用旧环境或 `/run/ssh/` 材料的主机凭据迁入页面凭据存储，继续保留原主机关联和白名单。
无法确定主机关联时，先在运行 hosts.yaml 中明确登记主机及 credential_ref；
缺少材料时，将经核验的 known_hosts、原私钥放到运行目录 ssh/，修正引用后重试。
旧凭据若同时设置密码和私钥，需先明确一种认证方式；带口令的私钥不受支持。
不会扫描主机、信任未知密钥或自动增加授权。
废弃的 `server`、`auth.enabled`、`alert.trend_step`、`app.version` 也只在此次迁移中清理。

所有候选先完整校验，再在 `.runtime/assistant-data/config-migrations/<事务ID>/`
保存受限备份及校验摘要，原子替换文件，并更新 `current.json` 状态。
写入失败恢复原文件；进程中断时启动被阻止，保持停机并重新执行迁移完成恢复和重试。
若中断后有人另改配置，恢复拒绝覆盖人工更改，先对照受限备份核对。
迁移可重复执行。备份含凭据，不要提交或公开；不能删除 pending 标记绕过校验。

部署 Token、身份、主机授权、历史及 Grafana 数据保持原值。普通用户默认没有主机访问权，
管理员显式授权；具名会话仍要求身份及主机权限，匿名会话不自动认领。SQLite schema 仍为 7。
回退前停止服务，恢复对应版本代码和完整运行备份，不要通过删除初始化检查点来重试。

## 部署失败排查

| 现象 | 处理 |
|---|---|
| Docker 引擎不可达 | 启动 Docker 服务；Windows 确认 Linux 容器模式 |
| --wait 不识别 | 使用 Compose 2.24+ |
| 镜像或依赖下载失败 | 核对 Docker 网络、代理与仓库，修复后重试 |
| 端口占用 | 修改根 .env 的对应 HOST_PORT 后重新 up |
| init 退出非零 | 查看 init 日志，恢复缺失配置/凭据，不删业务目录 |
| Assistant 未就绪 | 查看配置校验、Assistant 日志与 Prometheus 状态 |
| 新主机未采集 | 核对目标列表、Prometheus Targets、网络和文件可读性 |
| Token 不匹配 | 核对项目名、运行目录、端口和身份映射；从对应 init 显式查看 |

生产镜像只包含运行文件，不包含宿主机凭据、运行数据或测试；测试使用独立 test 构建目标。
Python 依赖由 requirements-lock.txt 约束，验证命令见 [容器测试与验收](testing.md)。

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
