"""trace 记录与查询（D6）。

⚠️ 为什么单独一张表：生产里"答案不对"从来⛔ 是答案本身的问题，而是
**"这一轮到底走了哪几步、调了哪几路、各花多久"** 看不见。原实现里没有框架原生 trace，
所以评测只能靠离线回放 + 源码门禁（§1.5 D 第 3 句）；这里服务化之后终于能**逐请求**留痕。

存 sqlite 而⛔ Postgres：本仓库的测试与 CI 要能在零服务环境下跑通；
`DATABASE_URL` 存在时由 `server/persistence.py` 换 PG，⛔ 两处各写一套 SQL。
"""
from __future__ import annotations

import json
import sqlite3
import threading
import time
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Optional

DB_PATH = Path("data") / "traces.sqlite3"
_SCHEMA = """
CREATE TABLE IF NOT EXISTS traces (
  request_id   TEXT PRIMARY KEY,
  ts_epoch     REAL NOT NULL,
  ts_iso       TEXT NOT NULL,
  endpoint     TEXT NOT NULL,
  query        TEXT,
  user_key     TEXT,
  line         TEXT,
  steps        INTEGER NOT NULL,
  tool_sequence TEXT NOT NULL,
  skills        TEXT NOT NULL,
  codes         TEXT NOT NULL,
  tokens_in     INTEGER NOT NULL DEFAULT 0,
  tokens_out    INTEGER NOT NULL DEFAULT 0,
  latency_ms    REAL NOT NULL,
  degraded      INTEGER NOT NULL DEFAULT 0,
  events        INTEGER NOT NULL DEFAULT 0,
  envelope      TEXT
)
"""


@dataclass
class Trace:
    endpoint: str
    query: str = ""
    user_key: str = ""
    line: str = "plan"
    steps: int = 0
    tool_sequence: list[str] = field(default_factory=list)
    skills: list[str] = field(default_factory=list)
    codes: list[str] = field(default_factory=list)
    tokens_in: int = 0
    tokens_out: int = 0
    latency_ms: float = 0.0
    degraded: bool = False
    events: int = 0
    envelope: dict[str, Any] = field(default_factory=dict)
    request_id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    ts_iso: str = ""
    ts_epoch: float = 0.0

    def as_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["degraded"] = bool(self.degraded)
        return d


class TraceStore:
    """一个进程一个连接池太贵，这里用短连接 + 线程锁（够用且零依赖）。"""

    def __init__(self, path: Path | str = DB_PATH) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        with self._conn() as c:
            c.executescript(_SCHEMA)

    def _conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, timeout=10)
        conn.row_factory = sqlite3.Row
        return conn

    def add(self, t: Trace) -> Trace:
        if not t.ts_iso:
            t.ts_epoch = time.time()
            t.ts_iso = time.strftime("%Y-%m-%dT%H:%M:%S%z", time.localtime(t.ts_epoch))
        with self._lock, self._conn() as c:
            c.execute(
                "INSERT OR REPLACE INTO traces VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (t.request_id, t.ts_epoch, t.ts_iso, t.endpoint, t.query, t.user_key, t.line,
                 t.steps, json.dumps(t.tool_sequence, ensure_ascii=False),
                 json.dumps(t.skills, ensure_ascii=False), json.dumps(t.codes, ensure_ascii=False),
                 t.tokens_in, t.tokens_out, t.latency_ms, int(t.degraded), t.events,
                 json.dumps(t.envelope, ensure_ascii=False)))
        return t

    def get(self, request_id: str) -> Optional[dict[str, Any]]:
        with self._conn() as c:
            row = c.execute("SELECT * FROM traces WHERE request_id = ?", (request_id,)).fetchone()
        return self._decode(row) if row else None

    def recent(self, limit: int = 20) -> list[dict[str, Any]]:
        with self._conn() as c:
            rows = c.execute("SELECT * FROM traces ORDER BY ts_epoch DESC LIMIT ?",
                             (limit,)).fetchall()
        return [self._decode(r) for r in rows]

    def latency_percentile(self, pct: float = 95.0) -> Optional[float]:
        """真读数用：P95 延迟（毫秒）。样本不足直接返回 None，⛔ 编一个数。"""
        with self._conn() as c:
            rows = [r[0] for r in c.execute("SELECT latency_ms FROM traces ORDER BY latency_ms")]
        if not rows:
            return None
        idx = max(0, min(len(rows) - 1, int(round(pct / 100 * (len(rows) - 1)))))
        return round(rows[idx], 2)

    def count(self) -> int:
        with self._conn() as c:
            return int(c.execute("SELECT COUNT(*) FROM traces").fetchone()[0])

    @staticmethod
    def _decode(row: sqlite3.Row) -> dict[str, Any]:
        d = dict(row)
        for k in ("tool_sequence", "skills", "codes", "envelope"):
            d[k] = json.loads(d[k] or "[]") if k != "envelope" else json.loads(d[k] or "{}")
        d["degraded"] = bool(d["degraded"])
        return d


STORE = TraceStore()
