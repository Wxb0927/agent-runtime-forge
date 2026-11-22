"""RAG 语料：**规则库 + 模板库**，全部从本仓库已有的口径与常量派生。

为什么⛔ 手写一堆"制造业知识"当语料：那样评测出来的是"检索器能不能找到我编的话"，
与真实业务无关，而且很容易顺手把雇主的真实字段名/系统名带进公开仓库
（`tests/test_d2_hygiene.py` 的禁词表就是为拦这个）。

两类语料都有代码出处：

* **规则库**：预算超支判据（逐科目预警阈值）、成本加总口径（出库单据只展示不计入）、
  回款四阶段、逾期率分母口径、CPI 的真实含义（预算对比⛔ 挣值）、"无数据 ≠ 0"、
  合同表⛔ 反查项目金额、档位与收窄值分离、基准日由调用方注入……
  逐条来自 `forge/domain/models.py` 的校验器与 `forge/domain/formulas.py` 的 docstring。
* **模板库**：WBS 四层对象、非标装备典型工序链、交付物清单模板、各业务线立项必填布局。

每条文档 = `id / title / body / source(代码出处) / kind`；`source` 让"正确引用率"可判定。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from forge.domain.models import BudgetSubject, DeliverableKind, PaymentStage, TaskKind
from forge.graph.intake_graph import BU_LAYOUTS, REQUIRED_FIELDS


@dataclass(frozen=True)
class Doc:
    id: str
    title: str
    body: str
    source: str                      # 代码出处，让引用可核验
    kind: str = "rule"               # rule | template（规则库占多数，给默认值）
    members: tuple[str, ...] = ()    # 章 → 原条目 id，QA 的 gold 靠它映射
    tags: tuple[str, ...] = field(default_factory=tuple)

    def as_dict(self) -> dict[str, Any]:
        return {"id": self.id, "title": self.title, "body": self.body,
                "source": self.source, "kind": self.kind, "tags": list(self.tags),
                "members": list(self.members)}

    @property
    def sections(self) -> list[str]:
        """章节级分块的天然边界：标题 + 每条编号子句。"""
        return [self.title] + [ln.strip() for ln in self.body.split("；") if ln.strip()]


def _pct(bps: int) -> str:
    return f"{bps / 100:.0f}%"


def rule_docs() -> list[Doc]:
    docs: list[Doc] = []

    for subject in BudgetSubject:
        docs.append(Doc(
            id=f"rule-budget-{subject.value}",
            title=f"预算超支判据 · {subject.value}",
            body=(f"科目 {subject.value} 的预警阈值按系统配置逐科目取，缺省 {_pct(7000)}；"
                  "⛔ 拍一个全局阈值套所有科目。预算执行率 = 已发生 ÷ 该科目预算；"
                  "预算为 0 时结果是「无数据」，⛔ 返回 0"),
            source="forge/domain/models.py::BudgetLine.warning_ratio_bps；"
                   "forge/domain/formulas.py::budget_execution_rate",
            tags=("预算", subject.value, "超支"),
        ))

    docs += [
        Doc(id="rule-cost-scope",
            title="实际成本的加总口径",
            body="实际总成本 = 采购（−退货）+ 内部人工 + 外部人工 + 费用；"
                 "出库物料成本仅展示、⛔ 参与加总；形态转换也⛔ 计入",
            source="forge/domain/formulas.py::actual_cost_cents；forge/domain/models.py::CostDocKind",
            tags=("成本", "口径")),
        Doc(id="rule-cpi-meaning",
            title="本系统里的 CPI 是什么口径",
            body="CPI = 已发生 ÷ 预算总额，是「预算对比口径」；"
                 "挣值管理里的 PV/EV/AC 三件套是另一个能力，本系统未实现，⛔ 混用。"
                 "被问「CPI 怎么算的」时必须先说清这一点",
            source="forge/domain/formulas.py::cpi_cents",
            tags=("成本", "CPI", "口径")),
        Doc(id="rule-eac-zero",
            title="EAC 在零发生额时必须弃权",
            body="EAC 按当前执行率外推；已发生额为 0 或预算为 0 ⇒ 结果是「无数据」，"
                 "⛔ 折算成零金额。真实项目里「外推出一个数」是最容易被业务方当场推翻的一类错",
            source="forge/domain/formulas.py::eac_cents",
            tags=("成本", "EAC", "弃权")),
        Doc(id="rule-overdue-denominator",
            title="逾期率的分母只算应完成任务",
            body="逾期率 = 逾期任务数 ÷ 「计划上到基准日应完成」的任务数，"
                 "⛔ 除以全部任务数（那会把还没开工的项目算成不逾期，指标必然偏乐观）",
            source="forge/domain/formulas.py::overdue_rate；expected_to_finish",
            tags=("进度", "逾期", "口径")),
        Doc(id="rule-no-data-not-zero",
            title="无数据与 0 是两种状态",
            body="分母为空或数据缺失时返回「无数据」，并在成品正文里点名是哪一项；"
                 "⛔ 静默填 0，也⛔ 显示成零金额——业务方会把 0 读成「确实没花钱」",
            source="forge/domain/formulas.py 模块 docstring；forge/graph/contract.py",
            tags=("口径", "无数据")),
        Doc(id="rule-contract-sparse",
            title="⛔ 用合同表反查项目金额",
            body="合同与项目的关联极稀疏（合成数据里 3/94，与真实系统同形），"
                 "所以合同金额一律以计划表为主源，⛔ 通过合同表反查，也⛔ 把空值当 0 求和",
            source="forge/domain/models.py::Contract；forge/synth/generate.py",
            tags=("回款", "合同", "数据质量")),
        Doc(id="rule-plan-window",
            title="没有「实际工期」这个字段",
            body="真实系统里实际开工日 = 实际完工日（完成时回填），只保留实际结束日且大量为空；"
                 "任何基于「实际工期」的指标都是假的，所以模型层就⛔ 给这个字段",
            source="forge/domain/models.py::Project（无 actual_start）",
            tags=("进度", "口径", "否决")),
        Doc(id="rule-scope-tier-order",
            title="可见范围四档的先后顺序",
            body="company > bu > subline > member —— **subline 在 bu 之下**，"
                 "写反会让子产品线管理层看到整个 BU。档位答「够不够格问」，"
                 "命中行的业务线与类别答「具体圈哪几块」；算不出收窄值 ⇒ 降级为只看本人相关，"
                 "⛔ 默认放行",
            source="forge/domain/models.py::ScopeTier/TIER_WEIGHT；forge/graph/guard.py",
            tags=("权限", "档位", "fail-closed")),
        Doc(id="rule-llm-no-math",
            title="大模型⛔ 参与数值计算",
            body="所有业务算术在代码层完成并带分子/分母/基准日，模型只负责把成品正文逐字转述；"
                 "派生量必须可反查，否则一句「看起来合理的数」就能翻车",
            source="forge/domain/formulas.py 模块 docstring；forge/graph/contract.py",
            tags=("防幻觉", "口径")),
        Doc(id="rule-asof-injected",
            title="基准日由调用方注入",
            body="任何日期敏感计算必须显式接收基准日，⛔ 读系统时钟——否则「改动前后跑同一批语料」"
                 "必然出现假差异，差额恰好等于没有实际结束日的那批行",
            source="forge/domain/formulas.py 模块 docstring；forge/graph/compute.py::AS_OF",
            tags=("可复现", "口径")),
        Doc(id="rule-intake-required",
            title="立项必填与写前确认",
            body=f"立项必填字段：{'、'.join(REQUIRED_FIELDS)}；"
                 "缺任一项 ⇒ 即使人工批准也⛔ 落库，返回「未完成」而不是写占位符；"
                 "写操作前必须停在人工闸上",
            source="forge/graph/intake_graph.py::REQUIRED_FIELDS/n_confirm",
            tags=("立项", "校验", "HITL")),
        Doc(id="rule-agent-token-bypass",
            title="能力令牌绕过记录级权限时的处置",
            body="平台侧的能力令牌是方法转发器，⛔ 保证记录级隔离；"
                 "发现绕过后，权限判定必须落在被调方（技能侧）+ 审计留痕，"
                 "⛔ 相信调用方自觉，也⛔ 只在前端隐藏入口",
            source="forge/graph/guard.py（fail-closed 与越权点名）",
            tags=("权限", "审计", "异常处理")),
        Doc(id="rule-ambiguity-fallback",
            title="说法模糊时的降级路径",
            body="关键词⛔ 命中任何技能时，路由回退到全技能一趟出计划；"
                 "⛔ 用分词猜用户要哪个指标、⛔ 把猜测结果当成命中——猜出来的数字最像真的，也最危险",
            source="forge/graph/router.py::route；forge/contract/intent.py（⛔ 模糊匹配）",
            tags=("路由", "降级", "异常处理")),
    ]

    stages = "/".join(s.value for s in PaymentStage)
    for stage in PaymentStage:
        docs.append(Doc(
            id=f"rule-payment-{stage.value}",
            title=f"回款阶段 · {stage.value}",
            body=(f"回款分四阶段：{stages}；当前阶段 {stage.value} 的计划金额大量为空"
                  "（合成数据 8% 有值，与真实系统同形），缺失时按「无数据」处理，"
                  "⛔ 当 0 计入完成率"),
            source="forge/domain/models.py::PaymentStage/PaymentNode",
            tags=("回款", stage.value),
        ))
    return docs


def template_docs() -> list[Doc]:
    docs: list[Doc] = [
        Doc(id="tpl-wbs-levels",
            title="WBS 四层对象",
            body=f"一张任务表同时承载四类对象：{'/'.join(k.value for k in TaskKind)}，"
                 "靠类型谓词区分，⛔ 拆成四张表（真实系统就是这么存的，拆表会让统计口径分叉）",
            source="forge/domain/models.py::TaskKind/Task.wbs_level",
                 kind="template",
            tags=("WBS", "模板")),
        Doc(id="tpl-equipment-chain",
            title="非标装备项目的典型工序链",
            body="立项 → 设计（含工程变更）→ 采购 → 装配 → 调试 → 验收 → 回款；"
                 "每阶段挂里程碑，交付物按阶段清点；工程变更后需重排后续工序并复核关键路径。"
                 "此条是通用行业流程，⛔ 含任何企业内部定义",
            source="forge/domain/models.py::EcnChange/TaskKind（流程骨架为通用常识）",
                 kind="template",
            tags=("流程", "模板")),
        Doc(id="tpl-deliverables",
            title="交付物清单模板",
            body=f"每项目固定 10 项交付物，类型含 {'/'.join(k.value for k in DeliverableKind)}；"
                 "典型状态下只交付 0–3 项，所以「齐套」类指标本期⛔ 做（分母太稀疏，"
                 "算出来必然误导）",
            source="forge/domain/models.py::Deliverable；forge/synth/generate.py",
                 kind="template",
            tags=("交付物", "模板")),
        Doc(id="tpl-no-actual-duration",
            title="工期模板里⛔ 放实际工期字段",
            body="模板只定义计划起止与里程碑；实际开工日⛔ 单独录（完成时回填等于实际完工日），"
                 "所以任何「实际工期」派生指标都⛔ 进模板",
            source="forge/domain/models.py::Project/Task 字段集",
                 kind="template",
            tags=("WBS", "模板", "口径")),
    ]
    for bu, layout in sorted(BU_LAYOUTS.items()):
        docs.append(Doc(
            id=f"tpl-intake-{bu}",
            title=f"立项布局 · {bu}",
            body=(f"业务线 {bu} 的立项除通用必填外还要求：{'、'.join(layout) or '（无额外项）'}；"
                  "缺项时闸口必须拦，⛔ 用占位符填空后落库"),
            source="forge/graph/intake_graph.py::BU_LAYOUTS",
                 kind="template",
            tags=("立项", bu, "模板"),
        ))
    return docs


def corpus() -> list[Doc]:
    return rule_docs() + template_docs()


def corpus_dicts() -> list[dict[str, Any]]:
    return [d.as_dict() for d in corpus()]

#: 章节主题 → 归入该章的标签。把口径**合并成章**才有分块对照的意义：
#: 单条口径只有一百来字，固定长度切与章节切都只会切出 1 块，比了等于⛔ 比。
CHAPTERS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("成本与预算口径", ("成本", "预算", "CPI", "EAC", "超支")),
    ("进度与工期口径", ("进度", "逾期", "WBS", "口径", "否决", "可复现")),
    ("回款与合同口径", ("回款", "合同", "账龄", "数据质量")),
    ("权限与审计", ("权限", "档位", "审计", "fail-closed", "弃权")),
    ("立项与写操作", ("立项", "校验", "HITL", "模板")),
    ("流程与交付物", ("流程", "交付物", "路由", "降级", "异常处理")),
)


def chapters(docs: list[Doc] | None = None) -> list[Doc]:
    """按主题合并成章：每章 = 标题 + 归入它的口径逐条串接（章节内保留 `；` 边界）。"""
    docs = docs or corpus()
    out: list[Doc] = []
    used: set[str] = set()
    for title, keys in CHAPTERS:
        picked = [d for d in docs if set(d.tags) & set(keys) and d.id not in used]
        if not picked:
            continue
        used.update(d.id for d in picked)
        body = "；".join(f"{d.title}：{d.body}" for d in picked)
        out.append(Doc(
            id=f"ch-{len(out) + 1:02d}", title=title, body=body,
            source="由下列条目合并：" + ", ".join(d.id for d in picked),
            kind=picked[0].kind, tags=(title,) + keys,
            members=tuple(d.id for d in picked)))
    leftover = [d for d in docs if d.id not in used]
    if leftover:      # ⛔ 悄悄丢语料：没归进章的一律单独成章，保证语料总量可核对
        for d in leftover:
            out.append(d)
    return out


def doc_to_chapter() -> dict[str, str]:
    """原口径 id → 所在章 id（QA 的 gold 文档靠它落到可检索的块上）。"""
    m: dict[str, str] = {}
    for ch in chapters():
        for mid in (ch.members or (ch.id,)):
            m[mid] = ch.id
    return m
