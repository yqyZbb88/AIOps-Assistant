# AIOps-Assistant

AIOps-Assistant 是一套面向单主机环境的智能运维助手，基于 FastAPI、Prometheus、Alertmanager、LangChain 和 SQLite。

当前版本：`0.3.0`

## 主要能力

- AI 对话：按配置调用 Prometheus 指标工具回答问题
- 统一指标目录：内置 CPU、Memory、Disk、Network、System、Alerts 指标
- 告警接收：接收 Alertmanager webhook
- 告警分析：对未匹配规则的告警生成 AI 分析建议
- 自动巡检：按周期检查固定指标并生成等级
- 异常归档：异常巡检写入 SQLite，正常轮次写日志文件
- SSH 只读诊断：执行白名单诊断命令
- Agent 写命令：白名单写操作必须经过人工审批
- 会话历史：多轮聊天和状态归档
- API 鉴权：Bearer Token 到身份映射
- 数据时效：AI 工具结果明确标注观测时间

## 适用环境

当前版本适合：

```text
单主机
单 Prometheus
单 Alertmanager
单 worker
内网环境
少量用户
```

当前限制：

- 不支持多主机选择
- 不支持多 worker 下可靠审批
- 审批不跨进程重启恢复
- 配置修改后需要重启
- 不提供任意 PromQL 执行接口
- AI 不能读取任意日志文件

## 文档

| 文档 | 内容 |
|---|---|
| [docs/user-guide.md](docs/user-guide.md) | 部署、启动、验收、日常运维、备份和排障 |
| [docs/configuration.md](docs/configuration.md) | 所有运行时 YAML 字段、默认值、约束和示例 |
| [docs/auth.md](docs/auth.md) | API 密钥、身份、Alertmanager webhook 和审批职责分离 |
| [docs/database.md](docs/database.md) | SQLite 表结构、数据生命周期、备份和维护 |

## 快速开始

安装依赖：

```bash
pip install -r requirements.txt
```

复制环境变量样例：

```bash
cp .env.example .env
```

至少配置：

```env
DEEPSEEK_API_KEY=...
PROMETHEUS_URL=http://localhost:9090
OPS_API_TOKENS={"随机密钥":"alice"}
```

启动：

```bash
uvicorn src.main:app --host 127.0.0.1 --port 8000
```

或直接运行：

```bash
python src/main.py
```

访问：

```text
http://127.0.0.1:8000/docs
```

## 常用接口

| 方法/路径 | 说明 |
|---|---|
| `GET /health` | 健康检查 |
| `POST /sessions` | 创建会话 |
| `POST /chat` | 使用 `session_id` 进行聊天 |
| `POST /alert` | Alertmanager webhook |
| `GET /alerts/recent` | 最近接收的告警 |
| `GET /alerts/active` | 当前 pending/firing 告警 |
| `GET /alerts/history` | 落盘的告警、处置和 AI 建议 |
| `GET /approvals` | 待人工审批的写命令 |
| `POST /approvals/{id}` | 批准或拒绝写命令 |
| `GET /inspections/recent` | 最近的异常巡检记录 |
| `GET /sessions` | 会话列表 |
| `GET /sessions/{id}` | 会话详情 |
| `PATCH /sessions/{id}` | 修改会话标签 |
| `DELETE /sessions/{id}` | 删除会话及未决审批 |

除 `/health` 外，启用鉴权后都需要：

```text
Authorization: Bearer <密钥>
```

## 配置文件

运行时配置位于 `config/`：

| 文件 | 作用 |
|---|---|
| `settings.yaml` | 应用、服务、存储、日志、鉴权和巡检调度 |
| `metrics.yaml` | 指标启用范围、阈值、实例和 selector |
| `rules.yaml` | Alertmanager alertname 到写命令的规则映射 |
| `actions.yaml` | 规则自动处置的写命令白名单 |
| `agent_actions.yaml` | Agent 写命令白名单，强制人工审批 |
| `diagnostics.yaml` | SSH 只读诊断命令白名单 |

字段说明：

- [配置指南](docs/configuration.md)
- [鉴权指南](docs/auth.md)

## 安全建议

- 使用随机 Bearer Token
- Alertmanager 使用专用密钥
- 多用户环境使用 `approvers_only`
- 生产环境优先使用 SSH 私钥
- 保持严格 known_hosts 校验
- 不要把 `.env`、数据库和日志提交到 Git
- 自动写命令先保持 `auto_execute: false`
- 写命令必须人工检查命令、目标和参数

## 日志和数据

默认位置：

```text
data/aiops_assistant.db
logs/audit.log
logs/inspection.log
```

- `audit.log` 记录动作和审批审计，一行一个 JSON 对象
- `inspection.log` 记录每轮巡检结果，按指标分组输出多行文本
- SQLite 保存会话、告警归档、审批决策和异常巡检

详细说明见 [数据库文档](docs/database.md)。
