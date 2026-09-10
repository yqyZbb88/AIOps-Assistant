# ops-agent 项目交接 · 后续开发 / 维护提示词（新会话从这里开始）

> 用途：把下方「★ 从这里开始粘贴」整段贴到新对话开头，模型即获得项目全部上下文——
> 现状、硬性约束、已踩过的坑、既定决策、路线图与验证方式，不必再从零考古。
>
> 背景：原「按 obs-agent.txt 重构」任务**已完成**（`ops_bot/` 已删除、`src/` 分层架构就位），
> 后又落地「AI 只读诊断」能力。2026-09-04 再落地 **M1 统一命令内核**：只读诊断与写命令共用
> `src/plugins/common`（endpoint/ssh/audit/cmddef）内核，动作收敛成 `config/actions.yaml`
> 写命令目录 + 通用执行器（restart_service 等旧执行类已删）。2026-09-07 落地 **M2 能力单一实现**：
> CPU/内存/磁盘/告警的「查询+格式化」收编进 `src/core/capabilities.py` 纯函数（无 langchain），
> Agent 工具（@tool）与关键词兜底转发同一批函数 → 两条链路产出同款规范文案（不再双写、措辞统一）。
> 本文件是重构完成之后的 **开发守则 + 坑清单 + 待办**版本。
>
> 配套文档分工：
> - [README.md](./README.md)：结构 / 分层职责 / 配置指南 / 启动 / 测试 / 扩展指南（改文档先看它）；
> - **本文件**：硬性约束 / 安全不变量 / 已踩坑与后续怎么写 / 既定决策 / 路线图待办。

---

## ★ 从这里开始粘贴

你在仓库根 `c:\Users\yqyZ\Desktop\Assitant`（Windows，conda 环境 `langchain1.2`，
Python = `D:\MiniConda\envs\langchain1.2\python.exe`；shell 为 Git-Bash）。这是一个**已可运行**的
「智能运维 Agent」：FastAPI 服务 + LangChain 聊天 Agent + 告警写命令目录自动处置 + AI 只读主机诊断。
动手前先读完本文件「项目现状 / 模块地图 / 硬性约束 / 坑清单」与 README；拿不准时进入计划模式
先征询方案，**既定决策（见「既定决策记录」）不要推翻**。全程**离线验证**：不发起任何真实
DeepSeek API / Prometheus / SSH 调用（Agent 构建可以，不 invoke 不联网）。

### 硬性约束（违反即改错）

1. 根目录 **`init.py` 只读**：它是用户的模板，也是**唯一**的模型入口（`init_chat_model(...)`）。
   换模型/provider/参数只改它；新代码一律复用它的模型，不自己 new 客户端。
2. **不修改 `.env`；不回显 / 不新增 / 不探明任何明文密钥**。真实 SSH 密码只以
   `${SSH_PASSWORD_192_168_20_128}` 这类**占位符**出现在 `config/*.yaml`，真实值由用户自己填进 `.env`；
   代码/文档/注释/README 一律不落明文。用户要求列出 `.env` 的键或值时——**不做**，说明理由即可。
3. **分层依赖方向**：`api/ → core/ → interfaces/`；core 只依赖 `interfaces/` 抽象，
   **不得 import 插件实现**。具体实现放 `plugins/`：monitors / llms / diagnostics 是
   import 即注册进 `src/registry.py` 的插件类；**actions（写命令）例外**——M1b 起收敛成
   `config/actions.yaml` 目录 + 通用执行器，由 `container.executors()` 按目录构建，不再注册固定类。
   共享内核在 `plugins/common/`（endpoint/ssh/audit/cmddef，顶层无重量依赖）。`container.py`
   按 settings 惰性装配并缓存，重量依赖（langchain / paramiko / dotenv）只在用到时 import。
4. **Agent 两条工具通道，边界不同，别混**：
   - 监控工具（读指标/告警）与只读诊断工具 = **可自动调用**（只读、白名单命令、服务端参数校验、
     凭据不进模型），见 [src/agent/tools.py](src/agent/tools.py) 的 `build_tools` / `build_diag_tools`；
   - **写动作永不直接暴露给模型自由选**：M1b 起动作收敛成 `config/actions.yaml` 写命令目录
     （管理员声明模板/参数/sudo），`rules.yaml` 只引用目录 key；命令构建/权限/失败语义留在
     各自的接口与边界。`agent/tools.py` 底部留了「动作执行工具位」注释——若要启用需配合
     `interrupt_before` 人工审批，未批准不实现（路线图 M3）。
