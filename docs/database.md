# AIOps-Assistant 数据库说明

本文说明 AIOps-Assistant 使用 SQLite 保存的数据、字段含义、清理规则、备份方式和常见维护操作。

## 快速参考

| 项目 | 说明 |
|---|---|
| 数据库 | `data/aiops_assistant.db` |
| 引擎 | Python 标准库 `sqlite3` |
| 伴生文件 | `aiops_assistant.db-wal`、`aiops_assistant.db-shm` |
| schema 版本 | v8，保存在 `PRAGMA user_version` |
| 总开关 | `storage.enabled` |
| 默认备份方式 | 停服务后复制，或使用 SQLite `.backup` |

管理员保存配置产生的 `data/config-backups/` **不是数据库备份**，不包含会话、告警、
审批或执行记录。备份时应分别保存配置文件与 SQLite 数据。

数据库包含真实运维数据，不要提交到 Git，也不要把文件上传到在线预览服务。

## 数据库中保存什么

九张表分为四类：

### 会话

| 表 | 内容 |
|---|---|
| `sessions` | 一段会话的元信息 |
| `messages` | 会话中的用户和助手消息 |

### 告警状态日志

| 表 | 内容 |
|---|---|
| `alerts` | 收到的 Alertmanager 告警摘要 |
| `remediations` | 告警规则的处置结果 |
| `ai_advice` | 无规则告警的 AI 分析建议 |

### 审批、幂等和执行

| 表 | 内容 |
|---|---|
| `approval_decisions` | 人工审批决策记录 |
| `remediation_claims` | 自动处置幂等抢占记录 |
| `action_executions` | 规则与 Agent 写动作的统一目标、幂等和结果记录 |

### 巡检

| 表 | 内容 |
|---|---|
| `inspection_runs` | 异常巡检轮次快照和明细 |

normal 巡检轮次只写日志，不写数据库。

## 会话表

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

## 告警和处置表

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

## 审批与幂等表

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

