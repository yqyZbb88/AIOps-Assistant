# HTTP API 参考与错误语义

本文按当前 `0.6.2` 代码描述公开接口及 `/console` 使用的契约。精确字段、枚举和交互式
schema 以运行中服务的 `/openapi.json`、`/docs` 为准；这两个文档路由当前公开，部署到
不可信网络时需在反向代理限制。本文示例中的 `node-a`、Token 和告警值是占位符，
不代表真实环境。不要把实际 Token、SSH 目标、数据库内容或审批理由贴进工单。

## 通用规则

- 基址默认是 `http://127.0.0.1:8000`。所有普通业务接口始终需带
  `Authorization: Bearer <Token>`；管理接口始终要求管理员 Token。身份由服务端
  Token 映射决定，请求体中的用户名不可信。
- 普通 JSON 响应经过统一敏感信息脱敏；成功创建/轮换用户 Token 的专用响应仅当次返回明文并禁止缓存。主机 API 会返回 Prometheus `job`/`instance` 以解释
  监控目标，但不会返回 SSH 地址、用户名、密码或密钥路径。
- 多主机模式中的操作目标必须是登记的 `host_id`；请求不能提交自由 SSH 地址、PromQL
  或 Shell。会话一旦创建，其 `host_id` 不可修改。
- HTTP 成功只说明请求处理完毕，不代表 Exporter 或 Prometheus 可达；应同时看返回体的
  `state`、`exporter.state`、`storage` 和执行 `status`。
- 管理配置保存成功后立即切换运行快照。管理员 API 始终要求管理员 Token。

## 端点总览

| 方法 | 路径 | 用途 | 鉴权 |
|---|---|---|---|
| GET | `/health` | 进程与缓存健康快照；不主动访问 Prometheus | 公开 |
| GET | `/ready` | 主动探测 Prometheus；不就绪返回 503 | 公开 |
| GET | `/console`、`/console-assets/console.css`、`/console-assets/console.js` | 公开的无数据页面与同源资源 | 公开 |
| GET | `/hosts`、`/hosts/{host_id}` | 登记主机公开摘要 | 普通 Bearer |
| GET | `/hosts/{host_id}/status` | Exporter、核心指标和活动告警 | 普通 Bearer |
| GET | `/hosts/{host_id}/metrics/{metric_id}` | 指定指标即时样本 | 普通 Bearer |
| GET | `/hosts/{host_id}/metrics/{metric_id}/trend` | 指定时段统计及设备时间序列 | 普通 Bearer |
| GET | `/executions` | 规则/Agent 动作执行记录 | 普通 Bearer |
| POST、GET | `/sessions` | 创建、列出会话 | 普通 Bearer |
| GET、PATCH、DELETE | `/sessions/{session_id}` | 查看、改名、删除会话 | 本人身份 |
| POST | `/chat` | 在既有会话发送一轮消息 | 会话所有者 |
| POST | `/alert` | Alertmanager webhook | 普通 Bearer + `alert_identity` |
| GET | `/alerts/recent`、`/alerts/history`、`/alerts/active` | 内存热缓冲、SQLite 归档、监控源实时告警 | 普通 Bearer |
| GET | `/inspections/recent` | 异常巡检归档 | 普通 Bearer |
| GET、POST | `/approvals`、`/approvals/{approval_id}` | 列待审批项、提交决策 | 按审批模式授权 |
| GET、PUT | `/admin/config` | 脱敏配置草稿、revision 写回 | 始终管理员 Bearer |
| POST | `/admin/config/apply` | 显式应用磁盘 YAML；请求 `{"revision":"..."}` | 始终管理员 Bearer |
| GET | `/admin/hosts/options` | 已登记主机、Prometheus 已发现目标、凭据 ID | 始终管理员 Bearer |
| POST | `/admin/hosts/preview`、`/admin/hosts/ssh-check`、`/admin/hosts/add` | 新主机预览、只认证 SSH 测试与登记 | 始终管理员 Bearer |

`/status` 与 `/status-assets/*` 已删除，访问返回 404；不设置重定向。
业务 API 始终校验身份，鉴权开关已删除。缺少/无效 Token 返回 401，无可用身份配置返回 503。
主机列表仅含授权主机；直接访问未授权主机或其会话统一返回 404。

## 主机、指标和健康状态

