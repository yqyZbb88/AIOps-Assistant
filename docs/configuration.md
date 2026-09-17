# AIOps-Assistant YAML 配置指南

本文覆盖 `config/` 下所有运行时 YAML 文件：

| 文件 | 作用 |
|---|---|
| `settings.yaml` | 应用、服务、存储、日志、鉴权、巡检调度等全局配置 |
| `metrics.yaml` | 统一 Prometheus 指标目录的启用范围、阈值、实例和 selector |
| `rules.yaml` | Alertmanager 告警到写命令的规则映射 |
| `actions.yaml` | 规则自动处置可执行的写命令白名单 |
| `agent_actions.yaml` | 聊天 Agent 可申请执行的写命令白名单，强制人工审批 |
| `diagnostics.yaml` | 聊天 Agent 的 SSH 只读诊断命令白名单 |

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
- 项目启动时优先使用系统环境变量，再用根目录 `.env` 补充未设置值。
- 密钥不要直接写入 YAML；使用环境变量占位。

### 2. 严格校验

- `settings.yaml` 与 `metrics.yaml` 严格拒绝未知字段；命令目录还会严格检查模板占位符、
  参数声明和审批段字段。可选能力配置失败时会记录错误并关闭对应能力。
- 布尔值只接受明确的 `true/false`、`yes/no`、`on/off`、`1/0`。
- 类型不合法、阈值顺序错误、未知指标等会拒绝启动或在启动时关闭对应能力。
- 修改配置后需要重启服务，当前没有热更新入口。

### 3. 配置文件关系

```text
settings.yaml
  └── metrics.file ──> metrics.yaml ──> Prometheus 指标
  └── rules.yaml  ──> actions.yaml ──> 规则自动处置写命令
  └── agent_actions.yaml ──> Agent 写命令 + 人工审批
  └── diagnostics.yaml   ──> Agent SSH 只读诊断
```

`metrics.yaml` 不控制 `rules.yaml` 或 SSH 诊断。  
`rules.yaml` 不直接查 Prometheus 指标，只按 Alertmanager 的 `alertname` 匹配。

### 4. 配置文件路径

默认从仓库根目录下的 `config/` 读取。以下环境变量可替换加载路径，适合测试、容器挂载或
多套环境配置：

| 环境变量 | 默认文件 |
|---|---|
| `OPS_SETTINGS_FILE` | `config/settings.yaml` |
| `OPS_RULES_FILE` | `config/rules.yaml` |
| `OPS_ACTIONS_FILE` | `config/actions.yaml` |
| `OPS_AGENT_ACTIONS_FILE` | `config/agent_actions.yaml` |
| `OPS_DIAGNOSTICS_FILE` | `config/diagnostics.yaml` |

`metrics.yaml` 没有单独的路径环境变量；它由 `settings.yaml` 的 `metrics.file` 指定。相对
路径按仓库根目录解析，空字符串表示使用代码内置的默认运行配置。显式传给加载函数的路径
优先级高于上述环境变量。

## 二、settings.yaml

下列表格的“默认值”指仓库自带 `config/settings.yaml` 的出厂值。代码中的部分 dataclass
默认值会为离线测试关闭鉴权、存储和巡检；生产启动读取 YAML，不应把直接构造
`Settings()` 的测试默认值当作部署默认值。

### app

| 字段 | 类型 | 默认值 | 说明 |
|---|---:|---:|---|
| `app.name` | string | `AIOps-Assistant` | FastAPI 标题和日志应用名 |

应用版本不在 YAML 中配置，由程序内置版本号统一提供。

### server

| 字段 | 类型 | 默认值 | 说明 |
|---|---:|---:|---|
| `server.host` | string | `127.0.0.1` | 监听地址。`0.0.0.0` 会开放到网络 |
| `server.port` | int | `8000` | HTTP 端口 |

仅在 `python src/main.py` 直接运行时由应用读取；`uvicorn` 命令行参数会覆盖。

### prometheus

| 字段 | 类型 | 默认值 | 说明 |
|---|---:|---:|---|
| `prometheus.base_url` | string | `http://localhost:9090` | Prometheus 地址，建议用 `${PROMETHEUS_URL:-http://localhost:9090}` |
| `prometheus.timeout` | int | `5` | HTTP 查询超时秒数 |

所有指标即时查询和范围查询共用该超时。

### monitor

| 字段 | 类型 | 默认值 | 说明 |
|---|---:|---:|---|
| `monitor.provider` | string | `prometheus` | 监控源插件名；当前注册的是 `prometheus` |

### llm

| 字段 | 类型 | 默认值 | 说明 |
|---|---:|---:|---|
| `llm.provider` | string | `deepseek` | LLM 插件名 |
| `llm.enabled` | bool | `true` | `false` 时跳过告警 AI 分析；不控制聊天 Agent |

聊天 Agent 的总开关是 `agent.enabled`。

### agent

| 字段 | 类型 | 默认值 | 说明 |
|---|---:|---:|---|
| `agent.enabled` | bool | `true` | `false` 时聊天只走关键词兜底，不构建 LLM Agent |
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
| `metrics.file` | string | 空 | 统一指标配置文件路径；空表示使用代码内默认配置 |

