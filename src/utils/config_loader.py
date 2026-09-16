"""配置加载：递归 ${ENV} / ${ENV:-default} 插值 + 转 dataclass。

职责：
- 读取 config/settings.yaml，把字符串值里的 `${VAR}` / `${VAR:-default}`
  占位替换为环境变量（或默认值），代码不再硬编码可变参数 / 密钥；
- 解析成嵌套的 `Settings` dataclass，各层代码按需取用；
- 严格校验：配置文件缺失、pyyaml 未安装、未知字段或类型错误时抛 ConfigError，拒绝不确定启动。

敏感信息约束：本模块只做字符串替换，绝不在配置里产生明文密钥来源；
真实密钥只存在于 .env / 环境变量（见 README「安全说明」）。
"""
from __future__ import annotations

import logging
import math
import os
import re
from dataclasses import dataclass, field, fields
from pathlib import Path
from typing import Any, get_type_hints

from src import __app_name__, __version__

logger = logging.getLogger("aiops_assistant.config")

# ---------------------------------------------------------------------------
# 项目根目录（= 仓库根，init.py / config / .env 所在处）
# ---------------------------------------------------------------------------
ROOT_DIR: Path = Path(__file__).resolve().parent.parent.parent

# 默认配置文件：<仓库根>/config/settings.yaml（可用 OPS_SETTINGS_FILE 覆盖）
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
    version: str = __version__


@dataclass(frozen=True)
class ServerSettings:
    host: str = "127.0.0.1"
    port: int = 8000


@dataclass(frozen=True)
class PrometheusSettings:
    base_url: str = "http://localhost:9090"
    timeout: int = 5


@dataclass(frozen=True)
class MonitorSettings:
    provider: str = "prometheus"


@dataclass(frozen=True)
class LLMSettings:
    provider: str = "deepseek"
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
    trend_step: str = "5m"


@dataclass(frozen=True)
class MetricsSettings:
    """统一指标目录配置。空 file = 使用代码内默认运行配置。"""

    file: str = ""


@dataclass(frozen=True)
class InspectionSettings:
    """固定指标巡检调度与归档配置；具体指标来自 metrics.yaml。"""

    enabled: bool = False
    interval_seconds: int = 300
    run_on_start: bool = True
    ttl_hours: int = 168
    max_rows: int = 1000


