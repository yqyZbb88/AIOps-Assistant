# 智能运维 Agent（ops-agent）

基于 **Python + FastAPI + LangChain Agent** 的智能运维 Agent。已实现能力：

- **AI 对话**：DeepSeek Agent 按 `metrics.yaml` 的 chat surface 自主调用监控工具回答问题；
- **AI 只读主机诊断**：监控指标解释不了的怪现象，Agent 可调用 config/diagnostics.yaml 白名单只读命令
  （free / df / ps / systemctl / journalctl）到目标主机排查；命令模板与参数校验全部配置化，凭据只进 .env；
- **关键词兜底**：无 LLM 环境照常答复 CPU/内存/磁盘/告警/状态/帮助；
- **统一 Prometheus 指标目录**：CPU/Memory/Disk/Network/System 共 17 个内置指标，支持即时值、趋势、instance 与标签 selector；
- **告警状态主动查询**：pending / firing（`/alerts/active`）；
- **Alertmanager 告警接收**（`/alert` webhook）+ 最近记录（`/alerts/recent`）；
- **告警自动处置**：规则库（rules.yaml）引用 **config/actions.yaml 写命令目录** → 通用执行器按目录 key
  执行命令（模板 + 参数校验 + sudo 声明 + 审计日志）；「写命令」与「只读诊断」共用 SSH/审计内核，边界分离；
- **告警 AI 分析**：无自动处理规则的告警 → LLM 给出根因假设与处理建议。
- **固定指标自动巡检**：按全局周期检查 `metrics.yaml` 的 inspection surface 指标，按配置阈值生成
  `normal / warning / critical / error`；每轮写 `logs/inspection.log`，异常轮次写 SQLite，
  可通过 `/inspections/recent` 查询。巡检不调用 AI，也不执行写操作。
- **Agent 写命令（强制人工审批）**：Agent 可申请执行 config/agent_actions.yaml 声明的写命令
  （重启服务 / 清理磁盘等）；调用后**中断**，经 `/approvals` 人工批准才真正执行。
  与规则自动处置**完全隔离**（各用各的白名单）；未批准 / 过期 / 重启 → 命令永不执行。

代码按目标架构**就地分层**落地（既定决策留痕见 [PROJECT_HANDOVER.md](./PROJECT_HANDOVER.md)
的「既定决策记录」），落实：模块解耦（interfaces/ 抽象 + plugins/ 实现）、可扩展性（各能力预留接口）、
配置分离（config/ 不再硬编码）、可测试性（core 只依赖抽象接口，单测注入假组件）、
安全性（密钥走 .env / 环境变量，动作审计）。

---

## 项目结构

