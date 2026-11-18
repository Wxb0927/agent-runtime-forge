"""Line B 侧的"立项准备"长流程：**断点续办 + 写前人工闸**。

    START → collect → verify → confirm(interrupt) → write → END
                                          ↘（reject）→ abort → END

为什么这条线要单独存在（⛔ 跟 Line A 的 HITL 中间件重复）：中间件那种闸是**框架给的**，
只要工具在名单里就自动停；这里的闸是**自己画的节点**，正是公司那套"宿主驱动"要做的事
——它没有常驻进程、也没有 checkpointer，所以只能设计成"会话内跑完 + 下轮从库里捞状态"。
两边都实现一遍，才讲得出"框架替你承担了什么"。

⚠️ 三个语义在 ``tests/test_graph_resume.py`` 里用**硬杀进程**取证，⛔ 嘴上说：
* 已完成的节点（``collect``）重启后**不会重跑** ⇒ ``collect`` 的运行计数保持 1；
* 崩在节点中间（``verify``）时，重启是**该节点从头重跑** ⇒ ``verify`` 计数变成 2
  ⇒ 所以节点必须幂等，有副作用的写要放在闸后面；
* ``interrupt`` 的暂停点会被持久化 ⇒ 换进程 ``Command(resume=…)`` 能接着往下走。

副作用全部落在 ``scratch_dir`` 下的计数文件里——checkpoint ⛔ 是"跑过几次"的证据来源，
只有外部计数能证明节点到底执行了几次。
"""
from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Annotated, Any, Literal, Optional, TypedDict

import operator

from langgraph.graph import END, START, StateGraph
from langgraph.types import interrupt

REQUIRED_FIELDS = ("project_name", "customer", "business_unit", "plan_start", "plan_end")
BU_LAYOUTS = {"bu-alpha": ("wbs_template",), "bu-beta": ("wbs_template", "line_count"),
              "bu-gamma": ("wbs_template",), "bu-delta": ("wbs_template",)}


class IntakeState(TypedDict, total=False):
    thread_id: str
    user_key: str
    scratch_dir: str
    input: dict[str, Any]
    draft: dict[str, Any]
    missing: list[str]
    decisions: Annotated[list[str], operator.add]
    steps_seen: Annotated[list[str], operator.add]
    notes: Annotated[list[str], operator.add]
    outcome: str
    code: str
    audit: Annotated[list[dict[str, Any]], operator.add]


def _count(root: Path, name: str) -> int:
    """节点执行计数器：写在**进程外**，所以硬杀也带不走它。"""
    p = root / f"{name}.count"
    n = int(p.read_text(encoding="utf-8")) if p.exists() else 0
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(str(n + 1), encoding="utf-8")
    return n + 1


