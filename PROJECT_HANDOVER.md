# ops-agent 项目交接 · 开发 / 维护提示词（新会话从这里开始）

> **用途**：把下方「★ 从这里开始粘贴」整段贴到新会话开头，模型即获得项目全部上下文——
> 现状、硬性约束、已踩过的坑、既定决策、待办路线图与验证方式，不必再从零考古。
>
> **本文件由三份文档合并而成**（2026-09-10）：`REFACTOR_PROMPT.md`（开发守则 / 坑清单 / 既定决策）
> + `STRUCTURE_OPTIMIZE_PROMPT.md` 与 `STRUCTURE_OPTIMIZE_PLAN.md`（结构优化任务清单，
> 已全部执行完毕）。三份原文件已删除，内容要么已落地、要么并入本文件。
>
> **配套文档分工**：
> - [README.md](./README.md)：结构树 / 分层职责 / 配置 / 启动 / 安装 / 扩展指南（**权威结构文档**，
>   目录树只此一份，本文件不再复制，避免两处对不上）；
> - **本文件**：硬性约束 / 安全不变量 / 已踩的坑 / 既定决策 / 路线图待办 / 验证方式；
> - [docs/configuration.md](./docs/configuration.md)：所有运行时 YAML 配置字段、默认值、约束与示例。
> - [docs/database.md](./docs/database.md)：SQLite 库结构详解（八张表逐字段含义、表间关系、
>   常用查询、清理与迁移规则）。**改表结构时同步它。**
> - [docs/auth.md](./docs/auth.md)：鉴权与密钥（本项目的密钥清单、生成、配法、用法、
>   Alertmanager 侧配置、故障排查）。**改鉴权行为时同步它。**

---

## ★ 从这里开始粘贴

你在仓库根 `c:\Users\yqyZ\Desktop\Assitant`（Windows，conda 环境 `langchain1.2`，
Python = `D:\MiniConda\envs\langchain1.2\python.exe`；shell 为 Git-Bash）。这是一个**已可运行**的
「智能运维 Agent」：FastAPI 服务 + LangChain 聊天 Agent + 告警写命令目录自动处置 + AI 只读主机诊断。

动手前先读 [README.md](./README.md) 的「项目结构 / 分层职责」与本文件的
「硬性约束 / 安全不变量 / 已踩过的坑 / 既定决策」；拿不准时进入计划模式先征询方案。
**既定决策（见「既定决策记录」）不要推翻。**
全程**离线验证**：不发起任何真实 DeepSeek API / Prometheus / SSH 调用（构造 Agent 可以，invoke 不行）。

---

## 硬性约束（违反即改错）

1. 根目录 **`init.py` 只读**：它是用户的模板，也是**唯一**的模型入口（`init_chat_model(...)`）。
   换模型/provider/参数只改它；新代码一律复用它的模型，不自己 new 客户端。
   取模型统一走 [src/utils/model_loader.py](src/utils/model_loader.py) 的 `get_model()`
   （进程内单例缓存，聊天 Agent 与 LLM 客户端共用同一份）。
2. **不修改 `.env`；不回显 / 不新增 / 不探明任何明文密钥**。真实 SSH 密码只以
   `${SSH_PASSWORD_192_168_20_128}` 这类**占位符**出现在 `config/*.yaml`，真实值由用户自己填进 `.env`；
   代码/文档/注释/README 一律不落明文。用户要求列出 `.env` 的键或值时——**不做**，说明理由即可。
3. **分层依赖方向**：`api/ → core/ → interfaces/`；core 只依赖 `interfaces/` 抽象，
   **不得 import 插件实现，也不得 import `src.agent.*`**（2026-09-10 已修：
   告警分析提示词下沉到 [src/core/prompts.py](src/core/prompts.py)）。具体实现放 `plugins/`：
   monitors / llms / diagnostics 是 import 即注册进 `src/registry.py` 的插件类；
   **actions（写命令）例外**——M1b 起收敛成 `config/actions.yaml` 目录 + 通用执行器，
   由 `container.executors()` 按目录构建，不再注册固定类。
   共享内核在 `plugins/common/`（endpoint/ssh/audit/cmddef）。`container.py` 按 settings 惰性装配并缓存。
   **`utils/` 是叶子层**：不 import `src` 内任何东西（`model_loader` 只依赖同层的 `config_loader.ROOT_DIR`）。
4. **顶层不 import 重量依赖**：`langchain` / `langgraph` / `paramiko` / `dotenv` 一律在函数内惰性 import。
   改完必须跑 `tests/test_no_langchain_import.py`（它在子进程里把 langchain/dotenv 屏蔽为「未安装」，
   再 import container/core/plugins/utils 并验证聊天回落关键词）。
5. **Agent 两条工具通道，边界不同，别混**：
   - 监控工具（读指标/告警）与只读诊断工具 = **可自动调用**（只读、白名单命令、服务端参数校验、
     凭据不进模型），见 [src/agent/tools.py](src/agent/tools.py) 的 `build_tools` / `build_diag_tools`；
   - **写动作永不直接暴露给模型自由选**：两条路径都是「管理员声明的白名单 + 目录 key」——
     规则路径 = `config/actions.yaml` + `rules.yaml` 引用（**无头自动执行**）；
     Agent 路径 = `config/agent_actions.yaml` + `build_agent_action_tools`（**强制人工审批**）。
     两条白名单互相独立，**不要在它们之间共享条目**，也不要让 Agent 路径的写命令进
     `container.executors()`（那会让它绕过审批被规则无头执行）。
6. **安全不变量**（新增任何 Agent 能力先逐条对号）：
   - 模型上下文 / 工具参数里**永无** host / 密码 / 私钥 / 自由 shell 命令；
   - 命令串 = 配置里声明的模板 + 逐参校验并 `shlex.quote` 的值；模型自由文本永不拼进 shell；
   - **只读诊断恒不 sudo**；动作（写命令）的 sudo 只按 `config/actions.yaml` 每条命令的管理员声明，
     规则不能越权把非 sudo 项提权；凭据只在配置 target/规则（密码 `.env` 占位插值），构造后留在实例内。
7. **每个真实 SSH 尝试写审计**：`ops_agent.audit` logger，结构 `{action, command, host,
   params(redact_sensitive), started_at, finished_at, result/error}`，**绝不含密码**；整段输出不落日志。
   写命令执行器经 [src/plugins/common/audit.py](src/plugins/common/audit.py) 统一写入
   （不再有「plugins/actions 基类」这种东西——M1b 已删）。
8. 工具函数**返回 str**，绝不把异常抛给 Agent；监控能力的「取数+格式化+捕获 MonitorError 转可读文本」
   收敛在 [src/core/capabilities.py](src/core/capabilities.py)（无 langchain 纯函数），Agent 工具与
   关键词兜底都转发它——**同一能力只写一处**；远程**非零退出码不算工具失败**（grep 无匹配、
   systemctl 查 failed 服务常返回非 0）——用 `ok=False` 连输出回传，由 Agent 自行判断。
9. 改 `config/diagnostics.yaml` / `config/actions.yaml` / `config/agent_actions.yaml` /
   注册新插件 / 新增工具 → **需重启进程**才生效（Agent 工具集在首次聊天构建时定稿；
   executors 在容器首次装配时按目录构建并缓存）；
   唯一例外是 `RuleEngine.reload_rules()`（热更新，已支持）。
10. **`config/*.yaml` 的正文与注释都不要出现字面 `${`** ——`interpolate()` 对整个文件文本替换，
    注释里的 `${...}` 也会被当环境变量，冒出误导性日志。
11. 测试纪律：`python -m pytest tests -q`（仓库根，conda `langchain1.2`），**全部离线**——
    **这条现在有闸门强制执行**（[tests/conftest.py](tests/conftest.py) 的
    `_no_real_model_in_tests` 与 `_no_external_network_in_tests`，见坑表那两行），
    不再是靠自觉；
    用 [tests/conftest.py](tests/conftest.py) 的 `FakeMonitor` / `FakeLLM` / `FakeDiagnostics` 注入；
    新能力要带「屏蔽 langchain 后仍可 import + 聊天回落关键词」的回归（照
    [tests/test_no_langchain_import.py](tests/test_no_langchain_import.py) 的子进程屏蔽法写）。
    **当前基线：535 passed**（2026-09-13 鉴权 + 审批归属 + 决策落盘 + 决策人落库完成后；
    当日从 411 起共增 124：鉴权骨架 31 + 会话改造 45 + `/alert` 白名单 9 +
    审批归属 14 + 决策落盘与结果未知 15 + 决策人 10）。鉴权骨架批次的 31 例 = `test_api_auth.py` 16、
    `test_utils_auth.py` 14、既有文件 +1。
    M4 当日共增 109 例：
    `test_sqlite_db.py` 15、`test_session_store.py` 43、`test_api_sessions.py` 18、
    `test_chat_history.py` 14、`test_history_capable.py` 9、既有文件 +10）。
    M3 的 79 例其中
    `test_agent_actions_hitl_graph.py` 用**真 graph + 脚本化假模型**验证审批语义，
    含一条覆盖「容器 → 工厂 → OpsAssistant → 执行」的端到端装配用例——
    单件全绿但接线接错时只有它会红）。
12. **文档同步纪律（2026-09-10 用户明确要求）**：**每次对项目做修改，都要及时同步更新
    「代码注释 + 计划书（本文件）+ README」，不得先欠着、不许留到「以后补」。**
    与「已踩过的坑」表里那条「文档与代码互相误导」是同一件事的两面——那条讲**怎么删**旧内容，
    这条讲**什么时候必须动**。三个载体各自的更新触发点：

    | 载体 | 什么时候必须改 |
    |---|---|
    | 代码注释 / docstring | 随代码**同一个 commit**。解释行为、坑、边界的段落跟着改；**解释「已不存在的东西」的段落直接删**，不写「原 X 已被 Y 取代」 |
    | **本文件（计划书）** | 待做做完 → 从「路线图」移走并在「已实现能力」登记；新踩的坑 → 进「已踩过的坑」表；既定决策有变 → 显式写**修订**及其边界；测试计数（当前 **607**）随用例数更新 |
    | README.md | 结构树 / 分层职责 / 配置 / 路由表 / 出厂默认开关一览 / 测试计数（**结构权威文档**）——动了任何一项都要同步 |
    | [docs/database.md](docs/database.md) | **改表结构 / 加字段 / 改字段语义 / 改清理策略时必须同步**——它是库结构的逐字段说明。迁移那节的两条硬性约束（结构指纹一致、新列加末尾）也在那里 |
    | [docs/auth.md](docs/auth.md) | **改鉴权行为时必须同步**——新增/改动密钥种类、`auth` 段语义、豁免端点、401/404 规则、Alertmanager 侧配法，都要改它。它是鉴权细节的**唯一**出处，README 只留摘要 |

    **特别注意「两处都写了同一件事」**：README 与本文件经常重复描述同一条约束
    （例：对外 HTTP 契约、`agent_actions` 的开关状态、配置热更新的现状）。改一处时
    **先搜另一处**，否则就是本项目反复出现的「两处对不上」问题类型——历史上一次集中清理
    就修掉了 7 处这类问题。**验证方式**：把这一条当 review 清单逐项打勾，不靠记忆。

---

## 项目现状（已实现能力）

- **AI 聊天**：DeepSeek Agent 自主调用监控工具（CPU/内存/磁盘/活动告警）答问题；无 LLM/key 时
  回落到 `core/chat.py` 关键词兜底（CPU/内存/磁盘/告警/状态/帮助），服务照常。
  工具与兜底转发同一批 `core/capabilities.py` 纯函数（M2），产出同款规范文案。
- **AI 只读主机诊断**：监控解释不了的怪现象，Agent 可调 `config/diagnostics.yaml` 白名单只读命令
  （`memory`/`disk`/`top_processes`/`service_status`/`service_logs`）经 SSH 到目标主机排查；
  命令模板 + 参数校验全配置化，工具按白名单动态生成（模型只选命令名 + 语义参数）。
