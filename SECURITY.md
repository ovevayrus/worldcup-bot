# Security and private pool data

Report vulnerabilities privately to the maintainer or through GitHub private vulnerability reporting when enabled. Do not post tokens, database URLs, real chat IDs, player lists, screenshots, or sensitive reproduction details in a public issue.

Keep `WORLDCUP_BOT_TOKEN`, `WORLDCUP_WEBHOOK_SECRET`, `DATABASE_URL` and database backups private. If a credential was exposed, revoke or rotate it first and review provider activity. Deleting a file does not remove a credential from history.

Use separate test tokens and databases. Startup refuses to replace a different webhook unless an operator explicitly configures `WORLDCUP_ALLOW_WEBHOOK_RESET=true`. No test or screenshot workflow should change a production webhook. Deployments, rotations, data deletions and billing controls require their own operational review.

All allowed chats intentionally share one pool. A chat allowlist is an audience boundary, not tenant isolation. Player names, IDs, picks and results remain in the database until the owner follows an agreed retention/export procedure. Group membership caching can lag a change by five minutes. Review the README's finite work/storage limits, additive schema and rollback steps before rollout; application quotas do not guarantee a hosting bill.

Unit tests clear service credentials and block external networking. The separate PostgreSQL integration test requires explicit opt-in, loopback and a dedicated test database. Neither a clean scanner result nor passing tests establishes complete production safety.
