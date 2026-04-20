# Paperclip Runners

Lightweight HTTP adapter runners that bridge [Paperclip](https://paperclip.ing) heartbeat calls to a local LLM (Ollama or any OpenAI-compatible endpoint) and an optional memory layer (Mem0).

## What is a Paperclip Runner?

Paperclip's `http` adapter fires a `POST /heartbeat` to your runner on every agent wake cycle. A runner is a small FastAPI service that:

1. Fetches the assigned issue + comment thread from the Paperclip API
2. (Optionally) retrieves relevant memories from Mem0
3. (Optionally) reads workspace files attached to the issue's project
4. Builds a prompt and calls your local LLM
5. Posts the LLM's response back to the Paperclip issue as a comment
6. Saves the conversation to Mem0 for future context

This pattern lets you run **fully local, private agents** inside Paperclip — no cloud inference, no data leaving your machine.

```
Paperclip (control plane)
    │
    ▼  POST /heartbeat
Your Runner (FastAPI on localhost)
    │
    ├── Paperclip API  (read issue, post comment)
    ├── Mem0           (optional — search + save memories)
    └── Ollama / oMLX  (local LLM inference)
```

## Requirements

- Python 3.10+
- [Paperclip](https://paperclip.ing) with an `http` adapter agent
- [Ollama](https://ollama.com) or any OpenAI-compatible LLM server
- [Mem0](https://github.com/mem0ai/mem0) (optional, for persistent memory)

```
pip install -r requirements.txt
```

## Quick Start

### 1. Configure your runner

Copy `runner.py` and set your env vars (or edit the defaults at the top of the file):

```bash
export PAPERCLIP_API_URL=http://localhost:3100
export PAPERCLIP_API_KEY=your_paperclip_api_key
export OLLAMA_URL=http://localhost:11434         # or oMLX: http://localhost:8000/v1
export OLLAMA_MODEL=llama3.2
export MEM0_URL=http://localhost:8050            # optional
```

### 2. Write your system prompt

Edit `SYSTEM_PROMPT` in `runner.py` to define your agent's persona and capabilities.

### 3. Run

```bash
uvicorn runner:app --host 0.0.0.0 --port 6200
```

Check the health endpoint:
```bash
curl http://localhost:6200/health
```

### 4. Register in Paperclip

In Paperclip, create or update an agent with adapter type `http` and set the heartbeat URL to `http://localhost:6200/heartbeat`.

## Running as a macOS Service (LaunchAgent)

A template plist is included at `launchagent.plist.template`. Copy it to `~/Library/LaunchAgents/` and edit the placeholders, then:

```bash
launchctl load ~/Library/LaunchAgents/ing.paperclip.my-runner.plist
```

Restart:
```bash
launchctl stop ing.paperclip.my-runner && launchctl start ing.paperclip.my-runner
```

View logs:
```bash
tail -f ~/my-runner.log
```

## Architecture Notes

### OpenAI-compatible vs. legacy Ollama

The runner auto-detects which format to use based on the `OLLAMA_URL`:
- If it contains `/v1` → uses OpenAI-compatible `/chat/completions` endpoint (works with oMLX, LM Studio, vLLM, etc.)
- Otherwise → uses the native Ollama `/api/chat` endpoint

### Workspace files

If a Paperclip project has a workspace with a local `cwd` path, the runner reads text files from that directory and includes them in the prompt. Supported extensions are configurable.

### Memory (Mem0)

Memories are searched before each response and saved after. The `MEM0_USER_ID` namespaces memories per agent.

## Customization

The runner is intentionally minimal and easy to extend:

| What to change | Where |
|---|---|
| Agent persona | `SYSTEM_PROMPT` constant |
| LLM parameters (temperature, max tokens) | `ollama_chat()` function |
| Which file types to read | `text_extensions` in `read_workspace_files()` |
| Memory search limit | `mem0_search()` call in `handle_heartbeat()` |
| Conversation history window | `conversation_history[-10:]` slice |

## License

MIT