- **Prometheus 指标查询**：即时值 + 按实例近 1h 趋势。
- **Agent 写命令 + 强制人工审批**（M3，2026-09-10）：Agent 可申请执行
  `config/agent_actions.yaml` 声明的写命令 → `HumanInTheLoopMiddleware` 在**执行前中断**
  → `/approvals` 人工批准后才真正执行。凭据来自该文件 `target`（模型不可见），
  sudo 逐条声明；未批准 / 过期 / 重启 / 淘汰 → 命令永不执行。
  **本仓库自 2026-09-10 起 `enabled: true`**（用户要求开作示例；原设计出厂关闭）。
  **与规则自动处置完全隔离**（两条白名单各自独立）。
- **告警自动处置**：Alertmanager webhook 接收（`POST /alert`）+ 最近记录 + active 查询；
  `config/actions.yaml` 写命令目录（模板+参数+sudo）→ 通用执行器按目录 key 执行（带校验 + 审计）；
  只读诊断与写命令共用 `plugins/common`（endpoint/ssh/audit/cmddef）内核，两条入口/边界分离；
  规则表外的告警 → LLM 根因假设与建议（`[AI 分析]`），分析失败仅记日志不阻塞。
- **对话历史 + 多轮对话**（M4 第一/二批，2026-09-11）：[src/storage/db.py](src/storage/db.py)
  从空 TODO 占位变成真实现（连接工厂 + 当前八张表 + `PRAGMA user_version` 双向校验，当前 schema v6）；
  [src/storage/session.py](src/storage/session.py) 提供会话/消息读写（SQLite + 内存两套实现）；
  `/chat` 支持多轮（传回上次的 `session_id` 即带上历史，**路线 B**：历史表是唯一真相、
  每轮新 thread_id、checkpointer 仍只当审批草稿纸）；`/sessions` 三个端点可查可删；
  审批中断的整轮落 `pending_approval`、审批后**原地回填**最终回答；
  开库时把残留的孤儿审批轮标 `abandoned`。**零新增依赖**（stdlib `sqlite3`）。
  三级降级：配置关 → 不落库；SQLite 打不开 → 内存实现（多轮可用、重启即丢）；正常 → SQLite。
  **会话名**（`sessions.name`，v2 引入 / **v3 定位修订** / **09-12 再收尾**）：
  **选中会话只认 `session_id`**；名字是**给人识别的标签**，**不参与选中会话、不要求唯一**
  （同名两次 = 两段新对话）。**建会话时给**（`POST /sessions` 的 `name`），
  **改名走 `PATCH /sessions/{id}`**（空串 = 清除标签）——`/chat` 已无此字段。
  **见下方「会话名定位修订」及其「后续修订」。**
- **状态日志归档 + 审计落文件**（M4 第二批，2026-09-11）：
  `POST /alert` 的处理产出（告警 / 处置结果 / AI 建议）落三张表，靠 `Alert.alert_key`
  关联（**不是裸 fingerprint**——它默认空串，做 join key 会把不同告警并成一条）；
  `GET /alerts/history` 可跨重启查。写入放在 `process_alert` **末尾、一次性、单事务**，
  失败只记 ERROR——不给告警接收新增失败模式。
  审计经 `RotatingFileHandler` 落到 `logs/audit.log`（默认 5 MiB × 3 份），
  **handler 幂等**（`create_app` 会被调很多次，重复挂会让每条写 N 遍并争抢文件）。
- **API 鉴权（密钥 → 身份）**（2026-09-12）：[src/utils/auth.py](src/utils/auth.py) 是纯逻辑
  （JSON 映射解析 + `hmac.compare_digest` 全量比对 + 启动期校验，只依赖 stdlib），
  [src/api/deps.py](src/api/deps.py) 的 `require_auth` 是 FastAPI 接线。
  挂载用 `include_router(..., dependencies=[Depends(require_auth)])`（[src/main.py](src/main.py)），
  **fail-closed**：要豁免必须显式写出来，目前只有 `GET /health`。
  身份由**密钥**推出（服务端事实），请求体里自报的用户名一律不采信。
  `auth.enabled` 的 dataclass 默认是 **false**（同 `storage.enabled`：
  `create_app(settings=Settings())` 在测试里被调多次）；yaml 出厂现已为 true；直接构造 `Settings()` 的测试默认仍是 false。
  `enabled: true` 却解析不出任何密钥 → **拒绝启动**（绝不「空密钥 = 放行」）。
  **`POST /alert` 另有一层授权**（2026-09-13）：身份必须是 `auth.alert_identity`
  （默认 `alertmanager`），其他有效密钥 → **403**。因为这条路径把告警喂给 `RuleEngine`，
  `auto_execute: true` 的规则**直接执行写命令、不经审批**——不设限就等于每个聊天用户
  都能触发写入。留空 = 不限制，但启动时有一条 WARNING。细节见
  [docs/auth.md](docs/auth.md) 3.9。
- **待审批项的归属隔离**（2026-09-13）：`/approvals` 只看得到、也只批得动**自己触发**的
  待审批项（别人的一律 404，与「未知/已过期/已处理」同形）。靠两样东西：
  ① `ApprovalContextCapable` 新协议——`OpsAssistant.reply_turn_ctx(message, *, history,
  session_id, user)` 把会话与身份在**中断登记那一刻**记进 `ApprovalStore` 的记录
  （Agent 自己不知道这两者，只能由 core 层送进去）；`reply_turn` 保留并转发给它，
  既有调用方与所有测试假对象一行未改；
  ② `interfaces.approval.may_decide(pending, identity)`——api 两个端点共用的**唯一**判定
  （与会话那边的 `is_owner` 同款语义）。`identity is None`（鉴权关闭）→ 不校验。
  **决策人**两处都记：① 落库（`approval_decisions.decided_by`，v4 加的列）——
  日志会轮转被冲掉，而这张表 TTL 是 7 天，等你追问「那条 unknown 是谁批的」时
  日志早没了、库里那行还在；② `POST /approvals` 在命令执行**之前**往 `ops_agent.audit` 写一条
  `{action: "approval_decision", approval_id, decision, decided_by, reason, commands, decided_at}`
  ——写在之前是因为进程若死在执行中途，事后追责最需要的正是「谁批的」这一条
  （命令本身由执行器另写 `record_execution`，两条对照能还原完整经过）。
  队列里没有的 id / 越权尝试**不写**审计，免得留一串「批准」的假记录。
- **审批决策落盘 + 结果未知状态**（2026-09-13）：`approval_decisions` 表**不再是空表**。
  `decide()` 在 **pop 与 resume 之间**落一条决策记录（那是唯一能覆盖风险窗口的位置，
  core/api 都只能在这个区间外面动手，所以由 `OpsAssistant` 自己做，走窄接口
  `interfaces.session.ApprovalLedger`）。重启后 `reconcile_pending` 据此把孤儿 pending 轮
  分成两类：**有**决策记录 → 新状态 `unknown`（命令可能已执行，启动时 WARNING 喊出来）；
  **没有** → `abandoned`（确定没执行）。**写失败会中止这次决策、命令不执行**——
  那样调用方给出的「没执行」结论仍然是对的。
  另一条同源遗留一并修掉：审批项被 **TTL/容量**淘汰时，**当场**把会话里那轮收敛掉
  （`_purge_expired` 与 `_register_pending` 两条淘汰路径都要，漏一条就留行）；
  对账只在真淘汰时发生，普通轮次零额外开销。
- **会话归属（用户之间真正隔离）**（2026-09-12）：判定只有一处实现——
  [src/interfaces/session.py](src/interfaces/session.py) 的 `is_owner(info, identity)`：
  `identity is None`（鉴权关闭）不校验，否则要求 `user` **精确匹配**（取「一律拒绝」，
  空 `user` 的存量行任何身份都不匹配）。api 层（`/sessions/*`）与 core 层
  （`/chat` 的 `_resolve_session`）**共用**它——两处各写一遍必然漂移。
  不匹配一律 **404**（不是 403），不泄露「这个 id 存在，只是不是你的」。
  建会话只认 `POST /sessions`（`/chat` 的 `session_id` 是必填字段），改名走 `PATCH`，
  删除时**归属检查排在作废审批之前**（`cancel_pending` 不可逆）。

- **生产化加固（2026-09-15）**：
  - 审批 `pop()` 加锁并原子化，避免并发重复续跑写命令；
  - settings 严格拒绝未知字段和非法布尔值，配置缺失/损坏不再静默回退到无鉴权；
  - SSH 默认 `RejectPolicy` + known_hosts，未知主机不再 `AutoAddPolicy`；
  - 自动处置增加 `target_instance` 精确校验和 SQLite 幂等抢占（schema v5 `remediation_claims`）；
  - `/health` 同时反映会话与归档存储降级；
  - 增加 `agent.enabled`、递归上限、聊天消息上限、模型超时/重试/Token 上限；
  - `auth.approval_mode=approvers_only` 支持审批职责分离；
  - 新增固定指标自动巡检、独立轮转日志与异常归档（schema v6）；
  - CI + pyproject 测试配置 + 文档同步。

- **固定指标自动巡检**（2026-09-15）：应用 lifespan 启动单进程后台线程，按全局周期只读检查
  `metrics.yaml` 的 inspection surface（代码目录内置 CPU/Memory/Disk/Network/System 17 个指标）；每类指标可独立开关并配置 warning/critical 阈值。每轮写
  `ops_agent.inspection` 日志和 `logs/inspection.log`，等级映射为 normal=INFO、warning=WARNING、
  critical/error=ERROR；只有异常轮次写入 `inspection_runs`，可通过 `GET /inspections/recent` 查询。
  **不调用 LLM、RuleEngine 或任何写命令**；当前只支持单 worker。

- **统一指标目录（2026-09-15）**：PromQL 和指标元数据集中在 `src/metrics/catalog.py`，内置
  CPU/Memory/Disk/Network/System 17 个指标；`config/metrics.yaml` 只配置启用、surface、阈值、
  severity、instance 和 selector。巡检、聊天工具、关键词兜底、告警 AI 趋势统一走
  `MonitorSource.query_metric/query_metric_trend`；旧 `cpu_usage/memory_usage/disk_usage/instance_trend`
  保留为兼容包装。`rules.yaml` 仍按 alertname 匹配，SSH diagnostics 白名单保持不变。

### 分层边界（一句话版；完整目录树见 [README.md](./README.md)）

- `api/` 薄 HTTP 层，只做转发；`core/` 业务核心，只依赖 `interfaces/` 抽象；
  `interfaces/` 零第三方依赖的契约层；`plugins/` 具体实现；`utils/` 叶子层基础设施。
- **关键装配链路**：`settings → monitor(llm/executors←actions.yaml 目录) → ChatService(assistant_factory)
  → 首次聊天 OpsAssistant(monitor, diag) → tools = build_tools + (有 diag 才) build_diag_tools`。
- 输出口：处置结果与 AI 建议的 `print()` **只在 `core/agent.py`**
  （`_print_remediation` / `_print_ai_advice`）；`RuleEngine` 是纯决策，不做任何输出。

---

## 已踩过的坑 → 后续怎么写（改代码前先读，重点）

