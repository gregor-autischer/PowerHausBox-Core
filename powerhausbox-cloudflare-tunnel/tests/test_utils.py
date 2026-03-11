"""Tests for shared utilities in utils.py.

Covers: normalize_url, read_interval_seconds, parse_bool, to_positive_int,
should_run_periodic, write_secret_file, read_json_file.
"""

import importlib.util
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


UTILS_PATH = Path(__file__).resolve().parents[1] / "rootfs" / "opt" / "powerhausbox" / "utils.py"
UTILS_SPEC = importlib.util.spec_from_file_location("powerhausbox_utils_standalone", UTILS_PATH)
utils = importlib.util.module_from_spec(UTILS_SPEC)
assert UTILS_SPEC and UTILS_SPEC.loader
UTILS_SPEC.loader.exec_module(utils)


# ---------------------------------------------------------------------------
# normalize_url
# ---------------------------------------------------------------------------

class NormalizeUrlTests(unittest.TestCase):
    """Tests for the normalize_url shared utility."""

    def test_normalize_url_accepts_https_url_with_default_scheme_https(self) -> None:
        """A fully-qualified HTTPS URL must pass through unchanged."""
        result = utils.normalize_url("https://example.com", default_scheme="https", label="URL")
        self.assertEqual(result, "https://example.com",
            "normalize_url must return scheme://host unchanged when already valid")

    def test_normalize_url_accepts_http_url_with_default_scheme_http(self) -> None:
        """A bare hostname must be prefixed with the supplied default_scheme."""
        result = utils.normalize_url("192.168.1.100:8123", default_scheme="http", label="Internal URL")
        self.assertEqual(result, "http://192.168.1.100:8123",
            "normalize_url must prepend default_scheme when no scheme present")

    def test_normalize_url_strips_trailing_slash(self) -> None:
        """A URL with a trailing slash must have that slash removed."""
        result = utils.normalize_url("https://example.com/", default_scheme="https", label="URL")
        self.assertEqual(result, "https://example.com",
            "normalize_url must strip a bare trailing slash")

    def test_normalize_url_raises_for_empty_string(self) -> None:
        """An empty URL must raise ValueError — callers must never pass empty."""
        with self.assertRaises(ValueError, msg="normalize_url must raise for empty input"):
            utils.normalize_url("", default_scheme="https", label="Test URL")

    def test_normalize_url_raises_for_whitespace_only(self) -> None:
        """Whitespace-only input must raise ValueError, not silently succeed."""
        with self.assertRaises(ValueError, msg="normalize_url must raise for whitespace-only input"):
            utils.normalize_url("   ", default_scheme="https", label="Test URL")

    def test_normalize_url_raises_for_ftp_scheme(self) -> None:
        """FTP scheme is not supported and must raise ValueError."""
        with self.assertRaises(ValueError, msg="normalize_url must reject non-http/https schemes"):
            utils.normalize_url("ftp://files.example.com", default_scheme="https", label="Test URL")

    def test_normalize_url_raises_for_url_with_path(self) -> None:
        """A URL with a non-root path must raise ValueError to prevent misconfiguration."""
        with self.assertRaises(ValueError, msg="normalize_url must reject URLs with paths"):
            utils.normalize_url("https://example.com/some/path", default_scheme="https", label="Test URL")

    def test_normalize_url_raises_for_url_with_query(self) -> None:
        """A URL with a query string must raise ValueError."""
        with self.assertRaises(ValueError, msg="normalize_url must reject URLs with query strings"):
            utils.normalize_url("https://example.com?foo=bar", default_scheme="https", label="Test URL")

    def test_normalize_url_raises_for_url_with_fragment(self) -> None:
        """A URL with a fragment must raise ValueError."""
        with self.assertRaises(ValueError, msg="normalize_url must reject URLs with fragments"):
            utils.normalize_url("https://example.com#section", default_scheme="https", label="Test URL")

    def test_normalize_url_accepts_url_with_port(self) -> None:
        """A URL with a non-default port must be accepted and preserved."""
        result = utils.normalize_url("https://example.com:8443", default_scheme="https", label="URL")
        self.assertEqual(result, "https://example.com:8443",
            "normalize_url must preserve port numbers")

    def test_normalize_url_applies_default_scheme_https(self) -> None:
        """A schemeless HTTPS host must be prefixed with https when default_scheme='https'."""
        result = utils.normalize_url("box.powerhaus.cloud", default_scheme="https", label="External URL")
        self.assertEqual(result, "https://box.powerhaus.cloud",
            "normalize_url must apply 'https' default_scheme when no scheme in URL")

    def test_normalize_url_applies_default_scheme_http(self) -> None:
        """A schemeless host must be prefixed with http when default_scheme='http'."""
        result = utils.normalize_url("powerhausbox.local:8123", default_scheme="http", label="Internal URL")
        self.assertEqual(result, "http://powerhausbox.local:8123",
            "normalize_url must apply 'http' default_scheme when no scheme in URL")

    def test_normalize_url_raises_for_missing_netloc(self) -> None:
        """A URL that parses to an empty netloc must be rejected."""
        with self.assertRaises(ValueError, msg="normalize_url must reject invalid URLs with no netloc"):
            utils.normalize_url("https://", default_scheme="https", label="Test URL")

    def test_normalize_url_error_label_included_in_exception(self) -> None:
        """The label parameter must appear in the exception message for debuggability."""
        label = "MySpecialLabel"
        with self.assertRaises(ValueError) as ctx:
            utils.normalize_url("", default_scheme="https", label=label)
        self.assertIn(label, str(ctx.exception),
            "normalize_url must include the label in its ValueError message")


