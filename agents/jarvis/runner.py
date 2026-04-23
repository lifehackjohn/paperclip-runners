#!/usr/bin/env python3
"""
Jarvis Paperclip Runner
=======================

Personal assistant runner that handles two surfaces simultaneously:

  1. Paperclip heartbeats  (/heartbeat)
     - Picks up assigned Paperclip issues, calls oMLX, posts comments back.
     - Identical flow to other paperclip-runners agents.

  2. WhatsApp gateway  (/whatsapp/webhook)
     - Manages the Baileys bridge.js subprocess on startup.
     - Background asyncio task polls bridge GET /messages every second.
     - Incoming WhatsApp messages → oMLX → reply via bridge POST /send.
     - /whatsapp/webhook endpoint also accepts push-style webhook payloads
       (forward-compatible with WAHA or other bridge implementations).

LLM:    oMLX at localhost:8000/v1 (Qwen3.5-9B-mlx-lm-mxfp4)
Memory: mem0 at localhost:8050 (user_id=jarvis)
Bridge: Baileys bridge.js (reuses existing Hermes WhatsApp bridge)

Run:
  uvicorn runner:app --host 0.0.0.0 --port 6126
"""

import asyncio
import json
import logging
import os
import re
import signal
import subprocess
import threading
import urllib.request
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Dict, List, Optional

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
logger = logging.getLogger("jarvis-runner")

# ── Config ─────────────────────────────────────────────────────────────────────

PAPERCLIP_API_URL = os.environ.get("PAPERCLIP_API_URL", "http://localhost:3100")
PAPERCLIP_API_KEY = os.environ.get("JARVIS_PAPERCLIP_API_KEY", "")

OMLX_URL     = os.environ.get("OMLX_URL", "http://localhost:8000/v1")
OMLX_API_KEY = os.environ.get("OMLX_API_KEY", "")
OMLX_MODEL   = os.environ.get("OMLX_MODEL", "Qwen3.5-9B-mlx-lm-mxfp4")

MEM0_URL            = os.environ.get("MEM0_URL", "http://localhost:8050")
MEM0_USER_ID        = os.environ.get("MEM0_USER_ID", "jarvis")
MEM0_SHARED_USER_ID = os.environ.get("MEM0_SHARED_USER_ID", "johannes")

# WhatsApp bridge
BRIDGE_SCRIPT = os.environ.get(
    "WHATSAPP_BRIDGE_SCRIPT",
    str(Path.home() / ".hermes/hermes-agent/scripts/whatsapp-bridge/bridge.js"),
)
BRIDGE_PORT    = int(os.environ.get("WHATSAPP_BRIDGE_PORT", "3001"))
BRIDGE_SESSION = os.environ.get(
    "WHATSAPP_SESSION_DIR",
    str(Path.home() / ".paperclip/runners/jarvis/whatsapp-session"),
)
# Mode: "self-chat" (same number as user) or "bot" (dedicated bot number)
BRIDGE_MODE    = os.environ.get("WHATSAPP_MODE", "self-chat")
# Optional reply prefix (empty string = no prefix; default shows agent identity in self-chat)
BRIDGE_PREFIX  = os.environ.get("WHATSAPP_REPLY_PREFIX", "")

PORT = int(os.environ.get("JARVIS_RUNNER_PORT", "6126"))

# In-memory conversation history per chat (chatId → list of messages)
_CHAT_HISTORY: Dict[str, List[Dict[str, str]]] = {}
MAX_HISTORY_PER_CHAT = 20

# Bridge process handle (managed by lifespan)
_bridge_process: Optional[subprocess.Popen] = None
_bridge_poll_task: Optional[asyncio.Task] = None


# ── HTTP helpers ───────────────────────────────────────────────────────────────

def http_get(url: str, api_key: str = "", timeout: int = 15) -> Any:
    req = urllib.request.Request(url)
    if api_key:
        req.add_header("Authorization", f"Bearer {api_key}")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode())


def http_post(url: str, payload: dict, api_key: str = "", timeout: int = 60) -> Any:
    data = json.dumps(payload).encode()
    req = urllib.request.Request(url, data=data, method="POST")
    req.add_header("Content-Type", "application/json")
    if api_key:
        req.add_header("Authorization", f"Bearer {api_key}")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode())