`GET /hosts?include_disabled=true` 默认包含禁用主机；每项含 `host_id`、`name`、`enabled`、
`prometheus.job`/`instance`、`ssh_configured`、三类能力白名单和 `legacy`。单个
`GET /hosts/{host_id}` 可查询已登记主机摘要；监控和执行接口要求主机当前启用。

`GET /hosts/{host_id}/status` 返回：

```json
{
  "host": {"host_id": "node-a", "name": "示例主机", "enabled": true},
  "exporter": {"state": "reachable", "value": 1.0},
  "metrics": {"cpu.usage": {"state": "ok", "samples": []}},
  "active_alerts": []
}
```

上例省略 `host` 和 `metrics` 的其余字段。`exporter.state` 可为 `reachable`、
`unreachable`、`no_data`、`ambiguous` 或 `query_error`。若 Exporter 不可达，核心指标
可能为空；`query_error` 代表监控查询失败，不等于目标的 `up=0`。排障先看
[只读排障](troubleshooting.md#prometheus-与-exporter只读分层检查)，未经授权不要启动或重启远端 Exporter。

`GET /hosts/{host_id}/metrics/{metric_id}` 返回 `host_id`、`metric_id`、`state` 与
`samples`，每个样本有 `labels`、`value`、`timestamp`。`/trend` 增加 `minutes`
（1～1440，默认 60）和 `step`（正整数加 `s/m/h/d`，省略时自动计算），返回 `trend`：

```json
{"host_id":"node-a","metric_id":"cpu.usage","state":"ok",
 "trend":{"latest":0.6,"min":0.6,"max":0.6,"avg":0.6,"points":2,
 "state":"ok","step_seconds":15,"series_total":1,"truncated":false,
 "series":[{"labels":{"instance":"node-a:9100"},"values":[[1700000000,0.6],[1700000015,0.6]]}]}}
```

`trend` 保留 `latest/min/max/avg/points`，新增 `series=[{labels,values:[[时间戳,值],...]}]`、
`state`、`step_seconds`、`series_total`、`truncated`。摘要选择方向对应的最差设备序列；
采样按设备独立返回，不做跨挂载点平均。默认步长 `max(15,ceil(minutes*60/240))` 秒，
显式步长只可降低密度，每条最多 241 点、最多 32 条序列。超限 `truncated=true`。
非有限值/缺失点为 `null`；无有效样本 `state=no_data`，无 Swap 为 `not_applicable`。
活动告警历史查询使用 `ALERTS`，按 pending/firing 分线；缺少告警只在 `up=1` 时补零。
即时查询采用同样的补零门控。未知/禁用主机 404，非法参数 422，任何即时或趋势请求失败
均返回脱敏 502。`/ready` 核心依赖不就绪 503；`/health` 不发起外部 I/O。

## 会话、聊天和审批

先创建会话，再聊天：

```http
POST /sessions
Authorization: Bearer <Token>
Content-Type: application/json

{"name":"巡检排查","host_id":"node-a"}
```

创建成功返回 201 与会话元信息，其中 `session_id` 是后续唯一定位键。多主机时必须显式
提供 `host_id`；只有一台获授权且启用的主机时可省略。`GET /sessions?limit=50` 按最后活动时间
倒序列出当前身份仍有主机访问权的会话；`GET /sessions/{id}` 返回元信息及最近 100 条消息的状态。
`PATCH /sessions/{id}` 的 `{"name":"新名称"}` 改标签，空白名清除标签；
`DELETE /sessions/{id}` 删除消息并取消未决审批。删除/越权/未知会话统一 404；
改名后的返回读取若遇到并发删除，同样返回 404。

```http
POST /chat
Authorization: Bearer <Token>
Content-Type: application/json

{"session_id":"<服务端创建的 ID>","message":"概览这台主机的状态"}
```

`/chat` 响应包含 `reply`、`session_id`、`session_name`。当前是完整 JSON 响应，
**不是流式输出**；控制台会先在本地回显用户输入并显示加载动画。消息超过
`chat.max_message_chars` 返回 413。存储关闭或无法验证会话时返回 503，避免无主机范围的聊天回退。启用会话存储时，如果会话在回答期间被删除，
返回 404，不重新创建会话。待审批轮次写入失败会取消本次申请并返回 503；
不能用未保存的审批申请继续执行。普通最终回答遇到其它历史写入失败时仍可返回，历史可能缺失。

`GET /approvals` 返回当前身份可决策的待审批数组，每项包含 `approval_id`、
`session_id`、申请人 `user`、`host_id`、冻结动作 `actions`、目标摘要及配置指纹。
提交 `POST /approvals/{approval_id}`：

```json
{"action":"approve","reason":"已核对主机与动作"}
```

`action` 只可为 `approve`/`reject`，`reason` 最多 1000 字符，只入审计，不改命令。
响应含 `decision`、`status`、`reply` 和可能的 `next_approval_id`；续跑再次申请写命令时
`status=pending_approval`。审批范围取决于 `owner` 或 `approvers_only`；无权、过期、
已处理和未知 ID 对调用者都表现为 404。批准前应核对冻结主机、动作和参数。
启用会话存储且申请绑定会话时，消费前必须核对已保存的待审批轮次；尚未保存或无法核对返回 503，
不会续跑。如果原审批已消费、下一条链式申请却保存失败，会取消下一条并返回 503；
不要重放原决策，应先检查执行记录。

## 告警、巡检和执行历史

`POST /alert` 接收 Alertmanager webhook。请求体为通知对象（含 `status`、`receiver`、
`alerts[]`；每条告警常用 `labels.alertname`、`labels.job`、`labels.instance`、
`startsAt`、`fingerprint`）。返回 `received`、逐条 `alerts`、规则 `remediation`
和未匹配告警的 `ai_advice`。`/alert` 可能触发不经审批的规则自动处置。
始终要求有效 Token，并按 `alert_identity` 限定投递身份；自动执行总开关默认关闭。
归档写入失败返回 503，告警发送方可据此重试；重复写操作由幂等记录保护。

三个告警查询不要混淆：

- `/alerts/recent` 是本进程最近收到的有界热缓冲，重启即清空；支持 `limit`、`host_id`。
- `/alerts/history` 返回 `alerts`、`remediations`、`advice` 三组 SQLite 归档及 `storage`；
  每组最多 `limit` 条（1～500），可按 `host_id` 筛选。
- `/alerts/active` 实时查询监控源当前 pending/firing 状态；监控源不可用返回 502。

`GET /inspections/recent` 只列异常轮次，返回 `storage` 和 `inspections`；每项含
`checked_at`、`level`、`host_id`、`snapshot`、`findings` 与 `error`。正常轮次写巡检
日志而不入表。`GET /executions` 返回 `storage` 和 `executions`，每项含 `channel`
（规则或 Agent）、`host_id`、`action_name`、`approval_id`/`alert_key`、状态与时间戳。
这两类接口均支持 `limit=1..500` 和已启用 `host_id` 筛选；存储关闭时返回空数组与
`storage=off`，不是历史确实不存在的证明。

## 管理配置草稿与错误处理

`GET /admin/config` 返回 `revision`、`active_revision`、`restart_required`、
`available_identities` 以及 `config`。`config` 中只有可编辑的 `settings`、
`automation`、`catalogs`、`hosts[].allow` 和 `rules` 投影；不返回 API Token 明文、
SSH 凭据或主机 SSH 连接字段。`PUT /admin/config` 必须传入原 revision 和**完整**草稿：

```json
{"revision":"<GET 返回的 64 位 revision>","config":{"settings":{},"automation":{},"catalogs":{},"hosts":[],"rules":[]}}
```

上例只示意顶层结构，空对象不是有效候选配置；实际请求应在 GET 返回的 `config` 上
编辑再提交。成功响应仍有快照，并附 `changed_files` 与 `backup_id`。无文件变化时
`changed_files=[]`、`backup_id=null`。有变化时文件会先备份到
`data/config-backups/<backup_id>/`；备份只保护这次 YAML 写回，**不包含 SQLite 数据库**。
成功时 `active_revision=revision` 且 `restart_required=false`。若有未决审批或正在执行的
聊天、审批、告警处置请求，或无法核对未决审批状态，应用返回 409；需重启的字段返回 422 且整批不应用。

主机向导四接口统一要求管理员 Token。预览、SSH 检查和新增均提交
`{"revision":"<当前磁盘 revision>","draft":{"host_id":"node-c","name":"节点 C",...}}`。
外部模式的草稿填写从 `options.targets` 选择的 `job`/`instance`；自管 Compose
模式也可提交 `exporter_address:"主机:端口"`，服务端固定 `job=aiops-hosts`。
可选 `credential_id` 与 `ssh_port` 不允许自由 SSH 地址；若同时选
`diagnostics:["memory"]`，先调用 `ssh-check` 并把返回的短期
`verification_id` 带入新增请求。该检查只校验主机密钥和认证，不执行命令。
成功新增返回 `host_id`、新 revision 和 Exporter 状态；`up=0` 仍可登记，写授权始终为空。
主机管理接口提供新增操作。相同主机内容的重复新增可返回已有结果；
相同 `host_id` 但内容不同，或目标已归属另一主机，返回 409。SSH 检查会建立远程连接，
但不会运行诊断命令；未经授权不要在真实目标上调用。

| 状态码 | 典型原因 | 调用方处理 |
|---|---|---|
| 401 | 缺失/无效 Token | 重新连接，勿在 URL 传 Token |
| 403 | 有效 Token 但无管理员或 webhook 权限 | 核对服务端身份映射，不在浏览器侧绕过 |
| 404 | 主机、会话或审批不可见/不存在 | 刷新列表；审批不要重复提交 |
| 409 | revision 冲突、重复主机内容不同或应用期间有未决/进行中操作 | 重载并核对，等待操作结束 |
| 413 | 聊天消息超过配置上限 | 缩短问题 |
| 422 | 请求字段、命令模板/参数或跨文件引用无效 | 按服务端错误修正草稿 |
| 502 | 监控源查询失败 | 先检查 Prometheus 链路 |
| 503 | 存储或管理配置能力不可用、告警归档失败、readiness 不通过 | 看响应体和服务日志；告警投递可按响应重试 |

应用统一使用脱敏 JSON 响应；但响应可能仍含非凭据的主机名、监控 `instance`、告警
摘要和运维结果，调用方应按内部运维数据保管。`restart_required=true` 表示磁盘
版本尚未应用；管理员需检查是否含需重启字段，再调用 `/admin/config/apply`。
浏览器或 HTTP 客户端超时不能证明服务端已取消写入；重试前先查询 revision 或执行状态。

## 管理员凭据 API

所有端点始终要求管理员 Bearer。元信息查询和保存响应设置 `Cache-Control: no-store`、
`Pragma: no-cache`；仅创建/轮换响应额外含 `token_once`、`token_id`，这是一次性明文出口。
其它查询只含 revision、身份/凭据 ID、用户名/认证方式、模型非敏感参数及 configured/managed 状态。

| 方法 | 路径（前缀 `/admin/credentials`） | 操作 |
|---|---|---|
| GET | 空 | 配置状态及安全元信息 |
| POST | `/tokens` | `data.identity` 生成随机 Token |
| POST | `/tokens/{id}/rotate` | 轮换页面 Token |
| DELETE | `/tokens/{id}` | 撤销页面 Token |
| PUT / DELETE | `/ssh/{id}` | 新增/更新/删除页面 SSH 凭据 |
| PUT | `/hosts/{host_id}/ssh` | `data.credential_id` 更新已有主机关联 |
| POST | `/hosts/{host_id}/ssh/test` | 仅认证连接，不执行命令 |
| PUT | `/model` | 保存 OpenAI 兼容模型 配置，不请求模型 |
| POST | `/model/test` | 显式短请求测试 |

写入 body 为 `{"revision":"64位版本摘要","data":{...}}`，DELETE 也携带 body。
SSH data：username、method=password/private_key、password 或 private_key（上传内容）、known_hosts。
缺省敏感字段表示保留；不接受服务器 key_filename 路径，私钥和 known_hosts 各最多 128 KiB。
凭据 ID 为小写字母开头，含小写字母/数字/下划线/连字符，最长 63 字符；不支持带口令私钥。
模型 data：key_action=keep/replace/clear、api_key、base_url、model、timeout（大于 0 且不超过 300 秒）、
max_retries（0~10）、max_tokens（1~131072）、token_limit_param（max_tokens 或 max_completion_tokens，默认 max_tokens）。replace 必须提供新密钥；clear 覆盖部署来源。
Base URL 只允许无用户信息、查询串、片段的 HTTP(S)。只支持 OpenAI 兼容的 Chat Completions 接口；Base URL 是接口根地址，完整 Agent 需要标准工具调用。
非空密钥要求同时填写 Base URL 和模型名。页面配置整组覆盖环境配置，默认地址和模型名为空。
401 未鉴权、403 非管理员、409 revision/运行操作/审批/凭据引用冲突、422 输入无效或部署只读、
500 持久化失败并回滚、503 配置不可读；模型显式连接测试失败 502。
