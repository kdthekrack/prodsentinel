"""Generic synthetic plant generator, driven entirely by an industry pack.

Real machine-health history (from the configured source) drives downtime;
everything else is simulated with causal links:

  failure -> station down -> line capacity (series/parallel bottleneck)
          -> lost output -> late orders -> penalties
  station wear before failure -> linked quality metric drifts -> scrap
  supplier delay -> stock-out -> material-starved line
  absent certified role -> slower line; absent maintenance tech -> longer repair
  seasonality / rush orders -> demand pressure

Every injected disruption is written to ground_truth_events with the time it
became knowable (known_ts) and the time it hit output (impact_ts).
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from engine.pack import Pack
from engine.sources import load_machine_health

PRIO = {"A": 0, "B": 1, "C": 2}


class Sim:
    def __init__(self, pack: Pack):
        self.p = pack
        self.rng = np.random.default_rng(pack.get("seed", 42))
        self.H = int((pack.end - pack.start) / pd.Timedelta(hours=1))
        self.SH = pack.shift_hours
        self.NS = self.H // self.SH
        self.shift_names = pack.shift_names
        self.M = pack.machines.reset_index(drop=True)
        self.midx = {m: i for i, m in enumerate(self.M.machine)}
        self.gt: list[dict] = []

    # ---------------------------------------------------------------- helpers
    def hidx(self, ts) -> int:
        return int((pd.Timestamp(ts) - self.p.start) / pd.Timedelta(hours=1))

    def sts(self, k: int) -> pd.Timestamp:
        return self.p.start + pd.Timedelta(hours=k * self.SH)

    def rand_ts(self, lo=None, hi=None) -> pd.Timestamp:
        lo = lo or self.p.start + pd.Timedelta(days=3)
        hi = hi or self.p.end - pd.Timedelta(days=12)
        return lo + pd.Timedelta(hours=int(self.rng.integers(0, int((hi - lo) / pd.Timedelta(hours=1)))))

    def ri(self, lo_hi) -> int:
        return int(self.rng.integers(lo_hi[0], lo_hi[1] + 1))

    # ---------------------------------------------------------------- 1. machines
    def machines(self, mh):
        m = self.M.merge(mh["machine_info"], on="machine", how="left")
        st = self.p.stations.set_index("station_id")
        m["criticality"] = [
            "High" if st.loc[s, "mode"] == "series" else
            ("Medium" if st.loc[s, "required"] >= st.loc[s, "count"] else "Low")
            for s in m.station_id]
        return m

    # ---------------------------------------------------------------- 2. workforce
    def workforce(self, scen):
        wf = self.p["workforce"]
        roles = wf["roles"]
        certified = [r for r, v in roles.items() if v.get("certified")]
        gaps = []
        for _ in range(scen.get("workforce_gaps", {}).get("count", 0)):
            c = scen["workforce_gaps"]
            s = self.rand_ts().normalize() + pd.Timedelta(hours=self.p.start.hour)
            role = (self.rng.choice(certified) if self.rng.random() < c.get("certified_role_prob", 0.5)
                    else max(roles, key=lambda r: roles[r]["count"]))
            gaps.append(dict(line=self.rng.choice(self.p.line_ids), shift=self.rng.choice(self.shift_names),
                             role=role, start=s, end=s + pd.Timedelta(days=self.ri(c["days"])),
                             extra=min(self.ri(c["extra_absent"]), roles[role]["count"])))
        rows = []
        for k in range(self.NS):
            ts, sname = self.sts(k), self.shift_names[k % len(self.shift_names)]
            mult = wf.get("night_absence_multiplier", 1) if sname == self.shift_names[-1] else 1
            for line in self.p.line_ids:
                for role, v in roles.items():
                    absent = int(self.rng.binomial(v["count"], min(1, v["absence_rate"] * mult)))
                    if v.get("certified") and absent and self.rng.random() < 0.6:
                        absent = 0          # certified backup covered a random absence
                    notice = ts
                    for g in gaps:
                        if g["line"] == line and g["shift"] == sname and g["role"] == role and g["start"] <= ts < g["end"]:
                            absent = min(v["count"], absent + g["extra"])
                            notice = g["start"] - pd.Timedelta(hours=wf.get("notice_hours", 0))
                    rows.append(dict(shift_start=ts, shift=sname, line=line, role=role, planned=v["count"],
                                     present=v["count"] - absent, notice_ts=notice))
        shifts = pd.DataFrame(rows)
        for g in gaps:
            self.gt.append(dict(type="workforce_gap", key=f"workforce:{g['line']}:{g['role']}", line=g["line"], start=g["start"], end=g["end"],
                                known_ts=g["start"] - pd.Timedelta(hours=wf.get("notice_hours", 0)),
                                impact_ts=g["start"] + pd.Timedelta(hours=(self.p["calendar"]["shifts"][g["shift"]] - self.p.start.hour) % 24),
                                detail=f"{g['shift']} shift short {g['extra']} x {g['role']}"))
        return shifts

    def staff_factors(self, shifts):
        roles = self.p["workforce"]["roles"]
        s = shifts.copy()
        s["missing"] = s.planned - s.present
        s["factor"] = [roles[r]["speed_if_short"] ** m for r, m in zip(s.role, s.missing)]
        f = s.groupby(["shift_start", "line"]).factor.prod()
        cert = [r for r, v in roles.items() if v.get("certified")]
        c = s[s.role.isin(cert)].groupby(["shift_start", "line"]).apply(
            lambda g: g.present.sum() / g.planned.sum(), include_groups=False)
        mt = [r for r, v in roles.items() if v.get("repair_delay_h")]
        mt_short = s[s.role.isin(mt) & (s.missing > 0)].set_index(["shift_start", "line"]).index
        return f, c, set(mt_short)

    # ---------------------------------------------------------------- 3. downtime
    def downtime(self, mh, mt_short):
        down = np.zeros((self.H, len(self.M)), dtype=bool)
        ev = []
        delay_h = max((v.get("repair_delay_h", 0) for v in self.p["workforce"]["roles"].values()), default=0)
        for r in mh["failures"].itertuples():
            comp = self.p.component(r.component)
            dur = self.ri(comp["repair_hours"])
            k = self.hidx(r.ts) // self.SH
            line = self.M.line[self.midx[r.machine]]
            if (self.sts(k), line) in mt_short:
                dur += delay_h
            i = self.hidx(r.ts)
            down[i:i + dur, self.midx[r.machine]] = True
            ev.append(dict(machine=r.machine, line=line, start=r.ts, end=r.ts + pd.Timedelta(hours=dur),
                           type="unplanned", component=r.component, component_name=comp["name"], duration_h=dur))
        pm = self.p.get("planned_maintenance", {})
        fk = set(zip(mh["failures"].ts, mh["failures"].machine, mh["failures"].component))
        mt = mh["maintenance"]
        mt = mt[(mt.ts >= self.p.start) & (mt.ts < self.p.end)]
        for r in mt.itertuples():
            if (r.ts, r.machine, r.component) in fk:
                continue
            d = pm.get("duration_h", 2)
            if not pm.get("during_changeover", True):
                i = self.hidx(r.ts)
                down[i:i + d, self.midx[r.machine]] = True
            ev.append(dict(machine=r.machine, line=self.M.line[self.midx[r.machine]], start=r.ts,
                           end=r.ts + pd.Timedelta(hours=d), type="planned", component=r.component,
                           component_name=self.p.component(r.component)["name"], duration_h=d))
        for r in mh["failures"].itertuples():
            self.gt.append(dict(type="machine_failure", key=f"machine:{r.machine}", line=self.M.line[self.midx[r.machine]], start=r.ts, end=r.ts,
                                known_ts=pd.NaT, impact_ts=r.ts, machine=r.machine,
                                detail=f"{r.machine} {self.p.component(r.component)['name']} failure"))
        return down, pd.DataFrame(ev).sort_values("start").reset_index(drop=True)

    def line_capacity(self, down):
        """Hourly capacity share per line = bottleneck over stations."""
        up = ~down
        cap = {}
        for line in self.p.line_ids:
            st = self.p.stations[self.p.stations.line == line]
            c = np.ones(self.H)
            for s in st.itertuples():
                idx = [self.midx[m] for m in self.M.machine[self.M.station_id == s.station_id]]
                if s.mode == "series":
                    sc = up[:, idx].all(axis=1).astype(float)
                else:
                    sc = np.minimum(1.0, up[:, idx].sum(axis=1) / s.required)
                c = np.minimum(c, sc)
            cap[line] = c
        return cap

    # ---------------------------------------------------------------- 4. scenarios
    def supplier_delays(self, scen, mh):
        c = scen.get("supplier_delays", {})
        mats = list(self.p["materials"])
        out = []
        for _ in range(c.get("count", 0)):
            s = self.rand_ts()
            out.append(dict(material=self.rng.choice(mats), start=s,
                            end=s + pd.Timedelta(days=self.ri(c["window_days"])),
                            delay_days=self.ri(c["delay_days"]), lot_rejected=False))
        # Compound scenarios: line-stopping failure + material shortage + rush order.
        cc = scen.get("compound", {})
        compound = []
        if cc.get("count"):
            lo = self.p.val_end if cc.get("period") == "test" else self.p.start
            f = mh["failures"].merge(self.M[["machine", "line", "mode"]], on="machine")
            f = f[(f.ts > lo + pd.Timedelta(days=7)) & (f.ts < self.p.end - pd.Timedelta(days=7)) & (f["mode"] == "series")]
            used, used_mats = set(), set()
            for r in f.sample(frac=1, random_state=7).itertuples():
                if r.line in used:
                    continue
                used.add(r.line)
                bom = self.p.line(r.line)["bom"]
                share = {m: sum(m in l["bom"] for l in self.p.lines) for m in bom}
                cand = [m for m in bom if m not in used_mats] or list(bom)
                mat = min(cand, key=lambda m: (share[m], -self.p["materials"][m]["lead_time_days"]))
                used_mats.add(mat)
                s = r.ts - pd.Timedelta(days=5)
                out.append(dict(material=mat, start=s, end=s + pd.Timedelta(days=8),
                                delay_days=cc.get("delay_days", 4), lot_rejected=True))
                compound.append(dict(line=r.line, machine=r.machine, ts=r.ts, material=mat, component=r.component))
                if len(compound) >= cc["count"]:
                    break
        return out, compound

    def quality_drifts(self, scen):
        c = scen.get("quality_drifts", {})
        metrics = self.p.get("quality", {}).get("metrics", {})
        out = []
        for _ in range(c.get("count", 0) if metrics else 0):
            line = self.rng.choice(self.p.line_ids)
            st = self.p.stations[(self.p.stations.line == line) & self.p.stations.quality_metric.notna()]
            if st.empty:
                continue
            s_row = st.iloc[int(self.rng.integers(len(st)))]
            s = self.rand_ts()
            out.append(dict(line=line, station=s_row.station, metric=s_row.quality_metric, start=s,
                            end=s + pd.Timedelta(days=self.ri(c["days"]))))
        return out

    # ---------------------------------------------------------------- 5. production loop
    def simulate(self, cap, staff_f, skill, mh, delays, drifts):
        p, rng = self.p, self.rng
        mats = p["materials"]
        q = p.get("quality", {})
        metrics = q.get("metrics", {})
        n_samp = q.get("samples_per_shift", 100)
        win = q.get("pre_failure_window_h", 48)
        stations = p.stations

        daily_req = {m: 0.0 for m in mats}
        for l in p.lines:
            for m, qty in l["bom"].items():
                daily_req[m] += p.planned_rate(l["id"]) * 24 * 0.9 * qty
        stock = {m: mats[m]["initial_days"] * daily_req[m] for m in mats}

        # Hours to next failure per (station) for wear -> quality drift.
        fail_h = {}
        for r in mh["failures"].itertuples():
            fail_h.setdefault(self.M.station_id[self.midx[r.machine]], []).append(self.hidx(r.ts))
        for v in fail_h.values():
            v.sort()

        pos, prod, qual, inv = [], [], [], []
        po_n = 0
        day_hours = 24 // self.SH
        for k in range(self.NS):
            ts = self.sts(k)
            h0, h1 = k * self.SH, (k + 1) * self.SH
            sname = self.shift_names[k % len(self.shift_names)]

            for d in delays:
                if ts <= d["start"] < ts + pd.Timedelta(hours=self.SH):
                    if d["lot_rejected"]:
                        cover = mats[d["material"]]["lead_time_days"] + 0.5   # replacement lot arrives just too late
                        stock[d["material"]] = min(stock[d["material"]], cover * daily_req[d["material"]])
                    for po in pos:
                        if po["material"] == d["material"] and not po["_recv"] and not po["delay_days"]:
                            po["delay_days"] = d["delay_days"]
                            po["actual_arrival"] += pd.Timedelta(days=d["delay_days"])
                            po["revised_eta"] = po["actual_arrival"]
                            po["revision_notice_ts"] = d["start"]
            for po in pos:
                if not po["_recv"] and po["actual_arrival"] <= ts:
                    stock[po["material"]] += po["qty"]
                    po["_recv"] = True

            if k % day_hours == 0:   # daily reorder + snapshot, taken before this shift runs
                for m, md in mats.items():
                    on_order = sum(x["qty"] for x in pos if x["material"] == m and not x["_recv"])
                    if stock[m] + on_order < md["reorder_days"] * daily_req[m]:
                        lead = md["lead_time_days"] + int(rng.integers(0, 2))
                        delay = sum(d["delay_days"] for d in delays if d["material"] == m and d["start"] <= ts < d["end"])
                        promised = ts + pd.Timedelta(days=lead)
                        pos.append(dict(po_id=f"PO{po_n:05d}", material=m, supplier=md["supplier"],
                                        qty=round(md["order_days"] * daily_req[m], 1), order_ts=ts,
                                        promised_eta=promised,
                                        revised_eta=promised + pd.Timedelta(days=delay) if delay else pd.NaT,
                                        revision_notice_ts=ts + pd.Timedelta(days=1) if delay else pd.NaT,
                                        actual_arrival=promised + pd.Timedelta(days=delay), delay_days=delay, _recv=False))
                        po_n += 1
                    inv.append(dict(date=ts, material=m, supplier=md["supplier"], stock=round(stock[m], 1),
                                    daily_requirement=round(daily_req[m], 1),
                                    days_of_cover=round(stock[m] / daily_req[m], 2)))
            # Requested output per line, then allocate scarce materials.
            possible = {}
            for l in p.lines:
                lid = l["id"]
                possible[lid] = (p.planned_rate(lid) * self.SH * cap[lid][h0:h1].mean()
                                 * staff_f.get((ts, lid), 1.0) * rng.uniform(0.96, 1.02))
            need = {m: sum(possible[l["id"]] * l["bom"].get(m, 0) for l in p.lines) for m in mats}
            frac_m = {m: 1.0 if need[m] <= stock[m] else max(0.0, stock[m] / need[m]) for m in mats}
            for l in p.lines:
                lid = l["id"]
                frac = min(frac_m[m] for m in l["bom"])
                actual = possible[lid] * frac
                for m, qty in l["bom"].items():
                    stock[m] = max(0.0, stock[m] - actual * qty)

                # Quality: sample each metric linked to this line's stations.
                sk = float(skill.get((ts, lid), 1.0))
                good_p = 1.0
                for s in stations[(stations.line == lid) & stations.quality_metric.notna()].itertuples():
                    mdef = metrics[s.quality_metric]
                    wear = 0.0
                    nxt = next((fh for fh in fail_h.get(s.station_id, []) if fh >= h0), None)
                    if nxt is not None and nxt - h0 <= win:
                        wear = 1 - (nxt - h0) / win
                    tool = 0.0
                    for d in drifts:
                        if d["line"] == lid and d["metric"] == s.quality_metric and d["start"] <= ts < d["end"]:
                            tool = 0.4 + 0.8 * (ts - d["start"]) / (d["end"] - d["start"])
                    mu = mdef["target"] + mdef["wear_shift"] * (wear + tool)
                    sd = mdef["sd"] * (1 + q.get("skill_sd_inflation", 0) * (1 - sk))
                    x = rng.normal(mu, sd, n_samp)
                    oos = int(((x < mdef["lsl"]) | (x > mdef["usl"])).sum())
                    good_p *= 1 - oos / n_samp
                    qual.append(dict(shift_start=ts, shift=sname, line=lid, station=s.station, metric=s.quality_metric,
                                     mean=round(float(x.mean()), 3), sd=round(float(x.std()), 3),
                                     lsl=mdef["lsl"], usl=mdef["usl"], samples=n_samp, out_of_spec=oos))
                scrap = actual * (1 - good_p)
                prod.append(dict(shift_start=ts, shift=sname, line=lid, sku=l["product"]["sku"],
                                 planned_units=round(p.planned_rate(lid) * self.SH),
                                 actual_units=round(actual), scrap_units=round(scrap), good_units=round(actual - scrap),
                                 line_capacity=round(float(cap[lid][h0:h1].mean()), 4),
                                 staff_factor=round(float(staff_f.get((ts, lid), 1.0)), 4),
                                 material_fraction=round(frac, 4), material_starved=frac < 0.99))

        po = pd.DataFrame(pos).drop(columns="_recv")
        return pd.DataFrame(prod), pd.DataFrame(qual), pd.DataFrame(inv), po, daily_req

    # ---------------------------------------------------------------- 6. orders
    def orders(self, production, compound):
        p, rng = self.p, self.rng
        d = p["demand"]
        cust = p["customers"]
        shares = np.array([c["share"] for c in cust])
        mean_rate = np.mean([p.planned_rate(l) for l in p.line_ids])
        rows, n = [], 0

        def add(line, t, qty, lead, rush):
            nonlocal n
            c = cust[int(rng.choice(len(cust), p=shares))]
            if rush:
                c = next((x for x in cust if x["tier"] == "A"), c)
            prod = p.line(line)["product"]
            rows.append(dict(order_id=f"SO{n:05d}", line=line, sku=prod["sku"], customer=c["name"], tier=c["tier"],
                             qty=int(qty), placed_ts=t.floor("h"), due_ts=(t + pd.Timedelta(days=lead)).floor("h"),
                             invoice_value=round(qty * prod["price"], 0), rush=rush))
            n += 1

        for line in p.line_ids:
            good_per_day = production.loc[production.line == line, "good_units"].sum() / ((p.end - p.start).days)
            scale = p.planned_rate(line) / mean_rate
            lo, hi = (x * scale for x in d["order_size"])
            base_rate = d["load_factor"] * good_per_day / ((lo + hi) / 2) / 24     # orders per hour
            t = p.start
            while True:
                season = d["seasonality"][t.month - 1]
                t += pd.Timedelta(hours=float(rng.exponential(1 / (base_rate * season))))
                if t > p.end - pd.Timedelta(days=4):
                    break
                add(line, t, rng.uniform(lo, hi), self.ri(d["lead_time_days"]), False)
        ro = p.get("scenarios", {}).get("rush_orders", {})
        for _ in range(ro.get("count", 0)):
            line = rng.choice(p.line_ids)
            t = self.rand_ts()
            add(line, t, rng.uniform(*ro["size"]) * p.planned_rate(line) / mean_rate, self.ri(ro["lead_time_days"]), True)
        for c in compound:
            add(c["line"], c["ts"] - pd.Timedelta(days=3), 2.4 * p.planned_rate(c["line"]) * 24, 4, True)

        o = pd.DataFrame(rows).sort_values("placed_ts").reset_index(drop=True)
        o["completed_ts"] = self.fulfil(o, production)
        late = (o.completed_ts - o.due_ts).dt.total_seconds() / 86400
        o["days_late"] = late.clip(lower=0).round(2)
        o.loc[o.completed_ts.isna(), "days_late"] = ((p.end - o.due_ts).dt.total_seconds() / 86400).clip(lower=0).round(2)
        o["penalty"] = [self.p.penalty(c, inv, dl) for c, inv, dl in zip(o.customer, o.invoice_value, o.days_late)]
        for r in o[o.rush].itertuples():
            self.gt.append(dict(type="rush_order", key=f"orders:{r.line}", order_id=r.order_id, line=r.line, start=r.placed_ts, end=r.due_ts, known_ts=r.placed_ts,
                                impact_ts=r.due_ts if r.days_late > 0 else pd.NaT,
                                detail=f"Rush order {r.order_id} {r.customer} {r.qty:,} units due {r.due_ts:%d %b}"))
        return o

    def fulfil(self, o, production):
        """Earliest-due-date dispatch, customer tier as tie-break."""
        done = {}
        for line in self.p.line_ids:
            lo = o[o.line == line].sort_values("placed_ts")
            ids, placed = lo.order_id.to_numpy(), lo.placed_ts.to_numpy()
            key = list(zip(lo.due_ts, lo.tier.map(PRIO)))
            rem = lo.qty.astype(float).to_numpy().copy()
            ptr, open_ = 0, []
            for r in production[production.line == line].sort_values("shift_start").itertuples():
                while ptr < len(ids) and placed[ptr] <= np.datetime64(r.shift_start):
                    open_.append(ptr)
                    ptr += 1
                open_.sort(key=lambda i: key[i])
                cap = float(r.good_units)
                while open_ and cap > 0:
                    i = open_[0]
                    take = min(cap, rem[i])
                    rem[i] -= take
                    cap -= take
                    if rem[i] <= 0:
                        done[ids[i]] = r.shift_start + pd.Timedelta(hours=self.SH)
                        open_.pop(0)
        return o.order_id.map(done)

    # ---------------------------------------------------------------- 7. notes (text for RAG)
    def notes(self, dt_events, mh, quality, po, shifts):
        p, rng = self.p, self.rng
        techs = ["R. Iyer", "M. Kowalski", "S. Das", "L. Okafor", "A. Mehta", "P. Silva", "K. Nair"]
        rows = []
        for r in dt_events.itertuples():
            comp = p.component(r.component)
            st = self.M.station[self.midx[r.machine]]
            if r.type == "unplanned":
                sy = rng.choice(comp.get("symptoms", ["fault"]), size=2, replace=False) if len(comp.get("symptoms", [])) > 1 else ["fault", "fault"]
                txt = (f"BREAKDOWN {r.machine} ({st}, {r.line}): {sy[0]}; {sy[1]}. Root cause: {comp['name']} failure. "
                       f"Action: {rng.choice(comp.get('actions', ['repaired']))}. Line impact {r.duration_h}h.")
            else:
                txt = (f"PM {r.machine} ({st}): planned {comp['name']} replacement. "
                       f"{rng.choice(['No abnormal wear.', 'Minor wear observed.', 'Wear within limits.'])}")
            rows.append(dict(ts=r.start, line=r.line, machine=r.machine, source="maintenance", author=rng.choice(techs), note=txt))
        codes = p.get("error_codes", {})
        e = mh["errors"].sample(frac=0.35, random_state=1)
        for r in e.itertuples():
            rows.append(dict(ts=r.ts, line=self.M.line[self.midx[r.machine]], machine=r.machine, source="operator",
                             author="operator", note=f"{r.machine}: {codes.get(r.code, r.code)} ({r.code}) - "
                             f"{rng.choice(['reset and continued', 'called maintenance', 'monitoring', 'cleared after restart'])}."))
        qbad = quality[quality.out_of_spec / quality.samples > 0.08]
        for r in qbad.itertuples():
            rows.append(dict(ts=r.shift_start, line=r.line, machine="", source="quality", author="QC",
                             note=f"{r.line} {r.station}: {r.out_of_spec}/{r.samples} out of spec on {r.metric} "
                                  f"(mean {r.mean}, limits {r.lsl}..{r.usl}). Held batch for re-inspection."))
        for r in po[po.delay_days > 0].itertuples():
            rows.append(dict(ts=r.revision_notice_ts, line="", machine="", source="purchasing", author="Purchasing",
                             note=f"{r.supplier} revised ETA for {r.po_id} ({r.material}) from {r.promised_eta:%d %b} "
                                  f"to {r.revised_eta:%d %b} (+{r.delay_days}d)."))
        g = shifts[(shifts.planned - shifts.present) > 0]
        g = g[g.notice_ts < g.shift_start]
        for r in g.drop_duplicates(["line", "role", "notice_ts"]).itertuples():
            rows.append(dict(ts=r.notice_ts, line=r.line, machine="", source="supervisor", author="Supervisor",
                             note=f"{r.line}: {r.planned - r.present} x {r.role} on leave from {r.shift_start:%d %b} {r.shift} shift."))
        return pd.DataFrame(rows).sort_values("ts").reset_index(drop=True)

    # ---------------------------------------------------------------- run
    def run(self):
        p = self.p
        scen = p.get("scenarios", {})
        mh = load_machine_health(p)
        machines = self.machines(mh)
        shifts = self.workforce(scen)
        staff_f, skill, mt_short = self.staff_factors(shifts)
        down, dt_events = self.downtime(mh, mt_short)
        cap = self.line_capacity(down)
        delays, compound = self.supplier_delays(scen, mh)
        drifts = self.quality_drifts(scen)
        production, quality, inventory, po, daily_req = self.simulate(cap, staff_f, skill, mh, delays, drifts)
        orders = self.orders(production, compound)
        notes = self.notes(dt_events, mh, quality, po, shifts)

        # Ground truth for supplier delays / quality drifts / compound, with impact times.
        line_mats = {l["id"]: set(l["bom"]) for l in p.lines}
        for d in delays:
            lines = [l for l, ms in line_mats.items() if d["material"] in ms]
            st = production[production.line.isin(lines) & production.material_starved &
                            (production.shift_start >= d["start"]) &
                            (production.shift_start <= d["end"] + pd.Timedelta(days=d["delay_days"] + 3))]
            notice = po[(po.material == d["material"]) & (po.revision_notice_ts >= d["start"]) &
                        (po.revision_notice_ts <= d["end"] + pd.Timedelta(days=1))].revision_notice_ts.min()
            self.gt.append(dict(type="supplier_delay", key=f"material:{d['material']}", line=",".join(lines), start=d["start"], end=d["end"],
                                known_ts=notice, impact_ts=st.shift_start.min() if len(st) else pd.NaT,
                                detail=("Lot rejected at incoming inspection; " if d["lot_rejected"] else "")
                                + f"{p['materials'][d['material']]['supplier']} delays {d['material']} by {d['delay_days']}d"))
        for d in drifts:
            q = quality[(quality.line == d["line"]) & (quality.metric == d["metric"]) &
                        (quality.shift_start >= d["start"]) & (quality.shift_start < d["end"])]
            hit = q[q.out_of_spec / q.samples > 0.05]
            self.gt.append(dict(type="quality_drift", key=f"quality:{d['line']}:{d['metric']}", line=d["line"], start=d["start"], end=d["end"], known_ts=pd.NaT,
                                impact_ts=hit.shift_start.min() if len(hit) else pd.NaT,
                                detail=f"{d['station']} wear drifting {d['metric']}"))
        for c in compound:
            self.gt.append(dict(type="compound", key=f"compound:{c['line']}", line=c["line"], start=c["ts"] - pd.Timedelta(days=5),
                                end=c["ts"] + pd.Timedelta(days=2), known_ts=pd.NaT, impact_ts=c["ts"], machine=c["machine"],
                                detail=f"{c['machine']} {p.component(c['component'])['name']} failure + {c['material']} "
                                       f"shortage + rush order on {c['line']}"))
        gt = pd.DataFrame(self.gt).sort_values("start").reset_index(drop=True)
        gt.insert(0, "event_id", [f"GT{i:04d}" for i in range(len(gt))])

        out = dict(machines=machines, downtime_events=dt_events, shifts=shifts, production_log=production,
                   quality_inspections=quality, material_inventory=inventory, purchase_orders=po,
                   customer_orders=orders, notes=notes, ground_truth_events=gt)
        d = p.out("synthetic")
        for k, v in out.items():
            v.to_csv(d / f"{k}.csv", index=False)
        return out


def summarize(pack: Pack, t: dict) -> str:
    pr, o, gt = t["production_log"], t["customer_orders"], t["ground_truth_events"]
    lines = [f"{k:22s} {len(v):>8,} rows" for k, v in t.items()]
    lines += ["",
              f"output vs plan          {pr.actual_units.sum() / pr.planned_units.sum():.1%}",
              f"scrap rate              {pr.scrap_units.sum() / max(pr.actual_units.sum(), 1):.2%}",
              f"material-starved shifts {int(pr.material_starved.sum())}",
              f"orders late             {(o.days_late > 0).mean():.1%}   penalties {pack.money(o.penalty.sum())}",
              "ground truth: " + ", ".join(f"{k}={v}" for k, v in gt.type.value_counts().items())]
    return "\n".join(lines)


def generate(pack: Pack) -> dict:
    return Sim(pack).run()
