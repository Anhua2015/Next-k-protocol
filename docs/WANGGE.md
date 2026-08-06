# Next K 网格（Bitget）

源码目录名仍为 `vendor/wangge`（部署路径未改）；产品面统称 **Next K**。只保留 **Bitget** USDT 永续。

## 模型

| 项 | 说明 |
|----|------|
| 交易所 | 仅 Bitget（共享账户） |
| 选币官 | 仅白名单：核心 BTC/ETH/SOL（无次核心）；默认中性，仅极强趋势（≥75%）才做多/做空；最多 3 机 |
| 托管 | 自动调区间；换向门槛更高（约 55%）；强震荡可切回中性 |
| 模式 | `BG_MODE=paper` / `live` |

## 无干预

`BG_SYMBOLS` 建议留空，由选币官在**流动性白名单**内维护名册（最多 3：BTC/ETH/SOL）。进程约 20s 首次巡检：空仓时一次可开满高分标的；已有运行中后再开需要连续确认。之后默认每 **2 小时**巡检，**至少持有 6 小时**才可淘汰/替换。名单外旧仓会累计剔除分。

## 持久化（部署不丢）

网格快照与标的名单默认写在 `DATA_DIR`（或 `WANGGE_DATA_DIR`）：

| 文件 | 内容 |
|------|------|
| `$DATA_DIR/wangge.state.json` | 各标的运行快照 / 累计统计 |
| `$DATA_DIR/wangge_symbols.txt` | UI/选币官维护的标的列表 |
| `$DATA_DIR/wangge_paper.json` | Paper 账本（余额 / 仓位 / 挂单） |

Railway 必须挂 Volume 到 `/data`，并设 `DATA_DIR=/data`。否则每次 redeploy 容器盘清空，看起来就像「一部署就重置」。旧版写在 `vendor/wangge/.state.json` / `.env` 的文件会在启动时尽量迁移/忽略。

详见 `.env.example`。
