"""Load and validate an industry pack (YAML) and derive the plant structure.

The rest of the engine only talks to a `Pack` object, never to industry
constants, so any plant described in YAML can be analysed.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import math

import pandas as pd
import yaml

ROOT = Path(__file__).resolve().parents[1]


class PackError(ValueError):
    pass


def _require(d: dict, keys: list[str], where: str):
    missing = [k for k in keys if k not in d]
    if missing:
        raise PackError(f"{where}: missing {', '.join(missing)}")


@dataclass
class Pack:
    raw: dict
    path: Path
    machines: pd.DataFrame = field(init=False)
    stations: pd.DataFrame = field(init=False)

    # ------------------------------------------------------------------ basics
    @property
    def id(self) -> str:
        return self.raw["pack_id"]

    def __getitem__(self, k):
        return self.raw[k]

    def get(self, k, default=None):
        return self.raw.get(k, default)

    @property
    def currency(self) -> str:
        return self.raw.get("currency", {}).get("symbol", "")

    def money(self, v: float) -> str:
        """Human-readable amount. INR uses lakh / crore, others use K / M."""
        sym, a = self.currency, abs(v)
        sign = "-" if v < 0 else ""
        if self.raw.get("currency", {}).get("code") == "INR":
            if a >= 1e7:
                return f"{sign}{sym}{a / 1e7:.2f} crore"
            if a >= 1e5:
                return f"{sign}{sym}{a / 1e5:.1f} lakh"
            return f"{sign}{sym}{a:,.0f}"
        if a >= 1e6:
            return f"{sign}{sym}{a / 1e6:.2f}M"
        if a >= 1e3:
            return f"{sign}{sym}{a / 1e3:.1f}K"
        return f"{sign}{sym}{a:,.0f}"

    # ------------------------------------------------------------------ business rules
    def customer(self, name: str) -> dict:
        return next(c for c in self.raw["customers"] if c["name"] == name)

    def penalty(self, customer: str, invoice: float, days_late: float) -> float:
        """Late-delivery penalty from the customer's contract rule."""
        if days_late <= 0:
            return 0.0
        rule = self.customer(customer)["penalty"]
        days = math.ceil(days_late - 1e-9)
        if rule["type"] == "pct_of_invoice":
            return float(round(invoice * rule["value"] * (days if rule.get("per_day") else 1), 0))
        if rule["type"] == "flat_per_day":
            return float(rule["value"] * days)
        raise PackError(f"unknown penalty type {rule['type']}")

    def owner_role(self, category: str) -> str:
        return self.raw["owners"].get(category, "plant_manager")

    def people_for(self, role: str, line: str | None = None) -> list[dict]:
        out = [x for x in self.raw["people"] if x["role"] == role and
               (x["lines"] == "all" or line is None or line in x["lines"])]
        return out or [x for x in self.raw["people"] if x["role"] == role]

    def role_title(self, role: str) -> str:
        return self.raw["roles"].get(role, {}).get("title", role.replace("_", " ").title())

    # ------------------------------------------------------------------ time
    @property
    def start(self): return pd.Timestamp(self.raw["calendar"]["start"])
    @property
    def end(self): return pd.Timestamp(self.raw["calendar"]["end"])
    @property
    def train_end(self): return pd.Timestamp(self.raw["calendar"]["train_end"])
    @property
    def val_end(self): return pd.Timestamp(self.raw["calendar"]["validation_end"])
    @property
    def shift_hours(self) -> int: return int(self.raw["calendar"]["shift_hours"])
    @property
    def shift_names(self) -> list[str]:
        s = self.raw["calendar"]["shifts"]
        return sorted(s, key=lambda k: (s[k] - self.start.hour) % 24)

    def split_of(self, ts: pd.Series):
        import numpy as np
        return np.where(ts <= self.train_end, "train", np.where(ts <= self.val_end, "val", "test"))

    # ------------------------------------------------------------------ lines
    @property
    def lines(self) -> list[dict]:
        return self.raw["lines"]

    @property
    def line_ids(self) -> list[str]:
        return [l["id"] for l in self.lines]

    def line(self, line_id: str) -> dict:
        return next(l for l in self.lines if l["id"] == line_id)

    def planned_rate(self, line_id: str) -> float:
        """Planned units per hour (rated speed x target utilization)."""
        return self.line(line_id)["product"]["units_per_hour"] * self.raw.get("target_utilization", 1.0)

    def component(self, code: str) -> dict:
        return self.raw["components"].get(code, {"name": code, "repair_hours": [4, 8], "repair_cost": 0})

    # ------------------------------------------------------------------ paths
    def out(self, *parts) -> Path:
        p = ROOT / "output" / self.id
        for x in parts:
            p = p / x
        p.mkdir(parents=True, exist_ok=True)
        return p

    # ------------------------------------------------------------------ build
    def __post_init__(self):
        self._validate()
        self._build_structure()

    def _validate(self):
        r = self.raw
        _require(r, ["pack_id", "calendar", "data_sources", "lines", "line_templates", "materials",
                     "components", "workforce", "demand", "customers", "risk"], "pack")
        _require(r["calendar"], ["start", "end", "train_end", "validation_end", "shifts", "shift_hours"], "calendar")
        if 24 % r["calendar"]["shift_hours"] or len(r["calendar"]["shifts"]) * r["calendar"]["shift_hours"] != 24:
            raise PackError("calendar: shifts x shift_hours must cover 24h")
        for l in r["lines"]:
            _require(l, ["id", "template", "product", "bom"], f"line {l.get('id')}")
            _require(l["product"], ["units_per_hour", "price", "margin"], f"line {l['id']} product")
            if l["template"] not in r["line_templates"]:
                raise PackError(f"line {l['id']}: unknown template {l['template']}")
            for m in l["bom"]:
                if m not in r["materials"]:
                    raise PackError(f"line {l['id']}: BOM material '{m}' not in materials")
        for t, tpl in r["line_templates"].items():
            for s in tpl["stations"]:
                _require(s, ["name", "code", "count", "mode"], f"template {t} station")
                if s["mode"] not in ("series", "parallel"):
                    raise PackError(f"station {s['name']}: mode must be series or parallel")
                qm = s.get("quality_metric")
                if qm and qm not in r.get("quality", {}).get("metrics", {}):
                    raise PackError(f"station {s['name']}: unknown quality_metric {qm}")
                cr = s.get("critical_role")
                if cr and cr not in r["workforce"]["roles"]:
                    raise PackError(f"station {s['name']}: unknown critical_role {cr}")
        if abs(sum(c["share"] for c in r["customers"]) - 1) > 1e-6:
            raise PackError("customers: shares must sum to 1")
        _require(r, ["roles", "people", "owners", "escalation", "playbook"], "pack")
        for x in r["people"]:
            _require(x, ["name", "role", "lines"], "person")
            if x["role"] not in r["roles"]:
                raise PackError(f"person {x['name']}: unknown role {x['role']}")
            if x["lines"] != "all" and not set(x["lines"]) <= {l["id"] for l in r["lines"]}:
                raise PackError(f"person {x['name']}: unknown line in {x['lines']}")
        for role, v in r["roles"].items():
            if v.get("escalates_to") and v["escalates_to"] not in r["roles"]:
                raise PackError(f"role {role}: escalates_to unknown role {v['escalates_to']}")
            if not any(x["role"] == role for x in r["people"]):
                raise PackError(f"role {role}: nobody is assigned to it")
        for cat, role in r["owners"].items():
            if role not in r["roles"]:
                raise PackError(f"owners.{cat}: unknown role {role}")
        for cat, acts in r["playbook"].items():
            for a in acts:
                if a["owner"] not in r["roles"]:
                    raise PackError(f"playbook.{cat}: unknown owner {a['owner']}")

    def _build_structure(self):
        st_rows, m_rows = [], []
        for l in self.lines:
            for s in self.raw["line_templates"][l["template"]]["stations"]:
                sid = f"{l['id']}-{s['code']}"
                st_rows.append(dict(line=l["id"], station_id=sid, station=s["name"], mode=s["mode"],
                                    count=s["count"], required=s.get("required", s["count"]),
                                    quality_metric=s.get("quality_metric"),
                                    critical_role=s.get("critical_role")))
                for i in range(1, s["count"] + 1):
                    m_rows.append(dict(machine=f"{sid}-{i:02d}", line=l["id"], station_id=sid,
                                       station=s["name"], mode=s["mode"]))
        self.stations = pd.DataFrame(st_rows)
        self.machines = pd.DataFrame(m_rows)


def load_pack(path: str | Path) -> Pack:
    path = Path(path)
    if not path.is_absolute():
        path = ROOT / path
    with open(path, encoding="utf-8") as f:
        raw = yaml.safe_load(f)
    return Pack(raw=raw, path=path)
