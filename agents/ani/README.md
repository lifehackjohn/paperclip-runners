# Ani — Personal Companion Runner

Ani is a warm, conversational AI companion. She is not a task agent — she provides emotional support, thoughtful conversation, and memory-aware personal engagement.

## Agent Type

- **Role:** Personal companion
- **Tone:** Conversational, warm, gently honest
- **Memory:** Yes — Ani references past conversations to maintain continuity
- **Web access:** Yes (optional — for lookups when explicitly asked)
- **File access:** Yes (can read shared documents when directed)
- **Code/task work:** No

## Setup

### 1. Copy the generic runner

```bash
cp ../../runner.py ani-runner.py
```

### 2. Configure

Copy `config.env.example` to `.env` and fill in your values:

```bash
cp config.env.example .env
```

Key settings:
- `RUNNER_PORT=6123` — default port; change if you run multiple agents on the same machine
- `MEM0_USER_ID=ani` — keeps Ani's memories separate from other agents

### 3. Set the system prompt

In `ani-runner.py`, replace the `SYSTEM_PROMPT` constant with the contents of `system-prompt.md`.

### 4. Run

```bash
source .env
uvicorn ani-runner:app --host 0.0.0.0 --port $RUNNER_PORT
```

Or as a background service (see `launchagent.plist.template` in the project root).

### 5. Register in Paperclip

Create an agent with:
- **Adapter type:** `http`
- **Heartbeat URL:** `http://localhost:6123/heartbeat`

## macOS LaunchAgent

Use the template at `../../launchagent.plist.template`. Set:
- `Label` → `ing.paperclip.ani`
- `ProgramArguments` → point to your virtual env's `uvicorn` and `ani-runner:app`
- `StandardOutPath` / `StandardErrorPath` → `~/ani-runner.log`
- Required env vars: `PAPERCLIP_API_URL`, `PAPERCLIP_API_KEY`, `OLLAMA_URL`, `OLLAMA_MODEL`
