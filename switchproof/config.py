import os
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[1]
load_dotenv(ROOT / ".env", override=True)
STATE = Path(os.environ.get("SWITCHPROOF_STATE_DIR", Path.home() / ".local/state/switchproof"))
STATE.mkdir(parents=True, exist_ok=True)
NEO4J_URI = os.environ.get("SWITCHPROOF_NEO4J_URI", "bolt://127.0.0.1:17687")
PASSWORD = os.environ.get("SWITCHPROOF_DB_PASSWORD", "switchproof-local-only")
FALKOR_HOST = os.environ.get("SWITCHPROOF_FALKOR_HOST", "127.0.0.1")
FALKOR_PORT = int(os.environ.get("SWITCHPROOF_FALKOR_PORT", "16379"))
