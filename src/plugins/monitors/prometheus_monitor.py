"""Prometheus 监控源插件（实现 MonitorSource 与通用指标目录查询）。"""
from __future__ import annotations

import logging
import math
import re
import time
from typing import Any, Mapping

import requests

from src.interfaces.metric import MetricDefinition, MetricSample, MetricsConfig
from src.interfaces.host import MonitorTarget
from src.interfaces.monitor import MetricNotApplicable, MonitorError, MonitorSource
from src.metrics.catalog import METRIC_BY_ID, METRIC_CATALOG
from src.utils.metrics_config import default_metrics_config

logger = logging.getLogger("aiops_assistant.prometheus")

_EMPTY_TREND = {"latest": None, "min": None, "max": None, "avg": None, "points": 0}


def _clamp(value: float, low: float = 0.0, high: float = 100.0) -> float:
    """将百分比限制在 [low, high]，避免 Prometheus 计算误差导致越界。"""
    return max(low, min(high, value))


def _matcher_key(label_key: str) -> tuple[str, str]:
    for op in ("!~", "=~", "!=", "="):
        if label_key.endswith(op):
            return label_key[: -len(op)], op
    return label_key, "="


def _escape_label_value(value: Any) -> str:
    return str(value).replace("\\", "\\\\").replace('"', '\\"')


def _matcher_text(key: str, value: Any) -> str:
    label, op = _matcher_key(key)
    return f'{label}{op}"{_escape_label_value(value)}"'


def _strip_label(selector: Mapping[str, str], label: str) -> dict[str, str]:
    return {key: value for key, value in selector.items() if _matcher_key(key)[0] != label}


def _instance_from_selector(selector: Mapping[str, str] | None) -> str | None:
    """取调用方 selector 里的 instance 值；没有则 None。

    `query_metric(selector=...)` **只允许补充 instance**（内部调用点按实例取数用）：
    标签范围的正主是指标自己的 base_selector + metrics.yaml 的 selector，调用方不能
    用这个参数替换 PromQL 文本、也不该塞别的标签。多个 instance 键时取第一个。

    普通指标和 alert_count 都使用调用方提供的 instance，避免两类查询的范围不一致。
    """
    if not selector:
        return None
    for key, value in selector.items():
        if _matcher_key(key)[0] == "instance":
            return str(value)
    return None


