#!/usr/bin/env python3
"""
Analyst Paperclip Runner
Bridges Paperclip http adapter heartbeat calls to oMLX + Mem0.

Analyst is a Research & Intelligence runner specialized in stakeholder
intelligence, soft due diligence, and relationship mapping.

Paperclip's http adapter POSTs:
  { agentId, runId, context, ...payloadTemplate }

This runner:
1. Fetches the assigned issue + comments via Paperclip API
2. Reads attached issue documents (plans, intel briefs, engagement docs)
3. Reads workspace files if a project workspace is attached
4. Fetches relevant Mem0 memories (userId: 'analyst')
5. Generates a response via oMLX (/v1/chat/completions)
6. Posts the response back to Paperclip as a comment
7. Saves important context to Mem0

Run:
  uvicorn analyst-runner:app --host 0.0.0.0 --port 6123
"""

import json
import logging
import os
import re
import threading
import urllib.request
from contextlib import asynccontextmanager
from typing import Any, Optional

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s"
)
logger = logging.getLogger("analyst-runner")

# ── Config (override via env or payloadTemplate) ──────────────────────────────
PAPERCLIP_API_URL = os.environ.get("PAPERCLIP_API_URL", "http://localhost:3100")
PAPERCLIP_API_KEY = os.environ.get("ANALYST_PAPERCLIP_API_KEY", "")
OMLX_URL          = os.environ.get("OMLX_URL", "http://localhost:8000/v1")
OMLX_API_KEY      = os.environ.get("OMLX_API_KEY", "")
OMLX_MODEL        = os.environ.get("OMLX_MODEL", "")
MEM0_URL          = os.environ.get("MEM0_URL", "http://localhost:8050")
MEM0_USER_ID      = os.environ.get("MEM0_USER_ID", "analyst")
PORT              = int(os.environ.get("ANALYST_RUNNER_PORT", "6123"))


# ── HTTP helpers ──────────────────────────────────────────────────────────────

def http_get(url: str, api_key: str = "") -> Any:
    req = urllib.request.Request(url)
    if api_key:
        req.add_header("Authorization", f"Bearer {api_key}")
    with urllib.request.urlopen(req, timeout=15) as resp:
        return json.loads(resp.read().decode())


def http_post(url: str, payload: dict, api_key: str = "", timeout: int = 60) -> Any:
    data = json.dumps(payload).encode()
    req = urllib.request.Request(url, data=data, method="POST")
    req.add_header("Content-Type", "application/json")
    if api_key:
        req.add_header("Authorization", f"Bearer {api_key}")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode())


# ── Mem0 helpers ──────────────────────────────────────────────────────────────

def mem0_search(query: str, user_id: str = MEM0_USER_ID, limit: int = 10) -> list:
    try:
        result = http_post(f"{MEM0_URL}/v1/memories/search/", {
            "query": query,
            "user_id": user_id,
            "limit": limit,
        })
        return result.get("results", []) if isinstance(result, dict) else result
    except Exception as e:
        logger.warning(f"Mem0 search failed: {e}")
        return []


def mem0_add(messages: list, user_id: str = MEM0_USER_ID) -> None:
    """Fire-and-forget: save to Mem0 without blocking the heartbeat."""
    def _add():
        try:
            http_post(f"{MEM0_URL}/v1/memories/", {
                "messages": messages,
                "user_id": user_id,
            }, timeout=300)
        except Exception as e:
            logger.warning(f"Mem0 add failed: {e}")
    threading.Thread(target=_add, daemon=True).start()


# ── oMLX helper ───────────────────────────────────────────────────────────────

def strip_thinking(text: str) -> str:
    """Strip <think>...</think> blocks and bare 'Thinking Process:' preambles."""
    text = re.sub(r"<think>.*?</think>\s*", "", text, flags=re.DOTALL)
    text = re.sub(r"^Thinking Process:.*?(?=\n[A-Z*#]|\Z)", "", text, flags=re.DOTALL)
    return text.strip()


