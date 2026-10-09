# 部署与升级

0.6.2 仅提供容器运行方式。宿主机需要 Docker Engine / Docker Desktop 与
Compose 2.24+；Docker Desktop 使用 Linux 容器。无需宿主机 Python、Conda 或项目依赖。
Assistant 保持单进程、单 worker、SQLite schema 7。

## 首次启动

首次连接与问答按 [快速入门](quickstart.md) 操作。在项目根目录执行：

```bash
docker compose up -d --build --wait --wait-timeout 180
docker compose ps --all
docker compose run --rm --no-deps init show-token
```

控制台为 `http://127.0.0.1:8000/console`。初始化生成管理员、Alertmanager Token
和 Grafana 初始密码；初始化日志不输出密钥。Grafana 为 `http://127.0.0.1:3000`，
新部署账号为 `admin`，显式查看初始密码：

```bash
docker compose run --rm --no-deps init show-grafana-password
```

Grafana 页面改密后以其数据库为准。重复初始化保留有效 Token、已有配置、业务数据和演示指标。

## 可选配置：模型、端口与 SSH

管理员在“系统管理”配置 SSH、用户 Token 和 DeepSeek；页面配置优先于部署环境。
模型保存只验证参数，点击“测试连接”才请求模型；SSH 检查仅验证认证，不执行命令。
无模型密钥仍可监控、巡检和关键词问答。已有固定关闭开关需在管理页按需启用 Agent / LLM。

根 `.env` 仅供 Compose 参数与可选模型/SSH 环境配置，例如：

```env
ASSISTANT_HOST_PORT=8000
GRAFANA_HOST_PORT=3000
PROMETHEUS_HOST_PORT=9090
ALERTMANAGER_HOST_PORT=9093
NODE_EXPORTER_HOST_PORT=9100
DEEPSEEK_API_KEY=
DEEPSEEK_MODEL=deepseek-v4-flash
```

复制 `.env.example` 后按需填写，同名进程环境变量优先。容器应用不自动读取宿主机
`.env`，业务身份来自持久化部署凭据和页面 Token。
`DEEPSEEK_BASE_URL`、`DEEPSEEK_TIMEOUT=30`（有限正秒数）、
`DEEPSEEK_MAX_RETRIES=2`（非负整数）、`DEEPSEEK_MAX_TOKENS=2048`（正整数）
可传入初始化服务。超时和重试是单次 SDK 请求约束，不是整轮 Agent 的总时限。

修改 Compose 环境或部署凭据后重建；普通 `restart` 不会重新派生凭据：

```bash
docker compose up -d --build --force-recreate --wait --wait-timeout 180
```

非空模型/SSH 环境值覆盖已保存的部署值，空值不清除旧值。撤销部署密钥需同时移除
`.env` / 进程值及 `.runtime/compose.env` 中对应字段，再重建。页面管理的模型密钥
可直接保留、替换或明确清除；页面 Token 轮换/撤销后对后续请求生效。

部署式 SSH 文件放入 `.runtime/ssh/`，配置使用容器内路径 `/run/ssh/`；
页面管理的 SSH 文件由服务端写入受限凭据目录。密码和私钥认证都需要可信 known_hosts，
每条主机密钥占一行，不自动信任未知或不匹配的密钥。主机配置引用凭据后，还需分别授予
诊断或写命令权限。初始模板不授予 SSH 权限，部署不会安装远程 Exporter。
Linux 上初始化以容器 root 写入，手工管理可能需要 sudo；不要放宽私钥的读取权限。

发布端口默认绑定部署机 `127.0.0.1`。远程浏览器可使用 SSH 隧道：

```bash
ssh -L 8000:127.0.0.1:8000 ops@server.example
```

按实际发布端口调整目标；长期访问使用带 TLS 的反向代理并保留鉴权。

## 配置与数据位置

| 位置 | 用途 |
|---|---|
| `deploy/config/` | 七份首次部署模板，修改模板不覆盖运行配置 |
| `.runtime/config/` | 实际运行 YAML；管理页保存写这里 |
| 根 `.env` | Compose 参数与可选外部凭据，不提供业务登录 Token |
| `.runtime/compose.env` | 持久化部署 Token、角色及可选环境值，不作 Compose 的 --env-file |
| `.runtime/deployment.json` | 容器初始化元数据 v2；兼容读取 v1 |
| `.runtime/secrets/` | Assistant 专用派生凭据 |
| `.runtime/alertmanager/`、`.runtime/grafana/` | 各服务专用派生凭据 |
| `.runtime/assistant-data/` | SQLite、配置备份和 admin-credentials 页面凭据 |
| `.runtime/assistant-logs/` | 审计与巡检日志 |
| `.runtime/ssh/` | 部署式 SSH 文件 |
| `.runtime/prometheus-targets/` | 动态发现目标 |
| `.runtime/textfile/` | 容器演示告警合成指标 |
| Docker 命名卷 | Prometheus、Alertmanager、Grafana 数据 |

