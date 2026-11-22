"""④ 回归差集：改动前后跑同一批语料，**逐句**比对，未登记的变化即失败。

三条设计约定（都是真实项目里被逼出来的）：

* **变化必须"登记"才算数。** 差集只报"哪里变了"，⛔ 判断"变得对不对"——
  对的设计走 ``--rebase`` 更新基线，错的改动被这条门禁拦在 PR 前。
* **已知红必须写理由。** ``eval/known_red.json`` 里每条都要带一句话原因；
  ⛔ 空字符串、⛔ "todo"，也⛔ 把新增红静默容忍成"下次再说"。
* **逐句而非逐字节。** 表头分隔行、日期这类改动要能定位到行，否则报告只会说"不一样"。
"""
from __future__ import annotations

import re
from typing import Any



def sentences(text: str) -> list[str]:
    """按行拆（段名/表头/数据行各自一句），⛔ 整篇比字符串。"""
    return [ln.strip() for ln in (text or "").splitlines() if ln.strip()]


def diff_case(case_id: str, base: dict[str, Any] | None, cur: dict[str, Any],
              known: dict[str, str]) -> dict[str, Any]:
    """返回一个 case 的变化状态。``status`` ∈ new/added/unchanged/changed/removed。"""
    if base is None:
        return {"id": case_id, "status": "added", "registered": False}
    if cur.get("absent"):
        return {"id": case_id, "status": "removed", "registered": case_id in known}

    changed: dict[str, Any] = {}
    if base.get("code") != cur.get("code"):
        changed["code"] = f"{base.get('code')} → {cur.get('code')}"
    b_lines, c_lines = sentences(base.get("answer", "")), sentences(cur.get("answer", ""))
    added = [l for l in c_lines if l not in b_lines]
    removed = [l for l in b_lines if l not in c_lines]
    if added or removed:
        changed["answer"] = {"added": added, "removed": removed}
    if base.get("digest") != cur.get("digest"):
        changed["digest"] = {"was": base.get("digest"), "now": cur.get("digest")}

    if not changed:
        return {"id": case_id, "status": "unchanged", "registered": False}
    registered = case_id in known
    return {"id": case_id, "status": "changed", "changes": changed, "registered": registered,
            "known_reason": known.get(case_id)}


def run_diff(current: list[dict[str, Any]], baseline: dict[str, dict[str, Any]],
             known: dict[str, str]) -> dict[str, Any]:
    """汇总差集。``new_red`` 非空 ⇒ PR 前检查必须 exit 1。"""
    rows = [diff_case(r["id"], baseline.get(r["id"]), r, known) for r in current]
    baseline_only = sorted(set(baseline) - {r["id"] for r in current})
    removed = [{"id": i, "status": "removed", "registered": i in known} for i in baseline_only]
    all_rows = rows + removed
    unregistered_new = [r for r in all_rows if r["status"] == "added"]
    changed_unregistered = [r for r in all_rows
                            if r["status"] == "changed" and not r["registered"]]
    return {
        "baseline_established": bool(baseline),
        "compared": len(all_rows),
        "unchanged": sum(1 for r in all_rows if r["status"] == "unchanged"),
        "added": unregistered_new,
        "changed": [r for r in all_rows if r["status"] == "changed"],
        "removed": removed,
        "registered_changes": [r for r in all_rows if r.get("registered") and r["status"] != "unchanged"],
        # 首次建基线时的 added 不算红（⛔ 让 --rebase 之后第一次跑就一堆红）
        "new_red": ([] if not baseline else unregistered_new) + changed_unregistered
                   + [r for r in removed if not r["registered"]],
    }


def validate_known_red(known: dict[str, str], existing_ids: set[str]) -> list[str]:
    """已知红的自检：⛔ 空理由、⛔ 指向已不存在的用例（那叫"僵尸豁免"）。"""
    problems: list[str] = []
    for cid, reason in known.items():
        if not reason or len(reason.strip()) < 4 or reason.strip().lower() in {"todo", "tbd", "fixme"}:
            problems.append(f"known_red[{cid}] 缺理由或理由是占位符：{reason!r}")
        if cid not in existing_ids:
            problems.append(f"known_red[{cid}] 指向的用例已不存在（僵尸豁免，删掉它）")
    return problems
