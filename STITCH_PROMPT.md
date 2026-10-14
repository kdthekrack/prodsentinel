# Stitch prompt — Production Disruption Early Warning System

Paste **Part 1** into Stitch first to set up the app and design system. Then paste the Part 2 screen prompts one at a time, so each screen gets full detail.

---

## Part 1 — Main prompt (paste first)

Design a desktop web app (1440px wide, responsive down to tablet) called **"Early Warning — Production Disruption Control Room"** for **BlueRiver Beverages Pvt. Ltd., Plant 1**, a beverage bottling plant in India. It is an AI system that predicts production disruptions 24–72 hours before they happen. For each one it shows the cost in Indian Rupees (₹), explains why, and tells a named person what to do.

**Users:** Plant Manager (Anita Rao), Line Supervisors (Vikram Shah – L1/L2, Priya Menon – L3/L4/L5), Maintenance Lead (Arjun Patel), Quality Lead (Rahul Verma), Purchasing Manager (Sneha Kulkarni). These are busy shop-floor managers, not data scientists. Use plain language, not ML jargon.

**Plant structure:** 5 bottling lines:
- L1 Water 500ml
- L2 Water 1L
- L3 Cola 300ml
- L4 Lemon soda 600ml
- L5 Mango juice 250ml

Each line has 12 stations in sequence: Blow molder → Air compressor → Rinser → Filler → Capper → Fill inspector → Labeler → Date coder → Shrink wrapper → Palletizer → Conveyor → Chiller. There are 20 machines per line, 100 in total.

**Risk categories** (each has its own icon and accent colour):
- Machine: failure predicted by the AI model
- Material: stock-out or late supplier
- Workforce: missing certified operator
- Quality: drift in fill volume, cap torque, or label offset
- Orders: customer orders at risk of being late
- Compound: several risks overlapping on one line (highlight these strongly; they cost more than their parts)

**Severity bands** by expected impact:
- Critical: ≥ ₹5,00,000
- High: ≥ ₹1,50,000
- Medium: ≥ ₹50,000
- Low

Use Indian number formatting (₹5,00,000 / ₹1.5 L / ₹12.4 L).

**Every risk item has these fields:**
- title (e.g. "Filler M34 – pump / seal failure risk")
- line
- category
- severity
- probability (0–100%)
- starts_in_h and ends_in_h (e.g. "starts in 18h, lasts 6h")
- expected impact in ₹, broken down into lost output, scrap, repair cost, and late-order penalty
- owner role
- evidence: top 3 plain-English drivers such as "vibration rising over 24 hours" and "12 high-vibration alarms in the last 24 hours", sensor trends, recent alarms, and similar past breakdown notes
- affected customer orders (order ID, customer, due date, extra days late, extra penalty)
- recommended actions from a playbook, each with an owner

**Alert lifecycle:** New → Acknowledged → In progress → Resolved. Each severity has an acknowledge timer (Critical 15 min, High 30 min, Medium 2 h, Low 8 h). If nobody acknowledges in time, the alert escalates to the Plant Manager. Critical alerts copy the Plant Manager immediately.

**Visual style:** A modern industrial control-room look. Dark mode by default (deep slate/navy background) with a light mode toggle. Severity colours:
- Critical: red
- High: orange
- Medium: amber
- Low: slate/grey
- OK/healthy: teal-green

Use one primary brand colour (river blue) for navigation and buttons. Use the Inter font, with tabular numerals for money and hours. Use card-based layouts with 8px rounded corners, thin borders, and subtle shadows. Make information dense but calm, suitable for a wall TV as well as a laptop. Show countdown chips like "in 18h" and small sparklines for sensor trends. Include a simulated "Now: 20 Nov 2015, 06:00" time scrubber in the top bar, because the system replays 2015 data.

**Global layout:**
- Left sidebar navigation: Control Room, Alerts, Lines, Machines, Materials, Workforce, Quality, Orders, Model Performance, Settings
- Top bar: plant name, time scrubber, search, notification bell with count of unacknowledged alerts, user avatar with role

Start with the **Control Room** dashboard screen (see Screen 1).

---

## Part 2 — Screen prompts (paste one at a time)

### Screen 1 — Control Room (home dashboard)

Design the Control Room dashboard. Include:
- **KPI strip:** Total expected impact next 72h (₹38.6 L), Active alerts (14: 2 critical, 4 high, 8 medium), Unacknowledged (3, pulsing), Lines at risk (3 of 5), Machines flagged by AI (6 of 100).
- **Plant map:** 5 horizontal line lanes (L1–L5), each showing its 12 stations as small blocks coloured by current risk. Show a line health badge and product name on each lane. Clicking a station opens the machine.
- **72-hour risk timeline** (Gantt style): one row per line, with risk bars placed by start/end hour and coloured by category. Overlapping bars merge into a highlighted "Compound" band. Mark a "now" line at 0h.
- **Top risks list** ranked by priority: severity pill, category icon, title, line, "in 18h", probability %, ₹ impact, owner avatar, and an Acknowledge button.
- **Right rail:** "Needs your action" (alerts assigned to the logged-in user, with acknowledge countdown timers) and an escalation feed ("High alert on L3 escalated to Anita Rao – not acknowledged in 30 min").

