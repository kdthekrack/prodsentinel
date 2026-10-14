"""API workflow, auth, adapters, notifications, what-if, narration coverage, second pack."""
import http.server
import json
import threading

import pandas as pd
import pytest
from fastapi.testclient import TestClient

from engine.escalation import Webhook
from engine.narrate import Narrator
from engine.pack import load_pack
from engine.risk import RiskEngine
from engine.sources import PLANT_TABLES, load_machine_health, load_plant

BOTTLING = "industries/beverage_bottling.yaml"
BISCUIT = "industries/biscuit_bakery.yaml"


# ---------------------------------------------------------------- API
@pytest.fixture(scope="module")
def api():
    import api.main as m
    c = TestClient(m.app)
    tok = {v: k for k, v in m.TOKENS.items()}
    return c, tok


def H(tok, name):
    return {"X-User-Token": tok[name]}


def test_health_and_plant(api):
    c, _ = api
    assert c.get("/health").json()["ok"]
    p = c.get("/plant").json()
    assert len(p["lines"]) == 5 and p["machines"] == 100


def test_writes_require_valid_token(api):
    c, _ = api
    assert c.post("/clock/advance", json={"hours": 3}).status_code == 422          # header missing
    assert c.post("/clock/advance", json={"hours": 3}, headers={"X-User-Token": "bad"}).status_code == 401
    assert c.post("/ask", json={"question": "hello there"}).status_code == 422


def test_full_alert_workflow(api):
    c, tok = api
    pm = H(tok, "Anita Rao")
    assert c.post("/clock/set", json={"ts": "2015-10-14 06:00"}, headers=pm).status_code == 200
    assert c.post("/clock/set", json={"ts": "not a date"}, headers=pm).status_code == 400
    assert c.post("/clock/set", json={"ts": "2030-01-01"}, headers=pm).status_code == 400
    r = c.post("/clock/advance", json={"hours": 6}, headers=pm).json()
    assert r["now"] == "2015-10-14 12:00:00"
    alerts = c.get("/alerts").json()
    assert alerts, "expected open alerts"
    a = alerts[0]
    assert a["narration"]["headline"] and a["narration"]["actions"]
    holder = a["holder_name"]
    r = c.post(f"/alerts/{a['alert_id']}/ack", json={"note": "on it"}, headers=H(tok, holder))
    assert r.status_code == 200 and r.json()["alert"]["acknowledged_by"] == holder
    assert c.post(f"/alerts/{a['alert_id']}/ack", json={}, headers=H(tok, holder)).status_code == 409
    aid = a["alert_id"]
    acts = c.get(f"/alerts/{aid}").json()["actions"]
    owner = acts[0]["owner_name"]
    other = next(p["name"] for p in c.get("/plant").json()["people"] if p["name"] != owner and p["role"] != "plant_manager")
    assert c.post(f"/alerts/{aid}/actions/99/done", json={}, headers=pm).status_code == 404
    assert c.post(f"/alerts/{aid}/actions/0/done", json={}, headers=H(tok, owner)).status_code == 409   # not authorized yet
    assert c.post(f"/alerts/{aid}/authorize", json={}, headers=H(tok, other)).status_code == 403        # only the approver
    assert c.post(f"/alerts/{aid}/authorize", json={}, headers=pm).status_code == 200
    assert c.post(f"/alerts/{aid}/authorize", json={}, headers=pm).status_code == 409                   # already authorized
    assert c.post(f"/alerts/{aid}/actions/0/done", json={}, headers=H(tok, other)).status_code == 403   # someone else's task
    assert c.post(f"/alerts/{aid}/actions/0/done", json={}, headers=H(tok, owner)).status_code == 200
    assert c.post(f"/alerts/{aid}/actions/0/done", json={}, headers=H(tok, owner)).status_code == 409   # already done
    tasks = [n for n in c.get("/notifications?limit=500").json() if n["alert_id"] == aid and n["subject"].startswith("[TASK]")]
    assert {n["to_name"] for n in tasks} == {x["owner_name"] for x in acts}
    assert c.post(f"/alerts/{aid}/comment", json={"note": "called supplier"}, headers=pm).status_code == 200
    assert c.post(f"/alerts/{aid}/resolve", json={"resolution": "x"}, headers=H(tok, other)).status_code in (403, 422)
    r = c.post(f"/alerts/{a['alert_id']}/resolve", json={"resolution": "fixed at changeover"}, headers=pm)
    assert r.json()["alert"]["status"] == "resolved"
    assert c.post(f"/alerts/{a['alert_id']}/resolve", json={"resolution": "again"}, headers=pm).status_code == 409
    types = [e["type"] for e in c.get(f"/alerts/{a['alert_id']}").json()["history"]]
    assert types[:1] == ["created"] and "acknowledged" in types and types[-1] == "resolved"
    assert c.get("/alerts/ALR-99999").status_code == 404


