# MA 9/26 pullback strategy (OHLC4, 5m) - BACKTEST + FORWARD TEST. NO real money.
#   python mabot.py backtest 90   -> history test over the last 90 days (train/test split)
#   python mabot.py forward       -> replays the newest confirmed candles (run every 5 min)
# Data: OKX public candles (USDT perpetual swaps), top-5 coins.
import requests, time, json, os, csv, sys
from datetime import datetime, timezone

OKX = "https://www.okx.com/api/v5"
COINS = ["BTC", "ETH", "BNB", "XRP", "SOL"]
RRS = [1.0, 1.5, 2.0, 2.5, 3.0]
LEVS = [1, 3, 5, 10, 15]

CFG = dict(
    fast=9, slow=26, atr_n=14,
    atr_buf=0.1,         # stop = slow MA -/+ this many ATRs
    expire=30,           # candles to wait for the pullback after a cross
    cap=144,             # max candles in a trade (12h), then close at market
    warmup=60,           # ignore signals in the first candles of a window
    fee_pct=0.05,        # taker fee per side
    slip_pct=0.02,       # slippage per side (large caps)
    margin=20.0,         # fake dollars of own money per trade
    start_balance=100.0,
    maint_pct=0.5,       # maintenance margin for the liquidation check
)
FSTATE = "ma_fwd_state.json"
FLOG = "ma_fwd_trades.csv"


def get_json(url, params=None, tries=3):
    for i in range(tries):
        try:
            r = requests.get(url, params=params, timeout=25,
                             headers={"User-Agent": "paperbot"})
            if r.status_code == 200:
                return r.json()
            print("HTTP", r.status_code, url[-30:])
            if r.status_code in (429, 403):
                time.sleep(5)
        except Exception as e:
            print("net error:", str(e)[:80])
            time.sleep(3)
    return None


def parse(c):
    return dict(ts=int(c[0]) // 1000, o=float(c[1]), h=float(c[2]),
                l=float(c[3]), c=float(c[4]), v=float(c[5]))


def okx_candles(inst, limit=300):
    d = (get_json(OKX + "/market/candles", {"instId": inst, "bar": "5m",
                                              "limit": limit}) or {}).get("data") or []
    time.sleep(0.1)
    return [parse(c) for c in reversed(d) if len(c) >= 9 and c[8] == "1"]


def fetch_history(inst, days):
    need = days * 288
    out, after = [], None
    while len(out) < need:
        p = {"instId": inst, "bar": "5m", "limit": 100}
        if after:
            p["after"] = after
        d = (get_json(OKX + "/market/history-candles", p) or {}).get("data") or []
        time.sleep(0.15)
        if not d:
            break
        out.extend(parse(c) for c in d if len(c) >= 9 and c[8] == "1")
        after = d[-1][0]
    seen, rows = set(), []
    for c in sorted(out, key=lambda x: x["ts"]):
        if c["ts"] not in seen:
            seen.add(c["ts"])
            rows.append(c)
    return rows[-need:]


# ---------- indicators and signals ----------
def sma(vals, n):
    out, run = [None] * len(vals), 0.0
    for i, v in enumerate(vals):
        run += v
        if i >= n:
            run -= vals[i - n]
        if i >= n - 1:
            out[i] = run / n
    return out


def atr(cs, n):
    out = [None] * len(cs)
    tr = [None] + [max(cs[i]["h"] - cs[i]["l"], abs(cs[i]["h"] - cs[i - 1]["c"]),
                       abs(cs[i]["l"] - cs[i - 1]["c"])) for i in range(1, len(cs))]
    if len(cs) <= n:
        return out
    a = sum(tr[1:n + 1]) / n
    out[n] = a
    for i in range(n + 1, len(cs)):
        a = (a * (n - 1) + tr[i]) / n
        out[i] = a
    return out


