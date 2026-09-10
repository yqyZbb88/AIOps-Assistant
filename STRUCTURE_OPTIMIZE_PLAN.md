# ops-agent 仓库卫生 + 注释整理 + 结构收尾 · 执行计划书

> 用途：把下方「★ 从这里开始粘贴」整段贴到**新会话**开头（或直接把本文件作为附件交给新会话），
> 执行者即获得本轮任务的完整上下文——做什么、按什么顺序、怎么算做完、哪些不许碰。
>
> 本文件由 2026-09-10 的一次只读代码复审产出，**已逐条核对文件与行号**（行号是当日快照，
> 以符号名为准时以符号名为准）。
> 本轮与 [STRUCTURE_OPTIMIZE_PROMPT.md](./STRUCTURE_OPTIMIZE_PROMPT.md) 覆盖同一批目标，但做了三件事：
> **① 把「仓库卫生」「注释整理」提到最前面；② 修正了原提案的 3 处遗漏 / 不精确；③ 补了 4 类它没覆盖的债。**
> 两份文档冲突时，**以本文件为准**。
>
> 配套文档分工：
> - [README.md](./README.md)：结构 / 分层职责 / 配置 / 启动 / 测试 / 扩展指南（**权威结构文档**）；
> - [REFACTOR_PROMPT.md](./REFACTOR_PROMPT.md)：硬性约束 / 坑清单 / **既定决策**（**约束来源，不要推翻**）；
> - [STRUCTURE_OPTIMIZE_PROMPT.md](./STRUCTURE_OPTIMIZE_PROMPT.md)：上一轮结构复审清单（**参考**，修正见文末附录）；
> - **本文件**：本轮执行的**阶段划分 + 验收标准 + 提交粒度**。

---

## ★ 从这里开始粘贴

你在仓库根 `c:\Users\yqyZ\Desktop\Assitant`（Windows，conda 环境 `langchain1.2`，
Python = `D:\MiniConda\envs\langchain1.2\python.exe`）。这是一个**已可运行**的
「智能运维 Agent」：FastAPI 服务 + LangChain 聊天 Agent + 告警写命令目录自动处置 + AI 只读主机诊断。

本轮任务**不是**加功能：先**修好环境**，再**整理仓库卫生**，然后**注释/文档整理**，
最后才是**结构收尾**（分层违规、死代码、重复实现、类型契约、降级路径测试）。
**骨架不要重做，分层不要改，对外 HTTP 契约一个字都不要动。**

动手前先读 [README.md](./README.md) 的「项目结构 / 分层职责」与 [REFACTOR_PROMPT.md](./REFACTOR_PROMPT.md)
的「硬性约束 / 坑清单 / 既定决策」。**既定决策（REFACTOR_PROMPT「既定决策记录」）不要推翻。**

### 开工前必须向用户确认的 3 件事（都要用户点头，别自己拍板）

1. **允许安装两个缺失依赖**：当前 conda 环境 `langchain1.2` **缺 `typing_extensions` 与 `Pygments`**，
   导致 `pydantic` 无法 import、`pytest` 无法启动（2026-09-10 实测）。修复命令：
   `"D:/MiniConda/envs/langchain1.2/python.exe" -m pip install typing_extensions pygments`。
   **不修好环境，后面每一步的验收都做不了。**
2. **是否现在就 `git init` + 打基线提交**：仓库当前**不是 git 仓库**（`.git` 不存在）。
   本轮改动较多，强烈建议先打基线再逐条提交，便于对比回滚。
3. **`参考/` 是否入库**：该目录是尚硅谷课程讲义，**55.3 MB / 152 个文件**（绝大多数是 PNG），
   一旦入库就永久留在 git 历史里。**建议默认排除**（`.gitignore` 加 `参考/`），
   若用户要求入库，退一步只排除图片（`参考/**/assets/`）。

确认后按「阶段 0 → 阶段 9」顺序做，**一个任务一个 commit，做完一个立刻跑一次测试**，别攒到最后。

### 硬性约束（违反即改错，继承自前两轮）

1. 根目录 **`init.py` 只读**：它是用户的模板，也是**唯一**的模型入口（`init_chat_model(...)`）。
2. **不修改 `.env`；不回显 / 不新增 / 不探明任何明文密钥**。真实 SSH 密码只以
   `${SSH_PASSWORD_192_168_20_128}` 这类**占位符**出现在 `config/*.yaml`。
   用户要求列出 `.env` 的键或值时——**不做**，说明理由即可。