# ---------------------------------------------------------------------------
# parse_bool
# ---------------------------------------------------------------------------

class ParseBoolTests(unittest.TestCase):
    """Tests for the parse_bool utility."""

    def test_parse_bool_true_literal(self) -> None:
        self.assertTrue(utils.parse_bool(True, False),
            "parse_bool must return True for boolean True")

    def test_parse_bool_false_literal(self) -> None:
        self.assertFalse(utils.parse_bool(False, True),
            "parse_bool must return False for boolean False")

    def test_parse_bool_string_true(self) -> None:
        for val in ("true", "True", "TRUE", "1", "yes", "on"):
            with self.subTest(val=val):
                self.assertTrue(utils.parse_bool(val, False),
                    f"parse_bool must return True for string {val!r}")

    def test_parse_bool_string_false(self) -> None:
        for val in ("false", "False", "FALSE", "0", "no", "off"):
            with self.subTest(val=val):
                self.assertFalse(utils.parse_bool(val, True),
                    f"parse_bool must return False for string {val!r}")

    def test_parse_bool_none_returns_default(self) -> None:
        self.assertTrue(utils.parse_bool(None, True),
            "parse_bool must return the default when value is None")
        self.assertFalse(utils.parse_bool(None, False),
            "parse_bool must return the default when value is None")

    def test_parse_bool_nonzero_int_returns_true(self) -> None:
        self.assertTrue(utils.parse_bool(42, False),
            "parse_bool must return True for nonzero int")

    def test_parse_bool_zero_int_returns_false(self) -> None:
        self.assertFalse(utils.parse_bool(0, True),
            "parse_bool must return False for int 0")

    def test_parse_bool_unrecognized_string_returns_default(self) -> None:
        self.assertTrue(utils.parse_bool("maybe", True),
            "parse_bool must return default for unrecognized string")
        self.assertFalse(utils.parse_bool("maybe", False),
            "parse_bool must return default for unrecognized string")


# ---------------------------------------------------------------------------
# to_positive_int
# ---------------------------------------------------------------------------

class ToPositiveIntTests(unittest.TestCase):
    """Tests for the to_positive_int utility."""

    def test_to_positive_int_positive_value(self) -> None:
        self.assertEqual(utils.to_positive_int(5, 1), 5,
            "to_positive_int must return the parsed value when positive")

    def test_to_positive_int_zero_returns_default(self) -> None:
        self.assertEqual(utils.to_positive_int(0, 99), 99,
            "to_positive_int must return default for zero (not positive)")

    def test_to_positive_int_negative_returns_default(self) -> None:
        self.assertEqual(utils.to_positive_int(-1, 10), 10,
            "to_positive_int must return default for negative values")

    def test_to_positive_int_string_int(self) -> None:
        self.assertEqual(utils.to_positive_int("7", 1), 7,
            "to_positive_int must parse string representations of ints")

    def test_to_positive_int_none_returns_default(self) -> None:
        self.assertEqual(utils.to_positive_int(None, 42), 42,
            "to_positive_int must return default for None — crash prevention")

    def test_to_positive_int_non_numeric_string_returns_default(self) -> None:
        self.assertEqual(utils.to_positive_int("not-a-number", 5), 5,
            "to_positive_int must return default for non-numeric strings — crash prevention")

    def test_to_positive_int_float_truncates(self) -> None:
        self.assertEqual(utils.to_positive_int(3.9, 1), 3,
            "to_positive_int truncates floats via int()")

    def test_to_positive_int_list_returns_default(self) -> None:
        self.assertEqual(utils.to_positive_int([], 99), 99,
            "to_positive_int must return default for invalid types — crash prevention")


