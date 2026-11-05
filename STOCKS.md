# Stock Bot: market scanner + news scanner + day trader

A bot that trades **US stocks** (not crypto). During market hours it scans the whole market
for stocks that are moving hard on heavy volume, checks the news for a reason behind the move,
buys the strongest ones with a stop-loss and take-profit already attached, and sells everything
before the close. A news scanner also sends phone alerts about strong catalyst headlines
(FDA approvals, buyouts, earnings beats...) all day, even before the market opens.

It trades through **[Alpaca](https://alpaca.markets)**, a US broker with a free API, free market
data and news, and a **free paper-trading account** (fake money, real prices). It starts in paper
mode. You do not need to put any money in to try it.

It runs next to the crypto grid bots on the same server, uses the same `.env` for alerts
(ntfy and email), and the same Docker setup.

---

## How it works

Every 5 minutes, from 9:45 a.m. to 3:00 p.m. New York time:

1. **Build a watch list:** today's top 50 % gainers, the 50 most-traded stocks, and any stock
   with strong fresh news.
2. **Filter.** A stock must pass every rule (all are settings in `config-stocks.yaml`):
   - price $3–$300 (no penny stocks)
   - up 3–40% today (moving, but not already blown out)
   - trading at least **2x its normal volume** for this time of day ("relative volume")
   - at least $1M traded today and a bid/ask spread under 0.5% (easy to get in and out)
   - price above **VWAP** (the average price everyone paid today, so buyers are in control)
   - within 3% of today's high (still strong, not fading)
   - no bad news (share offerings, bankruptcy, downgrades, lawsuits...)
3. **Confirm the breakout:** the price is above the highs of the last three 5-minute bars, so
   it's moving up *right now*.
4. **Rank** the survivors by size of move, relative volume and news score, and buy the best
   ones until the open slots (3 by default) are filled.
5. **Every buy is a bracket order:** a stop-loss 3% below and a take-profit 6% above are sent
   to Alpaca together with the buy, so the position is protected even if the bot or server
   goes down.
6. **10 minutes before the close** it sells whatever is still open (day style). Set
   `trade.style: swing` to hold overnight instead.

**Position size:** each trade risks about 1% of your capital. With $1,000 and a 3% stop, that's
about $10 lost if the stop hits, and no single stock is ever more than 30% of the capital.

### The news scanner

It reads Alpaca's news feed (Benzinga headlines) and scores each headline:

| Strong good news (+3 to +4) | Bad news (−2 to −5) |
|---|---|
| FDA approval, breakthrough designation, positive trial results | Share offering, dilution, reverse split |
| Buyout / "to be acquired" | Bankruptcy, going concern, delisting notice |
| Beats earnings estimates, raises guidance | Misses estimates, cuts guidance |
| Added to the S&P 500, big contract win, upgrade | FDA rejection, failed trial, downgrade, SEC probe |

Fresh news counts more than old news, and articles about one company count more than market
round-ups. Good news raises a stock's rank; bad news blocks the bot from buying it.
Headlines scoring 2 or more trigger a **"News watch"** phone alert (each headline only once).

It is keyword matching, not magic: it can misread a headline. That's why news only *helps* pick
stocks; the price and volume rules still have to agree.

---

## Risks: read this first

- **Most day traders lose money.** Momentum stocks can drop 10% in a minute. A stop-loss
  limits losses but a fast drop or a trading halt can sell well below the stop.
- **Run it in paper mode for at least a few weeks** and look at the results before risking money.
- **Pattern day trader (PDT) rule.** With under $25,000 in a margin account, US brokers allow only
  3 day trades (buy and sell the same stock the same day) in any 5 business days. The bot counts
  them and stops opening trades before a 4th (`respect_pdt: true`). With a small account that
  means about 3 trades a week. Options: use a **cash account** (no PDT rule, but you can only
  reuse money after it settles the next day), or `style: swing` with wider stops. (FINRA has been
  working on changes to this rule; check Alpaca's current rules before going live.)
- **Daily loss limit:** if the account is down 3% of capital in a day, the bot sells and stops
  until tomorrow. It looks at the whole Alpaca account, so keep the bot's account separate from
  long-term investments.
- **Taxes:** short-term gains are taxed as ordinary income. Alpaca sends you a 1099.

---

## Setup

### 1. Get free Alpaca paper-trading keys

1. Sign up at **https://alpaca.markets** (free; a paper account needs no money or ID check).
2. Log in to https://app.alpaca.markets, make sure the top-left switch says **Paper**, then
   find **API Keys** on the home page and click **Generate New Keys**.
3. Copy the **Key** and the **Secret** (the secret is shown only once).

### 2. Add them to `.env` on the server

```bash
cd ~/trading-bot2
git pull
nano .env
```

Add (alerts reuse your existing `NTFY_TOPIC` / email settings):

```
ALPACA_API_KEY=PKxxxxxxxxxxxxxxxx
ALPACA_API_SECRET=xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx
```

### 3. Build and test

```bash
docker compose build
docker compose run --rm stock-bot python -m stockbot check
```

You should see your paper balance (Alpaca gives you $100,000 fake dollars; the bot only uses
`capital_usd`, $1,000 by default) and get a test alert.

### 4. Try the scanners

These work any time; during market hours they show live results:

```bash
docker compose run --rm stock-bot python -m stockbot scan   # stocks passing the rules + near misses
docker compose run --rm stock-bot python -m stockbot news   # strongest good and bad news right now
```

### 5. Start it

```bash
docker compose up -d stock-bot
docker compose logs -f --tail 30 stock-bot
```

It restarts by itself after crashes and server reboots.

---

## Everyday commands

```bash
docker compose exec stock-bot python -m stockbot status     # positions and profit/loss
docker compose exec stock-bot python -m stockbot pause      # no new trades (open ones keep their stops)
docker compose exec stock-bot python -m stockbot resume
docker compose exec stock-bot python -m stockbot sell-all   # sell everything the bot owns, then pause
docker compose exec stock-bot python -m stockbot scan
docker compose exec stock-bot python -m stockbot news
```

You can also watch trades on Alpaca's website or phone app.

## Alerts you'll get

- **Bought XYZ**: price, why (move, volume, news headline), stop and target
- **Sold XYZ (take-profit / stop-loss / sold): +$12.40**: with today's and all-time profit
- **News watch: ABC, DEF**: strong fresh catalyst headlines
- **Daily summary** after the close, on days it traded
- **Daily loss limit hit**, **Day-trade limit reached**, **Stock bot error** (at most every 30 min)

## Going live

Only after weeks of paper results you're happy with:

1. In Alpaca, switch to your **Live** account, fund it, and generate live API keys.
2. Add them to `.env` as `ALPACA_LIVE_API_KEY` and `ALPACA_LIVE_API_SECRET` (paper keys stay
   as they are).
3. In `config-stocks.yaml`, set `mode: live` and a `capital_usd` you can afford to lose.
4. `docker compose up -d stock-bot`

Paper and live keep separate state files (`data-stocks/state-paper.json`, `state-live.json`).

## Tuning tips

- **Fewer, higher-quality trades:** raise `min_relative_volume` to 3–5, or set
  `require_news: true` to only trade stocks with a fresh catalyst.
- **Volatile small caps:** widen `stop_loss_pct` (4–5) and `take_profit_pct` (8–10). Keep the
  take-profit about 2x the stop.
- **Free data note:** the free IEX feed only sees part of the market's volume. The bot compares
  IEX volume with IEX volume, so relative volume still works, but `min_dollar_volume` reads low.
  The paid `sip` feed fixes that.

## Code layout

| File | What it does |
|---|---|
| `stockbot/alpaca.py` | Alpaca API: account, bracket orders, screener, snapshots, bars, news |
| `stockbot/news.py` | Headline scoring and the news scanner |
| `stockbot/scanner.py` | Market scanner: watch list, filters, breakout check, ranking |
| `stockbot/trader.py` | Main loop: entry window, sizing, PDT, daily loss limit, end-of-day sell, alerts |
| `stockbot/config.py` | Loads `config-stocks.yaml` and the Alpaca keys |
| `tests/test_stockbot.py` | Tests with a fake Alpaca (run `pytest`) |
