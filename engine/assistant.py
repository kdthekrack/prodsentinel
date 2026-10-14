"""Claude-powered plant assistant: grounded Q&A and translation of alerts.

The deterministic narrator stays the source of truth. Claude only:
  - answers questions by calling tools that read the live risk/alert data, and
  - translates finished alert messages for floor staff (e.g. Hindi, Marathi).
If no Anthropic credentials are configured, both degrade gracefully.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

import anthropic
import pandas as pd
from anthropic import beta_tool

MODEL = "claude-opus-5-5"
FALLBACK = dict(betas=["server-side-fallback-2026-07-01"], fallbacks="default")

SYSTEM = """You are ProdSentinel, the early-warning assistant for a manufacturing plant. You help plant managers,
line supervisors, maintenance, quality and purchasing staff understand production risks.

Rules:
- Answer ONLY from data returned by your tools. Call tools before answering; never guess numbers,
  names, dates or causes. If the data does not answer the question, say so.
- Write for someone with no technical or data background: short sentences, plain words, no codes
  (say "the filler on Line 2", not "L2-FIL-01"), amounts exactly as the tools format them.
- When relevant, end with who should do what and by when (names come from the tools).
- Reply in the language requested by the user message (default English)."""


class PlantAssistant:
    def __init__(self, service):
        self.svc = service
        has_creds = any(os.environ.get(k) for k in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_PROFILE"))             or (Path.home() / ".config" / "anthropic").exists()
        self.client = anthropic.Anthropic() if has_creds else None
        self.tools = self._tools()

    # ------------------------------------------------------------------ tools (read-only views of live state)
    def _tools(self):
        svc = self.svc

        def brief(x):
            n = svc.last["narrations"][x["key"]]
            return dict(key=x["key"], line=x["line_name"], category=x["category"], severity=x["severity"],
                        expected_impact=svc.pack.money(x["expected_impact"]), starts_in_hours=x["starts_in_h"],
                        headline=n["headline"], owner=x["owner_role"])

        @beta_tool
        def plant_summary() -> str:
            """Current plant status: time of assessment, money at risk, open alerts and status per line."""
            return json.dumps(svc.plant_summary(), default=str, ensure_ascii=False)

        @beta_tool
        def list_risks(line: str = "", category: str = "", include_watch_list: bool = False) -> str:
            """List current risks, most urgent first.

            Args:
                line: Optional line id filter, e.g. "L2". Empty for all lines.
                category: Optional: machine, material, workforce, quality, orders, demand or compound.
                include_watch_list: Also include low-impact risks that are not alerts.
            """
            items = [x for x in svc.last["items"] if (include_watch_list or x["alert"]) and
                     (not line or x["line"] == line) and (not category or x["category"] == category)]
            return json.dumps([brief(x) for x in items[:25]], ensure_ascii=False)

        @beta_tool
        def risk_details(key: str) -> str:
            """Full explanation of one risk: summary, reasons, cost breakdown, actions with owners and deadlines.

            Args:
                key: Risk key from list_risks, e.g. "material:Label juice:L5".
            """
            x = next((i for i in svc.last["items"] if i["key"] == key), None)
            if not x:
                return json.dumps({"error": f"no active risk {key}"})
            n = svc.last["narrations"][key]
            return json.dumps(dict(risk=brief(x), explanation=n, evidence=x["evidence"]), default=str, ensure_ascii=False)

        @beta_tool
        def line_orders(line: str) -> str:
            """Open customer orders on a line with projected finish time, days late and penalty (normal capacity).

            Args:
                line: Line id, e.g. "L5".
            """
            if line not in svc.pack.line_ids:
                return json.dumps({"error": "unknown line", "lines": svc.pack.line_ids})
            e = svc.engine
            now = svc.last["now"]
            o = e._project(line, e._open_orders(line, now), e._capacity(line, [], 24 * 30), now)
            rows = [dict(order=r.order_id, customer=r.customer, remaining=int(r.remaining), due=f"{r.due_ts:%d %b %H:%M}",
                         projected_finish=f"{r.proj_done:%d %b %H:%M}", days_late=round(float(r.proj_late_days), 1),
                         penalty=svc.pack.money(r.proj_penalty)) for r in o.itertuples()]
            return json.dumps(rows[:20], ensure_ascii=False)

        @beta_tool
        def alert_status(alert_id: str = "") -> str:
            """Alert workflow status: who holds it, acknowledged or not, escalations and history.

            Args:
                alert_id: e.g. "ALR-00012". Empty to list all open alerts.
            """
            if not alert_id:
                return json.dumps([{k: a[k] for k in ("alert_id", "headline", "severity", "status", "holder_name", "level")}
                                   for a in svc.esc.alerts(["open", "acknowledged"])][:25], ensure_ascii=False)
            a = next((x for x in svc.esc.alerts() if x["alert_id"] == alert_id), None)
            if not a:
                return json.dumps({"error": "not found"})
            return json.dumps(dict(alert={k: v for k, v in a.items() if k != "payload"},
                                   actions=svc.esc.actions(alert_id), history=svc.esc.history(alert_id)),
                              default=str, ensure_ascii=False)

        @beta_tool
        def who_is_responsible(role: str, line: str = "") -> str:
            """Name and contact of the person in a role, optionally for a line.

            Args:
                role: plant_manager, line_supervisor, maintenance_lead, quality_lead or purchasing.
                line: Optional line id.
            """
            return json.dumps(svc.pack.people_for(role, line or None), ensure_ascii=False)

        return [plant_summary, list_risks, risk_details, line_orders, alert_status, who_is_responsible]

    # ------------------------------------------------------------------ public
    def available(self) -> bool:
        return self.client is not None

    def ask(self, question: str, asked_by: str = "", language: str = "English") -> dict:
        if not self.available():
            return self.offline(question, asked_by)
        who = f"Asked by {asked_by}. " if asked_by else ""
        msg = f"{who}Current plant time: {self.svc.last['now']}. Reply in {language}.\n\nQuestion: {question}"
        try:
            runner = self.client.beta.messages.tool_runner(
                model=MODEL, max_tokens=16000, system=SYSTEM, tools=self.tools,
                cache_control={"type": "ephemeral"}, output_config={"effort": "low"},
                messages=[{"role": "user", "content": msg}], **FALLBACK)
            final = runner.until_done()
        except anthropic.AuthenticationError:
            return dict(answer="The assistant's API key is invalid.", ok=False)
        except anthropic.RateLimitError:
            return dict(answer="The assistant is busy, please try again in a minute.", ok=False)
        except anthropic.APIConnectionError:
            return dict(answer="The assistant cannot reach the AI service right now.", ok=False)
        except anthropic.APIStatusError as e:
            return dict(answer=f"The assistant failed ({e.status_code}).", ok=False)
        if final.stop_reason == "refusal":
            return dict(answer="The assistant could not answer this question.", ok=False)
        text = "".join(b.text for b in final.content if b.type == "text").strip()
        return dict(answer=text, ok=True)

    # ------------------------------------------------------------------ offline mode (no API key needed)
    def offline(self, question: str, asked_by: str = "") -> dict:
        """Answer common questions from live data with no language model.

        Understands: overall status, riskiest line, a specific line, a category (machine,
        material/stock, staff, quality, orders), "what should I do", who is responsible,
        an alert id, and "what if we fix ...". Answers reuse the plain-language narration."""
        import re
        svc, q = self.svc, question.lower()
        items = [x for x in svc.last["items"] if x["alert"]]
        narr = svc.last["narrations"]
        money = svc.pack.money

        def reply(text):
            return dict(answer=text, ok=True, mode="offline")

        def line_of_q():
            m = re.search(r"\b(?:line\s*|l)(\d+)\b", q)
            if m:
                cand = [l for l in svc.pack.line_ids if l.lower().endswith(m.group(1))]
                return cand[0] if cand else "unknown"
            for l in svc.pack.lines:
                if l["product"]["name"].lower().split()[0] in q:
                    return l["id"]
            return None

        def card(x, full=False):
            n = narr[x["key"]]
            out = [f"- {n['headline']}. {n['urgency']}. {money(x['expected_impact'])} at risk."]
            if full:
                out += [f"  {n['summary']}", f"  {n['cost']}"]
                out += [f"  Why: {w}" for w in n["why"][:3]]
                out += [f"  Action: {a['text']} - {a['owner_name']}, {a['due_text']}." for a in n["actions"]]
            return "\n".join(out)

        cats = {"machine": ["machine", "breakdown", "fail", "repair", "maintenance"],
                "material": ["material", "stock", "supplier", "delivery", "shortage", "label", "cap", "preform"],
                "workforce": ["staff", "operator", "people", "absent", "shift", "workforce", "worker"],
                "quality": ["quality", "defect", "reject", "scrap", "torque", "fill", "seal"],
                "orders": ["order", "customer", "late", "penalt"]}
        user = next((x for x in svc.pack["people"] if asked_by.startswith(x["name"])), None)
        line = line_of_q()
        if line == "unknown":
            return reply("I don't know that line. Lines: " + ", ".join(l.get("name", l["id"]) for l in svc.pack.lines) + ".")
        cat = next((c for c, ws in cats.items() if any(w in q for w in ws)), None)

        m = re.search(r"alr-\d+", q)
        if m:
            a = next((x for x in svc.esc.alerts() if x["alert_id"].lower() == m.group(0)), None)
            if not a:
                return reply(f"I can't find alert {m.group(0).upper()}.")
            h = svc.esc.history(a["alert_id"])[-1]
            return reply(f"{a['alert_id']}: {a['headline']}. Status: {a['status']}, now with {a['holder_name']} "
                         f"(escalated {a['level']} time{'s' if a['level'] != 1 else ''}). Last update: {h['type'].replace('_', ' ')} on "
                         f"{pd.Timestamp(h['ts']):%a %d %b, %H:%M}.")

        if "what if" in q or "if we fix" in q or "how much" in q and "save" in q:
            cand = [x for x in items if x["category"] in ("machine", "material", "workforce", "quality")
                    and (not line or x["line"] == line)]
            if not cand:
                return reply("There is no active problem to fix right now.")
            x = cand[0]
            w = svc.engine.whatif(svc.last["now"], [x["key"]])
            return reply(f"If we fix this - {narr[x['key']]['headline']} - we save about {w['saved_text']}.")

        if any(k in q for k in ("who", "responsible", "contact", "call")):
            role = svc.pack.owner_role(cat) if cat else "plant_manager"
            ppl = "; ".join(f"{x['name']} ({svc.pack.role_title(x['role'])}, {x.get('phone', '')})"
                            for x in svc.pack.people_for(role, line))
            return reply(f"Responsible: {ppl}.")

        if user and any(k in q for k in ("i do", "my ", "should i", "for me")):
            acts = [(x, a) for x in items for a in narr[x["key"]]["actions"] if a["owner_name"] == user["name"]]
            if not acts:
                return reply(f"{user['name']}, you have no open actions right now.")
            rows = [f"- {a['text']} ({a['due_text']}) - because: {narr[x['key']]['headline']}." for x, a in acts[:6]]
            return reply(f"{user['name']}, your actions:\n" + "\n".join(rows))

        sel = [x for x in items if (not line or x["line"] == line) and
               (not cat or x["category"] == cat or (cat == "orders" and x["category"] in ("orders", "demand", "compound")))]
        if not sel:
            where = f" on {svc.pack.line(line).get('name', line)}" if line else ""
            return reply(f"No {cat or 'active'} problems{where} right now. Everything is running normally.")
        if line or cat or any(k in q for k in ("why", "explain", "detail", "what should", "what to do")):
            return reply("\n".join(card(x, full=True) for x in sel[:3]))
        s_ = svc.plant_summary()
        worst = max(s_["lines"].items(), key=lambda kv: kv[1]["money_at_risk"])
        return reply(f"Plant status on {pd.Timestamp(s_['assessed_at']):%a %d %b, %H:%M}: {s_['open_alerts']} open alerts, "
                     f"{s_['money_at_risk_text']} at risk in total. "
                     f"The line most at risk is {worst[1]['name']} ({money(worst[1]['money_at_risk'])}).\nMain problems:\n"
                     + "\n".join(card(x) for x in sel[:5]))

    def translate(self, narration: dict, language: str) -> dict:
        """Translate a narrated alert, keeping numbers, names and structure unchanged."""
        if language.lower() in ("english", "en") or not self.available():
            return narration
        schema = {"type": "object", "additionalProperties": False,
                  "required": ["headline", "summary", "why", "cost", "likelihood", "actions"],
                  "properties": {"headline": {"type": "string"}, "summary": {"type": "string"},
                                 "why": {"type": "array", "items": {"type": "string"}},
                                 "cost": {"type": "string"}, "likelihood": {"type": "string"},
                                 "actions": {"type": "array", "items": {"type": "string"}}}}
        src = {k: narration[k] for k in ("headline", "summary", "why", "cost", "likelihood")}
        src["actions"] = [a["text"] for a in narration["actions"]]
        try:
            r = self.client.beta.messages.create(
                model=MODEL, max_tokens=8000, output_config={"effort": "low", "format": {"type": "json_schema", "schema": schema}},
                system="Translate plant alert messages for factory floor staff. Keep every number, amount, date, "
                       "person name, company name and material name exactly as given. Use simple everyday words.",
                messages=[{"role": "user", "content": f"Translate into {language}:\n{json.dumps(src, ensure_ascii=False)}"}],
                **FALLBACK)
            if r.stop_reason == "refusal":
                return narration
            out = json.loads(next(b.text for b in r.content if b.type == "text"))
        except (anthropic.APIError, json.JSONDecodeError, StopIteration):
            return narration
        t = dict(narration)
        t.update({k: out[k] for k in ("headline", "summary", "why", "cost", "likelihood")})
        t["actions"] = [dict(a, text=out["actions"][i]) if i < len(out["actions"]) else a
                        for i, a in enumerate(narration["actions"])]
        t["language"] = language
        return t
