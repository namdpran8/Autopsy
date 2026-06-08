"""
tests/test_pipeline.py
======================
Unit tests for the autodebug-agent bug detection pipeline.

Run with::

    pytest tests/ -v
"""

from __future__ import annotations

import textwrap

from agent.bug_detector import detect_bug, extract_affected_files


# ------------------------------------------------------------------
# Test 1: IndexError detection
# ------------------------------------------------------------------
class TestDetectBugFindsIndexError:
    """detect_bug must identify a Python IndexError from a traceback."""

    SAMPLE_LOG = textwrap.dedent("""\
        2026-06-08 14:22:01 INFO  Starting data pipeline ...
        2026-06-08 14:22:02 INFO  Loading dataset (1024 rows)
        Traceback (most recent call last):
          File "/app/pipeline/loader.py", line 87, in process_batch
            row = batch[idx]
          File "/app/pipeline/utils.py", line 42, in __getitem__
            return self._rows[index]
        IndexError: list index out of range
    """)

    def test_detected_is_true(self) -> None:
        result = detect_bug(self.SAMPLE_LOG)
        assert result["detected"] is True

    def test_error_type_is_index_error(self) -> None:
        result = detect_bug(self.SAMPLE_LOG)
        assert result["error_type"] == "IndexError"

    def test_severity_is_high(self) -> None:
        result = detect_bug(self.SAMPLE_LOG)
        assert result["severity"] == "high"

    def test_message_contains_index_error(self) -> None:
        result = detect_bug(self.SAMPLE_LOG)
        assert "IndexError" in result["message"]

    def test_file_reference_points_to_innermost_frame(self) -> None:
        result = detect_bug(self.SAMPLE_LOG)
        assert "utils.py" in result["file"]
        assert "42" in result["file"]


# ------------------------------------------------------------------
# Test 2: No bug detected on clean log
# ------------------------------------------------------------------
class TestDetectBugNoMatch:
    """detect_bug must return detected=False on clean log output."""

    CLEAN_LOG = textwrap.dedent("""\
        2026-06-08 09:00:00 INFO  Application started on port 8080
        2026-06-08 09:00:01 INFO  Health check passed
        2026-06-08 09:00:05 INFO  Processed 250 requests in 4.8s
        2026-06-08 09:01:00 INFO  Graceful shutdown complete
    """)

    def test_detected_is_false(self) -> None:
        result = detect_bug(self.CLEAN_LOG)
        assert result["detected"] is False

    def test_result_has_no_error_type(self) -> None:
        result = detect_bug(self.CLEAN_LOG)
        assert "error_type" not in result

    def test_empty_string_returns_not_detected(self) -> None:
        result = detect_bug("")
        assert result["detected"] is False

    def test_whitespace_only_returns_not_detected(self) -> None:
        result = detect_bug("   \n\n  ")
        assert result["detected"] is False


# ------------------------------------------------------------------
# Test 3: extract_affected_files
# ------------------------------------------------------------------
class TestExtractAffectedFiles:
    """extract_affected_files must return unique filenames from traces."""

    TRACEBACK_LOG = textwrap.dedent("""\
        Traceback (most recent call last):
          File "/srv/app/handlers/user.py", line 134, in get_profile
            name = user.profile.display_name
          File "/srv/app/models/user.py", line 58, in __getattr__
            return getattr(self._data, key)
        AttributeError: 'NoneType' object has no attribute 'display_name'
    """)

    def test_returns_correct_files(self) -> None:
        files = extract_affected_files(self.TRACEBACK_LOG)
        assert "/srv/app/handlers/user.py" in files
        assert "/srv/app/models/user.py" in files

    def test_returns_unique_entries(self) -> None:
        # Duplicate the traceback — files should still be unique
        doubled = self.TRACEBACK_LOG + "\n" + self.TRACEBACK_LOG
        files = extract_affected_files(doubled)
        assert len(files) == len(set(files))

    def test_empty_input_returns_empty_list(self) -> None:
        assert extract_affected_files("") == []

    def test_clean_log_returns_empty_list(self) -> None:
        clean = "2026-06-08 INFO  All good, no errors here."
        assert extract_affected_files(clean) == []
