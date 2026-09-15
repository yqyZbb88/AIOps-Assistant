"""健康检查路由。"""
from __future__ import annotations

from fastapi import APIRouter, Request

router = APIRouter()


@router.get("/health", response_model=dict[str, str])
def health(request: Request) -> dict[str, str]:
    """健康检查接口，便于接入监控 / 负载均衡。

    另回带 `storage` 字段反映持久化状态——**这是为了让降级可被观测**：

    - `ok`       持久化正常（或未启用，那不算故障）；
    - `degraded` 读写失败过（磁盘满 / 权限 / 文件损坏）——此时聊天仍能答，但会话与
      告警归档**不再落盘**，且失败会被 store 内部记 ERROR 后降级，不抛到用户面前。

    没有这个字段的话，「历史静默不再记录」只能靠翻日志发现，而这恰恰是最容易被
    误当成「模型最近记性变差」的那类故障。
    """
    container = getattr(request.app.state, "container", None)
    stores = []
    if container is not None:
        # sessions / history 共享数据库实例，但各自维护「最近一次操作是否成功」，
        # 因此必须一起看。只看 sessions 会让「归档坏了、聊天还正常」显示为全绿。
        stores = [container.sessions(), container.history()]
    storage_ok = all(store is None or store.healthy for store in stores)
    storage = "ok" if storage_ok else "degraded"
    return {"status": storage, "storage": storage}
