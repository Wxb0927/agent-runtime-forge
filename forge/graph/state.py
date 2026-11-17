"""D2 图状态：状态通道就是竞态的解法本体。

为什么这个文件要先读：原架构那起事故（统计技能与偏差技能并行写**同一份上游交接文件**，
`from`/`step` 互相覆盖、数据却是对的）本质是**共享可变状态缺隔离**，不是取数错误。
所以这里的关键不是"加锁"，而是三条分支各写自己的通道、需要合并的字段显式声明 reducer。

实测过的三种共享形状（读数见 README「D2 框架语义实测」，⛔ 凭记忆）：
1. 两个并行节点**返回同一个 channel key** → LangGraph 抛
   ``InvalidUpdateError: Can receive only one value per step``（框架把静默覆盖升级成显式报错）。
2. 两个并行节点**原地改 state 里同一个 dict** → 静默覆盖，无任何报错。
3. 两个并行节点**读-改-写同一份外部交接文件** → 静默覆盖（= 公司那起事故的原形）。
⇒ 只有 ①会被框架抓住；②③ 必须靠设计避免：⛔ 外部共享可变状态，分支输出走各自通道，
   合并交给单一 fan-in 节点。
"""
from __future__ import annotations

import operator
from typing import Annotated, Any, Optional, TypedDict

from langchain_core.messages import AnyMessage
from langgraph.graph.message import add_messages

# --------------------------------------------------------------------------
# 分支输出：来源标记 + 自己的步号 + 指标。⛔ 三条分支共用一个实例。
# --------------------------------------------------------------------------


class BranchOut(TypedDict, total=False):
    """一条并行分支的产物。

    ``from_`` / ``step`` 就是当年互相覆盖的那两个字段（脱敏后是通用词）。
    两路并发跑完后，**两个 from_ 必须都还在**——这是本天唯一的靶心断言。
    """

    from_: str
    step: int
    project: str
    metrics: list[dict[str, Any]]
    row_counts: dict[str, int]


class Metric(TypedDict, total=False):
    """指标必须可反查：数字逐字来自计算层，带分子/分母/基准日。"""

    name: str
    numerator: Optional[int]
    denominator: Optional[int]
    value: Optional[str]
    cents: Optional[int]
    as_of: str
    from_: str
    note: str


class RoutePlan(TypedDict, total=False):
    """router 产出：技能子集 + 派生的度量名。

    ⚠️ 主流做法是"按路由裁剪 bind_tools 的子集"，⛔ 一次把所有工具丢给模型——
    这与生产里得出的"只吐菜单、不给多余自由度"是同一件事。
    """

    skills: list[str]
    metrics: list[str]
    projects: list[str]
    matched: list[str]
    write_intent: bool


class ScopeDecision(TypedDict, total=False):
    """guard 产出：档位 + 收窄值 + 可见项目集。⚠️ 算不出收窄值就降级，⛔ 默认放行。"""

    user_key: str
    tier: str
    effective_tier: str
    business_unit: Optional[str]
    project_type: Optional[str]
    visible_projects: list[str]
    named_outside: list[str]
    downgraded: bool
    reason: str


class ForgeState(TypedDict, total=False):
    # —— 输入 ——
    query: str
    user_key: str
    seed: int
    as_of: str
    thread_id: str

    # —— 编排中间产物 ——
    route: RoutePlan
    scope: ScopeDecision

    # —— 三条并行分支：各自独立的通道（竞态靶子的解法本体）——
    stats: BranchOut
    deviation: BranchOut
    cost: BranchOut

    # —— 需要合并的字段：显式声明 reducer（⛔ 靠后写覆盖）——
    notes: Annotated[list[str], operator.add]

    # —— 汇聚产物 ——
    handoff: dict[str, Any]
    digest: list[Metric]
    answer: str
    envelope: dict[str, Any]
    steps_seen: Annotated[list[str], operator.add]

    # —— Line A / A2 用（ReAct 的消息轨迹）——
    messages: Annotated[list[AnyMessage], add_messages]


# 分支名 → 通道名。⚠️ 这张表是 M1 变异的靶子：改歪 = 三个分支都返回同一个 key。
BRANCH_CHANNEL: dict[str, str] = {
    "stats": "stats",
    "deviation": "deviation",
    "cost": "cost",
}

BRANCH_SOURCE: dict[str, str] = {
    "stats": "task_table",
    "deviation": "milestone_table",
    "cost": "cost_docs",
}
