# AIOps-Assistant 鉴权与密钥

本文说明如何生成、配置、使用和轮换 AIOps-Assistant 的鉴权密钥。快速部署流程见 [guide.md](guide.md)；YAML 字段见 [configuration.md](configuration.md)。

## 一、先看结论

最少需要配置：

```env
OPS_API_TOKENS={"k-admin-随机值":"admin","k-am-随机值":"alertmanager"}
```

启用鉴权后，建议配置审批人：

```env
OPS_APPROVER_IDENTITIES=["admin"]
```

`config/settings.yaml` 对应配置：

```yaml
auth:
  enabled: true
  tokens: '${OPS_API_TOKENS:-}'
  approval_mode: approvers_only
  approver_identities: '${OPS_APPROVER_IDENTITIES:-["admin"]}'
  alert_identity: alertmanager
```

修改后必须重启服务。

## 二、项目中涉及哪些密钥

| 名称 | 用途 | 存放位置 | 谁需要 |
|---|---|---|---|
| `OPS_API_TOKENS` | API Key 到身份的映射 | `.env` | 人、脚本、Alertmanager |
| `OPS_APPROVER_IDENTITIES` | 审批人身份列表 | `.env` | 服务端 |
| `SSH_KEY_FILENAME` / `SSH_PASSWORD` | SSH 私钥路径或登录密码 | `.env` | 服务连接目标主机 |
| `DEEPSEEK_API_KEY` | 模型调用 | `.env` | 服务端 |
| `OPS_REDACTION_SECRETS` | 额外精确脱敏值的 JSON 数组 | `.env` | 服务端 |
| Alertmanager 凭据文件 | webhook Bearer Token | 独立文件 | Alertmanager |

只有 API Token 和 Alertmanager webhook Token 用于“谁在调用本服务”。其它密钥用于本服务连接外部系统。

## 三、生成 API 密钥

使用随机值：

```bash
python -c "import secrets; print(secrets.token_urlsafe(32))"
```

不要使用：

- 用户名或密码
- 时间和 UUID
- 可读单词
- 多个用户共享的长期固定字符串

建议每个用户、每个脚本和 Alertmanager 各自使用独立密钥，方便单独吊销和审计。

## 四、配置 API 鉴权

### 1. 写入环境变量

`OPS_API_TOKENS` 是 JSON 对象：

```json
{"k-admin-随机值":"admin","k-am-随机值":"alertmanager"}
```

- JSON key 是密钥，客户端放入 `Authorization: Bearer <key>`。
- JSON value 是身份名，服务端据此判断会话和审批归属。
- key 和 value 不能为空。
- 身份名大小写敏感。

不要把 key 和 value 写反。通常密钥较长且随机，身份名较短且可读。

### 2. 配置审批模式

有两种模式：

| `approval_mode` | 谁可以审批 |
|---|---|
| `owner` | **只有触发人自己**。此模式**不看** `approver_identities`，列在里面的身份也批不了别人的申请 |
| `approvers_only` | `approver_identities` 中的身份可以审批**任何人**触发的申请 |

注意 `approvers_only` 的语义是「审批人是**岗位**，不是**当事人本人**」：`admin` 既能
列出也能批准 `zhang` 触发的写命令。**同一份判定同时决定「看得见」与「批得动」**
（`src/interfaces/approval.py` 的 `may_decide`），所以 `GET /approvals` 的列表范围和
`POST /approvals/{id}` 的授权范围永远一致；范围外的 id 一律返回 404，与「不存在」同形。

推荐使用 `approvers_only`：

```env
OPS_APPROVER_IDENTITIES=["admin","audit"]
```

如果 `approvers_only` 模式没有配置审批人，服务会拒绝启动。

### 3. 配置 Alertmanager 身份

```yaml
auth:
  alert_identity: alertmanager
```

只有身份名为 `alertmanager` 的 API Token 可以调用 `POST /alert`。

如果该字段留空：

- 所有有效 API Token 都可以调用 `/alert`
- 启动时会产生 WARNING
- 不建议在生产环境使用

### 4. 重启服务

鉴权配置不会热更新。修改 `.env` 或 `settings.yaml` 后必须重启。

## 五、如何使用 API

### 命令行

```bash
curl -H "Authorization: Bearer <密钥>"   http://127.0.0.1:8000/sessions
```

### Python

```python
import httpx

response = httpx.get(
    "http://127.0.0.1:8000/sessions",
    headers={"Authorization": "Bearer <密钥>"},
)
```

### Swagger

打开：

```text
http://127.0.0.1:8000/docs
```

点击右上角 Authorize，填入密钥。

不要把密钥放到 URL 查询参数中。

## 六、身份会影响哪些行为

| 接口/行为 | 身份规则 |
|---|---|
| `POST /sessions` | 新会话记录为当前身份 |
| `GET /sessions` | 只返回自己的会话 |
| `GET/PATCH/DELETE /sessions/{id}` | 只能操作自己的会话；越权返回 404 |
| `POST /chat` | 只能使用自己的会话 |
| `GET /approvals` | 只看得到可见范围内的：`owner` = 自己触发的；`approvers_only` = 审批人看到全部 |
| `POST /approvals/{id}` | 同上；范围外返回 404（与「未知 id」同形） |
| `POST /alert` | 身份必须匹配 `auth.alert_identity` |

