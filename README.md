# NovelAI Gateway

NovelAI 透明反向代理网关，支持并发控制、请求排队和 OpenAI API 兼容接口。

## 功能特性

- 透明代理 NovelAI 网站和 API
- 重负载请求（图片生成）自动排队，避免 429 错误
- 自动注入 API 劫持脚本
- 兼容 OpenAI API 格式（`/v1/chat/completions`、`/v1/images/generations`、`/v1/images/edits`、`/v1/models`）
- 支持 NovelAI SDK 风格局部重绘接口（`/v1/images/inpainting`）
- 本地图床，自动保存生成的图片
- 生成统计（按日统计大图/小图数量）
- 可选 Cloudflare Tunnel 自动启动

## 快速开始

### 1. 安装依赖

```bash
uv sync
```

### 2. 配置

复制示例配置并按需修改：

```bash
cp .env.example .env
```

编辑 `.env` 文件：

```env
# 图片访问基础 URL（留空则自动根据请求 Host 生成）
# 如果你有 HTTPS 反代，填写你的公网地址，例如: https://your-domain.com
IMAGE_BASE_URL=

# Cloudflare Tunnel Token（可选，填写后启动时自动开启隧道）
CLOUDFLARE_TUNNEL_TOKEN=

# NovelAI 共享凭据（二选一，SHARED_API_KEY 优先）
# 持久 API Key（推荐，可在 NovelAI Account Settings → API Keys 生成）
SHARED_API_KEY=
# 或 Session Token（JSON 或裸串，来自浏览器 Local Storage 的 session 项）
SHARED_TOKEN=

# 下游访问密码（配置共享凭据时必须设置）
GATEWAY_PASSWORD=请设置一个足够长的随机密码

# 仅可信私网调试时可设为 true；公网部署不要开启
ALLOW_UNAUTHENTICATED_ACCESS=false

# 服务监听配置
HOST=0.0.0.0
PORT=31555

# 并发与冷却
MAX_CONCURRENT=1
COOLDOWN_MIN=0.5
COOLDOWN_MAX=1.0
```

### 3. 启动服务

```bash
uv run main.py
```

或使用 Windows 批处理：

```bash
start.bat
```

### 4. 访问

- 本地访问: `http://127.0.0.1:31555`
- OpenAI 兼容接口: `http://127.0.0.1:31555/v1/chat/completions`

配置共享 NovelAI 凭据后，所有 `/v1/*` 和 `/_api/*` 请求默认要求：

```http
Authorization: Bearer <GATEWAY_PASSWORD>
```

未设置 `GATEWAY_PASSWORD` 时，网关会拒绝使用共享凭据的请求，避免公网地址泄露后被他人消耗账户余额。只有在可信私网中，才应显式设置 `ALLOW_UNAUTHENTICATED_ACCESS=true`。

## 部署到服务器

### 使用 Docker 部署（推荐）

镜像基于 `python:3.11-slim`，已内置 `cloudflared`，可直接使用 Cloudflare Tunnel。

1. 准备配置文件：

```bash
cp .env.example .env
cp config/models.toml.example config/models.toml
# 按需修改 .env 和 config/models.toml
```

2. 构建并启动：

```bash
docker compose up -d --build
```

3. 查看日志：

```bash
docker compose logs -f
```

4. 升级 / 重建镜像：

```bash
git pull
docker compose up -d --build
```

说明：
- 端口默认 `31555`，可在 `.env` 中改 `PORT`，同时修改 `docker-compose.yml` 的端口映射。
- `config/`、`images/`、`logs/` 通过 bind mount 持久化到宿主机，重启不丢数据。
- 如不需要 Cloudflare Tunnel，可去掉 `docker-compose.yml` 里的 `cap_add: NET_ADMIN`。
- 容器内 `main.py` 默认关闭 `reload`；如需调试，可在 `.env` 加 `RELOAD=1`。

### 使用 HTTPS（推荐）

如果你的 NewAPI 或前端是 HTTPS 的，图片 URL 也必须是 HTTPS，否则浏览器会因为 Mixed Content 拒绝加载。

方案：

