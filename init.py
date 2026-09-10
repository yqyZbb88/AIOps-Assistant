import os

from dotenv import load_dotenv
from langchain.chat_models import init_chat_model

load_dotenv(override=True)    # 从.env 文件中提取并覆写相关接口值

DEEPSEEK_API_KEY = os.getenv("DEEPSEEK_API_KEY")
DEEPSEEK_BASE_URL = os.getenv("DEEPSEEK_BASE_URL")

model = init_chat_model(        # 实际上就是调用了ChatDeepSeek函数
    model="deepseek-v4-flash",
    model_provider="deepseek",
    api_key=DEEPSEEK_API_KEY,
    base_url=DEEPSEEK_BASE_URL,
    # max_retries=3,        # 网络请求重试次数
    # extra_body={
    #     "thinking": {"type": "disabled"}  # 关闭思考模式,开启情况下无法测试结构化输出
    # }
)