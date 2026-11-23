# -*- coding: utf-8 -*-
"""比较两个目录的 pytest 收集结果，找出 191 vs 155 的差异来源。"""
import collections
import re
import subprocess
import sys
from pathlib import Path

A = Path(r"D:\agent-runtime-forge")
B = Path(sys.argv[1]) if len(sys.argv) > 1 else None


def collect(root: Path) -> collections.Counter:
    r = subprocess.run([str(root / ".venv" / "Scripts" / "python.exe"), "-m", "pytest",
                        "--collect-only", "-q", "-o", "addopts=--basetemp=.tmp/pytest"],
                       cwd=str(root), capture_output=True, text=True, timeout=900)
    out = (r.stdout or "") + (r.stderr or "")
    per = collections.Counter()
    for line in out.splitlines():
        m = re.match(r"(tests/[^:]+\.py)::", line.strip())
        if m:
            per[m.group(1)] += 1
    total = re.findall(r"(\d+) tests? collected", out)
    err = re.findall(r"(ERROR|error)", out)
    return per, (total[-1] if total else "?"), len(err), out


for tag, root in (("工作仓库", A), ("全新 clone", B)):
    if not root or not root.exists():
        continue
    per, total, errs, raw = collect(root)
    print(f"\n=== {tag} ({root.name}) collected={total} errors={errs} ===")
    for f, n in sorted(per.items()):
        print(f"  {n:3d}  {f}")
    if errs:
        for line in raw.splitlines():
            if "ERROR" in line or "error" in line.lower():
                print("   !", line.strip()[:160])
                break
