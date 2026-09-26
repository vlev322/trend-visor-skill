import os
import unittest
from importlib.util import find_spec
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from tools.model_eval.config import ModelConfig, load_config
from tools.pageviews.errors import PageviewsError


class ModelConfigTests(unittest.TestCase):
    def test_config_rejects_unsafe_urls_and_never_repr_includes_key(self):
        config = ModelConfig("https://models.example/v1", "qwen3-coder-next", "fixture-secret")
        self.assertNotIn("fixture-secret", repr(config))
        for url in (
            "http://models.example/v1", "https://user:password@models.example/v1",
            "https://models.example/v1?api_key=fixture", "https://models.example/v1#fragment",
            "https://models.example/\npath", "not-a-url",
        ):
            with self.subTest(url=url):
                with self.assertRaises(PageviewsError) as caught:
                    ModelConfig(url, "gemma4", "fixture-secret")
                self.assertEqual(caught.exception.code, "invalid_request")
                self.assertNotIn("fixture-secret", str(caught.exception))

    def test_placeholder_or_missing_key_is_not_a_working_configuration(self):
        for key in ("", "replace_with_your_api_key", "invalid\nheader"):
            with self.subTest(key=key):
                with self.assertRaises(PageviewsError) as caught:
                    ModelConfig("https://models.example/v1", "qwen3-coder-next", key)
                self.assertEqual(caught.exception.code, "model_not_configured")

    def test_credential_cannot_be_used_in_public_model_metadata(self):
        for url, model in (
            ("https://models.example/v1/fixture-secret", "gemma4"),
            ("https://models.example/v1", "fixture-secret"),
        ):
            with self.subTest(url=url, model=model):
                with self.assertRaises(PageviewsError) as caught:
                    ModelConfig(url, model, "fixture-secret")
                self.assertEqual(caught.exception.code, "invalid_request")
                self.assertNotIn("fixture-secret", str(caught.exception))

    def test_oversized_or_invalid_utf8_env_file_is_rejected(self):
        with TemporaryDirectory() as directory, patch.dict(os.environ, {}, clear=True):
            path = Path(directory) / ".env"
            for body in (b"x" * 65537, b"\xff\xfe"):
                path.write_bytes(body)
                with self.assertRaises(PageviewsError) as caught:
                    load_config(path)
                self.assertEqual(caught.exception.code, "model_not_configured")

    @unittest.skipUnless(find_spec("dotenv") is not None, "Optional evaluation extra is not installed")
    def test_explicit_env_file_and_environment_override_without_mutation(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / ".env"
            path.write_text(
                'TREND_VISOR_LLM_BASE_URL="https://models.example/v1"\n'
                'TREND_VISOR_LLM_MODEL=qwen3-coder-next\n'
                'TREND_VISOR_LLM_API_KEY="fixture-${TOKEN}"\n',
            )
            environment = {"TREND_VISOR_LLM_MODEL": "gemma4", "TOKEN": "must-not-expand"}
            with patch.dict(os.environ, environment, clear=True):
                config = load_config(path)
                self.assertEqual(config.model, "gemma4")
                self.assertEqual(config.api_key, "fixture-${TOKEN}")
                self.assertEqual(dict(os.environ), environment)
                self.assertEqual(load_config(path, model="qwen3-coder-next").model, "qwen3-coder-next")

    @unittest.skipUnless(find_spec("dotenv") is not None, "Optional evaluation extra is not installed")
    def test_no_implicit_parent_file_or_unrelated_openai_credentials(self):
        with TemporaryDirectory() as directory, patch.dict(os.environ, {"OPENAI_API_KEY": "other-account"}, clear=True):
            with self.assertRaises(PageviewsError) as caught:
                load_config(Path(directory) / "missing.env")
            self.assertEqual(caught.exception.code, "model_not_configured")
            self.assertNotIn("other-account", str(caught.exception))