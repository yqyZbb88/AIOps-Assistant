# AIOps-Assistant 文档

本文档目录面向部署和使用 AIOps-Assistant 的用户与运维人员。

| 文档 | 内容 |
|---|---|
| [user-guide.md](user-guide.md) | 部署、启动、首次验收、日常运维、备份和排障 |
| [configuration.md](configuration.md) | 运行时 YAML 字段、默认值、约束和示例 |
| [auth.md](auth.md) | API 密钥、身份、Alertmanager webhook 和审批角色 |
| [database.md](database.md) | SQLite 表结构、数据生命周期、查询和备份 |

## 运行时文件

用户通常只需要关注：

```text
.env
config/*.yaml
data/
logs/
```

修改配置后需要重启服务。

## 配置顺序

首次部署建议按以下顺序完成：

1. 阅读 [user-guide.md](user-guide.md)
2. 配置 `.env`
3. 检查 `config/settings.yaml`
4. 检查 `config/metrics.yaml`
5. 检查 `config/diagnostics.yaml`
6. 按需配置 `config/rules.yaml`
7. 按需配置 `config/actions.yaml`
8. 按需配置 `config/agent_actions.yaml`
9. 配置 Alertmanager webhook 鉴权
10. 启动并完成首次验收

## 安全提醒

- 不要把 `.env` 提交到版本库
- 不要复用 Alertmanager 密钥
- 生产环境使用 SSH 私钥
- 保持 known_hosts 严格校验
- 自动写命令默认保持关闭
- 写命令必须经过人工审批
