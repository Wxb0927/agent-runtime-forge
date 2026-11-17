"""D2 probe #2: which shared-state shapes fail SILENTLY, and the middleware shapes.

The company bug was a silent overwrite of `from`/`step` in a shared upstream
handoff. Probe #1 proved that returning the same channel key is LOUD
(InvalidUpdateError). So before writing the race test I must find which shapes
are silent -- in-place mutation of a shared object vs a real scratch file.
Run: uv run python -X utf8 tools/d2_probe2.py
"""
from __future__ import annotations

import inspect
import json
import operator
import tempfile
import time
from pathlib import Path
from typing import Annotated, Any, TypedDict

from langgraph.graph import END, START, StateGraph

TMP = Path(tempfile.mkdtemp(prefix="forge-probe2-"))


def show(t: str) -> None:
    print()
    print("=" * 76)
    print(t)
    print("=" * 76)


# ---------------------------------------------------------------- CASE A
show("CASE A: two parallel nodes MUTATE IN PLACE a dict held in one channel")


class SharedObj(TypedDict, total=False):
    handoff: dict
    notes: Annotated[list, operator.add]


def node_a(state: SharedObj) -> dict:
    time.sleep(0.00)
    state["handoff"]["from"] = "task_table"       # in-place, no channel write
    state["handoff"]["step"] = 1
    return {"notes": ["a"]}


def node_b(state: SharedObj) -> dict:
    time.sleep(0.004)
    state["handoff"]["from"] = "milestone_table"
    state["handoff"]["step"] = 2
    return {"notes": ["b"]}


g = StateGraph(SharedObj)
g.add_node("a", node_a)
g.add_node("b", node_b)
g.add_edge(START, "a")
g.add_edge(START, "b")
g.add_edge("a", END)
g.add_edge("b", END)
try:
    out = g.compile().invoke({"handoff": {}})
    print("  result          :", out)
    print("  SILENT OVERWRITE?", out.get("handoff"))
except Exception as exc:  # noqa: BLE001
    print(f"  RAISED {type(exc).__name__}: {str(exc)[:200]}")

# ---------------------------------------------------------------- CASE B
show("CASE B: two parallel nodes write the SAME SCRATCH FILE (company's shape)")

HANDOFF = TMP / "upstream_handoff.json"


def write_from_skill(skill: str, step: int, delay: float) -> None:
    time.sleep(delay)
    doc = json.loads(HANDOFF.read_text(encoding="utf-8"))
    doc["from"] = skill
    doc["step"] = step
    doc.setdefault("written_by", []).append(skill)
    HANDOFF.write_text(json.dumps(doc, ensure_ascii=False), encoding="utf-8")


def f_a(state: SharedObj) -> dict:
    write_from_skill("task_table", 1, 0.00)
    return {"notes": ["a"]}


def f_b(state: SharedObj) -> dict:
    write_from_skill("milestone_table", 2, 0.004)
    return {"notes": ["b"]}


HANDOFF.write_text("{}", encoding="utf-8")
g2 = StateGraph(SharedObj)
g2.add_node("a", f_a)
g2.add_node("b", f_b)
g2.add_edge(START, "a")
g2.add_edge(START, "b")
g2.add_edge("a", END)
g2.add_edge("b", END)
g2.compile().invoke({})
print("  handoff file    :", HANDOFF.read_text(encoding="utf-8"))
print("  -> markers surviving both branches? (expect NO = silent overwrite)")

# ---------------------------------------------------------------- CASE C
show("CASE C: same scratch file, per-branch NAMESPACED keys (the fix shape)")

HANDOFF_C = TMP / "upstream_handoff_c.json"
HANDOFF_C.write_text("{}", encoding="utf-8")


def ns_a(state: SharedObj) -> dict:
    time.sleep(0.00)
    doc = json.loads(HANDOFF_C.read_text(encoding="utf-8"))
    doc["stats"] = {"from": "task_table", "step": 1}
    HANDOFF_C.write_text(json.dumps(doc, ensure_ascii=False), encoding="utf-8")
    return {"stats": {"from": "task_table", "step": 1}}


