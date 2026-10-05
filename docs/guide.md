# AIOps-Assistant 使用指南

本页按部署与日常维护组织。页面逐项操作见 [console.md](console.md)，精确 HTTP 请求/响应
见 [api.md](api.md)，证据优先的故障定位见 [troubleshooting.md](troubleshooting.md)。

本文面向部署和日常使用 AIOps-Assistant 的普通用户与运维人员。

## 快速开始

### Docker Compose 启动（推荐）

只需 Docker Desktop（Linux 容器）或 Docker Engine + Compose 2.24+，无需宿主机 Python：

```bash
docker compose up -d --build --wait --wait-timeout 180
docker compose run --rm --no-deps init show-token
```

第一条命令自动初始化配置和凭据，等待五个服务健康后返回；`init` 显示 `Exited (0)` 正常。
第二条显式显示管理员 Token，填入 `http://127.0.0.1:8000/console` 即可。
新部署默认开启鉴权；不填模型密钥也能使用监控与关键词问答。

实际配置位于 `.runtime/config/`，首次从 `deploy/config/` 复制；重复启动保留配置、密钥和数据。
Grafana 默认位于 `http://127.0.0.1:3000`，初始密码通过
`docker compose run --rm --no-deps init show-grafana-password` 查看。
`docker compose down` 停止服务并保留数据；五个发布端口均绑定本机回环地址。

模型密钥、端口调整、SSH、旧版本迁移和备份见 [部署与升级](deployment.md)。
旧部署保留原鉴权与功能开关；Windows/macOS 上 Node Exporter 观察 Docker Linux 环境。

### 本地 Python 启动

本地 Python 服务使用根目录 `.env` 和启动进程的环境变量。
`.runtime/compose.env` 不参与它的鉴权；修改 Token 后需重启本地进程。

#### 1. 安装依赖

```bash
pip install -r requirements.txt
```

#### 2. 创建环境变量文件

```bash
cp .env.example .env
```

至少填写：

```env
DEEPSEEK_API_KEY=你的模型密钥
PROMETHEUS_URL=http://localhost:9090
OPS_API_TOKENS={"随机密钥":"admin","alertmanager-随机密钥":"alertmanager"}
OPS_APPROVER_IDENTITIES=["admin"]
OPS_ADMIN_IDENTITIES=["admin"]
```

生成随机 API 密钥：

```bash
python -c "import secrets;print(secrets.token_urlsafe(32))"
```

#### 3. 检查配置

至少确认：

- `config/settings.yaml` 中的 `auth.enabled`；需要普通 API 使用 Token 校验时设为 `true`
- `config/hosts.yaml` 中每台主机的 ID、Prometheus `job`/`instance`、SSH 凭据引用和授权
- `config/metrics.yaml` 中要监控的指标
- `config/diagnostics.yaml` 中允许的只读命令；多主机 SSH 目标来自 `hosts.yaml`
- `config/rules.yaml` 和 `config/actions.yaml` 的自动处置状态

详细字段见 [configuration.md](configuration.md)。

#### 4. 启动服务

```bash
uvicorn src.main:app --host 127.0.0.1 --port 8000
```

或：

```bash
python src/main.py
```

#### 5. 验证服务

```bash
curl http://127.0.0.1:8000/health
```

#### 6. 创建会话并聊天

```bash
curl -X POST http://127.0.0.1:8000/sessions \
  -H "Authorization: Bearer 随机密钥" \
  -H "Content-Type: application/json" \
  -d '{"name":"第一次使用","host_id":"local-node"}'
```

复制返回的 `session_id`，然后：

```bash
curl -X POST http://127.0.0.1:8000/chat \
  -H "Authorization: Bearer 随机密钥" \
  -H "Content-Type: application/json" \
  -d '{"message":"CPU 使用率是多少","session_id":"会话ID"}'
```

### 五分钟安全告警演示

Compose 启动后，用 textfile 指标模拟高 CPU，不会制造真实负载：

```bash
docker compose run --rm --no-deps --entrypoint python init scripts/demo_fault.py cpu on --runtime /runtime
```

规则包含 `for: 30s`，等待 Prometheus 评估和 Alertmanager 推送后，在控制台查看活动告警、
告警归档。新部署的处置规则为空、写命令关闭；配置模型密钥后可生成 AI 建议，
不配置时仍接收和归档告警。已有部署按其实际规则处理。重复启动不会重置演示指标。

