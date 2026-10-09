# 快速入门：从首次启动到第一次问答

本教程适合第一次接触 AIOps-Assistant 的人。目标是先跑通本机监控、控制台和关键词问答，
不需要模型密钥，也不需要登录任何远程主机。已有部署的升级与恢复请使用
[部署与升级](deployment.md)，不要把已有数据目录当成新环境。

## 1. 先理解它做什么

AIOps-Assistant 把监控数据、告警、问答、人工审批和执行记录放在一起。
有模型密钥时可以用 AI 分析；没有密钥时仍能查看监控、接收告警并使用关键词问答。
新 Compose 部署没有 SSH 授权和处置规则，不会自动修改其它主机。

| 名称 | 可以把它理解成 |
|---|---|
| Assistant | 提供控制台和 API，组织问答、巡检、审批与历史记录 |
| Node Exporter | 采集 CPU、内存、磁盘等指标 |
| Prometheus | 定期读取 Exporter 指标，保存时序数据并计算告警 |
| Alertmanager | 将 Prometheus 告警发送给 Assistant |
| Grafana | 可选的独立监控可视化页面 |
| `init` | 启动前生成配置和凭据的一次性任务，结束后无需一直运行 |

“主机 ID”是项目中定位主机的稳定名字；“Token”是连接 API 的随机密钥，不能当作用户名。
本教程中自动登记的主机 ID 为 `local-node`。

## 2. 准备运行环境

取得项目源码，打开终端并进入包含 `compose.yaml` 的项目目录，例如：

```bash
cd Assitant
docker version
docker compose version
```

Linux 使用 Docker Engine；Windows/macOS 使用 Docker Desktop，并选择 Linux 容器。
需要 Compose 2.24 或更新版本。`docker version` 应能显示客户端和服务端信息；
若只有客户端或报无法连接，先启动 Docker 服务或 Desktop。

推荐路径只需要 Docker，不需要在电脑上安装 Python、Conda、Node.js 或模型 SDK。
首次启动需要联网下载镜像和 Python 依赖。命令所在目录不对时，Compose 会提示找不到配置文件。

## 3. 启动并检查

在同一个项目目录运行：

```bash
docker compose up -d --build --wait --wait-timeout 180
docker compose ps --all
```

`-d` 表示后台运行；`--build` 构建项目镜像；`--wait` 等待健康检查。
180 秒是容器启动后的就绪等待时间，不包含镜像下载和构建时间。

成功标准：命令返回成功，五个常驻服务显示运行中且健康，`init` 为 `Exited (0)`。
`init` 退出不是故障。首次指标抓取还可能需要等待一小段时间。

如果失败，先看错误和日志：

```bash
docker compose logs --tail 100 init assistant
```

镜像拉取失败时修复 Docker 仓库连接后重复原命令；端口被占用、初始化和健康检查故障
按 [部署失败排查](deployment.md#部署失败排查) 处理。不要删除 `.runtime/` 来碰运气重试。

## 4. 打开控制台并连接

显式查看自动生成的管理员 Token：

```bash
docker compose run --rm --no-deps init show-token
```

在部署这套服务的电脑上打开 [控制台](http://127.0.0.1:8000/console)，点击“连接”，
把上面的 Token 填入连接框。Token 只用于鉴权，不要贴进工单或截图。

如果服务运行在远程 Linux 服务器，浏览器里的 `127.0.0.1` 指向你自己的电脑。
请在自己电脑的终端建立隧道，把示例账号和地址替换为实际登录信息：

```bash
ssh -N -L 8000:127.0.0.1:8000 user@server.example
```

保持终端连接，再从自己的电脑打开上述控制台地址。项目默认端口只绑定服务器回环地址，
直接访问“服务器 IP:8000”不会连通。改过端口或需要长期对外访问时见
[可选配置与远程访问](deployment.md#可选配置模型端口与-ssh)。

## 5. 看第一台主机，问第一个问题

1. 在顶部“当前主机”选择 `local-node`，进入“总览”或“主机监控”查看 Exporter 和指标。
2. 进入“问答诊断”，在“主机会话”点击“新建”。
3. 输入“CPU 使用率是多少”并发送。没有模型密钥时使用关键词回答；有数据会返回观测值，
   没有数据或查询失败时会给出相应提示，不能把缺失值当作 0。
4. 再问“内存使用率是多少”。会话绑定当前主机，切换主机后需要选择该主机的会话。

`local-node` 对应栈内的 `node-exporter:9100`。它看到的是 Docker Linux 环境，
不等同于 Windows/macOS 宿主机，也不自动代表 Linux 宿主机的完整资源。
监控实际服务器，需要在那里部署 Exporter，再按 [使用指南](guide.md#登记实际主机) 登记。

问答成功只证明这次请求完成。判断监控是否正常，还要看 Exporter 状态、观测时间和采样。
“活动告警”与“历史记录”也不同；过去的异常不能用来判断现在仍然故障。

## 6. 理解数据放在哪里

首次初始化自动建立 `.runtime/`。Compose 实际配置在 `.runtime/config/`；
根 `config/` 是本地 Python 模式的配置，修改它不会更新新 Compose 部署。
管理员保存配置使用“系统管理”；字段、生效时机和热应用见 [配置指南](configuration.md)。

SQLite、日志、凭据和监控数据都持久化在部署目录或命名卷里。
不要提交 `.env`、`.runtime/`、Token 或数据库；详细目录和备份方法分别见
[部署数据位置](deployment.md#配置与数据位置) 与 [数据库备份](database.md#九备份)。

## 7. 停止、再次启动和下一步

```bash
docker compose down
docker compose up -d --wait --wait-timeout 180
```

普通 `down` 保留配置、凭据和数据。不要加 `-v`，它会删除监控数据卷。
以后在相同目录、项目名与部署目录启动即可继续使用。

到这里，你已经完成首次部署、Token 连接、指标查看和关键词问答。
接下来按需要选择一条路径：

| 下一步 | 阅读 |
|---|---|
| 日常看告警、巡检、审批与记录 | [使用指南](guide.md) |
| 配置模型密钥、修改端口或准备 SSH | [部署与升级](deployment.md#可选配置模型端口与-ssh) |
| 理解页面与管理员保存 | [控制台说明](console.md) |
| 在独立环境练习告警与故障恢复 | [本机测试与验收](testing.md) |
| 用本地 Python 开发 | [本地 Python 启动](deployment.md#本地-python-启动) |
| 找其它文档 | [文档导航](README.md) |
