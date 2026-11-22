"""竞态靶心：三条分支并发跑完后，**各分支的来源标记必须都还在**。

对应简历那句「定位一处并行共写竞态：统计与偏差技能共写同一份上游交接文件、`from`/`step`
互相覆盖（数据对、来源标签漂）→ 根因是共享可变状态缺隔离 → 给出独立状态通道 + fan-in 解法」。
这里三种形状各有独立测试，⛔ 靠注释声称：

1. **修复形状**：各写各的 state 通道 + 单写者 fan-in ⇒ 标记全在（绿灯）。
2. **事故形状**：共写同一份交接文件的同一组字段 ⇒ **静默**漂标记（本文件断言它真的会漂，
   用来证明第 1 条的绿不是"bug 压根没复现"）。
3. **框架自带的保护**：两个并行节点返回同一个 channel key ⇒ 直接抛
   ``InvalidUpdateError``（自研 DAG 层没有这层保护，这是"框架替你承担了什么"的具体一件）。

跑法：uv run pytest tests/test_graph_channels_race.py -q
"""
from __future__ import annotations

import asyncio
import json
import operator
import threading
from pathlib import Path
from typing import Annotated, TypedDict

import pytest
from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.errors import InvalidUpdateError
from langgraph.graph import END, START, StateGraph

from forge.graph.plan_graph import build_plan_graph

QUERY = "PRJ-1180 的进度、里程碑偏差和成本怎么样"
BRANCHES = ("stats", "deviation", "cost")
EXPECT = {"stats": ("task_table", 1), "deviation": ("milestone_table", 2), "cost": ("cost_docs", 3)}


def base_state(thread_id: str = "race-1") -> dict[str, object]:
    return {"query": QUERY, "user_key": "u0041", "seed": 42,
            "as_of": "2026-10-30", "thread_id": thread_id}


def markers_intact(state: dict) -> bool:
    """并发之后，通道里与 handoff（下游视角）里的来源标记都还在 ⇒ 没有互相覆盖。"""
    for b, (src, step) in EXPECT.items():
        got = state.get(b)
        if not got or got.get("from_") != src or got.get("step") != step:
            return False
    hand = state.get("handoff") or {}
    return all(hand.get(b, {}).get("from") == src and hand.get(b, {}).get("step") == step
               for b, (src, step) in EXPECT.items())


# --------------------------------------------------------------- 1 修复形状


def test_independent_channels_survive_fanout(tmp_path: Path) -> None:
    app = build_plan_graph(scratch_dir=tmp_path).compile()
    out = app.invoke(base_state())
    assert markers_intact(out), f"来源标记被覆盖：{out.get('handoff')}"
    assert sorted(out["handoff"]["branches"]) == sorted(BRANCHES)
    assert "task_table" in out["answer"] and "milestone_table" in out["answer"]


# --------------------------------------------------------------- 2 事故形状


def test_bug_shape_never_lets_both_markers_survive(tmp_path: Path) -> None:
    """共写同一份交接文件 ⇒ 下游**永远拿不到两条完整来源标记**。

    两路用 ``threading.Barrier`` 强制在读-改-写中段相遇：跑成串行会直接超时失败，
    ⛔ 靠 sleep 碰运气（那样测出来的红是抖动，不是证据）。

    ⚠️ 这条测试改过两次，两次都是被现实打脸：
    * 第一版只断言"``from``/``step`` 两个字段被覆盖"——实测更狠：**整份文档被后写者换掉**
      （连"两条分支都跑过"的记录都只剩一路）。
    * 第二版把断言写死成"文件能解析且 branches 只剩 1 个"——满载时偶发 ``JSONDecodeError``。
      那⛔ 是 flaky：并发非原子的 ``write_text`` 本来就可能把文件写坏。
    ⇒ 所以这里断言的是**唯一真正成立的不变量**：事故形状下两条来源标记**不可能同时完整**；
      其表现形式要么是覆盖/丢记录，要么是文件损坏到无法解析。两种都算复现成功，
      而"修复形状"必须**永远**不出现这两种结局（见 test_independent_channels_survive_fanout）。
    """
    bar = threading.Barrier(2, timeout=5.0)
    reached: list[str] = []

    def hook(branch: str) -> None:
        reached.append(branch)
        bar.wait()

    app = build_plan_graph(handoff_mode="shared", scratch_dir=tmp_path,
                           interleave_hook=hook).compile()
    try:
        out = app.invoke(base_state("bug-1"))
        crash: Exception | None = None
    except Exception as exc:  # noqa: BLE001
        out, crash = None, exc

    assert sorted(reached) == ["deviation", "stats"], f"两路没真并发：{reached}"

    if crash is not None:
        # 结局①：文件被并发写坏，下游（fanin 回读）当场炸
        assert "json" in type(crash).__module__ or isinstance(crash, ValueError), repr(crash)
        return

    # 结局②：静默漂移（这才是当年能活到线上的那种）
    assert out["envelope"]["ok"] is True, "漂了还不报错，正是原事故的隐蔽性所在"
    # 关键症状：**取数是对的**（图内通道两条都健在），坏的只是下游读到的那份共享状态
    assert out["stats"]["from_"] == "task_table" and out["deviation"]["from_"] == "milestone_table"
    assert not markers_intact(out), "事故形状下竟然没漂 ⇒ 这个复现失效了"

    files = list(Path(tmp_path).glob("*.json"))
    assert files, "事故形状应该留下了交接文件"
    text = files[0].read_text(encoding="utf-8")
    try:
        raw = json.loads(text)
    except json.JSONDecodeError:
        return                                     # 文件损坏也算"两条标记没同时活着"
    assert len(raw.get("branches", [])) <= 1, f"预期丢一整段记录，实际 {raw.get('branches')}"
    assert raw.get("from") in ("task_table", "milestone_table")
    drifted = [b for b in ("stats", "deviation")
               if (out["handoff"].get(b) or {}).get("from") != EXPECT[b][0]]
    assert drifted, f"下游 handoff 里两条标记都完好？复现失效：{out['handoff']}"