```
Assitant/
├── README.md / requirements.txt / .env.example
├── docs/
│   ├── database.md            # SQLite 库结构详解（八张表逐字段说明 + 常用查询）
│   ├── configuration.md       # ★ 所有运行时 YAML 配置字段、默认值、约束与示例
│   └── auth.md                # 鉴权与密钥（密钥怎么来/放哪/怎么用、Alertmanager 侧、排查）
├── init.py                    # 用户模板（只读）：init_chat_model 单一模型入口
├── config/
│   ├── settings.yaml          # 全局配置（支持 ${ENV} / ${ENV:-default} 插值）
│   ├── metrics.yaml           # 统一指标目录：启用范围、阈值、instance、selector
│   ├── rules.yaml             # 自动处置规则库（引用 actions.yaml 写命令；密码以环境变量占位）
│   ├── actions.yaml           # 写命令目录：规则（rules.yaml）自动处置用，无头执行
│   ├── agent_actions.yaml     # Agent 写命令白名单（独立一份）：每次调用强制人工审批
│   └── diagnostics.yaml       # 只读诊断命令白名单 + 目标主机（Agent 排查用）
├── scripts/                   # 压测脚本 load_gen.sh / load_mem.sh（保留）
├── 参考/                      # 尚硅谷 LangChain 1.x 讲义（10 章，仅本地参考，不入库）
├── src/
│   ├── main.py                # create_app()：装配 container → include api routers
│   ├── api/                   # 薄 HTTP 层（依赖注入取 OpsAgent，无业务逻辑）
│   │   ├── deps.py            #   ★ require_auth：共享密钥 → 身份（router 级依赖，fail-closed）
│   │   ├── chat.py            #   POST /chat
│   │   ├── alert.py           #   POST /alert、GET /alerts/recent、/alerts/active、/alerts/history
│   │   ├── approvals.py       #   GET /approvals、POST /approvals/{id}（写命令人工审批）
│   │   ├── sessions.py        #   POST /sessions、GET /sessions、GET/PATCH/DELETE /sessions/{id}
│   │   ├── inspections.py     #   GET /inspections/recent（异常巡检归档）
│   │   └── health.py          #   GET /health
│   ├── core/                  # 业务核心（只依赖 interfaces 抽象与注入组件）
│   │   ├── agent.py           #   OpsAgent 主控制器（chat/process_alert/recent/active_alerts）
│   │   ├── chat.py            #   ChatService：Agent-first + 关键词兜底（转发 capabilities）
│   │   ├── capabilities.py    #   ★ 监控能力单一实现：查询+格式化纯函数（无 langchain，工具与兜底共用）
│   │   ├── rule_engine.py     #   规则引擎（加载 + 匹配决策，只依赖 ActionExecutor 接口）
│   │   ├── prompts.py         #   告警分析 system 提示词（纯常量；analysis.py 用）
│   │   ├── analysis.py        #   告警 LLM 分析（上下文组装 + LLM 编排）
│   │   ├── inspection.py      #   ★ 固定指标巡检、阈值判定、汇总日志、后台调度
│   │   └── exceptions.py      #   业务异常基类
│   ├── metrics/               # ★ 代码内置 Prometheus 指标定义目录（17 个）
│   ├── interfaces/            # 抽象接口层（core 只依赖这里）
│   │   ├── metric.py          #   MetricDefinition / MetricSample / MetricsConfig
│   │   ├── monitor.py         #   MonitorSource + 通用查询接口
│   │   ├── action.py          #   ActionExecutor + ActionError
│   │   ├── command.py         #   CommandSpec / ParamSpec（命令元信息，读/写边界共用）
│   │   ├── llm_client.py      #   LLMClient + LLMError
│   │   ├── diagnostic.py      #   DiagnosticSource + DiagnosticError（只读诊断契约）
│   │   ├── agent_action.py    #   AgentActionSource（Agent 写命令契约）+ render_agent_action
│   │   ├── approval.py        #   PendingApproval / ApprovalOutcome / ApprovalError（审批中性类型）
│   │   ├── session.py         #   ChatMessage/SessionInfo/TurnResult + SessionStore + HistoryCapable
│   │   ├── history.py         #   AlertRecord/RemediationRecord/AdviceRecord + HistoryStore（归档契约）
│   │   └── assistant.py       #   Assistant + ApprovalCapable/ApprovalCancellable（Protocol）
│   ├── plugins/               # 具体插件实现（实现 interfaces）+ 共享内核
│   │   ├── common/                            # 跨家族共享内核（读/写两条边界共用）
│   │   │   ├── endpoint.py                    #   SshEndpoint 连接抽象（认证二选一）
│   │   │   ├── ssh.py                         #   exec_command：Endpoint + 命令串 → (exit/out/err)
│   │   │   ├── audit.py                       #   record_execution：统一审计写入 ops_agent.audit
│   │   │   └── cmddef.py                      #   命令目录机制：模板+参数+校验+build_command（纯）
│   │   ├── actions/                           # WRITE：rules.yaml 的写命令目录 → 通用执行器
│   │   │   ├── catalog.py                     #   config/actions.yaml 解析（写命令白名单）
│   │   │   └── executor.py                    #   CatalogCommandExecutor：按目录 key 执行命令
│   │   ├── agent_actions/                     # WRITE：Agent 写命令（强制人工审批）
│   │   │   ├── catalog.py                     #   config/agent_actions.yaml 解析 + 冲突检测
│   │   │   └── source.py                      #   SshAgentActionSource：按目录执行写命令
│   │   ├── monitors/prometheus_monitor.py      # MonitorSource：Prometheus 查询+趋势
│   │   ├── llm_clients/deepseek_client.py      # LLMClient：告警分析直连
│   │   └── diagnostics/                        # READ 边界：只读诊断（Agent 工具用）
│   │       ├── commands.py                     #   diagnostics.yaml 解析（config 级；命令级复用 common/cmddef）
│   │       └── ssh_diagnostics.py              #   经 SSH 执行白名单只读命令（注册 diagnostic "ssh"）
│   ├── agent/                 # ★ 保留的 Agent 编排层（create_agent + @tool + 提示词）
│   │   ├── prompts.py         #   系统提示词 / Agent 名（纯常量，只有 agent/ 用）
│   │   ├── tools.py           #   build_tools(monitor)/build_diag_tools：@tool 工厂（监控工具薄接线转发 core/capabilities）
│   │   └── assistant.py       #   OpsAssistant：经 utils/model_loader 取模型，构建 Agent
│   ├── registry.py            # 插件注册表（monitors / llms / diagnostics；两类写命令均走配置文件，不入注册表）
│   ├── container.py           # DI 容器：settings → 插件实例 → OpsAgent（全部惰性/缓存）
│   ├── storage/               # 数据模型与状态存储
│   │   ├── models.py          #   告警/规则 pydantic 模型（Alert / AlertmanagerPayload / …）
│   │   ├── memory.py          #   AlertStore：最近告警内存缓冲
│   │   ├── approval.py        #   ApprovalStore：待审批写命令的缓冲（有界/单次性/会过期）
│   │   ├── session.py         #   SessionStore：会话+消息落盘（SQLite 实现 / 内存降级实现）
│   │   ├── history.py         #   HistoryStore：告警/处置/AI 建议三表归档（SQLite 实现）
│   │   └── db.py              #   ★ SQLite 底座：连接工厂 + 建表 + 版本校验（失败降级为 None）
│   └── utils/                 # 基础设施工具
│       ├── config_loader.py   #   settings.yaml 解析 + 环境插值 + 默认兜底（ROOT_DIR）
│       ├── logger.py          #   日志级别控制 + 审计 logger（ops_agent.audit）
│       ├── time.py            #   ★ 统一时间戳格式（TTL 靠字符串比较，格式必须逐字一致）
│       ├── auth.py            #   ★ 鉴权纯逻辑：密钥→身份 JSON 解析 + 常数时间比对（无 FastAPI）
│       ├── security.py        #   敏感打码 redact_sensitive
│       └── model_loader.py    #   ★ 模型单一入口：惰性复用 init.py + 进程内单例缓存
└── tests/                     # 离线单测（conftest 提供 FakeMonitor / FakeLLM / FakeDiagnostics）
```

### 相关文档

| 文档 | 内容 |
|---|---|
| **README.md**（本文） | 项目结构 / 分层职责 / 配置 / 路由 / 扩展指南（**结构权威文档**） |
| [docs/configuration.md](docs/configuration.md) | **YAML 配置权威指南**：六个运行时配置文件的字段、类型、默认值、校验与示例 |
| [PROJECT_HANDOVER.md](PROJECT_HANDOVER.md) | 开发约束 / 安全不变量 / 已踩的坑 / 既定决策 / 路线图 |
| [docs/database.md](docs/database.md) | **SQLite 库结构详解**：八张表逐字段说明、表间关系、常用查询、迁移与备份 |
| [docs/auth.md](docs/auth.md) | **鉴权与密钥**：本项目有哪些密钥、怎么生成、怎么用、Alertmanager 侧怎么配、故障排查 |

### 分层职责（依赖方向：上 → 下，插件向上实现接口）

| 层 | 职责 | 依赖 |
|---|---|---|
| `api/` | 请求/响应校验 + 转发 | 只取 `OpsAgent` |
| `core/` | 业务逻辑：聊天 / 告警编排 / 规则决策 / 分析 | 只依赖 `interfaces/` 与注入组件 |
| `interfaces/` | 抽象契约（监控源 / 动作 / LLM / 只读诊断） | 无第三方依赖 |
| `plugins/` | 具体实现 + 共享内核（Prometheus / 写命令目录→通用执行器 / DeepSeek / 只读诊断） | 实现 interfaces，import 即注册（monitors/llms/diagnostics）；写命令按 actions.yaml 目录构建 |
| `agent/` | LangChain Agent 编排（保留的「尚硅谷 Agent」） | 惰性复用根 `init.py` |
| `storage/` `utils/` | 模型 / 缓冲 / 配置 / 日志 / 安全 | — |

---

## 安装与依赖

推荐在独立虚拟环境运行（本项目在 conda 环境 `langchain1.2` 中）：

```bash
python -m venv .venv
.venv\Scripts\activate            # Windows
pip install -r requirements.txt
```

`requirements.txt` 分「核心」与「可选」两块，可选依赖**全部惰性加载**（未安装时能力自动降级，服务照常启动）：

