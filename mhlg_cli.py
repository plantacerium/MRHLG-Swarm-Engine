"""
mhlg_cli.py — MHLG Swarm CLI Interface  (v0.2)
================================================
Human Gamer sends ONE prompt → 9 AI agents respond concurrently.

TWO modes:
  Terminal mode (default):  print 9 color-coded responses to the console.
  Server mode (--serve):    run a Python WebSocket server at ws://127.0.0.1:8080/ws
                            so the Astro browser UI connects and shows the 9 cells.

⚠️  This file MUST be named 'mhlg_cli.py' — NOT 'ollama.py'.
    Naming it 'ollama.py' shadows the installed 'ollama' package.

Requirements:
    pip install ollama websockets

Usage:
    python mhlg_cli.py                     # terminal mode
    python mhlg_cli.py --serve             # browser mode (connect Astro)
    python mhlg_cli.py --model llama3.2   # different model
"""

import json
import random
import argparse
import threading
import sys
import os
import asyncio

# ── Windows: force UTF-8 so ANSI escapes and Unicode don't crash ──────────────
if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")

from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

# ── Required: ollama ──────────────────────────────────────────────────────────
try:
    import ollama
except ImportError:
    print("[ERROR] Run: pip install ollama")
    sys.exit(1)

# ── Optional: websockets (only needed for --serve mode) ───────────────────────
try:
    import websockets
    import websockets.exceptions
    HAS_WEBSOCKETS = True
except ImportError:
    HAS_WEBSOCKETS = False

# ── ANSI Color Codes ──────────────────────────────────────────────────────────
class C:
    RESET   = "\033[0m"
    BOLD    = "\033[1m"
    DIM     = "\033[2m"
    THESIS  = "\033[38;5;147m"   # soft indigo   — Row 1
    CONNECT = "\033[38;5;120m"   # soft green    — Row 2
    ANTI    = "\033[38;5;210m"   # soft red      — Row 3
    ACCENT  = "\033[38;5;86m"    # bright cyan-green
    GRAY    = "\033[38;5;240m"   # dim gray
    WHITE   = "\033[97m"

# ── 3×3 Dialectical Matrix ────────────────────────────────────────────────────
# (agent_id, display_label, row_color, system_directive)
AGENTS = [
    (0, "F1A · Arquitecto",         C.THESIS,  "Eres el Arquitecto de Sistemas. Diseña patrones, infraestructuras y abstracciones ideales."),
    (1, "F1B · Pragmático",         C.THESIS,  "Eres el Pragmático. Entrega código directo, limpio, de bajo nivel, sin rodeos."),
    (2, "F1C · Explorador",         C.THESIS,  "Eres el Explorador Lateral. Conecta el prompt con ideas radicalmente inesperadas pero coherentes."),
    (3, "F2A · Traductor",          C.CONNECT, "Eres el Traductor Puente. Fusiona la visión arquitectónica con las restricciones críticas en lenguaje claro."),
    (4, "F2B · Catalizador",        C.CONNECT, "Eres el Catalizador de Síntesis. Genera híbridos resilientes entre código pragmático y vectores de seguridad."),
    (5, "F2C · Orquestador",        C.CONNECT, "Eres el Orquestador de Emergencia. Produce la respuesta que emergería si todos los nodos colaboraran perfectamente."),
    (6, "F3A · Crítico Socrático",  C.ANTI,    "Eres el Crítico Socrático. Usa la mayéutica para exponer falacias, deuda técnica y grietas de diseño mediante preguntas."),
    (7, "F3B · Ciberseguro",        C.ANTI,    "Eres el Analista de Ciberseguridad. Identifica vectores de ataque, surface de explotación y vulnerabilidades sistémicas."),
    (8, "F3C · Critico", C.ANTI,    "Eres el Critico Ontológico. Explora entropía máxima, límites físicos y modos de fallo catastrófico."),
]

