import importlib.util
import json
import sys
import tempfile
import types
import unittest
from pathlib import Path


if "flask" not in sys.modules:
    flask_stub = types.ModuleType("flask")

    class _DummyFlask:
        def __init__(self, *args, **kwargs):
            self.config = {}
            self.secret_key = ""

        def context_processor(self, func):
            return func

        def get(self, *args, **kwargs):
            def decorator(func):
                return func

            return decorator

        def post(self, *args, **kwargs):
            def decorator(func):
                return func

            return decorator

        def before_request(self, func):
            return func

    flask_stub.Flask = _DummyFlask
    flask_stub.Response = object
    flask_stub.flash = lambda *args, **kwargs: None
    flask_stub.jsonify = lambda *args, **kwargs: {}
    flask_stub.redirect = lambda *args, **kwargs: None
    flask_stub.render_template = lambda *args, **kwargs: ""
    flask_stub.request = types.SimpleNamespace(headers={}, script_root="", form={}, path="/pairing", args={})
    flask_stub.session = {}
    sys.modules["flask"] = flask_stub


MODULE_PATH = Path(__file__).resolve().parents[1] / "rootfs" / "opt" / "powerhausbox" / "server.py"
SPEC = importlib.util.spec_from_file_location("powerhausbox_server_url_sync", MODULE_PATH)
server = importlib.util.module_from_spec(SPEC)
assert SPEC and SPEC.loader
SPEC.loader.exec_module(server)


