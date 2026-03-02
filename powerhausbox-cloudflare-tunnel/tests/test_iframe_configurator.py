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
    SPEC.loader.exec_module(iframe_configurator)
else:
    iframe_configurator = None


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
            )

            self.assertEqual(result.status, iframe_configurator.STATUS_UPDATED_AND_RESTARTED)
            self.assertTrue(result.changed)
            self.assertTrue(result.backup_path.exists())

            parsed = yaml.safe_load(config_path.read_text(encoding="utf-8"))
            self.assertIn("http", parsed)
            self.assertFalse(parsed["http"]["use_x_frame_options"])
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
            )

            self.assertEqual(result.status, iframe_configurator.STATUS_UPDATED_AND_RESTARTED)
            parsed = yaml.safe_load(config_path.read_text(encoding="utf-8"))
            self.assertEqual(parsed["http"]["ssl_certificate"], "/ssl/fullchain.pem")
            self.assertEqual(parsed["http"]["ssl_key"], "/ssl/privkey.pem")
            self.assertFalse(parsed["http"]["use_x_frame_options"])

    def test_already_false_is_noop(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            original = "http:\n  use_x_frame_options: false\n"
            config_path = self._write_config(tmpdir, original)

            result = iframe_configurator.configure_iframe_embedding(
                config_path,
                lambda: (True, ""),
                lambda: (True, ""),
            )

            self.assertEqual(result.status, iframe_configurator.STATUS_ALREADY_CONFIGURED)
            self.assertFalse(result.changed)
            self.assertEqual(config_path.read_text(encoding="utf-8"), original)

    def test_invalid_yaml_no_write(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            original = "homeassistant: [\n"
            config_path = self._write_config(tmpdir, original)

            result = iframe_configurator.configure_iframe_embedding(
                config_path,
                lambda: (True, ""),
                lambda: (True, ""),
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
            )

            self.assertEqual(result.status, iframe_configurator.STATUS_FAILED_AND_ROLLED_BACK)
            self.assertTrue(result.changed)
            self.assertIn("Validation failed", result.message)
            self.assertEqual(config_path.read_text(encoding="utf-8"), original)

    def test_restart_failure_rolls_back_and_gives_manual_instruction(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            original = "default_config: {}\n"
            config_path = self._write_config(tmpdir, original)

            result = iframe_configurator.configure_iframe_embedding(
                config_path,
                lambda: (True, ""),
                lambda: (False, "restart endpoint unreachable"),
            )

            self.assertEqual(result.status, iframe_configurator.STATUS_FAILED_AND_ROLLED_BACK)
            self.assertTrue(result.changed)
            self.assertIn("Restart trigger failed", result.message)
            self.assertIn("Please restart Home Assistant Core manually", result.message)
            self.assertEqual(config_path.read_text(encoding="utf-8"), original)


if __name__ == "__main__":
    unittest.main()
