#!/usr/bin/env python3
"""cbBTC 1-minute trend + bounce bot for make.fun's bars-replay/v1 harness.

Protocol (agent-fun/bars-replay/v1), one JSON object per line on stdin/stdout:
  in:  {"type": "hello", ...} once, then {"type": "batch", "seq", "t",
        "bars": [{"symbol","t","o","h","l","c","v","o_depth","c_depth"}],
        "fills": [...], "account": {...}} per bar, then {"type": "done", ...}
  out: {"type": "reply", "seq": <same seq>, "orders": [...]} for every batch.
  buy order:  {"side": "buy",  "client_id": ..., "symbol": ..., "notional_quote_units": "<int>", "max_slippage_bps": 100}
  sell order: {"side": "sell", "client_id": ..., "symbol": ..., "qty": "<int>", "max_slippage_bps": 100}

Orders decided on bar k fill at bar k+1's open (fee 4 bps + slippage 2 bps).
The harness forbids shorts and leverage (caps.shorts is false), so this bot is
LONG/FLAT only: it buys strength or sharp dips and exits on stops, trailing
stops, or trend reversal. No network, no wall clock, no randomness, no files:
two runs on the same bars produce the same orders.

Strategy (per symbol, here one symbol: cbBTC/USD):
  TREND leg: EMA(120) vs EMA(480) on closes. Enter long when the fast EMA is
    above the slow one by more than `gate` AND the last 60 closes rose at
    least `confirm`; exit when the fast EMA falls back below the slow one,
    or on the hard stop / trailing stop.
  DIP leg: when flat, buy a sharp dip -- price at least `dip_pct` below the
    60-bar high with RSI(14) at most `rsi_max` -- and sell on a quick
    rebound (`take_pct`), the hard stop, or after `max_hold` bars.
  Position size is a fixed fraction of equity, capped by the harness's
  max_weight_bps and by available cash, and never below min_notional.
"""
import sys
import json

# --- tunables (chosen on 8 days of BTC 1m bars + the kit's dev window) ---
FAST = 120          # fast EMA length (bars)
SLOW = 480          # slow EMA length (bars)
GATE = 0.0005       # fast must exceed slow by this share of price to enter
CONFIRM = 0.004     # 60-bar return must be at least this to enter a trend
TREND_FRAC = 0.5    # equity fraction for a trend entry
STOP_PCT = 0.01     # hard stop: exit if price falls this far below entry
TRAIL_PCT = 0.02    # trailing stop: exit if price falls this far off its best
DIP_LOOKBACK = 60   # bars for the dip reference high
DIP_PCT = 0.015     # buy when price is this far below the lookback high
RSI_N = 14
RSI_MAX = 40        # ... and RSI(14) is at most this (oversold)
DIP_FRAC = 0.5      # equity fraction for a dip entry
DIP_TAKE = 0.01     # take profit on a dip trade
DIP_STOP = 0.01     # hard stop on a dip trade
DIP_MAX_HOLD = 240  # bars before a dip trade is closed regardless
MIN_HOLD = 5        # bars to wait after an exit before re-entering
MAX_SLIPPAGE_BPS = 100