# ── oMLX ──────────────────────────────────────────────────────────────────────

def strip_thinking(text: str) -> str:
    """Strip <think>…</think> blocks emitted by reasoning models."""
    text = re.sub(r"<think>.*?</think>\s*", "", text, flags=re.DOTALL)
    return text.strip()


def omlx_chat(messages: list, model: str = OMLX_MODEL) -> str:
    """Call oMLX via OpenAI-compatible /v1/chat/completions."""
    payload = {
        "model": model,
        "messages": messages,
        "max_tokens": 2048,
        "temperature": 0.7,
        "chat_template_kwargs": {"enable_thinking": False},
    }
    req = urllib.request.Request(
        f"{OMLX_URL.rstrip('/')}/chat/completions",
        data=json.dumps(payload).encode(),
        method="POST",
    )
    req.add_header("Content-Type", "application/json")
    if OMLX_API_KEY:
        req.add_header("Authorization", f"Bearer {OMLX_API_KEY}")
    with urllib.request.urlopen(req, timeout=300) as resp:
        result = json.loads(resp.read().decode())
    return strip_thinking(result["choices"][0]["message"]["content"].strip())


# ── Mem0 ──────────────────────────────────────────────────────────────────────

def mem0_search(query: str, user_id: str = MEM0_USER_ID, limit: int = 6) -> list:
    try:
        result = http_post(f"{MEM0_URL}/v1/memories/search/", {
            "query": query,
            "user_id": user_id,
            "limit": limit,
        })
        return result.get("results", []) if isinstance(result, dict) else result
    except Exception as e:
        logger.debug("Mem0 search skipped: %s", e)
        return []


def mem0_add(messages: list, user_id: str = MEM0_USER_ID) -> None:
    """Fire-and-forget: save to mem0 without blocking response."""
    def _add():
        try:
            http_post(f"{MEM0_URL}/v1/memories/", {
                "messages": messages,
                "user_id": user_id,
            }, timeout=300)
        except Exception as e:
            logger.debug("Mem0 add skipped: %s", e)
    threading.Thread(target=_add, daemon=True).start()


# ── WhatsApp formatting ────────────────────────────────────────────────────────

def to_whatsapp_markdown(text: str) -> str:
    """Convert standard markdown to WhatsApp-compatible formatting.

    WhatsApp uses: *bold*, _italic_, ~strikethrough~, ```code```
    Standard markdown uses **bold**, *italic*, ~~strike~~
    """
    if not text:
        return text

    # Protect fenced code blocks
    _FENCE_PH = "\x00FENCE"
    fences: list = []

    def _save_fence(m: re.Match) -> str:
        fences.append(m.group(0))
        return f"{_FENCE_PH}{len(fences) - 1}\x00"

    result = re.sub(r"```[\s\S]*?```", _save_fence, text)

    # Protect inline code
    _CODE_PH = "\x00CODE"
    codes: list = []

    def _save_code(m: re.Match) -> str:
        codes.append(m.group(0))
        return f"{_CODE_PH}{len(codes) - 1}\x00"

    result = re.sub(r"`[^`\n]+`", _save_code, result)

    # **bold** / __bold__ → *bold*
    result = re.sub(r"\*\*(.+?)\*\*", r"*\1*", result)
    result = re.sub(r"__(.+?)__", r"*\1*", result)
    # ~~strike~~ → ~strike~
    result = re.sub(r"~~(.+?)~~", r"~\1~", result)
    # # Headers → *bold*
    result = re.sub(r"^#{1,6}\s+(.+)$", r"*\1*", result, flags=re.MULTILINE)
    # [text](url) → text (url)
    result = re.sub(r"\[([^\]]+)\]\(([^)]+)\)", r"\1 (\2)", result)

    # Restore protected sections
    for i, fence in enumerate(fences):
        result = result.replace(f"{_FENCE_PH}{i}\x00", fence)
    for i, code in enumerate(codes):
        result = result.replace(f"{_CODE_PH}{i}\x00", code)

    return result


# ── WhatsApp bridge management ─────────────────────────────────────────────────

def bridge_url(path: str) -> str:
    return f"http://127.0.0.1:{BRIDGE_PORT}{path}"