1. **Nginx 反代 + Let's Encrypt**：在服务器上配置 Nginx 反代到 `127.0.0.1:31555`，并申请 SSL 证书
2. **Cloudflare Tunnel**：填写 `CLOUDFLARE_TUNNEL_TOKEN`，自动提供 HTTPS
3. **frp + HTTPS**：在 frp 服务端配置 TLS

配置好 HTTPS 后，将 `IMAGE_BASE_URL` 设为你的公网 HTTPS 地址：

```env
IMAGE_BASE_URL=https://your-domain.com
```

### 使用 systemd（Linux）

```ini
[Unit]
Description=NovelAI Gateway
After=network.target

[Service]
Type=simple
WorkingDirectory=/path/to/novelai-gateway
ExecStart=/path/to/uv run main.py
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
```

## OpenAI 兼容接口

### POST /v1/chat/completions

将聊天消息转为图像生成。支持 JSON 格式的 prompt：

```json
{
  "model": "nai-diffusion-4-5-full",
  "messages": [
    {"role": "user", "content": "{\"prompt\": \"1girl, blue hair\", \"negative_prompt\": \"lowres\", \"size\": [832, 1216]}"}
  ],
  "stream": true
}
```

### POST /v1/images/generations

标准 OpenAI 图片生成接口，返回 base64 编码的图片。

### POST /v1/images/inpainting

NovelAI SDK 风格局部重绘接口，JSON 请求。`image` 和 `mask` 支持裸 base64 或 `data:image/*;base64,...`；遮罩白色表示重绘、黑色表示保留，网关会自动缩放、二值化并对齐到 NovelAI 需要的 8x8 latent 网格。

```json
{
  "model": "nai-diffusion-4-5-full",
  "prompt": "1girl, standing, smile",
  "image": "iVBORw0KGgoAAAANS...",
  "mask": "iVBORw0KGgoAAAANS...",
  "strength": 1.0,
  "size": "832x1216",
  "response_format": "b64_json"
}
```

### POST /v1/images/edits

OpenAI 兼容图片编辑/局部重绘接口，支持 `multipart/form-data` 的 `image`、`mask`、`prompt`、`model`、`size`、`response_format` 字段，也支持 JSON base64。若省略 `mask`，会尝试使用 `image` 的 alpha 通道作为 OpenAI 风格遮罩。

### 导演工具 (Director Tools)

对单张图片做后处理的一组接口，输入 `image` 为裸 base64 PNG，输出二进制 PNG。均不走排队门控。

| 路径 | 说明 |
|---|---|
| `POST /v1/images/director/declutter` | 去杂物（移除悬浮文字/物体） |
| `POST /v1/images/director/bg-remover` | 背景移除并补全遮挡部分 |
| `POST /v1/images/director/lineart` | 线稿提取 |
| `POST /v1/images/director/sketch` | 草图化 |
| `POST /v1/images/director/colorize` | 线稿上色，可传 `prompt`/`defry` |
| `POST /v1/images/director/emotion` | 情感迁移（改表情），可传 `prompt`/`level` |

```json
// colorize / emotion 额外支持 prompt 等字段
{
  "image": "iVBORw0KGgoAAAANS...",
  "prompt": "red hair, blue eyes",
  "defry": 5
}
```

> ⚠️ 实验性：NAI 上游端点可能变更。

### GET /v1/models

返回支持的模型列表。

## 故障排查

### 429 错误（请求过于频繁）

- 检查 `MAX_CONCURRENT` 是否为 1
- 检查日志中的冷却时间是否正常执行

### 图片无法渲染

- 确认 `IMAGE_BASE_URL` 的协议与前端一致（都是 HTTPS 或都是 HTTP）
- 检查 CORS 是否正常（网关已自动添加 CORS 头）

### 内网穿透无法访问

- 检查防火墙是否放行端口
- 检查穿透工具是否正确转发到网关端口
- 检查 Cloudflare Rocket Loader 是否已关闭

## 技术栈

- FastAPI - Web 框架
- httpx - HTTP 客户端
- BeautifulSoup4 - HTML 解析
- Pillow - 局部重绘图片/遮罩预处理
- python-multipart - OpenAI 图片编辑 multipart 请求解析
- uvicorn - ASGI 服务器
- pydantic-settings - 配置管理
