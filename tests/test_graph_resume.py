"""验收②：kill 进程后重启，能不能**从中断的那个节点**接着办。

⚠️ 为什么必须起真子进程：同进程里换 ``thread_id`` 再 invoke，checkpoint 对象还挂在内存里，
怎么测都会过——那是**假验收**。这里用 ``Popen.kill()``（Windows 上就是 TerminateProcess，
⛔ 优雅退出），进程是被掐死的，能续跑才叫真续跑。

四个场景，逐个锁一种语义：

* **R1 停在闸上被杀** ⇒ 已完成的节点⛔ 重跑，换进程 ``Command(resume=…)`` 从闸继续。
* **R2 崩在节点中间** ⇒ 死掉的那个节点一定重放；**上一个节点是否重放是竞态，⛔ 钉不住**
  （上一超步的 checkpoint 提交与进程死亡并发发生）。可钉的是续跑后的整体正确性。
  ⇒ 结论："已完成节点不重跑"只在**已提交**之后成立 ⇒ 节点要幂等、写要放到闸后面。
* **checkpoint 文件删掉** ⇒ 一切从头跑（证明续跑靠的是**磁盘**，⛔ 进程内存）。
* **重启时再传一次新输入** ⇒ 那是**开新一轮**，已完成的节点会重跑（⛔ 当成断点续办）。

诊断纪律：每个子进程说过的每一行都留在 ``Host.lines`` 里，失败时一起打出来。
一次"满载下 collect 被重跑"的偶发失败就是因为现场被丢了而无从判断——
**留不住现场的红，等于没有红。**
"""
from __future__ import annotations

import json
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
HOST = ROOT / "tools" / "intake_host.py"
NL = chr(10)


def _popen(args: list[str]) -> subprocess.Popen:
    """⚠️ stderr 必须并进 stdout：分开两个 PIPE 而只读 stdout 时，子进程 stderr 写满
    管道缓冲区（Windows 约 64 KB）就会卡在写上，父进程则卡在 readline 上等——
    测试脚手架的经典死锁，第一版整套"挂死"有一部分就是它。"""
    return subprocess.Popen([sys.executable, "-u", "-X", "utf8", str(HOST), *args],
                            cwd=str(ROOT), stdout=subprocess.PIPE,
                            stderr=subprocess.STDOUT, text=True, encoding="utf-8",
                            bufsize=1)


class Host:
    """一个子进程 + 它到目前为止说过的所有话。"""

    def __init__(self, args: list[str], label: str) -> None:
        self.label = label
        self.proc = _popen(args)
        self.lines: list[str] = []

    def _tail(self, n: int = 30) -> str:
        return NL.join(self.lines[-n:]) or "(子进程一个字都没说)"

    def _readline(self) -> str:
        return self.proc.stdout.readline() if self.proc.stdout else ""

    def status(self, timeout: float = 60.0) -> dict:
        """读一行 STATUS。⛔ 静默挂死：超时或提前退出都把现场打出来。"""
        deadline = time.time() + timeout
        while time.time() < deadline:
            line = self._readline()
            if not line:
                if self.proc.poll() is not None:
                    raise AssertionError(
                        f"[{self.label}] host 提前退出 rc={self.proc.returncode}"
                        + NL + self._tail())
                time.sleep(0.05)
                continue
            self.lines.append(line.rstrip())
            if line.startswith("STATUS "):
                return json.loads(line[len("STATUS "):])
        raise AssertionError(f"[{self.label}] 等 STATUS 超过 {timeout}s" + NL + self._tail())

    def wait_for_beacon(self, needle: str, timeout: float = 90.0) -> None:
        """等到子进程自己报"我进 verify 了"再动手——⛔ 用 sleep 猜时机（那测的是冷启动快慢）。"""
        deadline = time.time() + timeout
        while time.time() < deadline:
            line = self._readline()
            if not line:
                if self.proc.poll() is not None:
                    raise AssertionError(f"[{self.label}] 没等到 {needle} 就退了" + NL + self._tail())
                continue
            self.lines.append(line.rstrip())
            if line.startswith(needle):
                return
        raise AssertionError(f"[{self.label}] 等 {needle} 超时" + NL + self._tail())

    def kill(self) -> None:
        self.proc.kill()
        self.proc.wait(timeout=30)

    def wait(self, timeout: float = 60.0) -> None:
        try:
            self.proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            self.kill()
            raise AssertionError(f"[{self.label}] 该自己退出的进程没退" + NL + self._tail())


def counts(scratch: Path) -> dict[str, int]:
    out = {}
    for name in ("collect", "verify"):
        p = scratch / f"{name}.count"
        out[name] = int(p.read_text(encoding="utf-8")) if p.exists() else 0
    return out


# --------------------------------------------------------------------- R1


