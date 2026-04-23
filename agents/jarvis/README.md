# Jarvis — Personal Assistant Runner

Jarvis is a personal assistant that runs on two surfaces simultaneously:

- **Paperclip** — picks up assigned issues, calls oMLX, posts comments back (same pattern as other runners)
- **WhatsApp** — manages a Baileys bridge subprocess, polls for incoming messages, routes them through oMLX, replies via the bridge

```
Paperclip (heartbeat)          WhatsApp (polling)
       │                              │
       ▼ POST /heartbeat              ▼ GET bridge/messages (1s poll)
  Jarvis Runner (FastAPI :6126)
       │                              │
       ├── Paperclip API (comment)    ├── route_whatsapp_message()
       ├── mem0 (search + save)       ├── mem0 (search + save)
       └── oMLX (Qwen3.5-9B)         └── bridge POST /send
```

## Requirements

- Python 3.10+ with `fastapi` and `uvicorn[standard]` (from project root `requirements.txt`)
- Node.js (for the Baileys bridge subprocess)
- oMLX running at `localhost:8000/v1`
- mem0 running at `localhost:8050`
- Paperclip agent with `http` adapter

## Setup

### 1. Configure

```bash
cp config.env.example .env
# Edit .env — set JARVIS_PAPERCLIP_API_KEY and OMLX_API_KEY
```

### 2. Run

From the project root:

```bash
source agents/jarvis/.env
uvicorn runner:app --host 0.0.0.0 --port 6126 --app-dir agents/jarvis
```

Check the health endpoint:

```bash
curl http://localhost:6126/health
```

### 3. Pair WhatsApp (first time only)

On first run the bridge will connect to WhatsApp but find no saved credentials.
It will print a QR code to the bridge log. Scan it with the WhatsApp mobile app:

```bash
# Tail the bridge log
tail -f ~/.paperclip/runners/jarvis/bridge.log
```

After scanning, the session is saved to `~/.paperclip/runners/jarvis/whatsapp-session/`.
Subsequent starts will reconnect automatically without scanning.

> **Note:** The Jarvis runner uses its own session directory, separate from the old
> Hermes Jarvis gateway. The old `ai.hermes.gateway-jarvis.plist` should be unloaded
> once this runner is confirmed working.

### 4. Register in Paperclip

Create or update the Jarvis agent:
- **Adapter type:** `http`
- **Heartbeat URL:** `http://localhost:6126/heartbeat`

## macOS LaunchAgent

A ready-to-use plist is included at `launchagent.plist`. Fill in the placeholders
(API keys), then:

```bash
cp agents/jarvis/launchagent.plist ~/Library/LaunchAgents/ing.paperclip.jarvis-runner.plist
# Edit ~/Library/LaunchAgents/ing.paperclip.jarvis-runner.plist — set API keys
launchctl load ~/Library/LaunchAgents/ing.paperclip.jarvis-runner.plist
```

View logs:

```bash
tail -f ~/jarvis-runner.log
tail -f ~/.paperclip/runners/jarvis/bridge.log
```

Restart:

```bash
launchctl stop ing.paperclip.jarvis-runner && launchctl start ing.paperclip.jarvis-runner
```

## Endpoints

| Endpoint | Description |
|----------|-------------|
| `GET /health` | Service + bridge health check |
| `POST /heartbeat` | Paperclip HTTP adapter endpoint |
| `POST /whatsapp/webhook` | Inbound WhatsApp message webhook (push-compatible) |
| `POST /whatsapp/send` | Internal: send a WhatsApp message `{chatId, message, replyTo?}` |

## Architecture Notes

### WhatsApp bridge

The runner reuses the existing Hermes Baileys bridge at
`~/.hermes/hermes-agent/scripts/whatsapp-bridge/bridge.js`. This bridge:
- Connects to WhatsApp Web via Baileys
- Exposes `GET /messages` (drain queue), `POST /send`, `GET /health`
- Stores session credentials in `WHATSAPP_SESSION_DIR`

The runner starts the bridge as a subprocess on startup and polls it every second
for new messages.

### WhatsApp mode

- `self-chat` — Jarvis uses Johannes's own number. Sent messages appear as self-messages
  (Saved Messages). Great for a personal assistant pattern.
- `bot` — Jarvis uses a dedicated linked device number. Messages appear from a
  second account.

### Conversation history

Per-chat history is kept in memory (last 20 turns) and not persisted across restarts.
Long-term memory is handled by mem0 (searched on every incoming message, saved
after every reply).

### Old Hermes gateway

Once this runner is stable, unload the old Hermes Jarvis LaunchAgent:

```bash
launchctl unload ~/Library/LaunchAgents/ai.hermes.gateway-jarvis.plist
```
