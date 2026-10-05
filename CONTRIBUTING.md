# AIOps-Assistant 贡献与维护约定

本文是代码、配置、注释、文档和测试的共同维护基线。任何功能修改都必须把它们视为
同一项交付，不能只改实现后把说明留给以后。

## 1. 变更完成的定义

一次代码变更只有同时满足以下条件才算完成：

1. 实现与接口行为正确。
2. 相关测试新增或更新，并保持离线可运行。
3. 源码注释和 docstring 描述的是当前行为，不保留已失效方案。
4. 配置字段、默认值、约束和生效时机已同步到配置样例与 `docs/configuration.md`。
5. 用户可见行为已同步到 README 或对应专题文档。
6. 数据库变更包含 schema 版本、迁移、结构测试和 `docs/database.md`。
7. 安全边界变化经过显式审查，没有扩大模型或调用方的自由输入范围。

如果变更不影响某一项，应在评审时说明“无需更新”的理由，而不是默认忽略。

## 2. 文档同步矩阵

| 变更类型 | 必须检查的说明 |
|---|---|
| API 路径、请求、响应或状态码 | README、`docs/api.md`、`docs/guide.md`、路由 docstring、API 测试 |
| 鉴权、身份、审批范围或公开端点 | `docs/auth.md`、README 安全说明、依赖/路由注释 |
| 控制台视图、状态或管理员操作 | `docs/console.md`、前端注释、DOM 安全与交互测试 |
| `settings.yaml` 字段 | 配置文件注释、`docs/configuration.md`、配置加载测试 |
| 指标或 `metrics.yaml` | 配置文件、配置指南、指标目录注释、监控/巡检/工具测试 |
| SSH 命令目录或参数 schema | 对应 YAML 注释、配置指南、安全测试和命令目录测试 |
| 数据库表、列、状态或清理策略 | `docs/database.md`、DDL/迁移注释、schema 与 Store 测试 |
| 值班诊断、故障状态或恢复步骤 | `docs/troubleshooting.md`、`docs/guide.md`、相关测试 |
| 组件装配、插件或降级路径 | README 架构、模块 docstring、Container 测试 |
| 运行快照、配置事务或主机登记 | `docs/architecture.md`、`docs/console.md`、并发/回滚/跨主机测试 |
| 镜像、Compose、部署脚本或模板 | `docs/deployment.md`、README、使用/鉴权/配置指南、部署测试与 CI |
| 版本号或发布说明 | 更新程序版本、README 和对应测试 |

测试数量会变化，交付信息以实际 `pytest` 输出为准。

## 3. 注释原则

- 解释“为什么这样设计”和安全/并发不变量，不逐行翻译代码。
- 删除已被当前实现取代的历史过程；仍影响兼容性的迁移历史留在迁移代码附近。
- 不用“永远安全”“绝不会泄露”等超出实现覆盖面的承诺。
- 注释中的接口、字段、默认值、文件名和状态必须可在代码中直接核对。
- 写清实际适用范围，例如“配置应用仅支持单 worker”。
- 凭据、真实地址和真实运维数据不得进入注释、示例或测试输出。

## 4. 分层约束

```text
api → core → interfaces
              ↑
        plugins / storage
```

- `api/` 只做 HTTP 校验、鉴权、异常映射和转发。
- `core/` 不创建具体插件或导入 `src.agent`；现存静态目录/字段常量的窄导入见
  [架构说明](docs/architecture.md)，新增代码不要把它扩展为运行时插件依赖。
- `interfaces/` 不依赖第三方实现。
- `plugins/` 放外部系统适配器和 SSH/LLM 实现。
- `storage/` 放 SQLite 与内存存储实现。
- `container.py` 是装配入口；新增能力应在这里明确缓存、降级和生命周期。
- LangChain、LangGraph、Paramiko 等可选重依赖保持惰性导入。

## 5. 安全不变量

