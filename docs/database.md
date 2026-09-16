# AIOps-Assistant 数据库说明

本文说明 AIOps-Assistant 使用 SQLite 保存的数据、字段含义、清理规则、备份方式和常见维护操作。

## 一、快速参考

| 项目 | 说明 |
|---|---|
| 数据库 | `data/aiops_assistant.db` |
| 引擎 | Python 标准库 `sqlite3` |
| 伴生文件 | `aiops_assistant.db-wal`、`aiops_assistant.db-shm` |
| schema 版本 | v6，保存在 `PRAGMA user_version` |
| 总开关 | `storage.enabled` |
| 默认备份方式 | 停服务后复制，或使用 SQLite `.backup` |

数据库包含真实运维数据，不要提交到 Git，也不要把文件上传到在线预览服务。

## 二、数据库中保存什么

八张表分为四类：

### 1. 会话

| 表 | 内容 |
|---|---|
| `sessions` | 一段会话的元信息 |
| `messages` | 会话中的用户和助手消息 |

### 2. 告警状态日志

| 表 | 内容 |
|---|---|
| `alerts` | 收到的 Alertmanager 告警摘要 |
| `remediations` | 告警规则的处置结果 |
| `ai_advice` | 无规则告警的 AI 分析建议 |

### 3. 审批和幂等

| 表 | 内容 |
|---|---|
| `approval_decisions` | 人工审批决策记录 |
| `remediation_claims` | 自动处置幂等抢占记录 |

### 4. 巡检

| 表 | 内容 |
|---|---|
| `inspection_runs` | 异常巡检轮次快照和明细 |

normal 巡检轮次只写日志，不写数据库。

## 三、会话表

### `sessions`

| 字段 | 说明 |
|---|---|
| `session_id` | 服务端生成的随机会话 ID |
| `user` | 会话归属身份，来自 API Token |
| `name` | 用户设置的会话标签，可为空 |
| `title` | 首条用户消息生成的可读标题 |
| `status` | 当前固定为 `active` |
| `created_at` | 创建时间，UTC |
| `updated_at` | 最后活动时间，TTL 依据此字段 |

会话名允许重复，不参与定位会话。

### `messages`

| 字段 | 说明 |
|---|---|
| `id` | 自增主键 |
| `session_id` | 所属会话 |
| `seq` | 会话内消息顺序 |
| `role` | `user` 或 `assistant` |
| `content` | 消息正文 |
| `status` | `final`、`pending_approval`、`abandoned`、`unknown` |
| `approval_id` | 待审批轮次关联的审批 ID |
| `created_at` | 写入时间 |

模型只读取 `final` 状态的历史，不读取待审批、作废和未知状态。

## 四、告警和处置表

`alerts`、`remediations`、`ai_advice` 通过 `alert_key` 关联：

```text
alerts
  ├── remediations
  └── ai_advice
```

`alert_key` 优先使用 Alertmanager fingerprint；没有 fingerprint 时使用：

```text
alertname | instance | startsAt
```

常用字段：

| 表 | 重要字段 | 说明 |
|---|---|---|
| `alerts` | `status`、`instance`、`starts_at` | 告警状态和来源 |
| `remediations` | `matched_rule`、`status`、`detail` | 自动处置结果 |
| `ai_advice` | `advice`、`analyzed_at` | AI 分析建议 |

常见处置状态：

```text
ok
failed
matched_but_auto_execute_disabled
instance_mismatch
duplicate_ignored
```

## 五、审批与幂等表

### `approval_decisions`

记录审批决策是否已经提交：

| 字段 | 说明 |
|---|---|
| `approval_id` | 审批 ID |
| `session_id` | 关联会话 |
| `action` | `approve` 或 `reject` |
| `decided_at` | 决策时间 |
| `decided_by` | 决策身份 |

这张表用于区分进程异常退出后的“未执行”和“结果未知”。

### `remediation_claims`

防止同一条告警的同一动作被执行多次：

| 字段 | 说明 |
|---|---|
| `alert_key` | 告警关联键 |
| `action` | 动作名 |
| `alert_status` | 告警状态 |
| `starts_at` | 告警开始时间 |
| `claimed_at` | 抢占时间 |

四列组成联合主键。Alertmanager 重投时只会有一个请求成功抢占。

## 六、巡检表

### `inspection_runs`

只保存异常轮次：

| 字段 | 说明 |
|---|---|
| `checked_at` | 巡检时间 |
| `level` | `warning`、`critical` 或 `error` |
| `snapshot_json` | 指标快照 |
| `findings_json` | 异常明细 |
| `error_text` | 查询错误或数据错误 |

可通过以下接口查询：

```text
GET /inspections/recent?limit=50
```

AI 的 `inspection_anomalies` 工具也读取这张表，并明确标记为历史数据。

## 七、安全查询方式

优先使用服务 API：

