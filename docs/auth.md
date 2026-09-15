# 鉴权与密钥

> **本文是「密钥怎么来、放哪、怎么用、怎么排查」的权威文档。**
> [README](../README.md) 的「安全说明」只保留**残余风险摘要**，细节一律指向这里——
> 同一件事在两处各写一遍，正是本仓库反复出现的「两处对不上」问题类型。

---

## 一、这个项目里有哪些密钥

| 名称 | 用途 | 存放位置 | 谁需要它 |
|---|---|---|---|
| **`OPS_API_TOKENS`** | **API 鉴权**：密钥 → 身份 | `.env` | **每个调用方各一份**（人 / 脚本 / Alertmanager） |
| `SSH_PASSWORD_192_168_20_128` | SSH 登录密码。规则自动处置、只读诊断、Agent 写命令**三处共用** | `.env` | 服务自身（`config/*.yaml` 里以 `${...}` 占位） |
| `DEEPSEEK_API_KEY` | 模型调用（根 `init.py` 读取） | `.env` | 服务自身 |
| `LANGSMITH_API_KEY` | LangChain 链路追踪（**可选**，不配只是不追踪） | `.env` | 服务自身 |
| **Alertmanager 凭据文件** | webhook 的 Bearer 令牌 | **一个独立文件**（不在仓库里） | Alertmanager 进程 |

只有第一项和最后一项属于「**鉴权**」——它们回答「**谁在调我**」。
中间三项是「**我拿什么去连别人**」，不参与本服务的准入判断。

---

## 二、通用纪律（先读这一节）

1. **密钥只进 `.env` / 环境变量。** 代码、`config/*.yaml`、文档、注释里一律不落明文——
   `config/` 下只允许 `${VAR}` 占位符，真实值由 `.env` 在加载时插值。
2. **`.env` 不入库。** `.gitignore` 已忽略 `.env` / `*.local.yaml` / `data/` / `logs/`。
   提交前确认 `git status --short` 干净，且 `git ls-files` 里没有 `.env`。
3. **不给别人看、不贴进聊天/工单/截图。** 一条密钥等价于那个身份的账号密码。
4. **本文档不写任何真实值**，只写形态与命令。

---

## 三、API 鉴权：`OPS_API_TOKENS`

### 3.1 它解决什么问题

没有鉴权时，任何能访问该端口的人都能：读**所有**对话原文、删别人的会话、
**批准待执行的写命令**、构造一条告警让规则引擎去执行写命令。
`server.host` 默认 `127.0.0.1` 是当时唯一的缓解。

### 3.2 为什么是「密钥 → 身份」，而不是用户名 + 密码

因为**自报的用户名无从核实**。改造前 `user` 是请求体里的一个字段，客户端说自己是
谁就是谁——基于它做的任何校验都只是装饰。现在的模型是：

```
密钥（不可猜的随机串） ──→  身份（服务端事实，"你是谁"由密钥推出）
```

请求体里**没有**用户名这个字段，服务端也不接受客户端指定身份。

### 3.3 生成密钥

**没有签发机制——密钥是你自己造的随机串。** 用系统密码学随机源：

```bash
python -c "import secrets; print(secrets.token_urlsafe(32))"
```

> 别用 `uuid4()`、时间戳、可读单词、或任何你能想出来的东西——它等价于账号密码，
> 而攻击者不需要猜中，只需要一个能基线比对的目标。`token_urlsafe(32)` 约 43 个字符。

### 3.4 写进 `.env`

`OPS_API_TOKENS` 是一个 **JSON 对象**：**键 = 密钥**，**值 = 身份名**。

```jsonc
OPS_API_TOKENS={"k-alice-9f3a…": "alice", "k-bob-71cd…": "bob", "k-am-3e77…": "alertmanager"}
```

| 部分 | 是什么 | 举例 |
|---|---|---|
| **键** | **密钥本身**。持有者放进 `Authorization: Bearer <这个>` | `k-alice-9f3a…` |
| **值** | **身份名**。服务端用它判会话归属，只是个标签 | `alice` |

> ⚠️ **键值别拿反**——这是最常见的错误。判断方法：**长的那串是密钥**，
> 短的可读名字是身份。

对应的 `config/settings.yaml` 长这样（**出厂就是配好的，通常不用动**）：

```yaml
auth:
  enabled: false              # 总开关。false = 无身份、不做会话归属校验，行为与改造前一致
  tokens: '${OPS_API_TOKENS:-}'
```

> 那对**单引号不能省**。不加引号时，变量为空会被 YAML 解析成 `null`、变量有值会被
> 当成**流式映射**解析成 dict——两种都会让启动校验失败。这是实测踩过的坑。

### 3.5 怎么用

