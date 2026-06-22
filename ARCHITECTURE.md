# Architecture — MHLG Swarm Resonance Engine  (v0.2)

---

## Overview

The system has **two interchangeable backends** for the same Astro frontend.
Both listen at `ws://127.0.0.1:8080/ws` and speak the same wire protocol.

```
                    ┌──────────────────────────────────┐
                    │       HUMAN GAMER                │
                    │  types ONE prompt in the UI      │
                    └────────────────┬─────────────────┘
                                     │ WebSocket send
                                     ▼
                    ┌──────────────────────────────────┐
                    │   ASTRO FRONTEND  :4321          │
                    │   src/components/MosaicGrid.astro│
                    │   • 3×3 grid of streaming cells  │
                    │   • Vanilla JS WS client         │
                    │   • Per-cell typing indicator    │
                    │   • Auto-reconnect on close      │
                    └────────────────┬─────────────────┘
                                     │ ws://127.0.0.1:8080/ws
                                     │ (same address for both backends)
                   ┌─────────────────┴──────────────────┐
                   │                                    │
         ┌─────────▼──────────┐             ┌──────────▼─────────┐
         │  RUST BACKEND      │    ─ OR ─   │  PYTHON BACKEND    │
         │  cargo run         │             │  mhlg_cli.py       │
         │  --release         │             │  --serve           │
         └─────────┬──────────┘             └──────────┬─────────┘
                   │                                   │
                   └──────────────┬────────────────────┘
                                  │ HTTP POST /api/chat
                                  │ (blocking, NDJSON stream)
                                  ▼
                    ┌──────────────────────────────────┐
                    │   OLLAMA  :11434                 │
                    │   gemma4:e4b                     │
                    │   <|think|> reasoning channel    │
                    │   4.5 B effective parameters     │
                    └──────────────────────────────────┘
```

---

## Full Data Flow (per prompt)

```
Human types prompt ──► ws.send({"prompt": "..."})
                                │
                       ┌────────▼────────┐
                       │  parse payload  │
                       └────────┬────────┘
                                │
               build_context()  │  rotating slice of all JSON entries
               (prime-step 13)  │  ~12 entries, max 3000 chars
                                │
              ┌─────────────────┼─────────────────────────┐
              │    9 AGENTS START CONCURRENTLY             │
              │                                            │
              │  F1A Arquitecto  ──► Ollama NDJSON stream  │
              │  F1B Pragmático  ──► Ollama NDJSON stream  │
              │  F1C Explorador  ──► Ollama NDJSON stream  │
              │  F2A Traductor   ──► Ollama NDJSON stream  │
              │  F2B Catalizador ──► Ollama NDJSON stream  │
              │  F2C Orquestador ──► Ollama NDJSON stream  │
              │  F3A Socrático   ──► Ollama NDJSON stream  │
              │  F3B Ciberseguro ──► Ollama NDJSON stream  │
              │  F3C Ab. Diablo  ──► Ollama NDJSON stream  │
              │                 │                          │
              │  each token ────► channel / queue          │
              │                 │                          │
              └─────────────────┼──────────────────────────┘
                                │
                    forward task / drain coroutine
                                │
                    ws.send({"cell_id": N, "token": "...", "done": false})
                    ws.send({"cell_id": N, "token": "",    "done": true })
                                │
                    9 cells fill independently in real-time
```

---

## Wire Protocol

Every WebSocket message from backend → frontend is a JSON object:

```json
{ "cell_id": 4, "token": "hybrid", "done": false }
{ "cell_id": 4, "token": "",       "done": true  }
```

| Field | Type | Description |
|-------|------|-------------|
| `cell_id` | `0–8` | Maps to the grid position (row-major, see matrix below) |
| `token` | `string` | Text fragment to append to the cell content |
| `done` | `bool` | `true` = agent finished; hides typing indicator, unlocks cell |

Incoming message from frontend → backend:

```json
{ "prompt": "¿Cómo diseñarías un sistema de morfogénesis distribuida?" }
```

