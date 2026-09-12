# 数据库结构说明

本文档说明 ops-agent 的 SQLite 持久化库：**六张表、每个字段的含义、以及它们之间的关系**。

> 实现代码见 [src/storage/db.py](../src/storage/db.py)（建表与连接）与
> [src/storage/session.py](../src/storage/session.py)、[src/storage/history.py](../src/storage/history.py)（读写）。
> 配置见 [config/settings.yaml](../config/settings.yaml) 的 `storage` 段。

---

## 一、概览

| 项 | 说明 |
|---|---|
| 引擎 | **Python 标准库自带的 `sqlite3`** —— 零新增依赖，不需要安装任何软件或启动数据库服务 |
| 位置 | 默认 `data/ops_agent.db`（相对仓库根，由 `storage.db_path` 配置） |
| 伴生文件 | WAL 模式下同目录会有 `ops_agent.db-wal` 与 `ops_agent.db-shm`（**同样是运行时产物，不要提交**） |
| 日志模式 | `wal`（同步盘/网络盘上开不起来时自动回落 `delete`，不影响使用） |
| schema 版本 | 当前 **v2**，记在 `PRAGMA user_version` 里 |
| 总开关 | `storage.enabled`。关掉 / 打不开 → **服务照常启动**，只是不落盘，行为与引入本功能前完全一致 |
| 清理 | TTL + 容量上限，**惰性 + 节流**触发（写入时顺带，默认 60 秒最多一次），无后台线程 |

### 怎么打开来看

```bash
# 环境里自带的命令行工具（零安装）
"D:/MiniConda/envs/langchain1.2/Library/bin/sqlite3.exe" data/ops_agent.db
sqlite> .tables
sqlite> .schema sessions
sqlite> SELECT * FROM sessions;
sqlite> .quit
```

图形界面：VSCode 装 `SQLite Viewer` 扩展后直接点开 `.db` 文件；或独立工具 DB Browser for SQLite。
**注意 SQLite 是「打开文件」而不是「连接服务器」**——不要去找 host/port 填写框。

> ⚠️ **安全提示**：这个库里有**真实的对话原文**（可能含主机名、报错内容、运维细节）。
> 所以：**不要提交进版本库**（`.gitignore` 已排除 `data/`），
> **也不要用那些「上传 .db 在线预览」的网站**——等于把全部会话内容发给第三方。

---

## 二、表之间的关系

六张表分成两组，**两组之间没有任何关联**（对话与告警是两条独立的业务线）。

### 第一组：对话（2 张）

```
sessions ──1:N──> messages
（一段对话）      （这轮对话里的每条消息）
```

### 第二组：状态日志（4 张）

```
                    alerts（收到的告警）
                      │
              ┌───────┴───────┐  alert_key
              ▼               ▼
       remediations       ai_advice
      （规则处置结果）    （AI 分析建议）
```

**关联键是 `alert_key`**，不是表里那个 `id`。它由
[src/storage/models.py](../src/storage/models.py) 的 `Alert.alert_key` 算出：

```
alert_key = fingerprint（若告警带了）否则 "告警名|实例|开始时间"
```

**为什么不用 `fingerprint` 直接当关联键**：它默认为空串，而且很多来源（包括测试构造的告警）
根本不设它。拿空串当关联键，会把**不同的告警并成同一条**。

一条告警可能对应 **0~N 条** `remediations`（`config/rules.yaml` 里可以让同一个 alertname
命中多条规则），以及 **0~1 条** `ai_advice`（命中规则就不走 AI 分析了）。

### `approval_decisions`（孤立表）

它不属于上面任何一组，当前也**没有任何代码往里写**——详见第四节末尾。

---

## 三、第一组：对话

### `sessions` —— 一段对话

| 字段 | 类型 | 含义 |
|---|---|---|
| `session_id` | TEXT 主键 | **服务端生成的 32 位随机十六进制**，不可猜。`/chat` 返回给你、下次带回来的就是它 |
| `user` | TEXT | 调用方标识（`/chat` 请求里的 `user`）。**只用于给会话名划定作用域**，不当作会话键 |
| `name` | TEXT | **客户端自定义的会话别名**（`session_name`），如 `test_1`、`生产环境 排查`。空串 = 未命名 |
| `title` | TEXT | 自动取**首条用户消息**的前 40 字，纯展示用 |
| `status` | TEXT | 目前恒为 `active`（保留字段，为将来的归档态留位） |
| `created_at` | TEXT | 创建时间（UTC，秒级，ISO-8601） |
| `updated_at` | TEXT | 最后活动时间。**TTL 清理看的是它，不是 `created_at`** ——活跃会话不会因为建得早被清掉 |

