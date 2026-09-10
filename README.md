# 智能运维 Agent（ops-agent）

基于 **Python + FastAPI + LangChain Agent** 的智能运维 Agent。已实现能力：

- **AI 对话**：DeepSeek Agent 自主调用监控工具（CPU / 内存 / 磁盘 / 活动告警）回答问题；
- **AI 只读主机诊断**：监控指标解释不了的怪现象，Agent 可调用 config/diagnostics.yaml 白名单只读命令
  （free / df / ps / systemctl / journalctl）到目标主机排查；命令模板与参数校验全部配置化，凭据只进 .env；
- **关键词兜底**：无 LLM 环境照常答复 CPU/内存/磁盘/告警/状态/帮助；
- **Prometheus 指标查询**：即时值 + 按实例历史趋势；
- **告警状态主动查询**：pending / firing（`/alerts/active`）；
- **Alertmanager 告警接收**（`/alert` webhook）+ 最近记录（`/alerts/recent`）；
- **告警自动处置**：规则库（rules.yaml）引用 **config/actions.yaml 写命令目录** → 通用执行器按目录 key
  执行命令（模板 + 参数校验 + sudo 声明 + 审计日志）；「写命令」与「只读诊断」共用 SSH/审计内核，边界分离；
- **告警 AI 分析**：无自动处理规则的告警 → LLM 给出根因假设与处理建议。

代码已按既定目标架构**就地重构**（`ops_bot/` 已删除；该任务的目标文档 `obs-agent.txt` 已不在仓库内，
决策留痕见 [REFACTOR_PROMPT.md](./REFACTOR_PROMPT.md) 的「既定决策记录」），落实：
模块解耦（interfaces/ 抽象 + plugins/ 实现）、可扩展性（各能力预留接口）、配置分离（config/ 不再硬编码）、
可测试性（core 只依赖抽象接口，单测注入假组件）、安全性（密钥走 .env / 环境变量，动作审计）。

---

## 项目结构

