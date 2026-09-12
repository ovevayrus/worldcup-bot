import unittest
from types import SimpleNamespace
from unittest.mock import patch

import bot


class FakeMessage:
    def __init__(self):
        self.replies = []

    async def reply_text(self, text, **kwargs):
        self.replies.append((text, kwargs))


class FakeCallbackQuery:
    def __init__(self, user=None, data=None):
        self.from_user = user
        self.data = data
        self.answers = []

    async def answer(self, text=None, **kwargs):
        self.answers.append((text, kwargs))


class FakeApplication:
    def __init__(self):
        self.handlers = []
        self.webhook_kwargs = None

    def add_handler(self, handler):
        self.handlers.append(handler)

    def run_webhook(self, **kwargs):
        self.webhook_kwargs = kwargs

    def run_polling(self, **kwargs):
        raise AssertionError("webhook test unexpectedly started polling")


class FakeApplicationBuilder:
    def __init__(self, application):
        self.application = application
        self.token_value = None

    def token(self, value):
        self.token_value = value
        return self

    def build(self):
        return self.application


def make_update(*, chat_id=100, user_id=200, callback_data=None):
    message = FakeMessage()
    user = SimpleNamespace(id=user_id, first_name="Player")
    callback_query = (
        FakeCallbackQuery(user=user, data=callback_data)
        if callback_data is not None
        else None
    )
    return SimpleNamespace(
        effective_chat=SimpleNamespace(id=chat_id, type="private"),
        effective_user=user,
        effective_message=message,
        message=message,
        callback_query=callback_query,
    )


class ConfigTests(unittest.TestCase):
    def test_parse_chat_ids(self):
        self.assertEqual(
            bot.parse_id_set("CHATS", "-1001, 2002 -1001"),
            frozenset({-1001, 2002}),
        )

    def test_rejects_invalid_ids(self):
        invalid_values = ("*", "0", str(2**63))
        for value in invalid_values:
            with self.subTest(value=value), self.assertRaises(SystemExit):
                bot.parse_id_set("CHATS", value)

        with self.assertRaises(SystemExit):
            bot.parse_id_set("ADMINS", "-123", positive_only=True)

    def test_webhook_config_is_fail_closed(self):
        secret = "a" * 32
        self.assertIsNone(bot.parse_webhook_config(None, None, ""))
        with self.assertRaises(SystemExit):
            bot.parse_webhook_config("10000", None, secret)
        with self.assertRaises(SystemExit):
            bot.parse_webhook_config("10000", "http://example.com", secret)
        with self.assertRaises(SystemExit):
            bot.parse_webhook_config("10000", "https://example.com/base", secret)
        with self.assertRaises(SystemExit):
            bot.parse_webhook_config("10000", "https://example.com", "short")
        self.assertEqual(
            bot.parse_webhook_config("10000", "https://example.com/", secret),
            (10000, "https://example.com"),
        )

    def test_vote_callback_pattern_is_strict(self):
        self.assertIsNotNone(bot.VOTE_CALLBACK_RE.fullmatch("vote:12:home"))
        self.assertIsNone(bot.VOTE_CALLBACK_RE.fullmatch("12:home"))
        self.assertIsNone(bot.VOTE_CALLBACK_RE.fullmatch("vote:12:home:extra"))

    def test_webhook_url_never_contains_the_bot_token(self):
        application = FakeApplication()
        builder = FakeApplicationBuilder(application)
        fake_token = "fake-bot-token"
        secret = "s" * 32
        with (
            patch.dict(
                bot.os.environ,
                {"PORT": "8443", "PUBLIC_URL": "https://example.com"},
                clear=True,
            ),
            patch.object(bot, "BOT_TOKEN", fake_token),
            patch.object(bot, "DATABASE_URL", "postgresql://configured"),
            patch.object(bot, "WEBHOOK_SECRET", secret),
            patch.object(bot, "ALLOWED_CHAT_IDS", frozenset({100})),
            patch.object(bot, "ADMIN_USER_IDS", frozenset({200})),
            patch.object(bot, "init_db"),
            patch.object(bot.Application, "builder", return_value=builder),
        ):
            bot.main()

        self.assertEqual(builder.token_value, fake_token)
        self.assertEqual(application.webhook_kwargs["secret_token"], secret)
        self.assertNotIn(fake_token, application.webhook_kwargs["webhook_url"])
        self.assertEqual(application.webhook_kwargs["url_path"], "telegram")

    def test_webhook_mode_requires_postgres(self):
        with (
            patch.dict(
                bot.os.environ,
                {"PORT": "8443", "PUBLIC_URL": "https://example.com"},
                clear=True,
            ),
            patch.object(bot, "BOT_TOKEN", "fake-token"),
            patch.object(bot, "DATABASE_URL", ""),
            patch.object(bot, "WEBHOOK_SECRET", "s" * 32),
            patch.object(bot, "init_db") as init_db,
            self.assertRaisesRegex(SystemExit, "DATABASE_URL"),
        ):
            bot.main()
        init_db.assert_not_called()


class AccessTests(unittest.IsolatedAsyncioTestCase):
    async def test_disallowed_chat_is_denied(self):
        update = make_update(chat_id=99)
        with patch.object(bot, "ALLOWED_CHAT_IDS", frozenset({100})):
            self.assertFalse(await bot.require_allowed_chat(update))
        self.assertEqual(len(update.message.replies), 1)

    async def test_non_admin_cannot_reach_mutation_code(self):
        update = make_update(chat_id=100, user_id=201)
        context = SimpleNamespace(args=["France", "vs", "Brazil"])
        with (
            patch.object(bot, "ALLOWED_CHAT_IDS", frozenset({100})),
            patch.object(bot, "ADMIN_USER_IDS", frozenset({200})),
            patch.object(bot, "db", side_effect=AssertionError("database was opened")),
        ):
            await bot.addmatch(update, context)
        self.assertIn("admin", update.message.replies[0][0])

    async def test_admin_must_also_be_in_an_allowed_chat(self):
        update = make_update(chat_id=99, user_id=200)
        with (
            patch.object(bot, "ALLOWED_CHAT_IDS", frozenset({100})),
            patch.object(bot, "ADMIN_USER_IDS", frozenset({200})),
        ):
            self.assertFalse(await bot.require_admin(update))

    async def test_whereami_is_available_before_allowlisting(self):
        update = make_update(chat_id=-1001, user_id=200)
        with patch.object(bot, "ALLOWED_CHAT_IDS", frozenset()):
            await bot.whereami(update, SimpleNamespace())
        reply = update.message.replies[0][0]
        self.assertIn("200", reply)
        self.assertIn("-1001", reply)

    async def test_unauthorized_callback_is_answered_once_without_database_access(self):
        update = make_update(chat_id=99, callback_data="vote:1:home")
        with (
            patch.object(bot, "ALLOWED_CHAT_IDS", frozenset({100})),
            patch.object(
                bot, "get_player", side_effect=AssertionError("database was read")
            ),
        ):
            await bot.vote_button(update, SimpleNamespace())
        self.assertEqual(len(update.callback_query.answers), 1)


if __name__ == "__main__":
    unittest.main()
