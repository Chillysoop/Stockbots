# Stockbots

**Setting up your own?** Follow [GETTING_STARTED.md](GETTING_STARTED.md), or paste [SETUP_PROMPT.md](SETUP_PROMPT.md) into Claude or ChatGPT for a step-by-step walkthrough.

Up to three AI trading bots race each other on the US stock market, each on its own Alpaca paper account:

| Bot | AI that makes the calls | Style |
|---|---|---|
| **Claude Trend** | Claude (`claude-haiku-5-5`) | Trend follower: ETFs in clear uptrends, adds once to winners, exits when the trend breaks |
| **Jev Dips** | Jev (`jev-latest`, TypeSafe) | Dip buyer: quality large caps above their 200-day average after a sharp pullback, sells the bounce |
| **Codex Momentum** | OpenAI (`gpt-6-luna`) | Momentum chaser: intraday breakouts on high volume, always flat by the close |

**It runs on paper by default.** Real prices, simulated money. Nothing touches real money unless you change server settings yourself, on purpose.

> **Not financial advice.** This is an experiment and a piece of software, not a trading product. AI trading bots can and do lose money.

## How a decision is made

Every minute while the market is open, for each bot:

1. **Look.** Account, positions and live prices from Alpaca.
2. **Protect.** Every position has a stop-loss order sitting at Alpaca, so it holds even if this server goes down.
3. **Forced exits.** Rules that never wait for an AI: the momentum bot closes everything at 3:50 PM ET, the dip buyer has a 6-day time stop, the trend bot exits when price falls well below its 50-day average.
4. **Menu.** The bot's style lists the moves that are valid right now: buy one of a few candidates, add to a winner, close a position the rules flag, or hold.
5. **Ask.** The bot's AI picks one move from the menu. It never sees an order button.
6. **Check.** Plain code has the final say: position size (15–20% of the account), stop distance, max positions, trades per day, a 3% daily loss stop, a re-entry cooldown, buying power, and never holding more than the account is worth (no margin borrowing).
7. **Record, then act.** The decision and who made it go into the database, then the order goes to Alpaca.

If an AI is down, slow, over its daily budget or answers off the menu, a simple code chooser stands in. It only takes exits the rules suggest; it never opens new trades.

## What you need

Run one, two or all three bots. For each bot you run:

- **An Alpaca paper account of its own** (Alpaca allows three per person). In the Alpaca dashboard, open new paper accounts from the account menu, give them the same starting balance for a fair race, then switch to each one and generate its API keys.
- **Its AI key:** Jev from [console.typesafe.ai/keys](https://console.typesafe.ai/keys); Anthropic from [platform.claude.com](https://platform.claude.com) (separate from a Claude subscription); OpenAI from [platform.openai.com/api-keys](https://platform.openai.com/api-keys) (separate from ChatGPT). Each needs billing on.

Plus **a server that runs Docker.** A Hostinger KVM 1 or KVM 2 VPS is plenty.

AI costs are small: each bot asks its AI at most once a minute, only when it has a real choice, and each bot has a $1/day cap by default.

## Deploy on Hostinger

[![Deploy on Hostinger](https://assets.hostinger.com/vps/deploy.svg)](https://www.hostinger.com/vps/docker-hosting?compose_url=https://raw.githubusercontent.com/Chillysoop/Stockbots/main/docker-compose.yml)

1. Click the button, pick a VPS plan and check out. Hostinger installs Docker and opens Docker Manager with this project's `docker-compose.yml` loaded. Click **Deploy** if it asks.
2. Find the server's IP address on the VPS page in hPanel and open `http://<that IP>` in a browser. You'll see the **Setup** page.
3. On Setup: tick the three statements, choose an owner password, tick **Run this bot** for each bot you want and paste its Alpaca paper keys, paste the AI keys those bots need, then press **Check keys and start paper trading**. Every key is checked before anything starts, and no two bots may share an Alpaca account.
4. You land on the dashboard. Log in later with your owner password.

**Do Setup soon after deploying.** The Setup page is open to whoever reaches it first, and it locks 2 hours after the app starts. To reopen it, restart the `app` container (Docker Manager → stockbots → app → Restart).

### Add HTTPS (recommended)

Without a domain, the Setup page sends your keys over plain HTTP. To fix that:

1. Point a domain or subdomain (for example `bots.yourname.com`) at the server's IP with an **A record**.
2. In Docker Manager, add the environment variable `PUBLIC_DOMAIN=bots.yourname.com` to the project and redeploy.
3. Open `https://bots.yourname.com`. Caddy gets the certificate automatically.

Best order: deploy, set up the domain, restart the app container, then do Setup over HTTPS.

### Already have a server?

```
curl -fsSLO https://raw.githubusercontent.com/Chillysoop/Stockbots/main/docker-compose.yml
docker compose up -d
```

Then open `http://<your-server-ip>/`.

## The dashboard

- **The race:** each bot's return since it started, with the leader on top.
- **Each bot:** account value, today's change, what it holds (with each stop), whether its AI or the code is making calls, AI cost today, and trades used today.
- **Pause** stops new trades (stops stay in place). **Close all positions** sells everything at market.
- **Trading rules:** the plain-English rules each AI reads with every decision. Edit them from the dashboard; sizes, stops and limits stay in code.
- **Decisions:** every real choice, who made it (Jev shows its confidence), and whether the order went through or the risk layer blocked it.
- **Change keys or add a bot** stops the bots and reopens Setup. Positions and their stops stay at Alpaca.

## Backtests

On the dashboard, **Backtests** replays a bot's rules on past prices (downloaded with that bot's Alpaca paper keys) and compares 48 combinations of position size (10–25%), daily loss stop (off, 2, 3, 5%) and stop distance (0.75x, 1x, 1.5x the normal one). A two-year run of a swing bot takes about a minute; the momentum bot uses 5-minute prices, so keep it to a few months. Reports stay in `/data/backtests`.

How to read one:
- **It tests the rules, not the AI.** Asking an AI about past prices leaks hindsight (it has read the news), so a code chooser takes every setup the rules rank highest. The live bot's AI may skip some.
- **Use the test period.** Settings are ranked on the first 70% of the period; the last 30% is the honest check. A setting that only shines in the first part is probably luck.
- **It's optimistic.** Today's big stocks are used throughout (survivorship bias), swing bots decide once a day at the close, and fills assume small slippage.

From the server instead: `docker compose exec app python -m stockbots.backtest --bot jev --months 24`.

## Settings

Most people need none. To change one, add it as an environment variable in Docker Manager (or a `.env` file next to `docker-compose.yml`) and redeploy.

| Setting | Default | What it does |
|---|---|---|
| `PUBLIC_DOMAIN` | blank | A domain pointed at the server, for automatic HTTPS |
| `MODEL_DAILY_USD_CAP` | `1` | Daily AI spending cap per bot, in dollars |
| `TICK_SECONDS` | `60` | How often each bot looks at the market |
| `DAILY_LOSS_STOP_PCT` | `3` | A bot stops opening trades for the day after losing this % |
| `CLAUDE_MODEL` | `claude-haiku-5-5` | Claude model for the trend bot |
| `OPENAI_MODEL` | `gpt-6-luna` | OpenAI model for the momentum bot (a Codex model works too, at a higher price) |
| `OPENAI_REASONING_EFFORT` | `low` | Sent to OpenAI; set blank for models without reasoning |
| `JEV_MODEL` | `jev-latest` | Jev model for the dip bot |
| `MAX_POSITION_USD` | `0` | Optional dollar cap per position (0 = only the % of account) |
| `MAX_EXPOSURE_PCT` | `100` | Most a bot may hold, as % of its account. 100 means it never borrows on margin |
| `OWNER_PASSWORD` | blank | Forgot your password? Set a new one here (8+ characters) and redeploy |
| `FALLBACK_OPENS_TRADES` | `false` | Let the code chooser open trades when the AI is down |

Model prices used for the spending cap can be changed with `JEV_USD_PER_MTOK`, `CLAUDE_USD_PER_MTOK_IN/OUT` and `OPENAI_USD_PER_MTOK_IN/OUT`. The cap counts what the app estimates; check each provider's billing page for real charges.

## Looking after it

- **Logs:** Docker Manager → stockbots → app → Logs, or `docker compose logs app`.
- **Update:** after new code lands on `main`, GitHub builds a new image. Redeploy in Docker Manager, or run `docker compose pull && docker compose up -d`. Your settings and history live in the `bots-data` volume and are kept.
- **Start Setup over** (new keys, more bots): use **Change keys or add a bot** on the dashboard, or run `docker compose exec app python -m stockbots.reset` and restart the app container.

## Real money (read this twice)

There is no button for it. Everything below is a server variable (Docker Manager, or `.env` next to `docker-compose.yml`), set by hand, on purpose.

**1. Turn on live mode for the whole server:** `MODE=live` and `LIVE_ACK=I-ACCEPT-REAL-MONEY-RISK`. With either missing, everything stays on paper.

**2. Give each bot its own real-money broker.** Each needs its own account, because Alpaca has allowed one live account per person so far:

| Broker | Set | Notes |
|---|---|---|
| Alpaca | `LIVE_BROKER_<BOT>=alpaca`, `ALPACA_LIVE_KEY_<BOT>`, `ALPACA_LIVE_SECRET_<BOT>` | Live keys from your live account (later: a live sub-account) |
| Tradier | `LIVE_BROKER_<BOT>=tradier`, `TRADIER_TOKEN_<BOT>`, optional `TRADIER_ACCOUNT_<BOT>` | API token from dash.tradier.com/settings/api. No extended hours |
| Public | `LIVE_BROKER_<BOT>=public`, `PUBLIC_SECRET_<BOT>`, optional `PUBLIC_ACCOUNT_<BOT>` | Secret key from Public's settings (API section). **No sandbox**: start small |
| none | `LIVE_BROKER_<BOT>=off` | The bot sits out |

`<BOT>` is `CLAUDE`, `JEV` or `OPENAI`. The bots keep using their Alpaca paper keys from Setup for market data, so every bot sees the same prices whatever broker holds its money. Tradier and Public get their protective stop as a separate order a few seconds after each buy fills; if that ever fails, the stop guard places it on the next minute.

**Test Tradier with fake money first:** on paper, set `PAPER_BROKER_<BOT>=tradier` and `TRADIER_SANDBOX_TOKEN_<BOT>` (Tradier's sandbox token). That bot then paper trades on Tradier's sandbox instead of Alpaca.

**Wash-sale guard:** on by default in live mode (`WASH_SALE_GUARD=auto`). Across every bot and broker on this server: after any bot sells a stock at a loss, no bot buys it again for 31 days; no two bots hold the same stock; and a bot won't buy a stock another bot bought in the last 30 days. `WASH_SALE_GROUPS` lists funds treated as identical (SPY, VOO, IVV and SPLG by default). It can't see your other accounts (an IRA, Robinhood, a spouse's), so buys there still count. It lowers the risk; it isn't tax advice, so keep your own records.

### Moving a bot to another broker (for example, onto Alpaca live sub-accounts)

1. On the dashboard, press **Prepare to move broker** on that bot. It pauses, sells everything at its current broker, cancels its orders, and shows **Ready to move** once nothing is left.
2. In Docker Manager, change `LIVE_BROKER_<BOT>` and paste the new broker's keys (for Alpaca: `ALPACA_LIVE_KEY_<BOT>` and `ALPACA_LIVE_SECRET_<BOT>` from that bot's sub-account). Redeploy.
3. The bot comes back on the new broker, still paused. Check its balance on the dashboard, then press **Resume**.

The race chart keeps going: the app records each bot's return day by day within each account and chains them, so a different balance at the new broker doesn't break the line. Move money between brokers yourself; the bots never withdraw or transfer.

Again: this is not financial advice, and you can lose money.

## Develop

```
pip install -r requirements.txt
python -m unittest discover -s tests -t .      # risk layer, styles, AI clients, engine, web
DEMO=true DATA_DIR=./data python -m stockbots   # a pretend market on a fast clock; password: demo-password
```

The demo needs no keys and no internet: one real second is one market minute.