5. **安全不变量**（新增任何 Agent 能力先逐条对号）：
   - 模型上下文 / 工具参数里**永无** host / 密码 / 私钥 / 自由 shell 命令；
   - 命令串 = 配置里声明的模板 + 逐参校验并 `shlex.quote` 的值；模型自由文本永不拼进 shell；
   - **只读诊断恒不 sudo**；动作（写命令）的 sudo 只按 `config/actions.yaml` 每条命令的管理员声明，
     规则不能越权把非 sudo 项提权；凭据只在配置 target/规则（密码 `.env` 占位插值），构造后留在实例内。
6. **每个真实 SSH 尝试写审计**：`ops_agent.audit` logger，结构 `{action, command, host,
   params(redact_sensitive), started_at, finished_at, result/error}`，**绝不含密码**；整段输出不落日志。
7. 工具函数**返回 str**，绝不把异常抛给 Agent；监控能力的「取数+格式化+捕获 MonitorError 转可读文本」
   收敛在 `src/core/capabilities.py`（无 langchain 纯函数），Agent 工具与关键词兜底都转发它——
   **同一能力只写一处**；远程**非零退出码不算工具失败**（grep 无匹配、systemctl 查 failed 服务常返回非 0）
   ——用 `ok=False` 连输出回传，由 Agent 自行判断。
8. 改 `config/diagnostics.yaml` / `config/actions.yaml`（写命令目录）/ 注册新插件 / 新增工具 → **需重启进程**
   才生效（Agent 工具集在首次聊天构建时定稿；executors 在容器首次装配时按目录构建并缓存）；
   唯一例外是 `RuleEngine.reload_rules()`（热更新，已支持）。
9. 测试纪律：`python -m pytest tests -q`（仓库根，conda `langchain1.2`），**全部离线**，
   用 [tests/conftest.py](tests/conftest.py) 的 `FakeMonitor` / `FakeLLM` / `FakeDiagnostics` 注入；
   新能力要带「屏蔽 langchain 后仍可 import + 聊天回落关键词」的回归（照
   [tests/test_no_langchain_import.py](tests/test_no_langchain_import.py) 的子进程屏蔽法写）。

---

### 项目现状（已实现能力）

- **AI 聊天**：DeepSeek Agent 自主调用监控工具（CPU/内存/磁盘/活动告警）答问题；无 LLM/key 时
  回落到 `core/chat.py` 关键词兜底（CPU/内存/磁盘/告警/状态/帮助），服务照常。
  工具与兜底转发同一批 `core/capabilities.py` 纯函数（M2），产出同款规范文案。
- **AI 只读主机诊断**：监控解释不了的怪现象，Agent 可调 `config/diagnostics.yaml` 白名单只读命令
  （`memory`/`disk`/`top_processes`/`service_status`/`service_logs`）经 SSH 到目标主机排查；
  命令模板 + 参数校验全配置化，工具按白名单动态生成（模型只选命令名 + 语义参数）。
- **Prometheus 指标查询**：即时值 + 按实例近 1h 趋势。
- **告警自动处置（M1 收敛成写命令目录）**：Alertmanager webhook 接收（`POST /alert`）+ 最近记录 + active 查询；
  `config/actions.yaml` 写命令目录（模板+参数+sudo）→ 通用执行器按目录 key 执行（带校验 + 审计）；
  只读诊断与写命令共用 `plugins/common`（endpoint/ssh/audit/cmddef）内核，两条入口/边界分离；
  规则表外的告警 → LLM 根因假设与建议（`[AI 分析]`），分析失败仅记日志不阻塞。

### 模块地图（照此导航 / 改）

