"""监控源插件：导入即注册到 registry（键 = provider 名）。"""
from src.plugins.monitors import prometheus_monitor  # noqa: F401
from src.registry import registry

registry.register_monitor(
    prometheus_monitor.PrometheusMonitor.name,
    prometheus_monitor.PrometheusMonitor,
)
