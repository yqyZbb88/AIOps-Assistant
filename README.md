# AIOps-Assistant

AIOps-Assistant 是面向少量 Linux 主机的轻量运维助手，把 Prometheus 指标、Alertmanager
告警、受控 SSH 诊断、人工审批和 SQLite 历史记录组织在同一控制台。
当前版本 **0.6.2**，采用单进程、单 worker，使用 SQLite。

第一次接触项目，请读 [快速入门](docs/quickstart.md)：从环境准备、首次启动到 Token
连接、主机指标与第一条问答。查找具体操作请打开 [文档导航](docs/README.md)。

## 当前能力与边界

- 显式多主机注册表，监控、会话、诊断、审批与执行记录绑定主机。
- 17 个内置指标支持即时与真实时间趋势查询，磁盘和网络按设备分线；支持巡检、归档和关键词问答。
- 两条独立写路径：规则自动处置，以及必须经人工审批的 Agent 写命令。
- 管理员控制台支持 SSH 凭据、用户 Token、DeepSeek 配置热生效、主机登记与历史查询。
- 强制 Bearer Token 身份鉴权、私有会话隔离、管理员配置用户主机访问权限、SSH 主机密钥校验、命令白名单、审计和输出脱敏。

适合单 Prometheus/Alertmanager、单 worker、少量用户的部署。不支持多实例共享配置目录。
待审批线程只在当前进程有效，重启后不能续跑旧申请；规则自动处置默认关闭。
Compose 部署始终验证身份，无模型密钥也能监控与关键词问答，没有默认 SSH 写授权。

## 启动入口

在含 `compose.yaml` 的项目目录运行（需要 Docker 和 Compose 2.24+）：

```bash
docker compose up -d --build --wait --wait-timeout 180
```

成功后访问 `http://127.0.0.1:8000/console`；Token 获取、远程访问和首次操作见
[快速入门](docs/quickstart.md)。连接后在“系统管理”配置 SSH、用户 Token 与模型；页面凭据保存在持久化数据目录，
保存模型不会发起真实请求。完整的端口、模型、SSH、升级和数据保留说明见
[部署与升级](docs/deployment.md)。

## 继续阅读

| 需要 | 入口 |
|---|---|
| 接手项目 | [项目整体交接](docs/handover.md) |
| 初次使用 | [快速入门](docs/quickstart.md) |
| 按任务定位文档 | [文档导航](docs/README.md) |
| 日常运维 | [使用指南](docs/guide.md) |
| 理解源码 | [架构说明](docs/architecture.md) |
| 贡献与验证 | [贡献约定](CONTRIBUTING.md) |

旧部署的废弃设置由 init 备份后移除：`auth.enabled`、`alert.trend_step`、
`app.version` 和 `server` 段。有效 Token、角色和主机授权保持，详见
[配置升级](docs/configuration.md#已移除的配置与升级)。
