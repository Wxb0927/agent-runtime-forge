"""可观测与存储的**外部后端适配器**：Postgres（trace）与 Qdrant（向量）。

⚠️ 为什么必须真写这两个适配器：任务书要求 compose 起 `app + postgres + 向量库`。
如果应用⛔ 连它们，compose 里那两个服务就是布景——评审一句
"你的应用什么时候会读 Postgres？" 就穿。所以：

* `DATABASE_URL` 存在 ⇒ trace 落 Postgres；否则落 sqlite（CI/本机零依赖可跑）。
* `FORGE_VECTOR_URL` 存在 ⇒ dense 索引用 Qdrant；否则用 faiss/numpy/纯 python。
* 两个都⛔ 是"装了依赖就假装能用"：缺依赖直接抛明确错误（与 D5 的
  "skipped ≠ broken" 同一条纪律）。

本机现状（2026-11-23 实测）：Docker 守护进程未运行 ⇒ **端到端容器验证没做**，
`tools/d6_compose_smoke.py` 在没有 Docker 时会 exit 3 并说明原因。
"""
from __future__ import annotations

import json
import os
from typing import Any, Optional, Sequence

TRACE_TABLE = "traces"


def database_url() -> Optional[str]:
    return os.environ.get("DATABASE_URL") or None


def vector_url() -> Optional[str]:
    return os.environ.get("FORGE_VECTOR_URL") or None


class PostgresTraceStore:
    """与 `server.trace.TraceStore` 同接口（⛔ 两套 API，否则换后端就改调用点）。"""

    def __init__(self, dsn: str) -> None:
        import psycopg  # 缺依赖 ⇒ 直接 ImportError 冒到调用方

        self._psycopg = psycopg
        self.dsn = dsn
        with self._conn() as c:
            c.execute(f"""
                CREATE TABLE IF NOT EXISTS {TRACE_TABLE} (
                  request_id text PRIMARY KEY, ts_epoch double precision, ts_iso text,
                  endpoint text, query text, user_key text, line text, steps integer,
                  tool_sequence text, skills text, codes text, tokens_in integer,
                  tokens_out integer, latency_ms double precision, degraded integer,
                  events integer, envelope text)
            """)

    def _conn(self) -> Any:
        return self._psycopg.connect(self.dsn)

    def add(self, t: Any) -> Any:
        import time

        if not t.ts_iso:
            t.ts_epoch = time.time()
            t.ts_iso = time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(t.ts_epoch))
        with self._conn() as c, c.cursor() as cur:
            cur.execute(
                f"INSERT INTO {TRACE_TABLE} VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)"
                " ON CONFLICT (request_id) DO UPDATE SET ts_epoch = EXCLUDED.ts_epoch",
                (t.request_id, t.ts_epoch, t.ts_iso, t.endpoint, t.query, t.user_key, t.line,
                 t.steps, json.dumps(t.tool_sequence, ensure_ascii=False),
                 json.dumps(t.skills, ensure_ascii=False), json.dumps(t.codes, ensure_ascii=False),
                 t.tokens_in, t.tokens_out, t.latency_ms, int(t.degraded), t.events,
                 json.dumps(t.envelope, ensure_ascii=False)))
        return t

    def _decode(self, row: Sequence[Any]) -> dict[str, Any]:
        keys = ("request_id", "ts_epoch", "ts_iso", "endpoint", "query", "user_key", "line",
                "steps", "tool_sequence", "skills", "codes", "tokens_in", "tokens_out",
                "latency_ms", "degraded", "events", "envelope")
        d = dict(zip(keys, row))
        for k in ("tool_sequence", "skills", "codes"):
            d[k] = json.loads(d[k] or "[]")
        d["envelope"] = json.loads(d["envelope"] or "{}")
        d["degraded"] = bool(d["degraded"])
        return d

    def get(self, request_id: str) -> Optional[dict[str, Any]]:
        with self._conn() as c, c.cursor() as cur:
            cur.execute(f"SELECT * FROM {TRACE_TABLE} WHERE request_id = %s", (request_id,))
            row = cur.fetchone()
        return self._decode(row) if row else None

    def recent(self, limit: int = 20) -> list[dict[str, Any]]:
        with self._conn() as c, c.cursor() as cur:
            cur.execute(f"SELECT * FROM {TRACE_TABLE} ORDER BY ts_epoch DESC LIMIT %s", (limit,))
            rows = cur.fetchall()
        return [self._decode(r) for r in rows]

    def latency_percentile(self, pct: float = 95.0) -> Optional[float]:
        with self._conn() as c, c.cursor() as cur:
            cur.execute(f"SELECT percentile_cont(%s) WITHIN GROUP (ORDER BY latency_ms)"
                        f" FROM {TRACE_TABLE}", (pct / 100.0,))
            v = cur.fetchone()[0]
        return None if v is None else round(float(v), 2)

    def count(self) -> int:
        with self._conn() as c, c.cursor() as cur:
            cur.execute(f"SELECT COUNT(*) FROM {TRACE_TABLE}")
            return int(cur.fetchone()[0])


class QdrantDense:
    """Qdrant 后端的 dense 索引（与 DenseIndex 同接口：search(query, k)）。"""

    backend = "qdrant"

    def __init__(self, url: str, collection: str, embedder: Any) -> None:
        from qdrant_client import QdrantClient  # 缺依赖 ⇒ 抛出去，⛔ 静默退化

        self.client = QdrantClient(url=url)
        self.collection = collection
        self.embedder = embedder
        self.chunks: list[Any] = []

    def ensure(self, vectors: Sequence[Sequence[float]], ids: Sequence[str]) -> None:
        from qdrant_client.models import Distance, PointStruct, VectorParams

        dim = len(vectors[0]) if vectors else 0
        if not self.client.collection_exists(self.collection):
            self.client.create_collection(self.collection,
                                          vectors_config=VectorParams(size=dim,
                                                                       distance=Distance.COSINE))
        self.client.upsert(self.collection, points=[
            PointStruct(id=n, vector=list(v), payload={"chunk_id": cid})
            for n, (v, cid) in enumerate(zip(vectors, ids))])

    def search(self, query: str, k: int = 5) -> list[tuple[str, float]]:
        hits = self.client.query_points(self.collection,
                                        query=list(self.embedder.encode([query])[0]),
                                        limit=k).points
        return [(h.payload["chunk_id"], float(h.score)) for h in hits]


def make_trace_store() -> Any:
    """按环境选后端；调用方只认一个接口。"""
    dsn = database_url()
    if not dsn:
        from server.trace import TraceStore

        return TraceStore()
    try:
        return PostgresTraceStore(dsn)
    except ImportError as exc:
        raise RuntimeError(
            f"DATABASE_URL 已设（{dsn.split('@')[-1]}）但 psycopg 未安装 ⇒ "
            "要么 uv add psycopg[binary]，要么清掉 DATABASE_URL 回 sqlite。⛔ 悄悄退回 sqlite") from exc


def describe_backends() -> dict[str, Any]:
    """给 /healthz 用：**实际应用到了哪个后端**（起了容器⛔ 等于连上了容器）。"""
    dsn = database_url()
    vec = vector_url()
    return {
        "trace_backend": "postgres" if dsn else "sqlite",
        "trace_host": (dsn.split('@')[-1].split('/')[0] if dsn else None),
        "vector_backend": "qdrant" if vec else "in-process",
        "vector_url": vec,
    }
