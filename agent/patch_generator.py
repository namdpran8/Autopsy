"""
agent/patch_generator.py
========================
Patch generator sub-agent for autodebug-agent.

Takes a root-cause analysis dict and the project codebase, asks Gemini
to produce a corrected version of the faulty file, and returns the
original + patched content alongside a commit-ready diff summary.
"""

from __future__ import annotations

import logging
import re
import sys
from pathlib import Path
from typing import Any

import google.generativeai as genai

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
logger = logging.getLogger("autodebug.patch_generator")
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
# Prompt templates
# ---------------------------------------------------------------------------
_PATCH_SYSTEM_PROMPT = """\
You are an expert Python developer tasked with fixing a bug.

You will receive:
1. A description of the bug and why it occurs.
2. The line number where the bug is located.
3. The complete source code of the file containing the bug.

Your task:
- Fix the bug described.
- Return the COMPLETE corrected file. Every single line must be included,
  not just the changed parts.
- Do NOT add markdown code fences, backticks, or any commentary.
- Do NOT add explanations before or after the code.
- Output ONLY the raw Python source code, nothing else.
"""

_DIFF_SUMMARY_PROMPT = """\
You are a senior engineer writing a Git commit message.

Below are two versions of a Python file: the ORIGINAL and the PATCHED version.
Write a single-sentence commit message in imperative tense (e.g. "Fix ...",
"Add ...", "Handle ...") that summarises what changed.

Rules:
- Maximum 72 characters.
- No period at the end.
- No markdown, no quotes, just the raw sentence.

## ORIGINAL
```
{original}
```

## PATCHED
```
{patched}
```
"""


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
_RE_CODE_FENCE = re.compile(
    r"```(?:python|py)?\s*\n(.*?)\n\s*```",
    re.DOTALL,
)

_RE_LEADING_FENCE = re.compile(r"^```(?:python|py)?\s*\n", re.MULTILINE)
_RE_TRAILING_FENCE = re.compile(r"\n\s*```\s*$", re.MULTILINE)


def _strip_code_fences(text: str) -> str:
    """Remove markdown code fences wrapping Python code, if present.

    Handles three cases:
      1. Full ``` ... ``` wrapping  (extract inner content)
      2. Leading ``` only           (strip it)
      3. Trailing ``` only          (strip it)
    """
    # Case 1: fully fenced block
    match = _RE_CODE_FENCE.search(text)
    if match:
        return match.group(1)

    # Case 2 / 3: partial fences
    result = _RE_LEADING_FENCE.sub("", text)
    result = _RE_TRAILING_FENCE.sub("", result)
    return result.strip()


def _resolve_file(
    root_cause: dict[str, Any],
    codebase: dict[str, str],
) -> tuple[str, str]:
    """Find the source file referenced by the root-cause analysis.

    Tries exact match first, then suffix / basename fallbacks.

    Returns
    -------
    tuple[str, str]
        ``(filename, file_content)`` or ``("", "")`` on failure.
    """
    # root_cause may carry "faulty_code_snippet" but the file reference
    # is usually inherited from the upstream bug dict.  We fall back to
    # scanning for the faulty snippet inside every file.

    # Strategy 1: look for a "file" key in root_cause (not standard, but
    # the orchestrator may inject it).
    raw_ref: str = root_cause.get("file", "")

    if raw_ref:
        # Strip trailing :line_no
        file_path = re.sub(r":\d+$", "", raw_ref).replace("\\", "/")
        for key, content in codebase.items():
            key_norm = key.replace("\\", "/")
            if key_norm == file_path:
                return key, content
        # Suffix match
        for key, content in codebase.items():
            key_norm = key.replace("\\", "/")
            if file_path.endswith(key_norm) or key_norm.endswith(file_path):
                return key, content
        # Basename
        basename = Path(file_path).name
        for key, content in codebase.items():
            if Path(key).name == basename:
                return key, content

    # Strategy 2: search for faulty_code_snippet inside codebase files
    snippet = root_cause.get("faulty_code_snippet", "").strip()
    if snippet:
        for key, content in codebase.items():
            if snippet in content:
                return key, content

    # Strategy 3: if only one file, assume that's the one
    if len(codebase) == 1:
        key = next(iter(codebase))
        return key, codebase[key]

    logger.warning("Could not resolve faulty file from root_cause dict.")
    return "", ""


