"""Data adapters: turn any configured source into the engine's canonical tables.

Canonical machine-health tables (all keyed by pack machine id, e.g. 'L1-FIL-01'):
  telemetry    ts, machine, <sensor columns listed in pack.sensors>
  errors       ts, machine, code
  maintenance  ts, machine, component
  failures     ts, machine, component
  machine_info machine, model, age

Canonical plant tables (see PLANT_TABLES) are produced by engine.synth or
supplied by the plant as CSVs with the same columns.
"""
from __future__ import annotations

import pandas as pd

from engine.pack import ROOT, Pack, PackError

PLANT_TABLES = ["machines", "downtime_events", "shifts", "production_log", "quality_inspections",
                "material_inventory", "purchase_orders", "customer_orders", "notes", "ground_truth_events"]
PARSE_DATES = {"downtime_events": ["start", "end"], "shifts": ["shift_start", "notice_ts"],
               "production_log": ["shift_start"], "quality_inspections": ["shift_start"],
               "material_inventory": ["date"],
               "purchase_orders": ["order_ts", "promised_eta", "revised_eta", "revision_notice_ts", "actual_arrival"],
               "customer_orders": ["placed_ts", "due_ts", "completed_ts"], "notes": ["ts"],
               "ground_truth_events": ["start", "end", "known_ts", "impact_ts"]}


# ----------------------------------------------------------------------------- machine health
def _azure_pdm(pack: Pack, cfg: dict) -> dict[str, pd.DataFrame]:
    p = ROOT / cfg.get("path", "data/raw")
    tel = pd.read_csv(p / "PdM_telemetry.csv", parse_dates=["datetime"])
    err = pd.read_csv(p / "PdM_errors.csv", parse_dates=["datetime"])
    mnt = pd.read_csv(p / "PdM_maint.csv", parse_dates=["datetime"])
    fai = pd.read_csv(p / "PdM_failures.csv", parse_dates=["datetime"])
    inf = pd.read_csv(p / "PdM_machines.csv")

    src_ids = sorted(inf.machineID.unique())
    pack_ids = list(pack.machines.machine)
    if len(pack_ids) > len(src_ids):
        raise PackError(f"pack defines {len(pack_ids)} machines but source has only {len(src_ids)}")
    if cfg.get("machine_mapping", "sequential") != "sequential":
        raise PackError("azure_pdm: only machine_mapping=sequential is supported")
    idmap = dict(zip(src_ids, pack_ids))

    def m(df):
        df = df[df.machineID.isin(idmap)].copy()
        df["machine"] = df.machineID.map(idmap)
        return df.drop(columns="machineID").rename(columns={"datetime": "ts"})

    info = m(inf)
    info["model"] = info["model"].astype(str)
    return {
        "telemetry": m(tel.drop_duplicates(["machineID", "datetime"])),
        "errors": m(err.drop_duplicates()).rename(columns={"errorID": "code"}),
        "maintenance": m(mnt).rename(columns={"comp": "component"}),
        "failures": m(fai).rename(columns={"failure": "component"}),
        "machine_info": info,
    }


def _csv_machine_health(pack: Pack, cfg: dict) -> dict[str, pd.DataFrame]:
    """Plant-supplied CSVs. cfg['tables'][name] = {file, columns: {canonical: source}}."""
    out = {}
    for name in ("telemetry", "errors", "maintenance", "failures", "machine_info"):
        t = cfg["tables"][name]
        df = pd.read_csv(ROOT / t["file"])
        df = df.rename(columns={v: k for k, v in t.get("columns", {}).items()})
        if "ts" in df:
            df["ts"] = pd.to_datetime(df["ts"])
        out[name] = df
    unknown = set(out["telemetry"].machine) - set(pack.machines.machine)
    if unknown:
        raise PackError(f"telemetry has machines not defined in pack: {sorted(unknown)[:5]}")
    return out


MACHINE_PROVIDERS = {"azure_pdm": _azure_pdm, "csv": _csv_machine_health}


def load_machine_health(pack: Pack) -> dict[str, pd.DataFrame]:
    cfg = pack["data_sources"]["machine_health"]
    try:
        fn = MACHINE_PROVIDERS[cfg["provider"]]
    except KeyError:
        raise PackError(f"unknown machine_health provider {cfg['provider']}") from None
    mh = fn(pack, cfg)
    sensors = list(pack["sensors"])
    missing = [s for s in sensors if s not in mh["telemetry"]]
    if missing:
        raise PackError(f"telemetry missing sensor columns {missing}")
    lo, hi = pack.start, pack.end
    for k in ("telemetry", "errors", "failures"):
        mh[k] = mh[k][(mh[k].ts >= lo) & (mh[k].ts <= hi)].reset_index(drop=True)
    return mh


# ----------------------------------------------------------------------------- plant tables
def load_plant(pack: Pack) -> dict[str, pd.DataFrame]:
    cfg = pack["data_sources"]["plant"]
    if cfg["provider"] == "synthetic":
        base, files = pack.out("synthetic"), {t: f"{t}.csv" for t in PLANT_TABLES}
    elif cfg["provider"] == "csv":
        base, files = ROOT, cfg["tables"]
    else:
        raise PackError(f"unknown plant provider {cfg['provider']}")
    out = {}
    for t in PLANT_TABLES:
        f = base / files[t] if t in files else None
        if f is None or not f.exists():
            if cfg["provider"] == "synthetic":
                raise FileNotFoundError(f"{f} not found - run `python run.py synth` first")
            continue
        df = pd.read_csv(f)
        for c in PARSE_DATES.get(t, []):
            if c in df:
                df[c] = pd.to_datetime(df[c])
        out[t] = df
    return out