当前出厂值为 `config/metrics.yaml`。路径非空但文件不存在、解析失败或含未知指标时会拒绝启动。

### alert

| 字段 | 类型 | 默认值 | 说明 |
|---|---:|---:|---|
| `alert.recent_limit` | int | `100` | 内存中最近告警条数，重启即清空 |
| `alert.trend_minutes` | int | `60` | 告警 AI 分析趋势窗口（分钟） |
| `alert.trend_step` | string | `5m` | 范围查询步长 |

### inspection

| 字段 | 类型 | 默认值 | 说明 |
|---|---:|---:|---|
| `inspection.enabled` | bool | `true` | 是否启动周期巡检；只支持单 worker |
| `inspection.interval_seconds` | int | `300` | 全局巡检间隔，必须为 30~86400 秒 |
| `inspection.run_on_start` | bool | `true` | 服务启动后是否立即巡检一次 |
| `inspection.ttl_hours` | int | `168` | 异常巡检归档保留小时数 |
| `inspection.max_rows` | int | `2500` | 异常巡检归档最大行数 |

具体检查项、阈值和 surface 不在本段配置，统一在 `metrics.yaml` 中设置。

### storage

| 字段 | 类型 | 默认值 | 说明 |
|---|---:|---:|---|
| `storage.enabled` | bool | `true` | SQLite 持久化总开关 |
| `storage.db_path` | string | `data/aiops_assistant.db` | 数据库路径；升级时若新路径不存在且旧 `data/ops_agent.db` 存在，会自动继续使用旧库 |
| `storage.session_ttl_hours` | int | `72` | 会话过期时间 |
| `storage.max_sessions` | int | `200` | 最大会话数，超出淘汰最旧会话 |
| `storage.message_window` | int | `20` | 每轮发送给模型的历史消息上限 |
| `storage.history_ttl_hours` | int | `168` | 告警/处置/AI 建议归档保留时间 |
| `storage.max_history_rows` | int | `5000` | 告警/处置/建议表及幂等、审批决策表各自的容量上限 |
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
| `auth.enabled` | bool | `true` | 是否启用 Bearer 鉴权 |
| `auth.tokens` | JSON string | 空 | 密钥到身份的 JSON 映射，建议用 `${OPS_API_TOKENS}` |
| `auth.approval_mode` | string | `approvers_only` | `owner` 或 `approvers_only` |
| `auth.approver_identities` | JSON array string | `["admin"]` | `approvers_only` 模式下允许审批的身份 |
| `auth.alert_identity` | string | `alertmanager` | 允许调用 `POST /alert` 的身份 |

示例：

```yaml
auth:
  enabled: true
  tokens: '${OPS_API_TOKENS:-}'
  approval_mode: approvers_only
  approver_identities: '${OPS_APPROVER_IDENTITIES:-["admin"]}'
  alert_identity: alertmanager
```

## 三、metrics.yaml

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

## 四、rules.yaml

规则只按 Alertmanager 的 `alertname` 匹配，不查询 Prometheus 指标。

```yaml
rules:
  - alertname: HighCPUUsage
    action: restart_service
    target_instance: "${PROMETHEUS_INSTANCE:-localhost:9100}"
    params:
      host: "${SSH_HOST:-localhost}"
      port: "${SSH_PORT:-22}"
      username: "${SSH_USER:-ops}"
      password: "${SSH_PASSWORD:-}"
      key_filename: "${SSH_KEY_FILENAME:-}"
      known_hosts: "${SSH_KNOWN_HOSTS:-}"
      service: nginx
    auto_execute: false
    description: "CPU 过高时重启 nginx"
```

| 字段 | 类型 | 必填 | 含义 |
|---|---:|---:|---|
| `alertname` | string | 是 | 与 Alertmanager alertname 精确匹配 |
| `action` | string | 是 | 必须存在于 `actions.yaml.commands` |
| `target_instance` | string | auto_execute 时是 | 允许处理的告警实例，必须精确匹配 |
| `params` | map | 否 | 写命令参数与 SSH 连接参数；省略时为空映射 |
| `auto_execute` | bool | 否 | true 自动执行，false 只命中不执行 |
| `description` | string | 否 | 给人看的说明 |

连接字段支持：

```text
host, port, username, password, key_filename, known_hosts, connect_timeout
```

`connect_timeout` 可以省略，**默认 8 秒**；它同时是「连不上」和「单条命令跑不完」的
上限，某条规则要跑更久就显式写大一点。写 `0` 也落到默认值上（`0` 在 paramiko 里是
「永不超时」，不能让一条会挂死的远程命令变成默认行为）。

这个默认值**只有一处定义**（`src/plugins/common/endpoint.py` 的
`DEFAULT_CONNECT_TIMEOUT`），`diagnostics.yaml` 与 `agent_actions.yaml` 的 `target`
用的是同一个数——三条 SSH 路径不该各有各的「没写就用多少」。

其余字段按 `actions.yaml` 对应命令的 `params` 校验。

