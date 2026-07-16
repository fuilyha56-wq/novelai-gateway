# NovelAI Gateway API Reference

本文档以当前 `src/proxy/app.py`、`src/proxy/openai.py`、`src/proxy/config.py` 和 `config/models.toml` 为准，描述网关当前公开的 HTTP 接口、输入格式、响应格式与计费映射。

## 目录

1. [基础信息](#1-基础信息)
2. [认证](#2-认证)
3. [模型列表](#3-模型列表)
4. [通用图像生成](#4-通用图像生成)
5. [图像响应格式](#5-图像响应格式)
6. [图像变换与参考图](#6-图像变换与参考图)
7. [图像工具](#7-图像工具)
8. [计费](#8-计费)
9. [Chat 与 TTS](#9-chat-与-tts)
10. [管理、静态与透明代理](#10-管理静态与透明代理)
11. [排队、限制与错误](#11-排队限制与错误)
12. [严格参数参考](#12-严格参数参考)
13. [NewAPI 计费对接](#13-newapi-计费对接)
14. [部署与配置](#14-部署与配置)
15. [完整调用示例](#15-完整调用示例)
16. [已验证的当前网关行为](#16-已验证的当前网关行为)

## 1. 基础信息

- 默认监听：`http://127.0.0.1:41555`
- 所有 JSON 请求使用 `Content-Type: application/json`
- 图像字段均为 PNG/JPEG 等图片的 Base64 字符串；允许带 `data:image/...;base64,` 前缀的路径会自动剥离前缀。
- 网关响应错误统一为：

```json
{"detail":"错误说明"}
```

- 除透明代理外，图像端点均自动附加 CORS 允许头。

### 1.1 上游路由

| 网关能力 | NovelAI 上游 |
|---|---|
| 图像生成、Vibe 编码、标签建议、Director Tools | `https://image.novelai.net` |
| 放大、注释图、TTS、用户类 API | `https://api.novelai.net` |
| 文本生成 | `https://text.novelai.net` |

### 1.2 当前能力开关

当前默认配置启用图像模型，禁用 Chat 和 TTS：

```toml
image_enabled = true
chat_enabled = false
tts_enabled = false
```

因此当前 `/v1/models` 只暴露图像模型；`/v1/chat/completions` 与 `/v1/audio/speech` 会返回 `403`。启用前请确认账户确实拥有可用的 NovelAI 文本/TTS 上游模型。

## 2. 认证

在 `.env` 中配置共享凭据。`SHARED_API_KEY` 优先于 `SHARED_TOKEN`。

```ini
SHARED_API_KEY=nai-xxxxxxxx
# 或
SHARED_TOKEN={"auth_token":"..."}

HOST=0.0.0.0
PORT=41555
MAX_CONCURRENT=1
COOLDOWN_MIN=0.5
COOLDOWN_MAX=1.0
```

配置共享凭据后，下游请求中的 Authorization 不用于选择凭据；网关统一使用配置的 NovelAI 凭据访问上游。

## 3. 模型列表

### `GET /v1/models`

返回当前 `config/models.toml` 中已启用的模型。

```json
{
  "object":"list",
  "data":[
    {"id":"nai-v4.5-full","object":"model","owned_by":"novelai"}
  ]
}
```

默认公开的图像模型：

| 模型 ID | 上游模型 |
|---|---|
| `nai-v4.5-full` | `nai-diffusion-4-5-full` |
| `nai-v4.5-curated` | `nai-diffusion-4-5-curated` |
| `nai-v4.5-inpaint` | `nai-diffusion-4-5-full-inpainting` |
| `nai-v4.5-full-limit` | `nai-diffusion-4-5-full` |
| `nai-v4.5-curated-limit` | `nai-diffusion-4-5-curated` |
| `nai-v4.5-inpaint-limit` | `nai-diffusion-4-5-full-inpainting` |
| `nai-v4-curated` | `nai-diffusion-4-curated-preview` |
| `nai-v3` | `nai-diffusion-3` |
| `nai-v3-furry` | `nai-diffusion-furry-3` |
| `nai-v3-inpaint` | `nai-diffusion-3-inpainting` |
| `nai-v3-furry-inpaint` | `nai-diffusion-furry-3-inpainting` |

`-limit` 模型仅可用于 `/v1/images/generations` 的 Opus 免费额度文生图：必须 `n_samples=1`、`steps<=28`、面积不超过 `1024x1024`、`action=generate`、无图像/参考图、且不使用 `service_tier=priority`。其他付费图像端点会拒绝 `-limit` 模型。

## 4. 通用图像生成

### `POST /v1/images/generations`

OpenAI 兼容文生图端点。支持 NAI 扩展字段。

最小请求：

```json
{
  "model":"nai-v4.5-full",
  "prompt":"1girl, orange hair",
  "size":"512x512",
  "steps":28,
  "response_format":"b64_json"
}
```

主要字段：

| 字段 | 类型 | 默认值 | 说明 |
|---|---|---|---|
| `model` | string | `nai-v4.5-full` | 网关模型 ID 或上游模型名 |
| `prompt` | string | `""` | 正向提示词 |
| `negative_prompt` | string | 内置 UC | 负向提示词 |
| `size` | string | `1024x1024` | 如 `832x1216`；可由 `width`/`height` 覆盖 |
| `width` / `height` | integer | - | 图像尺寸，64 到 1600；最终由网关限制 |
| `n` / `n_samples` | integer | `1` | 样本数，1 到 6 |
| `steps` | integer | `28` | 1 到 50 |
| `scale` / `guidance_scale` | number | `5.0` | CFG scale |
| `sampler` | string | `k_euler_ancestral` | 支持 `k_euler`、`k_euler_ancestral`、`k_dpmpp_2s_ancestral`、`k_dpmpp_2m`、`k_dpmpp_2m_sde`、`k_dpmpp_sde`、`ddim_v3` |
| `noise_schedule` | string | `karras` | `native`、`karras`、`exponential`、`polyexponential` |
| `seed` | integer | 自动生成 | 随机种子 |
| `quality_tags` | string | 内置质量标签 | 空字符串可禁用自动质量标签 |
| `response_format` | string | `b64_json` | 见 [响应格式](#5-图像响应格式) |
| `reference_image` | string | - | 单张 Vibe 参考图 |
| `reference_strength` | number | `0.6` | 单张 Vibe 强度 |
| `reference_information_extracted` | number | `1.0` | Vibe 信息提取量 |
| `controlnet_condition` | string | - | ControlNet 条件图 |
| `controlnet_model` | string | `hed` | ControlNet 模型 |
| `controlnet_strength` | number | `1.0` | ControlNet 强度 |
| `service_tier` | string | - | 原样传给上游；`-limit` 模型禁止 `priority` |

NewAPI 等会过滤扩展 body 字段时，可使用下列 Header 覆盖请求值：`X-Sampler`、`X-Noise-Schedule`、`X-Negative-Prompt`、`X-Service-Tier`、`X-Steps`、`X-Seed`、`X-N-Samples`、`X-Scale`。

V4/V4.5 模型所需的 `v4_prompt`、`v4_negative_prompt` 等结构由网关自动补全；也可自行传入高级结构。

## 5. 图像响应格式

所有生成、图生图、重绘、Vibe、角色参考和 Precise Reference 使用以下 `response_format`：

| 值 | 响应 |
|---|---|
| `b64_json`、`auto` 或省略 | OpenAI JSON，`data[0].b64_json` 为 PNG Base64 |
| `url` | OpenAI JSON，`data[0].url` 指向网关 `/images/{filename}` |
| `raw` | 原始上游 ZIP/JSON 二进制响应 |
| `nai_json` | 透传 NovelAI JSON 响应 |

`b64_json`/`url` 响应包含网关计费映射：

```json
{
  "created": 1710000000,
  "data":[{"b64_json":"...","revised_prompt":"..."}],
  "usage":{"prompt_tokens":294,"completion_tokens":0,"total_tokens":294}
}
```

其中 `prompt_tokens = round(Anlas / 17 * 1000)`，最小为 1。JSON 端点的 `usage` 是供 NewAPI 等下游按 token 规则计价的映射，不是 NovelAI 原始 token 计数。

## 6. 图像变换与参考图

### `POST /v1/images/img2img`

图生图，必填 `image`。主要字段：`prompt`、`model`、`image`、`width`、`height`、`strength`（默认 `0.7`）、`noise`（默认 `0`）、`steps`、`scale`、`sampler`、`response_format`。

```json
{
  "model":"nai-v4.5-full",
  "prompt":"watercolor illustration",
  "image":"<base64>",
  "width":512,
  "height":512,
  "strength":0.7
}
```

### `POST /v1/images/inpainting`

NAI SDK 风格局部重绘。必填 `image` 与 `mask`；动作固定为 `infill`。

```json
{
  "model":"nai-v4.5-inpaint",
  "prompt":"replace with a blue sky",
  "image":"<base64>",
  "mask":"<base64>",
  "width":512,
  "height":512,
  "strength":0.7
}
```

支持 `noise`、`add_original_image`、`cfg_rescale` 及常规采样参数。

### `POST /v1/images/edits`

OpenAI 兼容局部重绘。支持 JSON 和 `multipart/form-data`：

- JSON：`image` 为 Base64；可显式传 `mask`。
- multipart：`image` 为文件；`mask` 为可选文件；`prompt`、`model`、`size`、`response_format` 为表单字段。
- 未传 mask 时，网关从 image alpha 生成蒙版：透明区域为重绘区域。

JSON 示例：

```json
{
  "model":"nai-v4.5-inpaint",
  "prompt":"add a flower",
  "image":"<base64-png-with-alpha>",
  "size":"512x512"
}
```

multipart 示例：

```bash
curl -X POST http://127.0.0.1:41555/v1/images/edits \
  -F "model=nai-v4.5-inpaint" \
  -F "prompt=add a blue flower" \
  -F "size=512x512" \
  -F "response_format=b64_json" \
  -F "image=@source.png" \
  -F "mask=@mask.png"
```

multipart 请求中 `mask` 也可省略；此时 source 图片必须有 alpha 通道，透明区域会被转换为白色蒙版区域。

### `POST /v1/images/vibe-transfer`

Vibe Transfer。必填 `reference_image`（单个字符串）或 `reference_images`（数组）。V4/V4.5 会自动调用 `/ai/encode-vibe` 后再生成。

```json
{
  "model":"nai-v4.5-full",
  "prompt":"portrait",
  "reference_image":"<base64>",
  "reference_strength":0.6,
  "reference_information_extracted":1.0,
  "width":512,
  "height":512
}
```

多图时可传 `reference_strength_multiple` 与 `reference_information_extracted_multiple`。

### `POST /v1/images/character-reference`

多角色 V4/V4.5 参考图。必填 `characters`，每项必须有 `reference_image`。

```json
{
  "model":"nai-v4.5-full",
  "prompt":"two characters",
  "width":832,
  "height":1216,
  "characters":[
    {
      "reference_image":"<base64>",
      "prompt":"1girl, orange hair",
      "center":{"x":0.3,"y":0.5},
      "reference_strength":0.6,
      "reference_information_extracted":1.0
    }
  ]
}
```

`center` 可为 `{ "x": number, "y": number }` 或 centers 列表。网关自动构建角色 `v4_prompt`；若请求包含 `v4_prompt`/`v4_negative_prompt`，则使用传入的高级结构。

### `POST /v1/images/precise-reference`

Director Reference / Precise Reference。只支持 V4/V4.5。必填 `references`；网关会把每张参考图规范化为 NovelAI 官网所需的固定黑底 PNG 画布后发送。

```json
{
  "model":"nai-v4.5-full",
  "prompt":"anime portrait",
  "width":512,
  "height":512,
  "references":[
    {
      "reference_image":"<base64>",
      "reference_type":"character&style",
      "strength":1.0,
      "fidelity":1.0
    }
  ]
}
```

`reference_type` 只能为 `character`、`style`、`character&style`。`fidelity` 会映射为上游 secondary strength：`1 - fidelity`。

## 7. 图像工具

### `POST /v1/images/upscale`

二倍或四倍放大。必填 `image`；网关以 JSON 发往上游、解压上游 ZIP，最终返回 `image/png`。

```json
{
  "image":"<base64>",
  "width":512,
  "height":512,
  "scale":2
}
```

成功响应：`Content-Type: image/png`。

### `POST /v1/images/annotate`

生成控制图。必填 `image`；`model` 默认 `hed`，也兼容旧字段 `req_type`。

支持：`canny`、`hed`、`midas`、`mlsd`、`openpose`、`uniformer`、`fake_scribble`。

```json
{
  "image":"<base64>",
  "model":"canny"
}
```

网关向上游发送 `{ "model": "canny", "parameters": { "image": "..." } }`，解压 ZIP 后返回 `image/png`，并设置 `X-Annotate-Model` 响应头。

### `POST /v1/images/suggest-tags`

提示词标签建议。必填 `prompt`；可选 `model`，默认 `nai-diffusion-3`。网关对外接收 POST，但上游为 GET query。

```json
{"model":"nai-diffusion-3","prompt":"1girl"}
```

成功响应透传上游 JSON：

```json
{"tags":[{"tag":"1girl","count":10000,"confidence":0.95}]}
```

### Director Tools

所有导演工具输入均为 Base64 `image`，并建议传入与实际图片一致的 `width`/`height`。网关向上游发送 multipart 请求、解压 ZIP，成功响应均为 `image/png`。

| 路径 | req_type | 额外字段 | 固定 Anlas | 响应头 |
|---|---|---|---:|---|
| `/v1/images/director/declutter` | `declutter` | - | 2 | `X-Anlas-Cost: 2` |
| `/v1/images/director/bg-remover` | `bg-removal` | - | 65 | `X-Anlas-Cost: 65` |
| `/v1/images/director/lineart` | `lineart` | - | 2 | `X-Anlas-Cost: 2` |
| `/v1/images/director/sketch` | `sketch` | - | 2 | `X-Anlas-Cost: 2` |
| `/v1/images/director/colorize` | `colorize` | `prompt`、`defry` | 2 | `X-Anlas-Cost: 2` |
| `/v1/images/director/emotion` | `emotion` | `prompt`、`defry` | 2 | `X-Anlas-Cost: 2` |

`defry` 会被限制为 0 到 5。所有 Director Tools 还会返回 `X-Prompt-Tokens`，其值按 `Anlas / 17 * 1000` 映射。

## 8. 计费

### 8.1 生成类端点

适用于 `generations`、`img2img`、`inpainting`、`edits`、`vibe-transfer`、`character-reference`、`precise-reference`：

$$
\text{base} = \left\lceil 2.9518\times10^{-21}r + 5.7533\times10^{-7}r\cdot\text{steps}\right\rceil
$$

其中 $r=\max(width\times height,65536)$。

- 图生图/重绘在 `strength < 1` 时：`max(ceil(base * strength), 2)`。
- `uncond_scale != 1` 时，再乘以该值并向上取整。
- 最终乘以 `n_samples`。
- Opus 免费额度条件满足时，第一张可减免。
- Vibe/Character Reference：每张加 2 Anlas；第 5 张及以后每张再额外加 2。
- Precise Reference：每张加 5 Anlas。

示例：`512x512`、28 steps、单张文生图为 5 Anlas；同规格 `strength=0.7` 的图生图/重绘为 4 Anlas；单张 Vibe/Character Reference 为 7 Anlas；单张 Precise Reference 为 10 Anlas。

### 8.2 二进制工具端点

- Director Tools：固定费用见上表，通过 `X-Anlas-Cost` 返回。
- Upscale：上游消耗，但当前包装响应不生成 `usage` 或 `X-Anlas-Cost`；下游如需计价，应按路径自行配置。
- Annotate 与 suggest-tags：不生成图像 Anlas 映射。

## 9. Chat 与 TTS

### `POST /v1/chat/completions`

实现为 OpenAI Chat Completions 兼容接口，支持 `messages`、`model`、`stream`、`temperature`、`max_tokens`、`top_p`。启用时可返回标准 JSON 或 SSE。

**当前默认配置禁用**。请求会返回：

```json
{
  "error":{
    "message":"Chat is disabled in configuration",
    "type":"config_error",
    "code":"chat_disabled"
  }
}
```

状态码为 `403`。

### `POST /v1/audio/speech`

实现为 OpenAI TTS 兼容接口，启用时接受 `model`、`input`、`seed`、`voice`，返回 `audio/mpeg` 或 `audio/ogg`。

**当前默认配置禁用**，返回 `403` 和 `tts_disabled`。启用前必须确认账户具备可用 TTS 权限。

## 10. 管理、静态与透明代理

### `POST /admin/refresh-upstream-models`

从 NovelAI 网页 bundle 抓取上游模型建议并写入建议配置。若设置 `GATEWAY_PASSWORD`，必须提供：

```http
Authorization: Bearer <GATEWAY_PASSWORD>
```

否则返回 `401`。

### `GET /images/{filename}`

获取 `response_format=url` 生成并落盘的 PNG。路径穿越会返回 `403`，不存在返回 `404`。

### `ANY /_api/{path}`

NovelAI API 透明代理。支持 `GET`、`POST`、`PUT`、`DELETE`、`PATCH`、`OPTIONS`、`HEAD`。路径通过当前上游路由规则选择目标域名；重负载路径会记录生成统计。

示例：

```bash
curl http://127.0.0.1:41555/_api/user/subscription
```

透明代理不会把请求转换为 OpenAI 格式，也不会注入网关 `usage`。它适合已经兼容 NovelAI 原始协议的客户端。示例：

```bash
# 原始 NovelAI 路径 /ai/generate-image 映射为 /_api/ai/generate-image
curl -X POST http://127.0.0.1:41555/_api/ai/generate-image \
  -H "Content-Type: application/json" \
  -d '{"input":"1girl","model":"nai-diffusion-4-5-full","action":"generate","parameters":{"width":512,"height":512,"n_samples":1,"steps":28,"scale":5,"sampler":"k_euler_ancestral","noise_schedule":"karras"}}'
```

### `ANY /{path}`

网站透明代理兜底，转发到 `https://novelai.net/{path}` 并注入网关网页劫持逻辑。若设置 `GATEWAY_PASSWORD`，GET 请求需要 `gw_pass` Cookie。

浏览器访问受保护网页时可先写入 Cookie：

```javascript
document.cookie = "gw_pass=<GATEWAY_PASSWORD>; Path=/";
```

## 11. 排队、限制与错误

### 排队

以下包装端点会经由全局队列与冷却：生成、图生图、两种重绘、Vibe、角色/Precise Reference、upscale、TTS（若启用）。`max_concurrent`、`queue_timeout`、`cooldown_min`、`cooldown_max` 由环境变量配置。

Annotate、suggest-tags、Director Tools、models、Chat 及透明非重负载代理不进入此队列。

### 常见状态码

| 状态码 | 含义 |
|---:|---|
| 400 | 参数缺失、非法采样参数、无效模型、`-limit` 模型使用受限能力，或上游拒绝请求 |
| 401 | 管理端点密码错误 |
| 403 | Chat/TTS 在配置中禁用，或上游权限不足 |
| 404 | 静态图片不存在，或上游不存在该路径 |
| 502 | 上游连接、上游二进制解包或代理失败 |
| 503 | 队列超时/服务不可用 |

## 12. 严格参数参考

### 12.1 数值参数与限制

以下规则由网关在图像生成请求发送前执行；违反后直接返回 `400`：

| 参数 | 类型 | 合法范围 | 默认值 | 适用接口 |
|---|---|---|---|---|
| `width` | integer | 64 至 1600，且为 64 的倍数 | 1024 | 所有生成/编辑接口 |
| `height` | integer | 64 至 1600，且为 64 的倍数 | 1024 | 所有生成/编辑接口 |
| `steps` | integer | 1 至 50 | 28 | 所有生成/编辑接口 |
| `n_samples` | integer | 1 至 6 | 1 | generations |
| `n` | integer | 1 至 6 | 1 | generations，映射到 `n_samples` |
| `scale` / `guidance_scale` | number | 0 至 10 | 5.0 | 生成/编辑接口 |
| `strength` | number | 0 至 1 | 0.7 | img2img/inpainting/edits |
| `seed` | integer | 0 至 4294967295 | 自动生成 | 生成/编辑接口 |
| `defry` | integer | 0 至 5，超出会 clamp | 0 | colorize/emotion |

`sampler` 和 `noise_schedule` 不在本地白名单时，网关仅记录 warning 并继续透传给 NovelAI；上游可继续拒绝。建议值：

```text
sampler:
k_euler
k_euler_ancestral
k_dpmpp_2s_ancestral
k_dpmpp_2m
k_dpmpp_2m_sde
k_dpmpp_sde
ddim_v3

noise_schedule:
native
karras
exponential
polyexponential
```

`response_format` 则必须为 `b64_json`、`url`、`raw`、`nai_json` 或 `auto`；否则网关返回 `400`。

### 12.2 `-limit` 模型的免费额度保护

`nai-v4.5-full-limit`、`nai-v4.5-curated-limit`、`nai-v4.5-inpaint-limit` 是网关提供的保护性模型名，不是 NovelAI 的独立上游模型。它们只允许走符合 Opus 免费文生图边界的 `/v1/images/generations` 请求：

| 条件 | 必须满足 |
|---|---|
| 动作 | `action=generate` |
| 样本数 | `n_samples=1` |
| 步数 | `steps<=28` |
| 像素面积 | `width*height<=1048576` |
| 参考/输入图 | 不允许 `image`、`reference_image`、`reference_images` |
| 优先级 | 不允许 `service_tier=priority` |

图生图、重绘、Vibe、Character Reference、Precise Reference、upscale、Director Tools 等必然消耗 Anlas 的接口会拒绝 `-limit` 模型。请改用去掉 `-limit` 后缀的原版模型。

### 12.3 Header 覆盖规则

`/v1/images/generations` 支持下列 Header 覆盖 body 同名字段，适合被 NewAPI 等中转过滤扩展字段时使用：

| Header | 对应 body 字段 |
|---|---|
| `X-Sampler` | `sampler` |
| `X-Noise-Schedule` | `noise_schedule` |
| `X-Negative-Prompt` | `negative_prompt` |
| `X-Service-Tier` | `service_tier` |
| `X-Steps` | `steps` |
| `X-Seed` | `seed` |
| `X-N-Samples` | `n_samples` |
| `X-Scale` | `scale` |

Header 值优先级高于 body。

## 13. NewAPI 计费对接

### 13.1 网关侧 Anlas 映射

生成类 JSON 响应会包含 `usage.prompt_tokens`。当前映射为：

$$
	ext{prompt\_tokens}=\max(1,\operatorname{round}(\frac{\text{Anlas}}{17}\times1000))
$$

常用示例：

| 请求 | 网关 Anlas | `prompt_tokens` |
|---|---:|---:|
| 512x512、28 steps、单张文生图 | 5 | 294 |
| 1024x1024、28 steps、单张文生图 | 17 | 1000 |
| 512x512、28 steps、`strength=0.7` 图生图 | 4 | 235 |
| 512x512、28 steps、单张 Vibe/Character Reference | 7 | 412 |
| 512x512、28 steps、单张 Precise Reference | 10 | 588 |

Director Tools 不返回 `usage`，改用响应头：

```http
X-Anlas-Cost: 2
X-Prompt-Tokens: 118
```

`upscale`、`annotate`、`suggest-tags` 不附加网关 `usage`。如需要在 NewAPI 内为 upscale 计价，请按路径设置固定规则。

### 13.2 tiered_expr 示例

若 NewAPI 以 17 Anlas = 1000 prompt tokens 为计费基准，可使用类似表达式：

```text
tier("base", p * 4800)
```

实际金额取决于你的 NewAPI token 价格、倍率与分组规则；此表达式仅说明网关返回 `usage` 的用途。

### 13.3 条件计价建议

需要按参数细分时，优先根据响应 `usage` 计费。若使用请求条件计费，应至少覆盖：

- `n` / `n_samples`
- `steps`
- `size` 或 `width*height`
- `strength`
- `reference_image` / `reference_images`
- `service_tier`
- Director Tools、upscale 等路径

不要将 Chat/TTS 与图像 Anlas 规则混用；当前默认配置下这两个能力为禁用状态。

## 14. 部署与配置

### 14.1 本地启动

```powershell
cd E:\novelai-gateway
uv sync
uv run python main.py
```

### 14.2 Docker Compose

```bash
docker compose build
docker compose up -d
docker compose logs -f novelai-gateway
```

默认 Compose 服务会：

- 对外暴露 `41555:41555`。
- 挂载 `./config` 到 `/app/config`。
- 挂载 `./images` 到 `/app/images`，供 `response_format=url` 使用。
- 挂载 `./logs` 到 `/app/logs`。
- 通过环境变量配置 HTTP/SOCKS 代理（如使用 v2rayA）。

### 14.3 常用环境变量

| 环境变量 | 默认值 | 说明 |
|---|---|---|
| `HOST` | `0.0.0.0` | 监听地址 |
| `PORT` | `31555` | 监听端口 |
| `SHARED_API_KEY` | 空 | 持久 NovelAI API Key，优先级最高 |
| `SHARED_TOKEN` | 空 | Session token JSON 或裸 token |
| `GATEWAY_PASSWORD` | 空 | 网页代理/管理接口密码保护 |
| `IMAGE_BASE_URL` | 空 | URL 图片响应的公开基础地址 |
| `MAX_CONCURRENT` | `1` | 重负载并发数 |
| `QUEUE_TIMEOUT` | `300` | 排队超时秒数 |
| `COOLDOWN_MIN` | `0.5` | 完成后的最短冷却秒数 |
| `COOLDOWN_MAX` | `1.0` | 完成后的最长冷却秒数 |
| `UPSTREAM_TIMEOUT` | `120` | NovelAI 上游超时秒数 |
| `NOVELAI_API_URL` | `https://api.novelai.net` | API 上游域名 |
| `NOVELAI_IMAGE_URL` | `https://image.novelai.net` | 图像上游域名 |
| `NOVELAI_TEXT_URL` | `https://text.novelai.net` | 文本上游域名 |

### 14.4 更新流程

```bash
git pull origin master
docker compose build --pull novelai-gateway
docker compose up -d --force-recreate novelai-gateway
docker logs novelai-gateway --tail 30
```

发布更新后至少检查：容器状态、启动日志、`GET /v1/models` 和一条低成本 `suggest-tags` 或 annotate 请求。

### 14.5 常见排错

| 现象 | 检查方向 |
|---|---|
| `502 上游请求失败` | 代理端口、`http_proxy`/`https_proxy`/`all_proxy`、NovelAI 网络连通性、上游超时 |
| `400 model must be a valid enum value` | 使用 `/v1/models` 返回的模型 ID；annotate 仅使用 7 个已列出的 model |
| `400` 且 `-limit` 提示 | 改用非 `-limit` 模型，或改为符合免费文生图条件的请求 |
| `500` Director Tool | 确保图片为正常 PNG/JPEG，且 `width`/`height` 与实际图匹配 |
| `403 chat_disabled` / `tts_disabled` | 当前配置已禁用；确认账户能力后再改 `config/models.toml` 并重启 |
| `404` 静态图片 | 检查 URL 文件名、`images` 挂载和 `IMAGE_BASE_URL` |
| 浏览器网页被锁定 | 设置 `gw_pass` Cookie 为 `GATEWAY_PASSWORD` |

## 15. 完整调用示例

以下示例均以本机网关 `http://127.0.0.1:41555` 为例。将 `<BASE64_IMAGE>`、`<BASE64_MASK>` 替换为真实 Base64 数据。

### 15.1 生成一张 V4.5 图像

```bash
curl -X POST http://127.0.0.1:41555/v1/images/generations \
  -H "Content-Type: application/json" \
  -d '{
    "model":"nai-v4.5-full",
    "prompt":"1girl, orange hair, blue sky",
    "negative_prompt":"lowres, blurry",
    "size":"512x512",
    "steps":28,
    "scale":5.0,
    "sampler":"k_euler_ancestral",
    "response_format":"b64_json"
  }'
```

### 15.2 通过 Header 传递采样参数

```bash
curl -X POST http://127.0.0.1:41555/v1/images/generations \
  -H "Content-Type: application/json" \
  -H "X-Sampler: k_dpmpp_2m" \
  -H "X-Noise-Schedule: karras" \
  -H "X-Steps: 28" \
  -H "X-Scale: 5" \
  -d '{"model":"nai-v4.5-full","prompt":"city at night","size":"832x1216"}'
```

### 15.3 URL 图片响应

```bash
curl -X POST http://127.0.0.1:41555/v1/images/generations \
  -H "Content-Type: application/json" \
  -d '{"model":"nai-v4.5-full","prompt":"sunset","size":"512x512","response_format":"url"}'
```

### 15.4 图生图

```bash
curl -X POST http://127.0.0.1:41555/v1/images/img2img \
  -H "Content-Type: application/json" \
  -d '{
    "model":"nai-v4.5-full",
    "prompt":"watercolor landscape",
    "image":"<BASE64_IMAGE>",
    "width":512,
    "height":512,
    "strength":0.7,
    "noise":0.0
  }'
```

### 15.5 NAI SDK 风格重绘

```bash
curl -X POST http://127.0.0.1:41555/v1/images/inpainting \
  -H "Content-Type: application/json" \
  -d '{
    "model":"nai-v4.5-inpaint",
    "prompt":"a blue flower",
    "image":"<BASE64_IMAGE>",
    "mask":"<BASE64_MASK>",
    "width":512,
    "height":512,
    "strength":0.7
  }'
```

### 15.6 OpenAI edits（自动从 alpha 生成蒙版）

```bash
curl -X POST http://127.0.0.1:41555/v1/images/edits \
  -H "Content-Type: application/json" \
  -d '{
    "model":"nai-v4.5-inpaint",
    "prompt":"replace transparent area with clouds",
    "image":"<BASE64_RGBA_PNG>",
    "size":"512x512"
  }'
```

### 15.7 Vibe Transfer

```bash
curl -X POST http://127.0.0.1:41555/v1/images/vibe-transfer \
  -H "Content-Type: application/json" \
  -d '{
    "model":"nai-v4.5-full",
    "prompt":"portrait of a girl",
    "reference_image":"<BASE64_IMAGE>",
    "reference_strength":0.6,
    "reference_information_extracted":1.0,
    "width":512,
    "height":512
  }'
```

### 15.8 Character Reference

```bash
curl -X POST http://127.0.0.1:41555/v1/images/character-reference \
  -H "Content-Type: application/json" \
  -d '{
    "model":"nai-v4.5-full",
    "prompt":"two friends at a cafe",
    "width":832,
    "height":1216,
    "characters":[
      {"reference_image":"<BASE64_IMAGE_A>","prompt":"1girl, orange hair","center":{"x":0.3,"y":0.5}},
      {"reference_image":"<BASE64_IMAGE_B>","prompt":"1girl, black hair","center":{"x":0.7,"y":0.5}}
    ]
  }'
```

### 15.9 Precise Reference

```bash
curl -X POST http://127.0.0.1:41555/v1/images/precise-reference \
  -H "Content-Type: application/json" \
  -d '{
    "model":"nai-v4.5-full",
    "prompt":"anime portrait",
    "width":512,
    "height":512,
    "references":[
      {"reference_image":"<BASE64_IMAGE>","reference_type":"character&style","strength":1.0,"fidelity":1.0}
    ]
  }'
```

### 15.10 Upscale、annotate 和标签建议

```bash
# 二倍放大，响应为 image/png
curl -X POST http://127.0.0.1:41555/v1/images/upscale \
  -H "Content-Type: application/json" \
  -d '{"image":"<BASE64_IMAGE>","width":512,"height":512,"scale":2}' \
  --output upscaled.png

# Canny 控制图，响应为 image/png
curl -X POST http://127.0.0.1:41555/v1/images/annotate \
  -H "Content-Type: application/json" \
  -d '{"image":"<BASE64_IMAGE>","model":"canny"}' \
  --output canny.png

# 标签建议
curl -X POST http://127.0.0.1:41555/v1/images/suggest-tags \
  -H "Content-Type: application/json" \
  -d '{"model":"nai-diffusion-3","prompt":"1girl"}'
```

### 15.11 Director Tool

```bash
curl -X POST http://127.0.0.1:41555/v1/images/director/colorize \
  -H "Content-Type: application/json" \
  -d '{
    "image":"<BASE64_IMAGE>",
    "width":1024,
    "height":1024,
    "prompt":"bright orange and blue",
    "defry":1
  }' \
  --output colorized.png
```

检查响应头中的 `X-Anlas-Cost` 与 `X-Prompt-Tokens`。

### 15.12 Python 示例

```python
import base64
import httpx

response = httpx.post(
    "http://127.0.0.1:41555/v1/images/generations",
    json={
        "model": "nai-v4.5-full",
        "prompt": "anime landscape",
        "size": "512x512",
        "steps": 28,
    },
    timeout=180,
)
response.raise_for_status()
payload = response.json()
image_bytes = base64.b64decode(payload["data"][0]["b64_json"])
open("result.png", "wb").write(image_bytes)
print(payload["usage"])
```

## 16. 已验证的当前网关行为

运行中的网关已通过以下实际调用验证：

- 生成、图生图、inpainting、edits、Vibe Transfer、Character Reference、Precise Reference、upscale。
- 所有 7 种 annotate 模型。
- suggest-tags。
- 全部 6 种 Director Tools，且 `2/65` Anlas 响应头正确。
- Chat 与 TTS 在当前配置下按预期返回禁用 `403`。

部署或修改 `config/models.toml` 后，请重新执行端到端检查，以实际账户权限为准。
