# 部署与升级

当前代码新增 Grafana 托管仪表盘 provisioning 和只读 `/etc/aiops-dashboards` 挂载，
覆盖统一目录的 17 项指标。既有业务仪表盘不被替换；它们的查询需要部署负责人单独核验。
Grafana 使用自身权限体系，不能假定 Assistant 主机 ACL 自动生效。

升级到此代码会将 SQLite 迁移到 schema 8，新增审批追溯表。按本文备份流程准备同批
代码/配置/凭据/数据库；不能将迁移后的库直接交给只支持 v7 的旧代码。主机删除为显式
管理员操作，不会在升级中自动删除登记或停止目标。CPU 告警计算窗口改为 5 分钟、默认
磁盘阈值改为 80%，只有部署负责人明确更新实际 Prometheus 配置后才影响业务采集/告警。

项目通过 Docker 运行。第一次安装从 [快速入门](quickstart.md) 开始。
本页说明要上传什么、怎么配置 SSH、如何处理 Grafana 登录问题，以及日常升级和备份。
运行环境需要 Docker 和 Compose 2.24+；Windows/macOS 使用 Linux 容器。

<details>
<summary>维护者需要知道的运行限制</summary>

当前版本 0.6.2 使用单进程、单 worker，SQLite 数据库版本为 schema 8。
不支持多实例共享配置目录。宿主机不需要安装项目 Python/Node 依赖。

</details>

## 首次部署上传哪些文件

以下清单适用于在服务器构建并使用当前 compose.yaml 启动全栈，保持同一根目录：

```text
Assistant/
├── compose.yaml
├── Dockerfile
├── .dockerignore
├── requirements-runtime.txt
├── requirements-lock.txt
├── src/
├── scripts/
└── deploy/
    ├── alertmanager/
    ├── grafana/
    ├── prometheus/
    └── templates/
```

src 包含前端静态文件，deploy 包含初始化模板和被 Compose 直接挂载的组件配置，
三个目录均完整携带。建议同时保留 .env.example、README.md、docs/；
构建测试目标还需 requirements.txt 和 tests/。
首次新部署不上传开发机 .runtime、.env、__pycache__、*.pyc、.pytest_cache 或虚拟环境。
Git 和 Docker 有忽略规则，手动压缩/复制不会自动遵守这些规则。

已有部署升级时保留服务器自己的运行目录和数据卷；迁移到另一台服务器并保留历史时，
须另外搬迁同批运行目录与三个卷的受控备份。镜像、运行目录和数据卷是三类不同对象。
后续 Docker 命令都在包含 compose.yaml 的目录执行；使用自定义项目或运行目录时，
所有查看密码、重建、备份命令保持相同 -p、--env-file 和 AIOPS_RUNTIME_DIR。

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

## 可选配置：模型与端口

