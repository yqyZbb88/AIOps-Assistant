# AIOps-Assistant 使用指南

本文面向部署和日常使用 AIOps-Assistant 的普通用户与运维人员。

## 快速开始

### 一分钟 Compose 启动

前提是已安装 Docker Desktop（Windows）或 Docker Engine + Compose（Linux）：

```bash
python scripts/dev_stack.py up
```

脚本首次运行会创建 `.runtime/`、生成用户 Token、Alertmanager 专用 Token 和 Grafana
管理员密码，然后构建并启动全部五个服务。打开 `http://127.0.0.1:8000/status`，把终端显示的
用户 Token 保存到页面会话即可。Token 只进入浏览器 `sessionStorage`，关闭该标签页会话后清除。

```bash
python scripts/dev_stack.py status
python scripts/dev_stack.py show-token
python scripts/dev_stack.py logs
python scripts/dev_stack.py down
```

`down` 默认保留 Prometheus、Alertmanager 和 Grafana 命名卷。

### 本地 Python 启动

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
```

生成随机 API 密钥：

```bash
python -c "import secrets;print(secrets.token_urlsafe(32))"
```

#### 3. 检查配置

至少确认：

- `config/settings.yaml` 中的 `auth.enabled`
- `config/metrics.yaml` 中要监控的指标
- `config/diagnostics.yaml` 中目标主机和 known_hosts
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
  -d '{"name":"第一次使用"}'
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
python scripts/demo_fault.py cpu on
```

规则包含 `for: 30s`，等待 Prometheus 评估和 Alertmanager 推送后，在状态页查看活动告警、
告警归档和处置记录。`HighCPUUsage` 命中项目规则，但默认只生成“不自动执行”的记录。

```bash
python scripts/demo_fault.py reset
docker compose stop node-exporter
docker compose start node-exporter
```

停止 Node Exporter 可演示 `NodeExporterDown`。该告警没有处置规则：配置了 DeepSeek key 时
可生成 AI 建议；未配置时告警仍会被接收和归档，并明确显示 AI 不可用。

## 一、适用范围

当前版本适合：

```text
单主机
单 Prometheus
单 Alertmanager
单 worker
内网环境
少量用户
```

当前不支持：

- 多主机选择
- 多 worker 下可靠审批
- 审批跨进程重启恢复
- 配置热更新
- 任意 PromQL 执行
- AI 任意读取日志文件
- 生产级容器加固、TLS、高可用和灾备

后续版本以维护者的本地规划为准；本指南只描述当前已经实现的能力。

## 二、主要能力

- 查询 Prometheus 指标
- 查询 Alertmanager 活动告警
- 接收 Alertmanager webhook
- 对无规则告警生成 AI 分析
- 周期巡检固定指标
- 将异常巡检写入 SQLite
- 执行 SSH 白名单只读诊断
- 申请执行 SSH 白名单写命令
- 写命令人工审批
- 统一敏感信息脱敏和有界 SSH 输出读取
- 健康/就绪探针和只读状态页
- 可重复启动的五服务开发监控栈
- 保存会话和历史归档
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

### API 鉴权

```env
OPS_API_TOKENS={"k-admin-随机值":"admin","k-am-随机值":"alertmanager"}
OPS_APPROVER_IDENTITIES=["admin"]
```

要求：

- `OPS_API_TOKENS` 必须是合法 JSON 对象
- key 是密钥
- value 是身份名
- 身份名大小写敏感
- 不要使用容易猜测的密钥

## 四、鉴权和角色

### 普通用户

需要 Bearer Token：

```text
Authorization: Bearer <密钥>
```

`/health`、`/ready`、`/status` 及状态页静态资源公开，但不返回凭据、地址或详细异常。
状态页的所有实际运维数据仍通过受保护 API 获取。FastAPI 自带的 `/docs`、`/redoc` 和
`/openapi.json` 也公开；在不可信网络部署时应由反向代理限制这些路由。

