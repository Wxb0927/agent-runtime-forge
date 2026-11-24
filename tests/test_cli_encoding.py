"""控制台编码卫生：中文 Windows 默认代码页 cp936，⛔/✅ 一打就 UnicodeEncodeError。

2026-11-24 由 **fresh-clone 复验**抓到（本机一直没暴露，因为我看的是同一个 shell）：
README 承诺 `--intent '{"metric":"conplte_rate",...}'` 的退出码是 **2**，
而在 GBK 输出下它崩在 `print(json.dumps(env, ensure_ascii=False))` 那一步 ⇒ **退出码 1 + traceback**。
⇒ 最坏的一类缺陷：**报告结果的那一步崩掉**，用户拿到栈、⛔ 拿到结论。

三条用例的分工：
① 控制组——先证明 `PYTHONIOENCODING=gbk` 真能把子进程的 stdout 打到 GBK
   （⛔ 这条省了，下面两条就可能在一个强制 UTF-8 的环境里**双双空过**）；
② 两条真命令——`--describe` 必须 rc=0 且 stdout 可 JSON 解析，`--intent` 必须 rc=2；
③ 派生不变式——凡是带 `if __name__ == "__main__":` 的入口，都必须显式兜底，
   清单由 **AST 扫出来**，⛔ 手抄第二份文件列表（手抄的那份一定会漏掉下一个新增入口）。
"""
from __future__ import annotations

import ast
import json
import os
import pathlib
import subprocess
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]


def _child_env(encoding: str) -> dict[str, str]:
    env = dict(os.environ)
    env["PYTHONIOENCODING"] = encoding
    return env


def _run(args: list[str], encoding: str = "gbk") -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, *args], cwd=ROOT, env=_child_env(encoding),
                          capture_output=True, text=True, encoding="utf-8",
                          errors="backslashreplace")


def test_control_group_gbk_really_is_in_effect() -> None:
    """⛔ 跳过这条，后面两条就可能在 UTF-8 环境里空过——那叫假绿。"""
    proc = _run(["-c", "import sys; print(sys.stdout.encoding)"])
    assert proc.returncode == 0, proc.stderr
    assert "gbk" in proc.stdout.strip().lower(), (
        f"子进程 stdout 编码是 {proc.stdout.strip()!r}，⛔ 复现 cp936 ⇒ "
        "本文件其余用例的保护力为零，先修这个再谈别的")


def test_describe_survives_a_gbk_console() -> None:
    proc = _run(["-m", "forge.cli", "--describe", "stats"])
    assert "UnicodeEncodeError" not in proc.stderr, proc.stderr[-400:]
    assert proc.returncode == 0, f"rc={proc.returncode}\n{proc.stderr[-400:]}"
    assert json.loads(proc.stdout), "自描述输出必须可解析"


def test_intent_bad_envelope_keeps_the_exit_code_the_readme_promises() -> None:
    """README 写的就是这条命令 + 这个退出码；崩溃会把 2 变成 1。"""
    proc = _run(["-m", "forge.cli", "--intent",
                 '{"metric":"conplte_rate","topK":999}'])
    assert "UnicodeEncodeError" not in proc.stderr, proc.stderr[-400:]
    assert proc.returncode == 2, f"rc={proc.returncode}（README 承诺 2）\n{proc.stderr[-300:]}"
    env = json.loads(proc.stdout)
    assert env["code"] == "BAD_INTENT"
    assert any(n.startswith("CLAMPED") for n in env["notes"])


def test_every_console_entry_forces_utf8() -> None:
    """入口清单由 **git 跟踪的文件 + AST** 派生，⛔ 手抄第二份列表，也⛔ rglob。

    第一版用 rglob，结果扫到一个已提交的垃圾脚本（`.tmp_lat_fix.py`，语法都坏了）
    ⇒ 报的 offender 是垃圾而不是缺陷。以"会被发布出去的文件"为论域才是本条想守住的东西。
    """
    tracked = subprocess.run(["git", "ls-files", "*.py"], cwd=ROOT,
                             capture_output=True, text=True).stdout.split()
    offenders: list[str] = []
    scanned = 0
    for rel in tracked:
        py = ROOT / rel
        if not py.is_file():
            continue
        src = py.read_text(encoding="utf-8")
        tree = ast.parse(src, filename=str(py))
        is_entry = any(
            isinstance(st, ast.If)
            and isinstance(st.test, ast.Compare)
            and getattr(st.test.left, "id", "") == "__name__"
            for st in tree.body)
        if not is_entry:
            continue
        scanned += 1
        if "sys.stdout.reconfigure" not in src:
            offenders.append(rel)
    assert scanned >= 10, f"只扫到 {scanned} 个入口 ⇒ 扫描逻辑本身坏了（控制组）"
    assert not offenders, "这些控制台入口没兜底 UTF-8，GBK 机器上会崩：\n" + "\n".join(offenders)

