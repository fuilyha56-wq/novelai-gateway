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

在 `.env` 中配置共享凭据。若设置 `SHARED_API_KEYS`，它优先于 `SHARED_API_KEY` 和 `SHARED_TOKEN`；否则保持单 Key 兼容顺序：`SHARED_API_KEY` 优先于 `SHARED_TOKEN`。

```ini
# 多 Key 轮询：每个下游 HTTP 请求按顺序选择下一把 Key。
# 支持 JSON 数组，或逗号、分号、换行分隔；JSON 数组在 .env 中须使用单引号包裹。
SHARED_API_KEYS='["nai-key-a","nai-key-b","nai-key-c"]'
# SHARED_API_KEYS=nai-key-a,nai-key-b,nai-key-c

# 单 Key 回退（仅当 SHARED_API_KEYS 留空时使用）
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

### 多 Key 轮询规则

- `SHARED_API_KEYS` 可以写为 JSON 数组，或逗号、分号、换行分隔的非空 Key 列表；空项会忽略。
- 每个下游 HTTP 请求以轮询方式选择一把 Key，例如三把 Key 会按 `A -> B -> C -> A` 分配。
- 一个请求中的所有内部上游调用固定使用同一把 Key。例如 Vibe/Character Reference 的编码与最终生成、流式 Chat、TTS 都不会在请求中途切换 Key。
- 透明代理 `/_api/*` 和 OpenAI 包装端点使用同一轮询器；所有指向 NovelAI 上游的请求都会参与轮询。
- 轮询不根据余额、订阅级别、403/429 或上游错误自动重试、更换 Key；这种自动切换会隐藏真实扣费和权限问题。失败响应会原样返回，下一次下游请求才轮到下一把 Key。
- 多 Key 模式适合将多个**已获授权、由你控制**的 NovelAI API Key 均衡分配。不要配置来源不明、权限不同或不应共享的凭据。

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

`-limit` 模型是禁止超出 Opus 免费额度的保护性别名，不是“仅文生图”模型。它支持单张、28 steps 以内、面积不超过 `1024x1024` 的文生图、图生图、局部重绘和 ControlNet 条件图生成；完整端点与模型对应关系见下表。会产生额外 Anlas 的参考图、放大或 Director 操作会被拒绝。

| Gateway `-limit` 模型 | 支持的功能 | 可调用端点 | 不支持 |
|---|---|---|---|
| `nai-v4.5-full-limit` | 文生图、图生图、OpenAI/NAI 重绘、ControlNet 条件图生成 | `/v1/images/generations`、`/v1/images/img2img`、`/v1/images/inpainting`、`/v1/images/edits` | Vibe、Character/Precise Reference、upscale、Director Tools |
| `nai-v4.5-curated-limit` | 文生图、图生图、OpenAI/NAI 重绘、ControlNet 条件图生成 | `/v1/images/generations`、`/v1/images/img2img`、`/v1/images/inpainting`、`/v1/images/edits` | Vibe、Character/Precise Reference、upscale、Director Tools |
| `nai-v4.5-inpaint-limit` | 文生图、图生图、OpenAI/NAI 重绘、ControlNet 条件图生成 | `/v1/images/generations`、`/v1/images/img2img`、`/v1/images/inpainting`、`/v1/images/edits` | Vibe、Character/Precise Reference、upscale、Director Tools |

所有 `-limit` 请求必须同时满足：`n`/`n_samples=1`、`steps<=28`、`width*height<=1048576`、`service_tier` 不为 `priority`、不传 `reference_image`/`reference_images`/`references`。文生图还不能传 `image`；图生图和重绘可以传其必需的 `image`，重绘还可以传 `mask`；ControlNet 条件图可通过 `controlnet_condition`、`controlnet_model`、`controlnet_strength` 使用。超出任一限制，网关返回 `400`，请改用不带 `-limit` 后缀的模型。

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

#### 控制图生成与使用

控制图由 `POST /v1/images/annotate` 生成，不是直接输出最终作品的文生图功能。先将原图转为边缘、姿态或深度条件图；再把返回 PNG 的 Base64 填进本端点的 `controlnet_condition`，以约束最终生成的构图或姿势。

```text
原始图片 --POST /v1/images/annotate--> 控制图 PNG
控制图 PNG --controlnet_condition--> POST /v1/images/generations --> 最终图片
```

`controlnet_model` 必须与生成控制图时的 `model` 一致，可用值为 `canny`、`hed`、`midas`、`mlsd`、`openpose`、`uniformer`、`fake_scribble`。`controlnet_strength` 由 NovelAI 解释，通常从 `1.0` 开始：调低会更自由地遵从提示词，调高会更严格地保持控制图结构。

NewAPI 等会过滤扩展 body 字段时，可使用下列 Header 覆盖请求值：`X-Sampler`、`X-Noise-Schedule`、`X-Negative-Prompt`、`X-Service-Tier`、`X-Steps`、`X-Seed`、`X-N-Samples`、`X-Scale`。

V4/V4.5 模型所需的 `v4_prompt`、`v4_negative_prompt` 等结构由网关自动补全；也可自行传入高级结构。

### 4.1 经 NewAPI 的统一图像操作协议

当 NewAPI 只允许 `/v1/images/generations` 时，所有**产出图片**的能力都可走这一个端点。请求路径固定为：

```http
POST /v1/images/generations
Content-Type: application/json

{
  "novelai_operation": "<operation>",
  "...": "该操作所需的原有 JSON 字段"
}
```

普通文生图省略 `novelai_operation`，或设为 `generate`。其余图像能力使用下表的值，并在 JSON body 按对应专用端点的字段传参。`novelai_operation` 是推荐方案，适合 NewAPI 开启“透传请求体”后的每次动态调用。

| 功能 | `novelai_operation` | 关键请求字段 | 经 NewAPI 的响应 |
|---|---|---|---|
| 文生图 | 省略或 `generate` | `model`、`prompt`、尺寸和采样参数 | OpenAI 图片 JSON |
| 精密参考 | `precise-reference` | `references[]` | OpenAI 图片 JSON |
| 图生图 | `img2img` | `image`、`prompt`、`strength` | OpenAI 图片 JSON |
| NAI 局部重绘 | `inpainting` | `image`、`mask`、`prompt` | OpenAI 图片 JSON |
| OpenAI 风格重绘 | `edits` | `image`、`mask`（可选）、`prompt` | OpenAI 图片 JSON |
| Vibe Transfer | `vibe-transfer` | `reference_image` 或 `reference_images`、`prompt` | OpenAI 图片 JSON |
| Character Reference | `character-reference` | `characters[]`、`prompt` | OpenAI 图片 JSON |
| 放大 | `upscale` | `image`、`width`、`height`、`scale` | OpenAI 图片 JSON，PNG 在 `b64_json` |
| 控制图 | `annotate` | `image`、`model` | OpenAI 图片 JSON，PNG 在 `b64_json` |
| 去杂物 | `director-declutter` | `image`、`width`、`height` | OpenAI 图片 JSON，PNG 在 `b64_json` |
| 去背景 | `director-bg-remover` | `image`、`width`、`height` | OpenAI 图片 JSON，PNG 在 `b64_json` |
| 提取线稿 | `director-lineart` | `image`、`width`、`height` | OpenAI 图片 JSON，PNG 在 `b64_json` |
| 草图化 | `director-sketch` | `image`、`width`、`height` | OpenAI 图片 JSON，PNG 在 `b64_json` |
| 线稿上色 | `director-colorize` | `image`、`width`、`height`、`prompt`、`defry` | OpenAI 图片 JSON，PNG 在 `b64_json` |
| 情感迁移 | `director-emotion` | `image`、`width`、`height`、`prompt`、`defry` | OpenAI 图片 JSON，PNG 在 `b64_json` |

带 `novelai_operation` 的统一图像入口始终返回 `application/json`，并强制使用 `data[0].b64_json`。放大、控制图与 Director 工具的 PNG 不会重新编码：先前由 NovelAI 写入 PNG 的元数据会保留在 Base64 解码后的原始字节中。`usage` 仍按照实际操作的 Anlas 映射返回，Director 工具沿用固定的 5 或 65 Anlas。

也兼容 `X-NovelAI-Operation: <operation>` 请求头，供不能透传请求体的渠道使用。若请求头和 `novelai_operation` 同时存在且值不同，网关返回 `400`，避免把一次调用悄悄路由到错误功能。

`suggest-tags` 的结果是标签 JSON、Chat 的结果是文本、TTS 的结果是音频；它们不是图像，必须继续使用各自的专用端点，不能正确地伪装成 `/v1/images/generations` 的图片响应。

### 4.2 NewAPI 渠道配置与计费接入

在 NewAPI 的 Gateway 渠道高级设置中，按以下方式配置：

| 设置项 | 值 | 原因 |
|---|---|---|
| 透传请求体 | 开启 | `novelai_operation`、图片 Base64、`references` 和 `characters` 必须到达 Gateway |
| 请求头覆盖 | 不需要 | 统一操作由请求体选择，不依赖固定请求头 |
| 请求配置参数覆盖 | 不需要 | 此项是整个渠道的固定覆盖，无法按单次请求选择功能 |
| 强制格式化 | 关闭 | Gateway 已返回 OpenAI 标准图片 JSON，二次格式化可能破坏 `usage` 或 `b64_json` |
| `service_tier` 透传 | 默认关闭 | 避免客户端意外请求 `priority` 而增加实际 Anlas 消耗 |

下游应用的 Base URL 指向 NewAPI 根地址，仍请求标准路径 `/v1/images/generations`。每次调用在 JSON body 内传 `novelai_operation`，不能把它放进 `model`、`prompt` 或自定义 URL 路径。

```json
{
  "model": "nai-v4.5-full",
  "prompt": "watercolor landscape",
  "novelai_operation": "img2img",
  "image": "<BASE64_IMAGE>",
  "width": 512,
  "height": 512,
  "strength": 0.7
}
```

NewAPI 必须按 Gateway 响应中的 `usage.prompt_tokens` 计费，不应按同一路径 `/v1/images/generations` 设固定单价。当前换算为：

$$
	ext{prompt\_tokens}=\max(1,\operatorname{round}(\frac{\text{Anlas}}{20}\times1000))
$$

例如：普通 `512x512`、28 steps 文生图为 5 Anlas，即 250 tokens；Director 去背景为 65 Anlas，即 3250 tokens。图生图、重绘、Vibe、Character Reference 和 Precise Reference 保留各自原有的动态 Anlas 计算，改用统一路径不会改变其计费结果。

`upscale` 和 `annotate` 目前没有已验证的 Gateway 成本映射。为避免它们在同一路径下被错误计为 0 token，统一入口会拒绝这两个 operation；请直连 `/v1/images/upscale` 或 `/v1/images/annotate`，直到配置了可审计的费用规则。

### 4.3 NewAPI 统一调用示例

下列请求都发送到同一个路径。替换 `<NEWAPI_HOST>` 为 NewAPI 地址，替换图片占位符为图片 Base64。

```bash
# 普通文生图：novelai_operation 可省略
curl -X POST http://<NEWAPI_HOST>/v1/images/generations \
  -H "Content-Type: application/json" \
  -d '{"model":"nai-v4.5-full","prompt":"anime landscape","size":"832x1216","steps":28}'

# Precise Reference
curl -X POST http://<NEWAPI_HOST>/v1/images/generations \
  -H "Content-Type: application/json" \
  -d '{"model":"nai-v4.5-full","prompt":"anime portrait","width":832,"height":1216,"novelai_operation":"precise-reference","references":[{"reference_image":"<BASE64_IMAGE>","reference_type":"character&style","strength":1.0,"fidelity":1.0}]}'

# Vibe Transfer
curl -X POST http://<NEWAPI_HOST>/v1/images/generations \
  -H "Content-Type: application/json" \
  -d '{"model":"nai-v4.5-full","prompt":"portrait","novelai_operation":"vibe-transfer","reference_image":"<BASE64_IMAGE>","reference_strength":0.6,"reference_information_extracted":1.0,"width":512,"height":512}'

# Character Reference
curl -X POST http://<NEWAPI_HOST>/v1/images/generations \
  -H "Content-Type: application/json" \
  -d '{"model":"nai-v4.5-full","prompt":"two friends at a cafe","novelai_operation":"character-reference","width":832,"height":1216,"characters":[{"reference_image":"<BASE64_IMAGE_A>","prompt":"1girl, orange hair","center":{"x":0.3,"y":0.5}},{"reference_image":"<BASE64_IMAGE_B>","prompt":"1girl, black hair","center":{"x":0.7,"y":0.5}}]}'

# Director 线稿提取；结果为 OpenAI JSON 的 data[0].b64_json
curl -X POST http://<NEWAPI_HOST>/v1/images/generations \
  -H "Content-Type: application/json" \
  -d '{"novelai_operation":"director-lineart","image":"<BASE64_IMAGE>","width":1024,"height":1024}'
```

完整 operation 列表在上节表格中。对于 `img2img`、`inpainting`、`edits`、`director-*`，保留原专用端点示例里的 JSON 字段，只新增 `novelai_operation` 并把 URL 改为上述标准路径即可。经 NewAPI 的 `edits` 必须使用 JSON Base64，不支持 multipart 文件上传。

#### 将本文件的直连图像示例改为 NewAPI 调用

本文件后续的图像示例仍保留各自的直连 Gateway 路径，方便不了解扩展协议的客户端使用。要经 NewAPI 调用其中任意一个**JSON 图像示例**，只做以下两项替换，body 保持完全不变：

```text
原路径：POST /v1/images/<专用功能>
替换为：POST /v1/images/generations
在 JSON body 中新增："novelai_operation": "<上表对应 operation>"
```

例如，将图生图示例改为经 NewAPI 调用：

```bash
curl -X POST http://<NEWAPI_HOST>/v1/images/generations \
  -H "Content-Type: application/json" \
  -d '{
    "novelai_operation":"img2img",
    "model":"nai-v4.5-full",
    "prompt":"watercolor landscape",
    "image":"<BASE64_IMAGE>",
    "width":512,
    "height":512,
    "strength":0.7
  }'
```

`/v1/images/edits` 的 multipart 文件上传形式不能经这个统一 JSON 端点转发；需改为本文件给出的 JSON Base64 形式，再按上述规则加入 `"novelai_operation":"edits"`。所有统一入口的图片结果均从 `data[0].b64_json` 读取。

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

其中 `prompt_tokens = round(Anlas / 20 * 1000)`，最小为 1。JSON 端点的 `usage` 是供 NewAPI 等下游按 token 规则计价的映射，不是 NovelAI 原始 token 计数。

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

`reference_type` 只能为 `character`、`style`、`character&style`。NovelAI 当前上游要求每项 `fidelity` **严格为 `1.0`**；网关会在发送前校验，其他值返回 `400`。因此当前不能将 `fidelity` 当作可调强度；请使用 `strength` 调整参考约束强度。

为兼容只放行标准 OpenAI 图像路由的 NewAPI，也可向 `POST /v1/images/generations` 发送完全相同的 `references` 数组，并在 JSON body 中加入 `"novelai_operation":"precise-reference"`。网关以该字段明确启用 Precise Reference；此时不要同时传 `reference_image`。请求头 `X-NovelAI-Operation: precise-reference` 仅保留为兼容旧调用方的备选方案。

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

这是控制图生成端点。将其 PNG 响应 Base64 编码后，作为 `/v1/images/generations` 的 `controlnet_condition`，同时设置相同的 `controlnet_model`，才能生成受控制图约束的最终图片。

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
| `/v1/images/director/declutter` | `declutter` | - | 5 | `X-Anlas-Cost: 5` |
| `/v1/images/director/bg-remover` | `bg-removal` | - | 65 | `X-Anlas-Cost: 65` |
| `/v1/images/director/lineart` | `lineart` | - | 5 | `X-Anlas-Cost: 5` |
| `/v1/images/director/sketch` | `sketch` | - | 5 | `X-Anlas-Cost: 5` |
| `/v1/images/director/colorize` | `colorize` | `prompt`、`defry` | 5 | `X-Anlas-Cost: 5` |
| `/v1/images/director/emotion` | `emotion` | `prompt`、`defry` | 5 | `X-Anlas-Cost: 5` |

`defry` 会被限制为 0 到 5。所有 Director Tools 还会返回 `X-Prompt-Tokens`，其值按 `Anlas / 20 * 1000` 映射。

## 8. 计费

### 8.1 生成类端点

适用于 `generations`、`img2img`、`inpainting`、`edits`、`vibe-transfer`、`character-reference`、`precise-reference`：

$$
  ext{base} = \left\lceil 2.951823174884865\times10^{-6}r + 5.753298233447344\times10^{-7}r\cdot\text{steps}\right\rceil
$$

其中 $r=\max(width\times height,65536)$。

- 图生图在 `strength < 1` 时、掩膜重绘在 `inpaintImg2ImgStrength < 1` 时：`max(ceil(base * strength_factor), 2)`。
- `uncond_scale != 1` 时，再乘以该值并向上取整。
- 最终乘以 `n_samples`。
- Opus 免费额度适用于单张、`steps<=28`、像素面积不超过 $1024\times1024$ 且未使用 Character Reference 的 V4/V4.5 生成；批量请求仅减免第一张。
- Vibe/Character Reference：每张加 2 Anlas；第 5 张及以后每张再额外加 2。
- Precise Reference：每张参考图、每个请求样本加 5 Anlas。

示例：`512x512`、28 steps、单张文生图为 5 Anlas；同规格 `strength=0.7` 的图生图/重绘为 4 Anlas；单张 Vibe/Character Reference 为 7 Anlas；单张 Precise Reference 为 10 Anlas。

### 8.2 二进制工具端点与统一入口计费

- Director Tools：固定费用见上表，通过 `X-Anlas-Cost` 返回。
- Upscale：上游消耗，但当前包装响应不生成 `usage` 或 `X-Anlas-Cost`；下游如需计价，应按路径自行配置。
- Annotate 与 suggest-tags：不生成图像 Anlas 映射。

当这些图片工具通过 `POST /v1/images/generations` 加 `novelai_operation` 调用时，Gateway 会将 PNG 转换为标准 OpenAI `b64_json`。计费行为如下：

| operation 类别 | Gateway 返回的 `usage.prompt_tokens` | NewAPI 计费是否保持有效 |
|---|---:|---|
| 文生图、图生图、两种重绘、Vibe、Character Reference、Precise Reference | 按现有 Anlas 公式 | 是；NewAPI 应按 response `usage` 计费 |
| 六个 Director Tools | 固定 5 或 65 Anlas 的 token 映射 | 是；例如 5 Anlas 返回 250 tokens，65 Anlas 返回 3250 tokens |
| `upscale` | 不返回 | 不适用；统一入口会拒绝，避免 0 token 漏计费 |
| `annotate` | 不返回 | 不适用；统一入口会拒绝，避免 0 token 漏计费 |

因此，**不要在 NewAPI 为统一图片渠道只配置“按请求次数”或“按路径”的固定价格**：所有请求路径都相同，无法区分功能。对已有 Anlas 映射的 operation，使用响应 `usage.prompt_tokens` 计费即可。`upscale` 和 `annotate` 若必须收费，需先确定你希望采用的固定价格或已验证的 NovelAI 成本，再由 Gateway 写入相应 `usage`；在此之前，统一入口会直接返回 `400`，调用方应改走它们的专用 Gateway 路径。

## 9. Chat 与 TTS

### `POST /v1/chat/completions`

OpenAI Chat Completions 兼容接口，直连 Gateway，不经过 NewAPI 图像渠道。支持 `messages`、`model`、`stream`、`temperature`、`max_tokens`、`top_p`。启用时可返回标准 JSON 或 SSE。当前仅支持文本 `messages`，不支持 OpenAI 图片 content parts、视觉理解或其他多模态 Chat 输入；图片能力请使用本文的图像接口。

NovelAI 当前产品页面列出 Xialong、GLM-4.6、Erato、Kayra、Clio。网页展示名不一定等于原生文本 API 模型 ID；网关通过 `config/models.toml` 的 `[[chat.models]]` 公开模型。下列两个映射已经使用当前共享凭据在原生文本 API 实测成功：

| Gateway `model` | NovelAI 内部模型名 | 当前状态 |
|---|---|---|
| `nai-chat-erato` | `llama-3-erato-v1` | 已实测成功 |
| `nai-chat-kayra` | `kayra-v1` | 已实测成功 |

当前凭据的原生文本 API 探测结果：`xialong` 与 `clio-v1` 返回“model does not exist”；`glm-4-6` 返回“only available via OpenAI-compatible API”。因此它们没有写入运行时默认模型列表。若未来要接入 GLM-4.6，应连接 NovelAI 提示的专用 OpenAI-compatible 上游，而不是把它配置到本 Gateway 的原生文本转发。

只可使用 `GET /v1/models` 实际返回的 Chat 模型。未知模型会返回 `400`，不会再静默回退为 `xialong`。上游返回 `403` 表示共享凭据没有该文本模型权限，不是 OpenAI 请求格式错误。

非流式调用：

```bash
curl -X POST http://127.0.0.1:41555/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{
    "model":"nai-chat-erato",
    "messages":[
      {"role":"system","content":"Answer concisely."},
      {"role":"user","content":"Explain what an API gateway is."}
    ],
    "temperature":0.8,
    "max_tokens":200,
    "top_p":0.9,
    "stream":false
  }'
```

成功时返回 OpenAI `chat.completion` JSON；流式调用将 `stream` 改为 `true`，响应为 `text/event-stream`，终止帧为 `data: [DONE]`。

默认模板中 `chat_enabled=false`。未启用时请求会返回：

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

OpenAI TTS 兼容接口，直连 Gateway，不经过 NewAPI 图像渠道。所有本节参数都会经过校验后转为 NovelAI 的 `POST /ai/generate-voice` 请求。`model` 提供稳定默认值；下游可按单次请求覆盖版本、预设语音、种子和封装格式。

| `model` | NovelAI TTS 默认配置 | 成功响应 |
|---|---|---|
| `tts0-v1-mp3` | v1, voice 0, Aini | `audio/mpeg` |
| `tts0-v1-opus` | v1, voice 0, Aini | `audio/webm` |
| `tts1-v1-mp3` | v1, voice 1, Kayra | `audio/mpeg` |
| `tts1-v1-opus` | v1, voice 1, Kayra | `audio/webm` |
| `tts0-v2-mp3` | v2, voice 0, Aini | `audio/mpeg` |
| `tts0-v2-opus` | v2, voice 0, Aini | `audio/webm` |

#### TTS 请求参数

| 字段 | 类型与取值 | 默认值 | 映射到 NovelAI | 用途 |
|---|---|---|---|---|
| `model` | 已在 `GET /v1/models` 中公开的 TTS ID | `tts0-v2-mp3` | 选择配置默认值 | 选择基础版本、voice、格式与 seed |
| `input` | 非空 string | 无 | `text` | 要朗读的文本 |
| `version` | `v1` 或 `v2` | 由 `model` 决定 | `version` | v2 支持 seedmix 与三个独立的声音维度；v1 更简单 |
| `voice_id` | `-1`、`0`、`1` | 由 `model` 决定 | `voice` | 选择上游预设语音；`-1` 表示自定义种子模式 |
| `seed` | 非空 string | 由 `model` 决定 | `seed`，并强制 `voice=-1` | 自定义声音；常见人名会影响音色与语调 |
| `voice` | 非空 string | 无 | 与 `seed` 相同 | OpenAI 兼容的 `seed` 别名；不能与不同的 `seed` 同时使用 |
| `response_format` | `mp3` 或 `opus` | 由 `model` 决定 | `opus=false/true` | 选择返回容器：MP3 或 WebM/Opus |
| `opus` | boolean | 由 `model` 决定 | `opus` | `response_format` 的底层等价形式；不可与其冲突 |

`opus=true` 的 NovelAI 当前响应容器为 **WebM/Opus**，故网关返回 `Content-Type: audio/webm`，不是 `audio/ogg`。已通过真实上游验证。

`speed` 和 `volume` 是 NovelAI 网页播放器参数，不会影响下载的音频文件；网关会明确返回 `400`，避免下游误以为它们已经生效。

#### 声音调校方法

- 只换预设声音：传 `voice_id: 0` 或 `voice_id: 1`，不要同时传 `seed`。
- 自定义声音：传任意非空 `seed`，网关自动使用 `voice_id=-1`。例如 `Maria` 往往产生更偏女性化的音色。
- v2 混音：`seed` 以 `seedmix:` 开头，用 `+` 混合、用 `-` 减弱某个种子，例如 `seedmix:Kayra+Clio-Calliope`。seedmix 中不能有空格。
- v2 分维度调校：使用 `|style:`、`|intonation:`、`|cadence:` 分别设置种子。例如：

```text
seedmix:|style:Kayra+Clio|intonation:Krake+Euterpe|cadence:Genji-Snek
```

`style` 主要影响整体音调的高低与风格；`intonation` 最明显地决定像哪个人在说话；`cadence` 改变音素的快慢和重音，问句或感叹句更容易听出区别。`seedmix:` 仅适用于 `version: "v2"`。

```bash
curl -X POST http://127.0.0.1:41555/v1/audio/speech \
  -H "Content-Type: application/json" \
  -d '{
    "model":"tts0-v2-mp3",
    "input":"Hello from NovelAI Gateway.",
    "version":"v2",
    "seed":"seedmix:Kayra+Clio-Calliope",
    "response_format":"mp3"
  }' \
  --output speech.mp3
```

Opus 示例：

```bash
curl -X POST http://127.0.0.1:41555/v1/audio/speech \
  -H "Content-Type: application/json" \
  -d '{"model":"tts0-v2-mp3","input":"Hello.","voice_id":1,"response_format":"opus"}' \
  --output speech.webm
```

实际验证中，`tts0-v2-mp3` 返回 `200 audio/mpeg` 与有效 `ID3` MP3 数据；`opus=true` 返回有效的 WebM/Opus 数据。

默认模板中 `tts_enabled=false`。未启用时返回 `403` 和 `tts_disabled`；启用前必须确认账户具备可用 TTS 权限。

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

`nai-v4.5-full-limit`、`nai-v4.5-curated-limit`、`nai-v4.5-inpaint-limit` 是网关提供的保护性模型名，不是 NovelAI 的独立上游模型。它们只允许走符合 Opus 免费边界的单张生成、图生图或重绘请求：

| 条件 | 必须满足 |
|---|---|
| 动作 | `generate`、`img2img` 或 `infill` |
| 样本数 | `n_samples=1`；OpenAI 字段 `n` 也必须为 `1` |
| 步数 | `steps<=28` |
| 像素面积 | `width*height<=1048576` |
| 参考图 | 不允许 `reference_image`、`reference_images` |
| 优先级 | 不允许 `service_tier=priority` |

Vibe、Character Reference、Precise Reference、upscale、Director Tools 等会产生额外 Anlas 费用的接口会拒绝 `-limit` 模型。图生图或重绘超出上表边界时同样会被拒绝；请改用去掉 `-limit` 后缀的原版模型。

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
  ext{prompt\_tokens}=\max(1,\operatorname{round}(\frac{\text{Anlas}}{20}\times1000))
$$

常用示例：

| 请求 | 网关 Anlas | `prompt_tokens` |
|---|---:|---:|
| 512x512、28 steps、单张文生图 | 5 | 250 |
| 1024x1024、28 steps、单张文生图 | 20 | 1000 |
| 512x512、28 steps、`strength=0.7` 图生图 | 4 | 200 |
| 512x512、28 steps、单张 Vibe/Character Reference | 7 | 350 |
| 512x512、28 steps、单张 Precise Reference | 10 | 500 |

直连 Director Tools 返回 `X-Anlas-Cost` 与 `X-Prompt-Tokens` 响应头；经统一图片入口调用时，Gateway 会把相同成本写入标准 `usage`：

```http
X-Anlas-Cost: 5
X-Prompt-Tokens: 250
```

直连 `upscale`、`annotate`、`suggest-tags` 不附加网关 `usage`。`upscale` 与 `annotate` 当前不能经统一入口调用，因为网关没有这两项的可审计费用规则；统一路径下也不能让 NewAPI 按路径设置不同固定价格。

### 13.2 tiered_expr 示例

若 NewAPI 以 20 Anlas = 1000 prompt tokens 为计费基准，可使用类似表达式：

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
| `SHARED_API_KEYS` | 空 | 多个持久 NovelAI API Key；JSON 数组或逗号/分号/换行分隔，优先于单 Key 并按请求轮询 |
| `SHARED_API_KEY` | 空 | 单个持久 NovelAI API Key；仅在 `SHARED_API_KEYS` 为空时使用 |
| `SHARED_TOKEN` | 空 | Session token JSON 或裸 token；仅在两类 API Key 配置均为空时使用 |
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
| `400` 且 `-limit` 提示 | 改用非 `-limit` 模型，或改为符合免费生成边界的单张文生图、图生图或重绘请求 |
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

### 15.9 Precise Reference（经 NewAPI 或仅放行标准生成路由时推荐）

```bash
curl -X POST http://127.0.0.1:41555/v1/images/generations \
  -H "Content-Type: application/json" \
  -d '{
    "novelai_operation":"precise-reference",
    "model":"nai-v4.5-full",
    "prompt":"anime portrait",
    "width":512,
    "height":512,
    "references":[
      {"reference_image":"<BASE64_IMAGE>","reference_type":"character&style","strength":1.0,"fidelity":1.0}
    ]
  }'
```

这是推荐的统一路径：NewAPI 只会看到标准的 `/v1/images/generations`。`references` 中每一项都必须含 `reference_image`，`reference_type` 只能是 `character`、`style` 或 `character&style`。不要与 Vibe 的 `reference_image` 同时传递。

直接调用 Gateway 时，旧别名 `POST /v1/images/precise-reference` 仍可用：

```bash
curl -X POST http://127.0.0.1:41555/v1/images/precise-reference \
  -H "Content-Type: application/json" \
  -d '{
    "model":"nai-v4.5-full",
    "prompt":"anime portrait",
    "width":832,
    "height":1216,
    "steps":28,
    "response_format":"b64_json",
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

### 15.13 模型列表

```bash
curl http://127.0.0.1:41555/v1/models
```

响应是 OpenAI 模型列表 JSON。调用图片、Chat 或 TTS 前可先用此端点确认当前配置实际公开的模型 ID。

### 15.14 获取 `response_format=url` 的图片

当生成响应中的 `data[0].url` 为 `/images/<filename>` 时，直接 GET 该 URL：

```bash
curl http://127.0.0.1:41555/images/<filename> --output generated.png
```

`<filename>` 只能使用生成响应给出的文件名；该端点不接受任意本地路径。

### 15.15 所有 Director Tools

以下六个端点均直接返回 `image/png`。将 `<BASE64_IMAGE>` 替换为裸 Base64 图片；使用 `--output` 保存返回的二进制 PNG。`width` 和 `height` 应与输入图实际尺寸一致。

```bash
# 去杂物：POST /v1/images/director/declutter
curl -X POST http://127.0.0.1:41555/v1/images/director/declutter \
  -H "Content-Type: application/json" \
  -d '{"image":"<BASE64_IMAGE>","width":1024,"height":1024}' \
  --output declutter.png

# 去背景：POST /v1/images/director/bg-remover
curl -X POST http://127.0.0.1:41555/v1/images/director/bg-remover \
  -H "Content-Type: application/json" \
  -d '{"image":"<BASE64_IMAGE>","width":1024,"height":1024}' \
  --output background-removed.png

# 提取线稿：POST /v1/images/director/lineart
curl -X POST http://127.0.0.1:41555/v1/images/director/lineart \
  -H "Content-Type: application/json" \
  -d '{"image":"<BASE64_IMAGE>","width":1024,"height":1024}' \
  --output lineart.png

# 草图化：POST /v1/images/director/sketch
curl -X POST http://127.0.0.1:41555/v1/images/director/sketch \
  -H "Content-Type: application/json" \
  -d '{"image":"<BASE64_IMAGE>","width":1024,"height":1024}' \
  --output sketch.png

# 线稿上色：POST /v1/images/director/colorize
curl -X POST http://127.0.0.1:41555/v1/images/director/colorize \
  -H "Content-Type: application/json" \
  -d '{"image":"<BASE64_IMAGE>","width":1024,"height":1024,"prompt":"bright orange and blue","defry":1}' \
  --output colorized.png

# 情感迁移：POST /v1/images/director/emotion
curl -X POST http://127.0.0.1:41555/v1/images/director/emotion \
  -H "Content-Type: application/json" \
  -d '{"image":"<BASE64_IMAGE>","width":1024,"height":1024,"prompt":"happy;;","defry":1}' \
  --output emotion.png
```

`colorize` 与 `emotion` 的 `prompt` 可为空，`defry` 可选且会被限制到 0 至 5。响应头 `X-Anlas-Cost` 和 `X-Prompt-Tokens` 分别给出固定 Anlas 消耗与其 NewAPI token 映射。

### 15.16 Chat Completions

该端点仅在 `chat_enabled=true` 且模型配置中启用了 Chat 后可用；当前默认配置会返回 `403`。非流式调用：

```bash
curl -X POST http://127.0.0.1:41555/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{
    "model":"nai-chat-xialong",
    "messages":[
      {"role":"system","content":"Answer briefly."},
      {"role":"user","content":"Explain what an API gateway is."}
    ],
    "temperature":0.8,
    "max_tokens":200,
    "top_p":0.9,
    "stream":false
  }'
```

流式调用仅将 `stream` 改为 `true`；响应为 OpenAI 格式的 Server-Sent Events，终止事件为 `data: [DONE]`：

```bash
curl -N -X POST http://127.0.0.1:41555/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{"model":"nai-chat-erato","messages":[{"role":"user","content":"Say hello."}],"stream":true}'
```

### 15.17 Text-to-Speech

该端点仅在 `tts_enabled=true` 且模型配置中启用了 TTS 后可用。成功响应为 `audio/mpeg`，或模型配置/请求指定 Opus 时的 `audio/webm`。完整的参数含义、冲突规则、`seedmix:` 语法见 [TTS 请求参数](#tts-请求参数)。

```bash
curl -X POST http://127.0.0.1:41555/v1/audio/speech \
  -H "Content-Type: application/json" \
  -d '{
    "model":"tts0-v2-mp3",
    "input":"Hello from NovelAI Gateway.",
    "seed":"seedmix:Kayra+Clio-Calliope",
    "response_format":"mp3"
  }' \
  --output speech.mp3
```

使用 `response_format:"opus"` 时，输出文件应保存为 `.webm`。

### 15.18 刷新上游模型建议（管理端点）

该端点抓取 NovelAI 网页 bundle 并写入建议模型配置。若未设置 `GATEWAY_PASSWORD`，可以直接调用；若已设置，必须带 Bearer 密码：

```bash
curl -X POST http://127.0.0.1:41555/admin/refresh-upstream-models \
  -H "Authorization: Bearer <GATEWAY_PASSWORD>"
```

不要将管理密码配置给普通下游客户端。

### 15.19 NovelAI 原始 API 透明代理

原始 NovelAI API 路径前加 `/_api` 即可由网关转发。此模式不转换 OpenAI 请求或响应，也不返回网关 `usage` 映射；适用于已经实现 NovelAI 原生协议的调用方。

```bash
# 原始 GET /user/subscription -> Gateway GET /_api/user/subscription
curl http://127.0.0.1:41555/_api/user/subscription

# 原始 POST /ai/generate-image -> Gateway POST /_api/ai/generate-image
curl -X POST http://127.0.0.1:41555/_api/ai/generate-image \
  -H "Content-Type: application/json" \
  -d '{
    "input":"1girl, orange hair",
    "model":"nai-diffusion-4-5-full",
    "action":"generate",
    "parameters":{
      "width":512,
      "height":512,
      "n_samples":1,
      "steps":28,
      "scale":5,
      "sampler":"k_euler_ancestral",
      "noise_schedule":"karras"
    }
  }'
```

### 15.20 NovelAI 网页透明代理

所有不匹配上述固定路由的请求会作为 NovelAI 网站路径转发。例如：

```bash
curl -L http://127.0.0.1:41555/image --output novelai-image.html
```

该端点主要供浏览器访问和网页劫持脚本使用，并非下游 API 集成入口。设置 `GATEWAY_PASSWORD` 后，GET 请求需要名为 `gw_pass` 的 Cookie：

```bash
curl -L http://127.0.0.1:41555/image \
  -H "Cookie: gw_pass=<GATEWAY_PASSWORD>" \
  --output novelai-image.html
```

## 16. 已验证的当前网关行为

以下结果来自 2026-07-17 对运行中 Gateway 的实际上游调用。图像测试使用单张 `512x512` 输入或最短提示词；生成类响应均至少包含一张图片，二进制工具均校验了 PNG 文件头。

| 功能 | 请求方式 | 实测结果 |
|---|---|---|
| 文生图 | `/v1/images/generations`，`nai-v4.5-full-limit` | `200`，OpenAI 图片 JSON |
| 图生图 | `/v1/images/img2img`，`nai-v4.5-full-limit` | `200`，OpenAI 图片 JSON |
| NAI 局部重绘 | `/v1/images/inpainting`，`nai-v4.5-inpaint-limit` | `200`，OpenAI 图片 JSON |
| OpenAI edits | `/v1/images/edits`，`nai-v4.5-inpaint-limit` | `200`，OpenAI 图片 JSON |
| 控制图链 | `annotate(canny)` 后作为 `controlnet_condition` 生图 | 两步均 `200`，最终图片成功 |
| Vibe Transfer | `/v1/images/vibe-transfer` | `200`，OpenAI 图片 JSON |
| Character Reference | `/v1/images/character-reference` | `200`，OpenAI 图片 JSON |
| Precise Reference | `/v1/images/precise-reference`，`fidelity:1.0` | `200`，OpenAI 图片 JSON |
| 放大 | `/v1/images/upscale` | `200 image/png` |
| 控制图预处理 | `/v1/images/annotate` 的 7 个模型 | `canny`、`hed`、`midas`、`mlsd`、`openpose`、`uniformer`、`fake_scribble` 均 `200 image/png` |
| 标签建议 | `/v1/images/suggest-tags` | `200`，返回 10 个标签 |
| Director Tools | 全部 6 条 `/v1/images/director/*` 路径 | 均 `200 image/png`；`declutter`、`lineart`、`sketch`、`colorize`、`emotion` 为 5 Anlas，`bg-remover` 为 65 Anlas |
| Chat | `nai-chat-erato`、`nai-chat-kayra` | 均 `200` |
| TTS | `/v1/audio/speech` 的 v1、v2 seedmix、WebM/Opus | 均 `200`；MP3 为 `audio/mpeg`，Opus 为 `audio/webm` |

精密参考的 `fidelity` 当前必须严格为 `1.0`；其它值由网关直接返回 `400`。TTS 的 `speed`、`volume` 是网页播放参数，不影响下载音频，网关同样会直接返回 `400`。

部署或修改 `config/models.toml` 后，请重新执行端到端检查，以实际账户权限为准。
