"""容器部署初始化与启动；只处理部署目录，不连接模型、SSH 或监控服务。"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import secrets
import sys
import tempfile
from contextlib import contextmanager
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CONFIG_FILES = ("settings.yaml", "hosts.yaml", "metrics.yaml", "rules.yaml",
                "actions.yaml", "agent_actions.yaml", "diagnostics.yaml")
OPTIONAL_ENV = (
    "MODEL_API_KEY", "MODEL_BASE_URL", "MODEL_NAME", "MODEL_TIMEOUT",
    "MODEL_MAX_RETRIES", "MODEL_MAX_TOKENS", "MODEL_TOKEN_LIMIT_PARAM", "OPS_REDACTION_SECRETS",
)


class DeploymentError(RuntimeError):
    """可由用户修正的部署错误；错误文案不包含凭据。"""


def atomic_write(path: Path, content: str, *, mode: int = 0o600) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=".deploy-", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        if os.name == "posix":
            os.chmod(temporary, mode)
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


def read_credentials(runtime: Path) -> dict[str, str]:
    from dotenv import dotenv_values

    path = runtime / "compose.env"
    if not path.is_file():
        raise DeploymentError("部署尚未初始化，或 compose.env 丢失；请检查部署目录和备份。")
    values = {key: value for key, value in dotenv_values(path, interpolate=False).items()
              if value is not None}
    from src.utils.config_migration import reject_legacy_environment
    reject_legacy_environment(values)
    validate_credentials(values)
    return values


def validate_credentials(values: dict[str, str]) -> None:
    try:
        tokens = json.loads(values["OPS_API_TOKENS"])
        admins = json.loads(values.get("OPS_ADMIN_IDENTITIES", '["admin"]'))
        approvers = json.loads(values.get("OPS_APPROVER_IDENTITIES", '["admin"]'))
        user_token = values["AIOPS_USER_TOKEN"]
        if not isinstance(tokens, dict) or not tokens:
            raise ValueError
        if any(not isinstance(k, str) or not k or not isinstance(v, str) or not v
               for k, v in tokens.items()):
            raise ValueError
        identities = set(tokens.values())
        if (not isinstance(admins, list) or not admins
                or not isinstance(approvers, list) or not approvers
                or any(not isinstance(x, str) or x not in identities for x in [*admins, *approvers])
                or tokens.get(user_token) not in admins
                or "alertmanager" not in identities
                or not values.get("GF_SECURITY_ADMIN_PASSWORD")):
            raise ValueError
    except (KeyError, ValueError, TypeError):
        raise DeploymentError("部署凭据不完整或身份映射无效；请恢复 compose.env，程序不会自动轮换已有密钥。") from None


@contextmanager
def initialization_lock(runtime: Path):
    runtime.mkdir(parents=True, exist_ok=True)
    with (runtime / ".init.lock").open("a+") as handle:
        if os.name == "posix":
            import fcntl
            try:
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise DeploymentError("另一个部署初始化正在运行，请稍后重试。") from None
        yield


def initialize(runtime: Path, *, templates: Path,
               environment: dict[str, str] | None = None) -> str:
    from src.utils.config_migration import check_runtime, reject_legacy_environment
    reject_legacy_environment(os.environ if environment is None else environment)
    check_runtime(runtime)
    with initialization_lock(runtime):
        return _initialize(runtime, templates=templates, environment=environment)


def _initialize(runtime: Path, *, templates: Path,
               environment: dict[str, str] | None = None) -> str:
    """幂等初始化容器目录；保留已保存的配置、凭据和演示状态。"""
    environment = os.environ if environment is None else environment
    runtime.mkdir(parents=True, exist_ok=True)
    if os.name == "posix":
        runtime.chmod(0o700)
    marker = runtime / "deployment.json"
    existing_credentials = (runtime / "compose.env").is_file()
    metadata = {}
    if marker.exists():
        try:
            metadata = json.loads(marker.read_text(encoding="utf-8"))
            if (metadata.get("version") not in {1, 2}
                    or metadata.get("config_source") not in {"bundled", "legacy", "container"}):
                raise ValueError
        except (ValueError, KeyError, TypeError, AttributeError):
            raise DeploymentError("deployment.json 无效；请恢复部署元数据后重试。") from None
    # 完整部署绝不补默认文件；升级必须使用已经同步好的持久化配置。
    existing_deployment = bool(metadata.get("initialized") or
                               metadata.get("config_source") == "legacy" or
                               (existing_credentials and not metadata))
    if existing_deployment:
        if not existing_credentials:
            raise DeploymentError("已有部署的 compose.env 丢失；请从备份恢复，拒绝重新生成凭据。")
        for name in CONFIG_FILES:
            if not (runtime / "config" / name).is_file():
                if not marker.exists():
                    atomic_write(marker, json.dumps({"version": 2, "config_source": "container", "credentials_initialized": True}) + "\n")
                raise DeploymentError(f"缺少初始化配置 {name}；已有部署必须从备份恢复，拒绝覆盖为默认配置。")
    source = "container"
    checkpoint = {**metadata, "version": 2, "config_source": source}
    if metadata.get("initialized"):
        checkpoint["initialized"] = True
    if not marker.exists():
        atomic_write(marker, json.dumps(checkpoint) + "\n")

    if (runtime / "compose.env").exists():
        values = read_credentials(runtime)
    else:
        if (metadata.get("initialized") or metadata.get("credentials_initialized")
                or existing_deployment
                or (bool(metadata) and any((runtime / "config" / name).is_file()
                                          for name in CONFIG_FILES))
                or (runtime / "secrets" / "assistant.json").exists()):
            raise DeploymentError("已有部署的 compose.env 丢失；请从备份恢复，拒绝重新生成凭据。")
        user_token, alert_token = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
        values = {
            "OPS_API_TOKENS": json.dumps({user_token: "admin", alert_token: "alertmanager"}),
            "OPS_ADMIN_IDENTITIES": '["admin"]',
            "OPS_APPROVER_IDENTITIES": '["admin"]',
            "AIOPS_USER_TOKEN": user_token,
            "GF_SECURITY_ADMIN_USER": "admin",
            "GF_SECURITY_ADMIN_PASSWORD": secrets.token_urlsafe(24),
        }
    values.setdefault("OPS_ADMIN_IDENTITIES", '["admin"]')
    for key in OPTIONAL_ENV:
        if environment.get(key):
            values[key] = environment[key]
    validate_credentials(values)
    if any("\n" in value or "\r" in value for value in values.values()):
        raise DeploymentError("部署环境变量必须为单行值。")
    # 该文件由 python-dotenv 按字面值读取，不再作为 Compose 插值来源。
    atomic_write(runtime / "compose.env", "".join(
        f"{key}={json.dumps(value, ensure_ascii=False)}\n" for key, value in sorted(values.items())
    ))

    # 凭据写入成功即记录，不等配置和派生文件全部完成；中断后禁止静默轮换。
    atomic_write(marker, json.dumps({**checkpoint, "credentials_initialized": True}) + "\n")

    config_dir = runtime / "config"
    config_dir.mkdir(exist_ok=True)
    for name in CONFIG_FILES:
        target = config_dir / name
        if not target.exists():
            original = templates / name
            if not original.is_file():
                raise DeploymentError(f"缺少初始化配置 {name}；请恢复配置来源后重试。")
            atomic_write(target, original.read_text(encoding="utf-8"))
    for name in ("assistant-data", "assistant-logs", "prometheus-targets", "textfile"):
        (runtime / name).mkdir(exist_ok=True)
    target_file = runtime / "prometheus-targets" / "assistant.yml"
    if not target_file.exists():
        atomic_write(target_file, "[]\n", mode=0o644)
    # 初始化演示指标只发生一次，重新 up 不会清除正在演示的故障。
    demo = runtime / "textfile" / "aiops_demo_fault.prom"
    if not demo.exists():
        atomic_write(demo, "".join(
            f"# TYPE aiops_demo_{name}_percent gauge\naiops_demo_{name}_percent 0\n"
            for name in ("cpu", "memory", "disk")
        ), mode=0o644)

    auth_values = {key: value for key, value in values.items()
                   if key in OPTIONAL_ENV or key in {
                       "OPS_API_TOKENS", "OPS_ADMIN_IDENTITIES", "OPS_APPROVER_IDENTITIES"}}
    atomic_write(runtime / "secrets" / "assistant.json", json.dumps(auth_values, ensure_ascii=False, sort_keys=True))
    token = next(key for key, identity in json.loads(values["OPS_API_TOKENS"]).items()
                 if identity == "alertmanager")
    # 非 root 官方镜像需要可读文件；分别挂载各自目录，不暴露其它服务的密钥。
    atomic_write(runtime / "alertmanager" / "token", token, mode=0o644)
    atomic_write(runtime / "grafana" / "admin_password", values["GF_SECURITY_ADMIN_PASSWORD"], mode=0o644)
    atomic_write(runtime / "grafana" / "admin_user", values.get("GF_SECURITY_ADMIN_USER", "admin"), mode=0o644)
    # 旧版 secrets 目录为 0700；Assistant 本身以容器 root 读取自己的 0600 文件。
    for name in ("alertmanager", "grafana", "prometheus-targets", "textfile"):
        if os.name == "posix":
            (runtime / name).chmod(0o755)
    if os.name == "posix":
        target_file.chmod(0o644)
    atomic_write(marker, json.dumps({**checkpoint, "initialized": True}) + "\n")
    return source



def serve(secret_file: Path) -> None:
    try:
        values = json.loads(secret_file.read_text(encoding="utf-8"))
        if not isinstance(values, dict) or any(not isinstance(v, str) for v in values.values()):
            raise ValueError
    except (OSError, ValueError):
        raise DeploymentError("Assistant 部署凭据未就绪，请重新运行初始化服务。") from None
    from src.utils.config_migration import reject_legacy_environment
    reject_legacy_environment(values)
    pending = Path("/app/data/config-migrations/current.json")
    if pending.exists() and json.loads(pending.read_text()).get("status") != "complete":
        raise DeploymentError("配置迁移未完成，请先运行 migrate-config")
    os.environ.update(values)
    enabled = "true" if all(values.get(key) for key in ("MODEL_API_KEY", "MODEL_BASE_URL", "MODEL_NAME")) else "false"
    os.environ.setdefault("OPS_LLM_ENABLED", enabled)
    os.environ.setdefault("OPS_AGENT_ENABLED", enabled)
    os.execvp("uvicorn", ["uvicorn", "src.main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1"])


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["init", "serve", "show-token", "show-grafana-password", "migrate-config"])
    parser.add_argument("--runtime", type=Path, default=Path("/runtime"))
    parser.add_argument("--project-root", type=Path, default=Path("/migration-project"))
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    if args.dry_run and args.command != "migrate-config":
        parser.error("--dry-run 仅用于 migrate-config")
    try:
        if args.command == "migrate-config":
            from src.utils.config_migration import migrate
            with initialization_lock(args.runtime):
                changed = migrate(args.runtime, args.project_root, dry_run=args.dry_run)
            print(("迁移预览：" if args.dry_run else "迁移完成：") + ", ".join(changed))
        elif args.command == "init":
            from src.utils.config_migration import _dotenv, reject_legacy_environment
            bootstrap = Path("/bootstrap-project/.env")
            if bootstrap.is_file():
                reject_legacy_environment(_dotenv(bootstrap.read_text(encoding="utf-8")))
            source = initialize(args.runtime, templates=ROOT / "deploy" / "templates" / "assistant")
            print("部署初始化完成；配置来源=" + source + "；已有配置与密钥已保留。")
        elif args.command == "serve":
            serve(Path("/run/assistant-secrets/assistant.json"))
        else:
            values = read_credentials(args.runtime)
            key = "AIOPS_USER_TOKEN" if args.command == "show-token" else "GF_SECURITY_ADMIN_PASSWORD"
            print(values[key])
    except (DeploymentError, OSError, ValueError) as exc:
        print("部署失败：" + (str(exc) if isinstance(exc, ValueError) or isinstance(exc, DeploymentError) else "文件操作失败，请检查权限"), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
