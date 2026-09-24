# AIOps-Assistant

AIOps-Assistant 是面向多台 Linux 主机、单 Prometheus/Alertmanager 和单 worker 的轻量
智能运维助手。它把 Prometheus/Alertmanager 的事实数据、白名单 SSH 诊断、人工审批写操作、周期巡检和
SQLite 历史记录组织成一条可审计的运维链路。

新接手项目建议按 **README → [配置指南](docs/configuration.md)
→ [API 参考](docs/api.md) → [数据库说明](docs/database.md)** 阅读；本地若有被忽略的
`ARCHITECTURE.md`、`PROJECT_HANDOVER.md` 和 `roadmap.md`，再结合它们看当前验收背景。日常操作从
[控制台说明](docs/console.md) 和 [只读排障](docs/troubleshooting.md) 开始。

当前版本：`0.5.0`

`0.5.0` 已完成多主机监控、巡检、诊断及目标明确的异常处置；`0.6.0` 的日常入口、
交互式图形化控制台和管理员配置页已经实现，用户已反馈浏览器页面无问题。
基础稳定性和部署恢复验证尚未开始。
规则自动处置与 Agent 人工审批执行均为核心能力，前者继续默认关闭。
项目继续使用 SQLite；多 worker、高可用、审批跨重启续跑和跨主机批量编排暂不纳入。
最终交付采用 GHCR 版本化 Assistant 镜像加 Compose Release 部署包；同时提供在线包和
包含全部镜像的 Linux amd64 离线包，目标主机只需 Docker Engine 与 Docker Compose。

## 能力与边界

当前已实现：

- AI 多轮对话：模型按配置调用指标、告警、巡检和诊断工具
- 静态主机注册表：稳定主机 ID、精确 Prometheus 目标、SSH 凭据引用和逐主机能力授权
- 多主机隔离：会话、指标、告警、巡检、AI 上下文、诊断和执行均绑定明确主机
- 统一指标目录：内置 CPU、Memory、Disk、Network、System、Alerts 共 17 个指标
- Alertmanager webhook：接收告警、执行规则匹配，并分析未匹配告警
- 固定指标巡检：周期查询、等级判定、异常归档和独立巡检日志
- SSH 只读诊断：只执行管理员配置的命令模板和受控参数
- 两类写操作：规则自动处置，以及必须人工审批的 Agent 写命令
- Bearer Token 鉴权：Token 映射为身份，用于会话归属、审批授权和 webhook 隔离
- SQLite 持久化：保存会话、告警、处置、AI 建议、审批决策、异常巡检和动作执行状态
- 数据时效：所有提供给 Agent 的工具结果都标明观测或生成时间
- 统一脱敏：日志、审计、SQLite、API、模型和 SSH 输出执行递归与自由文本脱敏
- 健康探针：`/health` 只读缓存状态，`/ready` 主动检查 Prometheus
- 开发部署栈：固定版本的 Assistant、Prometheus、Alertmanager、Node Exporter 和 Grafana
- 图形化控制台：`/console` 多视图入口，覆盖主机、问答诊断、审批、结构化记录与浏览器级个性化
- 管理员控制台：独立最高权限身份管理安全开关、规则、三类命令目录与逐主机授权

当前适合：少量 Linux 主机、单 Prometheus、单 Alertmanager、单 worker、内网和少量用户。

当前不支持：

- 动态资产发现和主机级 RBAC
- 多 worker 下可靠审批与调度
- 待审批 checkpoint 跨进程恢复
- 多 worker、高可用与审批 checkpoint 跨进程恢复
- 任意 PromQL、任意 Shell 或任意日志文件读取
- 生产级容器加固、TLS、高可用和灾备

## 架构

```text
FastAPI API
    ↓
Core services
    ↓
Interfaces
    ↑
Plugins / Storage
```

