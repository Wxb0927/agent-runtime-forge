"""D6 服务化的编排层：事件流 + 下游故障注入 + 重试 + **如实降级**。

⚠️ 三条不许妥协的地方：

1. **降级⛔ 是"少一段也没关系"**。某路下游取数失败时，答案里必须点名
   `【降级】stats：下游取数失败（重试 2 次后放弃），本次未计入`——
   静默省略等于把"没取到"伪装成"没有这个问题"，与 §3.4 的"无数据 ≠ 0"是同一条纪律。
2. **降级⛔ 是 500 裸栈**。HTTP 侧永远返回合法信封（`ok` 可能为 false，但形状完整），
   因为宿主按字段取值，一个 500 会让上游整个链路失败而看不出原因。
3. **⛔ 另写一套降级用编排**。故障注入走 `plan_graph(branch_hook=…)`，
   生产路径与演练路径是同一条图，否则降级测的是"我以为的图"。

重试用小退避（默认 2 次 / 20ms）而⛔ 无上限：真实项目里"重试到成功为止"
会把一次抖动放大成排队。超限后**如实降级**。
"""
from __future__ import annotations

import asyncio
import os
import time
from dataclasses import dataclass, field
from typing import Any, AsyncIterator, Iterator, Optional

from forge.graph import contract
from forge.graph.compute import AS_OF
from forge.graph.plan_graph import build_plan_graph

BRANCHES = ("stats", "deviation", "cost")
DEGRADE_SECTION = "【降级】"


class DownstreamError(RuntimeError):
    """模拟下游 5xx。⛔ 用它冒泡到 HTTP 层（那正是"裸栈"）。"""


@dataclass
class FaultPlan:
    """哪些分支在几次尝试内一直失败。

    来源优先级：请求头/参数 > 环境变量 `FORGE_FAULTS`（形如 `stats:2,cost:perm`）。
    `perm` 或次数用尽后仍失败 ⇒ 降级；次数用完且恢复 ⇒ 只是慢，⛔ 丢数据。
    """
    spec: dict[str, int] = field(default_factory=dict)      # branch → 还需失败几次（-1=永久）
    attempts: dict[str, int] = field(default_factory=dict)
    outcomes: dict[str, str] = field(default_factory=dict)

    @classmethod
    def parse(cls, raw: Optional[str]) -> "FaultPlan":
        plan = cls()
        for part in (raw or os.environ.get("FORGE_FAULTS", "")).split(","):
            part = part.strip()
            if not part or ":" not in part:
                continue
            name, _, cnt = part.partition(":")
            if name in BRANCHES:
                plan.spec[name] = -1 if cnt.strip().lower() in {"perm", "always", "-1"} else \
                    int(cnt or 1)
        return plan

    def should_fail(self, branch: str) -> bool:
        left = self.spec.get(branch)
        if left is None:
            return False
        return left == -1 or left > 0

    def consume(self, branch: str) -> None:
        if self.spec.get(branch, 0) > 0:
            self.spec[branch] -= 1


def make_hook(plan: FaultPlan, retries: int = 2, backoff: float = 0.02):
    """给 `build_plan_graph(branch_hook=…)` 用的取数钩子。"""
    from forge.graph import compute

    def hook(branch: str, seed: int, projects: list[str], as_of: str) -> dict[str, Any]:
        if not plan.should_fail(branch):
            plan.outcomes.setdefault(branch, "ok")
            return compute.run_branch(branch, seed, projects, as_of)

        last: Optional[Exception] = None
        for attempt in range(retries + 1):
            plan.attempts[branch] = plan.attempts.get(branch, 0) + 1
            try:
                raise DownstreamError(f"{branch} 下游返回 503")
            except DownstreamError as exc:
                last = exc
                if not plan.should_fail(branch):
                    # 瞬时故障：这次恢复，重试有意义
                    plan.consume(branch)
                    time.sleep(backoff * (attempt + 1))
                    plan.outcomes[branch] = f"recovered_after_{attempt + 1}"
                    return compute.run_branch(branch, seed, projects, as_of)
                plan.consume(branch)
                time.sleep(backoff * (attempt + 1))
        plan.outcomes[branch] = f"degraded_after_{plan.attempts.get(branch, 0)}"
        # 返回**空产出**而⛔ 抛异常：抛出去会让整张图挂掉，降级必须在分支内隔离
        # （这也是原实现"失败隔离不当 0 计入"的同一条设计）
        return {"branch": branch, "metrics": [], "row_counts": {"downstream_error": 1},
                "as_of": as_of, "fault": "downstream_5xx"}

    return hook