```bash
docker compose run --rm --no-deps --entrypoint python init scripts/demo_fault.py reset --runtime /runtime
docker compose stop node-exporter
docker compose start node-exporter
```

停止 Node Exporter 可演示 `NodeExporterDown`。该告警没有处置规则：配置了 DeepSeek key 时
可生成 AI 建议；未配置时告警仍会被接收和归档，并明确显示 AI 不可用。

## 一、适用范围

## 运行范围

适用于少量 Linux 主机、单 Prometheus、单 Alertmanager、单 Assistant 进程和单 worker
的内网部署。监控查询与 SSH 命令均受管理员配置目录约束；待审批申请仅在当前进程有效。

## 二、主要能力

- 查询 Prometheus 指标
- 按稳定主机 ID 查询状态、指标、趋势和活动告警
- 查询 Alertmanager 活动告警
- 接收 Alertmanager webhook
- 对无规则告警生成 AI 分析
- 周期巡检固定指标
- 将异常巡检写入 SQLite
- 执行 SSH 白名单只读诊断
- 申请执行 SSH 白名单写命令
- 写命令人工审批
- 统一敏感信息脱敏和有界 SSH 输出读取
- 健康/就绪探针和图形化控制台
- 可重复启动的五服务开发监控栈
- 保存会话和历史归档
- 持久化规则/Agent 动作的主机、幂等状态和执行结果
- API Bearer Token 鉴权
- 所有 AI 工具结果显示数据时效

## 三、环境变量

完整样例见 `.env.example`。

### DeepSeek

```env
DEEPSEEK_API_KEY=...
DEEPSEEK_BASE_URL=https://api.deepseek.com
DEEPSEEK_MODEL=deepseek-v4-flash
DEEPSEEK_TIMEOUT=30
DEEPSEEK_MAX_RETRIES=2
DEEPSEEK_MAX_TOKENS=2048
```

### Prometheus

```env
PROMETHEUS_URL=http://localhost:9090
```

### SSH

```env
SSH_HOST=your-server.example.com
SSH_PORT=22
SSH_USER=ops
SSH_KEY_FILENAME=/path/to/id_ed25519
SSH_PASSWORD=
SSH_KNOWN_HOSTS=
```

生产环境优先使用 SSH 私钥，不建议长期使用 root 密码。

这些 `SSH_*` 变量供 `config/hosts.yaml` 的凭据占位符使用；当 `hosts.file` 为空时也用于
构造 `legacy-default` 单主机兼容目标。多主机请求只使用所选 `host_id` 的登记目标，不会
回退到另一个默认地址。

### API 鉴权

```env
OPS_API_TOKENS={"k-admin-随机值":"admin","k-am-随机值":"alertmanager"}
OPS_APPROVER_IDENTITIES=["admin"]
OPS_ADMIN_IDENTITIES=["admin"]
```

要求：

- `OPS_API_TOKENS` 必须是合法 JSON 对象
- key 是密钥
- value 是身份名
- 身份名大小写敏感
- 不要使用容易猜测的密钥

## 四、鉴权和角色

### 普通用户

`auth.enabled=true` 时，普通业务接口需要 Bearer Token：

```text
Authorization: Bearer <密钥>
```

`/health`、`/ready`、`/console` 及控制台静态资源公开，但空壳不返回运维数据、凭据、
地址或详细异常。普通业务接口是否校验 Token 取决于 `auth.enabled`；
系统管理接口始终校验管理员 Token。
FastAPI 自带的 `/docs`、`/redoc` 和
`/openapi.json` 也公开；在不可信网络部署时应由反向代理限制这些路由。

### Alertmanager

Alertmanager 使用单独的身份和密钥：

```yaml
auth:
  alert_identity: alertmanager
```

`auth.enabled=true` 且配置了 `alert_identity` 时，只有该身份可以调用 `POST /alert`。

### 审批人

推荐配置使用 `approvers_only`，只有 `approver_identities` 中的身份可以批准写命令
（审批人是岗位，能批准**任何人**触发的申请，不限于自己发起的；另一种 `owner` 模式
则相反，只认触发人自己）。详细规则见 [auth.md](auth.md)。

```yaml
auth:
  approval_mode: approvers_only
  approver_identities: '["admin"]'
```

### 系统管理员

