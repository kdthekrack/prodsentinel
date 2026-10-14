"""System-level backtest of the early-warning engine on the held-out test period.

The engine is replayed every `every_h` hours. It never sees ground truth; the
evaluator compares its alerts with what actually happened afterwards:

  1. Detection   - for each real disruption, was it alerted before it hit, and how early?
  2. Precision   - for each alert episode, did the predicted problem really happen?
  3. Money       - were the rupee estimates (median over each alert episode) close to the real losses?
  4. Value       - share of real penalties / downtime that was warned about in time
  5. Load        - how many alerts per day each role receives (alert fatigue)
"""
from __future__ import annotations

import json

import numpy as np
import pandas as pd

from engine.pack import Pack
from engine.risk import RiskEngine

WINDOW_H = {"machine_failure": 51, "supplier_delay": 168, "workforce_gap": 72, "quality_drift": 96,
            "rush_order": 240, "compound": 120}
MIN_LEAD_H = 6        # a warning must come at least this early to count as "in time" for value metrics


def _replay(pack: Pack, lo, hi, every_h) -> pd.DataFrame:
    eng = RiskEngine(pack)
    rows = []
    for t in pd.date_range(lo, hi, freq=f"{every_h}h"):
        for x in eng.assess(t):
            rows.append(dict(ts=t, key=x["key"], category=x["category"], line=x["line"], alert=x["alert"],
                             severity=x["severity"], expected=x["expected_impact"], owner=x["owner_role"],
                             starts_in_h=x["starts_in_h"], lost_margin=x["impact"].get("lost_margin", 0),
                             repair=x["impact"].get("repair", 0), penalty=x["impact"].get("late_penalty", 0),
                             orders=",".join(sorted({o["order"] for k in ("orders", "orders_made_late")
                                                     for o in x["evidence"].get(k, [])}))))
    return pd.DataFrame(rows)


def _matches(gt_row, keys: pd.Series) -> pd.Series:
    k = gt_row.key
    if gt_row.type == "supplier_delay":
        lines = str(gt_row.line).split(",")
        return keys.isin([f"{k}:{l}" for l in lines])
    if gt_row.type == "rush_order":
        l = gt_row.line
        return keys.isin([f"orders:{l}", f"demand:{l}", f"compound:{l}"])
    return keys == k


def detection(pack, R, gt, lo, hi) -> dict:
    st = pack.stations.set_index("station_id")
    mach = pack.machines.set_index("machine")
    A = R[R.alert]
    out = {}
    for typ, g in gt[(gt.impact_ts > lo + pd.Timedelta(days=1)) & (gt.impact_ts <= hi)].groupby("type"):
        rows = []
        for e in g.itertuples():
            w0 = e.impact_ts - pd.Timedelta(hours=WINDOW_H[typ])
            if typ == "quality_drift":
                w0 = min(w0, e.start - pd.Timedelta(hours=24))
            cand = A[(A.ts >= w0) & (A.ts < e.impact_ts)]
            hit = cand[_matches(e, cand.key)]
            seen = R[(R.ts >= w0) & (R.ts < e.impact_ts)]
            seen = seen[_matches(e, seen.key)]
            row = dict(event=e.event_id, alerted=len(hit) > 0, flagged=len(seen) > 0,
                       lead_h=(e.impact_ts - hit.ts.min()).total_seconds() / 3600 if len(hit) else 0.0)
            if typ == "machine_failure":
                row["line_stopping"] = st.loc[mach.loc[e.machine, "station_id"], "mode"] == "series"
            rows.append(row)
        d = pd.DataFrame(rows)
        res = dict(events=len(d), alerted=int(d.alerted.sum()), recall=round(float(d.alerted.mean()), 3),
                   flagged_any_level=round(float(d.flagged.mean()), 3),
                   median_lead_h=round(float(d.loc[d.alerted, "lead_h"].median()), 1) if d.alerted.any() else None)
        if typ == "machine_failure":
            s = d[d.line_stopping]
            res["line_stopping_events"] = len(s)
            res["line_stopping_recall"] = round(float(s.alerted.mean()), 3)
            res["line_stopping_median_lead_h"] = round(float(s.loc[s.alerted, "lead_h"].median()), 1) if s.alerted.any() else None
            res["note"] = "failures on machines with a backup do not stop the line and are correctly not alerted"
        out[typ] = res
    absorbed = gt[(gt.start > lo) & (gt.start <= hi) & gt.impact_ts.isna()].type.value_counts().to_dict()
    out["absorbed_without_impact"] = absorbed
    return out


