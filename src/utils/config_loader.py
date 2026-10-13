"""配置加载：递归 ${ENV} / ${ENV:-default} 插值 + 转 dataclass。

职责：
- 读取 config/settings.yaml，把字符串值里的 `${VAR}` / `${VAR:-default}`
  占位替换为环境变量（或默认值），代码不再硬编码可变参数 / 密钥；
- 解析成嵌套的 `Settings` dataclass，各层代码按需取用；
- 严格校验：配置文件缺失、pyyaml 未安装、未知字段或类型错误时抛 ConfigError，拒绝不确定启动。

敏感信息约束：YAML 应保留环境占位符；密钥由页面凭据、部署凭据或容器环境提供。
插值后的 Settings 在内存中包含明文值，不应完整打印或直接返回给客户端。
"""
from __future__ import annotations

import json
import logging
import math
import os
import re
from dataclasses import dataclass, fields
from pathlib import Path
from typing import Any, get_type_hints

from src import __app_name__, __version__

logger = logging.getLogger("aiops_assistant.config")

# ---------------------------------------------------------------------------
# 应用根目录（容器内为 /app）
# ---------------------------------------------------------------------------
ROOT_DIR: Path = Path(__file__).resolve().parent.parent.parent

# 默认配置文件：/app/config/settings.yaml（可用 OPS_SETTINGS_FILE 覆盖）
DEFAULT_SETTINGS_FILE: Path = ROOT_DIR / "config" / "settings.yaml"

# 匹配 ${VAR} 或 ${VAR:-default}
_ENV_REF = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(?::-([^}]*))?\}")


class ConfigError(Exception):
    """配置读取 / 解析失败。"""


# ---------------------------------------------------------------------------
# 配置模型（dataclass，结构对应 config/settings.yaml）
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class AppSettings:
    name: str = __app_name__
    @property
    def version(self) -> str:
        """只读代码版本元信息，不属于可配置 dataclass 字段。"""
        return __version__


@dataclass(frozen=True)
class PrometheusSettings:
    base_url: str = "http://localhost:9090"
    timeout: int = 5
    target_mode: str = "external"
    file_sd_path: str = ""


@dataclass(frozen=True)
class MonitorSettings:
    provider: str = "prometheus"


@dataclass(frozen=True)
class LLMSettings:
    provider: str = "openai_compatible"
    enabled: bool = True


@dataclass(frozen=True)
class AgentSettings:
    enabled: bool = True
    name: str = "ops_assistant"
    debug: bool = False
    recursion_limit: int = 25


@dataclass(frozen=True)
class ChatSettings:
    """聊天请求级保护。"""

    max_message_chars: int = 8000


@dataclass(frozen=True)
class AlertSettings:
    recent_limit: int = 100
    trend_minutes: int = 60


@dataclass(frozen=True)
class MetricsSettings:
    """统一指标目录配置。空 file = 使用代码内默认运行配置。"""

    file: str = ""


@dataclass(frozen=True)
class HostsSettings:
    """静态主机清单；必须显式配置或提供 OPS_HOSTS_FILE。"""

    file: str = ""


@dataclass(frozen=True)
class InspectionSettings:
    """固定指标巡检调度与归档配置；具体指标来自 metrics.yaml。"""

    enabled: bool = False
    interval_seconds: int = 300
    run_on_start: bool = True
    ttl_hours: int = 168
    max_rows: int = 1000
    max_hosts_per_run: int = 50
    max_queries_per_run: int = 500
    round_timeout_seconds: int = 120


@dataclass(frozen=True)
class StorageSettings:
    """SQLite 持久化配置。

    直接构造 `Settings()` 时默认关闭持久化；从文件启动时使用
    `config/settings.yaml` 的 `storage.enabled` 值。
    """

    enabled: bool = False
    db_path: str = "data/aiops_assistant.db"
    session_ttl_hours: int = 72
    max_sessions: int = 200
    message_window: int = 20
    history_ttl_hours: int = 168
    max_history_rows: int = 5000
    purge_interval_seconds: int = 60