3. **分层依赖方向**：`api/ → core/ → interfaces/`；**core 不得 import `src.agent.*`**
   （阶段 4 就是在修这条）。`plugins/` 实现 `interfaces/` 契约；`utils/` 是叶子层基础设施
   （现有三个模块都不 import `src` 内任何东西，**别打破这一点**）。
4. **顶层不 import 重量依赖**：`langchain` / `langgraph` / `paramiko` / `dotenv` 一律在函数内惰性 import。
   改完必须跑 `tests/test_no_langchain_import.py`（它在子进程里把 langchain/dotenv 屏蔽为「未安装」）。
5. **安全不变量**（本轮不应触碰，但改代码时逐条对号）：模型上下文 / 工具参数里永无
   host / 密码 / 私钥 / 自由 shell 命令；命令串 = 配置模板 + 逐参校验并 `shlex.quote` 的值；
   只读诊断恒不 sudo；每个真实 SSH 尝试写 `ops_agent.audit`（绝不含密码）；
   `config/*.yaml` 的正文与注释**都不要出现字面 `${`**（`interpolate()` 对整个文件文本替换）。
6. **文案即规范**：`core/capabilities.py` 的输出文本有测试逐字断言（`CPU 使用率：45.0%` 等），
   重构时**一个字都不能变**。
7. 测试纪律：**全部离线**（不发起任何真实 DeepSeek / Prometheus / SSH 调用；构造 Agent 可以，invoke 不行）。
   基线 = **60 passed**。每完成一个任务重跑一次。

---

## 任务清单

### 阶段 0　环境修复与基线（阻塞项，先做）

**症状（2026-09-10 实测）**

```text
D:/MiniConda/envs/langchain1.2/python.exe -m pytest tests -q
  -> ModuleNotFoundError: No module named 'pygments'            （pytest 9 的依赖）
D:/MiniConda/envs/langchain1.2/python.exe -c "import src.container"
  -> ModuleNotFoundError: No module named 'typing_extensions'    （pydantic 2.12 的依赖）
```

即：文档承诺的「基线 60 passed」**现在不可复现**。环境曾正常跑过
（`tests/__pycache__` 里留有 `*-pytest-9.0.3.pyc`），说明是后来被改动过。

**改法**：经用户同意后安装这两个依赖（见「开工前必须确认的第 1 条」）。

**验收**

```bash
python -m pytest tests -q                        # 必须 60 passed（本次基线）
python -m pytest tests/test_no_langchain_import.py -q
```

> 若 `pip` 因网络受限失败：停下来把完整报错交给用户，**不要**换解释器、不要改测试来「跑通」。

---

### 阶段 1　仓库卫生（本轮最高优先）

> 这一阶段**只碰文件组织与忽略规则，不碰任何 `.py` 逻辑**。做完就提交，后面阶段才有干净的 diff。

#### 1.1　`.gitignore` 补漏

**问题**：`.gitignore` 已覆盖 `.env` / `.venv/` / `__pycache__/` / `*.pyc` / `.pytest_cache/` / `.vscode/`，
但**没有覆盖** `.claude/settings.local.json`（本机工具配置，内容是命令权限白名单，无密钥但属本机状态），
也没有 `参考/`。

**改法**：追加两段（`参考/` 是否加取决于用户对第 3 条的决定）：

```gitignore
# 本机工具配置（含本机命令白名单，不入库）
.claude/settings.local.json

# 课程讲义（体积大，不入库；若要入库请删掉这一行）
参考/
```

**验收**：下面三行都要有输出（说明命中忽略规则）：

```bash
git check-ignore -v .env
git check-ignore -v .claude/settings.local.json
git check-ignore -v "参考/尚硅谷-05-Tools/尚硅谷-05-Tools.md"
```

#### 1.2　清理缓存与陈旧字节码

**问题**：`src/**/__pycache__`、`tests/**/__pycache__`、根 `.pytest_cache/` 里有历史产物，
其中 `src/plugins/actions/__pycache__/service_action.cpython-313.pyc` 与
`ssh_action.cpython-313.pyc` 对应的**源文件早在 M1b 迁移中就被删了**，属于幽灵产物
（会让人以为这两个模块还在）。

**改法**：删除全部 `__pycache__/` 与 `.pytest_cache/`（纯可再生，无信息损失）。**只删这些目录，别动其它。**

