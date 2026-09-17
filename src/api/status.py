"""只读状态页空壳；运维数据仍由现有 Bearer API 提供。"""
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


@router.get("/status", include_in_schema=False)
def status_page() -> FileResponse:
    headers = dict(_HEADERS)
    headers["Cache-Control"] = "no-store"
    return FileResponse(_WEB / "status.html", media_type="text/html", headers=headers)


@router.get("/status-assets/status.css", include_in_schema=False)
def status_css() -> FileResponse:
    return FileResponse(_WEB / "status.css", media_type="text/css", headers=_HEADERS)


@router.get("/status-assets/status.js", include_in_schema=False)
def status_js() -> FileResponse:
    return FileResponse(_WEB / "status.js", media_type="text/javascript", headers=_HEADERS)