@dataclass(frozen=True)
class AuthSettings:
    """Token 到稳定身份的映射及角色、主机授权配置。
    直接构造无 Token 的 Settings 只供内部装配，业务请求返回 503。
    tokens 与 host_access 使用 JSON 字符串，避免 YAML 插值产生歧义。
    """

    tokens: str = ""
    # 身份到可见主机 ID 数组的 JSON；管理员可管理所有主机，普通身份默认无权限。
    host_access: str = "{}"

    #: 审批模式：owner = 触发者本人可批准；approvers_only = 只有显式审批人可批准。
    approval_mode: str = "owner"
    #: JSON 数组字符串，例如 ["alice","audit-bot"]；approvers_only 模式下必填。
    approver_identities: str = "[]"

    #: 可使用系统管理 API 的最高权限身份。与审批人相互独立。
    admin_identities: str = "[]"

    #: `POST /alert`（Alertmanager webhook）**只接受这个身份**的密钥；留空 = 不限制。
    #:
    #: 为什么需要：`/alert` 不是只读的——它把告警喂给 `RuleEngine`，而
    #: `auto_execute: true` 的规则会**直接执行写命令、不经人工审批**。不限制的话，
    #: 任何一个持有效密钥的聊天用户都能构造告警去踩那条路径。给它一个专用身份，
    #: 就把「人」的密钥与「机器入口」的密钥分开了。
    #:
    #: 默认空串是**有意的 fail-open**（保持既有部署不变），但 `main.create_app` 会在
    #: 配置了有效身份而这里为空时打一条 WARNING——不能让它悄悄失效。
    alert_identity: str = ""


@dataclass(frozen=True)
class LoggingSettings:
    level: str = "INFO"
    audit: bool = True
    # 直接构造 Settings() 时不写审计文件；从文件启动时使用 logging.audit_file。
    audit_file: str = ""
    audit_max_bytes: int = 5 * 1024 * 1024
    audit_backup_count: int = 3
    inspection_file: str = ""
    inspection_max_bytes: int = 5 * 1024 * 1024
    inspection_backup_count: int = 3


@dataclass(frozen=True)
class Settings:
    """全局配置集合（对应 config/settings.yaml 顶层各节）。"""

    app: AppSettings = AppSettings()
    prometheus: PrometheusSettings = PrometheusSettings()
    monitor: MonitorSettings = MonitorSettings()
    llm: LLMSettings = LLMSettings()
    agent: AgentSettings = AgentSettings()
    alert: AlertSettings = AlertSettings()
    chat: ChatSettings = ChatSettings()
    metrics: MetricsSettings = MetricsSettings()
    hosts: HostsSettings = HostsSettings()
    inspection: InspectionSettings = InspectionSettings()
    storage: StorageSettings = StorageSettings()
    logging: LoggingSettings = LoggingSettings()
    auth: AuthSettings = AuthSettings()


# ---------------------------------------------------------------------------
# ${ENV} 插值
# ---------------------------------------------------------------------------
# 已告警过的缺失变量（同进程只提示一次，避免多处占位刷屏）
_warned_missing_vars: set[str] = set()


def _env_value(match: re.Match) -> str:
    """单处占位的替换逻辑：${VAR}→env，${VAR:-default}→env 或 default。"""
    name, default = match.group(1), match.group(2)
    value = os.environ.get(name)
    if value is None:
        if default is not None:
            return default
    # 解析阶段对缺失变量返回空串并告警；字段及启动鉴权仍会继续校验，同变量只提示一次。
        if name not in _warned_missing_vars:
            _warned_missing_vars.add(name)
            logger.warning("环境变量 %s 未设置且无默认值，已按空字符串处理", name)
        return ""
    return value


def interpolate(text: str) -> str:
    """把文本中的 ${VAR} / ${VAR:-default} 全部替换为环境变量值。"""
    return _ENV_REF.sub(_env_value, text)


