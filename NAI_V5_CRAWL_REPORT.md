# NovelAI Diffusion V5 抓取报告

> 抓取日期：2026-08-21
> 抓取方式：NovelAI 官网（novelai.net/image）内联浏览器实测，通过 UI 价格计算器采集 Anlas 数据点 + 拦截 `/ai/generate-image-stream` 请求获取内部模型名。

---

## 1. V5 模型清单（内部名）

从页面 JS bundle 中提取到的全部图像模型名，V5 新增 4 个：

| 内部模型名 | 说明 | 状态 |
|---|---|---|
| `nai-diffusion-5-full` | V5 全量模型（"Our newest and best model"） | **新增** |
| `nai-diffusion-5-full-inpainting` | V5 全量局部重绘 | **新增** |
| `nai-diffusion-5-curated` | V5 精选模型（"Recommended for streaming"） | **新增** |
| `nai-diffusion-5-curated-inpainting` | V5 精选局部重绘 | **新增** |

Legacy 模型（已确认仍在，UI 标注 "No longer recommended for use"）：
`nai-diffusion-4-5-full`、`nai-diffusion-4-5-full-inpainting`、`nai-diffusion-4-5-curated`、`nai-diffusion-4-5-curated-inpainting`、`nai-diffusion-4-full`、`nai-diffusion-4-full-inpainting`、`nai-diffusion-4-curated-preview`、`nai-diffusion-4-curated-inpainting`、`nai-diffusion-3`、`nai-diffusion-3-inpainting`、`nai-diffusion-furry-3`、`nai-diffusion-furry-3-inpainting`。

**V5 与 V4.5 价格完全相同**（Curated 与 Full 同价，实测多组数据点一致）。

## 2. V5 请求参数变化

拦截 V5 生成请求（`generate-image-stream` 端点，multipart form，`request` 字段为 JSON）实测：

```json
{
  "model": "nai-diffusion-5-curated",
  "action": "generate",
  "parameters": {
    "params_version": 4,
    "width": 1024,
    "height": 1024,
    "scale": 7,
    "sampler": "k_euler_ancestral",
    "steps": 28,
    "n_samples": 1,
    "ucPresetId": "heavy",
    "qualityPresetId": "standard",
    "autoSmea": false,
    "dynamic_thresholding": false,
    "controlnet_strength": 1,
    "legacy": false,
    "add_original_image": true,
    "cfg_rescale": 0,
    "legacy_v3_extend": false,
    "use_coords": false,
    "legacy_uc": false,
    "normalize_reference_strength_multiple": true,
    "inpaintImg2ImgStrength": 1,
    "seed": 33538014,
    "characterPrompts": [],
    "straight_alpha": true,
    "tag_hint_qt": 1,
    "tag_hint_uc_preset": 2,
    "v4_prompt": { "caption": { "base_caption": "...", "char_captions": [] }, "use_coords": false, "use_order": true },
    "v4_negative_prompt": { "caption": { "base_caption": "...", "char_captions": [] }, "legacy_uc": false },
    "negative_prompt": "...",
    "deliberate_euler_ancestral_bug": false,
    "prefer_brownian": true,
    "noise_schedule": "karras",
    "image_format": "png",
    "stream": "msgpack"
  }
}
```

关键差异：

- **`params_version`: V5 为 `4`**（V4.5 为 `3`）
- 端点改用 `generate-image-stream`，payload 为 multipart，`request` 部分为 JSON，`stream` 参数为 `msgpack`（网关目前走的 `/ai/generate-image` 旧端点依然可用，但 V5 建议带上 `params_version: 4`）
- V4.5 已有的字段（`v4_prompt`/`v4_negative_prompt`/`characterPrompts` 等）V5 沿用
- 默认 Guidance（scale）变为 7，默认 steps 23，默认噪声调度 `karras`
- 新增 `straight_alpha`（透明背景，对应 UI 的 "Transparent BG" 开关）、`tag_hint_qt`、`tag_hint_uc_preset` 等

## 3. V5 Anlas 计费公式（核心结论）

### 3.1 公式

V4.5 原始公式（version=3）：

```
raw = 2.951823174884865e-6 * r + 5.753298233447344e-7 * r * steps
V4.5 per_sample = ceil(raw)                     # r = width * height，最小 65536
```

**V5 公式 = V4.5 结果再乘 1.5 后向上取整**：

```
V5 per_sample = ceil( ceil(raw) * 1.5 )
```

注意：是**先对 raw 取整，再乘 1.5，再取整**。直接 `ceil(raw * 1.5)` 会算错（如 1024×1024 s29：`raw=20.593`，`ceil(raw)=21`，`ceil(21*1.5)=32` ✓；而 `ceil(20.593*1.5)=31` ✗）。

### 3.2 其余计费项（与 V4.5 相同）