身份名只用于归属和授权，不用于选择模型、主机或命令。

## 七、认证与授权状态码

| 情况 | 状态码 |
|---|---|
| 缺少 Bearer Token | 401 |
| Token 无效 | 401 |
| Token 有效但无权调用 `/alert` | 403 |
| Token 有效但访问别人的会话 | 404 |
| Token 有效但审批范围外的申请 | 404（`owner` 下批别人的，或 `approvers_only` 下不在 `approver_identities` 里） |
| 鉴权配置启用但没有有效 Token | 服务拒绝启动 |

`GET /health`、`GET /ready`、`GET /status`、状态页同源静态资源，以及 `/docs`、
`/redoc`、`/openapi.json` 不需要 Bearer Token。前三类只返回无敏感信息的探针或 HTML/CSS/JS
空壳；状态页调用 `/alerts/*`、`/inspections/recent`、`/approvals` 时仍必须携带 Token。

这里的“应用路由”特指 FastAPI 自动生成的文档/OpenAPI 路由；它们不属于项目挂载的
业务 router，因此不会经过 `require_auth`。如需隐藏接口结构，应在反向代理层限制
`/docs`、`/redoc` 和 `/openapi.json`，或在后续版本中显式关闭这些路由。

## 八、Alertmanager webhook 配置

### 1. 准备专用 Token

在 `OPS_API_TOKENS` 中增加 Alertmanager 身份：

```json
{"k-am-随机值":"alertmanager"}
```

### 2. 写入凭据文件

Bash：

```bash
printf '%s' '<k-am-随机值>' > /path/to/aiops_assistant_token
```

PowerShell：

```powershell
Set-Content -NoNewline -Path <路径> -Value '<k-am-随机值>'
```

凭据文件中只放密钥本身：

- 不要写 `Bearer`
- 不要加引号
- 不要写身份名

### 3. 配置 Alertmanager

```yaml
receivers:
  - name: aiops-assistant
    webhook_configs:
      - url: http://127.0.0.1:8000/alert
        send_resolved: true
        http_config:
          authorization:
            type: Bearer
            credentials_file: /path/to/aiops_assistant_token
```

推荐使用 `credentials_file`，避免把密钥直接写入 Alertmanager 配置。

### 4. 验证

不要只检查 Alertmanager 进程是否启动。启动成功不代表 webhook 能发送。

1. 重启 Alertmanager。
2. 手工推送一条测试告警。
3. 查询 `GET /alerts/recent` 或 `GET /alerts/history`。
4. 确认测试告警已入库。

如果服务端没有收到，优先查看 Alertmanager 自己的日志。

## 九、轮换和吊销

推荐顺序：

1. 新增一条密钥到 `OPS_API_TOKENS`。
2. 重启服务。
3. 客户端切换到新密钥。
4. 确认业务正常。
5. 删除旧密钥。
6. 再次重启。

修改密钥后没有热更新，必须重启。

应用会在写入和输出边界遮蔽当前 API Token、DeepSeek key、SSH 密码以及
`OPS_REDACTION_SECRETS` 中长度足够的值，但不会自动改写已有数据库和历史日志。轮换曾经使用
过的密钥，并按保留策略安全处理旧文件；不要把空值或短公共字符串加入额外脱敏列表。

## 十、历史会话和身份

开启鉴权后，会话只对 `sessions.user` 与身份相同的用户可见。

如果历史会话的 `user` 没有对应 Token，会表现为：

- 会话列表里看不到
- 按 id 查询返回 404

需要保留历史会话时，把某个 Token 映射到原来的身份名。

查询现有身份名：

```bash
sqlite3 data/aiops_assistant.db   "SELECT DISTINCT user, COUNT(*) FROM sessions GROUP BY user;"
```

## 十一、常见问题

| 现象 | 检查 |
|---|---|
| 启动失败，提示鉴权配置错误 | `auth.enabled=true` 但 Token 为空或 JSON 非法 |
| 请求返回 401 | 缺少 Token、Token 写错或不是 Bearer |
| `/alert` 返回 403 | Token 身份与 `auth.alert_identity` 不一致 |
| Alertmanager 没有任何告警入库 | webhook URL、凭据文件、Alertmanager 日志 |
| 开启鉴权后会话看不到 | 当前身份与历史 `sessions.user` 不一致 |
| 修改 `.env` 不生效 | 没有重启服务 |

## 十二、安全检查清单

- 使用 `secrets.token_urlsafe(32)` 生成密钥
- 每个调用方使用独立 Token
- Alertmanager 使用专用 Token
- 多人环境使用 `approvers_only`
- 不把密钥写入 URL、日志、截图或工单
- `.env` 和凭据文件不进入 Git
- 定期轮换和吊销密钥
- 不在没有鉴权的情况下监听 `0.0.0.0`

## 相关文档

- [guide.md](guide.md)：快速开始和日常使用
- [configuration.md](configuration.md)：配置字段
- [database.md](database.md)：会话身份和数据库维护
