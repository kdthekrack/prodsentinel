"""ProdSentinel command-line entry point. Every command takes an industry pack.

  python run.py synth    --pack industries/beverage_bottling.yaml
  python run.py features --pack ...
  python run.py train    --pack ...
  python run.py risk     --pack ... --at "2015-11-20 06:00"
  python run.py evaluate --pack ...
  python run.py all      --pack ...
"""
import argparse
import json
import sys
import time

from engine.pack import load_pack


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("command", choices=["validate", "synth", "features", "train", "risk", "evaluate", "all"])
    ap.add_argument("--pack", default="industries/beverage_bottling.yaml")
    ap.add_argument("--at", help="timestamp for `risk` snapshot")
    ap.add_argument("--top", type=int, default=10)
    a = ap.parse_args()
    sys.stdout.reconfigure(encoding="utf-8")

    pack = load_pack(a.pack)
    print(f"[{pack['company']['name']}] {len(pack.line_ids)} lines, {len(pack.stations)} stations, "
          f"{len(pack.machines)} machines, {len(pack['materials'])} materials")
    steps = ["synth", "features", "train", "evaluate"] if a.command == "all" else [a.command]
    for step in steps:
        t0 = time.time()
        print(f"\n== {step} ==")
        if step == "validate":
            print("pack OK")
        elif step == "synth":
            from engine.synth import generate, summarize
            print(summarize(pack, generate(pack)))
        elif step == "features":
            from engine.features import build_features
            df = build_features(pack)
            print(df.shape, "\n", df.groupby("split").failure_label.value_counts().unstack(fill_value=0))
        elif step == "train":
            from engine.train import train
            r = train(pack)
            print(json.dumps({k: r[k] for k in ("threshold", "test")}, indent=1))
        elif step == "risk":
            from engine.risk import RiskEngine
            eng = RiskEngine(pack)
            items = eng.assess(a.at or str(pack.val_end + (pack.end - pack.val_end) / 2))
            print(eng.format(items, a.top))
        elif step == "evaluate":
            from engine.evaluate import evaluate
            print(json.dumps(evaluate(pack), indent=1, default=str))
        print(f"({time.time() - t0:.1f}s)")


if __name__ == "__main__":
    sys.exit(main())