- 核心：fastapi / uvicorn / pydantic / requests / pyyaml；
- 可选① 聊天 Agent 与告警 AI 分析：langchain、langgraph、langchain-core、langchain-deepseek、python-dotenv；
- 可选② SSH 动作执行与只读诊断：paramiko（需要执行 rules.yaml 引用的写命令目录 / actions.yaml，或启用 AI 只读诊断时安装）；
- 可选③ 测试：pytest。

模型初始化统一走根目录 [init.py](./init.py)（只读，单一入口）。改模型/provider/参数只改该文件即可。

---

## 配置

### 出厂默认开关一览（先看这里）

各能力的启停散在下面五个配置文件里，这里集中列一份**出厂状态**，一眼看清「现在哪些是开着的」。
每个文件的格式细节见各自小节。

| 能力 | 配置文件 · 键 | 出厂值 | 说明 |
|---|---|---|---|
| 自动处置（写命令目录） | [actions.yaml](config/actions.yaml) · `enabled` | `true` | 目录即执行器集合；`false` → 无任何执行器，所有规则失效 |
| 单条规则是否自动执行 | [rules.yaml](config/rules.yaml) · `auto_execute` | **逐条**（见下表） | `false` = 命中只记录不执行 |
| Agent 只读诊断 | [diagnostics.yaml](config/diagnostics.yaml) · `enabled` | `true` | `false` → Agent 不注册诊断工具 |
| Agent 写命令 | [agent_actions.yaml](config/agent_actions.yaml) · `enabled` | `true`（**已启用**） | 原设计为出厂关闭，**2026-09-10 起本仓库已打开**；每次调用仍需人工审批 |
| 告警 AI 分析 | [settings.yaml](config/settings.yaml) · `llm.enabled` | `true` | `false` → 跳过 LLM 分析，告警接收不受影响 |
| 固定指标巡检 | settings.yaml · `inspection.enabled` | `true` | 周期检查 CPU/内存/磁盘/活动告警；**不调用 AI、不执行写命令**，仅单 worker |
| 巡检日志落盘 | settings.yaml · `logging.inspection_file` | `logs/inspection.log` | 每轮汇总都写；异常轮次另写 SQLite，normal 不入库 |
| 动作审计日志 | settings.yaml · `logging.audit` | `true` | `false` → **只记失败**，成功执行静默（**非完全关闭**） |
| 审计落盘 | settings.yaml · `logging.audit_file` | `logs/audit.log` | 留空则只进 console（重启即丢；审计的用处恰是事后追查） |
| SQLite 持久化 | settings.yaml · `storage.enabled` | `true`（**yaml 里**） | 代码里 dataclass 默认 `false`，看出厂值以本文件为准；`false`/打不开 → 服务照常起，只是不落盘 |
| **API 鉴权** | settings.yaml · `auth.enabled` | **`true`（已打开）** | 打开后除 `GET /health` 外全部要求 `Authorization: Bearer <密钥>`，身份由密钥推出、会话按身份隔离。关闭 = 无身份、不做归属校验，行为与改造前一致（但残余风险全部成立）。`true` 却没配密钥会**拒绝启动**。详见 [docs/auth.md](docs/auth.md) |
| 对外监听地址 | settings.yaml · `server.host` | `127.0.0.1` | 仅本机可访问；改 `0.0.0.0` 前先读「安全说明」的残余风险 |

**rules.yaml 出厂 4 条规则，全部 `auto_execute: false`**——也就是说**当前没有任何无人值守的写路径**：
告警只会被记录 + 交给 LLM 分析，不会自己改系统。

| alertname | action | auto_execute |
|---|---|---|
| HighCPUUsage | restart_service | `false` |
| HighCPUUsage | stop_loadgen | `false` |
| HighMemoryUsage | restart_service | `false` |
| HighDiskUsage | cleanup_disk | `false` |

要把某条规则变成自动处置，把它改成 `true` 并**重启进程**。注意规则的连接凭据在其 `params` 里
（密码用 `.env` 占位）——未填时插值为空串，连接阶段失败，**不会误连远端主机**。

于是当前**唯一能改动系统的路径就是 Agent 写命令那条，而它强制人工审批**；
规则侧的这一条开关是后续放开自动处置时才需要动的。

**不可关闭的**：Agent 写命令的**人工审批**。`agent_actions.yaml` 的 `approval` 段写任何多余键
（形如 `enabled`）都会导致校验失败、能力整体关闭——配置层不存在「绕过审批」这个选项（见下节）。

各 YAML 文件的完整字段说明见 [docs/configuration.md](docs/configuration.md)。

### config/settings.yaml（可变参数，不再硬编码）

| 键 | 说明 |
|---|---|
| `app.name/version` | 应用标识 / 版本 |
| `server.host/port` | uvicorn 监听（默认 127.0.0.1:8000） |
| `prometheus.base_url/timeout` | 数据源地址与超时；base_url 支持 `${PROMETHEUS_URL:-http://localhost:9090}` |
| `monitor.provider` | 监控源插件名（registry：prometheus） |
| `llm.provider/enabled` | LLM 插件与开关（false 则告警 AI 分析跳过） |
| `agent.enabled/name/debug/recursion_limit` | 聊天 Agent 总开关 / 归因名 / debug / 单轮图步骤上限 |
| `chat.max_message_chars` | 单条聊天消息上限，超限返回 413 |
| `alert.recent_limit/trend_minutes/trend_step` | 最近告警缓冲上限 / AI 分析趋势窗口 |
| `metrics.file` | 统一指标目录文件，默认 `config/metrics.yaml`；为空时使用代码目录默认配置 |
| `inspection.enabled/interval_seconds/run_on_start` | 巡检总开关、全局周期、是否启动即跑一轮（仅单 worker） |
| `inspection.ttl_hours/max_rows` | 异常巡检归档保留时长 / 最大行数 |
| `storage.enabled` | SQLite 持久化总开关（代码里 dataclass 默认 false，本文件才是出厂值） |
| `storage.db_path` | 库文件路径（相对仓库根，默认 `data/ops_agent.db`；WAL 会生成 `-wal`/`-shm`） |
| `storage.session_ttl_hours/max_sessions` | 会话过期时长 / 会话数上限 |
| `storage.message_window` | 每轮重发给模型的历史消息条数上限 |
| `storage.history_ttl_hours/max_history_rows` | 告警·处置·AI 建议归档的过期时长 / 行数上限 |
| `storage.purge_interval_seconds` | 清理节流间隔（避免每次写都全表扫） |
| `logging.level/audit` | 日志级别 / 动作审计开关 |
| `logging.audit_file` | 审计落盘路径（默认 `logs/audit.log`；留空 = 只进 console） |
| `logging.audit_max_bytes/audit_backup_count` | 审计文件轮转上限（5 MiB）与保留份数（3） |
| `logging.inspection_file` | 全量巡检日志路径；按 CPU/Memory/Disk/Network/System 分组输出多行人类可读文本 |
| `logging.inspection_max_bytes/inspection_backup_count` | 巡检日志轮转上限与保留份数 |
| `auth.enabled` | 共享密钥鉴权开关（代码里 dataclass 默认 `false`，本文件才是出厂值） |
| `auth.tokens` | 「密钥 → 身份」的 **JSON 映射**，形如 `{"k-alice-9f3a":"alice"}`；真实值走 `.env` 的 `OPS_API_TOKENS`。**yaml 里的占位符必须用单引号包住**（`'${OPS_API_TOKENS:-}'`），否则变量有值时会被 YAML 当成流式映射解析成 dict |
| `auth.alert_identity` | `POST /alert` 只接受的**身份名**（默认 `alertmanager`）；留空 = 不限制，且启动时有一条 WARNING。**必须与 `.env` 里某个密钥映射到的身份一致** |
| `auth.approval_mode` | `owner`（触发者本人审批，兼容旧行为）或 `approvers_only`（职责分离，只允许显式审批人） |
| `auth.approver_identities` | 审批人身份 JSON 数组，如 `["alice","audit-bot"]`；真实值可用 `OPS_APPROVER_IDENTITIES` 注入 |