**验收**：`Get-ChildItem -Recurse -Directory -Filter __pycache__ | Measure-Object` 计数为 0；
紧接着跑一次 `python -m pytest tests -q` 仍 60 passed（它们会重新生成，然后被 `.gitignore` 兜住）。

#### 1.3　`git init` + 基线提交（需用户确认）

```bash
git init
git add .
git status --short          # 逐行确认：不得出现 .env / 参考/ / __pycache__ / .pytest_cache
git commit -m "chore: 基线快照（重构完成后、结构收尾前）"
```

**验收**：`git status --short` 干净；下面两条都无输出：

```bash
git ls-files | Select-String "参考|\.env$|__pycache__|settings.local"
git ls-files | Select-String "obs-agent.txt"
```

#### 1.4　进入阶段 2 的门槛

```bash
git log --oneline        # 至少一条基线提交
git status --short       # 空
python -m pytest tests -q   # 60 passed
```

---

### 阶段 2　注释与文档整理（第二优先，**只改文字，不改逻辑**）

> 本阶段的判定标准（写进每个 commit message，也写进汇报）：
> **A. 修正与代码不符的描述；B. 删除解释「已不存在的东西」的段落；
> C. 保留安全不变量、扩展指引、「为什么这么写」的决策类注释——这类是资产，不许当垃圾删。**
> 本阶段全部是「文档与代码互相误导」问题，**零行为变更**，因此不需要新测试，跑既有 60 条即可。

#### 2.1　死链：`obs-agent.txt` 根本不存在

| 位置 | 现状 | 改为 |
|---|---|---|
| [README.md:16](./README.md) | `代码已按 [obs-agent.txt](./obs-agent.txt) 的目标架构就地重构` | 去掉链接，写成「已按既定目标架构就地重构（`ops_bot/` 已删除）」，要留痕就指向 REFACTOR_PROMPT 的既定决策记录 |
| [REFACTOR_PROMPT.md:160](./REFACTOR_PROMPT.md) | `obs-agent.txt 仅作参考保留` | 改为「原 `obs-agent.txt` 已不在仓库内」或直接删掉该分句 |

#### 2.2　`registry` 的类别说明（与阶段 3 的删代码对齐，直接写**最终态**）

| 位置 | 现状 | 改为 |
|---|---|---|
| [README.md:71](./README.md) | `插件注册表（monitors / actions / llms / diagnostics）` | `插件注册表（monitors / llms / diagnostics；写动作走 config/actions.yaml 目录，不入注册表）` |
| [src/registry.py:1](./src/registry.py) | `monitors / actions / llms / diagnostics 四类插件按名注册与取用` | 同上口径（三类 + 一句「写动作不走注册表：见 config/actions.yaml + `container.executors()`」） |

> 注意阶段依赖：本条的**文案**在阶段 2 落地，阶段 3 才删对应**代码**。先写文案不会冲突。

#### 2.3　「plugins/actions 基类」——基类早就不存在了

| 位置 | 现状 | 改为 |
|---|---|---|
| [README.md:274](./README.md) | `plugins/actions 基类在 execute 前后向 ops_agent.audit logger 写结构化记录` | 「写命令执行器（`CatalogCommandExecutor`）经 `plugins/common/audit.py` 统一向 `ops_agent.audit` 写结构化记录」 |

#### 2.4　`interfaces/action.py` 指向了不再存在的机制

| 位置 | 现状 | 改为 |
|---|---|---|
| [src/interfaces/action.py:4](./src/interfaces/action.py) | `不关心动作如何实现（SSH / 其它）；具体实现注册到 src/registry.py。` | 「不关心动作如何实现（SSH / 其它）；具体实现由 `container.executors()` 按 `config/actions.yaml` 目录构建并注入（**actions 不走 registry**）。」 |

#### 2.5　`storage/models.py` 的字段描述

| 位置 | 现状 | 改为 |
|---|---|---|
| [src/storage/models.py:95](./src/storage/models.py) | `description="动作名，必须已注册到 actions 注册表"` | `description="config/actions.yaml 写命令目录的 key"` |

#### 2.6　`plugins/actions/__init__.py` 的「已删除说明」段

[src/plugins/actions/__init__.py:10](./src/plugins/actions/__init__.py) 留着
「原 service_action.py / ssh_action.py 已被…取代而移除」。按判定标准 B：**删掉这半句**，
只留「M1b 起动作不再是 Python 类，走目录 + 通用执行器」的现行语义（现行语义是资产，别删）。
同类排查：全仓搜 `已删除` / `已被.*取代` / `原 ops_bot`，只保留仍然有用的**约束性**说明。

