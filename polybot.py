#!/usr/bin/env python3
"""polybot: scan Polymarket match markets for the "50-50 forfeit" mispricing and trade it (paper by default).

The idea (from the video): many match markets say a cancelled / forfeited match resolves 50-50, so every share
pays $0.50. If a forfeit is likely but prices still show a favourite, the cheap side (<50c) is under-priced: it
pays 50c on a 50-50 however low its price, while the expensive side loses.
"""
import argparse, json, math, os, re, sqlite3, sys, time
from dataclasses import dataclass, field, fields
from datetime import datetime, timedelta, timezone

import httpx

GAMMA, CLOB = "https://gamma-api.polymarket.com", "https://clob.polymarket.com"
GENERIC = {"yes", "no", "over", "under", "odd", "even"}          # prop markets, not team-vs-team
FIVE = re.compile(r"50\s*[-–—/]\s*50")
FORF = re.compile(r"forfeit|walkover|disqualif|withdr[ae]w|no[- ]show", re.I)
CANC = re.compile(r"cancel|not played|postpone|delayed|\btie\b|suspend", re.I)
NOTICE = re.compile(r"(has|have|had)\s+(withdrawn|forfeited|been disqualified)|withdrew|will forfeit"
                    r"|unable to (attend|play|participate)|visa (issues|problems)|disband", re.I)
SENT = re.compile(r"(?<=[.!?])\s+|\n+")
SCHEMA = """create table if not exists trades(id integer primary key, ts text, mode text, market_id text, slug text,
question text, token text, outcome text, idx integer, price real, shares real, usd real, q real, edge real, why text,
status text default 'open', payout real, pnl real, final_5050 integer)"""


@dataclass
class Cfg:
    tag: str = "esports"           # Gamma tag slug to scan
    pages: int = 8                 # pages of 100 events per scan
    interval: int = 120            # seconds between cycles in `run`
    q_conflict: float = 0.35       # ASSUMED P(50-50) when a team has two overlapping matches
    q_notice: float = 0.40         # ASSUMED P(50-50) when the rules text carries a withdrawal/forfeit notice
    conflict_hours: float = 2.0    # matches starting closer together than this count as a conflict
    min_edge: float = 0.10         # required expected return on capital, after fees
    min_price: float = 0.02
    max_price: float = 0.45        # only the cheap side can win this trade
    fee_bps: float = 0.0           # flat taker-fee assumption: check Polymarket's current fee schedule
    bankroll: float = 200.0
    kelly_frac: float = 0.25
    max_trade: float = 10.0        # USD per position
    max_exposure: float = 50.0     # USD open at once
    db: str = "polybot.db"
    watchlist: str = "watchlist.json"   # {"Some Team": 0.8}: your own P(50-50) for a team


@dataclass
class Opp:
    market_id: str; event_id: str; slug: str; question: str
    outcomes: list; tokens: list; mids: list
    start: object; forfeit: bool; notice: bool
    q: float = 0.0
    why: list = field(default_factory=list)


# ---------- helpers ----------
def jl(x):
    """Gamma returns several list fields as JSON-encoded strings."""
    if isinstance(x, str):
        try: return json.loads(x)
        except ValueError: return []
    return x or []


def parse_ts(s):
    if not s: return None
    s = str(s).strip().replace(" ", "T")
    if s.endswith("Z"): s = s[:-1] + "+00:00"
    if "T" in s and re.search(r"[+-]\d\d$", s): s += ":00"
    try: d = datetime.fromisoformat(s)
    except ValueError: return None
    return d if d.tzinfo else d.replace(tzinfo=timezone.utc)


def norm(n): return re.sub(r"[^a-z0-9]+", "", str(n).lower())


def get(http, url, **params):
    err = ""
    for _ in range(2):
        try:
            r = http.get(url, params=params)
            if r.status_code == 200: return r.json()
            err = f"HTTP {r.status_code}: {r.text[:200]}"
        except httpx.HTTPError as e:
            err = str(e)
        time.sleep(1.5)
    raise RuntimeError(f"{url} failed: {err}")


