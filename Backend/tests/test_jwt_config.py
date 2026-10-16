"""Isolated configuration checks; never read a dotenv file or private settings.

The stdlib checks execute the real module body with a minimal BaseSettings
substitute. The additional real-pydantic-settings check runs only if installed.
Run from Backend: python -B -m unittest discover -s tests -v
"""
import ast
from contextlib import redirect_stderr, redirect_stdout
import importlib.util
import io
import os
from pathlib import Path
import runpy
import unittest
from unittest.mock import patch


CONFIG = Path(__file__).resolve().parents[1] / "app/core/config.py"
MISSING = object()
TEST_KEY = "  isolated-test-key-not-for-deployment  "


class JWTConfigTests(unittest.TestCase):
    def setUp(self):
        environment = patch.dict(os.environ, {}, clear=True)
        environment.start()
        self.addCleanup(environment.stop)

    def load_with_substitute(self, secret=MISSING, **values):
        """Exercise Settings.__init__ and module-level settings without a loader."""
        class BaseSettingsSubstitute:
            def __init__(self, **kwargs):
                supplied = dict(values)
                if secret is not MISSING:
                    supplied["SECRET_KEY"] = secret
                supplied.update(kwargs)
                self.__dict__.update(supplied)

        source = ast.parse(CONFIG.read_text(encoding="utf-8-sig"))
        source.body = [n for n in source.body if not isinstance(n, (ast.Import, ast.ImportFrom))]
        namespace = {"BaseSettings": BaseSettingsSubstitute}
        exec(compile(source, str(CONFIG), "exec"), namespace)
        return namespace

    def test_missing_key_rejects_module_initialization(self):
        with self.assertRaisesRegex(ValueError, "SECRET_KEY"):
            self.load_with_substitute()

    def test_empty_key_rejected(self):
        with self.assertRaisesRegex(ValueError, "SECRET_KEY"):
            self.load_with_substitute("")

    def test_whitespace_only_keys_rejected(self):
        for secret in (" ", "\t\r\n", " \t ", "\u3000"):
            with self.subTest(kind=repr(secret)), self.assertRaisesRegex(ValueError, "SECRET_KEY"):
                self.load_with_substitute(secret)

    def test_valid_key_preserved_exactly(self):
        for secret in (TEST_KEY, "test-only", "\tvalid-test-key\n"):
            with self.subTest():
                namespace = self.load_with_substitute(secret)
                self.assertEqual(namespace["settings"].SECRET_KEY, secret)
                explicit = namespace["Settings"](SECRET_KEY=TEST_KEY)
                self.assertEqual(explicit.SECRET_KEY, TEST_KEY)

    def test_error_output_does_not_disclose_configuration(self):
        namespace = self.load_with_substitute(TEST_KEY)
        self.assertTrue(namespace["Settings"].Config.hide_input_in_errors)
        output = io.StringIO()
        with redirect_stdout(output), redirect_stderr(output):
            try:
                self.load_with_substitute("", MAIL_PASSWORD=TEST_KEY, POSTGRES_PASSWORD=TEST_KEY)
            except ValueError as error:
                print(error)
            else:
                self.fail("Blank key accepted")
        self.assertIn("SECRET_KEY", output.getvalue())
        self.assertNotIn(TEST_KEY, output.getvalue())
        self.assertNotIn("MAIL_PASSWORD", output.getvalue())
        self.assertNotIn("POSTGRES_PASSWORD", output.getvalue())

    def test_existing_celery_url_initialization_preserved(self):
        for password, expected in (("", "redis://redis:6379/0"), ("test-only", "redis://:test-only@redis:6379/0")):
            with self.subTest(authenticated=bool(password)):
                settings = self.load_with_substitute(TEST_KEY, REDIS_HOST="redis", REDIS_PASSWORD=password)["settings"]
                self.assertEqual(settings.CELERY_BROKER_URL, expected)
                self.assertEqual(settings.CELERY_RESULT_BACKEND, expected)

    @unittest.skipUnless(importlib.util.find_spec("pydantic_settings"), "pydantic-settings is not installed; real loader unverified")
    def test_real_settings_loader_with_dotenv_disabled(self):
        from pydantic import ValidationError
        from pydantic_settings import BaseSettings

        original_init = BaseSettings.__init__

        def isolated_init(instance, **kwargs):
            kwargs["_env_file"] = None
            kwargs["_secrets_dir"] = None
            original_init(instance, **kwargs)

        with patch.object(BaseSettings, "__init__", isolated_init):
            for secret in (MISSING, "", " \t\n", TEST_KEY):
                with self.subTest(configured=secret is not MISSING):
                    os.environ.clear()
                    if secret is not MISSING:
                        os.environ["SECRET_KEY"] = secret
                    if secret is MISSING or not secret.strip():
                        with self.assertRaisesRegex(ValueError, "SECRET_KEY"):
                            runpy.run_path(str(CONFIG))
                    else:
                        namespace = runpy.run_path(str(CONFIG))
                        self.assertEqual(namespace["settings"].SECRET_KEY, TEST_KEY)
                        with self.assertRaises(ValidationError) as caught:
                            namespace["Settings"](SECRET_KEY={"private": TEST_KEY})
                        self.assertNotIn(TEST_KEY, str(caught.exception))


if __name__ == "__main__":
    unittest.main()
