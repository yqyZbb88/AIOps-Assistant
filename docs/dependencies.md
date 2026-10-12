# 依赖与构建

默认生产镜像保留监控、模型问答、人工审批和 SSH 全部能力。运行依赖清单中的每个库都有
实际用途；删除声明但仍被上游要求的包不会减少安装量，绕过上游依赖则会破坏受支持的运行环境。

## 运行依赖的用途

| 依赖 | 使用位置与保留理由 |
|---|---|
| FastAPI、Pydantic | API 路由、请求响应及配置模型校验 |
| Uvicorn | `scripts/deployment.py` 启动单 worker ASGI 服务 |
| Requests | Prometheus 监控适配器；LangSmith、tiktoken 也依赖它 |
| PyYAML | 配置解析、标量扫描与安全加载；LangChain Core 也要求它 |
| ruamel.yaml | 管理页与配置迁移保留注释、引号和占位符，不能直接用 PyYAML 写回替代 |
| python-dotenv | 部署凭据读取与旧环境配置迁移，属于部署必需依赖 |
| LangChain | `create_agent` 与人工审批中间件 |
| LangGraph | 内存检查点与审批恢复 `Command`；LangChain 同时依赖它 |
| LangChain Core | 消息、工具和模型公共类型，业务代码直接使用 |
| LangChain OpenAI | 统一模型工厂的 ChatOpenAI 适配器 |
| Paramiko | SSH 只读诊断、受控写命令与凭据连接检查 |

`httpx/httpcore` 与 `httpx2/httpcore2` 分别由当前 LangChain Core/LangGraph SDK 和
OpenAI SDK/LangSmith 要求，不能直接合并。LangSmith 即使关闭追踪也仍是 LangChain Core
的安装依赖。SSH 的 cryptography、PyNaCl、bcrypt 和 CFFI 是 Paramiko 的依赖，保留这些库
能维持现有 SSH 认证和主机密钥校验。

前端使用原生 HTML/CSS/JavaScript 和仓库内固定版本的 Marked，无 npm 安装或打包阶段。
Node 仅用于测试，pytest、Pygments、pluggy、iniconfig 也只进入 test 镜像。

## 版本约束

- `requirements-runtime.txt`：业务直接依赖与支持范围。
- `requirements-lock.txt`：Python 3.13 Linux 已验证的完整运行依赖版本。
- `requirements.txt`：引用运行清单，并直接固定 pytest 及其新增传递依赖；安装时复用运行约束。

约束文件不会主动安装其中的包。离线回归的 `test_dependencies.py` 会根据已安装发行包
元数据遍历实际依赖闭包，校验没有漏锁、陈旧运行约束或安装版本偏移。更新步骤和验证范围见
[贡献约定](../CONTRIBUTING.md#部署验证与依赖)与[容器测试与验收](testing.md)。

## 构建与缓存

Dockerfile 在复制业务源码前安装运行与测试依赖。只改源码或测试时，两层安装均可复用；
只更新 `requirements.txt` 时，生产依赖层保持有效。test 从独立测试依赖层复制生产目标的 `/app`，
运行文件与发布镜像一致，生产目标不会拉取 Node 或安装 pytest。

pip 下载缓存放在 BuildKit cache mount，不进入发布镜像。依赖变化时，本机构建可复用
已下载的包。安装使用 `--no-compile`，跳过第三方源码的字节码预编译；运行继续使用
`PYTHONDONTWRITEBYTECODE=1`，因此 Python 在导入时解析源码，镜像体积更小，启动有少量额外开销。

CI 的测试和部署任务分别使用 GitHub Actions 构建层缓存，独立 scope 避免相互覆盖；
`mode=max` 保存中间依赖阶段。部署任务先加载构建好的生产镜像，再用 Compose `--no-build`
启动。两个任务仍独立运行，缓存未命中或不可用时需重新下载、构建；GitHub Actions
层缓存不自动跨 runner 持久化 pip cache mount。实现依据见
[Docker 缓存优化](https://docs.docker.com/build/cache/optimize/)与
[GitHub Actions 缓存](https://docs.docker.com/build/ci/github-actions/cache/)。

本地部署命令保持不变，详见[部署与升级](deployment.md)。不清理 Docker 构建缓存可以保留
增量构建收益。首次部署仍需下载 Prometheus、Alertmanager、node-exporter 和 Grafana；
Grafana 提供既有仪表盘功能，不随 Assistant 依赖精简而移除或更换版本。
