# AIOps-Assistant 鉴权与密钥

## 一、凭据来源与首次连接

新容器部署自动生成管理员和 Alertmanager 专用 Token，保存在
`.runtime/compose.env`，初始化派生服务专用文件。管理员显式查看：

```bash
docker compose run --rm --no-deps init show-token
```

浏览器只将 Token 输入连接框；不粘贴到工单、截图或日志。根 `.env` 提供 Compose
参数和可选模型/SSH 配置，不能替代持久化业务身份。应用不自动加载宿主机 .env。

运行 `.runtime/config/settings.yaml` 使用环境占位，密钥不写 YAML：

```yaml
auth:
  tokens: '${OPS_API_TOKENS}'
  approval_mode: approvers_only
  approver_identities: '${OPS_APPROVER_IDENTITIES}'
  admin_identities: '${OPS_ADMIN_IDENTITIES}'
  alert_identity: alertmanager
```

全部业务 API 始终验证 Token 并解析身份，没有可用 Token 的容器拒绝启动。
鉴权没有关闭开关；`auth.enabled` 已删除，init 会备份后移除旧字段。

## 二、项目中涉及哪些密钥

| 名称 | 用途 | 权威来源 |
|---|---|---|
| OPS_API_TOKENS | Token → 身份映射 | compose.env，合并页面用户 Token |
| OPS_ADMIN_IDENTITIES / OPS_APPROVER_IDENTITIES | 管理员与审批人身份名单 | 部署凭据或管理页角色配置 |
| SSH 私钥/密码和 known_hosts | 连接并验证目标主机 | 页面凭据目录或部署环境/ssh 目录 |
| DEEPSEEK_API_KEY | 模型调用 | 页面模型配置或部署环境 |
| OPS_REDACTION_SECRETS | 额外精确脱敏值，JSON 数组 | 部署环境 |
| Alertmanager Token 文件 | webhook Bearer Token | init 从部署映射派生 |

页面凭据保存在 assistant-data/admin-credentials/，包含 state.json 与 SSH assets。
密钥不进入普通配置备份或数据库历史。停机备份和恢复该目录时同时保留资产与状态，
通过受控渠道交付，不提交版本库。页面配置优先使用，未配置时使用部署来源。

## 三、页面用户 Token

管理员在“系统管理 → 用户 Token”按身份名生成随机 Token，可轮换或撤销页面管理项。
成功创建或轮换仅显示一次明文；响应禁止缓存，浏览器不持久化该值，离开页面清空展示。
请当次妥善记录。部署管理员和 webhook Token 只读保留，不能从页面删除或覆盖。

页面 Token 与部署映射合并。身份名大小写敏感且不能空白；角色继续由管理员/审批人名单
授权，生成 Token 本身不授予管理或审批权限。页面保存有 revision 校验；存在写操作或
未决审批时返回 409。撤销影响后续请求，不删除该身份历史会话。

## 四、配置 API 鉴权

### 1. 映射与角色

部署映射是 JSON 对象，key 为随机 Token，value 为身份名：

```json
{"随机管理员Token":"admin","随机webhookToken":"alertmanager"}
```

不要颠倒二者。更新部署身份时，在停止服务或受控维护窗口编辑 compose.env，
保留合法引号格式及管理员、webhook 映射；不能将它作为 Compose --env-file 使用。
页面创建一般用户优先使用上一节入口。

### 2. 配置审批模式

| approval_mode | 谁可见且可审批 |
|---|---|
| owner | 仅触发人；不使用 approver_identities |
| approvers_only | 审批名单中的身份可以处理任何人的申请 |

可见范围和决定权限使用同一判定；范围外 ID 返回 404。管理员名单与审批名单独立，
管理员不自动成为审批人。名单中的身份需要有可用 Token 映射，页面保存会验证。
逐主机 SSH 权限、命令目录和审批仍各自生效。

### 3. Alertmanager 身份

auth.alert_identity 默认 alertmanager；POST /alert 要求匹配该身份。
留空会允许任何有效身份推送告警，可能触发无需人工审批的规则自动处置，应保留专用身份。
Compose 初始化自动配置 /run/alertmanager-secrets/token，部署的 Alertmanager 通过
credentials_file 读取；该文件只含 Token，无 Bearer 前缀、引号或身份名。

### 4. 保存与生效

管理页角色、用户主机授权与页面 Token 可热生效；手动 YAML 修改需“应用磁盘配置”。
部署凭据变更需要重建，以重新派生文件并注入进程：

```bash
docker compose up -d --force-recreate --wait --wait-timeout 180
```

单独 restart 不会重建派生凭据；不要直接编辑派生文件。

## 五、如何使用 API

```bash
curl -H "Authorization: Bearer <Token>" http://127.0.0.1:8000/hosts
```

