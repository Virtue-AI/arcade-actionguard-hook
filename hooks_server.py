import json
import logging
import os
from contextlib import asynccontextmanager

import httpx
from dotenv import load_dotenv
from fastapi import FastAPI, Header, Request

load_dotenv()

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("hooks_server")

# ─── Config ──────────────────────────────────────────────────────────────────

ACTION_GUARD_URL       = os.environ["ACTION_GUARD_URL"]
ACTION_GUARD_POLICY_ID = os.environ["ACTION_GUARD_POLICY_ID"]
FAST_MODE            = os.environ.get("ACTION_GUARD_FAST_MODE", "false").lower() == "true"

# ─── HTTP client ─────────────────────────────────────────────────────────────

_http: httpx.AsyncClient | None = None

@asynccontextmanager
async def lifespan(app: FastAPI):
    global _http
    _http = httpx.AsyncClient(timeout=120.0)
    log.info("Hooks server ready – Action Guard at %s", ACTION_GUARD_URL)
    yield
    await _http.aclose()

app = FastAPI(title="Arcade Contextual Access – Action Guard", lifespan=lifespan)

# ─── Session history builder ──────────────────────────────────────────────────

def build_session_history(
    toolkit: str,
    tool: str,
    inputs: dict,
    context: dict,
    *,
    output: object = None,
    success: bool | None = None,
) -> dict:
    """
    Package the current Arcade tool call into the session_history format
    expected by POST /api/v1/guard_actions.

    For /pre hooks: only the agent action step is included.
    For /post hooks: the agent action step + tool result step are included.
    Any Arcade context metadata is surfaced as a preceding user step.
    """
    tool_full  = f"{toolkit}_{tool}" if toolkit else tool
    params_str = ", ".join(f'{k}={json.dumps(v)}' for k, v in inputs.items())
    action_str = f"{tool_full}({params_str})"

    trajectory = []
    step = 0

    # Include Arcade metadata as user-turn context if present
    arcade_meta = context.get("metadata", {})
    if arcade_meta:
        trajectory.append({
            "role": "user",
            "state": json.dumps(arcade_meta),
            "step_id": step,
        })
        step += 1

    # The tool call itself
    trajectory.append({
        "role": "agent",
        "action": action_str,
        "step_id": step,
        "metadata": {
            "tool_name": tool_full,
            "tool_params": inputs,
            "server_name": "Arcade",
            "server_id": "arcade-mcp",
        },
    })
    step += 1

    # Tool result (only for /post hook)
    if success is not None:
        result_str = json.dumps(output, default=str) if output is not None else ""
        trajectory.append({
            "role": "tool",
            "state": result_str,
            "step_id": step,
            "metadata": {
                "tool_name": tool_full,
                "success": success,
            },
        })
        step += 1

    actions_count = 1
    tool_count = 1 if success is not None else 0

    return {
        "session_info": {
            "gateway_id": "arcade",
            "metadata": {
                "step_count": step,
                "actions_count": actions_count,
                "tool_count": tool_count,
                "user_turn": 1 if arcade_meta else 0,
            },
        },
        "trajectory": trajectory,
    }

# ─── Endpoints ───────────────────────────────────────────────────────────────

@app.get("/health")
async def health():
    return {"status": "ok"}