```
Assitant/
├── README.md / requirements.txt / .env.example
├── init.py                    # 用户模板（只读）：init_chat_model 单一模型入口
├── config/
│   ├── settings.yaml          # 全局配置（支持 ${ENV} / ${ENV:-default} 插值）
│   ├── rules.yaml             # 自动处置规则库（引用 actions.yaml 写命令；密码以环境变量占位）
│   ├── actions.yaml           # 写命令目录：规则允许执行的修复命令白名单
│   └── diagnostics.yaml       # 只读诊断命令白名单 + 目标主机（Agent 排查用）
├── scripts/                   # 压测脚本 load_gen.sh / load_mem.sh（保留）
├── 参考/                      # 尚硅谷 LangChain 1.x 讲义（10 章，保留）
├── src/
│   ├── main.py                # create_app()：装配 container → include api routers
│   ├── api/                   # 薄 HTTP 层（依赖注入取 OpsAgent，无业务逻辑）
│   │   ├── chat.py            #   POST /chat
│   │   ├── alert.py           #   POST /alert、GET /alerts/recent、GET /alerts/active
│   │   └── health.py          #   GET /health
│   ├── core/                  # 业务核心（只依赖 interfaces 抽象与注入组件）
│   │   ├── agent.py           #   OpsAgent 主控制器（chat/process_alert/recent/active_alerts）
│   │   ├── chat.py            #   ChatService：Agent-first + 关键词兜底（转发 capabilities）
│   │   ├── capabilities.py    #   ★ 监控能力单一实现：查询+格式化纯函数（无 langchain，工具与兜底共用）
│   │   ├── rule_engine.py     #   规则引擎（加载 + 匹配决策，只依赖 ActionExecutor 接口）
│   │   ├── prompts.py         #   告警分析 system 提示词（纯常量；analysis.py 用）
│   │   ├── analysis.py        #   告警 LLM 分析（上下文组装 + LLM 编排）
│   │   └── exceptions.py      #   业务异常基类
│   ├── interfaces/            # 抽象接口层（core 只依赖这里）
│   │   ├── monitor.py         #   MonitorSource + MonitorError
│   │   ├── action.py          #   ActionExecutor + ActionError
│   │   ├── llm_client.py      #   LLMClient + LLMError
│   │   └── diagnostic.py      #   DiagnosticSource + DiagnosticError（只读诊断契约）
│   ├── plugins/               # 具体插件实现（实现 interfaces）+ 共享内核
│   │   ├── common/                            # 跨家族共享内核（读/写两条边界共用）
│   │   │   ├── endpoint.py                    #   SshEndpoint 连接抽象（认证二选一）
│   │   │   ├── ssh.py                         #   exec_command：Endpoint + 命令串 → (exit/out/err)
│   │   │   ├── audit.py                       #   record_execution：统一审计写入 ops_agent.audit
│   │   │   └── cmddef.py                      #   命令目录机制：模板+参数+校验+build_command（纯）
│   │   ├── actions/                           # WRITE 边界：写命令目录 → 通用执行器
│   │   │   ├── catalog.py                     #   config/actions.yaml 解析（写命令白名单）
│   │   │   └── executor.py                    #   CatalogCommandExecutor：按目录 key 执行命令
│   │   ├── monitors/prometheus_monitor.py      # MonitorSource：Prometheus 查询+趋势
│   │   ├── llm_clients/deepseek_client.py      # LLMClient：告警分析直连
│   │   └── diagnostics/                        # READ 边界：只读诊断（Agent 工具用）
│   │       ├── commands.py                     #   diagnostics.yaml 解析（config 级；命令级复用 common/cmddef）
│   │       └── ssh_diagnostics.py              #   经 SSH 执行白名单只读命令（注册 diagnostic "ssh"）
│   ├── agent/                 # ★ 保留的 Agent 编排层（create_agent + @tool + 提示词）
│   │   ├── prompts.py         #   系统提示词 / Agent 名（纯常量，只有 agent/ 用）
│   │   ├── tools.py           #   build_tools(monitor)/build_diag_tools：@tool 工厂（监控工具薄接线转发 core/capabilities）
│   │   └── assistant.py       #   OpsAssistant：经 utils/model_loader 取模型，构建 Agent
│   ├── registry.py            # 插件注册表（monitors / llms / diagnostics；写动作走 config/actions.yaml 目录，不入注册表）
│   ├── container.py           # DI 容器：settings → 插件实例 → OpsAgent（全部惰性/缓存）
│   ├── storage/               # 数据模型与状态存储
│   │   ├── models.py          #   告警/规则 pydantic 模型（Alert / AlertmanagerPayload / …）
│   │   ├── memory.py          #   AlertStore：最近告警内存缓冲
│   │   └── db.py              #   SQLite 持久化（搁置，仅 TODO 预留）
│   └── utils/                 # 基础设施工具
│       ├── config_loader.py   #   settings.yaml 解析 + 环境插值 + 默认兜底（ROOT_DIR）
│       ├── logger.py          #   日志级别控制 + 审计 logger（ops_agent.audit）
│       ├── security.py        #   敏感打码 redact_sensitive
│       └── model_loader.py    #   ★ 模型单一入口：惰性复用 init.py + 进程内单例缓存
└── tests/                     # 离线单测（conftest 提供 FakeMonitor / FakeLLM / FakeDiagnostics）
```

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

### config/settings.yaml（可变参数，不再硬编码）

| 键 | 说明 |
|---|---|
| `app.name/version` | 应用标识 / 版本 |
| `server.host/port` | uvicorn 监听（默认 127.0.0.1:8000） |
| `prometheus.base_url/timeout` | 数据源地址与超时；base_url 支持 `${PROMETHEUS_URL:-http://localhost:9090}` |
| `monitor.provider` | 监控源插件名（registry：prometheus） |
| `llm.provider/enabled` | LLM 插件与开关（false 则告警 AI 分析跳过） |
| `agent.name/debug` | Agent 归因名 / create_agent debug（预留） |
| `alert.recent_limit/trend_minutes/trend_step` | 最近告警缓冲上限 / AI 分析趋势窗口 |
| `logging.level/audit` | 日志级别 / 动作审计开关 |

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
- 改完本文件调用 `reload_rules()` 即可热更新（RuleEngine 支持）。

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
  [REFACTOR_PROMPT.md](./REFACTOR_PROMPT.md)「路线图 · 多主机诊断」）。

### .env / 环境变量

复制 `.env.example` 为 `.env` 并填写真实值（`.env` **勿提交版本库**）：

| 变量 | 用途 |
|---|---|
| `DEEPSEEK_API_KEY` / `DEEPSEEK_BASE_URL` | DeepSeek 模型（init.py 读取） |
| `PROMETHEUS_URL` | Prometheus 地址（缺省 localhost:9090） |
| `SSH_PASSWORD_192_168_20_128` | 目标主机 root 密码：规则中 SSH 动作与 diagnostics.yaml 只读诊断共用 |

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

### 对外路由（请求/响应契约不变）

