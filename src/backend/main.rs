//! MHLG Swarm Resonance Engine — Rust WebSocket Backend v0.3
//!
//! v0.3 fixes vs v0.2:
//!   ① Message loop no longer blocks during inference.
//!     Inference is spawned in its own tokio::spawn task; the loop is
//!     immediately free to respond to Ping frames or new messages.
//!   ② 20-second server-side heartbeat (Ping→Pong) keeps the WebSocket
//!     alive for the entire duration of a long inference pass.
//!   ③ Switched from reqwest::blocking + spawn_blocking to fully async
//!     reqwest::Client + tokio::spawn — proper async I/O, no thread-pool waste.
//!   ④ Correct NDJSON chunk-boundary handling via a remainder buffer so
//!     partial TCP chunks never cause a parse miss.

use actix_web::{middleware, web, App, HttpRequest, HttpResponse, HttpServer};
use actix_ws::Message;
use bytes::Bytes;
use futures_util::StreamExt;
use serde::{Deserialize, Serialize};
use std::{
    sync::{
        atomic::{AtomicUsize, Ordering},
        Arc,
    },
    time::Duration,
};

// ─── Wire Types ───────────────────────────────────────────────────────────────

#[derive(Deserialize)]
struct PromptPayload {
    prompt: String,
}

#[derive(Serialize)]
struct StreamToken {
    cell_id: usize,
    token:   String,
    done:    bool,
}

// ─── 3×3 Dialectical Matrix ───────────────────────────────────────────────────
//
//   Row 1 TESIS      F1A Arquitecto | F1B Pragmático | F1C Explorador
//   Row 2 SÍNTESIS   F2A Traductor  | F2B Catalizador | F2C Orquestador
//   Row 3 ANTÍTESIS  F3A Socrático  | F3B Ciberseguro | F3C Abogado Diablo
//
const SYSTEM_DIRECTIVES: [(&str, &str); 9] = [
    ("F1A: Arquitecto",
     "Eres el Arquitecto de Sistemas. Diseña patrones, infraestructuras y \
      abstracciones ideales. Propón diseños elegantes y escalables."),
    ("F1B: Pragmático",
     "Eres el Pragmático. Escribe código directo, limpio, de bajo nivel, sin rodeos. \
      Entrega la solución más concreta y ejecutable posible."),
    ("F1C: Explorador",
     "Eres el Explorador Lateral. Realiza saltos abstractos sin censura. \
      Conecta el prompt con ideas radicalmente inesperadas pero coherentes."),
    ("F2A: Traductor",
     "Eres el Traductor Puente. Fusiona la visión arquitectónica con las \
      restricciones críticas. Sintetiza lo ideal con lo posible en lenguaje claro."),
    ("F2B: Catalizador",
     "Eres el Catalizador de Síntesis. Acelera la convergencia entre código \
      pragmático y vectores de seguridad. Genera híbridos resilientes."),
    ("F2C: Orquestador",
     "Eres el Orquestador de Emergencia. Produce la respuesta más emergente \
      si todos los nodos colaboraran perfectamente bajo límites ontológicos estrictos."),
    ("F3A: Crítico Socrático",
     "Eres el Crítico Socrático. Usa la mayéutica para exponer deuda técnica, \
      falacias lógicas y grietas de diseño. Formula preguntas que destruyen certezas falsas."),
    ("F3B: Ciberseguro",
     "Eres el Analista de Ciberseguridad. Identifica vectores de ataque, \
      fugas de memoria, surface de explotación y vulnerabilidades sistémicas."),
    ("F3C: Critico",
     "Eres el Critico Ontológico. Explora la entropía máxima, \
      los límites físicos y termodinámicos, y los modos de fallo catastrófico."),
];

const OLLAMA_URL:          &str = "http://localhost:11434/api/chat";
const OLLAMA_MODEL:        &str = "gemma4:e4b";
const CONNECT_TIMEOUT_S:   u64  = 10;   // fail fast if Ollama is not running
const INFERENCE_TIMEOUT_S: u64  = 600;  // 10 min — generous for cold model load
const HEARTBEAT_S:         u64  = 20;   // server→browser ping interval
const CONTEXT_SAMPLE:      usize = 12;  // entries per request
const CONTEXT_MAX_CHARS:   usize = 3000;
const ENTRIES_PER_FILE:    usize = 5;

// ─── Shared State ─────────────────────────────────────────────────────────────

