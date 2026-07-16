# NovelAI 非 limit 模型按次计费 + 条件调价方案

> **目标**：在不修改 NewAPI 源码的前提下，利用 NewAPI 的 `tiered_expr` 计费模式实现 NovelAI 图像生成的"按次计费 + 条件调价"。

## 一、核心原理

利用 NewAPI 的 `tiered_expr` 计费模式，把"按次计费"伪装成"按 token 计费"：

- NovelAI 网关在图像响应中返回 `"usage": {"prompt_tokens": 1, "completion_tokens": 0, "total_tokens": 1}`
- NewAPI 的 `tiered_expr` 按 token 计费：`1 token × $24000/1M = $0.024/次`
- 通过 `param()` 函数读取请求参数，实现条件调价倍率

### 计费公式

```
quota = exprOutput / 1,000,000 × QuotaPerUnit × groupRatio
     = exprOutput / 1,000,000 × 500000 × 1
     = exprOutput / 2

价格($) = quota / QuotaPerUnit = exprOutput / 1,000,000
```

要实现 $0.024/次：`exprOutput = 0.024 × 1,000,000 = 24000`，即表达式 `p * 24000`。

## 二、NewAPI 支持的端点

NewAPI 只对以下端点调用 `ImageHelper`（走 `tiered_expr` 计费）：

| NewAPI 端点 | NovelAI 网关端点 | 说明 |
|-------------|------------------|------|
| `/v1/images/generations` | `/v1/images/generations` | 文生图、img2img、inpaint、vibe-transfer（通过 `action` 参数区分） |
| `/v1/images/edits` | `/v1/images/edits` | 局部重绘（OpenAI 兼容） |
| `/v1/audio/speech` | `/v1/audio/speech` | TTS（走音频计费） |

**NewAPI 不认识的端点**（不走 `ImageHelper`，不计费）：

| NovelAI 网关端点 | 说明 | 处理方式 |
|------------------|------|----------|
| `/v1/images/img2img` | 图生图 | 改用 `/v1/images/generations` + `action=img2img` |
| `/v1/images/vibe-transfer` | 风格迁移 | 改用 `/v1/images/generations` + `reference_image` |
| `/v1/images/inpainting` | 局部重绘(NAI格式) | 改用 `/v1/images/edits` |
| `/v1/images/upscale` | 图像放大 | 见下方"upscale/director 计费" |
| `/v1/images/director/*` | 导演工具 | 见下方"upscale/director 计费" |
| `/v1/images/annotate` | 注释图 | 免费，不计费 |
| `/v1/images/suggest-tags` | 标签建议 | 免费，不计费 |

## 三、图像生成完整条件调价表达式

### 3.1 设置计费模式

**系统设置 → 分组与模型定价设置 → 模型计费模式**

将 NovelAI 非 limit 模型的计费模式设为 `tiered_expr`。

### 3.2 计费表达式（完整版，覆盖所有消耗 Anlas 的参数）

```
tier("base", p * 24000) * (param("n_samples") != nil && param("n_samples") > 1 ? 2 : 1) * (param("steps") != nil && param("steps") > 28 ? 2 : 1) * (param("size") == "1216x832" ? 1.5 : 1) * (param("size") == "832x1216" ? 1.5 : 1) * (param("size") == "1536x1024" ? 2 : 1) * (param("size") == "1024x1536" ? 2 : 1) * (param("image") != nil ? 2 : 1) * (param("reference_image") != nil ? 2 : 1) * (param("reference_images") != nil ? 2 : 1) * (param("service_tier") == "priority" ? 2 : 1) * (param("controlnet_condition") != nil ? 1.5 : 1) * (param("action") == "img2img" ? 2 : 1) * (param("action") == "infill" ? 2 : 1)
```

### 3.3 条件组完整说明

| 组 | 参数 | 运算符 | 匹配值 | 倍率 | 说明 |
|---|---|---|---|---|---|
| 1 | `n_samples` | 大于 | `1` | **2** | 多张生成（每张额外计费） |
| 2 | `steps` | 大于 | `28` | **2** | 高步数（>28 步） |
| 3 | `size` | 等于 | `1216x832` | **1.5** | 大图横版 |
| 4 | `size` | 等于 | `832x1216` | **1.5** | 大图竖版 |
| 5 | `size` | 等于 | `1536x1024` | **2** | 超大图横版 |
| 6 | `size` | 等于 | `1024x1536` | **2** | 超大图竖版 |
| 7 | `image` | 存在 | （空） | **2** | img2img / inpaint（有输入图） |
| 8 | `reference_image` | 存在 | （空） | **2** | vibe-transfer 单图 |
| 9 | `reference_images` | 存在 | （空） | **2** | vibe-transfer 多图 |
| 10 | `service_tier` | 等于 | `priority` | **2** | 优先调度附加 |
| 11 | `controlnet_condition` | 存在 | （空） | **1.5** | ControlNet 附加 |
| 12 | `action` | 等于 | `img2img` | **2** | img2img 动作 |
| 13 | `action` | 等于 | `infill` | **2** | inpaint 动作 |

