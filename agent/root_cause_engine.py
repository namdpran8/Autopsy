"""
agent/root_cause_engine.py
==========================
Root cause analysis engine for autodebug-agent.

Uses the Google Gemini API to reason over a detected bug and the
relevant source code, producing a structured diagnosis that can be
posted directly to a GitLab issue or merge-request description.
"""

from __future__ import annotations

import json
import logging
import os
import re
import sys
from pathlib import Path
from typing import Any

import google.generativeai as genai

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
logger = logging.getLogger("autodebug.root_cause")
logger.setLevel(logging.DEBUG)

_stderr_handler = logging.StreamHandler(sys.stderr)
_stderr_handler.setLevel(logging.DEBUG)
_stderr_handler.setFormatter(
    logging.Formatter(
        "[%(asctime)s] %(levelname)s %(name)s - %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
)
logger.addHandler(_stderr_handler)


# ---------------------------------------------------------------------------
# Prompt template
# ---------------------------------------------------------------------------
_SYSTEM_PROMPT = """\
You are an expert software debugger. You will receive:
1. A bug report containing an error type, message, traceback, and the file where the error occurred.
2. The source code of the file that triggered the error.

Your task is to perform root-cause analysis and return a JSON object with EXACTLY these keys:

{
  "line": <int - the 1-indexed line number of the faulty code>,
  "reason": "<str - precise technical reason why this line fails>",
  "explanation": "<str - 2-3 sentence plain-English explanation suitable for a GitLab issue description>",
  "confidence": <float between 0.0 and 1.0>,
  "faulty_code_snippet": "<str - the exact faulty line(s) of code>"
}

Rules:
- Return ONLY the JSON object, no markdown fences, no commentary.
- "line" must be a positive integer.
- "confidence" must be a float between 0.0 and 1.0.
- "reason" should reference specific language semantics (e.g. "list index exceeds len()-1").
- "explanation" should be understandable by a developer who has not seen the traceback.
- "faulty_code_snippet" should contain the minimal code that is wrong (1-3 lines).
"""


def _build_user_prompt(bug: dict[str, Any], source_code: str, filename: str) -> str:
    """Assemble the user-facing prompt from the bug report and source."""
    sections = [
        "## Bug Report",
        f"**Error Type:** {bug.get('error_type', 'Unknown')}",
        f"**Message:** {bug.get('message', 'N/A')}",
        f"**File:** {bug.get('file', 'unknown')}",
        f"**Severity:** {bug.get('severity', 'unknown')}",
        "",
        "### Stack Trace",
        "```",
        bug.get("stack_trace", "(no traceback available)"),
        "```",
        "",
        f"## Source Code  ({filename})",
        "```python",
    ]

    # Number every line so Gemini can reference exact line numbers
    for i, line in enumerate(source_code.splitlines(), start=1):
        sections.append(f"{i:>4} | {line}")

    sections.append("```")
    return "\n".join(sections)


# ---------------------------------------------------------------------------
# JSON parsing helpers
# ---------------------------------------------------------------------------
_RE_CODE_FENCE = re.compile(
    r"```(?:json)?\s*\n?(.*?)\n?\s*```",
    re.DOTALL,
)


def _strip_code_fences(text: str) -> str:
    """Remove markdown code fences wrapping a JSON blob, if present."""
    match = _RE_CODE_FENCE.search(text)
    if match:
        return match.group(1).strip()
    return text.strip()


def _safe_parse_json(raw: str) -> dict[str, Any] | None:
    """Attempt to parse *raw* as JSON, stripping fences first.

    Returns
    -------
    dict or None
        Parsed dict on success, ``None`` on failure.
    """
    cleaned = _strip_code_fences(raw)
    try:
        parsed = json.loads(cleaned)
        if isinstance(parsed, dict):
            return parsed
        logger.warning("Gemini returned valid JSON but not an object: %s", type(parsed))
        return None
    except json.JSONDecodeError as exc:
        logger.warning("JSON decode failed: %s", exc)
        return None


def _normalise_result(parsed: dict[str, Any]) -> dict[str, Any]:
    """Coerce types and fill in missing keys so callers get a stable schema."""
    line = parsed.get("line")
    try:
        line = int(line)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        line = 0

    confidence = parsed.get("confidence")
    try:
        confidence = float(confidence)  # type: ignore[arg-type]
        confidence = max(0.0, min(1.0, confidence))
    except (TypeError, ValueError):
        confidence = 0.0

    return {
        "line": line,
        "reason": str(parsed.get("reason", "")),
        "explanation": str(parsed.get("explanation", "")),
        "confidence": confidence,
        "faulty_code_snippet": str(parsed.get("faulty_code_snippet", "")),
    }


def _best_effort_result(raw_response: str) -> dict[str, Any]:
    """Fallback when Gemini's response is not parseable JSON."""
    return {
        "line": 0,
        "reason": "Unable to parse structured response from Gemini.",
        "explanation": raw_response,
        "confidence": 0.0,
        "faulty_code_snippet": "",
    }


# ---------------------------------------------------------------------------
# Core public API
# ---------------------------------------------------------------------------

def _resolve_source_file(
    bug: dict[str, Any],
    codebase: dict[str, str],
) -> tuple[str, str]:
    """Match the bug's file reference to a codebase entry.

    The bug dict's ``"file"`` value typically looks like
    ``"/app/pipeline/utils.py:42"`` — we strip the line number, then
    try progressively shorter suffixes until we find a match in the
    codebase dict.

    Returns
    -------
    tuple[str, str]
        ``(matched_filename, file_content)`` or ``("unknown", "")`` if
        no match is found.
    """
    raw_ref = bug.get("file", "unknown")
    # Strip line number suffix  (e.g. ":42")
    file_path = re.sub(r":\d+$", "", raw_ref)

    # 1. Exact match
    if file_path in codebase:
        return file_path, codebase[file_path]

    # 2. Normalise to forward slashes and try again
    normalised = file_path.replace("\\", "/")
    for key, content in codebase.items():
        key_norm = key.replace("\\", "/")
        if key_norm == normalised:
            return key, content

    # 3. Suffix match (e.g. bug says "/app/utils.py", codebase has "utils.py")
    for key, content in codebase.items():
        key_norm = key.replace("\\", "/")
        if normalised.endswith(key_norm) or key_norm.endswith(normalised):
            return key, content

    # 4. Basename match
    basename = Path(normalised).name
    for key, content in codebase.items():
        if Path(key).name == basename:
            return key, content

    logger.warning(
        "Could not resolve '%s' in the codebase (%d files available).",
        raw_ref,
        len(codebase),
    )
    return "unknown", ""


def find_root_cause(
    model: genai.GenerativeModel,
    bug: dict[str, Any],
    codebase: dict[str, str],
) -> dict[str, Any]:
    """Ask Gemini to diagnose the root cause of *bug* given *codebase*.

    Parameters
    ----------
    model : google.generativeai.GenerativeModel
        An initialised Gemini model (e.g. ``gemini-2.0-flash``).
    bug : dict
        Output of :func:`agent.bug_detector.detect_bug`.
    codebase : dict
        Mapping of ``{"relative/path.py": "file contents"}``.

    Returns
    -------
    dict
        ``{"line", "reason", "explanation", "confidence", "faulty_code_snippet"}``
    """
    if not bug.get("detected"):
        logger.info("No bug detected — skipping root-cause analysis.")
        return _best_effort_result("No bug was detected in the provided log.")

    # --- Resolve the relevant source file --------------------------------
    filename, source_code = _resolve_source_file(bug, codebase)
    if not source_code:
        logger.warning("Source file not found in codebase; sending traceback only.")
        source_code = "(source not available)"

    user_prompt = _build_user_prompt(bug, source_code, filename)

    # --- Call Gemini ------------------------------------------------------
    try:
        logger.info(
            "Sending root-cause query to Gemini (file=%s, error=%s) ...",
            filename,
            bug.get("error_type"),
        )
        response = model.generate_content(
            [
                {"role": "user", "parts": [_SYSTEM_PROMPT + "\n\n" + user_prompt]},
            ],
            generation_config=genai.types.GenerationConfig(
                temperature=0.2,
                max_output_tokens=1024,
            ),
        )
    except Exception as exc:
        logger.error("Gemini API call failed: %s", exc)
        return _best_effort_result(f"Gemini API error: {exc}")

    # --- Extract & parse the response ------------------------------------
    try:
        raw_text = response.text
    except (AttributeError, ValueError) as exc:
        logger.error("Could not read response text: %s", exc)
        return _best_effort_result(f"Empty or blocked Gemini response: {exc}")

    logger.debug("Raw Gemini response:\n%s", raw_text)

    parsed = _safe_parse_json(raw_text)
    if parsed is None:
        logger.warning("Falling back to best-effort result.")
        return _best_effort_result(raw_text)

    result = _normalise_result(parsed)
    logger.info(
        "Root cause identified: line %d, confidence %.2f",
        result["line"],
        result["confidence"],
    )
    return result


# ---------------------------------------------------------------------------
# Codebase loader
# ---------------------------------------------------------------------------

def load_codebase(directory: str) -> dict[str, str]:
    """Read every ``.py`` file under *directory* into a dict.

    Parameters
    ----------
    directory : str
        Absolute or relative path to the project root.

    Returns
    -------
    dict[str, str]
        ``{"relative/path.py": "file content as string", ...}``
    """
    result: dict[str, str] = {}
    root = Path(directory).resolve()

    if not root.is_dir():
        logger.error("Directory does not exist: %s", root)
        return result

    for py_file in root.rglob("*.py"):
        rel_path = py_file.relative_to(root).as_posix()
        try:
            content = py_file.read_text(encoding="utf-8", errors="replace")
            result[rel_path] = content
        except OSError as exc:
            logger.warning("Skipping %s: %s", rel_path, exc)

    logger.info("Loaded %d .py files from %s", len(result), root)
    return result


# ===================================================================
# Self-test / demo harness
# ===================================================================
if __name__ == "__main__":
    import textwrap
    from dotenv import load_dotenv

    load_dotenv()

    api_key = os.getenv("GEMINI_API_KEY")
    if not api_key:
        print("ERROR: Set GEMINI_API_KEY in .env to run this demo.", file=sys.stderr)
        sys.exit(1)

    genai.configure(api_key=api_key)
    model = genai.GenerativeModel("gemini-2.0-flash")

    # Simulated bug from bug_detector
    sample_bug: dict[str, Any] = {
        "detected": True,
        "error_type": "IndexError",
        "message": "IndexError: list index out of range",
        "file": "demo/processor.py:15",
        "stack_trace": textwrap.dedent("""\
            Traceback (most recent call last):
              File "demo/processor.py", line 15, in process
                value = items[idx]
            IndexError: list index out of range
        """),
        "severity": "high",
    }

    # Simulated codebase
    sample_codebase: dict[str, str] = {
        "demo/processor.py": textwrap.dedent("""\
            \"\"\"Simple data processor.\"\"\"

            from __future__ import annotations


            def load_items() -> list[str]:
                return ["alpha", "beta", "gamma"]


            def process(idx: int) -> str:
                \"\"\"Return the item at position *idx*.\"\"\"
                items = load_items()
                # BUG: no bounds check before indexing
                value = items[idx]
                return value.upper()


            if __name__ == "__main__":
                # This will crash when idx >= 3
                print(process(5))
        """),
    }

    print("=" * 60)
    print("  AutoDebug - Root Cause Engine demo")
    print("=" * 60)
    print()

    result = find_root_cause(model, sample_bug, sample_codebase)
    print(json.dumps(result, indent=2))
    print()
    print("=" * 60)
    print("  Demo complete")
    print("=" * 60)
