"""确定性合成数据生成器（§3.5 规模与稀疏性）。

⚠️ 两个刻意的设计：
1. **规模用同余步长而不是随机抽样**。`random.sample` 的稳定性依赖 Python 版本实现细节，
   而同余计数只要步长与模数互质就是**可证明的精确值**。这让"同一 seed 两次逐字节相同"
   和"稀疏比例恰好是 8%/90%"都变成硬事实，⛔ 近似值。
2. **稀疏性不许填满**。payment_plan 只有 8% 带计划金额、ECN 成本 90% 为 0、交付物每项目
   只交付 0–3 项、750 用户只有 18 条权限配置、94 份合同只有 3 份关联到项目。
   这些形状正是逼出"金额以计划表为主源""默认放行策略必须显式定义"的原因；
   测试里会把它们钉死，防止后来者为了"数据好看"把它抹平。
"""
from __future__ import annotations

import argparse
import json
import random
from datetime import date, timedelta
from pathlib import Path

from forge.domain.models import (
    AppUser,
    BudgetLine,
    BudgetSubject,
    BudgetVersion,
    Contract,
    CostDoc,
    CostDocKind,
    Dataset,
    Deliverable,
    DeliverableKind,
    EcnChange,
    Issue,
    IssueStatus,
    PaymentNode,
    PaymentStage,
    PaymentStatus,
    PermissionProfile,
    Project,
    ProjectStatus,
    ScopeTier,
    Task,
    TaskDep,
    TaskKind,
    TaskState,
)

# ---- 规模常量（§3.5）----
N_PROJECTS = 300
N_TASKS_PER_PROJECT = 40  # 4 阶段 + 2 里程碑 + 8 工序 + 26 任务 → 合计 12,000
PHASES, MILESTONES, OPERATIONS = 4, 2, 8
TASKS_PLAIN = N_TASKS_PER_PROJECT - PHASES - MILESTONES - OPERATIONS
BU_COUNTS = [180, 90, 10, 10]  # 其余 10 个刻意"未填"
N_PAYMENT = 2300
PAYMENT_WITH_AMOUNT_PER_CYCLE = 2  # 步长 25 → 184/2300 = 8.00%
PAYMENT_STRIDE = 25
N_ECN = 120
ECN_NONZERO_PER_CYCLE = 1  # 步长 10 → 12/120 非零 ⇒ 90% 为 0
ECN_STRIDE = 10
N_ISSUE = 400
N_CONTRACT = 94
CONTRACT_LINKED = 3  # 真实数据里合同↔项目几乎没关联
N_USERS = 750
N_PERMISSIONS = 18  # ⚠️ 750 用户只有 18 条显式配置 ⇒ 默认策略必须显式定义
DELIVERABLES_PER_PROJECT = 10
N_COST = {"purchase": 700, "shipment": 700, "return": 12, "expense": 40, "labor_report": 615, "form_conversion": 0}
LABOR_REPORT_WITH_HOURS = 9  # 真实读数：报工工时有值 9/615
AS_OF = date(2026, 10, 30)  # 快照基准日；⛔ 读系统时钟

# 逾期形状（同余精确计数，见模块 docstring）
OVERDUE_NOT_STARTED_MOD, OVERDUE_NOT_STARTED_LT = 60, 7  # 12000*7/60 = 1400
DELAYED_IN_PROGRESS_MOD, DELAYED_IN_PROGRESS_LT = 600, 3  # 12000*3/600 = 60
DONE_MOD, DONE_LT = 60, 35  # 其余按 35/60 判完成

BUS = ["bu-alpha", "bu-beta", "bu-gamma", "bu-delta"]


def _hit(index: int, mult: int, add: int, mod: int, lt: int) -> bool:
    """(index*mult + add) % mod < lt —— mult 与 mod 互质时，完整周期内计数精确。"""
    return (index * mult + add) % mod < lt


def _bu_for(i: int) -> tuple[str | None, int]:
    """前 180 个 alpha、接 90 个 beta、再 10/10 给 gamma/delta，最后 10 个未填。"""
    start = 0
    for bi, cnt in enumerate(BU_COUNTS):
        if start <= i < start + cnt:
            return BUS[bi], bi
        start += cnt
    return None, -1  # 未填


def _plan_window(i: int) -> tuple[date, date]:
    base = date(2025, 3, 3)
    start = base + timedelta(days=(i % 200) * 3)
    end = start + timedelta(days=10 + (i % 17) * 4)
    return start, end


