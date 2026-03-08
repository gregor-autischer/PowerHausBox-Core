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

    def test_sync_homeassistant_core_urls_can_update_internal_url_only(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            core_config_path = Path(temp_dir) / "core.config"
            core_config_path.write_text(
                json.dumps(
                    {
                        "version": 1,
                        "minor_version": 4,
                        "key": "core.config",
                        "data": {
                            "internal_url": "http://old.local:8123",
                            "external_url": "https://box.powerhaus.ai",
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

                result = server.sync_homeassistant_core_urls(internal_url="http://powerhausboxtest.local:8123")

                self.assertEqual(result["internal_url"], "http://powerhausboxtest.local:8123")
                self.assertEqual(result["external_url"], "https://box.powerhaus.ai")
                updated = json.loads(core_config_path.read_text(encoding="utf-8"))
                self.assertEqual(updated["data"]["internal_url"], "http://powerhausboxtest.local:8123")
                self.assertEqual(updated["data"]["external_url"], "https://box.powerhaus.ai")
            finally:
                server.CORE_CONFIG_STORAGE_FILE = original_core_config_storage_file
                server.run_with_core_stopped = original_run_with_core_stopped

    def test_wait_for_homeassistant_api_reachability_retries_until_api_is_available(self) -> None:
        original_is_homeassistant_core_api_reachable = server.is_homeassistant_core_api_reachable
        original_sleep = server.time.sleep
        try:
            states = iter(
                [
                    False,
                    False,
                    True,
                ]
            )

            server.is_homeassistant_core_api_reachable = lambda: next(states)
            server.time.sleep = lambda *_args, **_kwargs: None

            server.wait_for_homeassistant_api_reachability(True, timeout_seconds=1)
        finally:
            server.is_homeassistant_core_api_reachable = original_is_homeassistant_core_api_reachable
            server.time.sleep = original_sleep

    def test_verify_applied_homeassistant_state_records_failure_on_mismatch(self) -> None:
        original_sync_state_file = server.SYNC_STATE_FILE
        original_read_saved_credentials = server.read_saved_credentials
        original_get_current_host_hostname = server.get_current_host_hostname
        original_read_live_core_urls = server.read_live_core_urls
        try:
            with tempfile.TemporaryDirectory() as temp_dir:
                server.SYNC_STATE_FILE = Path(temp_dir) / "sync_state.json"
                server.read_saved_credentials = lambda: {
                    "hostname": "powerhausboxtest",
                    "internal_url": "http://powerhausboxtest.local:8123",
                    "external_url": "https://box.powerhaus.ai",
                }
                server.get_current_host_hostname = lambda: "powerhausboxtest"
                server.read_live_core_urls = lambda: {
                    "internal_url": "http://wrong.local:8123",
                    "external_url": "https://box.powerhaus.ai",
                }

                with self.assertRaises(server.SupervisorAPIError):
                    server.verify_applied_homeassistant_state(
                        expected_hostname="powerhausboxtest",
                        expected_internal_url="http://powerhausboxtest.local:8123",
                        expected_external_url="https://box.powerhaus.ai",
                        target="unit-test",
                    )

                state = server.read_sync_state()
                self.assertEqual(state["last_apply_status"], "error")
                self.assertEqual(state["last_apply_target"], "unit-test")
                self.assertIn("internal_url", state["last_apply_error"])
        finally:
            server.SYNC_STATE_FILE = original_sync_state_file
            server.read_saved_credentials = original_read_saved_credentials
            server.get_current_host_hostname = original_get_current_host_hostname
            server.read_live_core_urls = original_read_live_core_urls

    def test_apply_saved_homeassistant_host_settings_verifies_and_updates_state(self) -> None:
        original_sync_state_file = server.SYNC_STATE_FILE
        original_read_saved_credentials = server.read_saved_credentials
        original_sync_homeassistant_hostname = server.sync_homeassistant_hostname
        original_sync_homeassistant_core_urls = server.sync_homeassistant_core_urls
        original_verify_applied_homeassistant_state = server.verify_applied_homeassistant_state
        try:
            with tempfile.TemporaryDirectory() as temp_dir:
                server.SYNC_STATE_FILE = Path(temp_dir) / "sync_state.json"
                server.read_saved_credentials = lambda: {
                    "hostname": "powerhausboxtest",
                    "internal_url": "http://powerhausboxtest.local:8123",
                    "external_url": "https://box.powerhaus.ai",
                    "config_version": "8",
                }
                synced_hostname: dict[str, str] = {}
                synced_urls: dict[str, str] = {}
                server.sync_homeassistant_hostname = lambda hostname: synced_hostname.update({"hostname": hostname}) or hostname
                server.sync_homeassistant_core_urls = lambda *, internal_url="", external_url="": synced_urls.update(
                    {"internal_url": internal_url, "external_url": external_url}
                ) or {"internal_url": internal_url, "external_url": external_url}
                server.verify_applied_homeassistant_state = lambda **kwargs: {
                    "hostname": kwargs["expected_hostname"],
                    "internal_url": kwargs["expected_internal_url"],
                    "external_url": kwargs["expected_external_url"],
                }

                result = server.apply_saved_homeassistant_host_settings(target="startup_saved_config")

                state = server.read_sync_state()
                self.assertEqual(result["hostname"], "powerhausboxtest")
                self.assertEqual(synced_hostname["hostname"], "powerhausboxtest")
                self.assertEqual(synced_urls["internal_url"], "http://powerhausboxtest.local:8123")
                self.assertEqual(synced_urls["external_url"], "https://box.powerhaus.ai")
                self.assertEqual(state["last_config_reconcile_status"], "applied")
                self.assertEqual(state["applied_config_version"], 8)
        finally:
            server.SYNC_STATE_FILE = original_sync_state_file
            server.read_saved_credentials = original_read_saved_credentials
            server.sync_homeassistant_hostname = original_sync_homeassistant_hostname
            server.sync_homeassistant_core_urls = original_sync_homeassistant_core_urls
            server.verify_applied_homeassistant_state = original_verify_applied_homeassistant_state

    def test_sync_addon_configuration_from_studio_merges_and_persists_credentials(self) -> None:
        original_sync_state_file = server.SYNC_STATE_FILE
        original_get_studio_base_url = server.get_studio_base_url
        original_read_saved_credentials = server.read_saved_credentials
        original_post_json = server.post_json
        original_persist_credentials = server.persist_credentials
        original_sync_homeassistant_urls = server.sync_homeassistant_urls
        original_sync_homeassistant_hostname = server.sync_homeassistant_hostname
        original_verify_applied_homeassistant_state = server.verify_applied_homeassistant_state
        try:
            with tempfile.TemporaryDirectory() as temp_dir:
                server.SYNC_STATE_FILE = Path(temp_dir) / "sync_state.json"
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
                server.verify_applied_homeassistant_state = lambda **kwargs: kwargs

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
            server.SYNC_STATE_FILE = original_sync_state_file
            server.get_studio_base_url = original_get_studio_base_url
            server.read_saved_credentials = original_read_saved_credentials
            server.post_json = original_post_json
            server.persist_credentials = original_persist_credentials
            server.sync_homeassistant_urls = original_sync_homeassistant_urls
            server.sync_homeassistant_hostname = original_sync_homeassistant_hostname
            server.verify_applied_homeassistant_state = original_verify_applied_homeassistant_state

    def test_apply_studio_configuration_locally_persists_config_version(self) -> None:
        original_sync_state_file = server.SYNC_STATE_FILE
        original_read_saved_credentials = server.read_saved_credentials
        original_persist_credentials = server.persist_credentials
        original_sync_homeassistant_urls = server.sync_homeassistant_urls
        original_sync_homeassistant_hostname = server.sync_homeassistant_hostname
        original_verify_applied_homeassistant_state = server.verify_applied_homeassistant_state
        try:
            with tempfile.TemporaryDirectory() as temp_dir:
                server.SYNC_STATE_FILE = Path(temp_dir) / "sync_state.json"
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
                server.verify_applied_homeassistant_state = lambda **kwargs: kwargs

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
            server.SYNC_STATE_FILE = original_sync_state_file
            server.read_saved_credentials = original_read_saved_credentials
            server.persist_credentials = original_persist_credentials
            server.sync_homeassistant_urls = original_sync_homeassistant_urls
            server.sync_homeassistant_hostname = original_sync_homeassistant_hostname
            server.verify_applied_homeassistant_state = original_verify_applied_homeassistant_state

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

    def test_run_auth_sync_once_updates_sync_state_with_snapshot_hash(self) -> None:
        original_sync_state_file = server.SYNC_STATE_FILE
        original_list_homeassistant_hash_users = server.list_homeassistant_hash_users
        original_sync_auth_hashes_to_studio = server.sync_auth_hashes_to_studio
        try:
            with tempfile.TemporaryDirectory() as temp_dir:
                server.SYNC_STATE_FILE = Path(temp_dir) / "sync_state.json"
                auth_rows = [
                    {
                        "user_id": "u1",
                        "credential_id": "c1",
                        "name": "Gregor",
                        "username": "gregor",
                        "password_hash": "hash",
                        "is_owner": True,
                        "is_active": True,
                        "system_generated": False,
                        "local_only": False,
                        "group_ids": ["system-users"],
                    }
                ]
                captured_users: list[dict[str, object]] = []

                server.list_homeassistant_hash_users = lambda: auth_rows
                server.sync_auth_hashes_to_studio = lambda *, users=None: (
                    captured_users.extend(users or []),
                    {"status": "ok", "synced_count": 1, "received_count": 1, "sync_id": "sync-1"},
                )[1]

                result = server.run_auth_sync_once(trigger="unit-test")

                state = server.read_sync_state()
                self.assertEqual(result["sync_id"], "sync-1")
                self.assertEqual(captured_users, auth_rows)
                self.assertEqual(state["last_auth_sync_status"], "ok")
                self.assertTrue(state["last_auth_sync_at"])
                self.assertEqual(state["last_auth_snapshot_hash"], server.compute_auth_snapshot_hash(auth_rows))
        finally:
            server.SYNC_STATE_FILE = original_sync_state_file
            server.list_homeassistant_hash_users = original_list_homeassistant_hash_users
            server.sync_auth_hashes_to_studio = original_sync_auth_hashes_to_studio

    def test_reconcile_desired_configuration_corrects_drift_and_reports_event(self) -> None:
        original_sync_state_file = server.SYNC_STATE_FILE
        original_read_saved_credentials = server.read_saved_credentials
        original_get_current_host_hostname = server.get_current_host_hostname
        original_read_live_core_urls = server.read_live_core_urls
        original_sync_homeassistant_hostname = server.sync_homeassistant_hostname
        original_sync_homeassistant_core_urls = server.sync_homeassistant_core_urls
        original_send_state_report = server.send_state_report
        original_verify_applied_homeassistant_state = server.verify_applied_homeassistant_state
        try:
            with tempfile.TemporaryDirectory() as temp_dir:
                server.SYNC_STATE_FILE = Path(temp_dir) / "sync_state.json"
                server.read_saved_credentials = lambda: {
                    "cloudflare_tunnel_token": "token",
                    "tunnel_hostname": "box.powerhaus.ai",
                    "box_api_token": "box-token",
                    "internal_url": "http://powerhaus.local:8123",
                    "external_url": "https://box.powerhaus.ai",
                    "hostname": "powerhaus",
                    "config_version": "7",
                }
                server.get_current_host_hostname = lambda: "homeassistant"
                server.read_live_core_urls = lambda: {
                    "internal_url": "http://192.168.1.201:8123",
                    "external_url": "https://old.powerhaus.ai",
                }

                synced_hostname: dict[str, str] = {}
                synced_urls: dict[str, str] = {}
                reported: dict[str, object] = {}
                server.sync_homeassistant_hostname = lambda hostname: synced_hostname.update({"hostname": hostname}) or hostname
                server.sync_homeassistant_core_urls = lambda *, internal_url="", external_url="": synced_urls.update(
                    {"internal_url": internal_url, "external_url": external_url}
                ) or {"internal_url": internal_url, "external_url": external_url}
                server.verify_applied_homeassistant_state = lambda **kwargs: kwargs
                server.send_state_report = lambda report_type, payload: reported.update(
                    {"report_type": report_type, "payload": payload}
                ) or {"status": "ok"}

                result = server.reconcile_desired_configuration(trigger="unit-test")

                state = server.read_sync_state()
                self.assertEqual(result["status"], "corrected")
                self.assertEqual(synced_hostname["hostname"], "powerhaus")
                self.assertEqual(synced_urls["internal_url"], "http://powerhaus.local:8123")
                self.assertEqual(synced_urls["external_url"], "https://box.powerhaus.ai")
                self.assertEqual(state["last_config_reconcile_status"], "corrected")
                self.assertEqual(state["applied_config_version"], 7)
                self.assertEqual(reported["report_type"], "event")
                self.assertEqual(reported["payload"]["event_type"], "config_drift_corrected")
        finally:
            server.SYNC_STATE_FILE = original_sync_state_file
            server.read_saved_credentials = original_read_saved_credentials
            server.get_current_host_hostname = original_get_current_host_hostname
            server.read_live_core_urls = original_read_live_core_urls
            server.sync_homeassistant_hostname = original_sync_homeassistant_hostname
            server.sync_homeassistant_core_urls = original_sync_homeassistant_core_urls
            server.verify_applied_homeassistant_state = original_verify_applied_homeassistant_state
            server.send_state_report = original_send_state_report

    def test_studio_config_apply_accepts_command_envelope(self) -> None:
        original_sync_state_file = server.SYNC_STATE_FILE
        original_request = server.request
        original_jsonify = server.jsonify
        original_read_saved_credentials = server.read_saved_credentials
        original_verify_studio_push_signature = server.verify_studio_push_signature
        original_has_processed_command_id = server.has_processed_command_id
        original_remember_processed_command_id = server.remember_processed_command_id
        original_apply_studio_configuration_locally = server.apply_studio_configuration_locally
        original_enqueue_sync_job = server.enqueue_sync_job
        try:
            with tempfile.TemporaryDirectory() as temp_dir:
                server.SYNC_STATE_FILE = Path(temp_dir) / "sync_state.json"
                remembered: list[str] = []
                enqueued: list[tuple[str, str]] = []
                applied_payloads: list[dict[str, object]] = []

                server.request = types.SimpleNamespace(
                    headers={"X-PowerHaus-Timestamp": "1", "X-PowerHaus-Signature": "sig"},
                    get_data=lambda cache=False: json.dumps(
                        {
                            "command_id": "cmd-1",
                            "command_type": "apply_config",
                            "config_version": 9,
                            "payload": {
                                "internal_url": "http://powerhaus.local:8123",
                                "external_url": "https://box.powerhaus.ai",
                                "hostname": "powerhaus",
                            },
                        }
                    ).encode("utf-8"),
                )
                server.jsonify = lambda payload: payload
                server.read_saved_credentials = lambda: {"cloudflare_tunnel_token": "token"}
                server.verify_studio_push_signature = lambda **kwargs: True
                server.has_processed_command_id = lambda command_id: False
                server.remember_processed_command_id = lambda command_id: remembered.append(command_id)
                server.apply_studio_configuration_locally = lambda payload: (
                    applied_payloads.append(dict(payload)),
                    {"status": "applied", "config_version": 9},
                )[1]
                server.enqueue_sync_job = lambda name, reason="": enqueued.append((name, reason))

                response, status_code = server.studio_config_apply()

                self.assertEqual(status_code, 200)
                self.assertEqual(response["command_id"], "cmd-1")
                self.assertEqual(response["command_type"], "apply_config")
                self.assertEqual(applied_payloads[0]["config_version"], 9)
                self.assertEqual(remembered, ["cmd-1"])
                self.assertEqual(enqueued, [("health_probe", "studio-push")])
        finally:
            server.SYNC_STATE_FILE = original_sync_state_file
            server.request = original_request
            server.jsonify = original_jsonify
            server.read_saved_credentials = original_read_saved_credentials
            server.verify_studio_push_signature = original_verify_studio_push_signature
            server.has_processed_command_id = original_has_processed_command_id
            server.remember_processed_command_id = original_remember_processed_command_id
            server.apply_studio_configuration_locally = original_apply_studio_configuration_locally
            server.enqueue_sync_job = original_enqueue_sync_job

    def test_healthz_returns_latest_health_snapshot(self) -> None:
        original_jsonify = server.jsonify
        original_get_latest_health_snapshot = server.get_latest_health_snapshot
        try:
            server.jsonify = lambda payload: payload
            server.get_latest_health_snapshot = lambda: {"status": "ok", "paired": True}

            response, status_code = server.healthz()

            self.assertEqual(status_code, 200)
            self.assertEqual(response["status"], "ok")
            self.assertTrue(response["paired"])
        finally:
            server.jsonify = original_jsonify
            server.get_latest_health_snapshot = original_get_latest_health_snapshot

    def test_collect_health_snapshot_is_degraded_when_last_apply_failed(self) -> None:
        original_sync_state_file = server.SYNC_STATE_FILE
        original_read_saved_credentials = server.read_saved_credentials
        original_get_current_host_hostname = server.get_current_host_hostname
        original_read_live_core_urls = server.read_live_core_urls
        original_supervisor_request = server.supervisor_request
        original_list_homeassistant_hash_users = server.list_homeassistant_hash_users
        original_is_cloudflared_running = server.is_cloudflared_running
        try:
            with tempfile.TemporaryDirectory() as temp_dir:
                server.SYNC_STATE_FILE = Path(temp_dir) / "sync_state.json"
                server.update_sync_state(
                    last_apply_status="error",
                    last_apply_target="unit-test",
                    last_apply_error="verification failed",
                )
                server.read_saved_credentials = lambda: {
                    "cloudflare_tunnel_token": "token",
                    "tunnel_hostname": "box.powerhaus.ai",
                    "box_api_token": "box-token",
                    "internal_url": "http://powerhausboxtest.local:8123",
                    "external_url": "https://box.powerhaus.ai",
                    "hostname": "powerhausboxtest",
                    "config_version": "4",
                }
                server.get_current_host_hostname = lambda: "powerhausboxtest"
                server.read_live_core_urls = lambda: {
                    "internal_url": "http://powerhausboxtest.local:8123",
                    "external_url": "https://box.powerhaus.ai",
                }
                server.supervisor_request = lambda method, path, payload=None: {
                    "data": {"state": "running", "version": "2026.3.0"}
                }
                server.list_homeassistant_hash_users = lambda: []
                server.is_cloudflared_running = lambda: True

                snapshot = server.collect_health_snapshot()

                self.assertEqual(snapshot["status"], "degraded")
                self.assertEqual(snapshot["last_apply"]["status"], "error")
                self.assertEqual(snapshot["last_apply"]["target"], "unit-test")
        finally:
            server.SYNC_STATE_FILE = original_sync_state_file
            server.read_saved_credentials = original_read_saved_credentials
            server.get_current_host_hostname = original_get_current_host_hostname
            server.read_live_core_urls = original_read_live_core_urls
            server.supervisor_request = original_supervisor_request
            server.list_homeassistant_hash_users = original_list_homeassistant_hash_users
            server.is_cloudflared_running = original_is_cloudflared_running


if __name__ == "__main__":
    unittest.main()
