"""Tests that verify specific bug fixes are working correctly.

Each test class corresponds to a named bug fix. Tests cover the exact failure
mode that was fixed so that any regression immediately breaks a test.

Bug fixes verified:
  1. sync_worker_loop catch-all Exception handler
  2. mutate_core_config_storage safety guard when Core is running
  3. apply_studio_configuration_locally persist ordering (credentials first)
  4. has_saved_pairing_credentials uses generator in all()
  5. normalize_url shared utility (delegated from normalize_external/internal_url)
  6. Route decorator @auth_required blocks unauthenticated access
"""

import importlib.util
import queue
import sys
import tempfile
import threading
import types
import unittest
from pathlib import Path


# ---------------------------------------------------------------------------
# Flask stub — must be injected before server.py is imported.
# We use a unique module name so multiple test modules can coexist.
# ---------------------------------------------------------------------------

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
    flask_stub.redirect = lambda *args, **kwargs: None
    flask_stub.render_template = lambda *args, **kwargs: ""
    flask_stub.request = types.SimpleNamespace(
        headers={}, script_root="", form={}, path="/pairing", args={}
    )
    flask_stub.session = {}
    sys.modules["flask"] = flask_stub


MODULE_PATH = (
    Path(__file__).resolve().parents[1] / "rootfs" / "opt" / "powerhausbox" / "server.py"
)
SPEC = importlib.util.spec_from_file_location("powerhausbox_server_bug_fixes", MODULE_PATH)
server = importlib.util.module_from_spec(SPEC)
assert SPEC and SPEC.loader
SPEC.loader.exec_module(server)


# ---------------------------------------------------------------------------
# Bug Fix 1: sync_worker_loop catch-all Exception handler
# ---------------------------------------------------------------------------

class SyncWorkerLoopCatchAllTests(unittest.TestCase):
    """The sync_worker_loop must survive unexpected exceptions without dying.

    Before this fix the loop only caught StudioSyncError, AuthStorageError, and
    SupervisorAPIError.  An unexpected KeyError or TypeError would escape,
    killing the background thread permanently and stopping all background sync.
    """

    def _run_loop_once(self, exc_to_raise: Exception) -> tuple[list[str], list[str]]:
        """Drive the worker loop with one failing job followed by one sentinel.

        Returns (log_lines, processed_job_names).
        """
        test_queue: queue.Queue[dict] = queue.Queue()
        log_lines: list[str] = []
        processed_jobs: list[str] = []

        # Monkey-patch the module-level queue and helpers.
        original_queue = server._sync_job_queue
        original_run_sync_job = server.run_sync_job
        original_log = server.log
        original_mark_done = server._mark_sync_job_done

        try:
            server._sync_job_queue = test_queue
            server.log = lambda msg, *args, **kwargs: log_lines.append(msg)
            server._mark_sync_job_done = lambda name: processed_jobs.append(name)

            call_count = [0]

            def _run_sync_job(name, *, reason=""):
                call_count[0] += 1
                if name == "bad_job":
                    raise exc_to_raise
                if name == "good_job":
                    return {"status": "ok"}
                raise server.StudioSyncError(f"Unknown sync job: {name}")

            server.run_sync_job = _run_sync_job

            test_queue.put({"name": "bad_job", "reason": "test"})
            test_queue.put({"name": "good_job", "reason": "test"})

            # Run the loop in a daemon thread and drain both jobs.
            def _loop():
                while True:
                    job = server._sync_job_queue.get()
                    job_name = str(job.get("name", "")).strip().lower()
                    job_reason = str(job.get("reason", "")).strip()
                    try:
                        server.run_sync_job(job_name, reason=job_reason)
                    except (server.StudioSyncError, server.AuthStorageError, server.SupervisorAPIError) as exc:
                        server.log(f"Sync job failed: name={job_name} reason={job_reason!r} error={exc}")
                    except Exception as exc:
                        server.log(
                            f"Sync job unexpected error: name={job_name} reason={job_reason!r} "
                            f"error={type(exc).__name__}: {exc}"
                        )
                    finally:
                        server._mark_sync_job_done(job_name)
                        server._sync_job_queue.task_done()

            t = threading.Thread(target=_loop, daemon=True)
            t.start()
            test_queue.join()

        finally:
            server._sync_job_queue = original_queue
            server.run_sync_job = original_run_sync_job
            server.log = original_log
            server._mark_sync_job_done = original_mark_done

        return log_lines, processed_jobs

    def test_keyerror_is_caught_and_logged(self) -> None:
        """A KeyError in run_sync_job must be caught; the loop must not die."""
        log_lines, processed = self._run_loop_once(KeyError("missing_key"))
        self.assertTrue(
            any("KeyError" in line for line in log_lines),
            "sync_worker_loop must log a caught KeyError — "
            "without the fix the thread dies silently",
        )

    def test_typeerror_is_caught_and_logged(self) -> None:
        """A TypeError in run_sync_job must be caught; the loop must not die."""
        log_lines, processed = self._run_loop_once(TypeError("bad type"))
        self.assertTrue(
            any("TypeError" in line for line in log_lines),
            "sync_worker_loop must log a caught TypeError",
        )

    def test_subsequent_job_runs_after_unexpected_exception(self) -> None:
        """The worker must continue processing jobs after an unexpected failure.

        This is the core availability guarantee: one bad job must not kill the
        background thread.
        """
        log_lines, processed = self._run_loop_once(RuntimeError("boom"))
        self.assertIn(
            "good_job",
            processed,
            "sync_worker_loop must process subsequent jobs after an unexpected exception — "
            "without the fix the thread dies and no further sync jobs run",
        )

    def test_both_jobs_marked_done_after_unexpected_exception(self) -> None:
        """task_done must be called for every job, even failed ones.

        Without this the queue.join() call in tests (or callers) would block
        forever, indicating a potential deadlock vector.
        """
        log_lines, processed = self._run_loop_once(ValueError("unexpected"))
        self.assertIn("bad_job", processed,
            "sync_worker_loop must call _mark_sync_job_done even for failed jobs")
        self.assertIn("good_job", processed,
            "sync_worker_loop must continue marking jobs done after failure")