### Alertmanager

Alertmanager 使用单独的身份和密钥：

```yaml
auth:
  alert_identity: alertmanager
```

只有这个身份可以调用 `POST /alert`。

### 审批人

推荐配置使用 `approvers_only`，只有 `approver_identities` 中的身份可以批准写命令
（审批人是岗位，能批准**任何人**触发的申请，不限于自己发起的；另一种 `owner` 模式
则相反，只认触发人自己）。详细规则见 [auth.md](auth.md)。

```yaml
auth:
  approval_mode: approvers_only
  approver_identities: '["admin"]'
```

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
| `metrics.yaml` | 指标启用范围、阈值、实例和 selector |
| `rules.yaml` | alertname 到自动处置命令的映射 |
| `actions.yaml` | 规则自动处置的写命令白名单 |
| `agent_actions.yaml` | Agent 写命令白名单，强制人工审批 |
| `diagnostics.yaml` | SSH 只读诊断命令白名单 |

所有配置修改后都需要重启服务；`RuleEngine` 虽有内部重载方法，但当前没有提供给用户的
热更新 API。字段说明见 [configuration.md](configuration.md)。

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
| `GET /status` | 只读状态页空壳 |
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

### 写命令审批

写命令不会在调用时立即执行。流程是：

```text
Agent 申请
→ 进入待审批
→ 人工批准或拒绝
→ 批准后才执行
```

审批前必须检查：

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

旧版本默认数据库为 `data/ops_agent.db`。如果新库不存在但旧库存在，程序会继续使用旧库并记录 WARNING。

Compose 的挂载和网络：

| 内容 | 位置 |
|---|---|
| Assistant SQLite | `.runtime/assistant-data/` |
| Assistant 日志 | `.runtime/assistant-logs/` |
| 本地凭据 | `.runtime/compose.env`、`.runtime/secrets/` |
| 演示 textfile 指标 | `.runtime/textfile/` |
| Prometheus/Alertmanager/Grafana 数据 | Docker 命名卷 |

所有发布端口默认只绑定 `127.0.0.1`。容器内 Assistant 使用 `prometheus:9090`，指标实例为
`node-exporter:9100`。Windows 上 Node Exporter 看到 Docker Linux 环境，不是 Windows 宿主机；
Linux 默认栈同样只保证容器环境采集，不承诺完整宿主机挂载。

## 十、备份

数据库结构和维护细节见 [database.md](database.md)。

需要备份：

```text
data/aiops_assistant.db
data/aiops_assistant.db-wal
data/aiops_assistant.db-shm
.env
config/
logs/
```

推荐方式：

- 优先停服务后复制
- 或使用 SQLite `.backup`
- 不要只复制 `.db` 而遗漏 WAL/SHM
- Compose 下同时备份 `.runtime/assistant-data`、`.runtime/assistant-logs` 和需要保留的命名卷
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

检查：

- `OPS_API_TOKENS`
- `metrics.yaml` 未知指标或非法阈值
- `settings.yaml` 未知字段
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
- 命令是否超过 `connect_timeout`

`output_limit` 同时约束 stdout/stderr 的共享保留预算。底层会交替读取 channel，超出预算后
继续排空但不再保留，并用单调时钟强制截止时间。截断不能替代命令本身的范围限制，命令
目录仍不应加入无界或长时间运行的命令。

### 巡检没有数据

检查：

- `inspection.enabled`
- Prometheus 地址
- instance selector
- 指标是否真实存在

### 写命令无法审批

检查：

- `agent_actions.enabled`
- 审批项是否过期
- 当前身份是否有审批权限
- 是否启用了 `approvers_only`

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
- 写命令审批前仔细核对命令和参数

## 十四、字段参考

所有 YAML 字段、默认值和约束见：

- [configuration.md](configuration.md)
- [auth.md](auth.md)
- [database.md](database.md)