#### 2.7　顺手全仓扫一遍措辞

```bash
git grep -n "obs-agent.txt"
git grep -n "必须已注册到 actions 注册表"
git grep -n "注册表（monitors / actions"
git grep -n "actions 基类"
git grep -n "M1b"        # 保留：这是决策标记；只确认没有指向已删代码
```

**阶段 2 验收**

```bash
python -m pytest tests -q        # 仍 60 passed（本阶段不该动到任何逻辑）
git grep -n "obs-agent.txt"      # 仅剩 REFACTOR_PROMPT 中已改写后的表述
git grep -n "必须已注册到 actions 注册表"   # 无输出
```

---

### 阶段 3　死代码清理（含测试同步）

> 判定标准同阶段 2 的 A/B/C，但这次**要改代码**，所以必须逐条跑测试。
> 核心原则：**死代码本身无害，但「文档 / 测试声称它有用」会持续误导后来人**——这才是要删的理由。

#### 3.1　registry 的 `_actions` 一整族（原提案 P0-2，判定成立）

**证据**：全仓只有 [src/registry.py](./src/registry.py) 自己引用
（`register_action` / `action_class` / `action_names` / `all_action_classes` / `has_action`）；
写动作现在走 `config/actions.yaml` + `container.executors()`。阶段 2.2 已把 docstring 改为三类。

**改法**

- 删 `_actions: dict[str, type]` 与上述 5 个方法；
- **不要删** `has_monitor` / `has_llm` / `has_diagnostic`（同族对称接口，`tests/test_diagnostics.py:257` 用 `has_diagnostic`）。

**附带的一个小决定（原提案漏了）**：删掉 `action_class` 之后，
`diagnostic_class(name, default=_MISSING)` 的 **`default` 分支也再无任何调用方**
（`monitor_class` / `llm_class` 都没有这个参数）。二选一，别放着不管：

- **推荐**：一并把 `diagnostic_class` 的 `default` 参数删掉，与另两个取类方法对称，`_MISSING` 随之删除；
- 或：保留参数并在 docstring 注明「预留：调用方需要时传默认实现」。

**验收**：`python -m pytest tests -q` 通过；
`git grep -n "_actions\|action_class\|all_action_classes"` 无实现残留。

#### 3.2　`utils/security.require_params`（原提案漏了，且**比普通死代码更糟**）

**证据**：[src/utils/security.py:37](./src/utils/security.py) 在生产路径上**零调用**
（host 必填由 `plugins/common/endpoint.py` 的 `endpoint_from_params` 校验，
命令参数由 `plugins/common/cmddef.py` 的 `validated_params` 校验）；
唯一引用是 [tests/test_security.py:37-46](./tests/test_security.py)。

**为什么必须处理**：它的 docstring 还写着「动作执行前用 `require_params()` 校验必填参数」——
**代码已死、文档还在宣称它是动作路径的一环，而且有测试为它背书**，会让人以为这条校验链还在。

**改法**：删除函数 + 模块 docstring 里对应的一句 + `tests/test_security.py` 的两个用例。
若判断要保留这个 API 位：必须在 docstring 明写「**预留，当前动作路径未使用**」——
**不允许维持现状**。

#### 3.3　其余三处零引用符号（一并处理）

| 位置 | 符号 | 建议 |
|---|---|---|
| [src/utils/config_loader.py:144](./src/utils/config_loader.py) | `interpolate_mapping`（只有自递归调用） | 删除（没有文档 / 注释承诺它是结构位） |
| [src/utils/logger.py:43](./src/utils/logger.py) | `get_app_logger` | 删除；若保留则 docstring 注明预留 |
| [src/core/exceptions.py:10](./src/core/exceptions.py) | `OpsAgentError` | **保留**：它是「业务异常基类」的结构位（README 结构树与 REFACTOR 模块地图都写了它），docstring 补一句「当前无子类，预留」即可 |

**阶段 3 验收**

```bash
python -m pytest tests -q                        # 全绿（测试数因 3.2 少 2 条，属预期）
python -m pytest tests/test_no_langchain_import.py -q
git grep -n "require_params\|interpolate_mapping\|get_app_logger\|register_action"   # 无输出
git status --short                               # 每个子任务各一个 commit，不留半截改动
```

---