def find_signals(cs):
    o4 = [(c["o"] + c["h"] + c["l"] + c["c"]) / 4 for c in cs]
    f, s, a = sma(o4, CFG["fast"]), sma(o4, CFG["slow"]), atr(cs, CFG["atr_n"])
    sigs, d, ci, left = [], 0, 0, False
    for i in range(1, len(cs)):
        if None in (f[i], s[i], f[i - 1], s[i - 1], a[i]):
            continue
        if f[i - 1] <= s[i - 1] and f[i] > s[i]:
            d, ci, left = 1, i, False
            continue
        if f[i - 1] >= s[i - 1] and f[i] < s[i]:
            d, ci, left = -1, i, False
            continue
        if d == 0:
            continue
        if i - ci > CFG["expire"] or (d == 1 and f[i] <= s[i]) or (d == -1 and f[i] >= s[i]):
            d = 0
            continue
        c = cs[i]
        buf = CFG["atr_buf"] * a[i]
        if d == 1:
            if not left:
                left = c["l"] > f[i]
            elif c["c"] <= s[i]:
                d = 0
            elif c["l"] <= f[i]:
                sigs.append(dict(i=i, side=1, sl=s[i] - buf))
                d = 0
        else:
            if not left:
                left = c["h"] < f[i]
            elif c["c"] >= s[i]:
                d = 0
            elif c["h"] >= f[i]:
                sigs.append(dict(i=i, side=-1, sl=s[i] + buf))
                d = 0
    return sigs


def simulate(cs, e, side, sl, rr):
    entry = cs[e]["o"]
    risk = abs(entry - sl)
    if risk <= 0 or (side == 1 and sl >= entry) or (side == -1 and sl <= entry):
        return None
    tp = entry + side * rr * risk
    last = min(e + CFG["cap"], len(cs) - 1)
    ex, reason, xi = None, None, None
    for k in range(e, last + 1):
        c = cs[k]
        if k > e:
            if (side == 1 and c["o"] <= sl) or (side == -1 and c["o"] >= sl):
                ex, reason, xi = c["o"], "SL", k
                break
            if (side == 1 and c["o"] >= tp) or (side == -1 and c["o"] <= tp):
                ex, reason, xi = c["o"], "TP", k
                break
        hit_sl = c["l"] <= sl if side == 1 else c["h"] >= sl
        hit_tp = c["h"] >= tp if side == 1 else c["l"] <= tp
        if hit_sl:
            ex, reason, xi = sl, "SL", k
            break
        if hit_tp:
            ex, reason, xi = tp, "TP", k
            break
        if k == e + CFG["cap"]:
            ex, reason, xi = c["c"], "TIME", k
            break
    if ex is None:
        return {"open": True}
    cost = 2 * (CFG["fee_pct"] + CFG["slip_pct"])
    gross = side * (ex / entry - 1) * 100
    risk_pct = risk / entry * 100
    return dict(entry=entry, sl=sl, tp=tp, exit=ex, reason=reason, exit_idx=xi,
                risk_pct=risk_pct, gross_pct=gross, net_pct=gross - cost,
                net_R=(gross - cost) / risk_pct)


def lev_pnl(margin, lev, net_pct, risk_pct):
    if lev > 1 and risk_pct >= 100.0 / lev - CFG["maint_pct"]:
        return -margin
    return max(margin * lev * net_pct / 100, -margin)


# ---------- backtest ----------
def build_trades(data, min_risk=0.0):
    trades = {rr: [] for rr in RRS}
    for coin, cs in data.items():
        sigs = find_signals(cs)
        for rr in RRS:
            busy = -1
            for sg in sigs:
                e = sg["i"] + 1
                if e >= len(cs) or e <= busy or sg["i"] < CFG["warmup"]:
                    continue
                if abs(cs[e]["o"] - sg["sl"]) / cs[e]["o"] * 100 < min_risk:
                    continue
                t = simulate(cs, e, sg["side"], sg["sl"], rr)
                if t is None or t.get("open"):
                    continue
                busy = t["exit_idx"]
                t.update(coin=coin, rr=rr, side=sg["side"], entry_ts=cs[e]["ts"],
                         exit_ts=cs[t["exit_idx"]]["ts"])
                trades[rr].append(t)
    return trades


def equity(ts, lev):
    cash, peak, maxdd, taken, wins = CFG["start_balance"], CFG["start_balance"], 0.0, 0, 0
    m = CFG["margin"]
    opened = []

    def settle(upto):
        nonlocal cash, peak, maxdd, opened
        opened.sort(key=lambda x: x[0])
        keep, closed = [], 0
        total = len(opened)
        for ex, pnl in opened:
            if ex <= upto:
                cash += m + pnl
                closed += 1
                eq = cash + m * (total - closed)
                peak = max(peak, eq)
                maxdd = max(maxdd, (peak - eq) / peak * 100)
            else:
                keep.append((ex, pnl))
        opened = keep

    for t in sorted(ts, key=lambda x: x["entry_ts"]):
        settle(t["entry_ts"])
        if cash >= m:
            cash -= m
            pnl = lev_pnl(m, lev, t["net_pct"], t["risk_pct"])
            opened.append((t["exit_ts"], pnl))
            taken += 1
            wins += 1 if pnl > 0 else 0
    settle(10 ** 12)
    return dict(final=cash, ret=(cash / CFG["start_balance"] - 1) * 100,
                dd=maxdd, taken=taken, wins=wins)


