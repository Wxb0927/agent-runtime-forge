"""``run(intent)``：键值字典校验 + ``BAD_INTENT`` 回带合法枚举（宿主自愈）。

三柱之三。规则与理由：

* **非法键：丢弃 + 点名**（⛔ 静默忽略，也⛔ 因为多一个键就整条拒答）。
  静默忽略会让"参数没生效"伪装成"数据就是这个值"——最难查的一类错。
* **非法枚举值：报 BAD 并回带合法枚举**（⛔ 猜测、⛔ 模糊匹配、⛔ 编辑距离自动纠正）。
  任务书原文："⛔ 不许用分词猜意图做主路径"。传 ``conplte_rate`` 就是⥒ 执行，
  也⥒ 悄悄按 ``completion_rate`` 跑出个看起来很对的数——那比报错危险得多。
* **越界：钳位 + 说明**（钳位是**合法语义**，报错才是事故）。
* **⚠️ 一次报全所有问题**：真实教训——校验器在第一个错误就抛，宿主每轮只能修一个、
  连烧三轮才跑通。这里一次列完（``metric 非法`` + ``topK 钳位`` + ``多余键丢弃``），一轮改完。
* **权限照常过 guard**：契约层⛔ 是绕过权限的后门，越权点名照样拒答。

信封严格用 §3.4 的六字段；自愈信息放 ``notes``（机器可读行以 ``heal:`` 开头，
用 ``parse_heal()`` 取回），⛔ 为了自己方便就往信封里塞第七个字段。
"""
from __future__ import annotations

import json
from datetime import date
from typing import Any, Optional

from forge.contract import cards as C
from forge.contract import metrics as M
from forge.graph import contract
from forge.graph.compute import dataset_for, run_branch
from forge.graph.guard import decide


def external_names() -> list[str]:
    """对外参数名（含 alias），与 ``describe()["params"]`` 同源。"""
    return sorted(C.alias_of(k) for k in C.SkillIntent.model_fields)


def _bounds(pname: str) -> dict[str, Any]:
    return C._bounds(C.SkillIntent.model_fields[pname])


def validate(intent: dict[str, Any]) -> dict[str, Any]:
    """返回 ``{ok, problems[], drops[], clamps[], heal{}, clean{}}``。"""
    problems: list[str] = []
    clamps: list[str] = []
    heal: dict[str, Any] = {}
    clean: dict[str, Any] = {}

    known = {C.alias_of(k): k for k in C.SkillIntent.model_fields}   # 对外名 → 内部名
    drops = sorted(set(intent) - set(known))
    if drops:
        # ⚠️ 丢弃 + 点名，但**⛔ 致命**：一个多余键毁掉整次查询，等于把"参数写错"
        #    升级成"服务不可用"。公司那套的语义就是 drop-and-continue，
        #    代价由 notes 里的 DROPPED 行承担（⛔ 静默忽略——那才是真危险）。
        notes_early = f"未知键已丢弃：{drops}"
        heal["unknown_keys"] = drops

    missing = sorted(C.alias_of(k) for k, f in C.SkillIntent.model_fields.items()
                     if f.is_required() and C.alias_of(k) not in intent)
    if missing:
        problems.append(f"缺少必填键：{missing}")
        heal["required"] = missing

    # metric：只认注册表里的名字，⛔ 模糊匹配、⛔ 自动纠正
    if "metric" in intent:
        declared = M.declared_metrics()
        if intent["metric"] in declared:
            clean["metric"] = intent["metric"]
        else:
            problems.append(f"metric 非法：{intent['metric']!r}（⛔ 猜测、⛔ 自动纠正）")
            heal["metric"] = declared                      # ← 回带合法枚举

    if "project" in intent:
        clean["project"] = intent["project"]

    if "topK" in intent:
        b = _bounds("top_k")
        lo, hi = int(b.get("min", 1)), int(b.get("max", C.TOP_K_MAX))
        raw = intent["topK"]
        try:
            val = int(raw)
        except (TypeError, ValueError):
            problems.append(f"topK 必须是整数，收到 {raw!r}")
            heal["topK"] = f"{lo}..{hi}"
        else:
            clean["top_k"] = min(max(val, lo), hi)
            if clean["top_k"] != val:
                msg = f"CLAMPED topK {val}→{clean['top_k']}（{'上限' if val > hi else '下限'} {hi if val > hi else lo}）"
                clamps.append(msg)                          # 钳位：说明即可，⛔ 报错
                heal["topK"] = f"{lo}..{hi}"
    else:
        clean["top_k"] = int(C.SkillIntent.model_fields["top_k"].default)

    return {"ok": not problems, "problems": problems, "notices": ([notes_early] if drops else []),
            "drops": drops, "clamps": clamps,
            "heal": heal, "clean": clean}


