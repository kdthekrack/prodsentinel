"""ProdSentinel REST API - AI-enabled production disruption early warning.

Run:  python -m uvicorn api.main:app --reload
Docs: http://127.0.0.1:8000/docs

The data covers a historical year, so the API runs on a plant clock:
POST /clock/advance moves time forward in assessment steps; each step runs
assess -> explain -> alert -> escalate exactly like a live deployment would.
"""
from __future__ import annotations

import json
import os
import threading

import pandas as pd
import secrets

from pathlib import Path

from fastapi import Depends, FastAPI, Header, HTTPException, Query
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from engine.assistant import PlantAssistant
from engine.escalation import EscalationManager
from engine.service import EarlyWarningService
from api.ui import build_router

PACK = os.environ.get("EWS_PACK", "industries/beverage_bottling.yaml")
STEP_H = int(os.environ.get("EWS_STEP_H", "3"))

app = FastAPI(title="ProdSentinel API", description="AI-enabled production disruption early warning", version="1.0")
_lock = threading.Lock()
DB = os.environ.get("EWS_DB")       # separate database per instance (e.g. tests)
svc = EarlyWarningService(PACK, db_path=DB, fresh=os.environ.get("EWS_FRESH", "1") == "1")
DEMO_START = pd.Timestamp(os.environ.get("EWS_START") or (svc.pack.get("demo") or {}).get("start")
                          or str(svc.pack.val_end.ceil("D") + pd.Timedelta(hours=6)))
_clock = {"now": DEMO_START}
svc.cycle(_clock["now"])
assistant = PlantAssistant(svc)

# Per-person access tokens (local development). Created once, stored next to the alert database.
_tok_file = svc.pack.out("state") / "tokens.json"
if _tok_file.exists():
    TOKENS = json.loads(_tok_file.read_text())
else:
    TOKENS = {secrets.token_urlsafe(24): x["name"] for x in svc.pack["people"]}
    _tok_file.write_text(json.dumps(TOKENS, indent=2))


def current_user(x_user_token: str = Header(..., description="personal token from output/<pack>/state/tokens.json")) -> dict:
    name = TOKENS.get(x_user_token)
    if not name:
        raise HTTPException(401, "invalid or missing X-User-Token")
    return next(x for x in svc.pack["people"] if x["name"] == name)


# ------------------------------------------------------------------ models
class Advance(BaseModel):
    hours: float = Field(3, gt=0, le=24 * 14)


class SetClock(BaseModel):
    ts: str


class Note(BaseModel):
    note: str = Field("", max_length=2000)


class Ask(BaseModel):
    question: str = Field(min_length=3, max_length=2000)
    language: str = "English"


class Resolve(BaseModel):
    resolution: str = Field(min_length=3, max_length=2000)


class WhatIf(BaseModel):
    keys: list[str] = Field(min_length=1)
    mitigation: float = Field(1.0, ge=0, le=1)


def _alert(alert_id: str) -> dict:
    a = next((x for x in svc.esc.alerts() if x["alert_id"] == alert_id), None)
    if not a:
        raise HTTPException(404, f"alert {alert_id} not found")
    return a


def _plain(o):
    """numpy / pandas scalars -> JSON-safe Python values."""
    if hasattr(o, "item"):
        return o.item()
    if isinstance(o, pd.Timestamp):
        return str(o)
    return str(o)


def _card(item: dict) -> dict:
    return json.loads(json.dumps(dict(item=item, narration=svc.last["narrations"][item["key"]]), default=_plain))


# ------------------------------------------------------------------ plant + clock
@app.get("/health")
async def health():
    return {"ok": True, "pack": svc.pack.id, "now": str(_clock["now"])}


@app.get("/plant")
async def plant():
    p = svc.pack
    return dict(company=p["company"], currency=p["currency"],
                lines=[dict(id=l["id"], name=l.get("name"), product=l["product"]) for l in p.lines],
                stations=len(p.stations), machines=len(p.machines),
                people=[{k: v for k, v in x.items()} for x in p["people"]],
                roles={k: v["title"] for k, v in p["roles"].items()})


@app.get("/clock")
async def clock():
    return {"now": str(_clock["now"]), "step_h": STEP_H}


