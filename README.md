# ProdSentinel — AI-Enabled Production Disruption Early Warning System

Backend for **BlueRiver Beverages Pvt. Ltd.** (fictional bottling plant, 5 lines, 100 machines).
Generic engine + YAML **industry packs**: `industries/beverage_bottling.yaml` (demo) and `industries/biscuit_bakery.yaml`
(GoldenCrust Foods, 3 lines, 48 machines — runs with zero code changes, proving the engine is industry-agnostic).

## Run

```
pip install -r requirements.txt
python run.py all                      # synth -> features -> train -> evaluate  (~3 min)
python run.py risk --at "2015-10-16 06:00"
python run.py all --pack industries/biscuit_bakery.yaml
python -m pytest -q                    # 20 tests: no-look-ahead proof, API workflow + auth, adapters, webhook, what-if, narration
python -m uvicorn api.main:app         # API docs at http://127.0.0.1:8000/docs  (EWS_PACK=... to switch pack)
```
Azure PdM CSVs go in `data/raw/`. Claude assistant needs `ANTHROPIC_API_KEY` (optional; everything else works without it).

**Auth:** write endpoints (`/clock/*`, `/alerts/*/ack|resolve|comment|actions`, `/ask`, `/me`) need header `X-User-Token`;
per-person dev tokens are generated in `output/<pack>/state/tokens.json`. The actor is taken from the token (cannot be spoofed).
**What-if:** `POST /whatif {"keys": [...], "mitigation": 1.0}` -> ₹ saved if those risks are prevented.

## Pipeline

| Step | File | What it does |
|---|---|---|
| Pack | `engine/pack.py` | Loads + validates plant config (lines, stations, BOM, materials, roles, people, playbook, penalties) |
| Data | `engine/sources.py` | Adapters (Azure PdM or plant CSVs) -> canonical tables |
| Synthetic plant | `engine/synth.py` | Real Azure failures drive downtime; simulated shifts, materials, quality, orders, notes + ground truth |
| ML | `engine/features.py`, `engine/train.py` | LightGBM failure model (48 h), calibrated, SHAP drivers |
| Risk | `engine/risk.py` | Machine / material / workforce / quality / orders / demand / compound risks, priced in ₹ (no look-ahead) |
| Language | `engine/narrate.py` | Plain-language headline, why, cost, actions with owner + deadline |
| Escalation | `engine/escalation.py` | SQLite alert lifecycle, notifications, auto-escalation, reminders, audit trail |
| Service/API | `engine/service.py`, `api/main.py` | assess -> explain -> alert -> escalate; REST on a replayable plant clock |
| Assistant | `engine/assistant.py` | Claude (`claude-opus-5-5`) grounded Q&A via tools + alert translation |
| Evaluation | `engine/evaluate.py` | Backtest on held-out Oct–Dec vs ground truth |

## Impact formula
`expected ₹ = P × (lost units × margin + extra scrap × (scrap cost + margin) + repair cost) + marginal late penalty`
(marginal penalty = order projection with vs without the risk).

## Results (test period Oct–Dec 2015, replay every 3 h)

| Disruption | Warned before impact | Median lead |
|---|---|---|
| Line-stopping machine failures | 100% (84/84) | 42 h |
| Supplier delays (with stock-out) | 100% | 119 h |
| Quality drifts | 100% (3/3) | 8 h |
| Workforce gaps | 86% (6/7; the 7th = absent maintenance tech, on watch list, no machine at risk) | 32 h |
| Rush orders at risk | 100% | 195 h |
| Compound events | 100% (3/3) | 39 h |

Alert precision: material 100%, compound 92%, quality 95%, machine 81%, orders 100%, workforce 100%.
Rules tuned without touching test: machine confirmation (2 readings or score >= 0.85) on Sep; supplier-slip window (24 h) and SPC drift rules (0.75 sd x 2 shifts or 1 sd x 1 shift) on Mar-Aug.
Compound = correct if an order went late or >= 2 of its predicted problems really happened.
₹ estimates (median over each alert episode) vs realized: machine 0.98×, material 0.89×, workforce 1.01× (total).
Customer penalties warned >= 6 h ahead with the order explicitly named: 94% of ₹72.2 lakh.
Load: ~3.6 alerts/day plant-wide. Failure model: 180/180 failures, 97% right component, 1.3 false alarms/day.
Biscuit pack (same engine, no code changes): line-stopping failures 34/34, supplier delays 6/6, workforce 7/7, compound 2/2;
₹ ratio machine 1.02×, material 0.89×, workforce 0.83×. Its one test-period quality drift is missed: it hit 4 h after onset, mid-shift.
Alerts naming only orders due after the data ends are reported as unresolved, not scored.
Limits: Azure PdM is a simulated benchmark and the plant layer is synthetic — scores are optimistic vs a real plant.
No live data feed yet (replay clock); Claude assistant untested live without an API key; quality drifts that start
mid-shift cannot be seen before that shift ends (needs sub-shift sampling).

## ProdSentinel web app

```
py -3.13 -m uvicorn api.main:app --port 8000     # then open http://localhost:8000
```

- Sign in by picking a person and entering a PIN. On a person's first sign-in the PIN they type becomes their PIN (stored hashed in `output/<pack>/state/pins.json`). Sign-in only works from the local machine.
- Settings (thresholds, timeouts, owners, contacts) can be edited by the Plant Manager; they are saved to `output/<pack>/state/settings.json`.
- The Claude assistant turns on when `ANTHROPIC_API_KEY` is set.
- Styles and fonts are served locally (no CDN). After changing classes in `web/`, rebuild the CSS:
  `cd web/build && npm install && npx tailwindcss -c tailwind.config.js -i input.css -o ../app.css --minify`
