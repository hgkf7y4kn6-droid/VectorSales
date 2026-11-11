"""Quote-to-cash: what happens to a lead from the moment it arrives until the
first invoice is paid.

    lead arrives -> instant reply -> quote priced -> quote sent (automatically,
    or after one-click review for large or low-margin jobs) -> follow-ups on
    day 2, 5 and 9 -> prospect views and accepts online -> invoice -> paid

Follow-ups stop as soon as the prospect replies, accepts or declines.
Everything runs on the system clock, which a demo can move forward
(advance_clock) to show a week of follow-up in seconds.
"""
import os
import secrets
import smtplib
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage
from typing import Any, Dict, List, Optional

import pricing
import writer
from store import Store

QUOTE_VALID_DAYS = 30
STAGES = ("new", "review", "sent", "replied", "won", "lost")

DEFAULT_PROFILE = {
    "company": "Brightline Facility Services (SAMPLE)",
    "sender": "Dana Ortiz",
    "phone": "(555) 010-0142",
    "email": "quotes@brightline.example",
}


# --- time ----------------------------------------------------------------------

def now(store: Store) -> datetime:
    return datetime.now(timezone.utc) + timedelta(seconds=store.setting("clock_offset", 0))


def iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def parse(ts: str) -> datetime:
    return datetime.fromisoformat(ts.replace("Z", "+00:00"))


def advance_clock(store: Store, days: float) -> List[Dict[str, Any]]:
    store.set_setting("clock_offset", store.setting("clock_offset", 0) + int(days * 86400))
    return run_due(store)


# --- settings ------------------------------------------------------------------

def profile(store: Store) -> Dict[str, Any]:
    return {**DEFAULT_PROFILE, **store.setting("profile", {})}


def rates(store: Store) -> Dict[str, float]:
    return {**pricing.DEFAULT_RATES, **store.setting("rates", {})}


# --- timeline ------------------------------------------------------------------

def log(store: Store, lead_id: str, kind: str, text: str, **extra: Any) -> Dict[str, Any]:
    return store.put("events", {"lead_id": lead_id, "created_at": iso(now(store)), "kind": kind, "text": text, **extra})


def _set_stage(store: Store, lead: Dict[str, Any], stage: str) -> None:
    lead["stage"] = stage
    lead["updated_at"] = iso(now(store))
    store.put("leads", lead)


# --- delivery ------------------------------------------------------------------

def _smtp_configured() -> bool:
    return bool(os.environ.get("SMTP_HOST") and os.environ.get("SMTP_FROM"))


def _deliver(store: Store, msg: Dict[str, Any], lead: Dict[str, Any]) -> None:
    """Email through SMTP when configured. Sample leads never leave the outbox."""
    msg["sent_at"] = iso(now(store))
    msg["status"] = "sent"
    if lead.get("sample") or not _smtp_configured():
        msg["delivery"] = "outbox"
        return
    email = EmailMessage()
    email["From"], email["To"], email["Subject"] = os.environ["SMTP_FROM"], msg["to"], msg["subject"]
    email.set_content(msg["body"])
    try:
        with smtplib.SMTP(os.environ["SMTP_HOST"], int(os.environ.get("SMTP_PORT", "587")), timeout=15) as s:
            s.starttls()
            if os.environ.get("SMTP_USER"):
                s.login(os.environ["SMTP_USER"], os.environ.get("SMTP_PASSWORD", ""))
            s.send_message(email)
        msg["delivery"] = "email"
    except (OSError, smtplib.SMTPException) as exc:
        msg["status"], msg["delivery"], msg["error"] = "failed", "email", str(exc)[:200]


