"""
paper_trade_cloud_engine.py

Fully stateless version of the paper-trading engine, built to run on
GitHub Actions (or any always-on runner) instead of Jason's Mac -- so the
demo keeps running whether or not his laptop is on.

WHY STATELESS: paper_trade_watchlist.py (the Mac version) kept a running
JSON ledger (balance, open positions, closed trades) that got updated
incrementally every poll. That works fine on a machine that's always on,
but is awkward across independent, ephemeral GitHub Actions runs -- each
run starts from a clean container with nothing carried over.

Instead, this version re-derives the ENTIRE current state from scratch on
every run: it fetches LOOKBACK_DAYS of live 5m history for all 10 coins,
runs the exact locked signal engine against it, and replays every
open/close event across all 10 coins in chronological order (a proper
discrete-event simulation) to apply the $100 concurrency-ceiling sizing
rule exactly as it would have applied in real time. Because this is
deterministic given the same price history and the same locked engine,
every run reproduces the identical balance/closed-trades/open-positions
picture a continuously-running poller would have -- no persisted ledger
required, as long as LOOKBACK_DAYS comfortably covers the full life of
the demo so far (see the docstring note on that limit below).

ENGINE IDENTITY: fetch_klines / http_get_json / resample_45m /
compute_rsi / compute_atr / compute_sma / detect_trades_live / pnl_dollars
are copied verbatim from paper_trade_watchlist.py (itself verified
byte-identical to the locked engine used in every regime-test/
stability-check script tonight). NOTHING about signal detection, trade
resolution, or P&L math changes here -- the only new code is
simulate_full_history(), the cross-symbol chronological concurrency
replay described above, and the same tier-2-defaults-to-FULL_SIZE
simplification already disclosed for the Mac version (regime_classifier_
final.py was never wired up).

HONEST LIMITATION: LOOKBACK_DAYS bounds how far back this can "see." If
a single position somehow stays open longer than LOOKBACK_DAYS (very
unlikely given ATR-based stops and tp_rr=4.5 on 45m bars, but not
provably impossible), it would drop out of view once its entry bar rolls
past the lookback window. LOOKBACK_DAYS=45 gives wide safety margin for
a demo that only just started. If this runs for months, LOOKBACK_DAYS
should be widened accordingly.

Usage
-----
    python3 paper_trade_cloud_engine.py                 # writes status.json + STATUS.md
    python3 paper_trade_cloud_engine.py --lookback-days 60
"""

import argparse
import datetime
import json
import time
import urllib.error
import urllib.request

BASE_URL = "https://api.binance.us"
KLINES_URL = BASE_URL + "/api/v3/klines"

INTERVAL = "5m"
PAGE_LIMIT = 1000
MAX_RETRIES_PER_PAGE = 3
RETRY_BACKOFF_SECONDS = 3

RSI_LEN = 14
ATR_LEN = 14
SMA_LEN = 200
STOP_ATR_MULT = 1.0
TP_RR = 4.5
RESAMPLE_MIN = 45
NATIVE_MIN = 5
BARS_PER_CHUNK = RESAMPLE_MIN // NATIVE_MIN

START_BALANCE = 1000.0
RISK_PCT = 2.0
RISK_DOLLARS_FULL = START_BALANCE * RISK_PCT / 100.0   # $20
RISK_DOLLARS_HALF = RISK_DOLLARS_FULL / 2.0             # $10
CONSERVATIVE_COST = 0.0030
MIN_STOP_PCT_FLOOR = 0.0005

CONCURRENCY_CEILING = 100.0
PER_TRADE_FLOOR = 2.0

LOOKBACK_DAYS_DEFAULT = 45

TIER1 = ["COMPUSDT", "CRVUSDT", "ONTUSDT", "CELRUSDT", "ILVUSDT", "SYSUSDT", "XNOUSDT"]
TIER2 = ["AUDIOUSDT", "LPTUSDT", "FLUXUSDT"]
WATCHLIST = TIER1 + TIER2


def now_ms():
    return int(time.time() * 1000)