@app.post("/pre")
async def pre_execution_hook(request: Request, authorization: str | None = Header(default=None)):
    """
    Called by Arcade before every tool execution.
    """
    # Use the bearer token Arcade sends (VIRTUE_AGENT_JWT) to authenticate with Action Guard
    auth_token = ""
    if authorization:
        _, _, auth_token = authorization.partition(" ")

    body = await request.json()

    execution_id = body.get("execution_id", "unknown")
    tool_info    = body.get("tool", {})
    toolkit      = tool_info.get("toolkit", "")
    tool         = tool_info.get("name", "")
    inputs       = body.get("inputs", {})
    context      = body.get("context", {})
    user_id      = context.get("user_id", "unknown")

    log.info("[pre] execution_id=%s user=%s tool=%s_%s", execution_id, user_id, toolkit, tool)

    session_history = build_session_history(toolkit, tool, inputs, context)

    payload = {
        "session_history": session_history,
        "session_id":      execution_id,   # execution_id as session scope (one eval per tool call)
        "policy_id":       ACTION_GUARD_POLICY_ID,
        "fast_mode":       FAST_MODE,
    }

    try:
        resp = await _http.post(
            f"{ACTION_GUARD_URL}/api/v1/guard_actions",
            headers={"Authorization": f"Bearer {auth_token}"},
            json=payload,
        )
        resp.raise_for_status()
        result = resp.json()

    except httpx.RequestError as exc:
        log.error("[pre] Action Guard unreachable – failing open: %s", exc)
        return {"code": "OK"}   # fail open if guard is down

    except httpx.HTTPStatusError as exc:
        log.error("[pre] Action Guard HTTP %s – failing closed", exc.response.status_code)
        return {"code": "CHECK_FAILED", "error_message": f"Action Guard returned HTTP {exc.response.status_code}"}

    allowed     = result.get("allowed", True)
    explanation = result.get("explanation", "")
    violations  = result.get("violations", [])

    if not allowed:
        reason = explanation or "; ".join(violations[:3]) or "policy violation"
        log.warning("[pre] BLOCKED tool=%s_%s reason=%s", toolkit, tool, reason[:200])
        return {"code": "CHECK_FAILED", "error_message": f"Action blocked by policy: {reason}"}

    log.info("[pre] ALLOWED tool=%s_%s", toolkit, tool)
    return {"code": "OK"}


@app.post("/post")
async def post_execution_hook(request: Request, authorization: str | None = Header(default=None)):
    """
    Called by Arcade after every tool execution.
    """
    auth_token = ""
    if authorization:
        _, _, auth_token = authorization.partition(" ")

    body = await request.json()

    execution_id = body.get("execution_id", "unknown")
    tool_info    = body.get("tool", {})
    toolkit      = tool_info.get("toolkit", "")
    tool         = tool_info.get("name", "")
    inputs       = body.get("inputs", {})
    context      = body.get("context", {})
    user_id      = context.get("user_id", "unknown")
    success      = body.get("success", True)
    output       = body.get("output")
    exec_error   = body.get("execution_error", "")

    log.info("[post] execution_id=%s user=%s tool=%s_%s success=%s",
             execution_id, user_id, toolkit, tool, success)

    if exec_error:
        log.warning("[post] execution_error=%s", exec_error[:200])

    session_history = build_session_history(
        toolkit, tool, inputs, context,
        output=output, success=success,
    )

    payload = {
        "session_history": session_history,
        "session_id":      execution_id,
        "policy_id":       ACTION_GUARD_POLICY_ID,
        "fast_mode":       FAST_MODE,
    }

    try:
        resp = await _http.post(
            f"{ACTION_GUARD_URL}/api/v1/guard_actions",
            headers={"Authorization": f"Bearer {auth_token}"},
            json=payload,
        )
        resp.raise_for_status()
        result = resp.json()

    except httpx.RequestError as exc:
        log.error("[post] Action Guard unreachable – failing open: %s", exc)
        return {"code": "OK"}

    except httpx.HTTPStatusError as exc:
        log.error("[post] Action Guard HTTP %s – failing closed", exc.response.status_code)
        return {"code": "CHECK_FAILED", "error_message": f"Action Guard returned HTTP {exc.response.status_code}"}

    allowed     = result.get("allowed", True)
    explanation = result.get("explanation", "")
    violations  = result.get("violations", [])

    if not allowed:
        reason = explanation or "; ".join(violations[:3]) or "policy violation"
        log.warning("[post] BLOCKED tool=%s_%s reason=%s", toolkit, tool, reason[:200])
        return {"code": "CHECK_FAILED", "error_message": f"Output blocked by policy: {reason}"}

    log.info("[post] ALLOWED tool=%s_%s", toolkit, tool)
    return {"code": "OK"}
