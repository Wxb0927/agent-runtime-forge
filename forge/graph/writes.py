"""唯一的"写"能力。Line A（工具）与 Line B（图节点）共用同一个实现，
这样 HITL 闸只需要守一个门口，⛔ 两处各写一份。

公司口径的对应物：L1 只读 → **L2 写操作前必须过人工确认闸** ⇒ 这里的
``submit_intake_draft`` 在设计上就假定"调用方已经拿到人的批准"，闸本身在
``react_agent``（HITL 中间件）与 ``intake_graph``（``interrupt()``）两侧实现。
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

AUDIT_NAME = "audit.jsonl"


def submit_intake_draft(payload: dict[str, Any], *, scratch_dir: Path | str,
                        actor: str, approved_by: str, thread_id: str) -> dict[str, Any]:
    """落一份立项草稿 + 一条审计。⛔ 幂等性不管：同 thread 重复调用会再写一条草稿，
    真实系统这里要有 idempotency key——留到 D3 的工具契约层一起做。
    """
    root = Path(scratch_dir)
    root.mkdir(parents=True, exist_ok=True)
    draft_path = root / f"draft-{thread_id}.json"
    doc = {"actor": actor, "approved_by": approved_by, "thread_id": thread_id,
           "payload": dict(payload)}
    draft_path.write_text(json.dumps(doc, ensure_ascii=False, sort_keys=True), encoding="utf-8")
    with (root / AUDIT_NAME).open("a", encoding="utf-8") as fh:
        fh.write(json.dumps({"event": "intake_draft_written", "thread_id": thread_id,
                             "actor": actor, "approved_by": approved_by},
                            ensure_ascii=False, sort_keys=True) + "\n")
    return {"ok": True, "code": "WRITTEN", "draft": str(draft_path),
            "payload": doc["payload"], "approved_by": approved_by}


def marker_exists(scratch_dir: Path | str, thread_id: str) -> bool:
    return (Path(scratch_dir) / f"draft-{thread_id}.json").exists()
