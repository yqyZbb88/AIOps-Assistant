"""DeepSeek 模型初始化入口。

模型凭据和参数统一通过环境变量配置。
"""
import os

from dotenv import load_dotenv
from langchain.chat_models import init_chat_model

load_dotenv(override=False)   # 环境变量优先；.env 只补充未设置项

DEEPSEEK_API_KEY = os.getenv("DEEPSEEK_API_KEY")
DEEPSEEK_BASE_URL = os.getenv("DEEPSEEK_BASE_URL")
DEEPSEEK_MODEL = os.getenv("DEEPSEEK_MODEL", "deepseek-v4-flash")
DEEPSEEK_TIMEOUT = float(os.getenv("DEEPSEEK_TIMEOUT", "30"))
DEEPSEEK_MAX_RETRIES = int(os.getenv("DEEPSEEK_MAX_RETRIES", "2"))
DEEPSEEK_MAX_TOKENS = int(os.getenv("DEEPSEEK_MAX_TOKENS", "2048"))

model = init_chat_model(        # 实际上就是调用了ChatDeepSeek函数
    model=DEEPSEEK_MODEL,
    model_provider="deepseek",
    api_key=DEEPSEEK_API_KEY,
    base_url=DEEPSEEK_BASE_URL,
    timeout=DEEPSEEK_TIMEOUT,
    max_retries=DEEPSEEK_MAX_RETRIES,
    max_tokens=DEEPSEEK_MAX_TOKENS,
    # 如需关闭思考模式，可按 DeepSeek 接口文档传入额外请求参数；默认不启用。
)