```bash
# 命令行
curl -H "Authorization: Bearer <你的密钥>" http://127.0.0.1:8000/sessions
```

```python
# Python
import httpx
r = httpx.get("http://127.0.0.1:8000/sessions",
              headers={"Authorization": "Bearer <你的密钥>"})
```

**Swagger UI**：打开 <http://127.0.0.1:8000/docs> → 右上角 **Authorize** → 粘贴密钥
→ 之后页面上所有请求自动带上，不用逐个填。

> 密钥**不要写进 URL 查询串**（会进日志、进浏览器历史、进 Referer）。只走
> `Authorization` 头。

### 3.6 身份名影响什么

身份名是**会话归属**的判据，别的什么都不是：

| 端点 | 身份名的作用 |
|---|---|
| `POST /sessions` | 新会话的 `user` 字段记成它——**创建者由密钥推出，客户端指定不了** |
| `GET /sessions` | 只列出 `user` 与身份**精确匹配**的会话 |
| `GET/PATCH/DELETE /sessions/{id}` | `user` 不匹配 → **404**（与「未知 id」同形，不泄露 id 是否存在） |
| `POST /chat` | 会话 `user` 不匹配 → 404 |
| **`POST /alert`** | **要求身份是 `auth.alert_identity`**（见 3.9），其他有效密钥 → **403** |
| **`/approvals`** | 默认只看得到、也只批得动自己触发的项；`approvers_only` 模式下只对显式审批人可见/可批 |
| **`POST /approvals/{id}`** | 决策**记名**：往审计日志写一条 `approval_decision`，`decided_by` 就是这里的身份名。鉴权关闭时为空串 |
| `/alerts/*` | 只校验「密钥有效」，不看身份名（三个都是只读端点） |

> **身份名是精确串、大小写敏感**，不做任何归一化（`Alice` ≠ `alice`）。
> 一个身份可以对应**多条**密钥（比如一个人换了新密钥还没删旧的）。

### 3.6.1 审批职责分离

默认 `auth.approval_mode: owner` 保持旧行为：待审批项只对它自己的触发者可见、可批。
多用户生产环境建议改成：

```yaml
auth:
  approval_mode: approvers_only
  approver_identities: '["alice", "audit-bot"]'
```

对应环境变量可写 `OPS_APPROVER_IDENTITIES=["alice","audit-bot"]`。
此时只有列出的身份能查看和批准待审批项，触发者本人不能批准自己的写命令。
`approvers_only` 模式若审批人数组为空，服务会拒绝启动。

### 3.7 新增 / 轮换 / 吊销

`OPS_API_TOKENS` 是**全量快照**——改完 `.env` 需要**重启服务**才生效（配置不是热更新的）。

| 想做什么 | 怎么做 |
|---|---|
| 给某人开一个身份 | 生成一条密钥，加进 JSON：`{"新密钥":"新身份"}` |
| 同一个人换密钥 | 先加新的（两条并存，服务端同时认），对方换完再删旧的 |
| 吊销某人的访问 | 从 JSON 里删掉那条，重启 |
| 停用整个鉴权 | `settings.yaml` 的 `auth.enabled: false`，重启 |

> 轮换之所以能「先加后删」，是因为查找是**遍历全部密钥**、谁匹配算谁——
> 不存在「一个身份只能有一条密钥」的限制。

### 3.8 行为细节（写代码/排查时会用到）

| 情形 | 行为 |
|---|---|
| `auth.enabled: false` | 不校验。所有接口开放，`user` 也无从谈起 → **不做会话归属校验**（与改造前逐字一致） |
| 密钥缺失 / 不是 Bearer / 不匹配 | **401** + `WWW-Authenticate: Bearer` 质询头 |
| `auth.enabled: true` 但解析不出任何密钥 | **拒绝启动**（绝不「空密钥 = 放行」） |
| 配了密钥但 `enabled: false` | 能启动，但启动日志里有一条 **WARNING**（提醒你其实没受保护） |
| `GET /health` | **豁免**，不需要密钥（给探针用） |
| `/docs` `/redoc` `/openapi.json` | FastAPI 自建的 app 级路由，**本来就不在任何 router 里**，不受影响 |
| 别人会话的 `id` 存在与否 | 都无法区分，一律 **404** |
| 密钥有效但身份不是 `auth.alert_identity`，且调的是 `/alert` | **403**（已认证、无权限——与会话那边的 404 有意区分） |
| 密钥有效但不是当前审批策略允许的人，调 `/approvals` | 默认模式是触发者本人；`approvers_only` 模式是非审批人。均看不到且不能决策 → **404** |
| 待审批项的 `user` 为空（鉴权关闭时产生的） | 开启鉴权后**任何身份都批不了**——它挂到 TTL 淘汰、命令永不执行（fail-safe 的方向） |
| 密钥含非 ASCII 字符 | 不会 500——比对前统一编码过 |

