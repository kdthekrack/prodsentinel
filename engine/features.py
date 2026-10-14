"""Machine-health features, generic over any pack's sensors / error codes / components.

One row per machine every `sample_every_h` hours, using only data with
timestamp <= t. Label: component failing in (t, t + failure_model_horizon_h].
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from engine.pack import Pack
from engine.sources import load_machine_health

ID_COLS = ["ts", "machine", "line", "station_id", "failure_label", "hours_to_failure", "split"]


def _telemetry(tel: pd.DataFrame, sensors: list[str]) -> pd.DataFrame:
    tel = tel.sort_values(["machine", "ts"]).reset_index(drop=True)
    g = tel.groupby("machine")[sensors]
    out = tel[["ts", "machine"]].copy()
    for w in (3, 24):
        mean = g.rolling(w, min_periods=1).mean().reset_index(level=0, drop=True)
        std = g.rolling(w, min_periods=2).std().reset_index(level=0, drop=True)
        for s in sensors:
            out[f"{s}_mean_{w}h"] = mean[s]
            out[f"{s}_std_{w}h"] = std[s]
    for s in sensors:
        out[f"{s}_trend_24h"] = out[f"{s}_mean_24h"] - out.groupby("machine")[f"{s}_mean_24h"].shift(24)
    return out


def _errors(err: pd.DataFrame, grid: pd.DataFrame, codes: list[str]) -> pd.DataFrame:
    e = err.assign(n=1).pivot_table(index=["machine", "ts"], columns="code", values="n", aggfunc="sum", fill_value=0)
    e = e.reindex(columns=codes, fill_value=0).reset_index()
    e = grid[["machine", "ts"]].merge(e, how="left", on=["machine", "ts"]).fillna(0).sort_values(["machine", "ts"])
    r = e.groupby("machine")[codes].rolling(24, min_periods=1).sum().reset_index(level=0, drop=True)
    out = e[["machine", "ts"]].copy()
    for c in codes:
        out[f"{c}_count_24h"] = r[c]
    out["errors_total_24h"] = r.sum(axis=1)
    return out


def _maintenance(mnt: pd.DataFrame, grid: pd.DataFrame, comps: list[str]) -> pd.DataFrame:
    out = grid[["machine", "ts"]].sort_values("ts").copy()
    origin = mnt.ts.min()
    for c in comps:
        m = mnt[mnt.component == c][["machine", "ts"]].rename(columns={"ts": "last"}).sort_values("last")
        out = pd.merge_asof(out, m, left_on="ts", right_on="last", by="machine", direction="backward")
        out[f"days_since_{c}"] = ((out.ts - out["last"]).dt.total_seconds() / 86400).fillna(
            (out.ts - origin).dt.total_seconds() / 86400)
        out = out.drop(columns="last")
    return out


def _labels(df: pd.DataFrame, failures: pd.DataFrame, horizon_h: int) -> pd.DataFrame:
    df = df.sort_values(["machine", "ts"]).reset_index(drop=True)
    df["failure_label"] = "none"
    df["hours_to_failure"] = np.nan
    for machine, f in failures.sort_values("ts").groupby("machine"):
        idx = df.index[df.machine == machine]
        ts = df.loc[idx, "ts"].to_numpy()
        for r in f.itertuples():
            sel = idx[(ts < np.datetime64(r.ts)) & (ts >= np.datetime64(r.ts - pd.Timedelta(hours=horizon_h)))]
            sel = sel[df.loc[sel, "hours_to_failure"].isna().to_numpy()]   # nearest failure wins
            df.loc[sel, "failure_label"] = r.component
            df.loc[sel, "hours_to_failure"] = (r.ts - df.loc[sel, "ts"]).dt.total_seconds() / 3600
    return df


def build_features(pack: Pack, save: bool = True) -> pd.DataFrame:
    mh = load_machine_health(pack)
    sensors = list(pack["sensors"])
    codes = list(pack.get("error_codes") or sorted(mh["errors"].code.unique()))
    comps = list(pack["components"])
    risk = pack["risk"]

    f = _telemetry(mh["telemetry"], sensors)
    f = f.merge(_errors(mh["errors"], f, codes), on=["machine", "ts"])
    f = f.merge(_maintenance(mh["maintenance"], f, comps), on=["machine", "ts"])
    info = mh["machine_info"].copy()
    info["model"] = pd.factorize(info["model"])[0]
    f = f.merge(info, on="machine", how="left")
    f = f.merge(pack.machines[["machine", "line", "station_id"]], on="machine")

    every = int(risk.get("sample_every_h", 3))
    hrs = ((f.ts - pack.start).dt.total_seconds() // 3600).astype(int)
    f = f[(hrs % every == 0) & (hrs >= 24)]
    f = _labels(f, mh["failures"], int(risk["failure_model_horizon_h"]))
    f["split"] = pack.split_of(f.ts)
    f = f.sort_values(["ts", "machine"]).reset_index(drop=True)
    if save:
        f.to_pickle(pack.out("processed") / "machine_features.pkl")
    return f


def feature_columns(df: pd.DataFrame) -> list[str]:
    return [c for c in df.columns if c not in ID_COLS]