| 方法/路径 | 说明 |
|---|---|
| `GET /health` | 健康检查 → `{"status": "ok"}` |
| `POST /chat` | 请求 `{"user","message"}` → `{"reply"}` |
| `POST /alert` | Alertmanager webhook 推送 → `AlertAck` |
| `GET /alerts/recent?limit=` | 最近被动接收的告警记录 |
| `GET /alerts/active` | 实时活动告警（监控源故障转 502） |

---

## 测试

```bash
python -m pytest tests -q
```

全部离线：不发起任何真实 DeepSeek / Prometheus / SSH 调用。`tests/conftest.py` 提供
`FakeMonitor` / `FakeLLM` / `FakeDiagnostics`，覆盖：规则引擎（假执行器 ok/failed/skip/unmatched）、
只读诊断（白名单解析、参数正则/转义安全边界、SSH stub 下的截断与审计、渲染）、
配置环境插值、敏感打码、ChatService 关键词兜底、OpsAgent 告警全链路，以及
M2 能力**同源回归**（同一 FakeMonitor 下 Agent 工具输出 == 关键词兜底输出，锁死"单一实现"）；
另有 `test_no_langchain_import.py`（在屏蔽 langchain/dotenv 的子进程中验证核心可 import、
聊天照常回落）。

---

## 安全说明

- **密钥只进 .env / 环境变量**：代码、`config/`、文档均不落明文。规则密码以
  `${SSH_PASSWORD_192_168_20_128}` 占位，加载时插值；变量未设置时插值为空，不会误连。
- **动作审计**：写命令执行器（`CatalogCommandExecutor`）经 `plugins/common/audit.py` 统一向
  `ops_agent.audit` logger 写结构化记录
  `{action, host, params(打码), started_at, result/error}`，**永不包含密码**。
- **敏感打码**：日志/处置记录统一过 `utils.security.redact_sensitive`（password/token/secret/api_key → ***）。
- **动作来源可信**：动作命令只来自 config/actions.yaml 写命令目录（管理员声明，含 sudo 与参数 schema），
  rules.yaml 只引用目录 key 并提供语义参数；多余/越权参数与非法值在进入 SSH 前被服务端拒绝，
  命令不取自告警内容。
- **只读诊断白名单**：聊天 Agent 的主机排查只能执行 config/diagnostics.yaml 声明的只读命令；
  模型只传语义参数（服务名 / 行数等），服务端做正则全匹配 / 取值收敛 / shell 转义后再拼命令，
  恒不 sudo、凭据不进模型上下文；每次真实执行写 `ops_agent.audit`（与动作同通道，记录命令/主机/参数，
  不含密码）。
- **SSH host key**：演示环境使用 `AutoAddPolicy`；生产建议核对 known_hosts 后改用 RejectPolicy。
- **提交前**：`.env` 与含密钥文件不要进版本库。仓库尚未 `git init`，建议：

  ```bash
  git init
  git add .
  # 确认 .gitignore 已忽略 .env / __pycache__ / .venv 等再 commit
  ```

---

## 扩展指南

- **新增监控源**：在 `plugins/monitors/` 写 `MonitorSource` 实现 → `__init__.py` 注册
  → settings 改 `monitor.provider`。
- **新增可执行修复命令**：在 config/actions.yaml 的 `commands` 加一条（安全模板 + 参数
  `pattern`/`min`/`max` + `sudo` 声明）→ rules.yaml 按 key 引用即可，无需改代码（改目录需重启进程生效）。
- **新增 LLM 供应商**：在 `plugins/llm_clients/` 写 `LLMClient` 实现 → `__init__.py` 注册。
- **新增监控能力**：先在 `core/capabilities.py` 加一个 `xxx_text(monitor)` 纯函数（措辞规范唯一出处），
  再到 `agent/tools.py` `build_tools()` 与 `core/chat.py` 关键词规则各转发一行——Agent 工具与无 LLM
  兜底自动同款文案。高危「执行动作工具」位已预留注释（默认不注册，启用前建议配合 `interrupt_before` 人工审批）。
- **给 Agent 加只读诊断命令**：在 `config/diagnostics.yaml` 的 `commands` 加一条（安全模板 +
  参数 `pattern`/`min`/`max`）→ 重启服务生效，工具按白名单自动生成，无需改代码。
- **聊天兜底加"状态/帮助"类静态回复**：`core/chat.py` 的 `_rules` 列表追加 `Rule(keyword, reply=...)`。

## 搁置项（本期不实现，仅预留）

notifiers（钉钉/企微通知）、storage/db.py 的 SQLite 持久化（仅 TODO）、docker/、
disk_clean 独立动作、插件目录自动扫描发现、logging.conf（用 settings 控制日志级别）。
对应占位注释与扩展位已留在相关文件中，后续按需实现即可。