### config/metrics.yaml（统一指标目录）

PromQL、指标类型、计算方向和多标签分组方式固定在代码目录
`src/metrics/catalog.py`；用户只在本文件选择实际启用指标和阈值：

```yaml
version: 1
instance: 192.168.20.128:9100
max_alert_analysis_metrics: 8

metrics:
  cpu.usage:
    enabled: true
    surfaces: { inspection: true, chat: true, alert_analysis: true }
    warning: 80
    critical: 90
    severity_enabled: true

  disk.io_util:
    enabled: true
    surfaces: { inspection: true, chat: false, alert_analysis: false }
    severity_enabled: false
```

代码目录内置 CPU、Memory、Disk、Network、System 共 17 个指标。每个指标可配置：

- `enabled`：是否启用
- `surfaces.inspection/chat/alert_analysis`：分别控制巡检、聊天工具、告警 AI 趋势
- `warning/critical/severity_enabled`：阈值与是否参与巡检等级
- `selector`：Prometheus 标签 matcher，可覆盖全局 `instance`
- `max_alert_analysis_metrics`：限制告警 AI 最多查询多少条趋势

`observe` 指标只记录不参与等级；所有 `*_bytes` 使用 `rate()`；未知指标 ID、非法阈值或未知字段会拒绝启动。
`rules.yaml` 仍按 Alertmanager alertname 匹配，`diagnostics.yaml` SSH 只读命令体系不受本文件影响。
修改后需重启服务。

### config/actions.yaml（写命令目录，WRITE 边界）

会改系统的修复命令**不再按 Python 执行类实现**，集中在本文件声明「允许执行的命令」白名单；
规则只能从这个目录里选命令执行。格式与 diagnostics.yaml 同一套机制（模板 + 参数声明），
命令级校验复用 src/plugins/common/cmddef.py：

```yaml
commands:
  restart_service:              # key = rules.yaml 里 action 字段引用
    summary: 重启一个 systemd 服务
    template: "systemctl restart {service}"
    sudo: false                 # 每条命令单独声明是否提权执行（管理员决定）
    params:
      service: { type: str, pattern: "^[A-Za-z0-9_.:@-]{1,128}$", description: 服务名 }
  cleanup_disk:
    summary: 执行磁盘清理脚本
    template: "cd /opt/ops && bash cleanup_disk.sh"   # 无参命令：模板即整条
    params: {}
```

要点：
- 模板占位集合必须等于 params 声明（多写/漏写 → 目录校验失败并关闭）；str 参数须声明
  `pattern`（全匹配），int 用 min/max 收敛；连接凭据由 rules.yaml 每条规则 params 提供（本文件不落）；
- 改目录需**重启进程**生效（executors 首次装配时按目录构建并缓存）。

### config/agent_actions.yaml（Agent 写命令白名单 · 强制人工审批）

与 `actions.yaml` **完全独立**的另一份白名单：`actions.yaml` 供规则无头自动执行，
本文件只给聊天 Agent 用，且**每次调用都必须经人工审批**。Agent 申请执行 → 服务中断并返回
待审批提示 → 人在 `/approvals` 批准后命令才真正执行。

```yaml
enabled: true                 # 本仓库已启用（原设计出厂关闭）；关停改 false 并重启进程
target:                       # 连接凭据只在这里（密码 .env 占位），模型永远看不到
  host: 192.168.20.128
  port: 22
  username: root
  password: "${SSH_PASSWORD_192_168_20_128}"
output_limit: 4000
approval:                     # 只有这两个键；写别的键（如 enabled）= 配置错误 → 关闭能力
  timeout_seconds: 900        # 待审批项存活时长，过期即丢弃（不会自动批准）
  max_pending: 32             # 同时待审批上限，超出按最旧淘汰（同样只丢弃不执行）
commands:
  restart_service:
    summary: 重启一个 systemd 服务
    template: "systemctl restart {service}"
    sudo: true                # 管理员逐条声明，模型无法提权
    params:
      service: { type: str, pattern: "^[A-Za-z0-9_.:@-]{1,128}$", description: 服务名 }
```

要点：
- **审批不可关闭**：配置层没有任何开关能绕过人工审核（`approval` 段写多余键即报错并关闭能力）；
  决策只支持 `approve` / `reject`——**不提供 `edit`**（允许编辑参数等于绕过管理员声明）；
- **审批 id 单次有效**：重复提交返回 404，不会二次执行；
- **fail-safe**：未批准 / 已过期 / 进程重启 / 多 worker 落到别的实例 / 审批队列淘汰
  → 命令**永不执行**（过期只会丢弃，绝不自动批准）；
- 命令 key 同时是 Agent 工具名，**不得与监控工具或只读诊断命令重名**
  （重名会被静默遮蔽）——冲突时本能力整体关闭并记 ERROR，聊天与只读诊断不受影响；
- 改完需**重启进程**生效。