def bridge_health() -> Optional[dict]:
    """Return bridge health dict or None if unreachable."""
    try:
        return http_get(bridge_url("/health"), timeout=3)
    except Exception:
        return None


def bridge_send(chat_id: str, message: str, reply_to: Optional[str] = None) -> bool:
    """Send a WhatsApp message via the bridge. Returns True on success."""
    try:
        payload: dict = {"chatId": chat_id, "message": message}
        if reply_to:
            payload["replyTo"] = reply_to
        http_post(bridge_url("/send"), payload, timeout=30)
        return True
    except Exception as e:
        logger.warning("Bridge send failed: %s", e)
        return False


def bridge_poll_messages() -> list:
    """Drain the bridge message queue. Returns list of message dicts."""
    try:
        result = http_get(bridge_url("/messages"), timeout=30)
        return result if isinstance(result, list) else []
    except Exception as e:
        logger.debug("Bridge poll error: %s", e)
        return []


async def start_bridge() -> bool:
    """Launch the WhatsApp bridge subprocess. Returns True if ready."""
    global _bridge_process

    if not Path(BRIDGE_SCRIPT).exists():
        logger.warning("Bridge script not found: %s — WhatsApp disabled", BRIDGE_SCRIPT)
        return False

    # Check if a bridge is already running on our port
    health = bridge_health()
    if health and health.get("status") == "connected":
        logger.info("Reusing existing bridge (status: connected)")
        return True
    if health:
        logger.info("Bridge found but status=%s — restarting", health.get("status"))

    # Ensure session directory exists
    Path(BRIDGE_SESSION).mkdir(parents=True, exist_ok=True)

    bridge_dir  = Path(BRIDGE_SCRIPT).parent
    log_path    = Path(BRIDGE_SESSION).parent / "bridge.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)

    # Auto-install npm deps if needed
    if not (bridge_dir / "node_modules").exists():
        logger.info("Installing bridge npm dependencies…")
        result = subprocess.run(
            ["npm", "install", "--silent"],
            cwd=str(bridge_dir),
            capture_output=True,
            text=True,
            timeout=120,
        )
        if result.returncode != 0:
            logger.error("npm install failed: %s", result.stderr)
            return False

    env = os.environ.copy()
    if BRIDGE_PREFIX is not None:
        env["WHATSAPP_REPLY_PREFIX"] = BRIDGE_PREFIX

    log_fh = open(log_path, "a")
    _bridge_process = subprocess.Popen(
        [
            "node",
            BRIDGE_SCRIPT,
            "--port",    str(BRIDGE_PORT),
            "--session", BRIDGE_SESSION,
            "--mode",    BRIDGE_MODE,
        ],
        stdout=log_fh,
        stderr=log_fh,
        preexec_fn=os.setsid,
        env=env,
    )
    logger.info("Bridge PID %d started — log: %s", _bridge_process.pid, log_path)

    # Wait up to 15s for HTTP server to come up
    for _ in range(15):
        await asyncio.sleep(1)
        if _bridge_process.poll() is not None:
            logger.error("Bridge exited early (code %d)", _bridge_process.returncode)
            return False
        health = bridge_health()
        if health:
            logger.info("Bridge HTTP ready (status=%s)", health.get("status", "unknown"))
            break
    else:
        logger.error("Bridge HTTP did not start in 15s")
        return False

    # Wait up to an additional 20s for WhatsApp to connect (uses saved creds)
    if bridge_health() and bridge_health().get("status") != "connected":
        logger.info("Waiting for WhatsApp to authenticate…")
        for _ in range(20):
            await asyncio.sleep(1)
            health = bridge_health()
            if health and health.get("status") == "connected":
                logger.info("WhatsApp connected")
                break
        else:
            logger.warning(
                "WhatsApp not connected after 35s — bridge running but needs pairing. "
                "Run: node %s --port %d --session %s --pair-only",
                BRIDGE_SCRIPT, BRIDGE_PORT, BRIDGE_SESSION,
            )

    return True


async def stop_bridge() -> None:
    """Terminate the bridge subprocess if we own it."""
    global _bridge_process
    if _bridge_process is None:
        return
    try:
        os.killpg(os.getpgid(_bridge_process.pid), signal.SIGTERM)
        await asyncio.sleep(1)
        if _bridge_process.poll() is None:
            os.killpg(os.getpgid(_bridge_process.pid), signal.SIGKILL)
    except (ProcessLookupError, PermissionError, OSError):
        try:
            _bridge_process.terminate()
        except Exception:
            pass
    _bridge_process = None
    logger.info("Bridge stopped")