- img2img：`per_sample = max(ceil(per_sample * strength), 2)`
- `uncond_scale != 1.0`：`per_sample = ceil(per_sample * uncond_scale)`
- Opus 免费折扣：`is_opus && steps <= 28 && r <= 1024*1024` 时第一张免费（`total = per_sample * (n_samples - 1)`）
- **Opus 免费边界实测不变**：1024×1024 s28 n1 = 0；1216×832（1011712 px）s28 n1 = 0；1088×960（1044480 px）s28 n1 = 0；1088×1024（1114112 px > 1M）s28 n1 = 33（收费）；s29 起收费
- Vibe Transfer 编码：**2 Anlas/张**（UI 明示 "Encoding required. This will cost 2 Anlas"），超 4 张后每张额外 +2
- Precise Reference：V5 支持（UI 有入口），沿 V4.5 的 5 Anlas/张/样本（未单独实测，上架后需用测试账户核对）
- V5 支持 Reference Images（Vibe Transfer + Precise Reference）、Image2Image、Character Prompts，功能面板与 V4.5 一致

### 3.3 实测数据点（V5 Full / Curated 同价）

| 尺寸 (r=px) | steps | 实测 Anlas | 公式验证 |
|---|---:|---:|---|
| 1024×1024 (1048576) | 1 | 6 | ceil(ceil(3.698)×1.5)=ceil(4×1.5)=6 ✓ |
| 1024×1024 | 2 | 8 | ceil(5×1.5)=8 ✓ |
| 1024×1024 | 10 | 15 | ceil(10×1.5)=15 ✓ |
| 1024×1024 | 28 | 30 | ceil(20×1.5)=30 ✓ |
| 1024×1024 | 29 | 32 | ceil(21×1.5)=32 ✓ |
| 1024×1024 | 30 | 33 | ceil(22×1.5)=33 ✓ |
| 1024×1024 | 35 | 38 | ceil(25×1.5)=38 ✓ |
| 1024×1024 | 40 | 42 | ceil(28×1.5)=42 ✓ |
| 1024×1024 | 45 | 47 | ceil(31×1.5)=47 ✓ |
| 1024×1024 | 50 | 51 | ceil(34×1.5)=51 ✓ |
| 832×1216 (1011712) | 23 | 26 | ceil(ceil(16.374)×1.5)=ceil(17×1.5)=26 ✓ |
| 832×1216 | 28 | 30 | ceil(20×1.5)=30 ✓ |
| 832×1216 | 30 | 32 | ceil(21×1.5)=32 ✓ |
| 1216×832 | 28 | 30 | 同上 ✓ |
| 512×512 (262144) | 1 | 2 | ceil(ceil(0.9246)×1.5)=ceil(1×1.5)=2 ✓ |
| 512×512 | 28 | 8 | ceil(5×1.5)=8 ✓ |
| 512×512 | 29 | 9 | ceil(6×1.5)=9 ✓ |
| 512×512 | 50 | 14 | ceil(9×1.5)=14 ✓ |
| 1536×1024 (1572864) | 1 | 9 | ceil(6×1.5)=9 ✓ |
| 1536×1024 | 28 | 45 | ceil(30×1.5)=45 ✓ |
| 1536×1024 | 29 | 47 | ceil(31×1.5)=47 ✓ |
| 1536×1024 | 50 | 75 | ceil(50×1.5)=75 ✓ |
| 1088×1024 (1114112) | 28 | 33 | ceil(ceil(21.225)×1.5)=ceil(22×1.5)=33 ✓ |
| 1600×1600 (2560000) | 28 | 74 | ceil(ceil(48.79)×1.5)=ceil(49×1.5)=74 ✓ |

多张数验证（1024×1024）：
- s28 n2 = 30（第一张 Opus 免费，只收第 2 张 30）✓
- s28 n3 = 60 ✓；s10 n2/n3/n4 = 15/30/45（每张 15，第一张免费）✓
- s29 n1/n2/n3/n4 = 32/64/96/128（无免费，每张 32）✓

### 3.4 V4.5 对照（价格未变，官方未调整 Legacy 价）

| 尺寸 | steps | V4.5 实测 | V5 实测 | 倍率 |
|---|---:|---:|---:|---:|
| 1024×1024 | 29 | 21 | 32 | 1.524 (=ceil(21×1.5)/21) |
| 1024×1024 | 50 | 34 | 51 | 1.5 |
| 832×1216 | 23 | 17 | 26 | 1.529 |

## 4. 网关侧换算（Anlas → token → RMB）

### 4.1 现行 V4.5 基准（服务器 NewAPI）

