# Next K 网格（Bitget）

产品名 **Next K**；本目录为 Bitget USDT 永续多标的网格实现：一条连接、共享账户余额，一标的一 GridBot。

配置与运行说明见仓库 [`docs/WANGGE.md`](../../docs/WANGGE.md) 与 [`.env.example`](.env.example)。

```bash
cp .env.example .env
npm install
npm start
```

默认 `BG_MODE=paper`。实盘需 `BG_MODE=live` 并填写 `BITGET_API_KEY` / `SECRET` / `PASSPHRASE`。
