# -*- coding: utf-8 -*-
"""修 d6_latency：形态标签要来自实测、⛔ 硬写；冷启动与热态分开报。"""
from pathlib import Path

p = Path(r"D:\agent-runtime-forge\tools\d6_latency.py")
s = p.read_text(encoding="utf-8")

# 1) 加 warmup 与形态探测
s = s.replace(
    '    ap.add_argument("--external", action="store_true",',
    '    ap.add_argument("--warmup", type=int, default=15,\n'
    '                    help="先打这么多发**不计入**统计：冷启动（数据集构建、JIT）会把 P99 抬到虚高，\n'
    '                    但⛔ 直接把它抹掉再报数——所以分两栏报，冷启动值单独留档")\n'
    '    ap.add_argument("--external", action="store_true",')

# 2) 用 healthz 的返回判定形态（trace 后端 + 是否外部），⛔ 写死"容器 = 否"
s = s.replace(
    '    health = json.loads(urllib.request.urlopen(f"{base}/healthz", timeout=10).read())\n'
    '    backend = health.get("trace_backend", "?")',
    '    health = json.loads(urllib.request.urlopen(f"{base}/healthz", timeout=10).read())\n'
    '    backend = health.get("trace_backend", "?")\n'
    '    # 形态来自实测：`--external` 意味着压的是**已经在跑的服务**（本项目里就是 compose 起的容器）。\n'
    '    # ⛔ 再像第一版那样硬写"容器 = 否"——那次我把容器测完了报告还写着非容器。\n'
    '    form = "compose 起的容器（外部服务）" if a.external else "本机 fork 的单实例"\n'
    '\n'
    '    cold = []\n'
    '    for _ in range(max(a.warmup, 0)):\n'
    '        try:\n'
    '            cold.append(post(base, QUERY))\n'
    '        except Exception:  # noqa: BLE001\n'
    '            pass')

# 3) 输出里分两栏，并保留冷启动观察值
s = s.replace(
    '        f"- 形态：**本机单实例 uvicorn，trace 后端 = {backend}，容器 = 否**，"\n'
    '        f"并发 {a.concurrency}，样本 {len(samples)}（失败 {errors}）",',
    '        f"- 形态：**{form}，trace 后端 = {backend}**，并发 {a.concurrency}，"\n'
    '        f"样本 {len(samples)}（失败 {errors}）；另有 {len(cold)} 次预热请求**未计入**下方统计",')

s = s.replace(
    '        f"- P50 = **{p50} ms** · P95 = **{p95} ms** · P99 = {p99} ms · 均值 "\n'
    '        f"{round(statistics.mean(samples), 1)} ms",',
    '        f"- 热态：**P50 = {p50} ms · P95 = {p95} ms · P99 = {p99} ms** · "
    f"均值 {round(statistics.mean(samples), 1)} ms",\n'
    '        f"- 冷启动（预热阶段观察值，单独留档、⛔ 混进热态数）：\n'
    '          首 {n} 次最大 {mx} ms" if cold else "- 冷启动：未采样",')

s = s.replace(
    '    OUT.write_text(text, encoding="utf-8", newline="")',
    '    cold_max = round(max(cold), 1) if cold else None\n'
    '    text = text.replace("{n}", str(min(3, len(cold) or 1))).replace("{mx}", str(cold_max))\n'
    '    OUT.write_text(text, encoding="utf-8", newline="")')

p.write_text(s, encoding="utf-8", newline="")
import ast
try:
    ast.parse(s)
    print("d6_latency 已修（形态来自实测 + 冷/热分开）")
except SyntaxError as e:
    print("语法错误", e.lineno, (e.text or "")[:100])
