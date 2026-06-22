"""
process.py — MHLG Memory Module Aggregator
==========================================
Loads ALL .json files from the repository, extracts synapse entries,
and writes a unified mhlg_ollama_dataset.json compatible with Ollama
multi-turn chat format (system / user / assistant roles).

Usage:
    python process.py [--repo-path ./] [--output mhlg_ollama_dataset.json]
"""

import os
import json
import glob
import argparse
import sys
from pathlib import Path

# Force UTF-8 output on Windows (avoids cp1252 UnicodeEncodeError)
if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")


# ── 3×3 Dialectical Matrix System Directives ─────────────────────────────────
SYSTEM_DIRECTIVES = [
    ("F1A: Arquitecto",
     "<|think|> Eres el Arquitecto de Sistemas. Propón patrones, infraestructuras y abstracciones ideales."),
    ("F1B: Pragmático",
     "<|think|> Eres el Pragmático. Escribe código directo, limpio, de bajo nivel, sin rodeos."),
    ("F1C: Explorador",
     "<|think|> Eres el Explorador Lateral. Realiza saltos abstractos y conecta ideas radicalmente inesperadas."),
    ("F2A: Traductor",
     "<|think|> Eres el Traductor Puente. Fusiona la visión arquitectónica con las restricciones críticas."),
    ("F2B: Catalizador",
     "<|think|> Eres el Catalizador de Síntesis. Genera híbridos resilientes entre código y seguridad."),
    ("F2C: Orquestador",
     "<|think|> Eres el Orquestador de Emergencia. Produce la respuesta que emergería si todos los nodos colaboraran."),
    ("F3A: Crítico Socrático",
     "<|think|> Eres el Crítico Socrático. Usa la mayéutica para exponer falacias y deuda técnica."),
    ("F3B: Ciberseguro",
     "<|think|> Eres el Analista de Ciberseguridad. Identifica vectores de ataque y vulnerabilidades sistémicas."),
    ("F3C: Critico",
     "<|think|> Eres el Critico Ontológico. Explora la entropía máxima y modos de fallo catastrófico."),
]


def extract_entries(data: any, source_file: str) -> list[dict]:
    """
    Robustly extract synapse/game entries from any JSON structure:
    - A top-level list of synapse objects
    - An object with a 'games', 'modules', or 'entries' key containing a list
    - A single object (wrapped into a 1-item list)
    """
    entries = []

    if isinstance(data, list):
        entries = data
    elif isinstance(data, dict):
        for key in ("games", "modules", "entries", "synapses", "data"):
            if key in data and isinstance(data[key], list):
                entries = data[key]
                break
        if not entries:
            entries = [data]  # Treat single dict as one entry
    else:
        print(f"  [WARN] Skipping {source_file}: unexpected root type {type(data)}")

    return entries


def build_dataset(repo_path: str, output_file: str) -> None:
    repo = Path(repo_path).resolve()
    json_files = sorted(repo.glob("*.json"))

    # Exclude the generated dataset itself to avoid circular loading
    json_files = [f for f in json_files if f.name != Path(output_file).name]

    print(f"\n{'='*60}")
    print(f"  MHLG Memory Module Aggregator")
    print(f"  Repository : {repo}")
    print(f"  JSON files found: {len(json_files)}")
    print(f"{'='*60}\n")

    all_entries = []

    for file_path in json_files:
        try:
            with open(file_path, "r", encoding="utf-8") as f:
                data = json.load(f)

            entries = extract_entries(data, file_path.name)
            all_entries.extend(entries)
            print(f"  [OK] {file_path.name:50s}  -> {len(entries):4d} entries")

        except json.JSONDecodeError as e:
            print(f"  [ERR] {file_path.name}: JSON parse error -- {e}")
        except Exception as e:
            print(f"  [ERR] {file_path.name}: {e}")

    print(f"\n  Total synapse entries loaded: {len(all_entries)}")

    # Build Ollama-compatible dataset
    # Each entry gets 9 variants (one per dialectical agent)
    ollama_dataset = []

    for entry in all_entries:
        # Build a human-readable prompt from the synapse entry
        game  = entry.get("game", "Módulo de Expansión Conceptual")
        mech  = entry.get("mechanic", json.dumps(entry, ensure_ascii=False)[:200])
        h_role = entry.get("human_role", "")
        a_role = entry.get("ai_role", "")
        h_exp  = entry.get("human_expansion", "")
        mod    = entry.get("module", "")

        human_query = (
            f"Activa el módulo '{game}' (Módulo: {mod}). "
            f"Mecánica: {mech}. "
            f"Rol Humano: {h_role} "
            f"Expansión esperada: {h_exp}"
        ).strip()

        # For each dialectical agent, create a fine-tuning example
        for agent_label, system_prompt in SYSTEM_DIRECTIVES:
            ai_response = (
                f"[{agent_label}] Módulo activado: '{game}'. "
                f"Rol IA: {a_role} "
                f"Mecánica en ejecución: {mech}"
            )

            ollama_dataset.append([
                {"role": "system",    "content": system_prompt},
                {"role": "user",      "content": human_query},
                {"role": "assistant", "content": ai_response},
            ])

    output_path = Path(output_file)
    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(ollama_dataset, f, ensure_ascii=False, indent=2)

    print(f"\n  [OK] Dataset written to: {output_path.resolve()}")
    print(f"    {len(ollama_dataset)} fine-tuning examples "
          f"({len(all_entries)} entries × 9 agents)")
    print(f"{'='*60}\n")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="MHLG Memory Module Aggregator")
    parser.add_argument("--repo-path", default="./",
                        help="Path to the repository with JSON files (default: ./)")
    parser.add_argument("--output", default="mhlg_ollama_dataset.json",
                        help="Output dataset filename (default: mhlg_ollama_dataset.json)")
    args = parser.parse_args()

    build_dataset(args.repo_path, args.output)