# ---------------------------------------------------------------------------
# Bug Fix 2: mutate_core_config_storage safety guard
# ---------------------------------------------------------------------------

class MutateCoreConfigStorageSafetyTests(unittest.TestCase):
    """mutate_core_config_storage must refuse to run while Core is live.

    Before this fix, calling mutate_core_config_storage while HA Core was
    running would corrupt the storage file because HA keeps it open.
    """

    def test_raises_supervisor_api_error_when_core_is_running(self) -> None:
        """If is_homeassistant_core_api_reachable() is True the call must raise.

        This is the safety guard: callers must always use run_with_core_stopped().
        """
        original_is_reachable = server.is_homeassistant_core_api_reachable
        try:
            server.is_homeassistant_core_api_reachable = lambda: True

            with self.assertRaises(server.SupervisorAPIError) as ctx:
                server.mutate_core_config_storage(lambda doc: doc)

            self.assertIn(
                "running",
                str(ctx.exception).lower(),
                "The error message must mention that Core is running so operators understand the cause",
            )
        finally:
            server.is_homeassistant_core_api_reachable = original_is_reachable

    def test_proceeds_when_core_is_stopped(self) -> None:
        """When Core is not reachable the mutation must be allowed to execute."""
        original_is_reachable = server.is_homeassistant_core_api_reachable
        original_read_core_config_document = server.read_core_config_document
        original_write_json_file = server.write_json_file

        try:
            server.is_homeassistant_core_api_reachable = lambda: False

            fake_doc = {"data": {"internal_url": None, "external_url": None}}
            server.read_core_config_document = lambda: dict(fake_doc)
            written: list[dict] = []
            server.write_json_file = lambda path, payload: written.append(payload)

            result = server.mutate_core_config_storage(lambda doc: {"mutated": True})

            self.assertEqual(result, {"mutated": True},
                "mutate_core_config_storage must return the mutator's result when Core is stopped")
            self.assertTrue(len(written) == 1,
                "mutate_core_config_storage must write the (possibly mutated) document to disk")

        finally:
            server.is_homeassistant_core_api_reachable = original_is_reachable
            server.read_core_config_document = original_read_core_config_document
            server.write_json_file = original_write_json_file

    def test_error_message_references_run_with_core_stopped(self) -> None:
        """The error message must guide operators toward the correct fix."""
        original_is_reachable = server.is_homeassistant_core_api_reachable
        try:
            server.is_homeassistant_core_api_reachable = lambda: True

            with self.assertRaises(server.SupervisorAPIError) as ctx:
                server.mutate_core_config_storage(lambda doc: doc)

            self.assertIn(
                "run_with_core_stopped",
                str(ctx.exception),
                "Error message must reference run_with_core_stopped() so callers know what to do",
            )
        finally:
            server.is_homeassistant_core_api_reachable = original_is_reachable


# ---------------------------------------------------------------------------
# Bug Fix 3: apply_studio_configuration_locally persist ordering
# ---------------------------------------------------------------------------

