"""度量注册表：合法指标枚举**从 compute 的源码 AST 里解析**，再用 G2 门禁对账实跑。

为什么⛔ 手抄一份指标清单：真实项目里说明书就是这么过期的——加指标的人改了公式、
⛔ 忘了改 `describe()`，于是模型拿着一份过期枚举去调，报错还报得理直气壮。
本模块的做法是**根本没有第二份清单**：

* ``declared_metrics()`` —— 解析 ``forge/graph/compute.py`` 的 AST，按 ``<分支>_branch``
  函数收集它们产出的指标名（``_ratio_metric("…")`` 的第一参数与 ``{"name": "…"}`` 字面量），
  带科目后缀的那类从 ``BudgetSubject`` 枚举展开 ⇒ **全部来自代码，无手抄**。
* ``produced_metrics()`` —— 在固定探针范围上**真跑一遍**，收集计算层实际输出的键。
* ``assert_registry_matches_run()`` —— **G2 门禁**：两者必须相等。
  新增指标但公式里没落地 ⇒ 红；落地了但解析不到（比如换了写法）⇒ 也红。

⚠️ 探针范围固定为 seed=42 数据集的前 60 个项目，⛔ 依赖调用者可见范围——
否则合法枚举会随权限变化，模型看到的接口就不稳定了。
"""
from __future__ import annotations

import ast
from pathlib import Path
from typing import Iterable

from forge.domain.models import BudgetSubject
from forge.graph.compute import BRANCH_COMPUTE, dataset_for, run_branch

COMPUTE_SRC = Path(__file__).resolve().parents[1] / "graph" / "compute.py"


def _literal_names(fn: ast.FunctionDef) -> tuple[list[str], list[str]]:
    """返回（平名指标, 带后缀的指标前缀）。"""
    plain: list[str] = []
    prefixed: list[str] = []
    for node in ast.walk(fn):
        # _ratio_metric("completion_rate", ...) 的第一参数
        if isinstance(node, ast.Call) and getattr(node.func, "id", None) == "_ratio_metric" \
                and node.args and isinstance(node.args[0], ast.Constant) \
                and isinstance(node.args[0].value, str):
            plain.append(node.args[0].value)
        # {"name": "eac", ...} 这种直接构造的 Metric 字典
        if isinstance(node, ast.Dict):
            for k, v in zip(node.keys, node.values):
                if isinstance(k, ast.Constant) and k.value == "name" \
                        and isinstance(v, ast.Constant) and isinstance(v.value, str):
                    plain.append(v.value)
        # f"budget_execution[{subject.value}]" → 取第一段字面量当前缀
        if isinstance(node, ast.JoinedStr) and node.values \
                and isinstance(node.values[0], ast.Constant) \
                and isinstance(node.values[0].value, str) \
                and node.values[0].value.endswith("["):
            prefixed.append(node.values[0].value[:-1])
    return plain, prefixed


def parsed_registry() -> dict[str, list[str]]:
    """{分支: [指标名…]}，全部从源码解析得到。"""
    tree = ast.parse(COMPUTE_SRC.read_text(encoding="utf-8"))
    reg: dict[str, list[str]] = {}
    for node in tree.body:
        if not isinstance(node, ast.FunctionDef) or not node.name.endswith("_branch"):
            continue
        branch = node.name[: -len("_branch")]
        if branch not in BRANCH_COMPUTE:
            continue
        plain, prefixed = _literal_names(node)
        names = sorted(set(plain))
        for base in set(prefixed):
            names.extend(f"{base}[{s.value}]" for s in BudgetSubject)
        reg[branch] = sorted(set(names))
    missing = set(BRANCH_COMPUTE) - set(reg)
    if missing:
        raise AssertionError(f"compute 里有分支解析不到指标名，注册表会静默缺一块：{sorted(missing)}")
    return reg


def declared_metrics() -> list[str]:
    return sorted({m for names in parsed_registry().values() for m in names})


def branch_of(metric: str) -> str:
    """反查指标属于哪条分支（run() 据此只跑必要分支，⛔ 全跑一遍再挑）。"""
    for branch, names in sorted(parsed_registry().items()):
        if metric in names:
            return branch
    raise KeyError(f"未登记的指标: {metric}")


def probe_projects() -> list[str]:
    return sorted(p.code for p in dataset_for(42).projects)[:60]


def produced_metrics(projects: Iterable[str] | None = None) -> list[str]:
    """实跑并集：真跑一遍收集计算层**实际输出**的键。"""
    scope = sorted(set(projects if projects is not None else probe_projects()))
    got: set[str] = set()
    for branch in sorted(BRANCH_COMPUTE):
        got.update(m["name"] for m in run_branch(branch, 42, scope)["metrics"])
    return sorted(got)


class RegistryDrift(AssertionError):
    """G2 门禁失败：登记的枚举与实跑产出不一致。"""


def assert_registry_matches_run() -> dict[str, list[str]]:
    declared, produced = set(declared_metrics()), set(produced_metrics())
    if declared != produced:
        raise RegistryDrift(
            f"度量注册表漂移：只登记未跑出来={sorted(declared - produced)}；"
            f"跑出来但⛔ 在注册表里={sorted(produced - declared)}")
    return {"declared": sorted(declared), "produced": sorted(produced)}