def _message(store: Store, lead: Dict[str, Any], kind: str, draft: Dict[str, str], *,
             quote_id: Optional[str] = None, send_at: Optional[datetime] = None, step: str = "") -> Dict[str, Any]:
    msg = {
        "lead_id": lead["id"], "quote_id": quote_id, "kind": kind, "step": step,
        "to": lead.get("email", ""), "subject": draft["subject"], "body": draft["body"],
        "written_by": draft.get("written_by", "template"),
        "created_at": iso(now(store)), "scheduled_for": iso(send_at or now(store)), "status": "scheduled",
    }
    if send_at is None:
        _deliver(store, msg, lead)
        log(store, lead["id"], "email", f"Sent: {msg['subject']}")
    return store.put("messages", msg)


# --- the flow ------------------------------------------------------------------

LEAD_FIELDS = ("company", "contact", "title", "email", "phone", "city", "state", "facility_type", "sqft",
               "visits_per_week", "occupants", "current_monthly", "current_supplies_monthly",
               "current_vendor", "notes", "source")


def can_price(lead: Dict[str, Any]) -> bool:
    return bool(lead.get("sqft") and lead.get("visits_per_week") and lead.get("facility_type"))


def intake(store: Store, payload: Dict[str, Any], base_url: str, sample: bool = False) -> Dict[str, Any]:
    """A new lead from the web form, the API, a CRM or the demo."""
    t = now(store)
    lead = {k: payload.get(k) for k in LEAD_FIELDS if payload.get(k) not in (None, "")}
    if not lead.get("company") or not lead.get("email"):
        raise ValueError("A lead needs at least a company and an email.")
    for k in ("sqft", "visits_per_week", "occupants", "current_monthly", "current_supplies_monthly"):
        if k in lead:
            try:
                lead[k] = float(lead[k])
            except (TypeError, ValueError):
                lead.pop(k)
    lead.update({"created_at": iso(t), "stage": "new", "sample": bool(sample or payload.get("sample")),
                 "source": lead.get("source", "Website form")})
    lead = store.put("leads", lead)
    log(store, lead["id"], "lead", f"Lead received from {lead['source']}")

    auto = can_price(lead)
    ack = _message(store, lead, "acknowledgement", writer.acknowledgement(lead, profile(store), auto))
    lead["first_response_seconds"] = max(1, int((parse(ack["sent_at"]) - t).total_seconds()))
    store.put("leads", lead)
    if auto:
        q = draft_quote(store, lead)
        if q["needs_review"]:
            log(store, lead["id"], "review", "Quote held for review: over the auto-send limit or under target margin")
            _set_stage(store, lead, "review")
        else:
            send_quote(store, q["id"], base_url, by="auto-send")
    return store.get("leads", lead["id"])


def draft_quote(store: Store, lead: Dict[str, Any]) -> Dict[str, Any]:
    r = rates(store)
    priced = pricing.quote(lead, r)
    # A new draft replaces any older unsent one.
    store.delete_where("quotes", lead_id=lead["id"], status="draft")
    number = f"Q-{1000 + len(store.list('quotes')) + 1}"
    q = store.put("quotes", {
        "lead_id": lead["id"], "number": number, "token": secrets.token_urlsafe(16),
        "created_at": iso(now(store)), "status": "draft", "selected": priced["recommended"],
        "needs_review": pricing.needs_review(priced, r), "sample": lead.get("sample", False), **priced,
    })
    rec = next(o for o in priced["options"] if o["tier"] == priced["recommended"])
    log(store, lead["id"], "quote", f"Quote {number} priced: {rec['name']} {writer.money(rec['monthly'])}/mo "
                                    f"at {rec['margin_pct']}% margin")
    return q


def proposal_link(base_url: str, q: Dict[str, Any]) -> str:
    return f"{base_url.rstrip('/')}/p/{q['token']}"


