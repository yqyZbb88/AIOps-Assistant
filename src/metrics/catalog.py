"""内置 Prometheus/node_exporter 指标目录（中性代码目录）。

PromQL 只在本模块维护；用户 YAML 只能选择指标、阈值、instance 和受支持 selector。
"""
from __future__ import annotations

from src.interfaces.metric import MetricDefinition

# __SELECTORS__ 会由 PrometheusMonitor 替换成经过转义的标签 matcher 片段。
CPU_USAGE = MetricDefinition(
    metric_id="cpu.usage",
    display_name="CPU 使用率",
    group="CPU",
    unit="%",
    kind="percentage",
    direction="above",
    expr_template=(
        '100 - avg by(instance)(rate(node_cpu_seconds_total{mode="idle"__SELECTOR_COMMA__}[5m])) * 100'
    ),
    range_expr_template=(
        '100 - avg by(instance)(rate(node_cpu_seconds_total{mode="idle"__SELECTOR_COMMA__}[5m])) * 100'
    ),
    group_by=("instance",),
    aliases=("CPU", "cpu", "处理器"),
    legacy_tool_name="cpu_usage",
    min_value=0,
    max_value=100,
    default_warning=80,
    default_critical=90,
    default_chat=True,
    default_alert_analysis=True,
)

CPU_LOAD1 = MetricDefinition(
    metric_id="cpu.load1",
    display_name="每核 1 分钟负载",
    group="CPU",
    unit="load/core",
    kind="gauge",
    direction="above",
    expr_template=(
        'avg by(instance)(node_load1{__SELECTORS__}) / '
        'count by(instance)(node_cpu_seconds_total{mode="idle"__SELECTOR_COMMA__})'
    ),
    range_expr_template=(
        'avg by(instance)(node_load1{__SELECTORS__}) / '
        'count by(instance)(node_cpu_seconds_total{mode="idle"__SELECTOR_COMMA__})'
    ),
    group_by=("instance",),
    aliases=("负载", "load1", "系统负载"),
)

CPU_IOWAIT = MetricDefinition(
    metric_id="cpu.iowait",
    display_name="CPU iowait",
    group="CPU",
    unit="%",
    kind="percentage",
    direction="above",
    expr_template=(
        'avg by(instance)(rate(node_cpu_seconds_total{mode="iowait"__SELECTOR_COMMA__}[5m])) * 100'
    ),
    range_expr_template=(
        'avg by(instance)(rate(node_cpu_seconds_total{mode="iowait"__SELECTOR_COMMA__}[5m])) * 100'
    ),
    group_by=("instance",),
    aliases=("iowait", "IO等待"),
    min_value=0,
    max_value=100,
)

MEMORY_USAGE = MetricDefinition(
    metric_id="memory.usage",
    display_name="内存使用率",
    group="Memory",
    unit="%",
    kind="percentage",
    direction="above",
    expr_template=(
        '(1 - node_memory_MemAvailable_bytes{__SELECTORS__} / '
        'node_memory_MemTotal_bytes{__SELECTORS__}) * 100'
    ),
    range_expr_template=(
        '(1 - node_memory_MemAvailable_bytes{__SELECTORS__} / '
        'node_memory_MemTotal_bytes{__SELECTORS__}) * 100'
    ),
    group_by=("instance",),
    aliases=("内存", "memory"),
    legacy_tool_name="memory_usage",
    min_value=0,
    max_value=100,
    default_warning=85,
    default_critical=95,
    default_chat=True,
    default_alert_analysis=True,
)

MEMORY_AVAILABLE = MetricDefinition(
    metric_id="memory.available",
    display_name="可用内存",
    group="Memory",
    unit="bytes",
    kind="gauge",
    direction="below",
    expr_template='node_memory_MemAvailable_bytes{__SELECTORS__}',
    range_expr_template='node_memory_MemAvailable_bytes{__SELECTORS__}',
    group_by=("instance",),
    aliases=("可用内存", "memory available"),
)

