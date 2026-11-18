"""Line B：公司范式在 LangGraph 原语上重画（宿主驱动：框架出计划，一趟跑完，⛔ 环）。

    START → router → guard → {stats ∥ deviation ∥ cost} → fanin → aggregate → END
                              ↘（点名越权）→ aggregate(拒答)

这个文件同时是本天竞态靶子的现场：

- **默认形状**：三条分支各写自己的 state 通道，只有 ``fanin`` 单点写外部交接文件，
  且按分支命名空间写 ⇒ ``from``/``step`` 不可能互相覆盖。
- **事故形状**（``handoff_mode="shared"``，仅供复现与变异测试）：分支各自读-改-写
  同一份交接文件的同一组 ``from``/``step`` ⇒ 静默覆盖，数据对、来源标签漂。
- ⚠️ 并发本身也是断言，不是假设：``interleave_hook`` 用 ``threading.Barrier`` 要求
  两路**必须同时到达**，跑成串行会直接超时失败（那说明拓扑没并行，测出来的绿是假的）。
"""
from __future__ import annotations

import threading
import time
from pathlib import Path
from typing import Any, Callable, Optional

from langgraph.graph import END, START, StateGraph

from forge.graph import compute, contract, router as router_mod
from forge.graph.guard import decide
from forge.graph.handoff import NamespacedHandoff, SharedHandoff
from forge.graph.state import BRANCH_SOURCE, ForgeState

HandoffMode = str  # "namespaced" | "shared"
Hook = Optional[Callable[[str], None]]

BRANCH_STEP = {"stats": 1, "deviation": 2, "cost": 3}


def barrier_hook(n: int, timeout: float = 3.0) -> Hook:
    """让 n 路必须交错；串行执行会抛 BrokenBarrierError ⇒ 测试直接红，⛔ 假绿。"""
    bar = threading.Barrier(n)

    def _hook(branch: str) -> None:
        bar.wait(timeout)

    return _hook


def _delay(branch: str) -> None:
    # 确定性微延迟：只为把写入交错开，⛔ 依赖随机 sleep 碰运气
    time.sleep({"stats": 0.0, "deviation": 0.004, "cost": 0.002}[branch])