- 用户和模型不能提交任意 PromQL。
- 模型不能提交任意 Shell、SSH 主机或连接凭据。
- 命令必须来自管理员配置的模板；字符串参数必须全匹配正则，整数参数必须受范围约束。
- Agent 写命令必须经过人工审批，不提供关闭审批的配置开关。
- 规则自动处置与 Agent 写命令是两条独立路径；不要把审批下沉到通用执行器。
- SSH 默认拒绝未知主机密钥。
- 新增输出面必须同时考虑长度上限和敏感信息泄露。
- 新增读能力必须限制资源、路径、条数和字符数；新增写能力必须可审计且具备幂等策略。

## 6. 配置约定

- 密钥来自 `.env`、部署专用凭据文件或运行环境；YAML 只保留占位符。
- `deploy/config/` 是新 Compose 部署的独立安全模板，不能从开发机运行配置直接打包。
- 初始化必须幂等，保留既有配置、Token 和数据；升级兼容性需有回归测试。
- 新增字段必须有类型、默认值、范围、失败语义和生效时机。
- `settings.yaml` 和 `metrics.yaml` 的错误属于启动配置错误，应拒绝不确定启动。
- 可选命令目录错误可以关闭对应能力，但必须记录明确日志，不能静默放宽。
- 管理页可编辑字段由 `PUT /admin/config` 校验、备份并热应用；页面外手改 YAML 须调用
  `POST /admin/config/apply`。监听地址、SQLite/日志位置及环境变量密钥仍需重启。
- 新增配置字段时明确归类为“可热应用”或“需重启”，并同步运行快照白名单与测试。
- 管理写锁仅在当前单进程内有效，多实例共享同一配置目录不在支持范围内。

## 7. 数据库约定

- DDL 变更必须递增 `SCHEMA_VERSION`。
- 迁移只能向前追加，并保留所有旧版本迁移分支。
- 新库结构必须与逐版本迁移后的结构一致。
- 删除父子数据必须在同一事务中显式完成，不依赖未统一启用的 SQLite 外键。
- 时间戳统一使用 `src/utils/time.py` 的 UTC 秒级格式。

## 8. 测试与提交前检查

完整命令与人工验收步骤见 [本机测试与验收](docs/testing.md)。

测试不得访问真实 DeepSeek、Prometheus、SSH 或公共网络，也不得写入真实数据库和日志。
运行离线测试前，应在测试进程环境中设置 `LANGSMITH_TRACING=false`、
`LANGCHAIN_TRACING=false` 和 `LANGCHAIN_TRACING_V2=false`。

```bash
LANGSMITH_TRACING=false LANGCHAIN_TRACING=false LANGCHAIN_TRACING_V2=false \
  conda run -n langchain1.2 python -m pytest -q
node --check src/web/console.js
node --test tests/console_ui.test.cjs
python -m compileall -q src tests init.py
git diff --check
```

PowerShell 中先用 `$env:LANGSMITH_TRACING='false'` 等语句设置上述三个环境变量，
再运行 `conda run`。

根据改动再运行针对性检查，例如：

```bash
conda run -n langchain1.2 python -m pytest -q tests/test_no_langchain_import.py
rg -n "src\.agent" src/core
```

提交前还要确认：

- `git status --short` 中没有 `.env`、数据库、日志、缓存或参考资料。
- 新增配置已有安全默认值和文档。
- 新增接口默认受鉴权保护，豁免必须显式说明。
- 文档示例能对应当前请求/响应结构。

## 部署验证与依赖

`requirements-runtime.txt` 声明运行依赖，`requirements.txt` 加入测试依赖；
`requirements-lock.txt` 固定已验证的直接与传递依赖版本，是约束快照，不是带哈希的安装锁。
CI 与 Docker 构建均使用 `-c requirements-lock.txt`。更新时在 Python 3.13 的干净环境
重新解析依赖、检查 `pip check`，同步快照并运行离线测试和 Compose 冒烟测试。

离线测试不连接真实 SSH 或模型；独立 CI 部署任务只测试临时 Docker 服务，
验证健康状态、鉴权、告警链路及重建后的会话与配置持久化。该任务需要网络下载镜像，
本地验证必须使用独立项目名、部署目录和端口，不能复用真实部署数据。