MEMORY_SWAP_USAGE = MetricDefinition(
    metric_id="memory.swap_usage",
    display_name="Swap 使用率",
    group="Memory",
    unit="%",
    kind="percentage",
    direction="above",
    expr_template='((1 - node_memory_SwapFree_bytes{__SELECTORS__} / node_memory_SwapTotal_bytes{__SELECTORS__}) * 100 and node_memory_SwapTotal_bytes{__SELECTORS__} > 0) or ((node_memory_SwapTotal_bytes{__SELECTORS__} == 0) * 0 - 1)',
    range_expr_template='((1 - node_memory_SwapFree_bytes{__SELECTORS__} / node_memory_SwapTotal_bytes{__SELECTORS__}) * 100 and node_memory_SwapTotal_bytes{__SELECTORS__} > 0) or ((node_memory_SwapTotal_bytes{__SELECTORS__} == 0) * 0 - 1)',
    group_by=("instance",),
    aliases=("swap", "交换分区"),
    min_value=0,
    max_value=100,
)

DISK_USAGE = MetricDefinition(
    metric_id="disk.usage",
    display_name="磁盘使用率",
    group="Disk",
    unit="%",
    kind="percentage",
    direction="above",
    expr_template=(
        '100 - (node_filesystem_avail_bytes{fstype!~"tmpfs|overlay"__SELECTOR_COMMA__} / '
        'node_filesystem_size_bytes{fstype!~"tmpfs|overlay"__SELECTOR_COMMA__}) * 100'
    ),
    range_expr_template=(
        '100 - (node_filesystem_avail_bytes{fstype!~"tmpfs|overlay"__SELECTOR_COMMA__} / '
        'node_filesystem_size_bytes{fstype!~"tmpfs|overlay"__SELECTOR_COMMA__}) * 100'
    ),
    group_by=("instance", "mountpoint", "device", "fstype"),
    aliases=("磁盘", "disk"),
    legacy_tool_name="disk_usage",
    min_value=0,
    max_value=100,
    default_warning=80,
    default_critical=90,
    default_chat=True,
    default_alert_analysis=True,
)

DISK_READ_BYTES = MetricDefinition(
    metric_id="disk.read_bytes",
    display_name="磁盘读取速率",
    group="Disk",
    unit="MiB/s",
    kind="counter_rate",
    direction="observe",
    expr_template=(
        'sum by(instance, device)(rate(node_disk_read_bytes_total{__SELECTORS__}[5m])) / 1048576'
    ),
    group_by=("instance", "device"),
    aliases=("磁盘读", "disk read"),
)

DISK_WRITE_BYTES = MetricDefinition(
    metric_id="disk.write_bytes",
    display_name="磁盘写入速率",
    group="Disk",
    unit="MiB/s",
    kind="counter_rate",
    direction="observe",
    expr_template=(
        'sum by(instance, device)(rate(node_disk_written_bytes_total{__SELECTORS__}[5m])) / 1048576'
    ),
    group_by=("instance", "device"),
    aliases=("磁盘写", "disk write"),
)

DISK_IO_UTIL = MetricDefinition(
    metric_id="disk.io_util",
    display_name="磁盘 IO 利用率",
    group="Disk",
    unit="%",
    kind="percentage",
    direction="above",
    expr_template=(
        'sum by(instance, device)(rate(node_disk_io_time_seconds_total{__SELECTORS__}[5m])) * 100'
    ),
    group_by=("instance", "device"),
    aliases=("磁盘IO", "io util"),
    min_value=0,
    max_value=100,
)

NETWORK_RX_BYTES = MetricDefinition(
    metric_id="network.rx_bytes",
    display_name="网络接收速率",
    group="Network",
    unit="MiB/s",
    kind="counter_rate",
    direction="observe",
    expr_template=(
        'sum by(instance, device)(rate(node_network_receive_bytes_total{__SELECTORS__}[5m])) '
        '/ 1048576'
    ),
    base_selector={"device!=": "lo"},
    group_by=("instance", "device"),
    aliases=("网络接收", "rx bytes"),
)

