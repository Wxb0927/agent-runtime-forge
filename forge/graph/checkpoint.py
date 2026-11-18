"""checkpointer 装配。⚠️ 实测坑（⛔ 凭记忆）：

* ``SqliteSaver`` **只实现同步**——它的 ``aget_tuple`` 直接 ``NotImplementedError``。
  并发/服务化路径必须换 ``AsyncSqliteSaver``（依赖 ``aiosqlite``）。
* 两个 ``from_conn_string`` 都是**上下文管理器**，⛔ 当普通构造函数用（拿不到 ``__enter__``
  就会在 GC 时才落盘，测试会随机丢 checkpoint）。
* ⚠️ 业务大对象⛔ 进 state：``Dataset`` 按 seed 重载，checkpoint 里只放引用与指标。
"""
from __future__ import annotations

from contextlib import asynccontextmanager, contextmanager
from pathlib import Path
from typing import Any, Iterator

DEFAULT_DB = Path("data") / "checkpoint.sqlite3"


def _ensure_parent(path: Path) -> Path:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


@contextmanager
def saver(path: str | Path = DEFAULT_DB) -> Iterator[Any]:
    """同步 saver（Line B 压测、intake 图跨进程续办用）。"""
    from langgraph.checkpoint.sqlite import SqliteSaver

    p = _ensure_parent(Path(path))
    with SqliteSaver.from_conn_string(str(p)) as s:
        yield s


@asynccontextmanager
async def async_saver(path: str | Path = DEFAULT_DB):
    """异步 saver（并发压测、D6 服务化用）。"""
    from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver

    p = _ensure_parent(Path(path))
    async with AsyncSqliteSaver.from_conn_string(str(p)) as s:
        yield s


def thread_config(thread_id: str, *, user_id: str | None = None) -> dict[str, Any]:
    cfg: dict[str, Any] = {"configurable": {"thread_id": thread_id}}
    if user_id:
        cfg["configurable"]["user_id"] = user_id
    return cfg


def state_of(app: Any, thread_id: str) -> dict[str, Any]:
    """读回某个 thread 的当前状态（续跑测试拿它断言"节点没重跑"）。"""
    snap = app.get_state(thread_config(thread_id))
    return dict(snap.values or {})


def history_of(app: Any, thread_id: str) -> list[dict[str, Any]]:
    return [{"next": tuple(s.next or ()), "checkpoint_id": (s.config or {})
             .get("configurable", {}).get("checkpoint_id")}
            for s in app.get_state_history(thread_config(thread_id))]
