# -*- coding: utf-8 -*-
"""D6 服务层测试：REST / SSE / trace / 降级 / 后端选择。

⚠️ 两条断言是**为今天的真实翻车写的**，⛔ 删：
* `test_graph_runs_once`：早期 `run_once` 在 stream 之后又 invoke 一次 ⇒ 图跑两遍、
  故障尝试次数翻倍、延迟虚高一倍（P95 就成假数了）。
* `test_stub_stream_admits_it_is_not_tokens`：打桩模式必须自报
  `stream_kind=message_chunk(stub)`，⛔ 让 SSE 的形状冒充真 token 流。
"""
from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from server import pipeline
from server.app import app
from server.persistence import describe_backends
from server.trace import Trace, TraceStore

client = TestClient(app)


# ------------------------------------------------------------------ REST


def test_healthz_reports_the_backends_it_actually_uses() -> None:
    h = client.get("/healthz").json()
    assert h["ok"] is True
    # ⛔ 只报"服务活着"，要报"连的是哪个后端"（compose 里那两个服务必须被应用真用）
    assert {"trace_backend", "vector_backend"} <= set(h)
    assert h["trace_backend"] in ("sqlite", "postgres")


def test_ask_returns_valid_envelope_and_request_id() -> None:
    r = client.post("/v1/ask", json={"query": "PRJ-1180 的进度、偏差和成本",
                                     "user_key": "u0041"})
    assert r.status_code == 200
    env = r.json()
    assert list(env) == ["ok", "code", "message", "digest", "notes", "answer"]
    assert env["code"] == "OK" and len(env["digest"]) == 10
    assert r.headers.get("x-request-id")


def test_trace_records_node_sequence_and_steps() -> None:
    r = client.post("/v1/ask", json={"query": "PRJ-1180 的完成率", "user_key": "u0041"})
    rid = r.headers["x-request-id"]
    t = client.get(f"/v1/trace/{rid}").json()
    assert t["tool_sequence"][0] == "router" and t["tool_sequence"][-1] == "aggregate"
    # 只问进度 ⇒ 5 个节点（router/guard/stats/fanin/aggregate）。
    # ⚠️ 这是**图节点数**，与简历里"首问 4 步"（工具调用步数）是两个不同口径，
    #    面试说串了就是自相矛盾 ⇒ 这里用断言把语义钉住。
    assert t["steps"] == 5 == len(t["tool_sequence"])
    assert t["degraded"] is False


def test_unknown_trace_id_returns_envelope_not_html() -> None:
    r = client.get("/v1/trace/does-not-exist")
    assert r.status_code == 404
    assert list(r.json()) == ["ok", "code", "message", "digest", "notes", "answer"]
    assert r.json()["code"] == "NOT_FOUND"


def test_stats_route_is_not_shadowed_by_id_route() -> None:
    """路由顺序：/v1/trace/stats ⛔ 被 /v1/trace/{request_id} 吃掉（实测错过一次）。"""
    r = client.get("/v1/trace/stats")
    assert r.status_code == 200
    body = r.json()
    assert {"samples", "p50_ms", "p95_ms"} <= set(body), body


def test_percentile_refuses_to_invent_numbers() -> None:
    """样本为空 ⇒ None，⛔ 返回 0（0 会被读成"极快"，那就是编数）。"""
    from pathlib import Path as _P
    tmp = _P("data") / "_t_percentile.sqlite3"
    if tmp.exists():
        tmp.unlink()
    store = TraceStore(path=tmp)
    assert store.latency_percentile(95.0) is None


# ------------------------------------------------------------------ 降级


def test_degradation_names_the_gap_and_keeps_the_rest() -> None:
    r = client.post("/v1/ask", json={"query": "PRJ-1180 的进度、偏差和成本",
                                     "user_key": "u0041", "faults": "cost:perm"})
    assert r.status_code == 200, "下游故障⛔ 变成 500 裸栈"
    env = r.json()
    assert env["code"] == "PARTIAL" and env["ok"] is True
    assert any(n == "DEGRADED:cost" for n in env["notes"])
    assert "【降级】" in env["answer"] and "cost" in env["answer"].split("【降级】")[1]
    assert any(m["branch"] == "stats" for m in env["digest"]), "其他分支被一起丢了"


def test_all_downstreams_down_says_not_ok() -> None:
    """全挂 ⇒ ok=false，⛔ 仍然说"成功"（这是 §3.4 弃权纪律的服务化版本）。"""
    r = client.post("/v1/ask", json={"query": "PRJ-1180 的成本", "user_key": "u0041",
                                     "faults": "cost:perm"})
    env = r.json()
    assert r.status_code == 200
    assert env["ok"] is False and env["code"] == "DEGRADED"


