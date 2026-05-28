# Moss Quant → Next-k-protocol 接入设计

日期: 2026-05-28

## 目标

将 Moss Quant 策略的信号接入 Next-k-protocol 实盘执行，替代当前的纯纸面模式。

## 约束

- Moss Quant 不与其他策略（ZCT VWAP / momentum / jiezhen）同时运行
- Protocol URL 复用现有 `PROTOCOL_API_URL`
- 滚仓功能默认开启，通过 protocol 侧开关控制
- 移动止损通过 protocol 动态修改 SL 端点实现

## 架构

```
next-k-api                              Next-k-protocol
paper_scanner ──POST /signals/ingest──→ guards → dispatcher → execute_trade
               ──PUT  /positions/:id/sl─→ 动态修改止损
               ──POST /positions/close──→ 平仓
```

## Protocol 侧改造

### 1. guards.py — 放开 moss_quant source

- VALID_SOURCES 添加 "moss_quant"
- guard_position_exists: moss_quant 豁免同 symbol 检查（允许滚仓加仓）

### 2. trader.py — execute_trade 支持 moss_quant

- source=="moss_quant" 时，直接从 signal["notional_usdt"] 算 qty
- leverage 从 source_config 读

### 3. router.py — 新增动态修改 SL 端点

`PUT /api/binance/positions/{position_id}/sl`

- 校验 position 存在且为 open
- 取消当前 SL 条件单
- 以新 sl_price 重新下 STOP_MARKET 条件单
- 更新 positions 表 sl_price

### 4. repos/config_repo.py — 新增配置项

- moss_quant_rolling_enabled (默认 "true")

## next-k-api 侧改造

### 5. 新建 moss_quant/signal_sender.py

MossQuantSignalSender 类：

- send_open(symbol, side, entry_price, sl_price, tp_price, notional, profile_id, ...)
  → POST PROTOCOL_API_URL/api/binance/signals/ingest
- send_close(symbol, side, exit_rule, close_price, signal_id)
  → POST PROTOCOL_API_URL/api/binance/positions/close
- send_update_sl(position_id, new_sl_price)
  → PUT PROTOCOL_API_URL/api/binance/positions/{id}/sl
- send_rolling(symbol, side, notional, profile_id, ...)
  → 同 send_open，play 标记为 "rolling"

### 6. 改造 paper_scanner.py

实盘模式下：
- OPEN 分支 → signal_sender.send_open()
- CLOSE 分支 → signal_sender.send_close()
- 滚动扫描检测 trailing_stop 触发 → signal_sender.send_update_sl()
- 滚仓触发 → signal_sender.send_rolling()

### 7. moss_quant/config.py — 新增配置项

- MOSS_QUANT_REAL_MODE: bool, 默认 True（实盘模式）
- 信号发送失败不阻塞扫描，仅记录日志

## 信号生命周期

1. 开仓: composite>threshold → send_open → protocol 闸门→去重→MARKET开仓→SL/TP条件单
2. 持仓: 每轮扫描检测 trailing → send_update_sl → 取消旧SL→下新SL
3. 平仓: SL/TP触发 或 signal_reverse → send_close → 取消SL/TP→MARKET平仓
4. 滚仓: 浮盈>trigger_pct → send_rolling → MARKET加仓→更新SL/TP
