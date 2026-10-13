# 快速入门：从首次启动到第一次问答

这篇教程带你完成三件事：启动项目、打开控制台、查看指标并发送第一个问题。
第一次运行只需要项目文件和 Docker，暂时不用准备 AI 密钥或 SSH 密码。

如果你已经部署过，更新时请看 [部署与升级](deployment.md#升级与回滚)，保留原来的数据目录。

## 1. 先理解它做什么

AIOps-Assistant 是一个运维控制台。你可以在里面看服务器的 CPU、内存、磁盘和告警，
也可以提问。配置好 AI 和 SSH 后，还能让它分析问题、读取服务器信息，并在审批后执行指定操作。

首次启动会自动添加一台名为 `local-node` 的主机。这台主机显示的是 Docker 里的 Linux
环境，不能直接当成你电脑或实际服务器的完整数据。

<details>
<summary>项目里的几个组件是什么？</summary>

| 名称 | 用来做什么 |
|---|---|
| Assistant | 你打开的运维控制台 |
| Node Exporter | 采集 CPU、内存、磁盘等数据 |
| Prometheus | 定时收集数据，并判断是否达到告警条件 |
| Alertmanager | 把告警发给 Assistant |
| Grafana | 另一套看图表的页面，可稍后使用 |
| init | 第一次启动时准备配置和密码，完成后会退出 |

</details>

## 2. 准备运行环境

### 安装并启动 Docker

- **Windows/macOS**：安装 [Docker Desktop](https://docs.docker.com/desktop/)，打开它并等待启动完成。
  Windows 要使用 Linux 容器，命令在 PowerShell 中运行。
- **Linux 服务器**：按 [官方安装说明](https://docs.docker.com/engine/install/) 安装 Docker Engine
  和 Compose 插件。[Ubuntu 安装步骤在这里](https://docs.docker.com/engine/install/ubuntu/)。

不需要另外安装 Python、Node.js 或 Conda。第一次启动需要联网下载镜像和依赖。

在准备运行项目的电脑或服务器上，打开终端，执行：

```bash
docker version
docker compose version
```

**看到什么算成功：** 第一条能显示 Client 和 Server；第二条能显示 Compose 版本，要求 2.24 或更新。
如果提示 Docker 没启动，先启动它。Linux 提示 permission denied 时，先处理 Docker 账号权限；
如果改用 sudo，后面的每条 docker 命令也都要加 sudo。

### 找到项目目录

解压或下载完整项目。进入后，应能直接看到 `compose.yaml`、`Dockerfile`、`src/`、`scripts/` 和 `deploy/`。
后面所有 Docker 命令都在这个目录运行。手动上传服务器时，按 [文件清单](deployment.md#首次部署上传哪些文件) 上传。

例如项目文件夹叫 Assistant，在它外面打开终端时执行：

```bash
cd Assistant
```

已经在项目目录里就不用再执行 cd。后面如果提示找不到 compose.yaml，先检查终端所在目录。

### 第一次需要改配置吗？

**使用默认端口时，不用创建 .env，也不用填模型密钥，直接到下一步。**

<details>
<summary>默认端口被占用，或者想换端口怎么办？</summary>

先在项目目录复制样例。Linux/macOS 执行：

```bash
cp .env.example .env
```

Windows PowerShell 执行：

```powershell
Copy-Item -LiteralPath .env.example -Destination .env
```

用文本编辑器打开 .env，例如把 `ASSISTANT_HOST_PORT=8000` 改为 `ASSISTANT_HOST_PORT=18000`。
保存后，下面的网页地址和 SSH 转发命令也要换成对应端口。其它参数见 [部署配置](deployment.md#可选配置模型与端口)。

</details>

## 3. 启动并检查

**在哪里操作：** 运行项目的电脑或服务器，在项目目录的终端里。

```bash
docker compose up -d --build --wait --wait-timeout 180
docker compose ps --all
```

第一次下载和构建可能要等一会儿。命令中的 180 秒是启动后的检查时间，不包括下载时间。

**看到什么算成功：** assistant、prometheus、alertmanager、node-exporter、grafana 五个服务
都在运行且健康；init 显示 Exited (0)。init 完成任务后退出是正常现象。

如果启动失败，执行下面的命令查看原因：

```bash
docker compose logs --tail 100 init assistant
```

常见原因和处理方法见 [部署失败排查](deployment.md#部署失败排查)。不要通过删除 .runtime 来反复重试，
这个目录里有密码、配置和历史数据。

<details>
<summary>想进一步检查服务是否准备好？</summary>

在运行项目的机器上，用浏览器打开 `http://127.0.0.1:8000/ready`。
也可在 PowerShell 执行下面的命令；Bash 把 curl.exe 换成 curl：

```powershell
curl.exe -i http://127.0.0.1:8000/health
curl.exe -i http://127.0.0.1:8000/ready
```

预期 HTTP 状态为 200。ready 返回 503 时，看结果中哪个组件失败，再查看日志。
这个检查不包含真实 AI 调用和 SSH 登录。

</details>

## 4. 打开控制台并连接

### 先取得控制台登录密钥

在**运行项目的机器**上，仍在项目目录执行：

```bash
docker compose run --rm --no-deps init show-token
```

输出的一长串随机字符就是 Token。你可以把它理解成控制台的登录密钥。
复制这串字符，不加引号，也不要加 Bearer 或用户名。不要把它发进聊天、工单或截图。

### 如果项目就在自己的电脑上

打开 `http://127.0.0.1:8000/console`，点击“连接”，粘贴刚才的 Token。

**看到什么算成功：** 主机列表里出现 local-node，页面开始显示监控数据。
新标签页可能需要重新输入 Token。

### 如果项目运行在远程服务器上

默认网页端口只允许服务器自己访问。你需要在**个人电脑**的终端开一条 SSH 转发：

```bash
ssh -N -L 8000:127.0.0.1:8000 user@server.example
```

把 user 换成服务器登录账号，server.example 换成服务器地址。第一次连接先核对服务器指纹。
登录后终端保持打开，没有继续输出通常是正常的。

接着在个人电脑浏览器打开 `http://127.0.0.1:8000/console`，用前面取得的 Token 连接。
这里的 127.0.0.1 指向你的电脑，SSH 会把访问转发到服务器。
直接打开“服务器 IP:8000”默认连不上。

<details>
<summary>SSH 端口或个人电脑的端口不同怎么办？</summary>

SSH 使用 2222 端口时，在上面的命令中加入 `-p 2222`。
个人电脑的 8000 已占用时，把 `-L 8000:127.0.0.1:8000` 改成
`-L 18000:127.0.0.1:8000`，浏览器改访问 `http://127.0.0.1:18000/console`。
关闭这个终端会断开转发。

</details>

## 5. 看第一台主机，问第一个问题

1. 顶部“当前主机”选择 local-node，打开“主机”页。
2. 看 CPU、内存和磁盘数据，确认观测时间在更新。刚启动时先等几次采集，每次约 15 秒。
3. 打开“问答诊断”，在“主机会话”点“新建”。
4. 输入“CPU 使用率是多少”，点“发送”，等它回答后再问“内存使用率是多少”。

**看到什么算成功：** 回答里有这台主机的实际指标，重新打开会话还能看到刚才的消息。
没有 AI 密钥时，这些简单问题也可以回答。

“—”或“无数据”不表示使用率为 0；没有 Swap 时显示“不适用”可以是正常的。
新启动的系统没有以前的趋势数据。遇到问题看 [监控排障](troubleshooting.md#prometheus-与-exporter只读分层检查)。
不要用“状态”这个问题判断系统是否正常，它目前返回模拟文本。

要查看自己的真实 Linux 服务器，继续看 [安装采集程序](deployment.md#独立-linux-测试主机安装-exporter)
和 [添加主机](guide.md#登记实际主机)。

<details>
<summary>还想打开 Grafana、Prometheus 或 Alertmanager？</summary>

在运行项目的机器上，地址分别是：

| 页面 | 默认地址 |
|---|---|
| Grafana | http://127.0.0.1:3000 |
| Prometheus 采集目标 | http://127.0.0.1:9090/targets |
| Alertmanager | http://127.0.0.1:9093 |

Grafana 新用户名为 admin。密码在项目目录执行下面的命令查看：

```bash
docker compose run --rm --no-deps init show-grafana-password
```

它和 Assistant Token 是两套密码。密码无效时看 [Grafana 登录排查](deployment.md#grafana-登录失败与旧数据卷)。
Grafana 预置“Assistant 主机指标 · 统一口径”仪表盘，选择实际的 job 和 instance 后可查看
17 项指标。它使用 Grafana 自己的权限，Assistant 的主机授权不会自动同步过去。
远程访问 Grafana 时，在个人电脑另开终端执行：

```bash
ssh -N -L 3000:127.0.0.1:3000 user@server.example
```

保持终端打开，再用个人电脑浏览器访问 127.0.0.1:3000。
其它组件按相同方法转发自己的端口。

</details>

## 6. 理解数据放在哪里

首次启动会创建 `.runtime/`。这里保存运行配置、登录密钥、会话、告警和日志。
页面里改配置，保存的就是这个目录下的文件。

`deploy/templates/assistant/` 是新部署用的配置样例，已有部署不要靠改样例来改当前配置。
Prometheus、Alertmanager 和 Grafana 的部分数据保存在 Docker 数据卷里。
备份时要同时考虑这些数据，具体见 [数据位置](deployment.md#配置与数据位置) 和 [备份](database.md#备份)。

## 7. 停止、再次启动和下一步

停止项目：

```bash
docker compose down
```

下次在同一个项目目录启动：

```bash
docker compose up -d --wait --wait-timeout 180
```

普通 down 会保留数据，不要加 -v，它会删除监控数据卷。
如果更新了代码，需要按 [升级步骤](deployment.md#升级与回滚) 重新构建。

到这里，先确认：服务健康、Token 能连接、local-node 有数据、CPU/内存问题能回答、会话能重新打开。

| 接下来想做什么 | 去哪里看 |
|---|---|
| 接入真实服务器、开启 AI、配置 SSH 和审批 | [使用指南](guide.md) |
| 获取并验证 known_hosts | [SSH 主机公钥教程](deployment.md#获取并核验-ssh-known_hosts) |
| 练习告警、压测和恢复 | [测试流程](testing.md) |
| 按任务找其它说明 | [文档导航](README.md) |
