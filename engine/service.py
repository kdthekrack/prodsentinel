"""ProdSentinel service: one cycle = assess -> explain -> alert -> escalate.

Used by the API (live mode) and by the evaluator / demo (replay mode).
"""
from __future__ import annotations

import pandas as pd

from engine.escalation import EscalationManager
from engine.narrate import Narrator
from engine.pack import Pack, load_pack
from engine.risk import RiskEngine


class EarlyWarningService:
    def __init__(self, pack: Pack | str, db_path=None, fresh=False):
        self.pack = load_pack(pack) if isinstance(pack, str) else pack
        self.engine = RiskEngine(self.pack)
        self.narrator = Narrator(self.pack)
        self.esc = EscalationManager(self.pack, db_path=db_path, fresh=fresh)
        self.last: dict | None = None

    def cycle(self, now) -> dict:
        now = pd.Timestamp(now)
        self.engine.mitigations = self.esc.active_mitigations(now)
        items = self.engine.assess(now)
        narr = {it["key"]: self.narrator.narrate(it) for it in items}
        sync = self.esc.sync(items, narr, now)
        escalated = self.esc.tick(now)
        self.last = dict(now=now, items=items, narrations=narr, sync=sync, escalated=escalated)
        return self.last

    # ------------------------------------------------------------------ views
    def plant_summary(self) -> dict:
        if not self.last:
            return {}
        # Same set the Control Room shows: alert-level risks plus risks whose alert is still open.
        open_keys = {a["key"] for a in self.esc.alerts(["open", "acknowledged"])}
        items = [x for x in self.last["items"] if x["alert"] or x["key"] in open_keys]
        exclusive = [x for x in items if x["category"] not in ("compound", "orders")]
        by_line = {}
        for l in self.pack.line_ids:
            li = [x for x in items if x["line"] == l]
            worst = max((x["severity"] for x in li), key=lambda s: ["low", "medium", "high", "critical"].index(s), default="ok")
            by_line[l] = dict(name=self.pack.line(l).get("name", l), status=worst, alerts=len(li),
                              money_at_risk=round(sum(x["expected_impact"] for x in li if x["category"] not in ("compound", "orders"))))
        orders = {o["order"] for x in items if x["category"] in ("orders", "compound") for o in x["evidence"].get("orders", [])}
        acted = [x for x in self.last["items"] if x.get("mitigation") and x["category"] not in ("compound", "orders")]
        protected = sum(max(0, x["impact_before_actions"] - x["expected_impact"]) for x in acted)
        return dict(assessed_at=str(self.last["now"]), open_alerts=len(items),
                    money_at_risk=round(sum(x["expected_impact"] for x in exclusive)),
                    money_at_risk_text=self.pack.money(sum(x["expected_impact"] for x in exclusive)),
                    orders_at_risk=len(orders), lines=by_line,
                    critical=sum(x["severity"] == "critical" for x in items),
                    money_protected=round(protected), money_protected_text=self.pack.money(protected),
                    risks_acted_on=len(acted))

    def for_role(self, role: str, line: str | None = None) -> list[dict]:
        """Alerts relevant to a role: those it owns or has actions on."""
        if not self.last:
            return []
        out = []
        for x in self.last["items"]:
            if not x["alert"] or (line and x["line"] != line):
                continue
            n = self.last["narrations"][x["key"]]
            if x["owner_role"] == role or any(a["owner_role"] == role for a in n["actions"]) or role == "plant_manager":
                out.append(dict(item=x, narration=n))
        return out