def send_quote(store: Store, quote_id: str, base_url: str, by: str = "rep") -> Dict[str, Any]:
    q = store.get("quotes", quote_id)
    if not q or q["status"] not in ("draft",):
        raise ValueError("Only a draft quote can be sent.")
    lead = store.get("leads", q["lead_id"])
    t = now(store)
    link = proposal_link(base_url, q)
    q.update({"status": "sent", "sent_at": iso(t), "sent_by": by,
              "valid_until": iso(t + timedelta(days=QUOTE_VALID_DAYS)), "base_url": base_url})
    store.put("quotes", q)
    draft = writer.personalize(writer.quote_email(lead, q, profile(store), link), lead)
    _message(store, lead, "quote", draft, quote_id=q["id"])
    lead["first_quote_seconds"] = lead.get("first_quote_seconds") or max(
        1, int((t - parse(lead["created_at"])).total_seconds()))
    _set_stage(store, lead, "sent")
    for days, step in writer.FOLLOW_UPS:
        store.put("messages", {
            "lead_id": lead["id"], "quote_id": q["id"], "kind": "follow_up", "step": step, "to": lead.get("email", ""),
            "subject": "", "body": "", "created_at": iso(t), "scheduled_for": iso(t + timedelta(days=days)),
            "status": "scheduled",
        })
    log(store, lead["id"], "sequence", "Follow-ups scheduled for day 2, 5 and 9")
    return q


def run_due(store: Store) -> List[Dict[str, Any]]:
    """Send every follow-up that is due and expire stale quotes. Called by the
    background loop and after the demo clock moves."""
    t = now(store)
    sent = []
    for msg in store.find("messages", status="scheduled"):
        if parse(msg["scheduled_for"]) > t:
            continue
        q, lead = store.get("quotes", msg["quote_id"]), store.get("leads", msg["lead_id"])
        if not q or not lead or q["status"] != "sent":
            msg["status"] = "cancelled"
            store.put("messages", msg)
            continue
        viewed = bool(q.get("viewed_at"))
        draft = writer.personalize(
            writer.follow_up(msg["step"], lead, q, profile(store), proposal_link(q["base_url"], q), viewed), lead)
        msg.update({"subject": draft["subject"], "body": draft["body"], "written_by": draft["written_by"]})
        _deliver(store, msg, lead)
        store.put("messages", msg)
        log(store, lead["id"], "email", f"Follow-up sent automatically: {msg['subject']}")
        sent.append(msg)
    for q in store.find("quotes", status="sent"):
        if q.get("valid_until") and parse(q["valid_until"]) < t:
            q["status"] = "expired"
            store.put("quotes", q)
            log(store, q["lead_id"], "quote", f"Quote {q['number']} expired")
    return sent


def _stop_sequence(store: Store, lead_id: str, reason: str) -> None:
    stopped = 0
    for msg in store.find("messages", lead_id=lead_id, status="scheduled"):
        msg["status"] = "cancelled"
        store.put("messages", msg)
        stopped += 1
    if stopped:
        log(store, lead_id, "sequence", f"Follow-ups stopped: {reason} ({stopped} not sent)")


def record_view(store: Store, q: Dict[str, Any]) -> None:
    if not q.get("viewed_at") and q["status"] == "sent":
        q["viewed_at"] = iso(now(store))
        store.put("quotes", q)
        log(store, q["lead_id"], "view", f"Prospect opened proposal {q['number']}")


def record_reply(store: Store, lead_id: str, text: str = "") -> Dict[str, Any]:
    lead = store.get("leads", lead_id)
    if not lead:
        raise ValueError("No such lead.")
    _stop_sequence(store, lead_id, "prospect replied")
    log(store, lead_id, "reply", "Prospect replied" + (f": “{text[:200]}”" if text else ""))
    if lead["stage"] in ("new", "sent", "review"):
        _set_stage(store, lead, "replied")
    return lead