struct AppState {
    entries:       Arc<Vec<serde_json::Value>>,
    request_count: Arc<AtomicUsize>,
}

// ─── JSON Loader ──────────────────────────────────────────────────────────────

fn load_json_entries() -> Vec<serde_json::Value> {
    let paths = match glob::glob("./*.json") {
        Ok(g)  => g,
        Err(e) => { eprintln!("[MHLG] glob error: {e}"); return vec![]; }
    };

    let mut all: Vec<serde_json::Value> = Vec::new();
    let mut file_count = 0usize;

    for result in paths {
        let path = match result {
            Ok(p)  => p,
            Err(e) => { eprintln!("[MHLG] path error: {e}"); continue; }
        };
        let content = match std::fs::read_to_string(&path) {
            Ok(c)  => c,
            Err(e) => { eprintln!("[MHLG] read {:?}: {e}", path); continue; }
        };
        match serde_json::from_str::<serde_json::Value>(&content) {
            Ok(serde_json::Value::Array(arr)) => {
                all.extend(arr.into_iter().take(ENTRIES_PER_FILE));
                file_count += 1;
            }
            Ok(obj) => { all.push(obj); file_count += 1; }
            Err(e)  => eprintln!("[MHLG] parse {:?}: {e}", path),
        }
    }


    // ── Startup summary ─────────────────────────────────────────────────────
    if all.is_empty() {
        println!("[MHLG] ┌─────────────────────────────────────────────────┐");
        println!("[MHLG] │  ⚠  No JSON memory files found in '.'          │");
        println!("[MHLG] │  Agents will run WITHOUT memory context.        │");
        println!("[MHLG] │  To add context, run:  python process.py        │");
        println!("[MHLG] │  or place *.json modules in the project root.   │");
        println!("[MHLG] └─────────────────────────────────────────────────┘");
    } else {
        println!("[MHLG] ✓ Loaded {} entries from {} JSON files (dataset included)",
                 all.len(), file_count);
    }
    all
}

// ─── Context Builder ──────────────────────────────────────────────────────────

fn build_context(entries: &[serde_json::Value], count: &AtomicUsize) -> String {
    // Returns "" when no entries are loaded — callers check is_empty() to skip
    // the memory section entirely rather than injecting a misleading placeholder.
    if entries.is_empty() { return String::new(); }
    let n   = entries.len();
    let c   = count.fetch_add(1, Ordering::Relaxed);
    let off = c.wrapping_mul(13) % n;
    let sz  = CONTEXT_SAMPLE.min(n);
    let sample: Vec<&serde_json::Value> = (0..sz).map(|i| &entries[(off + i) % n]).collect();
    let raw = serde_json::to_string(&sample).unwrap_or_default();
    if raw.len() > CONTEXT_MAX_CHARS {
        format!("{}…[truncated]", &raw[..CONTEXT_MAX_CHARS])
    } else {
        raw
    }
}

// ─── Token Emitter ────────────────────────────────────────────────────────────

fn emit(tx: &tokio::sync::mpsc::UnboundedSender<String>, cell_id: usize, token: &str, done: bool) {
    if let Ok(s) = serde_json::to_string(&StreamToken { cell_id, token: token.to_string(), done }) {
        let _ = tx.send(s);
    }
}

// ─── Async Agent Runner ───────────────────────────────────────────────────────

