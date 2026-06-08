"""
api/main.py
===========
FastAPI application layer for autodebug-agent.

Exposes the debug pipeline over HTTP so it can be triggered by
webhooks, CI runners, or a hackathon demo UI.
"""

from __future__ import annotations

import logging
import os
import sys
import textwrap
from contextlib import asynccontextmanager
from typing import Any

import uvicorn
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

from agent.orchestrator import run_pipeline

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
logger = logging.getLogger("autodebug.api")
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
# Environment
# ---------------------------------------------------------------------------
load_dotenv()


# ---------------------------------------------------------------------------
# Lifespan (replaces deprecated @app.on_event)
# ---------------------------------------------------------------------------
@asynccontextmanager
async def _lifespan(app: FastAPI):
    """Startup / shutdown lifecycle hook."""
    # ---- Startup --------------------------------------------------------
    print()
    print("=" * 52)
    print("   AutoDebug Agent is running")
    print("=" * 52)
    print()

    # Validate env vars — warn but don't crash so the health endpoint
    # and demo endpoint remain reachable even without credentials.
    gemini_key = os.getenv("GEMINI_API_KEY")
    gitlab_token = os.getenv("GITLAB_TOKEN")

    if not gemini_key:
        logger.warning(
            "GEMINI_API_KEY is not set. "
            "Root-cause analysis and patch generation will fail."
        )
    else:
        logger.info("GEMINI_API_KEY is configured.")

    if not gitlab_token:
        logger.warning(
            "GITLAB_TOKEN is not set. "
            "GitLab issue/branch/MR creation will fail."
        )
    else:
        logger.info("GITLAB_TOKEN is configured.")

    yield  # ---- application runs here ----

    # ---- Shutdown -------------------------------------------------------
    logger.info("AutoDebug Agent shutting down.")


# ---------------------------------------------------------------------------
# FastAPI application
# ---------------------------------------------------------------------------
app = FastAPI(
    title="AutoDebug Agent",
    version="1.0.0",
    description=(
        "AI-powered debugging agent that analyses CI/CD failures, "
        "identifies root causes via Google Gemini, and opens fix MRs on GitLab."
    ),
    lifespan=_lifespan,
)

# CORS — wide open for hackathon / demo purposes
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ---------------------------------------------------------------------------
# Request / response models
# ---------------------------------------------------------------------------
class DebugRequest(BaseModel):
    """Payload for the ``POST /debug`` endpoint."""

    logs: str = Field(
        ...,
        min_length=1,
        description="Raw log / stderr text that may contain error traces.",
        json_schema_extra={
            "example": (
                "Traceback (most recent call last):\n"
                '  File "app.py", line 42, in handler\n'
                "    result = items[idx]\n"
                "IndexError: list index out of range"
            )
        },
    )
    code_directory: str = Field(
        ...,
        min_length=1,
        description="Path to the project source tree on the server.",
        json_schema_extra={"example": "/srv/myproject"},
    )


class HealthResponse(BaseModel):
    status: str
    service: str


class ErrorResponse(BaseModel):
    status: str = "error"
    message: str


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

@app.get(
    "/health",
    response_model=HealthResponse,
    summary="Health check",
    tags=["ops"],
)
async def health() -> HealthResponse:
    """Returns a simple liveness probe."""
    return HealthResponse(status="ok", service="autodebug-agent")


@app.post(
    "/debug",
    summary="Run the full debug pipeline",
    tags=["debug"],
    responses={
        200: {
            "description": "Pipeline result (fix_submitted, partial, or no_bug_found)",
        },
        500: {
            "description": "Internal error",
            "model": ErrorResponse,
        },
    },
)
async def debug(request: DebugRequest) -> dict[str, Any]:
    """Accept raw logs + a code directory and run the full autodebug pipeline.

    Returns the orchestrator result dict which contains the issue, MR,
    root-cause analysis, and patch summary.
    """
    try:
        logger.info(
            "POST /debug — log length=%d, code_directory=%s",
            len(request.logs),
            request.code_directory,
        )
        result = run_pipeline(
            log_input=request.logs,
            code_directory=request.code_directory,
        )
        logger.info("Pipeline finished with status: %s", result.get("status"))
        return result

    except Exception as exc:
        logger.exception("Unhandled error in /debug endpoint")
        raise HTTPException(
            status_code=500,
            detail={"status": "error", "message": str(exc)},
        ) from exc


@app.get(
    "/demo",
    summary="Demo payload for hackathon judges",
    tags=["debug"],
)
async def demo() -> dict[str, Any]:
    """Return a hardcoded demo payload so judges can test without GitLab.

    The response contains a sample log with an ``IndexError`` and a
    code directory path — paste it straight into ``POST /debug``.
    """
    sample_log = textwrap.dedent("""\
        2026-06-08 14:22:01 INFO  Starting data pipeline ...
        2026-06-08 14:22:02 INFO  Loading dataset (1024 rows)
        2026-06-08 14:22:03 INFO  Processing batch 7/10
        Traceback (most recent call last):
          File "demo/buggy_service/processor.py", line 34, in process_batch
            record = batch[index]
          File "demo/buggy_service/utils.py", line 12, in __getitem__
            return self._rows[i]
        IndexError: list index out of range
        2026-06-08 14:22:03 ERROR Pipeline failed — aborting run.
    """)

    return {
        "instructions": (
            "Copy the 'logs' and 'code_directory' values below "
            "into a POST /debug request to trigger the full pipeline."
        ),
        "request_body": {
            "logs": sample_log,
            "code_directory": "demo/buggy_service",
        },
        "curl_example": (
            'curl -X POST http://localhost:8080/debug '
            '-H "Content-Type: application/json" '
            "-d '"
            '{"logs": "<paste logs>", "code_directory": "demo/buggy_service"}'
            "'"
        ),
    }


# ---------------------------------------------------------------------------
# Uvicorn runner
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8080)