def interpolate_yaml(text: str) -> str:
    """保留 YAML 引号内插值字符串的原值；未加引号字段沿用原标量解析行为。"""
    import yaml
    from yaml.tokens import ScalarToken

    try:
        quoted = [token for token in yaml.scan(text)
                  if isinstance(token, ScalarToken) and token.style in {"'", '"'}
                  and _ENV_REF.search(token.value)]
    except yaml.YAMLError:
        # 某些原有未加引号占位（例如 flow 集合）在插值后才是合法 YAML。
        return interpolate(text)
    pieces: list[str] = []
    cursor = 0
    for token in quoted:
        pieces.append(interpolate(text[cursor:token.start_mark.index]))
        # JSON 字符串是合法 YAML 双引号标量，能保留引号、反斜线、换行和 Unicode。
        pieces.append(json.dumps(interpolate(token.value)))
        cursor = token.end_mark.index
    pieces.append(interpolate(text[cursor:]))
    return "".join(pieces)


def parse_yaml(text: str) -> Any:
    """插值后安全解析；语法诊断仅保留位置，不附带可能含凭据的原文。"""
    import yaml
    from src.utils.config_migration import reject_legacy_text
    reject_legacy_text(text)

    try:
        return yaml.safe_load(interpolate_yaml(text))
    except yaml.YAMLError as exc:
        mark = getattr(exc, "problem_mark", None)
        location = f"（第 {mark.line + 1} 行，第 {mark.column + 1} 列）" if mark else ""
        # ParserError 的原文与异常链均可能包含插值后的明文或转义凭据。
        raise yaml.YAMLError(f"YAML 语法错误{location}") from None


# ---------------------------------------------------------------------------
# YAML 到数据类
# ---------------------------------------------------------------------------
_TRUE_WORDS = frozenset({"1", "true", "yes", "on"})
_FALSE_WORDS = frozenset({"0", "false", "no", "off"})


def _as_scalar(value: Any, typ: type, *, what: str) -> Any:
    """把 YAML 值严格规约到目标标量类型；非法值抛 ConfigError。

    安全相关配置不能走 ``bool("tru") == True`` 那类宽松转换，更不能把无法识别的
    字符串静默当成 False；例如命令执行开关拼错时必须拒绝配置。
    """
    if typ is bool:
        if isinstance(value, bool):
            return value
        if isinstance(value, str):
            text = value.strip().lower()
            if text in _TRUE_WORDS:
                return True
            if text in _FALSE_WORDS:
                return False
        raise ConfigError(f"{what} 必须是布尔值 true/false，得到类型: {type(value).__name__}")
    if typ is str:
        if not isinstance(value, str):
            raise ConfigError(f"{what} 必须是字符串，得到类型: {type(value).__name__}")
        return value
    if typ is int:
        if isinstance(value, bool):
            raise ConfigError(f"{what} 必须是整数，得到类型: {type(value).__name__}")
        try:
            return int(value)
        except (TypeError, ValueError):
            raise ConfigError(f"{what} 必须是整数，得到类型: {type(value).__name__}") from None
    if typ is float:
        if isinstance(value, bool):
            raise ConfigError(f"{what} 必须是数字，得到类型: {type(value).__name__}")
        try:
            result = float(value)
        except (TypeError, ValueError):
            raise ConfigError(f"{what} 必须是数字，得到类型: {type(value).__name__}") from None
        if not math.isfinite(result):
            raise ConfigError(f"{what} 必须是有限数字")
        return result
    try:
        return typ(value)
    except (TypeError, ValueError):
        raise ConfigError(f"{what} 类型无效，得到类型: {type(value).__name__}") from None


def _build_dataclass(model_type: type, data: Any, *, prefix: str) -> Any:
    """递归构造严格配置 dataclass；未知键、类型错误与缺失映射直接报错。"""
    if not isinstance(data, dict):
        raise ConfigError(f"配置节 {prefix or '<root>'} 必须是映射，得到类型: {type(data).__name__}")
    hints = get_type_hints(model_type)
    allowed = {f.name for f in fields(model_type)}
    unknown = set(data) - allowed
    if unknown:
        where = f"配置节 {prefix}" if prefix else "settings.yaml"
        raise ConfigError(f"{where} 存在未知字段: " + ", ".join(sorted(unknown)))

    kwargs: dict[str, Any] = {}
    for f in fields(model_type):
        if f.name not in data:
            continue
        value = data[f.name]
        typ = hints[f.name]
        child_prefix = f"{prefix}.{f.name}" if prefix else f.name
        if hasattr(typ, "__dataclass_fields__"):
            kwargs[f.name] = _build_dataclass(typ, value, prefix=child_prefix)
        else:
            kwargs[f.name] = _as_scalar(value, typ, what=child_prefix)
    return model_type(**kwargs)


