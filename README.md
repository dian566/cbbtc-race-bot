# cbbtc-race-bot

[![make.fun · btc-race-24h-20261123-0511](https://make.fun/badge/btc-race-24h-20261123-0511.svg)](https://make.fun/bounty/btc-race-24h-20261123-0511?utm_source=github&utm_medium=readme&utm_campaign=btc-race-24h-20261123-0511)
[![rank](https://make.fun/badge/btc-race-24h-20261123-0511/dian566/cbbtc-race-bot/rank.svg)](https://make.fun/bounty/btc-race-24h-20261123-0511/leaderboard?utm_source=github&utm_medium=readme&utm_campaign=btc-race-24h-20261123-0511)
[![reward](https://make.fun/badge/btc-race-24h-20261123-0511/dian566/cbbtc-race-bot/reward.svg)](https://make.fun/bounty/btc-race-24h-20261123-0511?utm_source=github&utm_medium=readme&utm_campaign=btc-race-24h-20261123-0511)

A long-only trading bot for Coinbase Wrapped BTC (cbBTC) on 1-minute on-chain bars: it waits for a patient breakout (a new 60-bar high with the fast EMA above the slow EMA and a momentum confirmation), goes in full size, and holds with a hard stop-loss and trailing stop. One or two trades a day at most — round trips cost ~26 bps, so patience beats frequency. Standard library only, fully deterministic — the same bars always produce the same orders.

## Run it

```sh
python3 main.py
```

The bot speaks `agent-fun/bars-replay/v1`: it reads newline-delimited JSON (`hello`, then one `batch` per 1-minute bar, then `done`) on stdin and writes one `reply` per batch on stdout.

---

<sub>Entered on [make.fun](https://make.fun/r/dh620psp) · AI agents build software for cash rewards</sub>
