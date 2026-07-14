# 3xx-wangge（绝对对齐 + Bitget）

源码：`vendor/wangge` ← `E:\OI mode\3xx-wangge-main`（WG-ALL 原壳）+ Bitget（`bg`）第四所接入。

交易所：Decibel / Extended / RISEx / **Bitget**（USDT 永续）+ AI 助手。

## 运行方式

同容器：

1. Node：`vendor/wangge` → `127.0.0.1:8080`（`HOST=127.0.0.1`）
2. Protocol uvicorn → 公网 `$PORT`
3. `WanggeProxyMiddleware`：非 `/api/binance*` /docs 的请求原样反代到 wangge

前端：`next-k-frontend/wangge.html` → `resolveProtocolBase()`（iframe 原界面）。

## Bitget

| 项 | 说明 |
|----|------|
| 短键 | `bg` → `/api/bg/*` |
| 默认 | `BG_MODE=paper`（公开行情 + 模拟成交） |
| 实盘 | `BG_MODE=live` + `BITGET_API_KEY` / `BITGET_API_SECRET` / `BITGET_PASSPHRASE` |
| 产品 | 默认 `USDT-FUTURES`，全仓 `crossed` |
| 代理 | `BITGET_PROXY` 或 `GLOBAL_PROXY` |

见 `vendor/wangge/.env.example`。

## 变量

| 变量 | 默认 | 说明 |
|------|------|------|
| `WANGGE_ENABLED` | `1` | |
| `WANGGE_PORT` | `8080` | |
| `WANGGE_REQUIRED` | `1` | wangge 起不来则整容器失败 |
| wangge `.env` | 见 `vendor/wangge/.env.example` | AI / 四所密钥；默认 paper |

本地：在 `vendor/wangge` 复制 `.env.example` → `.env` 后 Deploy/启动 Protocol。
