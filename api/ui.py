"""Read-only data views and local sign-in used by the web frontend (web/)."""
from __future__ import annotations

import hashlib
import json
import os
import secrets as _secrets

import numpy as np
import pandas as pd
from fastapi import APIRouter, Header, HTTPException, Query, Request
from pydantic import BaseModel, Field


class Login(BaseModel):
    name: str
    pin: str = Field(min_length=4, max_length=12, pattern=r"^\d+$")


class Switch(BaseModel):
    name: str


class ChangePin(BaseModel):
    old: str
    new: str = Field(min_length=4, max_length=12, pattern=r"^\d+$")


class Settings(BaseModel):
    alert_threshold: float = Field(gt=0)
    severity_bands: dict[str, float]
    ack_timeout_min: dict[str, int]
    auto_resolve_after_h: float = Field(gt=0)
    owners: dict[str, str]
    contacts: dict[str, dict[str, str]] = {}


def _hash(pin: str, salt: str) -> str:
    return hashlib.pbkdf2_hmac("sha256", pin.encode(), salt.encode(), 200_000).hex()


def apply_settings(p, s: dict):
    """Mutate the pack in place so the running engine and escalation manager pick changes up."""
    p.raw["risk"]["alert_threshold"] = float(s["alert_threshold"])
    p.raw["escalation"]["severity_bands"].update({k: float(v) for k, v in s["severity_bands"].items()})
    p.raw["escalation"]["ack_timeout_min"].update({k: int(v) for k, v in s["ack_timeout_min"].items()})
    p.raw["escalation"]["auto_resolve_after_h"] = float(s["auto_resolve_after_h"])
    p.raw["owners"].update(s["owners"])
    for x in p.raw["people"]:
        c = s.get("contacts", {}).get(x["name"])
        if c:
            x["email"], x["phone"] = c.get("email", x["email"]), c.get("phone", x["phone"])


