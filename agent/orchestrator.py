"""
agent/orchestrator.py
=====================
Central pipeline orchestrator for autodebug-agent.

Coordinates the full debug-and-fix workflow:

    log text
      -> bug detection
      -> root-cause analysis (Gemini)
      -> patch generation (Gemini)
      -> GitLab issue + branch + commit + merge request

Each step is individually wrapped so a failure in one MCP call does not
prevent earlier results from being returned.
"""

from __future__ import annotations

import logging
import os
import sys
from typing import Any

import google.generativeai as genai
from dotenv import load_dotenv

from agent.bug_detector import detect_bug
from agent.root_cause_engine import find_root_cause, load_codebase
from agent.patch_generator import generate_patch
from mcp.gitlab_client import (
    create_issue,
    create_branch,
    commit_patch,
    create_mr,
)

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
logger = logging.getLogger("autodebug.orchestrator")
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
# Gemini model initialisation
# ---------------------------------------------------------------------------
load_dotenv()

_GEMINI_API_KEY: str | None = os.getenv("GEMINI_API_KEY")


def _init_model() -> genai.GenerativeModel:
    """Configure the Gemini SDK and return a ``GenerativeModel`` instance.

    Raises
    ------
    EnvironmentError
        If ``GEMINI_API_KEY`` is not set.
    """
    if not _GEMINI_API_KEY:
        raise EnvironmentError(
            "GEMINI_API_KEY is not set. "
            "Export it or add it to your .env file."
        )
    genai.configure(api_key=_GEMINI_API_KEY)
    return genai.GenerativeModel("gemini-2.0-flash")


# ---------------------------------------------------------------------------
# Pipeline helpers
# ---------------------------------------------------------------------------

