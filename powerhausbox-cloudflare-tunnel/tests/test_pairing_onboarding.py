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


if __name__ == "__main__":
    unittest.main()
