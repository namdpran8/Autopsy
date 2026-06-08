"""
agent/bug_detector.py
=====================
Bug detection sub-agent for autodebug-agent.

Parses raw log / stderr text and extracts structured bug information
for Python exceptions, generic log-level errors, and Java-style
NullPointerExceptions.
"""

from __future__ import annotations

import re
from typing import Any


# ---------------------------------------------------------------------------
# Severity classification
# ---------------------------------------------------------------------------
_CRITICAL_EXCEPTIONS = frozenset({
    "ZeroDivisionError",
    "SystemExit",
    "MemoryError",
    "RecursionError",
    "NullPointerException",
})

_HIGH_EXCEPTIONS = frozenset({
    "KeyError",
    "IndexError",
    "AttributeError",
    "TypeError",
    "FileNotFoundError",
    "PermissionError",
    "ConnectionError",
    "TimeoutError",
})

# Everything else that matches is "medium".


def _classify_severity(error_type: str, message: str) -> str:
    """Return ``"critical"``, ``"high"``, or ``"medium"``."""
    if error_type in _CRITICAL_EXCEPTIONS:
        return "critical"
    if error_type in _HIGH_EXCEPTIONS:
        return "high"

    # Generic log lines: CRITICAL / FATAL → critical, ERROR → high
    upper = message.upper()
    if "FATAL" in upper or "CRITICAL" in upper:
        return "critical"
    if "ERROR" in upper:
        return "high"

    return "medium"


# ---------------------------------------------------------------------------
# Regex patterns
# ---------------------------------------------------------------------------

# Full Python traceback block (DOTALL so . matches newlines)
_RE_PYTHON_TRACEBACK = re.compile(
    r"(Traceback \(most recent call last\):.*?^(\w+(?:Error|Exception|Warning)): .+)",
    re.MULTILINE | re.DOTALL,
)

# The final exception line at the end of a traceback
_RE_PYTHON_EXCEPTION_LINE = re.compile(
    r"^(\w+(?:Error|Exception|Warning)): (.+)$",
    re.MULTILINE,
)

# File references inside a traceback
_RE_TRACEBACK_FILE = re.compile(
    r'File "([^"]+)", line (\d+)',
)

# Generic ERROR / CRITICAL / FATAL log lines
# Matches patterns like:  ERROR something, [ERROR] something, 2024-01-01 ERROR something
_RE_GENERIC_LOG_ERROR = re.compile(
    r"^.*?\b(CRITICAL|FATAL|ERROR)\b[:\s\]\-]+(.+)$",
    re.MULTILINE | re.IGNORECASE,
)

# Java-style NullPointerException
_RE_JAVA_NPE = re.compile(
    r"^(.*?(?:java\.lang\.)?NullPointerException.*)$",
    re.MULTILINE,
)

# Java stack-trace file references:  at com.foo.Bar.method(Bar.java:42)
_RE_JAVA_STACK_FILE = re.compile(
    r"\((\w+\.java):(\d+)\)",
)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

_EMPTY_RESULT: dict[str, Any] = {"detected": False}


def detect_bug(log_text: str) -> dict[str, Any]:
    """Analyse *log_text* and return structured bug information.

    Detection priority:
      1. Python traceback blocks (most specific).
      2. Java-style ``NullPointerException`` lines.
      3. Generic ``ERROR`` / ``CRITICAL`` / ``FATAL`` log lines.

    Returns
    -------
    dict
        On detection::

            {
                "detected": True,
                "error_type": str,
                "message": str,
                "file": str,
                "stack_trace": str,
                "severity": str,
            }

        When nothing is found::

            {"detected": False}
    """
    if not log_text or not log_text.strip():
        return dict(_EMPTY_RESULT)

    # ----- 1. Python traceback -------------------------------------------
    tb_match = _RE_PYTHON_TRACEBACK.search(log_text)
    if tb_match:
        stack_trace = tb_match.group(0).strip()
        error_type = tb_match.group(2)

        # Extract the final exception message
        exc_line_match = _RE_PYTHON_EXCEPTION_LINE.search(stack_trace)
        message = exc_line_match.group(0) if exc_line_match else stack_trace.splitlines()[-1]

        # Find the innermost (last) file reference in the traceback
        file_matches = _RE_TRACEBACK_FILE.findall(stack_trace)
        if file_matches:
            last_file, last_line = file_matches[-1]
            file_ref = f"{last_file}:{last_line}"
        else:
            file_ref = "unknown"

        return {
            "detected": True,
            "error_type": error_type,
            "message": message,
            "file": file_ref,
            "stack_trace": stack_trace,
            "severity": _classify_severity(error_type, message),
        }

    # ----- 2. Java NullPointerException ----------------------------------
    npe_match = _RE_JAVA_NPE.search(log_text)
    if npe_match:
        message = npe_match.group(1).strip()

        # Try to extract a Java stack-trace block (all "at …" lines after the NPE)
        npe_start = npe_match.start()
        remaining = log_text[npe_start:]
        stack_lines = [message]
        for line in remaining.splitlines()[1:]:
            stripped = line.strip()
            if stripped.startswith("at ") or stripped.startswith("..."):
                stack_lines.append(stripped)
            elif stripped.startswith("Caused by:"):
                stack_lines.append(stripped)
            else:
                break
        stack_trace = "\n".join(stack_lines)

        # Innermost Java file reference
        java_files = _RE_JAVA_STACK_FILE.findall(stack_trace)
        file_ref = f"{java_files[-1][0]}:{java_files[-1][1]}" if java_files else "unknown"

        return {
            "detected": True,
            "error_type": "NullPointerException",
            "message": message,
            "file": file_ref,
            "stack_trace": stack_trace,
            "severity": "critical",
        }

    # ----- 3. Generic log-level errors -----------------------------------
    log_match = _RE_GENERIC_LOG_ERROR.search(log_text)
    if log_match:
        level = log_match.group(1).upper()
        message = log_match.group(0).strip()

        return {
            "detected": True,
            "error_type": level,
            "message": message,
            "file": "unknown",
            "stack_trace": "",
            "severity": _classify_severity(level, message),
        }

    # ----- Nothing found -------------------------------------------------
    return dict(_EMPTY_RESULT)