def _step_failed(
    step_name: str,
    exc: Exception,
    result: dict[str, Any],
) -> dict[str, Any]:
    """Log a step failure and mark the pipeline as partial.

    Returns the *result* dict (mutated in-place) so callers can
    ``return _step_failed(...)`` in one line.
    """
    logger.error("Step '%s' failed: %s", step_name, exc)
    result["status"] = "partial"
    result.setdefault("errors", []).append(
        {"step": step_name, "error": str(exc)}
    )
    return result


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def run_pipeline(log_input: str, code_directory: str) -> dict[str, Any]:
    """Execute the full autodebug pipeline.

    Parameters
    ----------
    log_input : str
        Raw log / stderr text that may contain error traces.
    code_directory : str
        Path to the project source tree (all ``.py`` files will be read).

    Returns
    -------
    dict
        Final status dict.  ``status`` is one of:

        * ``"no_bug_found"``  - nothing to fix
        * ``"fix_submitted"`` - full pipeline succeeded
        * ``"partial"``       - some steps failed; partial results included
    """
    result: dict[str, Any] = {"status": "in_progress"}

    # ==================================================================
    # Step 1 — Bug detection
    # ==================================================================
    try:
        logger.info("Step 1/9: Detecting bugs in log input ...")
        bug = detect_bug(log_input)

        if not bug.get("detected"):
            logger.info("No bug detected. Pipeline complete.")
            return {"status": "no_bug_found"}

        result["bug"] = bug
        logger.info(
            "Bug detected: %s (%s)",
            bug.get("error_type"),
            bug.get("severity"),
        )
    except Exception as exc:
        return _step_failed("detect_bug", exc, result)

    # ==================================================================
    # Step 2 — Load codebase
    # ==================================================================
    try:
        logger.info("Step 2/9: Loading codebase from '%s' ...", code_directory)
        codebase = load_codebase(code_directory)

        if not codebase:
            logger.warning("Codebase is empty; analysis may be limited.")

        result["codebase_files"] = list(codebase.keys())
        logger.info("Loaded %d source files.", len(codebase))
    except Exception as exc:
        return _step_failed("load_codebase", exc, result)

    # ==================================================================
    # Step 3 — Create GitLab issue
    # ==================================================================
    issue: dict[str, Any] | None = None
    try:
        logger.info("Step 3/9: Creating GitLab issue ...")
        issue_title = f"[AutoDebug] {bug.get('error_type', 'Unknown Error')}"
        issue_body = (
            f"**Error type:** `{bug.get('error_type')}`\n"
            f"**File:** `{bug.get('file', 'unknown')}`\n"
            f"**Severity:** {bug.get('severity', 'unknown')}\n\n"
            f"### Stack Trace\n"
            f"```\n{bug.get('stack_trace', '(no traceback)')}\n```\n\n"
            f"---\n"
            f"*Created automatically by autodebug-agent.*"
        )

        issue = create_issue(title=issue_title, description=issue_body)
        result["issue"] = issue
        logger.info("Issue created: #%s  %s", issue["iid"], issue["url"])
    except Exception as exc:
        _step_failed("create_issue", exc, result)
        # Continue — we can still analyse and generate a patch locally

    # ==================================================================
    # Step 4 — Initialise Gemini model
    # ==================================================================
    try:
        model = _init_model()
    except Exception as exc:
        return _step_failed("init_gemini_model", exc, result)

    # ==================================================================
    # Step 5 — Root cause analysis
    # ==================================================================
    root_cause: dict[str, Any] | None = None
    try:
        logger.info("Step 4/9: Running root-cause analysis ...")
        root_cause = find_root_cause(model, bug, codebase)
        result["root_cause"] = root_cause
        logger.info(
            "Root cause: line %s (confidence %.2f)",
            root_cause.get("line"),
            root_cause.get("confidence", 0.0),
        )
    except Exception as exc:
        return _step_failed("find_root_cause", exc, result)

    # ==================================================================
    # Step 6 — Patch generation
    # ==================================================================
    patch: dict[str, Any] | None = None
    try:
        logger.info("Step 5/9: Generating patch ...")
        # Inject file ref so patch_generator can resolve the file
        if "file" not in root_cause and "file" in bug:
            root_cause["file"] = bug["file"]

        patch = generate_patch(model, root_cause, codebase)
        result["patch_summary"] = patch.get("diff_summary", "")
        logger.info("Patch generated: %s", patch.get("diff_summary"))

        if not patch.get("patched"):
            logger.warning("Patch content is empty — cannot commit.")
            result["status"] = "partial"
            return result
    except Exception as exc:
        return _step_failed("generate_patch", exc, result)

    # ------------------------------------------------------------------
    # From here on we need a valid issue to create branches/MRs.
    # If issue creation failed earlier, we return partial.
    # ------------------------------------------------------------------
    if issue is None:
        logger.warning(
            "Skipping GitLab branch/commit/MR — issue creation failed earlier."
        )
        result["status"] = "partial"
        return result

    # ==================================================================
    # Step 7 — Create branch
    # ==================================================================
    branch_name = f"autodebug/fix-issue-{issue['iid']}"
    try:
        logger.info("Step 6/9: Creating branch '%s' ...", branch_name)
        create_branch(branch_name=branch_name)
        result["branch"] = branch_name
        logger.info("Branch created: %s", branch_name)
    except Exception as exc:
        return _step_failed("create_branch", exc, result)

    # ==================================================================
    # Step 8 — Commit patch
    # ==================================================================
    try:
        logger.info("Step 7/9: Committing patch to '%s' ...", branch_name)
        commit_result = commit_patch(
            branch=branch_name,
            file_path=patch["file"],
            new_content=patch["patched"],
            commit_message=patch.get("diff_summary", "fix: autodebug patch"),
        )
        logger.info("Commit result: %s", commit_result)
    except Exception as exc:
        return _step_failed("commit_patch", exc, result)

    # ==================================================================
    # Step 9 — Create merge request
    # ==================================================================
    try:
        logger.info("Step 8/9: Opening merge request ...")
        mr = create_mr(
            source_branch=branch_name,
            issue_iid=issue["iid"],
            description=root_cause.get("explanation", "Automated fix."),
        )
        result["mr"] = mr
        logger.info("MR opened: !%s  %s", mr["iid"], mr["url"])
    except Exception as exc:
        return _step_failed("create_mr", exc, result)

    # ==================================================================
    # Done
    # ==================================================================
    result["status"] = "fix_submitted"
    logger.info("Pipeline complete: fix_submitted")

    return result


# ===================================================================
# CLI entry point
# ===================================================================
if __name__ == "__main__":
    import json
    import textwrap

    # If a log file path is provided, read it; otherwise use a built-in sample
    if len(sys.argv) >= 3:
        log_path = sys.argv[1]
        code_dir = sys.argv[2]

        with open(log_path, encoding="utf-8") as f:
            log_text = f.read()
    elif len(sys.argv) == 2:
        code_dir = sys.argv[1]
        log_text = textwrap.dedent("""\
            2026-06-08 14:22:01 INFO  Starting data pipeline ...
            Traceback (most recent call last):
              File "demo/processor.py", line 14, in process
                value = items[idx]
            IndexError: list index out of range
        """)
    else:
        print(
            "Usage: python -m agent.orchestrator [LOG_FILE] CODE_DIRECTORY",
            file=sys.stderr,
        )
        print(
            "       python -m agent.orchestrator CODE_DIRECTORY   (uses sample log)",
            file=sys.stderr,
        )
        sys.exit(1)

    print("=" * 60)
    print("  AutoDebug - Pipeline Orchestrator")
    print("=" * 60)
    print()

    final = run_pipeline(log_text, code_dir)
    print()
    print(json.dumps(final, indent=2, default=str))
