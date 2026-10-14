"""Risk engine: detect, predict, fuse and price disruptions at a point in time.

`RiskEngine.assess(t)` uses ONLY information knowable at time t:
  - machine-failure predictions from rows with ts <= t
  - production of shifts that have finished by t
  - inventory snapshots, receipts and supplier ETA notices up to t
  - staffing gaps whose notice was given by t
  - quality samples of finished shifts
  - customer orders placed by t

For each line it builds an hourly capacity forecast under every detected
risk, projects the order backlog (earliest-due-date), and prices each risk:

  expected impact = P x (lost units x margin + extra scrap x (scrap cost + margin) + repair cost)
                    + marginal late-order penalty (projection with vs. without the risk)

Risks on the same line that overlap in time are also reported as a compound
risk whose penalty is the joint effect (often larger than the parts).
"""
from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np
import pandas as pd
from scipy.stats import norm
from sklearn.isotonic import IsotonicRegression

from engine.pack import Pack
from engine.sources import load_machine_health, load_plant

FEATURE_LABELS = {
    "errors_total_24h": "number of machine alarms in the last 24 hours",
    "days_since": "a long time since the {c} was last replaced",
    "mean_24h": "average {s} over the last 24 hours",
    "mean_3h": "average {s} over the last 3 hours",
    "std_24h": "how unstable the {s} has been over 24 hours",
    "std_3h": "how unstable the {s} has been over 3 hours",
    "trend_24h": "change in {s} compared with the day before",
    "count_24h": "'{e}' alarms in the last 24 hours",
    "age": "machine age",
    "model": "machine model",
}


@dataclass
class Effect:
    line: str
    start_h: float       # hours after t
    end_h: float
    loss: float = 0.0    # share of line capacity lost (0..1)
    scrap: float = 0.0   # extra scrap share of output (0..1)


@dataclass
class Risk:
    key: str
    category: str
    line: str
    title: str
    probability: float
    effects: list[Effect]
    repair_cost: float = 0.0
    evidence: dict = field(default_factory=dict)
    context: dict = field(default_factory=dict)
    impact: dict = field(default_factory=dict)
    related: list[str] = field(default_factory=list)
    window: tuple[float, float] | None = None     # when it could hit (uncertainty), for overlap checks

    @property
    def span(self):
        return self.window if self.window else (self.start_h, self.end_h)

    @property
    def start_h(self):
        return min(e.start_h for e in self.effects) if self.effects else 0.0

    @property
    def end_h(self):
        return max(e.end_h for e in self.effects) if self.effects else 0.0


class LineBacklog:
    """Replays earliest-due-date dispatch on finished shifts to know each open order's remaining qty at t."""

    def __init__(self, orders: pd.DataFrame, production: pd.DataFrame, shift_h: int):
        self.o = orders.sort_values("placed_ts").reset_index(drop=True)
        self.placed = self.o.placed_ts.to_numpy()
        self.key = list(zip(self.o.due_ts, self.o.tier.map({"A": 0, "B": 1, "C": 2})))
        self.rem = self.o.qty.astype(float).to_numpy().copy()
        self.prod = production.sort_values("shift_start").reset_index(drop=True)
        self.sh = pd.Timedelta(hours=shift_h)
        self.ptr = self.sp = 0
        self.open: list[int] = []
        self.t = pd.Timestamp.min

    def advance(self, t: pd.Timestamp):
        if t < self.t:
            raise ValueError("backlog can only move forward in time")
        self.t = t
        P = self.prod
        while self.sp < len(P) and P.shift_start[self.sp] + self.sh <= t:
            s0 = P.shift_start[self.sp]
            while self.ptr < len(self.placed) and self.placed[self.ptr] <= np.datetime64(s0):
                self.open.append(self.ptr)
                self.ptr += 1
            self.open.sort(key=lambda i: self.key[i])
            cap = float(P.good_units[self.sp])
            while self.open and cap > 0:
                i = self.open[0]
                take = min(cap, self.rem[i])
                self.rem[i] -= take
                cap -= take
                if self.rem[i] <= 0:
                    self.open.pop(0)
            self.sp += 1
        # orders placed after the last finished shift but before t are open too
        while self.ptr < len(self.placed) and self.placed[self.ptr] <= np.datetime64(t):
            self.open.append(self.ptr)
            self.ptr += 1
        self.open.sort(key=lambda i: self.key[i])

    def open_orders(self) -> pd.DataFrame:
        idx = [i for i in self.open if self.rem[i] > 0]
        df = self.o.iloc[idx].copy()
        df["remaining"] = self.rem[idx]
        return df


