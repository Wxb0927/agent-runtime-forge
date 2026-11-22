"""``describe()``：技能身份证**由代码派生**，并落成一份可 diff 的 ``contracts/cards.json``。

对齐公司那套的两根柱子：`cards.json`（菜单自吐）+ ``--describe``（由代码派生，⛔ 手抄）。
派生来源三处，⛔ 有一处是我手写第二份清单：

1. **参数形状 / 必填 / 默认值 / 上下限** ← ``SkillIntent`` 这个 pydantic 模型
   （``le``/``ge`` 从 ``FieldInfo.metadata`` 里取，⛔ 另开一张上限表）；
2. **``metric`` 的合法枚举** ← ``metrics.declared_metrics()``（AST 解析 compute 源码）；
3. **技能清单** ← ``compute.BRANCH_COMPUTE`` 的键。

G1 门禁（自描述 ⊆ 真参数面）在 ``gates.assert_describe_matches_args``：
卡片声明的参数必须**恰好等于** ``SkillIntent`` 真接受的参数，
差一个就红——这正是当年"描述说支持三个参数、实跑只认两个"那类事故的防线。

⚠️ ``cards.json`` 是**生成物**（像锁文件），改模型后必须 ``--write`` 重生成；
忘了重生成 ⇒ G1/漂移门禁红，而不是悄悄给模型一份过期说明书。
"""
from __future__ import annotations

import argparse
import json
from datetime import date
from pathlib import Path
from typing import Any, Optional

import inspect

from pydantic import BaseModel, ConfigDict, Field

from forge.contract import metrics as M
from forge.graph.compute import BRANCH_COMPUTE

CARDS_PATH = Path("contracts") / "cards.json"

# 上限写在字段元数据里 ⇒ describe() 与校验器共用同一个数，⛔ 两处各写一遍
TOP_K_MAX = 50


class SkillIntent(BaseModel):
    """**意图面**：只放"模型该填的键"。

    ⚠️ 身份与基准日**⛔ 属于这里**——那是宿主注入项（见 ``describe()["host_injected"]``）：
    让模型填 ``user_key`` 等于让它猜权限，让模型填 ``as_of`` 等于让它猜今天几号。
    对外形状用 camelCase（与公司那套 CLI 的 ``topK`` 一致），Python 内部仍 snake_case。
    """

    model_config = ConfigDict(populate_by_name=False, extra="forbid")

    metric: str = Field(description="要查的指标名，合法值见 enums.metric（由代码派生）")
    project: Optional[str] = Field(default=None, description="项目码，如 PRJ-1180；省略=全部可见项目")
    top_k: int = Field(default=10, ge=1, le=TOP_K_MAX, alias="topK",
                       description=f"最多返回几行，1..{TOP_K_MAX}；越界**钳位**并说明，⛔ 报错")


def alias_of(pname: str) -> str:
    """Python 名 → 对外名（alias）。说明书必须报对外形状，⛔ 报内部命名。"""
    f = SkillIntent.model_fields[pname]
    return f.alias or pname


def host_injected() -> dict[str, Any]:
    """宿主注入项：**从 run() 的函数签名派生**，⛔ 再抄一份字段清单。"""
    from forge.contract.intent import run

    out: dict[str, Any] = {}
    for name, prm in inspect.signature(run).parameters.items():
        if name == "intent":
            continue
        out[name] = {"default": None if prm.default is inspect._empty else prm.default,
                     "note": "由宿主注入，⛔ 由模型填写"}
    return out


def _bounds(field: Field) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for meta in field.metadata:               # Annotated[..., Le(50)] 落在这里
        le = getattr(meta, "le", None)
        ge = getattr(meta, "ge", None)
        if le is not None:
            out["max"] = le
        if ge is not None:
            out["min"] = ge
    return out


def describe(skill: Optional[str] = None) -> dict[str, Any]:
    """返回 {skill: 卡片}，或指定技能的单张卡片。"""
    schema = SkillIntent.model_json_schema(by_alias=True)
    cards: dict[str, Any] = {}
    for branch in sorted(BRANCH_COMPUTE):
        cards[branch] = {
            "name": branch,
            "summary": (BRANCH_COMPUTE[branch].__doc__ or "").strip().splitlines()[0],
            "params": {
                alias_of(pname): {
                    "type": (props.get("anyOf") or [props])[0].get("type", props.get("type", "string"))
                    if "anyOf" in props else props.get("type", "string"),
                    "required": pname in schema.get("required", []),
                    "description": props.get("description", ""),
                    **({"default": props["default"]} if "default" in props else {}),
                    **_bounds(SkillIntent.model_fields[pname]),
                }
                for pname, props in zip(SkillIntent.model_fields, schema["properties"].values())
            },
            "host_injected": host_injected(),
            "enums": {
                "metric": [m for m in M.declared_metrics() if M.branch_of(m) == branch],
                "skill": sorted(BRANCH_COMPUTE),
            },
            "derived_from": {
                "params": "forge/contract/cards.py::SkillIntent",
                "enums.metric": "AST(forge/graph/compute.py) + domain.models.BudgetSubject",
                "skills": "forge/graph/compute.py::BRANCH_COMPUTE",
            },
        }
    return cards if skill is None else {skill: cards[skill]}


def cards_json() -> str:
    return json.dumps(describe(), ensure_ascii=False, sort_keys=True, indent=1) + "\n"


def write_cards(path: Path = CARDS_PATH) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(cards_json(), encoding="utf-8", newline="")
    return path


def load_committed_cards(path: Path = CARDS_PATH) -> dict[str, Any]:
    if not path.exists():
        raise AssertionError(f"缺 {path}：先跑 `python -m forge.contract.cards --write` 生成菜单")
    return json.loads(path.read_text(encoding="utf-8"))


def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(prog="forge.contract.cards",
                                 description="技能自描述（--describe）：从代码派生，⛔ 手抄")
    ap.add_argument("skill", nargs="?", default=None, help="只看某个技能")
    ap.add_argument("--write", action="store_true", help="把全量卡片写进 contracts/cards.json")
    a = ap.parse_args(argv)
    if a.write:
        p = write_cards()
        print(f"已写出 {p}（{len(describe())} 张卡片，指标枚举 {len(M.declared_metrics())} 个）")
        return 0
    print(json.dumps(describe(a.skill), ensure_ascii=False, indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
