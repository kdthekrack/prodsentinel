"""Turn risk items into plain-language messages anyone on the shop floor can follow.

Rules:
  - Only facts computed by the risk engine are used (no invented numbers).
  - No codes or jargon: component names, line names, "bottles", "days".
  - Every message answers: what is happening, why we think so, what it costs,
    what to do, who does it, and by when.
"""
from __future__ import annotations

import re

import pandas as pd

from engine.pack import Pack


class _Safe(dict):
    def __missing__(self, k):
        return {"component": "part", "machine_name": "the machine", "station": "machine", "material": "material",
                "supplier": "the supplier", "role": "operator", "shift": "next", "metric": "the quality check",
                "order": "the most urgent order", "customer": "the customer"}.get(k, k.replace("_", " "))


def likelihood(p: float) -> str:
    if p >= 0.9:
        return "almost certain"
    if p >= 0.7:
        return "very likely"
    if p >= 0.5:
        return "likely"
    if p >= 0.3:
        return "possible"
    return "unlikely, but worth watching"


URGENCY = {"critical": "Very urgent - act now", "high": "Urgent - act today",
           "medium": "Important - plan a fix", "low": "For information"}


def pct(p: float) -> str:
    return f"{min(p, 0.99):.0%}"


def per100(share: float) -> str:
    n = share * 100
    return f"{n:.0f} in every 100" if n >= 1 else "less than 1 in every 100"


def day(ts) -> str:
    return f"{pd.Timestamp(ts):%a %d %b, %H:%M}"


def when(now: pd.Timestamp, hours: float) -> str:
    if hours <= 1:
        return "right now"
    at = now + pd.Timedelta(hours=hours)
    if hours < 12:
        return f"in about {round(hours)} hours (at {at:%H:%M})"
    if hours < 24:
        return f"in about {round(hours)} hours ({at:%a %d %b, %H:%M})"
    d = round(hours / 24 * 2) / 2
    return f"in about {d:g} day{'s' if d != 1 else ''} ({at:%a %d %b, %H:%M})"


def line_label(pack: Pack, line: str) -> str:
    l = pack.line(line)
    return f"{l.get('name', line).split(' - ')[0]} ({l['product']['name']})"


def _short_line(pack: Pack, line: str) -> str:
    return pack.line(line).get("name", line).split(" - ")[0]


def cost_sentence(pack: Pack, item: dict) -> str:
    imp = item["impact"]
    parts = []
    if imp.get("lost_margin"):
        parts.append(f"{pack.money(imp['lost_margin'])} of lost production")
    if imp.get("scrap"):
        parts.append(f"{pack.money(imp['scrap'])} of rejected bottles")
    if imp.get("repair"):
        parts.append(f"{pack.money(imp['repair'])} of repair cost")
    if imp.get("late_penalty"):
        parts.append(f"{pack.money(imp['late_penalty'])} in late-delivery penalties")
    if not parts:
        return "No significant cost is expected yet."
    body = parts[0] if len(parts) == 1 else ", ".join(parts[:-1]) + " and " + parts[-1]
    return f"About {pack.money(item['expected_impact'])} is at risk: {body}."