比对用 `hmac.compare_digest` 并**遍历全部密钥不提前返回**，避免用响应时间泄露
「猜的密钥前缀对不对」。

### 3.9 `/alert` 的身份白名单（授权，不只是认证）

**`POST /alert` 和别的端点不一样：它要求身份。** 光有「有效密钥」不够。

```yaml
# config/settings.yaml
auth:
  alert_identity: alertmanager    # 只有这个身份能调 /alert；留空 = 不限制
```

| 调用者 | 结果 |
|---|---|
| 无凭据 | **401**（没认证） |
| `alice` 的有效密钥 | **403**（认证过了，但身份不对） |
| `alertmanager` 的密钥 | 200 |
| `alert_identity` 留空 | 任何有效密钥都 200（**有意的 fail-open**，启动时有一条 WARNING） |
| 鉴权关闭 | 200（也没有身份可言，行为与改造前一致） |

**为什么单单这个端点要设限**：`/alert` 不是只读的——它把告警喂给 `RuleEngine`，
而 **`auto_execute: true` 的规则会直接执行写命令、不经人工审批**。不设限的话，
任何持有效密钥的聊天用户都能构造一条告警去踩那条路径，等于从「聊天用户」越权到
「可触发写命令」。（出厂 4 条规则全是 `auto_execute: false`，所以现在是惰性的——
但改一条就活了，而那正是 README 说的启用方式。）

**为什么这里是 403 而会话那边是 404**：会话那边要藏「这个 id 存不存在」，所以统一
404；这里调用者已经通过鉴权了，只是没权限——「认证过但被拒」在语义上就是 403。

**同组的其他路由不受影响**：`/alerts/recent`、`/alerts/active`、`/alerts/history`
仍是「任意有效密钥」——它们是只读的。

### 3.10 故障排查

| 现象 | 多半是 |
|---|---|
| 启动直接失败，日志说「鉴权配置有误，拒绝启动」 | `enabled: true` 但没配密钥，或 `OPS_API_TOKENS` 不是合法 JSON |
| 启动失败，日志说 JSON 解析错误 | `settings.yaml` 里 `tokens` 的**单引号丢了**，或 `.env` 里 JSON 写错（缺少引号/花括号） |
| 所有请求 401 | 忘了带 `Authorization` 头；或带了但前缀不是 `Bearer `（注意有个空格） |
| 只有 `/health` 能通 | 这是**正确行为**，它本来就是唯一豁免的 |
| 能登录但**看不到自己刚建的会话** | 身份名与建会话时的不一致（换了密钥/身份名）。身份是精确匹配，改过就找不回来了 |
| **`/alert` 返回 403** | 你的密钥映射到的身份 ≠ `auth.alert_identity`。要么换用 webhook 那条密钥，要么把配置改成你的身份名 |
| 启动日志出现 `alert_identity 为空` 的 WARNING | `/alert` 对**任何**有效密钥开放——这不是错误，是提醒你还没设限 |
| **告警一条都收不到** | 见第四节——`/alert` 也要求密钥，去 **Alertmanager 的日志**里找 `Notify attempt failed` |

---

## 四、Alertmanager 侧：webhook 也要密钥

`POST /alert` 同样受鉴权保护（否则任何人都能构造告警让规则引擎执行写命令）。

> **推荐顺序：先配好 Alertmanager、确认告警照常入库，最后才打开 `auth.enabled`。**
> 鉴权关闭时服务端会**忽略**那个头，所以这个顺序是**零停机、可随时回退**的。

### 4.1 给 webhook 一条专用密钥

**别跟人共用。** `alertmanager.yml` 常被提交进运维仓库或打进镜像，泄漏面比服务自己的
`.env` 大得多；专用令牌泄漏只丢一个告警入口，不会连带丢掉别人的对话。

```jsonc
OPS_API_TOKENS={"k-alice-9f3a…": "alice", "k-am-3e77…": "alertmanager"}
//                                              ↑ webhook 专用身份
```

⚠️ **这个名字必须和 `settings.yaml` 的 `auth.alert_identity` 一致**——`/alert` 只认
那个身份（见 3.9），名字对不上会直接 **403**，而表现是「告警一条都收不到」。
两处一处是 `.env` 的**值**、一处是 `settings.yaml` 的字面量，改名字时**两处都要改**。

### 4.2 把同一个令牌写进凭据文件

Alertmanager 从**文件**读令牌（而不是写死在配置里）：

```bash
printf '%s' '<上面那条 k-am-… 密钥>' > 'D:/Prometheus/alertmanager-0.33.1.windows-amd64/ops_agent_token'
```

