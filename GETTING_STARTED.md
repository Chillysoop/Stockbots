# Getting started

You're setting up your own copy of Stockbots: up to three AI trading bots that trade US stocks with **simulated money at real prices**, on a small server that runs around the clock. Each bot uses a different AI and a different style, and a dashboard shows which one is winning.

Nothing here trades real money, and nothing here is financial advice.

**Prefer a guide?** Paste [SETUP_PROMPT.md](SETUP_PROMPT.md) into Claude or ChatGPT and it will walk you through these steps one at a time.

## What it costs

| | Cost |
|---|---|
| Server (Hostinger VPS) | about $7–15 a month, depending on plan and term |
| AI calls | usually cents a day; each bot is capped at $1 a day |
| Trading money | none: paper accounts are free and pretend |

Plan on about 45 minutes, most of it creating accounts.

## 1. Pick your bots

You can run one, two or all three. Each bot needs its own Alpaca paper account and one AI key:

| Bot | Trades like | Needs |
|---|---|---|
| Claude Trend | a patient trend follower on ETFs (SPY, QQQ, sector funds) | an Anthropic API key |
| Jev Dips | a dip buyer on big, healthy companies | a Jev key from TypeSafe |
| Codex Momentum | a fast intraday momentum trader on tech stocks | an OpenAI API key |

Not sure? Start with one, then add the others from the dashboard later.

## 2. Collect your keys first

The Setup page on your server stays open for only 2 hours after it starts, so gather everything before you deploy. Keep the keys in a private note or password manager. Never paste them into a chat, an email or a screenshot.

**Alpaca (one paper account per bot)**
1. Sign up at [alpaca.markets](https://alpaca.markets). Paper trading needs no deposit.
2. In the dashboard, open the account menu and create a new **paper** account for each bot you picked (Alpaca allows three). Give them all the same starting balance so the race is fair.
3. Switch to each paper account and generate its **API keys**. Write down the key ID and the secret, labelled with the bot's name. The secret is shown only once.

**Jev (for Jev Dips)**
1. Go to [typesafe.ai](https://typesafe.ai) and sign up. You may need to join a waitlist first.
2. Create a key at [console.typesafe.ai/keys](https://console.typesafe.ai/keys) and turn on billing.

**Anthropic (for Claude Trend)**
1. Sign in at [platform.claude.com](https://platform.claude.com). This is the developer platform, separate from a Claude subscription.
2. Add a few dollars of credit, then create an API key.

**OpenAI (for Codex Momentum)**
1. Sign in at [platform.openai.com](https://platform.openai.com). This is separate from ChatGPT Plus.
2. Add a few dollars of credit, then create a key at [platform.openai.com/api-keys](https://platform.openai.com/api-keys).

## 3. Deploy the server

[![Deploy on Hostinger](https://assets.hostinger.com/vps/deploy.svg)](https://www.hostinger.com/vps/docker-hosting?compose_url=https://raw.githubusercontent.com/Chillysoop/Stockbots/main/docker-compose.yml)

1. Click the button, pick a VPS plan (KVM 1 or KVM 2 is plenty) and check out.
2. Hostinger installs Docker and opens **Docker Manager** with Stockbots loaded. Click **Deploy** if it asks.
3. On the VPS page in hPanel, copy your server's **IP address**.

Already have a server with Docker? Run `curl -fsSLO https://raw.githubusercontent.com/Chillysoop/Stockbots/main/docker-compose.yml && docker compose up -d` instead.

## 4. Run Setup

1. Open `http://YOUR-SERVER-IP` in a browser. You'll see **Set up your bots**.
2. Tick the three statements and pick an **owner password** (you'll log in with it).
3. For each bot: leave **Run this bot** ticked if you want it, give it a name, and paste its Alpaca key ID and secret. Untick the bots you're skipping.
4. Paste the AI key for each bot you're running.
5. Press **Check keys and start paper trading**. Every key is tested; fix anything it flags and press it again.

You land on your dashboard. The bots trade only while the US market is open (9:30 AM–4:00 PM Eastern, weekdays), so outside those hours you'll see "Market closed".

## 5. Optional: add HTTPS

Without a domain, your browser talks to the server over plain HTTP. To add HTTPS:

1. Point a domain or subdomain (like `bots.yourname.com`) at the server's IP with an **A record**.
2. In Docker Manager, add the variable `PUBLIC_DOMAIN=bots.yourname.com` and redeploy.
3. Use `https://bots.yourname.com` from then on.

## Reading the dashboard

- **The race chart:** each bot's return over the past month, leader first.
- **Bot cards:** account value, today's change, what it holds and where each stop is, and whether its AI is making the calls.
- **Decisions:** every real choice a bot made, which AI made it, and whether the order went through or the safety rules blocked it.
- **Pause** stops new trades. **Close all positions** sells everything now. **Change keys or add a bot** reopens Setup.

## If something goes wrong

| You see | Do this |
|---|---|
| "Setup locked" | Docker Manager → stockbots → **app** → Restart, then open the page again |
| "Alpaca rejected these keys" | Use keys generated inside that bot's own **paper** account |
| "Two bots are using the same Alpaca account" | Give each bot a different paper account |
| A bot says "Code is choosing" | Its AI key failed or hit the $1 daily cap: check that provider's billing page |
| The page won't load | Check both containers are running in Docker Manager, and that the server's firewall allows ports 80 and 443 |
| Forgot the owner password | In Docker Manager, add the variable `OWNER_PASSWORD=` with a new password (8+ characters), redeploy, and log in with it |

## Ground rules

- It's paper trading. Real money takes deliberate server changes and is covered in the [README](README.md#real-money-read-this-twice).
- Run it with your own accounts and keys. Don't run it for anyone else's money.
- Not financial advice. AI bots can and do lose money.
