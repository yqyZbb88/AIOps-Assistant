# AIOps-Assistant 数据库说明

本文说明 AIOps-Assistant 使用 SQLite 保存的数据、字段含义、清理规则、备份方式和常见维护操作。

## 一、快速参考

| 项目 | 说明 |
|---|---|
| 数据库 | `data/aiops_assistant.db` |
| 引擎 | Python 标准库 `sqlite3` |
| 伴生文件 | `aiops_assistant.db-wal`、`aiops_assistant.db-shm` |
| schema 版本 | v7，保存在 `PRAGMA user_version` |
| 总开关 | `storage.enabled` |
| 默认备份方式 | 停服务后复制，或使用 SQLite `.backup` |

管理员保存配置产生的 `data/config-backups/` **不是数据库备份**，不包含会话、告警、
审批或执行记录。两类备份需要分别规划。

数据库包含真实运维数据，不要提交到 Git，也不要把文件上传到在线预览服务。

## 二、数据库中保存什么

九张表分为四类：

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

### 3. 审批、幂等和执行

| 表 | 内容 |
|---|---|
| `approval_decisions` | 人工审批决策记录 |
| `remediation_claims` | 自动处置幂等抢占记录 |
| `action_executions` | 规则与 Agent 写动作的统一目标、幂等和结果记录 |

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
| `host_id` | 会话绑定的稳定主机 ID；旧记录为空表示未知 |

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
| `alerts` | `host_id`、`host_resolution`、`status`、`instance`、`starts_at` | 主机解析结果、告警状态和来源 |
| `remediations` | `host_id`、`execution_id`、`matched_rule`、`status`、`detail` | 自动处置结果及统一执行记录关联 |
| `ai_advice` | `host_id`、`advice`、`analyzed_at` | 指定主机的 AI 分析建议 |

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
| `decided_by` | 实际提交 approve/reject 的认证身份；与申请人可以不同 |
| `host_id` | 审批冻结的主机 ID |
| `target_summary` | 不含凭据的目标摘要 |
| `actions_json` | 审批时冻结的动作与参数快照 |
| `config_fingerprint` | 审批时的主机/动作配置指纹 |

这张表用于区分进程异常退出后的“未执行”和“结果未知”。

保留期是 `history_ttl_hours`（默认 168 小时 = 7 天），由归档清理一并执行（见第八节）。

### `remediation_claims`

防止同一条告警的同一动作被执行多次：

| 字段 | 说明 |
|---|---|
| `alert_key` | 告警关联键 |
| `action` | 动作名 |
| `alert_status` | 告警状态 |
| `starts_at` | 告警开始时间 |
| `claimed_at` | 抢占时间 |
| `host_id` | 被解析并授权的主机 ID |

四列组成联合主键。Alertmanager 重投时只会有一个请求成功抢占。

### `action_executions`

规则自动处置和 Agent 审批写操作共用的持久化执行状态：

| 字段 | 说明 |
|---|---|
| `execution_id` | 服务端生成的执行 ID |
| `channel` | `rule` 或 `agent` |
| `idempotency_key` | 全局唯一幂等键 |
| `host_id` | 唯一执行目标 |
| `action_name` / `params_json` | 白名单动作及已校验参数 |
| `target_summary` | 脱敏目标摘要 |
| `config_fingerprint` | 执行/审批使用的配置指纹 |
| `approval_id` / `alert_key` | 对应审批或告警，可为空 |
| `status` | `running`、`succeeded`、`failed`、`rejected` 等状态 |
| `detail` | 脱敏结果或错误摘要 |
| `created_at` / `started_at` / `finished_at` | UTC 时间 |

创建记录和唯一幂等键先于远程写命令。命令发出后若无法可靠写入最终状态，记录会保留在
`running`，按“结果未知”人工核对，程序不会自动重试。

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
| `host_id` | 本轮巡检对应的稳定主机 ID |

可通过以下接口查询：

```text
GET /inspections/recent?limit=50&host_id=local-node
```

AI 的 `inspection_anomalies` 工具也读取这张表，并明确标记为历史数据。

## 七、安全查询方式

应用对会话、历史、巡检与 API 返回执行“写入前脱敏 + 读取时再次脱敏”。这会保护新记录，
并在应用读取旧记录时遮蔽当前已知密钥；程序不会原地重写用户数据库。若旧库可能包含已经
暴露的密钥，应先轮换密钥，再按组织的数据保留策略迁移或清理旧记录。

优先使用服务 API：