class Narrator:
    def __init__(self, pack: Pack):
        self.p = pack

    # ------------------------------------------------------------------ public
    def narrate(self, item: dict) -> dict:
        fn = getattr(self, f"_{item['category']}")
        head, summary, why = fn(item)
        return dict(
            urgency=URGENCY[item["severity"]],
            headline=head,
            summary=summary,
            why=why,
            cost=cost_sentence(self.p, item),
            likelihood=f"{likelihood(item['probability']).capitalize()} ({pct(item['probability'])} chance)",
            actions=self.actions(item),
        )

    def actions(self, item: dict) -> list[dict]:
        cats = [item["category"]]
        if item["category"] == "compound":
            cats = sorted({k.split(":")[0] for k in item["related"]})
        now = pd.Timestamp(item["assessed_at"])
        lead = max(1.0, 0.5 * item["starts_in_h"]) if item["starts_in_h"] else 1.0
        out = []
        for cat in cats:
            for i, a in enumerate(self.p["playbook"].get(cat, [])):
                if item["category"] == "compound" and i > 0:
                    break      # first action of each part; the plant manager coordinates
                base = item["context"].get("parts", {}).get(cat, {}) if item["category"] == "compound" else {}
                ctx = _Safe({**item["context"], **base})
                ctx["line_name"] = _short_line(self.p, item["line"])
                text = a["action"].format_map(ctx)
                person = self.p.people_for(a["owner"], item["line"])[0]
                due = now + pd.Timedelta(hours=lead * (1 + i))
                out.append(dict(text=text, owner_role=a["owner"], owner_title=self.p.role_title(a["owner"]),
                                owner_name=person["name"], due_by=due.isoformat(),
                                due_text=f"by {due:%a %d %b, %H:%M}"))
        if item["category"] == "compound":
            pm = self.p.people_for("plant_manager")[0]
            out.append(dict(text=f"Hold a 15-minute call with the owners above to agree one plan for {_short_line(self.p, item['line'])}",
                            owner_role="plant_manager", owner_title=self.p.role_title("plant_manager"),
                            owner_name=pm["name"], due_by=(now + pd.Timedelta(hours=1)).isoformat(),
                            due_text=f"by {now + pd.Timedelta(hours=1):%a %d %b, %H:%M}"))
        return out

    # ------------------------------------------------------------------ categories
    def _machine(self, x):
        e, c = x["evidence"], x["context"]
        L = line_label(self.p, x["line"])
        station = c["station"]
        head = f"The {station} on {_short_line(self.p, x['line'])} may break down within {e['expected_within_h'] // 24} days"
        stop = ("It has no backup, so the whole line would stop" if e["line_stops"]
                else f"The line would slow down to about {1 - e['capacity_loss_share']:.0%} speed")
        summary = (f"The {e['likely_component']} on the {station} of {L} is {likelihood(x['probability'])} to fail "
                   f"in the next {e['expected_within_h'] // 24} days ({pct(x['probability'])} chance). "
                   f"{stop} for about {e['repair_hours']:.0f} hours while it is repaired. "
                   f"Machine number: {c['machine']}.")
        why = []
        if e["alarms_24h"]:
            al = ", ".join(f"{n} x {k.lower()}" for k, n in e["alarms_24h"].items())
            k = sum(e["alarms_24h"].values())
            why.append(f"The machine raised {k} alarm{'s' if k != 1 else ''} in the last 24 hours ({al}).")
        signs = [d for d in e["drivers"][:2] if "alarms" not in d]
        if signs:
            why.append("Main warning signs: " + " and ".join(signs) + ".")
        moves = sorted(((abs(v), s, v) for s, v in e["sensor_trends"].items() if abs(v) >= 1.0), reverse=True)[:2]
        if moves:
            why.append("Since yesterday the " + " and the ".join(
                f"{self.p['sensors'][s]['label'].lower()} has gone {'up' if v > 0 else 'down'}" for _, s, v in moves) + ".")
        if e["similar_past_cases"]:
            why.append("The same thing happened before: " + self._case(e["similar_past_cases"][-1]))
        if e.get("maintenance_tech_short"):
            why.append("A maintenance technician is absent, so a repair would take longer than usual.")
        if e.get("orders_made_late"):
            why.append(self._orders_line(e["orders_made_late"]))
        return head, summary, why

    def _material(self, x):
        e = x["evidence"]
        now = pd.Timestamp(x["assessed_at"])
        short = _short_line(self.p, x["line"])
        head = f"{short} will run out of {e['material']} {when(now, x['starts_in_h'])}"
        nxt = e["open_purchase_orders"][0] if e["open_purchase_orders"] else None
        if nxt:
            deliv = f"The next delivery from {e['supplier']} is expected on {nxt['eta']}"
            if nxt["revised"]:
                deliv += f" (the supplier has pushed it back from {nxt['promised']})"
        else:
            deliv = f"No delivery from {e['supplier']} is on the way yet"
        if e["scenario"] == "supplier_slip":
            summary = (f"There is enough {e['material']} for about {e['days_of_cover']:.1f} days. {deliv}. "
                       f"If {e['supplier']} is late again - this happened to {e['supplier_delay_rate']:.0%} of their "
                       f"recent deliveries, by about {e['supplier_avg_delay_days']:.0f} days - the line would stop for "
                       f"about {e['stockout_hours']:.0f} hours.")
        else:
            summary = (f"There is enough {e['material']} for about {e['days_of_cover']:.1f} days. {deliv}, which is too late: "
                       f"{short} would stand still for about {e['stockout_hours']:.0f} hours with no {e['material']}.")
        why = [f"We have {e['stock_units']:,.0f} {e['unit']} left and use about "
               f"{e['hourly_use_all_lines']:,.0f} {e['unit']} every hour."]
        if len(e["lines_using_it"]) > 1:
            why.append("Other lines also use it: " + ", ".join(_short_line(self.p, l) for l in e["lines_using_it"] if l != x["line"]) + ".")
        if e.get("orders_made_late"):
            why.append(self._orders_line(e["orders_made_late"]))
        return head, summary, why

    def _workforce(self, x):
        e, c = x["evidence"], x["context"]
        short = _short_line(self.p, x["line"])
        role = c["role"]
        head = f"{short} will be short of {role}s on {len(e['shifts'])} shift{'s' if len(e['shifts']) > 1 else ''}"
        kind = "trained and certified " if e["certified"] else ""
        slow = e["speed_when_short"]
        m = re.match(r"(\d+)% of normal speed per missing person", slow)
        if m:
            slow = f"each missing person slows the line down by about {100 - int(m.group(1))}%"
        summary = f"Not enough {kind}{role}s are scheduled on {short}, so {slow}."
        why = ["Affected shifts: " + "; ".join(e["shifts"]) + ".",
               f"We were told about it on {e['notice_given']}."]
        if e.get("orders_made_late"):
            why.append(self._orders_line(e["orders_made_late"]))
        return head, summary, why

    def _quality(self, x):
        e = x["evidence"]
        short = _short_line(self.p, x["line"])
        head = f"{e['metric']} on {short} ({e['station'].lower()}) is drifting out of limits"
        f = lambda v: per100(float(str(v).rstrip("%")) / 100)
        summary = (f"In the last shift, {f(e['last_shift_out_of_spec'])} checked bottles failed the check "
                   f"(allowed range {e['limits'][0]} to {e['limits'][1]} {e['unit']}). Normally it is "
                   f"{f(e['normal_out_of_spec'])}. If nothing is done, about {f(e['projected_out_of_spec'])} bottles "
                   f"will be rejected in the next shift.")
        trend = "rising" if e["trend_per_shift"] > 0 else "falling"
        why = [f"Average readings in the last shifts: {', '.join(str(v) for v in e['recent_means'])} {e['unit']} "
               f"- the target is {e['target']}, and the readings keep {trend}."]
        return head, summary, why

    def _orders(self, x):
        e = x["evidence"]
        short = _short_line(self.p, x["line"])
        n = len(e["orders"])
        head = f"{n} customer order{'s' if n > 1 else ''} from {short} may be delivered late"
        o = e["orders"][0]
        summary = (f"Because of the problems on {short}, order {o['order']} for {o['customer']} "
                   f"({o['remaining']:,} bottles still to make) is due {o['due']} but will only be ready "
                   f"{o['projected_finish']} - about {self._days(o['days_late'])} late.")
        why = [f"Order {r['order']} for {r['customer']}: due {r['due']}, ready {r['projected_finish']}, "
               f"penalty {self.p.money(r['penalty'])}." for r in e["orders"]]
        return head, summary, why

    def _demand(self, x):
        e = x["evidence"]
        short = _short_line(self.p, x["line"])
        head = f"{short} has more orders than it can make on time"
        summary = (f"Even with no breakdowns, {short} has {e['backlog_units']:,} bottles still to make but can only "
                   f"make about {round(e['normal_output_per_day'], -3):,.0f} a day, so some orders will be late.")
        why = [f"Order {r['order']} for {r['customer']}: due {r['due']}, ready {r['projected_finish']}." for r in e["orders"]]
        return head, summary, why

    def _compound(self, x):
        e = x["evidence"]
        short = _short_line(self.p, x["line"])
        head = f"Several problems are hitting {short} at the same time"
        parts = e.get("part_texts") or [self._plain_title(t) for t in e["parts"]]
        listing = " ".join(f"({i + 1}) {p[0].upper() + p[1:]}." for i, p in enumerate(parts))
        summary = (f"{len(parts)} problems are expected on {short} at the same time: {listing} "
                   f"Each one could be handled alone, but together they put {self.p.money(x['expected_impact'])} at risk.")
        why = []
        if e["joint_penalty"] > e["sum_of_separate_penalties"]:
            why.append(f"Together they cause {self.p.money(e['joint_penalty'])} in late penalties - more than the "
                       f"{self.p.money(e['sum_of_separate_penalties'])} they would cause one at a time, because the line "
                       f"has no spare time left to catch up.")
        if e["orders"]:
            o = e["orders"][0]
            why.append(f"The order most at risk is {o['order']} for {o['customer']}, due {o['due']} - "
                       f"it could be about {self._days(o['days_late'])} late.")
        return head, summary, why

    # ------------------------------------------------------------------ helpers
    def _plain_title(self, t: str) -> str:
        t = re.sub(r"\b(L\d+)-[A-Z]{3}-\d+\b", "", t)
        t = re.sub(r"\bL(\d+)\b", r"Line \1", t)
        return re.sub(r"\s+", " ", t).replace(" - ", ": ").strip()

    @staticmethod
    def _days(d: float) -> str:
        if d < 1:
            return f"{max(1, round(d * 24))} hours"
        return f"{d:.0f} day{'s' if round(d) != 1 else ''}" if d >= 1.5 else "1 day"

    @staticmethod
    def _case(note: str) -> str:
        date, _, text = note.partition(": ")
        m = re.search(r"BREAKDOWN \S+ \(([^,]+), L(\d+)\): (.*?)\. Root cause: (.*?)\. Action: (.*?)\. Line impact (\d+)h", text)
        if not m:
            return f"on {date}: {text}"
        return (f"on {date} the {m.group(1).lower()} on Line {m.group(2)} had '{m.group(3)}'. The cause was a "
                f"{m.group(4)}. It was fixed by: {m.group(5)} ({m.group(6)} hours stopped).")

    def _orders_line(self, rows: list[dict]) -> str:
        r = rows[0]
        more = f" and {len(rows) - 1} more" if len(rows) > 1 else ""
        return (f"This would make order {r['order']} for {r['customer']} (due {r['due']}) late{more}, "
                f"adding {self.p.money(sum(x['extra_penalty'] for x in rows))} in penalties.")
