"""契约层的三道门禁（G1 / G2 / G3）——每条都配"注入一个坏输入→必须红"的用例。

门禁⛔ 是"检查一下"，是**能在 CI 里 exit 1 的断言**。三道各挡一类真实事故：

* **G1 自描述 ⊆ 真参数面**：``describe()`` 报出去的参数名，必须**恰好等于**
  ``validate()`` 真正接受的参数名。挡的是"说明书说支持 topK、代码只认 top_k"
  ——模型照说明书调，永远调不通，而且报错信息还指向别处。
* **G2 登记枚举 == 实跑并集**（见 ``metrics.py``）：挡"加了指标忘了登记"与
  "登记了但公式没落地"两个方向。
* **G3 菜单未过期**：``contracts/cards.json``（发给宿主的菜单）必须与**当下代码**派生结果
  逐字相等。挡的是最经典的那类分叉——改了代码⛔ 重生成说明书，
  于是线上跑的是一份过期菜单。

⚠️ 三个函数都接受注入值（``live=`` / ``committed=`` / ``cards=`` / ``fields=``），
所以"门禁自己能失败"可以**在单元测试里直接证明**，⛔ 靠改磁盘源码才能测到（那是
D1 学到的"假能失败"另一半：门禁本身没被测过红，就等于没有门禁）。
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Optional

from forge.contract import cards as C
from forge.contract import intent as I
from forge.contract import metrics as M

__all__ = ["assert_describe_matches_args", "assert_registry_matches_run",
           "assert_menu_not_stale", "run_all", "main"]


class GateFailure(AssertionError):
    pass


def assert_describe_matches_args(cards: Optional[dict[str, Any]] = None,
                                 accepted: Optional[set[str]] = None) -> dict[str, Any]:
    """G1：说明书声明的参数 == 校验器真正接受的参数。"""
    declared = set((cards or C.describe())["stats"]["params"])
    accepted = accepted if accepted is not None else set(I.external_names())
    if declared != accepted:
        raise GateFailure(
            f"G1 自描述与真参数面分叉：只在说明书里有={sorted(declared - accepted)}；"
            f"只在代码里认={sorted(accepted - declared)}")
    # 上限也必须一致（⛔ 只对齐键名：说明书写 1..50、代码钳到 20 更阴险）
    top = (cards or C.describe())["stats"]["params"]["topK"]
    b = I._bounds("top_k")
    if top.get("max") != b.get("max") or top.get("min") != b.get("min"):
        raise GateFailure(f"G1 上限分叉：说明书 {top.get('min')}..{top.get('max')} "
                          f"vs 代码 {b.get('min')}..{b.get('max')}")
    return {"params": sorted(declared), "max": top.get("max")}


def assert_registry_matches_run() -> dict[str, Any]:
    """G2：注册表（AST 派生）== 计算层实跑并集。"""
    return M.assert_registry_matches_run()


def assert_menu_not_stale(cards_path: Path | str = C.CARDS_PATH,
                          live: Optional[dict[str, Any]] = None) -> dict[str, Any]:
    """G3：发给宿主的 ``cards.json`` 必须与当下代码派生结果逐字相等。"""
    path = Path(cards_path)
    if not path.exists():
        raise GateFailure(f"G3 缺菜单文件 {path}：跑 `python -m forge.contract.cards --write`")
    committed = json.loads(path.read_text(encoding="utf-8"))
    live = live if live is not None else C.describe()
    if committed != live:
        diff = sorted({k for k in set(committed) | set(live) if committed.get(k) != live.get(k)})
        raise GateFailure(f"G3 菜单已过期（代码改了⛔ 重生成）：受影响的技能={diff}")
    return {"skills": sorted(live), "bytes": path.stat().st_size}


def run_all() -> dict[str, Any]:
    return {"G1": assert_describe_matches_args(),
            "G2": assert_registry_matches_run(),
            "G3": assert_menu_not_stale()}


def main(argv: Optional[list[str]] = None) -> int:
    import sys

    try:
        res = run_all()
    except (GateFailure, M.RegistryDrift) as exc:
        print(f"[GATE FAIL] {exc}")
        return 1
    for gate, detail in res.items():
        print(f"[GATE OK] {gate}: {detail}")
    return 0


if __name__ == "__main__":
    import sys
    if hasattr(sys.stdout, "reconfigure"):   # 中文 Windows 控制台默认 cp936：
        sys.stdout.reconfigure(encoding="utf-8", errors="backslashreplace")   # 打 ⛔/✅ 会 UnicodeEncodeError
    raise SystemExit(main())