TOTAL_AGENTS   = len(AGENTS)
CONTEXT_SAMPLE = 15          # entries to inject per request
CONTEXT_MAXLEN = 3000        # char cap for context string

# Row labels used in export (derived from AGENTS ordering)
_ROWS = ["TESIS"] * 3 + ["SÍNTESIS"] * 3 + ["ANTÍTESIS"] * 3


# ── Session Export ───────────────────────────────────────────────────────────

def export_session(
    prompt:    str,
    model:     str,
    results:   list[tuple[int, str]],   # (agent_id, full_response)
    repo_path: str,
    source:    str = "mhlg_cli",
) -> tuple[str, str]:
    """
    Write exports/mhlg_YYYYMMDD_HHMMSS.json and .md.
    Returns (json_path, md_path) or ("", "") on error.
    """
    from datetime import datetime, timezone

    now     = datetime.now(timezone.utc)
    ts_str  = now.strftime("%Y%m%d_%H%M%S")
    ts_iso  = now.isoformat()

    exports_dir = Path(repo_path).resolve() / "exports"
    exports_dir.mkdir(parents=True, exist_ok=True)
    base = exports_dir / f"mhlg_{ts_str}"

    # Build per-agent data (sorted by cell_id)
    results_map = dict(results)
    agents_data = []
    for agent_id, label, _color, directive in AGENTS:
        agents_data.append({
            "cell_id":   agent_id,
            "label":     label,
            "row":       _ROWS[agent_id],
            "directive": directive,
            "response":  results_map.get(agent_id, ""),
        })

    # ── JSON ─────────────────────────────────────────────────────────────────
    json_doc = {
        "timestamp":  ts_iso,
        "model":      model,
        "source":     source,
        "prompt":     prompt,
        "agents":     agents_data,
    }
    json_path = str(base) + ".json"
    try:
        with open(json_path, "w", encoding="utf-8") as f:
            json.dump(json_doc, f, ensure_ascii=False, indent=2)
    except Exception as e:
        print(f"  [EXPORT] JSON error: {e}")
        json_path = ""

    # ── Markdown ────────────────────────────────────────────────────────────
    lines = [
        "# 🧬 MHLG Swarm Export\n",
        "| Campo | Valor |",
        "|---|---|",
        f"| Fecha | `{ts_iso}` |",
        f"| Modelo | `{model}` |",
        f"| Fuente | `{source}` |\n",
        "## 💬 Prompt\n",
    ]
    for ln in prompt.splitlines():
        lines.append(f"> {ln}")
    lines += ["\n---\n"]

    sections = [
        ("🔺 TESIS — Expansión",      [0, 1, 2]),
        ("🔀 SÍNTESIS — Conexión",     [3, 4, 5]),
        ("🔻 ANTÍTESIS — Restricción", [6, 7, 8]),
    ]
    for section_title, cell_ids in sections:
        lines.append(f"## {section_title}\n")
        for cid in cell_ids:
            entry = agents_data[cid]
            response = entry["response"] or "_Sin respuesta_"
            lines.append(f"### {entry['label']}\n")
            lines.append(response)
            lines.append("")
        lines.append("---\n")

    md_path = str(base) + ".md"
    try:
        with open(md_path, "w", encoding="utf-8") as f:
            f.write("\n".join(lines))
    except Exception as e:
        print(f"  [EXPORT] MD error: {e}")
        md_path = ""

    return json_path, md_path


# ── JSON Memory Loader ────────────────────────────────────────────────────────