def build_dataset(seed: int, as_of: date = AS_OF) -> Dataset:
    # ⚠️ 分工明确：**形状用同余算术**（保证 1400/60/184/12/18 这类计数是精确值而不是近似），
    #    **内容抖动用 seeded RNG**（让 --seed 真的改变数据，且仍然逐字节可复现）。
    rng = random.Random(seed)
    projects: list[Project] = []
    for i in range(N_PROJECTS):
        bu, _ = _bu_for(i)
        start = date(2025, 3, 3) + timedelta(days=(i % 200) * 3)
        end = start + timedelta(days=60 + (i % 23) * 5)
        status_cycle = i % 6
        status = list(ProjectStatus)[status_cycle]
        # ⚠️ actual_end 只在已交付/结案上存在，且真实数据里它等于"回填日"——
        #    所以模型层⛔ 提供 actual_start，任何"实际工期"口径都不成立。
        actual_end = end + timedelta(days=(i % 11)) if status in (ProjectStatus.DELIVERED, ProjectStatus.CLOSED) else None
        projects.append(
            Project(
                code=f"PRJ-{1000 + i}",
                name=f"line-{i:03d} {rng.choice(['conveyor', 'cell', 'fixture', 'packer'])} project",
                customer=f"customer-{rng.randrange(37):02d}",
                business_unit=bu,
                category=rng.choice(["delivery", "presale", "spare_part", "after_sale"]),
                status=status,
                plan_start=start,
                plan_end=end,
                actual_end=actual_end,
            )
        )

    tasks: list[Task] = []
    deps: list[TaskDep] = []
    idx = 0
    for p in projects:
        pi = int(p.code.split("-")[1]) - 1000
        kinds = ([TaskKind.PHASE] * PHASES + [TaskKind.MILESTONE] * MILESTONES
                 + [TaskKind.OPERATION] * OPERATIONS + [TaskKind.TASK] * TASKS_PLAIN)
        parent = None
        for k, kind in enumerate(kinds):
            tid = f"{p.code}-T{k:02d}"
            plan_start, plan_end = _plan_window(idx)
            overdue_ns = _hit(idx, 7, 3, OVERDUE_NOT_STARTED_MOD, OVERDUE_NOT_STARTED_LT)
            delayed_ip = _hit(idx, 11, 5, DELAYED_IN_PROGRESS_MOD, DELAYED_IN_PROGRESS_LT)
            if overdue_ns:
                # 逾期未开工：计划期整体在过去，状态仍未开始
                plan_start = as_of - timedelta(days=90 + (idx % 13))
                plan_end = as_of - timedelta(days=7 + (idx % 30))
                state = TaskState.NOT_STARTED
                actual_end = None
            elif delayed_ip:
                plan_start = as_of - timedelta(days=45 + (idx % 9))
                plan_end = as_of - timedelta(days=1 + (idx % 20))
                state = TaskState.IN_PROGRESS
                actual_end = None
            elif _hit(idx, 13, 1, DONE_MOD, DONE_LT) or plan_end < as_of:
                # ⚠️ 兜住"计划期已过但没被划进逾期形状"的任务：它们必须算完成，
                #    ⛔ 留在进行中——否则"延期进行中恰好 60 条"这个精确计数就被污染了。
                state = TaskState.DONE
                actual_end = plan_end + timedelta(days=(idx % 9) - 4)
            elif plan_start <= as_of:
                state = TaskState.IN_PROGRESS
                actual_end = None
            else:
                state = TaskState.NOT_STARTED
                actual_end = None
            is_ms = kind is TaskKind.MILESTONE
            tasks.append(
                Task(
                    task_id=tid,
                    project_code=p.code,
                    name=f"{kind.value}-{k:02d}",
                    plan_type=kind,
                    state=state,
                    wbs_level=1 if kind is TaskKind.PHASE else 2,
                    parent_id=parent if kind is not TaskKind.PHASE else None,
                    is_milestone=is_ms,
                    owner=f"u{(idx % N_USERS):04d}" if not is_ms or idx % 3 else None,
                    plan_start=plan_start,
                    plan_end=plan_end,
                    actual_end=actual_end,
                    estimated_hours=0 if is_ms else 8 + (idx % 41),
                )
            )
            if kind is TaskKind.PHASE:
                parent = tid
            if k > 0 and kind is TaskKind.OPERATION:
                # 依赖边必须标注来源：doc = 规格给定，inferred = 由真实调用链反推
                deps.append(TaskDep(predecessor_id=f"{p.code}-T{k-1:02d}", successor_id=tid,
                                    kind="finish_to_start", source="inferred" if k % 4 else "doc"))
            idx += 1
    assert len(tasks) == N_PROJECTS * N_TASKS_PER_PROJECT == 12000, len(tasks)

    versions: list[BudgetVersion] = []
    lines: list[BudgetLine] = []
    subjects = list(BudgetSubject)
    for i, p in enumerate(projects):
        versions.append(BudgetVersion(project_code=p.code, version=1, is_current=True,
                                      approved_on=p.plan_start + timedelta(days=i % 7)))
        for s_idx, subject in enumerate(subjects):
            budget = 1_000_00 + rng.randrange(8_000_00)
            # 刻意留 1/50 的零预算科目，逼出"执行率无数据 ≠ 0%"
            if (i + s_idx) % 50 == 0:
                budget = 0
            incurred = int(budget * rng.uniform(0.3, 1.2)) if budget else 0
            lines.append(BudgetLine(project_code=p.code, version=1, subject=subject,
                                    budget_cents=budget, incurred_cents=incurred,
                                    warning_ratio_bps=[7000, 8500, 500][ (i * 4 + s_idx) % 3 ]))

    cost_docs: list[CostDoc] = []
    for kind_name, count in N_COST.items():
        for j in range(count):
            p = projects[(hash_order(j, kind_name)) % N_PROJECTS]
            amount = 0
            if kind_name == "labor_report":
                # 真实读数：615 条报工里只有 9 条有值 ⇒ 人工成本绝不能默认 0
                amount = (50_000 + j * 1_300) if j < LABOR_REPORT_WITH_HOURS else 0
            else:
                amount = 12_000 + (j * 7_717) % 4_000_000
            cost_docs.append(CostDoc(doc_no=f"CD-{kind_name[:3].upper()}-{j:04d}", project_code=p.code,
                                     kind=CostDocKind(kind_name), amount_cents=amount,
                                     occurred_on=date(2025, 4, 1) + timedelta(days=(j * 3) % 500)))

    payments: list[PaymentNode] = []
    stages = list(PaymentStage)
    for j in range(N_PAYMENT):
        p = projects[j % N_PROJECTS]
        stage = stages[j % 4]
        due = date(2025, 5, 1) + timedelta(days=(j * 11) % 700)
        has_amount = _hit(j, 13, 2, PAYMENT_STRIDE, PAYMENT_WITH_AMOUNT_PER_CYCLE)
        status_cycle = j % 4
        status = [PaymentStatus.UNPAID, PaymentStatus.PAID, PaymentStatus.OVERDUE_UNPAID, PaymentStatus.OVERDUE_PAID][status_cycle]
        paid_on = due + timedelta(days=(j % 21) - 10) if status in (PaymentStatus.PAID, PaymentStatus.OVERDUE_PAID) else None
        payments.append(PaymentNode(node_id=f"PN-{j:05d}", project_code=p.code, stage=stage, due_on=due,
                                    status=status, plan_amount_cents=(900_000 + (j * 6_113) % 90_000_00) if has_amount else None,
                                    actual_amount_cents=(800_000 + (j * 5_077) % 90_000_00) if paid_on else None,
                                    paid_on=paid_on))

    contracts = [Contract(contract_no=f"CT-{j:04d}", customer=f"customer-{j % 37:02d}",
                          amount_cents=2_000_000 + (j * 131_131) % 90_000_000,
                          project_code=projects[j * 7 % N_PROJECTS].code if j < CONTRACT_LINKED else None)
                 for j in range(N_CONTRACT)]

    ecns = [EcnChange(ecn_id=f"ECN-{j:04d}", project_code=projects[j % N_PROJECTS].code,
                      reason=["drawing_change", "material_substitute", "customer_request", "process_fix"][j % 4],
                      method=["rework", "replace", "scrap"][j % 3],
                      responsible_dept=["design", "purchase", "assembly"][j % 3],
                      cost_cents=(30_000 + j * 9_091) if _hit(j, 3, 1, ECN_STRIDE, ECN_NONZERO_PER_CYCLE) else 0,
                      raised_on=date(2025, 6, 1) + timedelta(days=(j * 5) % 400))
            for j in range(N_ECN)]

    issues = [Issue(issue_id=f"ISS-{j:04d}", project_code=projects[(j * 3) % N_PROJECTS].code,
                  status=list(IssueStatus)[j % 6],
                  impact_level=["blue", "yellow", "orange", "red"][j % 4],
                  delay_days=(j % 12), amount_cents=(j * 40_000) % 900_000)
              for j in range(N_ISSUE)]

    deliverables: list[Deliverable] = []
    dkinds = list(DeliverableKind)
    for p in projects:
        # ⚠️ 用 seeded RNG 决定"这个项目交付了哪几项"，让每项目的已交付数真的落在 0–3 之间浮动。
        #    （同余步长在固定周期内只会给出恒定数量，那样"0–3"就名存实亡。）
        delivered_slots = set(rng.sample(range(DELIVERABLES_PER_PROJECT), rng.randrange(0, 4)))
        for k in range(DELIVERABLES_PER_PROJECT):
            deliverables.append(Deliverable(item_id=f"{p.code}-D{k:02d}", project_code=p.code,
                                            kind=dkinds[k % 6], required=(k % 9 != 8),
                                            delivered=k in delivered_slots))

    users = [AppUser(user_key=f"u{j:04d}", display_name=f"staff {j:04d}",
                     role_keys=["LB_ProjectManager"] if j % 9 == 0 else (["admin"] if j % 251 == 0 else ["member"]))
             for j in range(N_USERS)]
    tiers = list(ScopeTier)
    permissions = [PermissionProfile(user_key=f"u{(j * 41) % N_USERS:04d}", tier=tiers[j % 4],
                                     business_unit=BUS[j % 4] if j % 4 in (1, 2) else None,
                                     project_type=["delivery", "presale"][j % 2] if j % 3 == 0 else None)
                   for j in range(N_PERMISSIONS)]

    return Dataset(projects=projects, tasks=tasks, task_deps=deps, budget_versions=versions,
                   budget_lines=lines, cost_docs=cost_docs, payment_nodes=payments, contracts=contracts,
                   ecns=ecns, issues=issues, deliverables=deliverables, users=users, permissions=permissions,
                   seed=seed, as_of=as_of)


