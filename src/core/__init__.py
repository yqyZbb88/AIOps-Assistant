"""core：业务核心逻辑层。

- agent.py        OpsAgent 主控制器（聊天 / 告警处理 / 查询编排）
- chat.py         ChatService：Agent-first + 关键词兜底
- rule_engine.py  规则引擎（加载 + 匹配决策，只依赖 ActionExecutor 接口）
- analysis.py     告警 AI 分析（上下文组装 + LLM 编排）
- exceptions.py   业务异常基类

core 只依赖 interfaces 抽象接口与注入组件，不依赖具体插件实现（可单测替换）。
"""
