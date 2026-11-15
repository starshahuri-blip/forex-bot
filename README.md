# Forex bot for OANDA

A small program that day-trades forex on OANDA by itself, on 5-minute bars, inside fixed limits.
It needs no AI while running, so it costs nothing per trade.

## What it does

- Trades EUR/USD, GBP/USD, USD/JPY and AUD/USD.
- Strategy: trend breakout. It buys when price breaks above the last 20 bars while the trend is up
  (and the mirror image for selling), and only when the market is actually trending.
- Every trade is sent with a stop-loss and a take-profit attached at OANDA, so it stays protected
  even if the server goes down.
- Trades 07:00-19:00 UTC (11pm-11am Pacific), closes everything at 20:00 UTC, never holds overnight.

## The limits (all in `config.py`)

| Limit | Setting |
|---|---|
| Risk per trade | 2% of the account (often less: position size is capped at 10x leverage) |
| Trades open at once | 3 |
| Trades per day | 10 |
| Losses in a row | 4, then done for the day |
| Daily loss | -6%: closes everything, stops until tomorrow |
| Total drop from the account's high | -15%: closes everything, stops until you restart it |
| Spread | skips the trade if the spread is too expensive |

## Set up (one time)

On the server, paste this one line:

```
sudo apt-get update && sudo apt-get install -y git && git clone https://github.com/starshahuri-blip/forex-bot && bash forex-bot/install.sh
```

It asks for your OANDA API token and account ID, tests the connection, and starts the bot.

## Everyday commands

| I want to... | Paste this |
|---|---|
| See what the bot is doing | `tail -n 30 ~/forex-bot/bot.log` |
| Test the strategy on the past 180 days | `cd ~/forex-bot && python3 backtest.py 180` |
| Pause it and close all trades | `touch ~/forex-bot/STOP` |
| Un-pause | `rm ~/forex-bot/STOP` |
| Restart after the -15% halt | `rm ~/forex-bot/HALT` |
| Get the newest version of the code | `cd ~/forex-bot && git pull && sudo systemctl restart forexbot` |
| Switch to real money | `bash ~/forex-bot/install.sh live` |

## Emergency stop from your phone (no server needed)

1. OANDA website -> My Services -> Manage API Access -> **Revoke** the token. The bot can no longer trade.
2. OANDA app -> close any open trades.

## Important

- Use an OANDA account that only the bot trades in. Don't place manual trades in it.
- Adding or withdrawing money mid-day confuses the daily-loss maths for that day.
- Optional phone alerts: add `TG_TOKEN=` and `TG_CHAT=` (Telegram bot token and chat ID) to the `.env` file.
- No strategy is guaranteed to make money. Run the backtest before going live.
