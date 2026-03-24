import json
import logging
import os
from contextlib import asynccontextmanager
from datetime import datetime, timezone

import httpx
from dotenv import load_dotenv
from fastapi import FastAPI, Header, Request

load_dotenv()

logging.basicConfig(level=logging.DEBUG, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("hooks_server")

# ─── Config ──────────────────────────────────────────────────────────────────

ACTION_GUARD_URL       = os.environ["ACTION_GUARD_URL"]
ACTION_GUARD_POLICY_ID = os.environ["ACTION_GUARD_POLICY_ID"]
FAST_MODE            = os.environ.get("ACTION_GUARD_FAST_MODE", "false").lower() == "true"
ACTION_GUARD_JWT       = os.environ["ACTION_GUARD_JWT"]

# MCP Gateway logging (for Virtue Dashboard trajectory)
VIRTUE_DASHBOARD_URL   = os.environ.get("VIRTUE_DASHBOARD_URL", "https://agentgateway.virtueai.io")
GATEWAY_JWT            = os.environ.get("GATEWAY_JWT", ACTION_GUARD_JWT)

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
REQUEST_LOG_DIR = os.path.join(SCRIPT_DIR, "logs", "requests")
os.makedirs(REQUEST_LOG_DIR, exist_ok=True)


def _save_request(hook: str, execution_id: str, headers: dict, body: dict):
    """Dump the full incoming request (headers + body) to a JSON file for debugging."""
    ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S_%f")
    record = {
        "timestamp": ts,
        "hook": hook,
        "headers": headers,
        "body": body,
    }
    path = os.path.join(REQUEST_LOG_DIR, f"{ts}_{hook}_{execution_id}.json")
    with open(path, "w") as f:
        json.dump(record, f, indent=2, default=str)
    log.debug("[%s] saved full request to %s", hook, path)


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

# ─── Gateway logging ─────────────────────────────────────────────────────────

async def _log_tool_event(
    event_type: str,
    session_id: str,
    tool_name: str,
    tool_params: dict | None = None,
    server_name: str = "Arcade",
    server_id: str = "arcade-mcp",
    user_query: str | None = None,
    result: object = None,
    guard_info: dict | None = None,
    gateway_id: str = "arcade",
):
    """Fire-and-forget call to MCP Gateway log-tool-event endpoint."""
    payload = {
        "event_type": event_type,
        "session_id": session_id,
        "tool_name": tool_name,
        "server_name": server_name,
        "server_id": server_id,
    }
    if gateway_id:
        payload["gateway_id"] = gateway_id
    if tool_params is not None:
        payload["tool_params"] = tool_params
    if user_query:
        payload["user_query"] = user_query
    if result is not None:
        payload["result"] = result
    if guard_info:
        payload["guard_info"] = guard_info

    try:
        resp = await _http.post(
            f"{VIRTUE_DASHBOARD_URL}/api/log-tool-event",
            headers={"Authorization": f"Bearer {GATEWAY_JWT}"},
            json=payload,
        )
        resp.raise_for_status()
        data = resp.json()
        log.info("[log] %s logged → session=%s step=%s", event_type, data.get("session_id"), data.get("step_id"))
    except Exception as exc:
        log.warning("[log] Failed to log %s: %s", event_type, exc)


def _build_guard_info(guard_result: dict, hook: str) -> dict:
    """Convert Action Guard response into the guard_info format for session storage."""
    action_guard = {
        "allowed": guard_result.get("allowed", True),
        "checked": True,
    }
    if guard_result.get("violations"):
        action_guard["violations"] = guard_result["violations"]
    if guard_result.get("explanation"):
        action_guard["explanation"] = guard_result["explanation"]
    if guard_result.get("threat_category"):
        action_guard["threat_category"] = guard_result["threat_category"]
    if guard_result.get("policy_id"):
        action_guard["policy_id"] = guard_result["policy_id"]

    return {
        "access_control": {"allowed": True, "checked": False},
        "action_guard": action_guard,
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
    # Extract Arcade's header token for debug logging (not used for Action Guard auth)
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
    _save_request("pre", execution_id, dict(request.headers), body)

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
            headers={"Authorization": f"Bearer {ACTION_GUARD_JWT}"},
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

    # Log tool_call to MCP Gateway for Virtue Dashboard trajectory
    tool_full = f"{toolkit}_{tool}" if toolkit else tool
    guard_info = _build_guard_info(result, "pre")
    session_id = request.headers.get("x-session-id", execution_id)
    arcade_meta = context.get("metadata", {})
    user_query = arcade_meta.get("user_query") if arcade_meta else None
    await _log_tool_event(
        event_type="tool_call",
        session_id=session_id,
        tool_name=tool_full,
        tool_params=inputs,
        user_query=user_query,
        guard_info=guard_info,
    )

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
    _save_request("post", execution_id, dict(request.headers), body)

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
            headers={"Authorization": f"Bearer {ACTION_GUARD_JWT}"},
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

    # Log tool_result to MCP Gateway for Virtue Dashboard trajectory
    tool_full = f"{toolkit}_{tool}" if toolkit else tool
    guard_info = _build_guard_info(result, "post")
    session_id = request.headers.get("x-session-id", execution_id)
    await _log_tool_event(
        event_type="tool_result",
        session_id=session_id,
        tool_name=tool_full,
        result=output,
        guard_info=guard_info,
    )

    if not allowed:
        reason = explanation or "; ".join(violations[:3]) or "policy violation"
        log.warning("[post] BLOCKED tool=%s_%s reason=%s", toolkit, tool, reason[:200])
        return {"code": "CHECK_FAILED", "error_message": f"Output blocked by policy: {reason}"}

    log.info("[post] ALLOWED tool=%s_%s", toolkit, tool)
    return {"code": "OK"}