def load_all_json_context(repo_path: str) -> tuple[list, str]:
    """
    Load ALL *.json files from repo_path.
    Returns (list_of_all_entries, compact_context_string).
    Includes mhlg_ollama_dataset.json — no file is excluded.
    """
    repo = Path(repo_path).resolve()
    all_entries: list = []

    for file_path in sorted(repo.glob("*.json")):
        try:
            with open(file_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, list):
                all_entries.extend(data[:5])      # max 5 entries per file
            elif isinstance(data, dict):
                for key in ("games", "modules", "entries", "synapses"):
                    if key in data and isinstance(data[key], list):
                        all_entries.extend(data[key][:5])
                        break
                else:
                    all_entries.append(data)
        except Exception as e:
            print(f"  [WARN] {file_path.name}: {e}")

    if not all_entries:
        print(f"  {C.GRAY}[INFO] No se encontraron archivos *.json en {repo}.{C.RESET}")
        print(f"  {C.GRAY}       Los agentes funcionarán sin contexto de memoria.{C.RESET}")
        print(f"  {C.GRAY}       Ejecuta 'python process.py' para generar el dataset.{C.RESET}")
        return all_entries, ""   # empty string = no context

    sample = random.sample(all_entries, min(CONTEXT_SAMPLE, len(all_entries)))
    raw    = json.dumps(sample, ensure_ascii=False)
    ctx    = raw[:CONTEXT_MAXLEN] + ("…" if len(raw) > CONTEXT_MAXLEN else "")
    return all_entries, ctx


# ── Token extractor — handles ollama 0.4.x–0.6.x API differences ─────────────

def _extract_token(chunk) -> str:
    """
    ollama ≥0.4: chunk is a Pydantic ChatResponse → chunk.message.content
    older/dict:  chunk is a dict              → chunk["message"]["content"]
    """
    try:
        return chunk.message.content or ""
    except AttributeError:
        pass
    try:
        return chunk["message"]["content"] or ""
    except (KeyError, TypeError):
        return ""


# ── Single-agent runner (runs in a thread) ────────────────────────────────────

def run_agent_thread(
    agent_id:     int,
    label:        str,
    color:        str,
    directive:    str,
    user_prompt:  str,
    json_context: str,
    model:        str,
    print_lock:   threading.Lock,
    on_token=None,       # callback(agent_id, token, done) — used in serve mode
) -> tuple[int, str]:
    """
    Runs one agent's inference synchronously (blocking).
    Streams tokens to terminal (terminal mode) and/or calls on_token callback (serve mode).
    Returns (agent_id, full_response).
    """
    # Build system prompt — omit memory section when no JSON context available
    # so the model doesn't receive confusing empty brackets or placeholder text.
    if json_context:
        system = (
            f"<|think|>\n{directive}\n\n"
            f"## Base de Conocimiento MHLG (Memory Modules):\n{json_context}\n\n"
            f"Responde en el idioma del prompt. Sé conciso y técnico. Máx 200 palabras."
        )
    else:
        system = (
            f"<|think|>\n{directive}\n\n"
            f"Responde en el idioma del prompt. Sé conciso y técnico. Máx 200 palabras. "
            f"(Modo autónomo — sin base de conocimiento JSON.)"
        )
    messages = [
        {"role": "system", "content": system},
        {"role": "user",   "content": user_prompt},
    ]

    full_response = ""
    buffer:list[str] = []

    def _flush(final=False):
        nonlocal buffer
        if buffer:
            chunk_text = "".join(buffer)
            with print_lock:
                print(f"{color}{chunk_text}{C.RESET}", end="", flush=True)
            buffer = []

    try:
        with print_lock:
            print(f"\n{color}{C.BOLD}[{label}]{C.RESET}")
            print(f"{color}{'─' * 52}{C.RESET}", flush=True)

        stream = ollama.chat(
            model=model,
            messages=messages,
            stream=True,
            options={"temperature": 0.85, "top_p": 0.95, "top_k": 64},
        )

        for chunk in stream:
            token = _extract_token(chunk)
            if not token:
                continue

            full_response += token
            buffer.append(token)

            # Flush to terminal on newline or every 6 tokens
            if "\n" in token or len(buffer) >= 6:
                _flush()

            # Callback for serve mode (sends to WebSocket queue)
            if on_token:
                on_token(agent_id, token, False)

        _flush(final=True)

        with print_lock:
            print(f"\n{C.DIM}{'─' * 52}{C.RESET}", flush=True)

    except Exception as e:
        err_msg = f"[{label} error: {e}]"
        full_response = err_msg
        with print_lock:
            print(f"\n{color}{C.BOLD}[{label}]{C.RESET} {C.ANTI}ERROR: {e}{C.RESET}")
        if on_token:
            on_token(agent_id, err_msg, False)

    # Signal completion
    if on_token:
        on_token(agent_id, "", True)

    return agent_id, full_response


