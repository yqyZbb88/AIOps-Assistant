# ops-agent 结构优化 · 交接提示词（新会话从这里开始）

> 用途：把下方「★ 从这里开始粘贴」整段贴到新对话开头，模型即获得本次任务的完整上下文——
> 改什么、为什么改、改到什么程度算完、哪些不许碰、怎么验证。
>
> 背景：2026-09-10 对 `src/` 做了一次**结构复审**（逐文件通读 + 全仓符号引用核查 + 测试与文档对照）。
> 结论：分层骨架扎实（container/registry/interfaces/plugins 的划分、惰性装配、缺依赖降级都兑现了），
> 遗留问题是 **M1/M2 两次迁移只做了一半的残留** + 少量接口松动。本文件把这些残留整理成可执行任务。
>
> 配套文档分工：
> - [README.md](./README.md)：结构 / 分层职责 / 配置 / 启动 / 测试 / 扩展指南（**权威结构文档**）；
> - [REFACTOR_PROMPT.md](./REFACTOR_PROMPT.md)：上一轮重构的硬性约束 / 坑清单 / 既定决策（**约束来源**）；
> - **本文件**：本轮结构优化的任务清单与验收标准。

---

## ★ 从这里开始粘贴

你在仓库根 `c:\Users\yqyZ\Desktop\Assitant`（Windows，conda 环境 `langchain1.2`，
Python = `D:\MiniConda\envs\langchain1.2\python.exe`；shell 为 Git-Bash）。这是一个**已可运行**的
「智能运维 Agent」：FastAPI 服务 + LangChain 聊天 Agent + 告警写命令目录自动处置 + AI 只读主机诊断。

本轮任务**不是**加功能，而是**清理结构债**：把 M1（写命令目录）/ M2（能力单一实现）两次迁移留下的
半截残留收尾，并拧紧几处类型契约。骨架不要重做，分层不要改，只做本文件「任务清单」里列的事。

动手前先读 [README.md](./README.md) 的「项目结构 / 分层职责」与 [REFACTOR_PROMPT.md](./REFACTOR_PROMPT.md)
的「硬性约束 / 坑清单 / 既定决策」——本文件的约束大多继承自它们，重复的部分在此仍写全，但细节以那两份为准。
拿不准时进入计划模式先征询方案。**既定决策（REFACTOR_PROMPT「既定决策记录」）不要推翻。**

### 硬性约束（违反即改错，继承自上一轮重构）

1. 根目录 **`init.py` 只读**：它是用户的模板，也是**唯一**的模型入口（`init_chat_model(...)`）。
2. **不修改 `.env`；不回显 / 不新增 / 不探明任何明文密钥**。真实 SSH 密码只以
   `${SSH_PASSWORD_192_168_20_128}` 这类**占位符**出现在 `config/*.yaml`。
   用户要求列出 `.env` 的键或值时——**不做**，说明理由即可。
3. **分层依赖方向**：`api/ → core/ → interfaces/`；**core 不得 import `src.agent.*`**
   （本轮 P0-1 就是在修这条）。`plugins/` 实现 `interfaces/` 契约；`utils/` 是叶子层基础设施
   （现有三个模块都不 import `src` 内任何东西，**别打破这一点**）。
4. **顶层不 import 重量依赖**：`langchain` / `langgraph` / `paramiko` / `dotenv` 一律在函数内惰性 import。
   改完必须跑 `tests/test_no_langchain_import.py`（它在子进程里把 langchain/dotenv 屏蔽为「未安装」）。
5. **安全不变量**（本次改动不应触碰，但读代码时会对号入座）：模型上下文 / 工具参数里永无
   host / 密码 / 私钥 / 自由 shell 命令；命令串 = 配置模板 + 逐参校验并 `shlex.quote` 的值；
   只读诊断恒不 sudo；每个真实 SSH 尝试写 `ops_agent.audit`（绝不含密码）。
6. **`config/*.yaml` 的正文与注释都不要出现字面 `${`** ——`interpolate()` 对整个文件文本替换，
   注释里的 `${...}` 也会被当环境变量，冒出误导性日志。
7. 测试纪律：`python -m pytest tests -q`（仓库根，conda `langchain1.2`），**全部离线**——
   不发起任何真实 DeepSeek / Prometheus / SSH 调用（构造 Agent 可以，invoke 不行）。
   当前基线：**60 passed**。每完成一个任务就重跑一次，别攒到最后。

---

## 任务清单

分三档：**P0 是真正的结构缺陷**（建议各成一个 commit，便于回滚）；**P1 是接口收紧**；
**P2 是收尾项**（可选，按需做）。行号是 2026-09-10 的快照，可能已漂移，以函数名 / 符号名为准。

### P0-1　core 反向依赖 agent（唯一的真分层违规）

