"""Entrypoint packaged as the Tauri Windows sidecar."""
from pathlib import Path
import os
import sys

BASE_DIR = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parents[1]))
os.chdir(BASE_DIR)
sys.path.insert(0, str(BASE_DIR))

from app import app  # noqa: E402

if __name__ == "__main__":
    port = int(os.environ.get("PORT", "5000"))
    app.run(host="127.0.0.1", port=port, debug=False, use_reloader=False)