- 1 NAI 积分（Anlas）对应返回 **50 token**，50 token = **0.024 RMB**
- 服务器 NewAPI 默认计价单位为 RMB：输入 **100,000 代币/mtoken**，**200 服务器代币 = 1 RMB**
- 即 1 Anlas = 50 token = 0.024 RMB → 1 token = 0.00048 RMB

### 4.2 V5 定价建议（本站销售定价）

上游 V5 成本是 V4.5 的 1.5 倍（`ceil(ceil(raw)*1.5)`）；**本站销售定价按用户要求为 V4.5 × 2**（保留 1.5/2 ≈ 33% 毛利）：

- **V5 非 limit：1 销售 Anlas 对应返回 100 token**（V4.5 的 50 token × 2，即 `_anlas_to_tokens` 直接对 ×2 后的销售 Anlas 换算，20→1000 基准不变）
- base case（1024×1024, s28, n1）：上游 30 Anlas → 销售 40 Anlas = **2000 token** = 0.096 RMB/Anlas 计（按 V4.5 的 0.048 RMB/Anlas × 2）
- 实现：`_calc_anlas_cost(..., price_multiplier=2.0)` 对最终 total（含参考图附加费）整体 ×2，`prompt_tokens = round(anlas/20*1000)` 无需改

**V5 -limit（免费额度路径）：固定 0.07 元/张**（NewAPI 渠道 116 固定 `model_price`，不走 tiered_expr）

### 4.3 V5 免费额度双限额（每日 190 + 每周 1730）

官方 Opus V5 免费额度（UI 实测 2026-08-21）：
- 每周总量 **1730 张**（"99% remaining (~1713 images)"）
- 每日自动补充 **~190 张**（"Currently refills at 11% per day (~190 images)"）

本站限制策略（**双限额，任一触达即拒绝**）：
1. **每日上限 190 张** —— 对齐官方每日补充速率，避免净消耗存量额度；
2. **滚动 7 天窗口上限 1730 张** —— 对齐官方每周总量硬顶。

- 限制对象：所有 V5 系模型（`nai-diffusion-5-*`，含 `-limit` 变体）的生成张数（`n_samples` 累计，含 img2img/inpaint/vibe/character/precise 各端点）
- 实现：网关侧按日计数（UTC+8 自然日，持久化 `logs/v5_daily_usage.json`），请求前预检超限返回 **429**；不依赖 NewAPI 配额
- 日志：每次生成输出 `🎨 V5 生成 +N 张 | 模型 | 今日 x/190 ████░░░░░░ (x%) | 本周 y/1730 ██░░░░░░░░ (y%)` 进度条
- 状态：✅ 已实现（`src/proxy/v5_quota.py` + 7 个端点预检/计数）

## 5. 实施状态（已完成）

1. ✅ `config/models.toml` 新增 6 个 V5 条目：`nai-v5-full` / `nai-v5-curated` / `nai-v5-inpaint`（+ `-limit` 变体，命名沿用现有风格）
2. ✅ `src/proxy/openai.py`：
   - `_calc_anlas_cost` 增加 `price_multiplier`（V5 非 limit = 2.0，作用于最终 total）
   - V5 请求注入 `params_version: 4`（V4/V4.5 保持 3）；`_is_v4_family()` 统一 V4/V4.5/V5 的 v4_prompt / SMEA 关闭 / Accept 逻辑
   - 新增每日 190 / 滚动周 1730 张 V5 双限流（`src/proxy/v5_quota.py`，7 个端点预检 + 计数）
   - Precise Reference / Vibe / Character 端点全部放行 V5
3. ✅ 单测 `tests/test_v5.py`（34 个用例全部通过，含回归）
4. ⏳ 部署到服务器（`git push` + `docker cp src/` + `docker cp config/models.toml` + restart，见 `SERVER_CONNECTION_GUIDE.md`）
5. ⏳ NewAPI 渠道 115 追加 V5 非 limit 模型（同 tiered_expr）；渠道 116 追加 V5 -limit 模型，`model_price = 0.07`（quota 35000）
6. ⏳ 更新 `API_REQUEST_DOC.md`、`TIERED_PRICING.md`、`newapi-pricing.md`、`SERVER_CONNECTION_GUIDE.md`

## 6. 抓取过程备注

- 账户为 Opus（Anlas 余额 10000），Opus 免费额度内的请求 UI 显示 0 Anlas，故用 `steps=29+`、`n>=2` 或超 1M 像素绕过免费额度测单价
- 测试中发现上传 Vibe 参考图后即使缩略图消失，编码费仍计入总价（UI 状态残留），验证公式时已扣除
- V4.5 价格复测确认官方未调整（s29=21、s50=34、832×1216 s23=17），V5 的 1.5 倍关系稳定成立
- Precise Reference 的 V5 单价未实测（需要角色提示词配合），暂沿用 V4.5 的 5 Anlas/张/样本，上架后建议用测试账户对账