def _build_patch_prompt(
    root_cause: dict[str, Any],
    filename: str,
    source_code: str,
) -> str:
    """Assemble the user prompt for the patching call."""
    lines = [
        "## Bug Report",
        "",
        f"**File:** `{filename}`",
        f"**Faulty line:** {root_cause.get('line', 'unknown')}",
        f"**Faulty code:** `{root_cause.get('faulty_code_snippet', 'N/A')}`",
        "",
        "### Why it fails",
        root_cause.get("reason", "(no reason provided)"),
        "",
        "### Plain-English explanation",
        root_cause.get("explanation", "(no explanation provided)"),
        "",
        "## Complete Original File",
        "",
    ]

    # Include numbered lines so Gemini can cross-reference with the bug line
    for i, line in enumerate(source_code.splitlines(), start=1):
        lines.append(f"{i:>4} | {line}")

    lines.append("")
    lines.append(
        "Return the COMPLETE corrected file below (raw Python, no fences):"
    )
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def generate_patch(
    model: genai.GenerativeModel,
    root_cause: dict[str, Any],
    codebase: dict[str, str],
) -> dict[str, Any]:
    """Ask Gemini to produce a patched version of the faulty file.

    Parameters
    ----------
    model : google.generativeai.GenerativeModel
        Initialised Gemini model.
    root_cause : dict
        Output of :func:`agent.root_cause_engine.find_root_cause`.
    codebase : dict
        ``{"relative/path.py": "file content", ...}``

    Returns
    -------
    dict
        ``{"file", "original", "patched", "diff_summary"}``
    """
    # --- Resolve the file to patch ---------------------------------------
    filename, original = _resolve_file(root_cause, codebase)
    if not filename:
        msg = "Cannot generate patch: faulty file could not be identified."
        logger.error(msg)
        return {
            "file": "",
            "original": "",
            "patched": "",
            "diff_summary": msg,
        }

    user_prompt = _build_patch_prompt(root_cause, filename, original)

    # --- Call Gemini for the patched file --------------------------------
    try:
        logger.info(
            "Requesting patch from Gemini for '%s' (line %s) ...",
            filename,
            root_cause.get("line", "?"),
        )
        response = model.generate_content(
            [
                {
                    "role": "user",
                    "parts": [_PATCH_SYSTEM_PROMPT + "\n\n" + user_prompt],
                },
            ],
            generation_config=genai.types.GenerationConfig(
                temperature=0.1,
                max_output_tokens=4096,
            ),
        )
    except Exception as exc:
        logger.error("Gemini API call failed during patch generation: %s", exc)
        return {
            "file": filename,
            "original": original,
            "patched": "",
            "diff_summary": f"Gemini API error: {exc}",
        }

    # --- Extract patched content -----------------------------------------
    try:
        raw_patched = response.text
    except (AttributeError, ValueError) as exc:
        logger.error("Could not read patched response: %s", exc)
        return {
            "file": filename,
            "original": original,
            "patched": "",
            "diff_summary": f"Empty or blocked Gemini response: {exc}",
        }

    patched = _strip_code_fences(raw_patched)

    # Sanity check: patched content should not be empty
    if not patched.strip():
        logger.warning("Gemini returned empty patched content.")
        return {
            "file": filename,
            "original": original,
            "patched": "",
            "diff_summary": "Gemini returned an empty patch.",
        }

    # --- Generate diff summary -------------------------------------------
    try:
        diff_summary = generate_diff_summary(model, original, patched)
    except Exception as exc:
        logger.warning("Diff summary generation failed: %s", exc)
        diff_summary = f"Fix bug at line {root_cause.get('line', '?')} in {filename}"

    logger.info("Patch generated for '%s': %s", filename, diff_summary)

    return {
        "file": filename,
        "original": original,
        "patched": patched,
        "diff_summary": diff_summary,
    }


def generate_diff_summary(
    model: genai.GenerativeModel,
    original: str,
    patched: str,
) -> str:
    """Ask Gemini for a one-sentence commit message describing the diff.

    Parameters
    ----------
    model : google.generativeai.GenerativeModel
        Initialised Gemini model.
    original : str
        Original file content.
    patched : str
        Patched file content.

    Returns
    -------
    str
        Imperative-tense commit message, max 72 characters.
    """
    prompt = _DIFF_SUMMARY_PROMPT.format(original=original, patched=patched)

    try:
        response = model.generate_content(
            [{"role": "user", "parts": [prompt]}],
            generation_config=genai.types.GenerationConfig(
                temperature=0.0,
                max_output_tokens=100,
            ),
        )
    except Exception as exc:
        logger.error("Gemini API call failed during diff summary: %s", exc)
        return "Fix identified bug"

    try:
        raw = response.text.strip()
    except (AttributeError, ValueError) as exc:
        logger.error("Could not read diff-summary response: %s", exc)
        return "Fix identified bug"

    # Clean up: remove surrounding quotes or trailing period
    raw = raw.strip('"\'')
    raw = raw.rstrip(".")

    # Enforce 72-char limit
    if len(raw) > 72:
        raw = raw[:69] + "..."

    return raw


# ===================================================================
# Self-test / demo harness
# ===================================================================
if __name__ == "__main__":
    import json
    import os
    import textwrap

    from dotenv import load_dotenv

    load_dotenv()

    api_key = os.getenv("GEMINI_API_KEY")
    if not api_key:
        print(
            "ERROR: Set GEMINI_API_KEY in .env to run this demo.",
            file=sys.stderr,
        )
        sys.exit(1)

    genai.configure(api_key=api_key)
    model = genai.GenerativeModel("gemini-2.0-flash")

    # Simulated root_cause from root_cause_engine
    sample_root_cause: dict[str, Any] = {
        "line": 14,
        "reason": (
            "items has length 3 (indices 0-2) but process() is called "
            "with idx=5, which exceeds the valid range."
        ),
        "explanation": (
            "The process function indexes into a list without checking "
            "whether the given index is within bounds. When called with "
            "an index larger than the list length, it raises an IndexError."
        ),
        "confidence": 0.95,
        "faulty_code_snippet": "value = items[idx]",
        "file": "demo/processor.py",
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
    print("  AutoDebug - Patch Generator demo")
    print("=" * 60)
    print()

    result = generate_patch(model, sample_root_cause, sample_codebase)

    # Print metadata (without dumping full file contents to console)
    meta = {
        "file": result["file"],
        "diff_summary": result["diff_summary"],
        "original_lines": len(result["original"].splitlines()),
        "patched_lines": len(result["patched"].splitlines()),
        "has_patch": bool(result["patched"]),
    }
    print(json.dumps(meta, indent=2))
    print()

    if result["patched"]:
        print("-" * 60)
        print("  PATCHED FILE CONTENT")
        print("-" * 60)
        for i, line in enumerate(result["patched"].splitlines(), 1):
            print(f"{i:>4} | {line}")
        print("-" * 60)

    print()
    print("=" * 60)
    print("  Demo complete")
    print("=" * 60)
