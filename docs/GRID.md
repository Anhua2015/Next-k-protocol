# Bitget 网格（本仓库）

源码：`vendor/bitget-grid`  
入口：同容器 `/grid-bot` → Node `:8080`  
前端：`next-k-frontend/fleet.html` 直连本服务（**不经 next-k-api**）

## Railway

`Dockerfile` + `scripts/start_railway.sh`：先起 Worker，再起 Protocol FastAPI。

| 变量 | 说明 |
|------|------|
| `BITGET_API_KEY` / `SECRET` / `PASSPHRASE` | 缺则跳过 Worker |
| `GRID_WORKER_ENABLED=1` | |
| `BITGET_GRID_PORT=8080` | |
| `GRID_AUTH_TOKEN` | 可选看板密码 |
| `FLEET_AUTOSTART=0` | |

探测：`GET /grid-bot/_status` · `GET /grid-bot/api/health`

```powershell
cd vendor\bitget-grid
npm run selfcheck
```