# ── WhatsApp message routing ───────────────────────────────────────────────────

def _load_system_prompt() -> str:
    """Load system prompt from system-prompt.md adjacent to this file, or fall back."""
    prompt_path = Path(__file__).parent / "system-prompt.md"
    if prompt_path.exists():
        return prompt_path.read_text(encoding="utf-8").strip()
    return (
        "You are Jarvis, a personal assistant. "
        "Be concise, helpful, and direct. "
        "You are responding over WhatsApp — avoid markdown tables and headers. "
        "Use plain text with occasional *bold* for emphasis."
    )


SYSTEM_PROMPT = _load_system_prompt()


async def route_whatsapp_message(msg: dict) -> None:
    """Process one incoming WhatsApp message: call oMLX, reply via bridge."""
    chat_id     = msg.get("chatId", "")
    sender_name = msg.get("senderName") or msg.get("senderId", "user")
    body        = msg.get("body", "").strip()
    message_id  = msg.get("messageId")
    is_from_me  = msg.get("fromMe", False)

    if not chat_id or not body:
        return
    if is_from_me:
        return  # Ignore echoed outgoing messages

    logger.info("WhatsApp message from %s (%s): %.80s", sender_name, chat_id, body)

    # Build / update per-chat history
    history = _CHAT_HISTORY.setdefault(chat_id, [])
    history.append({"role": "user", "content": body})
    # Trim to window
    if len(history) > MAX_HISTORY_PER_CHAT:
        _CHAT_HISTORY[chat_id] = history[-MAX_HISTORY_PER_CHAT:]
        history = _CHAT_HISTORY[chat_id]

    # Mem0 search — agent-specific memories + shared johannes institutional knowledge
    memory_context = ""
    try:
        agent_memories  = mem0_search(body)
        shared_memories = mem0_search(body, user_id=MEM0_SHARED_USER_ID)
        seen: set = set()
        all_memories: list = []
        for m in agent_memories + shared_memories:
            text = m.get("memory", "")
            if text and text not in seen:
                seen.add(text)
                all_memories.append(m)
        if all_memories:
            items = [m.get("memory", "") for m in all_memories]
            memory_context = "Relevant memories:\n" + "\n".join(f"- {m}" for m in items[:5])
    except Exception as e:
        logger.debug("Mem0 search error: %s", e)

    # Build LLM messages
    system_content = SYSTEM_PROMPT
    if memory_context:
        system_content += f"\n\n{memory_context}"

    messages = [{"role": "system", "content": system_content}]
    messages.extend(history[-MAX_HISTORY_PER_CHAT:])
    if messages[-1]["role"] != "user":
        messages.append({"role": "user", "content": body})

    # Call oMLX
    try:
        response = omlx_chat(messages)
        logger.info("oMLX response (%d chars) for chat %s", len(response), chat_id)
    except Exception as e:
        logger.error("oMLX call failed for WhatsApp message: %s", e)
        return

    # Convert to WhatsApp formatting and send
    wa_response = to_whatsapp_markdown(response)
    sent = bridge_send(chat_id, wa_response, reply_to=message_id)
    if not sent:
        logger.warning("Failed to send reply to %s", chat_id)
        return

    # Update history with assistant reply
    history.append({"role": "assistant", "content": response})
    if len(history) > MAX_HISTORY_PER_CHAT:
        _CHAT_HISTORY[chat_id] = history[-MAX_HISTORY_PER_CHAT:]

    # Save to mem0 (fire-and-forget)
    mem0_add([
        {"role": "user", "content": body},
        {"role": "assistant", "content": response},
    ])


async def _bridge_poll_loop() -> None:
    """Background task: poll bridge for incoming messages every second."""
    logger.info("Bridge poll loop started")
    while True:
        try:
            messages = bridge_poll_messages()
            for msg in messages:
                await route_whatsapp_message(msg)
        except asyncio.CancelledError:
            break
        except Exception as e:
            logger.debug("Poll loop error: %s", e)
        await asyncio.sleep(1)
    logger.info("Bridge poll loop stopped")


