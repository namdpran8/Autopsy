"""
mcp/gitlab_client.py
====================
GitLab MCP integration layer for autodebug-agent.

Provides four atomic operations against a GitLab project:
  • create_issue   – open an issue
  • create_branch  – create a branch from a ref
  • commit_patch   – update a file on a branch
  • create_mr      – open a merge request that closes an issue
"""

from __future__ import annotations

import os
import sys
import logging
from typing import Any

import gitlab
from dotenv import load_dotenv

# ---------------------------------------------------------------------------
# Logging – all errors go to stderr so they never pollute JSON/stdout output
# ---------------------------------------------------------------------------
logger = logging.getLogger("autodebug.gitlab")
logger.setLevel(logging.DEBUG)

_stderr_handler = logging.StreamHandler(sys.stderr)
_stderr_handler.setLevel(logging.DEBUG)
_stderr_handler.setFormatter(
    logging.Formatter(
        "[%(asctime)s] %(levelname)s %(name)s — %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
)
logger.addHandler(_stderr_handler)

# ---------------------------------------------------------------------------
# Environment & GitLab connection
# ---------------------------------------------------------------------------
load_dotenv()

GITLAB_URL: str = os.getenv("GITLAB_URL", "https://gitlab.com")
GITLAB_TOKEN: str | None = os.getenv("GITLAB_TOKEN")
GITLAB_PROJECT_ID: str | None = os.getenv("GITLAB_PROJECT_ID")


def _get_project() -> gitlab.v4.objects.Project:
    """Return an authenticated :class:`gitlab.v4.objects.Project` handle.

    Raises
    ------
    EnvironmentError
        If required environment variables are missing.
    gitlab.exceptions.GitlabAuthenticationError
        If the token is invalid.
    """
    if not GITLAB_TOKEN:
        raise EnvironmentError(
            "GITLAB_TOKEN is not set. "
            "Export it or add it to your .env file."
        )
    if not GITLAB_PROJECT_ID:
        raise EnvironmentError(
            "GITLAB_PROJECT_ID is not set. "
            "Export it or add it to your .env file."
        )

    gl = gitlab.Gitlab(url=GITLAB_URL, private_token=GITLAB_TOKEN)
    gl.auth()  # validates the token early
    project = gl.projects.get(GITLAB_PROJECT_ID)
    return project


# ===================================================================
# 1. create_issue
# ===================================================================
def create_issue(title: str, description: str) -> dict[str, Any]:
    """Create a GitLab issue in the configured project.

    Parameters
    ----------
    title : str
        Issue title.
    description : str
        Markdown body of the issue.

    Returns
    -------
    dict
        ``{"iid": <int>, "url": <str>}``
    """
    try:
        project = _get_project()
        issue = project.issues.create({"title": title, "description": description})
        logger.info("Created issue #%s — %s", issue.iid, issue.web_url)
        return {"iid": issue.iid, "url": issue.web_url}

    except gitlab.exceptions.GitlabCreateError as exc:
        logger.error("GitLab API refused to create the issue: %s", exc)
        raise
    except gitlab.exceptions.GitlabAuthenticationError as exc:
        logger.error("Authentication failed — check GITLAB_TOKEN: %s", exc)
        raise
    except EnvironmentError as exc:
        logger.error("Configuration error: %s", exc)
        raise
    except Exception as exc:
        logger.error("Unexpected error while creating issue: %s", exc)
        raise


# ===================================================================
# 2. create_branch
# ===================================================================
def create_branch(branch_name: str, ref: str = "main") -> dict[str, Any]:
    """Create a new Git branch from *ref*.

    Parameters
    ----------
    branch_name : str
        Name for the new branch (e.g. ``fix/issue-42``).
    ref : str, optional
        Source ref (branch, tag, or SHA). Defaults to ``"main"``.

    Returns
    -------
    dict
        ``{"name": <str>}``
    """
    try:
        project = _get_project()
        branch = project.branches.create({"branch": branch_name, "ref": ref})
        logger.info("Created branch '%s' from '%s'", branch.name, ref)
        return {"name": branch.name}

    except gitlab.exceptions.GitlabCreateError as exc:
        logger.error(
            "GitLab API refused to create branch '%s' from '%s': %s",
            branch_name,
            ref,
            exc,
        )
        raise
    except gitlab.exceptions.GitlabAuthenticationError as exc:
        logger.error("Authentication failed — check GITLAB_TOKEN: %s", exc)
        raise
    except EnvironmentError as exc:
        logger.error("Configuration error: %s", exc)
        raise
    except Exception as exc:
        logger.error(
            "Unexpected error while creating branch '%s': %s", branch_name, exc
        )
        raise


# ===================================================================
# 3. commit_patch
# ===================================================================
def commit_patch(
    branch: str,
    file_path: str,
    new_content: str,
    commit_message: str,
) -> dict[str, Any]:
    """Update an existing file on *branch* via single-file commit.

    Parameters
    ----------
    branch : str
        Target branch that already exists.
    file_path : str
        Repository-relative path to the file (e.g. ``src/utils.py``).
    new_content : str
        Complete new content of the file.
    commit_message : str
        Commit message.

    Returns
    -------
    dict
        ``{"committed": True, "branch": <str>}``
    """
    try:
        project = _get_project()

        commit_data = {
            "branch": branch,
            "commit_message": commit_message,
            "actions": [
                {
                    "action": "update",
                    "file_path": file_path,
                    "content": new_content,
                }
            ],
        }

        project.commits.create(commit_data)
        logger.info(
            "Committed patch to '%s' on branch '%s': %s",
            file_path,
            branch,
            commit_message,
        )
        return {"committed": True, "branch": branch}

    except gitlab.exceptions.GitlabCreateError as exc:
        logger.error(
            "GitLab API refused the commit on branch '%s' for '%s': %s",
            branch,
            file_path,
            exc,
        )
        raise
    except gitlab.exceptions.GitlabAuthenticationError as exc:
        logger.error("Authentication failed — check GITLAB_TOKEN: %s", exc)
        raise
    except EnvironmentError as exc:
        logger.error("Configuration error: %s", exc)
        raise
    except Exception as exc:
        logger.error("Unexpected error during commit_patch: %s", exc)
        raise


# ===================================================================
# 4. create_mr
# ===================================================================
def create_mr(
    source_branch: str,
    issue_iid: int,
    description: str,
) -> dict[str, Any]:
    """Open a merge request targeting ``main`` that references an issue.

    Parameters
    ----------
    source_branch : str
        The branch containing the fix.
    issue_iid : int
        IID of the related issue (used in the MR title and description).
    description : str
        Markdown body for the merge request.

    Returns
    -------
    dict
        ``{"iid": <int>, "url": <str>}``
    """
    try:
        project = _get_project()

        mr_title = f"fix: resolve issue #{issue_iid} — AutoDebug patch"
        full_description = (
            f"Closes #{issue_iid}\n\n"
            f"---\n\n"
            f"{description}"
        )

        mr = project.mergerequests.create(
            {
                "source_branch": source_branch,
                "target_branch": "main",
                "title": mr_title,
                "description": full_description,
                "remove_source_branch": True,
            }
        )
        logger.info("Opened MR !%s — %s", mr.iid, mr.web_url)
        return {"iid": mr.iid, "url": mr.web_url}

    except gitlab.exceptions.GitlabCreateError as exc:
        logger.error(
            "GitLab API refused to create MR from '%s' → main: %s",
            source_branch,
            exc,
        )
        raise
    except gitlab.exceptions.GitlabAuthenticationError as exc:
        logger.error("Authentication failed — check GITLAB_TOKEN: %s", exc)
        raise
    except EnvironmentError as exc:
        logger.error("Configuration error: %s", exc)
        raise
    except Exception as exc:
        logger.error("Unexpected error while creating MR: %s", exc)
        raise


# ===================================================================
# Self-test harness
# ===================================================================
if __name__ == "__main__":
    import json
    import uuid

    # Unique suffix so parallel runs never collide
    run_id = uuid.uuid4().hex[:8]

    print("=" * 60)
    print("  AutoDebug — GitLab integration smoke test")
    print("=" * 60)
    print()

    # ------------------------------------------------------------------
    # 1. Create a test issue
    # ------------------------------------------------------------------
    print("[1/4] Creating issue …")
    issue = create_issue(
        title="[TEST] AutoDebug connection check",
        description=(
            f"Automated smoke-test issue created by `gitlab_client.py`.\n\n"
            f"Run ID: `{run_id}`\n\n"
            f"This issue can be safely closed."
        ),
    )
    print(f"  ✓ Issue #{issue['iid']}  →  {issue['url']}")
    print()

    # ------------------------------------------------------------------
    # 2. Create a feature branch
    # ------------------------------------------------------------------
    branch_name = f"test/autodebug-{run_id}"
    print(f"[2/4] Creating branch '{branch_name}' …")
    branch = create_branch(branch_name=branch_name, ref="main")
    print(f"  ✓ Branch '{branch['name']}' created")
    print()

    # ------------------------------------------------------------------
    # 3. Commit a patch to the branch
    # ------------------------------------------------------------------
    print("[3/4] Committing test patch …")
    patch_result = commit_patch(
        branch=branch_name,
        file_path="README.md",
        new_content=(
            "# AutoDebug Agent\n\n"
            f"Smoke-test commit — run `{run_id}`.\n"
        ),
        commit_message=f"test: autodebug smoke-test commit ({run_id})",
    )
    print(f"  ✓ Committed: {patch_result['committed']}  on  {patch_result['branch']}")
    print()

    # ------------------------------------------------------------------
    # 4. Open a merge request
    # ------------------------------------------------------------------
    print("[4/4] Opening merge request …")
    mr = create_mr(
        source_branch=branch_name,
        issue_iid=issue["iid"],
        description=(
            "Automated MR opened by the AutoDebug smoke-test harness.\n\n"
            f"Run ID: `{run_id}`"
        ),
    )
    print(f"  ✓ MR !{mr['iid']}  →  {mr['url']}")
    print()

    # ------------------------------------------------------------------
    # Summary
    # ------------------------------------------------------------------
    print("=" * 60)
    print("  All 4 operations succeeded ✓")
    print("=" * 60)
    print()
    print(json.dumps({"issue": issue, "branch": branch, "patch": patch_result, "mr": mr}, indent=2))
