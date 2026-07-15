# Bitget 多标的网格（wangge）

仅支持 **Bitget** USDT 永续：一条连接、共享账户余额，`BG_SYMBOLS` 一标的一 GridBot。

配置与运行说明见仓库 [`docs/WANGGE.md`](../../docs/WANGGE.md) 与 [`.env.example`](.env.example)。

```bash
cp .env.example .env
npm install
npm start
```

默认 `BG_MODE=paper`。实盘需 `BG_MODE=live` 并填写 `BITGET_API_KEY` / `SECRET` / `PASSPHRASE`。