PowerShell 用 `Set-Content -NoNewline -Path <路径> -Value '<密钥>'`。

- 文件里**只放密钥本身**：不要 `Bearer ` 前缀、不要引号、不要身份名。
- **尾随换行会被 Alertmanager 自动去掉**（实测），怎么存都行。
- 路径随便放，只要配置文件指到同一处。

### 4.3 在 receiver 上加 `http_config.authorization`

```yaml
receivers:
  - name: 'web.hook'
    webhook_configs:
      - url: 'http://127.0.0.1:8000/alert'
        send_resolved: true
        http_config:
          authorization:
            type: Bearer
            credentials_file: 'D:/Prometheus/alertmanager-0.33.1.windows-amd64/ops_agent_token'
```

用 `credentials_file` 而不是内联 `credentials:`——后者等于把密钥又抄一份进那个
**常被提交进仓库**的文件里；而 `credentials_file` 指向的文件可以单独设权限。

### 4.4 验证（**不能省**）

> ⚠️ **实测（alertmanager 0.33.1）：凭据文件缺失或路径写错时，Alertmanager 照常启动、
> Web UI 一切正常**，只是通知一条都发不出去——只在**它自己的日志**里留一条
> `level=WARN ... "Notify attempt failed, will retry later"`，服务端侧完全无感。
> 所以「进程起来了」**不等于**配好了。

```bash
# ① 重启 Alertmanager 后，手动推一条告警进去
curl -s -X POST localhost:9093/api/v2/alerts -H 'Content-Type: application/json' \
     -d '[{"labels":{"alertname":"AuthCheck","severity":"warning"},"startsAt":"2026-09-13T00:00:00Z"}]'

# ② 确认服务端收到了（auth 还没开时不需要凭据）
curl -s localhost:8000/alerts/recent

# ③ 看到 AuthCheck 之后，再把 auth.enabled 改成 true 并重启
```

第 ② 步没收到 → 去 **Alertmanager 的日志**（不是你服务的日志）找 `Notify attempt failed`。

---

## 五、其余密钥（不参与鉴权）

| 密钥 | 怎么来 | 配在哪 |
|---|---|---|
| `DEEPSEEK_API_KEY` | DeepSeek 控制台申请 | `.env`，根 `init.py` 读取（**唯一模型入口**） |
| `DEEPSEEK_BASE_URL` | 非密钥，API 地址 | `.env` |
| `SSH_PASSWORD_*` | 目标主机的登录密码 | `.env`；`config/rules.yaml` / `diagnostics.yaml` / `agent_actions.yaml` 里以 `${SSH_PASSWORD_<IP>}` 占位。命名规则：**点换成下划线**，`192.168.20.128` → `SSH_PASSWORD_192_168_20_128` |
| `LANGSMITH_API_KEY` | LangSmith 控制台 | `.env`，可选。**仓库代码不直接读**，由 langchain 自行拾取 |

> SSH 密码变量**未设置时插值为空串**——连接会在认证阶段失败，**不会误连到别的机器**。

---

## 六、升级须知：开启鉴权后存量会话会「消失」

会话归属取「**一律拒绝**」：`user` 与身份**精确匹配**才算你的，`user` 为空的行
**任何人都不匹配**。所以打开 `auth.enabled` 的那一刻，库里 `user` 不在你的密钥映射里的
历史会话会立刻访问不到（列表里没有，按 id 也是 404）。

**要保住某段对话，把一条密钥映射到它原本的 `user` 值上：**

```jsonc
// .env —— 这样 yqyZ 那段历史重新可见
OPS_API_TOKENS={"k-你的随机值…": "yqyZ"}
```

查库里有哪些 `user` 值（只读，不暴露消息内容）：

```bash
python -c "import sqlite3;print(sqlite3.connect('data/ops_agent.db').execute('SELECT DISTINCT user, COUNT(*) FROM sessions GROUP BY 1').fetchall())"
```

---

## 七、速查

```bash
# 看谁批准过写命令（审计日志，与命令执行记录同文件）
grep approval_decision logs/audit.log

# 造密钥
python -c "import secrets; print(secrets.token_urlsafe(32))"

# 带密钥请求
curl -H "Authorization: Bearer <密钥>" http://127.0.0.1:8000/sessions

# 确认服务当前是否需要鉴权（401 = 需要）
curl -s -o /dev/null -w "%{http_code}\n" http://127.0.0.1:8000/sessions

# 确认 /health 始终豁免
curl -s http://127.0.0.1:8000/health
```

**相关**：[README](../README.md)（安全说明 / 出厂开关一览 / 路由表）·
[PROJECT_HANDOVER](../PROJECT_HANDOVER.md)（硬性约束 / 坑表 / 路线图）·
[docs/database.md](database.md)（`sessions.user` 字段的说明）