---

## 3×3 Agent Matrix

```
          Col A                 Col B                  Col C
    ┌───────────────────┬───────────────────┬───────────────────┐
    │  F1A · Arquitecto │  F1B · Pragmático │  F1C · Explorador │ ← TESIS
    │  cell_id = 0      │  cell_id = 1      │  cell_id = 2      │   Expansion
    ├───────────────────┼───────────────────┼───────────────────┤
    │  F2A · Traductor  │  F2B · Catalizador│  F2C · Orquestador│ ← SÍNTESIS
    │  cell_id = 3      │  cell_id = 4      │  cell_id = 5      │   Conexión
    ├───────────────────┼───────────────────┼───────────────────┤
    │  F3A · Socrático  │  F3B · Ciberseguro│  F3C · Ab. Diablo │ ← ANTÍTESIS
    │  cell_id = 6      │  cell_id = 7      │  cell_id = 8      │   Restricción
    └───────────────────┴───────────────────┴───────────────────┘
```

Each agent receives the **same user prompt** but a different `<|think|>` system directive,
producing 9 orthogonal perspectives simultaneously.

---

## Rust Backend Detail  (`src/backend/main.rs` — v0.3)

### Startup

```
main()
  └─ load_json_entries()
       ├─ glob("./*.json")  — ALL files, including mhlg_ollama_dataset.json
       ├─ take up to 5 entries per file
       └─ store as Arc<Vec<serde_json::Value>>  in AppState
```

### WebSocket message loop (`tokio::select!`)

```
actix_web::rt::spawn(async move {
  loop {
    tokio::select! {
      biased;

      // ① Incoming message — highest priority
      msg = msg_stream.next() => {
        Text(prompt) → tokio::spawn(run_inference(...))  // ← returns immediately
        Ping(b)      → session.pong(&b)                  // ← always handled
        Close | None → break
      }

      // ② Heartbeat every 20 s — keeps browser connection alive
      _ = heartbeat.tick() => {
        session.ping(&Bytes::from_static(b"mhlg"))
        // if ping fails → client gone → break
      }
    }
  }
})
```

> **Why `biased;`?**  Without it, `tokio::select!` randomly chooses between
> ready branches.  `biased;` always tries branches top-to-bottom, so a
> user message is always preferred over a heartbeat tick.

### Per-request concurrency (inside `run_inference`)

```
tokio::spawn(run_inference(...))          ← own task, never blocks message loop
│
├─ build_context(&entries, &request_count)
│    offset = (count × 13) % n           ← prime-step rotation (lock-free)
│    sample 12 entries, cap 3 000 chars
│
├─ mpsc::unbounded_channel::<String>()
│    └─ forward task: rx.recv() → ws_session.text()
│
└─ for each of 9 agents:
     tokio::spawn(run_agent(...))         ← fully async, no blocking pool
          │
          ├─ reqwest::Client (async, not blocking)
          │    connect_timeout : 10 s
          │    total timeout  : 600 s
          ├─ POST /api/chat  { stream: true }
          ├─ body.bytes_stream() → remainder buffer → NDJSON lines
          └─ emit(tx, cell_id, token, done)

After join_all(handles): drop(Arc<Sender>) → channel closes → forward exits
```

### v0.1 → v0.2 → v0.3 progression

| Issue | v0.1 | v0.2 | v0.3 |
|-------|------|------|------|
| Concurrency primitive | `rayon::scope` (blocks executor) | `spawn_blocking` (blocking pool) | `tokio::spawn` (async, no pool) |
| Message loop during inference | Blocked — cannot respond to pings | Blocked — cannot respond to pings | **Free** — inference in separate task |
| Heartbeat | None | None | **20 s server ping** |
| HTTP client | `reqwest::blocking` | `reqwest::blocking` | **`reqwest::Client` async** |
| NDJSON parsing | Single-line assumption | Single-line assumption | **Remainder buffer** (handles partial chunks) |
| Context | Frozen `Arc<String>` at startup | Rotating slice | Rotating slice |
| Dataset | Excluded | Included | Included |