# ---------------------------------------------------------------------------
# read_interval_seconds
# ---------------------------------------------------------------------------

class ReadIntervalSecondsTests(unittest.TestCase):
    """Tests for the read_interval_seconds utility."""

    def test_read_interval_seconds_uses_env_value(self) -> None:
        """An env-var value above the minimum must be returned as-is."""
        with patch.dict(os.environ, {"TEST_INTERVAL": "120"}):
            result = utils.read_interval_seconds("TEST_INTERVAL", default=60, minimum=30)
        self.assertEqual(result, 120,
            "read_interval_seconds must use the env-var value when set and valid")

    def test_read_interval_seconds_uses_default_when_env_missing(self) -> None:
        """When the env var is absent the function must return the default."""
        env_copy = {k: v for k, v in os.environ.items() if k != "MISSING_INTERVAL"}
        with patch.dict(os.environ, env_copy, clear=True):
            result = utils.read_interval_seconds("MISSING_INTERVAL", default=300, minimum=60)
        self.assertEqual(result, 300,
            "read_interval_seconds must return the default when env var is absent")

    def test_read_interval_seconds_clamps_to_minimum(self) -> None:
        """Values below the minimum must be clamped to the minimum for safety."""
        with patch.dict(os.environ, {"TEST_INTERVAL": "5"}):
            result = utils.read_interval_seconds("TEST_INTERVAL", default=60, minimum=30)
        self.assertEqual(result, 30,
            "read_interval_seconds must clamp to minimum to prevent too-frequent polling")

    def test_read_interval_seconds_ignores_non_int_env_value(self) -> None:
        """A non-integer env-var value must fall back to the default."""
        with patch.dict(os.environ, {"TEST_INTERVAL": "not-a-number"}):
            result = utils.read_interval_seconds("TEST_INTERVAL", default=45, minimum=10)
        self.assertEqual(result, 45,
            "read_interval_seconds must use default for non-integer env values — crash prevention")

    def test_read_interval_seconds_clamps_default_to_minimum(self) -> None:
        """Even the default is clamped to the minimum."""
        env_copy = {k: v for k, v in os.environ.items() if k != "TINY_INTERVAL"}
        with patch.dict(os.environ, env_copy, clear=True):
            result = utils.read_interval_seconds("TINY_INTERVAL", default=1, minimum=30)
        self.assertEqual(result, 30,
            "read_interval_seconds must clamp the default itself to the minimum")


# ---------------------------------------------------------------------------
# should_run_periodic
# ---------------------------------------------------------------------------

class ShouldRunPeriodicTests(unittest.TestCase):
    """Tests for the should_run_periodic utility."""

    def test_should_run_periodic_returns_true_when_never_run(self) -> None:
        """A missing/empty last_run_at must always return True."""
        self.assertTrue(utils.should_run_periodic("", 60),
            "should_run_periodic must return True when last_run_at is empty")

    def test_should_run_periodic_returns_true_when_interval_elapsed(self) -> None:
        """When the interval has passed the function must return True."""
        import time
        now = time.time()
        past_ts = utils.datetime.fromtimestamp(now - 120, tz=utils.timezone.utc).isoformat()
        self.assertTrue(utils.should_run_periodic(past_ts, 60, now=now),
            "should_run_periodic must return True when elapsed > interval")

    def test_should_run_periodic_returns_false_when_too_soon(self) -> None:
        """When the interval has not yet elapsed the function must return False."""
        import time
        now = time.time()
        recent_ts = utils.datetime.fromtimestamp(now - 10, tz=utils.timezone.utc).isoformat()
        self.assertFalse(utils.should_run_periodic(recent_ts, 60, now=now),
            "should_run_periodic must return False when elapsed < interval")

    def test_should_run_periodic_returns_true_for_invalid_timestamp(self) -> None:
        """An invalid timestamp must be treated as 'never run'."""
        self.assertTrue(utils.should_run_periodic("not-a-timestamp", 60),
            "should_run_periodic must return True for invalid timestamps — safe default")