def test_escalation_through_api(api):
    c, tok = api
    pm = H(tok, "Anita Rao")
    c.post("/clock/set", json={"ts": "2015-11-10 06:00"}, headers=pm)
    r = c.post("/clock/advance", json={"hours": 12}, headers=pm).json()
    assert r["escalations"], "unacknowledged alerts must escalate"
    assert all(e["to"] in {"Anita Rao", None} for e in r["escalations"])


def test_views_orders_whatif_me(api):
    c, tok = api
    assert c.get("/views/nobody").status_code == 404
    assert c.get("/lines/ZZ/orders").status_code == 404
    assert isinstance(c.get("/lines/L1/orders").json(), list)
    assert "items" in c.get("/views/maintenance_lead").json()
    risks = c.get("/risks").json()
    key = next(x["item"]["key"] for x in risks if x["item"]["category"] in ("machine", "material", "workforce", "quality"))
    w = c.post("/whatif", json={"keys": [key]}).json()
    assert w["saved"] >= 0 and w["lines"]
    assert c.post("/whatif", json={"keys": ["machine:NOPE"]}).status_code == 404
    assert c.post("/whatif", json={"keys": [key], "mitigation": 2}).status_code == 422
    me = c.get("/me", headers=H(tok, "Arjun Patel")).json()
    assert me["person"]["role"] == "maintenance_lead"


def test_assistant_degrades_without_key(api, monkeypatch):
    c, tok = api
    import api.main as m
    monkeypatch.setattr(m.assistant, "client", None)
    for q in ["Which line is most at risk?", "Why is line 5 at risk?", "Any stock problems?", "What should I do?",
              "Who is responsible for machines on line 2?", "What if we fix line 1?", "status of ALR-00001",
              "quality issues on line 9?"]:
        r = c.post("/ask", json={"question": q}, headers=H(tok, "Anita Rao")).json()
        assert r["ok"] and r["mode"] == "offline" and len(r["answer"]) > 10, (q, r)
        assert "{" not in r["answer"] and "nan" not in r["answer"].lower().split(), r
    key = c.get("/risks").json()[0]["item"]["key"]
    assert c.get(f"/risks/{key}", params={"language": "Hindi"}).status_code == 200


# ---------------------------------------------------------------- engine pieces
def test_whatif_bounds():
    e = RiskEngine(load_pack(BOTTLING))
    t = "2015-11-12 15:00"
    keys = [x["key"] for x in e.assess(t) if x["alert"] and x["category"] in ("machine", "material")][:2]
    full, none = e.whatif(t, keys, 1.0), e.whatif(t, keys, 0.0)
    assert none["saved"] == 0 and full["saved"] > 0


@pytest.mark.parametrize("pack_path,times", [
    (BOTTLING, ["2015-10-15 06:00", "2015-10-24 15:00", "2015-11-13 12:00", "2015-11-27 00:00", "2015-12-05 06:00"]),
    (BISCUIT, ["2015-10-01 15:00", "2015-10-02 06:00", "2015-11-20 06:00", "2015-12-10 06:00"])])
def test_narration_all_categories(pack_path, times):
    pack = load_pack(pack_path)
    e, n = RiskEngine(pack), Narrator(pack)
    seen = set()
    for t in times:
        for x in e.assess(t):
            m = n.narrate(x)
            seen.add(x["category"])
            text = " ".join([m["headline"], m["summary"], m["cost"], *m["why"], *(a["text"] for a in m["actions"])])
            assert "{" not in text and "}" not in text, text
            assert " nan" not in text.lower() and "None" not in text, text
            assert all(a["owner_name"] for a in m["actions"])
    assert {"machine", "material", "workforce", "quality"} <= seen


def test_second_pack_is_consistent():
    pack = load_pack(BISCUIT)
    assert pack.id == "biscuit_bakery" and len(pack.machines) == 48
    items = RiskEngine(pack).assess("2015-11-20 06:00")
    assert all(x["line"] in pack.line_ids for x in items)
    assert all(x["owner_role"] in pack["roles"] for x in items)


# ---------------------------------------------------------------- adapters
def test_csv_adapters_roundtrip(tmp_path):
    """A plant supplying its own CSVs (canonical columns) gets identical tables."""
    pack = load_pack(BOTTLING)
    mh = load_machine_health(pack)
    cut = pack.start + pd.Timedelta(days=3)
    tables = {}
    for name, df in mh.items():
        sub = df[df.ts <= cut] if "ts" in df else df
        f = tmp_path / f"{name}.csv"
        sub.rename(columns={"ts": "time", "machine": "asset"}).to_csv(f, index=False)
        tables[name] = {"file": str(f), "columns": {"ts": "time", "machine": "asset"}}
    raw = dict(pack.raw)
    raw["data_sources"] = {"machine_health": {"provider": "csv", "tables": tables},
                           "plant": {"provider": "csv", "tables": {t: str(pack.out("synthetic") / f"{t}.csv") for t in PLANT_TABLES}}}
    from engine.pack import Pack
    p2 = Pack(raw=raw, path=pack.path)
    mh2 = load_machine_health(p2)
    assert len(mh2["telemetry"]) == len(mh["telemetry"][mh["telemetry"].ts <= cut])
    assert set(mh2["failures"].machine) <= set(pack.machines.machine)
    pl1, pl2 = load_plant(pack), load_plant(p2)
    assert all(len(pl1[t]) == len(pl2[t]) for t in PLANT_TABLES)