@app.post("/clock/advance")
async def advance(body: Advance, user: dict = Depends(current_user)):
    with _lock:
        end = _clock["now"] + pd.Timedelta(hours=body.hours)
        created, escalated = 0, []
        t = _clock["now"]
        while t < end:
            t = min(end, t + pd.Timedelta(hours=STEP_H))
            r = svc.cycle(t)
            created += r["sync"]["created"]
            escalated += r["escalated"]
        _clock["now"] = end
    return {"now": str(end), "alerts_created": created, "escalations": escalated}


@app.post("/clock/set")
async def set_clock(body: SetClock, user: dict = Depends(current_user)):
    try:
        ts = pd.Timestamp(body.ts)
    except ValueError:
        raise HTTPException(400, "invalid timestamp")
    if not (svc.pack.start + pd.Timedelta(days=15) <= ts <= svc.pack.end - pd.Timedelta(days=1)):
        raise HTTPException(400, "timestamp outside the data range")
    with _lock:
        if ts < _clock["now"]:        # going back in time: alert history from the "future" is discarded
            svc.esc = EscalationManager(svc.pack, db_path=DB, fresh=True)
        _clock["now"] = ts
        svc.engine.backlogs = {}
        svc.cycle(ts)
    return {"now": str(ts)}


@app.post("/demo/reset")
async def demo_reset(user: dict = Depends(current_user)):
    """Start the demo again: clear all alerts and go back to the demo moment."""
    with _lock:
        svc.esc = EscalationManager(svc.pack, db_path=DB, fresh=True)
        _clock["now"] = DEMO_START
        svc.engine.backlogs = {}
        svc.cycle(DEMO_START)
    return {"now": str(DEMO_START), "open_alerts": len(svc.esc.alerts(["open"]))}


# ------------------------------------------------------------------ risks
@app.get("/summary")
async def summary():
    return svc.plant_summary()


@app.get("/risks")
async def risks(line: str | None = None, category: str | None = None, alerts_only: bool = True,
          limit: int = Query(50, le=500)):
    out = [x for x in svc.last["items"] if (not alerts_only or x["alert"]) and
           (not line or x["line"] == line) and (not category or x["category"] == category)]
    return [_card(x) for x in out[:limit]]


@app.get("/risks/{key:path}")
async def risk(key: str, language: str = "English"):
    x = next((i for i in svc.last["items"] if i["key"] == key), None)
    if not x:
        raise HTTPException(404, "risk not active")
    card = _card(x)
    if language.lower() not in ("english", "en"):
        card["narration"] = assistant.translate(card["narration"], language)
    return card


@app.get("/views/{role}")
async def view(role: str, line: str | None = None):
    if role not in svc.pack["roles"]:
        raise HTTPException(404, f"unknown role {role}")
    return dict(role=role, title=svc.pack.role_title(role), items=svc.for_role(role, line))


@app.get("/lines/{line}/orders")
async def line_orders(line: str):
    if line not in svc.pack.line_ids:
        raise HTTPException(404, "unknown line")
    eng = svc.engine
    open_o = eng._open_orders(line, _clock["now"])
    L = 24 * 30
    effects = []  # baseline view: normal capacity
    proj = eng._project(line, open_o, eng._capacity(line, effects, L), _clock["now"])
    cols = ["order_id", "customer", "tier", "qty", "remaining", "due_ts", "proj_done", "proj_late_days", "proj_penalty"]
    return json.loads(proj[cols].to_json(orient="records", date_format="iso"))


# ------------------------------------------------------------------ alerts + workflow
@app.get("/alerts")
async def alerts(status: str | None = "open,acknowledged", person: str | None = None):
    st = status.split(",") if status else None
    return [{k: v for k, v in a.items() if k != "payload"} | {"narration": a["payload"]["narration"]}
            for a in svc.esc.alerts(st, person)]


@app.get("/alerts/{alert_id}")
async def alert(alert_id: str):
    a = _alert(alert_id)
    return dict(alert={k: v for k, v in a.items() if k != "payload"}, item=a["payload"]["item"],
                narration=a["payload"]["narration"], actions=svc.esc.actions(alert_id),
                history=svc.esc.history(alert_id), notifications=svc.esc.notifications(alert_id))


@app.post("/alerts/{alert_id}/ack")
async def ack(alert_id: str, body: Note, user: dict = Depends(current_user)):
    _alert(alert_id)
    try:
        svc.esc.acknowledge(alert_id, user["name"], _clock["now"], body.note)
        svc.cycle(_clock["now"])
    except ValueError as e:
        raise HTTPException(409, str(e))
    return await alert(alert_id)