### config/rules.yaml（自动处置规则库）

内置示例引用 config/actions.yaml 的目录 key，格式如下（密码一律环境变量占位）：

```yaml
rules:
  - alertname: HighCPUUsage
    action: restart_service        # 必须是 config/actions.yaml 声明的写命令 key
    params: { host: 192.168.20.128, port: 22, username: root,
              password: "${SSH_PASSWORD_192_168_20_128}", service: nginx }
    auto_execute: false            # false=仅命中；true=收到告警自动执行
```

要点：
- `action` 必须是 config/actions.yaml 里的写命令 key；params = 连接字段（host/…，密码占位）
  + 该目录命令声明的语义参数（如 service）；多余/越权参数会被拒；sudo 由目录项声明，规则不可越权提权；
- 同一 `alertname` 想执行多个动作 → 写多条相同 alertname 的规则，按文件顺序执行；
- 密码一律用环境变量占位（见安全说明），**本文件不落任何明文**；
- 改完本文件需**重启进程**生效。`RuleEngine.reload_rules()` 虽已实现，但**全仓没有任何调用入口**
  （无路由、无 CLI），且它不会重建执行器集合，换不掉动作——只热加载规则价值有限。热更新端点已登记
  为待办，见 [PROJECT_HANDOVER.md](./PROJECT_HANDOVER.md)「路线图 · 配置热更新」。

### config/diagnostics.yaml（AI 只读诊断命令白名单）

给聊天 Agent 一条「指标解释不了怪现象时的只读排查」通道：Agent 只能执行本文件 `commands`
声明的命令（内置 `memory` / `disk` / `top_processes` / `service_status` / `service_logs`），
凭据在 `target` 段以环境变量占位（复用 rules.yaml 同一约定），模型上下文里永无 host/密码/私钥。

```yaml
target:
  host: 192.168.20.128
  port: 22
  username: root
  password: "${SSH_PASSWORD_192_168_20_128}"   # 真实值只在 .env
commands:
  service_status:                              # key 即 Agent 工具名
    summary: 查看某个 systemd 服务的运行状态
    template: "systemctl status --no-pager -l {service}"   # 只允许 {name} 占位
    params:
      service:
        type: str
        pattern: "^[A-Za-z0-9_.:@-]{1,128}$"   # str 必须声明 pattern，全匹配
  service_logs:
    summary: 查看某个服务最近的运行日志
    template: "journalctl -u {service} --no-pager -n {lines}"
    params:
      service: { type: str, pattern: "^[A-Za-z0-9_.:@-]{1,128}$" }
      lines:   { type: int, default: 50, min: 1, max: 200 }
```

要点：
- 命令模板静态部分是管理员可信文本；占位符集合必须等于 `params` 声明（模板含未声明占位 /
  声明了没用到的参数 → 配置加载失败并关闭该能力）；每个参数拼接前做全匹配校验 + shell 转义；
- **恒不 sudo、只读**：模型拿不到凭据，也只能在声明参数内取值，无法拼自由命令；
- 未装 paramiko / 文件缺失或 `enabled: false` → 诊断能力自动关闭（Agent 工具不注册，服务照常启动）；
- 改完文件需**重启服务**生效（Agent 工具集在首次聊天时构建并缓存）；
- 目前**单台目标主机**（`target`），多主机未实现。已定扩展方向：命令白名单共享 + `targets`
  命名映射（每台各自 `${VAR}` 密码）+ 每个工具加受限 `host` 枚举参数（详见
  [PROJECT_HANDOVER.md](./PROJECT_HANDOVER.md)「路线图 · 多主机诊断」）。

### .env / 环境变量

复制 `.env.example` 为 `.env` 并填写真实值（`.env` **勿提交版本库**）：

| 变量 | 用途 |
|---|---|
| `DEEPSEEK_API_KEY` / `DEEPSEEK_BASE_URL` / `DEEPSEEK_MODEL` | DeepSeek 凭据、地址与模型名（init.py 读取） |
| `DEEPSEEK_TIMEOUT` / `DEEPSEEK_MAX_RETRIES` / `DEEPSEEK_MAX_TOKENS` | 模型超时、重试与输出上限 |
| `PROMETHEUS_URL` | Prometheus 地址（缺省 localhost:9090） |
| `SSH_PASSWORD_192_168_20_128` | 目标主机 root 密码：规则 SSH 动作、只读诊断、Agent 写命令三处共用 |
| `SSH_KNOWN_HOSTS` | known_hosts 文件路径；留空使用系统 `~/.ssh/known_hosts`。未知主机默认拒绝 |
| `OPS_API_TOKENS` | **API 共享密钥 → 身份** 的 JSON 映射，如 `{"k-alice-9f3a":"alice"}`。配合 `settings.yaml` 的 `auth.enabled`；用随机值生成（`python -c "import secrets;print(secrets.token_urlsafe(32))"`）。留空且 `auth.enabled: true` → **拒绝启动** |
| `OPS_APPROVER_IDENTITIES` | `approvers_only` 模式下的审批人身份 JSON 数组 |
| `LANGSMITH_*`（4 个） | LangChain 链路追踪（`TRACING` / `ENDPOINT` / `API_KEY` / `PROJECT`）；由 langchain 直接读取，本仓库代码不引用，不配则只是不追踪 |

---

## 启动

仓库根执行（二选一）：

```bash
# 推荐（开发热重载）
uvicorn src.main:app --reload --host 127.0.0.1 --port 8000

# 直跑
python src/main.py
```

访问 http://localhost:8000/docs 查看交互式 API。

> 自动巡检使用应用内后台线程，**只支持单 worker**。如果启动多个 Uvicorn worker，
> 每个 worker 都会运行一套巡检；当前版本不使用文件锁或外部调度。

### 对外路由

