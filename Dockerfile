FROM python:3.11-slim

# 安装系统依赖与 cloudflared（用于可选的隧道功能）
RUN apt-get update \
    && apt-get install -y --no-install-recommends curl ca-certificates \
    && ARCH=$(dpkg --print-architecture) \
    && case "$ARCH" in \
        amd64)  CFD_ARCH=amd64 ;; \
        arm64)  CFD_ARCH=arm64 ;; \
        aarch64) CFD_ARCH=arm64 ;; \
        *) echo "unsupported arch: $ARCH" && exit 1 ;; \
    esac \
    && curl -fsSL -o /usr/local/bin/cloudflared \
        "https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-${CFD_ARCH}" \
    && chmod +x /usr/local/bin/cloudflared \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# 先装依赖，利用层缓存
RUN pip install --no-cache-dir \
        "fastapi>=0.100.0" \
        "uvicorn[standard]>=0.23.0" \
        "httpx[socks]>=0.24.0" \
        "beautifulsoup4>=4.12.0" \
        "pydantic-settings>=2.0.0" \
        "aiofiles>=25.1.0" \
        "pillow>=10.0.0" \
        "python-multipart>=0.0.9"

# 拷贝源码
COPY pyproject.toml ./
COPY src ./src
COPY main.py ./

# 默认配置文件目录（用户可通过 volume 覆盖）
RUN mkdir -p config images logs
COPY config/models.toml.example ./config/models.toml.example

ENV HOST=0.0.0.0 \
    PORT=31555 \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

EXPOSE 31555

CMD ["python", "main.py"]
