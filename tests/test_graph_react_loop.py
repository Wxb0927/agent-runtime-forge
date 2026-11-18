"""Line A（``create_agent`` + 中间件）与 Line A2（手写最小环）的行为断言。

全部离线：模型是 ``ScriptedChatModel``，⛔ 联网、⛔ 烧钱、⛔ 随网络抖动。
断言的对象是**编排语义**（谁持有循环、并行写回会不会互相覆盖、闸拦不拦得住），
不是模型聪明不聪明——那是 D4 评测线的事。
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from langgraph.types import Command

from forge.graph import checkpoint
from forge.graph.react_agent import WRITE_TOOL, build_react_agent
from forge.graph.react_min import build_react_min
from forge.model.scripted import ScriptedChatModel

USER = "u0041"
PROJECT = "PRJ-1180"
ARGS = {"user_key": USER, "project": PROJECT, "as_of": "2026-10-30"}
WARGS = {"project_name": "新线体导入", "customer": "customer-07",
         "business_unit": "bu-beta", "user_key": USER, "as_of": "2026-10-30"}


def msg_kinds(out: dict[str, Any]) -> list[str]:
    kinds = []
    for m in out["messages"]:
        tag = type(m).__name__
        if tag == "AIMessage" and getattr(m, "tool_calls", None):
            tag += f"({len(m.tool_calls)})"
        kinds.append(tag)
    return kinds


def tool_payloads(out: dict[str, Any]) -> dict[str, dict]:
    got = {}
    for m in out["messages"]:
        if type(m).__name__ == "ToolMessage":
            doc = json.loads(m.content)
            got[doc.get("branch") or doc.get("code") or "unknown"] = doc
    return got


# --------------------------------------------------------------- 环归框架


def test_framework_holds_the_loop(tmp_path: Path) -> None:
    """模型发 2 个并行 tool_calls → 框架执行 → 回到模型 → 收尾。**循环不在我代码里**。"""
    m = ScriptedChatModel(script=[{"tool_calls": [("stats", ARGS), ("deviation", ARGS)]},
                                  {"content": "completion_rate 见工具返回值"}])
    agent, _ = build_react_agent(model=m, scratch_dir=tmp_path)
    out = agent.invoke({"messages": [{"role": "user", "content": "进度和偏差"}]})
    assert m.calls == 2, f"模型应当被调用两轮（要工具 / 给答案），实际 {m.calls}"
    assert msg_kinds(out) == ["HumanMessage", "AIMessage(2)", "ToolMessage", "ToolMessage",
                              "AIMessage"], msg_kinds(out)


def test_parallel_tool_calls_never_clobber_each_other(tmp_path: Path) -> None:
    """Line A 版的竞态断言：两条并行工具调用写回 ``messages`` 走 ``add_messages`` reducer
    ⇒ 两条结果都在。⛔ 后写覆盖先写（那正是 Line B 事故形状的失效模式）。"""
    m = ScriptedChatModel(script=[{"tool_calls": [("stats", ARGS), ("deviation", ARGS),
                                                  ("cost", ARGS)]},
                                  {"content": "齐了"}])
    agent, _ = build_react_agent(model=m, scratch_dir=tmp_path)
    losses = 0
    for i in range(20):
        mm = ScriptedChatModel(script=m.script, fallback="齐了")
        agent_i, _ = build_react_agent(model=mm, scratch_dir=tmp_path / f"r{i}")
        out = agent_i.invoke({"messages": [{"role": "user", "content": "全量"}]})
        payloads = tool_payloads(out)
        if not {"stats", "deviation", "cost"} <= set(payloads):
            losses += 1
    assert losses == 0, f"20 次并行工具调用里有 {losses} 次结果被覆盖"


def test_route_prunes_the_bound_tool_subset(tmp_path: Path) -> None:
    """主流做法的取证点：路由只给 ``cost`` ⇒ 模型这一轮**根本看不见** stats/deviation。"""
    m = ScriptedChatModel(script=[{"tool_calls": [("cost", ARGS)]}, {"content": "只看了成本"}])
    agent, meta = build_react_agent(model=m, skills=["cost"], scratch_dir=tmp_path)
    agent.invoke({"messages": [{"role": "user", "content": "超支了吗"}]})
    assert meta["tools"] == ["cost", WRITE_TOOL], meta["tools"]
    assert m.bound_history[0] == sorted(["cost", WRITE_TOOL]), m.bound_history


def test_model_call_limit_stops_an_endless_loop(tmp_path: Path) -> None:
    """治理挂中间件：模型永远要调工具时，``ModelCallLimitMiddleware`` 必须收尾，
    ⛔ 把 recursion_limit 当治理用（那是兜底，不是策略）。"""
    endless = [{"tool_calls": [("stats", ARGS)]}] * 30
    m = ScriptedChatModel(script=endless, fallback="（已按调用上限收尾）")
    agent, _ = build_react_agent(model=m, scratch_dir=tmp_path, thread_call_limit=3)
    out = agent.invoke({"messages": [{"role": "user", "content": "反复查"}]})
    assert m.calls <= 4, f"调用上限失效，模型被叫了 {m.calls} 次"
    assert out["messages"][-1].content, "收尾必须给出一条消息"


# ------------------------------------------------------------ 手写最小环


def test_react_min_is_the_same_loop_written_by_hand(tmp_path: Path) -> None:
    m = ScriptedChatModel(script=[{"tool_calls": [("stats", ARGS)]}, {"content": "完成率见返回值"}])
    app, tool_names = build_react_min(model=m, scratch_dir=tmp_path)
    out = app.invoke({"messages": [{"role": "user", "content": "进度"}]},
                     {"recursion_limit": 25})
    assert sorted(tool_names) == sorted(["stats", "deviation", "cost", WRITE_TOOL])
    assert msg_kinds(out) == ["HumanMessage", "AIMessage(1)", "ToolMessage", "AIMessage"]
    assert tool_payloads(out)["stats"]["metrics"], "手写环也要真取到数"


# ------------------------------------------------------------ 三方对照


def test_agent_and_min_agree_on_numbers(tmp_path: Path) -> None:
    """官方入口与手写环跑同一问句 ⇒ 工具产出的指标**逐字相同**（编排⛔ 影响正确性）。"""
    def run(builder, **kw):
        m = ScriptedChatModel(script=[{"tool_calls": [("stats", ARGS), ("deviation", ARGS)]},
                                      {"content": "done"}])
        app, _ = builder(model=m, scratch_dir=tmp_path / kw["tag"], **kw.get("extra", {}))
        out = app.invoke({"messages": [{"role": "user", "content": "进度和偏差"}]},
                         kw.get("config", {"recursion_limit": 25}))
        return {b: p["metrics"] for b, p in tool_payloads(out).items()}

    a = run(build_react_agent, tag="a")
    b = run(build_react_min, tag="min")
    assert a == b, "两条编排给出的数字不一致 ⇒ 有一条在渲染层动了数"


@pytest.mark.parametrize("decision,should_write", [
    ({"type": "approve"}, True),
    ({"type": "reject", "message": "先不要建项目"}, False),
])
def test_hitl_approve_and_reject_paths(tmp_path: Path, decision, should_write) -> None:
    """验收③的两条：approve 落草稿+审计；reject **根本不执行写**（marker 缺失为证）。"""
    with checkpoint.saver(tmp_path / "ckpt.sqlite3") as s:
        m = ScriptedChatModel(script=[{"tool_calls": [(WRITE_TOOL, WARGS)]},
                                      {"content": "已按人工决定处理"}])
        scratch = tmp_path / "drafts"
        agent, _ = build_react_agent(model=m, checkpointer=s, scratch_dir=scratch)
        cfg = checkpoint.thread_config(f"hitl-{decision['type']}")
        out = agent.invoke({"messages": [{"role": "user", "content": "帮我立项"}]}, cfg)
        assert "__interrupt__" in out and out["__interrupt__"], "写操作前必须停下来等人"
        payload = out["__interrupt__"][0].value
        assert payload["action_requests"][0]["name"] == WRITE_TOOL
        draft = scratch / f"draft-{USER}-2026-10-30.json"
        assert not draft.exists(), "还没批准就写了草稿 ⇒ 闸是假的"

        out2 = agent.invoke(Command(resume={"decisions": [decision]}), cfg)
        assert draft.exists() is should_write, f"{decision['type']} 路径的写行为不对"
        audit = scratch / "audit.jsonl"
        assert audit.exists() is should_write
        tail = [mm for mm in out2["messages"] if type(mm).__name__ == "ToolMessage"]
        assert tail, "批准/拒绝后都要给模型一条工具回执"


def test_hitl_edit_actually_uses_the_human_value(tmp_path: Path) -> None:
    """三态里最容易被糊过去的一条：edit 必须**用改后的值**去写，⛔ 只记一笔"人改过"。"""
    edited = dict(WARGS, customer="customer-99")
    with checkpoint.saver(tmp_path / "ckpt.sqlite3") as s:
        m = ScriptedChatModel(script=[{"tool_calls": [(WRITE_TOOL, WARGS)]},
                                      {"content": "已按人工修改后的参数提交"}])
        scratch = tmp_path / "drafts"
        agent, _ = build_react_agent(model=m, checkpointer=s, scratch_dir=scratch)
        cfg = checkpoint.thread_config("hitl-edit")
        agent.invoke({"messages": [{"role": "user", "content": "帮我立项"}]}, cfg)
        agent.invoke(Command(resume={"decisions": [
            {"type": "edit", "edited_action": {"name": WRITE_TOOL, "args": edited}}]}), cfg)
        doc = json.loads((scratch / f"draft-{USER}-2026-10-30.json").read_text(encoding="utf-8"))
        assert doc["payload"]["customer"] == "customer-99", doc["payload"]
        assert doc["payload"]["customer"] != WARGS["customer"]
