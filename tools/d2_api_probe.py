"""D2 step-0 API probe. Prints REAL import paths and REAL framework behaviour.

Nothing in the D2 implementation may rely on a remembered signature that this
script did not print. Run: uv run python -X utf8 tools/d2_api_probe.py
"""
from __future__ import annotations

import importlib
import inspect
import sys


def line(k: str, v: object) -> None:
    print(f"{k:52s} {v}")


def probe_module(mod: str, *names: str) -> None:
    try:
        m = importlib.import_module(mod)
    except Exception as exc:  # noqa: BLE001
        line(f"{mod}.{names}" if names else mod, f"IMPORT-FAIL {type(exc).__name__}: {exc}")
        return
    for n in names:
        if "." in n:  # attribute chain
            obj = m
            ok = True
            for part in n.split("."):
                if hasattr(obj, part):
                    obj = getattr(obj, part)
                else:
                    ok = False
                    break
            line(f"{mod}:{n}", "OK" if ok else "MISSING")
        else:
            line(f"{mod}:{n}", "OK" if hasattr(m, n) else "MISSING")


print("=" * 78)
print("1) VERSIONS")
print("=" * 78)
line("python", sys.version.split()[0])
for pkg in ("langgraph", "langgraph.checkpoint", "langgraph.checkpoint.sqlite",
            "langgraph.prebuilt", "langchain", "langchain_core", "langchain_openai"):
    try:
        m = importlib.import_module(pkg)
        ver = getattr(m, "__version__", None)
        if ver is None:
            from importlib.metadata import version

            ver = version(pkg.replace("_", "-"))
        line(pkg, ver)
    except Exception as exc:  # noqa: BLE001
        line(pkg, f"FAIL {type(exc).__name__}: {exc}")

print()
print("=" * 78)
print("2) IMPORT PATHS (what actually resolves in THIS install)")
print("=" * 78)
probe_module("langgraph.graph", "StateGraph", "START", "END", "message.add_messages")
probe_module("langgraph.graph.state", "StateGraph.compile", "CompiledStateGraph")
probe_module("langgraph.types", "interrupt", "Command", "RetryPolicy")
probe_module("langgraph.errors", "InvalidUpdateError", "GraphInterrupt", "NodeInterrupt")
probe_module("langgraph.prebuilt", "ToolNode", "tools_condition")
probe_module("langgraph.checkpoint.sqlite", "SqliteSaver")
probe_module("langchain.agents", "create_agent")
probe_module("langchain.agents.middleware", "HumanInTheLoopMiddleware", "before_model",
             "after_model", "wrap_tool_call", "SummarizationMiddleware",
             "ModelCallLimitMiddleware", "ToolCallLimitMiddleware", "TodoListMiddleware")
probe_module("langchain_core.tools", "tool", "StructuredTool", "BaseTool")
probe_module("langchain_openai", "ChatOpenAI")
probe_module("langchain_core.messages", "AIMessage", "ToolMessage", "HumanMessage", "SystemMessage")

print()
print("=" * 78)
print("3) create_agent SIGNATURE (real params, in real order)")
print("=" * 78)
try:
    from langchain.agents import create_agent

    sig = inspect.signature(create_agent)
    for pname, p in sig.parameters.items():
        ann = p.annotation
        ann_s = getattr(ann, "__name__", str(ann)).replace("typing.", "")
        default = "" if p.default is inspect._empty else f" = ...{type(p.default).__name__}"
        print(f"  {pname}: {ann_s}{default}")
    doc = (inspect.getdoc(create_agent) or "").splitlines()
    print("  --- doc first lines ---")
    for d in doc[:8]:
        print("   ", d)
except Exception as exc:  # noqa: BLE001
    line("create_agent signature", f"FAIL {type(exc).__name__}: {exc}")

print()
print("=" * 78)
print("4) interrupt / Command availability")
print("=" * 78)
try:
    from langgraph.types import Command, interrupt

    line("interrupt signature", str(inspect.signature(interrupt)))
    line("Command fields", sorted(getattr(Command, "__dataclass_fields__", {}) or
                                  [a for a in dir(Command) if not a.startswith("_")])[:12])
except Exception as exc:  # noqa: BLE001
    line("interrupt/Command", f"FAIL {type(exc).__name__}: {exc}")

print()
print("=" * 78)
print("5) BEHAVIOUR TEST: two nodes in ONE superstep writing the SAME key")
print("   (no reducer) -> silent last-write-wins, or raise?")
print("=" * 78)

import operator  # noqa: E402
import time  # noqa: E402
from typing import Annotated, TypedDict  # noqa: E402

from langgraph.graph import END, START, StateGraph  # noqa: E402


class SharedKey(TypedDict, total=False):
    handoff: dict          # <-- the bug shape: both branches write this
    notes: Annotated[list, operator.add]


def _mk(name: str, delay_ms: int):
    def node(state: SharedKey) -> dict:
        time.sleep(delay_ms / 1000.0)
        return {"handoff": {"from": name, "step": 1 if name == "a" else 2},
                "notes": [f"{name} ran"]}
    return node


g = StateGraph(SharedKey)
g.add_node("a", _mk("a", 0))
g.add_node("b", _mk("b", 5))
g.add_edge(START, "a")
g.add_edge(START, "b")
g.add_edge("a", END)
g.add_edge("b", END)
try:
    out = g.compile().invoke({})
    line("SAME-KEY concurrent write result", f"NO RAISE -> {out}")
except Exception as exc:  # noqa: BLE001
    line("SAME-KEY concurrent write result", f"RAISED {type(exc).__name__}: {str(exc)[:300]}")

print()
print("=" * 78)
print("6) BEHAVIOUR TEST: separate keys + reducer on notes")
print("=" * 78)


class SepKeys(TypedDict, total=False):
    a_out: dict
    b_out: dict
    notes: Annotated[list, operator.add]


def _mk2(name: str, delay_ms: int):
    def node(state: SepKeys) -> dict:
        time.sleep(delay_ms / 1000.0)
        return {f"{name}_out": {"from": name, "step": 1 if name == "a" else 2},
                "notes": [f"{name} ran"]}
    return node


g2 = StateGraph(SepKeys)
g2.add_node("a", _mk2("a", 0))
g2.add_node("b", _mk2("b", 5))
g2.add_edge(START, "a")
g2.add_edge(START, "b")
g2.add_edge("a", END)
g2.add_edge("b", END)
line("SEPARATE-KEY result", g2.compile().invoke({}))

print()
print("=" * 78)
print("7) SqliteSaver real constructor")
print("=" * 78)
try:
    from langgraph.checkpoint.sqlite import SqliteSaver

    print("  from_conn_string:", inspect.signature(SqliteSaver.from_conn_string))
    print("  __init__:", inspect.signature(SqliteSaver.__init__))
    print("  methods:", [m for m in dir(SqliteSaver)
                         if m in ("get_tuple", "put", "put_writes", "list", "delete_thread")])
except Exception as exc:  # noqa: BLE001
    line("SqliteSaver", f"FAIL {type(exc).__name__}: {exc}")

print()
print("=" * 78)
print("8) ToolNode real signature")
print("=" * 78)
try:
    from langgraph.prebuilt import ToolNode

    print("  ToolNode:", str(inspect.signature(ToolNode.__init__))[:300])
    from langgraph.prebuilt import tools_condition

    print("  tools_condition:", str(inspect.signature(tools_condition)))
except Exception as exc:  # noqa: BLE001
    line("ToolNode", f"FAIL {type(exc).__name__}: {exc}")