# ---------- 1. collect ----------
def fetch_events(http, tag, pages, closed=False):
    params = dict(closed=str(closed).lower(), limit=100, order="endDate", ascending="false" if closed else "true")
    if not closed: params["active"] = "true"
    if tag: params["tag_slug"] = tag
    out = []
    for i in range(pages):
        page = get(http, f"{GAMMA}/events", offset=i * 100, **params)
        out += page
        if len(page) < 100: break
    return out


def fetch_book(http, token):
    try: js = get(http, f"{CLOB}/book", token_id=token)
    except RuntimeError: return [], 5.0
    asks = [(float(a["price"]), float(a["size"])) for a in js.get("asks", [])]
    return asks, float(js.get("min_order_size") or 5)


def classify_rules(desc):
    """(forfeit_means_5050, cancel_means_5050) from the market's rules text."""
    f = c = False
    for s in SENT.split(desc or ""):
        if FIVE.search(s):
            f |= bool(FORF.search(s)); c |= bool(CANC.search(s))
    return f, c


def has_notice(text):
    body = " ".join(s for s in SENT.split(text or "") if not re.search(r"resolve|50\s*[-–—/]\s*50", s, re.I))
    return bool(NOTICE.search(body))


def extract(ev, m):
    outs, toks, px = jl(m.get("outcomes")), jl(m.get("clobTokenIds")), jl(m.get("outcomePrices"))
    if len(outs) != 2 or len(toks) != 2 or len(px) != 2: return None
    if {str(o).lower() for o in outs} <= GENERIC: return None
    if m.get("closed") or m.get("active") is False or m.get("acceptingOrders") is False: return None
    desc = m.get("description") or ev.get("description") or ""
    forfeit, cancel = classify_rules(desc)
    if not (forfeit or cancel): return None
    start = parse_ts(m.get("gameStartTime") or ev.get("startTime") or m.get("endDate"))
    return Opp(str(m.get("id")), str(ev.get("id")), m.get("slug", ""), m.get("question", ""), outs, toks,
               [float(x) for x in px], start, forfeit, has_notice(desc))


# ---------- 2. analyse ----------
def find_conflicts(opps, hours):
    """Same team in two different matches starting within `hours` of each other (the FaZe case in the video)."""
    by_team, names, hits = {}, {}, {}
    for o in opps:
        if o.start:
            for n in o.outcomes:
                by_team.setdefault(norm(n), []).append(o); names[norm(n)] = n
    for team, lst in by_team.items():
        lst.sort(key=lambda o: o.start)
        for i, a in enumerate(lst):
            for b in lst[i + 1:]:
                if b.start - a.start >= timedelta(hours=hours): break
                same = {norm(x) for x in a.outcomes} == {norm(x) for x in b.outcomes}
                if not same and a.event_id != b.event_id:
                    for o in (a, b): hits.setdefault(o.market_id, set()).add(names[team])
    return hits


def analyse(opps, cfg, watch):
    conflicts = find_conflicts(opps, cfg.conflict_hours)
    for o in opps:
        sig = []
        if o.market_id in conflicts:
            sig.append((cfg.q_conflict, "schedule conflict: " + ", ".join(sorted(conflicts[o.market_id]))))
        if o.notice: sig.append((cfg.q_notice, "forfeit/withdrawal notice in rules"))
        for n in o.outcomes:
            if norm(n) in watch: sig.append((watch[norm(n)], f"watchlist: {n}"))
        if sig:   # cancel-only rule wording does not cover forfeits, so halve the probability
            o.q = max(q for q, _ in sig) * (1.0 if o.forfeit else 0.5)
            o.why = [w for _, w in sig]
    return [o for o in opps if o.q > 0]