def _approver_role() -> str:
    return svc.pack["escalation"].get("approver_role", "plant_manager")


def _require_approver(user: dict):
    if user["role"] != _approver_role():
        who = svc.pack.people_for(_approver_role())[0]["name"]
        raise HTTPException(403, f"only the {svc.pack.role_title(_approver_role())} ({who}) can do this")


@app.post("/alerts/{alert_id}/authorize")
async def authorize(alert_id: str, body: Note, user: dict = Depends(current_user)):
    _require_approver(user)
    _alert(alert_id)
    try:
        svc.esc.authorize(alert_id, user["name"], _clock["now"], body.note)
    except ValueError as e:
        raise HTTPException(409, str(e))
    svc.cycle(_clock["now"])
    return await alert(alert_id)


@app.post("/alerts/{alert_id}/actions/{idx}/done")
async def action_done(alert_id: str, idx: int, body: Note, user: dict = Depends(current_user)):
    _alert(alert_id)
    act = next((a for a in svc.esc.actions(alert_id) if a["idx"] == idx), None)
    if not act:
        raise HTTPException(404, f"action {idx} not found")
    if act["status"] == "done":
        raise HTTPException(409, "this task is already done")
    if act["status"] != "dispatched":
        raise HTTPException(409, "the plan has not been authorized yet")
    if act["owner_name"] != user["name"]:
        raise HTTPException(403, f"only {act['owner_name']} can complete this task")
    svc.esc.complete_action(alert_id, idx, user["name"], _clock["now"], body.note)
    svc.cycle(_clock["now"])          # the plant view updates immediately
    return await alert(alert_id)


@app.post("/alerts/{alert_id}/resolve")
async def resolve(alert_id: str, body: Resolve, user: dict = Depends(current_user)):
    _require_approver(user)
    a = _alert(alert_id)
    if a["status"] not in ("open", "acknowledged"):
        raise HTTPException(409, f"{alert_id} is already {a['status']}")
    svc.esc.resolve(alert_id, user["name"], _clock["now"], body.resolution)
    svc.cycle(_clock["now"])
    return await alert(alert_id)


@app.post("/alerts/{alert_id}/comment")
async def comment(alert_id: str, body: Note, user: dict = Depends(current_user)):
    _alert(alert_id)
    svc.esc.comment(alert_id, user["name"], _clock["now"], body.note)
    return await alert(alert_id)


@app.get("/notifications")
async def notifications(person: str | None = None, limit: int = Query(50, le=500)):
    n = svc.esc.notifications(limit=5000)
    if person:
        n = [x for x in n if x["to_name"] == person]
    return n[:limit]


# ------------------------------------------------------------------ model + evaluation
@app.get("/model/report")
async def model_report():
    f = svc.pack.out("models") / "failure_model_report.json"
    return json.loads(f.read_text()) if f.exists() else {}


@app.get("/evaluation")
async def evaluation():
    f = svc.pack.out("reports") / "evaluation.json"
    return json.loads(f.read_text(encoding="utf-8")) if f.exists() else {"hint": "run `python run.py evaluate`"}


# ------------------------------------------------------------------ assistant (Claude)
@app.post("/ask")
async def ask(body: Ask, user: dict = Depends(current_user)):
    return assistant.ask(body.question, f"{user['name']} ({svc.pack.role_title(user['role'])})", body.language)


@app.get("/me")
async def me(user: dict = Depends(current_user)):
    return dict(person=user, alerts=[{k: v for k, v in a.items() if k != "payload"}
                                     for a in svc.esc.alerts(["open", "acknowledged"], user["name"])])


@app.post("/whatif")
async def whatif(body: WhatIf):
    try:
        return json.loads(json.dumps(svc.engine.whatif(_clock["now"], body.keys, body.mitigation), default=_plain))
    except KeyError as e:
        raise HTTPException(404, str(e))


@app.get("/assistant/status")
async def assistant_status():
    return {"available": assistant.available()}


# ------------------------------------------------------------------ web frontend
app.include_router(build_router(svc, _clock, TOKENS))
WEB = Path(__file__).resolve().parent.parent / "web"
app.mount("/static", StaticFiles(directory=WEB), name="static")


@app.get("/", include_in_schema=False)
async def index():
    return FileResponse(WEB / "index.html")