# --------------------------------------------------------------- 3 框架保护
class _OneKey(TypedDict, total=False):
    handoff: dict


def _writer(payload: dict):
    def _n(_s: _OneKey) -> dict:
        return {"handoff": payload}
    return _n


def _same_key_graph() -> StateGraph:
    g = StateGraph(_OneKey)
    g.add_node("a", _writer({"from": "task_table", "step": 1}))
    g.add_node("b", _writer({"from": "milestone_table", "step": 2}))
    g.add_edge(START, "a")
    g.add_edge(START, "b")
    g.add_edge("a", END)
    g.add_edge("b", END)
    return g


def test_shared_channel_key_raises_loudly() -> None:
    """M1 的机制：三条分支改成返回同一个 key ⇒ 框架当场报错（改歪必然红）。"""
    with pytest.raises(InvalidUpdateError):
        _same_key_graph().compile().invoke({})


def test_reducer_makes_the_same_key_legal_and_merges() -> None:
    """反面对照：同一个 key 只要声明了 reducer 就合法——所以"报错"不是目的，
    **让合并语义显式化**才是。这条测试防的是"把 M1 误讲成 LangGraph 不能共写"。

    ⚠️ 踩过的坑：``operator`` / ``Annotated`` 必须**模块级**导入。本文件带
    ``from __future__ import annotations``，类里的注解是字符串，LangGraph 用
    ``get_type_hints`` 在模块全局求值——写在函数里就 ``NameError``。
    """

    class _Merged(TypedDict, total=False):
        handoff: Annotated[list, operator.add]

    g = StateGraph(_Merged)
    g.add_node("a", _writer([{"from": "task_table", "step": 1}]))
    g.add_node("b", _writer([{"from": "milestone_table", "step": 2}]))
    g.add_edge(START, "a")
    g.add_edge(START, "b")
    g.add_edge("a", END)
    g.add_edge("b", END)
    out = g.compile().invoke({})
    assert sorted(d["from"] for d in out["handoff"]) == ["milestone_table", "task_table"]


# --------------------------------------------------------------- 验收① 压测


def test_stress_20_runs_same_thread_zero_overwrite(tmp_path: Path) -> None:
    """任务书验收①：同一 thread 连跑 20 次 ⇒ 字段覆盖 0 次。"""
    db = tmp_path / "ckpt.sqlite3"
    with SqliteSaver.from_conn_string(str(db)) as saver:
        app = build_plan_graph(scratch_dir=tmp_path / "h").compile(checkpointer=saver)
        cfg = {"configurable": {"thread_id": "stress-same-thread"}}
        losses = [i for i in range(20) if not markers_intact(app.invoke(base_state("stress-same-thread"), cfg))]
        assert losses == [], f"20 次里第 {losses} 次来源标记被覆盖"


def test_stress_20_concurrent_threads_zero_overwrite(tmp_path: Path) -> None:
    """验收①加强：20 个 thread 真并发（asyncio.gather），⛔ 串行凑数。

    ⚠️ 实测坑：``SqliteSaver`` **根本没实现异步**（``aget_tuple`` 直接
    ``NotImplementedError``），并发路径必须换 ``AsyncSqliteSaver``（依赖 ``aiosqlite``）。
    这条读数本身要写进 README——"本地用同步 saver、并发/服务化换异步 saver"是真实工程决策。
    """
    from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver

    async def main() -> list[dict]:
        async with AsyncSqliteSaver.from_conn_string(str(tmp_path / "ckpt2.sqlite3")) as saver:
            app = build_plan_graph(scratch_dir=tmp_path / "h").compile(checkpointer=saver)
            return await asyncio.gather(*(
                app.ainvoke(base_state(f"conc-{i}"), {"configurable": {"thread_id": f"conc-{i}"}})
                for i in range(20)))

    results = asyncio.run(main())
    broken = [f"conc-{i}" for i, r in enumerate(results) if not markers_intact(r)]
    assert not broken, f"并发下有 {len(broken)} 次覆盖：{broken}"
    assert len(results) == 20
