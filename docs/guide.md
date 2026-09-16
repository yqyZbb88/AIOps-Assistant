# AIOps-Assistant 使用指南

本文面向部署和日常使用 AIOps-Assistant 的普通用户与运维人员。

## 快速开始

### 1. 安装依赖

```bash
pip install -r requirements.txt
```

### 2. 创建环境变量文件

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

### 3. 检查配置

至少确认：

- `config/settings.yaml` 中的 `auth.enabled`
- `config/metrics.yaml` 中要监控的指标
- `config/diagnostics.yaml` 中目标主机和 known_hosts
- `config/rules.yaml` 和 `config/actions.yaml` 的自动处置状态

详细字段见 [configuration.md](configuration.md)。

### 4. 启动服务

```bash
uvicorn src.main:app --host 127.0.0.1 --port 8000
```

或：

```bash
python src/main.py
```

### 5. 验证服务

```bash
curl http://127.0.0.1:8000/health
```

### 6. 创建会话并聊天

```bash
curl -X POST http://127.0.0.1:8000/sessions   -H "Authorization: Bearer 随机密钥"   -H "Content-Type: application/json"   -d '{"name":"第一次使用"}'
```

复制返回的 `session_id`，然后：

```bash
curl -X POST http://127.0.0.1:8000/chat   -H "Authorization: Bearer 随机密钥"   -H "Content-Type: application/json"   -d '{"message":"CPU 使用率是多少","session_id":"会话ID"}'
```

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

后续拓展方向和阶段计划见 [roadmap.md](roadmap.md)。

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

除 `/health` 外，其它接口都需要鉴权。

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

所有配置修改后都需要重启服务。字段说明见 [configuration.md](configuration.md)。

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
```

### 常用 API

| 路径 | 用途 |
|---|---|
| `POST /sessions` | 创建会话 |
| `POST /chat` | 聊天 |
| `POST /alert` | Alertmanager webhook |
| `GET /alerts/recent` | 最近接收的告警 |
| `GET /alerts/active` | 当前 pending/firing 告警 |
| `GET /alerts/history` | 告警和处置归档 |
| `GET /inspections/recent` | 异常巡检记录 |
| `GET /approvals` | 待审批写命令 |
| `POST /approvals/{id}` | 批准或拒绝 |
| `GET /sessions` | 会话列表 |
| `GET /sessions/{id}` | 会话详情 |

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
- 自动写命令保持关闭直到充分验证
- 写命令审批前仔细核对命令和参数

## 十四、字段参考

所有 YAML 字段、默认值和约束见：

- [configuration.md](configuration.md)
- [auth.md](auth.md)
- [database.md](database.md)