@dataclass(frozen=True)
class StorageSettings:
    """SQLite 持久化配置。

    `enabled` 的 dataclass 默认值是 **False**：测试里 `Settings()` 被直接构造多次
    （create_app(settings=Settings())），默认打开会让用例写真实 db 文件、跨用例串数据。
    出厂是否打开只由 config/settings.yaml 决定。
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
    """共享密钥鉴权配置。

    `enabled` 的 dataclass 默认值是 **False**：与 StorageSettings.enabled 同理——
    测试里 `Settings()` 被直接构造多次（`create_app(settings=Settings())`），
    默认打开会让既有用例集体 401。出厂是否打开只由 config/settings.yaml 决定。

    `tokens` 是「密钥 → 身份」的 **JSON 字符串原文**，解析在 `utils/auth.py`。
    这里保持成字符串，是因为 yaml 的插值结果本就是字符串，而本模块只做类型规约
    （见 `_as_scalar`）——不放 dict 字段，避免 `dict(value)` 那条路径把
    单引号 repr 当成合法 JSON。
    """

    enabled: bool = False
    tokens: str = ""

    #: 审批模式：owner = 触发者本人可批准；approvers_only = 只有显式审批人可批准。
    approval_mode: str = "owner"
    #: JSON 数组字符串，例如 ["alice","audit-bot"]；approvers_only 模式下必填。
    approver_identities: str = "[]"

    #: `POST /alert`（Alertmanager webhook）**只接受这个身份**的密钥；留空 = 不限制。
    #:
    #: 为什么需要：`/alert` 不是只读的——它把告警喂给 `RuleEngine`，而
    #: `auto_execute: true` 的规则会**直接执行写命令、不经人工审批**。不限制的话，
    #: 任何一个持有效密钥的聊天用户都能构造告警去踩那条路径。给它一个专用身份，
    #: 就把「人」的密钥与「机器入口」的密钥分开了。
    #:
    #: 默认空串是**有意的 fail-open**（保持既有部署不变），但 `main.create_app` 会在
    #: `enabled` 为真而这里为空时打一条 WARNING——不能让它悄悄失效。
    alert_identity: str = ""


@dataclass(frozen=True)
class LoggingSettings:
    level: str = "INFO"
    audit: bool = True
    # 审计落盘：dataclass 默认**空串**（不落文件）——与 storage.enabled 同理，
    # 测试里 Settings() 被直接构造多次，默认写文件会在仓库里堆 logs/ 并跨用例串数据。
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
    server: ServerSettings = ServerSettings()
    prometheus: PrometheusSettings = PrometheusSettings()
    monitor: MonitorSettings = MonitorSettings()
    llm: LLMSettings = LLMSettings()
    agent: AgentSettings = AgentSettings()
    alert: AlertSettings = AlertSettings()
    chat: ChatSettings = ChatSettings()
    metrics: MetricsSettings = MetricsSettings()
    inspection: InspectionSettings = InspectionSettings()
    storage: StorageSettings = StorageSettings()
    logging: LoggingSettings = LoggingSettings()
    auth: AuthSettings = AuthSettings()


# 顶层字段 → 子 dataclass 的映射
_SUB_MODELS: dict[str, type] = {
    "app": AppSettings,
    "server": ServerSettings,
    "prometheus": PrometheusSettings,
    "monitor": MonitorSettings,
    "llm": LLMSettings,
    "agent": AgentSettings,
    "alert": AlertSettings,
    "chat": ChatSettings,
    "metrics": MetricsSettings,
    "inspection": InspectionSettings,
    "storage": StorageSettings,
    "logging": LoggingSettings,
    "auth": AuthSettings,
}


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
        # 未提供默认值的变量缺失：给空字符串（避免启动失败），同变量只提示一次
        if name not in _warned_missing_vars:
            _warned_missing_vars.add(name)
            logger.warning("环境变量 %s 未设置且无默认值，已按空字符串处理", name)
        return ""
    return value


def interpolate(text: str) -> str:
    """把文本中的 ${VAR} / ${VAR:-default} 全部替换为环境变量值。"""
    return _ENV_REF.sub(_env_value, text)


# ---------------------------------------------------------------------------
# YAML → dataclass
# ---------------------------------------------------------------------------
_TRUE_WORDS = frozenset({"1", "true", "yes", "on"})
_FALSE_WORDS = frozenset({"0", "false", "no", "off"})


def _as_scalar(value: Any, typ: type, *, what: str) -> Any:
    """把 YAML 值严格规约到目标标量类型；非法值抛 ConfigError。

    安全相关配置不能走 ``bool("tru") == True`` 那类宽松转换，更不能把无法识别的
    字符串静默当成 False。例如 ``auth.enabled: tru`` 过去会关闭鉴权，属于 fail-open。
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
        raise ConfigError(f"{what} 必须是布尔值 true/false，得到: {value!r}")
    if typ is str:
        if not isinstance(value, str):
            raise ConfigError(f"{what} 必须是字符串，得到: {value!r}")
        return value
    if typ is int:
        if isinstance(value, bool):
            raise ConfigError(f"{what} 必须是整数，得到: {value!r}")
        try:
            return int(value)
        except (TypeError, ValueError) as exc:
            raise ConfigError(f"{what} 必须是整数，得到: {value!r}") from exc
    if typ is float:
        if isinstance(value, bool):
            raise ConfigError(f"{what} 必须是数字，得到: {value!r}")
        try:
            result = float(value)
        except (TypeError, ValueError) as exc:
            raise ConfigError(f"{what} 必须是数字，得到: {value!r}") from exc
        if not math.isfinite(result):
            raise ConfigError(f"{what} 必须是有限数字，得到: {value!r}")
        return result
    try:
        return typ(value)
    except (TypeError, ValueError) as exc:
        raise ConfigError(f"{what} 类型无效，得到: {value!r}") from exc


def _build_dataclass(model_type: type, data: Any, *, prefix: str) -> Any:
    """递归构造严格配置 dataclass；未知键、类型错误与缺失映射直接报错。"""
    if not isinstance(data, dict):
        raise ConfigError(f"配置节 {prefix or '<root>'} 必须是映射，得到: {data!r}")
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
    """跨字段配置约束；只在服务启动加载时执行。"""
    ins = settings.inspection
    if not 30 <= ins.interval_seconds <= 86400:
        raise ConfigError("inspection.interval_seconds 必须在 30~86400 秒之间")
    if ins.ttl_hours < 1:
        raise ConfigError("inspection.ttl_hours 必须大于等于 1")
    if ins.max_rows < 1:
        raise ConfigError("inspection.max_rows 必须大于等于 1")



def _build_settings(data: dict[str, Any]) -> Settings:
    """从解析后的 dict 递归构造严格 Settings。"""
    settings = _build_dataclass(Settings, data, prefix="")
    _validate_settings(settings)
    return settings

# ---------------------------------------------------------------------------
# 加载入口
# ---------------------------------------------------------------------------
def default_settings() -> Settings:
    """返回默认 Settings（文件缺失 / 解析失败时兜底，保证可启动）。"""
    return Settings()


def load_settings(path: str | Path | None = None) -> Settings:
    """读取并解析 config/settings.yaml，返回 Settings。

    - 解析前先尽力加载根目录 .env（python-dotenv 可选，失败忽略）；
    - 文件缺失 / pyyaml 未装 / 解析失败 → 抛 ConfigError，拒绝带着不确定配置启动。
    """
    _load_dotenv_if_available()

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
        data = yaml.safe_load(interpolate(raw)) or {}
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


def _load_dotenv_if_available() -> None:
    """尝试加载根目录 .env（python-dotenv 可选）；失败静默交给调用方。"""
    try:
        from dotenv import load_dotenv

        load_dotenv(ROOT_DIR / ".env", override=False)
    except Exception:  # 未安装 python-dotenv / 其它异常均忽略
        pass