def hash_order(j: int, kind_name: str) -> int:
    """确定性散列：⛔ 内建 hash()（它对 str 加了随机盐，跨进程不稳定）。"""
    salt = sum(ord(c) for c in kind_name)
    return (j * 7919 + salt) % 100_000


def to_jsonl(ds: Dataset) -> str:
    """固定顺序 + sort_keys ⇒ 逐字节可复现。"""
    out: list[str] = []
    for name, items in [
        ("project", ds.projects), ("task", ds.tasks), ("task_dep", ds.task_deps),
        ("budget_version", ds.budget_versions), ("budget_line", ds.budget_lines),
        ("cost_doc", ds.cost_docs), ("payment_plan", ds.payment_nodes), ("contract", ds.contracts),
        ("ecn", ds.ecns), ("issue", ds.issues), ("deliverable", ds.deliverables),
        ("user", ds.users), ("permission", ds.permissions),
    ]:
        for obj in items:
            rec = {"_t": name, **obj.model_dump(mode="json")}
            out.append(json.dumps(rec, sort_keys=True, ensure_ascii=False, separators=(",", ":")))
    return "\n".join(out) + "\n"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="deterministic synthetic dataset generator")
    ap.add_argument("--seed", type=int, required=True)
    ap.add_argument("--out", type=Path, default=Path("data"))
    args = ap.parse_args(argv)
    args.out.mkdir(parents=True, exist_ok=True)
    ds = build_dataset(args.seed)
    body = to_jsonl(ds)
    dest = args.out / f"synth-seed{args.seed}.jsonl"
    dest.write_bytes(body.encode("utf-8"))
    print(f"wrote {dest} lines={body.count(chr(10))} bytes={len(body.encode('utf-8'))}")
    return 0


if __name__ == "__main__":
    import sys
    if hasattr(sys.stdout, "reconfigure"):   # 中文 Windows 控制台默认 cp936：
        sys.stdout.reconfigure(encoding="utf-8", errors="backslashreplace")   # 打 ⛔/✅ 会 UnicodeEncodeError
    raise SystemExit(main())