NETWORK_TX_BYTES = MetricDefinition(
    metric_id="network.tx_bytes",
    display_name="网络发送速率",
    group="Network",
    unit="MiB/s",
    kind="counter_rate",
    direction="observe",
    expr_template=(
        'sum by(instance, device)(rate(node_network_transmit_bytes_total{__SELECTORS__}[5m])) '
        '/ 1048576'
    ),
    base_selector={"device!=": "lo"},
    group_by=("instance", "device"),
    aliases=("网络发送", "tx bytes"),
)

NETWORK_RX_ERRORS = MetricDefinition(
    metric_id="network.rx_errors",
    display_name="网络接收错误率",
    group="Network",
    unit="errors/s",
    kind="counter_rate",
    direction="above",
    expr_template=(
        'sum by(instance, device)(rate(node_network_receive_errs_total{__SELECTORS__}[5m]))'
    ),
    base_selector={"device!=": "lo"},
    group_by=("instance", "device"),
    aliases=("接收错误", "rx errors"),
)

NETWORK_TX_ERRORS = MetricDefinition(
    metric_id="network.tx_errors",
    display_name="网络发送错误率",
    group="Network",
    unit="errors/s",
    kind="counter_rate",
    direction="above",
    expr_template=(
        'sum by(instance, device)(rate(node_network_transmit_errs_total{__SELECTORS__}[5m]))'
    ),
    base_selector={"device!=": "lo"},
    group_by=("instance", "device"),
    aliases=("发送错误", "tx errors"),
)

SYSTEM_PROCESS_COUNT = MetricDefinition(
    metric_id="system.process_count",
    display_name="运行进程数",
    group="System",
    unit="processes",
    kind="gauge",
    direction="observe",
    expr_template='sum by(instance)(node_procs_running{__SELECTORS__})',
    group_by=("instance",),
    aliases=("进程数", "process count"),
)

SYSTEM_UPTIME = MetricDefinition(
    metric_id="system.uptime",
    display_name="系统运行时长",
    group="System",
    unit="seconds",
    kind="gauge",
    direction="observe",
    expr_template='time() - node_boot_time_seconds{__SELECTORS__}',
    group_by=("instance",),
    aliases=("运行时长", "uptime"),
)

ALERTS_ACTIVE_COUNT = MetricDefinition(
    metric_id="alerts.active_count",
    display_name="活动告警数",
    group="Alerts",
    unit="alerts",
    kind="alert_count",
    direction="above",
    group_by=("state",),
    aliases=("告警", "pending", "firing"),
    legacy_tool_name="active_alerts",
    default_warning=1,
    default_critical=3,
    default_chat=True,
    supports_alerts=True,
)


METRIC_CATALOG: tuple[MetricDefinition, ...] = (
    CPU_USAGE,
    CPU_LOAD1,
    CPU_IOWAIT,
    MEMORY_USAGE,
    MEMORY_AVAILABLE,
    MEMORY_SWAP_USAGE,
    DISK_USAGE,
    DISK_READ_BYTES,
    DISK_WRITE_BYTES,
    DISK_IO_UTIL,
    NETWORK_RX_BYTES,
    NETWORK_TX_BYTES,
    NETWORK_RX_ERRORS,
    NETWORK_TX_ERRORS,
    SYSTEM_PROCESS_COUNT,
    SYSTEM_UPTIME,
    ALERTS_ACTIVE_COUNT,
)

METRIC_BY_ID = {definition.metric_id: definition for definition in METRIC_CATALOG}


def query_semantics(metric_id: str) -> dict:
    """即时求值、rate 计算窗口与趋势范围是不同概念。"""
    metric = METRIC_BY_ID[metric_id]
    window = "5m" if "[5m]" in metric.expr_template else "1m" if metric_id == "cpu.load1" else None
    return {"unit": metric.unit, "calculation_window": window,
            "calculation": "rate" if "rate(" in metric.expr_template else "load1" if metric_id == "cpu.load1" else "gauge",
            "timestamp_semantics": "Prometheus 求值时间；gauge 使用该时刻可用的最新样本，rate 使用此前 5 分钟样本"}


def tool_name(definition: MetricDefinition) -> str:
    """Agent 工具名；旧指标保持原有名称，新指标使用点号转下划线。"""
    return definition.legacy_tool_name or definition.metric_id.replace(".", "_")