| 方法/路径 | 说明 |
|---|---|
| `GET /health` | 健康检查 → `{"status","storage"}`（`storage` 为 `ok`/`degraded`） |
| `POST /chat` | 请求 `{"message","session_id"}`（**`session_id` 必填**）→ `{"reply","session_id","session_name"}` |
| `POST /alert` | Alertmanager webhook 推送 → `AlertAck`。**鉴权上除密钥外还要求身份是 `auth.alert_identity`**，其他有效密钥 → 403 |
| `GET /alerts/recent?limit=` | 最近被动接收的告警记录 |
| `GET /alerts/active` | 实时活动告警（监控源故障转 502） |
| `GET /alerts/history` | **落盘归档**：告警 + 处置结果 + AI 建议（持久化未启用时三者均为空） |
| `GET /inspections/recent?limit=` | 最近的异常巡检归档（normal 不落库；持久化关闭时返回空列表） |
| `GET /approvals` | 待人工审批的写命令调用（不可用时返回空列表）。**鉴权开启时只列自己触发的** |
| `POST /approvals/{id}` | 提交 `{"action":"approve"\|"reject","reason":""}` 并续跑；未知/已用/**不是你的** → 404，能力未启用 → 503 |
| `POST /sessions` | **新建会话** `{"name"?}` → `201` + 会话元信息（含 `session_id`）。创建者取自密钥，**不可指定** |
| `GET /sessions` | 会话列表，最近更新的在前（鉴权开启时**只列自己的**；持久化未启用时返回空列表） |
| `GET /sessions/{id}` | 会话元信息 + 全部消息（含 pending/abandoned）；未知 / 不属于你 → 404，未启用 → 503 |
| `PATCH /sessions/{id}` | 改标签 `{"name"}`（空串 = 清除标签）；未知 / 不属于你 → 404 |
| `DELETE /sessions/{id}` | 删除会话连同其消息，并**作废该会话的未决审批**；未知 / 已删 / 不属于你 → 404，未启用 → 503 |

> **鉴权**：`auth.enabled` 打开后，上表除 `GET /health` 外**全部**要求
> `Authorization: Bearer <密钥>`（含 `POST /alert`，记得同步改 Alertmanager）。详见「安全说明」。
>
> **会话归属**：身份由密钥推出，`/sessions/*` 只能看到 / 改 / 删**自己的**会话。
> 别人的会话一律 **404**（不是 403）——与「未知 id」同形，不泄露「这个 id 存在，只是不是你的」。
>
> **`POST /alert` 的身份白名单**：它不只看「密钥有效」，还要求身份等于
> `auth.alert_identity`（默认 `alertmanager`），其他有效密钥 → **403**。因为这条路径会把
> 告警喂给规则引擎，而 `auto_execute: true` 的规则**直接执行写命令、不经人工审批**——
> 不设限的话每个聊天用户都能构造告警去触发写入。
>
> ### 会话：两段式（2026-09-12 起）
>
> **先建会话拿 id，之后每轮都带上它。** `/chat` 不再兼职建会话。
>
> ```
> 1) POST /sessions  {"name": "生产排查"}          →  {"session_id": "a50a3e…", ...}
> 2) POST /chat      {"message": "...", "session_id": "a50a3e…"}
> ```
>
> | 端点 | 说明 |
> |---|---|
> | `POST /sessions` | 建会话的**唯一**入口。`name` 是可选的**给人识别的标签**（任意可见字符，≤64），不填则未命名 |
> | `POST /chat` | `session_id` **必填**。未知 / 不属于你 → 404（不静默重建，否则 `DELETE` 形同虚设）；空串或纯空白 → **422**（绝不静默新建一段会话） |
> | `PATCH /sessions/{id}` | 改名。空串 = 清除标签——既然能建未命名会话，就该能退回未命名 |
>
> **标签（`name`）不参与选中会话**：选中只认 `session_id`。所以**同名两次 = 两段独立对话**，
> 名字**不要求唯一**——同一个人可以有两段都叫「生产排查」的对话，靠 `GET /sessions` 里的
> id / 时间 / 标题区分。改名**不更新**会话的 `updated_at`（它表示「最后一句话是什么时候说的」，
> 不该被一次纯标签操作刷新）。
>
> **标签的处理规则**（三步）：
>
> 1. **归一化**：全角字母变半角（`ｔｅｓｔ１` → `test1`），四种空格
>    （普通 / 不换行 / 全角 / 换行制表）一律折成一个普通空格并去首尾。
> 2. **拒绝不可见字符**：只挡 Unicode 的 `Cc`（控制字符）与 `Cf`（格式字符，
>    如零宽空格、BOM、RTL 覆盖）两类。
> 3. **限长** 64。
>
> **不限制用哪些文字**——汉字、日文假名、重音字母、中文标点、emoji、阿拉伯文都可以。
> 被拒的只有**肉眼看不见**的那两类：它们恰是归一化唯一治不了的（NFKC 消不掉零宽空格），
> 而复制粘贴时很容易带上——那会让你在 `/sessions` 里看到两个**看起来完全一样**的标签、
> 却没有任何线索区分。响应里回带的是归一化后的标签。
>
> **空串与纯空白在「建会话 / 改名」里视同「没填」**：**Swagger UI 会把所有字段都填进示例
> 请求体**，把某一栏清空时发出去的是 `""` 而不是省略该字段；不这样处理的话，这个最常见的
> 操作会直接 422。（`/chat` 的 `session_id` 是例外——那里空白必须 422，见上表。）
>
> ### ⚠️ 破坏性变更（2026-09-12，本项目第一次）
>
> `POST /chat` 的**请求体**改了，**响应体三键不变**（`reply` / `session_id` /
> `session_name`，其中 `session_name` 改为只读回带会话标签）：
>
> | 字段 | 变更 |
> |---|---|
> | `user` | **删除**。身份改由 `Authorization` 密钥推出——自报的用户名无从核实，基于它的校验只是装饰 |
> | `session_name` | **删除**。名字搬到了 `POST /sessions`（建会话时给），改名走 `PATCH` |
> | `session_id` | **可选 → 必填** |
>
> 客户端若仍带着 `user` / `session_name`，会被**静默忽略**（pydantic 默认 `extra="ignore"`），
> 不会报 422——它们不再是可信输入，为它们报错只会让既有调用方困惑。

### 升级须知（开启鉴权前必读）

**存量会话的 `user` 若不在密钥映射里，开启鉴权后会立刻访问不到。** 归属校验取
「一律拒绝」：`user` 与身份**精确匹配**才算你的，`user` 为空的行任何人都不匹配。

本仓库 `data/ops_agent.db` 里实测有两类历史值：`user='yqyZ'`（真实对话）与
`user='string'`（Swagger 示例值留下的垃圾）。**要保住某段对话，把一条密钥映射到它
原本的 `user` 值上即可**：

```jsonc
// .env —— 这样 yqyZ 那段历史重新可见，"string" 那两条丢掉无妨
OPS_API_TOKENS={"k-你的随机值": "yqyZ"}
```

另外两条同源的坑：

- `auth.enabled: true` 却解析不出任何密钥 → **服务拒绝启动**（绝不「空密钥 = 放行」）。
  错配的表现是进程起不来，而不是静默地无鉴权运行。
- 打开鉴权后 **`POST /alert` 也会要求密钥**，必须在 Alertmanager 侧同步配置，
  否则告警静默 401。详见「安全说明」。
>
> ⚠️ **名字不提供任何访问保护**：它既不可靠（能猜）也不参与选中会话。真正的隔离只能靠
> 鉴权——在加鉴权之前 `GET /sessions` 会把所有会话的 id 直接列出来，**任何能访问端口的人
> 都能读到全部对话**。下面那条警告说的是同一件事。
>
> ⚠️ `/sessions/*` 是**无鉴权的读接口**，会把对话原文整段吐出来（可能含主机名、报错原文、
> 运维细节）。`server.host` 默认 127.0.0.1 是目前唯一的缓解；绑 `0.0.0.0` 前请先加鉴权。

---

## 测试

```bash
python -m pytest tests -q
```

**全部离线**（不发起任何真实 DeepSeek / Prometheus / SSH 调用），而且这条**由两道闸门强制**
（[tests/conftest.py](tests/conftest.py)）：`_no_real_model_in_tests` 把模型入口换成「一调用就炸」，
`_no_external_network_in_tests` 让任何**非回环地址**的连接直接失败（后台线程里的违规由
`pytest_sessionfinish` 兜底判失败）。后者是为一个真实坑加的——配了 LangSmith 时 langchain 会起
后台线程偷偷发外网请求，而测试**照样全绿**，只有抓 socket 才看得见。

**测试也绝不碰仓库里那个真实数据库**：`import src.main` 会按仓库真实配置打开
`data/ops_agent.db`，而**开库会跑迁移、写数据**——conftest 在模块级把
`open_database` 换成「只接受仓库外路径」，指向 `data/` 的那次直接降级为 None
（必须写在模块级：那个 import 发生在收集阶段，比任何 fixture 都早）。

`tests/conftest.py` 提供
`FakeMonitor` / `FakeLLM` / `FakeDiagnostics`，覆盖：规则引擎（假执行器 ok/failed/skip/unmatched）、
只读诊断（白名单解析、参数正则/转义安全边界、SSH stub 下的截断与审计、渲染）、
配置环境插值、敏感打码、ChatService 关键词兜底、OpsAgent 告警全链路，以及
M2 能力**同源回归**（同一 FakeMonitor 下 Agent 工具输出 == 关键词兜底输出，锁死"单一实现"）；
另有 `test_no_langchain_import.py`（在屏蔽 langchain/dotenv 的子进程中验证核心可 import、
聊天照常回落）。

Agent 写命令的审批链路用**真 graph + 脚本化假模型**离线验证
（`test_agent_actions_hitl_graph.py`）：走真实的 `create_agent` +
`HumanInTheLoopMiddleware` + `InMemorySaver`，断言「批准前不执行 / 批准后恰好执行一次 /
拒绝不执行 / 审批 id 单次性 / 非法决策不毒化线程 / 只读工具不被打断」，并有一条**端到端装配**
用例覆盖「容器 → 工厂 → OpsAssistant → 执行」整条接线。持久化与对话历史（M4）的用例同样全离线：`test_sqlite_db.py`（建表幂等、schema 版本
**双向校验**、开库失败降级、连接确实被关闭）、`test_session_store.py`（同一组断言跑
SQLite 与内存两套实现、**会话名的 user 作用域与重名复用**、**v1→v2 迁移**）、
`test_api_sessions.py`（端点 + 未启用时的 503/空列表降级 + 会话名端到端与格式校验）、
`test_chat_history.py`（历史注入、会话落库、读写失败降级）、
`test_history_capable.py`（**真 graph** 跑多轮×审批的跨模块接线：中断落 pending、
审批后原地回填、链式审批、作废）、`test_history_store.py`（三表归档、关联键、重投幂等、
截断、清理）、`test_api_alerts.py`（`POST /alert` → `/alerts/history` 端到端 + 归档失败
不影响告警接收）、`test_logger.py`（审计落盘的幂等挂载与开关语义）。

鉴权与会话归属的用例也全离线：`test_utils_auth.py`（配置解析、**常数时间全量比对**、
非 ASCII 凭据、启动期校验）、`test_api_auth.py`（逐路由 401、`/health` 与文档路由豁免、
**枚举 `app.routes` 兜底「没有路由漏挂」**、`enabled` 却无密钥时拒绝启动、配了密钥却没启用
时的反向告警；以及**跨身份的会话隔离**：别人读/聊/改/删一律 404、`GET /sessions` 只看得到
自己的、**非 owner 的 DELETE 不得作废别人的待审批写命令**、存量 `user` 的可见性规则）。

**当前基线：535 passed。**

---

## 安全说明

- **API 鉴权（`settings.yaml` 的 `auth` 段）**：启用后除 `GET /health`（探针豁免）
  外**所有**接口都要求 `Authorization: Bearer <密钥>`，**身份由密钥推出**（服务端事实）——
  请求体里自报的用户名一律不采信。密钥 → 身份 的映射走 `.env` 的 `OPS_API_TOKENS`。
  `auth.enabled: true` 却解析不出任何密钥会**拒绝启动**（绝不「空密钥 = 放行」）。
  **关闭时下面列的所有残余风险全部成立**，`server.host` 默认仅 127.0.0.1 是唯一的缓解。
  **密钥怎么生成、怎么用、身份名影响什么、出问题怎么查——见 [docs/auth.md](docs/auth.md)**。
- **⚠️ `/sessions/*` 会吐出对话原文**：可能含主机名、报错原文、运维细节与写命令的参数。
  未启用 `auth` 时 `GET /sessions` 会把**所有**会话的 id 直接列出来，任何能访问该端口的人
  都能读到全部对话。启用 `auth` 后按身份过滤（只能看到自己的）。
- **密钥只进 .env / 环境变量**：代码、`config/`、文档均不落明文。规则密码以
  `${SSH_PASSWORD_192_168_20_128}` 占位，加载时插值；变量未设置时插值为空，不会误连。
  各类密钥的清单与获取方式见 [docs/auth.md](docs/auth.md)。
- **动作审计**：写命令执行器（`CatalogCommandExecutor`）经 `plugins/common/audit.py` 统一向
  `ops_agent.audit` logger 写结构化记录
  `{action, host, params(打码), started_at, result/error}`，**永不包含密码**。
- **审批决策审计**：`POST /approvals` 在命令执行**之前**记一条
  `{action: "approval_decision", approval_id, decision, decided_by, reason, commands, decided_at}`
  ——「谁批准了这条命令」只能在这里查到（命令本身由执行器另记 `record_execution`）。
  鉴权关闭时 `decided_by` 为空串（当时没有身份可言）。两份都落 `logs/audit.log`。
- **敏感打码**：日志/处置记录统一过 `utils.security.redact_sensitive`（password/token/secret/api_key → ***）。
- **动作来源可信**：动作命令只来自 config/actions.yaml 写命令目录（管理员声明，含 sudo 与参数 schema），
  rules.yaml 只引用目录 key 并提供语义参数；多余/越权参数与非法值在进入 SSH 前被服务端拒绝，
  命令不取自告警内容。
- **只读诊断白名单**：聊天 Agent 的主机排查只能执行 config/diagnostics.yaml 声明的只读命令；
  模型只传语义参数（服务名 / 行数等），服务端做正则全匹配 / 取值收敛 / shell 转义后再拼命令，
  恒不 sudo、凭据不进模型上下文；每次真实执行写 `ops_agent.audit`（与动作同通道，记录命令/主机/参数，
  不含密码）。
- **Agent 写命令的强制人工审批**：Agent 只能申请执行 `config/agent_actions.yaml` 声明的命令，
  凭据来自该文件 `target`（不进模型上下文 / 不进工具参数），参数服务端全匹配校验 + shell 转义，
  sudo 只按目录逐条声明。调用后由 `HumanInTheLoopMiddleware` **在执行前中断**，
  经 `/approvals` 人工批准后才执行；写工具与审批策略在代码里**成对产出**
  （`WriteToolBundle`），结构上无法只取工具而不启用审批。
- **⚠️ `/approvals/*` 是一条「能执行白名单写命令」的句柄**：未启用 `auth` 时，任何能访问该
  端口且知道审批 id 的人都能批准写命令。启用 `auth` 后**按归属隔离**——只看得到、也只批得动
  自己触发的待审批项，别人的一律 404。
  其他缓解：审批 id 为随机不可枚举值、`server.host` 默认仅 `127.0.0.1`、白名单固定且命令由管理员声明。
  **注意：原设计里还有一条「出厂 `enabled: false`」的缓解，但 2026-09-10 起本仓库已把该能力打开，
  这条保护不再成立**——写通道现在是活的。
  **绑 `0.0.0.0` 前必须先打开 `auth`**，或把 `agent_actions.yaml` 的 `enabled` 改回 `false`。
  多用户生产环境建议设置 `auth.approval_mode: approvers_only`，避免“自己申请、自己批准”。
- **⚠️ 打开 `auth` 前必须先配好 Alertmanager**：`POST /alert` 也要求密钥。配方（含
  「凭据文件缺失时 Alertmanager 照常启动、但通知静默发不出去」这条实测坑）见
  [docs/auth.md](docs/auth.md) 第四节。**顺序**：先配 Alertmanager、确认告警照常入库，
  最后才打开 `auth.enabled`——鉴权关闭时服务端会忽略那个头，所以零停机、可随时回退。
- **SSH host key**：默认使用 `RejectPolicy`，加载系统 known_hosts（可用 `SSH_KNOWN_HOSTS` 覆盖）；未登记主机一律拒绝连接。生产仍应定期核对和轮换 known_hosts。
- **提交前**：`.env` 与含密钥文件不要进版本库。仓库已于 2026-09-10 完成 `git init`，
  `.gitignore` 已忽略 `.env` / `__pycache__` / `.pytest_cache` / `.venv` / `参考/` /
  `.claude/settings.local.json`。提交纪律：先确认 `git status --short` 干净，且
  `git ls-files` 里**不得出现**上述被忽略项；一任务一 commit，做完立刻跑一次全量测试。

---

## 扩展指南

- **新增监控源**：在 `plugins/monitors/` 写 `MonitorSource` 实现 → `__init__.py` 注册
  → settings 改 `monitor.provider`。
- **新增可执行修复命令**：在 config/actions.yaml 的 `commands` 加一条（安全模板 + 参数
  `pattern`/`min`/`max` + `sudo` 声明）→ rules.yaml 按 key 引用即可，无需改代码（改目录需重启进程生效）。
- **新增 LLM 供应商**：在 `plugins/llm_clients/` 写 `LLMClient` 实现 → `__init__.py` 注册。
- **新增监控能力**：先在 `core/capabilities.py` 加一个 `xxx_text(monitor)` 纯函数（措辞规范唯一出处），
  再到 `agent/tools.py` `build_tools()` 与 `core/chat.py` 关键词规则各转发一行——Agent 工具与无 LLM
  兜底自动同款文案。若加的是**写**能力（会改系统），不在此列——见下面「给 Agent 加写命令」。
- **给 Agent 加只读诊断命令**：在 `config/diagnostics.yaml` 的 `commands` 加一条（安全模板 +
  参数 `pattern`/`min`/`max`）→ 重启服务生效，工具按白名单自动生成，无需改代码。
- **给 Agent 加写命令**：在 `config/agent_actions.yaml` 的 `commands` 加一条（安全模板 +
  参数 `pattern`/`min`/`max` + `sudo` 声明）→ 重启服务生效。写工具与审批策略自动成对生成，
  无需改代码；**审批无法关闭**，新增命令一律走人工审核。
  注意命令 key 不得与监控工具（`cpu_usage` 等）或只读诊断命令重名。
- **聊天兜底加"状态/帮助"类静态回复**：`core/chat.py` 的 `_rules` 列表追加 `Rule(keyword, reply=...)`。

## 搁置项（本期不实现，仅预留）

notifiers（钉钉/企微通知）、docker/、插件目录自动扫描发现、logging.conf（用 settings 控制日志级别）、
配置热更新、**熔断机制（工具/模型调用次数上限）**。
对应占位注释与扩展位已留在相关文件中，后续按需实现即可。
熔断机制的现状、已定设计与未决点登记在 [PROJECT_HANDOVER.md](./PROJECT_HANDOVER.md)「路线图」。

> **对话历史维护与状态日志（M4）已完成**：SQLite 底座、会话与消息落盘、`/chat` 多轮、
> 审批中断落 pending 与审批后回填、会话删除作废未决审批、孤儿审批回收、`/sessions` 三端点；
> 告警/处置/AI 建议三表归档 + `/alerts/history`；审计落文件并轮转。
>
> **Agent 多轮对话采用显式会话**：先 `POST /sessions`，之后 `/chat` 必须携带 `session_id`。
> 服务在持久化层不是无状态的；持久化关闭时会话能力降级，但关键词/LLM 回复仍可工作。
