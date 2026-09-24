# 配置热更新与快捷新增监控节点：实施草案

状态：待实施。本文记录 2026-09-24 对现有代码的调查和建议顺序，不表示这些能力已经上线。
继续使用 SQLite 与单 worker；节点接入不自动安装、启动或修改远程 Exporter/SSH 服务。

## 当前边界

- `PUT /admin/config` 只写磁盘、备份并标记 `restart_required`。运行中的 `Container` 缓存
  Settings、主机注册表、指标目录、规则执行器、Agent 和巡检调度器。
- `hosts.yaml` 登记 Assistant 的主机 ID、Prometheus 标签、SSH 凭据引用和逐主机授权。
  登记主机不会让 Prometheus 开始抓取。开发栈的 `deploy/prometheus/prometheus.yml`
  当前只抓取内置 `node-exporter:9100`。
- 未决 Agent 审批和 checkpoint 在进程内。直接替换 Agent 会丢失续跑上下文；写动作的目标
  与参数还受冻结校验约束。
- 当前管理接口刻意不返回 Token、SSH 地址或 Prometheus 连接目标。节点向导应作为单独的
  管理员操作，不能把这些字段塞进原有通用配置草稿。

## 第一阶段：配置保存与运行态切换

1. 定义单一 `RuntimeSnapshot`：版本号、Settings、主机与指标注册表、规则/动作/诊断目录、
   Agent 工厂及服务引用。每个请求开始时读取同一个快照引用；进行中的请求用旧快照完成。
2. 保留现有 `PUT /admin/config` 的磁盘 revision、校验与备份。新增
   `POST /admin/config/apply`，携带期望磁盘 revision 与期望 active revision。
   服务端在锁内重新读取、校验并**先构建完整候选快照**，成功后一次切换引用；
   构建失败则保留旧运行态，并显示磁盘配置仍待应用。
3. 将巡检调度器的停启与快照切换合并为受控步骤，避免双调度器并跑或请求读到混合配置。
   配置变更时提供应用结果、错误字段、active/disk revision 和时间；审计操作者与变更文件。
4. 第一版先热应用主机列表/授权、规则与三个命令目录。改变 Agent 或写能力前，
   若有未决审批则返回 409，待审批处理完或过期后再应用；不得悄悄丢弃 checkpoint。
   第二步再覆盖 `settings.yaml` 中可安全切换的鉴权/功能开关和巡检周期，并把
   `metrics.yaml` 纳入 revision 与候选快照，支持阈值热更新。新旧管理员身份切换时
   必须保证新快照仍有有效管理员；浏览器遇到 401 应重新认证。
   进程监听地址、SQLite 路径、日志文件位置和来自环境变量的密钥仍标记“需重启”。
5. 管理页增加“保存”“应用已保存配置”和失败回退提示。保存成功不等于运行态已应用；
   应用失败时保留已有服务和备份，不自动扩大授权。

验收：成功切换后新请求读取同一 revision；旧请求完成但不混用规则/主机；错误配置不改变
active revision；并发保存返回 409；巡检只有一个调度器；未决审批阻止相关切换；
鉴权、自动处置默认关闭和逐主机白名单保持不变。

## 第二阶段：快捷新增节点监控

1. 管理员向导先提供“选择 Prometheus 已发现目标”：读取 Prometheus Targets，
   选择其实际 `job`/`instance`，填写稳定 `host_id`、显示名；先不配置 SSH，
   三类写授权保持空。预览检查 ID、标签唯一性、目标是否被抓取及最近 `up`。
2. 对项目自管的 Compose Prometheus，改用 `file_sd_configs` 管理新增 Exporter 目标：
   Assistant 只写专用 targets 文件，Prometheus 只读挂载同一目录。
   目标文件经校验后原子替换；Prometheus 会监视文件并重新读取目标组，
   无需开放 `/-/reload`。外部 Prometheus 不由 Assistant 修改，向导只允许登记已经
   出现在 Targets 中的目标，并说明缺少抓取配置时需要由其管理员处理。
3. 新节点写入 `hosts.yaml` 时沿用 revision、备份、跨文件校验和热应用机制。
   使用幂等请求避免重复主机；若 Prometheus 目标已加入而 Assistant 登记失败，
   页面明确显示部分完成状态并提供安全回滚。禁用节点优先于删除节点，保留历史记录。
4. SSH 诊断作为独立可选步骤：仅引用服务端已有凭据 ID；新凭据走单独受控入口，
   不在列表/响应中回显密码、密钥路径或已登记主机的连接地址。新主机的诊断和写命令
   白名单默认全空；诊断授权由管理员逐项开启，写动作仍遵守人工审批。

验收：新增目标在 Prometheus Targets 可见，抓取成功/失败均有明确状态；`up=0`
时只报告证据，不远程启动 Exporter；节点登记后会话、指标、告警、巡检均正确归属；
重复 ID、重复 instance、非法地址、文件写入失败和热应用失败可恢复；真实双主机隔离
与完整离线回归继续通过。

Prometheus 的文件服务发现支持监视目标文件和原子重命名，格式及行为见
[官方配置文档](https://prometheus.io/docs/prometheus/latest/configuration/configuration/#file_sd_config)。