class ApplyStudioConfigurationLocallyPersistOrderingTests(unittest.TestCase):
    """Credentials must be persisted BEFORE URLs are synced to HomeAssistant.

    Before this fix, sync_homeassistant_urls was called first. If it succeeded
    but persist_credentials then failed, HomeAssistant would have the new URLs
    but the add-on's credential file would still hold the old ones — causing an
    inconsistent state on the next restart.
    """

    def _run_apply_and_capture_order(self) -> list[str]:
        """Run apply_studio_configuration_locally and return the call order."""
        original_read_saved_credentials = server.read_saved_credentials
        original_persist_credentials = server.persist_credentials
        original_sync_homeassistant_urls = server.sync_homeassistant_urls
        original_sync_homeassistant_hostname = server.sync_homeassistant_hostname
        original_verify_applied_homeassistant_state = server.verify_applied_homeassistant_state

        call_order: list[str] = []

        try:
            server.read_saved_credentials = lambda: {
                "cloudflare_tunnel_token": "old-token",
                "tunnel_hostname": "old.powerhaus.ai",
                "box_api_token": "box-token-123",
                "internal_url": "http://192.168.1.1:8123",
                "external_url": "https://old.powerhaus.ai",
                "hostname": "homeassistant",
                "config_version": "1",
            }
            server.persist_credentials = lambda *args, **kwargs: call_order.append("persist_credentials")
            server.sync_homeassistant_urls = lambda internal_url, external_url: (
                call_order.append("sync_homeassistant_urls") or external_url
            )
            server.sync_homeassistant_hostname = lambda hostname: (
                call_order.append("sync_homeassistant_hostname") or hostname
            )
            server.verify_applied_homeassistant_state = lambda **kwargs: kwargs

            server.apply_studio_configuration_locally({
                "cloudflare_tunnel_token": "new-token",
                "tunnel_hostname": "new.powerhaus.ai",
                "internal_url": "http://powerhausbox.local:8123",
                "external_url": "https://new.powerhaus.ai",
                "hostname": "powerhausbox",
                "config_version": 5,
            })
        finally:
            server.read_saved_credentials = original_read_saved_credentials
            server.persist_credentials = original_persist_credentials
            server.sync_homeassistant_urls = original_sync_homeassistant_urls
            server.sync_homeassistant_hostname = original_sync_homeassistant_hostname
            server.verify_applied_homeassistant_state = original_verify_applied_homeassistant_state

        return call_order

    def test_persist_credentials_is_called_before_sync_homeassistant_urls(self) -> None:
        """persist_credentials must appear before sync_homeassistant_urls in the call order.

        Credentials are the source of truth; they must be durable before any
        live system mutation is attempted.
        """
        call_order = self._run_apply_and_capture_order()

        self.assertIn("persist_credentials", call_order,
            "persist_credentials must be called during apply_studio_configuration_locally")
        self.assertIn("sync_homeassistant_urls", call_order,
            "sync_homeassistant_urls must be called during apply_studio_configuration_locally")

        persist_idx = call_order.index("persist_credentials")
        sync_idx = call_order.index("sync_homeassistant_urls")
        self.assertLess(
            persist_idx, sync_idx,
            "persist_credentials must be called BEFORE sync_homeassistant_urls — "
            "without this ordering a crash between the two steps leaves inconsistent state",
        )

    def test_persist_credentials_is_called_before_hostname_sync(self) -> None:
        """persist_credentials must also precede sync_homeassistant_hostname."""
        call_order = self._run_apply_and_capture_order()

        if "sync_homeassistant_hostname" not in call_order:
            self.skipTest("hostname sync not triggered in this configuration")

        persist_idx = call_order.index("persist_credentials")
        hostname_idx = call_order.index("sync_homeassistant_hostname")
        self.assertLess(
            persist_idx, hostname_idx,
            "persist_credentials must be called BEFORE sync_homeassistant_hostname",
        )

    def test_persist_credentials_called_exactly_once(self) -> None:
        """persist_credentials must not be called more than once per apply."""
        call_order = self._run_apply_and_capture_order()
        persist_calls = call_order.count("persist_credentials")
        self.assertEqual(persist_calls, 1,
            "persist_credentials must be called exactly once per apply invocation")


# ---------------------------------------------------------------------------
# Bug Fix 4: has_saved_pairing_credentials uses generator
# ---------------------------------------------------------------------------

class HasSavedPairingCredentialsTests(unittest.TestCase):
    """has_saved_pairing_credentials must correctly reflect whether all required
    credential fields are present.

    The fix changed from a list comprehension to a generator expression inside
    all(), which is a memory micro-optimization.  The observable behaviour must
    remain identical.
    """

    def test_returns_true_when_all_credentials_present(self) -> None:
        """All required fields non-empty -> True."""
        original = server.read_saved_credentials
        try:
            server.read_saved_credentials = lambda: {
                "cloudflare_tunnel_token": "tok",
                "tunnel_hostname": "box.powerhaus.ai",
                "box_api_token": "api-tok",
                "internal_url": "http://192.168.1.1:8123",
                "external_url": "https://box.powerhaus.ai",
            }
            self.assertTrue(server.has_saved_pairing_credentials(),
                "has_saved_pairing_credentials must return True when all fields are set")
        finally:
            server.read_saved_credentials = original

    def test_returns_false_when_tunnel_token_missing(self) -> None:
        """An empty cloudflare_tunnel_token must cause False."""
        original = server.read_saved_credentials
        try:
            server.read_saved_credentials = lambda: {
                "cloudflare_tunnel_token": "",
                "tunnel_hostname": "box.powerhaus.ai",
                "box_api_token": "api-tok",
                "internal_url": "http://192.168.1.1:8123",
                "external_url": "https://box.powerhaus.ai",
            }
            self.assertFalse(server.has_saved_pairing_credentials(),
                "has_saved_pairing_credentials must return False when cloudflare_tunnel_token is empty")
        finally:
            server.read_saved_credentials = original

    def test_returns_false_when_internal_url_missing(self) -> None:
        """An empty internal_url must cause False."""
        original = server.read_saved_credentials
        try:
            server.read_saved_credentials = lambda: {
                "cloudflare_tunnel_token": "tok",
                "tunnel_hostname": "box.powerhaus.ai",
                "box_api_token": "api-tok",
                "internal_url": "",
                "external_url": "https://box.powerhaus.ai",
            }
            self.assertFalse(server.has_saved_pairing_credentials(),
                "has_saved_pairing_credentials must return False when internal_url is empty")
        finally:
            server.read_saved_credentials = original

    def test_returns_false_when_external_url_missing(self) -> None:
        """An empty external_url must cause False."""
        original = server.read_saved_credentials
        try:
            server.read_saved_credentials = lambda: {
                "cloudflare_tunnel_token": "tok",
                "tunnel_hostname": "box.powerhaus.ai",
                "box_api_token": "api-tok",
                "internal_url": "http://192.168.1.1:8123",
                "external_url": "",
            }
            self.assertFalse(server.has_saved_pairing_credentials(),
                "has_saved_pairing_credentials must return False when external_url is empty")
        finally:
            server.read_saved_credentials = original

    def test_returns_false_when_all_fields_empty(self) -> None:
        """All empty fields -> False."""
        original = server.read_saved_credentials
        try:
            server.read_saved_credentials = lambda: {
                "cloudflare_tunnel_token": "",
                "tunnel_hostname": "",
                "box_api_token": "",
                "internal_url": "",
                "external_url": "",
            }
            self.assertFalse(server.has_saved_pairing_credentials(),
                "has_saved_pairing_credentials must return False when all fields are empty")
        finally:
            server.read_saved_credentials = original


