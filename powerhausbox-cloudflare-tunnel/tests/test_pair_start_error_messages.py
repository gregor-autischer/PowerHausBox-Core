import importlib.util
import sys
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
    flask_stub.request = types.SimpleNamespace(headers={}, script_root="", form={})
    flask_stub.session = {}
    sys.modules["flask"] = flask_stub


MODULE_PATH = Path(__file__).resolve().parents[1] / "rootfs" / "opt" / "powerhausbox" / "server.py"
SPEC = importlib.util.spec_from_file_location("powerhausbox_server", MODULE_PATH)
server = importlib.util.module_from_spec(SPEC)
assert SPEC and SPEC.loader
SPEC.loader.exec_module(server)


class PairStartErrorMessageTests(unittest.TestCase):
    def test_cloudflare_403_gets_waf_hint(self) -> None:
        message = server.build_pair_start_error_message(
            status_code=403,
            api_error="",
            api_detail="",
            request_id="req-123",
            cf_ray="ray-456",
            server_header="cloudflare",
        )

        self.assertIn("blocked before Studio app", message)
        self.assertIn("request_id=req-123", message)
        self.assertIn("cf_ray=ray-456", message)

    def test_tenant_mismatch_message(self) -> None:
        message = server.build_pair_start_error_message(
            status_code=403,
            api_error="tenant_mismatch",
            api_detail="wrong tenant",
            request_id="req-999",
            cf_ray="",
            server_header="gunicorn",
        )

        self.assertIn("different Studio environment/account", message)
        self.assertIn("tenant_mismatch", message)
        self.assertIn("wrong tenant", message)

    def test_code_used_message(self) -> None:
        message = server.build_pair_start_error_message(
            status_code=400,
            api_error="code_used",
            api_detail="",
            request_id="",
            cf_ray="",
            server_header="",
        )

        self.assertIn("already used", message)

    def test_rate_limited_status_without_error_code(self) -> None:
        message = server.build_pair_start_error_message(
            status_code=429,
            api_error="",
            api_detail="",
            request_id="",
            cf_ray="",
            server_header="",
        )

        self.assertIn("Too many attempts", message)

    def test_extract_request_id_prefers_payload(self) -> None:
        request_id = server.extract_api_request_id(
            {"request_id": "req-payload"},
            {"x-request-id": "req-header"},
        )

        self.assertEqual(request_id, "req-payload")

    def test_extract_request_id_falls_back_to_header(self) -> None:
        request_id = server.extract_api_request_id(
            {},
            {"x-request-id": "req-header"},
        )

        self.assertEqual(request_id, "req-header")


if __name__ == "__main__":
    unittest.main()
