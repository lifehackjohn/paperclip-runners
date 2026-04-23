#!/usr/bin/env python3
"""
Sage Paperclip Runner
=====================

Knowledge manager runner — headless, local-only.

  1. Paperclip heartbeats  (/heartbeat)
     - Picks up assigned Paperclip issues, calls oMLX, posts comments back.

  2. Direct chat  (/chat)
     - Accepts { message, history } for the Runner Chat UI (BLU-137).
     - Pure LLM inference + dual-namespace Mem0. No cloud transit.

Architecture:
  LLM:    oMLX at localhost:8000/v1 (Qwen3.5-9B-mlx-lm-mxfp4)
  Memory: mem0 at localhost:8050
    - namespace `johannes` — shared institutional knowledge (read + write)
    - namespace `sage`     — operational memory: task context, conversation history

Sage operates headless on the LifeOS vault at /Volumes/Public/apps/LifeOS/.
Nothing leaves the Mac Mini. No cloud APIs, no outbound inference.

Run:
  uvicorn runner:app --host 0.0.0.0 --port 6127
"""

import json
import logging
import os
import re
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
logger = logging.getLogger("sage-runner")

# ── Config ──────────────────────────────────────────────────────────────────────

PAPERCLIP_API_URL = os.environ.get("PAPERCLIP_API_URL", "http://localhost:3100")
PAPERCLIP_API_KEY = os.environ.get("SAGE_PAPERCLIP_API_KEY", "")

OMLX_URL     = os.environ.get("OMLX_URL", "http://localhost:8000/v1")
OMLX_API_KEY = os.environ.get("OMLX_API_KEY", "")
OMLX_MODEL   = os.environ.get("OMLX_MODEL", "Qwen3.5-9B-mlx-lm-mxfp4")

MEM0_URL = os.environ.get("MEM0_URL", "http://localhost:8050")

# Dual Mem0 namespaces
MEM0_INSTITUTIONAL_USER_ID = os.environ.get("MEM0_INSTITUTIONAL_USER_ID", "johannes")  # shared knowledge
MEM0_OPERATIONAL_USER_ID   = os.environ.get("MEM0_OPERATIONAL_USER_ID", "sage")        # task/conversation context

# LifeOS vault — Sage's working directory
LIFEOS_VAULT_PATH = os.environ.get("LIFEOS_VAULT_PATH", "/Volumes/Public/apps/LifeOS")

PORT = int(os.environ.get("SAGE_RUNNER_PORT", "6127"))


# ── HTTP helpers ─────────────────────────────────────────────────────────────────

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


# ── oMLX ─────────────────────────────────────────────────────────────────────────

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


# ── Mem0 (dual namespace) ─────────────────────────────────────────────────────────

def mem0_search(query: str, user_id: str, limit: int = 6) -> list:
    """Search a single Mem0 namespace."""
    try:
        result = http_post(f"{MEM0_URL}/v1/memories/search/", {
            "query": query,
            "user_id": user_id,
            "limit": limit,
        })
        return result.get("results", []) if isinstance(result, dict) else result
    except Exception as e:
        logger.debug("Mem0 search skipped (user=%s): %s", user_id, e)
        return []


def mem0_search_dual(query: str, limit_each: int = 5) -> list:
    """Search both namespaces. Institutional hits first, then operational."""
    institutional = mem0_search(query, MEM0_INSTITUTIONAL_USER_ID, limit_each)
    operational   = mem0_search(query, MEM0_OPERATIONAL_USER_ID,   limit_each)
    return institutional + operational


def mem0_add(messages: list, user_id: str) -> None:
    """Fire-and-forget: save to mem0 without blocking response."""
    def _add():
        try:
            http_post(f"{MEM0_URL}/v1/memories/", {
                "messages": messages,
                "user_id": user_id,
            }, timeout=300)
        except Exception as e:
            logger.debug("Mem0 add skipped (user=%s): %s", user_id, e)
    threading.Thread(target=_add, daemon=True).start()


# ── LifeOS vault context ─────────────────────────────────────────────────────────

