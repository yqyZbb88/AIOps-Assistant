# 17 项指标查询对照表

由 `python -m scripts.metric_contract` 从代码目录生成；修改目录或告警后重新生成。

`$job` / `$instance` 是表格和 Grafana 的单选变量。Assistant 始终以登记主机的精确 job/instance 覆盖全局及指标 selector；其它设备 selector 保留。Grafana 不读取 Assistant 的私有 selector，自定义过滤时应在仪表盘副本中同步并注明。

| 指标 | 单位 | 计算窗口 | 即时/趋势统一表达式（下表各入口复用） |
|---|---|---|---|
| cpu.usage | % | 5m | `100 - avg by(instance)(rate(node_cpu_seconds_total{mode="idle",job="$job",instance="$instance"}[5m])) * 100` |
| cpu.load1 | load/core | 1m | `avg by(instance)(node_load1{job="$job",instance="$instance"}) / count by(instance)(node_cpu_seconds_total{mode="idle",job="$job",instance="$instance"})` |
| cpu.iowait | % | 5m | `avg by(instance)(rate(node_cpu_seconds_total{mode="iowait",job="$job",instance="$instance"}[5m])) * 100` |
| memory.usage | % | 当前求值时刻 | `(1 - node_memory_MemAvailable_bytes{job="$job",instance="$instance"} / node_memory_MemTotal_bytes{job="$job",instance="$instance"}) * 100` |
| memory.available | bytes | 当前求值时刻 | `node_memory_MemAvailable_bytes{job="$job",instance="$instance"}` |
| memory.swap_usage | % | 当前求值时刻 | `((1 - node_memory_SwapFree_bytes{job="$job",instance="$instance"} / node_memory_SwapTotal_bytes{job="$job",instance="$instance"}) * 100 and node_memory_SwapTotal_bytes{job="$job",instance="$instance"} > 0) or ((node_memory_SwapTotal_bytes{job="$job",instance="$instance"} == 0) * 0 - 1)` |
| disk.usage | % | 当前求值时刻 | `100 - (node_filesystem_avail_bytes{fstype!~"tmpfs\|overlay",job="$job",instance="$instance"} / node_filesystem_size_bytes{fstype!~"tmpfs\|overlay",job="$job",instance="$instance"}) * 100` |
| disk.read_bytes | MiB/s | 5m | `sum by(instance, device)(rate(node_disk_read_bytes_total{job="$job",instance="$instance"}[5m])) / 1048576` |
| disk.write_bytes | MiB/s | 5m | `sum by(instance, device)(rate(node_disk_written_bytes_total{job="$job",instance="$instance"}[5m])) / 1048576` |
| disk.io_util | % | 5m | `sum by(instance, device)(rate(node_disk_io_time_seconds_total{job="$job",instance="$instance"}[5m])) * 100` |
| network.rx_bytes | MiB/s | 5m | `sum by(instance, device)(rate(node_network_receive_bytes_total{device!="lo",job="$job",instance="$instance"}[5m])) / 1048576` |
| network.tx_bytes | MiB/s | 5m | `sum by(instance, device)(rate(node_network_transmit_bytes_total{device!="lo",job="$job",instance="$instance"}[5m])) / 1048576` |
| network.rx_errors | errors/s | 5m | `sum by(instance, device)(rate(node_network_receive_errs_total{device!="lo",job="$job",instance="$instance"}[5m]))` |
| network.tx_errors | errors/s | 5m | `sum by(instance, device)(rate(node_network_transmit_errs_total{device!="lo",job="$job",instance="$instance"}[5m]))` |
| system.process_count | processes | 当前求值时刻 | `sum by(instance)(node_procs_running{job="$job",instance="$instance"})` |
| system.uptime | seconds | 当前求值时刻 | `time() - node_boot_time_seconds{job="$job",instance="$instance"}` |
| alerts.active_count | alerts | 当前求值时刻 | `label_replace((sum(ALERTS{alertstate="firing",job="$job",instance="$instance"}) or (vector(0) and on() (max(up{job="$job",instance="$instance"}) == 1))), "state", "firing", "", "") or label_replace((sum(ALERTS{alertstate="pending",job="$job",instance="$instance"}) or (vector(0) and on() (max(up{job="$job",instance="$instance"}) == 1))), "state", "pending", "", "")` |

