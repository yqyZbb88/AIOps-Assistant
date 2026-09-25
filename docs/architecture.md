# 当前架构与重构边界

本文描述仓库当前实现，不是目标架构或“已无缺陷”的声明。代码是最终依据；接口字段见
[API 参考](api.md)，配置约束见[配置指南](configuration.md)，操作步骤见
[控制台说明](console.md)。根目录可能存在被忽略的本地架构/交接记录，提交后的共用说明以
本文和上述专题文档为准。

## 运行形态与入口

- 一个 FastAPI 进程、一个 worker、一个 Prometheus/Alertmanager、SQLite。多 worker
  或多个 Assistant 实例共享配置目录不在当前支持范围内。
- `src/main.py:create_app` 负责装配、鉴权路由挂载、请求快照中间件和唯一巡检调度器。
  `uvicorn src.main:app` 会在导入时创建应用；单元测试可显式传入 Settings 和管理器。
- `/console` 是公开静态空壳；业务数据与管理变更都通过同源 API，管理 API 始终校验
  Token→管理员身份。浏览器 `localStorage` 只放显示偏好，Token 仅放当前标签页的
  `sessionStorage`。
- 业务主干是 `api → core → interfaces ← plugins/storage`。`src/container.py`
  负责插件和 Store 的惰性装配；`src/agent/` 只在需要 Agent 时加载重依赖。

## 状态所有权

| 状态 | 当前所有者 | 生命周期与注意点 |
|---|---|---|
| Settings、主机/指标目录、规则、命令目录、Agent | `RuntimeSnapshot` / 新 `Container` | 应用配置后新请求用新版本；已绑定的读请求可用旧版本完成 |
| 数据库连接、会话与历史 Store、执行幂等、最近告警 | 热切换时由旧 `Container` 共享 | 不因配置切换清空；进程重启后的恢复语义以各 Store 为准 |
| 待审批对象与 LangGraph checkpoint | 当前进程内 Agent | 有未决审批时拒绝应用配置；重启后不能续跑旧申请 |
| 巡检线程与最近轮次状态 | 单个 `InspectionScheduler` | 调度器不重建；每轮从当前快照取 checker 和设置 |
| YAML 与 managed Prometheus 目标文件 | 本机磁盘 | revision 是文件内容指纹，不包括环境变量值；写锁仅限本进程 |
| SSH 检查票据 | `HostOnboarding` 内存 | 5 分钟有效，绑定配置 revision 与主机/目标/凭据；重启即失效 |
| 浏览器草稿与偏好 | `src/web/console.js` | 管理草稿按整份配置保存；偏好不是服务端授权依据 |

`src/utils/runtime.py` 用 ContextVar 固定一次 HTTP 请求的快照。普通业务写请求先计入
active operation，配置应用遇到进行中的写请求或未决审批返回 409；管理员写请求由
`AdminConfigManager` 的锁和 `begin_apply` 协调。此机制不是跨进程锁，也不保证外部进程
不改 YAML。不要通过多开 worker 来“提高并发”而绕过这些前提。

## 主要数据流

1. 监控：`hosts.yaml` 显式登记 `host_id → job/instance`，`metrics.yaml` 定义固定指标；
   `src/plugins/monitors/` 查询 Prometheus，`src/core/hosts.py` 组合主机状态。
   API 不接受自由 PromQL。趋势接口只给统计摘要，不给逐点时序。
2. 聊天：会话固定 `host_id` 与所有者；`src/core/chat.py` 调用关键词兜底或 Agent。
   Agent 工具只从受控指标、告警、巡检与该主机获授权的诊断目录构建。诊断目标和
   SSH 凭据由服务端注册表解析，不由模型或浏览器提交。
3. 告警：`POST /alert` 要求 webhook 身份；`src/core/rule_engine.py` 按告警名与唯一
   主机目标匹配。全局自动处置、单规则自动执行、命令启用及主机授权共同决定能否
   走规则写路径；它不经过 Agent 人工审批，必须保留幂等和审计。
4. Agent 写操作：模型只能申请已启用、已授权的命令；审批冻结主机、动作及参数。
   `POST /approvals/{id}` 校验审批身份后续跑，结果写执行记录。审批权限不等于
   管理员权限；不可见审批与未知审批均返回 404。
5. 巡检：单调度线程按主机边界串行运行，受每轮主机数、查询数和时间预算限制；
   正常轮次写巡检日志，异常轮次另入 SQLite。页面上的历史异常不是实时健康判定。

## 配置热更新与主机登记

`src/utils/admin_config.py` 是结构化配置入口，不允许浏览器编辑原始 YAML 或连接凭据。
`GET /admin/config` 返回安全投影、磁盘 revision 和 active revision；`PUT` 需要完整草稿
及所见 revision。服务端以原文件为底稿合并可编辑字段，验证跨文件引用，先构建候选
运行快照，再备份有变化的文件、逐文件原子替换，最后切换快照。写入异常时尝试回滚。
页面外改 YAML 后调用 `POST /admin/config/apply`；不支持热切换的 Settings 差异整批
拒绝，不做部分应用。环境变量中的 Token/密钥、监听地址、SQLite/日志路径仍需重启。