`admin_identities` 与审批人相互独立。只有管理员身份能调用 `/admin/config` 并看到控制台的
“系统管理”导航。管理 API 不受 `auth.enabled=false` 影响，始终校验 Token。

```yaml
auth:
  admin_identities: '${OPS_ADMIN_IDENTITIES:-["admin"]}'
```

管理页可维护安全/功能开关、自动处置总开关、告警规则、规则动作、Agent 动作、只读诊断
命令和逐主机授权。普通配置编辑区不显示或修改 Token、SSH 地址、密码、密钥文件、
known-hosts 或 Prometheus 连接地址。页面分为“安全与开关”“告警规则”“命令目录”
“主机授权”“监控主机”五个分区，命令目录内可再切换三类命令；前四区共用一份
未保存草稿，顶部统一保存或重载。保存成功即切换运行快照；页面外修改 YAML 后点击
“应用磁盘配置”。监听地址、SQLite/日志路径及环境变量密钥仍需重启。

“监控主机”向导先选 Prometheus 已发现目标；只有项目自管 Compose 模式能输入新的
Exporter `主机:端口`。管理员只看到目标的必要标签与凭据 ID，不看到 SSH 密码或
密钥路径。可选 SSH 的主机名从真实抓取目标推导，代理抓取目标不开放 SSH 选项。
“测试 SSH”仅检查主机密钥和认证，不执行远程命令；通过后才可授予只读诊断。
新增默认无规则动作和 Agent 写授权。`up=0` 仍可登记，但页面会明确提示 Exporter
不可达。向导不会安装、启动或重启远程服务。

控制台支持新增监控主机。清退或修改已有主机时，应核对规则、会话、执行与巡检引用，
并分别处理 Prometheus 目标配置；直接删除 `hosts.yaml` 中的记录不会自动清理这些数据。

主机详情的图形展示的是所选时间范围内的最小、平均、当前和最大值区间摘要，不是逐点
时间序列；数值相同时会合并标记。运维记录默认只显示指标快照的简短预览，完整快照和
原始 JSON 可分别展开查看。

### 轮换密钥

更完整的密钥配置和排查见 [auth.md](auth.md)。

1. 先增加新密钥
2. 重启服务
3. 客户端切换新密钥
4. 删除旧密钥
5. 再次重启

## 五、Alertmanager webhook

在 Alertmanager 的 `webhook_configs` 中配置 Bearer Token：

```yaml
receivers:
  - name: aiops-assistant
    webhook_configs:
      - url: http://127.0.0.1:8000/alert
        http_config:
          authorization:
            type: Bearer
            credentials_file: /path/to/aiops-assistant-token
```

配置后必须实际推送一条测试告警，不要只确认 Alertmanager 进程启动成功。

## 六、配置文件

| 文件 | 作用 |
|---|---|
| `settings.yaml` | 应用、服务、存储、日志、鉴权和巡检调度 |
| `hosts.yaml` | 主机 ID、Prometheus 目标、SSH 凭据引用和逐主机授权 |
| `metrics.yaml` | 指标启用范围、阈值、实例和 selector |
| `rules.yaml` | alertname 到自动处置命令的映射 |
| `actions.yaml` | 规则自动处置的写命令白名单 |
| `agent_actions.yaml` | Agent 写命令白名单，强制人工审批 |
| `diagnostics.yaml` | SSH 只读诊断命令白名单 |

管理页可编辑字段保存后热生效；页面外手动修改 YAML 后通过管理员接口显式应用。
监听、SQLite/日志路径和环境密钥仍需重启。字段说明见
[configuration.md](configuration.md)。

## 七、指标和巡检

### 指标

`metrics.yaml` 控制：

- 指标是否启用
- 是否用于巡检
- 是否给聊天 Agent
- 是否用于告警 AI 趋势
- warning/critical 阈值
- instance 和标签 selector

### 巡检

推荐配置：

```yaml
inspection:
  enabled: true
  interval_seconds: 300
  run_on_start: true
  ttl_hours: 168
  max_rows: 2500
```

巡检结果：

- 每轮写 `logs/inspection.log`
- 异常轮次写 SQLite
- `GET /inspections/recent` 查询异常历史
- 聊天 Agent 可查询异常巡检归档

巡检日志不会输出到命令行。

## 八、日常使用

### 健康检查

```bash
curl http://127.0.0.1:8000/health
curl -i http://127.0.0.1:8000/ready
```

