"""D7：把 README 那三条命令的**真输出**渲染成 `docs/demo.gif`——"附 Demo"的真数支撑。

⚠️ 这份"录屏"的诚实边界（README 里必须照抄一句）：

* 每一帧的文本都来自**真跑一遍**的 stdout/stderr（本脚本就是现场跑给你看的），
  ⇒ 它能证明"照 README 敲这些命令会得到这些结果"；
* 但它⛔ 是鼠标/键盘/真终端截屏 ⇒ ⛔ 拿它说"这是屏幕录像"。要真屏幕录像就⛔ 用我，
  Win+G 或 OBS 自己录一遍（5 分钟）。

为什么⛔ 用 asciinema/agg：agg 要 Rust + ffmpeg，为一个门面引 300 MB 工具链⛔ 值；
Pillow 一枚轮子（6.9 MB，dev 组，Docker 镜像⛔ 装它）就够，而且**渲染逻辑可读可改**——
"Demo 是怎么来的"这件事本身也⛔ 该是黑盒。

用法：`uv run python tools/d7_record_demo.py`（约 1 分钟；产 `docs/demo.gif` + `docs/demo.txt`）
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DOCS = ROOT / "docs"
FONT_LATIN = Path("C:/Windows/Fonts/consola.ttf")
FONT_CJK = Path("C:/Windows/Fonts/msyh.ttc")
FONT_SIZE = 15
ROWS = 30                      # 屏内可见行数
ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")


def strip_ansi(s: str) -> str:
    return ANSI_RE.sub("", s.replace("\r\n", "\n").replace("\r", "\n"))


def run(argv: list[str], timeout: int = 240) -> list[str]:
    """跑一条命令，返回清洗后的输出行（真 stdout+stderr 合并）。"""
    p = subprocess.run(argv, cwd=ROOT, capture_output=True, text=True,
                       encoding="utf-8", errors="replace", timeout=timeout,
                       env={**os.environ, "PYTHONIOENCODING": "utf-8",
                            "PYTHONUNBUFFERED": "1"})
    out = strip_ansi((p.stdout or "") + (p.stderr or ""))
    lines = [ln.rstrip() for ln in out.split("\n")]
    while lines and not lines[-1].strip():
        lines.pop()
    lines.append(f"# exit={p.returncode}")
    return [ln for ln in lines if ln.strip()]


def keep(lines: list[str], head: int = 0, tail: int = 0) -> list[str]:
    """只留讲得清的几行。

    ⚠️ 三条硬规矩（第一版全犯了）：
    1. **head 与 tail ⛔ 同时用**——两边一叠加就会切出 `],\n ],` 这种**假 JSON**，
       画面里出现我亲手拼出来的行，那就不叫录屏叫伪造；
    2. 只要真的减量了，就必须**留下一行标记**（`# …略过 N 行`），⛔ 静默截尾；
    3. ⛔ 为了画面好看改写任何一行的内容。
    """
    body = [ln for ln in lines if not ln.startswith("warning: `VIRTUAL_ENV")]
    if head and tail:
        raise ValueError("head/tail 同时用会拼出假内容（见 docstring 第 1 条）")
    if head:
        if len(body) > head:
            n = len(body) - head
            return body[:head] + [f"# …略过 {n} 行（画面为短；全量以本地实跑为准）"]
        return body
    if tail:
        if len(body) > tail:
            return [f"# …略过 {len(body) - tail} 行"] + body[-tail:]
        return body
    return body


def capture() -> list[str]:
    """按 README「跑起来」那一节的顺序真跑一遍，收集每一行。"""
    L: list[str] = []

    def emit(lines: list[str]) -> None:
        L.extend(lines)

    emit(["# agent-runtime-forge — README「跑起来」三条命令，逐条真跑",
          "# Windows 11 · uv 0.11.17 · CPython 3.11.15 · 合成数据（已脱敏）",
          ""])

    emit(["$ uv run pytest"])
    # ⚠️ ⛔ 在这儿再手打一个 -q：addopts 已经带了 -q，命令行再加就成了 -qq，
    #    于是 **"199 passed in …" 那行汇总被吃掉**——Demo 的第一句判据当场消失（第一版就是这样）。
    emit(keep(run([sys.executable, "-m", "pytest"]), tail=3))
    emit([])

    emit(["$ uv run python tools/mutation_check.py M29   # 改歪→红 / 改回→绿（抽样 1 条）"])
    emit(keep(run([sys.executable, str(ROOT / "tools/mutation_check.py"), "M29"]), tail=4))
    emit([])

    emit(["$ uv run python -m forge.cli --gates"])
    emit(keep(run([sys.executable, "-m", "forge.cli", "--gates"])))
    emit([])

    emit(["$ uv run python -m forge.cli --intent "
          "'{\"metric\":\"conplte_rate\",\"topK\":999}'"])
    emit(keep(run([sys.executable, "-m", "forge.cli", "--intent",
                  '{"metric":"conplte_rate","topK":999}'])))
    emit([])

    emit(["$ uv run python -m forge.cli --describe stats"])
    emit(keep(run([sys.executable, "-m", "forge.cli", "--describe", "stats"]), head=12))
    emit([])

    # ── 服务化：本地 uvicorn + SSE（⛔ 依赖 Docker；compose 形态见 README） ──
    port = 8899
    emit([f"$ uv run python -m uvicorn server.app:app --port {port} &"])
    srv = subprocess.Popen([sys.executable, "-m", "uvicorn", "server.app:app",
                            "--port", str(port), "--log-level", "warning"],
                           cwd=ROOT, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                           env={**os.environ, "PYTHONIOENCODING": "utf-8"})
    try:
        deadline = time.time() + 40
        health = ""
        while time.time() < deadline:
            try:
                health = urllib.request.urlopen(
                    f"http://127.0.0.1:{port}/healthz", timeout=2).read().decode("utf-8")
                break
            except Exception:
                time.sleep(0.5)
        emit(["$ curl http://127.0.0.1:%d/healthz   # 起没起起来要自己说" % port])
        emit(keep([" ".join(strip_ansi(health).split())]))
        q = urllib.parse.quote("PRJ-1180 的完成率")
        emit([f"$ curl -N \"http://127.0.0.1:{port}/v1/ask/stream?query=…\"  # 事件流"])
        emit(keep(run(["curl.exe", "-N", "-s", "-m", "40",
                       "-H", "Accept: text/event-stream",
                       f"http://127.0.0.1:{port}/v1/ask/stream?query={q}"], timeout=60),
                  head=16))
        emit([])
        emit(["$ curl -X POST -d '{\"query\":\"PRJ-1180 的成本\",\"faults\":\"cost:perm\"}' …"])
        body = json.dumps({"query": "PRJ-1180 的成本", "user_key": "u0041",
                           "faults": "cost:perm"}).encode("utf-8")
        req = urllib.request.Request(f"http://127.0.0.1:{port}/v1/ask", data=body,
                                     headers={"Content-Type": "application/json"})
        resp = urllib.request.urlopen(req, timeout=40)
        rid = resp.headers.get("X-Request-Id", "")
        emit(keep([" ".join(strip_ansi(resp.read().decode("utf-8")).split())]))
        emit([])
        # trace **按 id 取那一条**：⛔ 用 /v1/trace/stats——本地 sqlite 是共享历史库，
        # 里面 p50/p95 混着这之前几百次调用，摆在 Demo 里会和 README 的 compose 读数当场打架。
        emit([f"$ curl http://127.0.0.1:{port}/v1/trace/{rid or '<request_id>'}"])
        emit(keep([" ".join(strip_ansi(urllib.request.urlopen(
            f"http://127.0.0.1:{port}/v1/trace/{rid}", timeout=20)
            .read().decode("utf-8")).split())]))
    finally:
        srv.terminate()
        try:
            srv.wait(timeout=15)
        except Exception:
            srv.kill()
    return L


# ------------------------------------------------------------------ 渲染

def wrap(line: str, limit: int = 108) -> list[str]:
    """⚠️ 这里⛔ 按"字符占几格"折行——中文字宽按 2 格估，而 Consolas 的拉丁字母实测宽
    和雅黑的中文实测宽都⛔ 等于那个估计值，第一版就是这么把长行画到画面外的。
    真正的折行只能按**测出来的像素宽**算，见 render() 里的 flow()。"""
    out, cur = [], ""
    for ch in line:
        if ch == " " and len(cur) > limit:
            out.append(cur)
            cur = ""
            continue
        cur += ch
    out.append(cur)
    return [o for o in out if o != ""] or [""]


def classify(line: str) -> str:
    if line.startswith("$ "):
        return "cmd"
    if line.startswith("# "):
        return "note"
    low = line.lower()
    if "fail" in low or "bad_intent" in low or "error" in low or "traceback" in low:
        return "bad"
    if "ok" in low or "passed" in low or "exit=0" in low or "heal" in low:
        return "good"
    return "text"


def render(all_lines: list[str], gif: Path, fps_frames: int = 26) -> tuple[int, int]:
    from PIL import Image, ImageDraw, ImageFont

    f_latin = ImageFont.truetype(str(FONT_LATIN), FONT_SIZE)
    f_cjk = ImageFont.truetype(str(FONT_CJK), FONT_SIZE)
    mono = f_latin.getlength("M")
    pad, lh = 14, int(FONT_SIZE * 1.55)
    W = int(mono * 116) + pad * 2
    H = lh * ROWS + pad * 2
    BG, COL = (18, 20, 24), {"cmd": (96, 205, 255), "note": (140, 148, 160),
                             "text": (214, 220, 228), "good": (126, 222, 148),
                             "bad": (255, 146, 138)}
    max_w = W - pad * 2
    # 逐字符画 20 万多次太慢 ⇒ 先把"这个字符占几像素"缓存下来（折行与绘制共用同一张表，
    # 两边⛔ 分叉：折行算窄了就会画到画面外）
    adv: dict[int, float] = {}

    def draw_frame(buf: list[tuple[str, str]]) -> Image.Image:
        img = Image.new("RGB", (W, H), BG)
        d = ImageDraw.Draw(img)
        y = pad
        for line, kind in buf[-ROWS:]:
            x = pad
            for ch in line:
                f = f_cjk if ord(ch) > 0x2E80 else f_latin
                d.text((x, y), ch, font=f, fill=COL[kind])
                x += advance(ch)
            y += lh
        return img

    # 逐行揭示；每条命令（$ 行）之后停一下，⛔ 一路刷完没人看得清
    # ⚠️ 帧时长最终由 GIF 编码器**合并相同帧**后决定 ⇒ 脚本⛔ 报"我打算写多少帧"，
    #    必须回头把存下来的文件读一遍再报（第一版就是报了 144 帧，实际只有 83 帧 / 3.3 秒，
    #    等于一段⛔ 有人看得清的 Demo——又一个"汇总数说谎"）。
    REVEAL_MS = 90
    HOLD_FRAMES = 12
    def ch_font(ch: str):
        return f_cjk if ord(ch) > 0x2E80 else f_latin

    def advance(ch: str) -> float:
        key = ord(ch)
        a = adv.get(key)
        if a is None:
            a = ch_font(ch).getlength(ch)
            adv[key] = a
        return a

    def flow(line: str) -> list[str]:
        """按**实测像素宽**折行（中英混排⛔ 是等宽的，按字符格数估会画到画面外）。"""
        out, cur, w = [], "", 0.0
        for ch in line:
            a = advance(ch)
            if w + a > max_w and cur:
                out.append(cur)
                cur, w = "", 0.0
            cur += ch
            w += a
        out.append(cur if cur else "")
        return out if out else [""]

    flat: list[tuple[str, str]] = []
    for ln in all_lines:
        for w in flow(ln):
            flat.append((w, classify(ln)))
    timeline: list[list[tuple[str, str]]] = []
    for i, (text, kind) in enumerate(flat):
        buf = flat[:i + 1]
        timeline.append(list(buf))
        if text.startswith("$ "):
            for _ in range(HOLD_FRAMES):
                timeline.append(list(buf))
    frames = [draw_frame(buf) for buf in timeline]
    gif.unlink(missing_ok=True)
    frames[0].save(gif, save_all=True, append_images=frames[1:],
                   duration=REVEAL_MS, loop=0, optimize=True)

    # 读回真值：⛔ 信"我以为写进去的"
    from PIL import Image as _I, ImageSequence as _Seq
    chk = _I.open(gif)
    durs = [f.info.get("duration", 0) for f in _Seq.Iterator(chk)]
    return len(durs), (chk.size[0], chk.size[1]), sum(durs) / 1000.0, len(timeline)


def main() -> int:
    if sys.platform != "win32":
        print("[WARN] 字体路径按 Windows 写；非 Windows 只会⛔ 影响字形")
    if not (FONT_LATIN.exists() and FONT_CJK.exists()):
        print("[FAIL] 缺 Consolas / 微软雅黑 ⇒ 渲染会崩，先确认字体")
        return 1
    t0 = time.time()
    lines = capture()
    DOCS.mkdir(exist_ok=True)
    (DOCS / "demo.txt").write_text("\n".join(lines) + "\n", encoding="utf-8", newline="\n")
    n, size, secs, planned = render(lines, DOCS / "demo.gif")
    mb = (DOCS / "demo.gif").stat().st_size / 1048576
    merged = planned - n
    print(f"[OK] demo.gif：{n} 帧（时间轴 {planned} 帧，编码器合并了 {merged} 个相同帧）"
          f" {size[0]}x{size[1]}，{mb:.2f} MB，**实际播放 {secs:.1f}s**；"
          f"demo.txt {len(lines)} 行；采集用时 {time.time() - t0:.1f}s")
    if secs < 12:
        print(f"[FAIL] 播放只 {secs:.1f}s ⇒ 没人看得清，这⛔ 算 Demo；调 REVEAL_MS/HOLD_FRAMES")
        return 2
    if mb > 6:
        print("[WARN] GIF 超过 6 MB ⇒ ⛔ 提交进仓库（会把 clone 变慢），先减量")
        return 2
    return 0


if __name__ == "__main__":
    import sys
    if hasattr(sys.stdout, "reconfigure"):   # 中文 Windows 控制台默认 cp936：
        sys.stdout.reconfigure(encoding="utf-8", errors="backslashreplace")   # 打 ⛔/✅ 会 UnicodeEncodeError
    raise SystemExit(main())
