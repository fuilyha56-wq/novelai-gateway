# NovelAI Gateway 动态计费配置

> 本文只描述当前代码实际返回的计费数据。旧版“固定 1 token/次 + 请求参数倍率”方案已经废弃，不应再使用。

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

因此 NewAPI 必须按照响应中的 usage 动态计费，不能再依据 `size`、`steps`、`n` 等原始请求字段重复乘倍率，否则会二次加价。

## 2. NewAPI 配置

对普通（非 `-limit`）图片模型使用 `tiered_expr`，表达式只处理响应中的 token：

```text
tier("base", p * 4800)
```

这里的 `4800` 是现有 NewAPI 配置约定的 token 单价系数。若站点希望采用不同的销售价格，只调整这个系数；不要再添加尺寸、步数、图片数量或参考图条件倍率。

网关当前估算示例：

| 请求 | 估算 Anlas | prompt_tokens |
|---|---:|---:|
| 512×512、28 steps、1 张 | 5 | 250 |
| 832×1216、28 steps、1 张 | 20 | 1000 |
| 1024×1024、28 steps、1 张 | 20 | 1000 |
| 1536×1024、28 steps、1 张 | 30 | 1500 |
| 1024×1024、50 steps、1 张 | 34 | 1700 |
| 1024×1024、28 steps、2 张 | 40 | 2000 |
| **V5** 1024×1024、28 steps、1 张 | 40（销售价） | 2000 |
| **V5** 1536×1024、28 steps、1 张 | 60（销售价） | 3000 |
| **V5** 1024×1024、50 steps、1 张 | 68（销售价） | 3400 |

尺寸、步数、样本数、图生图强度和参考图功能均可能改变 Anlas 估算。Precise Reference、Vibe 编码以及部分 Director 工具还包含额外费用。V5 的销售 Anlas 已含 ×2 倍率，表中的 **V5** 行即实际返回的 prompt_tokens。

## 3. `-limit` 模型

`-limit` 是 Opus 免费额度保护别名，不使用上述动态 Anlas 销售价。它只应被分配给明确配置好的固定价格模型。

- **V4.5 `-limit`：0.005 元/张**（固定）
- **V5 `-limit`：0.07 元/张**（固定，NewAPI 渠道 `model_price = 0.07`）

网关会限制张数、步数、画面面积、参考图和 priority 等参数。该限制只能降低意外消耗风险，不能替代对 NovelAI 实际余额和上游规则变化的监控。V5 全部模型（含 `-limit`）还受网关侧每日 190 / 滚动周 1730 张双限额约束（见 `NAI_V5_CRAWL_REPORT.md` 第 4.3 节）。

## 4. 非标准图片操作

通过 `/v1/images/generations` 加 `novelai_operation` 调用的 Director 等操作，会由网关在响应 `usage` 中写入对应成本。`upscale` 与 `annotate` 尚无经过验证的动态成本映射，因此统一入口会拒绝这两种操作；需要使用专用 Gateway 端点并在下游单独定价。

## 5. 验证

每次修改 NewAPI 定价后，至少验证以下请求并检查响应 `usage` 与 NewAPI 账单：

1. 512×512、28 steps、1 张。
2. 1024×1024、50 steps、1 张。
3. 1024×1024、28 steps、2 张，并确认响应 `data` 中确实有两张图。
4. 一个 Precise Reference 或 Vibe Transfer 请求。

不要以本文示例代替真实上游扣费审计。NovelAI 可能调整私有接口和计费规则；生产环境应定期用测试账户对比请求前后的实际 Anlas 余额。