class RiskEngine:
    def __init__(self, pack: Pack):
        self.p = pack
        self.r = pack["risk"]
        self.H = int(self.r["horizon_h"])
        self.mat_H = int(self.r.get("material_lookahead_h", 168))
        self.SH = pack.shift_hours
        t = load_plant(pack)
        self.t = t
        self.prod = t["production_log"]
        self.inv = t["material_inventory"].sort_values("date")
        self.po = t["purchase_orders"]
        self.shifts = t["shifts"]
        self.qual = t["quality_inspections"]
        self.orders = t["customer_orders"]
        self.notes = t["notes"]
        self.machines = t["machines"].set_index("machine")
        self.stations = pack.stations.set_index("station_id")
        mh = load_machine_health(pack)
        self.errors = mh["errors"]

        preds = pd.read_pickle(pack.out("processed") / "machine_predictions.pkl")
        self.preds_by_ts = {k: g for k, g in preds.groupby("ts")}
        self.pred_ts = np.array(sorted(self.preds_by_ts))
        import joblib
        self.threshold = joblib.load(pack.out("models") / "failure_model.joblib")["threshold"]
        # Calibrate model scores to real probabilities on the validation split only.
        val = preds[preds.split == "val"]
        self.calib = IsotonicRegression(out_of_bounds="clip", y_min=0.0, y_max=1.0).fit(
            val.p_failure, (val.failure_label != "none").astype(int))
        # Typical hours-to-failure when the model alerts (training split only).
        tp = preds[(preds.split == "train") & preds.alert & preds.hours_to_failure.notna()]
        self.hours_to_failure = float(tp.hours_to_failure.median()) if len(tp) else self.r["failure_model_horizon_h"] / 2

        self.line_mats = {l["id"]: l["bom"] for l in pack.lines}
        self.backlogs: dict[str, LineBacklog] = {}
        # Actions people have taken: key -> {factor 0..1, by, alert_id, since, until}. Set by the service.
        self.mitigations: dict[str, dict] = {}

    # ================================================================ public
    def assess(self, t) -> list[dict]:
        t = pd.Timestamp(t)
        self.now = t
        base = {l: self._base_factor(l, t) for l in self.p.line_ids}
        self.base = base
        risks: list[Risk] = []
        risks += self._machine_risks(t)
        self._machine_alert_keys = [r.key for r in risks]
        risks += self._material_risks(t)
        risks += self._workforce_risks(t)
        risks += self._quality_risks(t)
        self._apply_mitigations(risks)
        items = self._price(t, risks)
        return sorted(items, key=lambda x: -x["priority"])

    def _apply_mitigations(self, risks: list[Risk]):
        """Actions taken by people reduce the chance a risk still happens.
        factor = share of the risk removed (actions done / total, 1.0 when resolved)."""
        self._before = {}
        for r in risks:
            m = self.mitigations.get(r.key)
            if not m:
                continue
            self._before[r.key] = r.probability
            r.probability = r.probability * (1 - m["factor"])
            r.evidence["mitigation"] = m

    def whatif(self, t, keys: list[str], mitigation: float = 1.0) -> dict:
        """Money saved if the listed risks are prevented (mitigation=1) or reduced (0..1).

        Re-prices every affected line with those risks scaled down and compares
        expected losses + projected penalties before and after."""
        t = pd.Timestamp(t)
        self.now = t
        self.base = {l: self._base_factor(l, t) for l in self.p.line_ids}
        risks = (self._machine_risks(t) + self._material_risks(t) + self._workforce_risks(t) + self._quality_risks(t))
        known = {r.key for r in risks}
        missing = [k for k in keys if k not in known]
        if missing:
            raise KeyError(f"not active at {t}: {missing}")
        out = {}
        for line in sorted({r.line for r in risks if r.key in keys}):
            lr = [r for r in risks if r.line == line]
            open_o = self._open_orders(line, t)
            L = int(min(24 * 45, max(self.H, ((open_o.due_ts.max() - t).total_seconds() / 3600 + 24 * 7) if len(open_o) else self.H)))

            def cost(scale):
                eff = [(r.probability * scale(r), e) for r in lr for e in r.effects]
                pen = float(self._project(line, open_o, self._capacity(line, eff, L), t).proj_penalty.sum())
                direct = 0.0
                for r in lr:
                    d = self._direct(line, r)
                    direct += scale(r) * (d["lost_margin"] + d["scrap"] + d["repair"])
                return direct, pen

            d0, p0 = cost(lambda r: 1.0)
            d1, p1 = cost(lambda r: (1 - mitigation) if r.key in keys else 1.0)
            out[line] = dict(before=round(d0 + p0), after=round(d1 + p1), saved=round(d0 + p0 - d1 - p1),
                             penalty_before=round(p0), penalty_after=round(p1))
        total = sum(v["saved"] for v in out.values())
        return dict(assessed_at=str(t), risks=keys, mitigation=mitigation, lines=out,
                    saved=round(total), saved_text=self.p.money(total))

    # ================================================================ baseline
    def _base_factor(self, line, t) -> dict:
        """Normal performance of the line over the last 14 days (finished shifts only)."""
        p = self.prod[(self.prod.line == line) & (self.prod.shift_start + pd.Timedelta(hours=self.SH) <= t) &
                      (self.prod.shift_start >= t - pd.Timedelta(days=14))]
        if p.empty:
            return {"factor": 0.9, "scrap": 0.015}
        perf = float(p.actual_units.sum() / p.planned_units.sum())      # average, incl. normal small losses
        scrap = float(p.scrap_units.sum() / max(p.actual_units.sum(), 1))
        return {"factor": min(perf, 1.0), "scrap": scrap}

    def _rate(self, line):
        return self.p.planned_rate(line)

    # ================================================================ machine
    def _machine_risks(self, t) -> list[Risk]:
        i = np.searchsorted(self.pred_ts, np.datetime64(t), side="right") - 1
        if i < 0:
            return []
        snap = self.preds_by_ts[self.pred_ts[i]]
        snap = snap[snap.p_failure >= self.threshold]
        # Confirmation: a reading above threshold must repeat on the previous cycle, unless it is very strong.
        strong = float(self.r.get("machine_confirm_strong", 0.85))
        if i > 0 and len(snap):
            prev = self.preds_by_ts[self.pred_ts[i - 1]].set_index("machine").p_failure
            ok = (snap.machine.map(prev).fillna(0) >= self.threshold) | (snap.p_failure >= strong)
            snap = snap[ok.to_numpy()]
        out = []
        mt_short = self._maint_tech_short(t)
        for r in snap.itertuples():
            st = self.stations.loc[r.station_id]
            comp = self.p.component(r.predicted_component)
            prob = min(0.98, float(self.calib.predict([r.p_failure])[0]))   # never claim certainty
            repair = float(np.mean(comp["repair_hours"]))
            delay = 0.0
            if r.line in mt_short:
                delay = max(v.get("repair_delay_h", 0) for v in self.p["workforce"]["roles"].values())
            if st["mode"] == "series":
                loss = 1.0
            else:
                loss = 1 - min(1.0, (st["count"] - 1) / st["required"])
            start = max(1.0, min(self.hours_to_failure, self.r["failure_model_horizon_h"]))
            ev = {
                "failure_probability": round(prob, 3),
                "model_score": round(float(r.p_failure), 3),
                "likely_component": comp["name"],
                "expected_within_h": int(self.r["failure_model_horizon_h"]),
                "repair_hours": round(repair + delay, 1),
                "line_stops": loss >= 1.0,
                "capacity_loss_share": round(loss, 2),
                "drivers": [self._feature_text(d) for d in (r.driver_1, r.driver_2, r.driver_3) if d],
                "sensor_trends": {s: round(float(getattr(r, f"{s}_trend_24h")), 2) for s in self.p["sensors"]
                                  if pd.notna(getattr(r, f"{s}_trend_24h"))},
                "alarms_24h": self._recent_alarms(r.machine, t),
                "maintenance_tech_short": delay > 0,
                "similar_past_cases": self._similar_cases(r.station_id, comp["name"], t),
            }
            out.append(Risk(
                key=f"machine:{r.machine}", category="machine", line=r.line,
                title=f"{st['station']} {r.machine} - {comp['name']} failure risk",
                probability=prob, effects=[Effect(r.line, start, start + repair + delay, loss=loss)],
                repair_cost=float(comp.get("repair_cost", 0)), evidence=ev,
                window=(0.0, float(self.r["failure_model_horizon_h"]) + repair + delay),
                context=dict(machine=r.machine, machine_name=f"the {st['station'].lower()} (machine no. {r.machine})",
                             station=st["station"].lower(), component=comp["name"], station_id=r.station_id)))
        return out

    def _feature_text(self, f: str) -> str:
        for s, v in self.p["sensors"].items():
            if f.startswith(s + "_"):
                kind = f[len(s) + 1:]
                return FEATURE_LABELS.get(kind, f).format(s=v["label"].lower())
        for e, label in self.p.get("error_codes", {}).items():
            if f.startswith(e + "_"):
                return FEATURE_LABELS["count_24h"].format(e=label)
        if f.startswith("days_since_"):
            return FEATURE_LABELS["days_since"].format(c=self.p.component(f[11:])["name"])
        return FEATURE_LABELS.get(f, f.replace("_", " "))

    def _recent_alarms(self, machine, t) -> dict:
        e = self.errors[(self.errors.machine == machine) & (self.errors.ts <= t) &
                        (self.errors.ts > t - pd.Timedelta(hours=24))]
        names = self.p.get("error_codes", {})
        return {names.get(k, k): int(v) for k, v in e.code.value_counts().items()}

    def _similar_cases(self, station_id, comp_name, t, k=2) -> list[str]:
        st = self.stations.loc[station_id, "station"]
        n = self.notes[(self.notes.source == "maintenance") & (self.notes.ts < t) &
                       self.notes.note.str.startswith("BREAKDOWN") &
                       self.notes.note.str.contains(f"({st},", regex=False) &
                       self.notes.note.str.contains(comp_name, regex=False)]
        return [f"{r.ts:%d %b %Y}: {r.note}" for r in n.tail(k).itertuples()]

    def _maint_tech_short(self, t) -> set:
        roles = [r for r, v in self.p["workforce"]["roles"].items() if v.get("repair_delay_h")]
        s = self.shifts[self.shifts.role.isin(roles) & (self.shifts.notice_ts <= t) &
                        (self.shifts.shift_start <= t + pd.Timedelta(hours=self.r["failure_model_horizon_h"])) &
                        (self.shifts.shift_start + pd.Timedelta(hours=self.SH) > t) &
                        (self.shifts.present < self.shifts.planned)]
        return set(s.line)

    # ================================================================ material
    def _stock_at(self, material, t) -> tuple[float, pd.Timestamp]:
        snap = self.inv[(self.inv.material == material) & (self.inv.date <= t)]
        if snap.empty:
            return np.nan, t
        last = snap.iloc[-1]
        s0 = last.date
        stock = float(last.stock)
        rec = self.po[(self.po.material == material) & (self.po.actual_arrival > s0) & (self.po.actual_arrival <= t)]
        stock += float(rec.qty.sum())
        done = self.prod[(self.prod.shift_start >= s0) & (self.prod.shift_start + pd.Timedelta(hours=self.SH) <= t)]
        base = getattr(self, "base", {})
        for l, bom in self.line_mats.items():
            if material in bom:
                d = done[done.line == l]
                stock -= float(d.actual_units.sum()) * bom[material]
                # The shift in progress is consuming too: deduct its expected use so far.
                since = max(s0, d.shift_start.max() + pd.Timedelta(hours=self.SH)) if len(d) else s0
                run_h = min(max(0.0, (t - since).total_seconds() / 3600), self.SH)
                stock -= run_h * self._rate(l) * base.get(l, {"factor": 0.9})["factor"] * bom[material]
        return max(stock, 0.0), s0

    def _material_risks(self, t) -> list[Risk]:
        out = []
        for m, md in self.p["materials"].items():
            users = [l for l, bom in self.line_mats.items() if m in bom]
            if not users:
                continue
            stock, _ = self._stock_at(m, t)
            if np.isnan(stock):
                continue
            use_h = sum(self._rate(l) * self.base[l]["factor"] * self.line_mats[l][m] for l in users)
            open_po = self.po[(self.po.material == m) & (self.po.order_ts <= t) & (self.po.actual_arrival > t)].copy()
            known = open_po.revision_notice_ts.notna() & (open_po.revision_notice_ts <= t)
            open_po["eta"] = np.where(known, open_po.revised_eta, open_po.promised_eta)
            open_po["eta"] = pd.to_datetime(open_po["eta"]).clip(lower=t)
            open_po["eta_revised"] = known
            hist = self.po[(self.po.supplier == md["supplier"]) & (self.po.order_ts <= t - pd.Timedelta(days=1))]
            delay_rate = float((hist.revision_notice_ts.notna() & (hist.revision_notice_ts <= t)).mean()) if len(hist) else 0.1
            avg_delay = float(hist.loc[hist.delay_days > 0, "delay_days"].mean()) if (hist.delay_days > 0).any() else 3.0

            win = self._stockout(m, stock, use_h, open_po, t, slip_days=0)
            scenario, prob = "known", 0.95
            if win is None and len(open_po):
                win = self._stockout(m, stock, use_h, open_po, t, slip_days=avg_delay)
                scenario, prob = "supplier_slip", delay_rate
            if win is None or prob < 0.05:
                continue
            s_h, e_h = win
            effects = [Effect(None, s_h, e_h, loss=1.0)]
            if scenario == "known" and len(open_po) and not open_po.sort_values("eta").eta_revised.iloc[0]:
                # The next delivery may still slip (supplier history): price the longer outage at its own odds.
                slip = self._stockout(m, stock, use_h, open_po, t, slip_days=avg_delay)
                if slip and slip[1] > e_h:
                    effects.append(Effect(None, e_h, slip[1], loss=min(1.0, delay_rate / prob)))
            cover_days = stock / (use_h * 24) if use_h else np.inf
            po_list = [dict(po=r.po_id, qty=round(r.qty), eta=f"{r.eta:%a %d %b, %H:%M}",
                            promised=f"{r.promised_eta:%d %b}", revised=bool(r.eta_revised))
                       for r in open_po.sort_values("eta").itertuples()]
            for l in users:
                ev = dict(material=m, unit=md.get("unit", "units"), supplier=md["supplier"], stock_units=round(stock),
                          days_of_cover=round(cover_days, 2), hourly_use_all_lines=round(use_h),
                          lines_using_it=users, open_purchase_orders=po_list,
                          stockout_in_h=round(s_h, 1), stockout_hours=round(e_h - s_h, 1),
                          scenario=scenario, supplier_delay_rate=round(delay_rate, 2),
                          supplier_avg_delay_days=round(avg_delay, 1))
                out.append(Risk(key=f"material:{m}:{l}", category="material", line=l,
                                title=f"{m} stock-out on {l}", probability=prob,
                                effects=[Effect(l, e.start_h, e.end_h, loss=e.loss) for e in effects], evidence=ev,
                                context=dict(material=m, supplier=md["supplier"])))
        return out

    def _stockout(self, m, stock, use_h, open_po, t, slip_days) -> tuple[float, float] | None:
        """Hour-by-hour stock projection over the lookahead window.

        Includes known deliveries (revised ETA if the supplier has notified a delay),
        optionally slips the next delivery by `slip_days`, and applies the plant's
        own reorder policy for orders that will be placed in the future.
        Returns (hours until stock-out, hours until refilled) or None.
        """
        if use_h <= 0:
            return None
        md = self.p["materials"][m]
        snap = self.inv[(self.inv.material == m) & (self.inv.date <= t)]
        daily_req = float(snap.daily_requirement.iloc[-1]) if len(snap) else use_h * 24
        arrivals = []
        for i, r in enumerate(open_po.sort_values("eta").itertuples()):
            h = (r.eta - t).total_seconds() / 3600 + (24 * slip_days if i == 0 else 0)
            arrivals.append([h, float(r.qty)])
        level, start, check_hour = stock, None, self.p.start.hour
        for h in range(self.mat_H):
            for a in arrivals:
                if a[1] and a[0] <= h:
                    level += a[1]
                    a[1] = 0.0
            if start is not None and level > 0:
                return start, float(h)
            if (t + pd.Timedelta(hours=h)).hour == check_hour:
                pending = sum(a[1] for a in arrivals)
                if level + pending < md["reorder_days"] * daily_req:
                    arrivals.append([h + 24 * md["lead_time_days"], md["order_days"] * daily_req])
            level -= use_h
            if level <= 0:
                level = 0.0
                if start is None:
                    start = float(h)
        return (start, float(self.mat_H)) if start is not None else None

    # ================================================================ workforce
    def _workforce_risks(self, t) -> list[Risk]:
        roles = self.p["workforce"]["roles"]
        s = self.shifts[(self.shifts.notice_ts <= t) & (self.shifts.present < self.shifts.planned) &
                        (self.shifts.shift_start + pd.Timedelta(hours=self.SH) > t) &
                        (self.shifts.shift_start < t + pd.Timedelta(hours=self.H))]
        out = []
        for (line, role), g in s.groupby(["line", "role"]):
            factor = roles[role]["speed_if_short"]
            if factor >= 0.999:
                if roles[role].get("repair_delay_h"):
                    r = self._repair_cover_risk(t, line, role, g)
                    if r:
                        out.append(r)
                continue
            effects, shifts_txt = [], []
            for r in g.sort_values("shift_start").itertuples():
                missing = r.planned - r.present
                a = max(0.0, (r.shift_start - t).total_seconds() / 3600)
                b = (r.shift_start + pd.Timedelta(hours=self.SH) - t).total_seconds() / 3600
                effects.append(Effect(line, a, b, loss=1 - factor ** missing))
                shifts_txt.append(f"{r.shift_start:%a %d %b} {r.shift.lower()} shift ({missing} of {r.planned} missing)")
            first = g.sort_values("shift_start").iloc[0]
            ev = dict(role=role, certified=bool(roles[role].get("certified")), shifts=shifts_txt,
                      speed_when_short=f"{factor:.0%} of normal speed per missing person",
                      notice_given=f"{first.notice_ts:%a %d %b, %H:%M}")
            out.append(Risk(key=f"workforce:{line}:{role}", category="workforce", line=line,
                            title=f"{role.replace('_', ' ')} shortage on {line}", probability=0.95,
                            effects=effects, evidence=ev,
                            context=dict(role=role.replace("_", " "), shift=first["shift"].lower())))
        return out

    def _repair_cover_risk(self, t, line, role, g) -> Risk | None:
        """No maintenance technician: any line-stopping breakdown in those shifts takes longer to fix.
        Expected extra downtime = P(breakdown during gap) x repair delay. P comes from the line's own
        breakdown history (before t) plus any machine on the line the model is currently warning about."""
        delay = float(self.p["workforce"]["roles"][role]["repair_delay_h"])
        dt = self.t["downtime_events"]
        series = set(self.p.machines[(self.p.machines.line == line) & (self.p.machines["mode"] == "series")].machine)
        hist = dt[(dt.type == "unplanned") & dt.machine.isin(series) & (dt.start < t) & (dt.start >= t - pd.Timedelta(days=90))]
        per_hour = len(hist) / (90 * 24)
        effects, texts = [], []
        p_any = 0.0
        for r in g.sort_values("shift_start").itertuples():
            a = max(0.0, (r.shift_start - t).total_seconds() / 3600)
            b = (r.shift_start + pd.Timedelta(hours=self.SH) - t).total_seconds() / 3600
            p_shift = 1 - np.exp(-per_hour * (b - a))
            p_any = 1 - (1 - p_any) * (1 - p_shift)
            effects.append(Effect(line, a, a + delay, loss=1.0))
            texts.append(f"{r.shift_start:%a %d %b} {r.shift}")
        warned = [k for k in self._machine_keys_on(line)]
        if warned:
            p_any = max(p_any, 0.5)
        if not effects:
            return None
        return Risk(key=f"workforce:{line}:{role}", category="workforce", line=line,
                    title=f"{role.replace('_', ' ')} absent on {line}", probability=round(min(p_any, 0.95), 3),
                    effects=effects[:1], evidence=dict(role=role, certified=True, shifts=texts,
                    speed_when_short="no slowdown, but breakdowns take longer to fix",
                    extra_repair_hours=delay, breakdowns_last_90_days=len(hist), machines_at_risk_now=warned,
                    notice_given=f"{g.notice_ts.min():%d %b %H:%M}"),
                    context=dict(role=role.replace("_", " "), shift=g.sort_values("shift_start").iloc[0]["shift"].lower()))

    def _machine_keys_on(self, line) -> list[str]:
        return [k for k in getattr(self, "_machine_alert_keys", []) if k.split(":")[1].startswith(line + "-")]

    # ================================================================ quality
    def _quality_risks(self, t) -> list[Risk]:
        q = self.p.get("quality", {})
        W = int(self.r.get("quality_spc_window_shifts", 6))
        done = self.qual[self.qual.shift_start + pd.Timedelta(hours=self.SH) <= t]
        recent = done[done.shift_start > t - pd.Timedelta(hours=self.SH * W)]
        hist = done[(done.shift_start <= t - pd.Timedelta(hours=self.SH * W)) &
                    (done.shift_start > t - pd.Timedelta(days=60))]
        out = []
        for (line, metric), g in recent.groupby(["line", "metric"]):
            if len(g) < 3:
                continue
            g = g.sort_values("shift_start")
            mdef = q["metrics"][metric]
            hg = hist[(hist.line == line) & (hist.metric == metric)]
            base_oos = float(hg.out_of_spec.sum() / hg.samples.sum()) if len(hg) else 0.01
            x = np.arange(len(g))
            slope = float(np.polyfit(x, g["mean"], 1)[0])
            sd = float(np.sqrt((g["sd"] ** 2).mean()))
            proj_mean = float(g["mean"].iloc[-1] + slope)                # one shift ahead (no long extrapolation)
            proj_oos = float(norm.cdf(mdef["lsl"], proj_mean, sd) + norm.sf(mdef["usl"], proj_mean, sd))
            last_oos = float(g.out_of_spec.iloc[-1] / g.samples.iloc[-1])
            cur_oos = max(last_oos, float(g.out_of_spec.tail(2).sum() / g.samples.tail(2).sum()))
            expected = min(max(proj_oos, cur_oos), 2 * cur_oos + base_oos)   # cap: at most double the current rate
            k = float(q.get("spc_shift_sd", 1.0))
            z = (g["mean"].tail(2) - mdef["target"]).abs() / sd
            k1 = float(q.get("spc_single_shift_sd", 99))
            spc = bool((len(z) == 2 and (z >= k).all()) or z.iloc[-1] >= k1)  # 2 shifts in a row, or 1 big shift
            if spc:
                expected = max(expected, proj_oos)
            if expected < max(3 * base_oos, 0.03) and not spc:
                continue
            station = g.station.iloc[0]
            extra = max(0.0, expected - base_oos)
            ev = dict(metric=mdef["label"], unit=mdef["unit"], limits=[mdef["lsl"], mdef["usl"]], target=mdef["target"],
                      last_shift_out_of_spec=f"{last_oos:.1%}", normal_out_of_spec=f"{base_oos:.1%}",
                      projected_out_of_spec=f"{proj_oos:.1%}", trend_per_shift=round(slope, 3),
                      recent_means=[round(v, 2) for v in g["mean"].tail(4)], station=station)
            sid = self.p.stations[(self.p.stations.line == line) & (self.p.stations.station == station)].station_id.iloc[0]
            resp = float(q.get("response_hours", self.SH))              # time until the drift is corrected
            ev["priced_for_hours"] = resp
            ev["expected_out_of_spec"] = round(expected, 4)
            ev["mean_shift_sd"] = round(float(z.iloc[-1]), 2)
            ev["spc_signal"] = spc
            out.append(Risk(key=f"quality:{line}:{metric}", category="quality", line=line,
                            title=f"{mdef['label']} drifting on {line} {station}", probability=0.8,
                            effects=[Effect(line, 0.0, resp, scrap=extra)], evidence=ev,
                            context=dict(station=station.lower(), metric=mdef["label"].lower(), station_id=sid)))
        return out

    # ================================================================ projection + pricing
    def _capacity(self, line, effects: list[tuple[float, Effect]], L: int):
        """Hourly expected good output for `line` over L hours given (probability, effect) pairs."""
        b = self.base[line]
        f = np.full(L, b["factor"])
        good = np.full(L, 1 - b["scrap"])
        for prob, e in effects:
            if e.line != line:
                continue
            a, z = int(max(0, np.floor(e.start_h))), int(min(L, np.ceil(e.end_h)))
            if z <= a:
                continue
            if e.loss:
                f[a:z] *= 1 - prob * e.loss
            if e.scrap:
                good[a:z] *= 1 - prob * e.scrap
        return self._rate(line) * f * good

    def _project(self, line, open_o: pd.DataFrame, cap: np.ndarray, t) -> pd.DataFrame:
        if open_o.empty:
            return open_o.assign(proj_done=pd.NaT, proj_late_days=0.0, proj_penalty=0.0)
        o = open_o.copy()
        o["_k"] = list(zip(o.due_ts, o.tier.map({"A": 0, "B": 1, "C": 2})))
        o = o.sort_values("_k").drop(columns="_k")
        cum_need = o.remaining.cumsum().to_numpy()
        cum_cap = np.cumsum(cap)
        idx = np.searchsorted(cum_cap, cum_need)
        hours = np.where(idx < len(cap), idx + 1, len(cap) + (cum_need - cum_cap[-1]) / max(cap[-1], 1))
        o["proj_done"] = [t + pd.Timedelta(hours=float(h)) for h in hours]
        late = (o.proj_done - o.due_ts).dt.total_seconds() / 86400
        o["proj_late_days"] = late.clip(lower=0)
        o["proj_penalty"] = [self.p.penalty(c, v, d) for c, v, d in zip(o.customer, o.invoice_value, o.proj_late_days)]
        return o

    def _open_orders(self, line, t):
        if line not in self.backlogs or self.backlogs[line].t > t:
            self.backlogs[line] = LineBacklog(self.orders[self.orders.line == line],
                                              self.prod[self.prod.line == line], self.SH)
        bl = self.backlogs[line]
        bl.advance(t)
        return bl.open_orders()

    def _price(self, t, risks: list[Risk]) -> list[dict]:
        items = []
        by_line: dict[str, list[Risk]] = {}
        for r in risks:
            by_line.setdefault(r.line, []).append(r)

        for line in self.p.line_ids:
            lr = by_line.get(line, [])
            open_o = self._open_orders(line, t)
            L = int(min(24 * 45, max(self.H, ((open_o.due_ts.max() - t).total_seconds() / 3600 + 24 * 7)
                                     if len(open_o) else self.H)))
            all_eff = [(r.probability, e) for r in lr for e in r.effects]
            none = self._project(line, open_o, self._capacity(line, [], L), t)
            full = self._project(line, open_o, self._capacity(line, all_eff, L), t)
            pen_none, pen_full = float(none.proj_penalty.sum()), float(full.proj_penalty.sum())

            for r in lr:
                others = [(x.probability, e) for x in lr if x is not r for e in x.effects]
                wo = self._project(line, open_o, self._capacity(line, others, L), t)
                marginal = max(0.0, pen_full - float(wo.proj_penalty.sum()))
                r.impact = self._direct(line, r)
                r.impact["late_penalty"] = marginal
                r.impact["total"] = sum(r.impact.values())
                r.evidence["orders_made_late"] = self._orders_diff(wo, full)
                items.append(self._item(t, r))

            # Orders at risk on this line (combined effect of all risks) and pure demand overload.
            late_full = full[full.proj_late_days > 0]
            if len(late_full) and pen_full - pen_none > 0 and lr:
                top = late_full.sort_values("proj_penalty", ascending=False).iloc[0]
                r = Risk(key=f"orders:{line}", category="orders", line=line,
                         title=f"{len(late_full)} orders at risk on {line}", probability=1.0,
                         effects=[], evidence=dict(orders=self._order_rows(late_full),
                                                   caused_by=[x.key for x in lr]),
                         context=dict(order=top.order_id, customer=top.customer), related=[x.key for x in lr])
                r.impact = dict(lost_margin=0.0, scrap=0.0, repair=0.0, late_penalty=pen_full - pen_none)
                r.impact["total"] = r.impact["late_penalty"]
                items.append(self._item(t, r, start_h=float((late_full.due_ts.min() - t).total_seconds() / 3600)))
            late_none = none[none.proj_late_days > 0]
            if len(late_none):
                r = Risk(key=f"demand:{line}", category="demand", line=line,
                         title=f"Demand exceeds capacity on {line}", probability=1.0, effects=[],
                         evidence=dict(orders=self._order_rows(late_none),
                                       backlog_units=round(float(open_o.remaining.sum())),
                                       normal_output_per_day=round(self._rate(line) * self.base[line]["factor"] * 24)),
                         context=dict())
                r.impact = dict(lost_margin=0.0, scrap=0.0, repair=0.0, late_penalty=pen_none)
                r.impact["total"] = pen_none
                items.append(self._item(t, r, start_h=max(0.0, float((late_none.due_ts.min() - t).total_seconds() / 3600))))

            # Compound: two or more different risk categories overlapping on this line.
            cats = {}
            machine_stations = {r.context.get("station_id") for r in lr if r.category == "machine"}
            for r in lr:
                if r.impact.get("total", 0) < self.r["alert_threshold"] or \
                        not self._is_alert(r, r.impact["total"], r.start_h):
                    continue            # watch-list items are not part of a compound alert
                if r.category == "quality" and r.context.get("station_id") in machine_stations:
                    continue            # same worn station as a machine risk: one root cause, not two
                cats.setdefault(r.category, []).append(r)
            high = self.p["escalation"]["severity_bands"]["high"]
            capacity_causes = {c for c, rs in cats.items() if c in ("machine", "material", "workforce")
                               and max(x.impact["total"] for x in rs) >= high}
            if len(cats) >= 2 and (pen_full - pen_none >= self.r["alert_threshold"] or len(capacity_causes) >= 2):
                # customers materially hit, or two independent causes that each cut output
                parts = [x for v in cats.values() for x in v]
                if self._overlap(parts):
                    direct = {k: sum(self._direct(line, x)[k] for x in parts) for k in ("lost_margin", "scrap", "repair")}
                    direct["late_penalty"] = pen_full - pen_none
                    direct["total"] = sum(direct.values())
                    r = Risk(key=f"compound:{line}", category="compound", line=line,
                             title=f"Several problems at once on {line}: " + " + ".join(sorted(cats)),
                             probability=float(1 - np.prod([1 - x.probability for x in parts])), effects=[],
                             evidence=dict(parts=[x.title for x in parts], part_texts=[self._part_text(x) for x in parts],
                                           sum_of_separate_penalties=round(sum(x.impact["late_penalty"] for x in parts)),
                                           joint_penalty=round(pen_full - pen_none),
                                           orders=self._order_rows(full[full.proj_late_days > 0])),
                             context=dict(parts={x.category: x.context for x in
                                                 sorted(parts, key=lambda y: -y.impact.get("total", 0))[::-1]}),
                             related=[x.key for x in parts])
                    r.impact = direct
                    items.append(self._item(t, r, start_h=min(x.start_h for x in parts)))
        return items

    @staticmethod
    def _part_text(x: Risk) -> str:
        c = x.context
        if x.category == "machine":
            return f"the {c.get('station', 'machine')} may break down ({c.get('component', 'part')})"
        if x.category == "material":
            return f"{c.get('material', 'a material')} will run out"
        if x.category == "workforce":
            return f"there will not be enough {c.get('role', 'operator')}s"
        if x.category == "quality":
            return f"{c.get('metric', 'a quality check')} is drifting out of limits"
        return x.title

    @staticmethod
    def _overlap(parts: list[Risk]) -> bool:
        for i, a in enumerate(parts):
            for b in parts[i + 1:]:
                (a0, a1), (b0, b1) = a.span, b.span
                if a.category != b.category and a0 <= b1 and b0 <= a1:
                    return True
        return False

    def _direct(self, line, r: Risk) -> dict:
        prod = self.p.line(line)["product"]
        rate, b = self._rate(line), self.base[line]
        lost = sum(r.probability * e.loss * rate * b["factor"] * max(0.0, e.end_h - e.start_h) for e in r.effects)
        scrap_u = sum(r.probability * e.scrap * rate * b["factor"] * max(0.0, e.end_h - e.start_h) for e in r.effects)
        return dict(lost_margin=lost * prod["margin"],
                    scrap=scrap_u * (prod.get("scrap_cost", 0) + prod["margin"]),
                    repair=r.probability * r.repair_cost)

    def _order_rows(self, df: pd.DataFrame, k=5) -> list[dict]:
        df = df.sort_values("proj_penalty", ascending=False).head(k)
        return [dict(order=r.order_id, customer=r.customer, tier=r.tier, qty=int(r.qty),
                     remaining=int(r.remaining), due=f"{r.due_ts:%a %d %b, %H:%M}",
                     projected_finish=f"{r.proj_done:%a %d %b, %H:%M}", days_late=round(float(r.proj_late_days), 1),
                     penalty=round(float(r.proj_penalty))) for r in df.itertuples()]

    @staticmethod
    def _orders_diff(without: pd.DataFrame, with_: pd.DataFrame) -> list[dict]:
        a = without.set_index("order_id")
        b = with_.set_index("order_id")
        out = []
        for oid in b.index:
            add = float(b.at[oid, "proj_penalty"] - a.at[oid, "proj_penalty"])
            if add > 0:
                out.append(dict(order=oid, customer=b.at[oid, "customer"], due=f"{b.at[oid, 'due_ts']:%d %b}",
                                extra_days_late=round(float(b.at[oid, "proj_late_days"] - a.at[oid, "proj_late_days"]), 1),
                                extra_penalty=round(add)))
        return sorted(out, key=lambda x: -x["extra_penalty"])[:5]

    def _item(self, t, r: Risk, start_h: float | None = None) -> dict:
        sb = self.p["escalation"]["severity_bands"]
        tot = float(r.impact.get("total", 0.0))
        sev = "critical" if tot >= sb["critical"] else "high" if tot >= sb["high"] else "medium" if tot >= sb["medium"] else "low"
        s_h = r.start_h if start_h is None else start_h
        urgency = 0.5 ** (max(0.0, s_h) / self.r["urgency_halflife_h"])
        line = self.p.line(r.line)
        ctx = dict(line=r.line, line_name=line.get("name", r.line), **r.context)
        m = self.mitigations.get(r.key)
        if m and r.category in ("orders", "demand", "compound"):          # derived items: scale directly
            tot = tot * (1 - m["factor"])
            r.impact = {k: v * (1 - m["factor"]) for k, v in r.impact.items()}
        # Money at risk before anyone acted: re-price with the original probability.
        before_tot = tot
        if m and r.category not in ("orders", "demand", "compound"):
            p_now, r.probability = r.probability, getattr(self, "_before", {}).get(r.key, r.probability)
            before_tot = sum(self._direct(r.line, r).values()) + float(r.impact.get("late_penalty", 0))
            r.probability = p_now
        if m:
            before_tot = max(before_tot, tot, float(m.get("impact_at_mitigation") or 0))
        sev = "critical" if tot >= sb["critical"] else "high" if tot >= sb["high"] else "medium" if tot >= sb["medium"] else "low"
        alert = self._is_alert(r, tot, s_h) and not (m and m["factor"] >= 1)
        return dict(
            key=r.key, category=r.category, line=r.line, line_name=ctx["line_name"], title=r.title,
            mitigation=m, impact_before_actions=round(before_tot),
            assessed_at=t, probability=round(r.probability, 3),
            starts_in_h=round(max(0.0, s_h), 1), ends_in_h=round(r.end_h, 1) if r.effects else None,
            impact={k: round(v) for k, v in r.impact.items()}, expected_impact=round(tot),
            severity=sev, alert=alert, priority=round(tot * urgency),
            owner_role=self.p.owner_role(r.category), evidence=r.evidence, context=ctx, related=r.related)

    def _is_alert(self, r: Risk, total: float, start_h: float) -> bool:
        """Raise an alert only for material risks worth a person's attention now.
        What-if supplier-slip scenarios stay on the watch list until inside the horizon."""
        if r.category == "workforce" and r.evidence.get("machines_at_risk_now"):
            return True     # no technician while a machine on the line is predicted to fail
        if r.category == "quality":
            # Bad product reaching customers matters beyond scrap cost: alert on the signal itself.
            q_min = self.p.get("quality", {}).get("alert_min_out_of_spec", 0.05)
            if r.evidence.get("expected_out_of_spec", 0) >= q_min or r.evidence.get("spc_signal"):
                return True
        if total < self.r["alert_threshold"]:
            return False
        if r.category == "material" and r.evidence.get("scenario") == "supplier_slip":
            return start_h <= float(self.r.get("slip_alert_within_h", self.H))
        return True

    # ================================================================ text
    def format(self, items: list[dict], top=10) -> str:
        if not items:
            return "No risks detected."
        rows = [f"{'sev':8s} {'impact':>14s} {'in':>6s}  {'owner':16s} title"]
        for x in items[:top]:
            rows.append(f"{x['severity']:8s} {self.p.money(x['expected_impact']):>14s} {x['starts_in_h']:>5.0f}h  "
                        f"{x['owner_role']:16s} {x['title']}")
        return "\n".join(rows)
