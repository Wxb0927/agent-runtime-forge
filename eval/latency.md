# 服务延迟实测（2026-11-23）

- 形态：**本机单实例 uvicorn，trace 后端 = sqlite，容器 = 否**，并发 4，样本 120（失败 0）
- P50 = **54.6 ms** · P95 = **78.2 ms** · P99 = 472.7 ms · 均值 68.0 ms
- 问句：`PRJ-1180 的进度、偏差和成本`（三分支全跑，digest 10 项）

⚠️ 这组数**⛔ 代表生产**：单实例、sqlite、无网络跳转、模型走打桩。compose 那一档（Postgres + Qdrant + 真模型）要另测：`docker compose up -d && uv run python tools/d6_latency.py --external --port 8000`。
⚠️ 引用时必须带上形态那句，否则 P95 会被读成生产承诺。