def walk_asks(asks, usd):
    """Spend `usd` up the ask ladder -> (shares, avg_price, worst_price), or None if the book is too thin."""
    left, shares, worst = usd, 0.0, 0.0
    for p, s in sorted(asks):
        if left <= 1e-9: break
        take = min(s, left / p)
        shares += take; left -= take * p; worst = p
    return (shares, (usd - left) / shares, worst) if shares > 0 and left < 0.01 else None


def kelly(q, pi, a, frac):
    """Fractional Kelly for: 50-50 (pays 0.5), normal win (pays 1), normal loss (pays 0), at ask price a."""
    outcomes = [(q, (0.5 - a) / a), ((1 - q) * pi, (1 - a) / a), ((1 - q) * (1 - pi), -1.0)]
    best_g = best_f = 0.0
    for k in range(1, 1000):
        f = k / 1000
        g = sum(p * math.log(1 + f * r) for p, r in outcomes if p > 0)
        if g > best_g: best_g, best_f = g, f
    return best_f * frac


def evaluate(o, books, cfg, usd):
    """EV per share = q*0.5 + (1-q)*pi - ask. pi = market-implied win chance (the market ignores the 50-50 clause)."""
    tot, best = sum(o.mids) or 1.0, None
    for i in (0, 1):
        fill = walk_asks(books[o.tokens[i]][0], usd)
        if not fill: continue
        a = fill[1]
        if not cfg.min_price <= a <= cfg.max_price: continue
        pi = o.mids[i] / tot
        ev = o.q * 0.5 + (1 - o.q) * pi - a - a * cfg.fee_bps / 1e4
        if ev / a >= cfg.min_edge and (best is None or ev / a > best["edge"]):
            best = dict(i=i, price=a, shares=fill[0], edge=ev / a, pi=pi)
    return best


def plan(o, books, cfg, exposure):
    room = min(cfg.max_trade, cfg.max_exposure - exposure)
    if room < 1: return None
    c = evaluate(o, books, cfg, room)
    if not c: return None
    stake = min(room, cfg.bankroll * kelly(o.q, c["pi"], c["price"], cfg.kelly_frac))
    asks, min_size = books[o.tokens[c["i"]]]
    fill = walk_asks(asks, stake) if stake >= 1 else None
    if not fill or fill[0] < min_size: return None
    c.update(shares=fill[0], price=fill[1], usd=round(stake, 2))
    return c


# ---------- 3. execute ----------
class Paper:
    def buy(self, token, asks, usd):
        fill = walk_asks(asks, usd)
        return fill and fill[:2]


class Live:
    """Real orders via Polymarket's CLOB V2 client (pip install py-clob-client-v2). UNTESTED against the live exchange."""
    def __init__(self):
        try: from py_clob_client_v2 import ClobClient
        except ImportError: sys.exit("This build has no live-trading SDK. Build from source with build_exe.bat, "
                                     "or: pip install py-clob-client-v2")
        key, funder = os.environ["POLYMARKET_PRIVATE_KEY"], os.environ.get("POLYMARKET_FUNDER")
        sig = int(os.environ.get("POLYMARKET_SIG_TYPE", 2 if funder else 0))   # 0 EOA, 1 email proxy, 2 Safe proxy
        kw = dict(host=CLOB, chain_id=137, key=key, signature_type=sig, funder=funder)
        self.c = ClobClient(**kw)
        self.c = ClobClient(**kw, creds=self.c.create_or_derive_api_key())

    def buy(self, token, asks, usd):
        from py_clob_client_v2 import OrderArgs, OrderType, Side
        fill = walk_asks(asks, usd)
        if not fill: return None
        shares, avg, worst = fill
        tick = float(self.c.get_tick_size(token))
        limit = min(0.99, math.ceil(worst / tick - 1e-9) * tick)
        r = self.c.create_and_post_order(OrderArgs(token_id=token, price=round(limit, 4),
                                                   size=math.floor(shares * 100) / 100, side=Side.BUY),
                                         order_type=OrderType.FOK)          # fill-or-kill: nothing rests on the book
        if not (isinstance(r, dict) and r.get("success") and r.get("status") == "matched"):
            print("   order rejected:", r); return None
        spent, got = float(r.get("makingAmount") or 0), float(r.get("takingAmount") or 0)
        return (got, spent / got) if got > 0 else (shares, avg)