# ── Paperclip helpers ──────────────────────────────────────────────────────────

def paperclip_get_issue(issue_id: str, api_url: str, api_key: str) -> Optional[dict]:
    try:
        return http_get(f"{api_url}/api/issues/{issue_id}", api_key)
    except Exception as e:
        logger.warning("Failed to fetch issue %s: %s", issue_id, e)
        return None


def paperclip_get_comments(issue_id: str, api_url: str, api_key: str) -> list:
    try:
        result = http_get(f"{api_url}/api/issues/{issue_id}/comments", api_key)
        return result if isinstance(result, list) else []
    except Exception as e:
        logger.warning("Failed to fetch comments for %s: %s", issue_id, e)
        return []


def paperclip_get_issue_documents(issue_id: str, api_url: str, api_key: str) -> str:
    try:
        docs = http_get(f"{api_url}/api/issues/{issue_id}/documents", api_key)
        if not isinstance(docs, list) or not docs:
            return ""
        parts = []
        for doc in docs:
            title = doc.get("title") or doc.get("key", "")
            body  = doc.get("body", "")
            if body:
                parts.append(f"### Document: {title}\n\n{body}")
        if not parts:
            return ""
        return "## Issue Documents\n\n" + "\n\n---\n\n".join(parts)
    except Exception as e:
        logger.debug("Issue documents fetch skipped: %s", e)
        return ""


def paperclip_post_comment(
    issue_id: str, body: str, run_id: str, api_url: str, api_key: str
) -> None:
    try:
        req = urllib.request.Request(
            f"{api_url}/api/issues/{issue_id}/comments",
            data=json.dumps({"body": body}).encode(),
            method="POST",
        )
        req.add_header("Content-Type", "application/json")
        req.add_header("Authorization", f"Bearer {api_key}")
        req.add_header("X-Paperclip-Run-Id", run_id)
        with urllib.request.urlopen(req, timeout=15) as resp:
            resp.read()
    except Exception as e:
        logger.warning("Failed to post comment to %s: %s", issue_id, e)


# ── Paperclip heartbeat handler ────────────────────────────────────────────────

