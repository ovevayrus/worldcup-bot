import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import bot


def update(number=1, user=200, chat=100, chat_type="private", data=None):
    message = SimpleNamespace(replies=[])

    async def reply(text, **kwargs):
        message.replies.append(text)

    message.reply_text = reply
    person = SimpleNamespace(id=user, first_name="Synthetic")
    query = None
    if data:
        query = SimpleNamespace(from_user=person, data=data, answers=[], edits=[])

        async def answer(text=None, **kwargs):
            query.answers.append(text)

        async def edit(text, **kwargs):
            query.edits.append(text)

        query.answer = answer
        query.edit_message_text = edit
    return SimpleNamespace(
        update_id=number,
        effective_chat=SimpleNamespace(id=chat, type=chat_type),
        effective_user=person,
        effective_message=message,
        message=message,
        callback_query=query,
    )


class DatabaseFixture:
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="worldcup-synthetic-")
        self.patches = [
            patch.object(bot, "DATABASE_URL", ""),
            patch.object(bot, "DB_FILE", str(Path(self.temp.name) / "test.db")),
            patch.object(bot, "ALLOWED_CHAT_IDS", frozenset({100})),
            patch.object(bot, "ADMIN_USER_IDS", frozenset({200})),
        ]
        for p in self.patches:
            p.start()
        bot.init_db()

    def tearDown(self):
        for p in reversed(self.patches):
            p.stop()
        self.temp.cleanup()

    def matches(self, count=3):
        with bot.db() as conn:
            conn.executemany(
                "INSERT INTO matches (home,away) VALUES (?,?)",
                [(f"Home {i}", f"Away {i}") for i in range(count)],
            )


class TransactionTests(DatabaseFixture, unittest.TestCase):
    def test_receipt_and_mutation_commit_once_across_two_consumers(self):
        def apply():
            try:
                with bot.db(update=update(42)) as conn:
                    conn.execute(
                        "INSERT INTO matches (home,away) VALUES (?,?)",
                        ("Synthetic A", "Synthetic B"),
                    )
                return "committed"
            except bot.DuplicateUpdate:
                return "duplicate"

        with ThreadPoolExecutor(max_workers=2) as workers:
            outcomes = list(workers.map(lambda _: apply(), range(2)))
        self.assertCountEqual(outcomes, ["committed", "duplicate"])
        with bot.db() as conn:
            self.assertEqual(
                conn.execute("SELECT COUNT(*) AS n FROM matches").fetchone()["n"], 1
            )
            self.assertEqual(
                conn.execute("SELECT COUNT(*) AS n FROM processed_updates").fetchone()[
                    "n"
                ],
                1,
            )

    def test_rollback_allows_retry_and_unseen_lower_ids(self):
        with (
            self.assertRaisesRegex(RuntimeError, "rollback"),
            bot.db(update=update(1000)) as conn,
        ):
            conn.execute("INSERT INTO matches (home,away) VALUES (?,?)", ("A", "B"))
            raise RuntimeError("rollback")
        with bot.db(update=update(1000)):
            pass
        with bot.db(update=update(1)):
            pass
        with bot.db() as conn:
            self.assertEqual(
                conn.execute("SELECT COUNT(*) AS n FROM matches").fetchone()["n"], 0
            )

    def test_receipt_storage_stops_at_capacity_then_expiry_frees_it(self):
        with (
            patch.object(bot, "MAX_RECEIPTS", 1),
            patch.object(bot.time, "time", return_value=200000),
        ):
            with bot.db(update=update(1)):
                pass
            with self.assertRaises(bot.WorkLimitError), bot.db(update=update(2)):
                pass
        with (
            patch.object(bot, "MAX_RECEIPTS", 1),
            patch.object(bot.time, "time", return_value=400000),
            bot.db(update=update(2)),
        ):
            pass
        with bot.db() as conn:
            self.assertEqual(
                conn.execute("SELECT COUNT(*) AS n FROM processed_updates").fetchone()[
                    "n"
                ],
                1,
            )

    def test_command_quotas_are_shared_and_reset(self):
        with (
            patch.object(bot, "GLOBAL_PER_MINUTE", 2),
            patch.object(bot.time, "time", return_value=1200),
        ):
            self.assertTrue(bot.admit_command(100, 1))
            self.assertTrue(bot.admit_command(101, 2))
            self.assertFalse(bot.admit_command(102, 3))
        with patch.object(bot.time, "time", return_value=1260):
            self.assertTrue(bot.admit_command(102, 3))


