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

# 只用 uv 装依赖（不装项目本身：仓库是扁平布局 + PYTHONPATH，与 README 一致）
COPY pyproject.toml uv.lock uv.toml ./
RUN pip install --no-cache-dir uv==0.11.17 \
 && uv sync --frozen --no-dev --extra semantic

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
