# 服务镜像。⚠️ 基础镜像用 python:3.11-slim（与本项目 requires-python 一致）；
# 依赖由 uv 锁文件决定，装完即**不可变**（评审复现时不必猜版本）。
FROM python:3.11-slim

# 缓存目录也在镜像里钉到非 /root 路径：容器里 /root 就是"系统盘"，
# 万一有人把 HF 依赖烤进镜像，⛔ 落到 /root/.cache。
ENV PYTHONUNBUFFERED=1 \
    FORGE_CACHE_ROOT=/var/cache/forge \
    HF_HOME=/var/cache/forge/hf \
    TORCH_HOME=/var/cache/forge/torch \
    XDG_CACHE_HOME=/var/cache/forge/cache

WORKDIR /app

# ⚠️ uv sync 把依赖装进 /app/.venv，系统 python（/usr/local/bin）看不见它。
#    实测第一次起容器就报「No module named uvicorn」——本机 curl 验过了⛔ 等于容器能跑。
#    多行 ENV 必须带续行反斜杠：漏了它第二行会被当未知指令而**悄悄失效**（我就漏了一次）。
ENV VIRTUAL_ENV=/app/.venv \
    PATH="/app/.venv/bin:${PATH}"

# 只用 uv 装依赖（不装项目本身：仓库是扁平布局 + PYTHONPATH，与 README 一致）
COPY pyproject.toml uv.lock uv.toml ./
# ⚠️ 服务镜像只装 server extra：REST/SSE/trace/降级全程没 import 过 torch，
#    把 semantic（torch + sentence-transformers 约 2 GB）烤进服务镜像纯属浪费，
#    还让每次构建慢十几分钟。RAG 评测是离线批处理，在本机跑：
#      uv sync --extra semantic --extra zh
#      uv run python -m eval.rag_eval --semantic --rerank
RUN pip install --no-cache-dir uv==0.11.17 \
 && uv sync --frozen --no-dev --extra server

COPY forge/ ./forge/
COPY eval/ ./eval/
COPY server/ ./server/
COPY contracts/ ./contracts/
COPY tools/ ./tools/

# ⛔ 把 .env 拷进镜像（密钥）；.dockerignore 已排除，这里再断言一次
RUN ! test -f .env

EXPOSE 8000
# 健康检查由 compose 的 healthcheck 与 /healthz 负责（⛔ 在 Dockerfile 里写死 curl 依赖）
CMD ["python", "-m", "uvicorn", "server.app:app", "--host", "0.0.0.0", "--port", "8000"]
