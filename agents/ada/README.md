# Ada — Infrastructure & Operations Runner

Ada is a local infrastructure monitoring agent. She checks system health, service status, logs, and storage — and surfaces issues that need attention. No web access required.

## Agent Type

- **Role:** Infrastructure & operations monitoring
- **Tone:** Direct, systematic, low-noise
- **Memory:** Optional — useful for tracking recurring issues
- **Web access:** No — all checks are local/LAN
- **File access:** Yes — reads logs, config files, system state
- **Terminal access:** Yes — runs health-check commands (read-only by default)

## Setup

### 1. Copy the generic runner

```bash
cp ../../runner.py ada-runner.py
```

### 2. Configure

Copy `config.env.example` to `.env` and fill in your values:

```bash
cp config.env.example .env
```

Key settings:
- `RUNNER_PORT=6125` — default port
- `MEM0_USER_ID=ada` — namespaced memory for infra issues

### 3. Set the system prompt

In `ada-runner.py`, replace the `SYSTEM_PROMPT` constant with the contents of `system-prompt.md`.

### 4. Run

```bash
source .env
uvicorn ada-runner:app --host 0.0.0.0 --port $RUNNER_PORT
```

### 5. Register in Paperclip

Create an agent with:
- **Adapter type:** `http` (or `hermes_local` for tool-use capability)
- **Heartbeat URL:** `http://localhost:6125/heartbeat`
- **Toolsets:** `terminal,file` only — do NOT enable `web` or `browser`

## Recommended Paperclip Toolsets

```
terminal,file
```

Ada does not need internet access. Keeping her toolset minimal reduces risk surface.

## Suggested Health Check Prompts

Assign Ada tasks like:

- "Check disk usage on all mounted volumes and alert if any volume is above 85%"
- "Verify all expected services are running and report any that are down"
- "Review system logs from the last 24 hours for errors or anomalies"
- "Confirm scheduled backup jobs ran successfully last night"

## macOS LaunchAgent

Use the template at `../../launchagent.plist.template`. Set:
- `Label` → `ing.paperclip.ada`
- `ProgramArguments` → point to your virtual env's `uvicorn` and `ada-runner:app`
- `StandardOutPath` / `StandardErrorPath` → `~/ada-runner.log`