### 阶段 4　P0-1：core 反向依赖 agent（唯一真分层违规；原提案判定成立，改法需精确）

**问题**：[src/core/analysis.py:17](./src/core/analysis.py) `from src.agent.prompts import ALERT_ANALYSIS_SYSTEM`。
现在没炸，只因 `agent/prompts.py` 恰好是纯常量；一旦有人启用 [src/agent/prompts.py:45-56](./src/agent/prompts.py)
那段 `ToolStrategy` 结构化输出示例，`core/analysis.py` 就会被拖进 langchain，
而 analysis 正是「无 langchain 也能降级」这条主卖点所在的路径。

**推荐改法（方案 A：按「谁用谁拥有」切分）**

- 新建 `src/core/prompts.py`，**只放** `ALERT_ANALYSIS_SYSTEM`（唯一使用方是 `core/analysis.py`）；
- `core/analysis.py` 改 import `src.core.prompts`；
- `src/agent/prompts.py` **保留** `AGENT_NAME` / `OPS_SYSTEM_PROMPT` / ToolStrategy 注释段
  （它们是 Agent 层话术，只有 `agent/assistant.py` 用）；
- 不需要删 `src/agent/prompts.py`，也不需要新增反向依赖。

**备选方案 B（原提案的做法）**：把三个常量整段搬到 `src/core/prompts.py`，删 `src/agent/prompts.py`，
`assistant.py:154` 改 import `src.core.prompts`。可行，但会让 core 持有 Agent 话术，
且 REFACTOR_PROMPT 的路线图会多一处漂移。**默认用方案 A。**

**文档同步（必做，且与本阶段同一个 commit）**

| 位置 | 方案 A | 方案 B |
|---|---|---|
| [README.md:68](./README.md) | 结构树里 `agent/prompts.py` 注为「Agent 系统提示词 / 名称」；core/ 下新增 `prompts.py`（告警分析 system） | 结构树把 `prompts.py` 整个挪到 core/ |
| [REFACTOR_PROMPT.md:118](./REFACTOR_PROMPT.md) | 模块地图 `agent/prompts.py` 注为「Agent 系统提示词 / AGENT_NAME（纯常量）」，core/ 下补 `prompts.py` | 同左但整体挪位 |
| [REFACTOR_PROMPT.md:190](./REFACTOR_PROMPT.md) | **不用改**（ToolStrategy 注释仍留在 agent/prompts.py） | 需同步改为 core/prompts.py |

**验收**

```bash
git grep -n "src\.agent" src/core        # 无输出
python -m pytest tests -q                # 60 passed
python -m pytest tests/test_no_langchain_import.py -q
```

---

### 阶段 5　P0-3：模型入口的加载逻辑被复制成两份（判定成立，但有两个坑要写清）

**问题**（两处逐行等价，各缓存一份进程内单例）：

- [src/agent/assistant.py:48-72](./src/agent/assistant.py) 的 `get_model()`；
- [src/plugins/llm_clients/deepseek_client.py:23-45](./src/plugins/llm_clients/deepseek_client.py) 的 `_get_model()`。

另外 `_content_text()` 在 [assistant.py:117](./src/agent/assistant.py) 与
[deepseek_client.py:47](./src/plugins/llm_clients/deepseek_client.py) **逐字符相同**。

**改法**：新建 `src/utils/model_loader.py`（顶层不 import langchain / dotenv / init），暴露
`get_model()` 与 `content_text()`；两处调用方改为复用。

**坑 1（原提案没提）**：新模块**不要自己算项目根路径**。两个旧副本一个用
`Path(__file__).resolve().parent.parent.parent`（assistant，深 3 层），一个用四层 `parent`（deepseek），
它们碰巧都指到仓库根；搬到 `src/utils/` 后层级又变了。**直接复用
`from src.utils.config_loader import ROOT_DIR`**（utils 内部依赖，不破坏叶子层语义）。

**坑 2（行为变更要写进 commit message）**：合并后**全进程只有一份模型缓存**
（原先 assistant 与 deepseek 各一份）。这更符合「单一模型入口」的既定决策，
但属于可观察行为变化，必须写清楚。

**异常映射（采原提案的推荐项）**：`model_loader` 抛自己的 `ModelLoadError`，
两个调用方各自 catch 后映射为自己的 `LLMError`（`assistant.LLMError` / `interfaces.LLMError`），
保持 `utils/` 零 `src` 内依赖。

