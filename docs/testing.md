# 0.6.2 容器测试与验收

本文用于在开发机或部署机上验证 AIOps-Assistant 的功能、异常处理和数据保留。
每个场景给出操作与预期结果；测试报告按实际执行填写，不把部署成功等同于业务验收通过。
部署准备见 [部署与升级](deployment.md)，页面操作见 [控制台说明](console.md)。

第一次测试可以按下面顺序做，不用先看完所有脚本和字段：

1. 按第 3 节建立一套单独的测试环境。它使用自己的目录、端口和数据，不复用日常部署。
2. 按第 4 节运行自动检查，再按第 5 节用浏览器实际操作。
3. 按第 6 节触发一次告警，确认能收到，再恢复测试状态。
4. 要用 AI、SSH 或审批时，再做第 7 节。没有准备这些能力就记录为“未执行”。
5. 最后做第 8 节的重启和备份恢复，按第 9 节记录结果。

第 2 节的离线测试检查程序逻辑，模型和 SSH 使用模拟对象，不会实际登录你的服务器。
真实连接是否可用，要通过后面的现场操作确认。

## 1. 选择测试范围

| 范围 | 使用环境 | 验证内容 |
|---|---|---|
| 离线自动化 | 独立 Docker test 镜像，内置 Python/Node | API、配置、SQLite、主机隔离、模拟 LLM/SSH、控制台逻辑 |
| 隔离 Compose | Docker 与 Compose 2.24+，使用独立测试栈 | 初始化、鉴权、监控、真实本地告警投递、重建保留数据 |
| 页面验收 | 浏览器访问测试栈 | 会话、主机、记录、配置热更新与失败提示 |
| AI/SSH 闭环 | 模型密钥、独立 Linux 测试主机及 SSH 凭据 | AI 分析、只读诊断、人工审批和受控执行 |

离线测试运行时不连接真实模型、Prometheus 或 SSH；首次安装依赖仍需要网络或本地缓存。
Compose 首次构建与拉取镜像需要可用的仓库网络。仅运行 Compose 时，宿主机不需要 Python
或 Node.js。AI/SSH 场景按已配置能力执行；未准备外部依赖时记录为“未执行”，不要记为通过。

