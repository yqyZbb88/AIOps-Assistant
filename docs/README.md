# ops-agent 文档索引

本项目的文档按受众分为三类。第一次阅读时，先根据你的角色选择入口。

## 1. 用户 / 运维人员

适合部署、启动、修改配置和日常使用：

| 文档 | 内容 |
|---|---|
| [user-guide.md](user-guide.md) | 部署前准备、启动、鉴权、常用 API、配置修改、备份与排障 |
| [configuration.md](configuration.md) | 所有运行时 YAML 字段、类型、默认值、约束与示例 |
| [auth.md](auth.md) | API 密钥、身份、Alertmanager webhook 和审批职责分离 |
| [database.md](database.md) | SQLite 表结构、数据生命周期、备份和维护 |

用户只需要关注：

- `config/*.yaml`
- `.env` / `.env.example`
- `logs/`
- `data/`
- 对外 HTTP API

不需要修改 `src/` 下的 Python 代码。

## 2. 开发工程师

适合修改代码、增加能力、运行测试和理解架构：

| 文档 | 内容 |
|---|---|
| [developer-guide.md](developer-guide.md) | 分层架构、模块地图、扩展流程、测试、发布和代码约定 |
| [PROJECT_HANDOVER.md](../PROJECT_HANDOVER.md) | 项目历史、既定决策、踩坑记录、路线图和硬性约束 |
| [database.md](database.md) | schema 版本、迁移规则和存储实现背景 |
| [auth.md](auth.md) | 鉴权接线、密钥解析和授权边界 |

开发工程师主要关注：

- `src/` 下的 Python 代码
- `tests/`
- `requirements.txt`
- `init.py`
- `config/` 的结构契约
- `.github/workflows/ci.yml`

## 3. 代码注释与配置注释的受众约定

| 位置 | 受众 | 说明 |
|---|---|---|
| `src/**/*.py` 顶层 docstring、注释、类型 | 开发工程师 | 解释实现原因、边界、坑和扩展方式 |
| `tests/**/*.py` 注释 | 开发工程师 | 解释回归场景和验收条件 |
| `config/*.yaml` 注释 | 用户 / 运维 | 解释可修改字段、默认行为和风险 |
| `.env.example` 注释 | 用户 / 运维 | 解释密钥和环境变量用途 |
| `README.md` | 用户 + 开发 | 项目入口和导航 |
| `PROJECT_HANDOVER.md` | 开发工程师 | 不面向普通用户 |

## 4. 文档更新规则

- 修改用户可配置字段时，同时更新 `configuration.md` 和对应 YAML 注释。
- 修改 API 契约时，同时更新 `README.md`、`user-guide.md` 和相关测试。
- 修改数据库 schema 时，同时更新 `database.md`、迁移测试和 `PROJECT_HANDOVER.md`。
- 修改安全边界、鉴权或审批时，同时更新 `auth.md` 和 `developer-guide.md`。
- 新功能完成后，在 `README.md` 的功能列表登记，并判断是否需要用户指南或开发者指南。
