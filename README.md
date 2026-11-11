# optlab sim

Pick a call or put straight from the live option chain, then pull levers and play out what-if scenarios to see what happens to it.

1. **Type a ticker** and press LOAD. The chain comes from CBOE (15 minutes delayed).
2. **Pick an expiration, calls or puts, and a strike.** Each row shows what one contract costs, how much it moves per $1 in the stock, and how much traded today. Set a budget to dim what you can't afford.
3. **Pull the levers:** stock price, days from now, volatility and volume. The P&L updates as you drag.
4. **Read the chart:** candles with volume and RSI or MACD underneath. Scroll or pinch to zoom, drag to pan, hover for a day's values. Pick a RANGE (1M to 5Y): it sets how far back the chart shows and how far back support and resistance are found. Toggle EMA 9/21, SMA 50/200 and Bollinger lines.
5. **Hit a what-if:** resistance breaks, rejected at resistance, support breaks, bounce off support, volume spike up or down, chop, IV crush. Press PLAY to watch one roll of it, or REROLL for another. The band shows where 8 of 10 rolls land, and the odds show how 300 rolls of that scenario end.

Your pick is remembered in the browser, and its live P&L refreshes every 15 seconds. Nothing places a trade.

## Run it

```bash
python -m venv .venv
.venv/bin/pip install -r requirements.txt
bash scripts/run.sh            # http://127.0.0.1:8600
```

Over Tailscale it's the same address as before, `https://<computer>.<tailnet>.ts.net/`, because it uses the same port (8600) as the old dashboard.

## How the simulation works

- **Option value:** Black-Scholes, with the contract's implied volatility solved from its market price, so it starts exactly at the market.
- **Scenarios:** each is a scripted price story (reach resistance, break it, follow through) plus random noise sized from the stock's own 20-day volatility.
- **Volume** sets how far a move goes, not which way: moves scale with the square root of relative volume, and a surge fades by half every 2 days.
- **Volatility** rises 3% for each 1% the price falls, eases 1.5% for each 1% it rises, and gets a small lift from a volume surge.
- **Resistance and support** come from swing highs and lows over the RANGE you pick: swing points within half a day's average range of each other are one level, and each is a touch. The nearest above and below drive the scenarios; tap another level, or type your own.

A simulation shows what follows from these assumptions. It is not a forecast.

## Tests

```bash
.venv/bin/python -m pytest tests     # data and server
node --test tests/*.mjs              # the simulation and indicator maths
```