def decide_from(seed: int, user_key: str, project: Optional[str]) -> dict[str, Any]:
    return decide(dataset_for(seed), user_key, [project] if project else None)


def run(intent: dict[str, Any], *, user_key: str = "u0000",
        as_of: str = "2026-10-30", seed: int = 42) -> dict[str, Any]:
    """契约层唯一入口。``user_key`` / ``as_of`` / ``seed`` 是**宿主注入项**（见 cards 的
    ``host_injected``，由 ``inspect.signature`` 派生），⛔ 出现在意图 JSON 里。

    无论成败都返回**合法信封**。
    """
    v = validate(intent)
    notes: list[str] = ([f"heal:{json.dumps(v['heal'], ensure_ascii=False, sort_keys=True)}"]
                        if v["heal"] else [])
    notes += [f"DROPPED:{k}" for k in v["drops"]]
    notes += v.get("notices") or []
    notes += v["clamps"]

    if not v["ok"]:
        return contract.envelope(
            ok=False, code="BAD_INTENT",
            message="；".join(v["problems"]),
            digest=[], notes=notes,
            answer="【意图未执行】" + "；".join(v["problems"]) + "。合法取值见 notes，请改后重试。")

    clean = v["clean"]
    try:
        as_of_iso = date.fromisoformat(as_of).isoformat()
    except ValueError:
        return contract.envelope(
            ok=False, code="BAD_HOST_ARG",
            message=f"宿主注入的 as_of 必须是 YYYY-MM-DD，收到 {as_of!r}（⛔ 用系统时间兜底）",
            digest=[], notes=notes, answer="")

    scope = decide_from(seed, user_key, clean.get("project"))
    if scope["named_outside"]:
        return contract.envelope(
            ok=False, code="NOT_IN_SCOPE",
            message=f"项目 {scope['named_outside']} 不在可见范围内，未取数",
            digest=[], notes=notes + [f"[guard] {scope['reason']}"], answer="")

    branch = M.branch_of(clean["metric"])
    projects = [clean["project"]] if clean.get("project") else scope["visible_projects"]
    rows = [m for m in run_branch(branch, seed, projects, as_of_iso)["metrics"]
            if m["name"] == clean["metric"]]
    for m in rows:
        m["branch"] = branch
    top_k = int(clean["top_k"])
    if len(rows) > top_k:
        notes.append(f"CLAMPED 返回行数 {len(rows)}→{top_k}")
        rows = rows[:top_k]

    if rows:
        answer = contract.render_answer(scope=scope, digest=rows, as_of=as_of_iso, projects=projects)
    else:
        notes.append(f"NO_MATCH:{clean['metric']} 本次产出里没有这一项")
        answer = (f"【范围】{scope['effective_tier']}｜基准日 {as_of_iso}\n"
                  f"【无数据】{clean['metric']} 本次无产出（⛔ 当作 0）")
    contract.assert_no_banned(answer)
    return contract.envelope(ok=True, code="OK", digest=rows, notes=notes, answer=answer)


def parse_heal(env: dict[str, Any]) -> dict[str, Any]:
    """从信封 notes 里取回机器可读的自愈信息。"""
    for note in env.get("notes") or []:
        if note.startswith("heal:"):
            return json.loads(note[len("heal:"):])
    return {}