**注意**：别把 `model_loader` 加进 `src/utils/__init__.py` 的 eager import 链
（`tests/test_no_langchain_import.py` 会 `from src.utils import ...`，没必要扩大该测试的 import 面）。

**附带决定**：`assistant.get_model` 目前无任何测试 / 外部引用，**建议直接删**；
若想保守，保留为一行 delegate。

**验收**

```bash
python -m pytest tests -q
python -m pytest tests/test_no_langchain_import.py -q
git grep -n "from init import model" src     # 只剩 src/utils/model_loader.py 一处
```

---

### 阶段 6　P1-1：`capabilities` 用 `getattr` 字符串绕过接口契约

**问题**：[src/core/capabilities.py:38-47](./src/core/capabilities.py) 的
`_usage_text(monitor, method: str, name: str)` 内部 `getattr(monitor, method)()`。
为省几行重复，代价是**字符串调用绕过了 `MonitorSource` 的抽象方法契约**
（类型检查器与 IDE 都发现不了 `"cpu_usage"` 拼错），而这恰是 interfaces 层存在的意义。

**改法**（推荐后者）：

```python
def _usage_text(getter: Callable[[], float | None], name: str) -> str: ...

def cpu_usage_text(monitor: MonitorSource) -> str:
    return _usage_text(monitor.cpu_usage, "CPU 使用率")
```

**铁律**：成功 / 失败 / 无数据三类文案**一个字都不能变**（有测试逐字断言）；
`_pct()` 保留（`disk_usage_text` 用）。

**验收**：`tests/test_chat.py::test_agent_tools_share_text_with_fallback` 必须仍然通过
（它断言 `fb == out and fb == "CPU 使用率：45.0%"`，是本任务的护栏）；全量 60 passed。

---

### 阶段 7　P1-2：把 `Any` 换成层间接口（判定成立，**原提案清单不全**）

| 位置 | 现状 | 改为 |
|---|---|---|
| [src/core/chat.py:85](./src/core/chat.py) | `assistant_factory: Callable[[], Any] \| None` | `Callable[[], Assistant] \| None` |
| [src/core/chat.py:90](./src/core/chat.py) | `self._assistant: Any = None` | `Assistant \| None` |
| [src/core/agent.py:41](./src/core/agent.py) | `rule_engine: Any \| None = None` | `RuleEngine \| None = None` |
| [src/container.py:44](./src/container.py) | `self._executors: dict[str, Any] \| None` | `dict[str, ActionExecutor] \| None`（**原提案漏了**） |
| [src/container.py:83](./src/container.py) | `def executors(self) -> dict[str, Any]` | `dict[str, ActionExecutor]`（**原提案漏了**） |

**新建 `src/interfaces/assistant.py`**：`Assistant` 是 `Protocol`，只声明
`reply(self, message: str) -> str`；docstring 写明「langchain 实现见 `src/agent/assistant.py::OpsAssistant`；
测试可注入任意同形状假对象（见 `tests/test_chat.py` 的 `FakeAssistant`）」。
`typing.Protocol` 是标准库，不违反 interfaces 层零第三方依赖。

**顺手补齐导出**：[src/interfaces/__init__.py](./src/interfaces/__init__.py) 目前只导出 monitor/action/llm 三家，
**漏了整个 diagnostic 家族**（`DiagnosticSource` / `DiagnosticError` / `CommandSpec` / `ParamSpec` / `render_diag`）。
加 `Assistant` 时一并把 `__all__` 补全，保持「接口层统一从包根导出」的一致性。

**注意**：`core/agent.py` 直接在顶层 `from src.core.rule_engine import RuleEngine` 是有意的
（`rule_engine.py` 不 import `core/agent`，无环；同文件的 `ChatService` 已经这么写），加注解即可。

**验收**：60 passed；`tests/test_chat.py` 的 `FakeAssistant`（只有 `reply` 的本地类）不改一行仍类型正确。

---

### 阶段 8　P2-3：降级路径补测试（**性价比最高的一档，建议全部做**）

以下模块**零单测**，而它们恰好是降级逻辑最密集的地方（降级写错会静默失效）：