def run_once(query: str, *, user_key: str = "u0000", as_of: Optional[str] = None,
             seed: int = 42, thread_id: Optional[str] = None,
             faults: Optional[FaultPlan] = None, retries: int = 2) -> dict[str, Any]:
    """跑一次端到端，返回 {envelope, steps, tool_sequence, skills, degraded, fault_log, events}。"""
    plan = faults or FaultPlan.parse(None)
    started = time.perf_counter()
    state = {"query": query, "user_key": user_key, "seed": seed,
             "as_of": as_of or AS_OF.isoformat(),
             "thread_id": thread_id or f"srv-{int(started * 1000)}"}
    events: list[dict[str, Any]] = []
    tool_sequence: list[str] = []
    cfg = {"configurable": {"thread_id": state["thread_id"]}}
    # ⚠️ 只跑**一遍**：早期版本 stream 完又 invoke 一次，等于把图跑两遍 ⇒
    #    故障尝试次数翻倍、延迟虚高一倍，P95 就成了假数。终态从 checkpoint 读。
    from langgraph.checkpoint.memory import MemorySaver

    app = build_plan_graph(scratch_dir="data/server-handoff",
                           branch_hook=make_hook(plan, retries=retries)).compile(
                               checkpointer=MemorySaver())
    for chunk in app.stream(state, cfg, stream_mode="updates"):
        for node, update in chunk.items():
            tool_sequence.append(node)
            events.append(_node_event(node, update))
    out = dict(app.get_state(cfg).values or {})
    env = dict(out.get("envelope") or {})
    degraded = [b for b, o in plan.outcomes.items() if str(o).startswith("degraded")]
    if degraded:
        env = _apply_degradation(env, degraded, plan, state)
    env.setdefault("ok", True)
    return {"envelope": env, "steps": len(tool_sequence), "tool_sequence": tool_sequence,
            "skills": list((out.get("route") or {}).get("skills") or []),
            "degraded": bool(degraded), "fault_log": dict(plan.outcomes),
            "attempts": dict(plan.attempts), "events": events,
            "latency_ms": round((time.perf_counter() - started) * 1000, 2),
            "state": {k: out.get(k) for k in ("scope", "route", "digest")}}


def _apply_degradation(env: dict[str, Any], degraded: list[str], plan: FaultPlan,
                       state: dict[str, Any]) -> dict[str, Any]:
    """把缺口**写进答案与 notes**，并保证信封形状合法。"""
    notes = list(env.get("notes") or [])
    lines = [f"{b}：下游取数失败（尝试 {plan.attempts.get(b, 1)} 次后降级），本次未计入"
             for b in sorted(degraded)]
    notes += [f"DEGRADED:{b}" for b in sorted(degraded)]
    answer = (env.get("answer") or "").rstrip()
    if answer:
        answer += "\n" + DEGRADE_SECTION + "；".join(lines)
    else:
        answer = DEGRADE_SECTION + "；".join(lines)
    still_have = bool(env.get("digest"))
    env = dict(env)
    env["notes"] = notes
    env["answer"] = answer
    env["code"] = "PARTIAL" if still_have else "DEGRADED"
    env["ok"] = still_have          # 全部下游都挂 ⇒ ok=false，但⛔ 500
    env["message"] = env.get("message") or ("部分指标本次未取到" if still_have else "本次未取到任何指标")
    contract.assert_no_banned(answer)
    return env


def _node_event(node: str, update: dict[str, Any]) -> dict[str, Any]:
    ev: dict[str, Any] = {"type": "node", "node": node}
    if node == "router":
        ev["skills"] = list((update.get("route") or {}).get("skills") or [])
    elif node == "guard":
        sc = update.get("scope") or {}
        ev.update(tier=sc.get("tier"), effective=sc.get("effective_tier"),
                  visible=len(sc.get("visible_projects") or []))
    elif node in BRANCHES:
        payload = update.get(node) or {}
        ev.update(metrics=len(payload.get("metrics") or []), source=payload.get("from_"),
                  step=payload.get("step"))
    elif node == "aggregate":
        env = update.get("envelope") or {}
        ev.update(code=env.get("code"), ok=env.get("ok"),
                  digest=len(env.get("digest") or []))
    return ev