def extract_affected_files(log_text: str) -> list[str]:
    """Return a deduplicated list of filenames mentioned in stack traces.

    Handles both Python-style ``File "path", line N`` references and
    Java-style ``(Foo.java:42)`` references.

    Returns
    -------
    list[str]
        Unique file paths / names in the order they first appear.
    """
    if not log_text or not log_text.strip():
        return []

    seen: set[str] = set()
    result: list[str] = []

    # Python files
    for filepath, _line in _RE_TRACEBACK_FILE.findall(log_text):
        if filepath not in seen:
            seen.add(filepath)
            result.append(filepath)

    # Java files
    for filename, _line in _RE_JAVA_STACK_FILE.findall(log_text):
        if filename not in seen:
            seen.add(filename)
            result.append(filename)

    return result


# ===================================================================
# Self-test harness
# ===================================================================
if __name__ == "__main__":
    import json
    import textwrap

    def _run_test(name: str, log: str) -> None:
        print(f"{'-' * 60}")
        print(f"  TEST: {name}")
        print(f"{'-' * 60}")
        result = detect_bug(log)
        print(json.dumps(result, indent=2))
        files = extract_affected_files(log)
        print(f"  Affected files: {files}")
        print()

    # ------------------------------------------------------------------
    # Test 1 — IndexError
    # ------------------------------------------------------------------
    _run_test(
        "IndexError in data pipeline",
        textwrap.dedent("""\
            2026-06-08 14:22:01 INFO  Starting data pipeline …
            2026-06-08 14:22:03 INFO  Loading dataset (1024 rows)
            Traceback (most recent call last):
              File "/app/pipeline/loader.py", line 87, in process_batch
                row = batch[idx]
              File "/app/pipeline/utils.py", line 42, in __getitem__
                return self._rows[index]
            IndexError: list index out of range
        """),
    )

    # ------------------------------------------------------------------
    # Test 2 — AttributeError
    # ------------------------------------------------------------------
    _run_test(
        "AttributeError — NoneType",
        textwrap.dedent("""\
            [2026-06-08T18:00:00Z] INFO  Initialising service
            [2026-06-08T18:00:01Z] INFO  Connecting to database
            Traceback (most recent call last):
              File "/srv/app/handlers/user.py", line 134, in get_profile
                name = user.profile.display_name
              File "/srv/app/models/user.py", line 58, in __getattr__
                return getattr(self._data, key)
            AttributeError: 'NoneType' object has no attribute 'display_name'
        """),
    )

    # ------------------------------------------------------------------
    # Test 3 — No bug (clean log)
    # ------------------------------------------------------------------
    _run_test(
        "Clean log — no errors",
        textwrap.dedent("""\
            2026-06-08 09:00:00 INFO  Application started on port 8080
            2026-06-08 09:00:01 INFO  Health check passed
            2026-06-08 09:00:05 INFO  Processed 250 requests in 4.8s
            2026-06-08 09:01:00 INFO  Graceful shutdown complete
        """),
    )

    print("=" * 60)
    print("  All 3 test cases executed [OK]")
    print("=" * 60)