```text
GET /sessions
GET /sessions/{id}
GET /alerts/history
GET /inspections/recent
GET /approvals
GET /executions
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

### 告警、幂等、审批决策和执行记录清理

| 字段 | 当前默认 | 说明 |
|---|---:|---|
| `history_ttl_hours` | 168 | 告警、处置、AI 建议、幂等抢占、审批决策和动作执行记录 |
| `max_history_rows` | 5000 | 上述各表的容量上限 |

`approval_decisions` 有**两条**清理路径，缺一不可：

- **快路径**：对应会话被 TTL 或容量策略删除时，它的审批决策一起删除；
- **兜底**：按 `history_ttl_hours` 过期、按 `max_history_rows` 限容，由归档清理执行。

兜底不能省：快路径只在会话被删时才跑，既扫不到 `session_id` 为空的行，也扫不到会话
早已消失的孤儿行。`remediation_claims` 和 `action_executions` 同样由归档清理按 TTL 与
容量上限回收。

### 巡检清理

| 字段 | 当前默认 | 说明 |
|---|---:|---|
| `inspection.ttl_hours` | 168 | 异常巡检记录保留小时数 |
| `inspection.max_rows` | 2500 | 异常巡检最大记录数 |


## 九、备份

以下是现有维护方式说明；0.6.0 的生产级并发、恢复时间和干净环境部署演练尚未完成，
不能把这些步骤当作已验证的灾备承诺。执行真实恢复前应先做独立副本和维护窗口计划。

推荐：

1. 停止服务。
2. 复制整个 `data/` 目录。
3. 同时备份 `.env` 和 `config/`。

运行中优先使用 SQLite 的在线备份接口（CLI 的 `.backup` 使用该接口），生成独立的
一致性快照；不要在写入期间用普通文件复制代替：

```bash
sqlite3 data/aiops_assistant.db ".backup backup.db"
```

若服务已停止且确需文件级复制，应复制同一时刻的数据库文件及存在的 WAL 文件，
最简单的是复制整个 `data/` 目录，不要只取主 `.db`：

```text
aiops_assistant.db
aiops_assistant.db-wal
```

`-wal` 可能仍含已提交但未并入主文件的事务；`-shm` 是可重建的 WAL 索引，
不是必须保留的数据文件。不要把不同时间点或不同数据库的主文件与 WAL 混在一起。
依据：[SQLite 在线备份](https://www.sqlite.org/backup.html)、
[WAL 文件说明](https://www.sqlite.org/wal.html)、
[WAL 索引说明](https://www.sqlite.org/walformat.html)。

## 十、恢复

恢复会覆盖当前数据；先确认备份来源、时间、目标路径和可回退副本。不要在服务运行中
删除或替换数据库、`-wal`、`-shm`，也不要把管理员 YAML 配置备份当成 SQLite 文件。

1. 停止服务。
2. 完整保留当前 `data/`，在**独立的空目录**准备恢复副本，避免与旧 WAL/SHM 混用。
3. 如果来源是 `.backup`，在空目录放入生成的独立数据库文件；如果来源是停机文件
   副本，保持同一批主文件和 WAL 文件配对。先在副本上执行 `PRAGMA integrity_check`。
4. 确认目标路径与回退方案后，以准备好的完整副本替换目标数据目录；不要对运行中的
   数据目录直接做文件级拼接。随后启动服务并检查 `/health` 和 `/ready`。
5. 查询会话、告警和巡检接口确认数据可读，并核对最近时间戳和记录数。

这些是保守的维护步骤，不代表本项目已经完成恢复时间、并发或故障注入演练。

如果恢复的是旧版 `data/ops_agent.db`，程序在新库不存在时可以继续使用旧文件。

## 十一、数据库锁定

如果出现 `database is locked`：

- 优先关闭 DB Browser、SQLite Viewer 等外部连接
- 不要长期打开同一个数据库文件
- 通过服务 API 查询
- 命令行查询后立即退出
- 检查 OneDrive 或同步盘是否锁文件

服务端会进行短暂重试。重试失败时：

- 会话创建、会话读取等强依赖存储的接口返回 503
- 已经生成回答的聊天轮次不会因落库失败而改写回答，但本轮历史可能缺失
- 告警归档失败不阻塞 webhook 确认，但本批历史可能缺失
- `/health` 显示 `storage=degraded`

后续一次成功操作会恢复为 `ok`。

审批决策记录落库失败时，命令尚未进入续跑，待审批项会恢复到内存队列；接口返回 503，
调用方可以使用同一审批 ID 重试。若决策已落库但续跑过程异常，则不会恢复审批项，因为
远端命令可能已经执行，重试会造成重复写；重启对账时该轮会标记为 `unknown`。

## 十二、schema 和迁移

普通用户不需要修改 schema。

维护原则：

- schema 版本保存在 `PRAGMA user_version`
- 版本高于代码支持的版本时，服务拒绝使用数据库
- 迁移只追加表和列
- 新库和迁移后的旧库结构必须一致

v7 在 v6 基础上为会话、告警、处置、建议、审批、幂等和巡检追加 `host_id` 及必要的目标
元数据，并新增 `action_executions`。迁移可在中断后重试：已存在的列会跳过。旧行不具备
可靠映射证据，因此 `host_id` 保持空字符串，查询时按未知归属处理。

如果数据库来自更新版本程序，不要用旧版本程序写入。

## 十三、常用查询

### 查看表

```bash
sqlite3 data/aiops_assistant.db ".tables"
```

### 查看会话身份

```bash
sqlite3 data/aiops_assistant.db   "SELECT user, host_id, COUNT(*) FROM sessions GROUP BY user, host_id;"
```

### 查看最近异常巡检

```bash
sqlite3 data/aiops_assistant.db   "SELECT host_id, checked_at, level FROM inspection_runs ORDER BY checked_at DESC LIMIT 20;"
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
UNION ALL SELECT 'inspection_runs', COUNT(*) FROM inspection_runs
UNION ALL SELECT 'action_executions', COUNT(*) FROM action_executions;
"
```

## 十四、后续阶段的数据库决策

0.5.0 多主机监控与异常处置已使用 schema v7 完成，0.6.0 成品收尾继续使用 SQLite。
更换数据库引擎不是成品发布的前置条件。

Prometheus 保存时序指标，SQLite 保存告警、巡检异常、会话、处置和审批等业务数据。
增加被监控主机不等于这些主机直接并发访问数据库；首版保持单 Assistant 进程、单 worker、
本地 SQLite。现有连接层已有 WAL 尝试启用、锁等待、有限重试和显式事务，应先复用并验证。

| 阶段 | 要做的工作 |
|---|---|
| 0.5.0 | 已补齐稳定主机归属、执行目标/状态、必要索引和事务约束；从 v6 追加迁移并验证新库与升级库一致 |
| 0.6.0 | 模拟多主机同时告警、巡检写入与审批提交；检查延迟、锁争用、记录完整性、重复执行防护，并验证重启及备份恢复 |
| 按需迁移 | 合理优化后持续写入争用仍不达标，或需要多台 Assistant 实例共享远程数据库时，评估 PostgreSQL；若影响验收，在当阶段处理 |

SQLite 每个数据库文件同时只有一个写入者，是否适用应根据实际写入负载判断，
不以“监控到多少台主机”设定未经验证的迁移阈值。
参见 [SQLite 官方适用场景说明](https://www.sqlite.org/whentouse.html)。

处置可靠性必须独立于数据库引擎评估：

- 自动规则按主机/动作授权，Agent 写命令仍需审批；审批记录绑定主机、动作、参数及配置指纹。
- 执行所需的决策和幂等证据必须在远程写入前可靠持久化；关键记录无法写入时阻止新执行，
  不能使用面向监控读取的内存降级策略继续写操作。
- 事务只覆盖必要数据库操作，不持锁等待 SSH 或模型响应。
- 重复告警与重复批准不能触发重复执行，主机维度应贯穿记录和唯一约束。
- 命令可能已执行但结果未记录时标记结果未知，禁止自动重试；SSH 与数据库不是一个原子事务，
  不能承诺 exactly-once。备份恢复后同样不能自动重放旧动作。
- 待审批/checkpoint 的跨重启续跑仍延后；保留执行证据、旧申请失效及结果核对属于必要范围。
  将 SQLite 换为 PostgreSQL 不会自动使内存中的审批状态持久化。

未来若迁移引擎，另行验证数据迁移、唯一约束、事务及幂等语义和备份恢复；
现在不提前引入双数据库维护负担。

## 十五、相关文档

- [guide.md](guide.md)：部署和日常使用
- [configuration.md](configuration.md)：数据库相关配置字段
- [auth.md](auth.md)：会话身份和 API Token
- [api.md](api.md)：历史与执行记录接口
- [troubleshooting.md](troubleshooting.md)：锁争用与结果未知时的只读排查

## 十六、热更新与数据库边界

管理员保存配置与新增监控主机不会创建数据库表或切换 SQLite 文件。运行快照切换
会复用既有会话、历史、执行记录、幂等守卫及最近告警存储对象；正在执行的业务
请求或未决审批存在时拒绝应用配置，避免旧 checkpoint 在新授权下继续运行。
环境中的 SQLite 路径修改仍需重启，并不属于热更新字段。