管理员在“系统管理”配置 SSH、用户 Token 和通用模型。SSH 只在该页面录入，支持多份凭据；
已登记主机选择凭据后，用户按照主机访问授权使用它。密码和私钥认证都需要经核验的
known_hosts，每条主机公钥占一行；认证测试不执行命令，也不会自动授予诊断或写权限。
具体获取步骤见 [获取并核验 SSH known_hosts](#获取并核验-ssh-known_hosts)。

模型支持 OpenAI 兼容的 Chat Completions 接口，完整 Agent 要求标准工具调用。
保存只校验参数，管理员点击“测试已保存的模型连接”才发出请求。
无完整模型配置仍可监控、巡检和关键词问答。

复制 `.env.example` 后可填写以下 Compose 输入，同名进程环境变量优先：

```env
ASSISTANT_HOST_PORT=8000
GRAFANA_HOST_PORT=3000
PROMETHEUS_HOST_PORT=9090
ALERTMANAGER_HOST_PORT=9093
NODE_EXPORTER_HOST_PORT=9100
MODEL_API_KEY=
MODEL_BASE_URL=
MODEL_NAME=
MODEL_TIMEOUT=30
MODEL_MAX_RETRIES=2
MODEL_MAX_TOKENS=2048
MODEL_TOKEN_LIMIT_PARAM=max_tokens
```

Base URL 是接口根地址（自建服务例如 `http://model.example/v1`），不要追加 `/chat/completions`。
输出长度高级选项支持 `max_tokens` 或 `max_completion_tokens`，只发送所选字段。
新部署没有默认模型地址或名称，不会自动请求公共服务。

页面模型配置整组优先于环境配置；清除页面密钥后也不回退环境密钥。
环境模型参数由初始化服务持久化，非空环境值覆盖持久部署值，空值不清除旧值；
撤销环境密钥时同时删除根 `.env`、进程环境和 `.runtime/compose.env` 中的对应值。
页面 Token 轮换和撤销对后续请求生效，身份历史保留。
已有固定关闭开关需在“安全与开关”按需开启 Agent 和 LLM。

修改 Compose 输入或持久部署凭据后重建，普通 restart 不会重新派生凭据：

```bash
docker compose up -d --build --force-recreate --wait --wait-timeout 180
```

发布端口默认绑定部署机 `127.0.0.1`。远程浏览器可使用 SSH 隧道：

```bash
ssh -N -L 8000:127.0.0.1:8000 ops@server.example
```

按实际发布端口调整目标；长期访问使用带 TLS 的反向代理并保留鉴权。

## 获取并核验 SSH known_hosts

**你要准备的是：一条能证明目标服务器身份的公钥记录。**
这条记录叫 known_hosts。密码和登录私钥证明“你是谁”，known_hosts 用来确认“连的是哪台服务器”。
使用密码登录时也需要它。

下面按 SSH 端口 22 举例。把所有 192.0.2.20 换成自己的目标服务器 IP。
如果登记主机时用域名，下面也用同一个域名。9100 是监控端口，不是 SSH 端口。

### 第一步：到目标服务器确认指纹

**在哪里操作：** 目标服务器的本地终端、云平台网页终端，或以前已经确认安全的管理员连接。

```bash
sudo ssh-keygen -lf /etc/ssh/ssh_host_ed25519_key.pub -E sha256
```

**会看到什么：** 一行结果，其中有 `SHA256:...`。把这一段完整记录下来，后面要比较。
指纹是公钥的简短校验值，不是要粘贴到页面的最终内容。
命令只读取 .pub 公钥，不要读取同名但没有 .pub 后缀的私钥。

如果这个文件不存在，先请服务器管理员确认正在使用的主机公钥类型，再取对应指纹。
不要为了完成教程重新生成服务器密钥。

### 第二步：在自己的电脑获取公钥记录

**在哪里操作：** 个人电脑或部署机，只要能访问目标 SSH。无需输入 SSH 密码。
先找一个新的空工作目录，在里面运行下面两条命令。

Linux/macOS Bash：

```bash
ssh-keyscan -T 5 -p 22 -t ed25519 192.0.2.20 > known_hosts.candidate
ssh-keygen -lf known_hosts.candidate -E sha256
```

Windows PowerShell：

```powershell
ssh-keyscan -T 5 -p 22 -t ed25519 192.0.2.20 | Set-Content -Encoding ascii .\known_hosts.candidate
ssh-keygen -lf .\known_hosts.candidate -E sha256
```

**会看到什么：** 当前目录出现 known_hosts.candidate，第二条命令也会打印 SHA256 指纹。
把它与第一步的指纹逐字比较，公钥类型也要相同。

**只有一致才继续。** ssh-keyscan 只取回网络上提供的公钥，本身不判断它是否可信。
没有输出、连接超时或指纹不一致时，检查地址、SSH 端口和服务器是否换过密钥，
不要直接把结果录入页面。无法通过可信途径确认指纹时，先请目标管理员协助。

缺少 ssh-keyscan/ssh-keygen 时，安装 OpenSSH 客户端。
Windows 安装方法见 [Microsoft 说明](https://learn.microsoft.com/en-us/windows-server/administration/openssh/openssh_install_firstuse)，
个人电脑只需客户端；Linux 使用发行版的 OpenSSH 客户端包。

### 第三步：复制文件内容，填到页面

Linux/macOS 查看文件：

```bash
cat known_hosts.candidate
```

Windows PowerShell 查看文件：

```powershell
Get-Content -Raw .\known_hosts.candidate
```

复制完整的公钥行。每行包含三部分：服务器地址、密钥类型、公钥内容，用空格分隔。
**页面要的是文件内容，不是文件名，也不是 SHA256 指纹。**

1. 用管理员 Token 连接，打开“系统管理 → SSH 凭据”。
2. 凭据 ID 可填 linux-test-ssh；用户名填这台 Linux 服务器的登录账号。
3. 选密码认证并填写该账号密码，或选私钥认证并上传用户登录私钥。
4. 把刚才复制的公钥行粘贴到“可信 known_hosts”，点“保存 SSH 凭据”。

用户的 .pub 公钥、服务器私钥、终端提示文字都不能填到 known_hosts。
当前不支持带口令的登录私钥；不确定选哪个文件时，先用允许的密码方式登录。

### 第四步：关联主机，再测试连接

- **正在添加新主机：** 在“监控主机”向导里选择刚保存的凭据，填写实际 SSH 端口，点“测试 SSH”。
- **主机已经添加：** 在“SSH 凭据”页下方找到它，选凭据并点“更新关联”，再点“测试已保存的 SSH 连接”。

**看到什么算成功：** 页面提示主机密钥和认证通过。保存成功只说明格式可以接受，
连接测试通过才说明这组信息能登录；此时还没有执行任何远程命令。
下一步按 [只读诊断教程](guide.md#第一次执行只读诊断) 授权查询。

<details>
<summary>SSH 不是 22 端口，例如 2222，怎么填写？</summary>

扫描时把 -p 22 改成 -p 2222。例如 Bash：

```bash
ssh-keyscan -T 5 -p 2222 -t ed25519 192.0.2.20 > known_hosts-2222.candidate
ssh-keygen -lf known_hosts-2222.candidate -E sha256
```

记录中的地址应为 `[192.0.2.20]:2222`，核验后复制这个文件的内容。
主机向导的“SSH 端口”也填 2222。Exporter 仍用其实际监控端口，例如 9100。
已有主机的关联按钮不提供改端口功能，处理办法见 [关联 SSH](guide.md#准备并关联-ssh-凭据)。

</details>

<details>
<summary>自己已经有可信的 known_hosts 文件，怎么提取？</summary>

Linux/macOS：

```bash
ssh-keygen -F 192.0.2.20 -f ~/.ssh/known_hosts
ssh-keygen -F '[192.0.2.20]:2222' -f ~/.ssh/known_hosts
```

PowerShell：

```powershell
ssh-keygen -F 192.0.2.20 -f "$env:USERPROFILE\.ssh\known_hosts"
ssh-keygen -F '[192.0.2.20]:2222' -f "$env:USERPROFILE\.ssh\known_hosts"
```

选符合实际端口的那条命令。输出中 # 开头的是注释，复制完整公钥行；
哈希形式的主机记录可以原样保留。共用一个凭据连接多台服务器时，每台已核验记录各放一行。
服务器重装或换密钥后重新核验，不能靠忽略检查来解决不匹配。

</details>

<details>
<summary>也可以直接在可信的服务器终端生成公钥记录</summary>

在第一步使用的可信终端上执行，地址换成 Assistant 实际连接的地址：

```bash
awk -v host='192.0.2.20' '{print host, $1, $2}' /etc/ssh/ssh_host_ed25519_key.pub
```

通过可信渠道把输出的完整一行带回，填到页面即可。
端口非 22 时，把 host 值改成 `[192.0.2.20]:2222`。

</details>

<details>
<summary>没有登录私钥，又需要使用私钥认证，怎么准备？</summary>

在个人电脑新建一个工作目录，确认以下文件名还没有用过，再生成专用测试密钥：

```bash
ssh-keygen -t ed25519 -f ./aiops_test_ed25519
```

如果提示覆盖旧文件，选择不覆盖。当前项目不支持私钥口令，所以这次专用测试密钥的
两次口令输入都留空回车。得到两个文件：

| 文件 | 放到哪里 |
|---|---|
| aiops_test_ed25519 | 用户登录私钥，在 Assistant 页面上传，不能公开 |
| aiops_test_ed25519.pub | 用户公钥，添加到远端账号的 authorized_keys，不能当作 known_hosts |

在目标服务器可信终端里，使用实际登录账号操作，不要误写到 root 的家目录：

```bash
mkdir -p ~/.ssh
chmod 700 ~/.ssh
```

用编辑器打开该账号的 ~/.ssh/authorized_keys，保留原有内容，另起一行粘贴刚生成的用户公钥。
保存后执行 `chmod 600 ~/.ssh/authorized_keys`。远端不需要客户端私钥。
确认 SSH 服务允许该账号公钥登录，再在个人电脑测试：

```bash
ssh -i ./aiops_test_ed25519 -p 22 ops@192.0.2.20
```

换成真实账号、地址和端口，首次连接核对服务器指纹。成功后 exit 退出。
之后上传私钥到 Assistant，并填写相同用户名和可信 known_hosts。
无口令私钥应限制读取权限，只给测试账号所需权限；结束后按需撤回远端用户公钥授权。

</details>

命令参数含义见 OpenSSH 官方 [ssh-keyscan](https://man.openbsd.org/ssh-keyscan) 与
[ssh-keygen](https://man.openbsd.org/ssh-keygen) 手册。

## Grafana 登录失败与旧数据卷

先分清三种登录信息：Assistant 用 Token，Linux 用 SSH 账号，Grafana 用自己的用户名和密码。
新 Grafana 用户名是 admin。下面的命令显示的是项目保存的初始密码。
如果你在 Grafana 改过密码，或 Docker 还在使用以前的数据卷，这个初始密码可能已经不能登录。
Grafana 只在首次初始化时采用 admin_password，见 [官方配置说明](https://grafana.com/docs/grafana/latest/setup-grafana/configure-grafana/#admin_password)。

先在对应部署项目中执行只读检查：

```bash
docker compose ps --all
docker compose logs --tail 60 grafana
docker compose run --rm --no-deps init show-grafana-password
```

确认浏览器连到正确部署及发布端口；新部署用 admin 和命令输出原值，不添加引号。
日志若提示登录次数限制，停止重试，等待限制时间后再测试。不要公开最后一条命令的密码输出。

Linux Bash 查看实际数据库卷和创建时间：

```bash
grafana_container=$(docker compose ps -q grafana)
docker inspect "$grafana_container" --format '{{range .Mounts}}{{if eq .Destination "/var/lib/grafana"}}{{.Name}}{{end}}{{end}}'
```

将下面占位卷名替换为上一条输出，不带尖括号执行：

```bash
docker volume inspect <实际卷名> --format '{{.CreatedAt}}'
```

卷早于本次部署说明可能复用了旧数据。普通 down 保留卷；重新生成 .runtime 或删除
应用镜像不会重置 Grafana 数据库。没有确认旧密码时，可在维护窗口用
[官方 CLI](https://grafana.com/docs/grafana/latest/administration/cli/#reset-admin-password)
将默认管理员 ID 1 的密码重置为当前容器挂载的初始密码；先保留 Grafana 卷备份：

```bash
docker compose exec -T grafana sh -c '
test -s /run/grafana-secrets/admin_password &&
grafana cli --homepath /usr/share/grafana --config /etc/grafana/grafana.ini \
  --configOverrides cfg:default.paths.data=/var/lib/grafana \
  admin reset-admin-password --password-from-stdin \
  < /run/grafana-secrets/admin_password
'
```

这会修改数据库中的管理员密码，保留仪表盘、数据源和数据卷；只在确认需恢复账号时执行。
看到 Admin password changed successfully 后再登录。管理员已改名或 ID 非 1 时，
根据 CLI 返回的实际管理员列表选择 --user-id，不反复重置错误账号。
登录成功后如再次改密，使用新密码并另行妥善保存；初始化文件不会自动同步。

## 配置与数据位置

| 位置 | 用途 |
|---|---|
| `deploy/templates/assistant/` | 七份首次部署模板，修改模板不覆盖运行配置 |
| `deploy/` 下监控组件目录 | Compose 直接挂载的组件配置 |
| `.runtime/config/` | 实际运行 YAML；管理页保存写这里 |
| 根 `.env` | 可选初始化输入及端口、运行目录；不提供业务登录 Token |
| `.runtime/compose.env` | 持久化部署 Token、角色及可选环境值，不作 Compose 的 --env-file |
| `.runtime/deployment.json` | 初始化状态和配置迁移版本；不是当前配置内容 |
| `.runtime/secrets/` | Assistant 专用派生凭据 |
| `.runtime/alertmanager/`、`.runtime/grafana/` | 各服务专用派生凭据 |
| `.runtime/assistant-data/` | SQLite、配置备份和 admin-credentials 页面凭据 |
| `.runtime/assistant-logs/` | 审计与巡检日志 |
| `.runtime/prometheus-targets/` | 动态发现目标 |
| `.runtime/textfile/` | 容器演示告警合成指标 |
| Docker 命名卷 | Prometheus、Alertmanager、Grafana 数据 |

`AIOPS_RUNTIME_DIR` 可指定目录；全部管理命令必须保持相同目录与项目名。
独立栈同时隔离项目名、端口和运行目录。Prometheus 配置与目标格式见
[配置指南](configuration.md#compose-的-prometheus-主配置与目标列表)。

新部署从模板初始化。已完成部署缺少任何一份 YAML 会拒绝启动，需恢复备份；
已有部署丢失 compose.env 也不会生成新 Token。仅派生凭据丢失可在保留 compose.env
时重新初始化修复。初始化中断保留凭据检查点，可修复原因后重试。

三类文件职责不同：模板只用于首次复制；持久配置是当前实际来源；派生文件由初始化服务
从 `.runtime/compose.env` 单向生成，不能反向同步。手工修改派生文件不会更新来源，
下次初始化还会覆盖派生内容。Grafana 初始密码只在空数据库第一次启动时使用，
页面改密后实际密码在 Grafana 数据库里，更改上述文件不会自动同步数据库。

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
支持 doctor、up、down、status、logs、show-token、show-grafana-password、migrate-config；
该脚本只操作 Docker，不能直接启动宿主机服务。doctor 仅检查 Docker/Compose 与部署配置。

## 升级与回滚

升级需要维护窗口。先记录当前 Git 提交、镜像摘要、Compose 项目名、运行目录、环境输入
及三个监控卷的实际名称；检查未决审批和 unknown/running 执行，核对远端事实后再停机。
以下命令使用默认项目；自定义项目必须始终带相同 `-p`、`--env-file` 与运行目录输入。

1. 运行当前版本的离线测试，并在独立栈验证候选版本，不能用生产部署运行写入型验收。
2. `docker compose down` 停止整栈，保留当前镜像与完整回退备份；不加 `-v`。
3. 备份运行目录、根 `.env`（如有）、部署组件配置和三个命名卷。备份记录对应代码和
   镜像；仅复制 SQLite 不足以回滚配置与凭据，具体步骤见下节。
4. 切换到已验证的源码版本。需要旧字段迁移时，保持停机，按[一次性配置迁移](#一次性配置迁移)
   先预览再执行；正常启动不会隐式清理旧字段。
5. 执行 `docker compose up -d --build --wait --wait-timeout 180`。
6. 检查 init 退出 0、五个常驻服务健康、`/health`、`/ready`、原身份和主机 ACL、
   会话/告警历史、指标即时/趋势及监控目标；真实模型/SSH 按已启用能力显式验证。

升级失败先保留现状和日志，再停止整栈。恢复对应旧代码/镜像、同批环境输入、完整运行
目录和监控卷后启动旧版，重复上述业务核验。恢复目标应为空或使用保留副本隔离，避免
混入新版本文件及旧 WAL。不能仅回退镜像而把已迁移数据交给旧版写入；没有自动降级
迁移，不通过删除 deployment.json 或迁移 pending 标记绕过检查。

恢复旧备份可能丢失近期会话、审批、幂等键和执行结果；先对账远端动作，避免恢复后
重复执行。现场恢复时间和数据丢失目标为待核实项，必须演练后记录。

## 停机整栈备份

在 down 前用 `docker compose ps --all` 和 `docker inspect` 核对挂载，或按项目标签
列出卷：`docker volume ls --filter label=com.docker.compose.project=aiops-assistant`。
自定义项目替换标签值；备份 prometheus-data、alertmanager-data、grafana-data 的实际卷。

下面是 PowerShell 示例，仅演示默认路径和卷名。先按实际部署替换变量；备份目录必须
位于运行目录之外、已创建且访问受限，不能递归归档自身。镜像使用停机前保留的版本。

```powershell
$backupRoot = 'D:/AIOpsBackups/pre-upgrade'
$runtimeSource = (Resolve-Path -LiteralPath .runtime).Path
$backupImage = 'aiops-assistant:local'
$backupVolumes = @('aiops-assistant_prometheus-data', 'aiops-assistant_alertmanager-data', 'aiops-assistant_grafana-data')
docker compose down
docker run --rm --network none --entrypoint python --workdir /source --mount "type=bind,src=$runtimeSource,dst=/source,readonly" --mount "type=bind,src=$backupRoot,dst=/backup" $backupImage -m tarfile -c /backup/runtime.tar .
if ($LASTEXITCODE -ne 0) { throw '运行目录备份失败' }
foreach ($backupVolume in $backupVolumes) {
    docker volume inspect $backupVolume
    if ($LASTEXITCODE -ne 0) { throw "卷不存在，停止备份：$backupVolume" }
    docker run --rm --network none --entrypoint python --workdir /source --mount "type=volume,src=$backupVolume,dst=/source,readonly" --mount "type=bind,src=$backupRoot,dst=/backup" $backupImage -m tarfile -c "/backup/$backupVolume.tar" .
    if ($LASTEXITCODE -ne 0) { throw "卷备份失败：$backupVolume" }
}
```

每一步检查退出码，运行目录归档失败则停止后续步骤；另行受控复制根 `.env`、部署配置
及版本清单。Linux 可使用相同 Docker 挂载与 `python -m tarfile`，变量语法换为 Bash。
这只是停机归档方式，不是在线 Prometheus 快照。生成后校验归档可读、文件列表及摘要，
转存加密备份介质；归档包含明文凭据。

### Linux Bash 停机整栈备份示例

下面同样针对默认项目名 aiops-assistant、运行目录 .runtime 和镜像 aiops-assistant:local。
自定义部署先按实际挂载替换三个变量和卷名，不从文件夹名称猜测卷名。
在部署机项目根目录的新 Bash 终端整块执行，set -e 会在出错时停止；它会停止业务栈，
应安排维护窗口。备份目录建在项目旁，不能建到 .runtime 内导致归档自身：

```bash
set -e
runtime_source=$(realpath .runtime)
backup_root="$(dirname "$PWD")/aiops-backup-$(date -u +%Y%m%dT%H%M%SZ)"
backup_image=aiops-assistant:local
test -d "$runtime_source"
docker image inspect "$backup_image" > /dev/null
mkdir -m 700 "$backup_root"
docker compose down
docker run --rm --network none --entrypoint python --workdir /source \
  --mount "type=bind,src=$runtime_source,dst=/source,readonly" \
  --mount "type=bind,src=$backup_root,dst=/backup" \
  "$backup_image" -m tarfile -c /backup/runtime.tar .
for backup_volume in aiops-assistant_prometheus-data aiops-assistant_alertmanager-data aiops-assistant_grafana-data
do
  docker volume inspect "$backup_volume" > /dev/null
  docker run --rm --network none --entrypoint python --workdir /source \
    --mount "type=volume,src=$backup_volume,dst=/source,readonly" \
    --mount "type=bind,src=$backup_root,dst=/backup" \
    "$backup_image" -m tarfile -c "/backup/$backup_volume.tar" .
done
cp -a deploy "$backup_root/deploy"
cp compose.yaml "$backup_root/compose.yaml"
if [ -f .env ]
then
  cp .env "$backup_root/.env"
fi
docker compose config --images > "$backup_root/images.txt"
(
  cd "$backup_root"
  sha256sum *.tar > SHA256SUMS
  sha256sum --check SHA256SUMS
)
printf '备份位置：%s\n' "$backup_root"
```

预期有 runtime.tar、三个卷归档、校验清单和部署组件配置。另行保存当前已验证源码/
Git 提交与镜像包或可重新取得的镜像摘要；images.txt 只是标签列表，不等于完整镜像备份。
归档内容含凭据，应转存受控备份介质。备份结束且无需升级时，在原项目目录
执行 `docker compose up -d --wait --wait-timeout 180` 恢复服务，并重新核对业务。
校验和通过仅证明归档未发生改变，能否恢复仍需按下节在测试环境验证。

### 恢复归档

恢复只使用自己验证过的归档，在停机、确认目标为空且保留当前副本后，通过同一镜像
将对应目录或卷挂载到 `/restore`，执行 `python -m tarfile -e /backup/对应归档.tar /restore`。
恢复到新卷时显式核对 Compose 实际挂载；不要覆盖仍在运行的卷。Python 解包不负责
恢复部署所需的全部 POSIX 属主/权限，需按原部署核对，尤其 Grafana 等非 root 服务。
随后验证 SQLite 完整性、凭据引用、监控数据与业务接口，见[数据库恢复](database.md#恢复)。
首次演练应在独立测试栈进行，按 [重启、备份与日志](testing.md#8-重启备份与日志)
事先记录会话 ID、配置和告警，再恢复同批数据逐一比较。不要用生产数据副本启动带真实
写授权的第二套服务，以免自动处置重复作用于实际主机。

## 日志与监控

Compose 标准输出使用 json-file，每服务 10 MB × 3 文件轮转；应用审计与巡检日志在
运行目录 assistant-logs。应用日志的轮转和保留由 settings.yaml 控制，不能由容器日志
上限推断磁盘总用量。定期检查运行目录、命名卷与备份介质空间，按组织策略导出重要审计。

值班检查 `/health` 缓存状态、`/ready` 依赖状态、Prometheus Targets、Alertmanager 投递
错误、最新巡检时间及 unknown/running 执行。就绪不验证真实模型/SSH，也不保证所有
Exporter 可达。读取日志可用 `docker compose logs --tail 200 assistant init alertmanager`；
排障路径见[只读排障](troubleshooting.md)。应用脱敏不代替受限日志权限。

## 一次性配置迁移

升级前停止业务栈，备份完整运行目录和监控命名卷，保持原 Compose 项目名、
`AIOPS_RUNTIME_DIR` 及端口输入。宿主机运行包装脚本仅需标准库 Python；
YAML、凭据校验由新镜像中的迁移程序完成，不需要在宿主机安装项目依赖。

```bash
python scripts/stack.py down
python scripts/stack.py migrate-config --dry-run
python scripts/stack.py migrate-config
python scripts/stack.py up
```

命令先检查该项目没有运行服务，构建初始化镜像，再在容器内处理根 `.env`、
`.runtime/compose.env`、七份运行 YAML 和页面凭据。dry-run 只报告候选文件名，不输出值。
正常启动发现旧字段会明确失败并提示该命令，不会隐式升级。
初始化只读挂载项目目录检查根 .env 的旧字段，业务服务不挂载项目源码或自动加载该文件。

迁移映射 `DEEPSEEK_API_KEY/BASE_URL/TIMEOUT/MAX_RETRIES/MAX_TOKENS` 到对应 `MODEL_*`，
`DEEPSEEK_MODEL` 到 `MODEL_NAME`，供应商名改为 `openai_compatible`。
旧版本隐式使用的 DeepSeek 地址和名称转成显式配置；已有页面模型整组保留。
所有迁移输入中的新旧同义字段值相同则合并，不同则停止，只报告字段名；请人工选择后重试。
根 `.env` 与持久环境仍各自保留原来的配置层级，根输入在重新初始化时生效。

旧 `SSH_*` 和命令目录全局 `target` 被删除。已明确登记的连接目标保留；
引用旧环境或 `/run/ssh/` 材料的主机凭据迁入页面凭据存储，继续保留原主机关联和白名单。
无法确定主机关联时，先在运行 hosts.yaml 中明确登记主机及 credential_ref；
缺少材料时，将经核验的 known_hosts、原私钥放到运行目录 ssh/，修正引用后重试。
旧凭据若同时设置密码和私钥，需先明确一种认证方式；带口令的私钥不受支持。
不会扫描主机、信任未知密钥或自动增加授权。
废弃的 `server`、`auth.enabled`、`alert.trend_step`、`app.version` 也只在此次迁移中清理。

所有候选先完整校验，再在 `.runtime/assistant-data/config-migrations/<事务ID>/`
保存受限备份及校验摘要，原子替换文件，并更新 `current.json` 状态。
写入失败恢复原文件；进程中断时启动被阻止，保持停机并重新执行迁移完成恢复和重试。
若中断后有人另改配置，恢复拒绝覆盖人工更改，先对照受限备份核对。
迁移可重复执行。备份含凭据，不要提交或公开；不能删除 pending 标记绕过校验。

部署 Token、身份、主机授权、历史及 Grafana 数据保持原值。普通用户默认没有主机访问权，
管理员显式授权；具名会话仍要求身份及主机权限，匿名会话不自动认领。配置迁移不操作 SQLite；
当前 Assistant 启动时将受支持的旧库升级到 schema 8，数据库回退边界见[数据库说明](database.md#schema-和迁移)。
回退前停止服务，恢复对应版本代码和完整运行备份，不要通过删除初始化检查点来重试。

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
依赖用途、测试约束与本地/CI 构建缓存见 [依赖与构建](dependencies.md)。源码更新复用依赖层，
pip 下载缓存不进入镜像；默认生产镜像仍包含 AI 与 SSH 能力。

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
登记不会自动安装远程 Exporter，也不扩大 Compose 容器权限。

### 独立 Linux 测试主机安装 Exporter

本例适用于使用 systemd 的 Linux 测试主机；操作在**被监控主机终端**执行，
不是在 Assistant 容器里执行。已有受管理 Exporter 时先核对其版本、服务与监听端口，
不要覆盖另一套安装。安装工具需 curl、tar、sha256sum；Ubuntu/Debian 可通过
`sudo apt-get install ca-certificates curl tar coreutils` 安装。

以固定的 node_exporter 1.12.1 为例，在新的下载目录执行；amd64 对应 x86_64，
ARM64 主机改用 arm64，其它架构从 [官方发布页](https://github.com/prometheus/node_exporter/releases/tag/v1.12.1)
选择匹配包。不要从来历不明的镜像站直接取二进制运行。

```bash
uname -m
mkdir -p ~/aiops-exporter-download
cd ~/aiops-exporter-download
exporter_version=1.12.1
exporter_arch=amd64
exporter_archive="node_exporter-${exporter_version}.linux-${exporter_arch}.tar.gz"
curl -fLO "https://github.com/prometheus/node_exporter/releases/download/v${exporter_version}/${exporter_archive}"
curl -fLO "https://github.com/prometheus/node_exporter/releases/download/v${exporter_version}/sha256sums.txt"
grep " ${exporter_archive}$" sha256sums.txt > selected-checksum.txt
sha256sum --check selected-checksum.txt
```

必须看到该文件校验 OK，任一下载/筛选/校验失败均停止后续安装。
校验清单与二进制来自同一官方发布，校验主要发现下载损坏；来源信任仍依赖官方渠道。
没有设置全局 set -e 的终端应逐条检查结果，不在前一步失败后继续复制命令。

```bash
tar -xzf "$exporter_archive"
sudo install -m 0755 "node_exporter-${exporter_version}.linux-${exporter_arch}/node_exporter" /usr/local/bin/node_exporter
id node_exporter
```

如果最后一条提示用户不存在，才创建专用服务账户：

```bash
sudo useradd --system --no-create-home --shell /usr/sbin/nologin node_exporter
```

检查 `/usr/sbin/nologin` 在该发行版存在；存在的 node_exporter 账户不重复创建。
用编辑器创建 `/etc/systemd/system/node_exporter.service`，粘贴上一节完整 ini 服务定义。
例如 `sudo nano /etc/systemd/system/node_exporter.service`，nano 使用 Ctrl+O、回车保存，
Ctrl+X 退出。确认 User/Group 对应该账户，ExecStart 对应安装路径后执行：

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now node_exporter
sudo systemctl status node_exporter --no-pager
curl -fsS http://127.0.0.1:9100/metrics -o /tmp/aiops-node-metrics.txt
grep '^node_memory_MemTotal_bytes ' /tmp/aiops-node-metrics.txt
```

预期服务为 active (running)，指标文件包含 node_memory_MemTotal_bytes 等 node_* 样本。
这只证明目标本机采集正常；防火墙与云安全组应只允许 Prometheus 所在受信网络访问 9100。
在隔离内网教程中使用 HTTP；跨不可信网络需另行配置 TLS/认证，不能将明文 Exporter
端口直接暴露给公网。组件原理与安装示例见 [Prometheus 官方指南](https://prometheus.io/docs/guides/node-exporter/)。

下一步在**部署机项目根目录**，从 Prometheus 所在网络位置检查目标。将示例地址替换为
被监控主机实际可达 IP；助手容器仅用于同一 Compose 网络的初筛，最终以 Prometheus Targets 为准：

```bash
docker compose exec -T assistant python -c "import requests; r=requests.get('http://192.0.2.20:9100/metrics', timeout=5); r.raise_for_status(); print('Exporter HTTP', r.status_code)"
```

预期 HTTP 200；连接拒绝检查服务和监听，超时检查路由、防火墙、安全组。
不要把目标主机本地的 127.0.0.1 或个人电脑地址填成容器可达目标。
接着按 [登记实际主机](guide.md#登记实际主机) 完成页面登记并检查采样。