- `src/api/`：HTTP 校验、鉴权依赖和异常映射
- `src/core/`：聊天、告警、分析、规则和巡检编排
- `src/interfaces/`：跨层契约和中性数据类型
- `src/plugins/`：Prometheus、DeepSeek、SSH 诊断和命令执行实现
- `src/storage/`：SQLite、内存缓冲、审批队列和幂等守卫
- `src/agent/`：LangChain/LangGraph Agent、工具和人工审批续跑
- `src/container.py`：按配置惰性装配并缓存组件

核心原则是：Prometheus 提供事实，模型负责解释和选择受控工具；模型不能提交自由
PromQL 或自由 Shell。写命令只能来自管理员维护的目录。

## 快速开始

### 一分钟 Compose 启动

安装 Docker Desktop（Windows）或 Docker Engine + Compose（Linux）后运行：

```bash
python scripts/dev_stack.py up
```

脚本会在被忽略的 `.runtime/` 中生成 Compose 开发栈 Token、Grafana 密码、数据和日志目录，
构建 Assistant 并启动五个服务。终端会显示控制台地址和 Compose 用户 Token。再次查看：

```bash
python scripts/dev_stack.py show-token
```

Grafana 默认绑定 `127.0.0.1:3000`。端口已被本机服务占用时，可在启动前设置
`GRAFANA_HOST_PORT`（例如 `3001`）；容器内端口仍为 `3000`。

停止服务但保留监控数据卷：

```bash
python scripts/dev_stack.py down
```

Windows 上的 Node Exporter 观察 Docker Linux 虚拟机/容器环境，不代表 Windows 宿主机指标。

### 图形化控制台

打开 `http://127.0.0.1:8000/console` 并输入用户 Token。控制台提供总览、主机详情、
问答诊断、人工审批、运维记录和个性化设置。主题、显示密度、自动刷新、默认主机、收藏指标
及总览卡片顺序只保存在浏览器 `localStorage`；Bearer Token 只保存在当前标签页的
`sessionStorage`。旧 `/status` 已移除，访问会返回 404。

管理员 Token 对应的身份必须列入 `OPS_ADMIN_IDENTITIES`。鉴权成功后侧栏才显示“系统管理”。
管理页不显示 Token、SSH 连接信息或凭据；“监控主机”分区仅显示必要的 Prometheus
目标标签与凭据 ID。保存会校验 revision、备份原文件、保留 YAML 注释并切换运行快照，
成功即生效。页面外手动修改 YAML 后可点击“应用磁盘配置”；监听地址、SQLite/日志路径
和环境变量密钥仍需重启。管理页按安全与开关、告警规则、命令目录、主机授权和监控主机
分区，切换前四区不会丢失未保存草稿；进入主机向导前须先保存或放弃草稿。主机页面展示所选时段的
最小、平均、当前和最大值区间摘要（不是逐点时间序列）；运维记录中的完整指标快照可展开查看。
各页面操作与错误处理详见 [控制台说明](docs/console.md)。

### 本地 Python 启动

建议使用独立虚拟环境。项目当前测试环境为 Conda `langchain1.2`。
本地 Python 服务从项目根目录 `.env`（或启动进程的环境变量）读取
`OPS_API_TOKENS`。它不会读取 `.runtime/compose.env`，后者只供 Compose 使用。
若本机 8000 端口运行的是 Python 服务，查看对应管理员 Token 请使用
`python scripts/dev_stack.py show-token --source local`；不要使用默认的 Compose Token。
Token 更改后需要重启相应 Assistant 进程才能生效。

```bash
pip install -r requirements.txt
cp .env.example .env
```

至少配置：

```env
DEEPSEEK_API_KEY=...
PROMETHEUS_URL=http://localhost:9090
OPS_API_TOKENS={"随机密钥":"admin","alertmanager-随机密钥":"alertmanager"}
OPS_APPROVER_IDENTITIES=["admin"]
OPS_ADMIN_IDENTITIES=["admin"]
```