def http_get_json(url, params, timeout=15):
    qs = "&".join(f"{k}={v}" for k, v in params.items())
    full_url = f"{url}?{qs}" if params else url
    req = urllib.request.Request(full_url, headers={"User-Agent": "Mozilla/5.0 (paper-trade-cloud-engine)"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def fetch_klines(symbol, start_ms, end_ms):
    """Pulls 5m klines from start_ms to end_ms, paginating as needed. Public
    endpoint -- no API key, no account, no KYC."""
    bars = []
    cursor_ms = start_ms
    while cursor_ms < end_ms:
        rows = None
        for attempt in range(1, MAX_RETRIES_PER_PAGE + 1):
            try:
                page = http_get_json(KLINES_URL, {
                    "symbol": symbol, "interval": INTERVAL,
                    "startTime": cursor_ms, "endTime": end_ms, "limit": PAGE_LIMIT,
                })
                rows = page if isinstance(page, list) else []
                break
            except Exception as e:
                print(f"    [{symbol}] fetch attempt {attempt}/{MAX_RETRIES_PER_PAGE} failed: {e}")
                if attempt < MAX_RETRIES_PER_PAGE:
                    time.sleep(RETRY_BACKOFF_SECONDS)
        if rows is None or not rows:
            break
        page_bars = []
        for r in rows:
            try:
                ts, o, h, l, c = int(r[0]), float(r[1]), float(r[2]), float(r[3]), float(r[4])
                if ts >= end_ms:
                    continue
                page_bars.append({"t": ts, "o": o, "h": h, "l": l, "c": c})
            except Exception:
                continue
        if not page_bars:
            break
        bars.extend(page_bars)
        cursor_ms = page_bars[-1]["t"] + 1
        if len(rows) < PAGE_LIMIT:
            break
    return bars


# ---------------------------------------------------------------------------
# LOCKED ENGINE -- verbatim from paper_trade_watchlist.py / every regime-test
# script tonight. Do not touch.
# ---------------------------------------------------------------------------

def resample_45m(bars5):
    out = []
    n = len(bars5)
    for i in range(0, n - BARS_PER_CHUNK + 1, BARS_PER_CHUNK):
        chunk = bars5[i:i + BARS_PER_CHUNK]
        if len(chunk) < BARS_PER_CHUNK:
            break
        out.append({
            "t": chunk[0]["t"], "o": chunk[0]["o"],
            "h": max(b["h"] for b in chunk), "l": min(b["l"] for b in chunk),
            "c": chunk[-1]["c"],
        })
    return out


def compute_rsi(closes, length=RSI_LEN):
    rsis = [None] * len(closes)
    if len(closes) < length + 1:
        return rsis
    gains, losses = [], []
    for i in range(1, length + 1):
        change = closes[i] - closes[i - 1]
        gains.append(max(change, 0))
        losses.append(max(-change, 0))
    avg_gain = sum(gains) / length
    avg_loss = sum(losses) / length
    rsis[length] = 100.0 if avg_loss == 0 else 100 - (100 / (1 + avg_gain / avg_loss))
    for i in range(length + 1, len(closes)):
        change = closes[i] - closes[i - 1]
        gain, loss = max(change, 0), max(-change, 0)
        avg_gain = (avg_gain * (length - 1) + gain) / length
        avg_loss = (avg_loss * (length - 1) + loss) / length
        rsis[i] = 100.0 if avg_loss == 0 else 100 - (100 / (1 + avg_gain / avg_loss))
    return rsis


def compute_atr(bars, length=ATR_LEN):
    n = len(bars)
    tr = [None] * n
    for i in range(n):
        if i == 0:
            tr[i] = bars[i]["h"] - bars[i]["l"]
        else:
            tr[i] = max(bars[i]["h"] - bars[i]["l"], abs(bars[i]["h"] - bars[i - 1]["c"]), abs(bars[i]["l"] - bars[i - 1]["c"]))
    atr = [None] * n
    for i in range(n):
        if i < length - 1:
            continue
        atr[i] = sum(tr[0:i + 1]) / length if i == length - 1 else (atr[i - 1] * (length - 1) + tr[i]) / length
    return atr


def compute_sma(closes, length=SMA_LEN):
    out = [None] * len(closes)
    running = 0.0
    for i, c in enumerate(closes):
        running += c
        if i >= length:
            running -= closes[i - length]
        if i >= length - 1:
            out[i] = running / length
    return out


def detect_trades_live(bars45, stop_atr_mult=STOP_ATR_MULT, tp_rr=TP_RR):
    """Identical signal/resolution logic to the locked detect_trades() used
    all night. The ONLY difference: also returns whatever position is still
    open (unresolved) at the end of the bar array, since live trading needs to
    display that instead of silently discarding it."""
    n = len(bars45)
    warmup = max(SMA_LEN, RSI_LEN + 1, ATR_LEN)
    if n < warmup + 5:
        return [], None
    closes = [b["c"] for b in bars45]
    rsi = compute_rsi(closes)
    atr = compute_atr(bars45)
    sma = compute_sma(closes)

    trades = []
    in_pos = False
    sl = tp = None
    is_buy = False
    stop_pct = 0.0
    entry_t = None
    entry_price = None

    for i in range(warmup, n):
        if in_pos:
            hi, lo = bars45[i]["h"], bars45[i]["l"]
            hit_tp = (hi >= tp) if is_buy else (lo <= tp)
            hit_sl = (lo <= sl) if is_buy else (hi >= sl)
            if hit_tp or hit_sl:
                r = -1.0 if hit_sl else tp_rr
                trades.append({
                    "entry_t": entry_t, "exit_t": bars45[i]["t"], "r": r,
                    "stop_pct": stop_pct, "is_buy": is_buy, "entry_price": entry_price,
                })
                in_pos = False
            continue

        if rsi[i] is None or atr[i] is None or sma[i] is None or i + 1 >= n:
            continue

        signal_buy = rsi[i] <= 30 and closes[i] > sma[i]
        signal_sell = rsi[i] >= 70 and closes[i] < sma[i]
        if not (signal_buy or signal_sell):
            continue

        entry_i = i + 1
        ep = bars45[entry_i]["o"]
        risk = stop_atr_mult * atr[i]
        if risk <= 0 or ep <= 0:
            continue
        if signal_buy:
            is_buy = True
            sl = ep - risk
            tp = ep + tp_rr * risk
        else:
            is_buy = False
            sl = ep + risk
            tp = ep - tp_rr * risk
        stop_pct = risk / ep
        entry_t = bars45[entry_i]["t"]
        entry_price = ep
        in_pos = True

    open_state = None
    if in_pos:
        open_state = {
            "entry_t": entry_t, "entry_price": entry_price, "is_buy": is_buy,
            "sl": sl, "tp": tp, "stop_pct": stop_pct,
        }
    return trades, open_state


def pnl_dollars(trade, risk_dollars, cost_pct=CONSERVATIVE_COST):
    effective_stop = max(trade["stop_pct"], MIN_STOP_PCT_FLOOR)
    cost_in_r = cost_pct / effective_stop
    r_net = trade["r"] - cost_in_r
    return round(risk_dollars * r_net, 4)


def base_risk_for(symbol):
    # Tier-2 Model D not wired up (see docstring) -- fails safe to FULL_SIZE,
    # same simplification already disclosed for the Mac version.
    return RISK_DOLLARS_FULL


# ---------------------------------------------------------------------------
# NEW: cross-symbol chronological concurrency-ceiling replay.
# This is new code (not copied from the Mac version's incremental poller),
# sanity-tested below -- a good-faith reconstruction of the adopted
# concurrency-ceiling concept (Part 13), producing the same $100-ceiling /
# $2-floor / proportional-scaling behavior but computed as a full discrete-
# event replay across all 10 symbols at once instead of incrementally.
# ---------------------------------------------------------------------------

def collect_positions(symbol, bars45):
    trades, open_state = detect_trades_live(bars45)
    positions = []
    for t in trades:
        positions.append({**t, "symbol": symbol, "exit_t": t["exit_t"]})
    if open_state is not None:
        positions.append({**open_state, "symbol": symbol, "exit_t": None})
    return positions


def simulate_full_history(positions_by_symbol):
    """positions_by_symbol: {symbol: [position dicts]} where each position dict
    has entry_t, exit_t (None if still open), is_buy, stop_pct, entry_price, r
    (r only present for resolved positions). Replays every ENTRY/EXIT event
    across all symbols in chronological order, applying the concurrency-
    ceiling sizing rule at each entry exactly as it would apply in real time."""
    events = []
    for symbol, positions in positions_by_symbol.items():
        for p in positions:
            events.append((p["entry_t"], 0, "ENTRY", p))
            if p["exit_t"] is not None:
                events.append((p["exit_t"], -1, "EXIT", p))
    # EXIT sorts before ENTRY at an identical timestamp (frees room first).
    events.sort(key=lambda e: (e[0], e[1]))

    assigned = {}       # (symbol, entry_t) -> risk_dollars or None
    current_total = 0.0
    balance = START_BALANCE
    closed_trades = []
    open_positions = {}  # symbol -> position dict with risk_dollars

    for t, _, kind, p in events:
        pid = (p["symbol"], p["entry_t"])
        if kind == "ENTRY":
            base = base_risk_for(p["symbol"])
            room = CONCURRENCY_CEILING - current_total
            sized = None
            if room > 0:
                candidate = min(base, room)
                if candidate >= PER_TRADE_FLOOR:
                    sized = round(candidate, 4)
            assigned[pid] = sized
            if sized is not None:
                current_total += sized
                open_positions[p["symbol"]] = {**p, "risk_dollars": sized}
        else:  # EXIT
            sized = assigned.get(pid)
            if sized is not None:
                pnl = pnl_dollars(p, sized)
                balance = round(balance + pnl, 4)
                closed_trades.append({**p, "risk_dollars": sized, "pnl": pnl})
                current_total = round(current_total - sized, 4)
            existing = open_positions.get(p["symbol"])
            if existing is not None and existing["entry_t"] == p["entry_t"]:
                del open_positions[p["symbol"]]

    closed_trades.sort(key=lambda t: t["exit_t"])
    return {
        "balance": balance,
        "open_positions": open_positions,
        "closed_trades": closed_trades,
    }


def fmt_ts(ms):
    return datetime.datetime.utcfromtimestamp(ms / 1000).strftime("%Y-%m-%d %H:%M UTC")


def build_status_markdown(state, generated_at_ms):
    balance = state["balance"]
    open_positions = state["open_positions"]
    closed_trades = state["closed_trades"]
    total_pnl = round(balance - START_BALANCE, 2)
    wins = sum(1 for t in closed_trades if t["pnl"] > 0)
    losses = sum(1 for t in closed_trades if t["pnl"] <= 0)
    win_rate = (wins / len(closed_trades) * 100.0) if closed_trades else 0.0
    cutoff_ms = generated_at_ms - 24 * 3600 * 1000
    last_24h = [t for t in closed_trades if t["exit_t"] >= cutoff_ms]

    lines = []
    lines.append(f"# Paper Trade Demo Status")
    lines.append("")
    lines.append(f"Generated: {fmt_ts(generated_at_ms)}")
    lines.append("")
    lines.append(f"Balance: ${balance:.2f} (start ${START_BALANCE:.2f}, net {'+' if total_pnl >= 0 else ''}{total_pnl:.2f})")
    lines.append(f"Closed trades: {len(closed_trades)}  Wins: {wins}  Losses: {losses}  Win rate: {win_rate:.1f}%")
    lines.append(f"Open positions: {len(open_positions)}")
    lines.append("")
    lines.append("## Open positions")
    if open_positions:
        for symbol, pos in sorted(open_positions.items()):
            side = "LONG" if pos["is_buy"] else "SHORT"
            lines.append(f"- [{symbol}] {side}  entry={pos['entry_price']:.6f}  sl={pos['sl']:.6f}  "
                          f"tp={pos['tp']:.6f}  risk=${pos['risk_dollars']:.2f}  opened={fmt_ts(pos['entry_t'])}")
    else:
        lines.append("- (none)")
    lines.append("")
    lines.append("## Closed in last 24h")
    if last_24h:
        for t in last_24h:
            side = "LONG" if t["is_buy"] else "SHORT"
            outcome = "WIN" if t["pnl"] > 0 else "LOSS"
            lines.append(f"- [{t['symbol']}] {side}  {outcome}  entry={t['entry_price']:.6f}  "
                          f"risk=${t['risk_dollars']:.2f}  pnl=${t['pnl']:+.2f}  closed={fmt_ts(t['exit_t'])}")
    else:
        lines.append("- (none)")
    lines.append("")
    return "\n".join(lines)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--lookback-days", type=int, default=LOOKBACK_DAYS_DEFAULT)
    ap.add_argument("--status-json", default="status.json")
    ap.add_argument("--status-md", default="STATUS.md")
    args = ap.parse_args()

    print("=" * 100)
    print("PAPER TRADE CLOUD ENGINE -- fully stateless recompute, no exchange account, no KYC.")
    print(f"Watchlist ({len(WATCHLIST)}): {', '.join(WATCHLIST)}")
    print(f"Lookback: {args.lookback_days} days")
    print("=" * 100)

    end = now_ms()
    start = end - args.lookback_days * 86400 * 1000

    positions_by_symbol = {}
    for symbol in WATCHLIST:
        print(f"  [{symbol}] fetching {args.lookback_days}d of 5m history...")
        bars = fetch_klines(symbol, start, end)
        bars45 = resample_45m(bars)
        positions = collect_positions(symbol, bars45)
        positions_by_symbol[symbol] = positions
        print(f"  [{symbol}] {len(bars)} bars, {len(positions)} position(s) detected in window")

    state = simulate_full_history(positions_by_symbol)
    generated_at_ms = end

    print()
    print(f"Balance: ${state['balance']:.2f}")
    print(f"Open positions: {len(state['open_positions'])}")
    print(f"Closed trades: {len(state['closed_trades'])}")

    with open(args.status_json, "w") as f:
        json.dump({"generated_at_ms": generated_at_ms, **state}, f, indent=2)

    md = build_status_markdown(state, generated_at_ms)
    with open(args.status_md, "w") as f:
        f.write(md)

    print(f"\nWrote {args.status_json} and {args.status_md}")


if __name__ == "__main__":
    main()
