import importlib.util
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
SPEC = importlib.util.spec_from_file_location("powerhausbox_server_pairing", MODULE_PATH)
server = importlib.util.module_from_spec(SPEC)
assert SPEC and SPEC.loader
SPEC.loader.exec_module(server)


class PairingOnboardingTests(unittest.TestCase):
    def test_extract_pair_code_from_single_hidden_field(self) -> None:
        pair_code = server.extract_pair_code_from_form({"pair_code": "123456"})
        self.assertEqual(pair_code, "123456")

    def test_extract_pair_code_from_digit_fields(self) -> None:
        pair_code = server.extract_pair_code_from_form(
            {
                "pair_code_1": "1",
                "pair_code_2": "2",
                "pair_code_3": "3",
                "pair_code_4": "4",
                "pair_code_5": "5",
                "pair_code_6": "6",
            }
        )
        self.assertEqual(pair_code, "123456")

    def test_pairing_page_uses_onboarding_template_when_unpaired(self) -> None:
        original_require_auth_or_redirect = server.require_auth_or_redirect
        original_has_saved_pairing_credentials = server.has_saved_pairing_credentials
        original_load_pairing_context = server.load_pairing_context
        original_render_template = server.render_template
        try:
            server.require_auth_or_redirect = lambda: None
            server.has_saved_pairing_credentials = lambda: False
            server.load_pairing_context = lambda: {"pending_verification_code": "", "poll_after_seconds": 2}
            server.render_template = lambda template_name, **kwargs: template_name
            self.assertEqual(server.pairing_page(), "pairing_onboarding.html")
        finally:
            server.require_auth_or_redirect = original_require_auth_or_redirect
            server.has_saved_pairing_credentials = original_has_saved_pairing_credentials
            server.load_pairing_context = original_load_pairing_context
            server.render_template = original_render_template

    def test_pairing_page_uses_full_template_when_paired(self) -> None:
        original_require_auth_or_redirect = server.require_auth_or_redirect
        original_has_saved_pairing_credentials = server.has_saved_pairing_credentials
        original_load_pairing_context = server.load_pairing_context
        original_render_template = server.render_template
        try:
            server.require_auth_or_redirect = lambda: None
            server.has_saved_pairing_credentials = lambda: True
            server.load_pairing_context = lambda: {"pending_verification_code": "", "poll_after_seconds": 2}
            server.render_template = lambda template_name, **kwargs: template_name
            self.assertEqual(server.pairing_page(), "pairing.html")
        finally:
            server.require_auth_or_redirect = original_require_auth_or_redirect
            server.has_saved_pairing_credentials = original_has_saved_pairing_credentials
            server.load_pairing_context = original_load_pairing_context
            server.render_template = original_render_template

    def test_ingress_url_without_prefix_returns_absolute_path(self) -> None:
        original_request = server.request
        try:
            server.request = types.SimpleNamespace(headers={}, script_root="", form={}, path="/pairing", args={})
            self.assertEqual(server.ingress_url("/pairing"), "/pairing")
        finally:
            server.request = original_request

    def test_build_apply_alert_returns_message_for_failed_apply(self) -> None:
        original_sync_state_file = server.SYNC_STATE_FILE
        try:
            with tempfile.TemporaryDirectory() as temp_dir:
                server.SYNC_STATE_FILE = Path(temp_dir) / "sync_state.json"
                server.write_json_file(
                    server.SYNC_STATE_FILE,
                    {
                        **server._default_sync_state(),
                        "last_apply_status": "error",
                        "last_apply_target": "config_reconcile",
                        "last_apply_error": "verification failed",
                    },
                )
                alert = server.build_apply_alert()
                self.assertEqual(alert["category"], "error")
                self.assertIn("verification failed", alert["message"])
        finally:
            server.SYNC_STATE_FILE = original_sync_state_file

    def test_load_pairing_context_uses_health_snapshot_for_paired_overview(self) -> None:
        original_get_pairing_state = server.get_pairing_state
        original_read_saved_credentials = server.read_saved_credentials
        original_get_current_host_hostname = server.get_current_host_hostname
        original_collect_health_snapshot = server.collect_health_snapshot
        original_has_saved_pairing_credentials = server.has_saved_pairing_credentials
        try:
            server.get_pairing_state = lambda: {}
            server.read_saved_credentials = lambda: {
                "hostname": "powerhausbox",
                "internal_url": "http://powerhausbox.local:8123",
                "external_url": "https://box.powerhaus.cloud",
                "tunnel_hostname": "box.powerhaus.cloud",
            }
            server.get_current_host_hostname = lambda: "powerhausbox"
            server.has_saved_pairing_credentials = lambda: True
            server.collect_health_snapshot = lambda: {
                "status": "ok",
                "cloudflared_running": True,
                "last_syncs": {"config": "2026-03-08T14:00:00Z", "auth": "2026-03-08T15:00:00Z"},
                "last_apply": {"target": "startup_saved_config"},
            }

            context = server.load_pairing_context()

            self.assertEqual(context["tunnel_status"], "Connected")
            self.assertEqual(context["system_status"], "Ok")
            self.assertEqual(context["tunnel_hostname"], "box.powerhaus.cloud")
            self.assertEqual(context["last_sync_target"], "startup_saved_config")
        finally:
            server.get_pairing_state = original_get_pairing_state
            server.read_saved_credentials = original_read_saved_credentials
            server.get_current_host_hostname = original_get_current_host_hostname
            server.collect_health_snapshot = original_collect_health_snapshot
            server.has_saved_pairing_credentials = original_has_saved_pairing_credentials

    def test_delete_token_requires_delete_confirmation(self) -> None:
        original_require_auth_or_redirect = server.require_auth_or_redirect
        original_require_completed_pairing_or_redirect = server.require_completed_pairing_or_redirect
        original_request = server.request
        original_flash = server.flash
        original_redirect_ingress_path = server.redirect_ingress_path
        try:
            flashes: list[tuple[str, str]] = []
            server.require_auth_or_redirect = lambda: None
            server.require_completed_pairing_or_redirect = lambda: None
            server.request = types.SimpleNamespace(form={"confirmation": "wrong"})
            server.flash = lambda message, category="info": flashes.append((category, message))
            server.redirect_ingress_path = lambda path: path

            result = server.delete_token()

            self.assertEqual(result, "/settings")
            self.assertEqual(flashes[0][0], "error")
            self.assertIn('löschen', flashes[0][1])
        finally:
            server.require_auth_or_redirect = original_require_auth_or_redirect
            server.require_completed_pairing_or_redirect = original_require_completed_pairing_or_redirect
            server.request = original_request
            server.flash = original_flash
            server.redirect_ingress_path = original_redirect_ingress_path


if __name__ == "__main__":
    unittest.main()