async fn run_agent(
    cell_id:   usize,
    label:     String,
    directive: String,
    prompt:    Arc<String>,
    context:   Arc<String>,
    tx:        Arc<tokio::sync::mpsc::UnboundedSender<String>>,
) -> String {                    // ← returns full accumulated response for export
    let mut full_text = String::new();

    // Build system prompt — memory section is omitted when no JSON context
    // is available so the model doesn't see confusing empty brackets.
    let system = if context.is_empty() {
        format!(
            "<|think|>\n{directive}\n\n\
             Responde en el idioma del prompt. Sé conciso y técnico. Máx 200 palabras.\
             (Sin base de conocimiento JSON disponible — operando en modo autónomo.)",
        )
    } else {
        format!(
            "<|think|>\n{directive}\n\n\
             ## Base de Conocimiento MHLG (Memory Modules):\n{context}\n\n\
             Responde en el idioma del prompt. Sé conciso y técnico. Máx 200 palabras.",
        )
    };

    // Async HTTP client — no blocking thread pool needed
    let client = match reqwest::Client::builder()
        .connect_timeout(Duration::from_secs(CONNECT_TIMEOUT_S))
        .timeout(Duration::from_secs(INFERENCE_TIMEOUT_S))
        .build()
    {
        Ok(c)  => c,
        Err(e) => {
            eprintln!("[{label}] client build error: {e}");
            emit(&tx, cell_id, &format!("[{label}] client error: {e}"), true);
            return full_text;
        }
    };

    let body = serde_json::json!({
        "model": OLLAMA_MODEL,
        "messages": [
            { "role": "system", "content": system },
            { "role": "user",   "content": prompt.as_str() }
        ],
        "options": { "temperature": 0.85, "top_p": 0.95, "top_k": 64 },
        "stream": true
    });

    let resp = match client.post(OLLAMA_URL).json(&body).send().await {
        Ok(r)  => r,
        Err(e) => {
            eprintln!("[{label}] request failed: {e}");
            let err_tok = format!(
                "[{label}] Ollama no responde — asegúrate de que 'ollama serve' está activo."
            );
            emit(&tx, cell_id, &err_tok, true);
            return err_tok;
        }
    };

    // ── Stream NDJSON response ────────────────────────────────────────────────
    // Ollama sends one JSON object per line (NDJSON). TCP may split lines
    // across chunks, so we buffer unconsumed bytes in `remainder`.
    let mut body_stream = resp.bytes_stream();
    let mut remainder: Vec<u8> = Vec::new();

    'stream: while let Some(chunk_result) = body_stream.next().await {
        let chunk = match chunk_result {
            Ok(b)  => b,
            Err(e) => { eprintln!("[{label}] stream error: {e}"); break; }
        };

        remainder.extend_from_slice(&chunk);

        // Process every complete '\n'-terminated line
        while let Some(nl) = remainder.iter().position(|&b| b == b'\n') {
            let line_bytes: Vec<u8> = remainder.drain(..=nl).collect();
            let line = String::from_utf8_lossy(&line_bytes);
            let line = line.trim();

            if line.is_empty() { continue; }

            let val = match serde_json::from_str::<serde_json::Value>(line) {
                Ok(v)  => v,
                Err(_) => continue,
            };

            if let Some(tok) = val["message"]["content"].as_str() {
                if !tok.is_empty() {
                    full_text.push_str(tok);   // ← accumulate for export
                    emit(&tx, cell_id, tok, false);
                }
            }

            if val["done"].as_bool().unwrap_or(false) {
                emit(&tx, cell_id, "", true);
                break 'stream;
            }
        }
    }

    // Ensure done is always sent even if stream ended without "done":true
    emit(&tx, cell_id, "", true);
    full_text
}

// ─── Inference Orchestrator ───────────────────────────────────────────────────
//
// Spawns 9 async agent tasks, a forward task (channel → WebSocket), then
// waits for all to complete.  Automatically exports to exports/ on finish.
// Called from inside its own tokio::spawn so it never blocks the message loop.

async fn run_inference(
    prompt:        String,
    mut ws_out:    actix_ws::Session,
    entries:       Arc<Vec<serde_json::Value>>,
    request_count: Arc<AtomicUsize>,
) {
    let prompt  = Arc::new(prompt);
    let context = Arc::new(build_context(&entries, &request_count));

    let (tx, mut rx) = tokio::sync::mpsc::unbounded_channel::<String>();
    let tx = Arc::new(tx);

    // Forward task: channel → WebSocket (runs concurrently with agents)
    let fwd = tokio::spawn(async move {
        while let Some(packet) = rx.recv().await {
            if ws_out.text(packet).await.is_err() {
                break; // client disconnected
            }
        }
    });

    // Launch all 9 agents as (cell_id, JoinHandle<String>) pairs so we can
    // collect each agent's full text for export after all finish.
    let handles: Vec<(usize, tokio::task::JoinHandle<String>)> = SYSTEM_DIRECTIVES
        .iter()
        .enumerate()
        .map(|(cell_id, (label, directive))| {
            let h = tokio::spawn(run_agent(
                cell_id,
                (*label).to_string(),
                (*directive).to_string(),
                Arc::clone(&prompt),
                Arc::clone(&context),
                Arc::clone(&tx),
            ));
            (cell_id, h)
        })
        .collect();

    // Await all 9 and collect full responses for export
    let mut agent_responses: Vec<(usize, String)> = Vec::new();
    for (cell_id, h) in handles {
        match h.await {
            Ok(text) => agent_responses.push((cell_id, text)),
            Err(e)   => eprintln!("[MHLG] Agent {cell_id} join error: {e}"),
        }
    }
    agent_responses.sort_by_key(|(id, _)| *id);

    // Drop last sender → channel closes → forward task drains and exits
    drop(tx);
    let _ = fwd.await;

    println!("[MHLG] ✓ 9/9 agents complete.");

    // Export to exports/ directory (non-blocking, best-effort)
    export_session(&prompt, "rust_backend", &agent_responses).await;
}