def omlx_chat(messages: list, model: str = OMLX_MODEL) -> str:
    """Call oMLX via OpenAI-compatible /v1/chat/completions."""
    base_url = OMLX_URL.rstrip("/")
    payload = {
        "model": model,
        "messages": messages,
        "max_tokens": 2048,
        "temperature": 0.7,
        "chat_template_kwargs": {"enable_thinking": False},
    }
    req = urllib.request.Request(
        f"{base_url}/chat/completions",
        data=json.dumps(payload).encode(),
        method="POST"
    )
    req.add_header("Content-Type", "application/json")
    if OMLX_API_KEY:
        req.add_header("Authorization", f"Bearer {OMLX_API_KEY}")
    with urllib.request.urlopen(req, timeout=300) as resp:
        result = json.loads(resp.read().decode())
    return strip_thinking(result["choices"][0]["message"]["content"].strip())


# ── Paperclip helpers ─────────────────────────────────────────────────────────

def paperclip_get_issue(issue_id: str, api_url: str, api_key: str) -> Optional[dict]:
    try:
        return http_get(f"{api_url}/api/issues/{issue_id}", api_key)
    except Exception as e:
        logger.warning(f"Failed to fetch issue {issue_id}: {e}")
        return None


def paperclip_get_comments(issue_id: str, api_url: str, api_key: str) -> list:
    try:
        result = http_get(f"{api_url}/api/issues/{issue_id}/comments", api_key)
        return result if isinstance(result, list) else []
    except Exception as e:
        logger.warning(f"Failed to fetch comments for {issue_id}: {e}")
        return []


def paperclip_get_issue_documents(issue_id: str, api_url: str, api_key: str) -> str:
    """Fetch all issue documents (plan, intel briefs, etc.) and return their combined text."""
    try:
        docs = http_get(f"{api_url}/api/issues/{issue_id}/documents", api_key)
        if not isinstance(docs, list) or not docs:
            return ""
        parts = []
        for doc in docs:
            key   = doc.get("key", "")
            title = doc.get("title", key)
            body  = doc.get("body", "")
            if body:
                parts.append(f"### Document: {title}\n\n{body}")
        if not parts:
            return ""
        return "## Issue Documents\n\n" + "\n\n---\n\n".join(parts)
    except Exception as e:
        logger.warning(f"Failed to fetch issue documents for {issue_id}: {e}")
        return ""


def read_workspace_files(cwd: str, max_total_chars: int = 100000) -> str:
    """Read text files from a workspace directory, including subdirectories."""
    import pathlib
    workspace_path = pathlib.Path(cwd)
    if not workspace_path.exists() or not workspace_path.is_dir():
        logger.warning(f"Workspace path does not exist: {cwd}")
        return ""

    # Research formats: text, structured data, web
    text_extensions = {'.txt', '.md', '.rtf', '.text', '.csv', '.json', '.yaml', '.yml', '.html'}
    files_content = []
    total_chars = 0

    for f in sorted(workspace_path.rglob("*")):
        if not f.is_file():
            continue
        if f.suffix.lower() not in text_extensions:
            continue
        if any(part.startswith('.') for part in f.parts):
            continue
        try:
            content  = f.read_text(encoding='utf-8', errors='replace')
            rel_path = f.relative_to(workspace_path)
            if total_chars + len(content) > max_total_chars:
                remaining = max_total_chars - total_chars
                if remaining > 500:
                    content = content[:remaining] + "\n\n[... truncated ...]"
                    files_content.append(f"### {rel_path}\n\n{content}")
                break
            files_content.append(f"### {rel_path}\n\n{content}")
            total_chars += len(content)
        except Exception as e:
            logger.warning(f"Failed to read {f}: {e}")

    if not files_content:
        return ""
    return "## Project Files\n\n" + "\n\n---\n\n".join(files_content)


def paperclip_post_comment(issue_id: str, body: str, run_id: str, api_url: str, api_key: str) -> None:
    try:
        req = urllib.request.Request(
            f"{api_url}/api/issues/{issue_id}/comments",
            data=json.dumps({"body": body}).encode(),
            method="POST"
        )
        req.add_header("Content-Type", "application/json")
        req.add_header("Authorization", f"Bearer {api_key}")
        req.add_header("X-Paperclip-Run-Id", run_id)
        with urllib.request.urlopen(req, timeout=15) as resp:
            resp.read()
    except Exception as e:
        logger.warning(f"Failed to post comment to {issue_id}: {e}")


