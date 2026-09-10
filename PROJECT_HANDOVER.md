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
> - **本文件**：硬性约束 / 安全不变量 / 已踩的坑 / 既定决策 / 路线图待办 / 验证方式。

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
11. 测试纪律：`python -m pytest tests -q`（仓库根，conda `langchain1.2`），**全部离线**，
    用 [tests/conftest.py](tests/conftest.py) 的 `FakeMonitor` / `FakeLLM` / `FakeDiagnostics` 注入；
    新能力要带「屏蔽 langchain 后仍可 import + 聊天回落关键词」的回归（照
    [tests/test_no_langchain_import.py](tests/test_no_langchain_import.py) 的子进程屏蔽法写）。
    **当前基线：189 passed**（2026-09-10 M3 后；M3 新增 79 例，其中
    `test_agent_actions_hitl_graph.py` 用**真 graph + 脚本化假模型**验证审批语义，
    含一条覆盖「容器 → 工厂 → OpsAssistant → 执行」的端到端装配用例——
    单件全绿但接线接错时只有它会红）。

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
| 文档与代码互相误导（2026-09-10 集中清理过一轮） | 代码改了就同步文档；**解释「已不存在的东西」的段落一律删掉**（如「原 X 已被 Y 取代而移除」），只留仍然有用的约束性说明。 |

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
  现状确为**单轮**：`reply()` 每轮只传当前一条消息
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
    → `assistant.reply(message, *, history=None)`（带默认值的 kwarg，既有调用点与 FakeAssistant 都不失效）
    → `api/sessions.py`（`GET /sessions`、`GET /sessions/{id}`、`DELETE /sessions/{id}`）。

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

  **未决（动手前先问用户）**：路线 A 还是 B（倾向 B）；不传 `session_id` 时新建会话还是保持单轮
  （倾向新建）；`/chat` 加字段还是用 header（倾向加字段）。
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

- `notifiers`（钉钉/企微）、[src/storage/db.py](src/storage/db.py) 的 SQLite 持久化（仅 TODO 占位，
  **由路线图的「对话历史维护」激活**——届时历史表就落在这里）、
  `docker/`、插件目录自动扫描发现、`logging.conf`。
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
  **唯一的例外已登记**：路线图「对话历史维护」计划给 `/chat` **追加**可选 `session_id`
  （请求与响应各一个字段，属**扩充**而非修改，同当初加 `/approvals` 的性质）——
  真做的时候要连同 `tests/test_api_approvals.py:112` 的 `set(resp.json()) == {"reply"}`
  与本文件此条一并更新，别让两处对不上。
- **`config/*.yaml` 的现有格式与示例内容**：无需求不动配置。
- **安全不变量相关的一切**（`shlex.quote`、redact、审计、恒不 sudo、凭据不进模型）：只许加固，不许放松。
- **M3 的人工审批**（2026-09-10 新增，同样只许加固）：写工具与审批策略必须继续**成对产出**
  （`WriteToolBundle`）；不得引入 `edit` 决策；`config/agent_actions.yaml` 的 `approval` 段
  不得新增开关键；审批护栏只许加在 **Agent 路径**，绝不下沉进 `execute()`/`run()` 内部
  （否则 `RuleEngine` 的无头自动处置会被连带影响）。
  **已知残余风险**：`/approvals/*` 无鉴权，见 README「安全说明」——绑 `0.0.0.0` 前必须先加鉴权。

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
python -m pytest tests -q            # 目前 189 passed（2026-09-10 M3 写命令+人工审批后）
                                     #   M3 相关：agent_actions_catalog 16、agent_action_source 12、
                                     #     approval_store 10、agent_tools_approval 10、
                                     #     api_approvals 9、hitl_graph 15、container 16 → 23
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
