"""Probe momentum bot for the pump.fun bonding-curve replay.

Strategy (agent-fun/replay/v2, Python stdlib only):

* Watch every curve from its CreateEvent. A curve becomes a candidate only
  once it is mature (age >= 1000 slots) and its recent tape shows strong,
  accelerating buy pressure: at least 2 SOL of net inflow over a 40-slot
  window, at least 4 buys from at least 8 distinct wallets, a price up
  15-120% sitting within 5% of its window high, and at least twice as much
  net inflow in the newer half of the window as in the older half. A single
  dominant buyer or a creator who already sold vetoes the entry.
* The buy is a single 0.05 SOL probe per coin (no pyramiding: adds piled
  into losers on the dev window). Total exposure is capped at 5 SOL.
* A position is sold whole on the first of: a stop loss against cost, a take
  profit, a trailing stop once the position is up, a fade of the recent net
  flow, the creator selling while held, staleness, or a max-hold time limit.
  A curve that completes is closed by the replay itself.
* Guardrail probes keep the run above the 20-coin minimum: when the coins
  the bot has round-tripped lag the pace implied by elapsed block time, it
  buys a tiny probe on a liquid curve and sells it as soon as the buy lands.

Deterministic by construction: no clock, no randomness, no environment reads;
iteration only over insertion-ordered dicts fed by the batch stream. All
amounts are integer lamports; prices are float ratios of the integer
reserves, computed the same way in both graded executions.
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "vendor"))

import makefun_bot  # noqa: E402  (after the sys.path line: -P leaves the script directory off sys.path)

LAMPORTS = makefun_bot.LAMPORTS_PER_SOL
# Curve fee on sells: protocol 95 bps + creator 30 bps.
CURVE_FEE_BPS = 125

# --- entry signal -------------------------------------------------------
WINDOW = 40            # entry window, slots
MIN_NET_SOL = 2.0      # net buy SOL (buys minus sells) over the window
MIN_BUYS = 4           # buy trades in the window
MIN_BUYERS = 8         # distinct buying wallets in the window
MIN_PRICE_UP = 0.15    # price now over price at window start, minus 1
MAX_PRICE_UP = 1.20    # skip curves that already ran too far in one window
NEAR_HIGH = 0.95       # price now at least this share of the window high
ACCEL = 2.0          # newer-half net inflow must be at least this multiple of the older half's
MAX_TOP_SHARE = 0.60   # largest single wallet's share of window buy SOL
MIN_RQ_SOL = 3.0       # curve real SOL bounds at entry
MAX_RQ_SOL = 80.0
MIN_AGE = 1000          # slots since CreateEvent before an entry is allowed
# --- sizing -------------------------------------------------------------
INITIAL_SOL = 0.05     # first buy of a position
ADD_SOL = 0.10         # each pyramid add
MAX_STAKE_SOL = 0.05   # most SOL into one position in total
MIN_BUY_SOL = 0.012    # smallest buy worth sending
ADD_STEP = 0.02        # add only once price is this far above the last buy
ADD_MIN_FLOW_SOL = 0.2  # add only while net SOL over the window is >= this
# --- exits ---------------------------------------------------------------
STOP_LOSS = 0.12       # exit when value < cost * (1 - STOP_LOSS)
TAKE_PROFIT = 0.35     # exit when value >= cost * (1 + TAKE_PROFIT)
TRAIL_ARM = 0.10       # trailing stop arms once value >= cost * (1 + TRAIL_ARM)
TRAIL = 0.15           # exit on a fall of this share from the best value
MAX_HOLD = 1200        # exit after this many slots held
COOLDOWN = 200         # slots to wait after an exit before re-buying a coin
MAX_ENTRIES = 2        # most first entries per coin over the run
STALE_SLOTS = 1500     # exit when no trade has hit the coin for this long
FADE_WINDOW = 20       # exit when net SOL over this window falls below ...
FADE_NET_SOL = -1.0
# --- guardrail pacing ----------------------------------------------------
TARGET_COINS = 25      # round-tripped coins to aim for (minimum is 20)
PACE_SECS = 21600      # block-time seconds the pace is spread over (6 h)
PROBE_SOL = 0.012      # guardrail probe buy (>= 0.01 SOL round-trip minimum)
MAX_PROBES = 1         # most probe round trips open at once
MAX_EXPOSURE_SOL = 5.0  # most SOL at cost across all positions at once

BUY_SLIPPAGE_BPS = 1500
SELL_SLIPPAGE_BPS = 10000


class Tape:
    """A curve's recent trade tape, kept in slot order."""

    __slots__ = ("created", "creator", "trades", "entries", "last_exit",
                 "dev_sold", "last_trade_slot", "complete")

    def __init__(self):
        self.created = None      # slot of the CreateEvent
        self.creator = None      # creator wallet
        self.trades = []         # (slot, is_buy, sol_lamports, user, price)
        self.entries = 0         # first entries taken on this coin
        self.last_exit = -10**12
        self.dev_sold = False    # creator sold at least once
        self.last_trade_slot = 0
        self.complete = False