// ─── Export: JSON + Markdown ──────────────────────────────────────────────────

async fn export_session(
    prompt:  &str,
    source:  &str,
    results: &[(usize, String)],
) {
    // Create exports/ directory (silently skip if it already exists)
    if let Err(e) = tokio::fs::create_dir_all("exports").await {
        eprintln!("[EXPORT] Cannot create exports/ dir: {e}"); return;
    }

    // Unix timestamp as filename key (zero deps, always sortable)
    let ts = std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .unwrap_or_default()
        .as_secs();
    let base = format!("exports/mhlg_{ts}");

    // ── JSON ──────────────────────────────────────────────────────────────────
    let agents_json: Vec<serde_json::Value> = results.iter().map(|(cell_id, response)| {
        let (label, directive) = SYSTEM_DIRECTIVES[*cell_id];
        let row = match cell_id {
            0..=2 => "TESIS",
            3..=5 => "SÍNTESIS",
            _     => "ANTÍTESIS",
        };
        serde_json::json!({
            "cell_id":   cell_id,
            "label":     label,
            "row":       row,
            "directive": directive,
            "response":  response,
        })
    }).collect();

    let json_doc = serde_json::json!({
        "timestamp_unix": ts,
        "model":          OLLAMA_MODEL,
        "source":         source,
        "prompt":         prompt,
        "agents":         agents_json,
    });

    let json_str = match serde_json::to_string_pretty(&json_doc) {
        Ok(s)  => s,
        Err(e) => { eprintln!("[EXPORT] JSON serialise error: {e}"); return; }
    };
    let json_path = format!("{base}.json");
    match tokio::fs::write(&json_path, json_str.as_bytes()).await {
        Ok(_)  => println!("[MHLG] 💾 Exported JSON → {json_path}"),
        Err(e) => eprintln!("[EXPORT] Write {json_path}: {e}"),
    }

    // ── Markdown ──────────────────────────────────────────────────────────────
    let md_str  = build_markdown_export(prompt, source, ts, results);
    let md_path = format!("{base}.md");
    match tokio::fs::write(&md_path, md_str.as_bytes()).await {
        Ok(_)  => println!("[MHLG] 💾 Exported MD  → {md_path}"),
        Err(e) => eprintln!("[EXPORT] Write {md_path}: {e}"),
    }
}

fn build_markdown_export(
    prompt:  &str,
    source:  &str,
    ts_unix: u64,
    results: &[(usize, String)],
) -> String {
    let mut md = String::new();

    md.push_str("# 🧬 MHLG Swarm Export\n\n");
    md.push_str(&format!("| Campo | Valor |\n|---|---|\n"));
    md.push_str(&format!("| Timestamp (Unix) | `{ts_unix}` |\n"));
    md.push_str(&format!("| Modelo | `{OLLAMA_MODEL}` |\n"));
    md.push_str(&format!("| Fuente | `{source}` |\n\n"));

    md.push_str("## 💬 Prompt\n\n");
    // Quote each line of the prompt
    for line in prompt.lines() {
        md.push_str(&format!("> {line}\n"));
    }
    md.push_str("\n---\n\n");

    let sections: [(&str, [usize; 3]); 3] = [
        ("🔺 TESIS — Expansión",       [0, 1, 2]),
        ("🔀 SÍNTESIS — Conexión",      [3, 4, 5]),
        ("🔻 ANTÍTESIS — Restricción",  [6, 7, 8]),
    ];

    for (section_title, cell_ids) in &sections {
        md.push_str(&format!("## {section_title}\n\n"));
        for &cid in cell_ids {
            let (label, _) = SYSTEM_DIRECTIVES[cid];
            let response = results.iter()
                .find(|(id, _)| *id == cid)
                .map(|(_, r)| r.as_str())
                .unwrap_or("_Sin respuesta_");
            md.push_str(&format!("### {label}\n\n{response}\n\n"));
        }
        md.push_str("---\n\n");
    }

    md
}