```text
GET /sessions
GET /sessions/{id}
GET /alerts/history
GET /inspections/recent
GET /approvals
```

直接查询 SQLite 时，命令应执行完后立即退出：

```bash
sqlite3 data/aiops_assistant.db "SELECT COUNT(*) FROM sessions;"
```

不要一边运行服务，一边让 DB Browser 等 GUI 长期持有数据库连接。

## 八、数据保留和清理

### 会话清理

由 `settings.yaml` 控制：

| 字段 | 当前默认 | 说明 |
|---|---:|---|
| `session_ttl_hours` | 72 | 过期会话及消息 |
| `max_sessions` | 200 | 最多保留的会话数 |
| `purge_interval_seconds` | 60 | 清理节流间隔 |

### 告警和幂等清理

| 字段 | 当前默认 | 说明 |
|---|---:|---|
| `history_ttl_hours` | 168 | 告警、处置、AI 建议和幂等抢占记录 |
| `max_history_rows` | 5000 | 上述归档表和幂等表的容量上限 |

`approval_decisions` 跟随会话清理：对应会话被 TTL 或容量策略删除时，其审批决策也一起删除。

### 巡检清理

| 字段 | 当前默认 | 说明 |
|---|---:|---|
| `inspection.ttl_hours` | 168 | 异常巡检记录保留小时数 |
| `inspection.max_rows` | 1000 | 异常巡检最大记录数 |

如果巡检周期为 300 秒且持续异常，建议把 `inspection.max_rows` 调整到 2500 左右。

## 九、备份

推荐：

1. 停止服务。
2. 复制整个 `data/` 目录。
3. 同时备份 `.env` 和 `config/`。

运行中备份可使用：

```bash
sqlite3 data/aiops_assistant.db ".backup backup.db"
```

如果直接复制文件，必须同时复制：

```text
aiops_assistant.db
aiops_assistant.db-wal
aiops_assistant.db-shm
```

只复制主文件可能得到不一致快照。

## 十、恢复

1. 停止服务。
2. 备份当前 `data/`。
3. 将备份数据库恢复到 `data/aiops_assistant.db`。
4. 删除或覆盖匹配的 `-wal`、`-shm` 文件。
5. 启动服务并检查 `/health`。
6. 查询会话、告警和巡检接口确认数据可读。

如果恢复的是旧版 `data/ops_agent.db`，程序在新库不存在时可以继续使用旧文件。

## 十一、数据库锁定

如果出现 `database is locked`：

- 优先关闭 DB Browser、SQLite Viewer 等外部连接
- 不要长期打开同一个数据库文件
- 通过服务 API 查询
- 命令行查询后立即退出
- 检查 OneDrive 或同步盘是否锁文件

服务端会进行短暂重试。重试失败时：

- 当前请求照常返回
- 本次数据可能不落库
- `/health` 显示 `storage=degraded`

后续一次成功操作会恢复为 `ok`。

## 十二、schema 和迁移

普通用户不需要修改 schema。

维护原则：

- schema 版本保存在 `PRAGMA user_version`
- 版本高于代码支持的版本时，服务拒绝使用数据库
- 迁移只追加表和列
- 新库和迁移后的旧库结构必须一致

如果数据库来自更新版本程序，不要用旧版本程序写入。

## 十三、常用查询

### 查看表

```bash
sqlite3 data/aiops_assistant.db ".tables"
```

### 查看会话身份

```bash
sqlite3 data/aiops_assistant.db   "SELECT user, COUNT(*) FROM sessions GROUP BY user;"
```

### 查看最近异常巡检

```bash
sqlite3 data/aiops_assistant.db   "SELECT checked_at, level FROM inspection_runs ORDER BY checked_at DESC LIMIT 20;"
```

### 查看失败处置

```bash
sqlite3 data/aiops_assistant.db   "SELECT alertname, matched_rule, status, detail, executed_at
   FROM remediations WHERE status='failed'
   ORDER BY executed_at DESC LIMIT 20;"
```

### 查看各类数据量

```bash
sqlite3 data/aiops_assistant.db "
SELECT 'sessions' AS table_name, COUNT(*) AS rows FROM sessions
UNION ALL SELECT 'messages', COUNT(*) FROM messages
UNION ALL SELECT 'alerts', COUNT(*) FROM alerts
UNION ALL SELECT 'remediations', COUNT(*) FROM remediations
UNION ALL SELECT 'ai_advice', COUNT(*) FROM ai_advice
UNION ALL SELECT 'approval_decisions', COUNT(*) FROM approval_decisions
UNION ALL SELECT 'remediation_claims', COUNT(*) FROM remediation_claims
UNION ALL SELECT 'inspection_runs', COUNT(*) FROM inspection_runs;
"
```

## 十四、相关文档

- [guide.md](guide.md)：部署和日常使用
- [configuration.md](configuration.md)：数据库相关配置字段
- [auth.md](auth.md)：会话身份和 API Token