**问题**：[src/core/analysis.py:17](src/core/analysis.py#L17) 写着
`from src.agent.prompts import ALERT_ANALYSIS_SYSTEM`。README「分层职责」表与 REFACTOR_PROMPT
硬性约束 #3 都明确禁止 core 依赖 agent。现在没炸，只是因为 `agent/prompts.py` 恰好是纯常量模块。

**风险不是理论上的**：[src/agent/prompts.py:45-56](src/agent/prompts.py#L45-L56) 已经躺着
`ToolStrategy` 结构化输出的一整段启用示例注释。哪天有人取消注释启用结构化输出（REFACTOR_PROMPT
路线图里明确列为待办），`core/analysis.py` 就跟着被拖进 langchain——而 analysis 正是
「无 langchain 环境也能降级」这条主卖点所在的路径。

**改法**（纯搬运，零逻辑改动）：
- 新建 `src/core/prompts.py`，把 `AGENT_NAME` / `OPS_SYSTEM_PROMPT` / `ALERT_ANALYSIS_SYSTEM`
  以及结构预留的那段注释整段搬过去；
- `src/core/analysis.py` 改 import `src.core.prompts`；
- `src/agent/assistant.py:154` 改 import `src.core.prompts`（agent → core 是允许方向，
  `agent/tools.py` 已经在 import `core/capabilities`，一致）；
- 删除 `src/agent/prompts.py`（确认无其他引用后）。

**验收**：
- `grep -rn "src\.agent" src/core` 返回空；
- `python -m pytest tests -q` 仍 60 passed；
- `python -m pytest tests/test_no_langchain_import.py -q` 通过。
- 同步更新 [README.md](./README.md) 的「项目结构」树（`agent/prompts.py` 挪走了）与
  [REFACTOR_PROMPT.md](./REFACTOR_PROMPT.md) 的「模块地图」。**文档落伍是上一轮的主要遗留问题之一，
  这次别再留。**

### P0-2　registry 里 actions 那一半是死代码

**问题**：[src/registry.py](src/registry.py) 的 `_actions` 整族（`register_action` /
`action_class` / `action_names` / `all_action_classes` / `has_action`）在 M1b 之后**已无调用方**
（全仓 grep 核实：除 registry.py 自身外零命中）。写动作现在走 `config/actions.yaml` 目录 +
`container.executors()`，不再注册类。

**为什么必须删**：这不是「留着无害」——文档与代码在互相误导。[src/registry.py:1](src/registry.py#L1)
的 docstring 仍写「monitors / actions / llms / diagnostics 四类」，扩展指南也写着「新增插件 →
registry.py 加类别」，后来人会以为加动作要动这里。

**改法**：
- 删掉 `_actions` 字典与上述五个方法；
- 模块 docstring 改为「monitors / llms / diagnostics 三类」，并补一句
  「写动作（actions）不走注册表：见 config/actions.yaml 目录 + `container.executors()`」；
- `_MISSING` **保留**（`diagnostic_class` 还在用）；`has_monitor` / `has_llm` 与 `has_diagnostic`
  是同一族对称接口，**保留**；
- 顺手修 [src/storage/models.py:95](src/storage/models.py#L95) 的过期描述
  `description="动作名，必须已注册到 actions 注册表"` → 改为「必须是 config/actions.yaml 声明的写命令 key」。

**验收**：`python -m pytest tests -q` 通过（`test_diagnostics.py:257` 用到的 `has_diagnostic` 不受影响）。

### P0-3　`init.py` 模型入口的加载逻辑被复制成两份

**问题**：REFACTOR_PROMPT 硬性约束 #1 说「init.py 是唯一的模型入口」——入口确实唯一，
但**加载逻辑不唯一**：
- [src/agent/assistant.py:48-72](src/agent/assistant.py#L48-L72) 的 `get_model()`；
- [src/plugins/llm_clients/deepseek_client.py:26-45](src/plugins/llm_clients/deepseek_client.py#L26-L45) 的 `_get_model()`。

两者逐行为同一件事：`load_dotenv(_PROJECT_ROOT/.env, override=True)` → `sys.path.insert` →
`from init import model` → 失败包成 `LLMError`，各缓存一份进程内单例。此外
`_content_text()` 在 [assistant.py:117](src/agent/assistant.py#L117) 与
[deepseek_client.py:48](src/plugins/llm_clients/deepseek_client.py#L48) **逐字符相同**（15 行）。

**改法**：新建 `src/utils/model_loader.py`（惰性 import，**顶层不碰 langchain**），暴露
`get_model()` 与 `content_text()`，两处改为 import 复用。

**一个需要你拍板的点**（我倾向 (b)）：
- (a) `model_loader` 直接抛 `interfaces.llm_client.LLMError` —— 但这会让 `utils/` 依赖 `interfaces/`，
  破坏「utils 是叶子层」的现状；
- (b) **推荐**：`model_loader` 抛自己的 `ModelLoadError`，两个调用方各自 catch 后映射为
  自己的 `LLMError`（`assistant.LLMError` / `interfaces.LLMError`），保持 utils 零 src 内依赖。

**注意**：别把 `model_loader` 加进 `src/utils/__init__.py` 的 eager import 链——
`tests/test_no_langchain_import.py` 会 `from src.utils import ...`，加了会扩大该测试的 import 面
（只要顶层不 import langchain 仍能过，但没必要扩大暴露面）。

**验收**：`python -m pytest tests -q` 通过；`tests/test_no_langchain_import.py` 通过；
`grep -c "from init import model" -r src` 的结果应只剩 model_loader 一处。

### P1-1　capabilities 用 `getattr` 字符串绕过了接口契约

**问题**：[src/core/capabilities.py:38-47](src/core/capabilities.py#L38-L47) 的
`_usage_text(monitor, method: str, name: str)` 内部 `getattr(monitor, method)()`。
为了省掉两个函数各 4 行重复，代价是**字符串调用绕过了 `MonitorSource` 的抽象方法契约**——
类型检查器与 IDE 都发现不了 `"cpu_usage"` 拼错，而这恰恰是 interfaces 层存在的意义。
抽象的复杂度比它消掉的多。

**改法**（二选一，推荐后者——保留共用逻辑又不丢类型）：

```python
def _usage_text(getter: Callable[[], float | None], name: str) -> str:
    """单百分比指标的共用实现：取数 → MonitorError 转失败文案 → None 转暂无数据。"""
    try:
        value = getter()
    except MonitorError as exc:
        ...

def cpu_usage_text(monitor: MonitorSource) -> str:
    return _usage_text(monitor.cpu_usage, "CPU 使用率")
```

**文案一个字都不能变**（有测试逐字断言）：成功 `CPU 使用率：45.0%` / 失败 `查询 CPU 使用率失败：{exc}` /
无数据 `…暂无数据（请确认 Node Exporter 已采集）`。

**验收**：`tests/test_chat.py::test_agent_tools_share_text_with_fallback`（M2 同源回归）必须仍然通过——
它断言 `fb == out and fb == "CPU 使用率：45.0%"`，是这次的护栏。

### P1-2　三个 `Any` 抹掉了层间接口

| 位置 | 现状 | 改为 |
|---|---|---|
| [src/core/chat.py:85-90](src/core/chat.py#L85-L90) | `assistant_factory: Callable[[], Any]`、`self._assistant: Any` | `Callable[[], Assistant] \| None`，`Assistant` 是 Protocol |
| [src/core/agent.py:41](src/core/agent.py#L41) | `rule_engine: Any \| None = None` | `RuleEngine \| None = None` |

**关于 `rule_engine` 的说明**：注释里担心的「core/agent 不想硬依赖 rule_engine」理由**不成立**——
同文件的 `chat_service: ChatService` 就是直接类型标注的，且 `rule_engine.py` 不 import `core/agent`
（无环）。这是纯粹的标注遗漏。

**关于 `Assistant` Protocol**：新建 `src/interfaces/assistant.py`：

```python
from typing import Protocol

class Assistant(Protocol):
    """聊天 Agent 契约：core/chat 只要求一个 reply(message)->str 的对象。

    langchain 实现见 src/agent/assistant.py 的 OpsAssistant；
    测试可注入任意同形状假对象（见 tests/test_chat.py 的 FakeAssistant）。
    """
    def reply(self, message: str) -> str: ...
```

`typing.Protocol` 是标准库，不违反 interfaces 层「零第三方依赖」。
`ChatService` 与 `OpsAssistant` 的契约目前完全靠「那个对象正好有个 `reply` 方法」的口头约定维持——
加上它，「core 不感知 langchain」就从注释变成了类型系统能验证的事实。
记得同步导出到 [src/interfaces/__init__.py](src/interfaces/__init__.py)。

**验收**：`python -m pytest tests -q` 通过；`tests/test_chat.py` 里的 `FakeAssistant`（一个只有
`reply` 的本地类）不改一行仍然类型正确。

### P2-1　目录级配置加载重复（可选）

M1 把**命令级**机制收编进了 [common/cmddef.py](src/plugins/common/cmddef.py)，但**目录级**骨架
[catalog.py](src/plugins/actions/catalog.py) 与 [commands.py](src/plugins/diagnostics/commands.py)
仍是两份：`enabled` 布尔校验 → `commands` 映射解析 → `interpolate` → 文件缺失/解析失败 → `None` + 降级日志。

可抽 `common/catalog.py` 的通用加载器（文件定位 + 读文本 + 插值 + yaml + 异常收敛为 None + 日志）。
**注意**：`DiagnosticsConfig` 比 `ActionsConfig` 多了 `provider` / `target` / `output_limit`，
解析器只能抽「外壳」，字段解析留在各自模块。收益中等、有回归风险，**优先级低于 P0/P1**，
想省事可以只在两处加一行「与 X 同构」的注释而不真抽。

### P2-2　输出通道没有抽象（可选）

[src/core/rule_engine.py:184](src/core/rule_engine.py#L184) 与 [src/core/agent.py:100](src/core/agent.py#L100)
直接用 `print()` 往 stdout 打处置结果与 AI 建议，而文档写着「后续可换钉钉/企微」。
真要换时得改 core。建议在 `interfaces/` 加 `Notifier` Protocol，默认实现 = 现在的 `print` 行为
（保证对外行为零变化），container 注入。

**这是设计决策不是缺陷**——做之前先问用户，别自作主张改输出格式（`print` 的内容是用户观察
`auto_execute=true` 效果的主要窗口）。

### P2-3　测试盲区（可选，但性价比高）

以下三个模块**零单测**，且恰好是「降级路径」最密集的地方（降级逻辑最容易静默写错）：

- [src/container.py](src/container.py)（176 行，全部装配与降级逻辑）：未注册的 monitor provider →
  `RuntimeError` 且报错含可用列表；`llm.enabled=false` → `None`；llm provider 未注册 → `None` + 警告；
  actions.yaml 缺失 → `executors() == {}`；缺 paramiko → `diagnostics()` 为 `None`（`sys.modules` 屏蔽法）。
- [src/core/analysis.py](src/core/analysis.py)：`build_alert_context` 的 `instance` 为
  `unknown`/空 → 走「实例信息缺失」分支；趋势有数据 vs 缺失（应补三行「暂无趋势数据」占位）；
  `llm=None` 时 `analyze_many` 短路的语义。
- [src/plugins/monitors/prometheus_monitor.py](src/plugins/monitors/prometheus_monitor.py)：`_clamp` 边界、
  `_summarize_values` 的 None/空值跳过、`active_alerts` 的 firing 优先排序、`disk_usage` 的降序
  与坏数据跳过（`_request` 可 monkeypatch，保持离线）。

新增测试沿用 [tests/conftest.py](tests/conftest.py) 的 `FakeMonitor` / `FakeLLM` / `FakeDiagnostics`。

---

## 不要动的东西（复审判定：现状是对的）

- **`plugins/common/` 共享内核**（endpoint / ssh / audit / cmddef）：本轮重构做得最好的部分，
  读写两条边界抽得干净，`cmddef` 的「模板占位集合 == params 声明集合」这类 fail-loud 校验是刻意的，
  别为了「灵活」放松它。
- **`container.py` 的惰性装配 + 缓存**：设计正确，只缺测试（见 P2-3）。
- **`capabilities.py` 作为能力单一实现的定位**：M2 的决策正确，别把「状态/帮助/DEFAULT_REPLY」
  这类 fallback-only 的东西塞进来（REFACTOR_PROMPT 既定决策 #6 已明确）。
- **`ChatService.Rule` 那个小规则框架**：有点自造感，但有测试锁着且确实换来了可读性，不值得改。
- **对外 HTTP 契约**（`/health` `/chat` `/alert` `/alerts/*` 的请求响应）：一个字都别改。
- **`config/*.yaml` 的现有格式与示例内容**：本轮不需要动配置。

---

## 验证

```bash
# 1) 全量离线单测（仓库根，conda langchain1.2）——基线 60 passed
python -m pytest tests -q

# 2) 无 langchain 降级回归（P0-1 / P0-3 改完必跑）
python -m pytest tests/test_no_langchain_import.py -q

# 3) P0-1 的方向检查（应无输出）
grep -rn "src\.agent" src/core

# 4) P0-3 的入口收敛检查（应只剩 model_loader 一处）
grep -rn "from init import model" src
```

实机链路由用户自行执行（需 `.env` 有 `DEEPSEEK_API_KEY` + `SSH_PASSWORD_192_168_20_128`、
装好 paramiko、目标机可达）——**本轮任务不需要联网验证，不要代跑**。

## 执行顺序建议

```
P0-1（搬 prompts，5 分钟）→ P0-2（删死代码，10 分钟）→ P0-3（抽 model_loader）
→ 每步跑一次全量测试 → P1-1 → P1-2 → 再跑测试
→ P2 按用户意愿挑，别一次全做
```

P0 三条各成一个 commit（仓库尚未 `git init`，见 REFACTOR_PROMPT 路线图——如果正好要做 `git init`，
建议先把当前状态作为基线提交，再逐条提交 P0，便于对比与回滚）。
**P0-1 + P0-2 + P0-3 做完就该停下来让用户确认，别顺势把 P1/P2 也做掉。**