### Screen 2 — Alert detail / risk explanation

Design a risk detail page for "Filler M34 on Line 3 – pump / seal failure risk", Critical, 82% probability, starts in 18h, 6h repair, ₹7.2 L expected impact, owner Maintenance Lead Arjun Patel. Sections:
1. **Header:** severity, status stepper (New → Acknowledged → In progress → Resolved), acknowledge countdown "11:42 left before escalation", and Acknowledge / Assign / Snooze / Resolve buttons.
2. **"What will happen":** plain-English summary sentence, capacity-loss chart for the line over the next 72h (100% → 0% during repair window), and a "line stops" badge because the Filler is a series station.
3. **"Why we think so":** top 3 drivers as readable sentences with sensor sparklines (vibration, pressure, voltage, rpm, last 7 days, with the 24h trend highlighted), an alarm count table for the last 24h, and 2 similar past breakdown notes quoted as cards.
4. **"What it costs":** stacked bar or waterfall showing lost output + scrap + repair + late-order penalty = total.
5. **"Orders affected":** table with Order ID, Customer (FreshMart Retail – Tier A), Due, Extra days late, Extra penalty.
6. **"What to do":** checklist of recommended actions, each with an owner avatar and a "Mark done" button, e.g. "Replace the pump / seal on filler M34 during the next planned stop, before it breaks down – Arjun Patel".
7. **Activity log:** who was notified, acknowledgement times, and comments.

### Screen 3 — Alerts inbox

Design the Alerts inbox as a filterable table. Filters: severity, category, line, owner, status, and time window. Columns: severity, category icon, title, line, starts in, probability, expected impact ₹, owner, status, acknowledge timer. Show saved views ("My alerts", "Unacknowledged", "Compound risks", "Escalated"), bulk acknowledge, and a split view with the alert detail preview on the right.

### Screen 4 — Line detail

Design a Line detail page for "Line 3 – Cola 300ml". Show:
- Station flow diagram (12 stations in sequence; parallel stations shown as stacked machines, e.g. 2 Blow molders or 2 Cappers, each with a health dot)
- Planned vs actual output per shift (last 14 days) with a bottleneck indicator
- 72h capacity forecast with and without risks
- Open order backlog, earliest due date first, with at-risk orders flagged
- Bill of materials with days of cover for each material
- Current shift staffing
- All active risks on this line

### Screen 5 — Machine health

Design a Machines page. At the top, a grid/heatmap of all 100 machines grouped by line and station, coloured by AI failure probability. Below it, a detail panel for the selected machine showing:
- Model, age
- Failure probability gauge and "likely component" (drive motor / bearing-gearbox / pneumatic valve / pump-seal)
- Sensor charts (Motor voltage V, Drive speed rpm, Pneumatic pressure psi, Vibration mm/s)
- Alarm history (Drive overcurrent trip, Motor over-temperature, Air pressure fluctuation, High vibration alarm, Encoder/sync fault)
- Days since each component was last replaced
- Maintenance notes timeline

### Screen 6 — Materials & suppliers

Design a Materials page. Show a table of 13 materials (PET preform 12g/22g, Cap 26mm/28mm, Labels for water/cola/soda/juice, Shrink film, Cola concentrate, Lemon concentrate, CO2, Mango pulp) with these columns: supplier, stock, days of cover bar (red below reorder level), lines using it, open purchase orders with promised vs revised ETA, and a "supplier delay notice" badge. Add a projected stock-out chart for the selected material, and a "Contact supplier" action.

### Screen 7 — Workforce & Quality

Design two tabs.
- **Workforce:** shift roster grid (lines × Morning/Evening/Night shifts for the next 3 days), showing planned vs present staff and highlighting missing certified roles (filler operator, QC technician, maintenance tech), with "Call in backup" and "Approve overtime" actions.
- **Quality:** SPC control charts per line for Fill volume deviation (ml, limits ±5), Cap torque (Nm, 1.2–2.0), and Label offset (mm, ±2), marking out-of-control points and drift warnings.

### Screen 8 — Customer orders

Design an Orders page. Show open orders across lines with: customer, tier (A/B/C badge), quantity, due date, projected completion, projected days late, and penalty ₹. Include a "rush order" tag, a customer penalty terms tooltip, and a "Notify customer" action. Add a summary chart of projected late penalties by customer.

### Screen 9 — Model performance

Design a "How accurate is the AI" page for managers. Use big-number cards: Failures warned in advance 180/180 (vs 57/180 for the old vibration-threshold rule), Median warning lead time 45h, Precision 87% / Recall 89%, False alarms 1.4 per day (vs 2.7), Correct failing component 97%. Add a side-by-side comparison chart of AI model vs baseline rule, and a note: "Tested on Oct–Dec 2015 data the model never saw during training."

### Screen 10 — Settings / escalation rules

Design a Settings page with sections for:
- People & roles table (name, role, lines, email, phone)
- Risk category → owner mapping
- Severity thresholds in ₹
- Acknowledge timeout per severity
- Notification channels (in-app outbox, email, SMS, webhook toggles)
- Alert threshold (₹50,000)
- Industry pack selector (currently "Beverage bottling")