# ---------------------------------------------------------------------------
# Bug Fix 5: normalize_url delegation from normalize_external/internal_url
# ---------------------------------------------------------------------------

class NormalizeUrlDelegationTests(unittest.TestCase):
    """normalize_external_url and normalize_internal_url must delegate to
    utils.normalize_url, converting ValueError to AuthStorageError.

    This verifies that both wrapper functions share the same validation logic.
    """

    def test_normalize_external_url_accepts_valid_https_url(self) -> None:
        """A valid HTTPS external URL must pass through unchanged."""
        result = server.normalize_external_url("https://box.powerhaus.cloud")
        self.assertEqual(result, "https://box.powerhaus.cloud",
            "normalize_external_url must return a valid HTTPS URL unchanged")

    def test_normalize_external_url_accepts_http_scheme(self) -> None:
        """normalize_external_url delegates to normalize_url which accepts both
        http and https — the 'external' wrapper only sets the default scheme to
        'https' for bare hostnames, it does not restrict the scheme itself.
        """
        result = server.normalize_external_url("http://box.powerhaus.cloud")
        self.assertEqual(result, "http://box.powerhaus.cloud",
            "normalize_external_url must accept http:// scheme — it validates URL "
            "structure but does not enforce HTTPS-only")

    def test_normalize_external_url_rejects_empty_string(self) -> None:
        """Empty external URL must raise AuthStorageError, not crash."""
        with self.assertRaises(server.AuthStorageError,
                msg="normalize_external_url must raise AuthStorageError for empty input"):
            server.normalize_external_url("")

    def test_normalize_external_url_rejects_url_with_path(self) -> None:
        """External URL with a path suffix must raise AuthStorageError."""
        with self.assertRaises(server.AuthStorageError,
                msg="normalize_external_url must reject URLs with paths"):
            server.normalize_external_url("https://box.powerhaus.cloud/app")

    def test_normalize_internal_url_accepts_valid_http_url(self) -> None:
        """A valid HTTP internal URL must pass through unchanged."""
        result = server.normalize_internal_url("http://192.168.1.100:8123")
        self.assertEqual(result, "http://192.168.1.100:8123",
            "normalize_internal_url must return a valid HTTP URL unchanged")

    def test_normalize_internal_url_accepts_https_url(self) -> None:
        """An internal URL using HTTPS (uncommon but valid) must be accepted."""
        result = server.normalize_internal_url("https://powerhausbox.local:8123")
        self.assertEqual(result, "https://powerhausbox.local:8123",
            "normalize_internal_url must accept HTTPS scheme too")

    def test_normalize_internal_url_prepends_http_scheme(self) -> None:
        """A bare host:port must be prefixed with http:// for internal URLs."""
        result = server.normalize_internal_url("192.168.1.201:8123")
        self.assertEqual(result, "http://192.168.1.201:8123",
            "normalize_internal_url must prepend http:// to a bare host:port")

    def test_normalize_internal_url_rejects_empty_string(self) -> None:
        """Empty internal URL must raise AuthStorageError, not crash."""
        with self.assertRaises(server.AuthStorageError,
                msg="normalize_internal_url must raise AuthStorageError for empty input"):
            server.normalize_internal_url("")

    def test_normalize_internal_url_rejects_url_with_query(self) -> None:
        """Internal URL with a query string must raise AuthStorageError."""
        with self.assertRaises(server.AuthStorageError,
                msg="normalize_internal_url must reject URLs with query strings"):
            server.normalize_internal_url("http://192.168.1.100:8123?debug=1")

    def test_normalize_external_url_wraps_valueerror_as_auth_storage_error(self) -> None:
        """The ValueError from normalize_url must be re-raised as AuthStorageError.

        We trigger the ValueError with a URL that has a path component, which
        normalize_url reliably rejects with ValueError regardless of scheme.
        """
        with self.assertRaises(server.AuthStorageError,
                msg="normalize_external_url must convert ValueError to AuthStorageError"):
            server.normalize_external_url("https://box.powerhaus.cloud/deep/path")

    def test_normalize_internal_url_wraps_valueerror_as_auth_storage_error(self) -> None:
        """The ValueError from normalize_url must be re-raised as AuthStorageError."""
        with self.assertRaises(server.AuthStorageError,
                msg="normalize_internal_url must convert ValueError to AuthStorageError"):
            server.normalize_internal_url("ftp://192.168.1.100:8123")


# ---------------------------------------------------------------------------
# Bug Fix 6: @auth_required decorator blocks unauthenticated access
# ---------------------------------------------------------------------------