class PrometheusMonitor(MonitorSource):
    """Prometheus HTTP API 监控源。"""

    name = "prometheus"
    description = "Prometheus（node_exporter）统一指标目录查询与活动告警"

    def __init__(
        self,
        base_url: str = "http://localhost:9090",
        timeout: int = 5,
        *,
        metrics_config: MetricsConfig | None = None,
        catalog: tuple[MetricDefinition, ...] = METRIC_CATALOG,
    ) -> None:
        self.base_url = (base_url or "http://localhost:9090").rstrip("/")
        self.timeout = timeout
        self.metrics_config = metrics_config or default_metrics_config()
        self.catalog = {definition.metric_id: definition for definition in catalog}

    # -- 底层查询 ---------------------------------------------------------
    def _request(self, path: str, params: dict[str, Any]) -> Any:
        url = f"{self.base_url}/api/v1/{path}"
        try:
            resp = requests.get(url, params=params, timeout=self.timeout)
            resp.raise_for_status()
        except requests.RequestException as exc:
            raise MonitorError(f"Prometheus 请求失败: {exc}") from exc

        try:
            payload = resp.json()
        except ValueError as exc:
            raise MonitorError("Prometheus returned invalid JSON") from exc
        if not isinstance(payload, dict):
            raise MonitorError("Prometheus returned an invalid response object")
        if payload.get("status") != "success":
            raise MonitorError(f"Prometheus 返回错误: {payload.get('error', '未知错误')}")
        data = payload.get("data")
        if not isinstance(data, dict):
            raise MonitorError("Prometheus 返回数据格式异常")
        return data

    def check_health(self) -> None:
        """使用 Prometheus 自身 readiness 端点，不执行用户可控查询。"""
        try:
            response = requests.get(f"{self.base_url}/-/ready", timeout=self.timeout)
            response.raise_for_status()
        except requests.RequestException as exc:
            raise MonitorError(f"Prometheus 未就绪: {exc}") from exc

    def _query(self, expr: str) -> list[dict[str, Any]]:
        result = self._request("query", {"query": expr}).get("result", [])
        if not isinstance(result, list):
            raise MonitorError("Prometheus 返回数据格式异常")
        return result

    def _query_range(self, expr: str, minutes: int, step: str) -> list[dict[str, Any]]:
        now = time.time()
        data = self._request(
            "query_range",
            {
                "query": expr,
                "start": now - max(int(minutes), 1) * 60,
                "end": now,
                "step": step,
            },
        )
        result = data.get("result", [])
        if not isinstance(result, list):
            raise MonitorError("Prometheus 返回数据格式异常")
        return result

    @staticmethod
    def _first_value(result: list[dict[str, Any]]) -> float | None:
        if not result:
            return None
        try:
            value = float(result[0]["value"][1])
            return value if math.isfinite(value) else None
        except (KeyError, IndexError, TypeError, ValueError) as exc:
            raise MonitorError("Prometheus 返回数据无法解析") from exc

    # -- 通用指标目录 -----------------------------------------------------
    def _definition(self, metric_id: str) -> MetricDefinition:
        definition = self.catalog.get(metric_id) or METRIC_BY_ID.get(metric_id)
        if definition is None:
            raise MonitorError(f"未知指标: {metric_id}")
        return definition

    def _runtime(self, metric_id: str):
        return self.metrics_config.runtime(metric_id)

    def _effective_selector(
        self,
        definition: MetricDefinition,
        *,
        instance: str | None = None,
        target: MonitorTarget | None = None,
    ) -> dict[str, str]:
        runtime = self._runtime(definition.metric_id)
        selector: dict[str, str] = dict(definition.base_selector)
        if self.metrics_config.instance:
            selector = _strip_label(selector, "instance")
            selector["instance"] = self.metrics_config.instance
        if runtime is not None:
            selector.update(runtime.selector)
        if target is not None:
            selector = _strip_label(selector, "job")
            selector = _strip_label(selector, "instance")
            selector["job"] = target.job
            selector["instance"] = target.instance
        elif instance:
            selector = _strip_label(selector, "instance")
            selector["instance"] = instance
        return selector

    def _render_expr(
        self,
        definition: MetricDefinition,
        *,
        instance: str | None = None,
        target: MonitorTarget | None = None,
        range_query: bool = False,
    ) -> str:
        template = (definition.range_expr_template or definition.expr_template) if range_query else definition.expr_template
        if definition.kind == "alert_count" and range_query:
            effective = self._effective_selector(definition, instance=instance, target=target)
            matchers = ",".join(_matcher_text(k, v) for k, v in effective.items())
            up_matchers = ",".join(_matcher_text(k, v) for k, v in effective.items()
                                   if _matcher_key(k)[0] in {"job", "instance"})
            branches = []
            for state in ("firing", "pending"):
                labels = f'alertstate="{state}"' + ("," + matchers if matchers else "")
                branches.append(
                    f'label_replace((sum(ALERTS{{{labels}}}) or (vector(0) '
                    f'and on() (max(up{{{up_matchers}}}) == 1))), '
                    f'"state", "{state}", "", "")'
                )
            return " or ".join(branches)
        if not template:
            raise MonitorError(f"指标 {definition.metric_id} 不支持该查询")
        selector = self._effective_selector(definition, instance=instance, target=target)
        matchers = ",".join(_matcher_text(key, value) for key, value in selector.items())
        # 纯动态 matcher 直接放进 {}；固定 matcher 后面才需要一个前导逗号。
        expr = template.replace("__SELECTOR_COMMA__", f",{matchers}" if matchers else "")
        return expr.replace("__SELECTORS__", matchers)

    def query_metric(
        self,
        metric_id: str,
        *,
        selector: Mapping[str, str] | None = None,
        target: MonitorTarget | None = None,
    ) -> list[MetricSample]:
        definition = self._definition(metric_id)
        instance = _instance_from_selector(selector)
        if definition.kind == "alert_count" and target is not None:
            # 同一条 PromQL 内门控补零，不增加巡检查询预算。旧无目标告警接口继续兼容。
            return self._parse_samples(metric_id, definition, self._query(
                self._render_expr(definition, target=target, range_query=True)))
        if definition.kind == "alert_count":
            alerts = self.active_alerts(target=target)
            effective = self._effective_selector(
                definition, instance=instance, target=target
            )
            if effective:
                alerts = [a for a in alerts if _alert_matches_selector(a, effective)]
            visible = [a for a in alerts if a.get("state") in ("pending", "firing")]
            samples: list[MetricSample] = []
            for state in ("firing", "pending"):
                samples.append(
                    MetricSample(
                        metric_id=metric_id,
                        labels={"state": state},
                        value=float(sum(1 for a in visible if a.get("state") == state)),
                    )
                )
            return samples

        return self._parse_samples(
            metric_id,
            definition,
            self._query(
                self._render_expr(definition, instance=instance, target=target)
            ),
        )

    def _parse_samples(
        self,
        metric_id: str,
        definition: MetricDefinition,
        result: list[dict[str, Any]],
    ) -> list[MetricSample]:
        samples: list[MetricSample] = []
        for item in result:
            try:
                timestamp = float(item["value"][0])
                value = float(item["value"][1])
            except (KeyError, IndexError, TypeError, ValueError):
                logger.warning("指标 %s 单条数据解析失败，已跳过: %s", metric_id, item)
                continue
            if metric_id == "memory.swap_usage" and value == -1:
                raise MetricNotApplicable("此主机未启用 Swap")
            # NaN/Inf 不是可用于阈值判断的样本，不能通过 clamp 伪装成 0/100。
            if not math.isfinite(timestamp) or not math.isfinite(value):
                continue
            if definition.min_value is not None or definition.max_value is not None:
                low = definition.min_value if definition.min_value is not None else value
                high = definition.max_value if definition.max_value is not None else value
                value = max(low, min(high, value))
            labels_raw = item.get("metric") or {}
            if not isinstance(labels_raw, dict):
                logger.warning("Skipping metric sample with invalid labels: %s", metric_id)
                continue
            labels = {
                key: str(labels_raw.get(key, "unknown"))
                for key in definition.group_by
                if labels_raw.get(key) not in (None, "")
            }
            samples.append(
                MetricSample(
                    metric_id=metric_id,
                    labels=labels,
                    value=value,
                    timestamp=timestamp,
                )
            )
        return samples

    def query_metric_trend(
        self,
        metric_id: str,
        instance: str | None = None,
        *,
        minutes: int = 60,
        step: str | None = None,
        selector: Mapping[str, str] | None = None,
        target: MonitorTarget | None = None,
    ) -> dict[str, Any]:
        definition = self._definition(metric_id)
        expr = self._render_expr(
            definition, instance=instance, target=target, range_query=True
        )
        # 限制查询密度：最多 240 个间隔（241 个点），不在前端丢弃真实走势。
        duration = max(1, int(minutes)) * 60
        units = {"s": 1, "m": 60, "h": 3600, "d": 86400}
        match = re.fullmatch(r"([1-9][0-9]*)([smhd])", step or "15s")
        if not match:
            raise MonitorError("step 必须是正整数加 s/m/h/d")
        seconds = max(15, math.ceil(duration / 240), int(match[1]) * units[match[2]])
        series = self._query_range(expr, minutes=minutes, step=f"{seconds}s")
        curves = []
        for item in series:
            if not isinstance(item, dict) or not isinstance(item.get("values"), list):
                continue
            labels = item.get("metric") or {}
            if not isinstance(labels, dict):
                continue
            points = {}
            applicable = False
            no_swap = False
            for pair in item["values"]:
                if not isinstance(pair, (list, tuple)) or len(pair) != 2:
                    continue
                try:
                    timestamp, value = float(pair[0]), float(pair[1])
                except (ValueError, TypeError):
                    continue
                if not math.isfinite(timestamp):
                    continue
                if metric_id == "memory.swap_usage" and value == -1:
                    points[timestamp] = None
                    no_swap = True
                    continue
                if math.isfinite(value):
                    if definition.min_value is not None:
                        value = max(definition.min_value, value)
                    if definition.max_value is not None:
                        value = min(definition.max_value, value)
                    applicable = True
                    points[timestamp] = value
                else:
                    points[timestamp] = None
            ordered = [[t, v] for t, v in sorted(points.items())][:241]
            if ordered:
                curves.append({"labels": {k: str(v) for k, v in sorted(labels.items())
                                           if k != "__name__"}, "values": ordered,
                               "applicable": applicable, "no_swap": no_swap})
        curves.sort(key=lambda c: tuple(c["labels"].items()))
        total = len(curves)
        curves = curves[:32]
        summaries = [_summarize_values(c["values"], low=definition.min_value,
                                      high=definition.max_value) for c in curves]
        valid = [summary for summary in summaries if summary["points"]]
        summary = dict(_EMPTY_TREND)
        if valid:
            choose = min if definition.direction == "below" else max
            summary = dict(choose(valid, key=lambda item: item["latest"]))
        state = "ok" if valid else "no_data"
        if metric_id == "memory.swap_usage" and curves and any(c["no_swap"] for c in curves) and not any(c["applicable"] for c in curves):
            state = "not_applicable"
        summary.update({"series": [{"labels": c["labels"], "values": c["values"]} for c in curves],
                        "state": state, "step_seconds": seconds,
                        "series_total": total, "truncated": total > 32})
        return summary

    # -- 旧接口兼容包装 ---------------------------------------------------
    def cpu_usage(self) -> float | None:
        definition = self._definition("cpu.usage")
        value = self._first_value(self._query(self._render_expr(definition)))
        return None if value is None else _clamp(value)

    def memory_usage(self) -> float | None:
        definition = self._definition("memory.usage")
        value = self._first_value(self._query(self._render_expr(definition)))
        return None if value is None else _clamp(value)

    def disk_usage(self) -> list[dict[str, Any]]:
        samples = self.query_metric("disk.usage")
        mounts = [
            {
                "mountpoint": sample.labels.get("mountpoint", "未知挂载点"),
                "usage": sample.value,
            }
            for sample in samples
        ]
        mounts.sort(key=lambda item: item["usage"], reverse=True)
        return mounts

    # -- MonitorSource：活动告警 ------------------------------------------
    def active_alerts(
        self, *, target: MonitorTarget | None = None
    ) -> list[dict[str, Any]]:
        data = self._request("alerts", {})
        records: list[dict[str, Any]] = []
        raw_alerts = data.get("alerts", [])
        if not isinstance(raw_alerts, list):
            raise MonitorError("Prometheus returned an invalid alerts list")
        for item in raw_alerts:
            if not isinstance(item, dict):
                logger.warning("Skipping malformed Prometheus alert item")
                continue
            labels = item.get("labels") or {}
            annotations = item.get("annotations") or {}
            if not isinstance(labels, dict) or not isinstance(annotations, dict):
                logger.warning("Skipping Prometheus alert with malformed labels")
                continue
            raw_value = item.get("value")
            try:
                value = float(raw_value) if raw_value not in (None, "") else None
            except (TypeError, ValueError):
                value = None
            if value is not None and not math.isfinite(value):
                value = None
            records.append(
                {
                    "alertname": labels.get("alertname", "unknown"),
                    "severity": labels.get("severity", "none"),
                    "instance": labels.get("instance", "unknown"),
                    "job": labels.get("job", ""),
                    "state": item.get("state", "unknown"),
                    "activeAt": item.get("activeAt", ""),
                    "value": value,
                    "summary": annotations.get("summary", ""),
                    "description": annotations.get("description", ""),
                }
            )

        if target is not None:
            records = [
                record for record in records
                if record["instance"] == target.instance
                and record["job"] == target.job
            ]
        state_rank = {"firing": 0, "pending": 1}
        records.sort(key=lambda r: (state_rank.get(r["state"], 9), r["alertname"]))
        return records

    def target_status(self, target: MonitorTarget) -> dict[str, Any]:
        """按登记的 job/instance 查询 up，明确区分四类状态。"""
        selector = {
            "job": target.job,
            "instance": target.instance,
        }
        expr = "up{" + ",".join(
            _matcher_text(key, value) for key, value in selector.items()
        ) + "}"
        result = self._query(expr)
        if not result:
            return {"state": "no_data", "value": None}
        if len(result) != 1:
            return {"state": "ambiguous", "value": None}
        value = self._first_value(result)
        if value is None:
            return {"state": "no_data", "value": None}
        return {
            "state": "reachable" if value >= 1.0 else "unreachable",
            "value": value,
        }

    def instance_trend(
        self,
        instance: str,
        *,
        minutes: int = 60,
        step: str = "5m",
    ) -> dict[str, dict[str, Any]]:
        """旧接口：固定返回 CPU/内存/磁盘趋势。"""
        result = {}
        for title, metric_id in (("CPU使用率", "cpu.usage"), ("内存使用率", "memory.usage"), ("磁盘使用率", "disk.usage")):
            try:
                result[title] = self.query_metric_trend(metric_id, instance, minutes=minutes, step=step)
            except MonitorError:
                result[title] = dict(_EMPTY_TREND)
        return result



