[![make.fun](https://make.fun/badge/u-riidzhsn-pump-fun-bot-sprint-make-bot-and-win-cb203fed.svg)](https://make.fun/bounty/u-riidzhsn-pump-fun-bot-sprint-make-bot-and-win-cb203fed)
[![rank](https://make.fun/badge/u-riidzhsn-pump-fun-bot-sprint-make-bot-and-win-cb203fed/Akhmad045/u-riidzhsn-pump-fun-bot-sprint-make-bot-and-win-cb203fed/rank.svg)](https://make.fun/bounty/u-riidzhsn-pump-fun-bot-sprint-make-bot-and-win-cb203fed)
[![reward](https://make.fun/badge/u-riidzhsn-pump-fun-bot-sprint-make-bot-and-win-cb203fed/Akhmad045/u-riidzhsn-pump-fun-bot-sprint-make-bot-and-win-cb203fed/reward.svg)](https://make.fun/bounty/u-riidzhsn-pump-fun-bot-sprint-make-bot-and-win-cb203fed)

# Pump.fun Momentum Bot

A paper-trading bot for the [Pump.fun Bot Sprint: Make Bot And Win](https://make.fun/bounty/u-riidzhsn-pump-fun-bot-sprint-make-bot-and-win-cb203fed) bounty on [make.fun](https://make.fun). It trades paper SOL on a replay of pump.fun bonding-curve market data, ranked on net PnL. No real money, no wallet, no network access — everything the bot runs is in this repository.

## Strategy

Probe-and-pyramid momentum, built for the final replay window rather than the dev sample:

- **Entry:** a curve becomes a candidate only once mature (1000+ slots after creation) and its recent tape shows strong, accelerating, broad-based buy pressure — at least 2 SOL of net inflow over a 40-slot window, at least 4 buys from at least 8 distinct wallets, price up 15–120% and sitting within 5% of its window high, with at least twice as much inflow in the newer half of the window as in the older half. A single dominant buyer or a creator who already sold vetoes the entry.
- **Sizing:** a single 0.05 SOL probe per coin (no pyramiding: adds piled into losers on the dev window). Total exposure is capped at 5 SOL.
- **Exits:** a position is sold whole on the first of a 12% stop loss, a 35% take profit, a 15% trailing stop (armed at +10%), a fade in recent net flow, the creator selling while held, staleness, or a 1200-slot max hold. Curves that complete are closed by the replay itself.
- **Guardrails:** tiny 0.012 SOL probe round trips keep the run above the 20-coin minimum whenever the round-tripped coin count lags the pace implied by elapsed block time.

## Layout

- `main.py` — the bot (Python 3.13, standard library only), speaking `agent-fun/replay/v2` on stdin/stdout.
- `vendor/makefun_bot.py` — the kit's Python protocol shim, vendored unchanged.

## Running it

The kit's local grader reproduces the server's dev grading:

```sh
node cli/makefun.mjs grade-local --kit <kit dir> --bot . --driver docker --bundle transcripts.tar.gz
```

## Referral

Built with referral code `4g7knchf`.

## License

MIT — see [LICENSE](LICENSE).
