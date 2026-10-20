FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    DEBIAN_FRONTEND=noninteractive \
    PORT=8080 \
    HOST=0.0.0.0

WORKDIR /app

# 安装系统基础依赖 (curl, ca-certificates, tar)
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    ca-certificates \
    tar \
    procps \
    && rm -rf /var/lib/apt/lists/*

# 安装 Python 依赖
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# 复制工程代码
COPY . .

# 自动准备官方 Codex Linux 独立运行时
RUN bash scripts/setup_codex.sh || true

# 暴露服务端口
EXPOSE 8080

# 默认启动命令
CMD ["python3", "web_server.py", "--host", "0.0.0.0", "--port", "8080"]
