# Agent 趋势闭环（bitget-fleet-grid-agent 规则）

黑客松仓库是 **规则 Agent**（EMA/ADX/RSI + funding），不是 LLM。本服务已接入同一决策层，驱动实盘 fleet。

```
Bitget 4H K + funding
        ↓
  agent-trend.js（RANGE/BULL/BEAR/UNCLEAR）
        ↓
  fleet mode: neutral | long | short | flat
        ↓
  挂单 / 空仓 / 定期 restart
```

## 环境变量

| 变量 | 默认 | 说明 |
|------|------|------|
| `GRID_AGENT_TREND` | `1` | `0` 关闭，回退旧 EMA 趋势 |
| `GRID_AGENT_INTERVAL_MS` | `14400000`（4h） | 闭环重判间隔 |

## API

- `GET /grid-bot/api/agent/status`
- `POST /grid-bot/api/agent/decide` body `{ "apply": true }` 立刻重配
- `GET /grid-bot/api/trend?marketId=1`

## MCP（可选）

原工程 Cursor MCP 仅方便人机对话拉 K 线；实盘闭环用公开 REST，**不依赖** MCP 进程。
见上游 [bitget-fleet-grid-agent](https://github.com/beibei030/bitget-fleet-grid-agent)。