> **注意**：组 7（`image` 存在）和组 12/13（`action`）会叠加。如果请求同时有 `image` 和 `action=img2img`，倍率 = 2×2 = 4。如不想叠加，删除组 12/13，只保留组 7。

### 3.4 完整计费示例表

| 场景 | 命中组 | 计算 | 最终倍率 | 价格 |
|------|--------|------|----------|------|
| 文生图 1024²·28步·1张 | 无 | 1 | 1 | **$0.024** |
| 文生图 1216×832·28步·1张 | 3 | 1.5 | 1.5 | $0.036 |
| 文生图 832×1216·28步·1张 | 4 | 1.5 | 1.5 | $0.036 |
| 文生图 1536×1024·28步·1张 | 5 | 2 | 2 | $0.048 |
| 文生图 1024×1536·28步·1张 | 6 | 2 | 2 | $0.048 |
| 文生图 1024²·50步·1张 | 2 | 2 | 2 | $0.048 |
| 文生图 1024²·28步·2张 | 1 | 2 | 2 | $0.048 |
| 文生图 1024²·50步·2张 | 1+2 | 2×2 | 4 | $0.096 |
| 文生图 1216×832·50步·1张 | 2+3 | 2×1.5 | 3 | $0.072 |
| 文生图 1024²·28步·1张+priority | 10 | 2 | 2 | $0.048 |
| 文生图 1024²·28步·1张+controlnet | 11 | 1.5 | 1.5 | $0.036 |
| img2img 1024²·28步·1张 | 7+12 | 2×2 | 4 | $0.096 |
| img2img 1216×832·28步·1张 | 3+7+12 | 1.5×2×2 | 6 | $0.144 |
| img2img 1024²·50步·1张 | 2+7+12 | 2×2×2 | 8 | $0.192 |
| img2img 1216×832·50步·1张 | 2+3+7+12 | 2×1.5×2×2 | 12 | $0.288 |
| inpaint 1024²·28步·1张 | 7+13 | 2×2 | 4 | $0.096 |
| vibe-transfer 1图·1024²·28步 | 8 | 2 | 2 | $0.048 |
| vibe-transfer 多图·1024²·28步 | 9 | 2 | 2 | $0.048 |
| vibe-transfer 多图·priority | 9+10 | 2×2 | 4 | $0.096 |
| 1536×1024·50步·img2img·priority·CN | 2+5+7+10+11+12 | 2×2×2×2×1.5×2 | 96 | **$2.304** |

> **⚠️ 注意**：最后一行倍率 96 是因为 `image` 和 `action=img2img` 叠加（2×2=4）。如不想叠加，删除组 12/13，倍率降为 48（$1.152）。

### 3.5 推荐表达式（避免叠加，删除组 12/13）

如果不想 `image` 和 `action` 叠加双重计费，使用以下表达式（只保留组 7，删除组 12/13）：

```
tier("base", p * 24000) * (param("n_samples") != nil && param("n_samples") > 1 ? 2 : 1) * (param("steps") != nil && param("steps") > 28 ? 2 : 1) * (param("size") == "1216x832" ? 1.5 : 1) * (param("size") == "832x1216" ? 1.5 : 1) * (param("size") == "1536x1024" ? 2 : 1) * (param("size") == "1024x1536" ? 2 : 1) * (param("image") != nil ? 2 : 1) * (param("reference_image") != nil ? 2 : 1) * (param("reference_images") != nil ? 2 : 1) * (param("service_tier") == "priority" ? 2 : 1) * (param("controlnet_condition") != nil ? 1.5 : 1)
```

## 四、upscale / director / tts 计费

这些端点 NewAPI 不认识，不走 `ImageHelper`。有以下处理方式：

### 4.1 方案 A：通过 `/v1/images/generations` 统一访问（推荐）

把 upscale/director 操作通过 `/v1/images/generations` + 特殊 `action` 参数访问，网关根据 `action` 转发到对应上游端点。

**需要网关支持**（当前未实现，需开发）：
- `action=upscale` → 转发到 `/ai/upscale`
- `action=director-colorize` → 转发到 `/ai/illustration`
- `action=director-emotion` → 转发到 `/ai/emotion-transfer`

### 4.2 方案 B：NewAPI 渠道参数覆盖

