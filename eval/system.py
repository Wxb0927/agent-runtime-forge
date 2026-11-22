"""被测系统（SUT）适配器：自然语言问句 → 信封 + 可反查的旁证。

⚠️ 评测要有**独立预言（oracle）**：⛔ 拿系统自己的输出当期望值，
那只能证明"它和它自己一致"。这里每条指标都由计算层**再算一遍**
（``forge.graph.compute.run_branch``，与图内调用同一函数、同一入参），
两套结果对不上就是问题。
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Optional

from forge.graph import router as router_mod
from forge.graph.compute import AS_OF, dataset_for, run_branch
from forge.graph.plan_graph import PLAN_GRAPH

DEFAULT_USER = "u0000"


@dataclass
class Run:
    query: str
    user_key: str
    as_of: str
    envelope: dict[str, Any]
    skills: list[str] = field(default_factory=list)       # 路由实际拉起的技能
    needed_skills: list[str] = field(default_factory=list)  # 目标指标真正需要的技能
    scope_projects: list[str] = field(default_factory=list)
    steps: list[str] = field(default_factory=list)

    @property
    def digest(self) -> list[dict[str, Any]]:
        return self.envelope.get("digest") or []

    def metric(self, name: str) -> Optional[dict[str, Any]]:
        for m in self.digest:
            if m.get("name") == name:
                return m
        return None


def ask(query: str, *, user_key: str = DEFAULT_USER, as_of: str = AS_OF.isoformat(),
        seed: int = 42, thread_id: str = "eval") -> Run:
    """跑一次端到端（Line B：router → guard → 三分支并行 → fanin → aggregate）。"""
    out = PLAN_GRAPH.invoke({"query": query, "user_key": user_key, "seed": seed,
                             "as_of": as_of, "thread_id": thread_id})
    route = out.get("route") or {}
    scope = out.get("scope") or {}
    named = route.get("projects") or []
    return Run(
        query=query, user_key=user_key, as_of=as_of,
        envelope=out.get("envelope") or {},
        skills=list(route.get("skills") or []),
        scope_projects=list(named or scope.get("visible_projects") or []),
        steps=list(out.get("steps_seen") or []),
    )


def oracle(metric: str, projects: list[str], as_of: str, seed: int = 42) -> dict[str, Any]:
    """独立重算：绕开图，直接问计算层要这一条指标。"""
    from forge.contract import metrics as M

    branch = M.branch_of(metric)
    rows = run_branch(branch, seed, projects, as_of)["metrics"]
    for r in rows:
        if r["name"] == metric:
            return r
    raise LookupError(f"计算层没有产出 {metric}（探针范围里缺这一项）")


def needed_skill(metric: str) -> str:
    from forge.contract import metrics as M

    return M.branch_of(metric)


def run_case(case: dict[str, Any]) -> Run:
    """按语料的 mode 派发到两条被测面：自然语言（Line B）与契约层（``--intent``）。

    ⚠️ 两面都要评：只评自然语言那条，契约层的非法参数行为就⛔ 进不了回归差集。
    """
    mode, inp = case.get("mode", "nl"), case.get("input") or {}
    if mode == "nl":
        return ask(inp.get("query", ""), user_key=inp.get("user_key", DEFAULT_USER))
    if mode == "intent":
        from forge.contract import intent as I

        host: dict[str, Any] = {"user_key": DEFAULT_USER, "as_of": AS_OF.isoformat()}
        host.update(inp.get("host") or {})
        env = I.run(dict(inp.get("intent") or {}), **host)
        metric = (inp.get("intent") or {}).get("metric")
        skills = [needed_skill(metric)] if metric in (oracle_domains()) else []
        return Run(query=json.dumps(inp.get("intent") or {}, ensure_ascii=False),
                   user_key=str(host.get("user_key")), as_of=str(host.get("as_of")),
                   envelope=env, skills=skills, needed_skills=skills,
                   scope_projects=[inp["intent"]["project"]] if (inp.get("intent") or {}).get("project")
                   else sorted(p.code for p in dataset_for(42).projects)[:60])
    raise ValueError(f"未知 mode：{mode}")


def oracle_domains() -> list[str]:
    from forge.contract import metrics as M

    return M.declared_metrics()
