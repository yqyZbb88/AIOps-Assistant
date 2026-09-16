# 开发工程师指南

本文面向修改 ops-agent 源码、增加指标、扩展工具、调整存储或发布版本的工程师。

## 一、技术栈

- Python 3.11～3.13
- FastAPI
- Pydantic 2
- LangChain 1.x / LangGraph
- Prometheus HTTP API
- Paramiko
- SQLite
- pytest

## 二、总体架构

```text
HTTP API
  ├── chat
  ├── alert
  ├── approvals
  ├── sessions
  └── inspections
        │
        ▼
Container 容器装配
  ├── MonitorSource / PrometheusMonitor
  ├── LLMClient / DeepseekLLMClient
  ├── RuleEngine + ActionExecutor
  ├── DiagnosticSource
  ├── AgentActionSource
  ├── SessionStore / HistoryStore / InspectionStore
  └── OpsAssistant / ChatService / OpsAgent
        │
        ├── Prometheus 指标目录
        ├── SSH 只读诊断
        └── SSH 写命令 + 人工审批
```

## 三、分层与依赖方向

```text
api → core → interfaces
              ↑
           plugins/storage
```

通用规则：

- `api/` 只做 HTTP 输入输出和异常映射
- `core/` 承载业务编排和规则
- `interfaces/` 放中性契约和数据类型
- `plugins/` 放 Prometheus、LLM、SSH 等实现
- `storage/` 放 SQLite、内存和归档实现
- `agent/` 放 LangChain/LangGraph 接线
- `utils/` 放无业务归属的基础工具

不要从 `core` 反向 import `plugins` 或 `agent`。

## 四、关键模块地图

| 模块 | 职责 |
|---|---|
| `src/main.py` | FastAPI 创建、路由挂载、lifespan |
| `src/container.py` | 依赖装配、缓存和降级 |
| `src/interfaces/metric.py` | 指标定义、样本和运行配置契约 |
| `src/metrics/catalog.py` | 17 个内置指标和 PromQL |
| `src/plugins/monitors/prometheus_monitor.py` | Prometheus HTTP 查询、selector 渲染、样本解析 |
| `src/core/inspection.py` | 巡检、阈值、等级、多行日志和调度 |
| `src/core/chat.py` | Agent-first、关键词兜底、会话历史 |
| `src/core/analysis.py` | 告警 AI 上下文和趋势查询 |
| `src/core/rule_engine.py` | alertname 匹配、自动处置、幂等抢占 |
| `src/agent/assistant.py` | LangGraph Agent、HITL、审批续跑 |
| `src/agent/tools.py` | 监控、巡检、诊断、写命令工具工厂 |
| `src/storage/db.py` | SQLite 连接、schema 和迁移 |
| `src/storage/session.py` | 会话和消息 |
| `src/storage/history.py` | 告警、处置、AI 建议 |
| `src/storage/inspection.py` | 异常巡检归档 |

## 五、必须遵守的硬性约束

### 1. 测试必须离线

测试不能访问：

- 真实 DeepSeek
- 真实 Prometheus
- 真实 SSH
- 外部网络

相关护栏见 `tests/conftest.py`。

### 2. 测试不能碰真实数据库和日志

- 真实数据库是 `data/ops_agent.db`
- `import src.main` 会按真实配置创建 app
- `tests/conftest.py` 会阻止真实 DB 和文件日志

修改模块级副作用时必须保留这些护栏。

### 3. 凭据不能进入模型上下文

- SSH host、password、private key 不进入工具参数
- 模型只选择命令 key 和语义参数
- 审计写入前必须走 `redact_sensitive`

### 4. 写命令必须经过 HITL

- `HumanInTheLoopMiddleware` 在工具执行前中断
- `agent_actions.yaml` 不提供关闭审批的配置
- 不允许 `edit` 决策
- 不允许把审批下沉到 `run()` 或 executor

### 5. SQLite schema 只追加

- 改 DDL 必须递增 `SCHEMA_VERSION`
- 增加迁移分支
- 保留旧迁移分支
- 新库和迁移结果必须一致
- 测试禁止修改真实数据库

### 6. 工具结果必须有时效说明

监控、告警、诊断、写命令和巡检工具返回给模型时必须带 `[数据时效]`。

- 实时指标：观测时间来自 Prometheus sample
- 活动告警：生成时间
- SSH：执行时间
- 巡检历史：`checked_at`，并标记为历史
- System prompt 明确“不能把历史当当前”

## 六、扩展指标

指标定义只放在 `src/metrics/catalog.py`。

新增指标步骤：

1. 增加 `MetricDefinition`
2. 加入 `METRIC_CATALOG`
3. 设置 `expr_template`、单位、方向、分组和别名
4. 如果支持告警趋势，设置 `range_expr_template`
5. 更新 `config/metrics.yaml`
6. 增加 Prometheus parser、selector、检查阈值测试
7. 更新 `configuration.md`

用户只修改 `config/metrics.yaml`：

