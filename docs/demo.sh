#!/usr/bin/env bash
# D7 演示脚本：三条命令看完这个项目的三块主张。
# ⚠️ 前两条不需要任何模型下载（核心依赖就够），第三条要么本机 fork、要么 compose。
set -euo pipefail
cd "$(dirname "$0")/.."

echo "══════ ① 测试 + 能失败证明（框架改歪必须让测试变红）══════"
uv sync                                   # 只装核心线；语义档见文末
uv run pytest -q
uv run python tools/mutation_check.py     # 40 条：改歪→红 / 改回→绿

echo
echo "══════ ② 契约层：说明书由代码派生 + 非法参数自愈 ══════"
uv run python -m forge.cli --describe stats | head -20
# 任务书那条验收：非法枚举被点名、越界钳位、回带合法枚举、⛔ 被执行
uv run python -m forge.cli --intent '{"metric":"conplte_rate","topK":999}'
uv run python -m forge.cli --gates        # G1/G2/G3，任一失败 exit 1

echo
echo "══════ ③ 服务化：REST + SSE 事件流 + trace + 如实降级 ══════"
uv run python -m uvicorn server.app:app --port 8899 &
SRV=$!
trap 'kill $SRV 2>/dev/null || true' EXIT
until curl -s -m 2 http://127.0.0.1:8899/healthz >/dev/null; do sleep 1; done
curl -s -N -m 30 -H 'Accept: text/event-stream' \
  'http://127.0.0.1:8899/v1/ask/stream?query=PRJ-1180%20%E7%9A%84%E5%AE%8C%E6%88%90%E7%8E%87' | head -12
# 下游故障：HTTP 仍是 200，响应体仍是合法信封（⛔ 裸栈），缺口被点名
curl -s -X POST -H 'Content-Type: application/json' \
  -d '{"query":"PRJ-1180 的成本","user_key":"u0041","faults":"cost:perm"}' \
  http://127.0.0.1:8899/v1/ask | head -c 400; echo

echo
echo "（可选）语义检索与 RAG 质量档需要本地模型：先确认模型缓存在非系统盘，再"
echo "  uv sync --extra semantic --extra zh && uv run python -m eval.rag_eval --semantic --tag semantic"
echo "容器形态：docker run 前先跑 uv run python tools/d6_build_guard.py（C 盘有 Docker 数据盘就拒绝构建）"
