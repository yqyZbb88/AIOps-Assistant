"""配置加载：递归 ${ENV} / ${ENV:-default} 插值 + 转 dataclass。

职责：
- 读取 config/settings.yaml，把字符串值里的 `${VAR}` / `${VAR:-default}`
  占位替换为环境变量（或默认值），代码不再硬编码可变参数 / 密钥；
- 解析成嵌套的 `Settings` dataclass，各层代码按需取用；
- 容错回退：配置文件缺失 / pyyaml 未安装时返回全默认值，服务照常启动。

敏感信息约束：本模块只做字符串替换，绝不在配置里产生明文密钥来源；
真实密钥只存在于 .env / 环境变量（见 README「安全说明」）。
"""
from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass, fields
from pathlib import Path
from typing import Any, get_type_hints

logger = logging.getLogger("ops_agent.config")

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
    name: str = "ops-agent"
    version: str = "0.2.0"


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
    name: str = "ops_assistant"
    debug: bool = False


@dataclass(frozen=True)
class AlertSettings:
    recent_limit: int = 100
    trend_minutes: int = 60
    trend_step: str = "5m"


@dataclass(frozen=True)
class LoggingSettings:
    level: str = "INFO"
    audit: bool = True


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
    logging: LoggingSettings = LoggingSettings()


# 顶层字段 → 子 dataclass 的映射
_SUB_MODELS: dict[str, type] = {
    "app": AppSettings,
    "server": ServerSettings,
    "prometheus": PrometheusSettings,
    "monitor": MonitorSettings,
    "llm": LLMSettings,
    "agent": AgentSettings,
    "alert": AlertSettings,
    "logging": LoggingSettings,
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


def interpolate_mapping(data: Any) -> Any:
    """递归遍历任意（YAML 解析后的）结构，对字符串节点做环境插值。"""
    if isinstance(data, str):
        return interpolate(data)
    if isinstance(data, list):
        return [interpolate_mapping(item) for item in data]
    if isinstance(data, dict):
        return {key: interpolate_mapping(value) for key, value in data.items()}
    return data


# ---------------------------------------------------------------------------
# YAML → dataclass
# ---------------------------------------------------------------------------
def _as_scalar(value: Any, typ: type) -> Any:
    """把 YAML 值规约到目标标量类型（字符串→bool 做常见词解析）。"""
    if value is None or isinstance(value, typ):
        return value
    if typ is bool and isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    return typ(value)


def _build_settings(data: dict[str, Any]) -> Settings:
    """从解析后的 dict 构造 Settings；缺节时用对应默认 dataclass。"""
    kwargs: dict[str, Any] = {}
    for key, model_type in _SUB_MODELS.items():
        section = data.get(key)
        if isinstance(section, dict):
            hints = get_type_hints(model_type)
            kwargs[key] = model_type(
                **{
                    f.name: _as_scalar(section[f.name], hints[f.name])
                    for f in fields(model_type)
                    if f.name in section
                }
            )
        else:
            kwargs[key] = model_type()  # 缺节 → 默认值
    return Settings(**kwargs)


# ---------------------------------------------------------------------------
# 加载入口
# ---------------------------------------------------------------------------
def default_settings() -> Settings:
    """返回默认 Settings（文件缺失 / 解析失败时兜底，保证可启动）。"""
    return Settings()


def load_settings(path: str | Path | None = None) -> Settings:
    """读取并解析 config/settings.yaml，返回 Settings。

    - 解析前先尽力加载根目录 .env（python-dotenv 可选，失败忽略）；
    - 文件缺失 / pyyaml 未装 / 解析失败 → 记日志并返回默认 Settings。
    """
    _load_dotenv_if_available()

    file_path = Path(path) if path else Path(os.getenv("OPS_SETTINGS_FILE", DEFAULT_SETTINGS_FILE))
    if not file_path.is_file():
        logger.warning("配置文件不存在，使用默认配置: %s", file_path)
        return default_settings()

    try:
        import yaml
    except ImportError:
        logger.error("缺少可选依赖 pyyaml，使用默认配置：pip install pyyaml")
        return default_settings()

    try:
        with open(file_path, encoding="utf-8") as fh:
            raw = fh.read()
        data = yaml.safe_load(interpolate(raw)) or {}
        if not isinstance(data, dict):
            raise ConfigError("配置顶层必须是映射")
        settings = _build_settings(data)
    except ConfigError:
        raise
    except Exception as exc:  # yaml 语法 / 类型规约等错误统一收敛为默认
        logger.error("配置文件解析失败（%s），使用默认配置: %s", exc, file_path)
        return default_settings()

    logger.info("已加载配置: %s（app=%s v%s）", file_path, settings.app.name, settings.app.version)
    return settings


def _load_dotenv_if_available() -> None:
    """尝试加载根目录 .env（python-dotenv 可选）；失败静默交给调用方。"""
    try:
        from dotenv import load_dotenv

        load_dotenv(ROOT_DIR / ".env", override=False)
    except Exception:  # 未安装 python-dotenv / 其它异常均忽略
        pass
