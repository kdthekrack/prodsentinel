"""Train and evaluate the machine-failure early-warning model for a pack.

LightGBM multiclass -> P(no failure / each component fails within horizon).
Train / validation / test are time-based splits from the pack calendar. The
alert threshold is tuned on validation only; test is reported untouched,
alongside a single-sensor threshold baseline.
"""
from __future__ import annotations

import json

import joblib
import lightgbm as lgb
import numpy as np
import pandas as pd
from sklearn.metrics import average_precision_score, precision_recall_fscore_support

from engine.features import feature_columns
from engine.pack import Pack
from engine.sources import load_machine_health


def _row(y, alert):
    p, r, f, _ = precision_recall_fscore_support(y, alert, average="binary", zero_division=0)
    return dict(precision=round(p, 3), recall=round(r, 3), f1=round(f, 3))


def _events(df, alert, failures, horizon_h, every_h):
    df = df.assign(alert=alert)
    lo, hi = df.ts.min(), df.ts.max()
    f = failures[(failures.ts > lo) & (failures.ts <= hi + pd.Timedelta(hours=horizon_h))]
    leads, hit = [], 0
    by_m = {m: g for m, g in df[df.alert].groupby("machine")}
    for r in f.itertuples():
        g = by_m.get(r.machine)
        if g is None:
            continue
        w = g[(g.ts < r.ts) & (g.ts >= r.ts - pd.Timedelta(hours=horizon_h))]
        if len(w):
            hit += 1
            leads.append((r.ts - w.ts.min()).total_seconds() / 3600)
    fa = df[df.alert & (df.failure_label == "none")].sort_values(["machine", "ts"])
    episodes = int((fa.groupby("machine").ts.diff() != pd.Timedelta(hours=every_h)).sum())
    days = (hi - lo).total_seconds() / 86400
    return dict(failures=len(f), detected=hit, event_recall=round(hit / max(len(f), 1), 3),
                median_lead_h=float(np.median(leads)) if leads else 0.0,
                false_alarm_episodes_per_day=round(episodes / days, 2))


def _best_threshold(y, score):
    grid = np.unique(np.quantile(score, np.linspace(0.85, 0.999, 250)))
    return float(grid[int(np.argmax([_row(y, score >= t)["f1"] for t in grid]))])


def train(pack: Pack) -> dict:
    df = pd.read_pickle(pack.out("processed") / "machine_features.pkl")
    failures = load_machine_health(pack)["failures"]
    feats = feature_columns(df)
    classes = ["none"] + list(pack["components"])
    y = df.failure_label.map({c: i for i, c in enumerate(classes)}).fillna(0).astype(int).to_numpy()
    tr, va, te = (df.split == s for s in ("train", "val", "test"))
    horizon = int(pack["risk"]["failure_model_horizon_h"])
    every = int(pack["risk"].get("sample_every_h", 3))

    model = lgb.LGBMClassifier(objective="multiclass", num_class=len(classes), n_estimators=600,
                               learning_rate=0.03, num_leaves=31, min_child_samples=40, subsample=0.8,
                               subsample_freq=1, colsample_bytree=0.8, reg_lambda=1.0,
                               random_state=pack.get("seed", 42), verbose=-1)
    model.fit(df.loc[tr, feats], y[tr], eval_set=[(df.loc[va, feats], y[va])],
              callbacks=[lgb.early_stopping(50, verbose=False)])

    proba = model.predict_proba(df[feats])
    p_any = 1 - proba[:, 0]
    comp_pred = np.array(classes[1:])[proba[:, 1:].argmax(1)]
    y_any = (y > 0).astype(int)
    thr = _best_threshold(y_any[va], p_any[va])

    # Baseline: best single-sensor 24h mean threshold (chosen on validation).
    base_col, base_thr, base_f1 = None, None, -1
    for s in pack["sensors"]:
        col = df[f"{s}_mean_24h"].to_numpy()
        for sign in (1, -1):
            t = _best_threshold(y_any[va], sign * col[va])
            f1 = _row(y_any[va], sign * col[va] >= t)["f1"]
            if f1 > base_f1:
                base_col, base_thr, base_f1, base_sign = f"{s}_mean_24h", t, f1, sign
    base = base_sign * df[base_col].to_numpy()

    report = {"threshold": round(thr, 4), "horizon_h": horizon, "baseline": base_col}
    for name, m in (("val", va), ("test", te)):
        sub, a, pos = df[m], p_any[m] >= thr, y[m] > 0
        report[name] = {
            "model": {**_row(y_any[m], a), "pr_auc": round(average_precision_score(y_any[m], p_any[m]), 3),
                      **_events(sub, a, failures, horizon, every),
                      "component_accuracy": round(float((comp_pred[m][pos & a] == sub.failure_label[pos & a]).mean()), 3)},
            "baseline": {**_row(y_any[m], base[m] >= base_thr), **_events(sub, base[m] >= base_thr, failures, horizon, every)},
        }
    imp = pd.Series(model.booster_.feature_importance("gain"), index=feats).sort_values(ascending=False)
    report["top_features"] = imp.head(10).round(0).to_dict()

    # Per-row explanation: top contributing features for the predicted class (LightGBM SHAP).
    # Only rows near/above the threshold need explanations.
    idx = np.where(p_any >= 0.5 * thr)[0]
    drivers = np.full((len(df), 3), "", dtype=object)
    if len(idx):
        contrib = model.booster_.predict(df.iloc[idx][feats], pred_contrib=True)
        k = len(feats) + 1
        cls = 1 + proba[idx, 1:].argmax(1)                  # most likely failing component
        c = contrib.reshape(len(idx), len(classes), k)[np.arange(len(idx)), cls, :-1]
        drivers[idx] = np.array(feats)[np.argsort(-c, axis=1)[:, :3]]

    out = pack.out("models")
    joblib.dump({"model": model, "features": feats, "classes": classes, "threshold": thr}, out / "failure_model.joblib")
    preds = df[["ts", "machine", "line", "station_id", "split", "failure_label", "hours_to_failure"]].copy()
    preds["p_failure"] = p_any.round(4)
    preds["predicted_component"] = comp_pred
    preds["alert"] = p_any >= thr
    for i in range(3):
        preds[f"driver_{i + 1}"] = drivers[:, i]
    for s in pack["sensors"]:
        preds[f"{s}_trend_24h"] = df[f"{s}_trend_24h"].round(3).to_numpy()
    preds["errors_total_24h"] = df["errors_total_24h"].to_numpy()
    preds.to_pickle(pack.out("processed") / "machine_predictions.pkl")
    (out / "failure_model_report.json").write_text(json.dumps(report, indent=2))
    return report