单个文件使用同目录临时文件与 `os.replace`；**多个文件没有跨文件或断电原子性**。
备份仅保护配置保存过程，不是 SQLite 备份或已经演练的恢复方案。外部编辑器或另一
进程绕开进程内写锁时仍可能产生竞态，revision 也无法代替跨进程协调。

`src/utils/host_onboarding.py` 目前只实现新增：外部 Prometheus 只能选已发现的
`job/instance`，managed Compose 可新增 file_sd Exporter 目标。默认监控开启、三类
授权为空；可选 SSH 只从目标实际地址推导并引用已有凭据 ID。SSH 检查仅握手认证，
不执行命令，也不证明 Exporter 可达。`up=0` 可登记，登记不安装/启动远程服务。
目前没有页面删除、停用或迁移主机功能；不要直接删 `hosts.yaml` 中有会话、规则或
历史引用的主机来模拟该功能。

## 改动落点与回归

| 需求 | 优先检查 | 必须保持的边界 |
|---|---|---|
| 新指标 | `src/metrics/`、`metrics.yaml`、监控适配器、巡检/Agent 工具 | 固定目录、selector 与查询预算，不开放自由 PromQL |
| 新命令 | 三个命令目录、`cmddef.py`、主机 allow、规则/审批测试 | 参数正则/整数范围、SSH 主机密钥、写操作审计与幂等 |
| 新主机操作 | `host_onboarding.py`、`hosts_config.py`、admin API 与控制台 | revision、跨引用、默认零授权、历史归属、目标文件回滚 |
| 新 Settings 字段 | `config_loader.py`、`admin_config.py:_candidate`、配置指南 | 明确热应用或重启；不可悄悄改变旧快照 |
| 新 API | `src/main.py` 路由挂载、API 文档与鉴权测试 | 新业务路由显式挂鉴权，管理路由强制管理员 |
| 数据库结构 | `src/storage/db.py`、Store、迁移测试、数据库说明 | schema 版本与旧库迁移一致，不能只改新库 DDL |
| 前端视图 | `console.html/js/css` 与 UI/路由测试 | 安全 DOM、响应式和键盘操作、Token 不持久化 |

先用离线测试固定现有行为，再拆模块；每次修改做针对性回归，阶段完成再跑全量。
完整验证命令见 [贡献约定](../CONTRIBUTING.md)。

## 已确认的限制与建议重构顺序

以下是代码和接口可以直接核对的限制，不等于已复现的缺陷清单：

- `console.js` 同时持有 API 请求、页面状态、DOM 渲染、管理员草稿和主机向导；
  `admin_config.py` 同时做脱敏投影、输入合并、跨文件校验、备份与热切换。
  这两个文件是后续改动最容易互相影响的集中点。
- 浏览器没有真正的聊天流式响应，只做立即回显和等待动画；请求超时不证明服务端
  已取消处理。对审批、保存等有副作用的请求，应先刷新状态再决定是否重试。
- 管理配置的冲突保护和写锁只覆盖单进程。多文件写入、进程崩溃和断电恢复没有
  事务保证；SQLite 并发负载与备份恢复尚未完成正式验收。
- 主机向导只增不删；已有主机是否允许停用、如何保留历史归属和清理 file_sd 目标，
  尚无完整产品语义，不能仅靠前端增加“删除”按钮。
- 未决审批在进程内，热应用会整体被挡住；进程重启后旧 checkpoint 不可续跑。
  扩成多 worker 前必须先设计共享状态、租约和幂等边界。

建议按以下顺序重构，而不是一次性替换框架或数据库：

1. 把用户发现的每个 bug 固定为最小复现、预期/实际结果和离线回归；先分清
   展示错误、API 契约错误、跨主机归属错误及写操作安全错误。真实主机只读检查，
   未经授权不启动、重启或修改远端服务。
2. 为配置保存、主机新增、审批续跑建立故障注入与并发测试；先确保失败时不会
   扩大授权、混用新旧快照或重复执行，再拆 `admin_config.py` 的投影、校验、
   持久化和快照构建职责。
3. 把控制台的请求/状态、通用安全渲染、各视图与管理表单分开，维持现有同源 API
   和无持久 Token 规则；拆分过程中保持键盘、窄屏和失败提示的测试覆盖。
4. 明确主机生命周期（停用优先还是删除、历史数据如何查询、目标文件如何清理）
   后再实现 UI/API；随后做 SQLite 并发、备份恢复和干净环境部署验收。

这些工作尚未在本次文档整理中实现。发现新问题时先更新对应测试和此处的边界，
避免把计划误写成已经上线的能力。