PowerShell 使用 curl.exe，或 Invoke-RestMethod 的 Headers 参数。Swagger /docs 中点击
Authorize，输入 Token 后调用 API。公开路径及错误契约见 [API 参考](api.md)。
控制台静态壳不含业务数据；/docs、/redoc、/openapi.json 是公开文档路由，
需要隐藏接口结构时在反向代理限制。

## 六、身份与错误

会话和聊天按服务端 Token 身份归属，不能在请求体自报身份。自己且仍有主机访问权的会话可读改删，
他人的会话和范围外审批返回 404。缺少或无效 Token 返回 401；
有身份但无管理/webhook 权限返回 403。身份不选择模型或任意 SSH 目标。

审批对象和 checkpoint 在进程内，重启不能续跑旧申请；会话与审批决策可持久化。
撤销身份 Token 后历史仍在，重新给同名身份创建 Token，并恢复对应主机授权后，可继续访问保留期内会话。

## 七、轮换和吊销

页面 Token 使用轮换/撤销入口，即时影响后续请求，明文仍只显示一次。
部署管理员轮换需同时更新映射与 AIOPS_USER_TOKEN，保持其映射到管理员名单身份，
重建后验证新 Token 再移除旧值。操作前保留受限备份。

Alertmanager 使用部署映射中第一个 alertmanager Token；轮换时把替代值放在旧值之前，
重建、验证真实告警投递后移除旧值。Grafana 已初始化密码在 Grafana 中修改，
改初始化密码文件不会重置 Grafana 数据库。

当前 API Token、模型 key、SSH 密码及额外脱敏值在写入/输出边界被遮蔽；
不会自动改写历史数据库和日志。短公共字符串、空值不应加入额外脱敏列表。
模型/SSH 凭据不是本服务用户登录 Token，不能互换。

## 八、验证与常见问题

| 现象 | 检查 |
|---|---|
| 401 | 对应栈的 Token、撤销/轮换状态、Authorization Bearer 格式 |
| 管理页 403 | 身份是否在管理员名单 |
| 审批看不到 | owner / approvers_only 模式及审批名单 |
| webhook 403 | Token 映射是否与 alert_identity 相同 |
| 修改部署凭据未生效 | 是否重建 init/assistant，是否误改派生文件 |
| 失去管理员入口 | 恢复受限部署凭据备份，不能删除运行目录重新初始化 |

验收使用独立栈，验证管理员权限、明文一次性、撤销和重建恢复、旧管理员及 webhook
继续有效。真实模型/SSH 只由显式测试按钮或有授权的业务触发。
完整步骤见 [容器测试与验收](testing.md)，配置字段见 [配置指南](configuration.md)。

## 九、用户主机访问权限与会话隔离

管理员先在“用户 Token”创建身份，再到“用户主机授权”勾选主机，点击顶部“保存并生效”。
权限使用稳定身份名，不使用 Token 字符串。普通用户默认无主机权限；管理员隐式可访问所有已登记主机，
但不能读取、使用、改名或删除其他用户的会话。审批角色与管理员角色分别授权。

磁盘字段示例（JSON 字符串，主机 ID 必须已登记，不支持通配符）：

```yaml
auth:
  host_access: '{"alice":["node-a"],"bob":["node-b"]}'
```

主机列表、即时/趋势指标、实时/历史告警、巡检、执行记录都在后端过滤；无 host_id 的查询也只合并授权主机。
无法解析到已登记主机的历史告警仅管理员可见。会话列表同时按用户和主机权限过滤，聊天锁定会话主机。
访问权限不会授予 SSH 凭据、诊断白名单、写命令白名单、自动执行或审批能力。

撤销某主机授权后，后续请求不能继续使用该主机的旧会话，历史数据保留；重新授权可恢复。
令牌撤销后身份认证失败；同名身份恢复时保留原 ACL。撤销管理员角色时其显式 ACL 才开始决定主机范围。
审批列表与决策同时校验决策人和申请人的当前主机权限。存在未决审批或正在执行的写业务时，
配置/凭据保存返回 409：先处理、取消会话审批或等申请过期，再撤权。不要把 409 当作撤权成功。
已绑定旧快照的读取请求可完成，保存成功后新请求使用新快照；单 worker 边界不变。

旧容器曾因 auth.enabled=false 跳过 Token 解析，令所有请求 user=None，造成会话隔离失效。
现在 HTTP 始终解析身份，已删除旧开关。已有匿名会话不能可靠推断原创建者，因此默认不可访问，
不提供自动认领。本次维护经用户明确授权，备份后仅清除了确认的 3 个匿名会话及关联消息/决策，
未清空其他业务历史。备份含敏感数据，只保存在受限运行目录，不提交仓库。


身份与角色名称按大小写精确匹配，例如 Alice 与 alice 不共享会话、主机授权或审批角色。
管理配置保存允许保留原有失效审批身份，避免撤销 Token 后或旧名单残留时阻塞其他配置；
新增审批身份仍需匹配有效 Token，管理员身份校验保持严格。
控制台显示失效角色项供管理员显式移除，系统不会自动改写大小写或转移角色。
