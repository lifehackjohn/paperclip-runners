#!/usr/bin/env python3
"""
Paperclip Runner — generic HTTP adapter runner template.

Bridges Paperclip's http adapter heartbeat calls to a local LLM (Ollama or
any OpenAI-compatible endpoint) with optional Mem0 persistent memory.

Paperclip's http adapter POSTs:
  { agentId, runId, context, ...payloadTemplate }

This runner:
1. Reads the assigned issue + comments from the Paperclip API
2. Searches Mem0 for relevant past memories  (optional)
3. Reads workspace files if a project workspace is attached
4. Builds a prompt and generates a response via the LLM
5. Posts the response back to Paperclip as a comment
6. Saves the conversation to Mem0

Quickstart:
  pip install -r requirements.txt
  uvicorn runner:app --host 0.0.0.0 --port 6200
"""

import json
import logging
import os
import urllib.request
import urllib.error
from contextlib import asynccontextmanager
from typing import Any, Optional

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s"
)
logger = logging.getLogger("paperclip-runner")

# ── Config — override via environment variables ───────────────────────────────

PAPERCLIP_API_URL = os.environ.get("PAPERCLIP_API_URL", "http://localhost:3100")
PAPERCLIP_API_KEY = os.environ.get("PAPERCLIP_API_KEY", "")
OLLAMA_URL        = os.environ.get("OLLAMA_URL",  "http://localhost:11434")
OLLAMA_MODEL      = os.environ.get("OLLAMA_MODEL", "llama3.2")
OLLAMA_API_KEY    = os.environ.get("OLLAMA_API_KEY", "")   # required for oMLX/LM Studio
MEM0_URL          = os.environ.get("MEM0_URL",  "http://localhost:8050")
MEM0_USER_ID      = os.environ.get("MEM0_USER_ID", "my-agent")
PORT              = int(os.environ.get("RUNNER_PORT", "6200"))


# ── System prompt — edit this to define your agent's persona ─────────────────

SYSTEM_PROMPT = """You are a helpful assistant integrated with Paperclip.

You receive task descriptions and conversation threads from Paperclip issues.
Your job is to respond thoughtfully, helpfully, and concisely.

Guidelines:
- Read the full conversation before responding
- Be direct and actionable
- If you cannot help with something, say so clearly
- Keep responses focused on the task at hand"""


# ── HTTP helpers ──────────────────────────────────────────────────────────────

def http_get(url: str, api_key: str = "") -> Any:
    req = urllib.request.Request(url)
    if api_key:
        req.add_header("Authorization", f"Bearer {api_key}")
    with urllib.request.urlopen(req, timeout=15) as resp:
        return json.loads(resp.read().decode())


def http_post(url: str, payload: dict, api_key: str = "") -> Any:
    data = json.dumps(payload).encode()
    req = urllib.request.Request(url, data=data, method="POST")
    req.add_header("Content-Type", "application/json")
    if api_key:
        req.add_header("Authorization", f"Bearer {api_key}")
    with urllib.request.urlopen(req, timeout=60) as resp:
        return json.loads(resp.read().decode())


# ── Mem0 helpers (optional) ───────────────────────────────────────────────────

def mem0_search(query: str, user_id: str = MEM0_USER_ID, limit: int = 8) -> list:
    """Search Mem0 for relevant memories. Returns empty list if Mem0 is unavailable."""
    try:
        result = http_post(f"{MEM0_URL}/v1/memories/search/", {
            "query": query,
            "user_id": user_id,
            "limit": limit,
        })
        return result.get("results", []) if isinstance(result, dict) else result
    except Exception as e:
        logger.debug(f"Mem0 search skipped: {e}")
        return []


def mem0_add(messages: list, user_id: str = MEM0_USER_ID) -> None:
    """Save a conversation turn to Mem0. Silent no-op if Mem0 is unavailable."""
    try:
        http_post(f"{MEM0_URL}/v1/memories/", {
            "messages": messages,
            "user_id": user_id,
        })
    except Exception as e:
        logger.debug(f"Mem0 add skipped: {e}")


# ── LLM helpers ───────────────────────────────────────────────────────────────

