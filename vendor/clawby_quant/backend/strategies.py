"""Active strategies: S02 / S06 / S11 / S14 (trimmed 2026-07-18 per backtest; the removed ones live in backup/2026-07-18-pre-trim/).

Each strategy implements evaluate(ctx) -> [Signal]; optionally check_exit(ctx,
position) -> reason|None for strategy-specific exits. All entries/holds respect
the <=24h intraday constraint via max_hold_sec.
"""
import logging
import statistics
import time
from dataclasses import dataclass, field

from . import binance, db, factors

log = logging.getLogger("strategies")


@dataclass
class Signal:
    strategy: str
    symbol: str
    side: str                 # long / short
    reason: str
    stop_price: float = 0.0
    take_profit: float = 0.0
    trail_dist: float = 0.0   # absolute price distance for chandelier trailing
    max_hold_sec: int = 86400
    time_exit_at: int = 0
    size_mult: float = 1.0


def _z(series, value=None, window=None):
    s = list(series)[-(window or len(series)):]
    if len(s) < 8:
        return 0.0
    mean, stdev = statistics.fmean(s), statistics.pstdev(s)
    if stdev == 0:
        return 0.0
    return ((value if value is not None else s[-1]) - mean) / stdev


class Ctx:
    """Per-tick context handed to strategies: factors, klines/ATR cache, positions."""

    def __init__(self, universe):
        self.universe = universe
        self._kcache = {}
        self._atr = {}

    def factor(self, name, symbol=""):
        return db.get_factor(name, symbol)

    async def klines(self, symbol, interval="1h", limit=100):
        key = (symbol, interval, limit)
        if key not in self._kcache:
            self._kcache[key] = await factors.get_klines(symbol, interval, limit)
        return self._kcache[key]

    async def atr(self, symbol):
        if symbol not in self._atr:
            ks = await self.klines(symbol, "1h", 60)
            self._atr[symbol] = factors.atr_from_klines(ks) or 0.0
        return self._atr[symbol]

    async def depth_ratio(self, symbol):
        """sum(bid qty)/sum(ask qty) top-50 levels."""
        try:
            d = await binance.depth(symbol, 50)
            bids = sum(float(q) for _, q in d.get("bids", []))
            asks = sum(float(q) for _, q in d.get("asks", []))
            return bids / asks if asks else 1.0
        except Exception:  # noqa: BLE001
            return 1.0

    def price(self, symbol):
        return factors.mark_price_of(symbol)

    def has_position(self, symbol, strategy=None):
        return bool(db.open_positions(strategy=strategy, symbol=symbol))


class Base:
    sid = "BASE"
    META = {}
    FACTORS = []
    DEFAULT_INTERVAL = "15m"   # design scan cadence; live cadence comes from yaml

    def __init__(self, cfg):
        self.cfg = cfg
        # instance identity: multiple yaml instances may share one template
        # class; positions/signals/cooldowns are attributed to the instance id
        self.sid = cfg.get("_iid") or type(self).sid
        self.p = cfg.get("params", {})
        self.symbols = [s.upper() for s in (cfg.get("symbols") or [])]
        self.risk = cfg.get("risk", {})
        self.max_hold = int(float(cfg.get("max_hold_hours", 24)) * 3600)

    def syms(self, ctx):
        """Per-strategy symbol list, or the global monitored universe if unset."""
        return self.symbols or ctx.universe

    async def evaluate(self, ctx) -> list:
        return []

    async def check_exit(self, ctx, pos):
        return None


def strategy_meta():
    """sid -> META for the dashboard strategy panel."""
    return {sid: cls.META for sid, cls in REGISTRY.items()}


# ── S02 爆仓瀑布接针 ────────────────────────────────────────────────────────

