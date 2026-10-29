# NovelAI Gateway

NovelAI 多账号网关，提供 OpenAI 兼容接口、NAI 生成参数扩展、原生透明代理和管理控制台。

版本 **0.3.0**。本文为仓库的完整使用与部署文档，更新于 **2026-10-30（UTC+8）**。

## 目录

1. [能力与兼容范围](#能力与兼容范围)
2. [安装与启动](#安装与启动)
3. [配置与账号](#配置与账号)
4. [模型](#模型)
5. [图像请求格式](#图像请求格式)
6. [组合功能示例](#组合功能示例)
7. [图像响应与流式](#图像响应与流式)
8. [工具接口](#工具接口)
9. [文本与语音](#文本与语音)
10. [Anlas 与 usage](#anlas-与-usage)
11. [下游销售定价](#下游销售定价)
12. [队列与额度](#队列与额度)
13. [管理接口](#管理接口)
14. [原生代理与访问控制](#原生代理与访问控制)
15. [部署更新与排障](#部署更新与排障)
16. [开发与验证](#开发与验证)

## 能力与兼容范围

| 能力 | OpenAI 兼容入口 | 说明 |
|---|---|---|
| 文生图 | `POST /v1/images/generations` | OAI 请求与 `data[].b64_json/url` 响应 |
| 图生图 | 同上，`action=img2img` | `image` 与参考图可以同时提交 |
| 局部重绘、扩图 | 同上，`action=infill` | `image` + `mask`；扩图需要调用方先扩展画布并制作遮罩 |
| 多角色、角色位置与正负提示词 | 同上，`characters` 或原生 V4 提示词结构 | 与图生图、重绘、参考图组合 |
| Vibe 风格参考 | 同上，`reference_images` | 编码、复用编码、原生缓存字段 |
| 精密角色/风格参考 | 同上，`references` | Character / Style / Character & Style |
| ControlNet 条件图 | 同上，`controlnet_*` | 条件图与输入图分别保留 |
| 透明背景与 WebP | 同上，V5 参数与 `image_format` | 保留有效透明通道；WebP 按原始格式返回 |
| 流式生成 | 同上，`stream=true` | 或 `/v1/images/generations/stream`，返回 SSE |
| 超分、Director、Vibe 编码、条件图预处理、标签建议 | 同上，`novelai_operation` | 也提供专用路由，见工具表 |
| 文本对话、续写、token 计数 | `/v1/chat/completions`、`/v1/completions`、`/v1/token-count` | 需要启用对应模型 |
| 语音合成 | `/v1/audio/speech` | v1/v2、预设音色、自定义 seed、MP3/Opus |
| NAI 原生接口 | `/ai/*`、`/_api/*` 等 | 保留需要原生协议的调用方式 |

这里的“OAI 兼容”是标准路径、鉴权与响应，加上 NAI 专属字段；标准 OAI Images 本身没有角色参考、Vibe 等字段。OpenAI SDK 请用 `extra_body` 提交这些扩展。中转平台必须允许这些字段或透传扩展请求头，网关无法恢复已被中转平台删除的数据。

网关支持构造与转发 NAI 生成 API 的参数及组合，**实际可用性仍取决于所选上游模型、订阅和当前 NAI 服务**。例如某模型尚未实现精密参考、ControlNet 或透明输出时，透传参数不会让它获得该能力；上游会返回错误，网关不会静默切换模型。Medium 另有严格约束，见下文。

故事编辑器、Lorebook 编辑、网页画布操作等前端行为不属于 OAI 生成协议。需要这些功能的应用应自行构建上下文、图像和遮罩，再调用对应 API。

官方参考：[图像 API](https://image.novelai.net/docs/index.html)、[文本 API](https://text.novelai.net/docs/index.html)、[主 API](https://api.novelai.net/docs/)、[精密参考](https://docs.novelai.net/en/image/precisereference/)、[图生图](https://docs.novelai.net/en/image/controltools/)。官方接口可能调整，当前代码与实际响应优先。

## 安装与启动

要求：Python **3.11+**；使用 Docker 时需要 Docker Engine 和 Compose v2。图像处理依赖 Pillow，基础内存建议至少 512 MiB，根据并发和参考图体积增加。

```bash
git clone https://github.com/fuilyha56-wq/novelai-gateway.git
cd novelai-gateway
cp .env.example .env
# 编辑 .env，至少填写 GATEWAY_PASSWORD 和 SHARED_API_KEY
uv sync
uv run python main.py
```

也可以使用标准 Python 虚拟环境：

```bash
python -m venv .venv
# Linux/macOS：source .venv/bin/activate
# Windows PowerShell：.venv\Scripts\Activate.ps1
python -m pip install -e .
python main.py
```

Windows 可用 `Copy-Item .env.example .env` 代替 `cp`，或运行 `start.bat`。配置完成后默认根地址为 `http://127.0.0.1:31555`，管理页面为 `/admin`。

Docker：

```bash
docker compose up -d --build
docker compose ps
docker compose logs --tail 100 novelai-gateway
```

Compose 会从 `.env` 读取 `PORT`，同时作为宿主机和容器端口。默认 31555；设置 41555 时两端均使用 41555。默认发布不可变镜像，不挂载 `src`；修改源码后必须重新构建并创建容器。

第一次启动会从 `config/models.toml.example` 生成 `config/models.toml`。

## 配置与账号

### 环境变量

`.env` 为 `KEY=value`。JSON 数组值建议用单引号包裹。不要把额外的业务配置键随意写入该文件，配置解析会检查未知字段。

| 变量 | 默认值 | 含义 |
|---|---|---|
| `HOST` | `0.0.0.0` | 服务监听地址 |
| `PORT` | `31555` | 服务端口 |
| `GATEWAY_PASSWORD` | 空 | 下游访问与管理员密码 |
| `SHARED_API_KEY` | 空 | 单个 NovelAI Persistent API Token |
| `SHARED_API_KEYS` | 空 | 多账号 JSON，或逗号/分号/换行分隔的 Token |
| `SHARED_TOKEN` | 空 | 兼容旧 Session Token；裸串或含 `auth_token` 的 JSON |
| `ALLOW_UNAUTHENTICATED_ACCESS` | `false` | 显式允许无密码使用共享凭据；仅用于受信任私网 |
| `MODELS_CONFIG` | `config/models.toml` | 模型表路径 |
| `IMAGE_DIR` | `images` | URL 格式输出的存储目录 |
| `IMAGE_BASE_URL` | 空 | 对外图床根地址；空时根据请求地址推导 |
| `MAX_CONCURRENT` | `1` | 没有可用账号池容量时的并发基数 |
| `QUEUE_TIMEOUT` | `300` | 排队等待上限，秒 |
| `QUEUE_MAX_WAITERS` | `8` | 允许等待的重负载请求数 |
| `COOLDOWN_MIN` / `COOLDOWN_MAX` | `0.5` / `1.0` | 单账号请求间随机冷却，秒 |
| `UPSTREAM_TIMEOUT` | `420` | 上游请求超时，秒；与排队超时分别计算 |
| `V5_QUOTA_ENABLED` | `true` | 启用网关自己的 V5 日/滚动周限制 |
| `V5_DAILY_LIMIT` | `190` | 所有 V5 成功生成合计日限额 |
| `V5_WEEKLY_LIMIT` | `1730` | 所有 V5 成功生成合计滚动 7 天限额 |
| `CLOUDFLARE_TUNNEL_TOKEN` | 空 | 可选 Cloudflare Tunnel Token |
| `NOVELAI_BASE_URL` | `https://novelai.net` | 官方网页上游 |
| `NOVELAI_API_URL` | `https://api.novelai.net` | 主 API、语音等 |
| `NOVELAI_IMAGE_URL` | `https://image.novelai.net` | 图像、Vibe、超分、部分用户数据、原生 OAI 接口 |
| `NOVELAI_TEXT_URL` | `https://text.novelai.net` | 原生文本生成 |
| `HEAVY_PREFIXES` | 代码内生成/工具路由集合 | 高级选项，JSON 字符串数组；控制原生代理的排队路径 |

`RELOAD=1` 是 `main.py` 的开发进程环境变量，不写进 `.env`。代理需要通过进程环境或 Compose override 设置 `http_proxy`、`https_proxy`、`all_proxy`、`no_proxy`，使用可从实际容器网络访问的地址，仓库不内置私有代理 IP。

### 多账号

```dotenv
GATEWAY_PASSWORD=replace-with-a-long-random-password
SHARED_API_KEYS='[{"id":"account-1","name":"主账号","key":"pst-your-token","weight":1,"enabled":true,"allow_v5":true,"allow_anlas":true}]'
```

每个账号可配置 `id`、`name`、`key`、`weight`（1–100）、`enabled`、`allow_v5`、`allow_anlas`。关闭 `allow_anlas` 后只能承接网关判定在免费边界内的请求，付费编码、精密参考、超分和优先服务不会借用该账号。

首次初始化后，账号保存在 **`config/accounts.json`**。已有该文件时，它优先于环境变量，后续请使用控制台管理账号；只改 `.env` 不会覆盖已有持久化账号。

账号选择采用平滑加权轮询；队列按完整凭据预留空闲账号。同一 Token 的重复配置只代表一个上游账号，不增加并发或余额。`credential_group` 是公开的去重分组标识，不包含完整凭据。

## 模型

`GET /v1/models` 返回当前已启用的模型条目。模型别名与上游名通过 `config/models.toml` 配置；图像还允许传原生模型名，最终权限由上游决定。

| 常用别名 | 上游模型 |
|---|---|
| `nai-v4.5-full` | `nai-diffusion-4-5-full` |
| `nai-v4.5-curated` | `nai-diffusion-4-5-curated` |
| `nai-v4.5-inpaint` | `nai-diffusion-4-5-full-inpainting` |
| `nai-v5-full` / `nai-v5-curated` | `nai-diffusion-5-full` / `nai-diffusion-5-curated` |
| `nai-v5-inpaint` | `nai-diffusion-5-full-inpainting` |
| `nai-v5-full-medium` | `nai-diffusion-5-full-medium` |
| `nai-v5-inpaint-medium` | `nai-diffusion-5-full-medium-inpainting` |

示例表还包含 V3、V3 Furry、V4 等条目。`-limit` 是网关提供的免费边界限制别名，不是新的 NAI 模型；重绘时自动解析对应 inpainting 模型。

`image_enabled` 默认开启，`chat_enabled`、`tts_enabled` 默认关闭。模型列表按这些开关展示；文本与语音处理器也检查类别开关。文本/语音必须使用已配置的对应类型条目，不会静默回退到其他模型。

### Medium 约束

Medium 使用固定 14 步、Euler Ancestral、Heavy UC、特定参数与负面提示词。允许文生图、图生图和重绘，面积不超过 3,145,728；不接受优先服务、参考图、ControlNet、付费工具、自定义负面提示词和角色负面提示词。与固定配置冲突的请求返回 400。不得用这些限制推断 Full/Curated 的能力。

## 图像请求格式

统一使用：

```http
POST /v1/images/generations
Authorization: Bearer <GATEWAY_PASSWORD>
Content-Type: application/json
```

```json
{
  "model": "nai-v5-full",
  "prompt": "a quiet landscape",
  "negative_prompt": "",
  "qualityToggle": false,
  "size": "832x1216",
  "steps": 28,
  "n": 1,
  "seed": 12345,
  "response_format": "b64_json"
}
```

| 字段 | 默认/范围 | 含义 |
|---|---|---|
| `model` | `nai-diffusion-4-5-full` | 图像模型名或配置别名 |
| `prompt` | 空字符串 | 正面提示词；兼容 `input` |
| `negative_prompt` | 网关默认 UC | 显式 `""` 保持为空 |
| `qualityToggle` | `true` | 是否添加默认质量词；`false` 同时停止网关注入 |
| `quality_tags` | `, very aesthetic, masterpiece, no text` | 自定义追加词；`null` 不追加 |
| `size` | `1024x1024` | `宽x高`；`width`、`height` 优先 |
| `width` / `height` | 64 的倍数 | 总面积 ≤ 3,145,728；还受每样本估算 140 Anlas 上限约束 |
| `steps` | 28，1–50 | Medium 固定 14 |
| `n` / `n_samples` | 1，1–8 | `n_samples` 优先；用于生成、额度与计费 |
| `scale` / `guidance_scale` | 5，0–10 | 提示词引导；`scale` 优先 |
| `seed` | 未传时随机 | 0–4,294,967,295；显式 0 保留 |
| `sampler` | `k_euler_ancestral` | 还可传其他上游支持的采样器 |
| `noise_schedule` | `karras` | 如 native/exponential/polyexponential，模型适配由 NAI 判断 |
| `cfg_rescale` / `noise` | 0 / 0 | CFG 重缩放、噪声 |
| `action` | 按输入推导 | 无输入图：generate；有图：img2img；有遮罩：infill |
| `image` / `mask` | Base64 或 data URL | 输入图与重绘遮罩；必须符合上游尺寸/格式要求 |
| `strength` | 图生图 0.7，0–1 | 图生图强度；重绘使用 `inpaintImg2ImgStrength`，默认 1 |
| `extra_noise_seed` | seed 或随机 | 图生图噪声 seed |
| `service_tier` | 未指定 | `priority` 视为消耗 Anlas；`-limit` 禁用 |
| `image_format` | 上游默认 | `png` / `webp` |
| `response_format` | `b64_json` | b64_json/url/raw/nai_json/auto |
| `stream` | false | `true` 返回 OAI 扩展 SSE |

显式 `action=generate` 不能携带输入 `image`，避免输入图被忽略；请选择 img2img 或 infill。V4/V4.5/V5 自动构造所需 V4 提示词结构和 `params_version`（V5 为 4，其他 V4 家族为 3），并关闭不支持的 SMEA 字段。

### 原生参数与优先级

支持顶层 NAI 参数，也支持 `parameters` 原生参数对象、`extra_params` 扩展对象，以及 JSON 请求头 `X-NovelAI-Extra-Params`。常规优先级为：

```text
parameters < 顶层字段 < extra_params < X-NovelAI-Extra-Params < 对应 X-* 专用请求头
```

`extra_params` 可以携带 `image`、`mask`、`references`、`characters` 等组合输入；不能覆盖 `model`、`prompt`、`action`、`response_format`、`size`、操作分派和流式协议字段。`parameters` 也不允许嵌套这些网关协议字段。关键数值覆盖在转发前校验，不能绕过 `-limit` 或账号权限。

```json
{
  "model": "nai-v5-full",
  "prompt": "a transparent crystal",
  "parameters": {"cfg_rescale": 0.3, "color_correct": true},
  "extra_params": {"steps": 30, "transparent_background": true}
}
```

其他未保留的字段进入 NAI `parameters`，便于适配新增参数。已构造的结构字段和模型强制配置优先，不能依靠重复别名改变含义。V5 默认 `straight_alpha=true`；`transparent_background` 映射为 `tag_hint_transparent_background`。V5 专属字段在其他模型上剔除，不代表其他模型支持该功能。

专用请求头：`X-Sampler`、`X-Noise-Schedule`、`X-Negative-Prompt`、`X-Service-Tier`、`X-Steps`、`X-Seed`、`X-N-Samples`、`X-Scale`。经过中转时必须检查中转自身的字段/请求头透传配置。

### multipart

生成及编辑别名支持 multipart：`image`、`mask`、`reference_image` 可上传文件；多个 `reference_images` 或 `reference_images[]` 文件作为 Vibe 列表；`references`、`characters`、`parameters`、`extra_params` 等复杂字段使用 JSON 字符串。不要手动设置 multipart boundary。

`/v1/images/edits` 未传 mask 时，从输入图 alpha 提取“透明区域重绘”遮罩；普通 `/generations` 的 infill 和 `/inpainting` 要求显式 mask。调用方要明确区分 OAI alpha 遮罩与自己提供的 NAI mask。

## 组合功能示例

### 图生图 + 真正的角色精密参考

```json
{
  "model": "nai-v4.5-full",
  "prompt": "a character sitting in a cafe",
  "action": "img2img",
  "image": "<输入图Base64>",
  "strength": 0.55,
  "size": "832x1216",
  "references": [
    {
      "reference_image": "<角色参考图Base64>",
      "reference_type": "character",
      "strength": 0.8,
      "fidelity": 0.7
    }
  ]
}
```

`image` 决定输入画面，`references` 决定精密参考，两者在**同一个生成请求**中转发。换为 `action=infill`，并补 `mask`，即可用于局部重绘。

精密参考类型可为 `character`、`style`、`character&style`。参考图自动适配到支持的标准画幅，使用 `director_reference_images`，不进行 Vibe 编码。`strength` 写入主参考强度，`fidelity` 映射为 `1-fidelity` 的二级强度，原生 information extraction 固定为 1。接受有限数值；上游可能限制特定模型或范围。不要把参考图数量理解成独立角色绑定，是否混合由 NAI 模型决定。

### 多角色、Vibe 和条件图

```json
{
  "model": "nai-v4.5-full",
  "prompt": "two people in a cafe",
  "image": "<输入图Base64>",
  "characters": [
    {"prompt":"red hair","negative_prompt":"hat","center":{"x":0.25,"y":0.5}},
    {"prompt":"blue hair","center":{"x":0.75,"y":0.5}}
  ],
  "reference_images": ["<Vibe原图或已编码数据>"],
  "controlnet_condition": "<预处理条件图Base64>",
  "controlnet_model": "hed",
  "controlnet_strength": 0.8
}
```

`characters` 可仅包含提示词，也可附带 `reference_image`，后者是历史 **Vibe 角色参考**，会走 Vibe 编码，不等同于 `references` 精密参考。角色坐标为 0–1，支持 `center` 或 `centers`；可直接传 `v4_prompt` / `v4_negative_prompt` 精确控制正负角色结构。网关最多接收 22 个角色，具体模型可能有更低限制。

Vibe 总数上限 16，包含独立 Vibe、角色附图和缓存项。可传单个 `reference_image` 或列表 `reference_images`，两种别名不要同时使用。强度与信息提取可用 `reference_strength` / `reference_information_extracted`，或同长度的 `*_multiple` 数组。原生 `reference_image_multiple`、`reference_image_multiple_cached` 和 `director_reference_*` 也可透传。

Vibe 与精密参考保存在不同字段，网关允许组合；特定模型是否支持一起执行由上游决定。原始 Vibe 图先编码；已编码 Vibe 数据可复用，响应 `data[].vibe` 可直接用于后续请求。

### Python OpenAI SDK

```python
import base64
from pathlib import Path
from openai import OpenAI

client = OpenAI(api_key="<GATEWAY_PASSWORD>", base_url="http://127.0.0.1:31555/v1", timeout=900)
b64 = lambda p: base64.b64encode(Path(p).read_bytes()).decode()
result = client.images.generate(
    model="nai-v4.5-full",
    prompt="a character sitting in a cafe",
    size="832x1216",
    response_format="b64_json",
    extra_body={
        "action": "img2img", "image": b64("input.png"), "strength": 0.55,
        "references": [{"reference_image": b64("character.png"), "reference_type": "character"}],
    },
)
Path("result.png").write_bytes(base64.b64decode(result.data[0].b64_json))
```

## 图像响应与流式

常规 OAI JSON：

```json
{
  "created": 1790000000,
  "data": [{"b64_json":"<图片Base64>","revised_prompt":"原始请求提示词"}],
  "usage": {"prompt_tokens":1250,"completion_tokens":1,"total_tokens":1251}
}
```

`revised_prompt` 是网关回显，不是 NAI 自动改写文本。使用 `url` 时图片保存至 `IMAGE_DIR`，通过 `/images/<随机文件名>` 访问；配置反代时正确设置 `IMAGE_BASE_URL`，并自行规划图片存储与清理。

| response_format | 实际响应 |
|---|---|
| `b64_json` / `auto` | 提取全部上游 PNG/WebP，返回 OAI JSON |
| `url` | 保存图片并返回 `data[].url` 与 usage |
| `raw` | 原始 ZIP bytes，计费信息在响应头 |
| `nai_json` | NAI 原生 JSON；对象型响应补充 usage |

响应头包括 `X-Anlas-Cost`、`X-Prompt-Tokens`、`X-Completion-Tokens`。图片字节保留必要元数据与有效透明通道。随机 URL 不是私有图片权限系统，持有图片链接者可能直接读取。

### SSE

`POST /v1/images/generations` 携带 `stream:true`，或调用 `/v1/images/generations/stream`。图生图、重绘和参考图仍使用相同请求字段。

事件：`image_generation.started` → 零个或多个 `image_generation.partial_image` → `image_generation.completed` → `data: [DONE]`。completed 包含最终 `data[]` 和 usage；单图时还提供 `b64_json`。partial 可能包含上游预览及 `novelai` 原始事件，不是最终图片。

上游错误、缺失最终帧或截断会发送 `event:error`，不会伪造 completed。账号预留持续到流结束/断开；最终成功样本计入统计。响应头的价格是完整请求的估算，流中失败或部分成功仍应由结算方按最终结果处理，不能仅凭预估头确认全批成功。关闭反代 SSE 缓冲，设置足够的读超时。

## 工具接口

统一入口指定 `novelai_operation`，或请求头 `X-NovelAI-Operation`；同时提供时必须一致。

```json
{"model":"nai-v5-curated","novelai_operation":"upscale","image":"<Base64>","declared_blur_sigma":0}
```

| operation | 专用路由 | 额外输入 |
|---|---|---|
| generate | `/v1/images/generations` | 标准生成；可使用 action |
| img2img | `/v1/images/img2img` | image、strength |
| inpainting | `/v1/images/inpainting` | image、mask |
| edits | `/v1/images/edits` | image、mask 或 alpha 遮罩 |
| vibe-transfer | `/v1/images/vibe-transfer` | Vibe 列表 |
| character-reference | `/v1/images/character-reference` | 历史 Vibe 角色模式，characters |
| precise-reference | `/v1/images/precise-reference` | 真正的精密参考，references |
| encode-vibe | `/v1/images/encode-vibe` | image、model、information_extracted |
| upscale | `/v1/images/upscale` | image、V5 Full/Curated、declared_blur_sigma |
| annotate | `/v1/images/annotate` | image、预处理 model |
| suggest-tags | `/v1/images/suggest-tags` | model、prompt，可选 lang |
| director-declutter | `/v1/images/director/declutter` | image、width、height |
| director-bg-remover | `/v1/images/director/bg-remover` | 同上 |
| director-lineart | `/v1/images/director/lineart` | 同上 |
| director-sketch | `/v1/images/director/sketch` | 同上 |
| director-colorize | `/v1/images/director/colorize` | 同上、prompt、defry（0–5） |
| director-emotion | `/v1/images/director/emotion` | 同上、prompt、defry（0–5） |

生成别名仍可组合输入图、遮罩与参考图，不再各自实现一套不一致的生成逻辑。统一入口的二进制工具结果包装为 OAI `data[].b64_json`；专用 upscale/annotate/director 路由保留 `image/png` 二进制响应。encode-vibe 返回 `object:vibe`、`reference_image` 编码与 usage；标签建议返回上游 JSON。

超分为 V5 扩散 2 倍超分，仅支持 `nai-v5-full`/`nai-v5-curated` 及对应原生名称。输入面积上限 3,145,728；不再接受旧版 ESRGAN 的 scale/width/height/defry 参数。Enhance 可用图生图配合目标尺寸、strength、noise 完成，不是独立超分 API。

annotate 模型可选 canny/hed/midas/mlsd/openpose/uniformer/fake_scribble；其输出可作为后续请求的 `controlnet_condition`。生成模型是否支持该 ControlNet 仍由 NAI 决定。

encode-vibe 还支持 mask、crop_to_mask、focus_seed、info_extract_seed；information_extracted 范围 0–1。复用已编码 Vibe 不需要再次调用编码接口。

## 文本与语音

### 文本

先在模型表启用 `chat_enabled=true`，保留订阅实际允许的条目，例如 `nai-chat-erato` → `llama-3-erato-v1`、`nai-chat-kayra` → `kayra-v1`。

```json
{
  "model":"nai-chat-erato",
  "messages":[{"role":"system","content":"Write a short story."},{"role":"user","content":"A quiet cafe."}],
  "max_tokens":200,
  "temperature":0.9,
  "stream":false,
  "parameters":{"repetition_penalty":1.1,"order":[6,0,1,2,3]},
  "min_p":0.05
}
```

`/v1/chat/completions` 将 messages 组织为原生文本上下文，支持流式/非流式。`parameters`、`extra_params` 和非协议顶层字段透传到 NAI 文本参数，可设置 repetition penalty、采样顺序、stop_sequences、词偏置等。`max_tokens`/`max_completion_tokens` 优先于原生 max_length，要求为正，上限由所选模型控制。多模态消息只提取文字，不提供视觉理解；该适配器不实现工具调用、JSON schema 约束输出或图像理解。

`POST /v1/completions` 将标准续写请求转发至 NAI `/oa/v1/completions`，保留其扩展字段与流式响应。`POST /v1/token-count` 接受 NAI 的 token-count 请求结构，例如 `{"model":"nai-chat-erato","prompt":{"ofString":"hello"}}`，不是标准 OpenAI 路由。原生上下文 token 上限、模型可用性与订阅由上游验证。

Chat 的 usage 当前为 0 占位，不能作为实际文本 token 或收费依据；原生 Completions 保留上游返回的 usage。图像的合成计费 token 也不可用于文本。

### TTS

先启用 `tts_enabled=true`。调用 `POST /v1/audio/speech`：

```json
{"model":"tts0-v2-mp3","input":"Hello world","seed":"custom-voice","version":"v2","response_format":"mp3"}
```

模型表内的 `tts0/tts1-v1/v2-mp3/opus` 映射音色、版本和编码。可设置 version（v1/v2）、response_format（mp3/opus）、voice_id（-1/0/1）、seed 或 voice 字符串。自定义 seed 必须使用 voice_id=-1 或省略该字段；voice 与 seed 冲突时拒绝。seedmix 仅支持 v2。返回音频二进制，不返回图像 usage。

## Anlas 与 usage

Gateway 不直接扣人民币；它估算 NAI 消耗，并提供下游结算所需字段。以下区分三层：**上游 Anlas、Gateway 合成 usage、运营方销售价格**。

### 图像 Anlas 估算

设 `R=max(width×height,65536)`、步数 `S`、样本数 `N`：

```text
base = ceil(2.951823174884865e-6 × R + 5.753298233447344e-7 × R × S)
per_sample = max(2, ceil(base × model_factor × strength_factor))
per_sample = ceil(per_sample × uncond_scale)
```

普通模型 model_factor=1，V5=1.5。文生图 strength_factor=1，图生图使用 strength，重绘使用 inpaintImg2ImgStrength。Medium 固定 S=14，步数项另乘 `1/1.06521739`，模型倍率为 1.5。

在该生成费用上增加：

- 精密参考：每参考图每生成样本 **5 Anlas**。
- Vibe 原图编码：每张 **2 Anlas**；已编码复用不重复收编码费。
- 超过 4 张 Vibe：每超出一张另加 **2 Anlas**。
- `-limit` 满足免费边界时扣除一个可免费生成样本的基础费用；付费参考等本身不能靠该模型绕过限制。

| 工具 | 估算 Anlas |
|---|---:|
| encode-vibe | 2/次 |
| declutter / lineart / sketch / colorize / emotion | 5/次 |
| bg-remover | 65/次 |
| annotate | 0（网关不计独立图像 Anlas） |
| V5 超分：输入 ≤1,048,576 像素 | 1 |
| 输入 ≤1,747,627 | 2 |
| 输入 ≤2,446,678 | 3 |
| 输入 ≤3,145,728 | 4 |

这是代码中的估算口径，不是实时读取官方扣款结果。普通 Full 模型即使可能由 Opus 免费额度支付，X-Anlas-Cost 仍可能报告未应用该订阅优惠的估算；免费边界和实际销售收费通过下述 usage 档位分别表达。运营方应使用专门测试账号核对实际订阅余额，不能把该头当作官方账单。

### usage 含义

| 场景 | prompt_tokens |
|---|---:|
| V4.5 免费边界内 | 0 |
| V5 High 免费边界内 | 25 |
| V5 Medium 免费边界内 | 18 |
| 边界外的动态请求 | `max(1, Anlas × 50)` |

0/25/18 是**协议计费档位标记，不是文本 token，也不是 Anlas 数量**。图像生成的 `completion_tokens` 是可计基础费的成功请求样本数量；独立工具通常为 0。`total_tokens=prompt_tokens+completion_tokens`。

免费边界要求：单样本、≤28 步、≤1,048,576 像素、非 priority、无精密参考、最多四张已编码 Vibe、动作 generate/img2img/infill；generate 不能携带输入图。原图 Vibe 编码和付费工具属于边界外。完整版边界外仍允许请求并动态计费，`-limit` 则直接 400。

例如，V5 2048×1536、28 步、图生图 strength=0.5、两样本、一个精密参考：基础每样本 45，参考费 10，总估算 100 Anlas；usage 为 prompt=5000、completion=2、total=5002。

## 下游销售定价

价格由下游平台配置，**不是启动 Gateway 就自带的统一售价**。以下是 2026-10-30 只读核验的现有 NewAPI/LFN 集成快照，供维护这套部署使用；自行部署应按自己的销售策略配置，不应照抄预扣价或汇率。

### NewAPI 当前图像策略

当前实际结算使用 `tiered_expr`，显示兑换参数 S=200，Draw 分组倍率=1。令 `p=prompt_tokens`、`c=completion_tokens`：

```text
V4.5:
p <= 1 ? tier("limit", p * 0 + c * 0 + 1000000)
       : tier("full", p * 80000 + c * 1000000)

V5 High:
p == 25 ? tier("limit", p * 0 + c * 0 + 5000000)
        : tier("full", p * 100000 + c * 5000000)

V5 Medium:
p == 18 ? tier("limit", p * 0 + c * 0 + 1000000)
        : tier("full", p * 100000 + c * 1000000)
```

在这套换算下，动态普通 Anlas=¥0.02，V5 Anlas=¥0.025；基础费 V4.5/Medium=¥0.005/样本，V5 High=¥0.025/样本。固定档单图分别为 ¥0.005/¥0.025/¥0.005。V4.5 的 `p<=1` 同时兼容部分 NewAPI 将 0 钳成 1 的行为。

`-limit` 在当前 NewAPI 配置使用 ratio/按次价，对应 ModelPrice 为 V4.5=1、V5=5；在 S=200 时分别为 ¥0.005/¥0.025。其他完整版条目的 ModelPrice 可能只是预扣参考，不是 tiered_expr 的实际结算单价。不同 NewAPI 版本和分组的单位换算应独立确认。

### LFN 当前站内策略

LFN 的每日策略与 NewAPI 直连结算是两套配置：当前站内普通/V5 Anlas 单价均为 ¥0.01；V4.5 基础费为 0；V5 基础费 ¥0.04，日累计图片达到 75/125/200 后总倍率为 1.10/1.15/1.20。该快照中 `strictApiBilling=false`，因此不要假定所有直连接口都应用站内策略。

Gateway 的变更不修改这些平台价格、钱包或阶梯。调整销售价格时，应同时验证档内、档外、批量、参考图、超分、失败和流式账单。不要在一次付费超时后盲目重试，先核对是否已成功生成或入账。

## 队列与额度

重负载请求在执行编码和生成之前排队，原始 Vibe 编码与最终生成共用一次队列与账号。每个唯一凭据只占一个可用并发槽；账号能力改变后会再次核验绑定凭据，避免付费请求落入禁用 Anlas 的账号。单账号模式额外应用冷却，多账号按空闲账号分摊。

队列满返回 429，账号能力不满足可能返回 409，排队超时与上游请求超时分开计算。重试只针对部分明确的上游并发锁错误，不自动无限重试付费生图。

V5 日限额按 UTC+8 的自然日统计，周限额为滚动 7 天窗口。**这是网关全局生成数量限制，默认不随账号数自动乘倍**；它与官方每账号的动态百分比额度不是同一个东西。统计保存在 `logs/v5_daily_usage.json`，成功批量按 n_samples 计数；预检和成功计数分离，极端并发下存在少量超出窗口上限的可能。

检测到官方免费额度耗尽时，会自动关闭该账号 V5 能力；探测到恢复百分比严格大于 15% 时可自动恢复。不要依靠关闭网关全局限额解除官方限制。

总览展示全部独立账号的剩余订阅+购买 Anlas，以及各账号非负可用 V5 百分比之和。**100%=一个账号的完整额度，可以显示 162%、300% 等**。重复凭据只计一次，停用/禁用计费能力的账号仍属于“全部账号”；过期或非 Opus 订阅不贡献可用 V5。拉取缺失会注明覆盖数量与“已知合计”，不会冒充完整总额。总览每 60 秒刷新上游余额，亦可手动刷新。

## 管理接口

访问 `/admin` 输入 GATEWAY_PASSWORD。管理 API 统一位于 `/admin/api`，使用 `Authorization: Bearer <GATEWAY_PASSWORD>`。响应不返回完整 Token。

| 方法/路径 | 用途 |
|---|---|
| GET session | 验证密码 |
| GET overview / status | 脱敏配置、模型、生成统计与账号状态 |
| GET accounts | 账号列表、能力、busy、credential_group |
| POST accounts | 新增账号，key 必填 |
| PUT accounts/{id} | 更新 name/key/weight/enabled/allow_v5/allow_anlas |
| DELETE accounts/{id} | 删除账号配置 |
| POST accounts/{id}/reset | 清除失败/冷却状态 |
| POST accounts/{id}/test | 查询轻量上游接口测试账号 |
| PUT accounts/{id}/enabled | `{ "enabled": true }` |
| PUT accounts/{id}/allow-v5 | `{ "allow_v5": true }` |
| PUT accounts/{id}/allow-anlas | `{ "allow_anlas": true }` |
| GET concurrency | 各能力有效并发汇总，供受信任下游使用 |
| GET upstream/account-data?account_id=... | 同一账号的 subscription + user_data |
| GET upstream/subscription / upstream/user/data | 单项上游数据，可传 account_id |
| GET env / PUT env | 读取脱敏配置或更新允许项，部分需要重启 |
| GET models/config / PUT models/config | 读取/保存 TOML，校验后写盘 |
| POST models/refresh | 抓取并更新上游模型候选信息 |
| GET logs / logs/recent | 日志尾部与 cursor 增量读取 |
| GET model-latency | 最近窗口的模型调用耗时数据 |
| GET model-latency.png | PNG 统计图 |
| POST restart | 请求重启；生产环境应确保有容器重启策略 |

控制台还支持账号开关、编辑、能力分组、模型耗时图、日志与本地壁纸设置。模型表和环境文件的修改是否立即生效以接口说明为准；账号管理会持久化并立即更新当前账号池。

## 原生代理与访问控制

OAI 客户端 Base URL 以 `/v1` 结尾；NAI 原生客户端通常填写网关根地址。不要重复拼接 `/v1/v1` 或 `/ai/ai`。

原生 `/ai/generate-image`、`/ai/generate-image-stream` 等保留 NAI 载荷和二进制协议。multipart 原生调用使用 `request` JSON 部分及其引用的图像文件。原生 MessagePack 流需要客户端按原协议解帧，不能直接当 OAI SSE 或图片读取。

图像/工具路由转到图像上游，原生文本转到文本上游，TTS 转到主 API；原生 OAI `/oa/v1/*` 的目标使用图像上游兼容服务。部分 `/user/*` 接口被代理，这不是按下游用户隔离的账户系统。

配置共享凭据时，OAI `Authorization` 使用网关密码，不是上游 pst Token。未设置密码默认拒绝共享凭据使用；显式私网选项是例外。管理接口仍要求非空管理员密码。

共享网关密码相当于管理员权限；公开运营应在前面部署独立鉴权/计费/路由隔离层，不应把该密码作为每位终端用户的普通 API Key。保护 `.env`、`config/accounts.json`、配置备份和日志，生产只开放必要端口。Persistent Token 的权限与处理要求见官方 API 文档。

## 部署更新与排障

### 数据与发布

需要持久化：`config/`（账号与模型）、`images/`（URL 图片）、`logs/`（统计与日志）。不要在更新时覆盖这些目录或 `.env`。

```bash
git pull --ff-only
docker compose build novelai-gateway
docker compose up -d --no-deps novelai-gateway
```

生产发布建议使用独立版本标签：构建镜像、在隔离环境做冒烟检查、备份 Compose、将 image 指向新标签，再重建服务。仓库默认 stop_grace_period=900s，允许长请求优雅结束。保留旧镜像与 Compose 以便回滚；回滚只切换 image，不删除账户、图片或统计卷。

仓库升级不会自动更新远程服务器。仅上传源码、docker cp 或重启旧镜像都不能保证重建后仍是新版本；核对 `docker inspect` 的 image、启动时间和容器内源码。不要把开发机硬编码代理地址、运维凭据或本地调试文件打进镜像。

| 问题 | 检查方向 |
|---|---|
| 401 | 网关 Bearer 密码、管理密码、或上游 Token 是否正确 |
| 403 | 文本/语音开关、订阅权限、上游拒绝 |
| 400 | 模型动作、尺寸/步数、引用结构、Medium/-limit 限制、NAI 不支持该组合 |
| 409 | 当前没有具备对应 V5/Anlas 能力的账号，或绑定账号失效 |
| 429 | 队列满、V5 本地限额、上游限流/并发锁 |
| 502 / 超时 | 上游地址、容器代理、长连接超时、响应解析；先核对请求是否实际完成 |
| 只收到流式预览 | 必须等待 completed；error/截断不算完成，检查反代缓冲和读超时 |
| 参考图没有生效 | 检查最终 action、引用模式、上游模型能力及中转字段是否被过滤 |
| 改环境变量后账号未变 | 已有 config/accounts.json 优先；使用账号管理接口 |
| 镜像更新后仍是旧界面 | 核对 image/tag、挂载覆盖及浏览器缓存 |
| 金额不一致 | 区分估算 Anlas、usage 标记、NewAPI 价格与 LFN 站内阶梯 |

日志包含业务信息和可能的上游错误内容，反馈问题时仅提交脱敏请求、时间、模型、请求 ID、错误码与最小复现；不要提交完整 Token、原始账号文件或用户图片。

## 开发与验证

```bash
uv sync --extra test
uv run python -m pytest tests -q
node --test tests/test_admin_balances.cjs
git diff --check
```

Node 仅用于管理前端余额逻辑测试，不是 Gateway 运行时依赖。回归覆盖生成/编辑、角色与精密参考组合、Vibe 编码复用、原生参数与 multipart、计费、限额、账号能力路由、透明 PNG、超分及流式成功/错误/截断。常规测试使用替代上游，不产生真实生图费用；它们能验证网关的构造、透传和资源管理，不能替代当前 NAI 模型的付费端到端验证。

核心目录：`src/proxy/openai.py`（OAI 适配）、`app.py`（路由/原生代理）、`account_pool.py`（账号）、`queue.py`（队列）、`v5_quota.py`（统计限额）、`admin.py` 与 `templates/`（控制台）。模板内字体和图表脚本是运行资源，保留在仓库；本地缓存、测试图片、账号文件、凭据脚本与历史文档不纳入发布。