def _episodes(A: pd.DataFrame, every_h) -> pd.DataFrame:
    A = A.sort_values(["key", "ts"])
    new = (A.key != A.key.shift()) | (A.ts - A.ts.shift() > pd.Timedelta(hours=every_h))
    A = A.assign(ep=new.cumsum())
    return A.groupby("ep").agg(key=("key", "first"), category=("category", "first"), line=("line", "first"),
                               owner=("owner", "first"), t0=("ts", "min"), t1=("ts", "max"),
                               expected=("expected", "median"), lost_margin=("lost_margin", "median"),
                               orders=("orders", lambda s: ",".join(sorted({o for v in s for o in v.split(",") if o}))),
                               repair=("repair", "median"), severity=("severity", "first")).reset_index(drop=True)


def precision_and_money(pack, R, t, every_h) -> tuple[dict, dict, pd.DataFrame]:
    prod, qual, orders = t["production_log"], t["quality_inspections"], t["customer_orders"]
    dt = t["downtime_events"]
    fails = dt[dt.type == "unplanned"]
    SH = pd.Timedelta(hours=pack.shift_hours)
    H = pd.Timedelta(hours=pack["risk"]["horizon_h"])
    mat_H = pd.Timedelta(hours=pack["risk"].get("material_lookahead_h", 168))
    E = _episodes(R[R.alert], every_h)
    tp, realized = [], []
    for e in E.itertuples():
        ok, real = False, np.nan
        margin = pack.line(e.line)["product"]["margin"]
        if e.category == "machine":
            m = e.key.split(":", 1)[1]
            f = fails[(fails.machine == m) & (fails.start > e.t0) & (fails.start <= e.t1 + pd.Timedelta(hours=51))]
            ok = len(f) > 0
            if ok:
                ev = f.iloc[0]
                sh = prod[(prod.line == e.line) & (prod.shift_start + SH > ev.start) & (prod.shift_start < ev.end)]
                real = float((sh.planned_units * (1 - sh.line_capacity)).sum() * margin) + \
                    float(pack.component(ev.component).get("repair_cost", 0))
        elif e.category == "material":
            sh = prod[(prod.line == e.line) & prod.material_starved & (prod.shift_start > e.t0) &
                      (prod.shift_start <= e.t1 + mat_H)]
            ok = len(sh) > 0
            real = float((sh.planned_units * sh.line_capacity * sh.staff_factor * (1 - sh.material_fraction)).sum() * margin)
        elif e.category == "workforce":
            _, line, role = e.key.split(":")
            speed = pack["workforce"]["roles"][role]["speed_if_short"]
            s = t["shifts"]
            s = s[(s.line == line) & (s.role == role) & (s.present < s.planned) &
                  (s.shift_start > e.t0 - SH) & (s.shift_start <= e.t1)]
            ok = len(s) > 0
            # Money is compared like-for-like: only the shifts that were already known when the alert was raised.
            s = s[(s.notice_ts <= e.t0) & (s.shift_start < e.t0 + H)]
            sh = prod.set_index(["shift_start", "line"])
            real = 0.0
            for x in s.itertuples():
                p_ = sh.loc[(x.shift_start, line)]
                left = min(1.0, max(0.0, (x.shift_start + SH - e.t0) / SH))     # part of the shift still ahead
                real += float(p_.planned_units * p_.line_capacity * (1 - speed ** (x.planned - x.present)) * margin) * left
        elif e.category == "quality":
            _, line, metric = e.key.split(":")
            q = qual[(qual.line == line) & (qual.metric == metric) & (qual.shift_start >= e.t0) & (qual.shift_start <= e.t1 + H)]
            ok = bool(((q.out_of_spec / q.samples) > 0.03).any())
        else:   # orders / demand / compound: did an order on this line really end up late?
            o = orders[(orders.line == e.line) & (orders.days_late > 0) & (orders.due_ts > e.t0) &
                       (orders.due_ts <= e.t1 + pd.Timedelta(days=10)) & (orders.placed_ts <= e.t1)]
            ok = len(o) > 0
            if e.category == "compound" and not ok:
                # "Several problems at once" is also correct if >= 2 of the predicted problems really happened.
                parts = R[(R.ts == e.t0) & (R.line == e.line) & R.alert & R.category.isin(["machine", "material", "workforce"])]
                happened = set()
                for k, c in zip(parts.key, parts.category):
                    if c == "machine":
                        m = k.split(":", 1)[1]
                        if len(fails[(fails.machine == m) & (fails.start > e.t0) & (fails.start <= e.t0 + pd.Timedelta(hours=51))]):
                            happened.add(c)
                    elif c == "material":
                        if len(prod[(prod.line == e.line) & prod.material_starved & (prod.shift_start > e.t0) &
                                    (prod.shift_start <= e.t0 + mat_H)]):
                            happened.add(c)
                    elif len(prod[(prod.line == e.line) & (prod.staff_factor < 0.999) & (prod.shift_start > e.t0 - SH) &
                                  (prod.shift_start <= e.t0 + H)]):
                        happened.add(c)
                ok = len(happened) >= 2
            if not ok:
                # Outcome unknown: every order it named is due after the data ends and was never finished.
                named = orders[orders.order_id.isin([x for x in e.orders.split(",") if x])]
                if len(named) and (named.completed_ts.isna() & (named.due_ts > pack.end)).all():
                    ok = None
        tp.append(ok)
        realized.append(real)
    E["true_positive"] = tp
    E["realized_loss"] = realized
    days = (R.ts.max() - R.ts.min()).total_seconds() / 86400
    E["unresolved"] = E.true_positive.isna()
    E["true_positive"] = E.true_positive.fillna(False).astype(bool)
    prec = {}
    for c, g in E.groupby("category"):
        k = g[~g.unresolved]
        prec[c] = dict(alert_episodes=len(g), precision=round(float(k.true_positive.mean()), 3) if len(k) else None)
        if g.unresolved.any():
            prec[c]["unresolved_after_data_end"] = int(g.unresolved.sum())
    money = {}
    for c in ("machine", "material", "workforce"):
        g = E[(E.category == c)]
        if g.empty:
            continue
        pred = g.lost_margin + g.repair
        tpg = g[g.true_positive & g.realized_loss.notna() & (g.realized_loss > 0)]
        money[c] = dict(
            episodes=len(g),
            predicted_expected_total=round(float(pred.sum())),
            realized_total=round(float(g.realized_loss.fillna(0).sum())),
            ratio_total=round(float(pred.sum() / max(g.realized_loss.fillna(0).sum(), 1)), 2),
            median_ratio_when_it_happened=round(float(((tpg.lost_margin + tpg.repair) / tpg.realized_loss).median()), 2)
            if len(tpg) else None)
    load = dict(alert_episodes_per_day=round(len(E) / days, 1),
                per_role_per_day={k: round(v / days, 1) for k, v in E.owner.value_counts().items()},
                by_severity=E.severity.value_counts().to_dict())
    return prec, dict(money=money, load=load), E