def answer_line_events(env: dict[str, Any]) -> list[dict[str, Any]]:
    """Line B 的"成品行"事件流。

    ⚠️ ⛔ 称之为 token 流：宿主驱动这条路里答案由计算层一次算完，
    根本没有逐 token 生成。真 token 流在 Line A（`stream_agent_events`）。
    """
    answer = (env or {}).get("answer") or ""
    return [{"type": "answer_line", "text": ln} for ln in answer.splitlines() if ln.strip()]


async def stream_agent_events(query: str, *, user_key: str = "u0000",
                              as_of: Optional[str] = None, live: bool = False,
                              model: Optional[str] = None) -> AsyncIterator[dict[str, Any]]:
    """引擎侧（Line A / ``create_agent``）的事件流。

    ⚠️ 实测形状（别再猜）：`agent.astream(payload)` 在 langgraph 1.x + create_agent 下
    返回的是**按节点/中间件的更新字典**（键如 `model`、`tools`、
    `ModelCallLimitMiddleware.before_model`），⛔ 是我以为的顶层 `{"messages": [...]}`。
    ⇒ 事件从每个节点的 `update["messages"]` 里取。
    ⇒ 真 token 增量只有 `live=True` 且走 `astream_events` 才有；
      打桩模式下发的是**整条消息块**，`done` 事件里用 `stream_kind` 明说，
      ⛔ 让评审把 stub 的"1 个块"当成"1 个 token"。
    """
    from forge.graph import router as router_mod
    from forge.graph.compute import AS_OF, dataset_for
    from forge.graph.react_agent import build_react_agent
    from forge.model.scripted import ScriptedChatModel

    skills = router_mod.route(query, dataset_for(42))["skills"]
    named = router_mod.named_projects(query, dataset_for(42))
    if live:
        from forge.model.factory import build_live_model

        m = build_live_model(model=model)
    else:
        args = {"user_key": user_key, "as_of": as_of or AS_OF.isoformat()}
        if named:
            args["project"] = named[0]
        m = ScriptedChatModel(script=[{"tool_calls": [(s_, dict(args)) for s_ in skills]},
                                      {"content": "（打桩模式：以下为工具返回值直引，⛔ 当真模型措辞）"}])
    agent, meta = build_react_agent(model=m, skills=skills)
    payload = {"messages": [{"role": "user", "content": query}]}
    yield {"type": "surface", "tools": meta["tools"], "middleware": meta["middleware"],
           "bound_skills": skills}

    text_chunks = 0
    tool_calls = 0
    if live:
        # 真 token 增量：走 astream_events v2 的 on_chat_model_stream
        async for ev in agent.astream_events(payload, version="v2"):
            kind = ev.get("event")
            if kind == "on_chat_model_stream":
                piece = getattr(ev.get("data", {}).get("chunk"), "content", "")
                if piece:
                    text_chunks += 1
                    yield {"type": "token", "text": str(piece)}
            elif kind == "on_tool_start":
                tool_calls += 1
                yield {"type": "tool_call", "name": ev.get("name"),
                       "input": ev.get("data", {}).get("input")}
            elif kind == "on_tool_end":
                out = ev.get("data", {}).get("output")
                yield {"type": "tool_result", "name": ev.get("name"),
                       "bytes": len(str(getattr(out, "content", out)))}
        stream_kind = "token"
    else:
        async for chunk in agent.astream(payload, stream_mode="updates"):
            for node, update in (chunk or {}).items():
                msgs = (update or {}).get("messages") if isinstance(update, dict) else None
                if not msgs:
                    continue
                for msg in msgs:
                    kind = type(msg).__name__
                    if kind == "AIMessage" and getattr(msg, "tool_calls", None):
                        tool_calls += len(msg.tool_calls)
                        yield {"type": "tool_call", "node": node,
                               "calls": [{"name": c["name"], "args": c["args"]}
                                         for c in msg.tool_calls]}
                    elif kind == "ToolMessage":
                        yield {"type": "tool_result", "node": node,
                               "name": getattr(msg, "name", ""),
                               "bytes": len(str(msg.content))}
                    elif kind == "AIMessage" and msg.content:
                        text_chunks += 1
                        yield {"type": "token", "node": node, "text": str(msg.content)}
        stream_kind = "message_chunk(stub)"

    yield {"type": "done", "text_chunks": text_chunks, "tool_calls": tool_calls,
           "stream_kind": stream_kind}