class S02LiqRebound(Base):
    sid = "S02_LIQ_REBOUND"
    FACTORS = ["liq_agg", "liq_orders"]   # 依赖因子(策略页展示/启用校验)
    DEFAULT_INTERVAL = "5m"   # design scan cadence (backtests use this)
    META = {
        "name": "爆仓瀑布接针", "type": "均值回归", "direction": "只多",
        "logic": "密集多头爆仓是清算引擎的被迫卖出而非新增看空共识,爆仓峰值后价格常超调,瀑布衰减确认后快速回填。",
        "entry": "1h多头爆仓 ≥ 7日均值6倍 + 30分钟急跌 + 爆仓单流10分钟内衰减≥70% + 买盘深度回补(bid/ask≥1.1) → 市价开多",
        "exit": "反弹至跌幅0.5回撤位止盈;新低0.6×ATR止损;最长持仓 4h",
        "factors": "liq_agg · liq_orders(OKX/Bitget补齐) · 5m K线 · 订单簿深度 · ATR（其余币安内置）",
        "risk": "接飞刀策略——瀑布仍在放大时绝不入场;交易所级黑天鹅直接熔断不接",
        "name_en": "Liq-Cascade Rebound",
        "logic_en": "Dense long liquidations are forced selling by the liquidation engine, not fresh bearish consensus; price overshoots at the cascade peak and refills fast once the cascade decays.",
        "entry_en": "1h long liqs >= 6x 7d avg + 30m sharp drop + liq order flow decaying >=70% within 10m + bid depth refilled (bid/ask >= 1.1) -> market long",
        "exit_en": "TP at 0.5 retrace of the drop; SL 0.6xATR under the low; max hold 4h",
        "factors_en": "liq_agg / liq_orders (OKX/Bitget) / 5m klines / depth / ATR (Binance otherwise)",
        "risk_en": "Knife-catching: never enter while the cascade is still expanding; exchange-level black swans are halted, not caught",
    }

    async def evaluate(self, ctx):
        out = []
        for sym in self.syms(ctx):
            if ctx.has_position(sym):
                continue
            liq = ctx.factor("liq_agg", sym)
            orders = ctx.factor("liq_orders", sym)
            if not liq or liq.get("long_mult", 0) < self.p["liq_spike_mult"]:
                continue
            ks = await ctx.klines(sym, "5m", 12)
            atr = await ctx.atr(sym)
            price = ctx.price(sym)
            if len(ks) < 7 or not atr or not price:
                continue
            drop = ks[-7]["close"] - price          # ~30m move
            if drop < self.p["drop_atr"] * atr / 2:  # 5m-scale: half the 1h-ATR bar
                continue
            if orders and orders["n_prev_20m"] > 0:
                decay = 1 - orders["n_10m"] / max(orders["n_prev_20m"] / 2, 1)
                if decay * 100 < self.p["decay_pct"]:
                    continue  # cascade still accelerating — don't catch yet
            if await ctx.depth_ratio(sym) < self.p["bid_ask_min"]:
                continue
            out.append(Signal(self.sid, sym, "long",
                              f"多头爆仓{liq['long_mult']:.1f}x均值+瀑布衰减+买盘回补",
                              stop_price=price - self.p["stop_atr"] * atr,
                              take_profit=price + drop * self.p["tp_retrace"],
                              max_hold_sec=self.max_hold))
        return out


# ── S06 CVD 背离反转 ───────────────────────────────────────────────────────

