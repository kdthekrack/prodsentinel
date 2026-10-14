r"""Alert lifecycle, notifications and automatic escalation (SQLite-backed).

Lifecycle:  open -> acknowledged -> resolved
                 \-> (condition cleared for N hours) -> auto_resolved

Escalation (checked on every tick):
  - an open alert not acknowledged before its deadline moves to the next role
    in the chain (roles.<role>.escalates_to); at the top of the chain the holder
    is reminded with a doubling interval, up to escalation.max_reminders times,
    after which the alert is flagged 'unattended'.
  - an acknowledged alert whose agreed action is overdue is escalated too.
  - a severity increase re-notifies the current holder and tightens the deadline.
  - critical alerts cc the plant manager immediately (configurable).

Every change is written to the `events` table (audit trail); every message to
`notifications` and the outbox file. External channels (email/sms/webhook) are
only used when explicitly configured.
"""
from __future__ import annotations

import json
import os
import smtplib
import sqlite3
from email.message import EmailMessage
from pathlib import Path

import pandas as pd

from engine.pack import Pack

SEV_RANK = {"low": 0, "medium": 1, "high": 2, "critical": 3}

SCHEMA = """
CREATE TABLE IF NOT EXISTS alerts (
  alert_id TEXT PRIMARY KEY, key TEXT, line TEXT, category TEXT, status TEXT, severity TEXT,
  expected_impact REAL, headline TEXT, owner_role TEXT, owner_name TEXT, level INTEGER,
  holder_role TEXT, holder_name TEXT, created_at TEXT, updated_at TEXT, last_seen_at TEXT,
  ack_deadline TEXT, acknowledged_by TEXT, acknowledged_at TEXT, resolved_at TEXT, resolved_by TEXT,
  resolution TEXT, reminders INTEGER DEFAULT 0, payload TEXT);
CREATE INDEX IF NOT EXISTS alerts_key ON alerts(key, status);
CREATE TABLE IF NOT EXISTS actions (
  alert_id TEXT, idx INTEGER, text TEXT, owner_role TEXT, owner_name TEXT, due_by TEXT,
  status TEXT, done_by TEXT, done_at TEXT, escalated INTEGER DEFAULT 0, PRIMARY KEY (alert_id, idx));
CREATE TABLE IF NOT EXISTS events (
  id INTEGER PRIMARY KEY AUTOINCREMENT, alert_id TEXT, ts TEXT, type TEXT, actor TEXT, detail TEXT);
CREATE TABLE IF NOT EXISTS mitigations (
  key TEXT PRIMARY KEY, alert_id TEXT, factor REAL, by TEXT, since TEXT, until TEXT, impact_at_mitigation REAL);
CREATE TABLE IF NOT EXISTS notifications (
  id INTEGER PRIMARY KEY AUTOINCREMENT, alert_id TEXT, ts TEXT, channel TEXT, to_name TEXT,
  to_role TEXT, address TEXT, subject TEXT, body TEXT, status TEXT);
"""


# ============================================================================ channels
class Outbox:
    name = "outbox"

    def __init__(self, path: Path):
        self.path = path

    def send(self, msg: dict) -> str:
        with open(self.path, "a", encoding="utf-8") as f:
            f.write(json.dumps(msg, ensure_ascii=False) + "\n")
        return "queued"


class Email:
    """SMTP e-mail. Enabled only when ESCALATION_SMTP_HOST is set."""
    name = "email"

    def __init__(self):
        self.host = os.environ.get("ESCALATION_SMTP_HOST")
        self.port = int(os.environ.get("ESCALATION_SMTP_PORT", "587"))
        self.user = os.environ.get("ESCALATION_SMTP_USER")
        self.pwd = os.environ.get("ESCALATION_SMTP_PASSWORD")
        self.sender = os.environ.get("ESCALATION_SMTP_FROM", self.user or "alerts@localhost")

    def send(self, msg: dict) -> str:
        if not self.host or not msg.get("address_email"):
            return "skipped"
        m = EmailMessage()
        m["From"], m["To"], m["Subject"] = self.sender, msg["address_email"], msg["subject"]
        m.set_content(msg["body"])
        with smtplib.SMTP(self.host, self.port, timeout=10) as s:
            s.starttls()
            if self.user:
                s.login(self.user, self.pwd)
            s.send_message(m)
        return "sent"


