# Lex — Legal Expert Runner

Lex is a partner-level legal AI specialising in international commercial law, private law, and asset protection. She provides structured legal analysis, risk assessment, and practical counsel.

## Agent Type

- **Role:** Legal counsel
- **Tone:** Precise, rigorous, practical — conclusions first
- **Memory:** Yes — maintains continuity across client matters
- **Web access:** Yes (for case law lookups, regulatory research)
- **File access:** Yes (reads contracts, briefs, legal documents)
- **Code/task work:** No

## Setup

### 1. Copy the generic runner

```bash
cp ../../runner.py lex-runner.py
```

### 2. Configure

Copy `config.env.example` to `.env` and fill in your values:

```bash
cp config.env.example .env
```

Key settings:
- `RUNNER_PORT=6124` — default port; change if needed
- `MEM0_USER_ID=lex` — keeps Lex's memories separate from other agents

### 3. Set the system prompt

In `lex-runner.py`, replace the `SYSTEM_PROMPT` constant with the contents of `system-prompt.md`.

### 4. Run

```bash
source .env
uvicorn lex-runner:app --host 0.0.0.0 --port $RUNNER_PORT
```

Or as a background service (see `launchagent.plist.template` in the project root).

### 5. Register in Paperclip

Create an agent with:
- **Adapter type:** `http`
- **Heartbeat URL:** `http://localhost:6124/heartbeat`

## macOS LaunchAgent

Use the template at `../../launchagent.plist.template`. Set:
- `Label` → `ing.paperclip.lex`
- `ProgramArguments` → point to your virtual env's `uvicorn` and `lex-runner:app`
- `StandardOutPath` / `StandardErrorPath` → `~/lex-runner.log`
- Required env vars: `PAPERCLIP_API_URL`, `PAPERCLIP_API_KEY`, `OLLAMA_URL`, `OLLAMA_MODEL`

## Model Recommendations

Legal analysis benefits from models with strong reasoning and long-context capability:
- **Qwen3.5-9B** (oMLX, 4-bit) — solid general-purpose, fast on Apple Silicon
- **Qwen2.5-72B** (if available) — better for complex multi-jurisdictional analysis
- Any model with ≥16K context is recommended for document review tasks