def _alert_matches_selector(alert: Mapping[str, Any], selector: Mapping[str, str]) -> bool:
    for key, expected in selector.items():
        label, op = _matcher_key(key)
        actual = str(alert.get(label, ""))
        if op == "=" and actual != str(expected):
            return False
        if op == "!=" and actual == str(expected):
            return False
        if op == "=~" and re.fullmatch(str(expected), actual) is None:
            return False
        if op == "!~" and re.fullmatch(str(expected), actual) is not None:
            return False
    return True


def _summarize_values(
    values: list[list[Any]],
    *,
    low: float | None = 0.0,
    high: float | None = 100.0,
) -> dict[str, Any]:
    """把 range values 汇总为 latest/min/max/avg/points。"""
    nums: list[float] = []
    for point in values:
        if not isinstance(point, (list, tuple)) or len(point) != 2:
            continue
        _, raw = point
        if raw in (None, ""):
            continue
        try:
            value = float(raw)
        except (TypeError, ValueError):
            continue
        if not math.isfinite(value):
            continue
        if low is not None:
            value = max(low, value)
        if high is not None:
            value = min(high, value)
        nums.append(value)
    if not nums:
        return dict(_EMPTY_TREND)
    return {
        "latest": nums[-1],
        "min": min(nums),
        "max": max(nums),
        "avg": sum(nums) / len(nums),
        "points": len(nums),
    }