---

## Python Backend Detail  (`mhlg_cli.py`)

### Terminal mode  (`python mhlg_cli.py`)

```
run_terminal()
│
└─ fire_swarm_terminal(prompt, context, model)
     └─ ThreadPoolExecutor(max_workers=9)
          ├─ Thread 0 → run_agent_thread(F1A) → ollama.chat(stream=True) → print THESIS color
          ├─ Thread 1 → run_agent_thread(F1B) → ollama.chat(stream=True) → print THESIS color
          │   ...
          └─ Thread 8 → run_agent_thread(F3C) → ollama.chat(stream=True) → print ANTI color
```

### Server mode  (`python mhlg_cli.py --serve`)

Starts a Python WebSocket server at `ws://127.0.0.1:8080/ws`.
The Astro frontend connects to it identically to the Rust backend.

```
asyncio.run(serve_mode())
│
└─ websockets.serve(handle_client, "127.0.0.1", 8080)
     │
     └─ handle_client(websocket):
          │
          ├─ receive {"prompt": "..."}
          │
          ├─ asyncio.Queue()  ← bridge between threads and async
          │
          ├─ for each of 9 agents:
          │    threading.Thread(target=run_agent_thread,
          │                     kwargs={..., on_token=make_on_token(agent_id)})
          │         │
          │         └─ on_token(id, token, done):
          │              loop.call_soon_threadsafe(queue.put_nowait, json_packet)
          │
          └─ drain loop:
               pkt = await asyncio.wait_for(queue.get(), timeout=350)
               await websocket.send(pkt)
               if pkt["done"]: done_count += 1
               exit when done_count == 9
```

The `loop.call_soon_threadsafe()` bridge is the key: it safely crosses the
thread boundary from a sync `threading.Thread` into the running `asyncio` event loop.

### Token access — ollama 0.6.x compatibility

```python
def _extract_token(chunk) -> str:
    try:
        return chunk.message.content   # Pydantic ChatResponse (ollama ≥0.4)
    except AttributeError:
        return chunk["message"]["content"]  # dict fallback (older versions)
```

---

## JSON Memory Injection Strategy  (v0.2)

```
All *.json files in the repo root
         │
         ├─ biomimetic-language.json
         ├─ future-figures-projected.json
         ├─ language-gamers-learners-*.json  (x22 files)
         ├─ linguistic-mapping-games.json
         ├─ silice-language.json
         └─ mhlg_ollama_dataset.json  ← included now (excluded in v0.1)
         │
         ▼
  load_json_entries() / load_all_json_context()
         │
         ├─ max 5 entries per file
         ├─ ~615+ total synapse entries in pool
         └─ Arc<Vec<Value>> / list — live in memory for the session lifetime

Per request:
  offset = (request_count × 13) % total_entries   ← rotates across calls
  sample = entries[offset … offset+12]             ← 12 entries, wraps around
  context = json.dumps(sample)[:3000]              ← injected into all 9 system prompts
```

The `<|think|>` prefix activates Gemma 4's native internal reasoning channel,
letting the model silently process the JSON context before generating visible output.

---

## Concurrency Comparison

| Aspect | Rust Backend | Python Backend |
|--------|-------------|----------------|
| 9 parallel calls | `tokio::spawn(run_agent)` × 9 | `threading.Thread` × 9 |
| Token forwarding | `mpsc::unbounded_channel` → Tokio task | `asyncio.Queue` + `call_soon_threadsafe` |
| WS server | Actix-Web + Actix-WS | `websockets` library |
| Timeout per agent | 600 s (`reqwest::Client` async) | 350 s (`asyncio.wait_for`) |
| Context rotation | `AtomicUsize` (lock-free) | `random.sample` per request |
| Dataset loading | All `*.json` at startup | All `*.json` per request (with fresh sample) |
| Performance | Higher (compiled, zero-copy) | Sufficient for local dev / single user |
| **Export** | Auto-writes `exports/mhlg_UNIX.{json,md}` | Auto-writes `exports/mhlg_YYYYMMDD_HHMMSS.{json,md}` |