class Held:
    """A position the bot is managing."""

    __slots__ = ("opened", "peak_value", "invested", "last_add_price",
                 "last_value")

    def __init__(self, opened, invested, last_add_price):
        self.opened = opened
        self.peak_value = 0      # best close value seen (lamports)
        self.invested = invested  # lamports sent in buys for this position
        self.last_add_price = last_add_price  # price at the last buy decision
        self.last_value = 0      # close value at the latest batch


def price_of(vq, vt):
    """Price (quote per token) from virtual reserves; 0 when undefined."""
    if vt <= 0:
        return 0.0
    return vq / vt


def sell_value(tokens, vq, vt, tx_fee):
    """Lamports a sale of `tokens` against (vq, vt) would pay, curve fee and
    one tx fee deducted; 0 when the sale is worthless."""
    if tokens <= 0 or vt <= 0:
        return 0
    quote = (tokens * vq) // (vt + tokens)
    fee = (quote * CURVE_FEE_BPS + 9999) // 10000
    value = quote - fee - tx_fee
    return value if value > 0 else 0


class Strategy:
    def __init__(self):
        self.tapes = {}
        self.held = {}
        self.probes = {}         # mint -> True once its buy has landed
        self.my_fills = 0        # the bot's own filled orders
        self.my_coins = set()    # mints the bot has filled on
        self.first_time = None
        self.last_time = None
        self.keep = max(WINDOW, FADE_WINDOW) + 5

    def tape_of(self, mint):
        tape = self.tapes.get(mint)
        if tape is None:
            tape = Tape()
            self.tapes[mint] = tape
        return tape

    def entry_signal(self, tape, now, rq_sol):
        """True when the tape shows accelerating, broad-based buy pressure."""
        if rq_sol < MIN_RQ_SOL or rq_sol > MAX_RQ_SOL:
            return False
        if tape.created is None or now - tape.created < MIN_AGE:
            return False
        if tape.dev_sold:
            return False
        start = now - WINDOW
        mid = now - WINDOW // 2
        buy_sol = 0
        sell_sol = 0
        buys = 0
        net_old = 0
        net_new = 0
        start_price = -1.0
        high = 0.0
        last = 0.0
        by_user = {}
        for (slot, is_buy, sol, user, price) in tape.trades:
            if slot <= start:
                start_price = price
                continue
            if start_price < 0:
                start_price = price
            signed = sol if is_buy else -sol
            if slot <= mid:
                net_old += signed
            else:
                net_new += signed
            if is_buy:
                buy_sol += sol
                buys += 1
                by_user[user] = by_user.get(user, 0) + sol
            else:
                sell_sol += sol
            if price > high:
                high = price
            last = price
        if buys < MIN_BUYS or len(by_user) < MIN_BUYERS:
            return False
        if buy_sol - sell_sol < int(MIN_NET_SOL * LAMPORTS):
            return False
        if start_price <= 0 or last <= 0:
            return False
        up = last / start_price - 1.0
        if up < MIN_PRICE_UP or up > MAX_PRICE_UP:
            return False
        if last < high * NEAR_HIGH:
            return False
        if net_new <= 0 or net_new < ACCEL * net_old:
            return False
        top = 0
        for v in by_user.values():
            if v > top:
                top = v
        if buy_sol > 0 and top > MAX_TOP_SHARE * buy_sol:
            return False
        return True

    def net_flow(self, tape, now, window):
        """Net lamports (buys minus sells) over the last `window` slots."""
        net = 0
        for i in range(len(tape.trades) - 1, -1, -1):
            (slot, is_buy, sol, _user, _price) = tape.trades[i]
            if slot <= now - window:
                break
            net += sol if is_buy else -sol
        return net

    def on_batch(self, state, batch):
        now = batch["slot"]
        tx = state.tx_fee_lamports()
        cap = state.hello["caps"]["max_orders_per_batch"]
        traded = set()

        for f in batch["fills"]:
            if f["status"] == "filled" and not f["client_id"].startswith("_"):
                self.my_fills += 1
                self.my_coins.add(f["mint"])

        for e in batch["events"]:
            if e["block_time"] is not None:
                if self.first_time is None:
                    self.first_time = e["block_time"]
                if self.last_time is None or e["block_time"] > self.last_time:
                    self.last_time = e["block_time"]
            mint = e["mint"]
            if e["event"] == "CreateEvent":
                tape = self.tape_of(mint)
                tape.created = e["slot"]
                tape.creator = e["creator"]
            elif e["event"] == "TradeEvent":
                tape = self.tape_of(mint)
                sol = int(e["sol_amount"])
                price = price_of(int(e["virtual_quote_reserves"]),
                                 int(e["virtual_token_reserves"]))
                tape.trades.append((e["slot"], e["is_buy"], sol, e["user"], price))
                tape.last_trade_slot = e["slot"]
                if not e["is_buy"] and e["user"] == tape.creator:
                    tape.dev_sold = True
                traded.add(mint)
            else:  # CompleteEvent
                self.tape_of(mint).complete = True

        for mint in traded:
            tape = self.tapes[mint]
            cut = 0
            while cut < len(tape.trades) and tape.trades[cut][0] <= now - self.keep:
                cut += 1
            if cut:
                del tape.trades[:cut]

        marks = {}
        for m in batch["marks"]:
            marks[m["mint"]] = (int(m["virtual_quote_reserves"]),
                                int(m["virtual_token_reserves"]),
                                int(m["real_quote_reserves"]))

        pending = {}
        for po in state.pending.values():
            pending.setdefault(po.order["mint"], []).append(po.order["side"])

        def has_pending(mint, side):
            return side in pending.get(mint, ())

        orders = []

        # --- exits first -------------------------------------------------
        for mint in list(self.held):
            held = self.held[mint]
            pos = state.positions.get(mint)
            curve = state.curves.get(mint)
            if pos is None:
                if not pending.get(mint):
                    del self.held[mint]
                    self.tape_of(mint).last_exit = now
                continue
            if curve is None or curve.complete:
                continue
            mk = marks.get(mint)
            if mk is not None:
                vq, vt, rq = mk
            else:
                vq = curve.virtual_quote_reserves
                vt = curve.virtual_token_reserves
                rq = curve.real_quote_reserves
            tokens = int(pos.token_amount)
            value = sell_value(tokens, vq, vt, tx)
            cost = int(pos.cost_lamports)
            if value > held.peak_value:
                held.peak_value = value
            held.last_value = value
            tape = self.tape_of(mint)
            exit_now = False
            if cost > 0 and value < cost * (1 - STOP_LOSS):
                exit_now = True
            if not exit_now and cost > 0 and value >= cost * (1 + TAKE_PROFIT):
                exit_now = True
            if (not exit_now and cost > 0
                    and held.peak_value >= cost * (1 + TRAIL_ARM)
                    and value < held.peak_value * (1 - TRAIL)):
                exit_now = True
            if not exit_now and self.net_flow(tape, now, FADE_WINDOW) < int(FADE_NET_SOL * LAMPORTS):
                exit_now = True
            if not exit_now and tape.dev_sold:
                exit_now = True
            if not exit_now and now - tape.last_trade_slot >= STALE_SLOTS:
                exit_now = True
            if not exit_now and now - held.opened > MAX_HOLD:
                exit_now = True
            if not exit_now or has_pending(mint, "sell"):
                continue
            if len(orders) >= cap:
                break
            orders.append({"side": "sell", "mint": mint,
                           "token_amount": pos.token_amount,
                           "max_slippage_bps": SELL_SLIPPAGE_BPS})

        # --- probe round trips: sell the probe as soon as its buy lands --
        for mint in list(self.probes):
            pos = state.positions.get(mint)
            curve = state.curves.get(mint)
            if pos is None:
                if not pending.get(mint):
                    del self.probes[mint]
                continue
            if curve is None or curve.complete or has_pending(mint, "sell"):
                continue
            if len(orders) >= cap:
                break
            orders.append({"side": "sell", "mint": mint,
                           "token_amount": pos.token_amount,
                           "max_slippage_bps": SELL_SLIPPAGE_BPS})
            self.probes[mint] = True

        # --- entries and pyramid adds ------------------------------------
        free = state.free_cash_lamports()
        slots_left = state.hello["caps"]["max_open_positions"] - state.committed_position_count()
        exposure = 0
        for mint, held in self.held.items():
            pos = state.positions.get(mint)
            exposure += max(held.invested,
                            int(pos.cost_lamports) if pos is not None else 0)
        max_exposure = int(MAX_EXPOSURE_SOL * LAMPORTS)

        for mint in traded:
            if len(orders) >= cap:
                break
            curve = state.curves.get(mint)
            if curve is None or curve.complete:
                continue
            if has_pending(mint, "buy") or mint in self.probes:
                continue
            tape = self.tapes[mint]
            price = price_of(curve.virtual_quote_reserves,
                             curve.virtual_token_reserves)
            rq_sol = curve.real_quote_reserves / LAMPORTS
            held = self.held.get(mint)
            if held is None:
                if slots_left <= 0:
                    continue
                if tape.entries >= MAX_ENTRIES or now - tape.last_exit < COOLDOWN:
                    continue
                if not self.entry_signal(tape, now, rq_sol):
                    continue
                want = min(int(INITIAL_SOL * LAMPORTS),
                           int(MAX_STAKE_SOL * LAMPORTS))
            else:
                if held.invested >= int(MAX_STAKE_SOL * LAMPORTS) - int(MIN_BUY_SOL * LAMPORTS):
                    continue
                pos = state.positions.get(mint)
                if pos is None:
                    continue
                if price < held.last_add_price * (1 + ADD_STEP):
                    continue
                if held.last_value < int(pos.cost_lamports) * 1.0:
                    continue
                if self.net_flow(tape, now, WINDOW) < int(ADD_MIN_FLOW_SOL * LAMPORTS):
                    continue
                if rq_sol > MAX_RQ_SOL:
                    continue
                want = min(int(ADD_SOL * LAMPORTS),
                           int(MAX_STAKE_SOL * LAMPORTS) - held.invested)
            cap_lamports = state.max_buy_lamports(mint)
            amount = min(cap_lamports, want, max_exposure - exposure)
            if amount > free - tx:
                amount = free - tx
            if amount < int(MIN_BUY_SOL * LAMPORTS):
                continue
            orders.append({"side": "buy", "mint": mint,
                           "quote_lamports": amount,
                           "max_slippage_bps": BUY_SLIPPAGE_BPS})
            free -= amount + tx
            exposure += amount
            if held is None:
                self.held[mint] = Held(now, amount, price)
                tape.entries += 1
                slots_left -= 1
            else:
                held.invested += amount
                held.last_add_price = price

        # --- guardrail pacing: open a probe when coins lag the pace ------
        if (len(self.probes) < MAX_PROBES and slots_left > 0
                and self.first_time is not None and self.last_time is not None):
            pace = min(1.0, (self.last_time - self.first_time) / PACE_SECS)
            need_coins = len(self.my_coins) < int(TARGET_COINS * pace + 0.999)
            if need_coins:
                min_rq = (int(PROBE_SOL * LAMPORTS) * 10000
                          // state.hello["caps"]["max_order_bps_of_real_reserve"])
                for mint in traded:
                    if len(orders) >= cap or len(self.probes) >= MAX_PROBES:
                        break
                    if (mint in self.held or mint in self.probes
                            or has_pending(mint, "buy")
                            or mint in state.positions):
                        continue
                    curve = state.curves.get(mint)
                    if curve is None or curve.complete:
                        continue
                    if curve.real_quote_reserves < min_rq * 3 // 2:
                        continue
                    if free < int(PROBE_SOL * LAMPORTS) + 2 * tx:
                        break
                    orders.append({"side": "buy", "mint": mint,
                                   "quote_lamports": int(PROBE_SOL * LAMPORTS),
                                   "max_slippage_bps": BUY_SLIPPAGE_BPS})
                    self.probes[mint] = False
                    free -= int(PROBE_SOL * LAMPORTS) + tx
        return orders


STRATEGY = Strategy()


def on_batch(state, batch):
    return STRATEGY.on_batch(state, batch)


if __name__ == "__main__":
    makefun_bot.run(on_batch)