class S06CvdFade(Base):
    sid = "S06_CVD_FADE"
    FACTORS = ["cvd", "ob_wall"]   # 依赖因子(策略页展示/启用校验)
    DEFAULT_INTERVAL = "15m"   # design scan cadence (backtests use this)
    META = {
        "name": "CVD背离反转", "type": "微观结构反转", "direction": "双向",
        "logic": "价格靠稀薄挂单打出新高但主动买量(CVD)不创新高 = 推手是挂单撤退而非真实买盘,背离后大概率回摆。",
        "entry": "价格创4h新高但CVD落后前高≥15% + 卖压比≥1.3 + 上方1%内有大额卖墙 → 开空(镜像开多);仓位减半",
        "exit": "回归1h VWAP止盈;卖墙被吃掉止损(逻辑失效);最长持仓 6h",
        "factors": "cvd · ob_wall · 订单簿深度 · 1h K线（均为币安内置）",
        "risk": "背离可以持续背离——单笔风险减半,靠高胜率小止损盈利",
        "name_en": "CVD Divergence Fade",
        "logic_en": "New price highs on thin books without new CVD highs mean the move is ask-side retreat, not real buying; the divergence usually snaps back.",
        "entry_en": "Price makes a 4h high but CVD lags >=15% + sell pressure >=1.3 + a large ask wall within 1% above -> short (mirrored long); half size",
        "exit_en": "TP at 1h VWAP; SL when the wall gets eaten (thesis invalid); max hold 6h",
        "factors_en": "cvd / ob_wall / orderbook depth / 1h klines (Binance-native)",
        "risk_en": "Divergence can keep diverging: half size per trade, profit via high win-rate small stops",
    }

    async def evaluate(self, ctx):
        out = []
        for sym in self.syms(ctx):
            if ctx.has_position(sym):
                continue
            cvd = ctx.factor("cvd", sym)
            wall = ctx.factor("ob_wall", sym)
            if not cvd or len(cvd.get("series", [])) < 10:
                continue
            ks = await ctx.klines(sym, "1h", 10)
            atr = await ctx.atr(sym)
            price = ctx.price(sym)
            if len(ks) < 5 or not atr or not price:
                continue
            price_hh = price >= max(k["high"] for k in ks[:-1])
            price_ll = price <= min(k["low"] for k in ks[:-1])
            s = cvd["series"]
            rng = (max(s) - min(s)) or 1
            gap_hh = (max(s[:-1]) - s[-1]) / rng * 100
            gap_ll = (s[-1] - min(s[:-1])) / rng * 100
            depth = await ctx.depth_ratio(sym)
            if price_hh and gap_hh >= self.p["div_gap_pct"] and depth <= 1 / self.p["ask_bid_min"]:
                ok, wall_tag, wall_detail = self._check_wall(wall, price, "ask", sym)
                if ok:
                    reason = (f"价格新高但CVD落后{gap_hh:.0f}%+卖压+{wall_tag}"
                              f"{(':' + wall_detail) if wall_detail else ''}")
                    log.info("S06 entry short %s gap=%.1f depth=%.3f wall=%s %s",
                             sym, gap_hh, depth, wall_tag, wall_detail or "")
                    db.log("info", f"S06信号 short {sym} wall={wall_tag} {wall_detail}".strip())
                    out.append(Signal(self.sid, sym, "short", reason,
                                      stop_price=price + 1.0 * atr,
                                      take_profit=price - 1.2 * atr,
                                      size_mult=self.p["size_mult"],
                                      max_hold_sec=self.max_hold))
                else:
                    log.info("S06 skip short %s: CVD/depth ok but wall miss (%s)",
                             sym, wall_detail or wall_tag)
                    db.log("info", f"S06跳过 short {sym}: 无合格卖墙 ({wall_detail or wall_tag})")
            elif price_ll and gap_ll >= self.p["div_gap_pct"] and depth >= self.p["ask_bid_min"]:
                ok, wall_tag, wall_detail = self._check_wall(wall, price, "bid", sym)
                if ok:
                    reason = (f"价格新低但CVD抬升{gap_ll:.0f}%+买撑+{wall_tag}"
                              f"{(':' + wall_detail) if wall_detail else ''}")
                    log.info("S06 entry long %s gap=%.1f depth=%.3f wall=%s %s",
                             sym, gap_ll, depth, wall_tag, wall_detail or "")
                    db.log("info", f"S06信号 long {sym} wall={wall_tag} {wall_detail}".strip())
                    out.append(Signal(self.sid, sym, "long", reason,
                                      stop_price=price - 1.0 * atr,
                                      take_profit=price + 1.2 * atr,
                                      size_mult=self.p["size_mult"],
                                      max_hold_sec=self.max_hold))
                else:
                    log.info("S06 skip long %s: CVD/depth ok but wall miss (%s)",
                             sym, wall_detail or wall_tag)
                    db.log("info", f"S06跳过 long {sym}: 无合格买墙 ({wall_detail or wall_tag})")
        return out

    def _check_wall(self, wall, price, side, symbol=""):
        """Return (ok, tag, detail). Soft-pass still allows entry but tags clearly."""
        want = "上方卖墙" if side == "ask" else "下方买墙"
        if not wall:
            tag = f"{want}缺失软放行"
            detail = "ob_wall=None"
            log.warning("S06 %s %s: %s", symbol, tag, detail)
            return True, tag, detail
        orders = wall.get("orders") or []
        if not isinstance(orders, list) or not orders:
            tag = f"{want}空列表软放行"
            detail = f"orders=0 thr={wall.get('threshold_usd')}"
            log.warning("S06 %s %s: %s", symbol, tag, detail)
            return True, tag, detail
        rng = self.p["wall_range_pct"] / 100
        nearest = None
        for o in orders:
            try:
                p = float(o.get("price") or 0)
                o_side = str(o.get("side") or o.get("type") or "").lower()
                notional = float(o.get("notional") or 0)
            except (TypeError, ValueError):
                continue
            is_ask = o_side in ("ask", "sell", "s")
            is_bid = o_side in ("bid", "buy", "b")
            if side == "ask" and is_ask and p > price:
                dist = (p - price) / price
                if nearest is None or dist < nearest[0]:
                    nearest = (dist, p, notional)
                if dist <= rng:
                    detail = f"px={p:.6g} dist={dist*100:.2f}% n={notional:.0f}U"
                    return True, want, detail
            if side == "bid" and is_bid and p < price:
                dist = (price - p) / price
                if nearest is None or dist < nearest[0]:
                    nearest = (dist, p, notional)
                if dist <= rng:
                    detail = f"px={p:.6g} dist={dist*100:.2f}% n={notional:.0f}U"
                    return True, want, detail
        if nearest:
            detail = (f"最近墙 px={nearest[1]:.6g} dist={nearest[0]*100:.2f}% "
                      f"> {self.p['wall_range_pct']}% n={nearest[2]:.0f}U "
                      f"orders={len(orders)}")
        else:
            detail = f"无同侧墙 orders={len(orders)}"
        return False, f"无合格{want}", detail