本文的 Compose 命令针对当前包含 `init` 服务的配置。先在项目根目录执行
`docker compose config --services`；如果没有 `init`，请使用对应版本的部署说明或先按
[升级步骤](deployment.md#一次性配置迁移)更新源码。所有后续命令都在项目根目录运行。

## 2. 离线自动化测试

全部回归在独立 test 镜像内运行，包含 Python 3.13、Node 22 与测试依赖；
生产镜像不包含 pytest 或测试代码。宿主机只需要 Docker，不安装项目运行环境。

```bash
docker build --target test -t aiops-regression:0.6.2 .
docker run --rm --network none aiops-regression:0.6.2
docker run --rm --network none --entrypoint node aiops-regression:0.6.2 --check src/web/console.js
docker run --rm --network none --entrypoint node aiops-regression:0.6.2 --check src/web/markdown.js
docker run --rm --network none --entrypoint node aiops-regression:0.6.2 --test tests/console_ui.test.cjs tests/console_markdown.test.cjs
docker run --rm --network none --entrypoint python aiops-regression:0.6.2 -m compileall -q src scripts tests
git diff --check
```

首次构建需下载镜像和依赖，运行回归时使用 --network none。测试自带合成身份、
关闭模型和追踪，不挂载真实运行目录；业务数据写入容器临时目录，退出即清理。
pytest 包含 Node 交互测试包装器；单独 Node 命令便于检查语法与查看前端用例结果。
构建执行 pip check；离线回归核对运行/测试约束与实际依赖闭包及版本一致。
测试独有依赖的版本直接在 requirements.txt 维护，源码更新不会重新安装测试依赖；
缓存边界见 [依赖与构建](dependencies.md)。
通过标准为无失败、无意外跳过、依赖与编译/差异检查无错误；
记录实际数量和第三方警告，不把固定数量作为通过条件。

定向检查可在镜像名后传入用例文件：

```bash
docker run --rm --network none aiops-regression:0.6.2 -q tests/test_deployment.py tests/test_stack.py
docker run --rm --network none aiops-regression:0.6.2 -q tests/test_admin_config.py tests/test_config_loader.py tests/test_hosts_config.py
docker run --rm --network none aiops-regression:0.6.2 -q tests/test_admin_credentials.py tests/test_metric_query_matrix.py
```

相关修正完成后执行完整回归，CI 使用同一 test 目标。

通用模型回归 `test_model_initialization.py` 使用回环 HTTP 服务检验真实 Chat Completions
请求、两种输出长度字段、文本响应、完整工具回合和超时。测试不访问公共模型。
`test_config_migration.py` 覆盖预览、新旧冲突、页面配置、材料缺失、写入回滚、
备份失败、迁移中断和重复执行。SSH 回归保留主机隔离、白名单、审批及主机密钥拒绝。
前端回归同时检查通用模型字段和敏感输入清理。

## 控制台、审批与执行的隔离验收

SSH 结果回归 `tests/test_ssh_write_outcome.py` 覆盖真实审批图续跑、规则 SQLite 共库路径、
退出码 0/1/137、缺少状态 -1、发送脚本异常、幂等不重放、审计详情和送给模型的实际工具
反馈。Paramiko 网络边界替换为合成通道，不连接真实 SSH 或模型。另以本地 POSIX sh
对照固定加载器与原 sh -c 的 18 项非交互式语义：管道、条件、命令替换、here-doc、
cat/read 和超过输入缓冲区的长模板、显式 stdin、set -e、trap、exec、cd、显式 Bash、
命令缺失、语法错误、末尾反斜线、空输出、内部变量清理；读取失败拒绝执行部分脚本。

另可使用已有 `golang:1.26.8` 镜像中的 Python 和 procps 验证真实 Linux 进程行为：

```powershell
docker run --rm --network none --mount "type=bind,source=$PWD/src,target=/app/src,readonly" --mount "type=bind,source=$PWD/tests,target=/app/tests,readonly" --workdir /app --entrypoint python3 golang:1.26.8 -m tests.ssh_process_acceptance
```

该脚本只允许临时 Linux Docker 容器，用唯一匹配词创建合成进程；sudo 以不提权的包装
替身模拟，SSH 网络也替换为本地子进程。实际公共 SSH 内核和真实 shell/pkill 验证旧包装
被误杀、新 stdin 包装正常返回 0、无匹配保留 1，以及中文/引号/多行、stdout/stderr 和
非零退出码保持原义，同时通过实际公共 SSH 内核运行上述 18 项语义对照和加载器失败检查。
镜像需自带 Python 3 和 procps，不下载包、不挂载业务运行目录。
该验收不连接真实 SSH/模型，不替代现场 sshd、sudoers 和业务主机验收。

规则与审批反馈回归使用 `test_rule_claim_regression.py` 与 `test_approval_progress.py`：
真实临时 SQLite 共库路径、并发告警仅执行一次、审计失败回滚且不运行命令，以及模拟 AI
阻塞时进度仍可读、角色/主机隔离。前端补充提前阶段反馈、超时无重放、旧身份响应丢弃及刷新失败保留结果。
真实 Chrome 阶段验收可将下方 preview 服务启动命令末尾加 `--slow-approval`，再运行
`python tests/approval_progress_browser_acceptance.py`。该模式延迟合成动作与 AI 反馈，不连接真实 SSH/模型。

先完成第 2 节的离线流程与镜像构建。`test_console_improvements.py` 覆盖服务端身份、管理员删除、
revision 冲突、引用/运行屏障、外部目标拒绝修改、双文件失败回滚、历史分页/权限、独立执行
结果及 v7→v8 结构一致。既有凭据测试继续覆盖关联热生效、保留原值、可信 known_hosts、
失败恢复和 SSH 检查票据失效；Node 测试补充编辑定位、取消清理、编辑 revision 保留及身份展示。

使用独立 test 镜像和合成数据可显式运行真实浏览器验收（宿主机需 Python Playwright 与 Chrome）：

```powershell
docker run --rm -d --name aiops-console-improvements-preview -p 127.0.0.1:18769:8000 --entrypoint python aiops-regression:0.6.2 -m tests.console_preview
python tests/console_browser_acceptance.py
docker stop aiops-console-improvements-preview
```

该服务仅在容器临时目录创建合成身份、SSH 材料和记录，监控调用替换为固定合成响应，禁用
模型和 SSH 测试，不可作为业务服务。端口占用时先定位占用者，不停止已有容器。浏览器
检查身份切换/失效、SSH 编辑真实 API 保存、敏感输入清理、审批/执行结果分列、移动端溢出
与页面异常。截图和结果写 `.runtime/console-improvements/`，不提交私有运行报告。

`python -m scripts.metric_contract --check` 检查查询表/Grafana JSON 与指标目录和告警规则
一致；该命令需完整工作区，test 镜像不打包 docs。生成用 `python -m scripts.metric_contract`。
下方 promtool 指标/告警验收继续适用；全部 17 项即时与趋势表达式均需真实引擎通过。

可额外验证实时 file_sd 更新（仅固定本机测试端口，使用 Prometheus 自身作为采集目标）：

```powershell
python tests/prometheus_file_sd_acceptance.py --prepare
docker run --rm -d --name aiops-prometheus-file-sd-test -p 127.0.0.1:19069:9090 --mount "type=bind,source=$PWD/.runtime/console-improvements/prometheus-live,target=/fixtures,readonly" prom/prometheus:v3.13.3 --config.file=/fixtures/prometheus.yml --storage.tsdb.path=/prometheus
python tests/prometheus_file_sd_acceptance.py
docker stop aiops-prometheus-file-sd-test
```

该项抓取/发现间隔缩短到 1 秒，生产模板仍为 15/30 秒。验证 17 项 HTTP 查询、缺 node
采集器返回无数据、精确 job/instance 和目标移除后 up 序列消失；有效指标值、多磁盘/网卡、
零值、无 Swap 与采样不足由 promtool 合成验收负责。此验收不等同真实 SSH/模型、完整
Compose 重建、业务 Grafana 或备份恢复演练；未执行项必须单独记录。

## 3. 建立独立 Compose 测试栈

测试栈使用项目名 `aiops-local-test`、目录 `.runtime/local-test` 和独立端口。
不要把已有部署的数据复制到这个目录，也不要将后面的停止、重建、告警或故障操作用于真实服务。

在新的终端中设置以下环境变量。不要在隔离测试终端导出真实模型或 SSH 凭据。

Windows PowerShell：

```powershell
$env:AIOPS_RUNTIME_DIR = './.runtime/local-test'
$env:AIOPS_IMAGE = 'aiops-assistant:local-test'
$env:ASSISTANT_HOST_PORT = '18000'
$env:GRAFANA_HOST_PORT = '13000'
$env:PROMETHEUS_HOST_PORT = '19090'
$env:ALERTMANAGER_HOST_PORT = '19093'
$env:NODE_EXPORTER_HOST_PORT = '19100'

foreach ($testEnvName in @('MODEL_API_KEY', 'MODEL_BASE_URL', 'MODEL_NAME', 'MODEL_TIMEOUT', 'MODEL_MAX_RETRIES', 'MODEL_MAX_TOKENS', 'MODEL_TOKEN_LIMIT_PARAM', 'OPS_REDACTION_SECRETS')) {
    Remove-Item -LiteralPath "Env:$testEnvName" -ErrorAction SilentlyContinue
}
```

Linux Bash：

```bash
export AIOPS_RUNTIME_DIR=./.runtime/local-test
export AIOPS_IMAGE=aiops-assistant:local-test
export ASSISTANT_HOST_PORT=18000 GRAFANA_HOST_PORT=13000
export PROMETHEUS_HOST_PORT=19090 ALERTMANAGER_HOST_PORT=19093 NODE_EXPORTER_HOST_PORT=19100
unset MODEL_API_KEY MODEL_BASE_URL MODEL_NAME MODEL_TIMEOUT MODEL_MAX_RETRIES
unset MODEL_MAX_TOKENS MODEL_TOKEN_LIMIT_PARAM OPS_REDACTION_SECRETS
```

所有测试栈命令都显式使用 `-p aiops-local-test --env-file .env.example`。
`--env-file` 避免读取根 `.env` 的业务密钥；进程环境变量仍优先，所以先清除上面的凭据。
换终端后须重新设置测试目录、镜像和端口，否则不要继续执行测试栈命令。
init 仍会只读检查根 `.env` 的旧字段；若当前工作副本含遗留输入，使用独立干净源码
检出执行测试，不改动真实部署的根环境文件来绕过检查。

```bash
docker compose version
docker info
docker compose -p aiops-local-test --env-file .env.example config --quiet
docker compose -p aiops-local-test --env-file .env.example up -d --build --wait --wait-timeout 180
docker compose -p aiops-local-test --env-file .env.example ps --all
```

预期：`init` 为 `Exited (0)`；五个常驻服务通过健康检查；`up` 返回成功。
180 秒是容器就绪等待时间，不包含构建和镜像下载。若失败，先查看
`docker compose -p aiops-local-test --env-file .env.example logs --tail 100`，修正原因后重试。

| 页面 | 测试地址 |
|---|---|
| Assistant 控制台 | `http://127.0.0.1:18000/console` |
| Grafana | `http://127.0.0.1:13000` |
| Prometheus Targets | `http://127.0.0.1:19090/targets` |
| Alertmanager | `http://127.0.0.1:19093` |

本机运行的 Compose Exporter 采集 Docker Linux 环境；不要把这些数值解释为 Windows
或 macOS 宿主机指标。若在服务器终端执行测试，从个人电脑浏览器访问时需使用相应 SSH 隧道
或已配置的代理地址，不能把个人电脑的 `127.0.0.1` 当作服务器。

## 4. 自动验证权限、告警链路与重建持久化

本节必须按顺序执行，再进行后续页面修改。两个脚本会创建合成身份、主机、凭据、会话、
配置注释及状态文件，向本地 Alertmanager 投递告警；不连接真实模型/SSH。只用于可丢弃栈。
用户权限脚本先登记测试主机，部署脚本随后记录配置哈希，顺序颠倒会使重建校验失败。

首次验证（cp 与 exec 可用于 PowerShell/Bash）：

```bash
docker compose -p aiops-local-test --env-file .env.example cp tests/compose_access_smoke.py assistant:/tmp/compose_access_smoke.py
docker compose -p aiops-local-test --env-file .env.example exec -T assistant python /tmp/compose_access_smoke.py
docker compose -p aiops-local-test --env-file .env.example cp tests/compose_smoke.py assistant:/tmp/compose_smoke.py
docker compose -p aiops-local-test --env-file .env.example exec -T -e PYTHONPATH=/app assistant python /tmp/compose_smoke.py
```

两次退出码均应为 0。权限脚本验证两用户私有会话、主机范围、跨主机 9 类查询和 Token
轮换；部署脚本验证 ready、鉴权、17 项即时/趋势、页面凭据、关键词聊天及真实本地
Alertmanager → Assistant → SQLite 归档。不要在此时编辑主机或凭据。

整栈重建，然后先验证部署持久化、再验证用户权限；/tmp 不持久化，必须重新复制：

```bash
docker compose -p aiops-local-test --env-file .env.example up -d --force-recreate --wait --wait-timeout 180
docker compose -p aiops-local-test --env-file .env.example cp tests/compose_smoke.py assistant:/tmp/compose_smoke.py
docker compose -p aiops-local-test --env-file .env.example exec -T -e PYTHONPATH=/app assistant python /tmp/compose_smoke.py --verify
docker compose -p aiops-local-test --env-file .env.example cp tests/compose_access_smoke.py assistant:/tmp/compose_access_smoke.py
docker compose -p aiops-local-test --env-file .env.example exec -T assistant python /tmp/compose_access_smoke.py --verify
```

预期凭据、配置、会话/消息、ACL 保留；撤权阻止旧会话，恢复授权可读历史，撤销 Token
返回 401，原管理员与 webhook 仍有效。成功验证会消费状态文件并清理部分测试数据，
不能重复 --verify。失败保留日志与数据，不删除目录来获得“通过”。

## 5. 控制台与业务验收

显式查看测试栈管理员 Token 和 Grafana 初始密码：

```bash
docker compose -p aiops-local-test --env-file .env.example run --rm --no-deps init show-token
docker compose -p aiops-local-test --env-file .env.example run --rm --no-deps init show-grafana-password
```

Token 输入控制台连接框，Grafana 用户名为 `admin`。报告中不记录明文密钥。
以下表格可逐项打勾，实际结果记录在第 9 节模板中。

| 编号 | 操作 | 预期结果 |
|---|---|---|
| UI-01 | 匿名打开控制台，尝试连接一个无效 Token，再输入有效管理员 Token | 静态页面可打开；无效 Token 不能取得业务数据；管理员连接后可见“系统管理” |
| UI-02 | 查看 `local-node` 主机状态、CPU/内存和区间指标 | Exporter 可达，指标有样本及观测时间；无样本明确显示空状态，区间值不冒充逐点曲线 |
| UI-03 | 创建名为“本机验收”的会话，发送“CPU”和“内存”，刷新页面 | 会话绑定 `local-node`，问题与回答可重新读取；等待动画结束，无模型密钥时走关键词问答 |
| UI-04 | 改会话名，另建一个临时会话并删除 | 改名保存；只删除选中的临时会话，“本机验收”及其消息仍存在 |
| UI-05 | 切换主题、密度、收藏和时间范围，再刷新 | 浏览器偏好保留，服务端主机配置与授权不被改变 |
| UI-06 | 断开连接，再重新连接 | 页面清除 Token 和已加载的管理表单；重新连接恢复访问；服务端会话不因断开而删除 |
| CFG-01 | 管理页切换前四个分区并修改草稿，再放弃修改 | 切换时草稿不丢失；放弃后重新加载服务端实际配置 |
| CFG-02 | 无进行中业务或待审批时，关闭巡检并保存，再重新启用并保存 | 两次保存成功且立即生效；`/ready` 正常；关闭后不再巡检，启用后按新间隔继续；调度线程保持唯一，无需重启容器 |
| CFG-03 | 修改一项草稿但不保存，尝试进入新增主机向导 | 页面要求先保存或放弃草稿，不静默丢失修改 |
| CFG-04 | 使用两个标签页读取同一配置；第一个保存一个开关，第二个提交旧草稿 | 旧 revision 返回 409，不覆盖第一次保存；第二个重载后可正常编辑；最后恢复原开关 |
| HOST-01 | 有独立测试 Exporter 时，从管理页登记其可达目标，先不配置 SSH | 新主机出现在清单，等待发现和抓取后有监控数据；三类命令授权为空 |
| HOST-02 | 有两台测试主机时分别创建会话、查询指标、查看告警及记录 | 会话各自保持创建时的 `host_id`；切换顶部主机不会改绑旧会话；结果与对应目标一致 |

新增主机只登记现有 Exporter，不会安装或启动远程服务。测试目标必须从 Prometheus 容器
可达；容器中的 `localhost` 指向容器自身。HOST-01/02 未准备第二个 Exporter 时标记“未执行”。
清退测试主机使用管理员“删除主机登记”入口，先撤销用户授权、移除规则引用并处理未决/运行
任务；仅登记移除时采集继续，停止托管采集须明确选择。仍须保留至少一台启用主机，历史
按各自保留策略保留；外部 Prometheus 由其维护者处理。完整约束见[配置指南](configuration.md#hostsyaml)。
专用测试栈可在完成验收后整体停止并保留证据。

## 6. 告警和异常恢复

先确认测试栈没有自动处置规则，写目录关闭。合成告警只修改 textfile 指标，不制造
真实 CPU 负载；已有业务规则可能对它执行真实写操作：

```bash
docker compose -p aiops-local-test --env-file .env.example run --rm --no-deps --entrypoint python init scripts/demo_fault.py cpu on --runtime /runtime
```

等待数次抓取、规则评估和通知周期，通常预留 1～2 分钟；查看 Prometheus 的
`HighCPUUsage`、Alertmanager 通知以及控制台 `local-node` 的告警历史。
合成告警不改变真实 `cpu.usage` 指标，不要求 CPU 指标卡同时显示 95%。
测试后恢复：

```bash
docker compose -p aiops-local-test --env-file .env.example run --rm --no-deps --entrypoint python init scripts/demo_fault.py reset --runtime /runtime
```

| 编号 | 操作 | 预期结果 |
|---|---|---|
| ALERT-01 | 触发上面的合成 CPU 告警并恢复 | firing 告警送达 Assistant 且归属 `local-node`；恢复后最终收到 resolved，已归档记录可查；默认无自动写操作 |
| FAIL-01 | `docker compose -p aiops-local-test --env-file .env.example stop node-exporter` | 等待抓取后主机 Exporter 显示不可达；不能把旧样本当作新的成功采集；Prometheus 正常时整体 `/ready` 仍可就绪 |
| FAIL-02 | `docker compose -p aiops-local-test --env-file .env.example start node-exporter` | 后续抓取恢复，主机状态与指标恢复，不用清空数据或删除主机 |
| FAIL-03 | `docker compose -p aiops-local-test --env-file .env.example stop prometheus` | `/health` 仍返回 200；`/ready` 返回 503；监控请求显示查询失败，不显示成真实 CPU 为 0 或主机确定离线 |
| FAIL-04 | `docker compose -p aiops-local-test --env-file .env.example start prometheus` | 等待 Prometheus 就绪和重新抓取后，`/ready` 与监控恢复；已有会话和历史仍可读 |

在 PowerShell 用 `curl.exe -i http://127.0.0.1:18000/ready` 检查 HTTP 状态；Bash 用
`curl -i http://127.0.0.1:18000/ready`。同时核对响应 `components` 与日志，不能只看浏览器顶部
“已连接”。每个故障场景完成后先恢复依赖，再执行下一个场景。

巡检日志位于 `.runtime/local-test/assistant-logs/inspection.log`，默认周期为 300 秒；
`/inspections/recent` 只归档异常，正常轮次没有异常记录也可能是正确结果。验证异常巡检时，
在测试栈临时调整指标阈值使已采集值达到异常等级，显式应用后等待一轮；核对时间、主机和
指标快照，再恢复原阈值并应用。合成告警本身不等同于异常巡检。

### 发送绑定测试主机的单条告警

本场景只验证 Alertmanager → Assistant → 归档/规则/AI 路径，不制造 CPU 负载，
也不经过 Prometheus 告警表达式。默认先保持自动处置关闭；仅在
[规则日志标记演练](guide.md#第一次配置告警规则自动处置) 中明确配置了测试动作时打开对应开关。

前提：已按第 3 节建立独立栈，终端的 AIOPS_RUNTIME_DIR、端口等环境变量保持原值；
已登记自己的 Linux 测试主机，并确认实际 job、instance。
在**部署机项目根目录**用编辑器创建 `.runtime/acceptance_alert.py`，粘贴以下内容，
将 job/instance 替换为该主机真实标签。示例地址是文档保留地址，不对应真实目标。

```python
from datetime import datetime, timedelta, timezone
import uuid
import requests

now = datetime.now(timezone.utc)
run_id = uuid.uuid4().hex[:12]
payload = [{
    "labels": {
        "alertname": "AIOpsAcceptance",
        "job": "aiops-hosts",
        "instance": "192.0.2.20:9100",
        "severity": "warning",
        "test_run": run_id,
    },
    "annotations": {"summary": "Isolated acceptance alert"},
    "startsAt": now.isoformat(),
    "endsAt": (now + timedelta(minutes=2)).isoformat(),
}]
response = requests.post("http://alertmanager:9093/api/v2/alerts", json=payload, timeout=10)
response.raise_for_status()
print("Alertmanager accepted", response.status_code, "test_run=", run_id)
```

Bash 执行（脚本在 init 的一次性 Python 容器运行，不要求部署机安装 requests）：

```bash
docker compose -p aiops-local-test --env-file .env.example run --rm -T --no-deps --entrypoint python init - < .runtime/acceptance_alert.py
```

Windows PowerShell 使用相同文件内容，改用管道：

```powershell
Get-Content -Raw -LiteralPath .runtime/acceptance_alert.py | docker compose -p aiops-local-test --env-file .env.example run --rm -T --no-deps --entrypoint python init -
```

执行结束应返回 0 并打印 2xx。接收成功只证明 Alertmanager 接受，继续等待通知周期，
在对应测试栈控制台选择该主机、进入“运维记录”看 AIOpsAcceptance 告警归档及 test_run。
约两分钟到期后再等通知周期检查 resolved；不持续重发同一告警来保持 firing。
每次执行生成新 test_run，适合演练关闭自动执行与启用测试动作的差异。
Alertmanager 会按告警名/实例分组，重复操作可能要等待 group_interval 才投递。
未匹配规则且启用了 LLM 时可能调用模型；有规则但关闭自动执行不会转交 AI。
只验收内置 local-node 时，将 job 改为 node-exporter、instance 改为 node-exporter:9100。
归属错误先检查标签，不直接扩大规则授权。

### 独立 Linux 测试主机的真实资源负载

这三个脚本用于在**独立 Linux 测试主机**上主动触发告警。默认直接制造高负载，
不需要先调低 Prometheus 的告警阈值。它们都是独立的 Bash 文件，只复制需要的那个也能运行。

| 文件 | 直接运行的效果 |
|---|---|
| [load_gen.sh](../scripts/load_gen.sh) | 全部可用 CPU 核心满载，保持 5 分钟 |
| [load_mem.sh](../scripts/load_mem.sh) | 根据当前剩余内存分配数据，把内存使用率升到约 90%，运行 5 分钟 |
| [load_disk.sh](../scripts/load_disk.sh) | 在 `/var/tmp` 所在磁盘创建临时文件，把该磁盘使用率升到约 90%，运行 5 分钟 |

**第一步：确认监控已连通。** 在 Assistant 控制台选择测试主机，确认能看到 CPU、内存
和磁盘的实时数据。脚本要在这台被监控的测试主机上执行；在 Assistant 容器里运行，
通常不会得到目标主机的高负载告警。Exporter 的 job/instance 应与主机配置一致。

**第二步：复制并运行脚本。** 从项目的 `scripts/` 目录复制三个 `load_*.sh` 文件到
测试主机，例如放到 `~/aiops-load-test/`，在该目录打开终端。需要 Linux、Bash 4+ 和
常见的 GNU 系统工具，不需要安装 Python、stress-ng 或其他压测程序。直接用 `bash`
运行，不必先 `chmod +x`，一般也不需要 sudo。若目标目录不可写，换成同一磁盘上的可写目录。

在复制脚本后的目录运行；一次执行一项，结束后再测试下一项：

```bash
# CPU：全核满载 5 分钟。
bash load_gen.sh

# 内存：升到约 90%，运行 5 分钟。
bash load_mem.sh

# 磁盘：/var/tmp 所在磁盘升到约 90%，运行 5 分钟。
bash load_disk.sh
```

脚本会打印设置和实际使用率。按 `Ctrl+C` 可以提前结束；到时会自动停止。
CPU 只清理自己启动的负载进程，内存随进程退出释放，磁盘只删除本次创建的临时文件。
脚本不会修改告警规则或自动处置设置；如果启用了自动处置，告警仍会按现有配置执行。

**第三步：观察告警和恢复。** 默认规则为 CPU >80%、内存 >85%、磁盘 >80%，
连续超限 30 秒。CPU 使用最近 5 分钟的 rate，还需要等待抓取、规则评估和通知，
所以启动后不会立刻收到告警，默认 5 分钟用于留出观察时间。

1. 在 Prometheus 的 Alerts 页面找到 `HighCPUUsage`、`HighMemoryUsage` 或 `HighDiskUsage`。
2. 确认状态从 `pending` 变为 `firing`，并在 Alertmanager 中看到同一测试主机的告警。
3. 在 Assistant 的“运维记录”查看告警归档；记录告警名、主机和触发时间。
4. 脚本结束后等待使用率回落，确认告警恢复为 `resolved`。CPU 的五分钟统计窗口需要时间衰减。

脚本显示的 CPU 是约 1 秒间隔的 `/proc/stat` 差分，便于观察负载是否启动；控制台与 CPU
告警使用 5 分钟 rate。两者窗口不同，瞬时读数上升或回落不要求五分钟指标立即同步。
既有部署以实际加载的规则为准，默认阈值和各指标口径见[查询对照表](metric-queries.md)。

如果脚本显示高使用率而没有告警，先核对 Prometheus 的实际指标、规则和实例标签，
再检查 Alertmanager 投递及 Assistant 日志。**脚本退出成功，只说明负载测试完成，
不能代替整条告警链路的验收。**

**调整时间和强度：** 第一个参数统一是分钟数；填 `0` 表示一直运行，直到 `Ctrl+C`。
CPU 的第二个参数是负载进程数；内存、磁盘的第二个参数是目标使用率。
磁盘的第三个参数是放置临时文件的目录，该目录必须已存在。

```bash
bash load_gen.sh 8          # 全核满载 8 分钟。
bash load_gen.sh 6 4        # 4 个进程，运行 6 分钟。
bash load_gen.sh 0          # 全核持续运行，Ctrl+C 结束。
bash load_mem.sh 8 90       # 目标内存使用率 90%，运行 8 分钟。
bash load_disk.sh 8 90 /data # /data 所在磁盘升到约 90%，运行 8 分钟。
```

多核主机上只开 1 个 CPU 进程，可能无法达到整机 80% 的告警阈值；默认会使用 `nproc`
报告的全部可用核心。虚拟机无法跑满时，可把进程数设为核心数的两倍，最多 256 个。
内存和磁盘目标支持 1～95%，默认 90%；根据当前余量计算占用量，不再默认只增加几十 MiB。
内存会考虑容器的 cgroup 限额，显示的是当前运行环境的有效内存使用率；为验证宿主机
告警，应直接在宿主机运行。

磁盘脚本优先用 `fallocate` 快速占用空间，系统不支持时改用 `dd` 实际写入。
压缩、去重或特殊文件系统可能让实际占用低于预期，以打印的使用率和 Prometheus 指标为准。
`/tmp`、`/var/tmp` 或 `/data` 可能位于不同磁盘；可用 `df -h /var/tmp` 确认。
默认规则排除 `tmpfs` 和 `overlay`，脚本的目标使用率模式也会拒绝这两类文件系统，
此时换成真实 Linux 磁盘上的目录。

只想看计划、不产生负载时，加 `--dry-run`：

```bash
bash load_gen.sh --dry-run
bash load_mem.sh --dry-run
bash load_disk.sh 5 90 /var/tmp --dry-run
```

检查脚本本身时，也可以指定很小的负载和秒数。`--duration` 的单位是**秒**，
不能和前面的分钟数参数同时填写；`--mib` 表示额外分配多少 MiB，会替代目标使用率模式。
这些小负载用于验证运行和清理，通常不会触发告警。

```bash
bash load_gen.sh --workers 1 --percent 10 --duration 3
bash load_mem.sh --mib 8 --duration 3
bash load_disk.sh --directory /var/tmp --mib 8 --duration 3
```

为保持测试机可操作，内存至少留出总量的 2% 或 32 MiB，磁盘至少留出总量的 1% 或 8 MiB，
取两者中较大值；内存分配还预留临时开销。余量不足会明确停止，低内存小机器可能无法达到
90%。使用 `--help` 查看完整参数。

强制 `kill -9`、断电或主机崩溃无法执行退出清理。CPU 子进程会检查父进程并自行停止，
内存由系统回收；磁盘文件可能遗留。磁盘脚本启动时会打印
`aiops-safe-stress-XXXXXXXX.tmp` 的完整路径，核对后只删除该文件即可。

想单独检查 Bash 参数和安全余量逻辑，可在 Linux 项目目录执行下面的测试。
默认只用合成读数，不产生真实 CPU、内存或磁盘负载：

```bash
bash tests/safe_stress.test.sh
```

测试的 --smoke 选项会产生极小负载，只用于受限测试容器，不纳入默认离线回归的真实负载。

## 7. AI、SSH 与人工审批（按需执行）

本节使用独立 Linux 测试主机，在测试栈管理页登记模型配置、SSH 凭据和经核验的
known_hosts，再关联目标主机。完整模型需密钥、接口根地址与名称，且支持标准工具调用。
保存后热生效，使用显式连接按钮核对；按需启用 Agent/LLM。详细操作见
[部署配置](deployment.md#可选配置模型与端口)。页面凭据不需要写入另一个环境文件。

| 编号 | 操作 | 预期结果 |
|---|---|---|
| AI-01 | 新建目标主机会话，询问“结合当前 CPU、内存和告警分析主机状态” | 回答依据工具事实，包含正确主机和数据时间；没有数据时说明缺失，不编造取数结果 |
| AI-02 | 触发一条未命中处置规则的测试告警 | 先尝试 AI 分析再最终归档，有对应建议或明确失败日志；模型失败不抹掉告警历史，也不触发未授权动作 |
| SSH-01 | 通过 SSH 认证检查，只授予 `memory`、`disk` 诊断，再在会话要求查看内存和磁盘 | 只在会话绑定主机执行授权只读命令；结果有时间与输出，不返回连接凭据 |
| SSH-02 | 使用测试凭据模拟认证失败或未登记主机密钥 | 连接被拒绝并显示明确错误；不自动信任未知密钥；恢复正确凭据并保存后可恢复 |
| AI-03 | 在专用测试栈中模拟模型不可达或无效密钥，再发起聊天或测试告警 | 单次 SDK 请求遵守超时/重试，整轮结束后给出失败或降级结果；记录整轮耗时，多回合不保证总时限；公开探针可访问，告警仍归档；恢复配置后能继续使用 |

审批测试需要启用 Agent 写命令目录并仅给测试主机授权一个测试动作。
为避免重启或清理业务服务，可在管理员命令目录中临时配置只写入测试主机系统日志的动作：

```yaml
test_marker:
  summary: 写入本机验收日志标记
  template: "logger -t aiops-acceptance -- {message}"
  sudo: false
  params:
    message:
      type: str
      pattern: "^[A-Za-z0-9_-]{1,64}$"
```

先确认测试主机有 `logger`，测试用户可以写日志，验收人有读取日志的权限；该动作不需要
授予 `restart_service` 或 `cleanup_disk`。管理页保存配置后，向 Agent 提出明确请求，例如
“请使用 test_marker 在当前主机写入标记 local_test_01”。

| 编号 | 操作 | 预期结果 |
|---|---|---|
| APR-01 | 发起测试动作申请，先不审批 | 生成 `pending_approval`，列表显示申请人、目标主机、动作和参数；此时目标日志无标记 |
| APR-02 | 拒绝该申请 | 不执行命令；会话得到拒绝结果，有审批审计；旧申请不能再次处理 |
| APR-03 | 发起新的标记申请并批准 | 只对冻结主机执行一次；执行记录和审计能关联审批 ID；目标日志能找到对应标记 |
| APR-04 | 在申请仍待审批时尝试保存配置 | 返回 409，运行配置不被部分替换；拒绝或完成申请后再保存成功 |
| APR-05 | 发起新申请，重启测试栈 Assistant 后读取旧申请 | 未决审批失效，旧 ID 无法继续执行；不会自动补执行，已有持久会话与历史仍可读 |

复核目标日志可使用测试主机上的 `journalctl -t aiops-acceptance --no-pager`；不要把
日志读取权限不足判成动作必然未执行。若结果为 `unknown`，先核对审计、执行记录和目标
状态，不通过重复批准来猜测结果。参数注入、越权和重复提交还由离线安全/审批测试覆盖。
完成后取消未决审批、撤回测试动作授权，再删除未被引用的测试命令并恢复原开关。

## 8. 重启、备份与日志

在控制台保留“本机验收”会话、至少一条告警记录和一项已保存的配置，然后停止并启动
独立测试栈：

```bash
docker compose -p aiops-local-test --env-file .env.example down
docker compose -p aiops-local-test --env-file .env.example up -d --wait --wait-timeout 180
```

预期：原管理员 Token 仍有效，配置不被模板覆盖，会话/消息/告警/已完成执行记录保留。
`/alerts/recent` 内存缓冲和未决审批不要求跨重启保留；用 `/alerts/history` 验证告警持久化。
AI 阶段保持相同配置来源与运行目录，不在重启时更换凭据来源。

备份恢复测试按 [数据库备份与恢复](database.md#备份)在独立测试栈执行：

1. 记录测试会话 ID、告警记录和配置内容，停止测试栈；备份完整测试部署目录及其命名卷。
2. 在独立副本中验证 SQLite `PRAGMA integrity_check` 返回 `ok`。
3. 保留恢复前副本，按数据库文档恢复同批数据、配置和凭据，保持测试项目名与目录一致。
4. 启动后复查 Token、会话消息、告警和配置；备份后新增的数据不要求出现在恢复结果中。

不要在运行时只复制 `.db`，不要混用不同批次的 WAL。配置保存产生的 YAML 备份不是数据库备份。

查看测试栈日志：

```bash
docker compose -p aiops-local-test --env-file .env.example logs --tail 200 assistant init alertmanager
```

通过标准：失败有可定位的上下文；审批/执行有审计；没有未捕获异常造成的反复重启；
API、模型输出、截图和应用日志不暴露 Token、密码或私钥。密钥查看命令的显式终端输出
不是普通应用日志，不应复制进测试报告。

测试结束后用相同项目、目录和环境文件执行 `down`。这会保留测试数据供排查；
不要在已有部署中使用 `down -v` 或删除 `.runtime/`。测试终端用完关闭，避免测试端口和
`AIOPS_RUNTIME_DIR` 继续影响正常部署命令。

## 9. 测试记录模板

将记录保存到被忽略的 `.runtime/local-test-report.md` 或组织的测试系统，不填入明文凭据、
真实内部拓扑或未经脱敏的命令输出。以下是模板，不代表已执行：

```markdown
# AIOps-Assistant 0.6.2 容器测试记录

- 测试时间与时区：
- 操作系统 / Docker / Compose / Python / Node 版本：
- Git 提交 / 是否含未提交源码改动：
- Compose 项目名 / 测试部署目录：
- 模型与 SSH 是否配置（仅是/否）：
- 离线测试结果、跳过项与警告：
- Compose 初次检查 / 重建验证：

| 场景编号 | 结果（通过/失败/未执行） | 实际现象与脱敏证据 | 恢复结果 |
|---|---|---|---|
| UI-01 | | | |
| ALERT-01 | | | |
| FAIL-01 | | | |
| AI-01 | | | |
| APR-01 | | | |
| 重启持久化 | | | |
| 备份恢复 | | | |

- 结论：本次通过的测试范围；未执行项及原因。
```

基础验收要求离线回归、隔离 Compose 冒烟、页面操作、告警投递、依赖失败恢复和重启持久化
通过。实际启用 AI、SSH 或写命令的部署还需完成对应场景；未配置的能力不要求为了测试而开放。

## 凭据、全指标和真实 Prometheus 验收

离线 `test_admin_credentials.py` 覆盖管理员权限、一次性 Token、热生效/轮换/撤销、重启恢复、
SSH 文件路径限制、引用冲突、连接不执行命令、票据失效、模型保留/清除/版本缓存、失败回滚
和运行冲突。`test_metric_query_matrix.py` 参数化全部 17 项即时与趋势行为、单位/标签隔离、
空结果与失败、无 Swap、零吞吐、非法样本、密度与序列上限；Node 前端测试覆盖真实采样、
设备切换、断点、失败提示、旧异步响应隔离和敏感输入清理。

CI 的隔离 Compose job 先通过 `tests/prometheus_acceptance.py` 生成合成样本验收文件，
再用部署镜像中的 `promtool test rules` 对 17 项即时/趋势表达式求值，覆盖多设备、无 Swap、
零吞吐、缺失采集器、标签不匹配、采样不足，以及 up 正常/异常下的告警补零。
`compose_smoke.py` 对真实 Prometheus 分别请求 17 项即时/趋势 API，再写入测试 Token、
SSH 和无密钥模型配置；重建后验证配置、用户鉴权、原管理员、Webhook、会话与消息保留。
合成验收与整栈脚本只能在隔离、无真实密钥的测试部署执行，不对真实模型/SSH 发请求。
真实模型和 SSH 连接由管理员显式按钮另行验收；Docker 不可用时必须记录未执行，不能将
离线通过当作真实 Prometheus 或容器重建通过。

可在第 4 节完成后执行以下只写测试验收文件的命令，检查真实 Prometheus 引擎：

```bash
docker compose -p aiops-local-test --env-file .env.example cp tests/prometheus_acceptance.py assistant:/tmp/prometheus_acceptance.py
docker compose -p aiops-local-test --env-file .env.example exec -T -e PYTHONPATH=/app assistant python /tmp/prometheus_acceptance.py /app/data/metrics-promtool.yml
docker compose -p aiops-local-test --env-file .env.example cp assistant:/app/data/metrics-promtool.yml .runtime/local-test/metrics-promtool.yml
docker compose -p aiops-local-test --env-file .env.example cp .runtime/local-test/metrics-promtool.yml prometheus:/tmp/metrics-promtool.yml
docker compose -p aiops-local-test --env-file .env.example exec -T prometheus promtool test rules /tmp/metrics-promtool.yml
```

## 主机告警标签回归

`tests/prometheus_alert_acceptance.py` 生成独立的告警规则验收文件，使用实际部署的
`deploy/prometheus/alerts.yml`，与 17 项指标表达式验收分开执行，避免生成的 ALERTS
改变原有指标用例。五组场景覆盖真实指标、演示指标、同主机多分支去重、相同 instance
不同 job 隔离，以及健康状态无告警；同时断言 pending 和 firing 的完整标签。

```bash
docker compose -p aiops-local-test --env-file .env.example cp tests/prometheus_alert_acceptance.py assistant:/tmp/prometheus_alert_acceptance.py
docker compose -p aiops-local-test --env-file .env.example exec -T -e PYTHONPATH=/app assistant python /tmp/prometheus_alert_acceptance.py /app/data/alerts-promtool.yml
docker compose -p aiops-local-test --env-file .env.example cp assistant:/app/data/alerts-promtool.yml .runtime/local-test/alerts-promtool.yml
docker compose -p aiops-local-test --env-file .env.example cp .runtime/local-test/alerts-promtool.yml prometheus:/tmp/alerts-promtool.yml
docker compose -p aiops-local-test --env-file .env.example exec -T prometheus promtool test rules /tmp/alerts-promtool.yml
```

CI 在隔离 Compose 中执行同样的真实引擎验收。修改任一主机告警时，既要验证阈值，
也要验证标签；写法约束见 [主机告警 PromQL 编写约束](configuration.md#主机告警-promql-编写约束)。

## 用户与主机授权验收

离线矩阵在 tests/test_host_access.py：强制身份校验、多用户会话、全部主机查询入口、
无 host_id 的记录过滤、权限撤销/恢复、Token 轮换/撤销、配置冲突、非法授权回滚、
角色与申请人/审批人主机权限。前端另验证身份授权编辑、撤权清理和过期响应隔离。

整栈命令统一在第 4 节维护，顺序为权限首次验证 → 部署首次验证 → 重建 →
部署 --verify → 权限 --verify。状态文件包含合成 Token，只能在受限测试目录保留。

## 旧配置迁移验收

离线 test_config_loader.py 验证旧 auth.enabled、alert.trend_step、app.version 被拒绝；
test_config_migration.py 验证候选、冲突、备份、写入失败/恢复、中断和重复执行。
test_deployment.py 验证初始化保留与拒绝遗留字段；管理 API 提交旧字段应 422 且 revision 不变。

只在独立测试项目和目录构造旧配置，不导入真实凭据。先记录合成 Token、角色、ACL、
页面凭据及数据库摘要，停栈后按[显式迁移](deployment.md#一次性配置迁移)预览和执行。
使用 scripts/stack.py 时，COMPOSE_PROJECT_NAME 必须设为 aiops-local-test，
AIOPS_RUNTIME_DIR 与测试终端一致；该脚本不接受 Compose 的 -p 参数。

确认 dry-run 不写配置，正式迁移保留凭据与授权、创建受限备份，重复迁移无新候选；
新旧同义值冲突时停止且不输出值。启动后重复第 4 节与指标/告警验收。CI 使用
tests/compose_migration_smoke.py 在停栈测试目录播种并验证；它会改写配置，不能对业务目录运行。
真实模型/SSH 另行显式测试，不属于离线迁移验收。

## 发布门禁与现场演练

按变更选择相关用例定位问题，修复后执行完整离线回归。发布至少要求隔离 Compose、
页面、告警、权限、配置冲突和重建持久化通过；启用的模型/SSH/写路径必须真实闭环。
失败阻止发布，未执行项由负责人明确是否适用及补验计划，不引用旧版本数量当证据。

容量/并发、满盘、进程强制终止、迁移中断、备份恢复时间与数据丢失目标、长期日志轮转、
代理 TLS 和目标浏览器兼容性需在独立现场环境演练，属于待核实范围。故障注入先保留
可恢复副本和观察指标，结束后恢复依赖、开关与测试授权；不能在生产上试验边界。