```yaml
metrics:
  cpu.usage:
    enabled: true
    surfaces: { inspection: true, chat: true, alert_analysis: true }
    warning: 80
    critical: 90
    severity_enabled: true
```

不要允许用户覆盖 PromQL 或计算类型。

## 七、扩展聊天工具

当前策略：

- 每个 `surfaces.chat=true` 指标生成一个工具
- 工具名来自 `legacy_tool_name` 或 metric ID 转下划线
- 工具由 `src/agent/tools.py::build_tools` 生成
- 工具结果由 `src/core/capabilities.py::metric_text` 格式化

如果指标数量明显增加，应优先考虑统一多选工具：

```text
query_metrics(metric_ids: [enum])
```

而不是继续生成大量独立工具。

## 八、扩展 SSH 只读诊断

只修改 `config/diagnostics.yaml` 即可增加命令：

```yaml
commands:
  service_status:
    summary: 查看服务状态
    template: "systemctl status --no-pager -l {service}"
    params:
      service: { type: str, pattern: "^[A-Za-z0-9_.:@-]{1,128}$" }
```

约束：

- 恒不 sudo
- 参数必须声明 pattern 或 min/max
- 模板占位符必须与 params 完全一致
- 修改后重启，首次聊天时重建工具

## 九、扩展 Agent 写命令

修改 `config/agent_actions.yaml`：

```yaml
commands:
  restart_service:
    summary: 重启服务
    template: "systemctl restart {service}"
    sudo: true
    params:
      service: { type: str, pattern: "^[A-Za-z0-9_.:@-]{1,128}$" }
```

禁止：

- 新增关闭审批的字段
- 允许模型传 host/password
- 允许自由文本进入 shell
- 使用与监控/诊断工具重名的 key

新增写命令后必须回归：

- 未批准不执行
- reject 不执行
- approve 后执行
- 只能执行一次
- 审计记录正确
- 链式审批可继续

## 十、扩展规则与动作

`rules.yaml` 当前只按 `alertname` 匹配，不查 Prometheus 指标。

规则动作必须引用 `actions.yaml` 中的命令 key。

约束：

- `auto_execute=true` 必须配置 `target_instance`
- 同一告警可以匹配多条规则
- 自动动作必须有幂等抢占
- 写动作非零退出码视为失败
- 不要绕过 `ActionExecutor` 接口

## 十一、扩展存储

新增表时：

1. 修改 `src/storage/db.py` DDL
2. 递增 `SCHEMA_VERSION`
3. 增加迁移分支
4. 更新结构测试
5. 增加 store 实现
6. 在 `container.py` 装配
7. 评估 TTL、容量上限、备份和并发
8. 更新 `database.md`

不要直接让 core/agent import SQLite 连接。

## 十二、测试

推荐命令：

```bash
python -m pytest -q --basetemp=.pytest_tmp
python -m compileall -q src tests init.py
```

测试类型：

- 纯函数和配置校验
- FakeMonitor / FakeLLM / FakeStore
- FastAPI TestClient
- SQLite 临时数据库
- 无 LangChain 降级子进程
- HITL 真 graph 测试

新增能力至少覆盖：

- 正常路径
- 无数据
- 查询失败
- 超时或异常
- 权限边界
- 重启/恢复语义
- 日志和审计

## 十三、代码注释约定

### 给开发工程师

`src/` 中的模块 docstring 和注释解释：

- 为什么这样设计
- 有哪些不可破坏的约束
- 过去踩过什么坑
- 修改后会影响哪些调用方

不要在用户 YAML 中写只能在源码里理解的内部实现细节。

### 给用户 / 运维

`config/*.yaml` 和 `.env.example` 只解释：

- 字段作用
- 取值和默认值
- 开启后的行为
- 安全风险
- 修改后是否需要重启

## 十四、文档更新映射

| 改动 | 必须同步 |
|---|---|
| API 契约 | `README.md`、`user-guide.md`、对应测试 |
| 用户配置字段 | `configuration.md`、YAML 注释 |
| 鉴权/审批 | `auth.md`、`developer-guide.md`、测试 |
| DB schema | `database.md`、迁移测试、`PROJECT_HANDOVER.md` |
| 新指标 | `metrics/catalog.py`、`metrics.yaml`、`configuration.md` |
| 新工具 | `agent/tools.py`、测试、`user-guide.md`/`developer-guide.md` |
| 发布 | `README.md`、版本号、变更说明 |

## 十五、发布前检查

- 全量测试通过
- compileall 通过
- 真实环境 smoke test 通过
- Alertmanager webhook 验证通过
- SSH known_hosts 验证通过
- `.env` 不进入 Git
- 数据库和日志有备份策略
- 文档中的默认值和实际 YAML 一致
- 明确当前限制：单 worker、审批不跨重启、无多主机

## 十六、相关文档

- [README.md](../README.md)
- [PROJECT_HANDOVER.md](../PROJECT_HANDOVER.md)
- [user-guide.md](user-guide.md)
- [configuration.md](configuration.md)
- [database.md](database.md)