def test_internal_failure_is_503_with_envelope_body() -> None:
    """未预期异常 ⇒ 503 + 合法信封（诚实的状态码 + 可解析的体，⛔ 裸栈 traceback）。"""
    import server.app as sa

    real = pipeline.run_once
    try:
        def boom(*a, **k):
            raise ZeroDivisionError("演练内部故障")

        sa.pipeline.run_once = boom  # type: ignore[assignment]
        # raise_server_exceptions 是 **TestClient 构造参数**（不是每次请求的参数，
        # 第一版写错了）：设成 False 才让服务端异常处理器接管，而不是把异常抛回测试里
        raw_client = TestClient(app, raise_server_exceptions=False)
        r = raw_client.post("/v1/ask", json={"query": "会不会裸栈", "user_key": "u0000"})
    finally:
        sa.pipeline.run_once = real  # type: ignore[assignment]
    assert r.status_code == 503
    body = r.json()
    assert list(body) == ["ok", "code", "message", "digest", "notes", "answer"]
    assert body["code"] == "INTERNAL_DEGRADED"
    assert "Traceback" not in r.text


# ------------------------------------------------------------------ SSE


def _events(sse_text: str) -> list[str]:
    return sorted({ln.split(":", 1)[1].strip() for ln in sse_text.splitlines()
                   if ln.startswith("event:")})


def test_ask_stream_emits_node_and_done_events() -> None:
    with client.stream("GET", "/v1/ask/stream", params={"query": "PRJ-1180 的完成率"}) as r:
        text = "".join(r.iter_text())
    kinds = _events(text)
    assert {"node", "answer_line", "done"} <= set(kinds), kinds
    assert '"stream_kind": "node_events+answer_lines"' in text, "宿主驱动那条要明说⛔ 是 token 流"


def test_stub_stream_admits_it_is_not_tokens() -> None:
    """诚实标注：打桩模式的流是**整条消息块**，⛔ 冒充 token 流。"""
    with client.stream("GET", "/v1/chat/stream", params={"query": "PRJ-1180 的完成率"}) as r:
        text = "".join(r.iter_text())
    kinds = _events(text)
    assert {"surface", "tool_call", "tool_result", "token", "done"} <= set(kinds), kinds
    assert "message_chunk(stub)" in text


def test_graph_runs_once() -> None:
    """早期版本 stream 完又 invoke ⇒ 图跑两遍。用**故障尝试次数**把它钉住。"""
    plan = pipeline.FaultPlan.parse("cost:perm")
    res = pipeline.run_once("PRJ-1180 的成本", user_key="u0000", faults=plan, retries=2)
    assert res["attempts"]["cost"] == 3, res["attempts"]     # 1 次首发 + 2 次重试，⛔ 6
    # 成本问句只路由 cost ⇒ 节点应为 router,guard,cost,fanin,aggregate = 5
    # 图若被跑两遍，这里会变成 10（就是当年那个 bug 的指纹）
    assert res["steps"] == len(res["events"]) == 5, res["steps"]
    assert res["tool_sequence"].count("router") == 1, res["tool_sequence"]


def test_tool_subset_is_pruned_per_route() -> None:
    """按路由裁剪 bind_tools 子集（主流做法）：只问成本⛔ 绑三个技能。"""
    names = pipeline.run_once("PRJ-1180 的成本", user_key="u0000")["skills"]
    assert names == ["cost"], names


# ------------------------------------------------------------------ 后端选择


def test_backend_selection_is_explicit(monkeypatch) -> None:
    monkeypatch.delenv("DATABASE_URL", raising=False)
    assert describe_backends()["trace_backend"] == "sqlite"
    monkeypatch.setenv("DATABASE_URL", "postgresql://u:p@h:5432/db")
    d = describe_backends()
    assert d["trace_backend"] == "postgres" and d["trace_host"] == "h:5432"


def test_postgres_url_without_driver_fails_loudly(monkeypatch) -> None:
    """设了 DATABASE_URL 却没装 psycopg ⇒ 必须报错，⛔ 悄悄退回 sqlite。"""
    import server.persistence as sp

    monkeypatch.setenv("DATABASE_URL", "postgresql://u:p@h:5432/db")
    real = sp.PostgresTraceStore
    try:
        def boom(dsn: str) -> None:
            raise ImportError("psycopg 未安装")

        sp.PostgresTraceStore = boom  # type: ignore[assignment]
        with pytest.raises(RuntimeError, match="psycopg"):
            sp.make_trace_store()
    finally:
        sp.PostgresTraceStore = real  # type: ignore[misc]