def build_intake_graph(*, verify_seconds: float = 0.0, crash_in_verify: float = -1.0):
    def n_collect(state: IntakeState) -> dict[str, Any]:
        root = Path(state["scratch_dir"])
        _count(root, "collect")
        draft = dict(state.get("input") or {})
        missing = [f for f in REQUIRED_FIELDS if not draft.get(f)]
        return {"draft": draft, "missing": missing, "steps_seen": ["collect"],
                "notes": [f"[collect] 缺失必填 {missing or '无'}"]}

    def n_verify(state: IntakeState) -> dict[str, Any]:
        root = Path(state["scratch_dir"])
        _count(root, "verify")
        # ⚠️ 条件必须覆盖"只要求崩溃、不要求等待"的情况：第一版写成 if verify_seconds，
        #    于是 --crash-in-verify 0（默认 verify_seconds=0.0）整块被跳过，
        #    **进程根本没崩**，probe 测的是一路正常跑完的对照组——典型的假实验。
        if verify_seconds or crash_in_verify >= 0:
            # 打一行给父进程看，父进程据此确定"已经进了 verify"再 kill()——
            # ⛔ 用 sleep(1.5) 猜时机（那样测到的是进程启动快慢，不是重放语义）
            print(f"PHASE verify-start {state.get('thread_id')}", flush=True)
            if crash_in_verify >= 0:
                # ⚠️ 决定性死点：节点自己 os._exit(9)，⛔ 由父进程掐表 kill
                #    （父进程 kill 的时机落在 sleep 里哪一秒是不确定的，测出来的
                #     "collect 会不会重跑"就会偶发翻转——实测就是这样翻车的）
                time.sleep(crash_in_verify)
                print(f"PHASE verify-crash {state.get('thread_id')}", flush=True)
                import os
                os._exit(9)
            time.sleep(verify_seconds)
        draft = dict(state.get("draft") or {})
        # ⚠️ 只登记缺什么，⛔ 拿占位符把洞填上——占位符一路写进库，
        #    就是"确认闸放行了不完整数据"（第一版真这么错过，见 README 的踩坑记录）。
        layout = BU_LAYOUTS.get(draft.get("business_unit") or "")
        extra = [k for k in layout if not draft.get(k)]
        return {"draft": draft, "missing": sorted(set(state.get("missing", [])) | set(extra)),
                "steps_seen": ["verify"],
                "notes": [f"[verify] BU 布局要求 {list(layout) or '无'}，仍缺 {extra or '无'}"]}

    def n_confirm(state: IntakeState) -> dict[str, Any]:
        """写前闸：``interrupt`` 把决定权交给人，值由 ``Command(resume=…)`` 送回。"""
        payload = {"kind": "intake_approval", "draft": state.get("draft"),
                   "missing": state.get("missing"),
                   "allowed_decisions": ["approve", "edit", "reject"]}
        decision = interrupt(payload) or {}
        kind = decision.get("type", "reject")
        draft = dict(state.get("draft") or {})
        if kind == "edit" and decision.get("edited_action"):
            draft.update(decision["edited_action"].get("args") or {})
            kind_note = "edit"
        else:
            kind_note = kind
        # 人改过参数 ⇒ 重新核一遍必填与 BU 布局，⛔ 拿改之前的 missing 判放行
        layout = BU_LAYOUTS.get(draft.get("business_unit") or "")
        missing = sorted({*REQUIRED_FIELDS, *layout} - {k for k, v in draft.items() if v})
        return {"draft": draft, "missing": missing, "steps_seen": [f"confirm:{kind_note}"],
                "decisions": [json.dumps(decision, ensure_ascii=False)],
                "outcome": kind_note,
                "notes": [f"[confirm] 人工决定={kind_note}，复核后仍缺 {missing or '无'}"]}

    def route_after_confirm(state: IntakeState) -> Literal["write", "abort"]:
        """闸有两道：人不批 ⛔ 写；人批了但数据仍缺必填，也 ⛔ 写。"""
        if state.get("outcome") == "reject":
            return "abort"
        return "abort" if state.get("missing") else "write"

    def n_write(state: IntakeState) -> dict[str, Any]:
        root = Path(state["scratch_dir"])
        root.mkdir(parents=True, exist_ok=True)
        path = root / f"draft-{state['thread_id']}.json"
        doc = {"thread_id": state["thread_id"], "actor": state.get("user_key", "?"),
               "approved_by": "human", "payload": state.get("draft") or {}}
        path.write_text(json.dumps(doc, ensure_ascii=False, sort_keys=True), encoding="utf-8")
        with (root / "audit.jsonl").open("a", encoding="utf-8") as fh:
            fh.write(json.dumps({"event": "intake_written", "thread_id": state["thread_id"],
                                 "decisions": state.get("decisions", [])},
                                ensure_ascii=False, sort_keys=True) + "\n")
        return {"code": "WRITTEN", "steps_seen": ["write"],
                "audit": [{"event": "intake_written", "path": str(path)}]}

    def n_abort(state: IntakeState) -> dict[str, Any]:
        code = "USER_REJECTED" if state.get("outcome") == "reject" else "INCOMPLETE_INTAKE"
        return {"code": code, "steps_seen": ["abort"],
                "notes": [f"[abort] {code}：未写入任何数据"]}

    g = StateGraph(IntakeState)
    g.add_node("collect", n_collect)
    g.add_node("verify", n_verify)
    g.add_node("confirm", n_confirm)
    g.add_node("write", n_write)
    g.add_node("abort", n_abort)
    g.add_edge(START, "collect")
    g.add_edge("collect", "verify")
    g.add_edge("verify", "confirm")
    g.add_conditional_edges("confirm", route_after_confirm, ["write", "abort"])
    g.add_edge("write", END)
    g.add_edge("abort", END)
    return g


INTAKE = build_intake_graph().compile()  # ⚠️ 无 checkpointer：只能同进程用；跨轮续办必须自己 compile(checkpointer=…)


def intake_input(**over: Any) -> dict[str, Any]:
    base = {"thread_id": "intake-1", "user_key": "u0041", "scratch_dir": "data/intake",
            # 默认给一份**齐全**的输入（bu-beta 布局要 wbs_template + line_count），
            # 缺字段的分支由 test_incomplete_draft_is_blocked 专门覆盖。
            "input": {"project_name": "新线体导入", "customer": "customer-07",
                      "business_unit": "bu-beta", "plan_start": "2026-12-01",
                      "plan_end": "2027-03-31", "wbs_template": "standard-wbs",
                      "line_count": 2}}
    base.update(over)
    return base