**唯一索引** `idx_sessions_user_name ON sessions(user, name) WHERE name <> ''`

两个要点：

- **作用域是 `user`**：A 的 `test_1` 与 B 的 `test_1` 是两段不同对话，不冲突。
- **`WHERE name <> ''` 是必须的**：绝大多数会话没有名字，若直接 `UNIQUE(user, name)`，
  同一 user 的**第二个无名会话会因为 `'' = ''` 而插入失败**。这叫部分索引——只约束有名字的。

> **`name` 为什么排在字段列表最后**：它由 v2 迁移的 `ALTER TABLE` 追加，而 ALTER 只能加在末尾。
> 全新建库的 DDL 也把新列写在末尾，两条路径的**列序**才对得上——否则将来有人写
> `INSERT INTO sessions VALUES (...)` 会在新库上正常、在旧库上炸。

**关于 `name` 的安全性**：`session_id` 是猜不到的（32 位随机），而**名字是能猜的**。
本服务没有鉴权，所以**不要**把「别人不知道这个名字」当作隔离手段。
名字的价值是「客户端可读、不用保存一串乱码」；把作用域定在 `user` 上，是为了将来接入登录后，
名字自动变成「(登录用户, 名字)」，那时猜名字就失效了。

### `messages` —— 对话里的每条消息

| 字段 | 类型 | 含义 |
|---|---|---|
| `id` | INTEGER 主键 | 自增，仅用于排序兜底 |
| `session_id` | TEXT | 属于哪段对话（对应 `sessions.session_id`） |
| `seq` | INTEGER | **在会话内的序号**，从 1 开始严格递增。**排序靠它，不靠时间戳** |
| `role` | TEXT | `user` 或 `assistant`。**只有这两种**（不存工具调用，见下） |
| `content` | TEXT | 消息原文 |
| `status` | TEXT | 见下方三态说明 |
| `approval_id` | TEXT | 该轮触发了写命令审批时，这里是审批 id；否则为 NULL |
| `created_at` | TEXT | 时间戳（UTC，秒级） |

**唯一约束** `UNIQUE(session_id, seq)`，另有索引 `idx_messages_session_seq`。

#### `status` 三种取值

| 值 | 含义 | 会进模型上下文吗 |
|---|---|---|
| `final` | 正常完成的一轮 | ✅ 会 |
| `pending_approval` | 这轮触发了写命令，**最终回答还没产生**（等你审批） | ❌ 不会 |
| `abandoned` | 该审批已不可能再有下文（进程重启 / 过期 / 会话被删） | ❌ 不会 |

**为什么 `seq` 不用时间戳代替**：时间戳是秒级的，同一秒里连写两行就分不出先后。

**为什么不存工具调用**：中间那几轮 tool-call 消息既难序列化也没必要——下一轮模型需要数据
会重新调工具。存了反而要维护一整套 langchain 消息对象的序列化。

**「一轮」= 两行**：`user` 一行 + `assistant` 一行，**共享同一个 `status` 与 `approval_id`**。
中断那轮两行一起标 `pending_approval`。若只标 assistant 那行，下一轮模型会看到一条
**没有回答的提问**，然后把旧问题当新问题再答一遍。

**一张表两个读口**（重要设计）：

- `history()` → 只返回 `final` → **给模型看**（避免模型读到「⚠️ 待人工审批…」这类控制面文本后，反复念叨「还在等批准」）
- `messages()` → 返回全部状态 → **给人看**（`GET /sessions/{id}`）

**审批完成后是「原地改写」而不是追加新行**：`decide()` 之后，那行 `pending_approval` 会被
UPDATE 成最终回答（`status` 变 `final`），`seq` 不变。追加新行会让历史里夹着控制面噪音。

---

## 四、第二组：状态日志

这组表承接的是「以前只进 console / 只随 HTTP 响应返回一次就没了」的数据。
（在引入本功能前，处置结果与 AI 建议**完全不落盘**。）

### `alerts` —— 收到的每一条告警

