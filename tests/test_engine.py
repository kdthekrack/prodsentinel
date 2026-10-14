"""Core guarantees of the engine. Run: python -m pytest -q"""
import copy

import numpy as np
import pandas as pd
import pytest

from engine.escalation import EscalationManager
from engine.narrate import Narrator
from engine.pack import PackError, Pack, load_pack
from engine.risk import LineBacklog, RiskEngine

PACK = "industries/beverage_bottling.yaml"
TIMES = ["2015-10-15 06:00", "2015-11-05 13:00", "2015-12-10 22:00"]
FAR = pd.Timestamp("2099-01-01")


@pytest.fixture(scope="module")
def pack():
    return load_pack(PACK)


@pytest.fixture(scope="module")
def engine(pack):
    return RiskEngine(pack)


def _truncate(e: RiskEngine, t: pd.Timestamp) -> RiskEngine:
    """Copy of the engine that holds ONLY information knowable at time t."""
    c = copy.copy(e)
    sh = pd.Timedelta(hours=e.SH)
    c.prod = e.prod[e.prod.shift_start + sh <= t].copy()
    c.inv = e.inv[e.inv.date <= t].copy()
    po = e.po[e.po.order_ts <= t].copy()
    unknown = po.revision_notice_ts.isna() | (po.revision_notice_ts > t)
    po.loc[unknown, ["revised_eta", "revision_notice_ts"]] = pd.NaT
    po.loc[po.actual_arrival > t, "actual_arrival"] = FAR           # "not arrived yet" is all that is known
    c.po = po
    c.shifts = e.shifts[e.shifts.notice_ts <= t].copy()
    c.qual = e.qual[e.qual.shift_start + sh <= t].copy()
    o = e.orders[e.orders.placed_ts <= t].copy()
    o[["completed_ts", "days_late", "penalty"]] = np.nan
    c.orders = o
    c.notes = e.notes[e.notes.ts <= t].copy()
    c.errors = e.errors[e.errors.ts <= t].copy()
    c.preds_by_ts = {k: v for k, v in e.preds_by_ts.items() if k <= t}
    c.pred_ts = np.array(sorted(c.preds_by_ts))
    c.backlogs = {}
    return c


def _strip(items):
    return [(x["key"], x["expected_impact"], x["probability"], x["starts_in_h"], x["alert"]) for x in items]


@pytest.mark.parametrize("t", TIMES)
def test_no_lookahead(engine, t):
    """Assessment with full data == assessment with data cut off at t."""
    t = pd.Timestamp(t)
    full = _strip(engine.assess(t))
    engine.backlogs = {}
    cut = _strip(_truncate(engine, t).assess(t))
    assert full == cut


def test_backlog_matches_simulator(pack, engine):
    """Replayed dispatch at the end of the year reproduces the simulator's completions."""
    o = engine.orders[engine.orders.line == "L1"]
    bl = LineBacklog(o, engine.prod[engine.prod.line == "L1"], pack.shift_hours)
    t = pd.Timestamp("2015-06-01 06:00")
    bl.advance(t)
    open_ids = set(bl.open_orders().order_id)
    truth = set(o[(o.placed_ts <= t) & ((o.completed_ts > t) | o.completed_ts.isna())].order_id)
    assert open_ids == truth


def test_every_alert_has_owner_actions_and_plain_text(pack, engine):
    n = Narrator(pack)
    items = [x for x in engine.assess(pd.Timestamp(TIMES[0])) if x["alert"]]
    assert items
    for x in items:
        m = n.narrate(x)
        assert m["headline"] and m["summary"] and m["cost"].startswith("About")
        assert m["actions"] and all(a["owner_name"] and a["due_by"] for a in m["actions"])
        text = " ".join([m["headline"], m["summary"], *m["why"], *(a["text"] for a in m["actions"])])
        assert "{" not in text and "comp1" not in text and "nan" not in text.lower().split()


def test_escalation_chain(pack, tmp_path):
    esc = EscalationManager(pack, db_path=tmp_path / "a.db", fresh=True)
    t0 = pd.Timestamp("2015-10-15 06:00")
    item = dict(key="material:X:L5", category="material", line="L5", alert=True, severity="high",
                expected_impact=200000, owner_role="purchasing")
    narr = {item["key"]: dict(headline="h", summary="s", why=[], cost="c", likelihood="l",
                              actions=[dict(text="do", owner_role="purchasing", owner_name="Sneha Kulkarni",
                                            owner_title="Purchasing Manager", due_by=str(t0), due_text="now")])}
    esc.sync([item], narr, t0)
    a = esc.alerts()[0]
    assert a["holder_name"] == "Sneha Kulkarni" and a["status"] == "open"
    esc.tick(t0 + pd.Timedelta(minutes=29))
    assert esc.alerts()[0]["holder_role"] == "purchasing"          # not yet overdue (high = 30 min)
    esc.tick(t0 + pd.Timedelta(minutes=31))
    a = esc.alerts()[0]
    assert a["holder_role"] == "plant_manager" and a["level"] == 1
    esc.acknowledge(a["alert_id"], "Anita Rao", t0 + pd.Timedelta(minutes=40))
    assert esc.alerts()[0]["status"] == "acknowledged"
    esc.tick(t0 + pd.Timedelta(hours=5))                           # action overdue -> escalation event
    types = [e["type"] for e in esc.history(a["alert_id"])]
    assert types.count("escalated") == 1 and "reminder" in types
    esc.sync([], {}, t0 + pd.Timedelta(hours=20))                  # condition cleared > 12h
    assert esc.alerts()[0]["status"] == "auto_resolved"