def paperclip_update_issue(issue_id: str, status: str, comment: str, run_id: str, api_url: str, api_key: str) -> None:
    try:
        data = json.dumps({"status": status, "comment": comment}).encode()
        req  = urllib.request.Request(f"{api_url}/api/issues/{issue_id}", data=data, method="PATCH")
        req.add_header("Content-Type", "application/json")
        req.add_header("Authorization", f"Bearer {api_key}")
        req.add_header("X-Paperclip-Run-Id", run_id)
        with urllib.request.urlopen(req, timeout=15) as resp:
            resp.read()
    except Exception as e:
        logger.warning(f"Failed to update issue {issue_id}: {e}")


# ── Analyst's system prompt ───────────────────────────────────────────────────

ANALYST_SYSTEM_PROMPT = """You are Analyst, a Research & Intelligence runner in a Paperclip multi-agent team.

Your role is to provide soft intelligence, stakeholder research, relationship mapping, and cultural context for complex advisory engagements. You are the human intelligence layer — you read between the lines, understand what doesn't appear in documents, and provide the contextual depth that makes strategic work land in the real world.

You are NOT just a research tool. You are a critical team member with capabilities no other runner possesses: understanding how individuals and institutions think, communicate, and make decisions in high-trust environments.

Core capabilities:
- Stakeholder intelligence: decision-making dynamics, who really decides vs. who holds the title, generational and cultural psychology of principals
- Soft due diligence: reputation assessment through network intelligence, relationship mapping, behavioral pattern recognition, cultural sensitivity
- Engagement support: advise on how to approach, engage, and retain key relationships; ensure deliverables land with the right tone
- Research and writing: internet research, structured intelligence briefings, stakeholder profiles, relationship maps

Personality:
- Precise and culturally attuned — how something is said matters as much as what is said
- Discreet — treat all client intelligence as privileged; never reference one client's information in the context of another
- Warm when appropriate, structured when the work demands it
- Honest about uncertainty — distinguish between confirmed intelligence and inference

Interaction style:
- Lead with the most actionable intelligence, then supporting context
- For stakeholder analysis: background, decision dynamics, risk flags, recommended approach
- Reference past engagement context naturally when relevant
- It's okay to say "I need more research on this" or "I don't have reliable intelligence on that"

What you do NOT do:
- Don't provide legal advice (flag for the Counsel runner)
- Don't write code or manage technical infrastructure
- Don't share intelligence across engagement boundaries without explicit approval
- Don't speculate as if it were confirmed intelligence — always label inferences clearly"""


# ── Core heartbeat handler ────────────────────────────────────────────────────