| 字段 | 类型 | 含义 |
|---|---|---|
| `id` | INTEGER 主键 | 自增 |
| `alert_key` | TEXT | **关联键**，见第二节。三张表靠它串起来 |
| `alertname` | TEXT | 告警名（Prometheus 规则的 `alert` 字段），如 `HighCPUUsage` |
| `severity` | TEXT | 严重级别，如 `critical` / `warning` |
| `instance` | TEXT | 触发告警的实例，如 `192.168.20.128:9100` |
| `summary` | TEXT | 告警摘要（**截断到 500 字符**） |
| `status` | TEXT | `firing`（触发中）或 `resolved`（已解决） |
| `starts_at` | TEXT | 告警**自身**声明的开始时间（来自 Alertmanager） |
| `ends_at` | TEXT | 告警自身的结束时间 |
| `received_at` | TEXT | **本服务收到它的时间**（与上面两个不同——那是告警说的时间） |

**唯一约束** `UNIQUE(alert_key, starts_at, status)`，索引 `idx_alerts_received`。

两点：

- **这个唯一键处理「重投」**：Alertmanager 会重复推送同一条告警，不该堆成多行
  （用 `INSERT OR IGNORE`，重复的直接忽略）。
- **唯一键里带了 `status` 是刻意的**：`firing → resolved` 是同一告警的**两个生命周期事件**，
  两边都要留下。如果只按 `alert_key` 去重（或用 REPLACE），「这条告警曾经 firing 过」的
  痕迹就没了——而那正是事后追查最需要的信息。

### `remediations` —— 规则处置的结果

| 字段 | 类型 | 含义 |
|---|---|---|
| `id` | INTEGER 主键 | 自增 |
| `alert_key` | TEXT | 关联到哪条告警 |
| `alertname` / `severity` / `instance` | TEXT | 冗余存一份，方便单表查询不用 join |
| `matched_rule` | TEXT | 命中了 `config/rules.yaml` 里的哪条规则（值是动作名） |
| `action` | TEXT | 实际执行的动作；**没执行时为 NULL** |
| `status` | TEXT | 见下 |
| `detail` | TEXT | 结果说明（**截断到 500 字符**，见下） |
| `executed_at` | TEXT | 处置时间 |

**`status` 三种取值**：

| 值 | 含义 |
|---|---|
| `ok` | 动作执行成功 |
| `failed` | 动作执行失败（异常信息在 `detail`） |
| `matched_but_auto_execute_disabled` | **命中了规则，但该规则的 `auto_execute: false`，跳过执行** |

第三个值价值很高：在引入本功能前，这种「命中但跳过」的情况**除了日志里一行 warning，
全无痕迹**——你无法事后判断某条规则到底是**没命中**、还是**命中了没执行**。

> ⚠️ **`detail` 为什么要截断**：执行器把远端命令的**整个返回结果**（含 **stdout**）拼进了这个字段。
> 以前它只进 console，所以「整段命令输出不落日志」这条安全约定一直成立；
> **一落盘就变成磁盘上的明文**。所以写入前强制截断。

### `ai_advice` —— AI 的根因分析建议

| 字段 | 类型 | 含义 |
|---|---|---|
| `id` | INTEGER 主键 | 自增 |
| `alert_key` | TEXT | 关联到哪条告警 |
| `alertname` / `severity` / `instance` | TEXT | 冗余存一份 |
| `advice` | TEXT | 模型给出的建议全文（**截断到 4000 字符**） |
| `analyzed_at` | TEXT | 分析时间 |

只有当告警**没有命中任何规则**时才会产生建议——命中规则的走处置链路，不需要 AI 出主意。

**这个字段以前是「响应发完即蒸发」的**：`POST /alert` 的响应体里带回给 Alertmanager，
没人保存。事后想回看「当时 AI 对这条告警说了什么」，完全没有途径。

### `approval_decisions` —— ⚠️ 当前是空表

| 字段 | 类型 | 含义 |
|---|---|---|
| `approval_id` | TEXT | 审批 id |
| `session_id` | TEXT | 属于哪段对话 |
| `action` | TEXT | `approve` / `reject` |
| `decided_at` | TEXT | 决策时间 |

**这张表建好了但没有任何代码往里写**（`git grep approval_decisions src/` 只命中建表那一行）。

它对应一个**尚未修复的风险窗口**：`decide()` 的执行顺序是「先取走待审批项 → 再执行命令」。
进程若恰好死在两者之间，重启后那条消息会被标成 `abandoned`（= 未执行），
**而命令其实可能已经执行过了**——历史表给出的结论是错的，且方向恰好与 fail-safe 相反。