保留期是 `history_ttl_hours`（默认 168 小时 = 7 天），由归档清理一并执行（见[数据保留和清理](#数据保留和清理)）。

### `remediation_claims`

防止同一条告警的同一动作被执行多次：

| 字段 | 说明 |
|---|---|
| `alert_key` | 告警关联键 |
| `action` | 动作名 |
| `alert_status` | 告警状态 |
| `starts_at` | 告警开始时间 |
| `claimed_at` | 抢占时间 |
| `host_id` | 兼容元数据，当前规则抢占路径可能为空；主机审计看 action_executions |

四列组成联合主键。在该幂等键尚未清理时，Alertmanager 重投只能有一个请求成功抢占；
TTL 清理或恢复旧备份后不能保证键仍在。

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

## 巡检表

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

## 安全查询方式

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

## 数据保留和清理

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

仍被 `pending_approval` 消息引用的决策记录暂不按 TTL 或容量删除，以保留重启时
判定 `unknown` 的依据；对账、正常完成或删除会话后再按原策略清理。因此决策表
可能暂时超过历史容量上限，但不会把可能已执行的审批当成从未执行。

兜底不能省：快路径只在会话被删时才跑，既扫不到 `session_id` 为空的行，也扫不到会话
早已消失的孤儿行。`remediation_claims` 和 `action_executions` 同样由归档清理按 TTL 与
容量上限回收。

### 巡检清理

| 字段 | 当前默认 | 说明 |
|---|---:|---|
| `inspection.ttl_hours` | 168 | 异常巡检记录保留小时数 |
| `inspection.max_rows` | 2500 | 异常巡检最大记录数 |


## 备份

以下步骤适用于当前单进程部署。执行恢复前先制作独立副本，安排维护窗口，
并在实际环境中验证备份文件可读。

推荐：

1. 停止服务。
2. 停机后备份整个 `.runtime/`，
   包括 `config/`、`compose.env`、`deployment.json`、凭据目录、SQLite 与日志；
3. 另行备份三个 Docker 命名卷及根 `.env`。恢复时保持项目名和部署目录一致，详见 [deployment.md](deployment.md)。

运行中使用 SQLite 在线备份接口。默认容器路径为 `/app/data/aiops_assistant.db`，
宿主机映射为 `.runtime/assistant-data/aiops_assistant.db`；自定义目录或旧库回退时先核对
实际使用路径。生产镜像没有要求安装 sqlite3 CLI，可用内置 Python 完成快照：

```bash
docker compose exec -T assistant python -c "import sqlite3; from pathlib import Path; s=sqlite3.connect('file:/app/data/aiops_assistant.db?mode=ro', uri=True); p=Path('/app/data/pre-upgrade.db'); p.touch(exist_ok=False); d=sqlite3.connect(str(p)); s.backup(d); d.close(); s.close()"
docker compose cp assistant:/app/data/pre-upgrade.db ./pre-upgrade.db
```

示例针对默认新库，先确认源文件存在；SQLite connect 会创建不存在的文件，不能用它
猜测源路径。目标名称必须未使用，避免覆盖上次备份。根目录的临时备份需立即转移到
受控备份位置，不提交仓库；完整运行目录及监控卷备份见[升级与回滚](deployment.md#升级与回滚)。
上面的只读源连接会在源文件不存在时失败，目标已存在也会拒绝覆盖。
重复演练时，为本次备份换一个新名称，并同步修改后续检查和复制命令。

### 第一次验证 SQLite 快照

先确认在线备份命令退出成功，再从部署机项目根目录执行以下检查，示例仍针对
运行目录中本次生成的 assistant-data/pre-upgrade.db，不是正在使用的业务数据库：

```bash
docker compose run --rm --no-deps --entrypoint python init -c "import sqlite3; c=sqlite3.connect('file:/runtime/assistant-data/pre-upgrade.db?mode=ro', uri=True); result=c.execute('PRAGMA integrity_check').fetchall(); c.close(); print(result); assert result == [('ok',)]"
```

预期输出 `[('ok',)]` 且退出码为 0。不存在的文件不会被创建；其它结果或异常均须停止恢复。
init 只用来提供相同运行目录挂载，此处覆盖入口为 Python，不执行初始化。
自定义项目、运行目录或隔离测试栈时，为所有命令加相同的 -p/--env-file 并保留对应环境输入。
完整性通过只证明 SQLite 结构可读，不能证明配置、凭据和三个监控卷已经备份。
恢复前还应记录并复查一条已知会话、告警的 ID 和时间，步骤见 [恢复](#恢复)。

### 停机文件副本

下列 SQL 查询示例使用宿主机自行安装的 sqlite3 CLI；其中 `data/` 表示应用工作目录，
在宿主机使用前需替换为实际数据库路径。

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

## 恢复

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

## 数据库锁定

如果出现 `database is locked`：

- 优先关闭 DB Browser、SQLite Viewer 等外部连接
- 不要长期打开同一个数据库文件
- 通过服务 API 查询
- 命令行查询后立即退出
- 检查 OneDrive 或同步盘是否锁文件

服务端执行锁等待与有限重试，总等待可超过单次 5 秒 busy timeout。重试失败时：

- 会话创建、会话读取等强依赖存储的接口返回 503
- 普通最终回答遇到历史写入失败仍可返回，但本轮历史可能缺失
- 待审批轮次写入失败会取消本次申请并返回 503；会话在回答期间被删除则返回 404
- 告警归档失败返回 503，让 webhook 发送方重试；已完成的处置不会因归档失败自动撤销
- `/health` 显示 `storage=degraded`

已装配 Store 在后续成功操作后可恢复为 `ok`；数据库初始化失败被缓存时，
修复底层原因后可能需要重启，不保证自动重新装配。

审批决策记录落库失败时，命令尚未进入续跑，待审批项会恢复到内存队列；接口返回 503，
调用方可以使用同一审批 ID 重试。若决策已落库但续跑过程异常，则不会恢复审批项，因为
远端命令可能已经执行，重试会造成重复写；重启对账时该轮会标记为 `unknown`。

启用会话存储时，绑定会话的审批必须存在已保存的待审批消息，才允许消费；检查覆盖全部消息，
不受展示或模型历史窗口限制。链式申请写回失败或原轮次已不存在时，下一条申请取消、原 ID 不恢复，
接口返回 503。若取消未成功，未保存的下一条也会在消费检查处被拒绝。

## schema 和迁移

普通用户不需要修改 schema。

维护原则：

- schema 版本保存在 `PRAGMA user_version`
- 版本高于代码支持的版本时，服务拒绝使用数据库
- 新迁移追加版本分支并保留历史分支；既有迁移含索引调整
- 新库和迁移后的旧库结构必须一致

v7 在 v6 基础上为会话、告警、处置、建议、审批、幂等和巡检追加 `host_id` 及必要的目标
元数据，并新增 `action_executions`。v7 的加列步骤可在中断后重试：已存在的列会跳过；不能由此推断全部历史迁移均具备
相同的中断恢复保证。旧行不具备
可靠映射证据，因此 `host_id` 保持空字符串，查询时按未知归属处理。

如果数据库来自更新版本程序，不要用旧版本程序写入。

v8 新增 `approval_requests` 及 `(host_id, created_at)` 索引。该表冻结 `approval_id`、
`session_id`、`requester`、`host_id`、`target_summary`、脱敏 `actions_json`、`created_at`，
并保存 `status`、`decided_by`、`decided_at`。新申请先落盘后入内存队列；人工决策与
`approval_decisions` 在同一 SQLite 事务写入。状态为 pending、approve、reject、expired、
cancelled、evicted、abandoned；重启失去 checkpoint 的未决项标为 abandoned，原因不猜测。
系统作废没有人工审批人/审批时间，保持空值。执行状态仍以 `action_executions` 为准。

删除主机登记或会话不删除此表。历史 TTL/容量沿用 `history_ttl_hours` 与 `max_history_rows`，
保护 pending 行；旧决策和已作废消息通过只读投影复用，不按 instance 猜测归属。
已有会话/决策的旧清理策略保持原样，因此旧数据缺失的申请人、时间等返回空值，页面显示未知。
迁移不重建旧 checkpoint，也不会自动执行旧申请。

## 常用查询

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

## SQLite 运行边界

Prometheus 保存时序指标；SQLite 保存告警、巡检异常、会话、处置和审批等业务数据。
Assistant 以单进程、单 worker 使用本地 SQLite。连接层尝试启用 WAL，并使用锁等待、
有限重试和显式事务。每个数据库文件同时只有一个写入者。

连接默认锁等待为 5 秒；程序内指定 `Database(timeout=...)` 时，SQLite 的
`busy_timeout` 使用同一秒数换算的毫秒值，有限重试会增加总等待时间。批量写入
在一个事务中提交，失败时整批回滚。会话消息写入先在同一事务中检查父会话存在，
避免并发删除或未知会话留下孤立消息；数据库结构保持不变。

SSH 执行与数据库事务分开。命令结果为 `unknown` 时，应核对执行记录和目标主机状态，
不要自动重放；恢复数据库备份后也应先核对动作记录。

## 相关文档

- [guide.md](guide.md)：日常使用
- [configuration.md](configuration.md)：数据库相关配置字段
- [auth.md](auth.md)：会话身份和 API Token
- [api.md](api.md)：历史与执行记录接口
- [troubleshooting.md](troubleshooting.md)：锁争用与结果未知时的只读排查

## 热更新与数据库边界

管理员保存配置与新增监控主机不会创建数据库表或切换 SQLite 文件。运行快照切换
会复用既有会话、历史、执行记录、幂等守卫及最近告警存储对象；正在执行的业务
请求、未决审批存在或审批状态无法核对时拒绝应用配置，避免旧 checkpoint 在新授权下继续运行。
环境中的 SQLite 路径修改仍需重启，并不属于热更新字段。

## 会话主机权限补充

HTTP 会话读写要求有效 Token、所有者身份匹配且仍有会话主机访问权；管理员也不能代读私有会话。
撤销主机权限或 Token 不删除历史。user 为空的旧会话默认不可访问，不自动推断归属。
常规初始化和升级不自动认领或删除匿名会话；当前 SQLite schema 为 8。

HTTP /chat 在持久化关闭或无法验证会话时返回 503，禁止回退到没有授权主机范围的回答。
内部可信旧入口的无持久化降级仍保留，不能通过 HTTP 触发。普通已完成回答的历史写入失败
仍按原降级契约处理；待审批轮次无法保存时取消申请并报错。
