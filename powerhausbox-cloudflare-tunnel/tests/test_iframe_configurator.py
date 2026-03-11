import importlib.util
import tempfile
import unittest
from pathlib import Path

try:
    import yaml
except ModuleNotFoundError:  # pragma: no cover - host dev env may miss PyYAML
    yaml = None


MODULE_PATH = Path(__file__).resolve().parents[1] / "rootfs" / "opt" / "powerhausbox" / "iframe_configurator.py"
if yaml is not None:
    SPEC = importlib.util.spec_from_file_location("iframe_configurator", MODULE_PATH)
    iframe_configurator = importlib.util.module_from_spec(SPEC)
    assert SPEC and SPEC.loader
    import sys as _sys
    _sys.modules["iframe_configurator"] = iframe_configurator
    SPEC.loader.exec_module(iframe_configurator)
else:
    iframe_configurator = None


TEST_TRUSTED_PROXIES = ["172.30.33.1"]


@unittest.skipUnless(yaml is not None, "PyYAML is required to run iframe configurator tests.")
class IframeConfiguratorTests(unittest.TestCase):
    def _write_config(self, tmpdir: str, content: str) -> Path:
        config_path = Path(tmpdir) / "configuration.yaml"
        config_path.write_text(content, encoding="utf-8")
        return config_path

    def test_adds_http_block_when_missing(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            config_path = self._write_config(tmpdir, "default_config: {}\n")

            result = iframe_configurator.configure_iframe_embedding(
                config_path,
                lambda: (True, ""),
                lambda: (True, ""),
                TEST_TRUSTED_PROXIES,
            )

            self.assertEqual(result.status, iframe_configurator.STATUS_UPDATED_AND_RESTARTED)
            self.assertTrue(result.changed)
            self.assertTrue(result.backup_path.exists())

            parsed = yaml.safe_load(config_path.read_text(encoding="utf-8"))
            self.assertIn("http", parsed)
            self.assertFalse(parsed["http"]["use_x_frame_options"])
            self.assertTrue(parsed["http"]["use_x_forwarded_for"])
            self.assertEqual(parsed["http"]["trusted_proxies"], TEST_TRUSTED_PROXIES)
            self.assertIn("default_config", parsed)

    def test_keeps_existing_http_keys(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            config_path = self._write_config(
                tmpdir,
                "http:\n  ssl_certificate: /ssl/fullchain.pem\n  ssl_key: /ssl/privkey.pem\n",
            )

            result = iframe_configurator.configure_iframe_embedding(
                config_path,
                lambda: (True, ""),
                lambda: (True, ""),
                TEST_TRUSTED_PROXIES,
            )

            self.assertEqual(result.status, iframe_configurator.STATUS_UPDATED_AND_RESTARTED)
            parsed = yaml.safe_load(config_path.read_text(encoding="utf-8"))
            self.assertEqual(parsed["http"]["ssl_certificate"], "/ssl/fullchain.pem")
            self.assertEqual(parsed["http"]["ssl_key"], "/ssl/privkey.pem")
            self.assertFalse(parsed["http"]["use_x_frame_options"])
            self.assertTrue(parsed["http"]["use_x_forwarded_for"])
            self.assertEqual(parsed["http"]["trusted_proxies"], TEST_TRUSTED_PROXIES)

    def test_already_false_is_noop(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            original = (
                "http:\n"
                "  use_x_frame_options: false\n"
                "  use_x_forwarded_for: true\n"
                "  trusted_proxies:\n"
                "    - 172.30.33.1\n"
            )
            config_path = self._write_config(tmpdir, original)

            result = iframe_configurator.configure_iframe_embedding(
                config_path,
                lambda: (True, ""),
                lambda: (True, ""),
                TEST_TRUSTED_PROXIES,
            )

            self.assertEqual(result.status, iframe_configurator.STATUS_ALREADY_CONFIGURED)
            self.assertFalse(result.changed)
            self.assertEqual(config_path.read_text(encoding="utf-8"), original)

    def test_home_assistant_include_tags_are_preserved(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            config_path = self._write_config(
                tmpdir,
                (
                    "default_config:\n"
                    "frontend:\n"
                    "  themes: !include_dir_merge_named themes\n"
                    "automation: !include automations.yaml\n"
                ),
            )

            result = iframe_configurator.configure_iframe_embedding(
                config_path,
                lambda: (True, ""),
                lambda: (True, ""),
                TEST_TRUSTED_PROXIES,
            )

            self.assertEqual(result.status, iframe_configurator.STATUS_UPDATED_AND_RESTARTED)
            updated_text = config_path.read_text(encoding="utf-8")
            self.assertIn("themes: !include_dir_merge_named", updated_text)
            self.assertIn("automation: !include", updated_text)

            parsed = iframe_configurator.parse_configuration_yaml(config_path)
            self.assertIn("http", parsed)
            self.assertFalse(parsed["http"]["use_x_frame_options"])
            self.assertTrue(parsed["http"]["use_x_forwarded_for"])
            self.assertEqual(parsed["http"]["trusted_proxies"], TEST_TRUSTED_PROXIES)

    def test_invalid_yaml_no_write(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            original = "homeassistant: [\n"
            config_path = self._write_config(tmpdir, original)

            result = iframe_configurator.configure_iframe_embedding(
                config_path,
                lambda: (True, ""),
                lambda: (True, ""),
                TEST_TRUSTED_PROXIES,
            )

            self.assertEqual(result.status, iframe_configurator.STATUS_FAILED_AND_ROLLED_BACK)
            self.assertIn("Malformed YAML", result.message)
            self.assertFalse(result.changed)
            self.assertEqual(config_path.read_text(encoding="utf-8"), original)

    def test_validation_failure_rolls_back(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            original = "default_config: {}\n"
            config_path = self._write_config(tmpdir, original)

            result = iframe_configurator.configure_iframe_embedding(
                config_path,
                lambda: (False, "invalid config"),
                lambda: (True, ""),
                TEST_TRUSTED_PROXIES,
            )

            self.assertEqual(result.status, iframe_configurator.STATUS_FAILED_AND_ROLLED_BACK)
            self.assertTrue(result.changed)
            self.assertIn("Validation failed", result.message)
            self.assertEqual(config_path.read_text(encoding="utf-8"), original)

    def test_restart_failure_keeps_change_and_gives_manual_instruction(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            original = "default_config: {}\n"
            config_path = self._write_config(tmpdir, original)

            result = iframe_configurator.configure_iframe_embedding(
                config_path,
                lambda: (True, ""),
                lambda: (False, "restart endpoint unreachable"),
                TEST_TRUSTED_PROXIES,
            )

            self.assertEqual(result.status, iframe_configurator.STATUS_UPDATED_RESTART_REQUIRED)
            self.assertTrue(result.changed)
            self.assertIn("Restart trigger failed", result.message)
            self.assertIn("Please restart Home Assistant Core manually", result.message)
            parsed = iframe_configurator.parse_configuration_yaml(config_path)
            self.assertFalse(parsed["http"]["use_x_frame_options"])
            self.assertTrue(parsed["http"]["use_x_forwarded_for"])
            self.assertEqual(parsed["http"]["trusted_proxies"], TEST_TRUSTED_PROXIES)

    def test_existing_trusted_proxies_are_extended_without_duplicates(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            config_path = self._write_config(
                tmpdir,
                (
                    "http:\n"
                    "  use_x_frame_options: false\n"
                    "  use_x_forwarded_for: true\n"
                    "  trusted_proxies:\n"
                    "    - 10.0.0.5\n"
                    "    - 172.30.33.1\n"
                ),
            )

            result = iframe_configurator.configure_iframe_embedding(
                config_path,
                lambda: (True, ""),
                lambda: (True, ""),
                ["10.0.0.5", "172.30.33.1", "172.30.33.2"],
            )

            self.assertEqual(result.status, iframe_configurator.STATUS_UPDATED_AND_RESTARTED)
            parsed = iframe_configurator.parse_configuration_yaml(config_path)
            self.assertEqual(
                parsed["http"]["trusted_proxies"],
                ["10.0.0.5", "172.30.33.1", "172.30.33.2"],
            )


if __name__ == "__main__":
    unittest.main()