`/health` 是进程存活探针，只读取缓存并始终返回 200。`/ready` 主动检查 Prometheus；
Prometheus 不可达，或已启用巡检但调度器未运行时返回 503。存储降级时仍 ready，但整体
状态为 `degraded`，因为服务会按既有语义退化到内存能力。

### 常用 API

| 路径 | 用途 |
|---|---|
| `GET /health` | 缓存的组件存活状态 |
| `GET /ready` | 主动就绪检查 |
| `GET /console` | 图形化操作控制台空壳 |
| `GET /admin/config` | 管理员读取脱敏结构化配置 |
| `PUT /admin/config` | 管理员保存配置并生成配置备份 |
| `GET /hosts` | 已登记主机清单，不包含 SSH 地址和凭据 |
| `GET /hosts/{id}/status` | 指定主机状态、核心指标和活动告警 |
| `GET /hosts/{id}/metrics/{metric}` | 指定主机指标；追加 `/trend` 查询趋势 |
| `GET /executions` | 规则与 Agent 的统一动作执行记录，可按 `host_id` 过滤 |
| `POST /sessions` | 创建会话 |
| `POST /chat` | 聊天 |
| `POST /alert` | Alertmanager webhook |
| `GET /alerts/recent` | 最近接收的告警 |
| `GET /alerts/active` | 当前 pending/firing 告警 |
| `GET /alerts/history` | 告警、处置和 AI 建议归档 |
| `GET /inspections/recent` | 异常巡检记录 |
| `GET /approvals` | 待审批写命令 |
| `POST /approvals/{id}` | 批准或拒绝 |
| `GET /sessions` | 会话列表 |
| `GET /sessions/{id}` | 会话详情 |
| `PATCH /sessions/{id}` | 修改或清除会话标签 |
| `DELETE /sessions/{id}` | 删除会话并取消关联未决审批 |

`storage.enabled=false` 或数据库初始化失败且未获得会话存储时，聊天仍可无状态回答，但
`POST /sessions` 不可用，`POST /chat` 不保存历史且响应会话字段为 null。告警实时缓冲与
Prometheus 查询不依赖会话存储。

多主机配置下，创建会话必须提交明确 `host_id`，且绑定后不可更换。只有一台启用主机或
使用 `legacy-default` 兼容方式时，省略 `host_id` 才会自动绑定唯一目标。

### 写命令审批

写命令不会在调用时立即执行。流程是：

```text
Agent 申请
→ 进入待审批
→ 人工批准或拒绝
→ 批准后才执行
```

审批前必须检查：

- 主机 ID 和脱敏目标摘要
- 命令名
- 参数
- 目标影响
- 业务时间窗口

## 九、日志和数据库

默认文件：

```text
data/aiops_assistant.db
logs/audit.log
logs/inspection.log
```

- `audit.log` 是 JSON Lines 审计记录
- `inspection.log` 是多行人类可读巡检记录
- SQLite 保存会话、告警、审批和异常巡检
- SQLite schema v7 还保存所有记录的主机归属和规则/Agent 统一动作执行状态

旧版本默认数据库为 `data/ops_agent.db`。如果新库不存在但旧库存在，程序会继续使用旧库并记录 WARNING。

Compose 的挂载和网络：

| 内容 | 位置 |
|---|---|
| Assistant SQLite | `.runtime/assistant-data/` |
| Assistant 日志 | `.runtime/assistant-logs/` |
| 生效配置 | `.runtime/config/`（本地 Python 使用根 `config/`） |
| 部署凭据及元数据 | `.runtime/compose.env`、`.runtime/deployment.json`、各服务专用凭据目录 |
| 演示 textfile 指标 | `.runtime/textfile/` |
| Prometheus/Alertmanager/Grafana 数据 | Docker 命名卷 |

所有发布端口默认只绑定 `127.0.0.1`。容器内 Assistant 使用 `prometheus:9090`，指标实例为
`node-exporter:9100`。Windows 上 Node Exporter 看到 Docker Linux 环境，不是 Windows 宿主机；
Linux 默认栈采集容器环境；宿主机指标取决于所部署的 Exporter 和挂载配置。

## 十、备份

数据库结构和维护细节见 [database.md](database.md)。

本地 Python 模式需要备份：

```text
data/aiops_assistant.db
data/aiops_assistant.db-wal
.env
config/
logs/
```

推荐方式：

