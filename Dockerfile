# --- Stage 1: Build K8s MCP Server (Go) ---
FROM golang:latest AS k8s-builder
WORKDIR /app
# 复制子模块源码
COPY kubernetes-mcp-server/ ./
# 注意：根据子模块目录结构，main.go 可能在 cmd/kubernetes-mcp-server/ 目录下
RUN GOOS=linux GOARCH=amd64 go build -o kubernetes-mcp-server ./cmd/kubernetes-mcp-server/main.go

# --- Stage 2: Build Filesystem MCP Server (Node.js) ---
FROM node:22.12-alpine AS fs-builder
WORKDIR /app
# 复制父级 tsconfig 以满足继承关系
COPY servers/tsconfig.json /tsconfig.json
COPY servers/src/filesystem/package*.json ./
# 复制所有源码文件
COPY servers/src/filesystem/*.ts ./
COPY servers/src/filesystem/tsconfig.json ./
# 安装开发依赖以进行编译，然后清理
RUN npm install && npm run build && npm prune --production

# --- Stage 3: Final Runtime Image ---
FROM python:3.12-slim-bookworm

# 安装必要的运行时依赖 (Node.js 用于运行 FS MCP)
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    nodejs \
    npm \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# 设置环境变量
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONPATH="/app" \
    ENVIRONMENT=k8s

# 1. 复制并安装 Python 项目依赖
COPY pyproject.toml ./
RUN pip install --no-cache-dir .

# 2. 集成 K8s MCP Server 二进制文件
COPY --from=k8s-builder /app/kubernetes-mcp-server /usr/local/bin/kubernetes-mcp-server
RUN chmod +x /usr/local/bin/kubernetes-mcp-server

# 3. 集成 Filesystem MCP Server 代码
RUN mkdir -p /usr/local/lib/mcp-filesystem
COPY --from=fs-builder /app/dist /usr/local/lib/mcp-filesystem/
COPY --from=fs-builder /app/package.json /usr/local/lib/mcp-filesystem/
COPY --from=fs-builder /app/node_modules /usr/local/lib/mcp-filesystem/node_modules

# 4. 复制项目主程序代码
COPY . .

# 5. 创建运行时目录
RUN mkdir -p /app/dist /app/kubeConfig /projects

# 暴露端口
EXPOSE 6789

# 启动主程序
CMD ["python", "main.py"]
