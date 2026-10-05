"""
Trade Desk alert bot.  Runs on GitHub Actions every hour, needs no browser open.
Sends BUY / EXIT / WIN / LOSS messages to Telegram and/or WhatsApp.

Rules (identical to the website):
  Bitcoin trend     4h close crosses ABOVE EMA-100 AND daily close > daily EMA-50  -> BUY. Exit on 4h close below EMA-100 or 5% stop.
  Nasdaq trend      same rule on NDX (needs TWELVEDATA_KEY), 3% stop.
  Ethereum surge    daily RSI(2) > 90 AND close > SMA-50 AND Bitcoin > daily SMA-100 -> BUY. Exit after 5 days or 10% stop.
  Small-coin surge  same on XRP ADA DOGE XLM, 7% stop.
  Bitcoin surge     4h RSI(2) > 90 AND close > SMA-300 -> BUY. Exit after 5 days or 10% stop.
  Follow-through    Bitcoin daily candle >= +4% AND Bitcoin > SMA-100 -> BUY the coin basket. Exit after 3 days or 7% stop.
  Nasdaq dip        4h close in the bottom 30% of its 20-candle range AND daily close > daily EMA-50 -> BUY. Exit 24h later or 2% stop.
  Ethereum quiet    daily ATR14/price in the lowest 13% of 100 days AND Bitcoin > SMA-100 -> BUY. Exit after 5 days or 10% stop.
  Gold window       reminder 15 min before the US reopen, Oct-Feb, Tue-Fri.
  Fed days          reminder 3 days before and the evening before.

Secrets (repo Settings -> Secrets and variables -> Actions):
  TELEGRAM_TOKEN, TELEGRAM_CHAT_ID         (optional)
  WHATSAPP_PHONE, WHATSAPP_APIKEY          (optional, CallMeBot)
  TWELVEDATA_KEY                           (optional, Nasdaq)
"""
import json, os, sys, time, urllib.request, urllib.parse, datetime as dt

HERE = os.path.dirname(os.path.abspath(__file__))
STATE = os.path.join(HERE, "state.json")
TG_TOKEN = os.environ.get("TELEGRAM_TOKEN", ""); TG_CHAT = os.environ.get("TELEGRAM_CHAT_ID", "")
WA_PHONE = os.environ.get("WHATSAPP_PHONE", ""); WA_KEY = os.environ.get("WHATSAPP_APIKEY", "")
TD = os.environ.get("TWELVEDATA_KEY", "")
FOMC = ["2026-10-28","2026-12-09","2027-01-27","2027-03-17","2027-04-28","2027-06-09","2027-07-28","2027-09-15","2027-10-27","2027-12-08"]
COINS = {"ETH":"ETHUSDT","XRP":"XRPUSDT","ADA":"ADAUSDT","DOGE":"DOGEUSDT","XLM":"XLMUSDT"}   # basket + surge coins
ONLY = os.environ.get("ONLY_COINS", "")   # e.g. "ETH" for FundingPips (no small coins)
if ONLY: COINS = {k:v for k,v in COINS.items() if k in ONLY.split(",")}

# ---------------- messaging ----------------
def _get(url, timeout=30):
    req = urllib.request.Request(url, headers={"User-Agent": "trade-desk-bot"})
    with urllib.request.urlopen(req, timeout=timeout) as r: return r.read().decode()
def send(msg):
    ok = False
    if TG_TOKEN and TG_CHAT:
        try:
            data = urllib.parse.urlencode({"chat_id": TG_CHAT, "text": msg, "parse_mode": "HTML"}).encode()
            urllib.request.urlopen("https://api.telegram.org/bot%s/sendMessage" % TG_TOKEN, data=data, timeout=30).read(); ok = True
        except Exception as e: print("telegram failed:", e)
    if WA_PHONE and WA_KEY:
        try:
            plain = msg.replace("<b>","*").replace("</b>","*")
            _get("https://api.callmebot.com/whatsapp.php?phone=%s&text=%s&apikey=%s" % (urllib.parse.quote(WA_PHONE), urllib.parse.quote(plain), urllib.parse.quote(WA_KEY))); ok = True
        except Exception as e: print("whatsapp failed:", e)
    print(("SENT " if ok else "NOT SENT (no channel) ") + msg.replace("\n", " | "))

