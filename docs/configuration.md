# AIOps-Assistant YAML 配置指南

本文覆盖七个运行时 YAML 文件。容器使用 `.runtime/config/`，首次从
`deploy/templates/assistant/` 复制，升级保留已有值。下文 `config/`
是相对于应用工作目录的路径，在容器内为 `/app/config/`。部署入口见 [deployment.md](deployment.md)。

配置文件包括：

页面允许修改的配置子集及保存流程见 [console.md](console.md)；以下仍是磁盘文件的完整
字段参考。配置保存产生的 YAML 备份与 SQLite 数据备份不同，后者见
[database.md](database.md)。

| 文件 | 作用 |
|---|---|
| [`settings.yaml`](#二settingsyaml) | 应用、服务、存储、日志、鉴权、巡检调度等全局配置 |
| [`hosts.yaml`](#三hostsyaml) | 稳定主机 ID、Prometheus 目标、SSH 凭据引用和逐主机能力授权 |
| [`metrics.yaml`](#四metricsyaml) | 统一 Prometheus 指标目录的启用范围、阈值、实例和 selector |
| [`rules.yaml`](#五rulesyaml) | Alertmanager 告警到写命令的规则映射 |
| [`actions.yaml`](#六actionsyaml) | 规则自动处置可执行的写命令白名单 |
| [`agent_actions.yaml`](#七agent_actionsyaml) | 聊天 Agent 可申请执行的写命令白名单，强制人工审批 |
| [`diagnostics.yaml`](#八diagnosticsyaml) | 聊天 Agent 的 SSH 只读诊断命令白名单 |

> `.github/workflows/ci.yml` 是 CI 配置，不属于运行时配置。

## 一、通用规则

### 1. 环境变量插值

YAML 字符串支持：

```yaml
value: ${VAR}
value: ${VAR:-default}
```

- `${VAR}`：读取环境变量；变量缺失时替换为空字符串并记录 WARNING。
- `${VAR:-default}`：变量存在时取变量值，否则取 default。
- Compose 的根 `.env` 提供可选模型参数与发布端口；身份由初始化服务生成。
  初始化将凭据保存至 `.runtime/compose.env`，Assistant 入口导入其派生 JSON 后启动。
  新模板的 `OPS_LLM_ENABLED` / `OPS_AGENT_ENABLED` 随密钥、地址和模型名是否全部存在赋值；管理页保存固定开关后以配置为准。
- 密钥不要直接写入 YAML；使用环境变量占位，并给字符串字段加引号，SSH 密钥通过管理员“SSH 凭据”保存。
  引号内插值会保留环境值中的引号、反斜线、换行和 Unicode，不需要手工再做 YAML 转义。
  未加引号字段仍按 YAML 标量解析，用于数值、布尔值等；块标量沿用原文本插值，不适合包含多行环境密钥的场景。
  YAML 语法错误只报告行列位置，不在解析诊断中显示插值后的原文；请根据位置检查配置文件。
  `settings.yaml` 的字段类型错误报告字段路径和类型要求，不回显字段值。

### 2. 严格校验

- `settings.yaml` 与 `metrics.yaml` 严格拒绝未知字段；命令目录还会严格检查模板占位符、
  参数声明和审批段字段。可选能力配置失败时会记录错误并关闭对应能力。
- `settings.yaml`、`metrics.yaml` 的布尔字段可识别明确的 `true/false`、`yes/no`、`on/off`、`1/0` 文本。
  主机 `enabled`、命令项 `enabled/sudo` 和参数 `required` 要使用实际 YAML 布尔值；不能用字符串 `"false"` 代替。
- 类型不合法、阈值顺序错误、未知指标等会拒绝启动或在启动时关闭对应能力。
- 管理页面可编辑字段保存后热生效；手工修改 YAML 需管理员显式调用
  `POST /admin/config/apply`。Compose 发布端口、SQLite/日志路径和部署环境密钥仍需重建。

### 3. 配置文件关系

```text
settings.yaml
  └── metrics.file ──> metrics.yaml ──> Prometheus 指标
  └── hosts.file ──> hosts.yaml ──> 监控目标 + SSH 目标 + 逐主机授权
      ├── rules.yaml ──> actions.yaml ──> 规则自动处置写命令
      ├── agent_actions.yaml ──> Agent 写命令 + 人工审批
      └── diagnostics.yaml ──> Agent SSH 只读诊断
```

`metrics.yaml` 不控制 `rules.yaml` 或 SSH 诊断。  
`rules.yaml` 不直接查 Prometheus 指标，只按 Alertmanager 的 `alertname` 匹配。

### 4. 配置文件路径

默认从容器 /app/config/ 读取，宿主机挂载来源为运行目录 config/。以下环境变量可替换容器加载路径：

| 环境变量 | 默认文件 |
|---|---|
| `OPS_SETTINGS_FILE` | `config/settings.yaml` |
| `OPS_HOSTS_FILE` | `config/hosts.yaml`（仅在 `hosts.file` 为空时读取） |
| `OPS_RULES_FILE` | `config/rules.yaml` |
| `OPS_ACTIONS_FILE` | `config/actions.yaml` |
| `OPS_AGENT_ACTIONS_FILE` | `config/agent_actions.yaml` |
| `OPS_DIAGNOSTICS_FILE` | `config/diagnostics.yaml` |

`metrics.yaml` 没有单独的路径环境变量；它由 `settings.yaml` 的 `metrics.file` 指定。
`hosts.yaml` 通常由 `hosts.file` 指定；该值为空时才读取 `OPS_HOSTS_FILE`，两者都为空则拒绝构造主机清单。相对路径按容器应用根目录 /app 解析。

### 5. 通用模型参数与来源

模型仅使用 OpenAI 兼容的 Chat Completions 接口，统一入口为 `src/utils/model_loader.py`。
新部署的密钥、地址和名称均为空，需要管理员明确配置。保存配置只做校验；管理员点击
“测试已保存的模型连接”才发出请求。完整 Agent 需要标准工具调用能力。

| 环境变量 | 页面字段 | 默认值 |
|---|---|---|
| `MODEL_API_KEY` | `api_key` | 空 |
| `MODEL_BASE_URL` | `base_url` | 空，填写接口根地址，不含 /chat/completions |
| `MODEL_NAME` | `model` | 空 |
| `MODEL_TIMEOUT` | `timeout` | 30 秒，须大于 0 且不超过 300 |
| `MODEL_MAX_RETRIES` | `max_retries` | 2，范围 0~10 |
| `MODEL_MAX_TOKENS` | `max_tokens` | 2048，范围 1~131072 |
| `MODEL_TOKEN_LIMIT_PARAM` | `token_limit_param` | max_tokens，也可选 max_completion_tokens |

页面模型配置整组优先于环境配置，不逐字段混合；明确清除页面密钥后也不回退环境密钥。
配置版本变化后新请求使用新实例，旧请求继续使用旧实例。未提供供应商推理参数或任意请求参数入口。
超时和重试约束单次 SDK 请求，不等同整轮 Agent 时限。

根 `.env` 是 Compose 的可选输入；首次及重新初始化时，非空模型值持久化到
`.runtime/compose.env`，再单向派生至 `.runtime/secrets/assistant.json`。
已有持久模型值不会被空环境值清除。要撤销环境密钥，删除其所有输入来源和持久字段后重建。
页面凭据位于 `.runtime/assistant-data/admin-credentials/`，不进入 YAML 或普通配置备份。
详见 [部署与迁移](deployment.md)。

## 二、settings.yaml

下列表格的“默认值”指 deploy/templates/assistant/settings.yaml 模板及其未覆盖的程序默认值；环境变量占位符
按启动进程的环境求值。启动时读取该 YAML，未配置的字段使用程序内置默认值。
Compose 首次部署使用独立模板：强制身份校验、`local-node` 指向 `node-exporter:9100`、
没有 SSH 授权和处置规则，命令目录关闭；不提供模型密钥时 AI 关闭。模板不会改变旧部署设置。

### app

| 字段 | 类型 | 默认值 | 说明 |
|---|---:|---:|---|
| `app.name` | string | `AIOps-Assistant` | FastAPI 标题和日志应用名 |

应用版本不在 YAML 中配置，由程序内置版本号统一提供。

容器入口固定单 worker、监听 0.0.0.0:8000，发布端口由 Compose 控制。旧字段通过显式 migrate-config 迁移移除。

### prometheus

| 字段 | 类型 | 默认值 | 说明 |
|---|---:|---:|---|
| `prometheus.base_url` | string | `http://prometheus:9090` | Prometheus 地址，模板读取 PROMETHEUS_URL |
| `prometheus.timeout` | int | `5` | HTTP 查询超时秒数 |
| `prometheus.target_mode` | string | `managed` | `external` 仅登记已发现目标；`managed` 使用文件发现 |
| `prometheus.file_sd_path` | string | `/app/prometheus-targets/assistant.yml` | `managed` 模式下的 Prometheus 目标文件路径 |

所有指标即时查询和范围查询共用该超时。

### monitor

| 字段 | 类型 | 默认值 | 说明 |
|---|---:|---:|---|
| `monitor.provider` | string | `prometheus` | 监控源插件名；当前注册的是 `prometheus` |

### llm

| 字段 | 类型 | 默认值 | 说明 |
|---|---:|---:|---|
| `llm.provider` | string | `openai_compatible` | OpenAI 兼容 Chat Completions 插件名 |
| `llm.enabled` | bool | 随部署模型密钥 | `false` 时跳过告警 AI 分析；不控制聊天 Agent |

聊天 Agent 的总开关是 `agent.enabled`。

### agent

| 字段 | 类型 | 默认值 | 说明 |
|---|---:|---:|---|
| `agent.enabled` | bool | 随部署模型密钥 | `false` 时聊天只走关键词兜底，不构建 LLM Agent |
| `agent.name` | string | `ops_assistant` | Agent 名称，用于 trace 和日志归因 |
| `agent.debug` | bool | `false` | LangChain Agent 调试输出 |
| `agent.recursion_limit` | int | `25` | 单轮 Agent 图步骤上限，防止工具调用无限循环 |

### chat

| 字段 | 类型 | 默认值 | 说明 |
|---|---:|---:|---|
| `chat.max_message_chars` | int | `8000` | `/chat` 单条消息字符上限，超过返回 413 |

### metrics

| 字段 | 类型 | 默认值 | 说明 |
|---|---:|---:|---|
| `metrics.file` | string | `config/metrics.yaml` | 统一指标配置文件路径；空表示使用代码内默认配置 |

路径非空但文件不存在、解析失败或含未知指标时会拒绝启动。

### hosts

| 字段 | 类型 | 默认值 | 说明 |
|---|---:|---:|---|
| `hosts.file` | string | `config/hosts.yaml` | 静态主机清单；空时须显式设置 OPS_HOSTS_FILE |

### alert

| 字段 | 类型 | 默认值 | 说明 |
|---|---:|---:|---|
| `alert.recent_limit` | int | `100` | 内存中最近告警条数，重启即清空 |
| `alert.trend_minutes` | int | `60` | 告警 AI 分析趋势窗口（分钟） |
|
### inspection

| 字段 | 类型 | 默认值 | 说明 |
|---|---:|---:|---|
| `inspection.enabled` | bool | `true` | 是否启动周期巡检；只支持单 worker |
| `inspection.interval_seconds` | int | `300` | 全局巡检间隔，必须为 30~86400 秒 |
| `inspection.run_on_start` | bool | `true` | 服务启动后是否立即巡检一次 |
| `inspection.ttl_hours` | int | `168` | 异常巡检归档保留小时数 |
| `inspection.max_rows` | int | `2500` | 异常巡检归档最大行数 |
| `inspection.max_hosts_per_run` | int | `50` | 单轮最多巡检的主机数，超过时按轮次轮转 |
| `inspection.max_queries_per_run` | int | `500` | 单轮 Prometheus 查询预算 |
| `inspection.round_timeout_seconds` | int | `120` | 整轮时限；只在主机边界停止，下一轮续接 |

具体检查项、阈值和 surface 不在本段配置，统一在 `metrics.yaml` 中设置。
查询预算按一个完整主机的“Exporter 状态 + 启用巡检指标数”折算；预算不足一台时仍检查一台，
因此不是逐个查询的硬上限。轮次时限也只在主机之间检查，不中断当前主机查询。

### storage

| 字段 | 类型 | 默认值 | 说明 |
|---|---:|---:|---|
| `storage.enabled` | bool | `true` | SQLite 持久化总开关 |
| `storage.db_path` | string | `data/aiops_assistant.db` | 数据库路径；升级时若新路径不存在且旧 `data/ops_agent.db` 存在，会自动继续使用旧库 |
| `storage.session_ttl_hours` | int | `72` | 会话过期时间 |
| `storage.max_sessions` | int | `200` | 最大会话数，超出淘汰最旧会话 |
| `storage.message_window` | int | `20` | 每轮发送给模型的历史消息上限 |
| `storage.history_ttl_hours` | int | `168` | 告警/处置/AI 建议归档保留时间 |
| `storage.max_history_rows` | int | `5000` | 各归档与幂等表的容量上限；待审批消息引用的决策暂不淘汰，见数据库说明 |
| `storage.purge_interval_seconds` | int | `60` | 过期清理节流间隔 |

### logging

| 字段 | 类型 | 默认值 | 说明 |
|---|---:|---:|---|
| `logging.level` | string | `INFO` | `DEBUG/INFO/WARNING/ERROR` |
| `logging.audit` | bool | `true` | `false` 时审计 logger 提到 WARNING，只记录失败 |
| `logging.audit_file` | string | `logs/audit.log` | 审计轮转文件；空表示只进 console |
| `logging.audit_max_bytes` | int | `5242880` | 审计单文件字节上限 |
| `logging.audit_backup_count` | int | `3` | 审计轮转文件保留数量 |
| `logging.inspection_file` | string | `logs/inspection.log` | 全量巡检日志，按指标分组输出多行人类可读文本；空表示不写日志 |
| `logging.inspection_max_bytes` | int | `5242880` | 巡检单文件字节上限 |
| `logging.inspection_backup_count` | int | `3` | 巡检轮转文件保留数量 |

巡检日志是多行分组格式，例如：

```text
[巡检] level=warning checked_at=2026-09-16T00:00:00+00:00
[CPU]
  cpu.usage [warning] instance=web-01:9100: 85.2% (warning>=80.0%, critical>=90.0%)
[Memory]
  memory.usage [normal] instance=web-01:9100: 60.1% (warning>=85.0%, critical>=95.0%)
[汇总]
  overall=warning metrics=17 anomalies=1
```

`logging.audit_file` 仍保持一行一个 JSON 对象，便于机器解析；巡检日志和审计日志的格式职责不同。

### auth

| 字段 | 类型 | 默认值 | 说明 |
|---|---:|---:|---|
|| `auth.host_access` | JSON object string | `{}` | 身份名到已登记主机 ID 数组；普通用户无显式授权时拒绝，管理员可访问全部主机 |
| `auth.tokens` | JSON string | 初始化生成 | 密钥到身份的 JSON 映射，建议用 `${OPS_API_TOKENS}` |
| `auth.approval_mode` | string | `approvers_only` | `owner` 或 `approvers_only` |
| `auth.approver_identities` | JSON array string | `["admin"]` | `approvers_only` 模式下允许审批的身份 |
| `auth.admin_identities` | JSON array string | `["admin"]` | 可访问系统管理 API 的最高权限身份 |
| `auth.alert_identity` | string | `alertmanager` | 允许调用 `POST /alert` 的身份 |

示例：

```yaml
auth:
  tokens: '${OPS_API_TOKENS:-}'
  approval_mode: approvers_only
  approver_identities: '${OPS_APPROVER_IDENTITIES:-["admin"]}'
  admin_identities: '${OPS_ADMIN_IDENTITIES:-["admin"]}'
  alert_identity: alertmanager
```

管理员身份必须存在于 `auth.tokens` 的身份值中。它与审批人互不继承；管理 API
始终要求管理员 Token，业务 API 始终校验身份。

系统管理页面不是 YAML 编辑器：它只提交 `settings` 的安全/功能字段、规则、命令目录
、用户主机授权和主机 `allow` 白名单。服务端保留 Token、SSH/Prometheus 连接信息等非页面字段，并
复用现有解析器验证整个候选配置。保存前比较配置文件（含已指定的 `metrics.yaml`，
自管模式还含目标文件）的磁盘 revision；不一致返回 409，
字段或跨文件引用错误返回 422。仅有变化的文件会先备份到
`data/config-backups/<UTC 时间>/`，再逐文件原子替换。写入异常会尝试回滚已替换文件，
但跨文件批次和断电不是原子事务；需人工检查备份。校验和写盘成功后立即切换
运行快照；页面的“待应用”表示磁盘 revision 与当前运行 revision 不同。详见
[console.md](console.md) 与 [api.md](api.md)。

## 三、hosts.yaml

`hosts.yaml` 是多主机模式中所有监控、诊断和执行目标的唯一来源。API 和模型只引用
`host_id`，不会接收任意地址或凭据。

```yaml
version: 1
credentials: {} # 敏感材料保存在管理员凭据存储，示例需先创建 ops-a
hosts:
  - id: node-a
    name: 节点 A
    enabled: true
    prometheus: {job: node, instance: "node-a:9100"}
    ssh:
      host: node-a.internal
      port: 22
      credential_ref: ops-a
      connect_timeout: 8
    allow:
      diagnostics: [memory, disk]
      rule_actions: []
      agent_actions: []
```

约束：

- `version` 当前必须为 `1`；`hosts` 必须是非空数组，且至少一台启用。
- `id` 必须匹配 `^[a-z][a-z0-9-]{0,62}$`。
- 启用主机的 Prometheus `instance` 必须唯一；`job` 和 `instance` 都不能为空。
- `ssh` 可省略；省略时三个 `allow` 列表必须为空，该主机仍可监控但不能 SSH。
- `ssh.connect_timeout` 默认 8 秒，分别限制 TCP、banner、认证、通道打开和命令输出等待，
  不代表整次 SSH 调用的总时限。
- `credential_ref` 必须引用页面管理的 SSH 凭据 ID（也可引用已有显式 credentials）；凭据不会返回给 API 或模型。
- SSH 密码保留解析后的原始字符串，包括首尾空白和 Unicode 字符；空字符串或 null 表示未配置。
- `allow.diagnostics`、`allow.rule_actions`、`allow.agent_actions` 分别引用三个命令目录中的 key。
- 示例默认不授权任何写动作。启用规则自动处置还必须同时设置规则 `auto_execute: true`。
- 多主机模式不会使用 `diagnostics.yaml`、`agent_actions.yaml` 或规则参数中的默认地址改向执行。

手工修改后须由管理员显式应用。页面凭据支持多份，同一凭据可供多台已登记主机引用；
用户通过主机访问授权使用对应凭据，不能自行传入连接参数。
管理员向导当前只支持新增主机，不能从页面删除、停用或迁移现有 `host_id`。
手改清单前须核对规则与历史引用；不能把删除一行 YAML 当作已支持的主机清理流程。

## 四、metrics.yaml

### 顶层字段

| 字段 | 类型 | 默认值 | 说明 |
|---|---:|---:|---|
| `version` | int | `1` | 当前只支持 1 |
| `instance` | string | `${PROMETHEUS_INSTANCE:-localhost:9100}` | 默认 Prometheus instance，需与 node_exporter 的 instance 标签一致 |
| `max_alert_analysis_metrics` | int | `8` | 告警 AI 最多查询的趋势指标数量，范围 1~50 |
| `metrics` | map | 无 | 指标 ID 到运行配置的映射，不能为空 |

### 单个 metric 字段

```yaml
metrics:
  cpu.usage:
    enabled: true
    surfaces:
      inspection: true
      chat: true
      alert_analysis: true
    warning: 80
    critical: 90
    severity_enabled: true
    selector:
      instance: web-01:9100
```

| 字段 | 类型 | 含义 |
|---|---:|---|
| `enabled` | bool | 是否启用该指标 |
| `surfaces.inspection` | bool | 是否参与自动巡检 |
| `surfaces.chat` | bool | 是否生成聊天 Agent 工具和关键词兜底规则 |
| `surfaces.alert_analysis` | bool | 是否进入告警 AI 趋势上下文 |
| `warning` | number | warning 阈值；启用 severity 时必填 |
| `critical` | number | critical 阈值；启用 severity 时必填 |
| `severity_enabled` | bool | 是否参与巡检等级；observe 指标必须为 false |
| `selector` | map | Prometheus 标签 matcher，覆盖全局 instance 或追加过滤 |

### selector 语法

支持四种运算符：

```yaml
selector:
  instance: web-01:9100
  device: eth0
  device!=: lo
  device=~: "eth.*"
  fstype!~: "tmpfs|overlay"
```

- 地址、磁盘和网卡过滤由服务端配置控制。
- 模型不能传 PromQL、host、device 或 selector。
- 全局 `instance` 默认应用到所有 Prometheus 指标；指标级 selector 可覆盖。

### 阈值方向与等级

指标在代码目录中声明方向：

- `above`：值越大越危险。必须满足 `warning < critical`。
- `below`：值越小越危险。必须满足 `warning > critical`。
- `observe`：只记录，不参与巡检等级，不能启用 severity。

巡检等级优先级：

```text
error > critical > warning > normal
```

- 查询失败或无数据：`error`
- 达到 critical：`critical`
- 达到 warning：`warning`
- 其他：`normal`

### 内置指标目录

| 指标 ID | 分组 | 单位 | 默认分组标签 | 语义 |
|---|---|---|---|---|
| `cpu.usage` | CPU | % | instance | CPU 使用率 |
| `cpu.load1` | CPU | load/core | instance | 按 CPU 核数归一化的 1 分钟负载 |
| `cpu.iowait` | CPU | % | instance | iowait 占比 |
| `memory.usage` | Memory | % | instance | 内存使用率 |
| `memory.available` | Memory | bytes | instance | 可用内存 |
| `memory.swap_usage` | Memory | % | instance | Swap 使用率 |
| `disk.usage` | Disk | % | instance/mountpoint/device/fstype | 文件系统使用率 |
| `disk.read_bytes` | Disk | MiB/s | instance/device | 磁盘读取速率 |
| `disk.write_bytes` | Disk | MiB/s | instance/device | 磁盘写入速率 |
| `disk.io_util` | Disk | % | instance/device | 磁盘 IO 利用率 |
| `network.rx_bytes` | Network | MiB/s | instance/device | 网络接收速率 |
| `network.tx_bytes` | Network | MiB/s | instance/device | 网络发送速率 |
| `network.rx_errors` | Network | errors/s | instance/device | 接收错误率 |
| `network.tx_errors` | Network | errors/s | instance/device | 发送错误率 |
| `system.process_count` | System | processes | instance | 运行进程数 |
| `system.uptime` | System | seconds | instance | 系统运行时长 |
| `alerts.active_count` | Alerts | alerts | state | pending/firing 告警数 |

### 当前出厂行为

- `cpu.usage`、`memory.usage`、`disk.usage`：巡检 + 聊天 + 告警分析，参与等级。
- `alerts.active_count`：巡检 + 聊天，参与等级。
- 其余新指标：参与巡检，默认只观察，不进入聊天和告警分析。
- `*_bytes` 都是 rate，不是累计总量。
- 网络指标默认排除 `lo`。

## 五、rules.yaml

规则只按 Alertmanager 的 `alertname` 匹配，不查询 Prometheus 指标。

```yaml
auto_execute_enabled: false
rules:
  - id: high_cpu_restart
    enabled: true
    alertname: HighCPUUsage
    action: restart_service
    target_instance: "${PROMETHEUS_INSTANCE:-localhost:9100}"
    params:
      service: nginx
    auto_execute: false
    description: "CPU 过高时重启 nginx"
```

| 字段 | 类型 | 必填 | 含义 |
|---|---:|---:|---|
| `id` | string | 管理页保存后是 | 稳定规则 ID；旧规则首次保存时自动补齐 |
| `enabled` | bool | 否 | false 时不参与匹配，默认 true |
| `alertname` | string | 是 | 与 Alertmanager alertname 精确匹配 |
| `action` | string | 是 | 必须存在于 `actions.yaml.commands` |
| `target_instance` | string | auto_execute 时是 | 允许处理的告警实例，必须精确匹配 |
| `params` | map | 否 | `actions.yaml` 声明的业务参数；省略时为空映射 |
| `auto_execute` | bool | 否 | true 自动执行，false 只命中不执行 |
| `description` | string | 否 | 给人看的说明 |

目标 SSH 地址、凭据和超时来自告警解析出的 `hosts.yaml` 主机。即使旧规则仍含连接字段，
多主机执行也会剥离这些字段，不能覆盖登记目标。其余字段按 `actions.yaml` 对应命令的
`params` 校验。

重要行为：

- 顶层 `auto_execute_enabled=false` 会保留规则匹配和审计，但阻止所有自动执行。
- `enabled=false` 的规则不参与匹配；旧规则缺省按 true 处理。
- `auto_execute=false` 仍然是“命中规则”，会阻止该 alertname 进入 AI 建议。
- `auto_execute=true` 且缺少 `target_instance` 时，规则会在加载阶段被禁用。
- `target_instance` 必须与告警和已登记主机 instance 同时一致；主机还必须在
  `allow.rule_actions` 中授权该动作。
- 同名 alertname 可以写多条规则，按文件顺序处理。

## 六、actions.yaml

这是规则自动处置的写命令白名单。

```yaml
enabled: true
output_limit: 4000
commands:
  restart_service:
    summary: 重启 systemd 服务
    template: "systemctl restart {service}"
    sudo: true
    params:
      service:
        type: str
        pattern: "^[A-Za-z0-9_.:@-]{1,128}$"
        description: 服务名
```

| 字段 | 类型 | 必填 | 含义 |
|---|---:|---:|---|
| `enabled` | bool | 否 | 文件总开关；省略时 true，出厂模板显式 false |
| `output_limit` | int | 否 | stdout/stderr 共享输出上限，默认 4000 字符 |
| `commands` | map | 是 | 命令 key 到命令定义的映射 |
| 命令项 `enabled` | bool | 否 | false 时保留定义但不注册为可调用命令，默认 true |
| `summary` | string | 否 | 命令说明 |
| `template` | string | 是 | 管理员可信命令模板 |
| `sudo` | bool | 否 | 是否提权执行 |
| `params` | map | 否 | 模板参数声明；无参模板可省略，默认空映射 |

每个参数支持：

| 字段 | 类型 | 含义 |
|---|---:|---|
| `type` | `str/int` | 参数类型 |
| `required` | bool | 是否必填；无 default 时默认必填 |
| `default` | any | 默认值 |
| `pattern` | regex string | str 必须配置，使用 `re.fullmatch` |
| `min/max` | int | int 参数上下界，运行时会收敛 |
| `description` | string | 参数说明 |

约束：

- `{name}` 占位符集合必须与 `params` 完全一致。
- 模板只能来自本文件，规则不能传自由命令。
- sudo 由本文件声明，规则不能覆盖；`sudo` 和 `required` 使用 YAML 布尔值，字符串 `"false"` 等错误类型会拒绝加载目录。
- 命令 key 必须完整匹配 `^[a-z][a-z0-9_]*$`，不允许末尾换行。
- 管理页面保存命令目录后立即生效；手工修改后须显式应用。

## 七、agent_actions.yaml

这是聊天 Agent 的写命令白名单，和 `actions.yaml` 完全隔离。

```yaml
enabled: false
output_limit: 4000
approval:
  timeout_seconds: 900
  max_pending: 32
commands:
  restart_service:
    summary: 重启服务
    template: "systemctl restart {service}"
    sudo: true
    params:
      service: { type: str, pattern: "^[A-Za-z0-9_.:@-]{1,128}$" }
```

字段：

| 字段 | 类型 | 含义 |
|---|---:|---|
| `enabled` | bool | 是否启用 Agent 写命令 |
| `output_limit` | int | 回传 Agent 的输出字符上限 |
| `approval.timeout_seconds` | int | 待审批超时秒数 |
| `approval.max_pending` | int | 最大待审批数量 |
| `commands` | map | 写命令定义，格式同 actions.yaml |

`output_limit` 由共享 SSH 内核在 channel 读取阶段执行。stdout/stderr 共用由字符上限推导的
字节预算；超出后继续排空远端 channel，但不再把内容保留在内存。命令模板仍应主动限制
范围和运行时间。

审批段只接受 `timeout_seconds` 和 `max_pending`，没有关闭审批的开关。命令 key 不能与聊天指标工具或 SSH 诊断工具重名。

命令目录只定义命令和执行策略。会话按 `host_id` 使用已登记主机的
SSH 目标，并要求该命令出现在主机的 `allow.agent_actions`；审批冻结目标摘要、动作快照和
配置指纹，批准时再次复核，配置漂移会拒绝旧申请。

## 八、diagnostics.yaml

这是 Agent 的 SSH 只读诊断白名单，不绑定 `metrics.yaml` 指标。出厂模板关闭，以下为显式启用示例。

```yaml
enabled: true
provider: ssh
output_limit: 4000
commands:
  memory:
    summary: 查看内存明细
    template: "free -m"
    params: {}
```

字段：

| 字段 | 类型 | 含义 |
|---|---:|---|
| `enabled` | bool | 是否启用只读诊断 |
| `provider` | string | 诊断实现，当前为 `ssh` |
| `output_limit` | int | 单条命令回传上限 |
| `commands` | map | 只读命令白名单 |

`output_limit` 与 Agent 写命令具有相同的流式共享预算。即使超出预算也会继续排空 channel，
避免远端阻塞；本地不再保留多余内容。命令仍应使用 `head`、`journalctl -n` 等静态或
参数化上限。

命令参数格式与 `actions.yaml` 相同，但：

- 永远不 sudo。
- 非零退出码只作为可读结果，不触发写动作。
- 管理页面保存诊断目录后立即重建可用工具；手工修改后须显式应用。

命令目录不包含连接目标。Agent 使用会话 `host_id` 对应的
`hosts.yaml` SSH 目标，并只暴露 `allow.diagnostics` 授权的诊断命令。

## 九、常见配置流程

### 启用一个新巡检指标

```yaml
# metrics.yaml
disk.io_util:
  enabled: true
  surfaces: { inspection: true, chat: false, alert_analysis: false }
  warning: 80
  critical: 95
  severity_enabled: true
```

管理员应用磁盘配置后，后续巡检轮次会包含该指标。

### 把指标加入聊天 Agent

```yaml
cpu.iowait:
  enabled: true
  surfaces: { inspection: true, chat: true, alert_analysis: false }
  severity_enabled: false
```

管理员应用磁盘配置后，后续 Agent 会话和关键词兜底会包含该指标。

### 让指标进入告警 AI 分析

```yaml
memory.available:
  enabled: true
  surfaces: { inspection: true, chat: false, alert_analysis: true }
  severity_enabled: false
```

同时调整 `max_alert_analysis_metrics`，避免一次查询过多指标。

### 给某个指标指定实例

```yaml
metrics:
  cpu.usage:
    selector:
      instance: web-01:9100
```

## 十、常见错误

| 错误 | 原因 |
|---|---|
| `配置节 xxx 存在未知字段` | 拼写错误或使用了旧字段 |
| `metrics.yaml 含未知指标` | 指标 ID 不在代码目录 |
| `非法或保留的 host id` | 主机 ID 格式不合法 |
| `Prometheus instance 重复` | 两台启用主机配置了相同 instance，无法唯一归属告警 |
| `warning < critical` / `warning > critical` | 阈值方向不符 |
| `observe 指标不能启用 severity` | observe 指标只能记录 |
| Prometheus 400 Bad Request | 通常是 selector/PromQL 拼接错误，应检查配置中的特殊标签 |
| 修改后不生效 | 页面外修改 YAML 后须由管理员点击“应用磁盘配置”；若涉及监听/存储/日志/环境密钥则需重启 |

## 十一、字段生效与重启

| 配置 | 生效方式 |
|---|---|
| `settings.yaml` | 页面身份、用户主机授权、LLM/Agent/巡检开关热生效；监听、SQLite/日志路径与环境密钥需重启 |
| `hosts.yaml` | 管理员新增主机或修改授权后立即切换主机注册表；手改后显式应用 |
| `metrics.yaml` | 纳入 revision；手改后显式应用 |
| `rules.yaml` | 页面保存或手动应用后热生效 |
| `actions.yaml` | 页面保存或手动应用后热生效 |
| `agent_actions.yaml` | 页面保存或手动应用后热生效 |
| `diagnostics.yaml` | 页面保存或手动应用后热生效 |

热应用前若有未决审批或正在执行的业务请求会返回 409，旧运行快照与 checkpoint
保持不变；巡检只保留一个进程内调度线程，每轮读取完整运行快照。部署 Token 明文仍通过 init 的显式查看命令获取；页面管理的 Token 可创建、轮换和撤销，
成功创建/轮换时仅显示一次，普通配置快照不回显。

### Prometheus 目标模式

`prometheus.target_mode` 显式选 `external`（默认）或 `managed`；不根据 URL 猜测。
`external` 只允许选择 Prometheus `/api/v1/targets` 已发现的目标。
`managed` 必须配置 `prometheus.file_sd_path`，Compose 已将
`.runtime/prometheus-targets/assistant.yml` 映射到 Assistant 可写目录和 Prometheus
只读目录 `/etc/aiops-targets/`，并在 `deploy/prometheus/prometheus.yml` 预置 `aiops-hosts` 的
`file_sd_configs`。首次部署该能力需更新一次**本地** Prometheus 容器配置；
此后新增目标只原子更新目标文件，无需修改远程主机。新主机的诊断、规则动作和 Agent
写授权默认全为空，`up=0` 可以登记但不代表 Exporter 已恢复。

#### Compose 的 Prometheus 主配置与目标列表

下面使用默认部署目录 `.runtime/`；设置了 `AIOPS_RUNTIME_DIR` 时，目标文件和主机登记
路径应使用该目录。两个文件都在宿主机上编辑，但由 Prometheus 容器读取：

| 宿主机文件 | Prometheus 容器内文件 | 内容与生效方式 |
|---|---|---|
| `deploy/prometheus/prometheus.yml` | `/etc/prometheus/prometheus.yml` | 主配置：抓取任务、间隔、规则和目标发现方式；修改后检查并重载 |
| `.runtime/prometheus-targets/assistant.yml` | `/etc/aiops-targets/assistant.yml` | 目标列表：Exporter 地址和可选标签；保存后自动发现 |

`compose.yaml` 将 `./deploy/prometheus/` 挂载到 `/etc/prometheus/`，将部署目录下的
`prometheus-targets/` 挂载到 `/etc/aiops-targets/`。因此，虽然主配置在宿主机编辑，
其中的文件路径仍要使用**容器内路径**。

主配置的 `scrape_configs` 下已经有以下任务；这是片段，不要用它覆盖整个主配置，
也不要重复添加顶层 `scrape_configs` 或同名任务：

```yaml
scrape_configs:
  - job_name: aiops-hosts
    file_sd_configs:
      - files:
          - /etc/aiops-targets/*.yml
        refresh_interval: 30s
```

`job_name` 给抓取任务命名；`file_sd_configs` 表示从文件发现目标；`*.yml` 匹配目标目录
中的所有 `.yml` 文件。`refresh_interval: 30s` 是定期重新检查目标列表的间隔，文件变化
也会被监听；它不是指标抓取间隔。当前主配置的全局 `scrape_interval` 为 `15s`。

目标文件使用 YAML 列表，示例如下；将示例地址替换为 Prometheus 容器可访问的实际
Exporter 地址，手动追加时保留已有目标：

```yaml
- targets:
    - "192.0.2.10:9100"
    - "192.0.2.11:9100"
```

目标文件不写 `scrape_configs`、`file_sd_configs` 或 `job_name`；这些目标自动归属主配置
中的 `aiops-hosts` 任务。增加第三台服务器时，在 `targets` 下追加地址即可。

日常新增主机优先使用管理员控制台的新增主机入口，选择“输入新 Exporter 目标”。
登记会同时维护 `.runtime/config/hosts.yaml` 和 `.runtime/prometheus-targets/assistant.yml`。
单独手改目标文件只改变 Prometheus 的抓取列表，不会自动把主机登记进 Assistant；
若还要在项目页面监控它，需要从已发现目标中登记该主机。

主配置中的“首次启用”注释指：运行中的 Prometheus 尚未加载 `aiops-hosts` 任务时，
需要先加载一次新主配置。从项目目录执行，检查成功后再发送重载信号：

```bash
docker compose exec prometheus promtool check config /etc/prometheus/prometheus.yml
docker compose kill -s SIGHUP prometheus
```

这里明确发送 `SIGHUP`，Prometheus 会重新加载配置；修改目标文件无需执行这两条命令。
当前 Compose 没有开启 `--web.enable-lifecycle`，不能直接调用 `/-/reload`。
若启动时指定了 Compose 项目名或环境文件，上述命令也使用相同选项。

保存后到 `http://localhost:9090/targets` 检查 `aiops-hosts` 中的目标是否为 `UP`；若配置了
`PROMETHEUS_HOST_PORT`，使用对应端口。新增目标可能需要等待一次发现和抓取。
Prometheus 容器里的 `localhost:9100` 指向该容器自身，远程 Exporter 应填写实际地址。
更完整的连接检查见 [Prometheus 与 Exporter 排障](troubleshooting.md#prometheus-与-exporter只读分层检查)。

## 页面管理的 SSH、Token 与模型

管理员控制台的三个凭据分区使用独立持久化存储，配置值不加入 YAML。
容器内为 `/app/data/admin-credentials/`，宿主机为 `.runtime/assistant-data/admin-credentials/`；
采用与普通配置相同的锁、revision 和运行快照，成功立即生效，失败保留旧配置。
详细字段、长度和错误码见 [管理员凭据 API](api.md#管理员凭据-api)，操作见
[控制台](console.md#管理员配置操作)。环境模型与持久部署 Token 保留且页面只读；SSH/模型
页面配置优先，Token 合并身份映射。普通配置保存不能把解析到内存中的凭据写回 YAML。
显式清除模型密钥不回退环境值；设置 API Key 后按需启用 LLM 与 Agent 开关。

所有 17 项 Linux node_exporter 指标具有即时和趋势行为：数值指标缺少独立 range 表达式
时复用即时表达式，活动告警采用历史 ALERTS。磁盘按挂载点/设备/文件系统、I/O 和网络
按设备返回；无 Swap 为“不适用”。缺少采集器、标签不匹配、rate 采样不足按 no_data。
告警补零的 up 探测内嵌同一查询，不增加巡检请求数，原每主机预算仍含独立 Exporter 查询。
趋势接口与告警分析统一使用自动步长 `max(15 秒, ceil(窗口秒数 / 240))`；
未被使用的 `alert.trend_step` 已删除，趋势窗口继续使用 `alert.trend_minutes`。

## 主机告警 PromQL 编写约束

Assistant 当前按告警的 `job + instance` 与主机登记进行严格匹配。所有需要在
AI 活动告警工具、主机状态和告警计数/趋势中查询的主机告警，都必须保留这两个标签。
仅让 webhook 成功接收不代表实时查询可以匹配；本节约束同样适用于外部规则。

1. **保留主机标签。** 聚合使用 `by (job, instance)`；需要设备维度时使用
   `by (job, instance, device)` 或增加 `mountpoint` 等标签。使用 `without (...)`
   时不得移除 `job`、`instance`。嵌套聚合的每一层都要保留，外层无法恢复内层丢掉的标签。
2. **保证标签真实一致。** 告警最终的 `job`、`instance` 必须与 Prometheus 实际
   抓取标签、Assistant 主机登记完全一致，包括实例端口。不要在规则的 `labels:`、
   recording rule 或 alert relabel 中将其移除或覆盖成统一常量。记录规则也必须保留它们。
3. **检查所有分支和向量匹配。** `or` 的真实指标分支与演示/兜底分支都要提供
   `job + instance`；按主机合并使用 `or on (job, instance)`，避免相同 instance、
   不同 job 互相遮蔽。算术连接若显式使用 `on (...)`，也要保留完整主机标识；
   多设备连接额外匹配必要设备标签，避免多对多错误。
4. **不凭空制造正常值。** 主机告警使用实际采集指标；缺失指标不能直接视为零或正常。
   演示指标仅用于显式验收。阈值、单位、`rate` 时间窗和 `for` 持续时间应明确；
   当前值超过阈值并不意味着已经满足 `for` 并进入 firing。
5. **先验证最终告警标签，再发布。** 使用 `promtool check rules` 检查语法，并用
   `promtool test rules` 验证实际输出标签，覆盖真实/演示分支、pending/firing、
   相同 instance 不同 job、同一目标多分支去重和健康无告警。部署后同时检查
   Prometheus `/api/v1/alerts` 的标签，以及 Assistant 所选主机的活动告警与计数。

例如 CPU 告警必须让内外两层聚合和备用分支都保留主机标识：

```promql
max by (job, instance) (
  100 - avg by (job, instance) (
    rate(node_cpu_seconds_total{mode="idle"}[2m])
  ) * 100
) > 80
or on (job, instance) aiops_demo_cpu_percent > 80
```

本节约束的是主机告警规则。平台级、跨主机聚合告警应单独设计归属和展示，
不能假设缺少主机标签时也能被当前主机工具查询。新增来源还需确认指标目录适配；
保留标签本身不会将 Windows Exporter 指标转换成 Linux 指标。

规则文件为 `deploy/prometheus/alerts.yml`。更新后使用当前部署对应的 Compose 项目/
环境文件检查并重新加载 Prometheus（不是只重建 Assistant）：

```bash
docker compose exec prometheus promtool check rules /etc/prometheus/alerts.yml
docker compose exec prometheus promtool check config /etc/prometheus/prometheus.yml
docker compose kill -s SIGHUP prometheus
```

重新加载前已产生的无 job 告警不会被补写标签；以新规则完成评估后的告警为准。

## 已移除的配置与升级

`auth.enabled` 和管理 API 的 `auth_enabled` 已删除，不能关闭身份验证。
`alert.trend_step` 已没有调用方；`app.version` 改为只读代码元信息；
`server` 段随本机服务入口删除。运行 migrate-config 先备份，再移除上述 YAML 字段。
业务加载器直接读取残留旧字段会拒绝启动，管理 API 提交未知 settings 字段返回 422。
备份位置与恢复边界见 [部署指南](deployment.md)。其他有效配置保持。