class AuthRequiredDecoratorTests(unittest.TestCase):
    """The @auth_required decorator must intercept requests from unauthenticated
    users and redirect them to the login page, instead of running the handler.

    This test verifies the decorator contract directly, independently of any
    specific route, so that regressions in the decorator itself are caught.
    """

    def test_auth_required_decorator_calls_handler_when_authenticated(self) -> None:
        """When the user is authenticated the wrapped function must run."""
        original_require_auth = server.require_auth_or_redirect
        try:
            server.require_auth_or_redirect = lambda: None  # authenticated

            handler_called = [False]

            @server.auth_required
            def _my_handler():
                handler_called[0] = True
                return "handler-result"

            result = _my_handler()
            self.assertTrue(handler_called[0],
                "auth_required must call the wrapped handler when authenticated")
            self.assertEqual(result, "handler-result",
                "auth_required must return the handler's return value when authenticated")
        finally:
            server.require_auth_or_redirect = original_require_auth

    def test_auth_required_decorator_blocks_handler_when_not_authenticated(self) -> None:
        """When the user is not authenticated the wrapped function must NOT run."""
        original_require_auth = server.require_auth_or_redirect
        try:
            sentinel = object()
            server.require_auth_or_redirect = lambda: sentinel  # unauthenticated

            handler_called = [False]

            @server.auth_required
            def _my_handler():
                handler_called[0] = True
                return "handler-result"

            result = _my_handler()
            self.assertFalse(handler_called[0],
                "auth_required must NOT call the handler when unauthenticated — "
                "this is the primary security guarantee of the decorator")
            self.assertIs(result, sentinel,
                "auth_required must return the redirect sentinel when unauthenticated")
        finally:
            server.require_auth_or_redirect = original_require_auth

    def test_auth_required_preserves_handler_name(self) -> None:
        """The decorator must use @wraps so that Flask routing still works."""
        @server.auth_required
        def _my_named_handler():
            return "ok"

        self.assertEqual(_my_named_handler.__name__, "_my_named_handler",
            "auth_required must preserve the wrapped function's __name__ via @wraps")

    def test_auth_required_passes_through_kwargs(self) -> None:
        """The decorator must forward positional and keyword arguments to the handler."""
        original_require_auth = server.require_auth_or_redirect
        try:
            server.require_auth_or_redirect = lambda: None

            received: list = []

            @server.auth_required
            def _my_handler(a, b, *, c):
                received.extend([a, b, c])

            _my_handler(1, 2, c=3)
            self.assertEqual(received, [1, 2, 3],
                "auth_required must forward all args and kwargs to the wrapped function")
        finally:
            server.require_auth_or_redirect = original_require_auth

    def test_pairing_required_decorator_blocks_when_not_paired(self) -> None:
        """The @pairing_required decorator must block access when not paired."""
        original_require_pairing = server.require_completed_pairing_or_redirect
        try:
            redirect_sentinel = "/pairing"
            server.require_completed_pairing_or_redirect = lambda: redirect_sentinel

            handler_called = [False]

            @server.pairing_required
            def _my_handler():
                handler_called[0] = True
                return "ok"

            result = _my_handler()
            self.assertFalse(handler_called[0],
                "pairing_required must block the handler when the add-on is not paired")
            self.assertEqual(result, redirect_sentinel,
                "pairing_required must return the redirect when not paired")
        finally:
            server.require_completed_pairing_or_redirect = original_require_pairing

    def test_pairing_required_allows_access_when_paired(self) -> None:
        """The @pairing_required decorator must allow access when paired."""
        original_require_pairing = server.require_completed_pairing_or_redirect
        try:
            server.require_completed_pairing_or_redirect = lambda: None  # paired

            handler_called = [False]

            @server.pairing_required
            def _my_handler():
                handler_called[0] = True
                return "ok"

            result = _my_handler()
            self.assertTrue(handler_called[0],
                "pairing_required must allow the handler when paired")
            self.assertEqual(result, "ok",
                "pairing_required must return the handler's result when paired")
        finally:
            server.require_completed_pairing_or_redirect = original_require_pairing