```
src/
├── main.py                  create_app()：装配 container → include api routers
├── api/{chat,alert,health}.py   薄 HTTP 层（依赖注入取 OpsAgent，无业务逻辑）
├── core/                    ★ 业务核心，只依赖 interfaces 抽象
│   ├── agent.py             OpsAgent 主控制器（chat / process_alert / recent / active_alerts）
│   ├── chat.py              ChatService：Agent-first → 关键词兜底 → DEFAULT_REPLY（动态规则转发 capabilities）
│   ├── capabilities.py      ★ 监控能力单一实现：CPU/内存/磁盘/告警 查询+格式化纯函数
│   │                          （无 langchain；捕获 MonitorError 返回可读文本；工具与兜底共用）
│   ├── rule_engine.py       规则加载 + 匹配决策 + reload_rules() 热更新
│   ├── analysis.py          告警 AI 分析（上下文组装 + LLM 编排）
│   └── exceptions.py        业务异常基类
├── interfaces/              抽象契约（零第三方依赖；core 只 import 这里）
│   ├── monitor.py           MonitorSource + MonitorError（cpu/memory/disk/alerts/trend）
│   ├── action.py            ActionExecutor + ActionError
│   ├── llm_client.py        LLMClient + LLMError
│   └── diagnostic.py        DiagnosticSource + DiagnosticError + render_diag
├── plugins/
│   ├── common/                ★ 共享内核（读写两边界共用；顶层无 langchain/paramiko）
│   │   ├── endpoint.py        SshEndpoint（连接抽象，认证二选一 key→password）
│   │   ├── ssh.py             exec_command(endpoint, command, *, sudo) → (exit/out/err)
│   │   ├── audit.py           record_execution 统一审计（收敛动作/诊断两处 JSON 拼装）
│   │   └── cmddef.py          命令目录机制：模板+参数+校验+build_command（纯，可离线单测）
│   ├── actions/               WRITE 边界：写命令目录（config/actions.yaml）+ 通用执行器
│   │   ├── catalog.py         actions.yaml 解析（写命令白名单；允许 sudo 声明）
│   │   └── executor.py        CatalogCommandExecutor：按目录 key 执行（非零=失败→ActionError）
│   ├── monitors/prometheus_monitor.py
│   ├── llm_clients/deepseek_client.py
│   └── diagnostics/           READ 边界：只读诊断（Agent 工具用；sudo 恒 False；非零=数据）
│       ├── commands.py        diagnostics.yaml 解析（config 级；命令级机制委托 common/cmddef）
│       └── ssh_diagnostics.py SshDiagnostics(DiagnosticSource)：SSH 执行白名单只读命令
├── agent/                    ★ LangChain Agent 编排（惰性，只在首次聊天构建）
│   ├── prompts.py           系统提示词 / 告警分析 system / AGENT_NAME（纯常量）
│   ├── tools.py             build_tools(monitor) + build_diag_tools(diag)（监控 @tool 薄接线转发 core/capabilities）
│   └── assistant.py         OpsAssistant：惰性复用 init.py 模型，构建 Agent
├── registry.py              注册表：monitors / llms / diagnostics 按类注册；
│                            actions 不再注册固定类（M1b 改走 actions.yaml 写命令目录，见 container.executors）
├── container.py             DI：settings → 插件实例 → OpsAgent（惰性 + 缓存）；executors 按 actions.yaml 构建
├── storage/{models,memory}.py   数据模型（pydantic）+ 最近告警内存缓冲
│   └── db.py                SQLite 持久化（仅 TODO 预留，未实现）
└── utils/
    ├── config_loader.py     yaml 解析 + 全文件 ${ENV}/${ENV:-d} 插值 + Settings 冻结 dataclass
    ├── logger.py            日志级别 + 审计 logger（ops_agent.audit）
    └── security.py          redact_sensitive（password/token/secret/api_key → ***）
config/{settings,rules,actions,diagnostics}.yaml    配置（密码一律 ${VAR} 占位）
tests/                       离线单测（conftest 提供 Fake* 全套）
```

关键装配链路（container.py）：`settings → monitor(llm/executors←actions.yaml 目录) → ChatService(assistant_factory)
→ 首次聊天 OpsAssistant(monitor, diag) → tools = build_tools + (有 diag 才) build_diag_tools`。

---

### 已踩过的坑 → 后续怎么写（改代码前先读，重点）