async def handle_heartbeat(body: dict) -> dict:
    agent_id = body.get("agentId", "")
    run_id   = body.get("runId", "")
    context  = body.get("context", {})

    api_key   = body.get("paperclipApiKey") or PAPERCLIP_API_KEY
    api_url   = body.get("paperclipApiUrl") or PAPERCLIP_API_URL
    model     = body.get("model") or OMLX_MODEL
    mem0_url  = body.get("mem0Url") or MEM0_URL
    mem0_user = body.get("mem0UserId") or MEM0_USER_ID

    issue_id        = context.get("issueId") or context.get("taskId")
    wake_reason     = context.get("wakeReason", "timer")
    wake_comment_id = context.get("wakeCommentId")

    logger.info(f"Heartbeat: agent={agent_id} run={run_id} issue={issue_id} wake={wake_reason}")

    if not api_key:
        logger.error("No Paperclip API key — cannot process heartbeat")
        return {"status": "error", "reason": "missing_api_key"}

    if not issue_id:
        logger.info("Timer wake with no issue — nothing to do")
        return {"status": "ok", "action": "idle"}

    # ── Fetch issue + comments ────────────────────────────────────────────────
    issue = paperclip_get_issue(issue_id, api_url, api_key)
    if not issue:
        return {"status": "ok", "action": "skipped", "reason": "issue_fetch_failed"}

    issue_title = issue.get("title", "")
    issue_desc  = issue.get("description", "")
    comments    = paperclip_get_comments(issue_id, api_url, api_key)

    # ── Read workspace files ──────────────────────────────────────────────────
    workspace_files_context = ""
    project_workspace_id = issue.get("projectWorkspaceId")
    if project_workspace_id:
        try:
            project_id = issue.get("projectId")
            if project_id:
                workspaces = http_get(f"{api_url}/api/projects/{project_id}/workspaces", api_key)
                if isinstance(workspaces, list):
                    for ws in workspaces:
                        if ws.get("id") == project_workspace_id and ws.get("cwd"):
                            workspace_files_context = read_workspace_files(ws["cwd"])
                            if workspace_files_context:
                                logger.info(f"Read {len(workspace_files_context)} chars from workspace")
                            break
        except Exception as e:
            logger.warning(f"Failed to read workspace files: {e}")

    # ── Read issue documents ──────────────────────────────────────────────────
    issue_documents_context = paperclip_get_issue_documents(issue_id, api_url, api_key)
    if issue_documents_context:
        logger.info(f"Read {len(issue_documents_context)} chars from issue documents")

    # ── Build conversation history ────────────────────────────────────────────
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
        if body_text.strip().startswith("HTTP ") and len(body_text.strip()) < 100:
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

    has_user_messages = any(m["role"] == "user" for m in conversation_history)

    if not triggering_message and not has_user_messages:
        triggering_message = f"New task: {issue_title}"
        if issue_desc:
            triggering_message += f"\n\n{issue_desc}"

    if not triggering_message:
        return {"status": "ok", "action": "skipped", "reason": "no_message"}

    # ── Mem0 recall ───────────────────────────────────────────────────────────
    memory_context = ""
    try:
        memories = mem0_search(triggering_message, user_id=mem0_user)
        if memories:
            memory_items = [m.get("memory", "") for m in memories if m.get("memory")]
            if memory_items:
                memory_context = "Relevant stakeholder/engagement memories:\n" + "\n".join(f"- {m}" for m in memory_items[:10])
    except Exception as e:
        logger.warning(f"Mem0 search failed: {e}")

    # ── Build messages ────────────────────────────────────────────────────────
    system_content = ANALYST_SYSTEM_PROMPT
    if memory_context:
        system_content += f"\n\n{memory_context}"
    if issue_documents_context:
        system_content += "\n\nThe following documents are attached to this task. Read them before providing analysis."
        system_content += f"\n\n{issue_documents_context}"
    if workspace_files_context:
        system_content += "\n\nYou have access to the following project files. Use their contents directly in your response."
        system_content += f"\n\n{workspace_files_context}"

    messages = [{"role": "system", "content": system_content}]

    # Last 20 messages — enough for full multi-step engagement continuity
    recent_history = conversation_history[-20:]
    messages.extend(recent_history)

    if not messages or messages[-1]["role"] != "user":
        messages.append({"role": "user", "content": triggering_message})

    # ── Call oMLX ─────────────────────────────────────────────────────────────
    try:
        response = omlx_chat(messages, model=model)
        logger.info(f"oMLX response ({len(response)} chars)")
    except Exception as e:
        logger.error(f"oMLX call failed: {e}")
        return {"status": "error", "reason": f"omlx_failed: {e}"}

    # ── Post response ─────────────────────────────────────────────────────────
    paperclip_post_comment(issue_id, response, run_id, api_url, api_key)

    # ── Save to Mem0 ──────────────────────────────────────────────────────────
    mem0_add([
        {"role": "user",      "content": triggering_message},
        {"role": "assistant", "content": response},
    ], user_id=mem0_user)

    logger.info(f"Heartbeat complete: posted response to issue {issue_id}")
    return {"status": "ok", "action": "responded", "issueId": issue_id}


# ── FastAPI app ───────────────────────────────────────────────────────────────

@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info(f"Analyst runner starting on port {PORT}")
    logger.info(f"  Paperclip API: {PAPERCLIP_API_URL}")
    logger.info(f"  oMLX:          {OMLX_URL} / {OMLX_MODEL or '(from env)'}")
    logger.info(f"  Mem0:          {MEM0_URL} (user={MEM0_USER_ID})")
    yield
    logger.info("Analyst runner shutting down")


app = FastAPI(
    title="Analyst Paperclip Runner",
    description="Bridges Paperclip heartbeats to oMLX + Mem0 for a Research & Intelligence agent",
    version="1.1.0",
    lifespan=lifespan,
)


@app.get("/health")
def health():
    return {"status": "ok", "service": "analyst-runner", "version": "1.1.0"}


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
        logger.error(f"Heartbeat handler error: {e}", exc_info=True)
        return JSONResponse(content={"status": "error", "reason": str(e)}, status_code=200)


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=PORT, log_level="info")