# ── S11 高频接针(暴跌/暴涨清算捕捉)─────────────────────────────────────────

class S11CrashScalp(Base):
    sid = "S11_CRASH_SCALP"
    FACTORS = ["liq_agg"]   # 依赖因子(策略页展示/启用校验)
    DEFAULT_INTERVAL = 1   # design scan cadence (backtests use this)
    META = {
        "name": "高频接针(暴跌/暴涨)", "type": "高频均值回归", "direction": "双向",
        "logic": "暴跌/暴涨瞬间大量杠杆被强平,清算引擎的被迫单制造价格超调。用 WebSocket 逐笔价格做秒级速度监控,"
                 "在刀势减缓(企稳)的一刻反向接针,吃清算后的均值回摆——不追趋势,只做快进快出的反弹/回落。",
        "entry": "观察窗口(默认30s)内跌幅≥1.2% 且 近3s刀势减缓(近端幅度≤窗口幅度×0.4)→ 市价接多;"
                 "暴涨镜像接空。爆仓倍数因子可用时作为增强确认。同币有冷却期防止一波反复进。",
        "exit": "反弹/回落 0.6% 自动止盈(核心);继续恶化 0.8% 止损;浮盈过半且秒级动能反转 → 提前止盈;最长持仓 2h。",
        "factors": "WebSocket 逐笔价格(币安秒级)· liq_agg 爆仓倍数(OKX/Bitget补齐)· 订单簿深度",
        "risk": "接飞刀本质高风险——严格小止损+秒级监控;黑天鹅单边时靠 0.8% 止损和全局熔断兜底,不加仓不扛单。",
        "name_en": "HF Crash Scalp",
        "logic_en": "Crashes force-liquidate leverage and the forced flow overshoots price. Watch second-level tick velocity and catch the reversal the moment the knife decelerates: quick in, quick out.",
        "entry_en": "Window move >= threshold and the last 3s decelerated (<= decel x window pace) -> market entry against the move; per-coin cooldown",
        "exit_en": "TP +0.6% on the bounce (core); SL 0.8%; early exit when second-level momentum flips past half target; max hold 2h",
        "factors_en": "Binance tick WS / liq_agg (OKX/Bitget) / orderbook depth",
        "risk_en": "Inherently risky knife-catching: strict small stops + second-level monitoring; black swans covered by the 0.8% stop and global halt; never add to losers",
    }

    def _cooldown_key(self, sym):
        return f"s11_cd:{sym}"

    async def evaluate(self, ctx):
        from . import ws
        out = []
        win = self.p["window_sec"]
        recent = self.p["recent_sec"]
        now = time.time()
        for sym in self.syms(ctx):
            if ctx.has_position(sym):
                continue
            # same-coin cooldown
            last = float(db.get_meta(self._cooldown_key(sym), "0") or 0)
            if now - last < self.p["cooldown_sec"]:
                continue
            if ws.buffer_span(sym) < self.p["min_buffer_sec"]:
                continue  # not enough tick history yet
            move = ws.change_pct(sym, win)          # window move (neg = crash)
            near = ws.change_pct(sym, recent)        # last-3s move
            price = ctx.price(sym)
            if move is None or near is None or not price:
                continue

            liq = ctx.factor("liq_agg", sym) or {}
            drop = self.p["drop_pct"]
            decel = self.p["decel_ratio"]

            # ── crash -> catch long ──────────────────────────────────────────
            if move <= -drop:
                # deceleration: recent move no longer as steep as the window slide
                if near < move * decel:      # e.g. near=-0.9 vs move*0.4=-0.48 -> still falling
                    continue
                if self.p.get("require_liq") and liq.get("long_mult", 0) < self.p["liq_confirm_mult"]:
                    continue
                boost = liq.get("long_mult", 0) >= self.p["liq_confirm_mult"]
                out.append(Signal(
                    self.sid, sym, "long",
                    f"{win}s暴跌{move:.2f}%+近{recent}s企稳{near:.2f}%"
                    + (f"+爆仓{liq.get('long_mult',0):.1f}x确认" if boost else ""),
                    stop_price=price * (1 - self.p["stop_pct"] / 100),
                    take_profit=price * (1 + self.p["tp_pct"] / 100),
                    max_hold_sec=self.max_hold))
                db.set_meta(self._cooldown_key(sym), int(now))

            # ── spike -> catch short ─────────────────────────────────────────
            elif self.p.get("spike_short") and move >= drop:
                if near > move * decel:      # still ripping up
                    continue
                if self.p.get("require_liq") and liq.get("short_mult", 0) < self.p["liq_confirm_mult"]:
                    continue
                boost = liq.get("short_mult", 0) >= self.p["liq_confirm_mult"]
                out.append(Signal(
                    self.sid, sym, "short",
                    f"{win}s暴涨{move:.2f}%+近{recent}s见顶{near:.2f}%"
                    + (f"+爆仓{liq.get('short_mult',0):.1f}x确认" if boost else ""),
                    stop_price=price * (1 + self.p["stop_pct"] / 100),
                    take_profit=price * (1 - self.p["tp_pct"] / 100),
                    max_hold_sec=self.max_hold))
                db.set_meta(self._cooldown_key(sym), int(now))
        return out

    async def check_exit(self, ctx, pos):
        """Early take-profit: once past half the target and second-level momentum
        reverses, bank it (the bounce is exhausting)."""
        from . import ws
        price = ctx.price(pos["symbol"])
        if not price:
            return None
        sign = 1 if pos["side"] == "long" else -1
        gain_pct = (price - pos["entry_price"]) / pos["entry_price"] * 100 * sign
        if gain_pct < self.p["tp_pct"] * self.p["exhaust_tp_frac"]:
            return None
        near = ws.change_pct(pos["symbol"], self.p["recent_sec"])
        if near is None:
            return None
        # long: reversal = recent move turns negative; short: turns positive
        if (pos["side"] == "long" and near < -0.05) or (pos["side"] == "short" and near > 0.05):
            return f"反弹动能衰竭提前止盈(浮盈{gain_pct:.2f}%)"
        return None



