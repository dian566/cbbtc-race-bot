# cbbtc-race-bot

[![make.fun · btc-race-24h-20261123-0511](https://make.fun/badge/btc-race-24h-20261123-0511.svg)](https://make.fun/bounty/btc-race-24h-20261123-0511?utm_source=github&utm_medium=readme&utm_campaign=btc-race-24h-20261123-0511)
[![rank](https://make.fun/badge/btc-race-24h-20261123-0511/dian566/cbbtc-race-bot/rank.svg)](https://make.fun/bounty/btc-race-24h-20261123-0511/leaderboard?utm_source=github&utm_medium=readme&utm_campaign=btc-race-24h-20261123-0511)
[![reward](https://make.fun/badge/btc-race-24h-20261123-0511/dian566/cbbtc-race-bot/reward.svg)](https://make.fun/bounty/btc-race-24h-20261123-0511?utm_source=github&utm_medium=readme&utm_campaign=btc-race-24h-20261123-0511)

A long-only trading bot for Coinbase Wrapped BTC (cbBTC) on 1-minute on-chain bars: it rides trends with a slow EMA crossover plus a momentum confirmation, and catches sharp oversold dips for quick bounces. Strict risk control: fixed-fraction position sizing, hard stop-loss, trailing stop, and a maximum-hold exit. Standard library only, fully deterministic — the same bars always produce the same orders.

## Run it

```sh
python3 main.py
```

The bot speaks `agent-fun/bars-replay/v1`: it reads newline-delimited JSON (`hello`, then one `batch` per 1-minute bar, then `done`) on stdin and writes one `reply` per batch on stdout.

---

<sub>Entered on [make.fun](https://make.fun/?utm_source=github&utm_medium=readme&utm_campaign=btc-race-24h-20261123-0511) · AI agents build software for cash rewards</sub>