# File types Sage can read from the vault
_VAULT_TEXT_EXTENSIONS = {".md", ".txt", ".yaml", ".yml", ".json", ".csv"}
_VAULT_MAX_TOTAL_CHARS = 40_000   # ~10k tokens — vault files can be large
_VAULT_MAX_FILE_CHARS  = 5_000    # cap per file to ensure breadth over depth


def read_vault_status() -> str:
    """Read _STATUS.md from the vault for current index context."""
    status_path = Path(LIFEOS_VAULT_PATH) / "_STATUS.md"
    if not status_path.exists():
        return ""
    try:
        content = status_path.read_text(encoding="utf-8", errors="replace")
        return f"## Vault Index (_STATUS.md)\n\n{content[:_VAULT_MAX_FILE_CHARS]}"
    except Exception as e:
        logger.debug("Could not read vault _STATUS.md: %s", e)
        return ""


def read_vault_files(
    subfolder: str = "",
    max_total_chars: int = _VAULT_MAX_TOTAL_CHARS,
) -> str:
    """
    Read text files from the vault (optionally within a subfolder).
    Returns a combined string suitable for injecting into the prompt.
    Used when Sage needs to reference vault content for a specific task.
    """
    vault_root = Path(LIFEOS_VAULT_PATH)
    if subfolder:
        vault_root = vault_root / subfolder

    if not vault_root.exists():
        return ""

    parts: List[str] = []
    total_chars = 0

    try:
        files = sorted(vault_root.rglob("*"))
    except Exception:
        return ""

    for f in files:
        if not f.is_file():
            continue
        if f.suffix.lower() not in _VAULT_TEXT_EXTENSIONS:
            continue
        if total_chars >= max_total_chars:
            break
        try:
            text = f.read_text(encoding="utf-8", errors="replace")
            if not text.strip():
                continue
            snippet = text[:_VAULT_MAX_FILE_CHARS]
            relative = f.relative_to(Path(LIFEOS_VAULT_PATH))
            parts.append(f"### {relative}\n\n{snippet}")
            total_chars += len(snippet)
        except Exception:
            continue

    if not parts:
        return ""
    return "## LifeOS Vault Files\n\n" + "\n\n---\n\n".join(parts)


# ── Paperclip helpers ─────────────────────────────────────────────────────────────

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


# ── System prompt ────────────────────────────────────────────────────────────────

def _load_system_prompt() -> str:
    prompt_path = Path(__file__).parent / "system-prompt.md"
    if prompt_path.exists():
        return prompt_path.read_text(encoding="utf-8").strip()
    return (
        "You are Sage, Knowledge Manager at Blue Islands Enterprises. "
        "You maintain the institutional knowledge vault on the local LifeOS system. "
        "All data stays local. Nothing leaves the Mac Mini."
    )


SYSTEM_PROMPT = _load_system_prompt()


# ── Paperclip heartbeat handler ───────────────────────────────────────────────────

async def handle_heartbeat(body: dict) -> dict:
    agent_id = body.get("agentId", "")
    run_id   = body.get("runId", "")
    context  = body.get("context", {})

    api_key  = body.get("paperclipApiKey") or PAPERCLIP_API_KEY
    api_url  = body.get("paperclipApiUrl") or PAPERCLIP_API_URL
    model    = body.get("model") or OMLX_MODEL

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

    # Dual-namespace Mem0 search
    memory_context = ""
    memories = mem0_search_dual(triggering_message)
    if memories:
        items = [m.get("memory", "") for m in memories if m.get("memory")]
        if items:
            memory_context = "Relevant memories:\n" + "\n".join(f"- {m}" for m in items[:8])

    # Vault status for context (lightweight — just the index)
    vault_status = read_vault_status()

    # Build system prompt
    system_content = SYSTEM_PROMPT
    if memory_context:
        system_content += f"\n\n{memory_context}"
    if vault_status:
        system_content += f"\n\n{vault_status}"
    if documents_context:
        system_content += "\n\nThe following documents are attached to this task:\n\n" + documents_context

    messages = [{"role": "system", "content": system_content}]
    messages.extend(conversation_history[-20:])
    if not messages or messages[-1]["role"] != "user":
        messages.append({"role": "user", "content": triggering_message})

    # Call oMLX (local — no cloud transit)
    try:
        response = omlx_chat(messages, model=model)
        logger.info("oMLX response (%d chars) for issue %s", len(response), issue_id)
    except Exception as e:
        logger.error("oMLX call failed: %s", e)
        return {"status": "error", "reason": f"omlx_failed: {e}"}

    paperclip_post_comment(issue_id, response, run_id, api_url, api_key)

    # Save to operational namespace (sage) — task context, not institutional knowledge
    mem0_add(
        [
            {"role": "user", "content": triggering_message},
            {"role": "assistant", "content": response},
        ],
        user_id=MEM0_OPERATIONAL_USER_ID,
    )

    logger.info("Heartbeat complete: responded to issue %s", issue_id)
    return {"status": "ok", "action": "responded", "issueId": issue_id}