def build_router(svc, clock: dict, tokens: dict) -> APIRouter:
    r = APIRouter(prefix="/ui", tags=["ui"])
    p = svc.pack

    def now() -> pd.Timestamp:
        return clock["now"]

    state = p.out("state")
    pin_file, set_file = state / "pins.json", state / "settings.json"
    pins = json.loads(pin_file.read_text()) if pin_file.exists() else {}
    if set_file.exists():
        apply_settings(p, json.loads(set_file.read_text()))
        svc.cycle(clock["now"])

    def local_only(request: Request):
        if os.environ.get("EWS_OPEN_DEMO") == "1":      # hosted demo: anyone may switch person
            return
        if request.client and request.client.host not in ("127.0.0.1", "::1", "localhost", "testclient"):
            raise HTTPException(403, "sign-in is only allowed from this machine")

    def user_of(token: str | None) -> dict:
        name = tokens.get(token or "")
        if not name:
            raise HTTPException(401, "sign in first")
        return next(x for x in p["people"] if x["name"] == name)

    def recs(df: pd.DataFrame) -> list[dict]:
        return json.loads(df.to_json(orient="records", date_format="iso"))

    # ------------------------------------------------------------------ session
    @r.get("/people")
    async def people():
        return [dict(name=x["name"], title=p.role_title(x["role"]), has_pin=x["name"] in pins) for x in p["people"]]

    @r.post("/login")
    async def login(body: Login, request: Request):
        local_only(request)
        tok = next((t for t, n in tokens.items() if n == body.name), None)
        if not tok:
            raise HTTPException(404, "unknown person")
        rec = pins.get(body.name)
        if rec is None:                       # first sign-in: the person chooses their PIN
            salt = _secrets.token_hex(8)
            pins[body.name] = dict(salt=salt, hash=_hash(body.pin, salt))
            pin_file.write_text(json.dumps(pins, indent=1))
        elif not _secrets.compare_digest(rec["hash"], _hash(body.pin, rec["salt"])):
            raise HTTPException(401, "wrong PIN")
        person = next(x for x in p["people"] if x["name"] == body.name)
        return dict(token=tok, person=person, title=p.role_title(person["role"]))

    @r.post("/switch")
    async def switch(body: Switch, request: Request):
        """Demo mode: switch to any person with one click (no PIN). Local requests only."""
        local_only(request)
        tok = next((t for t, n in tokens.items() if n == body.name), None)
        if not tok:
            raise HTTPException(404, "unknown person")
        person = next(x for x in p["people"] if x["name"] == body.name)
        return dict(token=tok, person=person, title=p.role_title(person["role"]))

    @r.get("/me")
    async def me(x_user_token: str | None = Header(None)):
        u = user_of(x_user_token)
        return dict(person=u, title=p.role_title(u["role"]))

    @r.post("/pin")
    async def change_pin(body: ChangePin, x_user_token: str | None = Header(None)):
        u = user_of(x_user_token)
        rec = pins[u["name"]]
        if not _secrets.compare_digest(rec["hash"], _hash(body.old, rec["salt"])):
            raise HTTPException(401, "current PIN is wrong")
        salt = _secrets.token_hex(8)
        pins[u["name"]] = dict(salt=salt, hash=_hash(body.new, salt))
        pin_file.write_text(json.dumps(pins, indent=1))
        return {"ok": True}

    @r.put("/settings")
    async def save_settings(body: Settings, x_user_token: str | None = Header(None)):
        u = user_of(x_user_token)
        if u["role"] != "plant_manager":
            raise HTTPException(403, "only the Plant Manager can change settings")
        b = body.severity_bands
        if not all(k in b for k in ("critical", "high", "medium")) or not (b["critical"] > b["high"] > b["medium"] > 0):
            raise HTTPException(400, "severity bands must satisfy critical > high > medium > 0")
        if any(v <= 0 for v in body.ack_timeout_min.values()):
            raise HTTPException(400, "acknowledge timeouts must be positive")
        bad = [r_ for r_ in body.owners.values() if r_ not in p["roles"]]
        if bad:
            raise HTTPException(400, f"unknown role(s): {bad}")
        s = body.model_dump()
        apply_settings(p, s)
        set_file.write_text(json.dumps(s, indent=1))
        svc.cycle(clock["now"])               # re-assess with the new rules
        return {"ok": True}

    @r.get("/config")
    async def config():
        raw = p.raw
        return dict(company=raw["company"], currency=raw["currency"], risk=raw["risk"],
                    escalation=raw["escalation"], owners=raw["owners"],
                    roles={k: v["title"] for k, v in raw["roles"].items()}, people=raw["people"],
                    lines=[dict(id=l["id"], name=l.get("name"), product=l["product"]) for l in p.lines],
                    range=dict(start=str(p.start + pd.Timedelta(days=15)), end=str(p.end - pd.Timedelta(days=1))),
                    sensors=raw["sensors"])

    # ------------------------------------------------------------------ machines
    def _snapshot(t):
        e = svc.engine
        i = np.searchsorted(e.pred_ts, np.datetime64(t), side="right") - 1
        if i < 0:
            return pd.DataFrame()
        return e.preds_by_ts[e.pred_ts[i]]

    @r.get("/machines")
    async def machines():
        e = svc.engine
        snap = _snapshot(now())
        m = e.machines.reset_index()
        if not snap.empty:
            s = snap[["machine", "p_failure", "predicted_component", "driver_1"]].copy()
            s["probability"] = e.calib.predict(s.p_failure.to_numpy())
            s["flagged"] = s.p_failure >= e.threshold
            s["component"] = s.predicted_component.map(lambda c: p.component(c)["name"] if isinstance(c, str) else None)
            s["driver"] = s.driver_1.map(lambda d: e._feature_text(d) if isinstance(d, str) and d else "")
            m = m.merge(s.drop(columns=["predicted_component", "driver_1"]), on="machine", how="left")
        # Apply fixes people have made: a repaired machine's risk drops (fully when its alert is resolved).
        mit = svc.engine.mitigations or {}
        m["fixed"] = False
        m["fix_note"] = ""
        for i, row in m.iterrows():
            f = mit.get(f"machine:{row['machine']}")
            if not f:
                continue
            share = float(f["factor"])
            if "probability" in m and pd.notna(row.get("probability")):
                m.at[i, "probability"] = float(row["probability"]) * (1 - share)
            m.at[i, "flagged"] = bool(row.get("flagged")) and share < 1
            m.at[i, "fixed"] = share >= 1
            m.at[i, "fix_note"] = (f"Repaired - plan {f['alert_id']} completed" if share >= 1
                                   else f"Repair in progress - {share:.0%} of plan {f['alert_id']} done")
        return recs(m)

    @r.get("/machines/{machine}")
    async def machine(machine: str, days: int = Query(7, ge=1, le=30)):
        e = svc.engine
        if machine not in e.machines.index:
            raise HTTPException(404, "unknown machine")
        t = now()
        t0 = t - pd.Timedelta(days=days)
        mh = svc._mh if hasattr(svc, "_mh") else None
        if mh is None:
            from engine.sources import load_machine_health
            mh = svc._mh = load_machine_health(p)
        tel = mh["telemetry"]
        tel = tel[(tel.machine == machine) & (tel.ts > t0) & (tel.ts <= t)].sort_values("ts")
        err = mh["errors"]
        err = err[(err.machine == machine) & (err.ts > t0) & (err.ts <= t)]
        names = p.get("error_codes", {})
        mnt = mh["maintenance"]
        mnt = mnt[(mnt.machine == machine) & (mnt.ts <= t)]
        since = {}
        for code in p["components"]:
            last = mnt[mnt.component == code].ts.max()
            since[p.component(code)["name"]] = None if pd.isna(last) else round((t - last).total_seconds() / 86400, 1)
        notes = e.notes[(e.notes.machine == machine) & (e.notes.ts <= t)].tail(8)
        pr = [x for x in await machines() if x["machine"] == machine][0]
        hist = pd.concat([g[g.machine == machine][["ts", "p_failure"]] for ts, g in e.preds_by_ts.items()
                          if t0 < ts <= t]) if len(e.pred_ts) else pd.DataFrame()
        return dict(info=pr, sensors=p["sensors"], telemetry=recs(tel[["ts"] + list(p["sensors"])]),
                    alarms=[dict(ts=str(x.ts), name=names.get(x.code, x.code)) for x in err.itertuples()],
                    days_since=since, notes=recs(notes[["ts", "author", "note"]]),
                    risk_history=recs(hist.sort_values("ts")) if len(hist) else [], threshold=e.threshold)

    # ------------------------------------------------------------------ materials
    @r.get("/materials")
    async def materials():
        e, t = svc.engine, now()
        out = []
        for m, md in p["materials"].items():
            users = [l for l, bom in e.line_mats.items() if m in bom]
            stock, _ = e._stock_at(m, t)
            use_h = sum(e._rate(l) * e.base[l]["factor"] * e.line_mats[l][m] for l in users) if users else 0
            po = e.po[(e.po.material == m) & (e.po.order_ts <= t) & (e.po.actual_arrival > t)]
            pos = []
            for x in po.itertuples():
                rev = pd.notna(x.revision_notice_ts) and x.revision_notice_ts <= t
                pos.append(dict(po=x.po_id, qty=int(x.qty), ordered=str(x.order_ts), promised=str(x.promised_eta),
                                eta=str(x.revised_eta if rev else x.promised_eta), revised=bool(rev)))
            cover = (stock / use_h / 24) if use_h and not np.isnan(stock) else None
            out.append(dict(material=m, supplier=md["supplier"], lead_time_days=md["lead_time_days"],
                            reorder_days=md["reorder_days"], unit_cost=md["unit_cost"], lines=users,
                            stock=None if np.isnan(stock) else round(stock), hourly_use=round(use_h),
                            days_of_cover=None if cover is None else round(cover, 2), open_pos=pos))
        return out

    # ------------------------------------------------------------------ orders
    @r.get("/orders")
    async def orders():
        e, t = svc.engine, now()
        rows = []
        for line in p.line_ids:
            open_o = e._open_orders(line, t)
            if open_o.empty:
                continue
            proj = e._project(line, open_o, e._capacity(line, [], 24 * 30), t)
            proj = proj.assign(line=line)
            rows.append(proj)
        if not rows:
            return []
        df = pd.concat(rows)
        keep = [c for c in ["order_id", "line", "sku", "customer", "tier", "qty", "remaining", "placed_ts", "due_ts",
                            "proj_done", "proj_late_days", "proj_penalty", "rush", "invoice_value"] if c in df.columns]
        return recs(df[keep].sort_values("due_ts"))

    @r.get("/alert-history")
    async def alert_history(limit: int = Query(1000, le=5000)):
        return svc.esc.closed(limit)

    @r.get("/history")
    async def history(limit: int = Query(300, le=3000)):
        o = svc.engine.orders
        o = o[o.completed_ts.notna() & (o.completed_ts <= now())].sort_values("completed_ts", ascending=False).head(limit)
        return recs(o[["order_id", "line", "sku", "customer", "tier", "qty", "placed_ts", "due_ts", "completed_ts",
                       "days_late", "penalty", "rush"]])

    # ------------------------------------------------------------------ workforce + quality
    @r.get("/workforce")
    async def workforce(hours: int = Query(72, le=168)):
        e, t = svc.engine, now()
        s = e.shifts[(e.shifts.shift_start + pd.Timedelta(hours=e.SH) > t) &
                     (e.shifts.shift_start <= t + pd.Timedelta(hours=hours))].copy()
        known = s.notice_ts <= t
        s["present_known"] = np.where(known, s.present, s.planned)
        s = s[s.present_known < s.planned]
        roles = p["workforce"]["roles"]
        s["certified"] = s.role.map(lambda r: bool(roles.get(r, {}).get("certified")))
        return recs(s[["shift_start", "shift", "line", "role", "planned", "present_known", "certified", "notice_ts"]]
                    .sort_values("shift_start"))

    @r.get("/quality")
    async def quality(shifts: int = Query(18, le=90)):
        e, t = svc.engine, now()
        q = e.qual[e.qual.shift_start + pd.Timedelta(hours=e.SH) <= t]
        out = []
        for (line, metric), g in q.groupby(["line", "metric"]):
            g = g.sort_values("shift_start").tail(shifts)
            md = p["quality"]["metrics"][metric]
            out.append(dict(line=line, metric=metric, label=md["label"], unit=md["unit"], target=md["target"],
                            lsl=md["lsl"], usl=md["usl"], station=g.station.iloc[-1],
                            points=recs(g[["shift_start", "mean", "sd", "out_of_spec"]])))
        return out

    return r