| 使用入口 | 表达式与差异原因 | 时间范围与聚合 |
|---|---|---|
| 主机总览 / 主机详情摘要 | cpu.usage、memory.usage、disk.usage 复用上表 | 即时求值；摘要显示各独立序列最大值，磁盘详情仍保留挂载点 |
| 监控详情即时与趋势 | 全部 17 项复用上表 | 即时与趋势计算窗口相同；趋势 1–1440 分钟，步长自动限点，最多 241 点/序列、32 序列 |
| 收藏卡片 | 所选指标复用上表 | 浏览器选择趋势范围，默认 60 分钟；最多 6 卡，不改变 rate 窗口 |
| AI 工具 / 巡检 | 普通指标及巡检复用上表；AI 的 alerts.active_count 工具实际通过 /api/v1/alerts 列举规则实例 | 即时求值；多设备独立保留；告警列表用于说明规则，不能以空列表推断 up=1 或主机健康 |
| 告警分析 | 已启用 alert_analysis 的指标复用上表 | 趋势范围由 alert.trend_minutes 设置；rate 仍为 5 分钟 |
| Grafana | 全部 17 项复用上表；单选 job/instance；不使用 $__rate_interval | 默认趋势 1 小时，刷新 30 秒；磁盘/网卡分别成线；-1 显示无 Swap、不连接空点 |
| Prometheus 告警规则 | 下表为跨主机规则，无单机 selector | 以 job/instance 保留主机归属；磁盘用最坏挂载点触发单条主机告警，避免重复告警 |

## Prometheus 告警规则

| 告警 | 表达式 | 计算窗口 | 持续时间 |
|---|---|---|---|
| HighCPUUsage | `max by (job, instance) (100 - avg by (job, instance) (rate(node_cpu_seconds_total{mode="idle"}[5m])) * 100) > 80 or on (job, instance) aiops_demo_cpu_percent > 80` | 5m | 30s |
| HighMemoryUsage | `max by (job, instance) ((1 - node_memory_MemAvailable_bytes / node_memory_MemTotal_bytes) * 100) > 85 or on (job, instance) aiops_demo_memory_percent > 85` | 当前求值时刻 | 30s |
| HighDiskUsage | `max by (job, instance) ((1 - node_filesystem_avail_bytes{fstype!~"tmpfs\|overlay"} / node_filesystem_size_bytes{fstype!~"tmpfs\|overlay"}) * 100) > 80 or on (job, instance) aiops_demo_disk_percent > 80` | 当前求值时刻 | 30s |
| NodeExporterDown | `up{job=~"node\|node-exporter\|aiops-hosts"} == 0` | 当前求值时刻 | 30s |

即时查询是在当前时刻求值，不是零窗口采样。rate[5m] 使用此前 5 分钟计数器样本（至少两个）；load1 是 Exporter 的 1 分钟负载除以在线 CPU 序列数；趋势范围、求值步长和告警 for=30s 各自独立。Prometheus 默认抓取/规则求值均 15 秒，file_sd 刷新 30 秒。样本 timestamp 是求值时间，不声称它是原始抓取时间；缺失 gauge 在 lookback 范围内可能使用较旧样本。

零吞吐是有效 0；缺采集器、端口/标签不匹配和采样不足是无数据；HTTP/解析错误是查询失败；SwapTotal=0 使用 -1 哨兵并映射不适用。磁盘使用率排除 tmpfs/overlay，保留 instance/mountpoint/device/fstype；磁盘 IO 按 device 保留全部块设备，无默认设备排除。网络排除 lo，按 instance/device 独立保留。MiB/s=bytes/s÷1048576，磁盘使用率基于 avail（普通用户可用空间）。

Assistant 对目录定义的百分比边界做 0–100 展示保护，并跳过 NaN/Inf；Grafana 保留原始 PromQL 结果供诊断采样越界。两者查询表达式和计算窗口相同，但越界样本的显示可能不同。Assistant 趋势为请求/绘图容量限制最多 32 序列，Grafana 不施加该限制；设备序列不合并。

活动告警计数分 pending/firing 独立保留，仅 up=1 时将空 ALERTS 补零；up=0 或缺失时不补零。所有登记主机计数入口使用相同的 ALERTS 表达式；AI 告警工具、总览/详情的告警明细和旧无目标内部入口使用 /api/v1/alerts 并按精确 job/instance 过滤，目的是列举规则实例，列表本身不门控 up，空列表不等于主机健康。演示指标是告警验收专用 gauge 分支，不进入普通指标面板；真实分支优先且按 job/instance 去重。

默认 CPU/内存/磁盘告警阈值分别为 80/85/80，与指标目录 warning 一致。Prometheus 静态规则不会随用户修改 metrics.yaml 自动调整；自定义阈值需管理员单独同步外部规则。未配置其它 14 项告警规则，不把观察指标冒充已启用告警。Exporter down 规则覆盖 node/node-exporter/aiops-hosts；外部 job 需其管理员维护。

Windows Exporter（常见端口 9182）不提供本目录 node_* 指标，即使 up=1 仍可能无数据；不能通过替换端口猜测协议。核对 Targets 的最终 job、instance、抓取地址与 collector，再登记准确标签。控制台标签提示仅显示当前已授权主机的预期值，不暴露其它目标。

托管 Grafana 仪表盘由指标目录生成，经 provisioning 加载；既有业务仪表盘不在仓库内，查询需部署负责人单独核验。Grafana 具有独立认证和权限，Assistant 的主机 ACL 不会自动传递给 Grafana。