# ── Terminal swarm (1 prompt → 9 parallel terminal outputs) ──────────────────

def fire_swarm_terminal(
    user_prompt:   str,
    json_context:  str,
    model:         str,
) -> list[tuple[int, str]]:
    print_lock = threading.Lock()
    results: list[tuple[int,str]] = []

    with ThreadPoolExecutor(max_workers=TOTAL_AGENTS) as pool:
        futures = {
            pool.submit(
                run_agent_thread,
                agent_id, label, color, directive,
                user_prompt, json_context, model, print_lock,
            ): agent_id
            for agent_id, label, color, directive in AGENTS
        }
        for fut in as_completed(futures):
            try:
                results.append(fut.result())
            except Exception as e:
                aid = futures[fut]
                results.append((aid, f"[Exception: {e}]"))

    return sorted(results, key=lambda x: x[0])


# ══════════════════════════════════════════════════════════════════════════════
# TERMINAL MODE
# ══════════════════════════════════════════════════════════════════════════════

def run_terminal(model: str, repo_path: str) -> None:
    """Classic terminal session: type prompts, see 9 colored responses."""

    # Verify / pull model
    print(f"\n{C.ACCENT}{C.BOLD}Verificando modelo {model}…{C.RESET}")
    try:
        ollama.show(model)
        print(f"{C.ACCENT}✓ Modelo disponible localmente.{C.RESET}")
    except Exception:
        print(f"{C.ACCENT}Descargando {model}…{C.RESET}")
        ollama.pull(model)

    print(f"\n{C.ACCENT}Cargando módulos de memoria MHLG…{C.RESET}")
    all_entries, json_context = load_all_json_context(repo_path)
    if all_entries:
        print(f"{C.ACCENT}✓ {len(all_entries)} synapses cargados desde {Path(repo_path).resolve()}{C.RESET}")
    else:
        print(f"{C.ANTI}⚠  Sin archivos JSON — los agentes operarán en modo autónomo.{C.RESET}")
        print(f"{C.GRAY}   (Ejecuta 'python process.py' para generar el dataset){C.RESET}")

    shared_history: list[dict] = []

    print(f"\n{C.WHITE}{'═'*60}")
    print(f"  MHLG Swarm · Terminal Mode · {model}")
    print(f"  Comandos: exit | load | clear")
    print(f"{'═'*60}{C.RESET}\n")

    while True:
        try:
            user_input = input(f"{C.ACCENT}{C.BOLD}Human Gamer ▶ {C.RESET}").strip()
        except (EOFError, KeyboardInterrupt):
            print(f"\n{C.GRAY}Sesión cerrada.{C.RESET}")
            break

        if not user_input:
            continue
        if user_input.lower() == "exit":
            print(f"\n{C.GRAY}Morfogénesis finalizada.{C.RESET}")
            break
        if user_input.lower() == "clear":
            os.system("cls" if os.name == "nt" else "clear")
            continue
        if user_input.lower() == "load":
            if not all_entries:
                print(f"\n{C.GRAY}[⚠  Sin módulos de memoria — no hay JSONs cargados.]"
                      f"\n   Ejecuta 'python process.py' y reinicia la sesión.{C.RESET}\n")
                continue
            entry = random.choice(all_entries)
            preview = json.dumps(entry, ensure_ascii=False)[:120]
            print(f"\n{C.ACCENT}[Sincronizando Módulo]: {preview}…{C.RESET}\n")
            user_input = f"Activa y ejecuta este módulo: {json.dumps(entry, ensure_ascii=False)}"

        print(f"\n{C.WHITE}{C.BOLD}{'═'*60}\n  ENJAMBRE DISPARADO — 9 agentes en paralelo…\n{'═'*60}{C.RESET}")

        results = fire_swarm_terminal(user_input, json_context, model)

        # Keep Catalizador (F2B, index 4) as canonical history entry
        canonical = next((r for a, r in results if a == 4), "")
        shared_history.append({"role": "user",      "content": user_input})
        shared_history.append({"role": "assistant",  "content": canonical})

        # Export prompt + all 9 responses to exports/
        json_path, md_path = export_session(user_input, model, results, repo_path)
        if json_path:
            print(f"{C.ACCENT}  💾 {Path(json_path).name}  |  {Path(md_path).name}{C.RESET}")

        # Refresh context for next round
        _, json_context = load_all_json_context(repo_path)

        print(f"\n{C.ACCENT}{'═'*60}\n  ✓ 9/9 respuestas — listo.\n{'═'*60}{C.RESET}\n")


