# 两阶段：Node 打包前端 → Python 运行时直接托管 dist（后端已实现静态托管）
# 基础镜像可通过 --build-arg 换成镜像站前缀（国内直连 docker.io 常被拦）：
#   --build-arg REGISTRY=docker.m.daocloud.io
ARG REGISTRY=docker.io
FROM ${REGISTRY}/library/node:20-alpine AS web
WORKDIR /web
# 国内网络可用 --build-arg NPM_REGISTRY=https://registry.npmmirror.com 覆盖
ARG NPM_REGISTRY=https://registry.npmjs.org
COPY frontend/package.json frontend/package-lock.json ./
# 大包在慢网络下会读超时：把重试与超时放宽，否则 chromadb(23MB) 这类包必断
RUN npm config set registry "$NPM_REGISTRY" \
 && npm config set fetch-retries 5 \
 && npm config set fetch-timeout 120000 \
 && npm ci --no-audit --no-fund
COPY frontend/ ./
# 同源部署：前端用相对路径调 /api，不再硬编码 127.0.0.1:8000
ENV VITE_API_BASE=""
RUN npm run build

FROM ${REGISTRY}/library/python:3.11-slim AS runtime
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1
WORKDIR /app

# libgomp 是部分数值依赖的运行时库；chromadb 的 wheel 需要它
RUN apt-get update \
    && apt-get install -y --no-install-recommends libgomp1 \
    && rm -rf /var/lib/apt/lists/*

ARG PIP_INDEX_URL=https://pypi.org/simple
ARG PIP_TRUSTED_HOST=
COPY requirements.txt ./
RUN pip install --retries 10 --timeout 120 -r requirements.txt -i "$PIP_INDEX_URL" \
    ${PIP_TRUSTED_HOST:+--trusted-host "$PIP_TRUSTED_HOST"}

COPY backend ./backend
COPY --from=web /web/dist ./frontend/dist

# 运行期数据目录（挂卷覆盖时不会丢）
RUN mkdir -p backend/data/reports backend/data/audit_bundles backend/data/chroma

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD python -c "import urllib.request;urllib.request.urlopen('http://127.0.0.1:8000/health')"

# --app-dir 让 backend 成为模块根（代码里的 core.* / agents.* 依赖这一点）
CMD ["uvicorn", "app:app", "--app-dir", "backend", "--host", "0.0.0.0", "--port", "8000"]
