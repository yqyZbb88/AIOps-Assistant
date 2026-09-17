"""API JSON 输出的最后一道递归脱敏边界。"""
from typing import Any

from fastapi.responses import JSONResponse

from src.utils.security import redact_sensitive


class RedactingJSONResponse(JSONResponse):
    def render(self, content: Any) -> bytes:
        return super().render(redact_sensitive(content))
