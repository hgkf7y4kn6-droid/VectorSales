"""The words in every message VectorSales sends.

Templates write each message instantly, so the system never waits on anything.
With ANTHROPIC_API_KEY set, the quote email and follow-ups are personalized by
Claude from the lead's own request notes; if that call fails or is slow, the
template text is used. Lead details are sent to Anthropic only when the key is set.
"""
import json
import os
import urllib.error
import urllib.request
from typing import Any, Dict, Optional

AI_MODEL = os.environ.get("WRITER_AI_MODEL", "claude-haiku-5-5")
AI_TIMEOUT_SECONDS = 12


def money(n: Optional[float]) -> str:
    if n is None:
        return "-"
    return f"${n:,.0f}" if float(n).is_integer() else f"${n:,.2f}"


def first_name(lead: Dict[str, Any]) -> str:
    return (lead.get("contact") or "there").split()[0]


def _sign(profile: Dict[str, Any]) -> str:
    return f"\n\n{profile['sender']}\n{profile['company']}\n{profile['phone']}"


def acknowledgement(lead: Dict[str, Any], profile: Dict[str, Any], auto_quote: bool) -> Dict[str, str]:
    when = ("Your proposal is being built from the details you sent and will be in your inbox in a few minutes."
            if auto_quote else
            "We need a couple of details (square footage and how often you'd like service) to price it. "
            "Reply to this email or call the number below and we'll have a fixed price to you the same day.")
    return {
        "subject": f"Got your request, {first_name(lead)}",
        "body": f"Hi {first_name(lead)},\n\nThanks for reaching out about cleaning for {lead['company']}. {when}"
                + _sign(profile),
    }


def quote_email(lead: Dict[str, Any], quote: Dict[str, Any], profile: Dict[str, Any], link: str) -> Dict[str, str]:
    rec = next(o for o in quote["options"] if o["tier"] == quote["recommended"])
    lines = [f"  {o['label']} ({o['name']}): {money(o['monthly'])}/month" for o in quote["options"]]
    saving = ""
    if rec.get("savings_annual") and rec["savings_annual"] > 0:
        saving = (f"\n\nBased on the {money(quote['current_monthly'])}/month you pay today, our recommended "
                  f"{rec['name']} option saves you about {money(rec['savings_annual'])} a year.")
    return {
        "subject": f"Your cleaning proposal for {lead['company']}",
        "body": (f"Hi {first_name(lead)},\n\nHere is your fixed-price proposal for "
                 f"{int(quote['sqft']):,} sq ft, {quote['visits_per_week']} visits a week. Three options:\n\n"
                 + "\n".join(lines)
                 + f"{saving}\n\nView the full proposal and accept online:\n{link}\n\n"
                 f"The price is fixed for the term, with no surprise add-ons. It's good for 30 days."
                 + _sign(profile)),
    }


FOLLOW_UPS = (
    # (days after the quote is sent, step key)
    (2, "check_in"),
    (5, "value"),
    (9, "last_call"),
)


def follow_up(step: str, lead: Dict[str, Any], quote: Dict[str, Any], profile: Dict[str, Any],
              link: str, viewed: bool) -> Dict[str, str]:
    name = first_name(lead)
    rec = next(o for o in quote["options"] if o["tier"] == quote["recommended"])
    if step == "check_in":
        opener = ("I saw you had a chance to look over the proposal." if viewed
                  else "I wanted to make sure the proposal reached you.")
        return {"subject": f"Re: Your cleaning proposal for {lead['company']}",
                "body": f"Hi {name},\n\n{opener} Any questions I can answer? Happy to walk the space "
                        f"with you this week if that's easier.\n\n{link}" + _sign(profile)}
    if step == "value":
        why = (f"Most {quote['facility_label'].lower()} clients pick {rec['name']}: it comes to "
               f"{money(rec['per_sqft'])} per sq ft a month")
        if rec.get("savings_annual") and rec["savings_annual"] > 0:
            why += f", and for you that's about {money(rec['savings_annual'])} a year less than today"
        return {"subject": f"{lead['company']}: what {rec['name']} includes",
                "body": f"Hi {name},\n\n{why}. Every visit is checked by a supervisor and you get a "
                        f"named account manager.\n\nStarting is one click:\n{link}" + _sign(profile)}
    return {"subject": f"Should I close out your proposal, {name}?",
            "body": f"Hi {name},\n\nI haven't heard back, so I'll assume the timing isn't right and "
                    f"close out your proposal. If you'd still like a cleaning partner for "
                    f"{lead['company']}, it stays open here until it expires:\n{link}" + _sign(profile)}


def welcome(lead: Dict[str, Any], invoice: Dict[str, Any], profile: Dict[str, Any], link: str) -> Dict[str, str]:
    return {
        "subject": f"Welcome aboard, {lead['company']}: invoice {invoice['number']}",
        "body": (f"Hi {first_name(lead)},\n\nThank you for choosing {profile['company']}. Your account manager "
                 f"will call within one business day to schedule the initial deep clean and walkthrough.\n\n"
                 f"Your first invoice ({invoice['number']}, {money(invoice['total'])}) is ready:\n{link}"
                 + _sign(profile)),
    }


# --- Optional personalization --------------------------------------------------

def _call_anthropic(draft: Dict[str, str], lead: Dict[str, Any]) -> Dict[str, str]:
    prompt = (
        "You are editing a sales email from a commercial cleaning company. Keep every price, link, "
        "date and the signature exactly as written. Lightly personalize the opening using the prospect's "
        "own request notes. Keep it under 160 words, plain text, warm and direct. Return only JSON: "
        '{"subject": "...", "body": "..."}\n\n'
        f"Prospect notes: {lead.get('notes', '')[:600]}\n\nDraft:\n{json.dumps(draft)}"
    )
    body = {"model": AI_MODEL, "max_tokens": 700, "messages": [{"role": "user", "content": prompt}]}
    req = urllib.request.Request(
        "https://api.anthropic.com/v1/messages",
        data=json.dumps(body).encode(),
        headers={"content-type": "application/json", "x-api-key": os.environ["ANTHROPIC_API_KEY"],
                 "anthropic-version": "2023-06-01"},
    )
    with urllib.request.urlopen(req, timeout=AI_TIMEOUT_SECONDS) as resp:  # noqa: S310 - fixed https URL
        text = json.loads(resp.read())["content"][0]["text"]
    out = json.loads(text[text.index("{"): text.rindex("}") + 1])
    if not out.get("subject") or not out.get("body"):
        raise ValueError("incomplete")
    # A link the model dropped would break the email; keep the template then.
    for token in ("http", "$"):
        if token in draft["body"] and token not in out["body"]:
            raise ValueError("dropped a link or price")
    return {"subject": str(out["subject"])[:200], "body": str(out["body"])[:4000]}


def personalize(draft: Dict[str, str], lead: Dict[str, Any]) -> Dict[str, str]:
    if not os.environ.get("ANTHROPIC_API_KEY") or not lead.get("notes") or lead.get("sample"):
        return {**draft, "written_by": "template"}
    try:
        return {**_call_anthropic(draft, lead), "written_by": "ai"}
    except (urllib.error.URLError, TimeoutError, ValueError, KeyError, IndexError, OSError):
        return {**draft, "written_by": "template"}
