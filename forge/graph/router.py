"""router：意图 → 技能子集（规则版，确定性）。

⚠️ 这张关键词表**只映射到已有的分支名**（``compute.BRANCH_COMPUTE``），⛔ 另立一份指标清单——
两处写同一件事必然分叉，这是本项目的第一条硬规矩。

Line B 用规则路由（宿主驱动：框架出计划）；Line A 用模型自选工具（引擎驱动：框架跑循环）。
两边共用这里的 ``route()`` 结果做 **bind_tools 子集裁剪**——这就是生产里得出的
"只吐菜单、不给多余自由度"的主流写法：⛔ 一次把全部工具丢给模型。
"""
from __future__ import annotations

import re
from typing import Optional

from forge.domain.models import Dataset
from forge.graph.compute import BRANCH_COMPUTE
from forge.graph.state import RoutePlan

# 分支名必须是 compute 里真存在的那几个；启动即校验，⛔ 跑到一半才发现拼错
_ASSERT_SKILLS = set(BRANCH_COMPUTE)

KEYWORDS: dict[str, tuple[str, ...]] = {
    "stats": ("进度", "完成率", "逾期", "完工", "状态", "多少任务", "progress", "completion"),
    "deviation": ("偏差", "延期", "里程碑", "节点", "脱期", "deviation", "milestone"),
    "cost": ("成本", "预算", "花了", "费用", "CPI", "EAC", "超支", "cost", "budget"),
}

WRITE_KEYWORDS = ("立项", "新建项目", "创建项目", "开项", "写入", "create project", "intake")

PROJECT_RE = re.compile(r"PRJ-\d+")


def named_projects(query: str, ds: Optional[Dataset] = None) -> list[str]:
    """从问句里点名的项目码。⚠️ 抽出来是给 guard 判"点名越权"，⛔ 用它扩大可见范围。"""
    found = PROJECT_RE.findall(query)
    if ds is None:
        return sorted(set(found))
    known = {p.code for p in ds.projects}
    return sorted({c for c in found if c in known})


def route(query: str, ds: Dataset) -> RoutePlan:
    q = query.strip()
    matched = [kw for skill, kws in KEYWORDS.items() for kw in kws if kw.lower() in q.lower()]
    skills = sorted({skill for skill, kws in KEYWORDS.items()
                     if any(kw.lower() in q.lower() for kw in kws)})
    if not skills:
        # 未命中关键词 ⇒ 跑全量三条分支。⛔ 在这里发明新技能名（非法键的处理与自愈归 D3）。
        skills = sorted(_ASSERT_SKILLS)
    return {
        "skills": skills,
        "metrics": skills,  # 指标名 = 分支名，来源同一张表
        "projects": named_projects(q, ds),
        "matched": matched,
        "write_intent": any(kw.lower() in q.lower() for kw in WRITE_KEYWORDS),
    }