def llm_chat(messages: list, model: str = OLLAMA_MODEL) -> str:
    """
    Call the configured LLM. Supports:
    - OpenAI-compatible endpoints (oMLX, LM Studio, vLLM): OLLAMA_URL ends with /v1
    - Native Ollama: OLLAMA_URL is the base Ollama URL (e.g. http://localhost:11434)
    """
    base_url = OLLAMA_URL.rstrip("/")

    if "/v1" in base_url:
        # OpenAI-compatible path
        payload = {
            "model": model,
            "messages": messages,
            "max_tokens": 1024,
            "temperature": 0.7,
        }
        req = urllib.request.Request(
            f"{base_url}/chat/completions",
            data=json.dumps(payload).encode(),
            method="POST"
        )
        req.add_header("Content-Type", "application/json")
        if OLLAMA_API_KEY:
            req.add_header("Authorization", f"Bearer {OLLAMA_API_KEY}")
        with urllib.request.urlopen(req, timeout=300) as resp:
            result = json.loads(resp.read().decode())
        return result["choices"][0]["message"]["content"].strip()

    else:
        # Native Ollama path
        payload = {
            "model": model,
            "messages": messages,
            "stream": False,
            "options": {"temperature": 0.7, "num_predict": 1024, "num_ctx": 16384},
        }
        req = urllib.request.Request(
            f"{base_url}/api/chat",
            data=json.dumps(payload).encode(),
            method="POST"
        )
        req.add_header("Content-Type", "application/json")
        with urllib.request.urlopen(req, timeout=300) as resp:
            result = json.loads(resp.read().decode())
        return result.get("message", {}).get("content", "").strip()


# ── Workspace file reader ──────────────────────────────────────────────────────

def read_workspace_files(cwd: str, max_total_chars: int = 30000) -> str:
    """
    Read text files from a Paperclip project workspace directory.
    Returns a formatted string for inclusion in the LLM prompt.
    """
    import pathlib
    workspace_path = pathlib.Path(cwd)
    if not workspace_path.exists() or not workspace_path.is_dir():
        return ""

    # Extend this set to include other formats your agent needs
    text_extensions = {".txt", ".md", ".rtf", ".text", ".csv"}
    files_content = []
    total_chars = 0

    for f in sorted(workspace_path.iterdir()):
        if not f.is_file() or f.suffix.lower() not in text_extensions:
            continue
        try:
            content = f.read_text(encoding="utf-8", errors="replace")
            if total_chars + len(content) > max_total_chars:
                remaining = max_total_chars - total_chars
                if remaining > 500:
                    content = content[:remaining] + "\n\n[... truncated ...]"
                else:
                    break
            files_content.append(f"### {f.name}\n\n{content}")
            total_chars += len(content)
        except Exception as e:
            logger.warning(f"Failed to read {f}: {e}")

    if not files_content:
        return ""
    return "## Project Files\n\n" + "\n\n---\n\n".join(files_content)


# ── Paperclip API helpers ─────────────────────────────────────────────────────

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


# ── Core heartbeat handler ────────────────────────────────────────────────────