`AIOPS_RUNTIME_DIR` 可指定目录；全部管理命令必须保持相同目录与项目名。
独立栈同时隔离项目名、端口和运行目录。Prometheus 配置与目标格式见
[配置指南](configuration.md#compose-的-prometheus-主配置与目标列表)。

新部署从模板初始化。已完成部署缺少任何一份 YAML 会拒绝启动，需恢复备份；
已有部署丢失 compose.env 也不会生成新 Token。仅派生凭据丢失可在保留 compose.env
时重新初始化修复。初始化中断保留凭据检查点，可修复原因后重试。

## 日常操作

```bash
docker compose ps --all
docker compose logs --tail 200 assistant init
docker compose restart assistant
docker compose down
docker compose up -d --wait --wait-timeout 180
```

代码更新后用 `up -d --build --wait`。普通 down 保留数据，`down -v` 会删除监控卷。
手工改 YAML 后由管理员“应用磁盘配置”；数据库/日志路径等启动字段需要重建。
已有 Python 时可选用标准库包装脚本 `python scripts/stack.py`，
支持 doctor、up、down、status、logs、show-token、show-grafana-password；
该脚本只操作 Docker，不能直接启动宿主机服务。doctor 仅检查 Docker/Compose 与部署配置。

## 从旧开发栈升级

升级前停止旧服务并备份完整运行目录与三个命名卷，保持原 Compose 项目名。
具体备份方法见 [数据库说明](database.md)。

如旧版本只有根 config/ 和 compose.env，先把七份 YAML 同步至运行目录 config/，
保留已有运行配置中的更新和主机关联，确认备份后再更新至本版；本版不再挂载根 config/。
将旧 SSH 文件移入运行目录 ssh/，引用改为 /run/ssh/ 路径。旧凭据中发布端口搬至根 .env。

已有运行目录升级时保留鉴权、主机授权、规则、模型、Token 和历史。init 兼容 v1 元数据，
写入 v2；移除废弃的 `server` 段、`auth.enabled`、`alert.trend_step` 和 `app.version`。
迁移前原文备份至 `assistant-data/config-backups/obsolete-settings-<内容哈希>.yaml`；
已有备份保留，迁移可重复运行。备份或写入失败时保留原 settings 并拒绝继续初始化。
Token、角色、已有授权及其他有效开关保持，未知字段交给配置校验，不会静默删除。
容器始终监听 8000，外部端口由 Compose 控制，版本号取自代码。
未配置主机授权的普通用户默认无主机访问权，管理员从“用户主机授权”补充，不能默认全开放。
已有具名会话保留，需同名身份及对应主机授权；匿名会话不会自动认领或自动删除。SQLite schema 仍为 7。

回退前停机，恢复对应版本配置/元数据备份；若回退更早挂载方式，需合并期间运行配置变更。
不要删除凭据或初始化检查点来“重试”。

## 部署失败排查

| 现象 | 处理 |
|---|---|
| Docker 引擎不可达 | 启动 Docker 服务；Windows 确认 Linux 容器模式 |
| --wait 不识别 | 使用 Compose 2.24+ |
| 镜像或依赖下载失败 | 核对 Docker 网络、代理与仓库，修复后重试 |
| 端口占用 | 修改根 .env 的对应 HOST_PORT 后重新 up |
| init 退出非零 | 查看 init 日志，恢复缺失配置/凭据，不删业务目录 |
| Assistant 未就绪 | 查看配置校验、Assistant 日志与 Prometheus 状态 |
| 新主机未采集 | 核对目标列表、Prometheus Targets、网络和文件可读性 |
| Token 不匹配 | 核对项目名、运行目录、端口和身份映射；从对应 init 显式查看 |

生产镜像只包含运行文件，不包含宿主机凭据、运行数据或测试；测试使用独立 test 构建目标。
Python 依赖由 requirements-lock.txt 约束，验证命令见 [容器测试与验收](testing.md)。

## 页面凭据持久化与 Linux 宿主机监控

页面配置保存到 `.runtime/assistant-data/admin-credentials/`，重建 Assistant 或重跑 init
均保留。该目录包含敏感 state.json 和 SSH 版本资产，不进入普通 YAML 备份或数据库历史；
随整个运行目录通过受控加密渠道备份。恢复必须同时恢复 state.json 与 assets，并在停机
状态下操作；不要将凭据目录放入代码仓库。旧资产为旧请求保留，清理须在服务停止并核对
当前引用后进行。容器权限仍使用原部署边界，未增加默认 host PID/network 或根目录挂载。

Compose 的 `local-node` 继续代表容器环境。要监控独立 Linux 宿主机，在该宿主机部署
node_exporter（可信来源、固定版本），以独立 systemd 服务运行，例如二进制位于
`/usr/local/bin/node_exporter`：

```ini
[Unit]
Description=Linux node_exporter
After=network.target
[Service]
User=node_exporter
Group=node_exporter
ExecStart=/usr/local/bin/node_exporter --web.listen-address=:9100
Restart=on-failure
NoNewPrivileges=true
ProtectSystem=strict
ProtectHome=true
[Install]
WantedBy=multi-user.target
```

由宿主机管理员安装服务账户、核验二进制并启用服务；9100 仅允许 Prometheus 访问，
按实际安全边界配置 TLS/认证或可信内网。确保 Prometheus 容器可访问宿主机的实际网络
地址；不要将容器内 127.0.0.1 当作宿主机。自管模式在“监控主机”填写可达地址:9100 并
登记；外部模式先在外部 Prometheus 配置 scrape target，等待发现后再选目标登记。
核对 /targets 的 up、标签 job/instance 和 17 项即时/趋势状态，再关联 SSH 并单独授权。
本次不自动安装远程 Exporter，也不扩大 Compose 容器权限。
