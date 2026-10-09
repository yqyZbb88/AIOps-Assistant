"""metrics.yaml 配置加载与严格校验。"""
from __future__ import annotations

import logging
import math
import re
from pathlib import Path
from typing import Any

from src.interfaces.metric import (
    MetricDefinition,
    MetricRuntimeConfig,
    MetricSurfaceConfig,
    MetricsConfig,
)
from src.metrics.catalog import METRIC_BY_ID, METRIC_CATALOG
from src.utils.config_loader import ConfigError, ROOT_DIR, parse_yaml

logger = logging.getLogger("aiops_assistant.metrics_config")

DEFAULT_METRICS_FILE = ROOT_DIR / "config" / "metrics.yaml"
_METRIC_TOP_KEYS = frozenset({"enabled", "surfaces", "warning", "critical", "severity_enabled", "selector"})
_SURFACE_KEYS = frozenset({"inspection", "chat", "alert_analysis"})
_SELECTOR_KEY = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*(?:(?:=~|!~|!=|=))?$")


def default_metrics_config() -> MetricsConfig:
    """代码目录的出厂运行配置，用于 Settings()/无外部文件场景。"""
    runtime: dict[str, MetricRuntimeConfig] = {}
    for definition in METRIC_CATALOG:
        severity = definition.default_warning is not None and definition.default_critical is not None
        runtime[definition.metric_id] = MetricRuntimeConfig(
            metric_id=definition.metric_id,
            enabled=True,
            surfaces=MetricSurfaceConfig(
                inspection=definition.default_inspection,
                chat=definition.default_chat,
                alert_analysis=definition.default_alert_analysis,
            ),
            warning=definition.default_warning,
            critical=definition.default_critical,
            severity_enabled=severity,
            selector={},
        )
    return MetricsConfig(metrics=runtime)


def _as_bool(value: Any, what: str) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        text = value.strip().lower()
        if text in {"1", "true", "yes", "on"}:
            return True
        if text in {"0", "false", "no", "off"}:
            return False
    raise ConfigError(f"{what} 必须是布尔值 true/false，得到: {value!r}")


def _as_number(value: Any, what: str) -> float:
    if isinstance(value, bool):
        raise ConfigError(f"{what} 必须是数字，得到: {value!r}")
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise ConfigError(f"{what} 必须是数字，得到: {value!r}") from exc
    if not math.isfinite(number):
        raise ConfigError(f"{what} 必须是有限数字，得到: {value!r}")
    return number


def _parse_surfaces(value: Any, metric_id: str) -> MetricSurfaceConfig:
    if value is None:
        return MetricSurfaceConfig()
    if not isinstance(value, dict):
        raise ConfigError(f"metrics.{metric_id}.surfaces 必须是映射")
    unknown = set(value) - _SURFACE_KEYS
    if unknown:
        raise ConfigError(
            f"metrics.{metric_id}.surfaces 存在未知字段: " + ", ".join(sorted(unknown))
        )
    return MetricSurfaceConfig(
        inspection=_as_bool(value.get("inspection", False), f"metrics.{metric_id}.surfaces.inspection"),
        chat=_as_bool(value.get("chat", False), f"metrics.{metric_id}.surfaces.chat"),
        alert_analysis=_as_bool(
            value.get("alert_analysis", False), f"metrics.{metric_id}.surfaces.alert_analysis"
        ),
    )


def _parse_selector(value: Any, metric_id: str) -> dict[str, str]:
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise ConfigError(f"metrics.{metric_id}.selector 必须是映射")
    selector: dict[str, str] = {}
    for key, raw in value.items():
        key_text = str(key)
        if not _SELECTOR_KEY.fullmatch(key_text):
            raise ConfigError(f"metrics.{metric_id}.selector 标签名不合法: {key_text!r}")
        if raw is None:
            raise ConfigError(f"metrics.{metric_id}.selector.{key_text} 不能为空")
        selector[key_text] = str(raw)
    return selector


def _validate_thresholds(
    definition: MetricDefinition,
    *,
    metric_id: str,
    severity_enabled: bool,
    warning: float | None,
    critical: float | None,
) -> None:
    if definition.kind == "alert_count" and severity_enabled:
        if warning is None or critical is None or warning < 1 or critical <= warning:
            raise ConfigError(
                f"metrics.{metric_id} 必须满足 1 <= warning < critical"
            )
        return
    if definition.direction == "observe":
        if severity_enabled:
            raise ConfigError(f"metrics.{metric_id} 是 observe 指标，不能启用 severity")
        return
    if not severity_enabled:
        return
    if warning is None or critical is None:
        raise ConfigError(f"metrics.{metric_id} severity_enabled=true 时必须配置 warning 和 critical")
    if definition.direction == "above" and warning >= critical:
        raise ConfigError(f"metrics.{metric_id} direction=above 时必须满足 warning < critical")
    if definition.direction == "below" and warning <= critical:
        raise ConfigError(f"metrics.{metric_id} direction=below 时必须满足 warning > critical")