# ══════════════════════════════════════════════════════════════════════════════
# SERVER MODE  (Python WebSocket backend — Astro connects to this)
# ══════════════════════════════════════════════════════════════════════════════

async def serve_mode(model: str, repo_path: str) -> None:
    """
    Start a WebSocket server at ws://127.0.0.1:8080/ws.
    The Astro frontend connects to this instead of the Rust backend.
    Protocol is identical: receive {"prompt":"..."}, stream {"cell_id","token","done"}.
    """
    if not HAS_WEBSOCKETS:
        print("[ERROR] websockets not installed.")
        print("        Run: pip install websockets")
        sys.exit(1)

    # Verify model
    print(f"\n{C.ACCENT}{C.BOLD}Verificando modelo {model}…{C.RESET}")
    try:
        ollama.show(model)
        print(f"{C.ACCENT}✓ Modelo listo.{C.RESET}")
    except Exception:
        print(f"{C.ACCENT}Descargando {model}…{C.RESET}")
        ollama.pull(model)

    # Load all JSON context once
    print(f"\n{C.ACCENT}Cargando módulos de memoria…{C.RESET}")
    all_entries, json_context_ref = load_all_json_context(repo_path)
    print(f"{C.ACCENT}✓ {len(all_entries)} synapses cargados.{C.RESET}")

    # Use a mutable container so the closure can update it
    ctx_box = [json_context_ref]

    print(f"\n{C.WHITE}{'═'*60}")
    print(f"  MHLG Swarm · Server Mode · {model}")
    print(f"  WebSocket : ws://127.0.0.1:8080/ws")
    print(f"  Browser UI: http://localhost:4321  (run: npm run dev)")
    print(f"  Esperando conexión del frontend Astro…")
    print(f"{'═'*60}{C.RESET}\n")

    async def handle_client(websocket):
        """One WebSocket connection from the Astro browser."""
        remote = getattr(websocket, "remote_address", "?")
        print(f"{C.ACCENT}[SERVER] Cliente conectado: {remote}{C.RESET}")

        try:
            async for raw in websocket:
                # ── Parse incoming prompt ──────────────────────────────────
                try:
                    data   = json.loads(raw)
                    prompt = data.get("prompt", "").strip()
                except Exception:
                    continue
                if not prompt:
                    continue

                print(f"\n{C.ACCENT}[SERVER] Prompt: {prompt[:80]}{C.RESET}")

                # Refresh context
                _, ctx_box[0] = load_all_json_context(repo_path)
                json_ctx = ctx_box[0]

                # ── asyncio.Queue bridges threads → async WS send ──────────
                loop  = asyncio.get_running_loop()
                queue: asyncio.Queue[str] = asyncio.Queue()

                # Accumulate full responses per cell for export
                full_responses: dict[int, list[str]] = {i: [] for i in range(TOTAL_AGENTS)}

                def make_on_token(aid: int):
                    def on_token(agent_id: int, token: str, done: bool):
                        if not done and token:
                            full_responses[agent_id].append(token)  # accumulate
                        pkt = json.dumps({"cell_id": agent_id, "token": token, "done": done})
                        loop.call_soon_threadsafe(queue.put_nowait, pkt)
                    return on_token

                print_lock = threading.Lock()

                # Launch all 9 agent threads
                threads = []
                for agent_id, label, color, directive in AGENTS:
                    t = threading.Thread(
                        target=run_agent_thread,
                        args=(
                            agent_id, label, color, directive,
                            prompt, json_ctx, model,
                            print_lock, make_on_token(agent_id),
                        ),
                        daemon=True,
                    )
                    t.start()
                    threads.append(t)

                # ── Drain queue → WebSocket until all 9 agents done ────────
                done_count = 0
                while done_count < TOTAL_AGENTS:
                    try:
                        pkt = await asyncio.wait_for(queue.get(), timeout=350.0)
                    except asyncio.TimeoutError:
                        print(f"{C.ANTI}[SERVER] Timeout esperando agentes.{C.RESET}")
                        break

                    try:
                        await websocket.send(pkt)
                    except Exception:
                        print(f"{C.ANTI}[SERVER] Cliente desconectado mid-stream.{C.RESET}")
                        break

                    if json.loads(pkt).get("done"):
                        done_count += 1

                print(f"{C.ACCENT}[SERVER] ✓ {done_count}/{TOTAL_AGENTS} agentes completados.{C.RESET}")

                # Export prompt + all 9 responses to exports/
                serve_results = [(aid, "".join(toks)) for aid, toks in full_responses.items()]
                json_path, md_path = export_session(
                    prompt, model, serve_results, repo_path, source="serve"
                )
                if json_path:
                    print(f"{C.ACCENT}[SERVER] 💾 {Path(json_path).name}  |  {Path(md_path).name}{C.RESET}\n")


        except websockets.exceptions.ConnectionClosedOK:
            print(f"{C.GRAY}[SERVER] Cliente desconectado normalmente.{C.RESET}")
        except websockets.exceptions.ConnectionClosedError as e:
            print(f"{C.ANTI}[SERVER] Conexión cerrada con error: {e}{C.RESET}")
        except Exception as e:
            print(f"{C.ANTI}[SERVER] Error inesperado: {e}{C.RESET}")

    # Start the WebSocket server and run forever
    async with websockets.serve(handle_client, "127.0.0.1", 8080):
        await asyncio.Future()   # block until cancelled


# ══════════════════════════════════════════════════════════════════════════════
# ENTRY POINT
# ══════════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="MHLG Swarm CLI — 1 Human prompt → 9 AI responses"
    )
    parser.add_argument(
        "--model", default="gemma4:e4b",
        help="Ollama model to use (default: gemma4:e4b)",
    )
    parser.add_argument(
        "--repo-path", default="./",
        help="Directory containing *.json memory modules (default: ./)",
    )
    parser.add_argument(
        "--serve", action="store_true",
        help=(
            "Run as WebSocket server (ws://127.0.0.1:8080/ws) "
            "so the Astro browser UI can connect. "
            "Requires: pip install websockets"
        ),
    )
    args = parser.parse_args()

    if args.serve:
        try:
            asyncio.run(serve_mode(args.model, args.repo_path))
        except KeyboardInterrupt:
            print(f"\n{C.GRAY}[SERVER] Detenido.{C.RESET}")
    else:
        run_terminal(args.model, args.repo_path)