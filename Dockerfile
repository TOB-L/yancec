# syntax=docker/dockerfile:1
# 两阶段：Node 打包前端 → Python 运行时直接托管 dist（后端已实现静态托管）
FROM node:20-alpine AS web
WORKDIR /web
# 国内网络可用 --build-arg NPM_REGISTRY=https://registry.npmmirror.com 覆盖
ARG NPM_REGISTRY=https://registry.npmjs.org
COPY frontend/package.json frontend/package-lock.json ./
RUN npm config set registry "$NPM_REGISTRY" && npm ci
COPY frontend/ ./
# 同源部署：前端用相对路径调 /api，不再硬编码 127.0.0.1:8000
ENV VITE_API_BASE=""
RUN npm run build

FROM python:3.11-slim AS runtime
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1
WORKDIR /app

# libgomp 是部分数值依赖的运行时库；chromadb 的 wheel 需要它
RUN apt-get update \
    && apt-get install -y --no-install-recommends libgomp1 \
    && rm -rf /var/lib/apt/lists/*

ARG PIP_INDEX_URL=https://pypi.org/simple
COPY requirements.txt ./
RUN pip install -r requirements.txt -i "$PIP_INDEX_URL"

COPY backend ./backend
COPY --from=web /web/dist ./frontend/dist

# 运行期数据目录（挂卷覆盖时不会丢）
RUN mkdir -p backend/data/reports backend/data/audit_bundles backend/data/chroma

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD python -c "import urllib.request;urllib.request.urlopen('http://127.0.0.1:8000/health')"

# --app-dir 让 backend 成为模块根（代码里的 core.* / agents.* 依赖这一点）
CMD ["uvicorn", "app:app", "--app-dir", "backend", "--host", "0.0.0.0", "--port", "8000"]
