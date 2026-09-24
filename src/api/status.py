"""公开、无业务数据的操作控制台空壳。

模块名沿用旧路由位置；旧 `/status` 与 `/status-assets/*` 已删除，不做重定向。
HTML/CSS/JS 可以匿名读取，但真实数据只能由浏览器使用同源业务 API 获取。
"""
from pathlib import Path

from fastapi import APIRouter
from fastapi.responses import FileResponse

router = APIRouter()
_WEB = Path(__file__).resolve().parents[1] / "web"
_HEADERS = {
    "Content-Security-Policy": "default-src 'self'; script-src 'self'; style-src 'self'; connect-src 'self'",
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
}


@router.get("/console", include_in_schema=False)
def console_page() -> FileResponse:
    headers = dict(_HEADERS)
    headers["Cache-Control"] = "no-store"
    return FileResponse(_WEB / "console.html", media_type="text/html", headers=headers)


@router.get("/console-assets/console.css", include_in_schema=False)
def console_css() -> FileResponse:
    return FileResponse(_WEB / "console.css", media_type="text/css", headers=_HEADERS)


@router.get("/console-assets/console.js", include_in_schema=False)
def console_js() -> FileResponse:
    return FileResponse(_WEB / "console.js", media_type="text/javascript", headers=_HEADERS)