def report(trades, label, lo, hi, out_rows):
    print("=== %s ===" % label)
    print("RR  trades win%  avg_net%/trade  PF  avg_stop%  | return% (maxDD%) by leverage")
    for rr in RRS:
        ts = [t for t in trades[rr] if lo <= t["entry_ts"] < hi]
        n = len(ts)
        if n == 0:
            print("%-3s no trades" % rr)
            continue
        w = sum(1 for t in ts if t["net_pct"] > 0)
        gp = sum(t["net_pct"] for t in ts if t["net_pct"] > 0)
        gl = -sum(t["net_pct"] for t in ts if t["net_pct"] <= 0)
        avg = sum(t["net_pct"] for t in ts) / n
        pf = gp / gl if gl else 99.0
        ar = sum(t["risk_pct"] for t in ts) / n
        expr = sum(t["net_R"] for t in ts) / n
        parts = []
        for lev in LEVS:
            q = equity(ts, lev)
            parts.append("x%d %+.0f%%(%.0f)" % (lev, q["ret"], q["dd"]))
            out_rows.append([label, rr, lev, n, round(100 * w / n, 1), round(avg, 4),
                             round(expr, 3), round(pf, 2), round(ar, 3),
                             round(q["final"], 2), round(q["ret"], 1), round(q["dd"], 1)])
        print("%-3s %5d %4.0f%%  %+.3f%%       %4.2f  %.2f%%  | %s" % (
            rr, n, 100 * w / n, avg, pf, ar, " ".join(parts)))


def backtest(days):
    data = {}
    for coin in COINS:
        cs = fetch_history(coin + "-USDT-SWAP", days)
        print("%s: %d candles" % (coin, len(cs)))
        if len(cs) < 500:
            print("not enough data for", coin)
            return
        data[coin] = cs
    run_backtest(data)


def run_backtest(data):
    t0 = min(c[0]["ts"] for c in data.values())
    t1 = max(c[-1]["ts"] for c in data.values())
    mid = (t0 + t1) // 2
    cost = 2 * (CFG["fee_pct"] + CFG["slip_pct"])
    print("period %s -> %s | cost per trade %.2f%% of position | $%.0f margin, start $%.0f" % (
        datetime.fromtimestamp(t0, timezone.utc).strftime("%Y-%m-%d"),
        datetime.fromtimestamp(t1, timezone.utc).strftime("%Y-%m-%d"), cost,
        CFG["margin"], CFG["start_balance"]))
    rows, alltrades = [], []
    for name, min_risk in (("ALL", 0.0), ("STOP>=0.25%", 0.25)):
        trades = build_trades(data, min_risk)
        report(trades, "TRAIN " + name, t0, mid, rows)
        report(trades, "TEST " + name, mid, t1 + 1, rows)
        if min_risk == 0.0:
            alltrades = trades
    with open("ma_backtest_summary.csv", "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["period_filter", "rr", "lev", "trades", "win_pct", "avg_net_pct",
                    "exp_R", "pf", "avg_stop_pct", "final_usd", "return_pct", "max_dd_pct"])
        w.writerows(rows)
    with open("ma_backtest_trades.csv", "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["coin", "rr", "side", "entry_time", "exit_time", "entry", "sl",
                    "tp", "exit", "reason", "stop_pct", "net_pct", "net_R"])
        for rr in RRS:
            for t in alltrades[rr]:
                w.writerow([t["coin"], rr, "LONG" if t["side"] > 0 else "SHORT",
                            datetime.fromtimestamp(t["entry_ts"], timezone.utc).strftime("%m-%d %H:%M"),
                            datetime.fromtimestamp(t["exit_ts"], timezone.utc).strftime("%m-%d %H:%M"),
                            t["entry"], round(t["sl"], 6), round(t["tp"], 6), t["exit"],
                            t["reason"], round(t["risk_pct"], 3), round(t["net_pct"], 3),
                            round(t["net_R"], 3)])