def parse_metrics_config(text: str, *, path: str = "metrics.yaml") -> MetricsConfig:
    try:
        import yaml
    except ImportError as exc:  # pragma: no cover - core dependency missing path
        raise ConfigError("缺少指标配置依赖 pyyaml，请先安装：pip install pyyaml") from exc
    try:
        data = parse_yaml(text) or {}
    except Exception as exc:
        raise ConfigError(f"指标配置解析失败（{path}）: {exc}") from exc
    if not isinstance(data, dict):
        raise ConfigError("metrics.yaml 顶层必须是映射")
    unknown_top = set(data) - {"version", "instance", "max_alert_analysis_metrics", "metrics"}
    if unknown_top:
        raise ConfigError("metrics.yaml 存在未知顶层字段: " + ", ".join(sorted(unknown_top)))

    version = int(data.get("version", 1))
    if version != 1:
        raise ConfigError(f"metrics.yaml 只支持 version=1，得到: {version}")
    instance = str(data.get("instance", "") or "").strip()
    max_analysis = int(data.get("max_alert_analysis_metrics", 8))
    if not 1 <= max_analysis <= 50:
        raise ConfigError("metrics.yaml max_alert_analysis_metrics 必须在 1~50")
    raw_metrics = data.get("metrics")
    if not isinstance(raw_metrics, dict) or not raw_metrics:
        raise ConfigError("metrics.yaml metrics 必须是非空映射")

    runtime: dict[str, MetricRuntimeConfig] = {}
    for metric_id, spec in raw_metrics.items():
        metric_id = str(metric_id)
        definition = METRIC_BY_ID.get(metric_id)
        if definition is None:
            raise ConfigError(f"metrics.yaml 含未知指标: {metric_id}")
        if not isinstance(spec, dict):
            raise ConfigError(f"metrics.{metric_id} 必须是映射")
        unknown = set(spec) - _METRIC_TOP_KEYS
        if unknown:
            raise ConfigError(
                f"metrics.{metric_id} 存在未知字段: " + ", ".join(sorted(unknown))
            )
        enabled = _as_bool(spec.get("enabled", True), f"metrics.{metric_id}.enabled")
        surfaces = _parse_surfaces(spec.get("surfaces"), metric_id)
        if enabled and not any(
            (surfaces.inspection, surfaces.chat, surfaces.alert_analysis)
        ):
            raise ConfigError(f"metrics.{metric_id} enabled=true 时至少启用一个 surface")
        if surfaces.alert_analysis and definition.range_expr_template is None:
            raise ConfigError(f"metrics.{metric_id} 不支持 alert_analysis 趋势查询")
        severity_enabled = _as_bool(
            spec.get("severity_enabled", False), f"metrics.{metric_id}.severity_enabled"
        )
        warning = None if spec.get("warning") is None else _as_number(
            spec.get("warning"), f"metrics.{metric_id}.warning"
        )
        critical = None if spec.get("critical") is None else _as_number(
            spec.get("critical"), f"metrics.{metric_id}.critical"
        )
        _validate_thresholds(
            definition,
            metric_id=metric_id,
            severity_enabled=severity_enabled,
            warning=warning,
            critical=critical,
        )
        runtime[metric_id] = MetricRuntimeConfig(
            metric_id=metric_id,
            enabled=enabled,
            surfaces=surfaces,
            warning=warning,
            critical=critical,
            severity_enabled=severity_enabled,
            selector=_parse_selector(spec.get("selector"), metric_id),
        )
    return MetricsConfig(
        version=version,
        instance=instance,
        max_alert_analysis_metrics=max_analysis,
        metrics=runtime,
    )


def load_metrics_config(path: str | Path | None = None) -> MetricsConfig:
    """加载 metrics.yaml；path 为空使用代码默认配置。"""
    if not path:
        return default_metrics_config()
    file_path = Path(path)
    if not file_path.is_absolute():
        file_path = ROOT_DIR / file_path
    if not file_path.is_file():
        raise ConfigError(f"指标配置文件不存在: {file_path}")
    logger.info("加载指标配置: %s", file_path)
    return parse_metrics_config(file_path.read_text(encoding="utf-8"), path=str(file_path))
