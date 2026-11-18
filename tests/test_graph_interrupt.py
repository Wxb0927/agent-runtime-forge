"""验收③：写前人工闸的 **approve / edit / reject 三条路径** + 闸必须真的拦人。

这条线是**自画节点**的闸（``interrupt()``），与 Line A 的 HITL 中间件是同一道门的两种实现：
中间件是框架给的，自画节点是"没有常驻进程的环境里只能自己画"的那一类——公司那套属于后者。
"""
from __future__ import annotations

import json
from pathlib import Path

from langgraph.checkpoint.memory import MemorySaver
from langgraph.types import Command

from forge.graph import checkpoint
from forge.graph.intake_graph import REQUIRED_FIELDS, build_intake_graph, intake_input

BU_LAYOUT_FIELDS = ("wbs_template", "line_count")


def app():
    return build_intake_graph().compile(checkpointer=MemorySaver())


def pause_at_gate(thread: str, scratch: Path, **over):
    a = app()
    cfg = checkpoint.thread_config(thread)
    out = a.invoke(intake_input(thread_id=thread, scratch_dir=str(scratch), **over), cfg)
    assert "__interrupt__" in out, f"{thread} 没有停在闸上"
    return a, cfg, out["__interrupt__"][0].value


def test_gate_blocks_before_any_write(tmp_path: Path) -> None:
    """闸生效的判据是**副作用没发生**：⛔ 只看返回值说"已暂停"。"""
    a, cfg, payload = pause_at_gate("g1", tmp_path)
    assert payload["kind"] == "intake_approval"
    assert payload["allowed_decisions"] == ["approve", "edit", "reject"]
    assert not (tmp_path / "draft-g1.json").exists()
    assert not (tmp_path / "audit.jsonl").exists()
    assert a.get_state(cfg).next == ("confirm",), "应当停在 confirm 节点"


def test_approve_writes_and_audits(tmp_path: Path) -> None:
    a, cfg, _ = pause_at_gate("g2", tmp_path)
    out = a.invoke(Command(resume={"type": "approve"}), cfg)
    assert out["code"] == "WRITTEN"
    doc = json.loads((tmp_path / "draft-g2.json").read_text(encoding="utf-8"))
    assert doc["payload"]["customer"] == "customer-07"
    audit = (tmp_path / "audit.jsonl").read_text(encoding="utf-8").strip().splitlines()
    assert len(audit) == 1 and "approve" in audit[0]
    # 已完成的节点⛔ 再跑：collect/verify 各一次
    assert out["steps_seen"] == ["collect", "verify", "confirm:approve", "write"], out["steps_seen"]
    assert (tmp_path / "collect.count").read_text() == "1"


def test_reject_writes_nothing(tmp_path: Path) -> None:
    a, cfg, _ = pause_at_gate("g3", tmp_path)
    out = a.invoke(Command(resume={"type": "reject", "message": "预算还没批"}), cfg)
    assert out["code"] == "USER_REJECTED"
    assert out["steps_seen"][-1] == "abort"
    assert not (tmp_path / "draft-g3.json").exists(), "拒绝之后竟然写了草稿"
    assert not (tmp_path / "audit.jsonl").exists()


def test_edit_uses_the_human_value(tmp_path: Path) -> None:
    """三态里最容易糊过去的一条：edit 必须**用改后的值**落库。"""
    a, cfg, _ = pause_at_gate("g4", tmp_path)
    out = a.invoke(Command(resume={"type": "edit", "edited_action": {
        "name": "confirm", "args": {"customer": "customer-99", "line_count": 5}}}), cfg)
    assert out["code"] == "WRITTEN"
    doc = json.loads((tmp_path / "draft-g4.json").read_text(encoding="utf-8"))
    assert doc["payload"]["customer"] == "customer-99" and doc["payload"]["line_count"] == 5


def test_approve_with_missing_fields_is_still_blocked(tmp_path: Path) -> None:
    """⚠️ 真实踩过的坑（第一版）：verify 拿 ``<待补>`` 占位符把洞填上，
    于是"人一批就写库"。现在缺必填**一律不落库**，哪怕人批了。
    """
    incomplete = {"project_name": "新线体导入", "customer": "customer-07",
                  "business_unit": "bu-beta", "plan_start": "2026-12-01",
                  "plan_end": "2027-03-31"}  # 故意少 bu-beta 布局要求的两项
    a, cfg, payload = pause_at_gate("g5", tmp_path, input=incomplete)
    assert set(payload["missing"]) == set(BU_LAYOUT_FIELDS), payload["missing"]
    out = a.invoke(Command(resume={"type": "approve"}), cfg)
    assert out["code"] == "INCOMPLETE_INTAKE", out
    assert not (tmp_path / "draft-g5.json").exists(), "缺必填还写库 ⇒ 闸形同虚设"
    for field in REQUIRED_FIELDS:
        assert "⛔" not in json.dumps(out, ensure_ascii=False)


def test_edit_that_fills_the_gaps_may_then_write(tmp_path: Path) -> None:
    """补法也要测：人把缺的两项补上之后，同一 thread 应当能写进去。"""
    incomplete = {"project_name": "新线体导入", "customer": "customer-07",
                  "business_unit": "bu-beta", "plan_start": "2026-12-01",
                  "plan_end": "2027-03-31"}
    a, cfg, _ = pause_at_gate("g6", tmp_path, input=incomplete)
    out = a.invoke(Command(resume={"type": "edit", "edited_action": {
        "name": "confirm",
        "args": {"wbs_template": "standard-wbs", "line_count": 2}}}), cfg)
    assert out["code"] == "WRITTEN", out
    assert (tmp_path / "draft-g6.json").exists()


def test_two_threads_do_not_share_one_draft(tmp_path: Path) -> None:
    """thread 隔离：两个 thread 各自停在闸上，批一个⛔ 影响另一个。"""
    a = app()
    cfg1 = checkpoint.thread_config("A")
    cfg2 = checkpoint.thread_config("B")
    a.invoke(intake_input(thread_id="A", scratch_dir=str(tmp_path)), cfg1)
    a.invoke(intake_input(thread_id="B", scratch_dir=str(tmp_path),
                          input={"project_name": "另一个项目", "customer": "customer-02",
                                 "business_unit": "bu-alpha", "plan_start": "2026-12-01",
                                 "plan_end": "2027-01-31", "wbs_template": "t"}), cfg2)
    a.invoke(Command(resume={"type": "approve"}), cfg1)
    assert a.get_state(cfg2).next == ("confirm",), "B 被 A 的批准带过去了"
    assert (tmp_path / "draft-A.json").exists() and not (tmp_path / "draft-B.json").exists()
