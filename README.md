# World Cup prediction bot

Run a small football prediction pool in Telegram. Players pick home win, draw, or away win; a configured admin adds matches and records results, and the bot keeps the leaderboard.

This is a self-hosted bot for one trusted pool. All allowlisted chats share the same players, matches, and picks. Start with a separate test bot and SQLite; hosted webhooks use Postgres.

Example: an admin sends `/addmatch France vs Brazil`, a player joins with `/start` and picks through `/vote`, then `/result 1 home` scores the result.

I built this Telegram bot for a private World Cup pool with friends. Players
pick match results, then the bot scores the picks and keeps a leaderboard. It
uses SQLite when run locally and Postgres when deployed.

## What works today

Matches and results are entered manually. Each correct outcome adds one correct pick to the leaderboard; this bot does not fetch live fixtures or scores. There is no automatic kickoff lock: voting remains available until an admin records the result. Group vote replies reveal the player's pick, and all allowed chats intentionally use one pool.

## Commands

- `/start` joins the pool.
- `/vote` shows the current match and lets a player make or change a pick.
- `/matches [page]` lists up to 20 matches and their status.
- `/leaderboard [page]` shows up to 20 players from a pool ranking page.
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

Use Python 3.12 in a new virtual environment without production credentials, then install the hashed wheel-only dependencies and copy the sample configuration:

```powershell
python -m pip install --only-binary=:all: --require-hashes -r requirements.lock
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

Startup checks the existing Telegram webhook before the polling library can remove it. If one is configured, the app refuses to switch delivery modes. Use a separate test bot. An operator deliberately replacing a webhook may set `WORLDCUP_ALLOW_WEBHOOK_RESET=true`; that switch affects the token and must not be part of a local screenshot exercise.

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
4. Stop any consumer using that token before an operator-approved deployment. The app refuses to replace a different webhook unless `WORLDCUP_ALLOW_WEBHOOK_RESET=true` is explicitly set. Keep that flag false for normal operation.

`WORLDCUP_MODE=auto` selects hosted webhooks only when the required URL/port are present; `polling` and `webhook` explicitly select the intended setup and reject incompatible configuration. The Blueprint selects webhook mode and Python 3.12.14. Its `plan: free` is a source example, not verification of your actual Render or Neon plan, current availability, or charges.

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

## Work limits and data lifecycle

These technical limits do not choose a provider spending budget:

| Variable | Default | Purpose |
| --- | --- | --- |
| `WORLDCUP_GLOBAL_PER_MINUTE` | 120 | Shared accepted commands per minute |
| `WORLDCUP_CHAT_PER_MINUTE` | 30 | Accepted commands per chat per minute |
| `WORLDCUP_USER_PER_MINUTE` | 10 | Accepted commands per user per minute |
| `WORLDCUP_MAX_PLAYERS` | 500 | New registrations stop at this capacity |
| `WORLDCUP_MAX_MATCHES` | 1000 | New matches stop at this capacity |
| `WORLDCUP_MAX_RECEIPTS` | 100000 | New mutations wait when dedup storage is full |

Admission and mutation receipts are stored in the same database used by every consumer. SQLite uses an immediate transaction and Postgres uses a transaction advisory lock. A mutation's update ID commits with its state change; a repeated update does not add another match, advance twice, or overwrite a newer vote. Receipts expire after 48 hours on later mutation activity. Their count is bounded; this is not an exactly-once guarantee for Telegram replies or update delivery across crashes.

Commands have a ten-second asynchronous work deadline. Database connection, lock and query work have five-second limits. Match and ranking output has 20-row pages, and group membership checks use a five-minute cache capped at 2,000 rows. A page is taken from the shared pool ranking before group filtering; a group page can be empty. Group membership changes can remain visible in cached rankings for up to five minutes. `/whereami` remains available before allowlisting but shares the finite admission limits.

Players, matches and votes have no automatic deletion; only the temporary control/cache rows are pruned. Before archiving a tournament, stop the consumer, export the intended database privately, review retention with the pool members, and agree on a new empty test/season database. Do not delete production rows or change database URLs merely to pass a test. Schema additions are additive; back up the database before any approved rollout. Rollback should stop the new consumer and restore the previous app/database backup together if data compatibility is uncertain.

Render's build-pipeline spend cap only stops new builds; it does not cap service compute or bandwidth. Neon spending notifications are alerts, not a work stop. Review actual provider plans, usage, autoscaling/compute ceilings and storage retention with the owner before public use. No monetary budget is selected by this project. References: [Render build pipeline](https://render.com/docs/build-pipeline), [Render FAQ](https://render.com/docs/faq), [Neon spending notifications](https://neon.com/docs/introduction/spending-notifications).

## Tests

Run the built-in test suite from the project directory:

```powershell
python scripts/test_offline.py
```


The offline harness clears service credentials, uses temporary SQLite, and blocks external DNS/connections while permitting loopback for Python's event loop. It is venv/process isolation, not a container or filesystem security boundary. Never put a production `local.env` in a test checkout. Tests cover authorization, replay/rollback, concurrent duplicate mutations, work/storage limits, pagination, cache expiry and delivery-mode guards.

The PostgreSQL integration test is opt-in and refuses a non-loopback URL or a database name not beginning `worldcup_test`. CI defines an ephemeral Postgres service with synthetic credentials and a pinned image digest. To run it locally, create a dedicated test database first, set `WORLDCUP_RUN_PG_TESTS=1` and `WORLDCUP_TEST_DATABASE_URL` in a clean shell, then run `python -m unittest discover -s tests -p test_postgres_integration.py -v`. It creates and removes only a randomly named test schema. This integration was not run locally when no isolated PostgreSQL runtime was available; the default offline suite reports that skip.

## Contributing and product images

Keep changes focused and run the offline suite plus `python -m pip check`. Regenerate `requirements.lock` from the compatible ranges in `requirements.txt`, audit the resolved versions, and test before updating the Blueprint. Do not use a production token to take product screenshots.

Authentic vote-keyboard and leaderboard images are blocked until a separate authorized test bot and consenting test accounts are available. No Telegram conversation or product screenshot is fabricated here. The command example above is a text example of the implemented workflow.

No project license file is currently included; ask the maintainer before assuming reuse permissions. Preserve contributor and source attribution.
