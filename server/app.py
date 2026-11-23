"""FastAPI 服务：REST + SSE（节点事件流 / token 流）+ trace 查询。

⚠️ HTTP 语义怎么定的（别和稀泥）：

* 下游取数失败 → **200** + 合法信封，`code=PARTIAL|DEGRADED`、notes 点名缺口。
  这是"部分成功"，⛔ 客户端错误。
* 未预期的内部异常 → **503** + **合法信封**（`code=INTERNAL_DEGRADED`），
  ⛔ FastAPI 默认的裸栈 traceback。任务书说"不许 500 裸栈"，
  我的解释是：状态码该诚实（服务端确实坏了），**但响应体必须是可解析的信封**，
  否则上游只能按字符串处理错误、宿主取不到字段。两种做法都写进测试。
"""
from __future__ import annotations

from typing import Any, Optional

from fastapi import Body, FastAPI, Header, Query, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from sse_starlette.sse import EventSourceResponse

from server import pipeline
from server.persistence import describe_backends, make_trace_store
from server.trace import Trace

# DATABASE_URL 存在 ⇒ Postgres；否则 sqlite。**同一接口**，调用点⛔ 分叉。
STORE = make_trace_store()

app = FastAPI(title="agent-runtime-forge server", version="0.6.0")


class AskRequest(BaseModel):
    query: str = Field(description="自然语言问句")
    user_key: str = "u0000"
    as_of: Optional[str] = Field(default=None, description="基准日 YYYY-MM-DD；⛔ 服务端猜今天")
    seed: int = 42
    thread_id: Optional[str] = None
    faults: Optional[str] = Field(default=None,
                                  description='演练下游故障，如 "stats:2" 或 "cost:perm"')
    retries: int = Field(default=2, ge=0, le=5)


def _envelope_response(payload: dict[str, Any], status: int = 200) -> JSONResponse:
    return JSONResponse(payload["envelope"], status_code=status,
                        headers={"X-Request-Id": payload["trace"].request_id})


def _record(payload: dict[str, Any], endpoint: str) -> Trace:
    t = Trace(endpoint=endpoint, query=payload.get("query", ""),
              user_key=payload.get("user_key", ""), line="plan",
              steps=payload["steps"], tool_sequence=payload["tool_sequence"],
              skills=payload["skills"], codes=[str(payload["envelope"].get("code"))],
              latency_ms=payload["latency_ms"], degraded=payload["degraded"],
              events=len(payload["events"]), envelope=payload["envelope"])
    return STORE.add(t)


@app.exception_handler(Exception)
async def unhandled_exc(request: Request, exc: Exception) -> JSONResponse:
    """未预期异常 ⇒ 503 + 合法信封（⛔ 裸栈）。"""
    from forge.graph import contract

    env = contract.envelope(ok=False, code="INTERNAL_DEGRADED",
                            message=f"服务端内部故障：{type(exc).__name__}",
                            notes=[f"EXC:{type(exc).__name__}",
                                   "本次结果不可用，⛔ 按空数据当 0 处理"],
                            answer="【降级】服务端内部故障，本次未取得任何指标。")
    STORE.add(Trace(endpoint=str(request.url.path), degraded=True, codes=["INTERNAL_DEGRADED"],
                    envelope=env))
    return JSONResponse(env, status_code=503)


@app.post("/v1/ask")
def ask(req: AskRequest) -> JSONResponse:
    payload = pipeline.run_once(req.query, user_key=req.user_key, as_of=req.as_of,
                                seed=req.seed, thread_id=req.thread_id,
                                faults=pipeline.FaultPlan.parse(req.faults),
                                retries=req.retries)
    trace_row = _record(payload | {"query": req.query, "user_key": req.user_key}, "/v1/ask")
    return _envelope_response({"envelope": payload["envelope"], "trace": trace_row})


@app.get("/v1/ask/stream")
def ask_stream(query: str, user_key: str = "u0000", as_of: Optional[str] = None,
               faults: Optional[str] = None, retries: int = 2) -> EventSourceResponse:
    """宿主驱动那条路：发**节点事件 + 成品行**。⚠️ 这里⛔ 存在 token 流
    （答案由计算层一次算完），事件名也就⛔ 叫 token，免得骗人。
    """
    def gen():
        payload = pipeline.run_once(query, user_key=user_key, as_of=as_of,
                                    faults=pipeline.FaultPlan.parse(faults), retries=retries)
        for ev in payload["events"]:
            yield {"event": "node", "data": _dumps(ev)}
        for line in pipeline.answer_line_events(payload["envelope"]):
            yield {"event": "answer_line", "data": _dumps(line)}
        row = _record(payload | {"query": query, "user_key": user_key}, "/v1/ask/stream")
        yield {"event": "done", "data": _dumps({"request_id": row.request_id,
                                                "steps": payload["steps"],
                                                "tool_sequence": payload["tool_sequence"],
                                                "degraded": payload["degraded"],
                                                "latency_ms": payload["latency_ms"],
                                                "stream_kind": "node_events+answer_lines"})}
    return EventSourceResponse(gen())


@app.get("/v1/chat/stream")
async def chat_stream(query: str, user_key: str = "u0000", as_of: Optional[str] = None,
                      live: bool = False, model: Optional[str] = None) -> EventSourceResponse:
    """引擎侧那条路（``create_agent``）：真 token 流 + 工具调用事件。
    `live=false` 时是打桩消息块，`done` 事件里会写明 ``stream_kind`` 是哪一种。
    """
    async def gen():
        async for ev in pipeline.stream_agent_events(query, user_key=user_key, as_of=as_of,
                                                     live=live, model=model):
            yield {"event": ev["type"], "data": _dumps(ev)}
    return EventSourceResponse(gen())


@app.get("/v1/trace/stats")
def trace_stats() -> dict[str, Any]:
    """P95 只有**真跑出来**才填进简历；样本不足返回 null 而不是编一个数。"""
    return {"samples": STORE.count(),
            "p50_ms": STORE.latency_percentile(50.0),
            "p95_ms": STORE.latency_percentile(95.0),
            "degraded_share": _degraded_share()}


# ⚠️ 路由顺序：参数化路径必须注册在 /v1/trace/stats **之后**，
#    否则 stats 会被当成 request_id 走进 404 分支（实测踩过：smoke 第一次就命中）。
@app.get("/v1/trace/{request_id}")
def get_trace(request_id: str) -> JSONResponse:
    row = STORE.get(request_id)
    if not row:
        from forge.graph import contract

        return JSONResponse(contract.envelope(
            ok=False, code="NOT_FOUND", message=f"trace {request_id} 不存在",
            answer="【无数据】该 request_id 没有留痕记录"), status_code=404)
    return JSONResponse(row)


@app.get("/v1/traces")
def list_traces(limit: int = Query(default=20, ge=1, le=200)) -> dict[str, Any]:
    return {"count": STORE.count(), "recent": STORE.recent(limit)}


@app.get("/healthz")
def healthz() -> dict[str, Any]:
    return {"ok": True, "traces": STORE.count(), "version": app.version,
            **describe_backends()}   # ⛔ 只报"容器起了"，要报"应用连上了哪个后端"


def _dumps(obj: Any) -> str:
    import json

    return json.dumps(obj, ensure_ascii=False, default=str)


def _degraded_share() -> Optional[float]:
    rows = STORE.recent(500)
    if not rows:
        return None
    return round(sum(1 for r in rows if r["degraded"]) / len(rows), 4)
