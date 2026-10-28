# polybot: the "50-50 forfeit" strategy for Polymarket

**The idea (from the video audio):** many Polymarket match markets say a cancelled or forfeited match resolves
**50-50**, so every share pays $0.50. Most traders don't know this. If a forfeit is likely but prices still show a
favourite, the *cheap* side (under 50c) is under-priced: it pays 50c on a 50-50 however low its price, while the
expensive side loses. The video's example: FaZe Clan's CS2 team was scheduled in two tournaments at once.

**What the app does**
1. **Collect:** pulls open esports match markets (Gamma API) and order books (CLOB API).
2. **Analyse:** keeps markets whose rules contain a 50-50 forfeit/cancel clause, then flags likely forfeits:
   schedule conflicts (same team, two matches within 2h), a withdrawal notice in the rules text, or your own
   `watchlist.json` (`{"Some Team": 0.8}` = your probability of a 50-50). Expected value per share is
   `q*0.50 + (1-q)*p - ask`, computed on the real order-book fill price, sized with fractional Kelly.
3. **Trade:** paper trades by default (simulated fills against the real book). `--live` places real FOK orders.
4. **Settle & learn:** `settle` books results (win / lose / 50-50), `report` compares assumed vs actual 50-50 rate.

```
pip install -r requirements.txt
python test_polybot.py          # offline logic tests
python polybot.py selfcheck      # offline: libraries load, signing works
python polybot.py calibrate     # historical base rate of 50-50 resolutions
python polybot.py scan          # look, don't trade
python polybot.py run           # paper-trading loop (Ctrl-C or `touch STOP` to end)
python polybot.py report
```

**Live trading (real money):** set `POLYMARKET_PRIVATE_KEY`, plus `POLYMARKET_FUNDER` and `POLYMARKET_SIG_TYPE`
(0 plain wallet, 1 email proxy, 2 Safe proxy) if you use a Polymarket account, then `python polybot.py run --live`.
You must type a confirmation. Defaults cap risk at $10 per trade and $50 open. Winning shares must be redeemed.

**Be honest with yourself about**
- `--q-conflict` and `--q-notice` are **assumptions**, not measurements. Paper trade first and compare them with `report`.
- The edge only exists while other traders ignore the clause. It may already be crowded or gone.
- Not tested against the live APIs or exchange (written from the docs, verified on synthetic data). Run `scan` first.
- Not financial advice. Check that Polymarket is available to you where you live; this app does not bypass any block.

## Windows .exe

`polybot.exe` is a single-file build of `polybot.py` (PyInstaller 6.22.3, Python 3.12.16, live-trading SDK included).
SHA-256: `df92702eacdb905f167891b7625d3bdb0900584e1529a3f8385eb4e69465e701`

```
polybot.exe selfcheck     # no network: confirms the bundled libraries load and signing works
polybot.exe scan
polybot.exe run           # paper trading
```

- Run it from Command Prompt or PowerShell in its own folder (`polybot.db`, `watchlist.json` and `STOP` live there).
  Double-clicking only shows the help, because the program needs a command.
- It is unsigned, so Windows SmartScreen may warn ("More info", then "Run anyway"), and some antivirus tools flag
  PyInstaller programs.
- It was cross-built under Wine on Linux and smoke-tested there, not on a real Windows machine.
- This program can hold a wallet key, so the safest route is to build it yourself from source you can read:
  install Python 3.10+, put `polybot.py` and `build_exe.bat` in one folder and run `build_exe.bat`.