- 优先停服务后复制
- 或使用 SQLite `.backup`
- 停机文件级复制时保持主 `.db` 与存在的 `-wal` 来自同一时刻；`-shm` 可重建，
  但最省心的是复制整个数据目录。运行中请用 SQLite `.backup`，不要只复制 `.db`
- Compose 下停止服务后备份整个 `.runtime/`（含配置、初始化标记、凭据与数据库）及需要保留的命名卷；
  根 `.env` 如有自定义端口、模型或 SSH 环境值也需保留，详见 [部署与升级](deployment.md)
- `.runtime/compose.env` 与 secrets 必须按密钥材料单独加密保管，不要提交版本库

示例：

```bash
sqlite3 data/aiops_assistant.db ".backup backup.db"
```

## 十一、日常维护

### 每天

- 检查 `/health`
- 检查巡检日志
- 检查重复告警和 AI 分析
- 检查写命令审批

### 每周

- 检查数据库大小
- 检查日志轮转
- 检查巡检异常趋势
- 检查 Prometheus 和 Alertmanager 连通性
- 检查密钥是否需要轮换

## 十二、常见问题

### 服务无法启动

下面是快速检查项；不要通过关闭鉴权、清空主机授权或删除数据库来“试着启动”。

检查：

- `OPS_API_TOKENS`
- `metrics.yaml` 未知指标或非法阈值
- `settings.yaml` 未知字段
- `hosts.yaml` 主机 ID、instance 或凭据引用冲突
- 数据库目录权限

### 聊天没有调用工具

检查：

- 指标是否 `chat: true`
- `agent.enabled`
- 模型配置
- 会话 ID

### 告警没有 AI 分析

检查：

- `llm.enabled`
- alertname 是否命中 rules.yaml
- Alertmanager webhook 是否成功
- 模型是否可用

### SSH 诊断失败

检查：

- known_hosts
- 用户名和密码/私钥
- 网络与端口
- 目标主机是否可达
- 会话 `host_id` 是否对应已启用主机，命令是否在该主机 `allow.diagnostics` 中
- 命令是否超过 `connect_timeout`

`output_limit` 同时约束 stdout/stderr 的共享保留预算。底层会交替读取 channel，超出预算后
继续排空但不再保留，并用单调时钟强制截止时间。截断不能替代命令本身的范围限制，命令
目录仍不应加入无界或长时间运行的命令。

### 巡检没有数据

检查：

- `inspection.enabled`
- Prometheus 地址
- instance selector
- 所选主机在 `hosts.yaml` 中的 job/instance 是否与 Prometheus 标签完全一致
- 指标是否真实存在

若当前 `/ready` 正常但两台主机均显示 Exporter 不可达，应先分别核对 Prometheus 的
`up{job,instance}`、Targets 的最近抓取错误和 Prometheus 到 Exporter 的网络路径。
`up=0` 是观测状态，不授权启动/重启远端服务。逐层只读步骤见
[troubleshooting.md](troubleshooting.md)。

### 写命令无法审批

检查：

- `agent_actions.enabled`
- 审批项是否过期
- 当前身份是否有审批权限
- 是否启用了 `approvers_only`
- 审批冻结的主机/动作配置是否已变化；变化后需重新发起

## 十三、安全建议

- 使用随机 Bearer Token
- Alertmanager 使用专用密钥
- 多人环境使用 `approvers_only`
- 生产环境使用专用 SSH 账号
- SSH 优先使用私钥
- 严格校验 known_hosts
- 不提交 `.env`、数据库和日志
- 不提交 `.runtime/`、生成 Token、textfile 指标或监控数据卷
- 可用 `OPS_REDACTION_SECRETS` JSON 数组注册额外需精确遮蔽的旧密钥
- 历史日志和数据库不会自动改写；怀疑曾泄露时应轮换相应密钥
- 自动写命令保持关闭直到充分验证
- 每台主机只授权实际需要的诊断、规则动作和 Agent 动作；默认写授权保持为空
- 写命令审批前仔细核对命令和参数

## 十四、字段参考

所有 YAML 字段、默认值和约束见：

- [configuration.md](configuration.md)
- [auth.md](auth.md)
- [database.md](database.md)
- [console.md](console.md)：图形化页面与管理员配置操作
- [api.md](api.md)：请求响应与状态码
- [troubleshooting.md](troubleshooting.md)：只读排障流程
