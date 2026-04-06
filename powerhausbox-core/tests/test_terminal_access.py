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

        def route(self, *args, **kwargs):
            def decorator(func):
                return func

            return decorator

        def before_request(self, func):
            return func

    flask_stub.Flask = _DummyFlask
    flask_stub.Response = object
    flask_stub.flash = lambda *args, **kwargs: None
    flask_stub.jsonify = lambda *args, **kwargs: {}
    flask_stub.make_response = lambda value: value
    flask_stub.redirect = lambda *args, **kwargs: None
    flask_stub.render_template = lambda *args, **kwargs: ""
    flask_stub.request = types.SimpleNamespace(headers={}, script_root="", form={}, path="/pairing", args={})
    flask_stub.session = {}
    sys.modules["flask"] = flask_stub


if "aiohttp" not in sys.modules:
    aiohttp_stub = types.ModuleType("aiohttp")

    class _DummyBasicAuth:
        def __init__(self, *args, **kwargs):
            pass

    class _DummyWebModule:
        class Request:
            pass

        class WebSocketResponse:
            def __init__(self, *args, **kwargs):
                pass

        @staticmethod
        def Response(*args, **kwargs):
            return None

        @staticmethod
        def json_response(*args, **kwargs):
            return None

        @staticmethod
        def Application(*args, **kwargs):
            return types.SimpleNamespace(router=types.SimpleNamespace(add_route=lambda *a, **k: None, add_get=lambda *a, **k: None))

        @staticmethod
        def run_app(*args, **kwargs):
            return None

    class _DummyClientSession:
        pass

    class _DummyWSMsgType:
        TEXT = "TEXT"
        BINARY = "BINARY"
        CLOSE = "CLOSE"
        ERROR = "ERROR"

    aiohttp_stub.BasicAuth = _DummyBasicAuth
    aiohttp_stub.web = _DummyWebModule
    aiohttp_stub.ClientSession = _DummyClientSession
    aiohttp_stub.WSMsgType = _DummyWSMsgType
    sys.modules["aiohttp"] = aiohttp_stub


SERVER_MODULE_PATH = Path(__file__).resolve().parents[1] / "rootfs" / "opt" / "powerhausbox" / "server.py"
SERVER_SPEC = importlib.util.spec_from_file_location("powerhausbox_server_terminal_access", SERVER_MODULE_PATH)
server = importlib.util.module_from_spec(SERVER_SPEC)
assert SERVER_SPEC and SERVER_SPEC.loader
SERVER_SPEC.loader.exec_module(server)

TERMINAL_PROXY_MODULE_PATH = Path(__file__).resolve().parents[1] / "rootfs" / "opt" / "powerhausbox" / "terminal_proxy.py"
TERMINAL_PROXY_SPEC = importlib.util.spec_from_file_location(
    "powerhausbox_terminal_proxy_terminal_access",
    TERMINAL_PROXY_MODULE_PATH,
)
terminal_proxy = importlib.util.module_from_spec(TERMINAL_PROXY_SPEC)
assert TERMINAL_PROXY_SPEC and TERMINAL_PROXY_SPEC.loader
TERMINAL_PROXY_SPEC.loader.exec_module(terminal_proxy)


class LocalTerminalTokenTests(unittest.TestCase):
    def test_issue_local_terminal_token_persists_valid_token(self) -> None:
        original_tokens_file = server.LOCAL_TERMINAL_TOKENS_FILE
        try:
            with tempfile.TemporaryDirectory() as temp_dir:
                token_file = Path(temp_dir) / "local_terminal_tokens.json"
                server.LOCAL_TERMINAL_TOKENS_FILE = token_file

                token = server.issue_local_terminal_token(ttl_seconds=3600)

                self.assertTrue(token)
                written = json.loads(token_file.read_text(encoding="utf-8"))
                self.assertIn(token, written["tokens"])
                self.assertGreater(float(written["tokens"][token]), 0.0)
        finally:
            server.LOCAL_TERMINAL_TOKENS_FILE = original_tokens_file

    def test_terminal_proxy_accepts_valid_local_token(self) -> None:
        original_tokens_file = terminal_proxy.LOCAL_TERMINAL_TOKENS_FILE
        try:
            with tempfile.TemporaryDirectory() as temp_dir:
                token_file = Path(temp_dir) / "local_terminal_tokens.json"
                token_file.write_text(
                    json.dumps({"tokens": {"valid-token": server.time.time() + 600}}),
                    encoding="utf-8",
                )
                terminal_proxy.LOCAL_TERMINAL_TOKENS_FILE = token_file

                self.assertTrue(terminal_proxy._validate_local_token("valid-token"))
        finally:
            terminal_proxy.LOCAL_TERMINAL_TOKENS_FILE = original_tokens_file


class ManagedSshAddonTests(unittest.TestCase):
    def test_build_managed_ssh_addon_options_merges_local_and_studio_keys(self) -> None:
        original_read_addon_options = server.read_addon_options
        original_read_studio_synced_ssh_keys = server._read_studio_synced_ssh_keys
        try:
            server.read_addon_options = lambda: {
                "ssh": {
                    "username": "powerhaus",
                    "authorized_keys": ["local-key"],
                    "sftp": True,
                    "allow_tcp_forwarding": False,
                }
            }
            server._read_studio_synced_ssh_keys = lambda: ["studio-key"]

            options = server.build_managed_ssh_addon_options()

            self.assertEqual(options["ssh"]["username"], "powerhaus")
            self.assertEqual(options["ssh"]["authorized_keys"], ["local-key", "studio-key"])
            self.assertTrue(options["ssh"]["sftp"])
            self.assertTrue(options["share_sessions"])
            self.assertTrue(options["zsh"])
        finally:
            server.read_addon_options = original_read_addon_options
            server._read_studio_synced_ssh_keys = original_read_studio_synced_ssh_keys

    def test_write_authorized_keys_uses_managed_ssh_addon_when_enabled(self) -> None:
        original_use_external_ssh_addon = server.use_external_ssh_addon
        original_configure_managed_ssh_addon = server.configure_managed_ssh_addon
        try:
            called: dict[str, object] = {}
            server.use_external_ssh_addon = lambda: True
            server.configure_managed_ssh_addon = lambda **kwargs: called.update(kwargs) or {}

            server.write_authorized_keys(["studio-key"], strict=True)

            self.assertEqual(called["studio_keys"], ["studio-key"])
            self.assertTrue(called["start_after"])
        finally:
            server.use_external_ssh_addon = original_use_external_ssh_addon
            server.configure_managed_ssh_addon = original_configure_managed_ssh_addon

    def test_terminal_proxy_rejects_expired_local_token(self) -> None:
        original_tokens_file = terminal_proxy.LOCAL_TERMINAL_TOKENS_FILE
        try:
            with tempfile.TemporaryDirectory() as temp_dir:
                token_file = Path(temp_dir) / "local_terminal_tokens.json"
                token_file.write_text(
                    json.dumps({"tokens": {"expired-token": server.time.time() - 10}}),
                    encoding="utf-8",
                )
                terminal_proxy.LOCAL_TERMINAL_TOKENS_FILE = token_file

                self.assertFalse(terminal_proxy._validate_local_token("expired-token"))
        finally:
            terminal_proxy.LOCAL_TERMINAL_TOKENS_FILE = original_tokens_file
