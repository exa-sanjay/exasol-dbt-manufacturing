"""Pull the qwen2.5:0.5b model into the local Ollama service.

Streams the download response and prints progress so the user knows
something is happening during the ~400 MB download.
"""

import os
import sys
import json
import requests

OLLAMA_URL = os.environ.get("OLLAMA_URL", "http://localhost:11434")
MODEL      = "qwen2.5:7b"


def pull_model():
    print(f"==> Pulling Ollama model '{MODEL}' from {OLLAMA_URL} ...")
    print("    (This downloads ~400 MB on first run — subsequent runs are instant.)")

    try:
        resp = requests.post(
            f"{OLLAMA_URL}/api/pull",
            json={"name": MODEL},
            stream=True,
            timeout=600,
        )
        resp.raise_for_status()
    except requests.exceptions.ConnectionError:
        print(
            f"\nERROR: Cannot connect to Ollama at {OLLAMA_URL}.\n"
            "       Make sure 'docker compose up -d ollama' has run first.\n"
            "       Wait ~15s for the container to start, then retry."
        )
        sys.exit(1)

    last_status = ""
    for line in resp.iter_lines():
        if not line:
            continue
        try:
            data = json.loads(line)
        except json.JSONDecodeError:
            continue

        status = data.get("status", "")
        if status != last_status:
            total     = data.get("total", 0)
            completed = data.get("completed", 0)
            if total and total > 0:
                pct = int(completed * 100 / total)
                print(f"    {status}: {pct}%", end="\r", flush=True)
            else:
                print(f"    {status}", end="\r", flush=True)
            last_status = status

        if data.get("status") == "success":
            print(f"\n==> Model '{MODEL}' ready.")
            return

    print(f"\n==> Pull complete.")


def verify_model():
    """Quick sanity-check: ask Ollama to list models and confirm ours is there."""
    try:
        resp = requests.get(f"{OLLAMA_URL}/api/tags", timeout=10)
        models = [m["name"] for m in resp.json().get("models", [])]
        if any(MODEL.split(":")[0] in m for m in models):
            print(f"    Verified: '{MODEL}' is available in Ollama.")
        else:
            print(f"    Warning: '{MODEL}' not found in model list: {models}")
    except Exception as e:
        print(f"    Warning: could not verify model list ({e})")


if __name__ == "__main__":
    pull_model()
    verify_model()
