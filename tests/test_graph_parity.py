"""验收⑥：同一个业务问题在**三条编排**上跑，数字必须逐字相同。

项目二背景段写的是"同一个业务问题两边各实现一遍，用数据而不是偏好来回答该不该自研"。
这句话要能被机器检查，否则就是修辞。这里对账两件事：

1. **正确性一致**：三条线给出的 ``(指标名, 数值)`` 完全相同 ⇒ 编排方式⛔ 影响取数。
   （结构上它们都调 ``compute.run_branch``，这条测试防的是"将来有人在某条线里偷偷再算一遍"。）
2. **代价不一致**：ReAct 要**问两轮模型**才拿到同样的数（框架持有环、模型决定调几次），
   公司范式一趟出计划、⛔ 问模型。这个差值就是"该不该自研"的可量化那半边。

读数会被 README 的对照表直接引用（``dump_for_readme()``）。
"""
from __future__ import annotations

import json
from pathlib import Path

from forge.graph.plan_graph import build_plan_graph
from forge.graph.react_agent import build_react_agent
from forge.graph.react_min import build_react_min
from forge.model.scripted import ScriptedChatModel

PROJECT = "PRJ-1180"
QUERY = f"{PROJECT} 的进度、里程碑偏差和成本怎么样"
ARGS = {"user_key": "u0000", "project": PROJECT, "as_of": "2026-10-30"}
SKILLS = ["stats", "deviation", "cost"]


def _pairs(metrics: list[dict]) -> set[tuple[str, str | None]]:
    return {(m["name"], m["value"]) for m in metrics}


def run_line_b(tmp_path: Path) -> dict:
    app = build_plan_graph(scratch_dir=tmp_path / "b").compile()
    out = app.invoke({"query": QUERY, "user_key": "u0000", "seed": 42,
                      "as_of": "2026-10-30", "thread_id": "parity-b"})
    return {"metrics": out["envelope"]["digest"], "steps": list(out["steps_seen"]),
            "model_calls": 0, "answer": out["envelope"]["answer"]}


def _from_tool_messages(out: dict) -> list[dict]:
    metrics: list[dict] = []
    for m in out["messages"]:
        if type(m).__name__ == "ToolMessage":
            doc = json.loads(m.content)
            for item in doc.get("metrics") or []:
                metrics.append({**item, "branch": doc.get("branch")})
    return metrics


def run_line_a(tmp_path: Path) -> dict:
    m = ScriptedChatModel(script=[{"tool_calls": [(s, ARGS) for s in SKILLS]},
                                  {"content": "三项指标见工具返回值"}])
    agent, _ = build_react_agent(model=m, skills=SKILLS, scratch_dir=tmp_path / "a")
    out = agent.invoke({"messages": [{"role": "user", "content": QUERY}]})
    return {"metrics": _from_tool_messages(out), "steps": [type(x).__name__ for x in out["messages"]],
            "model_calls": m.calls, "answer": str(out["messages"][-1].content)}


def run_line_a2(tmp_path: Path) -> dict:
    m = ScriptedChatModel(script=[{"tool_calls": [(s, ARGS) for s in SKILLS]},
                                  {"content": "三项指标见工具返回值"}])
    app, _ = build_react_min(model=m, skills=SKILLS, scratch_dir=tmp_path / "min")
    out = app.invoke({"messages": [{"role": "user", "content": QUERY}]}, {"recursion_limit": 25})
    return {"metrics": _from_tool_messages(out), "steps": [type(x).__name__ for x in out["messages"]],
            "model_calls": m.calls, "answer": str(out["messages"][-1].content)}


def test_three_lines_agree_on_every_number(tmp_path: Path) -> None:
    a, a2, b = run_line_a(tmp_path), run_line_a2(tmp_path), run_line_b(tmp_path)
    pa, pa2, pb = _pairs(a["metrics"]), _pairs(a2["metrics"]), _pairs(b["metrics"])
    assert pa == pa2 == pb, (
        f"三条线数字不一致 ⇒ 有人在编排层动了数\nA={sorted(pa)}\nA2={sorted(pa2)}\nB={sorted(pb)}")
    assert pb, "对照表不能是空的"
    # 抽样反查：至少这些真指标在三条线上都出现，且值一致
    names = {n for n, _ in pb}
    for expected in ("completion_rate", "overdue_rate", "milestone_offschedule_rate", "cpi", "eac"):
        assert expected in names, names


def test_flexibility_is_paid_for_in_model_rounds(tmp_path: Path) -> None:
    """可量化那半边：ReAct 多花模型轮次，公司范式一趟出计划。"""
    a, b = run_line_a(tmp_path), run_line_b(tmp_path)
    assert a["model_calls"] == 2, a["model_calls"]
    assert b["model_calls"] == 0, "无环范式不该问模型"
    # Line B 的节点执行数（router/guard/3 分支/fanin/aggregate）
    assert len(b["steps"]) == 7, b["steps"]


def test_line_b_answer_quotes_the_same_values(tmp_path: Path) -> None:
    """渲染层⛔ 改数：answer 里出现的百分比/金额必须与 digest 逐字一致。"""
    b = run_line_b(tmp_path)
    for m in b["metrics"]:
        if m["value"] is not None:
            assert m["value"] in b["answer"], f"{m['name']}={m['value']} 不在 answer 里"


def dump_for_readme(tmp_path: Path = Path(".tmp/parity")) -> dict:
    """给 README 抄的**真实读数**（⛔ 手填数字）。``python -m tests.test_graph_parity``。"""
    tmp_path.mkdir(parents=True, exist_ok=True)
    a, a2, b = run_line_a(tmp_path), run_line_a2(tmp_path), run_line_b(tmp_path)
    pa, pa2, pb = _pairs(a["metrics"]), _pairs(a2["metrics"]), _pairs(b["metrics"])
    return {"指标条数": len(pb),
            "三条线数字一致": pa == pa2 == pb,
            "模型轮次": {"create_agent": a["model_calls"], "react_min": a2["model_calls"],
                         "plan_graph": b["model_calls"]},
            "节点执行数": {"plan_graph": len(b["steps"])},
            "样例指标": sorted(f"{n}={v}" for n, v in pb)[:6]}


if __name__ == "__main__":  # pragma: no cover
    import sys
    if hasattr(sys.stdout, "reconfigure"):   # 中文 Windows 控制台默认 cp936：
        sys.stdout.reconfigure(encoding="utf-8", errors="backslashreplace")   # 打 ⛔/✅ 会 UnicodeEncodeError
    print(json.dumps(dump_for_readme(), ensure_ascii=False, indent=1, default=str))
