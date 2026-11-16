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


def run_pytest(k: str) -> int:
    return subprocess.run([sys.executable, "-m", "pytest", "-q", "-k", k], cwd=ROOT,
                          stdout=subprocess.DEVNULL, stderr=subprocess.STDOUT).returncode


def main() -> int:
    failures = []
    for name, rel, old, new, test_id in MUTATIONS:
        path = ROOT / rel
        original = path.read_text(encoding="utf-8")
        if old not in original:
            print(f"[FAIL] {name}\n   源码里找不到锚点，变异没生效（这本身就是假能失败）：{rel}")
            failures.append(name)
            continue
        try:
            path.write_text(original.replace(old, new, 1), encoding="utf-8")
            red = run_pytest(test_id)
        finally:
            path.write_text(original, encoding="utf-8")
        green = run_pytest(test_id)
        ok = red != 0 and green == 0
        print(f"{'[OK]  ' if ok else '[FAIL]'} {name}")
        print(f"     改歪 → 红 (rc={red})   改回 → 绿 (rc={green})   [{test_id}]")
        if not ok:
            failures.append(name)
    print("\n能失败检查: %d/%d 通过" % (len(MUTATIONS) - len(failures), len(MUTATIONS)))
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