在 NewAPI 渠道设置中，用"参数覆盖"（`ParamOverride`）把请求重写到 `/v1/images/generations` 端点。

### 4.3 方案 C：单独定价（当前可用）

为 upscale/director/tts 创建独立的 NewAPI 模型，用 `ModelPrice` 按次计费（不支持条件调价）：

| 模型名 | 端点 | ModelPrice |
|--------|------|-----------|
| `nai-upscale` | `/v1/images/upscale` | $0.096（4倍） |
| `nai-director-colorize` | `/v1/images/director/colorize` | $0.048（2倍） |
| `nai-director-emotion` | `/v1/images/director/emotion` | $0.048（2倍） |
| `nai-tts-v2-mp3` | `/v1/audio/speech` | $0.120（5倍） |
| `nai-tts-v2-opus` | `/v1/audio/speech` | $0.600（25倍） |

> **注意**：方案 C 需要客户端使用对应的模型名，且 NewAPI 渠道需要把这些模型名映射到正确的端点。

## 五、客户端请求示例

### 5.1 基础文生图（$0.024）

```json
POST /v1/images/generations
{
  "model": "nai-diffusion-4-5-full",
  "prompt": "1girl, cute",
  "size": "1024x1024",
  "n": 1
}
```

### 5.2 高步数大图（$0.072）

```json
POST /v1/images/generations
{
  "model": "nai-diffusion-4-5-full",
  "prompt": "1girl, cute",
  "size": "1216x832",
  "n": 1,
  "steps": 50
}
```

### 5.3 img2img（$0.048，用推荐表达式）

```json
POST /v1/images/generations
{
  "model": "nai-diffusion-4-5-full",
  "prompt": "1girl, cute",
  "size": "1024x1024",
  "n": 1,
  "action": "img2img",
  "image": "data:image/png;base64,..."
}
```

### 5.4 vibe-transfer（$0.048）

```json
POST /v1/images/generations
{
  "model": "nai-diffusion-4-5-full",
  "prompt": "1girl, cute",
  "size": "1024x1024",
  "n": 1,
  "reference_image": "data:image/png;base64,..."
}
```

### 5.5 通过 Header 传参（NewAPI 兼容）

如果 NewAPI 过滤了 body 里的非标准字段，可以通过 Header 传递：

```
POST /v1/images/generations
X-Steps: 50
X-N-Samples: 2
X-Service-Tier: priority

{
  "model": "nai-diffusion-4-5-full",
  "prompt": "1girl, cute",
  "size": "1216x832",
  "n": 1
}
```

**注意**：通过 Header 传递的参数，`tiered_expr` 的 `param()` 无法读取（`param()` 只读 body）。如需通过 Header 调价，需使用 `header()` 函数：

```
(header("x-service-tier") == "priority" ? 2 : 1)
```

## 六、注意事项

### 6.1 param() 读取的是原始请求体

`tiered_expr` 的 `param()` 通过 `BodyStorage` 读取**客户端发送的原始请求体**，不是 NewAPI 转发给上游的请求体。所以即使 NewAPI 的 `ImageRequest.MarshalJSON` 丢弃了 `Extra` 字段，`param()` 仍能读到客户端发送的所有字段。

### 6.2 -limit 模型不接入条件调价

`-limit` 模型固定 $0.005/次，使用 `ModelPrice` 按次计费即可，不需要 `tiered_expr`。

### 6.3 基准价调整

系数 `24000` = `$0.024/次`。如需改为其他价格：

```
系数 = 目标价格($) × 1,000,000
```

- `$0.024/次` → `p * 24000`
- `$0.03/次` → `p * 30000`
- `$0.005/次` → `p * 5000`

### 6.4 显示类型

- **USD**：`$0.024` 直接显示为 `$0.024`
- **CNY**（汇率 R）：显示为 `$0.024 × R` 元。如需显示为 `0.024 元`，系数改为 `0.024 / R × 1,000,000`

## 七、验证方法

### 7.1 检查网关返回的 usage

```bash
curl -X POST http://gateway:41555/v1/images/generations \
  -H "Content-Type: application/json" \
  -d '{"model":"nai-diffusion-4-5-full","prompt":"test","size":"1024x1024"}' \
  | python -m json.tool
```

响应应包含 `"usage": {"prompt_tokens": 1, "completion_tokens": 0, "total_tokens": 1}`。

### 7.2 检查 NewAPI 计费日志

在 NewAPI 的日志中查看：
- `计费模式` 应为 `tiered_expr`
- `输入` 应为 `1 tokens`
- `花费` 应为 `$0.024`（基础场景）

### 7.3 条件调价验证

发送带 `steps=50` 的请求，检查花费是否为 `$0.048`（2 倍）。