| 问题（发生过） | 规矩（后续怎么写） |
|---|---|
| `from langchain.tools import StructuredTool` 报 ImportError（langchain 1.2.12 **不** re-export） | 一律 `from langchain_core.tools import StructuredTool`；`@tool` 装饰器在 `langchain.tools`。 |
| config yaml **注释里写了 `${...}` 字面量** → 被 `config_loader.interpolate()` 当环境变量替换，冒出误导性「环境变量未设置」日志 | interpolate() 对**整个文件文本**（含注释）做 `${VAR}` 替换 → 任何 config/*.yaml 的正文与注释**都不要出现字面 `${`**；要举例请改措辞或写「以 `SSH_PASSWORD…` 占位」。 |
| 工具/命令集的构建时机 | 诊断工具集在 **Agent 首次聊天构建时定稿**：加命令 / 加插件 / 调白名单 → **重启进程**，不是热更新；改动后文档要写明「需重启生效」。 |
| 命令 key 同时是 Agent 工具名 | 须匹配 `^[a-z][a-z0-9_]*$`（小写开头，仅 a-z0-9_），别用中划线/大写/点。 |
| 设过 `output_limit` 下限 200 → 测试样例全挂 | 校验边界按**语义**定（min=1 即可），别拍脑袋设大值；保证 SAMPLE/测试数据不被你自己的下限卡死。 |
| 模板占位与参数声明对不齐 | 命令模板的 `{name}` 集合**必须等于** `params` 声明的名字集合：模板含未声明占位 / 声明了未使用 → **配置加载失败并关闭该能力**（静默放行打字错误）。str 参数必须声明 `pattern`（`re.fullmatch` 全匹配）；int 用 min/max 收敛。 |
| pydantic v1/v2 对字段约束（ge/re/pattern）表现不一 | 动态工具 args schema 用 `pydantic.create_model` 建，但字段**不加**取值约束；所有校验（clamp / 全匹配 / 拒绝多余参数）收敛到服务端 `validated_params`，模型给的越界值被规约而不是弹 schema 错。 |
| 插件多了一类只记得加 `__init__.py` 注册 | 新增插件类别是**四处配套**：interfaces 加契约 → plugins 写实现 + 注册 → registry.py 加类别与 `register_*/names()/has_*` → container.py 惰性装配 + `try import` 可选依赖 + 缺失降级（返回 None / 空，服务照常）。M1b 例外：**actions 写命令不再注册固定类**。 |
| 忘了「无 langchain 也得出厂」 | 顶层 import 别碰 langchain/paramiko/dotenv；加能力后在屏蔽子进程里验证 container/core/plugins 仍可 import、聊天回落关键词。 |
| 工具把「查询失败」当异常抛 | 监控/诊断工具内部捕获 `MonitorError`/`DiagnosticError` 返回**可读 str**，绝不抛到 Agent；连接失败、参数被拒都只是普通返回。 |
| 同一监控能力写两遍（@tool 与兜底 handler 各查各的、措辞漂移） | M2 起能力**唯一实现** = `core/capabilities.py` 的 `xxx_text(monitor)` 纯函数（无 langchain，供兜底 import）；加能力只在此写「取数+格式化+失败文案」，再到 `agent/tools.py` build_tools 与 `core/chat.py` 规则各**转发一行**。Agent 工具输出 ≠ 兜底输出 = 改错位置了（`test_chat.py` 有同源回归锁死）。 |
| 给模型自由参数/自由命令 | **永不**。模型只选命令名 + 语义参数；连接凭据只在 config target，构造后留实例内；str 参数全匹配后再 `shlex.quote`，int 十进制；命令模板静态部分 = 管理员可信文本。 |
| **`create_agent` 对重名工具不报错**（2026-09-10 实测，静默遮蔽） | 命令 key 即工具名 → 写命令 key 与监控工具（`cpu_usage` 等）或只读诊断命令重名会被悄悄盖掉。冲突检测放在 **`container.agent_actions()`**（不是 assistant——那里抛异常会让 `ChatService._agent_broken` 置位、整条聊天回落）；命中即关本能力并记 ERROR。`RESERVED_AGENT_TOOL_NAMES` 有测试锁其与 `build_tools` 一致。 |
| **中断不是异常 / 错误 resume 负载会永久毒化线程**（2026-09-10 实测） | 四个必须记住的语义：① 中断时 `invoke` 正常返回 `{"__interrupt__": [...]}`——`reply()` 必须**先判中断**，否则 `_last_ai_content` 会返回「抱歉，我暂时无法回答这个问题。」把待审批伪装成失败；② 有 checkpointer 时**必须**传 `thread_id`，否则抛 `ValueError` 被 `ChatService` catch-all 吞掉 → 整条聊天静默回落关键词；③ resume 必须用**存下来的那个** thread_id，错的会静默无操作；④ 决策条数/类型不合法 → LangGraph 把错负载缓存为 resume 值，**该线程之后连正确负载都执行不了** → 故 `decide()` 必须**先校验后 resume**、且审批 id **先 pop 后执行**（单次性）。 |
| 真实密码入日志/文档 | 审计 params 一律 `redact_sensitive`；新写例子/注释/文档只允许 `${VAR}` 占位，真实值只存在用户 `.env`。 |
| （已修 2026-09-04）根 `requirements.txt` 曾被写成 **UTF-16**，`pip install -r` 直接挂 | 依赖/文档文件一律 **UTF-8**；requirements 分「核心 / 可选①聊天+AI 分析 / 可选②SSH+只读诊断 / 可选③测试」，与 README「安装与依赖」保持一致。 |
| **`Any` 退化成「看不见」的契约**（2026-09-10 踩到） | 层间边界一律标真实接口（`Assistant` Protocol / `RuleEngine` / `ActionExecutor`），不用 `Any`；**删/改 import 后用静态检查或全仓注解求值扫一遍**——因 `from __future__ import annotations` 使注解不求值，`Any` 被误删后测试与运行全绿，只有类型检查器会报。 |
| 文档与代码互相误导（2026-09-10 集中清理过一轮，一次修掉 7 处） | 代码改了就同步文档；**解释「已不存在的东西」的段落一律删掉**（如「原 X 已被 Y 取代而移除」），只留仍然有用的约束性说明。**完整规则见「硬性约束 #12」，动手前当 review 清单逐项打勾。** |
| **`with sqlite3.connect(p) as conn:` 不关连接**（2026-09-11 M4） | 连接上下文管理器**只提交/回滚事务**。漏一个 handle，Windows 上表现为 `database is locked`、db 文件删不掉、同步盘冲突。必须 `with closing(sqlite3.connect(...)) as conn, conn:`（见 [db.py](src/storage/db.py) 的连接工厂）。 |
| **sqlite 的 `foreign_keys` 是「每连接」pragma 且默认 OFF**（2026-09-11 M4） | 建表时写 `REFERENCES` **不产生任何约束**。本项目索性不声明外键、删父子数据时**显式同事务删子表**（见 [storage/session.py](src/storage/session.py) 的 `delete`），别假装有级联。 |
| **WAL 在网络盘 / 同步盘上开不起来**（2026-09-11 M4） | 仓库在 `Desktop` 下，OneDrive 常接管桌面，而同步盘不支持 WAL 所需的共享内存。开 WAL 失败要**回落 `journal_mode=DELETE` 并只记 warning**，不能整体关闭持久化——否则「能不能用历史」取决于一个无关的同步客户端。另：`PRAGMA user_version` 版本**高于**当前代码时（文件来自更新的代码）必须记 ERROR 并拒绝使用，**绝不写入**。 |
| **TTL 靠字符串比较，时间戳格式必须逐字一致**（2026-09-11 M4） | 清理是 `WHERE created_at < cutoff` 的字典序比较，只有同为 UTC + `timespec="seconds"` 时才等价于时间序。写成别的精度或本地时区 → 比较恒为「未过期」，清理**静默失效**且不报错。格式原先在 6 个文件各写一遍，已收敛到 [src/utils/time.py](src/utils/time.py)，**别再写第 7 份**。 |
| **审批中断要标「整轮」，不能只标 assistant 那行**（2026-09-11 M4） | 一轮 = user + assistant 两行。若只有 assistant 行是 `pending_approval`、user 行仍是 `final`，下一轮的历史窗口里就会出现一条**没有回答的提问**，模型会把旧问题当新问题再答一遍。两行必须共享同一个 `status` / `approval_id`（见 `SessionStore.append_turn`）。 |
| **`invoke` 返回的 `messages` 是「整条线程的累积 state」，含你传进去的历史**（2026-09-11 实测） | 输入 3 条 → 返回 4 条，且**首条就是输入首条**。因此 `_last_ai_content` 若反向扫全部消息，本轮 AI 的 content 为空时（模型没给文本 / 中断轮）会捞到**上一轮的旧回答**返回给用户——静默串味，看起来像个正常答案，比返回兜底文案危险得多。规矩：取本轮回答要**止步于最后一条 human 消息**（见 `_last_ai_content`）。回归用例 `test_last_ai_content_ignores_history`。 |
| **存储层「返回被淘汰的记录」必须返回整条，不能只回 id**（2026-09-11 修） | `ApprovalStore.add()` 原返回 `{"approval_id": …}`，而 docstring 声称「返回是为了让调用方 delete_thread」——调用方拿到后只能 `_delete_thread(None)`，静默无操作，**线程照旧泄漏**。契约与实现不符时，最危险的不是报错而是「看起来做了」。凡返回值声称供调用方清理/续作用途的，必须带上它真正需要的字段。 |
| **单例服务里的惰性构建必须加锁**（2026-09-11 修） | `ChatService` 是单例、路由跑在线程池，`_ensure_assistant` 无锁时并发首聊会各建一个 Agent——**连带各建一份 `ApprovalStore` 与 `InMemorySaver`**，于是待审批项与 checkpoint 线程分散到两个实例，表现为「刚批准的命令找不到线程」。修法是持锁 + 双检（锁只护首次构建）。加任何新的惰性单例时照此办理。 |
| **给既有方法「加一个带默认值的 kwarg」不等于向后兼容**（2026-09-11） | `runtime_checkable` 的 Protocol **只检验方法名是否存在、不检验签名**。所以给 `Assistant.reply` 加 `history=None` 后，只实现 `reply(self, message)` 的既有假对象仍会被 `isinstance(x, HistoryCapable)` 判为 True，然后在调用处 TypeError——**守卫形同虚设**。多轮能力因此另起方法名 `reply_turn`（照 `ApprovalCapable`/`ApprovalCancellable` 的先例）。加能力一律**新方法名 + 新 Protocol**，不要改既有方法的签名。 |
| **持久化/IO 调用不能落在 Agent 的 catch-all try 里**（2026-09-11） | `ChatService` 那条 `except Exception` 会把任何异常归类成「LLM 抖动」并回落关键词，且只 warning、无 `exc_info`。DB 读写若圈在里面，schema 写错、盘满、权限错最终都表现为「模型最近有点飘」，永远查不出来。落库必须放在 try **之外**，有自己的错误处理与可观测出口（`/health` 的 `storage` 字段）。 |
| **模型窗口里的「控制面消息」要过滤掉**（2026-09-11） | 待审批提示（`⚠️ 待人工审批…审批 id：xxx`）是给人看的，进模型上下文会让模型在后续轮次里幻觉「还在等批准」。会话表用 `status` 区分：`history()` 只返回 `final`、`messages()` 返回全部——**同一张表两个读口**，分别服务「给模型看」与「给人看」。 |
| **`database is locked` 的常见成因是「外部的查看器」，不一定是有人抢着写**（2026-09-12 用户报障） | WAL 模式下读者不挡写者，**但读者会挡住 checkpoint**；WAL 涨到阈值时写操作必须先 checkpoint，于是持续拿不到锁 → `database is locked`。实测：用户开着 **DB Browser for SQLite** 看库，服务端写入失败（`busy_timeout` 重试 5 秒后放弃）。**规矩**：不要一边跑服务一边用外部工具开着同一个 `.db`；要查数据优先用服务自己的读接口（`/sessions`、`/alerts/history`），或用完即走的命令行。另注意 `busy_timeout` 只在**单个连接**内自旋，跨进程的长事务它不是解药。 |
| **健康标志位必须「成功时恢复」，不能只置位不撤销**（2026-09-12 修） | 原先 `_fail()` 把 `healthy=False` 之后**永不恢复**，于是一次几秒的瞬时锁就让 `/health` **永远**显示 degraded，而库其实好好的——那样的字段会立刻失去可信度，**比不报还糟**。改为成对的 `_fail()` / `_ok()`：`healthy` 反映「最近一次操作的结果」。**凡是给运维看的状态位都要问一句：故障恢复后它会自己变回去吗？** |
| **跨进程争用要能自己扛过去，别一次失败就丢掉整轮**（2026-09-12 修） | `busy_timeout` 对付同进程内的短争用够用，但跨进程（外部查看器）的长争用会让一次操作直接失败。已在 `Database.execute/executemany/transaction` 加**退避重试**（0 / 0.2 / 0.5 秒，只对 `locked`/`busy` 这类瞬时错误；其它 sqlite 错误立即抛出——重试没有意义）。写测试时注意：**重试循环在 `Database.execute` 内部**，所以模拟故障要打在**连接**那一层（替换 `db.connect`），替换 `db.execute` 会把重试一起换掉、测出来的是「没有重试」。 |
| **`ALTER TABLE` 之后 SQLite 会重写 `sqlite_master.sql`，新旧库的「DDL 文本」永远不可能相同**（2026-09-11 实测踩到） | 所以「迁移后的库 == 全新建的库」这条不变量**不能靠比较 SQL 文本**去验——要比**结构指纹**（表名、每表有序的列定义、索引名）。见 `tests/test_sqlite_db.py::_structure`。另外两件与迁移有关的实事：① ALTER 只能把新列加到**末尾**，所以全新建库的 DDL 也把新列写在末尾，两条路径的**列序**才对得上（否则将来有人写 `INSERT INTO sessions VALUES(...)` 会在新库上正常、在旧库上炸）；② 加在表上的**唯一索引**要在迁移里单独 `CREATE UNIQUE INDEX`（ALTER 加不了表级约束），否则升级上来的库缺这层约束而新库有。 |
| **可选字段的空串要视同「未提供」，否则 Swagger 里清空输入框就会 422**（2026-09-12 用户报障） | **Swagger UI（`/docs`）会把所有字段都填进示例请求体**（`{"user":"string","message":"string","session_id":"string","session_name":"string"}`）。用户把某一栏**清空**（而不是删掉那一行）时，发出去的是 `""`——于是「清空可选字段」这个最常见的操作被判成「填了一个空值」而拒绝。规矩：**任何可选字符串字段**，在校验前先 `strip()`，空则返回 `None`。非字符串要原样放行交给 pydantic 报类型错，别在这里掩盖。反面例子：`session_id: ""` 当时被当成「没给 id」→ 静默新建会话（不报错反而更难发现）。 |
| **用户可见的「标识」类字符串要先归一化，再按 Unicode 类别拒绝——而不是白名单放行**（2026-09-12，同一处改了两次） | ① **必须归一化**：空格有**四种肉眼无法区分的编码**（`U+0020` 普通 / `U+00A0` 不换行 / `U+3000` 全角 / `U+200B` 零宽），全角与半角字母同理，而**中文输入法下按空格很可能打出全角空格**。不归一化的后果：用户以为在续聊同一段对话，服务端却当成全新标识——而他看两行数据长得一模一样，**没有任何线索可查**。做法：`NFKC` + 折叠空白 + 去首尾。② **归一化之后仍要挡零宽**：NFKC **消不掉**零宽空格 `U+200B` 与 BOM `U+FEFF`（它们不是空白的变体，`\s` 也不匹配）。③ **怎么挡：按 Unicode 类别，不要用白名单**。这两次都栽在这里——先写成 ASCII 白名单，被用户问「为什么中文不行」；改成「ASCII + 汉字」白名单，又被问「为什么还要这么严」。**根因是把「枚举危险字符会漏」错误地推广成了「白名单比黑名单好」**：枚举具体的危险字符确实会漏，但**按类别排除（`Cc`/`Cf`/`Cs`/`Co`/`Cn`）是有明确边界、完备的**。真正要挡的只有「看不见的字符」这一类，所有可见文字（假名、重音字母、中文标点、emoji、阿拉伯文）都不在其中——拒绝它们纯属误伤。④ 报错要**打出码点**（`U+200B`），否则零宽字符用户根本无从排查。 |
| **唯一约束遇到「多数行为空」的列要用部分索引**（2026-09-11） | 会话名只有少数会话有。直接 `UNIQUE(user, name)` 会让同一 user 的**第二个无名会话插入失败**（`'' = ''`）。写成 `CREATE UNIQUE INDEX ... ON sessions(user, name) WHERE name <> ''`——既约束了有名字的，又放过了没名字的。凡「可选标识」类字段都照此办理。 |
| **`logging` 的 handler 是全局状态，加 handler 必须幂等**（2026-09-11） | `configure_logging` 每次 `create_app` 都会调用（测试里几十次）。无条件 `addHandler` 会让每条日志写 N 遍，且 N 个 handler 争抢同一文件——**Windows 上轮转 rename 直接 PermissionError**。按路径查重跳过。另外 `RotatingFileHandler` **不创建父目录**（`logs/` 不存在直接抛），且用 `delay=True` 避免启动即占句柄。 |
| **聊天 Agent 的构建「没有任何开关」，测试里必须注入假模型**（2026-09-12 实测踩到两次） | `settings.llm.enabled=false` **只管告警 AI 分析**，聊天走的 `container._assistant_factory()` → `OpsAssistant` → `utils/model_loader.get_model()` → 根 `init.py`，配了 `.env` 的 key 就**真的发请求**。危险之处在于**失败形态是成功**——回复看起来像一段正常的运维回答，而不是报错（我因此误调了两次真模型）。规矩：`tests/conftest.py` 有 autouse 的 `_no_real_model_in_tests` 把 `get_model` 换成「一调用就炸」；用例要么照 `test_agent_actions_hitl_graph.py` 注入脚本化模型，要么给 `ChatService._assistant` 注入假对象。**注意必须在两处打补丁**——`assistant.py` 在模块顶层做了 `from ... import get_model`。 |
| **测试「全绿」不等于「离线」：LangSmith 的后台线程会偷偷发外网请求**（2026-09-12 实测） | 用户 `.env` 里配了 `LANGSMITH_TRACING` 时，langchain 会起一个**后台线程**周期性往 `api.smith.langchain.com` 发 `client.info`——**与有没有真跑模型无关**。所以任何模型层的桩都拦不住它，而且它跑在后台线程里、异常被自己吞掉，**测试照样全绿**。只有在 `socket.socket.connect` 上挂钩子才看得见（实测抓到一个 `34.8.121.39:443`）。规矩：① conftest 顶部（**任何 langchain 导入之前**）把 `LANGSMITH_TRACING` / `LANGCHAIN_TRACING*` 设成 `false`；② 再加一道 `_no_external_network_in_tests` 闸门——非回环地址的 `connect` 一律抛，并由 `pytest_sessionfinish` 兜底把整轮判失败（后台线程吞掉异常也跑不掉）。**排查这类问题的通用手法：在 socket 层挂钩子抓调用栈，比猜哪个库发的准得多。** |
| **`import src.main` 有副作用：模块级 `create_app()` 会按仓库真实配置挂上文件 handler**（2026-09-11 实测踩到） | 后果不只是「测试互相串」——测试里被 stub 掉的 `exec_command` 照样走 `record_execution`，于是**测试套件会往仓库的 `logs/audit.log` 写一堆假数据**（实测跑完留下 32 行 `192.168.1.1` / `h` / `boom`）。已加 `tests/conftest.py` 的 session 级 autouse fixture 摘掉文件 handler；文件落盘本身由 `test_logger.py` 用 `tmp_path` 专门覆盖。**新增任何「按真实配置打开资源」的模块级副作用时，都要同步加这样的隔离**，否则测试会悄悄污染工作区。 |
| **`purge_interval_seconds=0` 时清理会在写入途中就跑**（2026-09-11 踩到） | 「写入时顺带清理」的设计意味着：想手动控制清理时机的测试，必须在断言前**真的关掉**自动清理。而且不能只给个大数——`_last_purge` 初值是 0、clock 是 epoch 秒（1.7e9），**任何有限大数都会在首次写入时被判为「已超间隔」**。要关就传 `float("inf")`。（两次踩到：会话存储与状态日志各一次。） |
| **归档/落盘前要过一遍「这些内容原本进过磁盘吗」**（2026-09-11） | 把只进 console 的数据改为落盘时，等于**升级了它的泄露面**。`remediations.detail` 里含远端命令 stdout，此前因为「整段输出不落日志」的约定而安全；一落盘就成了磁盘明文。新增任何落盘字段前，先问「这段内容原本的可见范围是什么」。 |
| 写测试时：脚本化假模型**用尽后会重复末条**，且**未声明的工具名不会被 HITL 拦截**（2026-09-11 踩到） | ① 想让"跑完一轮"必须显式给一条终结 `AIMessage(content=...)`，否则模型会一直调工具直到撞递归上限（实测报 `GraphRecursionError: Recursion limit of 10000`）；② 测试里写第二/第三条写命令时，命令名**必须在假目录里声明过**，未声明的名字会被当成普通工具直接穿过审批（我曾用 `cleanup_disk` 验证链式审批，结果 `decide` 返回 completed）。 |
| **yaml 里 `KEY: ${VAR:-}` 这种「插值成空」的写法会静默变成 `None`；写成 `{"k":"v"}` 又会被当成流式映射解成 dict**（2026-09-12 实测） | `config_loader` 的插值在 `yaml.safe_load` **之前**跑，所以插值结果的**字面形态**直接决定 YAML 怎么解析它：① 变量未设、占位不带引号 → `tokens: ` → YAML 解成 **`None`**，而 `_as_scalar` 对 `None` 提前返回，dataclass 拿到 `None` 而**不是**字段声明的 `""` 默认值（默认值只对「键缺失」生效，不管「键存在但值为 null」）；② 变量有值、占位不带引号 → `tokens: {"k":"v"}` → YAML 当**流式映射**解成 `dict`，`_as_scalar` 做 `str(dict)` 得到**单引号 repr**，下游 `json.loads` 必炸。**规矩：任何可能插值成空、或内容形如 JSON/含特殊字符的占位，一律用单引号包住**（`'${VAR:-}'`）——实测这一种写法同时解决上面两种。另外 `_ENV_REF` 的默认值部分用 `[^}]*`，**匹配不了 `}`**，所以 `${VAR:-{}}` 这种默认值也是坏的（吃不到最后一个 `}`）。 |
| **`hmac.compare_digest` 收 `str` 且含非 ASCII 会抛 `TypeError` → 500 而不是 401**（2026-09-12 实测） | 常数时间比对有两个容易一起漏掉的点：① **先 `.encode()` 再比**，否则一个含中文的 Authorization 头会把「凭据无效」（401）变成服务端异常（500）；② **遍历全部密钥、不提前 return**，命中第一个也走完，否则响应时间泄露「第几个密钥匹配上了」＝泄露「猜的密钥前缀对不对」。密钥数量是个位数，全遍历的代价可以忽略。见 [src/utils/auth.py](src/utils/auth.py) 的 `resolve_identity`。 |
| **鉴权挂在哪，决定了「漏挂」是哪个方向**（2026-09-12） | 三条路只有一条是 fail-closed：① `@app.middleware("http")` 在**路由匹配之前**跑，拿不到「这条路由要不要鉴权」，只能手工维护路径白名单——**漏掉新路由 = 新路由默认敞开**；② `APIRouter(dependencies=...)` 要改每个 api 模块，将来新增 router 容易忘；③ **`include_router(..., dependencies=[Depends(...)])`**（本仓库采用）：`src/main.py` 是唯一的挂载点，一屏看完，**要豁免必须显式写出来**（只有 `health_api.router` 那行没加）。配套：`/docs`、`/redoc`、`/openapi.json` 是 **app 级**路由、不在任何 router 里，因此不会被拦——不用为它们开后门。兜底测试 `test_api_auth.py` 直接**枚举 `app.routes`** 断言「除豁免外每个都 401」，将来新增路由会被自动覆盖。 |
| **「认证」不等于「授权」：挂上 `require_auth` 之后每条路由都还要问一句「谁**该**能调它」**（2026-09-13 用户实测发现） | 加鉴权时我把 5 个 router 一律挂上 `require_auth`，**默认「只要密钥有效就能调」**。绝大多数端点这么想没问题，但 `POST /alert` 不是——它把告警喂给 `RuleEngine`，而 `auto_execute: true` 的规则**直接执行写命令、不经人工审批**。于是**任何一个持有效密钥的聊天用户都能构造告警去踩那条写路径**（用户自己试出来的：拿 `alice` 的密钥发告警，200，告警还进了归档）。**规矩**：每加一个端点，除了「要不要鉴权」还要问「**要不要限定身份**」；只要它**能产生副作用**（触发执行、改状态、发钱），答案默认是「要」。修法是 `src/api/deps.py` 的 `require_alert_identity`（403，与会话那边的 404 有意区分）。**同类的那条 `/approvals` 已于同日一并修掉**（按归属隔离，见「已实现能力」）。 |
| **测试会打开并迁移仓库里那个真实数据库**（2026-09-13 实测） | `import src.main` 会执行模块级的 `app = create_app()`，它按**仓库真实配置**（`storage.enabled: true`）装配容器 → `open_database(ROOT/data/ops_agent.db)`——而**开库会跑迁移、也会写数据**。实测：只 `import src.main` 就能让那个文件的 mtime 变化；我这轮加 v4 迁移时，跑测试就把用户真实库从 v3 升到了 v4。**规矩**：conftest 里有 `_block_real_database()` 把 `open_database` 换成「只接受仓库外路径」，指向 `data/` 的降级为 None。**它必须写在模块级**——那个 import 发生在**收集阶段**，比任何 fixture 都早。这和坑表里那条 `logs/audit.log` 副作用是同一类：**「按真实配置打开资源」的模块级副作用，测试必须隔离**。|
| **测试助手「把库退回旧版本」时，必须把**后续版本加的列**也撤掉**（2026-09-13 踩到） | `test_sqlite_db.py` 的 `_downgrade_to_v1` 靠改 `user_version` 假装成老库。加 v4 后它没撤 `decided_by`，于是升到 v3→v4 那步撞 `duplicate column name` → 迁移失败、`open_database` 返回 None，而报错信息看着像「迁移写错了」。**规矩**：模拟旧版就是**让它真的变成旧版**，每个后续版本新增的结构（列/索引）都要在助手里面撤干净。|
| **`caplog.at_level(...)` 设在 `create_app()` 之前会被它冲掉**（2026-09-13 踩到） | `create_app` 会调 `configure_logging`，而它对审计 logger 执行的是 `setLevel(NOTSET)`（跟随根级别）——**在它之前设的级别会被重置**，于是 `logger.info` 在**记录产生那一层**就被丢了，caplog 里空空如也，看着像「代码没写审计」而其实是测试自己没用对。**规矩**：`caplog.set_level(...)` 必须在 `create_app()` **之后**调用；分不清是「没写」还是「没抓到」时，先手工挂个 handler 验一遍——我这次就是这样定位的。|
| **同一个概念有两条产生路径时，钩子只挂一条 = 留残**（2026-09-13 踩到） | 待审批项有**两条**消失路径：TTL 过期（走 `ApprovalStore.purge_expired()`，由 `_purge_expired` 处理）与**容量淘汰**（在 `add()` 里发生、由它返回被挤掉的那条，`_register_pending` 处理）。我第一版只在前者挂了「收敛会话里那轮」的钩子，测试立刻抓到后者漏了——漏掉的表现是那轮会**永远**显示「有一条命令等你批」，而 `/approvals` 里根本没有它。**规矩**：加「状态变化后要同步某处」的钩子时，先把「这个状态有几种变化路径」列全，逐个挂；只挂主干那条，边缘路径就会静默留残。|
| **Alertmanager 的凭据文件有问题时不会启动失败，而是静默不发通知**（2026-09-13 实测） | 给 webhook 配 `http_config.authorization.credentials_file` 之后，文件不存在 / 路径写错时，**Alertmanager 照常启动、Web UI 一切正常**，只是通知一条都发不出去——只在**它自己的日志**里留一条 `level=WARN ... "Notify attempt failed, will retry later"`，服务端侧完全无感。**规矩**：改完 receiver 必须**实测投递**（往 `POST /api/v2/alerts` 推一条，再查服务端 `/alerts/recent`），别把「进程起来了」当成配好了。同一轮实测的另两条事实：① `credentials_file` 的**尾随换行会被自动去掉**，文件怎么存都行；② 内联 `credentials:` 也能用，但会让密钥多抄一份进那个**常被提交进仓库 / 打进镜像**的文件，故一律用 `credentials_file`。**方法学**：这次是先用「故意写坏的配置」做**对照实验**，确认验证手法真的能测出配置错误，才发现「两种情形都启动成功」不是手法失灵、而是真结论——报「验证通过」之前先证明你的验证有效。 |
| **`HTTPBearer(auto_error=True)` 缺头时返回 403，不是 401**（2026-09-12） | FastAPI 的已知行为。「没带凭据」语义上是 401（且必须带 `WWW-Authenticate: Bearer` 质询头，否则 Swagger/客户端走不了标准流程），403 是「认证过了但没权限」。故一律 `HTTPBearer(auto_error=False)` 自己抛 401。顺带：`auto_error=False` 时 `credentials` 为 `None` **同时覆盖了**「没带 Authorization 头」与「带了但不是 Bearer scheme」两种情况。 |
| **必填字段的 `after` 校验器返回 `None` 会**真的**得到 `None`——pydantic v2 不重校验返回值**（2026-09-12 实测） | 写 `session_id: str = Field(...)` + 一个「空白就返回 `None`」的校验器，模型里会实实在在存进 `None`（不是报错、也不会回落到默认值）。后果是下游 `if session_id:` 为假 → 走到「没给 id」的分支 → **白开一段会话**，还顺带把 `DELETE` 的语义架空。**规矩**：必填字段的校验器**只能抛 `ValueError`（→422），绝不能用「返回 None」表达「没填」**。只有**可选**字段才可以把空白折成 `None`（那时候类型本来就是 `str | None`）。两种语义不能混用——`session_name` 可选所以折成 None，`session_id` 必填所以必须 422。 |
| **删除是最容易做错顺序的操作：一次未授权的 DELETE 能造成两种不可逆副作用**（2026-09-12，加了归属校验才发现） | 两个都必须堵：① `SqliteSessionStore.delete` 原先**先删 messages 再删 sessions**（同事务）——守卫若只加在 sessions 那条 SQL 上，未授权的删除会**抹掉别人的对话正文**而会话行还在。守卫必须在同一事务内、在**任何写之前**判定。② `api/sessions.py` 原先**先 `cancel_pending` 再 `delete`**——`cancel_pending` 把那一轮永久标成 `abandoned` 且**不可逆**，所以「不是你的会话 → 404」是在白废掉别人一条待执行的写命令之后才返回的。**规矩：任何「有副作用的删除」都要问一句——副作用里有没有不可逆的？有的话，权限判定必须排在它前面。** 回归用例 `test_delete_by_non_owner_does_not_cancel_pending_approvals`。 |

### 既定决策记录（勿推翻）

1. **就地分层重构已完成**：不再扁平化/不再迁回 ops_bot；原目标文档 `obs-agent.txt` 已不在仓库内。
2. **单 Agent 够用**：不引入多 Agent 编排。异常处置走「单 Agent + 规则库 + 分层的**人工升级/审批**」；
   若将来要跑固定的故障应急剧本（runbook），优先用**线性 LangGraph 工作流**而不是并行多 Agent。
   多 Agent 只在出现「上下文膨胀 / 需并行分头排查 / 记忆相互独立」三个信号之一时才考虑。
3. **Agent 动系统 = 独立白名单 + 强制人工审批**（2026-09-10 **修订**，原为「Agent 不动系统」）：
   原决策是「写动作一律走 `config/actions.yaml` + `rules.yaml`，Agent 只读」。
   M3 已实现受控放开，边界如下，**不构成对原决策的推翻而是限缩性扩展**：
   - Agent 的写命令**另立一份白名单** `config/agent_actions.yaml`，**不复用** `actions.yaml`
     （后者仍专属规则无头自动处置）——两条路径各自独立、互不可见；
   - **每次调用强制人工审批**（`HumanInTheLoopMiddleware`），配置层**没有**任何开关能关闭；
     决策只有 approve/reject，**不提供 edit**；
   - 凭据只来自该文件 `target`（模型永不接触 host/密码）；sudo 由管理员逐条声明；
   - **本仓库自 2026-09-10 起 `enabled: true`（用户要求开作示例；关停改回 `false` 并重启进程）**；
     未批准 / 过期 / 重启 / 队列淘汰 → 命令**永不执行**；
   - 审批只加在 **Agent 调用路径**，`RuleEngine` 的无头自动处置**不受影响**
     （`agent_actions` 的执行器绝不进 `container.executors()`）。
   原「Agent 动作工具位」注释已由 `build_agent_action_tools` 落地（返回 `WriteToolBundle`，
   工具与审批策略成对产出）。
4. **模型入口收敛**：一切聊天/AI 分析都复用根 `init.py` 的模型，不各自 new；
   加载逻辑唯一实现在 `src/utils/model_loader.py`（`ModelLoadError` 由调用方映射为各自的 `LLMError`，
   保持 utils 零 src 内依赖）。
5. **诊断单目标**：`config/diagnostics.yaml` 目前只支持一台 `target`（多主机方案见路线图，已定规格）。
6. **Agent 能力单一实现（M2）**：CPU/内存/磁盘/告警监控的「查询+格式化」唯一出处 = `src/core/capabilities.py`
   纯函数（顶层无 langchain，兜底可 import）；`agent/tools.py` 的 @tool 与 `core/chat.py` 关键词动态规则
   都只转发同一批函数，产出同款规范文案（成功 `CPU 使用率：45.0%` / 失败 `查询 CPU 使用率失败：…` /
   无数据 `…暂无数据…`）。**「状态/帮助/DEFAULT_REPLY」保持 fallback-only，不进 capabilities。**
   **文案即规范**：`capabilities` 的输出有测试逐字断言，改措辞必须同步改测试。
7. **能力实现走接口，不走字符串**：`capabilities` 里取值一律传 `MonitorSource` 的方法本身
   （`monitor.cpu_usage`），不用 `getattr(monitor, "cpu_usage")`——字符串派发绕过接口契约，
   拼错只有运行时才炸。

---

## 路线图：待办与已定的扩展方向（按需推进，勿一次全做）

### 已定方案、尚未实现

- **多主机诊断**（**README 已注释指向本文件此段**）：目标 `target` → `targets` 命名映射，
  命令白名单保持共享一份。规格：
  - `DiagnosticSource` 加 `hosts() -> tuple[str,...]`（默认 `()`=单机，行为不变）；`run(command, *,
    host: str | None = None, params=None)`——host 走**独立通道**，不进模板/不进 shell；
  - 工具层 `build_diag_tools`：当 `len(hosts()) > 1` 时给每个命令工具**前置**一个必填 `host` 参数，
    取值 = `Literal[各 host label]`（`Literal.__getitem__(tuple(labels))` 动态构造），描述列可用主机；
    单台仍不生成该字段，保持现网工具签名不变；
  - 配置示例：`targets: { web-01: {host:…, password: "${SSH_PASSWORD_…}"}, db-01: {…} }`，密码按台各占位；
  - **host label 尽量与监控侧 instance 标识对齐**（monitor 输出里模型已见过 `instance=IP:9100`），
    否则模型无法把「刚才告警那台」映射到主机枚举；
  - 每台各拉一条连接、失败互不影响；审计多记 host label。旧单 `target` 格式解析为单元素表兼容。
- ~~**Agent 动作工具**（写系统，M3）~~ —— **2026-09-10 已实现**，见「既定决策记录 #3」。
  落地形态：`config/agent_actions.yaml`（独立白名单）+ `plugins/agent_actions/`
  + `agent/tools.py::build_agent_action_tools`（`WriteToolBundle`）
  + `HumanInTheLoopMiddleware` + `InMemorySaver` + `api/approvals.py`。
  **未做而留待将来**：多主机（host label）选择——当前写命令同样是**单目标**，
  与只读诊断的「多主机诊断」待办同规格，届时两者应一并处理。
- ~~**给 `/approvals/*` 加「归属」维度**~~ —— **2026-09-13 已实现**（本条目由此移入「已实现能力」）。
  （决策人审计也于同日补上，见「已实现能力」。）
- **输出通道抽象（Notifier）**：`print()` 目前只集中在 `core/agent.py` 两处（2026-09-10 已把
  `RuleEngine.print_results` 移入），真要接钉钉/企微时只需替换这一处。**届时再抽 `Notifier` Protocol，
  属设计决策而非缺陷；做之前先问用户**——`print` 内容是用户观察 `auto_execute=true` 效果的主要窗口。
- **结构化输出**（讲义 7.3）：`src/agent/prompts.py` 已留 ToolStrategy + AlertAnalysis schema
  注释与启用示例，默认关。启用前先确认不会把 `core/analysis.py` 拖进 langchain。
- **日志落盘**：目前日志走 console / settings 控级别；未加 RotatingFileHandler。
- **配置热更新（reload 入口）**：`RuleEngine.reload_rules()` 已实现，但**全仓只有 `__init__` 调用它**
  （[src/core/rule_engine.py:60](src/core/rule_engine.py#L60)）——无路由、无 CLI、无脚本。运行中的 HTTP
  服务里**没有任何配置是热的**：`main.py` import 即 `create_app()`，容器在装配期快照
  `settings.yaml` / `actions.yaml`（`executors()` 缓存）/ `diagnostics.yaml` / `agent_actions.yaml`，
  Agent 工具集还在**首次聊天**时定稿。做的时候的要点：
  - 端点形如 `POST /rules/reload`，**必须连带失效 `executors()` 缓存**——只热 reload 规则价值有限：
    引用新命令的规则要先出现在 `actions.yaml`，而规则加载按 `set(self.executors)` 过滤，新动作会被
    **静默丢弃**（[src/core/rule_engine.py:93-96](src/core/rule_engine.py#L93-L96)，只留一行 warning），
    看着 `auto_execute: true` 其实根本没启用；
  - 2026-09-10 用户决定：**先不做，仅登记**。
- **对话历史维护（多轮对话 + 历史记录表）**：2026-09-10 用户提出并判定为「一直被忽略的至关重要功能」。
  **⚠️ 2026-09-11 分两批交付，第二批的「多轮」部分已完成，剩下的只有状态日志：**
  - **第一批（已完成）**：存储底座 + 会话/消息两张表的读写 + `/sessions` 三端点。
    落点 `interfaces/session.py` → `storage/session.py`（SQLite + 内存降级两实现）→
    `container.sessions()`（三级降级：配置关 → None；SQLite 打不开 → 内存实现；正常 → SQLite）。
  - **第二批多轮部分（已完成）**：`reply_turn(message, *, history=())`、`ChatService.reply_turn`
    （落库在 Agent 的 catch-all **之外**，关键词兜底也落库）、`/chat` 带 `session_id`、
    审批中断落 pending 并原地回填、会话删除作废其未决审批、开库回收孤儿审批轮。
  - **第二批状态日志部分（已完成）**：告警/处置/AI 建议三表归档 + `GET /alerts/history` +
    审计落文件。**本条目整体已完成**，见「已实现能力」。

  改造前是**单轮**：`reply()` 每轮只传当前一条消息
  （[src/agent/assistant.py:276](src/agent/assistant.py#L276)），`/chat` 无会话概念
  （`user` 字段**只用于打日志**）。设计已完成，结论与未决点如下，**勿重新考古**：

  **先分清两件事**：「多轮记忆」（模型接得上上句，属线程态）与「历史记录表」（落盘、可查、可回放）
  是两个问题，共用一套会话 id，但**存储决策不同**——这正是下面 A/B 分歧的根源。

  **已定**：
  - 会话 id **显式**：`/chat` 请求体加**可选** `session_id`，不传则新建并在响应回带。
    **绝不复用 `user` 当会话 key**——同一人开两个话题会互相污染，且会悄悄改变一个既有字段的语义；
  - **历史表是唯一真相（推荐路线 B）**：每轮从表取窗口内历史 → 拼新消息 → invoke → 把 user 与回答
    都写回表；`thread_id` 仍每轮新 `uuid4()`、跑完即删，checkpointer 继续只当「审批中断的临时草稿纸」，
    用法与今天完全一致（不与 checkpoint 的 state 打架）。代价：每轮全量重发历史，靠窗口限 token；
  - 备选**路线 A**（`thread_id = session_id`、线程长活、历史全交给 checkpointer，可配官方
    `SummarizationMiddleware` / `ContextEditingMiddleware`）：LangGraph 正统、不必自己传历史，
    但**会架空这张表**——`InMemorySaver` 是进程内存，重启即丢、多 worker 各存一份；要持久得换
    `langgraph-checkpoint-sqlite`（新增依赖），且历史是序列化 blob，查出来展示很别扭。
    **A/B 是绑定的**：让中间件管记忆 ⟺ 必须走 A，**不能既要表是唯一真相又要中间件管历史**；
  - 表用 **stdlib `sqlite3`**（零新增依赖），正好激活 [src/storage/db.py](src/storage/db.py)
    那个空 TODO 占位（它原本就写着「落盘 + 归档」）。两张表：
    `sessions(session_id, user, title, created_at, updated_at, status)` /
    `messages(session_id, seq, role, content, status, approval_id, created_at)`，
    索引 `(session_id, seq)`（`seq` 必须显式递增——同秒时间戳排不准）；
  - **只存 user/assistant 文本，不存 tool 调用与工具返回**：中间那几轮 tool-call 消息既难序列化
    也没必要（下轮模型需要数据会重新调工具），存了反而要维护一整套 `BaseMessage` 序列化；
  - 落点：`interfaces/session.py`（`ChatMessage` 中性类型 + `SessionStore`）
    → `storage/session.py`（sqlite 实现，+ 可选内存实现供降级/测试）
    → `core/chat.py` 持有 store（**core 仍不 import langchain**）
    → `assistant.reply_turn(message, *, history=())` → `api/sessions.py`（`GET /sessions`、
    `GET /sessions/{id}`、`DELETE /sessions/{id}`）。

    **⚠️ 路线图原写「给 `reply` 加带默认值的 kwarg `history=None`」——2026-09-11 评审推翻，
    不采用。** 原因：`runtime_checkable` 只检验**方法名是否存在**、不检验签名，[tests/test_chat.py](tests/test_chat.py)
    里 3 个局部 fake 只实现 `reply(self, message)` → `isinstance(assistant, HistoryCapable)`
    仍判 True，带 kwarg 调用时才 TypeError，**守卫形同虚设**。改用**新方法名**
    `reply_turn()` 返回 `TurnResult`（含 `status` 与 `approval_id`），照 `ApprovalCapable` 的先例。
    另一个原因：`reply` 只回 `str`，而 core 需要 `approval_id` 才能写 `pending_approval` 行——
    只回文本的话 pending 行根本无从落。

  **审批 × 多轮的三个边界（最容易写错，实现前就要定）**：
  1. **中断那轮的回答不在 `reply()` 时产生，而在 `decide()` 时**——表里那条只能先记「待审批提示」
     （`status=pending_approval` + `approval_id`），续跑完再把最终回答补写进去；
     **不补写，历史里就会永远留一段没下文的记录**；
  2. 会话有未决审批时用户又发新消息：技术上安全（每轮线程独立，审批那条线程不受影响），
     建议**允许**，但在会话行上标未决审批数，让前端能提示「这个会话还有一条命令等你批」；
  3. **会话删除 / 过期时，未决审批必须一并作废**（继承 fail-safe：宁可让人重新发起，
     也不能让一个陈旧的中断还挂着等执行）。

  **要同步改的既有约束（别漏）**：`/chat` 加字段是**追加**（同 `/approvals` 的性质），但会碰三处——
  [tests/test_api_approvals.py:112](tests/test_api_approvals.py#L112) 断言 `set(resp.json()) == {"reply"}`、
  [README.md:342](README.md#L342) 写着「原五个路由的请求/响应契约一个字都没改」、
  本文件「不要动的东西」的对外契约条目。（备选：`X-Session-Id` 请求/响应头，JSON 契约可真正一字不动，
  但可发现性与可测性差一截。）

  **坑：`OpsAssistant.chat()` / `reset()` / `self.messages`**
  （[assistant.py:419-427](src/agent/assistant.py#L419-L427)）是讲义留下的**实例级共享列表**；
  Agent 是**单例**，谁真去用它，所有 HTTP 请求就串味。多轮落地时删掉或明确标注 deprecated，
  **别留着当诱饵**。

  **先实测再写代码**（M3 靠这个避开了线程毒化，这次同理，用脚本化假模型 + 真 graph）：
  ① 每轮新 `thread_id` + 传入完整多轮 messages，与 checkpointer 是否相容（会不会重复累积）；
  ② 带历史的一轮触发中断后，`decide()` 取到的 `_last_ai_content` 是否仍是**本轮**最终回答
  （而非历史里某条旧消息）；③ 历史为空 / 仅一条 / 超窗口三种边界。

  **三个未决点已于 2026-09-11 用户拍板**：路线 **B**；`/chat` **body 加可选 `session_id`**
  （不用 header）、响应**恒回带**；不传 `session_id` 时**新建会话**。
  另定：**落盘范围四份全做**（告警记录 / 处置结果 / 审计日志落文件 / AI 建议），
  保留策略 **TTL + 容量上限**（惰性清理 + 节流），**交付分两批**（见本条目开头）。
- ~~**`approval_decisions` 表是空的**~~ 与 ~~**淘汰时标 abandoned 偏晚**~~——
  **2026-09-13 均已实现**（见「已实现能力」的「审批决策落盘 + 结果未知状态」）。

- **熔断机制（调用次数 / 成本上限）**：2026-09-10 用户提出。现状：Agent **实际上没有成本熔断**——
  LangGraph 确实有 `recursion_limit`，但实测默认值是 **10000**
  （`langgraph._internal._config.DEFAULT_RECURSION_LIMIT`，本机 langgraph 实测），
  远超任何正常回路，起不到成本控制作用；模型在一个 `invoke` 里可反复调工具直到自己停下。
  **写命令路径有审批兜住，只读工具没有**：抽风的模型能把 Prometheus 打到超时、把 token 烧穿。
  规格（待细化）：
  - 用 langchain 1.2.12 现成的 `ToolCallLimitMiddleware` / `ModelCallLimitMiddleware`，挂在
    [src/agent/assistant.py](src/agent/assistant.py) 那条 `create_agent(middleware=[...])` 上——
    **与写命令审批零耦合**：只读 Agent 也能用，且不需要 checkpointer；
  - 上限值**配置化**（`config/settings.yaml` 的 `agent` 段，如 `max_tool_calls` / `max_model_calls`），
    不硬编码；缺省 / `0` = 关闭（保持现行为）；
  - **触发后的行为要定**：给模型一段可读文本让它收尾，还是直接终止本轮？**关键是不得把异常抛到
    `ChatService`**——会被它的 catch-all 吞掉、整条聊天静默回落关键词，与「中断不是异常」那条坑同源；
  - 顺带核对审批侧既有的容量熔断（`ApprovalStore` 的 `max_pending=32` + 900s TTL）是否够用。
  **动手前先实测**：多个中间件在 `create_agent(middleware=[a, b])` 里的**执行顺序语义**
  （历史/记忆钩子与 HITL 的先后），以及超限时的**返回形状**（中断？异常？还是正常返回一段文本）——
  **别凭记忆断言**。

### 搁置项（预留未实现）

- `notifiers`（钉钉/企微）、`docker/`、插件目录自动扫描发现、`logging.conf`、
  **告警/处置/AI 建议的归档写入**（三张表已随 M4 第一批建好，但**无写入方**，
  待 M4 第二批在 `OpsAgent.process_alert` 末尾接线）、**审计日志落文件**（RotatingFileHandler）。
- ~~[src/storage/db.py](src/storage/db.py) 的 SQLite 持久化（仅 TODO 占位）~~
  —— **2026-09-11 已实现**，见「已实现能力」。
- `disk_clean` 不再单独立动作：M1 后直接加进 `config/actions.yaml` 即可。

### 明确不做（除非需求变化）

- **P2-1 目录级配置加载去重**：`actions/catalog.py` 与 `diagnostics/commands.py` 的外壳同构，
  但 `DiagnosticsConfig` 多出 `provider/target/output_limit`，只能抽「外壳」。
  2026-09-10 复审判定为**收益中等、回归风险不小，不真抽**。若将来再有人提，先看这一条。
- 推翻既定决策、动对外 HTTP 契约、动 `config/*.yaml` 现有格式、放松安全不变量（见下）。

---

## 不要动的东西（复审判定：现状是对的）

- **`plugins/common/` 共享内核**（endpoint / ssh / audit / cmddef）：重构做得最好的部分，读写两条边界
  抽得干净，`cmddef` 的「模板占位集合 == params 声明集合」这类 fail-loud 校验是刻意的，
  别为了「灵活」放松它。
- **`container.py` 的惰性装配 + 缓存**：设计正确，已有测试（`tests/test_container.py`）。
- **`capabilities.py` 作为能力单一实现的定位**：见既定决策 #6。
- **`ChatService.Rule` 那个小规则框架**：有点自造感，但有测试锁着且确实换来了可读性，不值得改。
- **对外 HTTP 契约**（`/health` `/chat` `/alert` `/alerts/*` 的请求响应）：一个字都别改
  （`tests/test_container.py::test_create_app_smoke` 锁了路由存在性）。
  **新增路由不属于改动**：`/approvals`（M3）与 `/sessions`（M4 第一批）都是**追加**，
  原五个路由的请求/响应至今**一个字没动**。
  **已实施的例外 #1**（2026-09-11）：`/chat` **追加**了可选 `session_id`（请求与响应各一个），
  `/health` 追加了 `storage` 字段。两者都是**只增不改**——不传 `session_id` 时行为与改造前
  完全一致，`reply` 仍是 str。同 commit 已更新 `tests/test_api_approvals.py` 的形状断言
  （`{"reply"}` → `{"reply","session_id"}`）与 README 路由表，两处不再对不上。
  **鉴权不算契约改动**（2026-09-12）：`auth.enabled` 出厂 `false`，关闭时逐字与改造前一致；
  开启后是**多了一层准入要求**（无凭据 401），请求/响应**字段一个没动**。挂法与纪律见坑表。
  **例外 #2 —— 本项目第一次真正的破坏性变更**（2026-09-12，**经用户明确授权的契约变更**）：
  `POST /chat` 的**请求体**删掉 `user` 与 `session_name`、`session_id` 由可选改**必填**；
  同时**新增** `POST /sessions`（建会话的唯一入口）与 `PATCH /sessions/{id}`（改名）。
  理由有两条：① 身份改由**密钥**推出——自报的用户名无从核实，基于它的校验只是装饰；
  ② 「建会话」从 `/chat` 的副作用变成一个**显式动作**，调用方不必再猜「这次是新建还是续聊」。
  **响应体三键一字未改**（`session_name` 的语义从「客户端给的别名」变为「只读回带会话标签」）。
  旧字段不报错、**静默忽略**（pydantic 默认 `extra="ignore"`）：它们已无害，报 422 只会让
  既有调用方收到一个莫名其妙的错。README 有「破坏性变更」与「升级须知」两节，动这块前先读。
- **`config/*.yaml` 的现有格式与示例内容**：无需求不动配置。
- **安全不变量相关的一切**（`shlex.quote`、redact、审计、恒不 sudo、凭据不进模型）：只许加固，不许放松。
- **M3 的人工审批**（2026-09-10 新增，同样只许加固）：写工具与审批策略必须继续**成对产出**
  （`WriteToolBundle`）；不得引入 `edit` 决策；`config/agent_actions.yaml` 的 `approval` 段
  不得新增开关键；审批护栏只许加在 **Agent 路径**，绝不下沉进 `execute()`/`run()` 内部
  （否则 `RuleEngine` 的无头自动处置会被连带影响）。
  **已知残余风险**：未启用 `auth` 时，任何能访问该端口的人都能批准写命令。启用 `auth` 后
  **按归属隔离**（只看得到、也只批得动自己触发的，见「已实现能力」）。见 README「安全说明」，
  绑 `0.0.0.0` 前必须先打开 `auth`。

---

## ⚠️ 会话名定位修订（2026-09-12）：从「别名」降为「纯标签」

**这是对前一小节设计的修订，以此为准。**

用户提出：**重心搞错了**——应该「以 `session_id` 优先，name 填不填无所谓」，
并且名字只用于「让用户查找到对应对话以及识别」。

于是规则**塌缩成一条**：**有 `session_id` 就用它续聊，没有就新建一段对话。**

| 方面 | 旧（别名） | 新（纯标签） |
|---|---|---|
| `session_name` 的作用 | 能**直接选中**会话 | 只给新会话**打个标签**，供人识别 |
| 同名请求两次 | 接到**同一段**对话 | 得到**两段新对话** |
| 唯一性 | `UNIQUE(user, name)` | **不要求唯一**（v3 撤掉了那个索引） |
| id + 名字同时给 | **422**（歧义） | **名字被忽略**，200 |
| 存储 | `sessions.name` 列 | 不变（列已经够用，映射表没有存在理由） |

**顺带删掉的复杂度**：互斥检查、`find_by_name`、重名复用逻辑、唯一索引与它的迁移。
今天那一串关于名字的坑（互斥 422、重名复用、作用域）**大半随之消失**。

**关于「映射表」**：用户最初提过要用独立映射表。**在纯标签方案下它没有存在理由**——
那张表的全部价值是「把客户端的标识空间与存储键解耦」，而这个需求随「名字不再选中会话」
一起消失了。列比表简单：一次查询无 join，删会话时标签跟着消失，
结构上不可能出现「标签指向已不存在的会话」的孤儿。**若将来要「一名多会话」或「改名历史」，
那时再换成表。**

**必须澄清的一个误解**：用户当时认为「用随机 id 切换对话安全性更高」。
**实测这个前提不成立**——`GET /sessions` 无鉴权且直接列出所有会话的 `session_id`，
所以「id 不可猜」这层保护**本来就不存在**（README 早写明该端点会把对话原文吐出来）。
改方案的理由是**简单**（规则塌缩、删掉一堆校验），不是安全。
**真正的安全性只能来自鉴权**，这一点已在 README 里写得更醒目。
> 因此也**不要**因为「名字可能被猜到」而增加限制——那防不住任何东西，
> 能访问端口的人不需要猜名字。

### 后续修订（2026-09-12 当天，鉴权落地后）：名字只在建会话时给

上面那张表里「`session_name` 只在**新建**时生效、id + 名字同时给则名字被忽略」这条，
随着鉴权一起来了一次收尾——**`session_name` 已从 `/chat` 的请求体彻底移除**：

| 方面 | 上一版 | 现在 |
|---|---|---|
| 名字从哪给 | `/chat` 的 `session_name` 字段（仅新建时生效） | **`POST /sessions` 的 `name` 字段** |
| 改名 | 无（名字只有一次机会） | **`PATCH /sessions/{id}`**（空串 = 清除标签） |
| `/chat` 的 `session_id` | 可选（不传就新建） | **必填** |
| `/chat` 的响应 | `session_name` = 客户端给的别名 | `session_name` = **只读回带**该会话的标签（键名不变） |

那次「id + 名字同时给 → 名字被忽略」的规则**整条消失**了——两个字段不再共存于一个请求里，
也就不存在歧义。**这是「规则塌缩」的又一次收益**：不是加了校验去处理歧义，
而是让歧义在结构上无法表达。

---

## 会话名（2026-09-11，M4 之后；**定位已于 09-12 修订，见上一节**）

起因：用户按直觉发了 `{"user":"yqyZ","message":"cpu","session_id":"test_1"}`，得到 **404**。

**这不是用错，是 API 缺了一条路**：原先 `session_id` 的语义是「引用一个已存在的会话」，
而客户端**根本无法指定会话标识**——唯一拿到会话的方式是「不传 → 服务端给一个随机 id
→ 下次带回来」。所以 `test_1` 这种写法在任何情况下都不可能成功。

**方案（用户拍板）**：允许**自定义会话名**，但**不允许指定 session_id**。

- `session_id` 保持服务端生成的 32 位随机值（不可猜）；名字只是客户端可读的别名。
- `session_name`：**没有就按这个名字新建**（这是它区别于 `session_id` 的关键）。
- 唯一性 **限定在 user 内** `UNIQUE(user, name)`，用**部分索引**让空名不参与去重。
- 两个标识**同时给 → 422**：歧义比报错危险（实现会挑一个执行，调用方却以为另一个生效）。
- 名字格式在**入参校验阶段**处理（非法直接 422，不让它走到存储层变 500）：
  **先 NFKC 归一化 + 折叠空白，再按 Unicode 类别拒绝**（`Cc` 控制字符 / `Cf` 格式字符），
  长度 1~64。**不限制用哪些文字**。

  **两次修正的经过**（都在这一个点上，记下来免得重复走）：
  1. 初版只允许 ASCII（`^[A-Za-z0-9_.-]{1,64}$`）——用户问「为什么中文不行」，**过严**。
     汉字是明确安全的可见字符，禁它没有技术依据。
  2. 改成白名单放行「字母数字 `_ . -` 空格 + 汉字」——用户又问「为什么还要这么严，
     直接用字符串不行吗」，**还是过严**。
  3. 最终改为**按类别拒绝**：只挡 `Cc`/`Cf`，其余一律放行。

  **关键认识**：我最初用白名单的理由是「黑名单永远会漏」——这把**两件事混为一谈**了。
  枚举**具体的危险字符**确实会漏；但**按 Unicode 类别排除是有明确边界的、完备的**。
  真正需要挡的只有「看不见的字符」这一类（它们恰好是归一化唯一治不了的：
  NFKC 消不掉零宽空格与 BOM），而所有可见文字都不在其中。详见坑表对应条目。

**必须知道的一点**：名字是**能猜的**，所以它**不等于**「客户端指定 id」在安全上的等价物。
无鉴权下 id 不可猜曾是唯一的会话隔离手段；有了名字之后，猜到名字就能读到那段对话。
**这是有意的取舍**：换来了客户端可用性，代价是隔离强度下降。将来加鉴权时，
名字自动变成「(登录用户, 名字)」，这个代价就消失了——把作用域定在 `user` 上正是为此。

**顺带修掉一个既有的小问题**：`sessions.user` 一直是空的——因为改造前 `reply_turn` 根本
没接收 `user`。现在 `/chat` 会把 `user` 一路传到存储层。

**schema v1 → v2**：这是本项目第一次真实迁移。它**已经在真实文件上跑过了**——
用户库 `data/ops_agent.db` 在复现报错时被服务自动升到 v2，1 会话 / 2 消息 / 1 告警全保住。

---

## M4 第二批（2026-09-11）多轮接线 + 状态日志

把「对话历史」接到聊天链路上，并把四份重启即丢的数据落盘。测试 269 → 342。
**本小节之后 M4 整体完成**（只剩熔断机制仍是路线图条目）。

**先跑了三条 spike 实测**（脚本化假模型 + 真 graph，全离线），结论：

1. **每轮新 `thread_id` + 传入完整历史，不会跨轮累积**——返回 = 输入 + 本轮增量，
   两个新线程各自独立。路线 B 成立。
2. **带历史的一轮触发中断后，`decide()` 拿到的是本轮的最终回答**，不是历史里的旧回答。
   （这条正是前面那个 `_last_ai_content` bug 的验证——不修它这里就会串味。）
3. **历史边界**（空 / 仅一条 user / 孤儿 assistant / 30 条）全部正常，无异常。

**实施要点**：

- `OpsAssistant.reply_turn(message, *, history=())` 返回 `TurnResult`；`reply()` 转调它
  （所以**只有一条 invoke 代码路径**，既有单轮调用点与假对象全不动）。
  讲义遗留的 `chat()`/`reset()`/`self.messages` **已删**（单例共享列表，全仓无调用方）。
- `ChatService.reply_turn(message, *, session_id=None)`：`isinstance(assistant, HistoryCapable)`
  分派（不支持的旧对象退回单轮）。**落库在 Agent 的 catch-all try 之外**，
  且**关键词兜底的回答同样落库**——否则降级部署里会话表永远是空的。
- `SessionStore.update_pending` **按 approval_id 定位，不需要 session_id**。
  这比原计划「把 session_id 一路穿到审批记录再带回来」少了一整条管线：
  approval_id 由 uuid4 生成、全局唯一，单独就能定位到行。
- 新增 `ChatReply`（`reply` + `session_id` + `approval_id`）与 `ApprovalCancellable` Protocol
  （`cancel_pending`，供「删会话→作废其未决审批」用）。**没有**往 `ApprovalCapable` 里加方法
  ——那会让既有实现静默不再 isinstance 通过。
- 开库时 `reconcile_pending(set())` 把孤儿审批轮标 `abandoned`：新进程里任何 pending 行
  必然是孤儿（审批与 checkpoint 都在进程内存）。已实机验证（见下）。
- `/health` 追加 `storage: "ok"|"degraded"`——**为让写失败可观测**，不把降级藏进日志。

**状态日志部分**：

- `interfaces/history.py`（三个中性 dataclass + `HistoryStore` ABC）→ `storage/history.py`。
  告警表用 `UNIQUE(alert_key, starts_at, status)` + `INSERT OR IGNORE` 做**重投幂等**，
  同时保留 firing→resolved 的状态迁移（若用 `UNIQUE(alert_key)` + REPLACE，
  「曾经 firing 过」的痕迹就没了）。
- **写入前强制截断** `detail`（500 字符）/ `advice`（4000）。`detail` 里含执行器拼进去的
  **远端命令 stdout**，此前只进 console 所以无所谓；一落盘就变成磁盘明文，
  违反 `common/audit.py` 那条「整段命令输出不落日志」的既有不变量。
- 归档**不做内存降级**（与会话相反）：归档的价值全在「跨重启可查」，
  给一个重启即丢的归档表只会让人误以为记录还在。
- `src/api/alert.py` 追加 `GET /alerts/history`（**没有**另建 `api/alerts.py`——
  该文件本就承载 `/alerts/*` 三条路由，新增端点放这里更自然）。
- 审计落文件：`RotatingFileHandler` + **幂等挂载** + 建父目录 + `delay=True`，
  见坑表。`logging.audit=false` 的既有语义（提到 WARNING、不是关闭）保持不变。

**实机验证**（真 uvicorn、真 SQLite、**不触发真实模型**）：预置一条孤儿 pending 行后启动，
日志打出「已将 1 条残留的待审批对话标记为作废」，`/health` 返回 `{"status":"ok","storage":"ok"}`，
库里两行状态均为 `abandoned`。

---

## M4 前置修复（2026-09-11）三个既有 bug

在开第二批（多轮接线）之前先修掉的三个**已存在**的问题。第一个是多轮的**阻塞项**
（不修则带历史的一轮必现故障），后两个不修也能跑，但分别导致「同一会话两个 store 实例」
与「checkpoint 线程泄漏」。测试 264 → 269。

1. **`_last_ai_content` 扫全量消息 → 捞历史**（[assistant.py](src/agent/assistant.py)）：
   先写 spike 脚本实测确认了前提（`invoke` 返回的 `messages` **含输入历史**），再改。
   修法**不是**原计划的「传 skip 下标」——`decide()` 的续跑路径拿不到那个下标；
   改为**止步于最后一条 human 消息**，在多轮 / 续跑 / 中断重入三种情形下都恰好是本轮产出。
2. **`ApprovalStore.add()` 返回值缺 `thread_id`**：docstring 说返回值供调用方
   `delete_thread`，实际只回 `{"approval_id": …}`，调用方无从删线程 → 泄漏照旧。
   这是**根因在存储层**、而最初的修复只动调用方（`_register_pending`）时被回归测试当场抓出来的。
3. **`_ensure_assistant` 无锁竞态**（[core/chat.py](src/core/chat.py)）：加锁 + 双检。

---

## M4 第一批（2026-09-11）持久化底座 + 会话表

「对话历史维护 + 状态日志持久化」五阶段里的**前两阶段**。设计见「路线图」，
**不碰聊天链路**，可独立跑通与验证。测试 189 → 264。

1. **配置**：`config/settings.yaml` 新增 `storage` 段；`config_loader.py` 加 `StorageSettings`。
   **`StorageSettings.enabled` 的 dataclass 默认值是 `False`**——测试里 `Settings()` 被直接
   构造多次，默认打开会让用例写真实 db 文件、跨用例串数据、`git status` 变脏。出厂开关只在 yaml 里。
2. **底座**：`src/storage/db.py` 从空 TODO 变成真实现。要点：每操作新开连接、`closing` 关连接、
   `isolation_level=None` + 显式 `BEGIN IMMEDIATE`、WAL 失败回落、版本双向校验、失败返回 `None`。
3. **会话存储**：`interfaces/session.py`（中性类型 + `SessionStore` + `HistoryCapable`）→
   `storage/session.py`（SQLite + 内存两套实现，跑同一组契约断言）。TTL/容量清理**惰性 + 节流**，
   `clock` 可注入。
4. **端点**：`api/sessions.py` 三个端点；`container.sessions()` 三级降级（配置关 → None；
   SQLite 打不开 → 内存实现；正常 → SQLite）；`main.py` 挂 router。
5. **`.gitignore`**：补 `data/`、`*.db`、`*.db-wal`、`*.db-shm`——`*.db` **匹配不到 WAL 旁文件**，
   而它们同样含会话原文。

**中途修正的两处设计**（都已落到代码与注释里）：

- `sessions.pending_approvals` 原计划**存成列**，改为**读时统计**：存列就得在
  `append_turn` / `update_pending` / `reconcile_pending` 三处同步维护，批量作废时极易漂移。
- 审批中断原本只把 assistant 行标 `pending_approval`，测试直接暴露了后果
  （user 行仍是 final → 下轮窗口出现无回答的提问），改为**整轮同进同出**。

---

## 本轮（2026-09-10）结构收尾做了什么

一次「仓库卫生 + 注释文档 + 结构收尾」，**零功能变更**，共 12 个 commit（仓库从无到有建了 git）：

1. **仓库卫生**：`git init` + 基线快照（70 文件）；`.gitignore` 补 `.claude/settings.local.json`
   与 `参考/`（尚硅谷讲义，56 MB / 152 文件，**仅本地参考、不入库**）；清掉 15 个 `__pycache__`
   与 `.pytest_cache`（含两个源文件早已删除的幽灵 `.pyc`）。
2. **注释 / 文档**：修正 7 处「文档与代码互相误导」（`obs-agent.txt` 死链、registry 写成四类插件、
   「plugins/actions 基类」、`ActionRule.action` 的过期描述等）。
3. **死代码**：删 registry 的 `_actions` 全族（M1b 后无调用方）、`security.require_params`
   （其 docstring 仍宣称是动作路径的一环，且有测试为它背书）、`interpolate_mapping`、
   `get_app_logger`。保留 `OpsAgentError` 作为结构位并标注「预留」。
4. **P0-1 分层违规**：`ALERT_ANALYSIS_SYSTEM` 从 `agent/prompts.py` 下沉到
   [src/core/prompts.py](src/core/prompts.py)（谁用谁拥有），`core/analysis.py` 改 import。
5. **P0-3 模型入口收敛**：新建 `src/utils/model_loader.py`，合并 `assistant.get_model()` 与
   `deepseek_client._get_model()` 两份逐行等价副本 + 逐字符相同的 `_content_text()`。
   **行为变更**：全进程只剩一份模型缓存（原先两处各一份）。
6. **P1-1**：`capabilities` 去掉 `getattr` 字符串派发，改传 `MonitorSource` 的方法本身。
7. **P1-2**：新建 `interfaces/assistant.py`（`Assistant` Protocol）；`core/chat.py`、`core/agent.py`、
   `container.py` 的 `Any` 换成真实契约；补齐 `interfaces/__init__.py` 漏掉的 diagnostic 家族导出。
8. **P2-3 降级路径测试**：**测试 60 → 110**（+50）。其中 `container.ops_agent()` 与
   `main.create_app()` 此前**从未被任何测试调用**过，整条装配链路零断言。
9. **P2-2 便宜第一步**：`RuleEngine.print_results` 移入 `OpsAgent`，引擎变回纯决策、输出只剩一个口子。
   **Notifier 抽象按用户决定不做**（见路线图）。
10. **P2-1 按用户决定跳过**（见「明确不做」）。

**两处遗留**，下次接手时留意：

- 第 4 步（core 反向依赖修复）的代码改动**混在第 2 步的 `docs:` commit 里**（`3dc759c`），
  提交粒度不纯。功能无影响，仅回滚粒度上不够干净。
- 本轮**全程没有 mypy / ruff**，类型层面的回归只靠人工比对——第 7 步就因此漏删了一处
  `typing.Any` 导入（已在 `78aed53` 修复，并全仓扫过注解求值确认无同类问题）。
  **建议后续引入静态检查**，这类问题能被完全自动拦住。

---

## 验证

```bash
# 全量离线单测（仓库根，conda langchain1.2）——不碰真实 DeepSeek/Prometheus/SSH
python -m pytest tests -q            # 目前 535 passed（2026-09-13 鉴权 + 审批 + 决策人落库后）
                                     #   鉴权与归属新增：api_auth、utils_auth、session_store
                                     #     的归属契约用例、api_sessions 的建/改/越权用例
                                     #   M4 新增：sqlite_db 15、session_store 43（两种实现
                                     #     跑同一组契约断言）、api_sessions 18、chat_history 14、
                                     #     history_capable 9（真 graph 跑多轮×审批跨模块接线）
                                     #   M3 相关：agent_actions_catalog 16、agent_action_source 12、
                                     #     approval_store 10、agent_tools_approval 10、
                                     #     api_approvals 9、hitl_graph 15、container 16 → 27
                                     #   既有：diagnostics 22、prometheus_monitor 18、
                                     #     chat 17（含 M2 同源回归「工具输出==兜底输出」）、
                                     #     action_catalog 12、analysis 12、config_loader 5、
                                     #     rule_engine 5、security 2、no-langchain 1

# 无 langchain 降级回归（单独跑）
python -m pytest tests/test_no_langchain_import.py -q

# 分层方向检查（应无输出）
git grep -n "src\.agent" src/core

# 模型入口收敛检查（应只剩 model_loader 一处）
grep -rn "from init import model" src
```

实机链路（需要 `.env` 有 `DEEPSEEK_API_KEY` + `SSH_PASSWORD_192_168_20_128`、装好 paramiko、
目标机可达，**由用户自行准备与执行，不要代跑**）：

```bash
uvicorn src.main:app --reload --host 127.0.0.1 --port 8000
# → POST /chat {"user":"ops","message":"帮我看看系统忙不忙"}
# → 改 config/diagnostics.yaml 后重启，观察工具集变化
```

## 提交纪律

- **一任务一 commit**，做完一个立刻跑一次全量测试，别攒到最后。
- 仓库已 `git init`（2026-09-10），作者身份为**本仓库局部**配置 `yqyZ <yqyZ@localhost>`（占位，
  未写全局配置）。
- 每次都先确认 `git status --short` 干净，且 `git ls-files` 里**不得出现** `.env` / `参考/` /
  `__pycache__` / `.claude/settings.local.json`。