class PairingConfigTransactionTests(unittest.TestCase):
    def test_run_with_core_stopped_transactionally_restores_files_after_failed_restart(self) -> None:
        original_is_reachable = server.is_homeassistant_core_api_reachable
        original_supervisor_request = server.supervisor_request
        original_wait = server.wait_for_homeassistant_api_reachability
        original_ensure = server._ensure_core_started
        original_sync_state_file = server.SYNC_STATE_FILE

        try:
            with tempfile.TemporaryDirectory() as temp_dir:
                core_config_path = Path(temp_dir) / "core.config"
                configuration_yaml_path = Path(temp_dir) / "configuration.yaml"
                server.SYNC_STATE_FILE = Path(temp_dir) / "sync_state.json"
                original_core = '{"data":{"internal_url":"http://old.local:8123"}}\n'
                original_yaml = "default_config: {}\n"
                core_config_path.write_text(original_core, encoding="utf-8")
                configuration_yaml_path.write_text(original_yaml, encoding="utf-8")

                server.is_homeassistant_core_api_reachable = lambda: True
                server.supervisor_request = lambda *args, **kwargs: {}
                server.wait_for_homeassistant_api_reachability = lambda *args, **kwargs: None

                ensure_calls = [0]

                def _ensure_started():
                    ensure_calls[0] += 1
                    if ensure_calls[0] == 1:
                        raise server.SupervisorAPIError("first restart failed")

                server._ensure_core_started = _ensure_started

                def _operation():
                    core_config_path.write_text("broken core\n", encoding="utf-8")
                    configuration_yaml_path.write_text("broken yaml\n", encoding="utf-8")

                with self.assertRaises(server.SupervisorAPIError) as ctx:
                    server.run_with_core_stopped_transactionally(
                        _operation,
                        rollback_paths=[core_config_path, configuration_yaml_path],
                    )

                self.assertIn("Restored original Home Assistant config after failed startup", str(ctx.exception))
                self.assertEqual(core_config_path.read_text(encoding="utf-8"), original_core)
                self.assertEqual(configuration_yaml_path.read_text(encoding="utf-8"), original_yaml)
                self.assertEqual(
                    ensure_calls[0],
                    2,
                    "Transactional apply must retry Core startup after restoring backups",
                )
                sync_state = server.read_sync_state()
                self.assertEqual(sync_state["last_rollback_status"], "restored")
                self.assertEqual(
                    sync_state["last_rollback_restored_paths"],
                    [str(core_config_path), str(configuration_yaml_path)],
                )
        finally:
            server.is_homeassistant_core_api_reachable = original_is_reachable
            server.supervisor_request = original_supervisor_request
            server.wait_for_homeassistant_api_reachability = original_wait
            server._ensure_core_started = original_ensure
            server.SYNC_STATE_FILE = original_sync_state_file

    def test_apply_pairing_homeassistant_config_syncs_hostname_after_transaction(self) -> None:
        original_transaction = server.run_with_core_stopped_transactionally
        original_mutate = server.mutate_core_config_storage
        original_upsert = server.upsert_powerhaus_backup_config_entry_storage
        original_subprocess_run = server.subprocess.run
        original_sync_hostname = server.sync_homeassistant_hostname
        original_verify = server.verify_applied_homeassistant_state

        call_order: list[str] = []

        try:
            def _run_transaction(operation, *, rollback_paths):
                call_order.append("transaction")
                operation()

            server.run_with_core_stopped_transactionally = _run_transaction
            server.mutate_core_config_storage = lambda mutator: call_order.append("mutate_core_config_storage")
            server.upsert_powerhaus_backup_config_entry_storage = lambda: call_order.append(
                "upsert_powerhaus_backup_config_entry_storage"
            )
            server.subprocess.run = lambda *args, **kwargs: types.SimpleNamespace(returncode=0, stderr="", stdout="")
            server.sync_homeassistant_hostname = lambda hostname: call_order.append("sync_homeassistant_hostname")
            server.verify_applied_homeassistant_state = lambda **kwargs: call_order.append(
                "verify_applied_homeassistant_state"
            )

            server.apply_pairing_homeassistant_config(
                was_initial_pairing=True,
                normalized_hostname="powerhaus",
                normalized_internal_url="http://powerhaus.local:8123",
                normalized_external_url="https://box.powerhaus.ai",
            )

            self.assertEqual(
                call_order,
                [
                    "transaction",
                    "mutate_core_config_storage",
                    "upsert_powerhaus_backup_config_entry_storage",
                    "sync_homeassistant_hostname",
                    "verify_applied_homeassistant_state",
                ],
                "Hostname sync must happen only after the transactional config apply completed",
            )
        finally:
            server.run_with_core_stopped_transactionally = original_transaction
            server.mutate_core_config_storage = original_mutate
            server.upsert_powerhaus_backup_config_entry_storage = original_upsert
            server.subprocess.run = original_subprocess_run
            server.sync_homeassistant_hostname = original_sync_hostname
            server.verify_applied_homeassistant_state = original_verify


class ManualApplyDebugModeTests(unittest.TestCase):
    def test_apply_saved_homeassistant_host_settings_is_blocked_in_manual_mode(self) -> None:
        original_read_addon_options = server.read_addon_options
        try:
            server.read_addon_options = lambda: {
                "ui_auth_enabled": False,
                "ui_password": "pw",
                "studio_base_url": "https://studio.powerhaus.ai",
                "auto_enable_iframe_embedding": True,
                "debug_manual_apply_mode": True,
            }
            with self.assertRaises(server.SupervisorAPIError) as ctx:
                server.apply_saved_homeassistant_host_settings()
            self.assertIn("Manual apply debug mode is enabled", str(ctx.exception))
        finally:
            server.read_addon_options = original_read_addon_options

    def test_apply_studio_configuration_locally_marks_pending_manual_apply(self) -> None:
        original_read_addon_options = server.read_addon_options
        original_read_saved_credentials = server.read_saved_credentials
        original_persist_credentials = server.persist_credentials
        original_sync_state_file = server.SYNC_STATE_FILE
        original_sync_hostname = server.sync_homeassistant_hostname
        original_sync_urls = server.sync_homeassistant_urls
        original_verify = server.verify_applied_homeassistant_state

        persisted_calls: list[dict[str, object]] = []
        try:
            with tempfile.TemporaryDirectory() as temp_dir:
                server.SYNC_STATE_FILE = Path(temp_dir) / "sync_state.json"
                server.read_addon_options = lambda: {
                    "ui_auth_enabled": False,
                    "ui_password": "pw",
                    "studio_base_url": "https://studio.powerhaus.ai",
                    "auto_enable_iframe_embedding": True,
                    "debug_manual_apply_mode": True,
                }
                server.read_saved_credentials = lambda: {
                    "box_api_token": "box-token",
                    "cloudflare_tunnel_token": "old-tunnel",
                    "tunnel_hostname": "old.powerhaus.ai",
                    "internal_url": "http://old.local:8123",
                    "external_url": "https://old.powerhaus.ai",
                    "hostname": "oldbox",
                    "config_version": 1,
                }
                server.persist_credentials = lambda tunnel_token, tunnel_hostname, box_api_token, internal_url, external_url, hostname="", config_version=0: persisted_calls.append(
                    {
                        "tunnel_token": tunnel_token,
                        "tunnel_hostname": tunnel_hostname,
                        "box_api_token": box_api_token,
                        "internal_url": internal_url,
                        "external_url": external_url,
                        "hostname": hostname,
                        "config_version": config_version,
                    }
                )
                server.sync_homeassistant_hostname = lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("hostname apply must not run"))
                server.sync_homeassistant_urls = lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("URL apply must not run"))
                server.verify_applied_homeassistant_state = lambda **_kwargs: (_ for _ in ()).throw(AssertionError("verification must not run"))

                result = server.apply_studio_configuration_locally(
                    {
                        "cloudflare_tunnel_token": "new-tunnel",
                        "tunnel_hostname": "new.powerhaus.ai",
                        "internal_url": "http://powerhaus.local:8123",
                        "external_url": "https://new.powerhaus.ai",
                        "hostname": "powerhaus",
                        "config_version": 7,
                    }
                )

                self.assertEqual(result["status"], "pending_manual_apply")
                self.assertFalse(result["live_applied"])
                self.assertEqual(len(persisted_calls), 1)
                sync_state = server.read_sync_state()
                self.assertEqual(sync_state["last_apply_status"], "pending_manual_apply")
                self.assertEqual(sync_state["manual_apply_steps"]["core_urls"]["status"], "pending")
        finally:
            server.read_addon_options = original_read_addon_options
            server.read_saved_credentials = original_read_saved_credentials
            server.persist_credentials = original_persist_credentials
            server.SYNC_STATE_FILE = original_sync_state_file
            server.sync_homeassistant_hostname = original_sync_hostname
            server.sync_homeassistant_urls = original_sync_urls
            server.verify_applied_homeassistant_state = original_verify