目前唯一的补救是：命令真执行时会写审计日志（`logs/audit.log`），对照它能发现矛盾。
**单看会话表会被误导。** 该遗留已登记在 [PROJECT_HANDOVER.md](../PROJECT_HANDOVER.md) 的路线图。

---

## 五、常见查询

```sql
-- 最近 20 段对话（含命名）
SELECT session_id, user, name, title, updated_at
FROM sessions ORDER BY updated_at DESC LIMIT 20;

-- 某段对话的完整往来（给人看，含待审批行）
SELECT seq, role, status, content FROM messages
WHERE session_id = '把 id 填这里' ORDER BY seq;

-- 只看向模型展示的那部分（只看 final）
SELECT seq, role, content FROM messages
WHERE session_id = '把 id 填这里' AND status = 'final' ORDER BY seq;

-- 还有哪些待审批的轮次（含已作废的）
SELECT session_id, seq, approval_id, status FROM messages
WHERE status <> 'final' ORDER BY created_at DESC;

-- 某条告警的完整档案：告警本身 + 处置 + AI 建议
SELECT * FROM alerts       WHERE alert_key = 'K';
SELECT * FROM remediations WHERE alert_key = 'K';
SELECT * FROM ai_advice    WHERE alert_key = 'K';

-- 最近失败的处置（排查「为什么没修好」）
SELECT alertname, matched_rule, status, detail, executed_at
FROM remediations WHERE status = 'failed' ORDER BY executed_at DESC LIMIT 20;

-- 命中了规则但没执行的（检查 auto_execute 配置是否符合预期）
SELECT alertname, matched_rule, COUNT(*) FROM remediations
WHERE status = 'matched_but_auto_execute_disabled'
GROUP BY alertname, matched_rule;

-- 各类数据量
SELECT 'sessions' t, COUNT(*) n FROM sessions
UNION ALL SELECT 'messages', COUNT(*) FROM messages
UNION ALL SELECT 'alerts', COUNT(*) FROM alerts
UNION ALL SELECT 'remediations', COUNT(*) FROM remediations
UNION ALL SELECT 'ai_advice', COUNT(*) FROM ai_advice;
```

---

## 六、维护须知

### 清理规则

由 [config/settings.yaml](../config/settings.yaml) 的 `storage` 段控制，**写入时惰性触发**：

| 配置键 | 默认 | 含义 |
|---|---|---|
| `session_ttl_hours` | 72 | 会话过期时长（按 `updated_at` 算），过期连同其 `messages` 一并删 |
| `max_sessions` | 200 | 会话数上限，超出淘汰最旧的 |
| `history_ttl_hours` | 168 | 三张归档表的过期时长 |
| `max_history_rows` | 5000 | 三张归档表各自的行数上限 |
| `purge_interval_seconds` | 60 | 清理节流间隔（避免每次写都全表扫） |

> WAL 模式下**删行不会让文件变小**（页面会被复用，不会无限增长）。
> 代码在清理后会低频执行 `wal_checkpoint(TRUNCATE)` 回收空间。

### schema 迁移

改表结构时**必须**：递增 [src/storage/db.py](../src/storage/db.py) 的 `SCHEMA_VERSION`，
并在 `_migrate()` 里补一个 `if current < N:` 分支。规则是**只追加、不改名、不删列**
（别人的 .db 可能停在任意旧版本）。

两条硬性约束（都有测试锁着）：

1. **迁移路径产出的结构必须与全新建库一致**——比较的是**结构指纹**（表名、每表有序的列定义、
   索引名），不是 DDL 文本。`ALTER TABLE` 之后 SQLite 会用自己的格式重写 `sqlite_master.sql`，
   文本永远不可能相同。
2. **新列只能加在末尾**（ALTER 的限制）。所以全新建库的 DDL 也要把新列写在末尾，
   两条路径的列序才对得上。

**版本高于当前代码**（文件来自更新版本的代码）时，程序会记 ERROR 并**拒绝使用**——
绝不写入，以免损坏对方的数据。

### 备份

停掉服务后直接复制 `ops_agent.db` 即可（**运行中复制要连 `-wal` / `-shm` 一起**，
否则可能拿到不一致的快照）。或者用 sqlite 自己的方式：

```bash
sqlite3 data/ops_agent.db ".backup backup.db"
```

### 相关文档

- [README.md](../README.md) —— 项目结构、配置、路由（**权威结构文档**）
- [PROJECT_HANDOVER.md](../PROJECT_HANDOVER.md) —— 开发约束、已踩的坑、待办路线图
