"""Pull the Ollama models required by the AI layer.

  nomic-embed-text  — embedding model (~274 MB, used by setup_ai_tables + factory_ai_agent)
  qwen2.5:7b        — LLM for root cause analysis (~4 GB, used by factory_ai_agent)

Streams the download response and prints progress so the user knows
something is happening during the download.
"""

import json
import os
import sys
import time

import requests

from ai_constants import EMBED_MODEL

OLLAMA_URL  = os.environ.get("OLLAMA_URL", "http://localhost:11434")
LLM_MODEL   = "qwen2.5:7b"
MAX_RETRIES = 3

MODELS = [
    (EMBED_MODEL, "~274 MB"),
    (LLM_MODEL,   "~4 GB"),
]


def pull_model(model: str, size_hint: str):
    print(f"\n==> Pulling '{model}' ({size_hint}) from {OLLAMA_URL} ...")
    print("    (Subsequent runs are instant — model is cached in the ollama_data volume.)")

    for attempt in range(1, MAX_RETRIES + 1):
        try:
            resp = requests.post(
                f"{OLLAMA_URL}/api/pull",
                json={"name": model},
                stream=True,
                timeout=600,
            )
            resp.raise_for_status()
            break
        except requests.exceptions.ConnectionError:
            print(
                f"\nERROR: Cannot connect to Ollama at {OLLAMA_URL}.\n"
                "       Make sure 'docker compose up -d ollama' has run first.\n"
                "       Wait ~15s for the container to start, then retry."
            )
            sys.exit(1)
        except requests.exceptions.RequestException as exc:
            if attempt < MAX_RETRIES:
                wait = 30 * attempt
                print(f"\n    Attempt {attempt} failed ({exc}). Retrying in {wait}s ...")
                time.sleep(wait)
            else:
                print(f"\nERROR: Pull failed after {MAX_RETRIES} attempts: {exc}")
                sys.exit(1)

    last_status = ""
    last_pct    = -1
    for line in resp.iter_lines():
        if not line:
            continue
        try:
            data = json.loads(line)
        except json.JSONDecodeError:
            continue

        status    = data.get("status", "")
        total     = data.get("total", 0)
        completed = data.get("completed", 0)

        if status != last_status:
            print(f"    {status}", flush=True)
            last_status = status
            last_pct    = -1

        if total and total > 0:
            pct = int(completed * 100 / total)
            if pct >= last_pct + 5:      # print a new line every 5 %
                print(f"      {pct}%", flush=True)
                last_pct = pct

        if data.get("status") == "success":
            print(f"    '{model}' ready.")
            return

    print(f"    Pull complete.")


def verify_models():
    """Confirm both models appear in Ollama's model list."""
    try:
        resp   = requests.get(f"{OLLAMA_URL}/api/tags", timeout=10)
        names  = [m["name"] for m in resp.json().get("models", [])]
        for model, _ in MODELS:
            base = model.split(":")[0]
            if any(base in n for n in names):
                print(f"    Verified: '{model}' is available in Ollama.")
            else:
                print(f"    Warning:  '{model}' not found in model list: {names}")
    except Exception as e:
        print(f"    Warning: could not verify model list ({e})")


if __name__ == "__main__":
    for model, size_hint in MODELS:
        pull_model(model, size_hint)
    print("\n==> All models ready.")
    verify_models()
