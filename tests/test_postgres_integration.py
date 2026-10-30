import os
import unittest
import uuid
from types import SimpleNamespace
from unittest.mock import patch
from urllib.parse import urlsplit

import bot


@unittest.skipUnless(
    os.environ.get("WORLDCUP_RUN_PG_TESTS") == "1",
    "isolated PostgreSQL integration requires explicit opt-in and a loopback test database",
)
class PostgresTransactions(unittest.TestCase):
    def test_real_schema_rollback_and_duplicate_receipts(self):
        import psycopg2
        from psycopg2 import sql

        url = os.environ.get("WORLDCUP_TEST_DATABASE_URL", "")
        parsed = urlsplit(url)
        self.assertIn(parsed.hostname, ("localhost", "127.0.0.1", "::1"))
        self.assertTrue(parsed.path.lstrip("/").startswith("worldcup_test"))
        schema = "wc_test_" + uuid.uuid4().hex
        real_connect = psycopg2.connect
        admin = real_connect(url, connect_timeout=5)
        admin.autocommit = True
        with admin.cursor() as cursor:
            cursor.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))

        def scoped_connect(*args, **kwargs):
            kwargs["options"] = kwargs.get("options", "") + f" -c search_path={schema}"
            return real_connect(*args, **kwargs)

        try:
            with (
                patch.object(bot, "DATABASE_URL", url),
                patch("psycopg2.connect", scoped_connect),
            ):
                bot.init_db()
                item = SimpleNamespace(update_id=123)
                with self.assertRaises(RuntimeError), bot.db(update=item) as conn:
                    conn.execute(
                        "INSERT INTO matches(home,away) VALUES (?,?)",
                        ("Synthetic A", "Synthetic B"),
                    )
                    raise RuntimeError("rollback")
                with bot.db(update=item) as conn:
                    conn.execute(
                        "INSERT INTO matches(home,away) VALUES (?,?)",
                        ("Synthetic A", "Synthetic B"),
                    )
                with self.assertRaises(bot.DuplicateUpdate), bot.db(update=item):
                    pass
                with bot.db() as conn:
                    self.assertEqual(
                        conn.execute("SELECT COUNT(*) AS n FROM matches").fetchone()[
                            "n"
                        ],
                        1,
                    )
        finally:
            with admin.cursor() as cursor:
                cursor.execute(
                    sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(schema))
                )
            admin.close()