def test_r1_kill_at_gate_then_resume_in_a_new_process(tmp_path: Path) -> None:
    """停在人工闸时被硬杀 → 新进程从闸这里继续，前面的节点一个都不重跑。"""
    first = Host(["--scratch", str(tmp_path), "--thread", "r1", "--hold"], "r1-first")
    st = first.status()
    assert st["interrupted"] is True and st["pending_node"] == ["confirm"], st
    pid_alive = first.proc.pid
    first.kill()

    assert counts(tmp_path) == {"collect": 1, "verify": 1}, first.lines
    assert not (tmp_path / "draft-r1.json").exists()

    second = Host(["--scratch", str(tmp_path), "--thread", "r1",
                   "--resume", json.dumps({"type": "approve"})], "r1-resume")
    st2 = second.status()
    second.wait()

    assert st2["pid"] != pid_alive, "第二个进程必须是另一个 pid"
    assert st2["interrupted"] is False and st2["code"] == "WRITTEN", st2
    assert st2["steps_seen"] == ["collect", "verify", "confirm:approve", "write"], st2
    # ⛔ 已完成的节点被重放：计数若变 2，说明恢复是从头跑，⛔ 是"断点续办"
    assert st2["counts"] == {"collect": 1, "verify": 1}, (st2, first.lines, second.lines)
    assert (tmp_path / "draft-r1.json").exists()


# --------------------------------------------------------------------- R2


def test_r2_crash_inside_a_node_always_replays_that_node(tmp_path: Path) -> None:
    """崩在 ``verify`` 内部 ⇒ ``verify`` **一定**重跑；``collect`` 是否重跑是**竞态**。

    ⚠️ 这条测试改了两次才对，理由值得留着：
    * 第一版由父进程掐表 ``kill()``——死点落在 sleep 的哪一秒是随机的，
      整套跑 3 次红 2 次、单跑 6 次全绿（**测量方式的错，不是被测代码的错**）；
    * 第二版改成节点自己 ``os._exit``，并断言"刚进节点就会连 collect 一起重跑"——
      CLI 里 3/3 成立，进了 pytest 就翻车。
    ⇒ 真相是：**上一个超步的 checkpoint 是否已经落盘，与进程死亡是并发发生的**，
      所以 ``collect`` 重不重跑不可钉。可钉的只有两条不变量，这里断言它们：
      ① 死掉的那个节点一定重放（它的产出从未提交）；
      ② 续跑之后整体仍然正确——停在同一个闸上、批准只写一份草稿、审计只有一条。
      这就是"节点必须幂等，有副作用的写要放到闸后面"的实证依据。
    """
    first = Host(["--scratch", str(tmp_path), "--thread", "r2",
                  "--crash-in-verify", "1.5"], "r2-crash")
    first.wait_for_beacon("PHASE verify-crash")
    first.proc.wait(timeout=60)
    assert counts(tmp_path) == {"collect": 1, "verify": 1}, first.lines

    second = Host(["--scratch", str(tmp_path), "--thread", "r2", "--continue"], "r2-continue")
    st2 = second.status()
    second.kill()

    assert st2["interrupted"] is True and st2["pending_node"] == ["confirm"], (st2, first.lines)
    assert st2["counts"]["verify"] == 2, f"死掉的节点必须重放：{st2['counts']}"
    assert st2["counts"]["collect"] in (1, 2), f"collect 只可能是 1 或 2：{st2['counts']}"
    assert st2["outcome"] is None and not (tmp_path / "draft-r2.json").exists(), \
        "停在闸上之前⛔ 不许有任何写入"

    third = Host(["--scratch", str(tmp_path), "--thread", "r2",
                  "--resume", json.dumps({"type": "approve"})], "r2-approve")
    st3 = third.status()
    third.wait()
    assert st3["code"] == "WRITTEN" and st3["draft_written"] is True, st3
    assert (tmp_path / "draft-r2.json").exists()
    audit_lines = (tmp_path / "audit.jsonl").read_text(encoding="utf-8").strip().splitlines()
    assert len(audit_lines) == 1, f"崩溃-续跑-批准后审计必须恰好一条：{audit_lines}"
    assert st3["counts"]["verify"] == 2, "批准这一程⛔ 再重放任何已完成节点"


# ------------------------------------------------- 反面取证：靠磁盘，不靠内存


def test_checkpoint_file_is_what_makes_it_survive(tmp_path: Path) -> None:
    """删掉 sqlite 文件后重启同一个 thread ⇒ 必须从零开始跑。"""
    first = Host(["--scratch", str(tmp_path), "--thread", "r3", "--hold"], "r3-first")
    assert first.status()["interrupted"] is True
    first.kill()
    (tmp_path / "ckpt.sqlite3").unlink()

    second = Host(["--scratch", str(tmp_path), "--thread", "r3"], "r3-restart")
    st2 = second.status()
    second.kill()
    assert st2["counts"] == {"collect": 2, "verify": 2}, \
        f"checkpoint 没了还续得上？说明测的是内存：{st2['counts']}"


def test_fresh_input_is_a_new_turn_not_a_resume(tmp_path: Path) -> None:
    """⛔ 把"重启进程 + 再传一次输入"当断点续办：那是**新一轮**，collect 会重跑。

    这条是从一次真实的语义误解里补出来的（第一版 R2 就这么写错了）：
    真续跑只能是 ``invoke(None, cfg)``，让 checkpoint 里的待执行任务自己重放。
    """
    first = Host(["--scratch", str(tmp_path), "--thread", "nt", "--hold"], "nt-first")
    assert first.status()["interrupted"] is True
    first.kill()
    assert counts(tmp_path) == {"collect": 1, "verify": 1}

    again = Host(["--scratch", str(tmp_path), "--thread", "nt"], "nt-newturn")
    st = again.status()
    again.kill()
    assert st["counts"] == {"collect": 2, "verify": 2}, (st, again.lines)
    assert st["steps_seen"].count("collect") == 2, st["steps_seen"]
