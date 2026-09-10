"""agent：保留的 Agent 编排层（create_agent + @tool + 提示词）。

注意：本包 __init__ 刻意保持为空——不在此处 eager import tools/assistant，
因为 tools.py 顶层会 import langchain；只有真正构建 Agent（assistant 工厂被调用）时才
惰性导入，确保「未装 langchain」环境里 core/container 等其余模块可照常 import。
"""