def accept(store: Store, q: Dict[str, Any], tier: str, signer: str) -> Dict[str, Any]:
    if q["status"] != "sent":
        raise ValueError("This proposal can no longer be accepted.")
    if tier not in pricing.TIERS:
        raise ValueError("Pick one of the options.")
    if len(signer.strip()) < 2:
        raise ValueError("Type your full name to accept.")
    t = now(store)
    lead = store.get("leads", q["lead_id"])
    option = next(o for o in q["options"] if o["tier"] == tier)
    q.update({"status": "won", "selected": tier, "accepted_at": iso(t), "signed_by": signer.strip()[:120]})
    store.put("quotes", q)
    _stop_sequence(store, lead["id"], "proposal accepted")
    log(store, lead["id"], "won", f"Accepted online by {q['signed_by']}: {option['name']} "
                                  f"{writer.money(option['monthly'])}/mo")
    lead["won_at"] = iso(t)
    lead["mrr"] = option["monthly"]
    _set_stage(store, lead, "won")
    return create_invoice(store, q, option)


def create_invoice(store: Store, q: Dict[str, Any], option: Dict[str, Any]) -> Dict[str, Any]:
    lead = store.get("leads", q["lead_id"])
    t = now(store)
    lines = [
        {"description": f"Initial deep clean ({int(q['sqft']):,} sq ft, one time)", "amount": q["deep_clean"]},
        {"description": f"{option['name']} cleaning, first month", "amount": option["monthly"]},
    ]
    total = round(sum(line["amount"] for line in lines), 2)
    inv = store.put("invoices", {
        "lead_id": lead["id"], "quote_id": q["id"], "number": f"INV-{2000 + len(store.list('invoices')) + 1}",
        "created_at": iso(t), "due": iso(t + timedelta(days=15)), "lines": lines, "total": total,
        "status": "open", "token": q["token"], "sample": lead.get("sample", False),
    })
    log(store, lead["id"], "invoice", f"Invoice {inv['number']} issued: {writer.money(total)}, due in 15 days")
    _message(store, lead, "welcome", writer.welcome(lead, inv, profile(store), proposal_link(q["base_url"], q)),
             quote_id=q["id"])
    return inv


def pay(store: Store, inv: Dict[str, Any], method: str) -> Dict[str, Any]:
    if inv["status"] == "paid":
        return inv
    inv.update({"status": "paid", "paid_at": iso(now(store)), "method": method})
    store.put("invoices", inv)
    log(store, inv["lead_id"], "paid", f"Invoice {inv['number']} paid ({method})")
    return inv


def decline(store: Store, q: Dict[str, Any], reason: str) -> None:
    if q["status"] != "sent":
        raise ValueError("This proposal is closed.")
    q.update({"status": "lost", "loss_reason": reason[:200]})
    store.put("quotes", q)
    _stop_sequence(store, q["lead_id"], "prospect declined")
    log(store, q["lead_id"], "lost", "Prospect declined" + (f": {reason[:200]}" if reason else ""))
    _set_stage(store, store.get("leads", q["lead_id"]), "lost")


# --- reporting -----------------------------------------------------------------

def metrics(store: Store) -> Dict[str, Any]:
    leads = store.list("leads")
    quotes = store.list("quotes")
    resp = sorted(lead["first_response_seconds"] for lead in leads if lead.get("first_response_seconds"))
    to_quote = sorted(lead["first_quote_seconds"] for lead in leads if lead.get("first_quote_seconds"))
    decided = [q for q in quotes if q["status"] in ("won", "lost", "expired")]
    won = [q for q in quotes if q["status"] == "won"]

    def median(xs: List[float]) -> Optional[float]:
        return xs[len(xs) // 2] if xs else None

    def sel(q: Dict[str, Any]) -> Dict[str, Any]:
        return next(o for o in q["options"] if o["tier"] == q["selected"])

    return {
        "leads": len(leads),
        "median_response_seconds": median(resp),
        "median_quote_seconds": median(to_quote),
        "open_pipeline": sum(sel(q)["monthly"] for q in quotes if q["status"] == "sent"),
        "won_mrr": sum(sel(q)["monthly"] for q in won),
        "win_rate": round(len(won) / len(decided) * 100) if decided else None,
        "follow_ups_sent": len([m for m in store.find("messages", kind="follow_up") if m["status"] == "sent"]),
        "collected": sum(i["total"] for i in store.find("invoices", status="paid")),
    }