class HandlerTests(DatabaseFixture, unittest.IsolatedAsyncioTestCase):
    async def test_duplicate_addmatch_and_next_do_not_repeat_mutations(self):
        context = SimpleNamespace(args=["France", "vs", "Brazil"])
        first = update(10)
        await bot.addmatch(first, context)
        await bot.addmatch(first, context)
        self.matches(2)
        with bot.db() as conn:
            bot.set_setting(conn, bot.CURRENT_MATCH_KEY, 1)
        advance = update(11)
        await bot.next_game(advance, SimpleNamespace(args=["game"]))
        await bot.next_game(advance, SimpleNamespace(args=["game"]))
        with bot.db() as conn:
            self.assertEqual(
                conn.execute("SELECT COUNT(*) AS n FROM matches").fetchone()["n"], 3
            )
            self.assertEqual(bot.get_setting(conn, bot.CURRENT_MATCH_KEY), "2")
        self.assertTrue(
            any("already processed" in reply for reply in first.message.replies)
        )

    async def test_older_duplicate_vote_cannot_undo_newer_pick(self):
        self.matches(1)
        with bot.db() as conn:
            conn.execute(
                "INSERT INTO players(user_id,name) VALUES(?,?)", (200, "Synthetic")
            )
        first = update(21, data="vote:1:home")
        await bot.vote_button(first, SimpleNamespace())
        await bot.vote_button(update(22, data="vote:1:away"), SimpleNamespace())
        await bot.vote_button(first, SimpleNamespace())
        with bot.db() as conn:
            self.assertEqual(
                conn.execute("SELECT pick FROM votes").fetchone()["pick"], "away"
            )
        self.assertIn("already processed", first.callback_query.answers[-1])

    async def test_group_leaderboard_has_bounded_page_and_membership_cache(self):
        with bot.db() as conn:
            conn.executemany(
                "INSERT INTO players(user_id,name) VALUES(?,?)",
                [(i, f"Player {i:03}") for i in range(1, 46)],
            )
        calls = []

        async def member(chat, user, **kwargs):
            calls.append(user)
            return SimpleNamespace(status="member")

        context = SimpleNamespace(args=[], bot=SimpleNamespace(get_chat_member=member))
        first = update(30, chat_type="supergroup")
        await bot.leaderboard(first, context)
        self.assertEqual(len(calls), 20)
        self.assertIn("Next pool page: /leaderboard 2", first.message.replies[-1])
        await bot.leaderboard(update(31, chat_type="supergroup"), context)
        self.assertEqual(len(calls), 20)
        context.args = ["2"]
        await bot.leaderboard(update(32, chat_type="supergroup"), context)
        self.assertEqual(len(calls), 40)
        with patch.object(bot.time, "time", return_value=bot.time.time() + 301):
            context.args = []
            await bot.leaderboard(update(33, chat_type="supergroup"), context)
        self.assertEqual(len(calls), 60)

    async def test_matches_pagination_and_invalid_page(self):
        self.matches(21)
        first = update(40)
        await bot.matches_cmd(first, SimpleNamespace(args=[]))
        self.assertIn("More matches: /matches 2", first.message.replies)
        second = update(41)
        await bot.matches_cmd(second, SimpleNamespace(args=["2"]))
        self.assertIn("Home 20", second.message.replies[-1])
        await bot.matches_cmd(second, SimpleNamespace(args=["999999999"]))
        self.assertIn("page number", second.message.replies[-1])

    async def test_configured_storage_caps_preserve_existing_rows(self):
        self.matches(1)
        with patch.object(bot, "MAX_MATCHES", 1):
            request = update(50)
            await bot.addmatch(request, SimpleNamespace(args=["New", "vs", "Other"]))
            self.assertIn("capacity", request.message.replies[-1])
        with bot.db() as conn:
            self.assertEqual(
                conn.execute("SELECT COUNT(*) AS n FROM matches").fetchone()["n"], 1
            )

    async def test_startup_preflight_never_resets_a_webhook_implicitly(self):
        async def info():
            return SimpleNamespace(url="https://production.example/telegram")

        fake = SimpleNamespace(get_webhook_info=info)
        with patch.object(bot, "ALLOW_WEBHOOK_RESET", False):
            with self.assertRaisesRegex(RuntimeError, "separate test bot"):
                await bot.guard_delivery_mode(fake)
            await bot.guard_delivery_mode(fake, "https://production.example/telegram")
        with patch.object(bot, "ALLOW_WEBHOOK_RESET", True):
            await bot.guard_delivery_mode(fake)


if __name__ == "__main__":
    unittest.main()
