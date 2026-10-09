# 0.6.2 容器测试与验收

本文用于在开发机或部署机上验证 AIOps-Assistant 的功能、异常处理和数据保留。
每个场景给出操作与预期结果；测试报告按实际执行填写，不把部署成功等同于业务验收通过。
部署准备见 [部署与升级](deployment.md)，页面操作见 [控制台说明](console.md)。

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
[升级步骤](deployment.md#从旧开发栈升级)更新源码。所有后续命令都在项目根目录运行。

## 2. 离线自动化测试

全部回归在独立 test 镜像内运行，包含 Python 3.13、Node 22 与测试依赖；
生产镜像不包含 pytest 或测试代码。宿主机只需要 Docker，不安装项目运行环境。

```bash
docker build --target test -t aiops-regression:0.6.2 .
docker run --rm --network none aiops-regression:0.6.2
docker run --rm --network none --entrypoint node aiops-regression:0.6.2 --check src/web/console.js
docker run --rm --network none --entrypoint node aiops-regression:0.6.2 --test tests/console_ui.test.cjs
docker run --rm --network none --entrypoint python aiops-regression:0.6.2 -m compileall -q src scripts tests init.py
git diff --check
```

首次构建需下载镜像和依赖，运行回归时使用 --network none。测试自带合成身份、
关闭模型和追踪，不挂载真实运行目录；业务数据写入容器临时目录，退出即清理。
pytest 包含 Node 交互测试包装器；单独 Node 命令便于检查语法与查看前端用例结果。
构建执行 pip check。通过标准为无失败、无意外跳过、依赖与编译/差异检查无错误；
记录实际数量和第三方警告，不把固定数量作为通过条件。

定向检查可在镜像名后传入用例文件：

```bash
docker run --rm --network none aiops-regression:0.6.2 -q tests/test_deployment.py tests/test_stack.py
docker run --rm --network none aiops-regression:0.6.2 -q tests/test_admin_config.py tests/test_config_loader.py tests/test_hosts_config.py
docker run --rm --network none aiops-regression:0.6.2 -q tests/test_admin_credentials.py tests/test_metric_query_matrix.py
```

相关修正完成后执行完整回归，CI 使用同一 test 目标。

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

foreach ($testEnvName in @('DEEPSEEK_API_KEY', 'SSH_PASSWORD', 'SSH_KEY_FILENAME', 'SSH_KNOWN_HOSTS')) {
    Remove-Item -LiteralPath "Env:$testEnvName" -ErrorAction SilentlyContinue
}
```

Linux Bash：

```bash
export AIOPS_RUNTIME_DIR=./.runtime/local-test
export AIOPS_IMAGE=aiops-assistant:local-test
export ASSISTANT_HOST_PORT=18000 GRAFANA_HOST_PORT=13000
export PROMETHEUS_HOST_PORT=19090 ALERTMANAGER_HOST_PORT=19093 NODE_EXPORTER_HOST_PORT=19100
unset DEEPSEEK_API_KEY SSH_PASSWORD SSH_KEY_FILENAME SSH_KNOWN_HOSTS
```

所有测试栈命令都显式使用 `-p aiops-local-test --env-file .env.example`。
`--env-file` 避免读取根 `.env` 的业务密钥；进程环境变量仍优先，所以先清除上面的凭据。
换终端后须重新设置测试目录、镜像和端口，否则不要继续执行测试栈命令。

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

## 4. 自动验证告警链路与重建持久化

先完成本节，再修改测试配置。冒烟脚本会写入测试会话、发送一条本地 Alertmanager 告警、
在主机 YAML 中追加注释，并校验重建后凭据和配置的哈希；只允许在上述独立测试栈运行。
它不调用真实模型或 SSH。

使用 `cp` 而非终端输入重定向，以下命令可同时用于 PowerShell 和 Bash：

```bash
docker compose -p aiops-local-test --env-file .env.example cp tests/compose_smoke.py assistant:/tmp/compose_smoke.py
docker compose -p aiops-local-test --env-file .env.example exec -T -e PYTHONPATH=/app assistant python /tmp/compose_smoke.py
```

预期退出码为 0，最后显示：

```text
PASS: readiness, auth, all 17 instant/trend queries, managed credentials, chat and webhook fixture
```

脚本同时验证：公开 `/ready`、匿名业务请求 401、管理员访问管理配置、Alertmanager 身份不能
访问管理配置、管理员不能冒充 webhook 身份、Exporter 被正常抓取、关键词聊天、真实
Alertmanager → Assistant → SQLite 告警归档，并验证全部 17 项即时/趋势 API 与页面凭据保存。

重建后重新复制脚本，因为容器 `/tmp` 不持久化：

```bash
docker compose -p aiops-local-test --env-file .env.example up -d --force-recreate --wait --wait-timeout 180
docker compose -p aiops-local-test --env-file .env.example cp tests/compose_smoke.py assistant:/tmp/compose_smoke.py
docker compose -p aiops-local-test --env-file .env.example exec -T -e PYTHONPATH=/app assistant python /tmp/compose_smoke.py --verify
```

预期退出码为 0，显示：

```text
PASS: credentials, config, session and chat survived recreation
```

验证成功会删除脚本创建的测试会话和状态文件。若失败，保留数据与日志，按失败断言定位；
不要在两次校验之间编辑主机清单或更换凭据，也不要删除部署目录来获得“通过”。

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
| CFG-02 | 无进行中业务或待审批时，关闭巡检并保存，再重新启用并保存 | 两次保存成功且立即生效；`/ready` 正常；日志反映调度启停，无需重启容器 |
| CFG-03 | 修改一项草稿但不保存，尝试进入新增主机向导 | 页面要求先保存或放弃草稿，不静默丢失修改 |
| CFG-04 | 使用两个标签页读取同一配置；第一个保存一个开关，第二个提交旧草稿 | 旧 revision 返回 409，不覆盖第一次保存；第二个重载后可正常编辑；最后恢复原开关 |
| HOST-01 | 有独立测试 Exporter 时，从管理页登记其可达目标，先不配置 SSH | 新主机出现在清单，等待发现和抓取后有监控数据；三类命令授权为空 |
| HOST-02 | 有两台测试主机时分别创建会话、查询指标、查看告警及记录 | 会话各自保持创建时的 `host_id`；切换顶部主机不会改绑旧会话；结果与对应目标一致 |

新增主机只登记现有 Exporter，不会安装或启动远程服务。测试目标必须从 Prometheus 容器
可达；容器中的 `localhost` 指向容器自身。HOST-01/02 未准备第二个 Exporter 时标记“未执行”。
清退测试主机需按 [配置指南](configuration.md)核对规则与历史引用，不能把页面不存在的删除操作
当作测试失败。新建专用测试栈可在完成测试后整体停止保留证据。

## 安全合成告警演示

本节沿用前面已经建立的独立 `aiops-local-test` 测试栈、环境变量和数据目录。
不要在已有业务部署演示告警；其处置规则可能自动执行写命令。新测试栈的安全模板没有处置规则，
写命令关闭。以下命令只修改 Node Exporter textfile 合成指标，不制造真实 CPU 负载：

```bash
docker compose -p aiops-local-test --env-file .env.example run --rm --no-deps --entrypoint python init scripts/demo_fault.py cpu on --runtime /runtime
```

Prometheus 的高 CPU 规则包含 `for: 30s`，还需要等待抓取、规则评估和 Alertmanager 分组投递。
在控制台查看活动告警及告警归档，核对 `HighCPUUsage`、`local-node` 和时间。
未配置模型时仍可接收和归档，不会生成 AI 分析；合成告警不意味着真实 CPU 使用率升高，
也不保证产生异常巡检记录。

演示结束恢复指标，重复启动不会自动重置它：

```bash
docker compose -p aiops-local-test --env-file .env.example run --rm --no-deps --entrypoint python init scripts/demo_fault.py reset --runtime /runtime
```

等待 resolved 通知并检查归档；其它依赖停止/恢复场景见下一节。

## 6. 告警和异常恢复

合成告警只修改独立测试栈的 textfile 指标，不制造真实 CPU 负载：

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

## 7. AI、SSH 与人工审批（按需执行）

本节使用独立的 Linux 测试主机。模型密钥、SSH 私钥、已确认指纹的 known_hosts 与
容器路径按 [部署文档](deployment.md#可选配置模型端口与-ssh)准备；使用相同测试项目和
数据目录，但为此阶段创建单独、被忽略的环境文件（例如 `.runtime/local-ai-test.env`）。
将此阶段命令中的 `--env-file .env.example` 改为该文件，保持测试端口不变。
加入模型密钥后重建服务，并在管理页确认 Agent 和告警 AI 开关实际开启。

| 编号 | 操作 | 预期结果 |
|---|---|---|
| AI-01 | 新建目标主机会话，询问“结合当前 CPU、内存和告警分析主机状态” | 回答依据工具事实，包含正确主机和数据时间；没有数据时说明缺失，不编造取数结果 |
| AI-02 | 触发一条未命中处置规则的测试告警 | 告警归档后有对应 AI 分析或明确失败记录；模型失败不抹掉告警历史，也不触发未授权动作 |
| SSH-01 | 通过 SSH 认证检查，只授予 `memory`、`disk` 诊断，再在会话要求查看内存和磁盘 | 只在会话绑定主机执行授权只读命令；结果有时间与输出，不返回连接凭据 |
| SSH-02 | 使用测试凭据模拟认证失败或未登记主机密钥 | 连接被拒绝并显示明确错误；不自动信任未知密钥；恢复正确凭据并重建后可恢复 |
| AI-03 | 在专用测试栈中模拟模型不可达或无效密钥，再发起聊天或测试告警 | 请求在配置的超时/重试边界内结束并给出失败或降级结果；公开探针可访问，告警仍归档；恢复配置后能继续使用 |

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
AI 阶段继续使用对应环境文件，不要在重启时切换配置来源。

备份恢复测试按 [数据库备份与恢复](database.md#九备份)在独立测试栈执行：

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

隔离 Compose 自动验收先运行用户权限脚本，再运行上面的 compose_smoke.py，
避免新增测试主机改变后者记录的配置哈希；重建后分别执行两个脚本的 --verify。
使用同一隔离项目及环境，不连接模型或 SSH：

```bash
docker compose -p aiops-local-test --env-file .env.example cp tests/compose_access_smoke.py assistant:/tmp/compose_access_smoke.py
docker compose -p aiops-local-test --env-file .env.example exec -T assistant python /tmp/compose_access_smoke.py
# 然后运行 compose_smoke.py、重建整栈、运行 compose_smoke.py --verify
docker compose -p aiops-local-test --env-file .env.example cp tests/compose_access_smoke.py assistant:/tmp/compose_access_smoke.py
docker compose -p aiops-local-test --env-file .env.example exec -T assistant python /tmp/compose_access_smoke.py --verify
```

用户脚本会登记 restricted-node 测试目标，创建两个合成身份并授予不同主机，检查跨用户
读取/聊天/改名/删除均拒绝，跨主机 9 类查询均拒绝，轮换后身份及会话保留。
重建验证历史与 ACL，再测试撤权阻止旧会话、恢复授权恢复历史、Token 撤销返回 401，
同时检查原管理员及 webhook 继续有效。状态文件含合成测试 Token，验收后清除，
只能用于可丢弃栈。成功的 --verify 会消费状态文件，不能重复执行。

## 废弃配置迁移验收

tests/test_config_loader.py 验证旧 auth.enabled、alert.trend_step、app.version 被严格拒绝，
代码版本为只读元信息；tests/test_deployment.py 覆盖新模板无鉴权开关、四类旧字段移除、
未知字段保留、原文备份、环境占位符引号、角色/ACL/凭据保留、重复初始化及备份/写入失败。
tests/test_admin_config.py 验证 GET 不含 auth_enabled、提交旧字段返回 422 且 revision 不变。
容器启动无可用 Token 时失败；内部空配置装配的业务请求仍为 503，不能匿名访问。

实际隔离升级验收应先用旧模板初始化配置，保留 JSON 环境占位符引号，再启动新版 init。
确认原文备份完整、只移除已列明字段、原部署凭据未变化、服务健康；然后运行用户权限及
部署 smoke，整栈重建后运行两个 --verify。仅使用可丢弃项目、数据目录和合成凭据，
不直接对实际部署运行写入型验收。2026-10-09 本轮结果见 [交接记录](handover.md)。
