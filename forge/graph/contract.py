"""输出信封与成品正文：规范化在这里，⛔ 在模型那一侧。

契约（任务书 §3.4）：``{ok, code, message, digest[], notes[], answer}``。
- 数字**逐字来自计算层**（``digest``），渲染层⛔ 做任何算术；
- 派生量必须带 ``numerator/denominator/as_of``，可反查；
- **无数据 ≠ 0**：分母为空时 ``value`` 是 ``None``，且必须在 ``answer`` 里点名，
  ⛔ 静默显示成 0 或 ``¥0.00``（真实项目里这会被业务方当场推翻）；
- ``answer`` 是给宿主逐字照抄的成品正文，只锁段名/表头/列序/数字形态，⛔ 出现禁语。
"""
from __future__ import annotations

from typing import Any, Iterable, Optional

ENVELOPE_KEYS = ("ok", "code", "message", "digest", "notes", "answer")

# 测试会逐条扫 answer：命中即失败。这些词一出现，说明渲染层在猜、在推责、或在做无据概括。
BANNED_PHRASES: tuple[str, ...] = ("大概", "约莫", "一般来说", "我建议", "无法回答")

SECTION_ORDER = ("进度", "偏差", "成本")
BRANCH_SECTION = {"stats": "进度", "deviation": "偏差", "cost": "成本"}


def envelope(*, ok: bool, code: str, message: str = "",
             digest: Optional[list[dict[str, Any]]] = None,
             notes: Optional[Iterable[str]] = None, answer: str = "") -> dict[str, Any]:
    env = {"ok": ok, "code": code, "message": message,
           "digest": list(digest or []), "notes": list(notes or []), "answer": answer}
    assert tuple(env) == ENVELOPE_KEYS, f"envelope keys drifted: {tuple(env)}"
    return env


def _fmt_metric(m: dict[str, Any]) -> str:
    value = m.get("value")
    if value is None:  # 无数据：如实写，⛔ 显示 0
        return f"{m['name']}：{m.get('note') or '无数据'}"
    num, den = m.get("numerator"), m.get("denominator")
    tail = f"〔{m['note']}〕" if m.get("note") else ""
    if num is not None and den is not None:
        return f"{m['name']} {value}（{num:,}/{den:,}）{tail}"
    if num is not None:
        return f"{m['name']} {value}{tail}"
    return f"{m['name']} {value}{tail}"


def render_answer(*, scope: dict[str, Any], digest: list[dict[str, Any]],
                  as_of: str, projects: list[str]) -> str:
    """段名固定、列序固定、数字形态固定；⛔ 任何计算。"""
    lines: list[str] = []
    tier_note = scope.get("effective_tier", "member")
    narrowed = scope.get("business_unit") or scope.get("project_type") or "本人相关"
    lines.append(f"【范围】{tier_note}｜收窄={narrowed}｜可见 {len(projects)} 个项目｜基准日 {as_of}")
    for section in SECTION_ORDER:
        rows = [m for m in digest if BRANCH_SECTION.get(m.get("branch", "")) == section]
        if not rows:
            continue
        lines.append(f"【{section}】" + "；".join(_fmt_metric(m) for m in rows))
    empties = [m for m in digest if m.get("value") is None]
    if empties:
        lines.append("【无数据】" + "；".join(f"{m['name']}（{m.get('note') or '分母为空'}）"
                                             for m in empties))
    return "\n".join(lines)


def assert_no_banned(text: str) -> None:
    hits = [w for w in BANNED_PHRASES if w in text]
    if hits:
        raise AssertionError(f"answer 含禁语 {hits}（渲染层在做无据概括）")
