# Bitget 多标的网格（wangge）

源码：`vendor/wangge`。只保留 **Bitget** USDT 永续。

## 模型

| 项 | 说明 |
|----|------|
| 交易所 | 仅 Bitget（一条连接，**账户余额共享**） |
| 机器人 | `BG_SYMBOLS=BTCUSDT,ETHUSDT,...`，一标的一个 `GridBot` |
| 默认 | `BG_MODE=paper` |
| 实盘 | `BG_MODE=live` + API Key / Secret / Passphrase |
| 持久化 | `.state.json` 键 `sym_BTCUSDT` 等；旧键 `bg` 按配置标的迁移后删除 |
| 盈亏 | 各标的用该网格 `gridProfit` + 本市场浮动；总览可求和格盈亏；余额/权益取共享账户一次 |

不做假独立余额：保证金预检用真实账户权益；多标的会竞争同一保证金池。

## API

- `GET /api/overview` — 各标的汇总（共享账户）
- `GET/POST/DELETE /api/symbols` — 标的增删
- `/api/s/:SYM/start|stop|state|stream|...` — 单标的控制
- `POST /api/exchange/reconnect` — 重连 Bitget
- `/api/bg/*` — 兼容旧路径，优先映射到 `BTCUSDT`

## 运行

wangge Node → `127.0.0.1:8080`，Protocol 反代。前端 `wangge.html` iframe。

详见 `vendor/wangge/.env.example`。