def test_webhook_channel(monkeypatch):
    got = []

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_POST(self):
            got.append(json.loads(self.rfile.read(int(self.headers["Content-Length"]))))
            self.send_response(200)
            self.end_headers()

        def log_message(self, *a):
            pass

    srv = http.server.HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=srv.handle_request, daemon=True).start()
    monkeypatch.setenv("ESCALATION_WEBHOOK_URL", f"http://127.0.0.1:{srv.server_port}/hook")
    status = Webhook().send(dict(subject="[HIGH] test", body="hello"))
    srv.server_close()
    assert status == "sent" and got and "[HIGH] test" in got[0]["text"]
    monkeypatch.delenv("ESCALATION_WEBHOOK_URL")
    assert Webhook().send(dict(subject="s", body="b")) == "skipped"


def test_ui_settings_and_screens(api):
    """Frontend-only endpoints: config, screens, settings permissions and validation."""
    import shutil
    import api.main as m
    c, tok = api
    state = m.svc.pack.out("state")
    backup = {f: (state / f).read_bytes() for f in ("settings.json",) if (state / f).exists()}
    raw_backup = json.loads(json.dumps({k: m.svc.pack.raw[k] for k in ("risk", "escalation", "owners")}))
    try:
        cfg = c.get("/ui/config").json()
        body = dict(alert_threshold=cfg["risk"]["alert_threshold"], severity_bands=cfg["escalation"]["severity_bands"],
                    ack_timeout_min=cfg["escalation"]["ack_timeout_min"],
                    auto_resolve_after_h=cfg["escalation"]["auto_resolve_after_h"], owners=cfg["owners"])
        assert c.put("/ui/settings", json=body, headers=H(tok, "Arjun Patel")).status_code == 403
        bad = dict(body, severity_bands={"critical": 1, "high": 5, "medium": 3})
        assert c.put("/ui/settings", json=bad, headers=H(tok, "Anita Rao")).status_code == 400
        assert c.put("/ui/settings", json=body, headers=H(tok, "Anita Rao")).json()["ok"]
        for path in ["/ui/machines", "/ui/materials", "/ui/orders", "/ui/workforce", "/ui/quality", "/ui/history?limit=50", "/ui/people"]:
            assert c.get(path).status_code == 200, path
        mid = m.svc.pack.machines.machine.iloc[0]
        assert c.get(f"/ui/machines/{mid}?days=3").status_code == 200
        assert c.get("/ui/machines/NOPE?days=3").status_code == 404
        assert c.get("/").status_code == 200 and c.get("/static/app.js").status_code == 200
    finally:
        for f, b in backup.items():
            (state / f).write_bytes(b)
        if "settings.json" not in backup and (state / "settings.json").exists():
            (state / "settings.json").unlink()
        m.svc.pack.raw.update(raw_backup)


def test_machine_telemetry_reflects_repairs(api):
    """After the machine's tasks are done, Machine Telemetry shows it repaired with low risk."""
    import api.main as m
    c, tok = api
    pm = H(tok, "Anita Rao")
    c.post("/clock/set", json={"ts": "2015-10-15 06:00"}, headers=pm)
    c.post("/clock/advance", json={"hours": 24}, headers=pm)
    a = next(x for x in c.get("/alerts").json() if x["category"] == "machine")
    mach = a["key"].split(":", 1)[1]
    before = next(x for x in c.get("/ui/machines").json() if x["machine"] == mach)
    assert c.post(f"/alerts/{a['alert_id']}/authorize", json={}, headers=pm).status_code == 200
    for act in c.get(f"/alerts/{a['alert_id']}").json()["actions"]:
        assert c.post(f"/alerts/{a['alert_id']}/actions/{act['idx']}/done", json={}, headers=H(tok, act["owner_name"])).status_code == 200
    after = next(x for x in c.get("/ui/machines").json() if x["machine"] == mach)
    assert after["fixed"] and not after["flagged"] and after["probability"] < before["probability"]
    assert c.get(f"/alerts/{a['alert_id']}").json()["alert"]["status"] == "resolved"
    assert c.get(f"/ui/machines/{mach}?days=3").json()["info"]["fixed"]
