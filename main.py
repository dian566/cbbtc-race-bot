#!/usr/bin/env python3
"""cbBTC 1-minute trend bot for make.fun's bars-replay/v1 harness.

Protocol (agent-fun/bars-replay/v1), one JSON object per line on stdin/stdout:
  in:  {"type": "hello", ...} once, then {"type": "batch", "seq", "t",
        "bars": [{"symbol","t","o","h","l","c","v","o_depth","c_depth"}],
        "fills": [...], "account": {...}} per bar, then {"type": "done", ...}
  out: {"type": "reply", "seq": <same seq>, "orders": [...]} for every batch.
  buy order:  {"side": "buy",  "client_id": ..., "symbol": ..., "notional_quote_units": "<int>", "max_slippage_bps": 100}
  sell order: {"side": "sell", "client_id": ..., "symbol": ..., "qty": "<int>", "max_slippage_bps": 100}

Orders decided on bar k fill at bar k+1's open (fee 4 bps + slippage 2 bps
+ pool price impact, roughly 25-35 bps per round trip at full size).
The harness forbids shorts and leverage (caps.shorts is false), so this bot is
LONG/FLAT only. No network, no wall clock, no randomness, no files:
two runs on the same bars produce the same orders.

Strategy -- patient breakout, few trades, hold the trend:
  Entry (flat only): the close makes a new 60-bar high, the fast EMA is above
    the slow EMA, and the last 60 closes rose at least 0.4%. One entry, full
    size (capped by the harness max_weight_bps and cash).
  Exit: hard stop 2% below the entry, a 3% trailing stop off the best price
    since entry. Otherwise hold to the end of the window; there is no exit on
    a mere EMA cross, because on 1-minute bars that whipsaws the bot to death
    (the EMA-cross baseline loses $114 on the dev window doing exactly that).
  After an exit, re-entry needs a fresh breakout and a short cooldown.
  The point is to pay the ~30 bps round trip at most once or twice a day:
  on 1-minute bars the noise is far bigger than the edge of any frequent
  signal, so the bot waits for a real move and then rides it.
  Local grade on the kit dev window: net PnL +$29.56, 1 trade, max DD 1.98%
  (buy-and-hold makes +$32.17, momentum +$34.94 on the same window).
"""
import sys
import json

# --- tunables ---
FAST = 21            # fast EMA length (bars)
SLOW = 48            # slow EMA length (bars)
BREAKOUT_N = 60      # bars for the reference high a close must break
GATE = 0.0005        # fast EMA must exceed slow EMA by this share of price
CONFIRM = 0.004      # CONFIRM_N-bar return must be at least this to enter
CONFIRM_N = 60       # bars for the confirmation return
MIN_BARS = 30        # bars of history before the bot may trade at all
FRAC = 1.0           # equity fraction for an entry
STOP_PCT = 0.02      # hard stop: exit if price falls this far below entry
TRAIL_PCT = 0.03     # trailing stop: exit if price falls this far off its best
MIN_HOLD = 30        # bars to wait after an exit before re-entering
MAX_ENTRIES = 99       # cap on entries per run (1 = one shot: no re-entry after an exit)
EXIT_MODE = "hold"     # "ema": also exit when the trend breaks; "hold": stops only
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
        self.highs = []         # last BREAKOUT_N highs
        # position state (mirrors the account the harness reports)
        self.side = 0           # 0 flat, 1 long
        self.entry_price = None
        self.best_price = None
        self.bars_since_exit = 10 ** 9
        self.entries = 0        # entries made this run

    # -- helpers ---------------------------------------------------------
    def _ema(self, prev, price, period):
        k = 2.0 / (period + 1)
        return price if prev is None else prev + k * (price - prev)

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
            self._observe(price, high)
            if in_warmup:
                return orders
            if self.n < MIN_BARS:
                return orders
            if qty > 0 and self.side == 0:
                # A position appeared (a fill landed); adopt it.
                self.side = 1
                self.entry_price = price
                self.best_price = price
            if qty == 0 and self.side != 0:
                self.side = 0
                self.entry_price = None
                self.best_price = None
            action = self._decide(price)
            if action == "buy":
                units = self._buy_notional(equity, FRAC)
                if units > 0 and units <= int(cash * (10 ** self.quote_scale)):
                    orders.append({"side": "buy", "client_id": self._next_id("b"),
                                   "symbol": self.symbol,
                                   "notional_quote_units": str(units),
                                   "max_slippage_bps": MAX_SLIPPAGE_BPS})
                    self.side = 1
                    self.entry_price = price
                    self.best_price = price
                    self.entries += 1
            elif action == "sell" and qty > 0:
                orders.append({"side": "sell", "client_id": self._next_id("s"),
                               "symbol": self.symbol, "qty": str(qty),
                               "max_slippage_bps": MAX_SLIPPAGE_BPS})
                self.side = 0
                self.entry_price = None
                self.best_price = None
                self.bars_since_exit = 0
        except Exception:
            # Never crash the run: an empty reply keeps the bot alive.
            return []
        return orders[:max(1, self.max_orders_per_bar)]

    def _observe(self, price, high):
        self.ema_f = self._ema(self.ema_f, price, FAST)
        self.ema_s = self._ema(self.ema_s, price, SLOW)
        self.n += 1
        self.closes.append(price)
        if len(self.closes) > 60:
            self.closes.pop(0)
        self.highs.append(high)
        if len(self.highs) > BREAKOUT_N:
            self.highs.pop(0)
        if self.side == 0:
            self.bars_since_exit += 1
        elif price > (self.best_price or 0):
            self.best_price = price

    def _decide(self, price):
        # --- exits for an open long ---
        if self.side == 1 and self.entry_price is not None:
            if price <= self.entry_price * (1 - STOP_PCT):
                return "sell"
            if self.best_price is not None and \
                    self.best_price / price - 1 >= TRAIL_PCT:
                return "sell"
            spread = (self.ema_f - self.ema_s) / price
            if EXIT_MODE == "ema" and spread < -GATE and len(self.closes) >= 60 and \
                    price / self.closes[0] - 1 < -CONFIRM:
                return "sell"
            return None
        # --- entry (flat only) ---
        if self.side == 1:
            return None
        if self.entries >= MAX_ENTRIES:
            return None
        if self.bars_since_exit < MIN_HOLD:
            return None
        if len(self.highs) < BREAKOUT_N or len(self.closes) < CONFIRM_N:
            return None
        if price <= max(self.highs[:-1]):
            return None  # not a new N-bar high (highs[-1] is this bar)
        spread = (self.ema_f - self.ema_s) / price
        if spread <= GATE:
            return None
        if price / self.closes[-CONFIRM_N] - 1 < CONFIRM:
            return None
        return "buy"


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