重要行为：

- `auto_execute=false` 仍然是“命中规则”，会阻止该 alertname 进入 AI 建议。
- `auto_execute=true` 且缺少 `target_instance` 时，规则会在加载阶段被禁用。
- 同名 alertname 可以写多条规则，按文件顺序处理。

## 五、actions.yaml

这是规则自动处置的写命令白名单。

```yaml
enabled: true
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
| `enabled` | bool | 否 | 文件总开关，默认 true |
| `commands` | map | 是 | 命令 key 到命令定义的映射 |
| `summary` | string | 否 | 命令说明 |
| `template` | string | 是 | 管理员可信命令模板 |
| `sudo` | bool | 否 | 是否提权执行 |
| `params` | map | 是 | 模板参数声明 |

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
- sudo 由本文件声明，规则不能覆盖。
- 修改后需重启进程。

## 六、agent_actions.yaml

这是聊天 Agent 的写命令白名单，和 `actions.yaml` 完全隔离。

```yaml
enabled: false
target:
  host: "${SSH_HOST:-localhost}"
  port: 22
  username: "${SSH_USER:-ops}"
  key_filename: "${SSH_KEY_FILENAME:-}"
  password: "${SSH_PASSWORD:-}"
  known_hosts: "${SSH_KNOWN_HOSTS:-}"
  connect_timeout: 8
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
| `target.host` | string | 目标主机 |
| `target.port` | int | SSH 端口 |
| `target.username` | string | SSH 用户名 |
| `target.password` | string | 密码，建议环境变量占位 |
| `target.key_filename` | string | 私钥路径，与 password 二选一 |
| `target.known_hosts` | string | known_hosts 路径；空则使用系统路径 |
| `target.connect_timeout` | int | 连接和命令超时秒数，省略则 **8**（`≥1`，写 0 报配置错误） |
| `output_limit` | int | 回传 Agent 的输出字符上限 |
| `approval.timeout_seconds` | int | 待审批超时秒数 |
| `approval.max_pending` | int | 最大待审批数量 |
| `commands` | map | 写命令定义，格式同 actions.yaml |

`output_limit` 目前在 SSH 命令完成、输出读入内存后生效，用于限制模型上下文，不是底层
内存上限。命令模板仍应主动限制输出规模；流式限量读取属于 0.4.0 安全项。

审批段只接受 `timeout_seconds` 和 `max_pending`，没有关闭审批的开关。命令 key 不能与聊天指标工具或 SSH 诊断工具重名。

## 七、diagnostics.yaml

这是 Agent 的 SSH 只读诊断白名单，不绑定 `metrics.yaml` 指标。

```yaml
enabled: true
provider: ssh
target:
  host: "${SSH_HOST:-localhost}"
  port: 22
  username: "${SSH_USER:-ops}"
  key_filename: "${SSH_KEY_FILENAME:-}"
  password: "${SSH_PASSWORD:-}"
  known_hosts: "${SSH_KNOWN_HOSTS:-}"
  connect_timeout: 8
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
| `target` | map | SSH 连接参数，同 agent_actions（`connect_timeout` 省略则 8） |
| `output_limit` | int | 单条命令回传上限 |
| `commands` | map | 只读命令白名单 |

`output_limit` 与 Agent 写命令具有相同边界：限制返回字符数，但当前不会阻止远端命令先
产生大量输出。因此命令必须使用 `head`、`journalctl -n` 等静态或参数化上限。

命令参数格式与 `actions.yaml` 相同，但：

- 永远不 sudo。
- 非零退出码只作为可读结果，不触发写动作。
- 修改后需重启服务重建 Agent 工具。

## 八、常见配置流程

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

重启服务后，巡检日志和异常归档会包含该指标。

### 把指标加入聊天 Agent

```yaml
cpu.iowait:
  enabled: true
  surfaces: { inspection: true, chat: true, alert_analysis: false }
  severity_enabled: false
```

重启后，Agent 工具和关键词兜底会包含该指标。

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

## 九、常见错误

| 错误 | 原因 |
|---|---|
| `配置节 xxx 存在未知字段` | 拼写错误或使用了旧字段 |
| `metrics.yaml 含未知指标` | 指标 ID 不在代码目录 |
| `warning < critical` / `warning > critical` | 阈值方向不符 |
| `observe 指标不能启用 severity` | observe 指标只能记录 |
| Prometheus 400 Bad Request | 通常是 selector/PromQL 拼接错误，应检查配置中的特殊标签 |
| 修改后不生效 | 当前配置不支持热更新，需要重启服务 |

## 十、字段生效与重启

| 配置 | 生效方式 |
|---|---|
| `settings.yaml` | 启动时加载；修改后重启 |
| `metrics.yaml` | 启动时加载；修改后重启 |
| `rules.yaml` | 启动时加载；当前没有 HTTP 热更新入口 |
| `actions.yaml` | 容器首次构建执行器时加载；修改后重启 |
| `agent_actions.yaml` | 首次构建 Agent 时加载；修改后重启 |
| `diagnostics.yaml` | 首次构建 Agent 时加载；修改后重启 |