def build_plan_graph(*, handoff_mode: HandoffMode = "namespaced",
                     scratch_dir: Path | str = "data/handoff",
                     interleave_hook: Hook = None,
                     shared_branches: tuple[str, ...] = ("stats", "deviation")):
    def n_router(state: ForgeState) -> dict[str, Any]:
        ds = compute.dataset_for(int(state.get("seed", 42)))
        plan = router_mod.route(state.get("query", ""), ds)
        return {"route": plan, "steps_seen": ["router"],
                "notes": [f"[router] skills={plan['skills']} matched={plan['matched']}"]}

    def n_guard(state: ForgeState) -> dict[str, Any]:
        ds = compute.dataset_for(int(state.get("seed", 42)))
        scope = decide(ds, state.get("user_key", ""), (state.get("route") or {}).get("projects") or [])
        return {"scope": scope, "steps_seen": ["guard"],
                "notes": [f"[guard] tier={scope['tier']} effective={scope['effective_tier']} {scope['reason']}"]}

    def route_after_guard(state: ForgeState) -> list[str]:
        if (state.get("scope") or {}).get("named_outside"):
            return ["aggregate"]  # 越权短路：⛔ 不带任何数据进分支
        skills = (state.get("route") or {}).get("skills") or []
        return [s for s in ("stats", "deviation", "cost") if s in skills] or ["stats", "deviation", "cost"]

    def _branch(branch: str):
        def node(state: ForgeState) -> dict[str, Any]:
            scope = state.get("scope") or {}
            named = (state.get("route") or {}).get("projects") or []
            visible = named or scope.get("visible_projects") or []
            as_of = state.get("as_of") or compute.parse_as_of(None).isoformat()
            out = compute.run_branch(branch, int(state.get("seed", 42)), visible, as_of)
            _delay(branch)
            if handoff_mode == "shared" and branch in shared_branches:
                # ← 事故形状：读-改-写同一份文件的同一组字段
                store = SharedHandoff(Path(scratch_dir) / f"{state.get('thread_id', 'default')}.json",
                                      interleave_hook)
                store.update_branch(branch, BRANCH_SOURCE[branch], BRANCH_STEP[branch],
                                    out["row_counts"])
            payload = {"from_": BRANCH_SOURCE[branch], "step": BRANCH_STEP[branch],
                       "project": ",".join(named) if named else "*",
                       "metrics": [{**m, "branch": branch} for m in out["metrics"]],
                       "row_counts": out["row_counts"]}
            # 各写自己的通道；note 也各写各的 ⇒ 三条分支在**同一个 superstep** 里
            # 共写 notes，靠 Annotated[list, operator.add] 合并（M5 的靶子就在这）
            return {branch: payload, "steps_seen": [branch],
                    "notes": [f"[{branch}] from={payload['from_']} step={payload['step']}"]}
        return node

    def n_fanin(state: ForgeState) -> dict[str, Any]:
        """单写者：分支不碰外部状态，合并只在这里发生一次。

        ⚠️ ``shared`` 模式（事故形状）下**故意不做命名空间写回**，而是像当年那两下游
        技能一样**从文件回读** ``from``/``step`` 当归因依据 ⇒ 漂掉的来源标签一路带进答案。
        否则 fan-in 会顺手把事故现场修好，M1b 变异永远转不了红（= 假能失败）。
        """
        branches = {b: state[b] for b in ("stats", "deviation", "cost") if state.get(b)}
        path = Path(scratch_dir) / f"{state.get('thread_id', 'default')}.json"
        digest = [m for b in sorted(branches) for m in branches[b]["metrics"]]

        if handoff_mode == "shared":
            leaked = SharedHandoff(path).snapshot()
            merged_fb: dict[str, Any] = {
                b: {"from": leaked.get("from", branches[b]["from_"]),
                    "step": leaked.get("step", branches[b]["step"]),
                    "row_counts": branches[b]["row_counts"]}
                for b in branches}
            merged_fb["branches"] = sorted(branches)
            return {"handoff": merged_fb, "digest": digest, "steps_seen": ["fanin"]}

        merged: dict[str, Any] = {}
        for name, payload in branches.items():
            merged[name] = {"from": payload["from_"], "step": payload["step"],
                            "row_counts": payload["row_counts"]}
        store = NamespacedHandoff(path)
        handoff = store.write_merged(merged)
        handoff["branches"] = sorted(branches)
        return {"handoff": handoff, "digest": digest, "steps_seen": ["fanin"]}

    def n_aggregate(state: ForgeState) -> dict[str, Any]:
        scope = state.get("scope") or {}
        as_of = state.get("as_of") or compute.parse_as_of(None).isoformat()
        outside = scope.get("named_outside") or []
        if outside:
            answer = (f"【范围】{scope.get('effective_tier', 'member')}｜基准日 {as_of}\n"
                      f"【拒答】项目 {', '.join(outside)} 不在当前可见范围内，未取数。")
            env = contract.envelope(ok=False, code="NOT_IN_SCOPE",
                                    message="越权点名，已拒绝且未取数",
                                    digest=[], notes=list(state.get("notes") or []), answer=answer)
            return {"answer": answer, "envelope": env, "steps_seen": ["aggregate"]}

        digest = list(state.get("digest") or [])
        visible = (state.get("route") or {}).get("projects") or scope.get("visible_projects") or []
        answer = contract.render_answer(scope=scope, digest=digest, as_of=as_of, projects=visible)
        if digest:
            # ⚠️ 归因**读 handoff**（= 下游消费者的视角），⛔ 直接读分支通道：
            #     事故形状下漂掉的来源标签必须能被看见，否则这条链路等于没测。
            hand = state.get("handoff") or {}
            parts = []
            for b in ("stats", "deviation", "cost"):
                if not state.get(b):
                    continue
                src = hand.get(b) or {}
                parts.append(f"{b}←{src.get('from', state[b]['from_'])}"
                             f"(第 {src.get('step', state[b]['step'])} 步)")
            answer += "\n【来源】" + "、".join(parts)
        contract.assert_no_banned(answer)
        env = contract.envelope(ok=True, code="OK", message="", digest=digest,
                                notes=list(state.get("notes") or []), answer=answer)
        return {"answer": answer, "envelope": env, "steps_seen": ["aggregate"]}

    g = StateGraph(ForgeState)
    g.add_node("router", n_router)
    g.add_node("guard", n_guard)
    for b in ("stats", "deviation", "cost"):
        g.add_node(b, _branch(b))
    g.add_node("fanin", n_fanin)
    g.add_node("aggregate", n_aggregate)

    g.add_edge(START, "router")
    g.add_edge("router", "guard")
    g.add_conditional_edges("guard", route_after_guard, ["stats", "deviation", "cost", "aggregate"])
    for b in ("stats", "deviation", "cost"):
        g.add_edge(b, "fanin")
    g.add_edge("fanin", "aggregate")
    g.add_edge("aggregate", END)
    return g


PLAN_GRAPH = build_plan_graph().compile()