1. **`src/container.py`（完整装配链路从未被测过）**
   - 现状：连 `tests/test_no_langchain_import.py` 也只 `Container()` 了一下，
     **`container.ops_agent()` 与 `main.create_app()` 在测试里从未被调用**——
     整条装配（executors 构建、RuleEngine 装配、provider 解析、降级返回）没有任何断言。
   - 用例：未注册的 monitor provider → `RuntimeError` 且报错含可用列表；`llm.enabled=false` → `None`；
     llm provider 未注册 → `None` + 警告；`actions.yaml` 缺失 → `executors() == {}`；
     缺 paramiko → `diagnostics()` 为 `None`（`sys.modules` 屏蔽法）；
     用注入的 `Settings` 做一次 `create_app()` 冒烟（不联网、不 invoke）。
2. **`src/core/analysis.py`**（正常路径已被 `test_chat` 的告警链路**间接**覆盖，缺的是边界）：
   `build_alert_context` 的 `instance` 为 `unknown` / 空 → 走「实例信息缺失」分支；
   趋势有数据 vs 缺失（应补三行「暂无趋势数据」占位）；`llm=None` 时 `analyze_many` 的短路语义。
3. **`src/plugins/monitors/prometheus_monitor.py`**：`_clamp` 边界、`_summarize_values` 的
   None / 空值跳过、`active_alerts` 的 firing 优先排序、`disk_usage` 降序与坏数据跳过
   （monkeypatch `_request`，保持离线）。
4. **`src/core/chat.py`（原提案漏了）**：[ChatService.reply](./src/core/chat.py) 里
   「Agent 构建成功但 `reply()` 抛异常 → 回落关键词」这条分支**无测试**
   （现有测试只覆盖工厂失败）。

新增测试沿用 [tests/conftest.py](./tests/conftest.py) 的 `FakeMonitor` / `FakeLLM` / `FakeDiagnostics`。

**验收**：新增用例全绿；`python -m pytest tests -q` 全绿；测试数从 60 **上升**（不是下降）。

---

### 阶段 9　可选项（**做之前先问用户，别自作主张**）

- **P2-2 输出通道**：现在是 [src/core/rule_engine.py:184](./src/core/rule_engine.py) 与
  [src/core/agent.py:100](./src/core/agent.py) 两处 `print()`。抽 `Notifier` Protocol 属设计决策而非缺陷，
  且 `print` 内容是用户观察 `auto_execute=true` 效果的主要窗口。
  **更便宜的第一步**：`RuleEngine.print_results` 只有一个调用方（`core/agent.py`），
  把它移进 `OpsAgent`（那里已有 `_print_ai_advice`），让 `RuleEngine` 变回纯决策、输出只剩一个口子；
  等真出现钉钉/企微需求再抽抽象。
- **P2-1 目录级配置加载重复**：`actions/catalog.py` 与 `diagnostics/commands.py` 的外壳确实同构
  （enabled 布尔校验、commands 映射解析、interpolate 插值、文件缺失或解析失败收敛为 None 并记降级日志），
  但 `DiagnosticsConfig` 多出 `provider/target/output_limit`，只能抽「外壳」。
  **收益中等、回归风险不小**：建议只加一行「与 X 同构」的注释，不真抽。

---

## 不要动的东西（复审判定：现状是对的）

- **`plugins/common/` 共享内核**（endpoint / ssh / audit / cmddef）：本次重构做得最好的部分；
  `cmddef` 的「模板占位集合 == params 声明集合」这类 fail-loud 校验是刻意的，**别为了「灵活」放松它**。
- **`container.py` 的惰性装配 + 缓存**：设计正确，只缺测试（阶段 8）。
- **`capabilities.py` 作为能力单一实现的定位**：别把「状态 / 帮助 / DEFAULT_REPLY」这类
  fallback-only 的东西塞进来（REFACTOR_PROMPT 既定决策 #6）。
- **`ChatService.Rule` 那个小规则框架**：有点自造感，但有测试锁着且换来了可读性，不值得改。
- **对外 HTTP 契约**（`/health` `/chat` `/alert` `/alerts/*` 的请求响应）：一个字都别改。
- **`config/*.yaml` 的现有格式与示例内容**：本轮不需要动配置。
- **安全不变量相关的一切**（`shlex.quote`、redact、审计、恒不 sudo、凭据不进模型）：只许加固，不许放松。

---

## 全局验证（每个阶段收尾都跑）

```bash
python -m pytest tests -q                        # 基线 60 passed；阶段 3 / 8 后数量有预期变化
python -m pytest tests/test_no_langchain_import.py -q
git grep -n "src\.agent" src/core                # 阶段 4 后应为空
git grep -n "from init import model" src         # 阶段 5 后只剩 src/utils/model_loader.py
git status --short                               # 每个 commit 前必须干净
```