# ── S14 VWAP 偏离回归 ────────────────────────────────────────────────────────

class S14VwapRevert(Base):
    sid = "S14_VWAP_REVERT"
    FACTORS = ["taker_flow"]   # 依赖因子(策略页展示/启用校验)
    DEFAULT_INTERVAL = 60
    META = {
        "name": "VWAP偏离回归", "type": "高频均值回归", "direction": "双向",
        "logic": "价格对 60 分钟成交量加权均价(VWAP)的过度偏离,在推动性主动买卖流衰竭时大概率回归;"
                 "偏离越大、taker 流越衰竭,回归赔率越好。",
        "entry": "|price/VWAP60 − 1| ≥ dev_pct 且 taker 买卖比确认推动力衰竭(超买时 taker≤阈值,超卖镜像)→ 反向开仓。",
        "exit": "回归至偏离的 exit_frac 处止盈;继续偏离 0.8×dev 止损;最长持仓 45 分钟。",
        "factors": "1m K线(VWAP60)· taker_flow（均为币安官方）",
        "risk": "趋势日的持续偏离是主要风险——taker 衰竭过滤 + 偏离基础上的固定止损,严禁摊平。",
        "name_en": "VWAP Deviation Revert",
        "logic_en": "Excess deviation from the 60m volume-weighted average price mean-reverts once the driving taker flow exhausts; bigger deviation with drier flow = better odds.",
        "entry_en": "|price/VWAP60 - 1| >= dev threshold with taker flow confirming exhaustion -> enter against the deviation",
        "exit_en": "TP at exit_frac of the deviation back to VWAP; SL at 0.8x further deviation; max hold 45m",
        "factors_en": "1m klines (VWAP60) / taker_flow (Binance-native)",
        "risk_en": "Trend days keep deviating: taker exhaustion filter + hard stop beyond the deviation, never average down",
    }

    def _cd_key(self, sym):
        return f"s14_cd:{sym}"

    async def evaluate(self, ctx):
        out = []
        now = time.time()
        for sym in self.syms(ctx):
            if ctx.has_position(sym):
                continue
            if now - float(db.get_meta(self._cd_key(sym), "0") or 0) < self.p["cooldown_sec"]:
                continue
            price = ctx.price(sym)
            if not price:
                continue
            ks = await ctx.klines(sym, "1m", 61)
            if len(ks) < 45:
                continue
            pv = sum((k["high"] + k["low"] + k["close"]) / 3 * k["volume"] for k in ks[:-1])
            v = sum(k["volume"] for k in ks[:-1])
            if not v:
                continue
            vwap = pv / v
            dev = (price / vwap - 1) * 100
            if abs(dev) < self.p["dev_pct"]:
                continue
            tk = ctx.factor("taker_flow", sym)
            conf = self.p["taker_conf"]
            if dev > 0:                      # overbought -> need buy exhaustion
                if tk and tk["latest"] > conf:
                    continue
                side = "short"
                tp = vwap * (1 + dev / 100 * self.p["exit_frac"])
                sp = price * (1 + abs(dev) / 100 * 0.8)
            else:                            # oversold -> need sell exhaustion
                if tk and tk["latest"] < 1 / conf:
                    continue
                side = "long"
                tp = vwap * (1 + dev / 100 * self.p["exit_frac"])
                sp = price * (1 - abs(dev) / 100 * 0.8)
            out.append(Signal(self.sid, sym, side,
                              f"偏离VWAP60 {dev:+.2f}%且taker={tk['latest'] if tk else '?'}衰竭,回归",
                              stop_price=sp, take_profit=tp,
                              max_hold_sec=self.max_hold))
            db.set_meta(self._cd_key(sym), int(now))
        return out



REGISTRY = {cls.sid: cls for cls in [
    S02LiqRebound, S06CvdFade, S11CrashScalp, S14VwapRevert,
]}
