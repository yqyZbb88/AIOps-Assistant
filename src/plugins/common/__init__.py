"""plugins/common：跨插件家族的共享基础设施（动作写边界 与 只读诊断读边界 共用）。

- endpoint.py  SshEndpoint：SSH 连接参数统一抽象（认证二选一 key_filename → password）；
- ssh.py       exec_command：给定 Endpoint + 命令串 执行并返回 (exit_code, stdout, stderr)；
- audit.py     record_execution：统一审计写入 aiops_assistant.audit（收敛两边重复的 JSON 拼装）；
- cmddef.py    白名单命令目录机制：模板 + 参数声明 + 校验 + 安全命令构建（读/写目录共用）。

本包中性：不 import langchain / paramiko（paramiko 只在 ssh.py 的调用点惰性导入），
不向 src.registry 注册任何插件，可被 actions / diagnostics 安全共用。
"""
