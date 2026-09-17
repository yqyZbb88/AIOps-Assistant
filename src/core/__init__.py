"""core：业务核心逻辑层。

- agent.py        OpsAgent 主控制器（聊天 / 告警处理 / 查询编排）
- chat.py         ChatService：Agent-first + 关键词兜底
- rule_engine.py  规则引擎（加载 + 匹配决策，只依赖 ActionExecutor 接口）
- analysis.py     告警 AI 分析（上下文组装 + LLM 编排）
- inspection.py   固定指标巡检、等级判定与单线程调度
- capabilities.py Agent 工具与关键词兜底共用的只读能力
- prompts.py      告警分析系统提示词
- exceptions.py   业务异常基类

core 只依赖 interfaces 抽象接口与注入组件，不依赖具体插件实现（可单测替换）。
"""