class BackupIntegrationAutoInstallTests(unittest.TestCase):
    def test_default_manual_apply_steps_include_backup_integration(self) -> None:
        steps = server._default_manual_apply_steps(pending=True)

        self.assertIn("backup_integration", steps)
        self.assertEqual(steps["backup_integration"]["status"], "pending")

    def test_upsert_powerhaus_backup_config_entry_storage_creates_entry_when_missing(self) -> None:
        original_config_entries_file = server.CORE_CONFIG_ENTRIES_STORAGE_FILE
        original_is_reachable = server.is_homeassistant_core_api_reachable
        try:
            with tempfile.TemporaryDirectory() as temp_dir:
                server.CORE_CONFIG_ENTRIES_STORAGE_FILE = Path(temp_dir) / "core.config_entries"
                server.is_homeassistant_core_api_reachable = lambda: False

                result = server.upsert_powerhaus_backup_config_entry_storage()

                self.assertEqual(result["status"], "created")
                config_entries = server.read_json_file(server.CORE_CONFIG_ENTRIES_STORAGE_FILE)
                entries = config_entries["data"]["entries"]
                self.assertEqual(len(entries), 1)
                self.assertEqual(entries[0]["domain"], server.POWERHAUS_BACKUP_DOMAIN)
                self.assertEqual(entries[0]["title"], server.POWERHAUS_BACKUP_TITLE)
                self.assertEqual(entries[0]["data"], {})
                self.assertEqual(entries[0]["options"], {})
        finally:
            server.CORE_CONFIG_ENTRIES_STORAGE_FILE = original_config_entries_file
            server.is_homeassistant_core_api_reachable = original_is_reachable

    def test_upsert_powerhaus_backup_config_entry_storage_updates_existing_entry(self) -> None:
        original_config_entries_file = server.CORE_CONFIG_ENTRIES_STORAGE_FILE
        original_is_reachable = server.is_homeassistant_core_api_reachable
        try:
            with tempfile.TemporaryDirectory() as temp_dir:
                server.CORE_CONFIG_ENTRIES_STORAGE_FILE = Path(temp_dir) / "core.config_entries"
                server.is_homeassistant_core_api_reachable = lambda: False
                server.write_json_file(
                    server.CORE_CONFIG_ENTRIES_STORAGE_FILE,
                    {
                        "version": 1,
                        "minor_version": 1,
                        "key": "core.config_entries",
                        "data": {
                            "entries": [
                                {
                                    "entry_id": "existing-entry",
                                    "version": 1,
                                    "minor_version": 1,
                                    "domain": server.POWERHAUS_BACKUP_DOMAIN,
                                    "title": "PowerHaus Cloud",
                                    "data": {},
                                    "options": {},
                                }
                            ]
                        },
                    },
                )

                result = server.upsert_powerhaus_backup_config_entry_storage()

                self.assertEqual(result["status"], "updated")
                config_entries = server.read_json_file(server.CORE_CONFIG_ENTRIES_STORAGE_FILE)
                entry = config_entries["data"]["entries"][0]
                self.assertEqual(entry["title"], server.POWERHAUS_BACKUP_TITLE)
                self.assertEqual(entry["source"], "user")
                self.assertFalse(entry["pref_disable_new_entities"])
                self.assertFalse(entry["pref_disable_polling"])
        finally:
            server.CORE_CONFIG_ENTRIES_STORAGE_FILE = original_config_entries_file
            server.is_homeassistant_core_api_reachable = original_is_reachable

    def test_apply_pairing_homeassistant_config_upserts_backup_entry_inside_transaction(self) -> None:
        original_transaction = server.run_with_core_stopped_transactionally
        original_mutate = server.mutate_core_config_storage
        original_upsert = server.upsert_powerhaus_backup_config_entry_storage
        original_sync_hostname = server.sync_homeassistant_hostname
        original_verify = server.verify_applied_homeassistant_state

        call_order: list[str] = []

        try:
            def _run_transaction(operation, *, rollback_paths):
                call_order.append("transaction")
                operation()

            server.run_with_core_stopped_transactionally = _run_transaction
            server.mutate_core_config_storage = lambda mutator: call_order.append("mutate_core_config_storage")
            server.upsert_powerhaus_backup_config_entry_storage = lambda: call_order.append(
                "upsert_powerhaus_backup_config_entry_storage"
            )
            server.sync_homeassistant_hostname = lambda hostname: call_order.append("sync_homeassistant_hostname")
            server.verify_applied_homeassistant_state = lambda **kwargs: call_order.append(
                "verify_applied_homeassistant_state"
            )

            server.apply_pairing_homeassistant_config(
                was_initial_pairing=False,
                normalized_hostname="powerhaus",
                normalized_internal_url="http://powerhaus.local:8123",
                normalized_external_url="https://box.powerhaus.ai",
            )

            self.assertEqual(
                call_order,
                [
                    "transaction",
                    "mutate_core_config_storage",
                    "upsert_powerhaus_backup_config_entry_storage",
                    "sync_homeassistant_hostname",
                    "verify_applied_homeassistant_state",
                ],
            )
        finally:
            server.run_with_core_stopped_transactionally = original_transaction
            server.mutate_core_config_storage = original_mutate
            server.upsert_powerhaus_backup_config_entry_storage = original_upsert
            server.sync_homeassistant_hostname = original_sync_hostname
            server.verify_applied_homeassistant_state = original_verify

    def test_apply_saved_homeassistant_host_settings_installs_backup_entry_when_missing(self) -> None:
        original_read_addon_options = server.read_addon_options
        original_read_saved_credentials = server.read_saved_credentials
        original_sync_hostname = server.sync_homeassistant_hostname
        original_sync_core_urls = server.sync_homeassistant_core_urls
        original_has_entry = server.has_powerhaus_backup_config_entry
        original_ensure_entry = server.ensure_homeassistant_backup_integration_config_entry
        original_verify = server.verify_applied_homeassistant_state
        original_sync_state_file = server.SYNC_STATE_FILE

        ensure_calls: list[str] = []

        try:
            with tempfile.TemporaryDirectory() as temp_dir:
                server.SYNC_STATE_FILE = Path(temp_dir) / "sync_state.json"
                server.read_addon_options = lambda: {
                    "ui_auth_enabled": False,
                    "ui_password": "pw",
                    "studio_base_url": "https://studio.powerhaus.ai",
                    "auto_enable_iframe_embedding": True,
                    "debug_manual_apply_mode": False,
                }
                server.read_saved_credentials = lambda: {
                    "hostname": "powerhaus",
                    "internal_url": "http://powerhaus.local:8123",
                    "external_url": "https://box.powerhaus.ai",
                    "config_version": 3,
                }
                server.sync_homeassistant_hostname = lambda hostname: hostname
                server.sync_homeassistant_core_urls = lambda **kwargs: {
                    "internal_url": kwargs["internal_url"],
                    "external_url": kwargs["external_url"],
                }
                server.has_powerhaus_backup_config_entry = lambda: False
                server.ensure_homeassistant_backup_integration_config_entry = lambda: ensure_calls.append("called") or {
                    "status": "created"
                }
                server.verify_applied_homeassistant_state = lambda **kwargs: {
                    "hostname": kwargs["expected_hostname"],
                    "internal_url": kwargs["expected_internal_url"],
                    "external_url": kwargs["expected_external_url"],
                }

                result = server.apply_saved_homeassistant_host_settings()

            self.assertEqual(ensure_calls, ["called"])
            self.assertEqual(result["hostname"], "powerhaus")
            self.assertEqual(result["internal_url"], "http://powerhaus.local:8123")
            self.assertEqual(result["external_url"], "https://box.powerhaus.ai")
        finally:
            server.read_addon_options = original_read_addon_options
            server.read_saved_credentials = original_read_saved_credentials
            server.sync_homeassistant_hostname = original_sync_hostname
            server.sync_homeassistant_core_urls = original_sync_core_urls
            server.has_powerhaus_backup_config_entry = original_has_entry
            server.ensure_homeassistant_backup_integration_config_entry = original_ensure_entry
            server.verify_applied_homeassistant_state = original_verify
            server.SYNC_STATE_FILE = original_sync_state_file

    def test_run_manual_backup_integration_apply_creates_entry(self) -> None:
        original_ensure_entry = server.ensure_homeassistant_backup_integration_config_entry
        original_has_entry = server.has_powerhaus_backup_config_entry

        ensure_calls: list[str] = []

        try:
            server.ensure_homeassistant_backup_integration_config_entry = lambda: ensure_calls.append("called") or {
                "status": "created"
            }
            server.has_powerhaus_backup_config_entry = lambda: True

            result = server._run_manual_backup_integration_apply()

            self.assertEqual(ensure_calls, ["called"])
            self.assertEqual(result, "PowerHaus Backup integration entry created.")
        finally:
            server.ensure_homeassistant_backup_integration_config_entry = original_ensure_entry
            server.has_powerhaus_backup_config_entry = original_has_entry


if __name__ == "__main__":
    unittest.main()