# ---------- forward test ----------
def log_fwd(coin, rr, side, t):
    new = not os.path.exists(FLOG)
    with open(FLOG, "a", newline="") as f:
        w = csv.writer(f)
        if new:
            w.writerow(["entry_time", "coin", "side", "rr", "entry", "sl", "tp", "exit",
                        "reason", "risk_pct", "net_R"] + ["pnl_x%d" % l for l in LEVS])
        w.writerow([datetime.fromtimestamp(t["entry_ts"], timezone.utc).strftime("%m-%d %H:%M"),
                    coin, "LONG" if side > 0 else "SHORT", rr, t["entry"], round(t["sl"], 6),
                    round(t["tp"], 6), t["exit"], t["reason"], round(t["risk_pct"], 3),
                    round(t["net_R"], 3)] +
                   [round(lev_pnl(CFG["margin"], l, t["net_pct"], t["risk_pct"]), 3) for l in LEVS])
    print("CLOSED %s %s rr%s %s netR %+.2f" % (coin, "LONG" if side > 0 else "SHORT",
                                              rr, t["reason"], t["net_R"]))


def fwd_stats():
    if not os.path.exists(FLOG):
        return
    rows = list(csv.reader(open(FLOG)))[1:]
    print("FORWARD RESULTS (each trade $%.0f margin; leverage columns)" % CFG["margin"])
    for rr in RRS:
        r = [x for x in rows if float(x[3]) == rr]
        if not r:
            continue
        w = sum(1 for x in r if float(x[10]) > 0)
        pn = [sum(float(x[11 + i]) for x in r) for i in range(len(LEVS))]
        print("RR %s: trades=%d wins=%d sumR=%+.2f | $ " % (rr, len(r), w, sum(float(x[10]) for x in r)) +
              " ".join("x%d:%+.2f" % (l, p) for l, p in zip(LEVS, pn)))


def forward():
    s = {"last": {}, "pending": []}
    if os.path.exists(FSTATE):
        s = json.load(open(FSTATE))
    for coin in COINS:
        cs = okx_candles(coin + "-USDT-SWAP", 300)
        if len(cs) < 120:
            print("no data for", coin)
            continue
        idx = {c["ts"]: k for k, c in enumerate(cs)}
        keep = []
        for p in s["pending"]:
            if p["coin"] != coin:
                keep.append(p)
                continue
            e = idx.get(p["entry_ts"])
            t = simulate(cs, e, p["side"], p["sl"], p["rr"]) if e is not None else None
            if t is None:
                print("dropped pending", coin, p["entry_ts"])
            elif t.get("open"):
                keep.append(p)
            else:
                t["entry_ts"] = p["entry_ts"]
                log_fwd(coin, p["rr"], p["side"], t)
        s["pending"] = keep
        prev = s["last"].get(coin)
        busy = {}
        if prev is not None:
            for sg in find_signals(cs):
                e = sg["i"] + 1
                if e >= len(cs) or sg["i"] < CFG["warmup"] or cs[e]["ts"] <= prev:
                    continue
                for rr in RRS:
                    if any(p["coin"] == coin and p["rr"] == rr for p in s["pending"]):
                        continue
                    if cs[e]["ts"] <= busy.get(rr, 0):
                        continue
                    t = simulate(cs, e, sg["side"], sg["sl"], rr)
                    if t is None:
                        continue
                    t["entry_ts"] = cs[e]["ts"]
                    if t.get("open"):
                        s["pending"].append(dict(coin=coin, rr=rr, side=sg["side"],
                                                 sl=sg["sl"], entry_ts=cs[e]["ts"]))
                        print("OPEN  %s %s rr%s @ %s sl %.6g" % (
                            coin, "LONG" if sg["side"] > 0 else "SHORT", rr, t.get("entry", cs[e]["o"]), sg["sl"]))
                    else:
                        busy[rr] = cs[t["exit_idx"]]["ts"]
                        log_fwd(coin, rr, sg["side"], t)
        s["last"][coin] = cs[-1]["ts"]
    json.dump(s, open(FSTATE, "w"))
    print("pending:", len(s["pending"]))
    fwd_stats()


if __name__ == "__main__":
    mode = sys.argv[1] if len(sys.argv) > 1 else "forward"
    if mode == "backtest":
        backtest(int(sys.argv[2]) if len(sys.argv) > 2 else 90)
    else:
        forward()