启动：

```bash
uvicorn src.main:app --host 127.0.0.1 --port 8000
```

也可以使用 `python src/main.py`；此时监听地址和端口来自 `config/settings.yaml`。

验证：

```bash
curl http://127.0.0.1:8000/health
```

创建会话后聊天：

```bash
curl -X POST http://127.0.0.1:8000/sessions \
  -H "Authorization: Bearer <密钥>" \
  -H "Content-Type: application/json" \
  -d '{"name":"首次排查","host_id":"local-node"}'

curl -X POST http://127.0.0.1:8000/chat \
  -H "Authorization: Bearer <密钥>" \
  -H "Content-Type: application/json" \
  -d '{"session_id":"<上一步返回的 id>","message":"CPU 使用率是多少"}'
```

Swagger UI 位于 `http://127.0.0.1:8000/docs`。

## 鉴权说明

`auth.enabled=true` 时，数据和写操作 API 都需要 Bearer Token。`GET /health`、`GET /ready`、
`GET /console` 及其同源静态资源公开；控制台空壳不含运维数据，实际取数和操作仍需用户
输入 Token。管理员 API 始终强制 Bearer Token，即使普通接口鉴权关闭也不会匿名开放。
`/docs`、`/redoc` 和 `/openapi.json` 是 FastAPI 应用级文档路由，当前公开；在不可信网络
部署时应由反向代理限制它们。

`POST /alert` 还要求调用身份等于 `auth.alert_identity`。不要让普通聊天用户与
Alertmanager 共用 Token，因为 webhook 可触发 `auto_execute=true` 的规则写操作。

## API

下表用于定位接口；请求/响应结构、身份规则、错误码和典型调用顺序见
[HTTP API 参考](docs/api.md)。

| 方法和路径 | 说明 |
|---|---|
| `GET /health` | 始终 200 的进程存活与缓存组件状态 |
| `GET /ready` | 主动检查 Prometheus；核心依赖未就绪返回 503 |
| `GET /console` | 公开的图形化控制台空壳；数据和操作接口仍鉴权 |
| `GET /admin/config` | 管理员读取结构化、脱敏的可编辑配置与生效状态 |
| `PUT /admin/config` | 管理员校验、备份、保存并立即应用运行快照 |
| `POST /admin/config/apply` | 管理员显式应用手动修改的磁盘 YAML |
| `GET /admin/hosts/options` | 已登记主机、已发现目标及可选凭据 ID |
| `POST /admin/hosts/preview`、`/ssh-check`、`/add` | 预览、只认证的 SSH 检查、新增并热应用 |
| `GET /hosts` | 主机清单（不返回 SSH 地址或凭据） |
| `GET /hosts/{id}/status` | 指定主机的 Exporter、指标和活动告警状态 |
| `GET /hosts/{id}/metrics/{metric}` | 指定主机即时指标；追加 `/trend` 查询趋势 |
| `GET /executions` | 规则和 Agent 动作的统一持久化执行记录 |
| `POST /sessions` | 创建绑定主机的会话；多主机时必须显式提供 `host_id` |
| `GET /sessions` | 列出当前身份的会话 |
| `GET /sessions/{id}` | 会话详情和全部消息状态 |
| `PATCH /sessions/{id}` | 修改或清除会话标签 |
| `DELETE /sessions/{id}` | 删除会话并取消关联未决审批 |
| `POST /chat` | 使用既有 `session_id` 聊天 |
| `POST /alert` | Alertmanager webhook |
| `GET /alerts/recent` | 进程内最近接收的告警 |
| `GET /alerts/active` | Prometheus 当前 pending/firing 告警 |
| `GET /alerts/history` | 告警、处置和 AI 建议归档 |
| `GET /approvals` | 当前身份可见的待审批写命令 |
| `POST /approvals/{id}` | 批准或拒绝写命令 |
| `GET /inspections/recent` | 最近异常巡检归档 |