---

## Export System

Every completed swarm run is automatically exported to the `exports/` directory.

### Output files

```
exports/
├── mhlg_20260621_085552.json   ← full structured data
└── mhlg_20260621_085552.md     ← human-readable report
```

### JSON schema

```json
{
  "timestamp": "2026-06-21T08:55:52.123456+00:00",
  "model":     "gemma4:e4b",
  "source":    "mhlg_cli | serve | rust_backend | browser",
  "prompt":    "user prompt text",
  "agents": [
    {
      "cell_id":   0,
      "label":     "F1A: Arquitecto",
      "row":       "TESIS",
      "directive": "Eres el Arquitecto de Sistemas…",
      "response":  "full agent response text"
    },
    // … 8 more agents (cell_id 1–8)
  ]
}
```

### Markdown structure

```markdown
# 🧬 MHLG Swarm Export

| Campo | Valor |
|---|---|
| Fecha | `2026-06-21T08:55:52Z` |
| Modelo | `gemma4:e4b` |
| Fuente | `rust_backend` |

## 💬 Prompt

> user prompt text

---

## 🔺 TESIS — Expansión

### F1A: Arquitecto
…

### F1B: Pragmático
…

### F1C: Explorador
…

---

## 🔀 SÍNTESIS — Conexión
…

## 🔻 ANTÍTESIS — Restricción
…
```

### Export triggers

| Trigger | Location | Note |
|---------|----------|------|
| After every swarm | `run_inference()` in `main.rs` | Automatic, server-side |
| After every swarm | `run_terminal()` in `mhlg_cli.py` | Automatic, prints filename |
| After every swarm | `handle_client()` serve mode | Automatic, prints filename |
| **💾 Export button** | Browser UI (`MosaicGrid.astro`) | Manual, appears after all 9 cells complete; downloads JSON + MD directly to the browser's Downloads folder |

### Browser export detail

The browser reads cell content directly from the DOM — no server round-trip needed.

```
User clicks "💾 Export"
    │
    ├─ Read content-0 … content-8 from DOM
    ├─ Build JSON doc  → Blob → <a download> → click
    └─ Build MD string → Blob → <a download> → click
    (two simultaneous downloads: .json + .md)
```

The Export button:
- Is **hidden** on load
- **Appears** (purple glow) when `activeAgents === 0` (all 9 cells done)
- Is **hidden again** when a new swarm fires or Clear is pressed

---

## File Map

```
.
├── Cargo.toml                         Rust manifest (actix-web, actix-ws, tokio, reqwest, glob, bytes)
├── package.json                       Astro 4 dev/build scripts
├── astro.config.mjs                   Static output config
├── requirements.txt                   Python deps: ollama>=0.6, websockets>=12
│
├── src/
│   ├── backend/
│   │   └── main.rs                    Rust WS server — agents, export_session, build_markdown_export
│   ├── components/
│   │   └── MosaicGrid.astro           3×3 UI grid + WS client + 💾 Export button
│   ├── layouts/
│   │   └── Layout.astro               HTML shell, Google Fonts, CSS design tokens
│   └── pages/
│       └── index.astro                Root page (imports Layout + MosaicGrid)
│
├── mhlg_cli.py                        Python CLI: terminal + --serve modes + export_session()
├── process.py                         JSON aggregator → mhlg_ollama_dataset.json
│
├── exports/                           Auto-created; one .json + .md per swarm run
│   └── mhlg_TIMESTAMP.{json,md}
│
└── *.json                             Memory modules (615+ synapse entries total)
    ├── biomimetic-language.json
    ├── future-figures-projected.json
    ├── language-gamers-learners-*.json
    ├── linguistic-mapping-games.json
    ├── silice-language.json
    └── mhlg_ollama_dataset.json       Generated by process.py (5535 training examples)
```