实机链路由用户自行执行（需 `.env` 有 `DEEPSEEK_API_KEY` + `SSH_PASSWORD_192_168_20_128`、
装好 paramiko、目标机可达）——**本计划全程不需要联网验证，不要代跑**。

---

## 提交建议（一任务一 commit，便于回滚）

| # | commit | 内容 |
|---|---|---|
| 0 | `chore(env): 记录缺失依赖与安装方式` | 阶段 0（环境修复本身不进 commit；必要时把安装说明写进 README 安装节） |
| 1 | `chore: gitignore 补齐本机配置与讲义目录` | 阶段 1.1 |
| 2 | `chore: 清理 __pycache__/.pytest_cache` | 阶段 1.2（产物本就被忽略，此 commit 通常为空，可跳过） |
| 3 | `chore: 基线快照（重构完成后、结构收尾前）` | 阶段 1.3 |
| 4 | `docs: 修正 obs-agent.txt 死链与注册表/审计表述` | 阶段 2.1–2.3 |
| 5 | `docs: 修正 interfaces/models 的动作来源描述` | 阶段 2.4–2.6 |
| 6 | `refactor(registry): 移除 actions 注册死代码` | 阶段 3.1 |
| 7 | `refactor(utils): 移除无调用方的 require_params 等` | 阶段 3.2–3.3 |
| 8 | `refactor(core): 告警分析提示词下沉 core/prompts` | 阶段 4 |
| 9 | `refactor(utils): 模型入口收敛到 utils/model_loader` | 阶段 5 |
| 10 | `refactor(core): capabilities 去掉 getattr 字符串派发` | 阶段 6 |
| 11 | `refactor(types): 用 Protocol/RuleEngine/ActionExecutor 收紧层间契约` | 阶段 7 |
| 12 | `test: 补 container/analysis/prometheus/chat 降级路径用例` | 阶段 8 |

**停下来让用户确认的时机**：完成阶段 0–4（环境 + 卫生 + 注释 + 首个结构修复）后汇报一次，
不要顺势把阶段 5–9 全做完。

---

## 附录：本计划对 `STRUCTURE_OPTIMIZE_PROMPT.md` 的修正与补强

| 原提案 | 本计划的处理 |
|---|---|
| P0-1 把三个常量整体搬到 `core/prompts.py` | **建议改为只搬 `ALERT_ANALYSIS_SYSTEM`**（谁用谁拥有），避免 core 持有 Agent 话术 |
| P0-2 删 `_actions` + 改 `models.py:95` | **补**：`interfaces/action.py:4` 的同类误导；`diagnostic_class(default=...)` 在删掉 action 后也成孤例，要一并处理 |
| P0-3 抽 `model_loader`、抛 `ModelLoadError` | 采纳；**补**：用 `config_loader.ROOT_DIR` 而非自算路径（两个旧副本层级不同）；两处缓存合一属行为变更，要写进 commit |
| P1-2 收紧 `Any` | 采纳；**补**：`container.py` 的 `dict[str, Any]`（两处）、`interfaces/__init__.py` 漏导出 diagnostic 家族 |
| P2-3 测试盲区 | 采纳并**提级**（排在 P2-1 / P2-2 之前）；**补**：`container.ops_agent()` 与 `create_app()` 从未被调用；`ChatService.reply` 的回落分支无测试；`analysis.py` 并非「零覆盖」（正常路径被间接覆盖），缺的是边界 |
| （原提案未涉及） | **新增一整章：仓库卫生** —— `.gitignore` 漏 `.claude/settings.local.json`、`参考/` 55.3 MB、幽灵 `.pyc`、`git init` 与基线提交 |
| （原提案未涉及） | **新增一整章：注释 / 文档整理** —— 判定标准 A/B/C + 7 处具体漂移（含 README 的 `obs-agent.txt` 死链） |
| （原提案未涉及） | **新增：其它死代码** —— `require_params`（及其测试）、`interpolate_mapping`、`get_app_logger`；`OpsAgentError` 建议保留并标注预留 |
| （原提案未涉及） | **新增：环境阻塞项** —— 缺 `typing_extensions` / `Pygments`，导致「60 passed」不可复现，必须最先修 |
| 原提案「P0-1 + P0-2 + P0-3 做完就停下」 | 保持，但停止点前移到**阶段 4 之后**（卫生与注释已提前完成） |