def _validate_settings(settings: Settings) -> None:
    """跨字段约束，供启动加载及管理员候选配置校验共用。"""
    from src.utils.auth import AuthConfigError
    from src.utils.host_access import parse_host_access
    try:
        parse_host_access(settings.auth.host_access)
    except AuthConfigError as exc:
        raise ConfigError(str(exc)) from exc
    if settings.prometheus.target_mode not in {"external", "managed"}:
        raise ConfigError("prometheus.target_mode 只支持 external / managed")
    if settings.prometheus.target_mode == "managed" and not settings.prometheus.file_sd_path:
        raise ConfigError("自管 Prometheus 模式必须配置 prometheus.file_sd_path")

    ins = settings.inspection
    if not 30 <= ins.interval_seconds <= 86400:
        raise ConfigError("inspection.interval_seconds 必须在 30~86400 秒之间")
    if ins.ttl_hours < 1:
        raise ConfigError("inspection.ttl_hours 必须大于等于 1")
    if ins.max_rows < 1:
        raise ConfigError("inspection.max_rows 必须大于等于 1")
    if not 1 <= ins.max_hosts_per_run <= 1000:
        raise ConfigError("inspection.max_hosts_per_run 必须在 1~1000 之间")
    if not 1 <= ins.max_queries_per_run <= 100000:
        raise ConfigError("inspection.max_queries_per_run 必须在 1~100000 之间")
    if not 1 <= ins.round_timeout_seconds <= 86400:
        raise ConfigError("inspection.round_timeout_seconds 必须在 1~86400 秒之间")



def _build_settings(data: dict[str, Any]) -> Settings:
    """从解析后的 dict 递归构造严格 Settings。"""
    from src.utils.config_migration import reject_legacy_document
    reject_legacy_document("settings", data)
    settings = _build_dataclass(Settings, data, prefix="")
    _validate_settings(settings)
    return settings


# ---------------------------------------------------------------------------
# 加载入口
# ---------------------------------------------------------------------------
# 这里**刻意没有** `default_settings()` 之类的兜底构造函数：配置缺失 / 解析失败一律抛
# ConfigError，由调用方拒绝启动。一个「读不到配置就退回全默认值」的入口看起来更健壮，
# 实际上可能把缺失身份或意外关闭存储的默认值放行；严格加载要求调用方明确处理失败。


def load_settings(path: str | Path | None = None) -> Settings:
    """读取并解析 config/settings.yaml，返回 Settings。

    - 环境变量由容器启动入口注入，不自动加载宿主机 .env；
    - 文件缺失 / pyyaml 未装 / 解析失败 → 抛 ConfigError，拒绝带着不确定配置启动。
    """
    file_path = Path(path) if path else Path(os.getenv("OPS_SETTINGS_FILE", DEFAULT_SETTINGS_FILE))
    if not file_path.is_file():
        raise ConfigError(f"配置文件不存在: {file_path}")

    try:
        import yaml
    except ImportError as exc:
        raise ConfigError("缺少配置依赖 pyyaml，请先安装：pip install pyyaml") from exc

    try:
        with open(file_path, encoding="utf-8") as fh:
            raw = fh.read()
        data = parse_yaml(raw) or {}
        if not isinstance(data, dict):
            raise ConfigError("配置顶层必须是映射")
        if "auth" not in data:
            raise ConfigError("settings.yaml 必须显式声明 auth 配置，拒绝隐式无鉴权启动")
        settings = _build_settings(data)
    except ConfigError:
        raise
    except Exception as exc:  # yaml 语法 / 类型规约等错误统一拒绝
        raise ConfigError(f"配置文件解析失败（{file_path}）: {exc}") from exc

    logger.info("已加载配置: %s（app=%s v%s）", file_path, settings.app.name, settings.app.version)
    return settings