# ---------------------------------------------------------------------------
# write_secret_file
# ---------------------------------------------------------------------------

class WriteSecretFileTests(unittest.TestCase):
    """Tests for the write_secret_file utility."""

    def test_write_secret_file_creates_file_with_content(self) -> None:
        """The file must exist after writing and contain the expected content."""
        with tempfile.TemporaryDirectory() as tmp_dir:
            target = Path(tmp_dir) / "secrets.json"
            utils.write_secret_file(target, '{"key": "value"}')
            self.assertTrue(target.exists(),
                "write_secret_file must create the file")
            self.assertEqual(target.read_text(encoding="utf-8"), '{"key": "value"}',
                "write_secret_file must write the exact content")

    def test_write_secret_file_creates_parent_directories(self) -> None:
        """Missing parent directories must be created automatically."""
        with tempfile.TemporaryDirectory() as tmp_dir:
            target = Path(tmp_dir) / "deep" / "nested" / "file.json"
            utils.write_secret_file(target, "data")
            self.assertTrue(target.exists(),
                "write_secret_file must create missing parent directories")

    def test_write_secret_file_does_not_leave_tmp_file_on_success(self) -> None:
        """The temporary .tmp file must be cleaned up after a successful write."""
        with tempfile.TemporaryDirectory() as tmp_dir:
            target = Path(tmp_dir) / "secrets.json"
            utils.write_secret_file(target, "data")
            tmp_file = target.with_suffix(target.suffix + ".tmp")
            self.assertFalse(tmp_file.exists(),
                "write_secret_file must remove the .tmp file after atomic rename")

    def test_write_secret_file_overwrites_existing_content(self) -> None:
        """Calling write_secret_file twice must update the content."""
        with tempfile.TemporaryDirectory() as tmp_dir:
            target = Path(tmp_dir) / "secrets.json"
            utils.write_secret_file(target, "first")
            utils.write_secret_file(target, "second")
            self.assertEqual(target.read_text(encoding="utf-8"), "second",
                "write_secret_file must overwrite existing content")

    def test_write_secret_file_handles_empty_string(self) -> None:
        """Writing an empty string must not crash."""
        with tempfile.TemporaryDirectory() as tmp_dir:
            target = Path(tmp_dir) / "empty.json"
            utils.write_secret_file(target, "")
            self.assertTrue(target.exists(),
                "write_secret_file must handle empty string content without crashing")


# ---------------------------------------------------------------------------
# read_json_file
# ---------------------------------------------------------------------------

class ReadJsonFileTests(unittest.TestCase):
    """Tests for the read_json_file utility."""

    def test_read_json_file_returns_dict_for_valid_json(self) -> None:
        """A valid JSON file must be returned as a dict."""
        with tempfile.TemporaryDirectory() as tmp_dir:
            target = Path(tmp_dir) / "data.json"
            target.write_text('{"hello": "world"}', encoding="utf-8")
            result = utils.read_json_file(target)
            self.assertEqual(result, {"hello": "world"},
                "read_json_file must parse valid JSON correctly")

    def test_read_json_file_returns_empty_dict_when_file_missing(self) -> None:
        """A missing file must return an empty dict, not raise an exception."""
        target = Path("/tmp/does_not_exist_12345.json")
        result = utils.read_json_file(target)
        self.assertEqual(result, {},
            "read_json_file must return {} for missing files — crash prevention")

    def test_read_json_file_returns_empty_dict_for_invalid_json(self) -> None:
        """Malformed JSON must return an empty dict, not propagate an exception."""
        with tempfile.TemporaryDirectory() as tmp_dir:
            target = Path(tmp_dir) / "bad.json"
            target.write_text("{{not valid json{{", encoding="utf-8")
            result = utils.read_json_file(target)
            self.assertEqual(result, {},
                "read_json_file must return {} for invalid JSON — crash prevention")

    def test_read_json_file_returns_empty_dict_for_empty_file(self) -> None:
        """An empty file must return an empty dict, not raise an exception."""
        with tempfile.TemporaryDirectory() as tmp_dir:
            target = Path(tmp_dir) / "empty.json"
            target.write_text("", encoding="utf-8")
            result = utils.read_json_file(target)
            self.assertEqual(result, {},
                "read_json_file must return {} for empty file — crash prevention")


if __name__ == "__main__":
    unittest.main()
