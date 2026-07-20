"""Import/construction smoke test — cheap guard that the refactor didn't break
the module graph or the bot's public surface. No browser or network involved.

Run: python -m unittest tests.test_smoke
"""

import importlib
import unittest


class TestImports(unittest.TestCase):
    def test_all_modules_import(self):
        for mod in (
            "src.main",
            "src.supervisor",
            "src.settings",
            "src.vfs_bot.vfs_bot",
            "src.vfs_bot.vfs_bot_factory",
            "src.vfs_bot.errors",
            "src.vfs_bot.block_detection",
            "src.vfs_bot.diagnostics",
            "src.vfs_bot.turnstile",
            "src.vfs_bot.session",
            "src.vfs_bot.dom_utils",
            "src.vfs_bot.otp_flow",
            "src.vfs_bot.slot_check",
            "src.vfs_bot.browser_setup",
            "src.utils.config_reader",
            "src.utils.credentials",
            "src.utils.account_health",
            "src.utils.proxy_pool",
            "src.utils.route_schema",
            "src.utils.telegram_message",
        ):
            with self.subTest(module=mod):
                importlib.import_module(mod)


class TestExceptionIdentity(unittest.TestCase):
    """The supervisor catches exception classes by identity — the re-export from
    vfs_bot.py must be the SAME object as the one defined in errors.py."""

    def test_reexport_identity(self):
        import src.vfs_bot.errors as errors
        import src.vfs_bot.vfs_bot as vfs_bot
        import src.supervisor as supervisor

        for name in ("LoginError", "IpBlockedError", "RetryableError",
                     "AccessRestrictedError", "AccountBlockedError"):
            canonical = getattr(errors, name)
            self.assertIs(getattr(vfs_bot, name), canonical)
            if hasattr(supervisor, name):
                self.assertIs(getattr(supervisor, name), canonical)


class TestBotSurface(unittest.TestCase):
    def test_bot_constructs_with_selectors(self):
        from src.utils.config_reader import initialize_config
        from src.vfs_bot.vfs_bot_factory import get_vfs_bot

        initialize_config()
        bot = get_vfs_bot("AE", "ITA")
        # Public/critical surface used by the supervisor and factory.
        for attr in ("run", "set_credential", "selectors", "schema"):
            self.assertTrue(hasattr(bot, attr), f"VfsBot lost '{attr}'")
        # Selectors fall back to the built-in defaults before run() customises them.
        self.assertIn("username", bot.selectors)
        self.assertTrue(bot.selectors["username"])


if __name__ == "__main__":
    unittest.main()
