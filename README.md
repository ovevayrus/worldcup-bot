# World Cup prediction bot

I built this Telegram bot for a private World Cup pool with friends. Players
pick match results, then the bot scores the picks and keeps a leaderboard. It
uses SQLite when run locally and Postgres when deployed.

## Commands

- `/start` joins the pool.
- `/vote` shows the current match and lets a player make or change a pick.
- `/matches` lists every match and its status.
- `/leaderboard` shows the current scores.
- `/whereami` shows the user and chat IDs needed for the allowlist.
- `/addmatch France vs Brazil` adds a match. Add `ko` at the end for a knockout
  match with no draw option.
- `/next game` opens the next match for voting. `/nextgame` and `/next_game`
  work too.
- `/result 1 home` records a result. The result can be `home`, `draw`, or
  `away`.

Only IDs listed in `WORLDCUP_ADMIN_USER_IDS` can add matches, advance the
current match, or record results.

## Run it locally

Install the dependencies and copy the sample configuration:

```powershell
pip install -r requirements.txt
Copy-Item local.env.example local.env
```

Put the token from BotFather in `local.env`, then start the bot:

```powershell
.\start-local.ps1
```

On macOS or Linux, run `cp local.env.example local.env` and `python bot.py`
instead.

The bot starts locked down. Send `/whereami` in each private or group chat you
want to use, copy those chat IDs into `WORLDCUP_ALLOWED_CHAT_IDS`, and add your
own user ID to `WORLDCUP_ADMIN_USER_IDS`. Separate multiple IDs with commas,
then restart the bot.

Local data is stored in `worldcup.db`. The database and `local.env` are ignored
by Git.

## Deploy on Render with Neon

The bot switches to webhooks and Postgres when both `PORT` and a public URL are
present.

1. Create a Neon project and copy its Postgres connection string.
2. In Render, create a Blueprint and connect this repository. Render reads
   `render.yaml` for the build and start commands.
3. Set these environment variables in Render:
   - `WORLDCUP_BOT_TOKEN`: the token from BotFather
   - `WORLDCUP_ALLOWED_CHAT_IDS`: the private and group chat IDs that may use
     the pool
   - `WORLDCUP_ADMIN_USER_IDS`: the user IDs allowed to manage matches
   - `WORLDCUP_WEBHOOK_SECRET`: a random 32 to 256 character string using
     letters, numbers, underscores, or hyphens
   - `DATABASE_URL`: the Neon connection string
4. Deploy, then stop any local copy so only one instance handles updates.

You can generate a webhook secret with:

```powershell
python -c "import secrets; print(secrets.token_urlsafe(32))"
```

## Security and privacy

Do not put tokens, database URLs, or real IDs in source files. Keep them in
`local.env` or the hosting provider's environment settings.

All allowed chats use the same pool and database. Only allow chats whose
members should see the same matches, first names, picks, and leaderboard. The
bot stores Telegram user IDs and first names until you delete the database or
the corresponding Postgres rows.

Webhook requests use a separate secret header. The Telegram bot token is never
placed in the public webhook URL.

## Tests

Run the built-in test suite from the project directory:

```powershell
python -m unittest discover -s tests -v
```