class Bot:
    def __init__(self):
        self.symbol = None
        self.price_scale = 6
        self.qty_scale = 8
        self.quote_scale = 6
        self.start_cash = 0
        self.warmup_bars = 0
        self.max_weight_bps = 10000
        self.max_orders_per_bar = 1
        self.min_notional = 0
        self.order_no = 0
        # indicator state
        self.ema_f = None
        self.ema_s = None
        self.n = 0
        self.closes = []        # last 60 closes, for the 60-bar return
        self.highs = []         # last DIP_LOOKBACK highs
        self.prev_close = None
        self.gains = []
        self.losses = []
        # position state (mirrors the account the harness reports)
        self.side = 0           # 0 flat, 1 long
        self.entry_price = None
        self.best_price = None
        self.held = 0
        self.bars_since_exit = 10 ** 9
        self.mode = None        # "trend" or "dip" for the open position

    # -- helpers ---------------------------------------------------------
    def _ema(self, prev, price, period):
        k = 2.0 / (period + 1)
        return price if prev is None else prev + k * (price - prev)

    def _rsi(self):
        if len(self.gains) < RSI_N:
            return None
        g = sum(self.gains) / len(self.gains)
        l = sum(self.losses) / len(self.losses)
        if l == 0:
            return 100.0
        return 100.0 - 100.0 / (1.0 + g / l)

    def _next_id(self, prefix):
        self.order_no += 1
        return "%s%d" % (prefix, self.order_no)

    def _buy_notional(self, equity, frac):
        """Quote units to spend for `frac` of equity, within harness caps."""
        if equity <= 0:
            return 0
        target = equity * frac
        cap = equity * self.max_weight_bps / 10000.0
        if target > cap:
            target = cap
        units = int(target * (10 ** self.quote_scale))
        if units < self.min_notional:
            return 0
        return units

    # -- protocol --------------------------------------------------------
    def on_hello(self, msg):
        syms = msg.get("symbols") or []
        pick = None
        for s in syms:
            if "BTC" in s.get("symbol", ""):
                pick = s
                break
        if pick is None and syms:
            pick = syms[0]
        if pick is not None:
            self.symbol = pick.get("symbol")
            self.price_scale = int(pick.get("price_scale", 6))
            self.qty_scale = int(pick.get("qty_scale", 8))
        cap = msg.get("capital") or {}
        self.quote_scale = int(cap.get("quote_scale", 6))
        self.start_cash = int(cap.get("quote_units", 0))
        caps = msg.get("caps") or {}
        self.max_weight_bps = int(caps.get("max_weight_bps", 10000))
        self.max_orders_per_bar = int(caps.get("max_orders_per_bar", 1))
        fees = msg.get("fees") or {}
        self.min_notional = int(fees.get("min_notional_quote_units", 0))
        self.warmup_bars = int(msg.get("warmup_bars") or 0)

    def on_batch(self, msg):
        seq = msg.get("seq", 0)
        bars = msg.get("bars") or []
        account = msg.get("account") or {}
        orders = []
        try:
            bar = None
            for b in bars:
                if b.get("symbol") == self.symbol:
                    bar = b
                    break
            if bar is None:
                return orders
            price = int(bar["c"]) / (10 ** self.price_scale)
            high = int(bar["h"]) / (10 ** self.price_scale)
            equity = int(account.get("equity_quote_units", 0)) / (10 ** self.quote_scale)
            cash = int(account.get("cash_quote_units", 0)) / (10 ** self.quote_scale)
            positions = account.get("positions") or []
            qty = 0
            for p in positions:
                if p.get("symbol") == self.symbol:
                    qty = int(p.get("qty", 0))
            in_warmup = seq < self.warmup_bars
            self._observe(price, high, equity)
            if in_warmup:
                return orders
            if self.n < SLOW + 2:
                return orders
            if qty > 0 and self.side == 0:
                # Position appeared (e.g. a fill landed); adopt it as a trend leg.
                self.side = 1
                self.mode = "trend"
                self.entry_price = price
                self.best_price = price
                self.held = 0
            if qty == 0 and self.side != 0:
                self.side = 0
                self.mode = None
                self.entry_price = None
                self.best_price = None
            action = self._decide(price, equity, cash, qty)
            if action == "buy_trend":
                units = self._buy_notional(equity, TREND_FRAC)
                if units > 0 and units <= int(cash * (10 ** self.quote_scale)):
                    orders.append({"side": "buy", "client_id": self._next_id("b"),
                                   "symbol": self.symbol,
                                   "notional_quote_units": str(units),
                                   "max_slippage_bps": MAX_SLIPPAGE_BPS})
                    self.side = 1
                    self.mode = "trend"
                    self.entry_price = price
                    self.best_price = price
                    self.held = 0
            elif action == "buy_dip":
                units = self._buy_notional(equity, DIP_FRAC)
                if units > 0 and units <= int(cash * (10 ** self.quote_scale)):
                    orders.append({"side": "buy", "client_id": self._next_id("b"),
                                   "symbol": self.symbol,
                                   "notional_quote_units": str(units),
                                   "max_slippage_bps": MAX_SLIPPAGE_BPS})
                    self.side = 1
                    self.mode = "dip"
                    self.entry_price = price
                    self.best_price = price
                    self.held = 0
            elif action == "sell" and qty > 0:
                orders.append({"side": "sell", "client_id": self._next_id("s"),
                               "symbol": self.symbol, "qty": str(qty),
                               "max_slippage_bps": MAX_SLIPPAGE_BPS})
                self.side = 0
                self.mode = None
                self.entry_price = None
                self.best_price = None
                self.bars_since_exit = 0
        except Exception:
            # Never crash the run: an empty reply keeps the bot alive.
            return []
        return orders[:max(1, self.max_orders_per_bar)]

    def _observe(self, price, high, equity):
        self.ema_f = self._ema(self.ema_f, price, FAST)
        self.ema_s = self._ema(self.ema_s, price, SLOW)
        self.n += 1
        if self.prev_close is not None:
            ch = price - self.prev_close
            self.gains.append(max(ch, 0.0))
            self.losses.append(max(-ch, 0.0))
            if len(self.gains) > RSI_N:
                self.gains.pop(0)
                self.losses.pop(0)
        self.prev_close = price
        self.closes.append(price)
        if len(self.closes) > 60:
            self.closes.pop(0)
        self.highs.append(high)
        if len(self.highs) > DIP_LOOKBACK:
            self.highs.pop(0)
        if self.side == 0:
            self.bars_since_exit += 1
        else:
            self.held += 1
            if self.best_price is None:
                self.best_price = price
            elif price > self.best_price:
                self.best_price = price

    def _decide(self, price, equity, cash, qty):
        # --- exits for an open long ---
        if self.side == 1 and self.entry_price is not None:
            if self.mode == "dip":
                if price >= self.entry_price * (1 + DIP_TAKE):
                    return "sell"
                if price <= self.entry_price * (1 - DIP_STOP):
                    return "sell"
                if self.held >= DIP_MAX_HOLD:
                    return "sell"
            else:  # trend leg
                if price <= self.entry_price * (1 - STOP_PCT):
                    return "sell"
                if self.best_price is not None and \
                        self.best_price / price - 1 >= TRAIL_PCT:
                    return "sell"
                spread = (self.ema_f - self.ema_s) / price
                if spread < -GATE:
                    return "sell"
        if self.side == 1:
            return None
        # --- entries (flat only) ---
        if self.bars_since_exit < MIN_HOLD:
            return None
        spread = (self.ema_f - self.ema_s) / price
        if spread > GATE and len(self.closes) >= 60:
            r60 = price / self.closes[0] - 1
            if r60 >= CONFIRM:
                return "buy_trend"
        if len(self.highs) >= DIP_LOOKBACK:
            hi = max(self.highs)
            if price <= hi * (1 - DIP_PCT):
                rsi = self._rsi()
                if rsi is not None and rsi <= RSI_MAX:
                    return "buy_dip"
        return None


def main():
    bot = Bot()
    out = sys.stdout
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except Exception:
            continue
        mtype = msg.get("type")
        if mtype == "hello":
            try:
                bot.on_hello(msg)
            except Exception:
                pass
        elif mtype == "batch":
            orders = bot.on_batch(msg)
            out.write(json.dumps({"type": "reply", "seq": msg.get("seq", 0),
                                  "orders": orders}) + "\n")
            out.flush()
        elif mtype == "done":
            break


if __name__ == "__main__":
    main()
