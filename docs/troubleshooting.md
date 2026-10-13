# 只读排障与证据收集

遇到问题，先找到下面最接近的现象，再按顺序检查。先保留日志和数据，查清原因后再改配置。
看到主机不可达，不代表服务器已经关机，也不需要立即重启它。

| 遇到什么问题 | 从哪里开始 |
|---|---|
| 项目启动失败、网页打不开 | [部署失败排查](deployment.md#部署失败排查) |
| Assistant 提示 Token 无效 | [Token 返回 401](#控制台-token-返回-401) |
| Grafana 用户名密码无效 | [Grafana 登录排查](deployment.md#grafana-登录失败与旧数据卷) |
| 主机没有指标或趋势 | [监控检查](#prometheus-与-exporter只读分层检查) |
| SSH 登录失败或公钥不匹配 | [SSH 检查](#ssh-连接与-known_hosts-错误) |
| AI 连接失败或不调用工具 | [模型检查](#模型连接或工具调用失败) |
| 配置无法保存 | [保存失败](#管理配置保存失败) |

涉及重启、远程命令或修改配置时，先确认有相应权限，并选对目标主机。

## 先区分服务、依赖与数据状态

| 现象 | 先看哪里 | 通常意味着什么 |
|---|---|---|
| `/health` 不能连接 | Assistant 进程、监听地址、端口、本机防火墙 | HTTP 服务本身不可达 |
| `/health` 可读而 `/ready` 为 503 | `components.prometheus`、`components.storage`、`components.inspection` | 依赖探测或巡检调度不就绪 |
| `/ready` 正常而单主机 `exporter.state=unreachable` | 该主机在 Prometheus 的精确 `job`/`instance` 目标 | Prometheus 在最近抓取中得到 `up=0` |
| 指标 `state=no_data` | 指标名、查询时间范围、实际采集标签 | 查询有效但没有样本，不能直接等同于 `up=0` |
| 指标 `query_error` 或 API 502 | Prometheus 查询错误和服务端日志 | 查询失败，不等于目标真实下线 |

`/health` 是缓存/进程快照，不主动访问外部系统；`/ready` 主动检查 Prometheus，并核对启用的存储与巡检。两者
不是彼此的替代。单主机状态还应看 `exporter.state`，而不是只看页面顶部“已连接”。

## 控制台 Token 返回 401

Docker 启动、端口、挂载与初始化失败先查 [部署排障](deployment.md#部署失败排查)。
Compose 实际 YAML 位于 `.runtime/config/`，不要修改根 `config/` 后误以为容器会生效。

先确认访问的 Compose 项目名、发布端口和运行目录，再显式查看对应管理员 Token：

```bash
docker compose run --rm --no-deps init show-token
```

只将 Token 输入连接框，不粘贴到工单、截图或日志。页面 Token 还应核对轮换/撤销状态。
部署凭据修改后需重建 init 与 Assistant；不要直接改派生文件；鉴权开关已删除，连接问题应检查 Token 和身份映射。

## Prometheus 与 Exporter：只读分层检查

1. 在本机读取 `/health` 与 `/ready`，记录时间、HTTP 状态和 `components`。不要把
   Bearer Token 写进截图或日志。
2. 使用有权限的 Token 读取 `/hosts` 和目标 `/hosts/{host_id}/status`，核对登记的
   `host_id`、Prometheus `job`/`instance`、Exporter 状态。单主机状态中的地址是监控
   标签，不是 SSH 执行目标。
3. 在 Prometheus 自身的 Targets 页面或只读 API 查询同一 `job`/`instance` 的目标，
   记录 scrape 状态、last scrape、last error 与 `up`。`up=0` 说明最近抓取失败；
   没有 `up` 序列更可能是目标未登记、标签不一致或时间范围问题。
4. 从 Prometheus 所在网络位置检查到 Exporter 端口的连通性。仅当组织许可时，
   再读取 Exporter 的 `/metrics`；本机可达不等于 Prometheus 容器也可达。
5. 对照 `deploy/prometheus/prometheus.yml` 与 `hosts.yaml` 的 `job`/`instance`，
   但不要为了“让图变绿”直接改生产配置；先确认哪个才是真实目标。

Prometheus 查询示意（占位标签，替换为已登记目标；命令本身只读）：

```bash
curl --get --data-urlencode 'query=up{job="node",instance="example.invalid:9100"}' \
  http://127.0.0.1:9090/api/v1/query
```

若是在 Compose 内运行，`127.0.0.1` 的含义随所在容器变化；优先使用部署文件中的
Prometheus 服务地址。请勿在公开频道粘贴完整 `/metrics`、Targets 页面或真实内网地址。

## 告警收到但没有动作

先按顺序核对，不要直接打开自动处置：

1. Alertmanager 是否向 `/alert` 发送成功？业务接口始终鉴权，未带 Token 是 401，
   身份不匹配 `alert_identity` 是 403；只看告警列表不足以诊断发送失败。
   归档失败时接口返回 503，应检查 SQLite 与 Alertmanager 重试记录。
2. `/alerts/recent` 是重启即清空的内存热缓冲；`/alerts/history` 是 SQLite 归档。
   二者都没有时再看 Alertmanager 日志和 webhook 网络。
3. 规则只按 `alertname` 精确匹配，`enabled=false` 不参与；命中但
   `auto_execute=false` 或顶层 `auto_execute_enabled=false` 会审计但不执行。
4. 即使规则允许自动执行，也必须唯一解析到已登记主机，告警 `instance` 与规则目标及
   主机监控目标一致，主机还需在 `allow.rule_actions` 授权动作并有 SSH 配置。
5. 重复事件可能被幂等守卫标为 `duplicate_ignored`；执行记录要看
   `/executions` 的 `host_id`、`channel`、`status` 和时间戳。

规则自动处置**不经人工审批**；Agent 写命令则必须审批。两条路径的目录和授权不可混用。

## 问答、诊断与审批

### SSH 连接与 known_hosts 错误

先确认“系统管理 → SSH 凭据”中对应主机关联的是哪个凭据，目标地址和 SSH 端口
来自登记主机，不来自聊天文本。第一次准备材料见
[获取与核验 known_hosts](deployment.md#获取并核验-ssh-known_hosts)。

| 错误现象 | 先检查 | 处理方法 |
|---|---|---|
| 保存提示 known_hosts 格式无效 | 是否粘贴完整“地址 类型 Base64”行 | 不填 SHA256 指纹、路径、用户公钥、终端 banner；重新复制已核验原文 |
| 主机不在 known_hosts / unknown host key | 实际连接 IP/域名是否存在于记录中 | IP/域名须匹配；核验对应地址后补充记录，不能自动信任未知密钥 |
| 非 22 端口无法匹配 | 地址是否为 [主机]:端口，主机配置端口是否一致 | 重新扫描正确 SSH 端口，或从可信公钥生成正确记录 |
| host key mismatch / bad host key | 主机是否重装、地址是否指错或密钥是否轮换 | 通过可信控制台重新核验，再更新凭据；不直接删除旧记录绕过校验 |
| Authentication failed | Linux 用户、密码/用户私钥与远端 SSH 策略 | 不使用 Assistant Token 或 Grafana 密码；上传用户私钥，不上传 .pub；当前不支持带口令私钥 |
| Connection refused / timeout | SSH 服务、实际端口、路由及安全组 | 由目标维护者检查服务，从 Assistant 容器所在网络验证；个人电脑能连不代表容器能连 |
| 连接成功但诊断不可用 | 聊天 Agent、目录、命令和主机白名单 | 按 [首次诊断](guide.md#第一次执行只读诊断) 配置；认证测试本身不授予权限 |

不要把“保存 SSH 凭据成功”当作认证通过，也不要把“认证通过”当作命令已执行。
共享凭据更新会影响多个主机；非默认端口登记错误时，不能只换密码解决。

### 模型连接或工具调用失败

1. 确认使用同一部署的管理员 Token，在“模型配置”查看是否已配置页面来源。
   页面来源优先于环境；清除页面密钥后不会自动回退 .env。
2. 保存时选择正确密钥操作，核对接口根地址、实际模型名与 max_tokens/
   max_completion_tokens；不要重复添加 /chat/completions。
3. 点击“测试已保存的模型连接”。失败常见原因是密钥无效、网络/代理不通、模型名不存在、
   服务不接受所选输出参数；检查模型服务状态和 Assistant 日志，不公开密钥。
4. 文本连接通过后，在“安全与开关”开启聊天 Agent 并保存；告警 AI 需要 LLM 分析开关。
   再在有真实指标的主机会话请求查询 CPU/内存，检查是否依据工具事实。
5. 文本正常但工具失败，向服务提供方确认该模型支持标准工具调用；接口“兼容”或能回答 OK
   不等于支持完整工具回合。SSH 工具还需要各自凭据、目录和主机白名单。

请求超时可能涉及多回合及重试；页面没有流式输出。重试写动作前核对会话与执行记录。
一次失败不代表已保存配置被自动清除，不通过反复保存密钥来猜测服务状态。
Grafana 的 invalid/401 使用 [登录与旧数据卷排障](deployment.md#grafana-登录失败与旧数据卷)。

### 问答与审批的其它现象

关键词“状态”返回固定模拟文本，不是健康检查；请以主机指标、Exporter 与探针为依据。

- 聊天接口当前非流式。控制台先回显本地用户输入并显示加载动画，若最终超时或失败会
  在仍选中原会话且输入框为空时恢复输入；切换会话或身份后，旧请求不会覆盖当前草稿。
  不要把加载动画视为模型已经执行了某项操作。
- 诊断工具不可用时，检查 `diagnostics.yaml` 目录开关、命令 `enabled`、主机
  `allow.diagnostics`、Paramiko 依赖、SSH 主机密钥和连接超时。页面不提供自由命令
  测试口；不要用临时任意 SSH 命令绕过审计。
  `output_limit` 对两流各自按字符截断，并以其四倍作为共享原始字节预算；
  预算耗尽后继续排空但不再保留，
  命令截止时间仍生效。截断不能代替命令范围限制，目录不应加入无界或长时间命令。
- 待审批列表为空可能是没有申请、申请已过期、当前身份不是审批人、Agent 写目录关闭，
  或进程重启导致内存 checkpoint 消失。查看会话消息状态和审计记录再判断。
- 审批返回 404 可能是 ID 不存在、已处理/过期，或当前身份无权查看；不要根据 404
  猜测他人的审批是否存在。若记录是 `unknown`，远端命令可能已经执行，先对账。
- 审批返回 503 时，先核对会话轮次是否已保存、SQLite 是否可用。链式审批中若下一轮
  保存失败，原申请已消费，不能重放原 ID；先对照执行记录确认已完成的动作。

## 管理配置保存失败

| 响应 | 检查 | 安全处理 |
|---|---|---|
| 401/403 | Token 是否有效、身份是否列入 `admin_identities` | 使用已有管理员身份；不要在客户端绕过 |
| 409 | 磁盘 revision 已变，或存在进行中的请求/审批、审批状态无法确认 | 按错误提示重新加载或等待当前操作完成；不要覆盖外部修改 |
| 422 | 字段类型、模板占位符、参数约束或命令引用 | 修复草稿中所有引用后再一次保存 |
| 503 | 配置文件读取或 `ruamel.yaml` 不可用 | 检查部署依赖、权限和路径 |
| 500 | 写入失败且已尝试回滚 | 停止继续编辑，核对原文件、备份与服务日志 |

配置备份在 `data/config-backups/<UTC 时间>/`，只用于本次 YAML 写回失败保护。
Compose 中该目录位于部署目录的 `assistant-data/config-backups/` 下。
它不是 SQLite 备份，也没有自动恢复按钮。每文件替换是原子的，跨文件批次不是断电
事务；出现异常时先复制现状和日志做证据，再按维护流程恢复。管理页保存成功后，
可热更新的字段立即生效；监听、SQLite/日志路径和环境变量密钥变更需要重启。

## SQLite 与日志

- `/health` 中 `storage=degraded`、会话接口 503 或历史接口异常时，先查看磁盘空间、
  文件权限、同步盘/外部 SQLite 浏览器是否持有文件。不要在服务运行中删除 `-wal`
  或 `-shm`，也不要只复制主 `.db` 文件当备份。
- 审批已决策但执行状态 `unknown` 与“未执行”不同，先对照 `logs/audit.log`、
  `action_executions` 和目标主机状态；不能靠重复 POST 来碰运气。
- 审计日志为 JSON Lines，巡检日志是面向人的分组文本；两者职责不同。日志可能轮转，
  持久记录也有 TTL，重要事件应按组织保留策略导出并限制访问。
- SQLite 备份/恢复步骤见 [database.md](database.md)。恢复前先验证备份文件可读，
  并记录恢复时间与操作结果。

## 提交排障报告时至少记录

记录时间和时区、服务版本、受影响的 `host_id`、API 路径与 HTTP 状态、
`/ready` 组件状态、Prometheus 对应目标的抓取结果、相关告警/执行 ID，以及已经采取的
**只读**检查。不要附 Token、SSH 地址/密码/私钥路径、完整命令输出、真实内网拓扑或
未经脱敏的数据库文件。最后清楚标注：是否需要远程变更、准备变更哪台主机、影响范围
和回退方案；未获授权前停在证据汇报阶段。

## 磁盘、网络趋势查询与配置入口

管理员 Token 连接后，“系统管理”显示 SSH 凭据、用户 Token、模型配置。缺少入口先核对
Token 身份是否在管理员名单及当前镜像是否包含这些页面；普通用户不显示管理入口。
没有专门 range 表达式的磁盘/网络指标复用即时表达式；若返回“不支持该查询”，核对
实际部署镜像与运行源码。即时与趋势分别请求对应 API，502 表示 Prometheus 请求失败，
no_data 表示正常空结果；核对目标 job/instance、采集器是否启用及 rate 是否有至少两次采样。
容器 local-node 的磁盘/网卡范围不同于 Linux 宿主机；独立宿主机监控见部署专题。
无 Swap 显示不适用；零吞吐有有效 0 点；断点表示采样缺失或非法值，不能当连续直线。

### 所有指标都显示趋势无数据

先分别检查即时接口和趋势接口；`趋势：该时间范围内暂无数据` 表示没有有效采样，
不能据此判断只有趋势查询故障。打开 Prometheus 的 Targets 页面，核对该主机的
**最终 `job`、`instance` 标签**与主机登记完全一致，再检查抓取状态是否为 `up`。
`instance` 包含端口，`:9090` 和 `:9100` 属于不同目标。Linux Node Exporter 默认
监听 `9100`；`9090` 通常是 Prometheus 自身端口。采集配置指向错误端口、或者登记
保留旧目标标签，会让即时与趋势查询同时返回空结果。

修正采集地址后检查并重新加载 Prometheus；首次 rate 指标需要至少两次采样，
历史缺口不会自动补回。修改前保留原配置和回退步骤。

当前目录仍是 Linux `node_*` 指标；登记 Windows Exporter 目标不会自动获得
`windows_*` 指标适配。Windows 目标应与 Linux 目标分开验收；不要把 Linux 指标
在 Windows 主机上的空结果当作全部 PromQL 失效。Compose 内置 Linux 节点应使用
`job=node-exporter`、`instance=node-exporter:9100`，并从实际部署的持久化配置确认
是否仍保留旧主机登记。
