"""core：业务核心逻辑层。

- agent.py        OpsAgent 主控制器（聊天 / 告警处理 / 查询编排）
- chat.py         ChatService：Agent-first + 关键词兜底
- rule_engine.py  规则加载、匹配、调用执行器及产出处置记录
- analysis.py     告警 AI 分析（上下文组装 + LLM 编排）
- inspection.py   固定指标巡检、等级判定与单线程调度
- capabilities.py Agent 工具与关键词兜底共用的只读能力
- prompts.py      告警分析系统提示词
- exceptions.py   业务异常基类

运行时组件通过 interfaces 注入，可在测试中替换。指标目录与连接字段常量保留窄静态
依赖，详见 docs/architecture.md；不在 core 构造 Agent 或具体外部适配器。
"""