class Webhook:
    """POST JSON to ESCALATION_WEBHOOK_URL (e.g. Teams / Slack incoming webhook)."""
    name = "webhook"

    def send(self, msg: dict) -> str:
        url = os.environ.get("ESCALATION_WEBHOOK_URL")
        if not url:
            return "skipped"
        import urllib.request
        req = urllib.request.Request(url, data=json.dumps({"text": f"*{msg['subject']}*\n{msg['body']}"}).encode(),
                                     headers={"Content-Type": "application/json"})
        urllib.request.urlopen(req, timeout=10)
        return "sent"


# ============================================================================ manager
class EscalationManager:
    def __init__(self, pack: Pack, db_path: str | Path | None = None, fresh: bool = False):
        self.p = pack
        self.cfg = pack["escalation"]
        self.db_path = Path(db_path) if db_path else pack.out("state") / "alerts.db"
        self.db = sqlite3.connect(self.db_path, check_same_thread=False, timeout=10)
        self.db.row_factory = sqlite3.Row
        self.db.executescript(SCHEMA)
        cols = {r[1] for r in self.db.execute("PRAGMA table_info(alerts)")}
        if "peak_impact" not in cols:      # older databases
            self.db.execute("ALTER TABLE alerts ADD COLUMN peak_impact REAL")
        if fresh:   # clear in place: works even if another process has the file open (Windows)
            self.db.executescript("DELETE FROM alerts; DELETE FROM actions; DELETE FROM events; "
                                  "DELETE FROM notifications; DELETE FROM mitigations;")
        outbox = self.db_path.with_name("outbox.jsonl")
        if fresh and outbox.exists():
            outbox.write_text("", encoding="utf-8")
        avail = {"outbox": Outbox(outbox), "email": Email(), "webhook": Webhook()}
        self.channels = [avail[c] for c in self.cfg.get("channels", ["outbox"]) if c in avail] or [avail["outbox"]]

    # ------------------------------------------------------------------ helpers
    def _timeout(self, severity) -> pd.Timedelta:
        return pd.Timedelta(minutes=self.cfg["ack_timeout_min"].get(severity, 120))

    def _event(self, alert_id, ts, type_, actor, detail=""):
        self.db.execute("INSERT INTO events(alert_id, ts, type, actor, detail) VALUES (?,?,?,?,?)",
                        (alert_id, str(ts), type_, actor, detail))

    def _person(self, role, line):
        return self.p.people_for(role, line)[0]

    def _next_id(self) -> str:
        n = self.db.execute("SELECT COUNT(*) FROM alerts").fetchone()[0]
        return f"ALR-{n + 1:05d}"

    def notify(self, alert_id, ts, person: dict, subject: str, body: str):
        msg = dict(alert_id=alert_id, ts=str(ts), to_name=person["name"], to_role=person["role"],
                   address_email=person.get("email"), address_phone=person.get("phone"),
                   subject=subject, body=body)
        for ch in self.channels:
            try:
                status = ch.send(msg)
            except Exception as e:           # a failing channel must never stop escalation
                status = f"error: {e}"
            self.db.execute("INSERT INTO notifications(alert_id, ts, channel, to_name, to_role, address, subject, body, status)"
                            " VALUES (?,?,?,?,?,?,?,?,?)",
                            (alert_id, str(ts), ch.name, person["name"], person["role"],
                             person.get("email") if ch.name != "sms" else person.get("phone"), subject, body, status))

    def _message(self, a: dict, narration: dict, reason: str, for_person: dict) -> tuple[str, str]:
        subject = f"ProdSentinel [{a['severity'].upper()}] {narration['headline']}"
        mine = [x for x in narration["actions"] if x["owner_name"] == for_person["name"]]
        others = [x for x in narration["actions"] if x["owner_name"] != for_person["name"]]
        lines = [f"Hello {for_person['name'].split()[0]},", "", reason, "",
                 f"{narration.get('urgency', '')}: {narration['headline']}", "",
                 narration["summary"], "", f"How likely: {narration['likelihood']}", narration["cost"], ""]
        if narration["why"]:
            lines += ["Why we think so:"] + [f"  - {w}" for w in narration["why"]] + [""]
        if mine:
            lines += ["What you need to do:"] + [f"  {i + 1}. {x['text']} ({x['due_text']})" for i, x in enumerate(mine)] + [""]
        if others:
            lines += ["Others involved:"] + [f"  - {x['owner_name']} ({x['owner_title']}): {x['text']}" for x in others] + [""]
        deadline = pd.Timestamp(a["ack_deadline"])
        lines += [f"Please acknowledge alert {a['alert_id']} by {deadline:%a %d %b, %H:%M}. "
                  f"If it is not acknowledged, it will be escalated automatically."]
        return subject, "\n".join(lines)

    def _row(self, alert_id) -> dict:
        return dict(self.db.execute("SELECT * FROM alerts WHERE alert_id=?", (alert_id,)).fetchone())

    # ------------------------------------------------------------------ sync with risk engine output
    def sync(self, items: list[dict], narrations: dict[str, dict], now) -> dict:
        now = pd.Timestamp(now)
        stats = dict(created=0, updated=0, severity_up=0, auto_resolved=0)
        live = set()
        keep_min = self.cfg.get("keep_open_ratio", 0.5) * self.p["risk"]["alert_threshold"]
        open_keys = {r[0] for r in self.db.execute("SELECT key FROM alerts WHERE status IN ('open','acknowledged')")}
        for it in items:
            # Hysteresis: an existing alert stays live while the risk is above keep_min.
            if not it["alert"] and not (it["key"] in open_keys and it["expected_impact"] >= keep_min):
                continue
            live.add(it["key"])
            if not it["alert"]:
                self.db.execute("UPDATE alerts SET last_seen_at=?, expected_impact=? WHERE key=? AND status IN ('open','acknowledged')",
                                (str(now), it["expected_impact"], it["key"]))
                continue
            n = narrations[it["key"]]
            payload = json.dumps(dict(item=it, narration=n), default=str, ensure_ascii=False)
            row = self.db.execute("SELECT * FROM alerts WHERE key=? AND status IN ('open','acknowledged')",
                                  (it["key"],)).fetchone()
            if row is None and (it.get("mitigation") or {}).get("factor", 0) >= 1:
                continue
            if row is None:
                self._create(it, n, payload, now)
                stats["created"] += 1
                continue
            row = dict(row)
            up = SEV_RANK[it["severity"]] > SEV_RANK[row["severity"]]
            deadline = pd.Timestamp(row["ack_deadline"])
            if up and row["status"] == "open":
                deadline = min(deadline, now + self._timeout(it["severity"]))
            self.db.execute("UPDATE alerts SET peak_impact=MAX(COALESCE(peak_impact, 0), ?) WHERE alert_id=?",
                            (it["expected_impact"], row["alert_id"]))
            self.db.execute("UPDATE alerts SET severity=?, expected_impact=?, headline=?, updated_at=?, last_seen_at=?,"
                            " ack_deadline=?, payload=? WHERE alert_id=?",
                            (it["severity"] if up else row["severity"], it["expected_impact"], n["headline"], str(now),
                             str(now), str(deadline), payload, row["alert_id"]))
            stats["updated"] += 1
            if up:
                stats["severity_up"] += 1
                self._event(row["alert_id"], now, "severity_up", "system", f"{row['severity']} -> {it['severity']}")
                a = self._row(row["alert_id"])
                holder = self._person(a["holder_role"], a["line"])
                s, b = self._message(a, n, f"This alert has become more serious ({row['severity']} -> {it['severity']}).", holder)
                self.notify(a["alert_id"], now, holder, s, b)

        # Auto-resolve alerts whose condition has cleared for long enough.
        grace = pd.Timedelta(hours=self.cfg.get("auto_resolve_after_h", 12))
        for row in self.db.execute("SELECT * FROM alerts WHERE status IN ('open','acknowledged')").fetchall():
            row = dict(row)
            if row["key"] in live:
                continue
            if pd.Timestamp(row["last_seen_at"]) <= now - grace:
                self.db.execute("UPDATE alerts SET status='auto_resolved', resolved_at=?, resolved_by='system',"
                                " resolution='Condition no longer detected' WHERE alert_id=?", (str(now), row["alert_id"]))
                self._event(row["alert_id"], now, "auto_resolved", "system", "condition no longer detected")
                stats["auto_resolved"] += 1
        self.db.commit()
        return stats

    def _create(self, it, n, payload, now):
        aid = self._next_id()
        owner = self._person(it["owner_role"], it["line"])
        deadline = now + self._timeout(it["severity"])
        self.db.execute(
            "INSERT INTO alerts(alert_id, key, line, category, status, severity, expected_impact, headline, owner_role,"
            " owner_name, level, holder_role, holder_name, created_at, updated_at, last_seen_at, ack_deadline, payload)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (aid, it["key"], it["line"], it["category"], "open", it["severity"], it["expected_impact"], n["headline"],
             owner["role"], owner["name"], 0, owner["role"], owner["name"], str(now), str(now), str(now),
             str(deadline), payload))
        self.db.execute("UPDATE alerts SET peak_impact=? WHERE alert_id=?", (it["expected_impact"], aid))
        for i, x in enumerate(n["actions"]):
            self.db.execute("INSERT INTO actions(alert_id, idx, text, owner_role, owner_name, due_by, status)"
                            " VALUES (?,?,?,?,?,?,?)", (aid, i, x["text"], x["owner_role"], x["owner_name"], x["due_by"], "open"))
        self._event(aid, now, "created", "system", f"{it['severity']} {it['key']} impact {it['expected_impact']}")
        a = self._row(aid)
        s, b = self._message(a, n, f"You are responsible for this alert as {self.p.role_title(owner['role'])}.", owner)
        self.notify(aid, now, owner, s, b)
        self._event(aid, now, "notified", "system", owner["name"])
        # cc: plant manager on critical, and every other action owner.
        cc = {x["owner_name"]: x["owner_role"] for x in n["actions"] if x["owner_name"] != owner["name"]}
        if it["severity"] in self.cfg.get("notify_plant_manager_on", []):
            pm = self._person("plant_manager", it["line"])
            cc.setdefault(pm["name"], "plant_manager")
        for name, role in cc.items():
            person = next(x for x in self.p["people"] if x["name"] == name)
            s, b = self._message(a, n, f"For your information: {owner['name']} ({self.p.role_title(owner['role'])}) "
                                       f"owns this alert. You have actions on it.", person)
            self.notify(aid, now, person, s, b)

    # ------------------------------------------------------------------ time-driven escalation
    def tick(self, now) -> list[dict]:
        now = pd.Timestamp(now)
        out = []
        for row in self.db.execute("SELECT * FROM alerts WHERE status='open' AND ack_deadline < ?", (str(now),)).fetchall():
            out.append(self._escalate(dict(row), now, "was not acknowledged in time"))
        for row in self.db.execute(
                "SELECT a.*, x.idx AS a_idx, x.text AS a_text, x.owner_name AS a_owner FROM alerts a JOIN actions x"
                " ON a.alert_id = x.alert_id WHERE a.status='acknowledged' AND x.status='open' AND x.escalated=0"
                " AND x.due_by < ?", (now.isoformat(),)).fetchall():
            row = dict(row)
            self.db.execute("UPDATE actions SET escalated=1 WHERE alert_id=? AND idx=?", (row["alert_id"], row["a_idx"]))
            out.append(self._escalate(row, now, f"has an overdue action: '{row['a_text']}' ({row['a_owner']})"))
        self.db.commit()
        return out

    def _escalate(self, a: dict, now, why: str) -> dict:
        nxt = self.p["roles"][a["holder_role"]].get("escalates_to")
        prev = self._person(a["holder_role"], a["line"])
        new_role = nxt or a["holder_role"]
        person = self._person(new_role, a["line"])
        level = a["level"] + (1 if nxt else 0)
        reminders = int(a.get("reminders") or 0) + (0 if nxt else 1)
        if not nxt and reminders > self.cfg.get("max_reminders", 3):
            # Top of the chain and still ignored: flag once, stop messaging.
            self.db.execute("UPDATE alerts SET ack_deadline=?, updated_at=? WHERE alert_id=?",
                            ("9999-12-31", str(now), a["alert_id"]))
            self._event(a["alert_id"], now, "unattended", "system",
                        f"no response after {reminders - 1} reminders to {person['name']}")
            return dict(alert_id=a["alert_id"], to=None, level=level, why="unattended")
        backoff = 2 ** reminders if not nxt else 1
        deadline = now + self._timeout(a["severity"]) * backoff
        self.db.execute("UPDATE alerts SET level=?, holder_role=?, holder_name=?, ack_deadline=?, reminders=?, updated_at=?"
                        " WHERE alert_id=?",
                        (level, new_role, person["name"], str(deadline), reminders, str(now), a["alert_id"]))
        detail = f"{prev['name']} -> {person['name']}: alert {why}"
        self._event(a["alert_id"], now, "escalated" if nxt else "reminder", "system", detail)
        n = json.loads(a["payload"])["narration"]
        b = self._row(a["alert_id"])
        reason = (f"ESCALATED TO YOU: this alert {why}. It was with {prev['name']} ({self.p.role_title(prev['role'])})."
                  if nxt else f"REMINDER: this alert {why} and is still waiting for action.")
        s, body = self._message(b, n, reason, person)
        self.notify(a["alert_id"], now, person, "[ESCALATED] " + s if nxt else "[REMINDER] " + s, body)
        return dict(alert_id=a["alert_id"], to=person["name"], level=level, why=why)

    # ------------------------------------------------------------------ human actions
    def acknowledge(self, alert_id, actor, now, note=""):
        now = pd.Timestamp(now)
        a = self._row(alert_id)
        if a["status"] != "open":
            raise ValueError(f"{alert_id} is {a['status']}, cannot acknowledge")
        self.db.execute("UPDATE alerts SET status='acknowledged', acknowledged_by=?, acknowledged_at=?, updated_at=?"
                        " WHERE alert_id=?", (actor, str(now), str(now), alert_id))
        self._event(alert_id, now, "acknowledged", actor, note)
        self.db.commit()

    def authorize(self, alert_id, actor, now, note=""):
        """Approver authorizes the plan: every pending action is dispatched to its owner, who is notified."""
        now = pd.Timestamp(now)
        a = self._row(alert_id)
        if a["status"] not in ("open", "acknowledged"):
            raise ValueError(f"{alert_id} is {a['status']}")
        if a["status"] == "open":
            self.db.execute("UPDATE alerts SET status='acknowledged', acknowledged_by=?, acknowledged_at=?, updated_at=?"
                            " WHERE alert_id=?", (actor, str(now), str(now), alert_id))
            self._event(alert_id, now, "acknowledged", actor, "plan authorized")
        pending = [x for x in self.actions(alert_id) if x["status"] == "open"]
        if not pending:
            raise ValueError("plan already authorized")
        self.db.execute("UPDATE actions SET status='dispatched' WHERE alert_id=? AND status='open'", (alert_id,))
        self._event(alert_id, now, "authorized", actor, f"{len(pending)} action(s) dispatched {note}".strip())
        narration = json.loads(a["payload"])["narration"]
        b = self._row(alert_id)
        for name in sorted({x["owner_name"] for x in pending}):
            person = next(p for p in self.p["people"] if p["name"] == name)
            mine = [x for x in pending if x["owner_name"] == name]
            reason = (f"{actor} has authorized the plan. You have {len(mine)} task{'s' if len(mine) > 1 else ''} to do. "
                      f"Open ProdSentinel, switch to your name and mark each task done when it is finished.")
            s, body = self._message(b, narration, reason, person)
            self.notify(alert_id, now, person, "[TASK] " + s, body)
        self.db.commit()

    def complete_action(self, alert_id, idx, actor, now, note=""):
        now = pd.Timestamp(now)
        self.db.execute("UPDATE actions SET status='done', done_by=?, done_at=? WHERE alert_id=? AND idx=?",
                        (actor, str(now), alert_id, idx))
        self._event(alert_id, now, "action_done", actor, f"#{idx} {note}")
        acts = self.actions(alert_id)
        done = sum(a["status"] == "done" for a in acts)
        self._mitigate(alert_id, done / max(len(acts), 1), actor, now)
        if acts and done == len(acts) and self._row(alert_id)["status"] in ("open", "acknowledged"):
            # Every action carried out: the alert is closed and moves to history.
            self.db.execute("UPDATE alerts SET status='resolved', resolved_at=?, resolved_by=?, resolution=?, updated_at=?,"
                            " acknowledged_by=COALESCE(acknowledged_by, ?), acknowledged_at=COALESCE(acknowledged_at, ?)"
                            " WHERE alert_id=?", (str(now), actor, "All actions completed", str(now), actor, str(now), alert_id))
            self._event(alert_id, now, "resolved", actor, "All actions completed")
        self.db.commit()

    def resolve(self, alert_id, actor, now, resolution):
        now = pd.Timestamp(now)
        self.db.execute("UPDATE alerts SET status='resolved', resolved_at=?, resolved_by=?, resolution=?, updated_at=?"
                        " WHERE alert_id=?", (str(now), actor, resolution, str(now), alert_id))
        self._mitigate(alert_id, 1.0, actor, now)
        self._event(alert_id, now, "resolved", actor, resolution)
        self.db.commit()

    def _mitigate(self, alert_id, factor, actor, now):
        """Record that people acted on this alert. The risk engine lowers the risk by `factor`
        until the risk window has passed. Compound alerts mitigate each of their parts."""
        a = self._row(alert_id)
        item = json.loads(a["payload"])["item"]
        horizon = float(item.get("ends_in_h") or self.p["risk"]["horizon_h"])
        until = now + pd.Timedelta(hours=max(horizon, 12) + 24)
        keys = [item["key"]] + (item.get("related") or [])
        for k in keys:
            old = self.db.execute("SELECT factor, impact_at_mitigation, since FROM mitigations WHERE key=? AND until > ?",
                                  (k, str(now))).fetchone()
            f = max(factor, old[0] if old else 0.0)
            first_impact = old[1] if old else float(item.get("expected_impact", 0) if k == item["key"] else 0)
            self.db.execute("INSERT OR REPLACE INTO mitigations(key, alert_id, factor, by, since, until, impact_at_mitigation)"
                            " VALUES (?,?,?,?,?,?,?)", (k, alert_id, round(f, 3), actor, old[2] if old else str(now),
                                                        str(until), first_impact))
        self._event(alert_id, now, "mitigated", actor, f"{factor:.0%} of the risk removed")
        if factor >= 1:
            # Parts of a compound plan are fixed too: close their own open alerts with it.
            for k in item.get("related") or []:
                for r in self.db.execute("SELECT alert_id FROM alerts WHERE key=? AND status IN ('open','acknowledged')"
                                         " AND alert_id != ?", (k, alert_id)).fetchall():
                    note = f"Resolved together with {alert_id}"
                    self.db.execute("UPDATE alerts SET status='resolved', resolved_at=?, resolved_by=?, resolution=?,"
                                    " updated_at=? WHERE alert_id=?", (str(now), actor, note, str(now), r[0]))
                    self._event(r[0], now, "resolved", actor, note)

    def closed(self, limit=1000) -> list[dict]:
        """Closed alerts with what it took to close them (for the history page)."""
        rows = [dict(r) for r in self.db.execute(
            "SELECT * FROM alerts WHERE status IN ('resolved','auto_resolved') ORDER BY resolved_at DESC LIMIT ?", (limit,))]
        out = []
        for a in rows:
            acts = self.actions(a["alert_id"])
            ev = self.history(a["alert_id"])
            mit = self.db.execute("SELECT impact_at_mitigation FROM mitigations WHERE key=? AND alert_id=?",
                                  (a["key"], a["alert_id"])).fetchone()
            raised = max([float(a["expected_impact"] or 0), float(a.get("peak_impact") or 0)] +
                         ([float(mit[0])] if mit and mit[0] else []))
            by_people = a["status"] == "resolved"
            hrs = (pd.Timestamp(a["resolved_at"]) - pd.Timestamp(a["created_at"])).total_seconds() / 3600
            payload = json.loads(a["payload"])
            out.append(dict(
                alert_id=a["alert_id"], key=a["key"], line=a["line"], category=a["category"], severity=a["severity"],
                headline=a["headline"], status=a["status"], created_at=a["created_at"], resolved_at=a["resolved_at"],
                resolved_by=a["resolved_by"] if by_people else "System", resolution=a["resolution"],
                acknowledged_by=a["acknowledged_by"], owner_name=a["owner_name"],
                hours_to_close=round(hrs, 1), actions_done=sum(x["status"] == "done" for x in acts), actions_total=len(acts),
                escalations=sum(e["type"] == "escalated" for e in ev),
                impact_when_raised=round(raised), money_protected=round(raised) if by_people else 0,
                summary=payload["narration"].get("summary", "")))
        return out

    def active_mitigations(self, now) -> dict:
        now = pd.Timestamp(now)
        rows = self.db.execute("SELECT * FROM mitigations WHERE since <= ? AND until > ?", (str(now), str(now))).fetchall()
        return {r["key"]: dict(r) for r in rows}

    def comment(self, alert_id, actor, now, text):
        self._event(alert_id, pd.Timestamp(now), "comment", actor, text)
        self.db.commit()

    # ------------------------------------------------------------------ queries
    def alerts(self, status=None, person=None) -> list[dict]:
        q, args = "SELECT * FROM alerts", []
        cond = []
        if status:
            cond.append(f"status IN ({','.join('?' * len(status))})")
            args += list(status)
        if person:
            cond.append("(holder_name=? OR alert_id IN (SELECT alert_id FROM actions WHERE owner_name=?))")
            args += [person, person]
        if cond:
            q += " WHERE " + " AND ".join(cond)
        rows = [dict(r) for r in self.db.execute(q + " ORDER BY expected_impact DESC", args).fetchall()]
        for r in rows:
            r["payload"] = json.loads(r["payload"])
        return rows

    def history(self, alert_id) -> list[dict]:
        return [dict(r) for r in self.db.execute("SELECT * FROM events WHERE alert_id=? ORDER BY id", (alert_id,))]

    def actions(self, alert_id) -> list[dict]:
        return [dict(r) for r in self.db.execute("SELECT * FROM actions WHERE alert_id=? ORDER BY idx", (alert_id,))]

    def notifications(self, alert_id=None, limit=50) -> list[dict]:
        q, args = "SELECT * FROM notifications", []
        if alert_id:
            q, args = q + " WHERE alert_id=?", [alert_id]
        return [dict(r) for r in self.db.execute(q + " ORDER BY id DESC LIMIT ?", args + [limit])]
