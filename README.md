# SOL/USD Grid Trading Bot for Kraken

A bot that trades SOL/USD on Kraken around the clock using a **grid strategy**, runs on a
cheap cloud server, and emails you about everything it does.

It starts in **paper mode**: real Kraken prices, fake money. When you're happy with how it
behaves, you change one line to trade for real.

---

## Contents

1. [How the bot works](#how-the-bot-works)
2. [Risks: read this first](#risks-read-this-first)
3. [What you need](#what-you-need)
4. [Setup, step by step](#setup-step-by-step)
5. [Everyday commands](#everyday-commands)
6. [Dashboard](#dashboard)
7. [Going live with real money](#going-live-with-real-money)
8. [Changing settings](#changing-settings)
9. [The emails you'll get](#the-emails-youll-get)
10. [Troubleshooting](#troubleshooting)
11. [For the curious: code layout and tests](#for-the-curious-code-layout-and-tests)

---

## How the bot works

1. **It draws a grid.** The bot looks at how much SOL's price has been moving lately and
   draws about 10–20 price lines (a "grid") above and below the current price. With
   default settings and calm markets, that's roughly ±5–8% around the price, with lines about
   1% apart.
2. **It places orders on the lines.** It places a *buy* order on every line below the price
   and a *sell* order on every line above it. To have something to sell, it buys a small amount
   of SOL at the start.
3. **It buys low and sells high, over and over.** When the price dips and a buy fills, the bot
   places a sell one line higher. When that sell fills, it puts the buy back. Each completed
   buy-then-sell earns about one grid spacing (about 1%) on that slice of money, minus Kraken's
   fees (0.25% each way).
4. **It protects you.**
   - **Breakout pause:** if the price moves past the top or bottom of the grid, the bot
     cancels its orders, stops, and emails you. Nothing is sold. You decide when to resume.
   - **Kill switch:** if the bot's money drops **10%** below where it started, it cancels
     everything, stops for good, and emails you. Your coins are not sold.
5. **It remembers.** Everything is saved to disk, so if the server restarts, the bot picks
   up exactly where it left off.

Grids do best when the price **chops sideways**. They do worst when the price **falls
steadily**, because the bot keeps buying on the way down.

## Risks: read this first

- **You can lose money.** In a falling market the bot ends up holding SOL that's worth less
  than it paid. The kill switch limits the damage to about 10%, but a sudden crash between
  checks (every 30 seconds) can push the loss a bit past that.
- **Profits are small with small money.** With $400, each completed buy/sell earns roughly
  **$0.10–$0.20**. A good choppy week might mean dozens of those. This bot is a way to learn
  and to grow small gains slowly, not a way to get rich.
- **Taxes:** in the US every trade is a taxable event. Kraken lets you download a full trade
  history at tax time (*History → Export*).
- Only use money you can afford to lose. **Run paper mode for at least 1–2 weeks first.**

## What you need

| Thing | Cost | Why |
|---|---|---|
| A **cloud server (VPS)** | ~$4–6/month | Runs the bot 24/7 |
| A **Gmail account** with 2-Step Verification turned on | Free | Sends you alerts |
| A **Kraken account** (verified) | Free | Only needed when you go live |
| A computer with a terminal | Free | To connect to the server. Mac: the *Terminal* app. Windows: *PowerShell* |

---

## Setup, step by step

Type the commands in the grey boxes exactly as shown. Copy and paste works.
Lines starting with `#` are comments; you don't need to type them.

### Step 1: Rent a server

Any of these works. Pick one:

- **Hetzner Cloud** (cheapest, about €4/month): https://www.hetzner.com/cloud. Create a
  project, then *Add Server* → **Ubuntu 24.04** → the cheapest shared-CPU type.
- **DigitalOcean** (very beginner-friendly, $6/month): https://www.digitalocean.com. Choose
  *Create → Droplet* → **Ubuntu 24.04** → *Basic* → *Regular* → $6/month.

When asked how to log in, choose **Password** (simplest) and pick a long, strong password,
then save it in a password manager. (SSH keys are more secure. If you know how to set one up,
use that.)

When the server is ready you'll see its **IP address**, something like `203.0.113.45`.
Write it down.

### Step 2: Connect to the server

Open Terminal (Mac) or PowerShell (Windows) and type, using your IP address:

```bash
ssh root@203.0.113.45
```

- The first time, it asks *"Are you sure you want to continue connecting?"* Type `yes` and
  press Enter.
- Enter your server password. **Nothing appears on screen while you type the password.** That's
  normal; just type it and press Enter.

You're in when the prompt looks like `root@yourserver:~#`. Every command from here on is
typed into this window.

### Step 3: Update the server and install Docker

Docker is what keeps the bot running and restarts it automatically if it crashes or the
server reboots. Run these one at a time:

```bash
# Install security updates (takes a minute or two; if a pink/purple screen appears, press Enter)
apt update && apt upgrade -y

# Turn on automatic security updates
apt install -y unattended-upgrades && dpkg-reconfigure -f noninteractive unattended-upgrades

# Firewall: block everything except SSH
ufw allow OpenSSH && ufw --force enable

# Install Docker
curl -fsSL https://get.docker.com | sh
```

Check that it worked: `docker --version` should print a version number.

### Step 4: Download the bot

```bash
cd ~
git clone https://github.com/tlnetwork-hash/trading-bot2.git
cd trading-bot2
```

If your GitHub repository is **private**, git asks for a username and password. For the
password, GitHub needs a **personal access token**, not your real password:
GitHub → your photo → *Settings → Developer settings → Personal access tokens → Fine-grained
tokens → Generate new token*. Give it read-only *Contents* access to this repository and paste
it when git asks for the password.

### Step 5: Create a Gmail app password

The bot sends email through your Gmail. Google requires a special **app password** for this.
It's not your normal password, and you can delete it at any time.

1. Make sure 2-Step Verification is on: https://myaccount.google.com/signinoptions/twosv
2. Go to https://myaccount.google.com/apppasswords
3. Type a name like `grid bot` and click **Create**.
4. Google shows a 16-letter password such as `abcd efgh ijkl mnop`. Copy it now; you won't
   see it again.

### Step 6: Put your secrets in the `.env` file

```bash
cp .env.example .env
nano .env
```

`nano` is a simple text editor. Use the arrow keys to move around and fill in:

```
GMAIL_ADDRESS=yourname@gmail.com
GMAIL_APP_PASSWORD=abcd efgh ijkl mnop
ALERT_EMAIL_TO=
```

Leave the Kraken lines empty for now. Paper mode doesn't need them.

Save and exit: press **Ctrl+O**, then **Enter**, then **Ctrl+X**.

Then make the file readable only by you:

```bash
chmod 600 .env
```

### Step 7: Start the bot in paper mode

```bash
docker compose up -d --build
```

The first run takes a minute or two to build. Then test the setup:

```bash
docker compose exec bot python -m bot check
```

You should see something like:

```
Config OK: PAPER mode, SOL/USD, $400.00
Kraken reachable: SOL/USD = $151.23 (min order 0.02 SOL)
No Kraken API key set (fine for paper mode)
Test email sent to yourname@gmail.com (check spam if you don't see it)
```

Check your inbox (and your spam folder). If the alerts land in spam, mark one as *Not spam*.

Within a minute you should also get **"Bot started"** and **"Grid built"** emails.
**You're done:** the bot is running 24/7 with fake money. You can close the terminal window
and it keeps going.

---

## Everyday commands

First connect (`ssh root@YOUR-IP`), then go to the bot's folder (`cd ~/trading-bot2`):

| What you want | Command |
|---|---|
| See how it's doing | `docker compose exec bot python -m bot status` |
| Watch live activity (Ctrl+C to stop watching; the bot keeps running) | `docker compose logs -f --tail 50 bot` |
| Pause trading (cancels all orders, keeps your coins) | `docker compose exec bot python -m bot pause` |
| Resume (builds a fresh grid around today's price) | `docker compose exec bot python -m bot resume` |
| Start over (cancels orders, **sells the bot's SOL**, resets to `capital_usd`) | `docker compose exec bot python -m bot reset` |
| Test email and Kraken connection | `docker compose exec bot python -m bot check` |
| Restart after changing settings | `docker compose restart` |
| Stop the bot completely | `docker compose down` |
| Start it again | `docker compose up -d` |
| Install a newer version of the bot code | `git pull && docker compose up -d --build` |

**After a breakout pause or the kill switch:** take a look at the market first. When you
want to continue, run `resume`. After the kill switch, `resume` measures the next 10% loss
limit from your *current* balance.

> `docker compose down` stops the bot but **leaves its orders open on Kraken**. To cancel
> everything first, run `pause`, wait for the "Bot paused" email, and then stop the bot.

---

## Dashboard

A read-only web page that shows:
- your equity over time, with your starting amount and the kill-switch level marked
- the current price and the grid (each buy and sell order, with a "now" line at the current price)
- what the bot is holding
- the last 50 trades

It refreshes every 15 seconds. It can't place, cancel or change anything.

By default the dashboard is reachable **only from the server itself**. The safe way to open it
from your phone or computer is **[Tailscale](https://tailscale.com)**, a free private network
between your own devices. Nobody else on the internet can see the page.

1. Install Tailscale on the server and sign in. If it's already installed, skip this.
   ```bash
   curl -fsSL https://tailscale.com/install.sh | sh
   tailscale up
   ```
2. Install the Tailscale app on your phone and/or computer and sign in with the **same account**.
3. On the server, find its Tailscale address and tell the dashboard to listen on it:
   ```bash
   tailscale ip -4                       # prints something like 100.73.129.3
   cd ~/trading-bot2
   nano .env                             # set  DASHBOARD_BIND=100.73.129.3  (your address)
   docker compose up -d
   ```
4. With Tailscale switched on, open **http://100.73.129.3:8080** (using your address) on your
   phone or computer. Bookmark it, or add it to your phone's home screen.

> Never set `DASHBOARD_BIND=0.0.0.0`. That puts the page on the public internet.

The chart gets a new point every 5 minutes, so give it a little while after starting.
Equity history and the trade log are saved in `data/history-<mode>.jsonl` and
`data/trades-<mode>.jsonl`.

---

## Going live with real money

Only go live after paper mode has run for a while (1–2 weeks is a good minimum) and you're
comfortable with the emails you've been getting.

### 1. Create a Kraken API key

1. Log in at https://pro.kraken.com, open the account menu, then **Settings → API → Create API key**.
2. Name it `grid bot`.
3. Turn on **only** these permissions:
   - Query Funds
   - Query Open Orders & Trades
   - Query Closed Orders & Trades
   - Create & Modify Orders
   - Cancel/Close Orders
4. **Never** turn on *Withdraw Funds* or *Deposit Funds*. Without them, even if someone
   stole your key they couldn't take money out.
5. Recommended: under **IP address restrictions**, enter your server's IP address so the key
   only works from your server.
6. Click *Generate key*. Copy the **API Key** and the **Private Key** right away; Kraken
   won't show the private key again.

### 2. Add money to Kraken

Deposit at least your `capital_usd` amount (default **$400**) in **USD**. The bot checks that
the USD is free before it starts.

Tips:
- Don't trade SOL/USD by hand on the same Kraken account while the bot runs. Other coins and
  other pairs are fine.
- Keeping $10–20 above `capital_usd` is a good buffer.

### 3. Add the key and switch to live

```bash
cd ~/trading-bot2
nano .env            # paste KRAKEN_API_KEY and KRAKEN_API_SECRET, then Ctrl+O, Enter, Ctrl+X
nano config.yaml     # change   mode: paper   to   mode: live   (and capital_usd if you want)
docker compose restart
docker compose exec bot python -m bot check
```

`check` should now say `Kraken API key works: $400.00 USD ... available`. You'll get a
"Bot started" email showing **LIVE**, and a "Grid built" email once the real orders are
placed. You can also see the orders on Kraken under **Orders**.

Paper and live keep separate memory files (`data/state-paper.json` and
`data/state-live.json`), so live trading starts fresh.

**To go back to paper:** run `pause` first (so no real orders are left open), set
`mode: paper`, and restart.

---

## Changing settings

All settings live in `config.yaml`, and each one has a comment explaining it. Edit with
`nano config.yaml`, then run `docker compose restart`. The ones you're most likely to change:

| Setting | Default | Meaning |
|---|---|---|
| `mode` | `paper` | `paper` = fake money, `live` = real orders |
| `capital_usd` | `400` | The most USD the bot may use |
| `risk.max_drawdown_pct` | `10` | Kill switch: stop if the bot's money falls this % below the start |
| `risk.breakout_action` | `pause` | `pause` = stop and email you; `recenter` = rebuild the grid around the new price automatically |
| `grid.min_spacing_pct` | `1.0` | Gap between grid lines. Bigger means fewer trades with more profit each |
| `alerts.email_on_every_fill` | `true` | Set to `false` if fill emails get to be too many |
| `alerts.daily_summary_hour_utc` | `13` | Hour the daily summary is sent (UTC; 13 = 9am New York in summer) |

Grid settings take effect the next time a grid is built. To apply them right away, run
`pause` and then `resume`.

---

## The emails you'll get

| Subject | When | What to do |
|---|---|---|
| Bot started | Every time the bot starts (including server reboots) | Nothing |
| Grid built | A new grid was placed | Nothing |
| BUY filled / SELL filled | Every trade | Nothing. A SELL shows the profit from that step |
| Daily summary | Once a day | Glance at it |
| Bot paused | The price left the grid, or something blocked the grid | Look at the market, then run `resume` when ready |
| KILL SWITCH | The bot lost 10% | Decide whether to `resume` (keep going) or `reset` (sell and start over) |
| Cannot reach Kraken | About 10 minutes of network failures | Usually fixes itself. Check https://status.kraken.com |
| Error | Something unexpected | The bot keeps retrying. If it repeats, check the logs |

---

## Troubleshooting

**See what's going on:** `docker compose logs --tail 100 bot`

| Problem | Fix |
|---|---|
| No emails at all | Run `check`. Look in spam. Make sure `GMAIL_APP_PASSWORD` is the 16-letter app password, not your normal Gmail password |
| `Config problem: ...` in the logs | Your `config.yaml` has a typo. The message says which setting |
| `could not build grid: ... too little` | `capital_usd` is too small for Kraken's minimum order size. Raise it |
| `Kraken has $X free but the bot expects $Y` | Deposit more USD, lower `capital_usd`, or cancel manual orders that are holding your USD |
| `Kraken rejected the API key` | Re-copy the key and secret into `.env` with no extra spaces, then `docker compose restart` |
| `Invalid nonce` errors | Each API key must be used by one program only. Create a separate key just for the bot |
| `Permission denied` from Kraken | The key is missing a permission from the list in [Going live](#going-live-with-real-money) |
| The bot isn't running after a reboot | `docker compose up -d`. Docker should restart it on its own; check with `docker compose ps` |

**Don't delete the `data/` folder while live trading.** It's the bot's memory of which orders
on Kraken belong to it. If you have to wipe it, run `reset` first, or cancel the bot's orders
on Kraken by hand.

---

## For the curious: code layout and tests

```
bot/
  __main__.py   command line (run, check, status, pause, resume, reset)
  config.py     reads config.yaml and .env, checks the settings make sense
  grid.py       grid math: volatility (ATR) and where the lines go
  trader.py     the main loop: orders, fills, breakout pause, kill switch, reports
  exchange.py   Kraken via the ccxt library (live), or a simulator (paper)
  notifier.py   Gmail alerts
  dashboard.py  read-only web dashboard (+ dashboard.html)
  state.py      saves the bot's memory to data/state-<mode>.json
tests/          automated tests using a fake Kraken (no internet needed)
```

Run the tests on your own computer (needs Python 3.10+):

```bash
pip install -r requirements-dev.txt
python -m pytest
```

How a few details work:
- **Paper fills:** a simulated limit order fills when Kraken's last traded price reaches its
  price, which is checked every 30 seconds. Quick spikes between checks are missed, so paper
  results are slightly pessimistic.
- **The bot only counts its own money.** It tracks the USD and SOL it has traded itself, so
  other funds in your Kraken account don't affect the kill switch or order sizes.
- **Order safety:** the bot saves its memory right before and right after sending each order,
  and after every fill, so a restart picks up where it left off. If sending an order fails
  (for example a network blip), it retries on the next check.
