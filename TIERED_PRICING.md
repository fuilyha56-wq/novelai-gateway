# NovelAI Gateway 动态计费配置

> 本文只描述当前代码实际返回的计费数据。
>
> **2026-08-30（第二次调整）**：6 个完整版 V4.5/V5 模型改为**两档计费**——请求落在 Opus 免费额度档内按档内价（即原 `-limit` 价），超出档按完整版原价。`-limit` 模型保留不变。
> **2026-08-30（第一次调整）**：全部 NAI 模型改按次计费（ModelPrice × 分组倍率），废弃 `tier("base", p * 4800)` 旧表达式。
> 旧配置备份在 new-api 数据库 `options_backup_20260830` 表。

## 1. 计费数据来源

网关在成功的 OpenAI JSON 图片响应中返回：

```json
{
  "usage": {
    "prompt_tokens": 8,
    "completion_tokens": 0,
    "total_tokens": 8
  }
}
```

- **完整版 V4.5/V5 模型（两档计费）**：`prompt_tokens` = 档位价格（与 ModelPrice 同单位），由网关按 Opus 免费额度边界判档后直接写入（`src/proxy/openai.py` 的 `_billing_prompt_tokens`）。NewAPI 表达式 `tier("limit", p * 1000000)` 把该值 1:1 映射为按次扣费。
- **`-limit` 模型**：按次 ModelPrice，不读 usage；usage 按 Anlas 换算仅供观测（`prompt_tokens = max(1, round(Anlas/20*1000))`，V5 销售价 ×2）。
- 其他模型（V3/V4 等）：沿用 Anlas 换算，仅供观测。

## 2. NewAPI 配置

### 2.1 完整版模型两档计费（tiered_expr）

options 表两个键：

- `billing_setting.billing_mode`：6 个完整版模型 → `"tiered_expr"`
- `billing_setting.billing_expr`：6 个完整版模型 → `tier("limit", p * 1000000)`

换算：`quota = p × 1000000 / 1,000,000 × QuotaPerUnit × 分组倍率 = p × QuotaPerUnit × 分组倍率`，即 p 就是按次价格，与 ModelPrice 计费完全同构。

档位价格（网关 `openai.py` 的 `_BILLING_UNITS_V45` / `_BILLING_UNITS_V5` 常量，调价须同步修改）：

| 模型 | 档内（Opus 免费额度内） | 档外 |
|---|---:|---:|
| `nai-v4.5-full` / `nai-v4.5-curated` / `nai-v4.5-inpaint` | 0（免费） | 200 |
| `nai-v5-full` / `nai-v5-curated` / `nai-v5-inpaint` | 8 | 520 |

### 2.2 `-limit` 模型（按次，未变）

| 模型 | 按次价格 |
|---|---:|
| `nai-v4.5-*-limit` | $0（免费） |
| `nai-v5-*-limit` | $8 |

## 3. 档内 / 档外判定（Opus 免费额度边界）

与 `-limit` 模型的硬限制同一套边界（网关 `_in_opus_free_envelope`），但**不拒绝请求，只影响价格**。档内需同时满足：

- n_samples（n）= 1
- steps ≤ 28
- width×height ≤ 1024×1024（1048576 像素）
- service_tier ≠ priority
- 无 Precise Reference（`references`，每样本必扣 5 Anlas）
- 参考图（`reference_image(s)` 及 `characters[].reference_image`）全部为**已编码 vibe**（原始图片要先编码、必扣费 → 档外）
- action ∈ generate / img2img / infill；generate 不得携带 image（img2img / infill 携带 image 不影响）
- upscale / Director 等工具端点一律档外

任一不满足 → 档外：请求照常执行并出图，按完整版原价计费。

## 4. `-limit` 模型（保留）

`-limit` 模型行为完全不变：档外请求直接 400 拒绝，价格走按次 ModelPrice。网关仍限制张数、步数、画面面积、参考图和 priority 等参数。该限制只能降低意外消耗风险，不能替代对 NovelAI 实际余额和上游规则变化的监控。V5 全部模型（含 `-limit`）还受网关侧每日/滚动周双限额约束（限额值以服务器 `.env` 的 `V5_DAILY_LIMIT` / `V5_WEEKLY_LIMIT` 为准）。

两档计费上线后，完整版档内价与 `-limit` 价相同（V4.5 免费 / V5 $8），两者区别只剩：**完整版超界照常出图按原价计费，`-limit` 超界直接拒绝**。

## 5. 非标准图片操作

通过 `/v1/images/generations` 加 `novelai_operation` 调用的 Director 等操作，在两档计费下按完整版**档外价**计费（与按次计费时代的实收一致）。`upscale` 与 `annotate` 尚无经过验证的动态成本映射，因此统一入口会拒绝这两种操作；需要使用专用 Gateway 端点并在下游单独定价。

## 6. 验证

每次修改计费配置后，至少验证以下请求并检查响应 `usage` 与 NewAPI 账单：

1. 完整版 V4.5 档内（1024²、28 steps、1 张）→ 账单 0；完整版 V5 档内同参数 → 账单 8。
2. 完整版 V5 档外：steps=50 / 1536×1024 / n=2 / priority 各一发 → 各 520。
3. `-limit` 回归：V4.5 `-limit` 档内 → 0；V5 `-limit` 档内 → 8；V5 `-limit` steps=50 → 400 拒绝。
4. 一个已编码 vibe 参考图请求 → 档内价；一个原始图片参考图请求 → 档外价。
5. 一个 `novelai_operation` Director 请求 → 档外价。

不要以本文示例代替真实上游扣费审计。NovelAI 可能调整私有接口和计费规则；生产环境应定期用测试账户对比请求前后的实际 Anlas 余额。
