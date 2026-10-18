import json
import threading
from contextlib import asynccontextmanager
from typing import Literal

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from starlette.middleware.trustedhost import TrustedHostMiddleware

from .config import ROOT
from .db import falkor, neo_query
from .engine import RUNS, new_report, run_investigation, save

busy = threading.Lock()


@asynccontextmanager
async def lifespan(app):
    for path in RUNS.glob("*.json"):
        report = json.loads(path.read_text())
        if report.get("origin") == "api" and report["status"] in {"running", "queued"}:
            report.update(status="interrupted", error="Server stopped before this investigation finished")
            save(report)
    yield


app = FastAPI(title="SwitchProof", lifespan=lifespan)
app.add_middleware(TrustedHostMiddleware, allowed_hosts=["127.0.0.1", "localhost", "testserver"])


@app.middleware("http")
async def local_origin(request: Request, call_next):
    if request.method == "POST":
        origin = request.headers.get("origin")
        if origin and origin != str(request.base_url).rstrip("/"):
            return JSONResponse({"detail": "Only same-origin requests are allowed"}, status_code=403)
    return await call_next(request)


class Start(BaseModel):
    provider: Literal["zero", "anthropic"] = "anthropic"
    use_memory: bool = True


@app.get("/api/health")
def health():
    checks = {}
    for name, check in [
        ("neo4j", lambda: neo_query("RETURN 1 AS ready")),
        ("falkordb", lambda: falkor().connection.ping()),
    ]:
        try:
            check()
            checks[name] = True
        except Exception:
            checks[name] = False
    return {"ready": all(checks.values()), "databases": checks, "busy": busy.locked()}


@app.get("/api/runs")
def runs():
    output = []
    for path in RUNS.glob("*.json"):
        report = json.loads(path.read_text())
        output.append({k: report.get(k) for k in ["id", "created_at", "status", "stage", "fixture"]})
    return sorted(output, key=lambda r: r["created_at"], reverse=True)


def read_report(run_id):
    if len(run_id) != 12 or any(c not in "0123456789abcdef" for c in run_id):
        raise HTTPException(404, "Investigation not found")
    path = RUNS / (run_id + ".json")
    if not path.exists():
        raise HTTPException(404, "Investigation not found")
    return path


@app.get("/api/runs/{run_id}")
def run(run_id: str):
    return json.loads(read_report(run_id).read_text())


@app.get("/api/runs/{run_id}/receipt")
def receipt(run_id: str):
    return FileResponse(
        read_report(run_id), media_type="application/json", filename=f"switchproof-{run_id}.json"
    )


@app.post("/api/runs", status_code=202)
def start(options: Start):
    if not busy.acquire(blocking=False):
        raise HTTPException(409, "An investigation is already using the disposable source database")
    report = new_report()
    report["origin"] = "api"
    save(report)

    def work():
        try:
            run_investigation(report, options.provider, options.use_memory)
        finally:
            busy.release()

    threading.Thread(target=work, daemon=True).start()
    return report


web = ROOT / "web/dist"
if web.exists():
    app.mount("/", StaticFiles(directory=web, html=True), name="dashboard")