def ns_b(state: SharedObj) -> dict:
    time.sleep(0.004)
    doc = json.loads(HANDOFF_C.read_text(encoding="utf-8"))
    doc["deviation"] = {"from": "milestone_table", "step": 2}
    HANDOFF_C.write_text(json.dumps(doc, ensure_ascii=False), encoding="utf-8")
    return {"deviation": {"from": "milestone_table", "step": 2}}


class SepState(TypedDict, total=False):
    stats: dict
    deviation: dict
    notes: Annotated[list, operator.add]


g3 = StateGraph(SepState)
g3.add_node("a", ns_a)
g3.add_node("b", ns_b)
g3.add_edge(START, "a")
g3.add_edge(START, "b")
g3.add_edge("a", END)
g3.add_edge("b", END)
out_c = g3.compile().invoke({})
print("  state           :", out_c)
print("  file            :", HANDOFF_C.read_text(encoding="utf-8"))

# ---------------------------------------------------------------- CASE D
show("CASE D: interrupt() observed how? (__interrupt__ key?)")


class IState(TypedDict, total=False):
    x: int


def needs_approval(state: IState) -> dict:
    from langgraph.types import interrupt  # noqa: F811

    decision = interrupt({"kind": "approval", "payload": {"x": state.get("x")}})
    return {"x": 999}  # type: ignore[returned-temp]


g4 = StateGraph(IState)
g4.add_node("n", needs_approval)
g4.add_edge(START, "n")
g4.add_edge("n", END)
from langgraph.checkpoint.memory import MemorySaver  # noqa: E402

app = g4.compile(checkpointer=MemorySaver())
cfg = {"configurable": {"thread_id": "t1"}}
r1 = app.invoke({"x": 1}, cfg)
print("  first invoke    :", r1)
print("  keys seen       :", sorted(r1.keys()))
from langgraph.types import Command  # noqa: E402

r2 = app.invoke(Command(resume={"decision": "approve"}), cfg)
print("  after resume    :", r2)
hist = list(app.get_state_history(cfg))
print("  history len     :", len(hist), "| next:", [h.next for h in hist[:3]])

# ---------------------------------------------------------------- CASE E
show("CASE E: middleware constructor signatures (real params)")
try:
    from langchain.agents.middleware import (
        HumanInTheLoopMiddleware,
        ModelCallLimitMiddleware,
        ToolCallLimitMiddleware,
    )

    print("  HITL        :", str(inspect.signature(HumanInTheLoopMiddleware.__init__))[:400])
    print("  ModelCallLmt:", str(inspect.signature(ModelCallLimitMiddleware.__init__))[:300])
    print("  ToolCallLmt :", str(inspect.signature(ToolCallLimitMiddleware.__init__))[:300])
    doc = inspect.getdoc(HumanInTheLoopMiddleware) or ""
    for l in doc.splitlines():
        if any(k in l.lower() for k in ("approve", "edit", "reject", "decision", "interrupt_on")):
            print("   doc>", l.strip()[:160])
except Exception as exc:  # noqa: BLE001
    print(f"  FAIL {type(exc).__name__}: {exc}")

# ---------------------------------------------------------------- CASE F
show("CASE F: @tool with args_schema -- what the model actually sees")
try:
    from langchain_core.tools import tool

    class Inj(TypedDict):
        project: Annotated[str, "project code"]
        as_of: Annotated[str, "YYYY-MM-DD"]

    @tool(args_schema=Inj)
    def progress_stats(project: str, as_of: str) -> dict:
        """Progress statistics for one project."""
        return {"ok": True}

    print("  name        :", progress_stats.name)
    print("  description :", progress_stats.description)
    print("  args        :", json.dumps(progress_stats.args, ensure_ascii=False)[:400])
except Exception as exc:  # noqa: BLE001
    print(f"  FAIL {type(exc).__name__}: {exc}")

print()
print("scratch dir:", TMP)
