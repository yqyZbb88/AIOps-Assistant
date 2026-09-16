# 用户 / 运维使用指南

本文面向部署和使用 AIOps-Assistant 的运维人员，不要求阅读 Python 源码。

## 一、它能做什么

当前版本是一套单主机智能运维助手：

- 查询 Prometheus / Node Exporter 指标
- 查询 Alertmanager 活动告警
- 接收 Alertmanager webhook
- 按 alertname 匹配自动处置规则
- 对无规则告警执行 AI 分析
- 周期执行固定指标巡检
- 将异常巡检写入 SQLite
- 通过聊天 Agent 查询指标、告警、巡检历史和 SSH 只读诊断结果
- 申请执行白名单写命令，并由人工审批
- 记录动作审计和会话历史

## 二、适用范围与限制

当前适合：

```text
单主机
单 Prometheus
单 Alertmanager
单 worker
内网使用
少量用户
```

当前不支持：

- 多主机选择
- 多 worker 下可靠审批
- 审批跨进程重启恢复
- 配置热更新
- 任意 PromQL 由用户或模型提交
- AI 读取任意日志文件
- 自动处置所有异常

## 三、部署前准备

需要准备：

1. Python 3.11～3.13 环境
2. Prometheus
3. Node Exporter
4. Alertmanager
5. 可选：DeepSeek API Key
6. 可选：SSH 私钥或密码
7. 目标主机 known_hosts

安装依赖：

```bash
pip install -r requirements.txt
```

初始化环境变量：

```bash
cp .env.example .env
```

不要把 `.env` 提交到版本库。

## 四、必须配置的环境变量

### DeepSeek

```env
DEEPSEEK_API_KEY=...
DEEPSEEK_BASE_URL=https://api.deepseek.com
DEEPSEEK_MODEL=deepseek-v4-flash
DEEPSEEK_TIMEOUT=30
DEEPSEEK_MAX_RETRIES=2
DEEPSEEK_MAX_TOKENS=2048
```

如果不配置模型：

- 聊天会回落关键词模式
- 告警 AI 分析会跳过

### Prometheus

```env
PROMETHEUS_URL=http://localhost:9090
```

### SSH

```env
SSH_PASSWORD_192_168_20_128=...
SSH_KNOWN_HOSTS=
```

生产环境优先使用私钥，不建议长期使用 root 密码。

### API 鉴权

```env
OPS_API_TOKENS={"随机密钥":"alice"}
OPS_APPROVER_IDENTITIES=["alice"]
```

### Alertmanager

`POST /alert` 必须使用 `auth.alert_identity` 对应的专用密钥。

完整配置见 [auth.md](auth.md)。

## 五、启动服务

开发或试运行：

```bash
uvicorn src.main:app --host 127.0.0.1 --port 8000
```

直接运行：

```bash
python src/main.py
```

访问：

```text
http://127.0.0.1:8000/docs
```

生产环境建议使用 systemd 或容器，不要使用 `--reload`。

## 六、第一次验收

按顺序验证：

### 1. 健康检查

```bash
curl http://127.0.0.1:8000/health
```

### 2. 创建会话

```bash
curl -X POST http://127.0.0.1:8000/sessions   -H "Authorization: Bearer <密钥>"   -H "Content-Type: application/json"   -d '{"name":"首次验收"}'
```

### 3. 聊天

```bash
curl -X POST http://127.0.0.1:8000/chat   -H "Authorization: Bearer <密钥>"   -H "Content-Type: application/json"   -d '{"message":"CPU 使用率是多少","session_id":"<会话ID>"}'
```

### 4. Alertmanager 推送

用一个专用 Alertmanager 密钥推送测试告警，然后检查：

- `/alerts/recent`
- `/alerts/history`
- `logs/inspection.log`
- `logs/audit.log`

### 5. SSH 诊断

在聊天中询问：

```text
查看目标主机内存明细
```

### 6. 写命令审批

让 Agent 申请一次允许的写命令，确认：

- 审批前不执行
- `/approvals` 能看到待审批项
- 批准后才执行
- `logs/audit.log` 有审计记录

## 七、巡检

### 查看配置