# ---------------- data (Binance -> Binance.US -> Coinbase) ----------------
def _binance(host, sym, iv, n):
    j = json.loads(_get(f"https://{host}/api/v3/klines?symbol={sym}&interval={iv}&limit={n+1}"))
    now = time.time()*1000
    return [(k[0]/1000.0, float(k[4]), float(k[3]), float(k[2])) for k in j if k[6] < now]   # (open_time, close, low, high) closed candles only
def _coinbase(sym, iv, n):
    prod = sym.replace("USDT", "-USD"); gran = {"4h":14400, "1d":86400}[iv]; out = []; end = int(time.time()//gran*gran)
    while len(out) < n+1:
        start = end - 300*gran
        j = json.loads(_get(f"https://api.exchange.coinbase.com/products/{prod}/candles?granularity={gran}&start={dt.datetime.utcfromtimestamp(start).isoformat()}&end={dt.datetime.utcfromtimestamp(end).isoformat()}"))
        if not j: break
        out = [(c[0], float(c[4]), float(c[1]), float(c[2])) for c in j] + out; end = start
    out.sort(); now = time.time()
    return [c for c in out if c[0]+gran < now][-n:]
def candles(sym, iv, n):
    for f in (lambda: _binance("api.binance.com", sym, iv, n), lambda: _binance("api.binance.us", sym, iv, n), lambda: _coinbase(sym, iv, n)):
        try:
            c = f()
            if len(c) >= n*0.9: return c
        except Exception as e: print("source failed:", e)
    raise RuntimeError("no data for " + sym)
def twelve(sym, iv, n):
    if not TD: return None
    j = json.loads(_get(f"https://api.twelvedata.com/time_series?symbol={urllib.parse.quote(sym)}&interval={iv}&outputsize={n}&timezone=UTC&apikey={TD}"))
    v = j.get("values") or []
    return [(dt.datetime.fromisoformat(x["datetime"]).replace(tzinfo=dt.timezone.utc).timestamp(), float(x["close"]), float(x["low"]), float(x["high"])) for x in reversed(v)]

# ---------------- indicators ----------------
def ema(a, n):
    k = 2/(n+1); e = a[0]; out = [e]
    for x in a[1:]: e = x*k + e*(1-k); out.append(e)
    return out
def sma(a, n): return [sum(a[i-n+1:i+1])/n if i >= n-1 else float("nan") for i in range(len(a))]
def rsi(a, n):
    g = l = 0.0; out = [float("nan")]
    for i in range(1, len(a)):
        d = a[i]-a[i-1]; up = max(d, 0); dn = max(-d, 0)
        if i <= n:
            g += up/n; l += dn/n; out.append((100-100/(1+g/l) if l else 100) if i == n else float("nan"))
        else:
            g = (g*(n-1)+up)/n; l = (l*(n-1)+dn)/n; out.append(100-100/(1+g/l) if l else 100)
    return out
def fmt(p): return f"{p:,.4f}" if p < 2 else f"{p:,.2f}" if p < 100 else f"{p:,.0f}"

# ---------------- main ----------------
def main():
    st = json.load(open(STATE)) if os.path.exists(STATE) else {}
    st.setdefault("sent", {}); st.setdefault("open", [])
    now = dt.datetime.now(dt.timezone.utc); ist = now + dt.timedelta(hours=5, minutes=30); nowts = now.timestamp()
    msgs = []
    def once(key, text):            # send a given alert only once
        if key in st["sent"]: return
        st["sent"][key] = ist.strftime("%Y-%m-%d %H:%M"); msgs.append(text)
    def open_trade(k, name, sym, entry, stop_pct, hold_days, kind):
        if any(o["k"] == k for o in st["open"]): return
        st["open"].append({"k": k, "name": name, "sym": sym, "entry": entry, "stop": entry*(1-stop_pct), "opened": nowts, "hold": hold_days, "kind": kind})

    # --- daily Bitcoin context ---
    try:
        bd = candles("BTCUSDT", "1d", 130); bc = [x[1] for x in bd]
        btc_up50 = bc[-1] > ema(bc, 50)[-1]; btc_up100 = bc[-1] > sma(bc, 100)[-1]; btc_day = bc[-1]/bc[-2]-1; btc_day_t = bd[-1][0]
    except Exception as e:
        print("btc daily failed:", e); btc_up50 = btc_up100 = True; btc_day = 0.0; btc_day_t = 0

    # --- Bitcoin trend (4h, gated) ---
    try:
        k = candles("BTCUSDT", "4h", 420); c = [x[1] for x in k]; e = ema(c, 100); t = k[-1][0]
        cross_up = c[-1] > e[-1] and c[-2] <= e[-2]; cross_dn = c[-1] < e[-1] and c[-2] >= e[-2]
        if cross_up:
            if btc_up50:
                once(f"btcTrend:buy:{t}", f"🟢 <b>BUY Bitcoin trend</b>\n4h closed above EMA-100 at {fmt(c[-1])} · daily filter ✓\nStop 5% = {fmt(c[-1]*0.95)} · exit when a 4h candle closes below EMA-100")
                open_trade("btcTrend", "Bitcoin trend", "BTC", c[-1], 0.05, 999, "trend")
            else:
                once(f"btcTrend:skip:{t}", "⚪ Bitcoin trend crossed up but the daily close is below the daily EMA-50 → skip (filter).")
        for o in [o for o in st["open"] if o["k"] == "btcTrend"]:
            o["_px"] = c[-1]; o["_rule_exit"] = c[-1] < e[-1]
    except Exception as e: print("btcTrend:", e)

    # --- Nasdaq trend (4h, gated; needs Twelve Data) ---
    try:
        k = twelve("NDX", "4h", 420); d = twelve("NDX", "1day", 80)
        if k and d:
            c = [x[1] for x in k]; e = ema(c, 100); dc = [x[1] for x in d]; up50 = dc[-1] > ema(dc, 50)[-1]; t = k[-1][0]
            if c[-1] > e[-1] and c[-2] <= e[-2]:
                if up50:
                    once(f"nasTrend:buy:{t}", f"🟢 <b>BUY Nasdaq trend</b>\n4h closed above EMA-100 at {fmt(c[-1])} · daily filter ✓\nStop 3% = {fmt(c[-1]*0.97)} · exit on a 4h close below EMA-100")
                    open_trade("nasTrend", "Nasdaq trend", "NDX", c[-1], 0.03, 999, "trend")
                else: once(f"nasTrend:skip:{t}", "⚪ Nasdaq trend crossed up but the daily close is below the daily EMA-50 → skip.")
            for o in [o for o in st["open"] if o["k"] == "nasTrend"]:
                o["_px"] = c[-1]; o["_rule_exit"] = c[-1] < e[-1]
    except Exception as e: print("nasTrend:", e)

    # --- Nasdaq dip (4h, needs Twelve Data) ---
    try:
        k = twelve("NDX", "4h", 60); d = twelve("NDX", "1day", 80)
        if k and d:
            c = [x[1] for x in k]; lo = min(x[2] for x in k[-20:]); hi = max(x[3] for x in k[-20:]); pos = (c[-1]-lo)/max(1e-9, hi-lo)
            dc = [x[1] for x in d]; up50 = dc[-1] > ema(dc, 50)[-1]; t = k[-1][0]
            if pos < 0.30 and up50 and not any(o["k"] == "nasDip" for o in st["open"]):
                once(f"nasDip:buy:{t}", f"🟢 <b>BUY Nasdaq dip</b>\n4h closed at {pos*100:.0f}% of its 20-candle range, daily trend up ✓ · entry ≈ {fmt(c[-1])}\nStop 2% = {fmt(c[-1]*0.98)} · exit 6 four-hour candles later (24 h)")
                open_trade("nasDip", "Nasdaq dip", "NDX", c[-1], 0.02, 1, "time")
            for o in [o for o in st["open"] if o["k"] == "nasDip"]: o["_px"] = c[-1]
    except Exception as e: print("nasDip:", e)

    # --- Ethereum quiet buy (daily) ---
    try:
        k = candles("ETHUSDT", "1d", 130); tr = [k[0][3]-k[0][2]] + [max(x[3]-x[2], abs(x[3]-k[j-1][1]), abs(x[2]-k[j-1][1])) for j, x in enumerate(k) if j]
        atrp = [sum(tr[j-13:j+1])/14/k[j][1] if j >= 13 else None for j in range(len(k))]; win = [a for a in atrp[-100:] if a is not None]
        rank = sum(1 for a in win if a < atrp[-1])/max(1, len(win)); t = k[-1][0]
        if rank < 0.13 and btc_up100 and not any(o["k"] == "ethQuiet" for o in st["open"]):
            once(f"ethQuiet:{t}", f"🟢 <b>BUY Ethereum quiet</b>\nVolatility rank {rank*100:.0f}% (lowest 13%), Bitcoin filter ✓ · entry ≈ {fmt(k[-1][1])}\nStop 10% = {fmt(k[-1][1]*0.90)} · exit 5 daily closes later (5:30 am)")
            open_trade("ethQuiet", "Ethereum quiet", "ETH", k[-1][1], 0.10, 5, "time")
        for o in [o for o in st["open"] if o["k"] == "ethQuiet"]: o["_px"] = k[-1][1]
    except Exception as e: print("ethQuiet:", e)

    # --- Surge: ETH + small coins (daily, gated by Bitcoin > SMA-100) ---
    for name, sym in COINS.items():
        try:
            k = candles(sym, "1d", 80); c = [x[1] for x in k]; r = rsi(c, 2)[-1]; m = sma(c, 50)[-1]; t = k[-1][0]
            if r > 90 and c[-1] > m:
                if btc_up100:
                    sl = 0.10 if name == "ETH" else 0.07
                    once(f"surge:{name}:{t}", f"🚀 <b>BUY {name} surge</b>\nRSI(2) {r:.0f}, above SMA-50, Bitcoin filter ✓ · entry ≈ {fmt(c[-1])}\nStop {int(sl*100)}% = {fmt(c[-1]*(1-sl))} · exit 5 daily closes later (5:30 am)")
                    open_trade(f"surge:{name}", f"{name} surge", name, c[-1], sl, 5, "time")
                else: once(f"surge:{name}:skip:{t}", f"⚪ {name} surge fired but Bitcoin is below its 100-day average → skip.")
            for o in [o for o in st["open"] if o["k"] in (f"surge:{name}", f"follow:{name}")]: o["_px"] = c[-1]
        except Exception as e: print("surge", name, e)

    # --- Bitcoin surge (4h) ---
    try:
        k = candles("BTCUSDT", "4h", 340); c = [x[1] for x in k]; r = rsi(c, 2)[-1]; m = sma(c, 300)[-1]; t = k[-1][0]
        if r > 90 and c[-1] > m:
            once(f"btcMom:{t}", f"🚀 <b>BUY Bitcoin surge</b>\n4h RSI(2) {r:.0f}, above SMA-300 · entry ≈ {fmt(c[-1])}\nStop 10% = {fmt(c[-1]*0.90)} · exit after 30 four-hour candles (5 days). Half size if Bitcoin trend is open.")
            open_trade("btcMom", "Bitcoin surge", "BTC", c[-1], 0.10, 5, "time")
        for o in [o for o in st["open"] if o["k"] == "btcMom"]: o["_px"] = c[-1]
    except Exception as e: print("btcMom:", e)

    # --- Bitcoin follow-through basket (daily) ---
    try:
        if btc_day >= 0.04 and btc_up100 and btc_day_t:
            coins = ", ".join(COINS.keys())
            once(f"follow:{btc_day_t}", f"🟢 <b>BUY the coin basket — Bitcoin follow-through</b>\nBitcoin closed {btc_day*100:+.1f}% on the day, above SMA-100 ✓\nBuy {coins} at this open, ONE unit of risk split equally · stop 7% each · exit 3 daily closes later (5:30 am)")
            for name, sym in COINS.items():
                try:
                    k = candles(sym, "1d", 5); open_trade(f"follow:{name}", f"Follow-through {name}", name, k[-1][1], 0.07, 3, "time")
                except Exception as e: print("follow", name, e)
        elif btc_day >= 0.04 and btc_day_t:
            once(f"follow:skip:{btc_day_t}", f"⚪ Bitcoin closed {btc_day*100:+.1f}% but is below its 100-day average → no follow-through trade.")
    except Exception as e: print("follow:", e)

    # --- exits / outcomes for tracked trades ---
    still = []
    for o in st["open"]:
        px = o.pop("_px", None); rule = o.pop("_rule_exit", False)
        if px is None: still.append(o); continue
        held = (nowts - o["opened"])/86400.0; why = None
        if px <= o["stop"]: why = "stop hit"
        elif o["kind"] == "trend" and rule: why = "closed below EMA-100"
        elif o["kind"] == "time" and held >= o["hold"]: why = f"{o['hold']} days up"
        if why:
            pct = (px/o["entry"]-1)*100
            msgs.append(f"{'✅ WIN' if pct >= 0 else '❌ LOSS'} <b>{o['name']}</b> {pct:+.1f}% — {why}. EXIT now at ≈ {fmt(px)}.")
        else: still.append(o)
    st["open"] = still

    # --- gold window reminder (winter, Tue–Fri), 15 min before the US reopen ---
    m = ist.month
    if (m >= 10 or m <= 2) and ist.weekday() in (1, 2, 3, 4):
        y = now.year
        dst_start = dt.datetime(y, 3, 8 + (6 - dt.datetime(y, 3, 8).weekday()) % 7, 7, tzinfo=dt.timezone.utc)
        dst_end = dt.datetime(y, 11, 1 + (6 - dt.datetime(y, 11, 1).weekday()) % 7, 6, tzinfo=dt.timezone.utc)
        dst = dst_start <= now < dst_end
        win = "3:30–5:30 am" if dst else "4:30–6:30 am"; open_utc_h = 22 if dst else 23
        if (now.hour == open_utc_h - 1 and now.minute >= 40) or (now.hour == open_utc_h and now.minute < 20):
            once("gold:" + ist.strftime("%Y-%m-%d"), f"🥇 <b>Gold window opens soon</b> — buy inside {win} IST, sell at 12:30 pm, stop 1.5%.")

    # --- Fed reminders ---
    for d in FOMC:
        days = (dt.date.fromisoformat(d) - ist.date()).days
        if days == 3: once("fomc3:" + d, f"🏛 Fed statement in 3 days ({d}). Fed-day drift: buy Nasdaq the night before, sell before the statement.")
        if days == 1 and ist.hour >= 19: once("fomc1:" + d, f"🏛 Fed tonight → buy Nasdaq at {'1:30 am' if 3 <= int(d[5:7]) <= 10 else '2:30 am'} IST, sell at {'11:30 pm' if 3 <= int(d[5:7]) <= 10 else '12:30 am'} IST. Skip if Nasdaq trend is already open.")

    for x in msgs: send(x)
    st["last_run"] = ist.strftime("%Y-%m-%d")   # date only -> one heartbeat commit per day keeps the schedule alive
    if len(st["sent"]) > 2000: st["sent"] = dict(list(st["sent"].items())[-1500:])
    json.dump(st, open(STATE, "w"), indent=1)
    print(f"ok · {len(msgs)} alert(s) · {len(st['open'])} tracked trade(s) · {st['last_run']}")

if __name__ == "__main__": main()