| 问题（发生过） | 规矩（后续怎么写） |
|---|---|
| `from langchain.tools import StructuredTool` 报 ImportError（langchain 1.2.12 **不** re-export） | 一律 `from langchain_core.tools import StructuredTool`；`@tool` 装饰器在 `langchain.tools`。 |
| config yaml **注释里写了 `${...}` 字面量** → 被 `config_loader.interpolate()` 当环境变量替换，冒出误导性「环境变量未设置」日志 | interpolate() 对**整个文件文本**（含注释）做 `${VAR}` 替换 → 任何 config/*.yaml 的正文与注释**都不要出现字面 `${`**；要举例请改措辞或写「以 `SSH_PASSWORD…` 占位」。 |
| 工具/命令集的构建时机 | 诊断工具集在 **Agent 首次聊天构建时定稿**：加命令 / 加插件 / 调白名单 → **重启进程**，不是热更新；改动后文档要写明「需重启生效」。 |
| 命令 key 同时是 Agent 工具名 | 须匹配 `^[a-z][a-z0-9_]*$`（小写开头，仅 a-z0-9_），别用中划线/大写/点。 |
| 设过 `output_limit` 下限 200 → 测试样例全挂 | 校验边界按**语义**定（min=1 即可），别拍脑袋设大值；保证 SAMPLE/测试数据不被你自己的下限卡死。 |
| 模板占位与参数声明对不齐 | `config/diagnostics.yaml` 里命令模板的 `{name}` 集合**必须等于** `params` 声明的名字集合：模板含未声明占位 / 声明了未使用 → **配置加载失败并关闭该能力**（静默放行打字错误）。str 参数必须声明 `pattern`（`re.fullmatch` 全匹配）；int 用 min/max 收敛。 |
| pydantic v1/v2 对字段约束（ge/re/pattern）表现不一 | 动态工具 args schema 用 `pydantic.create_model` 建，但字段**不加**取值约束；所有校验（clamp / 全匹配 / 拒绝多余参数）收敛到服务端 `commands.validated_params`，模型给的越界值被规约而不是弹 schema 错。 |
| 插件多了一类只记得加 `__init__.py` 注册 | 新增插件类别是**四处配套**：interfaces 加契约 → plugins 写实现 + 注册 → registry.py 加类别与 `register_*/names()/has_*` → container.py 惰性装配 + `try import` 可选依赖 + 缺失降级（返回 None / 空，服务照常）。M1b 例外：**actions 写命令不再注册固定类**，改为 config/actions.yaml 目录 + catalog/executor，由 container.executors() 构建（registry 保留但生产不再用于 actions）。 |
| 忘了「无 langchain 也得出厂」 | 顶层 import 别碰 langchain/paramiko/dotenv；加能力后在屏蔽子进程里验证 container/core/plugins 仍可 import、聊天回落关键词。 |
| 工具把「查询失败」当异常抛 | 监控/诊断工具内部捕获 `MonitorError`/`DiagnosticError` 返回**可读 str**，绝不抛到 Agent；连接失败、参数被拒都只是普通返回。 |
| 同一监控能力写两遍（@tool 与兜底 handler 各查各的、措辞漂移） | M2 起能力**唯一实现** = `core/capabilities.py` 的 `xxx_text(monitor)` 纯函数（无 langchain，供兜底 import）；加能力只在此写「取数+格式化+失败文案」，再到 `agent/tools.py` build_tools 与 `core/chat.py` 规则各**转发一行**。Agent 工具输出 ≠ 兜底输出 = 改错位置了（`test_chat.py` 有同源回归锁死）。 |
| 给模型自由参数/自由命令 | **永不**。模型只选命令名 + 语义参数；连接凭据只在 config target，构造后留实例内；str 参数全匹配后再 `shlex.quote`，int 十进制；命令模板静态部分 = 管理员可信文本。 |
| 真实密码入日志/文档 | 审计 params 一律 `redact_sensitive`；新写例子/注释/文档只允许 `${VAR}` 占位，真实值只存在用户 `.env`。 |
| （已修 2026-09-04）根 `requirements.txt` 曾被写成 **UTF-16**，`pip install -r` 直接挂 | 依赖/文档文件一律 **UTF-8**；requirements 分「核心 / 可选①聊天+AI 分析 / 可选②SSH+只读诊断 / 可选③测试」，与 README「安装与依赖」保持一致，别把文件存成 UTF-16。 |

### 既定决策记录（勿推翻）

1. **就地分层重构已完成**：不再扁平化/不再迁回 ops_bot；`obs-agent.txt` 仅作参考保留。
2. **单 Agent 够用**：不引入多 Agent 编排。异常处置走「单 Agent + 规则库 + 分层的**人工升级/审批**」；
   若将来要跑固定的故障应急剧本（runbook），优先用**线性 LangGraph 工作流**而不是并行多 Agent。
   多 Agent 只在出现「上下文膨胀 / 需并行分头排查 / 记忆相互独立」三个信号之一时才考虑。
3. **Agent 不动系统**：写动作一律走 `config/actions.yaml` 写命令目录（管理员声明）+ `rules.yaml` 引用 key；
   Agent 层只读诊断 + 读监控。（Agent 动作工具位已留注释，启用必须 `interrupt_before` 人工审批。）
4. **模型入口收敛**：一切聊天/AI 分析都复用根 `init.py` 的模型，不各自 new。
5. **诊断单目标**：`config/diagnostics.yaml` 目前只支持一台 `target`（见路线图，多主机方案已定）。
6. **Agent 能力单一实现（M2）**：CPU/内存/磁盘/告警监控的「查询+格式化」唯一出处 = `src/core/capabilities.py`
   纯函数（顶层无 langchain，兜底可 import）；`agent/tools.py` 的 @tool 与 `core/chat.py` 关键词动态规则
   都只转发同一批函数，产出同款规范文案（成功 `CPU 使用率：45.0%` / 失败 `查询 CPU 使用率失败：…` /
   无数据 `…暂无数据…`）。「状态/帮助/DEFAULT_REPLY」保持 fallback-only，不进 capabilities。

### 路线图：待办与已定的扩展方向（按需推进，勿一次全做）

- **[已定方案·多主机诊断]（README 已注释指向此处）**：目标 `target` → `targets` 命名映射，命令白名单
  保持共享一份。具体规格：
  - `DiagnosticSource` 加 `hosts() -> tuple[str,...]`（默认 `()`=单机，行为不变）；`run(command, *,
    host: str | None = None, params=None)`——host 走**独立通道**，不进模板/不进 shell；
  - 工具层 `build_diag_tools`：当 `len(hosts()) > 1` 时给每个命令工具**前置**一个必填 `host` 参数，
    取值 = `Literal[各 host label]`（`Literal.__getitem__(tuple(labels))` 动态构造），描述列可用主机；
    单台仍不生成该字段，保持现网工具签名不变；
  - 配置示例：`targets: { web-01: {host:…, password: "${SSH_PASSWORD_…}"}, db-01: {…} }`，密码按台各占位；
  - **host label 尽量与监控侧 instance 标识对齐**（monitor 输出里模型已见过 `instance=IP:9100`），
    否则模型无法把「刚才告警那台」映射到主机枚举；
  - 每台各拉一条连接、失败互不影响；审计多记 host label。旧单 `target` 格式解析为单元素表兼容。
- **Agent 动作工具**（写系统，M3 设计，默认关）：M1 的写命令目录（config/actions.yaml）已就位，
  Agent 只能从该目录选「命令 key + 语义参数 + host label」，凭据不经模型；审批用 create_agent
  interrupt（或课程 08 中间件审批示例）+ checkpointer 断点续跑；审批只加在 **Agent 调用路径**，
  绝不进 execute() 内部（规则无头自动处置不受影响）；写工具表与 build_tools 读工具表分开构建。
- **结构化输出**（讲义 7.3）：`agent/prompts.py` 已留 ToolStrategy + AlertAnalysis schema 注释与启用示例，默认关。
- **日志落盘**：目前日志走 console / settings 控级别；未加 RotatingFileHandler。
- **git init**：仓库尚未纳入版本控制（README 有 .gitignore 建议）。
- 搁置项（预留未实现）：notifiers（钉钉/企微）、`storage/db.py` SQLite、docker/、
  插件目录自动扫描发现、logging.conf。（disk_clean 不再单独立动作：M1 后直接加进 actions.yaml 即可。）

---

### 验证

```bash
# 全量离线单测（仓库根，conda langchain1.2）——不碰真实 DeepSeek/Prometheus/SSH
python -m pytest tests -q            # 目前 60 passed（test_diagnostics.py 22 条、test_action_catalog.py 12 条、
                                     # test_chat.py 含 M2 同源回归「工具输出==兜底输出」、no-langchain 回归）

# 无 langchain 降级回归（单独跑）
python -m pytest tests/test_no_langchain_import.py -q
```

实机链路（需要 .env 有 DEEPSEEK_API_KEY + SSH_PASSWORD_192_168_20_128、装好 paramiko、目标机可达，
由用户自行准备与执行）：`uvicorn src.main:app --reload --host 127.0.0.1 --port 8000` →
`POST /chat {"user":"ops","message":"帮我看看系统忙不忙"}`；改 `config/diagnostics.yaml` 后重启观察工具集变化。