async def handle_heartbeat(body: dict) -> dict:
    agent_id = body.get("agentId", "")
    run_id   = body.get("runId", "")
    context  = body.get("context", {})

    # Allow config overrides from Paperclip's payloadTemplate
    api_key  = body.get("paperclipApiKey") or PAPERCLIP_API_KEY
    api_url  = body.get("paperclipApiUrl") or PAPERCLIP_API_URL
    model    = body.get("model") or OLLAMA_MODEL
    mem0_url = body.get("mem0Url") or MEM0_URL
    mem0_user = body.get("mem0UserId") or MEM0_USER_ID

    issue_id       = context.get("issueId") or context.get("taskId")
    wake_comment_id = context.get("wakeCommentId")

    logger.info(f"Heartbeat: agent={agent_id} run={run_id} issue={issue_id} wake={context.get('wakeReason', 'timer')}")

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

    # ── Read workspace files if available ─────────────────────────────────────
    workspace_context = ""
    project_workspace_id = issue.get("projectWorkspaceId")
    if project_workspace_id:
        try:
            project_id = issue.get("projectId")
            if project_id:
                workspaces = http_get(f"{api_url}/api/projects/{project_id}/workspaces", api_key)
                if isinstance(workspaces, list):
                    for ws in workspaces:
                        if ws.get("id") == project_workspace_id and ws.get("cwd"):
                            workspace_context = read_workspace_files(ws["cwd"])
                            break
        except Exception as e:
            logger.warning(f"Failed to read workspace: {e}")

    # ── Build conversation history from comments ───────────────────────────────
    triggering_message = None
    conversation_history = []

    for comment in comments:
        author_agent_id = comment.get("authorAgentId")
        author_user_id  = comment.get("authorUserId")
        body_text       = comment.get("body", "")

        if not body_text:
            continue
        # Skip system/control-plane comments (no author)
        if not author_agent_id and not author_user_id:
            continue

        if author_agent_id == agent_id:
            conversation_history.append({"role": "assistant", "content": body_text})
        else:
            conversation_history.append({"role": "user", "content": body_text})
            if wake_comment_id and comment.get("id") == wake_comment_id:
                triggering_message = body_text

    # Fall back to the most recent user message
    if not triggering_message:
        for msg in reversed(conversation_history):
            if msg["role"] == "user":
                triggering_message = msg["content"]
                break

    # If no user messages at all, use the issue description as the initial prompt
    if not triggering_message and not any(m["role"] == "user" for m in conversation_history):
        triggering_message = f"New task: {issue_title}"
        if issue_desc:
            triggering_message += f"\n\n{issue_desc}"

    if not triggering_message:
        logger.info("No message to respond to")
        return {"status": "ok", "action": "skipped", "reason": "no_message"}

    # ── Search Mem0 for relevant memories ─────────────────────────────────────
    memory_context = ""
    memories = mem0_search(triggering_message, user_id=mem0_user)
    if memories:
        items = [m.get("memory", "") for m in memories if m.get("memory")]
        if items:
            memory_context = "Relevant memories:\n" + "\n".join(f"- {m}" for m in items[:6])

    # ── Build LLM messages ────────────────────────────────────────────────────
    system_content = SYSTEM_PROMPT
    if memory_context:
        system_content += f"\n\n{memory_context}"
    if workspace_context:
        system_content += "\n\nThe following files are from the project workspace:\n\n" + workspace_context

    messages = [{"role": "system", "content": system_content}]
    # Keep last 10 turns of conversation history to stay within context limits
    messages.extend(conversation_history[-10:])

    # Ensure the last message is from the user
    if not messages or messages[-1]["role"] != "user":
        messages.append({"role": "user", "content": triggering_message})

    # ── Call LLM ──────────────────────────────────────────────────────────────
    try:
        response = llm_chat(messages, model=model)
        logger.info(f"LLM response ({len(response)} chars)")
    except Exception as e:
        logger.error(f"LLM call failed: {e}")
        return {"status": "error", "reason": f"llm_failed: {e}"}

    # ── Post response to Paperclip ────────────────────────────────────────────
    paperclip_post_comment(issue_id, response, run_id, api_url, api_key)

    # ── Save conversation to Mem0 ─────────────────────────────────────────────
    mem0_add(
        [
            {"role": "user", "content": triggering_message},
            {"role": "assistant", "content": response},
        ],
        user_id=mem0_user,
    )

    logger.info(f"Heartbeat complete: responded to issue {issue_id}")
    return {"status": "ok", "action": "responded", "issueId": issue_id}


# ── FastAPI app ───────────────────────────────────────────────────────────────

@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info(f"Paperclip runner starting on port {PORT}")
    logger.info(f"  Paperclip: {PAPERCLIP_API_URL}")
    logger.info(f"  LLM:       {OLLAMA_URL} / {OLLAMA_MODEL}")
    logger.info(f"  Mem0:      {MEM0_URL} (user={MEM0_USER_ID})")
    yield
    logger.info("Runner shutting down")


app = FastAPI(
    title="Paperclip Runner",
    description="HTTP adapter runner bridging Paperclip heartbeats to a local LLM + Mem0",
    version="1.0.0",
    lifespan=lifespan,
)


@app.get("/health")
def health():
    return {"status": "ok", "service": "paperclip-runner", "version": "1.0.0"}


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