// ─── WebSocket Handler ────────────────────────────────────────────────────────

async fn ws_handler(
    req:    HttpRequest,
    stream: web::Payload,
    state:  web::Data<AppState>,
) -> Result<HttpResponse, actix_web::Error> {
    let (response, session, mut msg_stream) = actix_ws::handle(&req, stream)?;

    actix_web::rt::spawn(async move {
        let mut session = session;

        // ── Heartbeat ────────────────────────────────────────────────────────
        // Ping the browser every 20 s so the connection stays alive while
        // Ollama generates 9 responses (can take several minutes on first run).
        let mut heartbeat = tokio::time::interval(Duration::from_secs(HEARTBEAT_S));
        // Skip the immediate first tick so we don't ping before first message.
        heartbeat.set_missed_tick_behavior(tokio::time::MissedTickBehavior::Delay);
        heartbeat.tick().await;

        loop {
            tokio::select! {
                biased; // prefer processing real messages over heartbeat

                // ── Incoming WebSocket frame ──────────────────────────────────
                msg = msg_stream.next() => {
                    match msg {
                        Some(Ok(Message::Text(text))) => {
                            let payload = match serde_json::from_str::<PromptPayload>(&text) {
                                Ok(p)  => p,
                                Err(e) => { eprintln!("[MHLG] bad payload: {e}"); continue; }
                            };

                            println!("[MHLG] → Prompt: {:.80}", payload.prompt);

                            // ✦ Spawn inference in its own task.
                            //   The message loop returns HERE immediately, free to
                            //   respond to Ping frames and process future messages.
                            let out_session    = session.clone();
                            let entries        = Arc::clone(&state.entries);
                            let request_count  = Arc::clone(&state.request_count);

                            tokio::spawn(async move {
                                run_inference(
                                    payload.prompt,
                                    out_session,
                                    entries,
                                    request_count,
                                ).await;
                            });
                        }

                        // Respond to browser pings — critical for keeping alive
                        Some(Ok(Message::Ping(b))) => { let _ = session.pong(&b).await; }
                        Some(Ok(Message::Pong(_))) => {} // response to our heartbeat
                        Some(Ok(Message::Close(_))) | None | Some(Err(_)) => break,
                        _ => {}
                    }
                }

                // ── Server-side heartbeat ──────────────────────────────────────
                _ = heartbeat.tick() => {
                    // If the client is gone, ping returns Err and we exit cleanly
                    if session.ping(&Bytes::from_static(b"mhlg")).await.is_err() {
                        break;
                    }
                }
            }
        }
    });

    Ok(response)
}

// ─── Health Check ─────────────────────────────────────────────────────────────

async fn health() -> impl actix_web::Responder {
    HttpResponse::Ok()
        .content_type("text/plain; charset=utf-8")
        .body("MHLG Swarm Backend v0.3 — Online")
}

// ─── Entry Point ──────────────────────────────────────────────────────────────

#[actix_web::main]
async fn main() -> std::io::Result<()> {
    println!("╔══════════════════════════════════════════════════╗");
    println!("║   MHLG Swarm Resonance Engine — Backend v0.3    ║");
    println!("╚══════════════════════════════════════════════════╝");
    println!();

    let entries = load_json_entries();
    if entries.is_empty() {
        eprintln!("[MHLG] WARNING: no JSON entries — run python process.py first.");
    }

    let state = web::Data::new(AppState {
        entries:       Arc::new(entries),
        request_count: Arc::new(AtomicUsize::new(0)),
    });

    println!("[MHLG] WebSocket  : ws://127.0.0.1:8080/ws");
    println!("[MHLG] Health     : http://127.0.0.1:8080/health");
    println!("[MHLG] Heartbeat  : every {HEARTBEAT_S} s");
    println!("[MHLG] Timeout    : {INFERENCE_TIMEOUT_S} s per agent");
    println!();

    HttpServer::new(move || {
        App::new()
            .app_data(state.clone())
            .wrap(
                middleware::DefaultHeaders::new()
                    .add(("Access-Control-Allow-Origin",  "*"))
                    .add(("Access-Control-Allow-Headers", "content-type")),
            )
            .route("/ws",     web::get().to(ws_handler))
            .route("/health", web::get().to(health))
    })
    .bind(("127.0.0.1", 8080))?
    .run()
    .await
}