## 配置

运行时配置位于 `config/`。管理页可编辑字段保存后立即生效；页面外手动修改
YAML 后须由管理员显式应用。服务监听、SQLite/日志路径和环境密钥仍需重启：

| 文件 | 作用 |
|---|---|
| `settings.yaml` | 服务、监控源、LLM、存储、日志、鉴权和巡检调度 |
| `hosts.yaml` | 主机 ID、Prometheus 目标、SSH 凭据引用和诊断/动作授权 |
| `metrics.yaml` | 指标启用范围、surface、阈值、instance 和 selector |
| `rules.yaml` | Alertmanager `alertname` 到规则写命令的映射 |
| `actions.yaml` | 规则自动处置写命令目录；该路径没有人工审批 |
| `agent_actions.yaml` | Agent 写命令目录；执行前强制人工审批 |
| `diagnostics.yaml` | Agent SSH 只读诊断命令目录 |

完整字段、默认值、约束和能力关闭语义见
[配置指南](docs/configuration.md)。

## 安全提示

- 密钥只放 `.env` 或运行环境，不写入 YAML、日志、截图或工单
- 对外监听前启用鉴权，并由反向代理提供 TLS
- Alertmanager 使用独立身份和 Token
- 多用户环境使用 `approvers_only`
- SSH 优先使用专用账号和私钥，并严格校验 known_hosts
- `actions.yaml` 的规则自动处置不经过人工审批，只有主机同时授权动作时才能执行；启用
  `auto_execute` 前必须在隔离目标实测
- `agent_actions.yaml` 的审批记录在内存中，进程重启会使未决审批失效
- 不提交 `.env`、数据库、WAL/SHM 文件和日志
- 可选用 `OPS_REDACTION_SECRETS` JSON 数组注册额外需遮蔽的值；不要加入空值或短值
- 升级前若历史日志或数据库可能保存旧密钥，应先轮换密钥；程序不会自动改写历史文件

## 数据与日志

默认位置：

```text
data/aiops_assistant.db
logs/audit.log
logs/inspection.log
```

- `audit.log`：写命令尝试与审批决策，JSON Lines，支持轮转
- `inspection.log`：每轮巡检的人类可读分组结果
- SQLite：九张表，schema 当前为 v7；详见 [数据库说明](docs/database.md)

## 文档索引

| 文档 | 内容 |
|---|---|
| [使用指南](docs/guide.md) | 部署、日常操作、备份和排障 |
| [配置指南](docs/configuration.md) | 全部运行时 YAML 字段和示例 |
| [鉴权说明](docs/auth.md) | Token、身份、审批角色和 Alertmanager 配置 |
| [数据库说明](docs/database.md) | 表结构、清理、迁移、备份和恢复 |
| [控制台说明](docs/console.md) | 页面、指标区间、会话审批与管理员草稿操作 |
| [API 参考](docs/api.md) | 所有 HTTP 路径、请求响应边界与错误语义 |
| [只读排障](docs/troubleshooting.md) | Prometheus/Exporter、告警、审批、配置与 SQLite 证据收集 |
| [贡献约定](CONTRIBUTING.md) | 分层、安全、测试和文档同步要求 |

## 开发验证

```bash
conda run -n langchain1.2 python -m pytest -q
python -m compileall -q src tests init.py
```

自动化测试必须离线，不访问真实 DeepSeek、Prometheus、SSH 或公共网络。fake
monitor/SSH 双主机隔离回归和两台独立 Linux 主机的真实 Prometheus、Alertmanager、
SSH、规则动作及 Agent 审批闭环均已完成；实机记录见 roadmap 和 PROJECT_HANDOVER。
本次热更新与主机向导完成后的完整离线回归为 `697 passed`。0.6.0 前端和管理员阶段已获用户浏览器反馈，
后续基础稳定性、SQLite 并发、部署恢复与发布包仍待实施。