`config/settings.yaml`：

```yaml
inspection:
  enabled: true
  interval_seconds: 300
  run_on_start: true
  ttl_hours: 168
  max_rows: 2500
```

### 查看结果

- 正常和异常轮次：`logs/inspection.log`
- 异常轮次：`inspection_runs` 表
- HTTP 查询：`GET /inspections/recent`

巡检日志不会输出到命令行，只进入专用日志文件。

### 调整指标

在 `config/metrics.yaml` 中配置：

- `enabled`
- `surfaces.inspection`
- `surfaces.chat`
- `surfaces.alert_analysis`
- `warning`
- `critical`
- `severity_enabled`
- `selector`

完整字段见 [configuration.md](configuration.md)。

## 八、使用聊天 Agent

Agent 可以：

- 查询已启用 chat 的 Prometheus 指标
- 查询活动告警
- 执行允许的 SSH 只读诊断
- 查询异常巡检历史
- 申请执行白名单写命令

Agent 不能：

- 构造自由 shell 命令
- 读取 SSH 密码
- 指定非白名单主机
- 绕过写命令审批
- 读取任意日志文件

所有工具结果都带 `[数据时效]`。历史巡检记录不代表当前状态。

## 九、日常运维

### 每天

- 检查 `/health`
- 检查 `logs/inspection.log`
- 检查告警归档和 AI 分析
- 确认日志文件没有异常增长

### 每周

- 检查 SQLite 大小
- 检查审计日志轮转
- 检查待审批队列
- 检查巡检异常趋势
- 检查 Prometheus/Alertmanager 连通性

### 变更配置后

当前没有热更新。修改以下文件后必须重启：

- `settings.yaml`
- `metrics.yaml`
- `rules.yaml`
- `actions.yaml`
- `agent_actions.yaml`
- `diagnostics.yaml`

## 十、备份

需要备份：

```text
data/aiops_assistant.db
data/aiops_assistant.db-wal
data/aiops_assistant.db-shm
.env
config/
logs/
```

优先使用 SQLite 在线备份或先停服务再复制数据库。

升级到 AIOps-Assistant 后，如果新路径 `data/aiops_assistant.db` 不存在而旧 `data/ops_agent.db` 存在，程序会继续使用旧库并记录 WARNING。

不要只复制 `.db` 而遗漏 `-wal` / `-shm`。

## 十一、安全建议

- 使用随机 Bearer Token
- Alertmanager 使用专用密钥
- 多用户环境使用 `approvers_only`
- 生产环境使用专用运维账号
- SSH 优先使用私钥
- known_hosts 必须严格校验
- 不要把 `.env`、数据库和日志提交到 Git
- 写命令先保持 `auto_execute: false`
- 审批前检查命令、参数、目标和影响

## 十二、常见问题

### 服务启动失败

查看：

- `auth.enabled=true` 是否配置了 `OPS_API_TOKENS`
- `metrics.yaml` 是否含未知指标或非法阈值
- `settings.yaml` 是否有未知字段

### 聊天没有调用工具

检查：

- `metrics.yaml` 对应指标是否 `chat: true`
- `agent.enabled` 是否为 true
- DeepSeek 配置是否有效
- 会话是否存在

### 告警没有 AI 分析

检查：

- `llm.enabled`
- alertname 是否在 rules.yaml 中
- 是否匹配到有效规则
- Alertmanager webhook 是否成功

### SSH 诊断失败

检查：

- known_hosts
- 用户名和密码/私钥
- 网络和端口
- 目标服务是否运行

### 巡检没有数据

检查：

- Prometheus 是否可达
- instance selector 是否正确
- 指标是否在 `/api/v1/query` 中可查询
- `inspection.enabled` 是否为 true

### 写命令一直审批不了

检查：

- `agent_actions.enabled`
- `/approvals` 是否有记录
- 当前身份是否有审批权限
- 审批项是否过期或已处理

## 十三、相关文档

- [configuration.md](configuration.md)：YAML 字段
- [auth.md](auth.md)：鉴权和密钥
- [database.md](database.md)：数据库和备份
- [README.md](../README.md)：项目概览和 API
