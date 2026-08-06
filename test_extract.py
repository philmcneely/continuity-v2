"""Unit tests for extract-to-fleet-memory.py defensive guards.

Tests verify that:
- store_in_fleet_memory() handles complete facts, missing 'type', and missing 'fact'
- The main extraction loop handles a mix of good and bad facts without crashing
"""

import importlib.util
import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

# ---------------------------------------------------------------------------
# Import the module under test by path (filename contains hyphens).
# ---------------------------------------------------------------------------
_module_path = Path(__file__).parent / "extract-to-fleet-memory.py"
_spec = importlib.util.spec_from_file_location("extract_to_fleet_memory", _module_path)
etfm = importlib.util.module_from_spec(_spec)
sys.modules["extract_to_fleet_memory"] = etfm
_spec.loader.exec_module(etfm)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _ok_subprocess():
    """Return a mock subprocess result indicating a fresh store."""
    m = MagicMock()
    m.returncode = 0
    m.stdout = '{"success": true, "message": "Memory stored successfully", "content_hash": "abc123"}'
    return m


def _duplicate_subprocess():
    """Return a mock subprocess result indicating the server rejected a dup."""
    m = MagicMock()
    m.returncode = 0
    m.stdout = '{"success": false, "message": "Duplicate content detected (exact match)", "content_hash": "abc123"}'
    return m


# ---------------------------------------------------------------------------
# Tests for store_in_fleet_memory()
# ---------------------------------------------------------------------------

class TestStoreInFleetMemory(unittest.TestCase):

    @patch.object(etfm.subprocess, "run")
    def test_complete_fact_does_not_raise(self, mock_run):
        """A fully-formed fact dict (type, fact, tags) should store without error."""
        mock_run.return_value = _ok_subprocess()
        fact = {
            "type": "decision",
            "fact": "Use WAL mode for all SQLite connections.",
            "tags": ["sqlite", "continuity"],
        }
        result = etfm.store_in_fleet_memory(fact, "session-abc", "kirk")
        self.assertEqual(result, "stored")
        mock_run.assert_called_once()
        cmd = mock_run.call_args[0][0]
        payload_str = cmd[cmd.index("-d") + 1]
        self.assertIn("[decision]", payload_str)
        self.assertIn("Use WAL mode", payload_str)

    @patch.object(etfm.subprocess, "run")
    def test_duplicate_content_reported_not_counted_as_stored(self, mock_run):
        """A server-side exact-match duplicate must surface as 'duplicate', not 'stored'."""
        mock_run.return_value = _duplicate_subprocess()
        fact = {
            "type": "config",
            "fact": "Fleet memory listens on 192.168.0.225:8766.",
            "tags": [],
        }
        result = etfm.store_in_fleet_memory(fact, "session-abc", "kirk")
        self.assertEqual(result, "duplicate")

    @patch.object(etfm.subprocess, "run")
    def test_missing_type_uses_unknown(self, mock_run):
        """A fact dict missing 'type' should not raise and should use 'unknown'."""
        mock_run.return_value = _ok_subprocess()
        fact = {
            "fact": "The fleet memory API listens on port 8766.",
            "tags": ["fleet-memory"],
        }
        result = etfm.store_in_fleet_memory(fact, "session-def", "beelink")
        self.assertEqual(result, "stored")
        cmd = mock_run.call_args[0][0]
        payload_str = cmd[cmd.index("-d") + 1]
        self.assertIn("[unknown]", payload_str)
        self.assertIn("port 8766", payload_str)

    @patch.object(etfm.subprocess, "run")
    def test_missing_fact_skips_gracefully(self, mock_run):
        """A fact dict missing 'fact' should not raise and should return False (skip)."""
        mock_run.return_value = _ok_subprocess()
        fact = {
            "type": "config",
            "tags": ["orphaned"],
        }
        result = etfm.store_in_fleet_memory(fact, "session-ghi", "hal9000")
        self.assertEqual(result, "failed")
        mock_run.assert_not_called()

    @patch.object(etfm.subprocess, "run")
    def test_empty_fact_string_skips_gracefully(self, mock_run):
        """A fact dict with an empty string for 'fact' should skip without error."""
        mock_run.return_value = _ok_subprocess()
        fact = {"type": "outcome", "fact": "", "tags": []}
        result = etfm.store_in_fleet_memory(fact, "session-jkl", "kirk")
        self.assertEqual(result, "failed")
        mock_run.assert_not_called()


# ---------------------------------------------------------------------------
# Tests for extract_facts_via_llm() timeout resilience
# ---------------------------------------------------------------------------

class TestExtractFactsViaLlmTimeout(unittest.TestCase):

    @patch.object(etfm.subprocess, "run")
    def test_timeout_returns_empty_list_not_raise(self, mock_run):
        """A slow/overloaded model must not crash the run — one session's
        timeout used to take down every other session in the same batch."""
        mock_run.side_effect = etfm.subprocess.TimeoutExpired(cmd="curl", timeout=120)
        result = etfm.extract_facts_via_llm("some transcript")
        self.assertEqual(result, [])


# ---------------------------------------------------------------------------
# Tests for the main extraction loop quarantine guard
# ---------------------------------------------------------------------------

class TestMainLoopQuarantineGuard(unittest.TestCase):

    @patch.object(etfm.subprocess, "run")
    @patch.object(etfm, "get_recent_sessions")
    @patch.object(etfm, "get_session_text")
    @patch.object(etfm, "extract_facts_via_llm")
    @patch.object(etfm, "save_state")
    @patch.object(etfm, "load_state")
    @patch.object(etfm, "sqlite3")
    @patch.object(etfm, "DB_PATH")
    def test_mixed_facts_no_crash(
        self,
        mock_db_path,
        mock_sqlite,
        mock_load_state,
        mock_save_state,
        mock_extract,
        mock_text,
        mock_sessions,
        mock_run,
    ):
        """Main loop must process a mix of good and bad facts without crashing."""
        # DB_PATH.exists() → True so main() doesn't bail early
        mock_db_path.exists.return_value = True

        # sqlite3.connect() returns a usable conn mock
        conn_mock = MagicMock()
        conn_mock.execute.return_value = conn_mock
        mock_sqlite.connect.return_value = conn_mock

        mock_load_state.return_value = {"last_extracted": {}, "last_extracted_turn_idx": {}}

        mock_sessions.return_value = [
            ("sess-001", "Test Session", "kirk", "2026-07-01", "2026-07-01", 8)
        ]
        # (transcript, last_turn_idx_included) — long enough to pass the length check
        mock_text.return_value = ("A" * 300, 7)

        # Mix: good, missing-type, quarantine (missing both), missing-fact
        mock_extract.return_value = [
            {"type": "decision", "fact": "Use async IO for all DB reads.", "tags": []},
            {"fact": "HAL9000 runs on Ubuntu 22.04.", "tags": ["hal9000"]},
            {"tags": ["garbage"]},          # missing both type AND fact → quarantine
            {"type": "config", "tags": []}, # missing fact → skip in store_in_fleet_memory
        ]

        mock_run.return_value = _ok_subprocess()

        with patch("sys.argv", ["extract-to-fleet-memory.py", "--hours", "1"]):
            rc = etfm.main()

        # No exception raised, clean exit code
        self.assertEqual(rc, 0)


if __name__ == "__main__":
    unittest.main()
