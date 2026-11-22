"""⑥ 失败归类：每个红都必须落进一个**有名字的桶**。

四类各有固定修法，混在一起看就只是"今天又红了几个"：

* **参数错误** —— 意图本身不合法（非法键、非法枚举、基准日畸形、越界）。
  ⇒ 修调用方/提示词，⛔ 动计算层。
* **上下文污染** —— 答案里出现了来源不明的数字、段名漂了、出现禁词。
  ⇒ 最危险的一类：数字看着对，其实是模型自己算的。
* **弃权边界** —— 系统选择不答（越权拒答、无数据点名）。
  ⇒ 这类"红"往往是对的，登记成已知即可，⛔ 当 bug 修掉。
* **数据缺失** —— 计算层在给定范围内根本没产出这一项。
  ⇒ 修数据或改口径，⛔ 让渲染层编。
"""
from __future__ import annotations

from collections import Counter
from typing import Any

CLASS_BY_RULE: dict[str, str] = {
    # 参数错误
    "bad-intent": "参数错误", "bad-host-arg": "参数错误", "unknown-key": "参数错误",
    "clamp": "参数错误", "envelope-shape": "参数错误",
    # 上下文污染
    "fabricated-number": "上下文污染", "value-in-answer": "上下文污染",
    "section-unknown": "上下文污染", "banned-words": "上下文污染",
    "section-order": "上下文污染", "section-missing": "上下文污染",
    "number-format": "上下文污染", "date-format": "上下文污染",
    "traceable": "上下文污染", "oracle-mismatch": "上下文污染",
    # 弃权边界
    "not-in-scope": "弃权边界", "no-data-named": "弃权边界", "no-data": "弃权边界",
    # 数据缺失
    "oracle-missing": "数据缺失", "empty-digest": "数据缺失",
}
CLASSES = ("参数错误", "上下文污染", "弃权边界", "数据缺失")
PRIORITY = ("上下文污染", "数据缺失", "参数错误", "弃权边界")


def classify_case(case: dict[str, Any], run: Any, findings: list[dict[str, str]]) -> dict[str, Any]:
    code = (run.envelope or {}).get("code")
    rules_hit = {f["rule"] for f in findings}
    if code == "NOT_IN_SCOPE":
        rules_hit.add("not-in-scope")
    if code == "BAD_INTENT":
        rules_hit.add("bad-intent")
    if code == "BAD_HOST_ARG":
        rules_hit.add("bad-host-arg")
    if any(str(n).startswith(("DROPPED:", "CLAMPED")) for n in (run.envelope.get("notes") or [])):
        rules_hit.add("clamp" if any("CLAMPED" in str(n) for n in run.envelope["notes"]) else "unknown-key")
    if not (run.envelope or {}).get("digest") and case.get("expect", {}).get("digest_empty") is False:
        rules_hit.add("empty-digest")

    buckets = {CLASS_BY_RULE.get(r, "其他") for r in rules_hit}
    primary = next((c for c in PRIORITY if c in buckets), None)
    if primary is None:
        primary = "弃权边界" if (code in ("NO_DATA", "NOT_IN_SCOPE") or "no-data" in rules_hit) else None
    return {"id": case["id"], "code": code, "rules": sorted(rules_hit),
            "classes": sorted(buckets), "primary": primary}


def summarize(labels: list[dict[str, Any]]) -> dict[str, Any]:
    counter: Counter = Counter(l["primary"] for l in labels if l["primary"])
    return {
        "counts": {c: counter.get(c, 0) for c in CLASSES},
        "其他": counter.get("其他", 0),
        "labeled": sum(1 for l in labels if l["primary"]),
        "clean": sum(1 for l in labels if not l["primary"]),
        "examples": {c: [l["id"] for l in labels if l["primary"] == c][:3] for c in CLASSES},
    }
