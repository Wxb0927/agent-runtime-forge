"""guard：四档可见范围，**算不出收窄值就降级，⛔ 默认放行**。

真实系统里的形状（D1 台账）：750 个用户只有 18 条显式权限配置 ⇒
"查不到配置"是**常态**，默认策略必须显式定义，⛔ 靠"查不到就全放行"。

⚠️ 档位与收窄值是**两件事**：档位答"够不够格问"，命中行的 ``business_unit/category``
答"具体圈哪几块"。只有档位没有收窄值 ⇒ 降级为"只看本人相关"，并如实记进 ``notes``。
⚠️ 顺序是 company > bu > subline > member —— **subline 在 bu 之下**，写反会让子产品线
管理层看到整个 BU。这个坑靠 ``TIER_WEIGHT``（D1 已定）比较，⛔ 靠枚举定义顺序。
"""
from __future__ import annotations

from typing import Any, Optional

from forge.domain.models import Dataset, PermissionProfile, ScopeTier, TIER_WEIGHT
from forge.graph.state import ScopeDecision

FLOOR = ScopeTier.MEMBER


def _profile_for(ds: Dataset, user_key: str) -> Optional[PermissionProfile]:
    for p in ds.permissions:
        if p.user_key == user_key:
            return p
    return None


def _owns_task(ds: Dataset, user_key: str) -> list[str]:
    return sorted({t.project_code for t in ds.tasks if t.owner == user_key})


def visible_projects(ds: Dataset, tier: ScopeTier, business_unit: Optional[str],
                     project_type: Optional[str], user_key: str) -> tuple[list[str], str, bool]:
    """返回 (可见项目, 原因, 是否降级)。"""
    if tier is ScopeTier.COMPANY:
        return sorted(p.code for p in ds.projects), "company 档：全量", False

    if tier is ScopeTier.BU:
        if not business_unit:
            return _owns_task(ds, user_key), "bu 档但缺 business_unit 收窄值 → 降级 member", True
        got = sorted(p.code for p in ds.projects if p.business_unit == business_unit)
        return got, f"bu 档：business_unit={business_unit}", False

    if tier is ScopeTier.SUBLINE:
        if not business_unit or not project_type:
            return _owns_task(ds, user_key), "subline 档但缺收窄值 → 降级 member", True
        got = sorted(p.code for p in ds.projects
                     if p.business_unit == business_unit and p.category == project_type)
        return got, f"subline 档：{business_unit} × {project_type}", False

    return _owns_task(ds, user_key), "member 档：仅本人承担任务的项目", False


def decide(ds: Dataset, user_key: str, named: Optional[list[str]] = None) -> ScopeDecision:
    """fail-closed 判定。⛔ 抛异常中断——降级与拒答都要带着 reasons 往下走。"""
    notes: list[str] = []
    profile = _profile_for(ds, user_key)
    if profile is None:
        tier = FLOOR
        notes.append(f"用户 {user_key} 无显式权限配置 → 落到 {FLOOR.value} 地板（fail-closed）")
        business_unit = project_type = None
        downgraded = True
        reason = "无权限记录 → member 地板"
    else:
        tier = profile.tier
        business_unit, project_type = profile.business_unit, profile.project_type
        downgraded = False
        reason = ""

    projects, why, was_downgraded = visible_projects(ds, tier, business_unit, project_type, user_key)
    downgraded = downgraded or was_downgraded
    notes.append(why)
    effective = tier
    if downgraded and TIER_WEIGHT[tier] > TIER_WEIGHT[FLOOR]:
        effective = FLOOR  # 实际生效档：收窄值算不出来，就只剩本人相关

    named = sorted(set(named or []))
    known = {p.code for p in ds.projects}
    named_outside = [c for c in named if c not in set(projects)]
    unknown = [c for c in named if c not in known]
    if unknown:
        notes.append(f"点名项目不存在于项目表：{unknown}")
    if named_outside:
        notes.append(f"点名项目越权，已拒绝：{named_outside}")

    return {
        "user_key": user_key,
        "tier": tier.value if hasattr(tier, "value") else str(tier),
        "effective_tier": effective.value if hasattr(effective, "value") else str(effective),
        "business_unit": business_unit,
        "project_type": project_type,
        "visible_projects": projects,
        "named_outside": named_outside,
        "downgraded": downgraded,
        "reason": reason or why,
    }


def guard_node(state: dict[str, Any]) -> dict[str, Any]:
    """图节点包装：把 notes 汇进 reducer 通道。"""
    ds: Dataset = _dataset_from(state)
    route = state.get("route") or {}
    scope = decide(ds, state.get("user_key", ""), route.get("projects") or [])
    return {"scope": scope, "notes": [f"[guard] {r}" for r in scope_reasons(scope)]}


def scope_reasons(scope: ScopeDecision) -> list[str]:
    return [scope.get("reason")] if scope.get("reason") else []


def _dataset_from(state: dict[str, Any]) -> Dataset:
    from forge.graph.compute import dataset_for  # 延迟导入：避免 checkpoint 序列化 Dataset

    return dataset_for(int(state.get("seed", 42)))
