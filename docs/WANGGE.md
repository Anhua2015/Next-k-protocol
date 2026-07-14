# 3xx-wangge（绝对对齐）

源码：`vendor/wangge` ← `E:\OI mode\3xx-wangge-main`（WG-ALL，未改策略/UI）。

**暂未接 Bitget。** 仍为 Decibel / Extended / RISEx + AI 助手原壳。

## 运行方式

同容器：

1. Node：`vendor/wangge` → `127.0.0.1:8080`（`HOST=127.0.0.1`）
2. Protocol uvicorn → 公网 `$PORT`
3. `WanggeProxyMiddleware`：非 `/api/binance*` /docs 的请求原样反代到 wangge

前端：`next-k-frontend/wangge.html` → `resolveProtocolBase()`（iframe 原界面）。

## 变量

| 变量 | 默认 | 说明 |
|------|------|------|
| `WANGGE_ENABLED` | `1` | |
| `WANGGE_PORT` | `8080` | |
| `WANGGE_REQUIRED` | `1` | wangge 起不来则整容器失败 |
| wangge `.env` | 见 `vendor/wangge/.env.example` | AI / 三所密钥；默认 paper |

本地：在 `vendor/wangge` 复制 `.env.example` → `.env` 后 Deploy/启动 Protocol。