def test_pack_validation_rejects_bad_config(pack):
    raw = copy.deepcopy(pack.raw)
    raw["lines"][0]["bom"]["Unobtainium"] = 1
    with pytest.raises(PackError):
        Pack(raw=raw, path=pack.path)
    raw = copy.deepcopy(pack.raw)
    raw["owners"]["machine"] = "nobody"
    with pytest.raises(PackError):
        Pack(raw=raw, path=pack.path)


def test_penalty_rules(pack):
    assert pack.penalty("FreshMart Retail", 100000, 0) == 0
    assert pack.penalty("FreshMart Retail", 100000, 1.2) == 100000 * 0.05 * 2      # per day, rounded up
    assert pack.penalty("Metro Cash Wholesale", 100000, 3) == 100000 * 0.03        # one-off
    assert pack.penalty("Highway Dhaba Network", 0, 2.5) == 15000                  # flat per day


def test_actions_reduce_risk_and_stay_resolved(pack, tmp_path):
    """Doing actions lowers money at risk; a resolved alert is not re-created while its fix holds."""
    from engine.service import EarlyWarningService
    s = EarlyWarningService(pack, db_path=tmp_path / "m.db", fresh=True)
    t = pd.Timestamp("2015-10-16 06:00")
    s.cycle(t)
    before = s.plant_summary()["money_at_risk"]
    a = next(x for x in s.esc.alerts(["open"]) if x["category"] == "material")
    s.esc.complete_action(a["alert_id"], 0, "Sneha Kulkarni", t)
    s.cycle(t)
    half = s.plant_summary()
    assert half["money_at_risk"] < before and half["money_protected"] > 0
    s.esc.resolve(a["alert_id"], "Anita Rao", t, "expedited delivery")
    s.cycle(t + pd.Timedelta(hours=3))
    after = s.plant_summary()
    assert after["money_at_risk"] < half["money_at_risk"]
    assert not [x for x in s.esc.alerts(["open", "acknowledged"]) if x["key"] == a["key"]]
    item = next(x for x in s.last["items"] if x["key"] == a["key"])
    assert item["alert"] is False and item["mitigation"]["factor"] == 1.0


def test_all_actions_done_closes_alert_into_history(pack, tmp_path):
    from engine.service import EarlyWarningService
    s = EarlyWarningService(pack, db_path=tmp_path / "h.db", fresh=True)
    t = pd.Timestamp("2015-10-16 06:00")
    s.cycle(t)
    a = next(x for x in s.esc.alerts(["open"]) if x["category"] == "material")
    for act in s.esc.actions(a["alert_id"]):
        s.esc.complete_action(a["alert_id"], act["idx"], "Sneha Kulkarni", t)
    s.cycle(t)
    assert a["alert_id"] not in {x["alert_id"] for x in s.esc.alerts(["open", "acknowledged"])}
    h = next(x for x in s.esc.closed() if x["alert_id"] == a["alert_id"])
    assert h["status"] == "resolved" and h["resolution"] == "All actions completed"
    assert h["actions_done"] == h["actions_total"] and h["money_protected"] > 0 and h["resolved_by"] == "Sneha Kulkarni"


def test_kpi_matches_open_alert_cards_after_compound_fix(pack, tmp_path):
    from engine.service import EarlyWarningService
    s = EarlyWarningService(pack, db_path=tmp_path / "k.db", fresh=True)
    t = pd.Timestamp("2015-10-16 06:00")
    s.cycle(t)
    c = next(x for x in s.esc.alerts(["open"]) if x["category"] == "compound")
    for a in s.esc.actions(c["alert_id"]):
        s.esc.complete_action(c["alert_id"], a["idx"], "Anita Rao", t)
    s.cycle(t)
    open_ = s.esc.alerts(["open", "acknowledged"])
    assert not [a for a in open_ if a["key"] in c["payload"]["item"]["related"]]      # parts closed with it
    cur = {i["key"]: i["expected_impact"] for i in s.last["items"]}
    cards = sum(cur.get(a["key"], 0) for a in open_ if a["category"] not in ("compound", "orders"))
    assert s.plant_summary()["money_at_risk"] == cards