async def handle_heartbeat(body: dict) -> dict:
    agent_id = body.get("agentId", "")
    run_id   = body.get("runId", "")
    context  = body.get("context", {})

    api_key  = body.get("paperclipApiKey") or PAPERCLIP_API_KEY
    api_url  = body.get("paperclipApiUrl") or PAPERCLIP_API_URL
    model    = body.get("model") or OMLX_MODEL
    mem0_uid = body.get("mem0UserId") or MEM0_USER_ID

    issue_id        = context.get("issueId") or context.get("taskId")
    wake_comment_id = context.get("wakeCommentId")

    logger.info(
        "Heartbeat: agent=%s run=%s issue=%s wake=%s",
        agent_id, run_id, issue_id, context.get("wakeReason", "timer"),
    )

    if not api_key:
        logger.error("No Paperclip API key")
        return {"status": "error", "reason": "missing_api_key"}

    if not issue_id:
        logger.info("Timer wake with no issue — idle")
        return {"status": "ok", "action": "idle"}

    issue = paperclip_get_issue(issue_id, api_url, api_key)
    if not issue:
        return {"status": "ok", "action": "skipped", "reason": "issue_fetch_failed"}

    issue_title = issue.get("title", "")
    issue_desc  = issue.get("description", "")
    comments    = paperclip_get_comments(issue_id, api_url, api_key)

    # Issue documents (plans, briefs, etc.)
    documents_context = paperclip_get_issue_documents(issue_id, api_url, api_key)

    # Build conversation history
    triggering_message   = None
    conversation_history = []

    for comment in comments:
        author_agent_id = comment.get("authorAgentId")
        author_user_id  = comment.get("authorUserId")
        body_text       = comment.get("body", "")
        if not body_text:
            continue
        if not author_agent_id and not author_user_id:
            continue
        if author_agent_id == agent_id:
            conversation_history.append({"role": "assistant", "content": body_text})
        else:
            conversation_history.append({"role": "user", "content": body_text})
            if wake_comment_id and comment.get("id") == wake_comment_id:
                triggering_message = body_text

    if not triggering_message:
        for msg in reversed(conversation_history):
            if msg["role"] == "user":
                triggering_message = msg["content"]
                break

    if not triggering_message and not any(m["role"] == "user" for m in conversation_history):
        triggering_message = f"New task: {issue_title}"
        if issue_desc:
            triggering_message += f"\n\n{issue_desc}"

    if not triggering_message:
        logger.info("No message to respond to")
        return {"status": "ok", "action": "skipped", "reason": "no_message"}

    # Mem0 search — agent-specific memories + shared johannes institutional knowledge
    memory_context = ""
    agent_memories  = mem0_search(triggering_message, user_id=mem0_uid)
    shared_memories = mem0_search(triggering_message, user_id=MEM0_SHARED_USER_ID)
    seen_hb: set = set()
    all_memories_hb: list = []
    for m in agent_memories + shared_memories:
        text = m.get("memory", "")
        if text and text not in seen_hb:
            seen_hb.add(text)
            all_memories_hb.append(m)
    if all_memories_hb:
        items = [m.get("memory", "") for m in all_memories_hb]
        memory_context = "Relevant memories:\n" + "\n".join(f"- {m}" for m in items[:6])

    # System prompt for Paperclip (no WA formatting constraints)
    system_content = SYSTEM_PROMPT
    if memory_context:
        system_content += f"\n\n{memory_context}"
    if documents_context:
        system_content += "\n\nThe following documents are attached to this task:\n\n" + documents_context

    messages = [{"role": "system", "content": system_content}]
    messages.extend(conversation_history[-20:])
    if not messages or messages[-1]["role"] != "user":
        messages.append({"role": "user", "content": triggering_message})

    # Call oMLX
    try:
        response = omlx_chat(messages, model=model)
        logger.info("oMLX response (%d chars) for issue %s", len(response), issue_id)
    except Exception as e:
        logger.error("oMLX call failed: %s", e)
        return {"status": "error", "reason": f"omlx_failed: {e}"}

    paperclip_post_comment(issue_id, response, run_id, api_url, api_key)

    mem0_add(
        [
            {"role": "user", "content": triggering_message},
            {"role": "assistant", "content": response},
        ],
        user_id=mem0_uid,
    )

    logger.info("Heartbeat complete: responded to issue %s", issue_id)
    return {"status": "ok", "action": "responded", "issueId": issue_id}


# ── FastAPI app ────────────────────────────────────────────────────────────────

@asynccontextmanager
async def lifespan(app: FastAPI):
    global _bridge_poll_task

    logger.info("Jarvis runner starting on port %d", PORT)
    logger.info("  Paperclip: %s", PAPERCLIP_API_URL)
    logger.info("  oMLX:      %s / %s", OMLX_URL, OMLX_MODEL)
    logger.info("  Mem0:      %s (user=%s)", MEM0_URL, MEM0_USER_ID)
    logger.info("  Bridge:    %s (port %d)", BRIDGE_SCRIPT, BRIDGE_PORT)
    logger.info("  Session:   %s", BRIDGE_SESSION)

    # Start WhatsApp bridge
    bridge_ok = await start_bridge()
    if bridge_ok:
        _bridge_poll_task = asyncio.create_task(_bridge_poll_loop())
        logger.info("WhatsApp gateway active")
    else:
        logger.warning(
            "WhatsApp bridge not started — Paperclip heartbeats still work. "
            "Check bridge script path and Node.js installation."
        )

    yield

    # Shutdown
    if _bridge_poll_task and not _bridge_poll_task.done():
        _bridge_poll_task.cancel()
        try:
            await _bridge_poll_task
        except (asyncio.CancelledError, Exception):
            pass

    await stop_bridge()
    logger.info("Jarvis runner shutdown complete")


app = FastAPI(
    title="Jarvis Paperclip Runner",
    description="Personal assistant runner — Paperclip heartbeats + WhatsApp gateway",
    version="1.0.0",
    lifespan=lifespan,
)


@app.get("/health")
def health():
    bridge_status = "unknown"
    h = bridge_health()
    if h:
        bridge_status = h.get("status", "running")
    elif _bridge_process is None and not Path(BRIDGE_SCRIPT).exists():
        bridge_status = "disabled"
    elif _bridge_process is None:
        bridge_status = "not_started"
    else:
        bridge_status = "unreachable"

    return {
        "status": "ok",
        "service": "jarvis-runner",
        "version": "1.0.0",
        "whatsapp_bridge": bridge_status,
    }