def open_exposure(db): return db.execute("select coalesce(sum(usd),0) from trades where status='open'").fetchone()[0]


def cycle(http, db, cfg, watch, broker):
    events = fetch_events(http, cfg.tag, cfg.pages)
    opps = [o for ev in events for m in ev.get("markets") or [] if (o := extract(ev, m))]
    cands = analyse(opps, cfg, watch)
    print(f"[{datetime.now():%H:%M:%S}] {len(events)} events -> {len(opps)} markets with a 50-50 clause "
          f"-> {len(cands)} flagged")
    for o in sorted(cands, key=lambda o: -o.q):
        books = {t: fetch_book(http, t) for t in o.tokens}
        p = plan(o, books, cfg, open_exposure(db))
        when = o.start.strftime("%m-%d %H:%MZ") if o.start else "?"
        print(f"{when}  {o.outcomes[0]} vs {o.outcomes[1]}  q={o.q:.2f}  [{'; '.join(o.why)}]")
        if not p:
            print("    no trade (edge/price/depth/limits)"); continue
        print(f"    -> BUY {o.outcomes[p['i']]}: {p['shares']:.1f} sh @ {p['price']:.3f} = ${p['usd']:.2f}, "
              f"expected return {p['edge']:+.0%}")
        if broker is None or os.path.exists("STOP") or db.execute(
                "select 1 from trades where market_id=?", (o.market_id,)).fetchone():
            continue
        got = broker.buy(o.tokens[p["i"]], books[o.tokens[p["i"]]][0], p["usd"])
        if not got: print("    not filled"); continue
        shares, avg = got
        db.execute("insert into trades(ts,mode,market_id,slug,question,token,outcome,idx,price,shares,usd,q,edge,why)"
                   " values(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                   (datetime.now(timezone.utc).isoformat(), "live" if isinstance(broker, Live) else "paper",
                    o.market_id, o.slug, o.question, o.tokens[p["i"]], o.outcomes[p["i"]], p["i"], avg, shares,
                    shares * avg, o.q, p["edge"], "; ".join(o.why)))
        db.commit(); print("    filled & recorded")


# ---------- 4. settle & analyse results ----------
def settle(db, http):
    for tid, mid, idx, shares, usd in db.execute(
            "select id,market_id,idx,shares,usd from trades where status='open'").fetchall():
        try: m = get(http, f"{GAMMA}/markets/{mid}")
        except RuntimeError as e: print(e); continue
        px = [float(x) for x in jl(m.get("outcomePrices"))]
        if not m.get("closed") or len(px) != 2 or any(abs(p * 2 - round(p * 2)) > 0.04 for p in px): continue
        payout = shares * px[idx]
        db.execute("update trades set status='settled',payout=?,pnl=?,final_5050=? where id=?",
                   (payout, payout - usd, int(abs(px[0] - .5) < .02), tid))
        print(f"settled #{tid} {m.get('question','')[:50]}: final {px} -> pnl ${payout - usd:+.2f}")
    db.commit()


def report(db):
    rows = db.execute("select mode,status,count(*),sum(usd),sum(coalesce(pnl,0)),sum(coalesce(final_5050,0)),"
                      "avg(q),avg(edge) from trades group by mode,status").fetchall()
    if not rows: return print("no trades yet")
    for mode, st, n, usd, pnl, fifty, q, edge in rows:
        extra = f", resolved 50-50: {fifty}/{n} (assumed q avg {q:.2f})" if st == "settled" else ""
        print(f"{mode:5} {st:8} n={n:3} staked=${usd:.2f} pnl=${pnl:+.2f} avg expected edge {edge:+.0%}{extra}")


