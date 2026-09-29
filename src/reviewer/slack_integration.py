"""Slack integration.

Three pieces:
  * notify_review()      - post a review with Accept / Reject buttons (Incoming Webhook)
  * handle_command()     - `/reviewer stats|rules|teach|feedback` slash command
  * handle_interaction() - the Accept / Reject button clicks

Environment:
    SLACK_WEBHOOK_URL      incoming webhook for notifications
    SLACK_SIGNING_SECRET   verifies requests coming from Slack (required by the server)
"""
from __future__ import annotations

import hashlib
import hmac
import os
import time

import requests

from .memory import VALID_STATUSES

ICONS = {"blocker": "🛑", "major": "🔴", "minor": "🟡", "nit": "⚪"}
HELP = (
    "*Reviewer commands*\n"
    "`/reviewer stats` – what the agent has learned\n"
    "`/reviewer rules` – list learned rules\n"
    "`/reviewer teach <rule>` – add a team rule\n"
    "`/reviewer feedback <id> accepted|rejected [reason]` – judge a finding"
)


def esc(text: str) -> str:
    """Escape Slack mrkdwn control characters."""
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


# ---------------------------------------------------------------- security
def verify_signature(secret: str, timestamp: str, body: bytes, signature: str,
                     now: float | None = None, tolerance: int = 300) -> bool:
    """Verify Slack's v0 request signature (also rejects replays older than 5 minutes)."""
    if not (secret and timestamp and signature):
        return False
    try:
        if abs((now or time.time()) - int(timestamp)) > tolerance:
            return False
    except ValueError:
        return False
    base = b"v0:" + timestamp.encode() + b":" + body
    expected = "v0=" + hmac.new(secret.encode(), base, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature)


# ------------------------------------------------------------ notifications
def review_blocks(result, title: str | None = None, url: str | None = None,
                  max_findings: int = 8) -> list[dict]:
    head = f"*Code review* `{result.review_id}`"
    if title:
        head += f" · {esc(title)}"
    if url:
        head += f" · <{url}|open>"
    blocks: list[dict] = [
        {"type": "section", "text": {"type": "mrkdwn", "text": head}},
        {"type": "section", "text": {"type": "mrkdwn", "text": esc(result.summary or "—")[:2900]}},
    ]
    if not result.findings:
        blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": "No new findings ✅"}})
    for f in result.findings[:max_findings]:
        loc = f"{f['file']}:{f['line']}" if f.get("line") else f["file"]
        text = (f"{ICONS.get(f['severity'], '')} *{f['severity']}* `{esc(loc)}` "
                f"({esc(f['category'] or '')}) #{f['id']}\n{esc(f['message'])}")
        if f.get("previously_rejected"):
            text += "\n:warning: _Rejected before, shown again because it is a blocker._"
        if f.get("suggestion"):
            text += f"\n_Fix:_ {esc(f['suggestion'])}"
        blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": text[:2900]}})
        blocks.append({"type": "actions", "elements": [
            {"type": "button", "text": {"type": "plain_text", "text": "Accept"},
             "style": "primary", "action_id": "fb_accepted", "value": str(f["id"])},
            {"type": "button", "text": {"type": "plain_text", "text": "Reject"},
             "style": "danger", "action_id": "fb_rejected", "value": str(f["id"])},
        ]})
    extra = len(result.findings) - max_findings
    if extra > 0:
        blocks.append({"type": "context", "elements": [
            {"type": "mrkdwn", "text": f"+{extra} more finding(s) in the dashboard"}]})
    if result.suppressed:
        blocks.append({"type": "context", "elements": [
            {"type": "mrkdwn",
             "text": f"{len(result.suppressed)} suggestion(s) hidden – your team rejected them before"}]})
    return blocks[:50]


def notify_review(result, title: str | None = None, url: str | None = None,
                  webhook_url: str | None = None) -> bool:
    """Post to Slack. Returns False (no error) when no webhook is configured."""
    webhook_url = webhook_url or os.getenv("SLACK_WEBHOOK_URL")
    if not webhook_url:
        return False
    r = requests.post(webhook_url, json={
        "text": f"Code review {result.review_id}: {len(result.findings)} finding(s)",
        "blocks": review_blocks(result, title, url)}, timeout=15)
    r.raise_for_status()
    return True


# ----------------------------------------------------------------- commands
def handle_command(agent, text: str) -> str:
    """Run a `/reviewer ...` command and return the reply text (Slack mrkdwn)."""
    parts = (text or "").strip().split(maxsplit=1)
    cmd = parts[0].lower() if parts else "help"
    rest = parts[1].strip() if len(parts) > 1 else ""

    if cmd == "stats":
        s = agent.memory.stats()
        rate = f"{s['acceptance_rate']:.0%}" if s["acceptance_rate"] is not None else "n/a"
        out = (f"*Learned so far:* {s['rules']} rules · {s['findings']} findings "
               f"({s['accepted']} accepted, {s['rejected']} rejected, {s['open']} open) · "
               f"acceptance rate {rate}")
        top = agent.memory.frequent_mistakes(3)
        if top:
            out += "\n*Most common real mistakes:* " + ", ".join(
                f"`{m['pattern_key']}` ({m['accepted']}x)" for m in top)
        return out

    if cmd == "rules":
        rules = agent.memory.list_rules(limit=15)
        if not rules:
            return "No rules yet. Add one with `/reviewer teach <rule>`."
        return "\n".join(f"#{r['id']} [{r['category']}] {esc(r['text'])}" for r in rules)

    if cmd == "teach":
        if not rest:
            return "Usage: `/reviewer teach <rule>`"
        rid = agent.memory.add_rule(rest, "convention", "*", source="slack")
        return "Rule added ✅" if rid else "I already knew that one – reinforced it."

    if cmd == "feedback":
        bits = rest.split(maxsplit=2)
        if len(bits) < 2 or not bits[0].isdigit() or bits[1].lower() not in VALID_STATUSES:
            return "Usage: `/reviewer feedback <id> accepted|rejected [reason]`"
        ok = agent.memory.set_status(int(bits[0]), bits[1].lower(), bits[2] if len(bits) > 2 else None)
        return f"Finding #{bits[0]} marked {bits[1].lower()}." if ok else f"No finding #{bits[0]}."

    return HELP


def handle_interaction(agent, payload: dict) -> str:
    """Handle an Accept/Reject button click. Returns a short confirmation."""
    actions = payload.get("actions") or []
    if not actions:
        return "Nothing to do."
    action = actions[0]
    action_id, value = action.get("action_id", ""), action.get("value", "")
    if not action_id.startswith("fb_") or not value.isdigit():
        return "Unknown action."
    status = action_id[3:]
    if status not in VALID_STATUSES:
        return "Unknown action."
    user = (payload.get("user") or {}).get("name", "someone")
    # No reason on button clicks: a reason would be stored as a permanent "do not flag" rule.
    ok = agent.memory.set_status(int(value), status)
    return f"Finding #{value} marked *{status}* by {user}." if ok else f"No finding #{value}."


def respond(response_url: str, text: str) -> None:
    """Send an ephemeral follow-up to Slack (best effort)."""
    try:
        requests.post(response_url, json={"response_type": "ephemeral", "text": text,
                                          "replace_original": False}, timeout=10)
    except requests.RequestException:
        pass