def value(pack, R, t, lo, hi) -> dict:
    orders = t["customer_orders"]
    A = R[R.alert & (R.orders != "")]            # warnings that explicitly named at-risk orders
    late = orders[(orders.days_late > 0) & (orders.due_ts > lo + pd.Timedelta(days=2)) & (orders.due_ts <= hi)]
    warned_pen = 0.0
    for o in late.itertuples():
        w = A[(A.line == o.line) & (A.ts <= o.due_ts - pd.Timedelta(hours=MIN_LEAD_H)) & (A.ts >= o.placed_ts)]
        if len(w) and w.orders.str.split(",").apply(lambda ids: o.order_id in ids).any():
            warned_pen += o.penalty
    total = float(late.penalty.sum())
    return dict(late_orders=len(late), penalties_total=round(total), penalties_total_text=pack.money(total),
                penalties_warned_in_time=round(warned_pen),
                share_of_penalty_warned=round(warned_pen / total, 3) if total else None,
                min_warning_h=MIN_LEAD_H)


def evaluate(pack: Pack, every_h: int = 3, period: str = "test", save: bool = True) -> dict:
    """period='val' replays the validation month (for tuning); 'test' is the untouched hold-out."""
    from engine.sources import load_plant
    t = load_plant(pack)
    if period == "train":        # rule tuning only (ML scores here are in-sample)
        lo = pack.start + pd.Timedelta(days=60)
        hi = pack.train_end.floor("D")
    elif period == "val":
        lo = pack.train_end.ceil("D") + pd.Timedelta(hours=pack.start.hour)
        hi = pack.val_end.floor("D")
    else:
        lo = pack.val_end.ceil("D") + pd.Timedelta(hours=pack.start.hour)
        hi = pack.end - pd.Timedelta(days=1)
    R = _replay(pack, lo, hi, every_h)
    gt = t["ground_truth_events"]
    report = dict(period=f"{lo:%d %b %Y} - {hi:%d %b %Y}", cycles=int(R.ts.nunique()), every_h=every_h)
    report["detection"] = detection(pack, R, gt, lo, hi)
    prec, rest, E = precision_and_money(pack, R, t, every_h)
    report["precision"] = prec
    report.update(rest)
    report["value"] = value(pack, R, t, lo, hi)
    if not save:
        return report
    out = pack.out("reports")
    R.to_pickle(out / "replay_items.pkl")
    E.to_csv(out / "alert_episodes.csv", index=False)
    (out / "evaluation.json").write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")
    return report