def calibrate(http, cfg):
    """Historical base rate: of closed markets with a 50-50 forfeit/cancel clause, how many resolved 50-50?"""
    n = hits = 0
    for ev in fetch_events(http, cfg.tag, cfg.pages, closed=True):
        for m in ev.get("markets") or []:
            px, outs = [float(x) for x in jl(m.get("outcomePrices"))], jl(m.get("outcomes"))
            if len(px) != 2 or not m.get("closed") or {str(o).lower() for o in outs} <= GENERIC: continue
            if any(classify_rules(m.get("description") or "")):
                n += 1; hits += abs(px[0] - .5) < .02 and abs(px[1] - .5) < .02
    print(f"{n} closed markets with a 50-50 clause, {hits} resolved 50-50 ({hits / max(n, 1):.1%} base rate).")
    print("Compare with --q-conflict / --q-notice: if your assumed q is far above what your signals really deliver, "
          "the bot will overpay.")


def selfcheck():
    import platform
    print("python", platform.python_version(), "| httpx", httpx.__version__)
    try:
        from py_clob_client_v2 import ClobClient
        from py_clob_client_v2.headers import create_level_1_headers
        c = ClobClient(host=CLOB, chain_id=137, key="0x" + "11" * 32)            # throwaway key, no network
        ok = {"POLY_ADDRESS", "POLY_SIGNATURE"} <= set(create_level_1_headers(c.signer, 0))
        print("live-trading SDK: OK, EIP-712 signing", "works" if ok else "FAILED")
    except Exception as e:
        print("live-trading SDK: not available (", type(e).__name__, str(e)[:100], ")")


def load_watchlist(path):
    try:
        with open(path) as f: return {norm(k): float(v) for k, v in json.load(f).items()}
    except FileNotFoundError: return {}


def main():
    ap = argparse.ArgumentParser(description=__doc__.strip().split("\n")[0])
    ap.add_argument("cmd", choices=["scan", "run", "settle", "report", "calibrate", "selfcheck"])
    ap.add_argument("--live", action="store_true", help="place REAL orders (default: paper trading)")
    for f in fields(Cfg):
        ap.add_argument("--" + f.name.replace("_", "-"), type=type(f.default), default=f.default)
    if len(sys.argv) == 1 and getattr(sys, "frozen", False):      # .exe double-clicked: show usage, don't vanish
        ap.print_help()
        try: input("\nPress Enter to close...")
        except EOFError: pass
        return
    a = ap.parse_args()
    for st in (sys.stdout, sys.stderr):
        if hasattr(st, "reconfigure"): st.reconfigure(errors="replace")
    if a.cmd == "selfcheck": return selfcheck()
    cfg = Cfg(**{f.name: getattr(a, f.name) for f in fields(Cfg)})
    db = sqlite3.connect(cfg.db); db.execute(SCHEMA)
    if a.cmd == "report": return report(db)
    broker = None
    if a.cmd == "run":
        if a.live:
            if not sys.stdin.isatty(): sys.exit("--live needs an interactive terminal")
            if input("LIVE mode places real orders with real money. Type I ACCEPT THE RISK: ") != "I ACCEPT THE RISK":
                sys.exit("aborted")
            broker = Live()
        else:
            broker = Paper()
    with httpx.Client(timeout=20, headers={"User-Agent": "polybot/0.1"}) as http:
        if a.cmd == "calibrate": return calibrate(http, cfg)
        if a.cmd == "settle": return settle(db, http)
        watch = load_watchlist(cfg.watchlist)
        while True:
            try:
                cycle(http, db, cfg, watch, broker); settle(db, http)
            except RuntimeError as e:
                print("error:", e)
                if a.cmd == "scan": sys.exit(1)
            if a.cmd == "scan" or os.path.exists("STOP"): break
            time.sleep(cfg.interval)


if __name__ == "__main__":
    main()
