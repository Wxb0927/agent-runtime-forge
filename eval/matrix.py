"""⑤ 覆盖率矩阵：**域 × 问法自动枚举**，⛔ 人挑题。

这条是生产上付过代价换来的：人自己挑验收集，报出来的命中率必然偏袒
"我已经想到的说法"（当年人挑曾报 100%，换成矩阵枚举后大幅回落）。
所以格子的来源是 ``phrasings.build_queries()`` = 注册表里的每个域 × 每种问法，
⛔ 手工挑题、⛔ 事后删格——删格等于回到"自选验收集"。

**报两个数，⛔ 只报好那个：**

* `hit_rate` —— 该域的目标指标是否真出现在答案里（正确性）；
* `extra_skill_rate` —— 为了拿这个指标，路由额外拉起了几个用不到的技能（**上下文成本**）。

第二个数就是"自由度成本"：路由放开则一趟出计划但多算几个分支，收窄则要多烧模型轮次。
生产上"路由只吐技能菜单、不抽项目名"是这个取舍的结论，这里把它变成可测读数。
"""
from __future__ import annotations

from typing import Any

from eval import phrasings, system

HIT_RATE_CAVEAT = (
    "命中率只表示「目标指标出现在答案里」。路由器在⛔ 命中关键词时会回退到全技能，"
    "所以这一项结构上必然接近 100%，⛔ 当质量指标读。有区分度的是 red_cells"
    "（值与结构错误数）与 extra_skill_rate（为拿到指标多算了几路 = 上下文成本）。"
    "更硬的问题——「是否只复述、有无编造因果」——离线评不了，需 --live-judge，"
    "见 report 的 not_evaluated 字段。"
)


def summarize(cells: list[dict[str, Any]]) -> dict[str, Any]:
    """cells = 已经跑过并评过的用例（⛔ 在此重新执行——同一批用例只跑一次）。"""
    rows: list[dict[str, Any]] = []
    for c in cells:
        if not c.get("domain"):
            continue
        needed = system.needed_skill(c["domain"])
        extra = sorted(set(c.get("skills") or []) - {needed})
        rows.append({
            "id": c["id"], "domain": c["domain"], "phrasing": c.get("phrasing"),
            "hit": bool(c.get("hit")), "code": c.get("code"),
            "needed": needed, "extra_skills": extra,
            "red": int(c.get("red_count") or 0),
        })
    n = len(rows)
    hits = sum(1 for r in rows if r["hit"])
    reds = sum(1 for r in rows if r["red"])
    extras = sum(len(r["extra_skills"]) for r in rows)

    by_phrasing: dict[str, dict[str, int]] = {}
    for key, _ in phrasings.PHRASINGS:
        rs = [r for r in rows if r["phrasing"] == key]
        by_phrasing[key] = {"cells": len(rs), "hits": sum(1 for r in rs if r["hit"])}
    # 最差问法排在前面：报告必须让人先看见最烂那一类，⛔ 只报平均数
    worst = sorted(({"phrasing": k, **v} for k, v in by_phrasing.items()),
                   key=lambda x: (x["hits"], -x["cells"]))

    return {
        "shape": list(phrasings.grid_shape()),
        "cells": n, "hits": hits,
        "hit_rate": round(hits / n, 4) if n else None,
        "red_cells": reds,
        "extra_skill_total": extras,
        "extra_skill_rate": round(extras / n, 3) if n else None,
        "by_phrasing": by_phrasing,
        "worst_phrasings": worst,
        "missed": [{"id": r["id"], "code": r["code"]} for r in rows if not r["hit"]][:20],
        "hit_rate_caveat": HIT_RATE_CAVEAT,
    }