class HomeAssistantUrlSyncTests(unittest.TestCase):
    def test_normalize_external_url_accepts_https_host(self) -> None:
        self.assertEqual(
            server.normalize_external_url("https://demo.powerhaus.ai"),
            "https://demo.powerhaus.ai",
        )

    def test_sync_homeassistant_urls_updates_core_config_storage(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            core_config_path = Path(temp_dir) / "core.config"
            core_config_path.write_text(
                json.dumps(
                    {
                        "version": 1,
                        "minor_version": 4,
                        "key": "core.config",
                        "data": {
                            "internal_url": None,
                            "external_url": None,
                        },
                    }
                ),
                encoding="utf-8",
            )

            original_core_config_storage_file = server.CORE_CONFIG_STORAGE_FILE
            original_run_with_core_stopped = server.run_with_core_stopped
            try:
                server.CORE_CONFIG_STORAGE_FILE = core_config_path
                server.run_with_core_stopped = lambda operation: operation()

                external_url = server.sync_homeassistant_urls("192.168.1.201:8123", "https://demo.powerhaus.ai")

                self.assertEqual(external_url, "https://demo.powerhaus.ai")
                updated = json.loads(core_config_path.read_text(encoding="utf-8"))
                self.assertEqual(updated["data"]["internal_url"], "http://192.168.1.201:8123")
                self.assertEqual(updated["data"]["external_url"], "https://demo.powerhaus.ai")
            finally:
                server.CORE_CONFIG_STORAGE_FILE = original_core_config_storage_file
                server.run_with_core_stopped = original_run_with_core_stopped

    def test_sync_addon_configuration_from_studio_merges_and_persists_credentials(self) -> None:
        original_get_studio_base_url = server.get_studio_base_url
        original_read_saved_credentials = server.read_saved_credentials
        original_post_json = server.post_json
        original_persist_credentials = server.persist_credentials
        original_sync_homeassistant_urls = server.sync_homeassistant_urls
        original_sync_homeassistant_hostname = server.sync_homeassistant_hostname
        try:
            persisted: dict[str, str] = {}
            synced_urls: dict[str, str] = {}
            synced_hostname: dict[str, str] = {}
            observed_payloads: list[dict[str, object]] = []

            server.get_studio_base_url = lambda: "https://studio.powerhaus.ai"
            server.read_saved_credentials = lambda: {
                "cloudflare_tunnel_token": "old-token",
                "tunnel_hostname": "old.powerhaus.ai",
                "box_api_token": "box-token",
                "internal_url": "http://192.168.1.201:8123",
                "hostname": "homeassistant",
                "config_version": "1",
            }
            def _post_json(url, payload, headers=None):
                observed_payloads.append(dict(payload))
                return (
                    200,
                    {
                        "status": "updated",
                        "config_version": 2,
                        "internal_url": "http://powerhaus.local:8123",
                        "external_url": "https://my-box.powerhaus.ai",
                        "tunnel_hostname": "new.powerhaus.ai",
                        "hostname": "powerhaustest",
                    },
                )

            server.post_json = _post_json
            server.persist_credentials = lambda cloudflare_tunnel_token, tunnel_hostname, box_api_token, internal_url, external_url, hostname="", config_version=0: persisted.update(
                {
                    "cloudflare_tunnel_token": cloudflare_tunnel_token,
                    "tunnel_hostname": tunnel_hostname,
                    "box_api_token": box_api_token,
                    "internal_url": internal_url,
                    "external_url": external_url,
                    "hostname": hostname,
                    "config_version": str(config_version),
                }
            )
            server.sync_homeassistant_urls = lambda internal_url, external_url: synced_urls.update(
                {"internal_url": internal_url, "external_url": external_url}
            ) or external_url
            server.sync_homeassistant_hostname = lambda hostname: synced_hostname.update({"hostname": hostname}) or hostname

            result = server.sync_addon_configuration_from_studio()

            self.assertEqual(result["status"], "updated")
            self.assertTrue(result["changed"])
            self.assertEqual(result["internal_url"], "http://powerhaus.local:8123")
            self.assertEqual(result["external_url"], "https://my-box.powerhaus.ai")
            self.assertEqual(persisted["cloudflare_tunnel_token"], "old-token")
            self.assertEqual(persisted["tunnel_hostname"], "new.powerhaus.ai")
            self.assertEqual(persisted["box_api_token"], "box-token")
            self.assertEqual(persisted["internal_url"], "http://powerhaus.local:8123")
            self.assertEqual(persisted["external_url"], "https://my-box.powerhaus.ai")
            self.assertEqual(persisted["hostname"], "powerhaustest")
            self.assertEqual(persisted["config_version"], "2")
            self.assertEqual(synced_urls["internal_url"], "http://powerhaus.local:8123")
            self.assertEqual(synced_urls["external_url"], "https://my-box.powerhaus.ai")
            self.assertEqual(synced_hostname["hostname"], "powerhaustest")
            self.assertEqual(len(observed_payloads), 2)
            self.assertEqual(observed_payloads[0]["current_hostname"], "homeassistant")
            self.assertEqual(observed_payloads[0]["reported_config_version"], 1)
            self.assertEqual(observed_payloads[1]["reported_config_version"], 2)
            self.assertEqual(observed_payloads[1]["reported_apply_status"], "applied")
        finally:
            server.get_studio_base_url = original_get_studio_base_url
            server.read_saved_credentials = original_read_saved_credentials
            server.post_json = original_post_json
            server.persist_credentials = original_persist_credentials
            server.sync_homeassistant_urls = original_sync_homeassistant_urls
            server.sync_homeassistant_hostname = original_sync_homeassistant_hostname

    def test_apply_studio_configuration_locally_persists_config_version(self) -> None:
        original_read_saved_credentials = server.read_saved_credentials
        original_persist_credentials = server.persist_credentials
        original_sync_homeassistant_urls = server.sync_homeassistant_urls
        original_sync_homeassistant_hostname = server.sync_homeassistant_hostname
        try:
            persisted: dict[str, str] = {}
            synced: dict[str, str] = {}
            synced_hostname: dict[str, str] = {}
            server.read_saved_credentials = lambda: {
                "cloudflare_tunnel_token": "old-token",
                "tunnel_hostname": "old.powerhaus.ai",
                "box_api_token": "box-token",
                "internal_url": "http://192.168.1.201:8123",
                "hostname": "homeassistant",
                "config_version": "1",
            }
            server.persist_credentials = lambda cloudflare_tunnel_token, tunnel_hostname, box_api_token, internal_url, external_url, hostname="", config_version=0: persisted.update(
                {
                    "cloudflare_tunnel_token": cloudflare_tunnel_token,
                    "tunnel_hostname": tunnel_hostname,
                    "box_api_token": box_api_token,
                    "internal_url": internal_url,
                    "external_url": external_url,
                    "hostname": hostname,
                    "config_version": str(config_version),
                }
            )
            server.sync_homeassistant_urls = lambda internal_url, external_url: synced.update(
                {"internal_url": internal_url, "external_url": external_url}
            ) or external_url
            server.sync_homeassistant_hostname = lambda hostname: synced_hostname.update({"hostname": hostname}) or hostname

            result = server.apply_studio_configuration_locally(
                {
                    "cloudflare_tunnel_token": "new-token",
                    "tunnel_hostname": "new.powerhaus.ai",
                    "internal_url": "http://powerhaus.local:8123",
                    "external_url": "https://my-box.powerhaus.ai",
                    "hostname": "powerhaustest",
                    "config_version": 5,
                }
            )

            self.assertEqual(result["status"], "applied")
            self.assertEqual(result["config_version"], 5)
            self.assertEqual(result["external_url"], "https://my-box.powerhaus.ai")
            self.assertEqual(result["hostname"], "powerhaustest")
            self.assertEqual(persisted["cloudflare_tunnel_token"], "new-token")
            self.assertEqual(persisted["tunnel_hostname"], "new.powerhaus.ai")
            self.assertEqual(persisted["box_api_token"], "box-token")
            self.assertEqual(persisted["internal_url"], "http://powerhaus.local:8123")
            self.assertEqual(persisted["external_url"], "https://my-box.powerhaus.ai")
            self.assertEqual(persisted["hostname"], "powerhaustest")
            self.assertEqual(persisted["config_version"], "5")
            self.assertEqual(synced["internal_url"], "http://powerhaus.local:8123")
            self.assertEqual(synced["external_url"], "https://my-box.powerhaus.ai")
            self.assertEqual(synced_hostname["hostname"], "powerhaustest")
        finally:
            server.read_saved_credentials = original_read_saved_credentials
            server.persist_credentials = original_persist_credentials
            server.sync_homeassistant_urls = original_sync_homeassistant_urls
            server.sync_homeassistant_hostname = original_sync_homeassistant_hostname

    def test_initial_pairing_enables_iframe_embedding_and_runs_configurator(self) -> None:
        original_read_addon_options = server.read_addon_options
        original_persist_addon_options = server.persist_addon_options
        original_subprocess_run = server.subprocess.run
        try:
            persisted: dict[str, object] = {}
            captured_cmd: list[str] = []

            server.read_addon_options = lambda: {
                "ui_auth_enabled": False,
                "ui_password": "change-this-password",
                "studio_base_url": "https://studio.powerhaus.ai",
                "auto_enable_iframe_embedding": False,
            }
            server.persist_addon_options = lambda options: persisted.update(options) or ""
            server.subprocess.run = lambda cmd, **kwargs: (
                captured_cmd.extend(cmd),
                types.SimpleNamespace(returncode=0, stdout="ok\n", stderr=""),
            )[1]

            warning = server.ensure_iframe_embedding_on_initial_pairing()

            self.assertEqual(warning, "")
            self.assertTrue(persisted["auto_enable_iframe_embedding"])
            self.assertEqual(captured_cmd[:2], ["python3", str(server.IFRAME_CONFIGURATOR_SCRIPT)])
        finally:
            server.read_addon_options = original_read_addon_options
            server.persist_addon_options = original_persist_addon_options
            server.subprocess.run = original_subprocess_run


if __name__ == "__main__":
    unittest.main()
