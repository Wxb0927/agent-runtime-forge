"""「能失败」检查器（D1 的灵魂）。

测试全绿本身不是证据——**没被抓住的错误实现仍然能让测试全绿**。
本工具把实现改歪、跑指定用例、要求它**必须红**，再恢复原样、要求**必须绿**。

⚠️ 两条纪律（真实项目里踩过）：
1. 变异是**改磁盘上的源码**再重新起一个 pytest 进程，⛔ 在测试进程里 monkeypatch
   自己刚 import 的模块——那会读回未变异版本，产生"假能失败"。
2. 每条变异都指定具体用例（``-k``），⛔ 靠"整个套件里有个别用例红了"蒙混。
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):  # ⚠️ Windows 默认 GBK 控制台，中文/符号会 UnicodeEncodeError
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

ROOT = Path(__file__).resolve().parents[1]

MUTATIONS: list[tuple[str, str, str, str]] = [
    (
        "逾期率分母改成全部任务数",
        "forge/domain/formulas.py",
        "    return Ratio(len(late), len(expected), as_of)",
        "    return Ratio(len(late), len(tasks), as_of)",
        "test_overdue_rate_denominator_is_only_expected_tasks",
    ),
    (
        "已过计划工期占比夹紧到 1.0",
        "forge/domain/formulas.py",
        "    return Ratio(elapsed, span, as_of)",
        "    return Ratio(min(elapsed, span), span, as_of)",
        "test_elapsed_plan_ratio_is_not_clamped_when_overdue",
    ),
    (
        "去掉预算为 0 的判空",
        "forge/domain/formulas.py",
        "    if line.budget_cents == 0:\n        return None",
        "    if False:\n        return None",
        "test_zero_budget_yields_no_data_not_zero",
    ),
    (
        "去掉 EAC 的 AC=0 判空",
        "forge/domain/formulas.py",
        "    if incurred_total_cents == 0 or budget_total_cents == 0:",
        "    if budget_total_cents == 0:",
        "test_eac_requires_nonzero_incurred",
    ),
    (
        "把 payment_plan 金额填充率改成 100%",
        "forge/synth/generate.py",
        "PAYMENT_WITH_AMOUNT_PER_CYCLE = 2  # 步长 25 → 184/2300 = 8.00%",
        "PAYMENT_WITH_AMOUNT_PER_CYCLE = 25  # 故意填满（变异体）",
        "test_payment_plan_sparsity_is_exactly_eight_percent",
    ),
]


# --------------------------------------------------------------------------
# D2 组：编排层（状态通道 / fail-closed / checkpointer / 人工闸 / reducer）
# ⚠️ 每条的锚点都先由脚本核对存在性；锚点找不到 = 变异根本没生效，
#    那正是 D1 学到的"假能失败"，所以 main() 里按 FAIL 处理，⛔ 当成通过。
# --------------------------------------------------------------------------

MUTATIONS_D2: list[tuple[str, str, str, str, str]] = [
    ('M1 三条独立通道改成共写同一个 handoff key',
     'forge/graph/plan_graph.py',
     '            return {branch: payload, "steps_seen": [branch],\n                    "notes": [f"[{branch}] from={payload[\'from_\']} step={payload[\'step\']}"]}',
     '            return {"handoff": payload, "steps_seen": [branch],\n                    "notes": [f"[{branch}] from={payload[\'from_\']} step={payload[\'step\']}"]}',
     'test_stress_20_concurrent_threads_zero_overwrite'),
    ('M1b 默认交接写法从 namespaced 改回 shared（事故形状）',
     'forge/graph/plan_graph.py',
     'def build_plan_graph(*, handoff_mode: HandoffMode = "namespaced",',
     'def build_plan_graph(*, handoff_mode: HandoffMode = "shared",',
     'test_independent_channels_survive_fanout'),
    ('M2 guard 查不到权限配置时改成默认放行',
     'forge/graph/guard.py',
     '    if profile is None:\n        tier = FLOOR',
     '    if profile is None:\n        tier = ScopeTier.COMPANY',
     'test_no_profile_falls_to_member_floor'),
    ('M3 host 不再挂 checkpointer',
     'tools/intake_host.py',
     # ⚠️ 锚点只咬 compile 调用本身：上一版把整行写全，后来我给 host 加了
     #    crash_in_verify 参数、那一行被拆成两行 ⇒ 锚点过期。锚点越长越脆，
     #    而且**过期必须报错而不是算通过**（本条就是这么被抓出来的）。
     'compile(checkpointer=saver)',
     'compile()',
     'test_r1_kill_at_gate_then_resume_in_a_new_process'),
    ('M4 写前的人工闸直接放行（删掉 interrupt）',
     'forge/graph/intake_graph.py',
     '        decision = interrupt(payload) or {}',
     '        decision = {"type": "approve"}  # 变异体：跳过人工闸',
     'test_gate_blocks_before_any_write'),
    ('M5 notes 去掉 operator.add reducer',
     'forge/graph/state.py',
     '    notes: Annotated[list[str], operator.add]',
     '    notes: list[str]',
     'test_independent_channels_survive_fanout'),
]

ALL_MUTATIONS = MUTATIONS + MUTATIONS_D2


def run_pytest(k: str) -> int:
    return subprocess.run([sys.executable, "-m", "pytest", "-q", "-k", k], cwd=ROOT,
                          stdout=subprocess.DEVNULL, stderr=subprocess.STDOUT).returncode


def main(argv: list[str] | None = None) -> int:
    # 用法：python tools/mutation_check.py [关键字 ...]
    # 过滤器是这次补的：M3 锚点过期时只想复验那一条，⛔ 为此等十分钟全量。
    wanted = list(argv or [])
    failures = []
    selected = [(i, m) for i, m in enumerate(ALL_MUTATIONS)
                if not wanted or any(w in m[0] for w in wanted)]
    if not selected:
        print("没有变异匹配这些关键字：", " ".join(wanted))
        return 1
    for i, (name, rel, old, new, test_id) in selected:
        group = "D1" if i < len(MUTATIONS) else "D2"
        path = ROOT / rel
        # ⚠️ 必须**按字节**备份再还原：文本模式写文件在 Windows 上会把换行
        #    翻译成 CRLF，"还原原样"于是悄悄改了全文件行尾
        #    （git 里整文件变红、真内容一处没动，实测踩过）。字节还原才叫还原。
        original_bytes = path.read_bytes()
        original = original_bytes.decode("utf-8")
        if old not in original:
            print(f"[FAIL] {name}\n   源码里找不到锚点，变异没生效（这本身就是假能失败）：{rel}")
            failures.append(name)
            continue
        try:
            path.write_text(original.replace(old, new, 1), encoding="utf-8", newline="")
            red = run_pytest(test_id)
        finally:
            path.write_bytes(original_bytes)
        green = run_pytest(test_id)
        ok = red != 0 and green == 0
        print(f"{'[OK]  ' if ok else '[FAIL]'}[{group}] {name}")
        print(f"     改歪 → 红 (rc={red})   改回 → 绿 (rc={green})   [{test_id}]")
        if not ok:
            failures.append(name)
    print()
    print("能失败检查: %d/%d 通过（本仓库共 %d 条：D1 %d + D2 %d）"
          % (len(selected) - len(failures), len(selected), len(ALL_MUTATIONS),
             len(MUTATIONS), len(MUTATIONS_D2)))
    if wanted:
        print("   （已按关键字过滤：%s）" % " ".join(wanted))
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
