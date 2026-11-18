"""技能 = **工具**，⛔ 图节点。参数 schema 由代码派生，描述由 docstring 派生。

这正是公司 `--describe` 的主流对应物：模型看到的 ``name``/``description``/``args`` 全部
从函数与 Pydantic 模型里生成，⛔ 手抄第二份（手抄的说明书必然与实现分叉）。

⚠️ 三条纪律：
1. 工具内部**必须过 guard**，⛔ 让模型自由传项目名就返回数据——越权拦截在能力侧，
   不在提示词里。
2. 工具只调用 ``compute`` 层取数，⛔ 自己做算术：**LLM 不参与数值计算**。
3. 输出是**精简摘要**（指标 + 行数），⛔ 把整表塞回上下文——生产里 74 KB 的 stdout
   会让宿主多烧一轮去读落盘文件，这叫 tool output budgeting。
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Optional

from langchain_core.tools import BaseTool, tool
from pydantic import BaseModel, Field

from forge.graph import compute
from forge.graph.guard import decide


class SkillArgs(BaseModel):
    """三个技能共用入参。``as_of`` 必须由调用方注入，⛔ 让模型猜今天几号。"""

    user_key: str = Field(description="caller identity, e.g. u0041; drives the visibility floor")
    project: Optional[str] = Field(default=None, description="project code like PRJ-1007; omit for all visible projects")
    as_of: str = Field(description="snapshot date YYYY-MM-DD; never inferred")
    seed: int = Field(default=42, description="synthetic dataset seed")


def _scope_gate(args: SkillArgs) -> tuple[Optional[dict[str, Any]], list[str]]:
    """返回 (拒答信封 or None, 可见项目列表)。越权一律不带数据返回。"""
    ds = compute.dataset_for(args.seed)
    named = [args.project] if args.project else []
    scope = decide(ds, args.user_key, named)
    if args.project and args.project in (scope.get("named_outside") or []):
        return ({"ok": False, "code": "NOT_IN_SCOPE",
                 "message": f"项目 {args.project} 不在 {scope['effective_tier']} 档可见范围内",
                 "digest": [], "notes": [f"档位={scope['tier']}，收窄值={scope.get('business_unit') or '无'}"],
                 "answer": ""}), []
    visible = scope.get("visible_projects") or []
    if args.project:
        visible = [args.project]
    if not visible:
        return ({"ok": True, "code": "NO_DATA", "message": "可见范围内没有项目",
                 "digest": [], "notes": ["无数据 ≠ 0"], "answer": ""}), []
    return None, visible


def _make(branch: str, title: str) -> BaseTool:
    doc = (f"{title}\n\n"
           f"Read-only. Returns metrics for the {branch} branch; every number carries "
           f"numerator/denominator/as_of so it can be traced back to the compute layer.")

    @tool(branch, args_schema=SkillArgs, description=doc)
    def _run(user_key: str, as_of: str, project: Optional[str] = None,
             seed: int = 42) -> dict[str, Any]:
        denied, visible = _scope_gate(SkillArgs(user_key=user_key, project=project,
                                                as_of=as_of, seed=seed))
        if denied:
            return denied
        out = compute.run_branch(branch, seed, visible, as_of)
        return {"ok": True, "code": "OK", "branch": branch, "as_of": out["as_of"],
                "projects": len(visible), "row_counts": out["row_counts"],
                "metrics": out["metrics"]}

    _run.__doc__ = doc
    return _run


TOOLS: dict[str, BaseTool] = {
    "stats": _make("stats", "Progress statistics: completion rate and overdue rate over task rows."),
    "deviation": _make("deviation", "Milestone deviation: off-schedule milestones and the worst deviation in days."),
    "cost": _make("cost", "Cost execution: per-subject budget execution, CPI (budget-comparison basis) and EAC."),
}


def tools_for(skills: Optional[list[str]] = None) -> list[BaseTool]:
    """按路由裁剪工具子集（主流的 bind_tools 用法）。未命中 → 全量三条。"""
    if not skills:
        return [TOOLS[k] for k in sorted(TOOLS)]
    return [TOOLS[s] for s in sorted(set(skills)) if s in TOOLS]


# ------------------------------------------------------------------ 写能力
# ⚠️ 写操作单独成组：Line A 的 HITL 中间件与 Line B 的 interrupt 闸都只盯这一组，
# 门口只留一个，⛔ 两处各判一次权限。


class IntakeArgs(BaseModel):
    project_name: str = Field(description="new project display name")
    customer: str = Field(description="customer label, e.g. customer-07")
    business_unit: str = Field(description="owning business unit, e.g. bu-beta")
    user_key: str = Field(description="caller identity; recorded in the audit line")
    as_of: str = Field(description="snapshot date YYYY-MM-DD; never inferred")


def write_tool(scratch_dir: str | Path) -> BaseTool:
    """绑定落盘目录后再交给图，⛔ 让模型自己挑路径（那等于绕过治理）。"""
    root = Path(scratch_dir)

    @tool("submit_intake_draft", args_schema=IntakeArgs)
    def _submit(project_name: str, customer: str, business_unit: str,
                user_key: str, as_of: str) -> dict[str, Any]:
        """Create a project intake draft. WRITE operation: requires human approval upstream."""
        from forge.graph.writes import submit_intake_draft

        return submit_intake_draft(
            {"project_name": project_name, "customer": customer,
             "business_unit": business_unit, "as_of": as_of},
            scratch_dir=root, actor=user_key, approved_by="human",
            thread_id=f"{user_key}-{as_of}")

    return _submit


def all_tools(skills: list[str] | None, scratch_dir: str | Path) -> list[BaseTool]:
    return tools_for(skills) + [write_tool(scratch_dir)]