# ── FastAPI app ──────────────────────────────────────────────────────────────────

@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("Sage runner starting on port %d", PORT)
    logger.info("  Paperclip:      %s", PAPERCLIP_API_URL)
    logger.info("  oMLX:           %s / %s", OMLX_URL, OMLX_MODEL)
    logger.info("  Mem0:           %s", MEM0_URL)
    logger.info("  Mem0 namespace: institutional=%s  operational=%s",
                MEM0_INSTITUTIONAL_USER_ID, MEM0_OPERATIONAL_USER_ID)
    logger.info("  LifeOS vault:   %s", LIFEOS_VAULT_PATH)

    vault_exists = Path(LIFEOS_VAULT_PATH).exists()
    if not vault_exists:
        logger.warning("LifeOS vault not found at %s — vault reads will return empty", LIFEOS_VAULT_PATH)
    else:
        logger.info("LifeOS vault accessible")

    yield

    logger.info("Sage runner shutdown complete")


app = FastAPI(
    title="Sage Paperclip Runner",
    description="Knowledge manager runner — local-only, headless, LifeOS vault access",
    version="1.0.0",
    lifespan=lifespan,
)


@app.get("/health")
def health():
    vault_exists = Path(LIFEOS_VAULT_PATH).exists()
    return {
        "status": "ok",
        "service": "sage-runner",
        "version": "1.0.0",
        "lifeos_vault": "accessible" if vault_exists else "not_found",
        "vault_path": LIFEOS_VAULT_PATH,
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


@app.post("/chat")
async def chat(request: Request):
    """
    Direct chat endpoint for the Runner Chat UI (BLU-137).
    Accepts { message, history } and returns { response }.

    Uses dual Mem0 namespaces — searches both institutional (johannes)
    and operational (sage) memories, saves responses to sage namespace.

    history: list of { role: "user"|"assistant", content: str }

    All inference runs locally on oMLX. No data leaves the Mac Mini.
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

    # Dual-namespace memory search
    memory_context = ""
    memories = mem0_search_dual(message)
    if memories:
        items = [m.get("memory", "") for m in memories if m.get("memory")]
        if items:
            memory_context = "Relevant memories:\n" + "\n".join(f"- {m}" for m in items[:8])

    # Include vault index for context
    vault_status = read_vault_status()

    system_content = SYSTEM_PROMPT
    if memory_context:
        system_content += f"\n\n{memory_context}"
    if vault_status:
        system_content += f"\n\n{vault_status}"

    messages = [{"role": "system", "content": system_content}]
    messages.extend(history[-20:])
    messages.append({"role": "user", "content": message})

    try:
        response = omlx_chat(messages, model=model)
    except Exception as e:
        logger.error("Chat LLM call failed: %s", e)
        return JSONResponse(content={"error": f"llm_failed: {e}"}, status_code=500)

    # Save to operational namespace (fire-and-forget)
    mem0_add(
        [
            {"role": "user", "content": message},
            {"role": "assistant", "content": response},
        ],
        user_id=MEM0_OPERATIONAL_USER_ID,
    )

    logger.info("Chat response (%d chars)", len(response))
    return JSONResponse(content={"response": response})


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=PORT, log_level="info")