@app.post("/heartbeat")
async def heartbeat(request: Request):
    try:
        body = await request.json()
    except Exception:
        body = {}
    try:
        result = await handle_heartbeat(body)
        return JSONResponse(content=result, status_code=200)
    except Exception as e:
        logger.error("Heartbeat handler error: %s", e, exc_info=True)
        return JSONResponse(content={"status": "error", "reason": str(e)}, status_code=200)


@app.post("/whatsapp/webhook")
async def whatsapp_webhook(request: Request):
    """
    Inbound WhatsApp message webhook.

    Accepts the same message format as the bridge's GET /messages response,
    either as a single object or a list. This endpoint is forward-compatible
    with webhook-push-based bridge implementations (e.g. WAHA).

    The polling loop also routes messages through route_whatsapp_message()
    so both paths share the same logic.
    """
    try:
        payload = await request.json()
    except Exception:
        return JSONResponse(content={"error": "invalid JSON"}, status_code=400)

    messages = payload if isinstance(payload, list) else [payload]
    processed = 0
    for msg in messages:
        if isinstance(msg, dict):
            asyncio.create_task(route_whatsapp_message(msg))
            processed += 1

    return JSONResponse(content={"status": "ok", "queued": processed}, status_code=200)


@app.post("/whatsapp/send")
async def whatsapp_send(request: Request):
    """
    Internal endpoint: send a WhatsApp message via the bridge.
    Payload: { chatId, message, replyTo? }
    """
    try:
        body = await request.json()
    except Exception:
        return JSONResponse(content={"error": "invalid JSON"}, status_code=400)

    chat_id  = body.get("chatId", "")
    message  = body.get("message", "")
    reply_to = body.get("replyTo")

    if not chat_id or not message:
        return JSONResponse(content={"error": "chatId and message required"}, status_code=400)

    success = bridge_send(chat_id, to_whatsapp_markdown(message), reply_to=reply_to)
    return JSONResponse(
        content={"status": "ok" if success else "error"},
        status_code=200 if success else 502,
    )


@app.post("/chat")
async def chat(request: Request):
    """
    Direct chat endpoint for the Runner Chat UI.
    Accepts { message, history } and returns { response }.
    Does not touch Paperclip or WhatsApp — pure LLM inference with optional Mem0.

    history: list of { role: "user"|"assistant", content: str }
    """
    try:
        body = await request.json()
    except Exception:
        return JSONResponse(content={"error": "invalid_json"}, status_code=400)

    message = (body.get("message") or "").strip()
    history = body.get("history") or []
    model   = body.get("model") or OMLX_MODEL

    if not message:
        return JSONResponse(content={"error": "message_required"}, status_code=400)

    # Search Mem0 for relevant memories (agent-specific + shared johannes)
    memory_context = ""
    agent_memories  = mem0_search(message)
    shared_memories = mem0_search(message, user_id=MEM0_SHARED_USER_ID)
    seen_chat: set = set()
    all_chat_mem: list = []
    for m in agent_memories + shared_memories:
        text = m.get("memory", "")
        if text and text not in seen_chat:
            seen_chat.add(text)
            all_chat_mem.append(m)
    if all_chat_mem:
        items = [m.get("memory", "") for m in all_chat_mem]
        memory_context = "Relevant memories:\n" + "\n".join(f"- {m}" for m in items[:6])

    system_content = SYSTEM_PROMPT
    if memory_context:
        system_content += f"\n\n{memory_context}"

    messages = [{"role": "system", "content": system_content}]
    messages.extend(history[-20:])
    messages.append({"role": "user", "content": message})

    try:
        response = omlx_chat(messages, model=model)
    except Exception as e:
        logger.error("Chat LLM call failed: %s", e)
        return JSONResponse(content={"error": f"llm_failed: {e}"}, status_code=500)

    # Save to Mem0 in the background
    mem0_add([
        {"role": "user", "content": message},
        {"role": "assistant", "content": response},
    ])

    logger.info("Chat response (%d chars)", len(response))
    return JSONResponse(content={"response": response})


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=PORT, log_level="info")
