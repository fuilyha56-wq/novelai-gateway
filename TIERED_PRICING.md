# NovelAI Gateway 动态计费配置

> 本文只描述当前代码实际返回的计费数据。
>
> **2026-08-30（最终版）**：6 个完整版 V4.5/V5 模型改为**两档计费**——请求落在 Opus 免费额度档内按**固定档内价**（即原 `-limit` 价：V4.5 免费 / V5 $8）；超出档按**原动态口径**按 token 计费（V5 = p × 130000 / V4.5 = p × 100000，p 为网关 Anlas 换算的 usage tokens）。`-limit` 模型保留不变。
> **2026-08-30（中途方案，已废弃）**：全部 NAI 模型改按次计费（ModelPrice × 分组倍率）、以及"档外一口价 200/520"的两档变体——均不再使用。
> 旧配置备份在 new-api 数据库 `options_backup_20260830`、`options_backup_two_tier_20260830` 表。

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

- **完整版 V4.5/V5 模型，档内**：`prompt_tokens` = 固定档内价（V4.5 = 0，V5 = 8），由网关判档写入（`_billing_prompt_tokens`）。NewAPI 表达式的 `p < 100` 分支把它 1:1 落账。
- **完整版 V4.5/V5 模型，档外**：网关不改写 usage，返回按 Anlas 换算的动态 token（`prompt_tokens = max(1, round(Anlas/20*1000))`，V5 销售价 ×2），NewAPI 按 `p × 130000`（V5）/ `p × 100000`（V4.5）动态计费。
- **`-limit` 模型**：按次 ModelPrice（V4.5 免费 / V5 $8），不读 usage；usage 同动态口径仅供观测。

## 2. NewAPI 配置（tiered_expr）

options 表 `billing_setting.billing_mode`：6 个完整版模型 → `"tiered_expr"`；
`billing_setting.billing_expr`：

```text
V5 三兄弟:   p < 100 ? tier("limit", p * 1000000) : tier("full", p * 130000)
V4.5 三兄弟: p < 100 ? tier("limit", p * 0)        : tier("full", p * 100000)
```

- `p < 100` 分支 = 档内固定价（档内 usage 只有 0/1[钳位]/8，动态档 usage 恒 ≥ 250，边界安全）；
- 档外分支 = 原动态口径：base case V4.5（20 Anlas → 1000 token）= 100 单位；V5（40 销售 Anlas → 2000 token）= 260 单位。
- **为什么分支必须返回 `p * 0` / `p * 1000000`（经 tier() 包装的浮点表达式）而不是整数字面量 `0`**：表达式引擎要求结果为 float64，三元 else 分支写整数字面量会在运行时返回 int，类型断言失败 → 结算报错 → 回退预扣额度，导致错误扣费。
- 档内价调整：改网关 `_BILLING_LIMIT_UNITS_*` 常量 + NewAPI 表达式 limit 分支；动态价调整：改 NewAPI 表达式 full 分支系数。

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

任一不满足 → 档外：请求照常执行并出图，按动态口径计费。

## 4. `-limit` 模型（保留）

`-limit` 模型行为完全不变：档外请求直接 400 拒绝，价格走按次 ModelPrice。V5 全部模型（含 `-limit`）受网关侧每日/滚动周双限额约束（限额值以服务器 `.env` 的 `V5_DAILY_LIMIT` / `V5_WEEKLY_LIMIT` 为准）。

两档计费上线后，完整版档内价与 `-limit` 价相同（V4.5 免费 / V5 $8），区别只剩：**完整版超界照常出图按动态口径计费，`-limit` 超界直接拒绝**。

## 5. 非标准图片操作

通过 `/v1/images/generations` 加 `novelai_operation` 调用的 Director 等操作属于档外，按动态口径计费（与按 token 计费时代的实收一致）。`upscale` 与 `annotate` 尚无经过验证的动态成本映射，因此统一入口会拒绝这两种操作；需要使用专用 Gateway 端点并在下游单独定价。

## 6. 验证

每次修改计费配置后，至少验证以下请求并检查响应 `usage` 与 NewAPI 账单：

1. 完整版 V4.5 档内（1024²、28 steps、1 张）→ usage 0、账单 0；完整版 V5 档内同参数 → usage 8、账单 8。
2. 完整版 V5 档外（如 steps=30、832×1216）→ usage = Anlas 换算 token（销售价 ×2），账单 = token × 130000 / 1e6 单位。
3. `-limit` 回归：V4.5 `-limit` 档内 → 0；V5 `-limit` 档内 → 8；V5 `-limit` steps=50 → 400 拒绝。
4. 一个已编码 vibe 参考图请求 → 档内价；一个原始图片参考图请求 → 动态口径。
5. 一个 `novelai_operation` Director 请求 → 动态口径（如 declutter 5 Anlas → 250 token）。

**已知坑（部署/排障必读）**：

1. `X-Steps` 等 `X-*` 自定义请求头**过不了 new-api**（只透传 body），经 new-api 调用时要传 steps/尺寸请用 body 里的 `extra_params` 对象或顶层 `size`。
2. new-api 图片链路会把 `usage.prompt_tokens == 0` 钳位成 1（`relay/image_handler.go`），发生在结算**之前**——这就是 V4.5 档内表达式必须用 `p * 0`（0/1 都判免费）而不是 `p == 0` 的原因。
3. 不要以本文示例代替真实上游扣费审计。NovelAI 可能调整私有接口和计费规则；生产环境应定期用测试账户对比请求前后的实际 Anlas 余额。
