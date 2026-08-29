# NovelAI Gateway 动态计费配置

> 本文只描述当前代码实际返回的计费数据。旧版“固定 1 token/次 + 请求参数倍率”方案已经废弃，不应再使用。
>
> **2026-08-30 起 NewAPI 侧计费方式已变更**：全部 NAI 模型改为**按次计费（ModelPrice × 分组倍率）**，
> 不再使用 tiered_expr 按 usage token 计费。网关返回的 usage 仍保留（仅供观测），不参与计费。
> 旧配置备份在 new-api 数据库 `options_backup_20260830` 表。

## 1. 计费数据来源

网关在成功的 OpenAI JSON 图片响应中返回：

```json
{
  "usage": {
    "prompt_tokens": 1000,
    "completion_tokens": 0,
    "total_tokens": 1000
  }
}
```

`prompt_tokens` 由网关先估算本次 NovelAI Anlas，再按下式换算：

```text
prompt_tokens = max(1, round(Anlas / 20 * 1000))
```

V4/V4.5 的 Anlas 即上游实际消耗；**V5 非 `-limit` 模型按销售定价 = V4.5 × 2**，网关在估算时已对最终 Anlas（含参考图附加费）整体乘 2，再套用上式换算 token（如 V5 base case = 40 销售 Anlas → 2000 token）。V5 `-limit` 模型走固定价（见第 3 节），不参与动态换算。

因此 ~~NewAPI 必须按照响应中的 usage 动态计费~~（**2026-08-30 起已改为按次计费，usage 不再参与计费**），不能依据 `size`、`steps`、`n` 等原始请求字段重复乘倍率，否则会二次加价。

## 2. NewAPI 配置（按次计费，2026-08-30 起）

全部 NAI 模型在 new-api 的 `ModelPrice`（按次，美元）中定价，计费 = ModelPrice × 分组倍率（GroupRatio）：

| 模型 | 按次价格 |
|---|---:|
| `nai-v4.5-full` / `nai-v4.5-curated` / `nai-v4.5-inpaint` | $200 |
| `nai-v5-full` / `nai-v5-curated` / `nai-v5-inpaint` | $520 |
| `nai-v4.5-*-limit` | $0（免费） |
| `nai-v5-*-limit` | $8 |

价格锚点：V4.5 基准（832×1216 或 1024²、28 步、1 张）改前实扣 $200；V5 基准改前实扣 $520（上游销售 Anlas ×2 后按旧系数换算）。
**注意与旧动态计费的差异**：小尺寸不再便宜（512² 由 V4.5 $50 / V5 $130 统一变为 $200 / $520），
大尺寸不再加价（V5 1536×1024 由 $780 变为 $520）。`tiered_expr` 中 nai 条目已全部移除（GLM/Kimi 不受影响）。

调价只需在 new-api 控制台「模型价格」里改对应条目，或改 `options` 表 `ModelPrice` 后重启 new-api。

### 历史方案（已被按次计费取代）

网关仍按下列公式在响应 usage 中返回 token（仅供观测，不参与计费）：

```text
prompt_tokens = max(1, round(Anlas / 20 * 1000))
```

V4/V4.5 的 Anlas 即上游实际消耗；V5 非 `-limit` 模型按销售定价 = V4.5 × 2，网关在估算时已对最终 Anlas（含参考图附加费）整体乘 2，再套用上式换算 token（如 V5 base case = 40 销售 Anlas → 2000 token）。

## 3. `-limit` 模型

`-limit` 是 Opus 免费额度保护别名，不使用动态 Anlas 销售价，走固定按次价格（ModelPrice × 分组倍率）。

- **V4.5 `-limit`：$0/张**（免费）
- **V5 `-limit`：$8/次**（当前 new-api `ModelPrice` 实配值；早期文档写的 0.07 元/张已不适用）

网关会限制张数、步数、画面面积、参考图和 priority 等参数。该限制只能降低意外消耗风险，不能替代对 NovelAI 实际余额和上游规则变化的监控。V5 全部模型（含 `-limit`）还受网关侧每日/滚动周双限额约束（限额值以服务器 `.env` 的 `V5_DAILY_LIMIT` / `V5_WEEKLY_LIMIT` 为准）。

## 4. 非标准图片操作

通过 `/v1/images/generations` 加 `novelai_operation` 调用的 Director 等操作，会由网关在响应 `usage` 中写入对应成本。`upscale` 与 `annotate` 尚无经过验证的动态成本映射，因此统一入口会拒绝这两种操作；需要使用专用 Gateway 端点并在下游单独定价。

## 5. 验证

每次修改 NewAPI 定价后，至少验证以下请求并检查响应 `usage` 与 NewAPI 账单：

1. 512×512、28 steps、1 张。
2. 1024×1024、50 steps、1 张。
3. 1024×1024、28 steps、2 张，并确认响应 `data` 中确实有两张图。
4. 一个 Precise Reference 或 Vibe Transfer 请求。

不要以本文示例代替真实上游扣费审计。NovelAI 可能调整私有接口和计费规则；生产环境应定期用测试账户对比请求前后的实际 Anlas 余额。
