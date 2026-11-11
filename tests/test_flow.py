"""The whole quote-to-cash flow through the HTTP API, plus the pricing rules it rests on."""
import importlib
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pricing  # noqa: E402

LEAD = {"company": "Test Clinic (SAMPLE)", "contact": "Pat Doe", "email": "pat@clinic.example",
        "facility_type": "medical", "sqft": 8400, "visits_per_week": 5, "occupants": 28, "current_monthly": 3350}


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("DEMO_MODE", "1")
    monkeypatch.delenv("AUTH_USERS", raising=False)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("SMTP_HOST", raising=False)
    import server
    importlib.reload(server)
    from fastapi.testclient import TestClient
    with TestClient(server.app) as c:
        yield c


# --- pricing -------------------------------------------------------------------

def test_price_is_cost_over_one_minus_margin_rounded_up_to_5():
    assert pricing.price_for(700, 30) == 1000
    assert pricing.price_for(700.01, 30) == 1005


def test_tiers_substitute_touchups_for_full_cleans():
    q = pricing.quote(LEAD)
    good, better, best = q["options"]
    assert (good["full_cleans"], good["touchups"]) == (5, 0)
    assert (better["full_cleans"], better["touchups"]) == (3, 2)
    assert better["monthly"] < good["monthly"]
    assert best["supply_program"] > 0 and best["monthly"] > better["monthly"]
    for o in q["options"]:
        assert o["margin_pct"] >= 30
    assert q["recommended"] == "better"
    assert better["savings_monthly"] == 3350 - better["monthly"]
    # Best is compared with the vendor plus the supplies it replaces.
    assert best["compare_to"] == round(3350 + q["current_supplies_monthly"], 2)


def test_lower_cost_per_use_towel_format_is_cheaper():
    c, m, h = (pricing.consumables(50, f) for f in ("cfold", "multifold", "hardwound"))
    assert c > m > h > 0


def test_large_jobs_wait_for_review():
    big = pricing.quote({**LEAD, "facility_type": "office", "sqft": 68000})
    assert pricing.needs_review(big)
    assert not pricing.needs_review(pricing.quote(LEAD))


# --- the flow ------------------------------------------------------------------

def _advance(client, days, lead_id):
    """Move the clock; the follow-ups sent for this lead (the sample pipeline has its own)."""
    sent = client.post("/api/clock/advance", json={"days": days}).json()["sent"]
    return [m for m in sent if m["lead_id"] == lead_id]


def _lead(client, company):
    return next(lead for lead in client.get("/api/state").json()["leads"] if lead["company"] == company)


def test_lead_to_cash(client):
    r = client.post("/api/inbound", json=LEAD)
    assert r.status_code == 200 and r.json()["quoted"] is True
    lead = _lead(client, LEAD["company"])
    assert lead["stage"] == "sent" and lead["sample"] is True
    assert lead["first_response_seconds"] <= 5

    d = client.get(f"/api/leads/{lead['id']}").json()
    kinds = [m["kind"] for m in d["messages"]]
    assert kinds[:2] == ["acknowledgement", "quote"]
    assert kinds.count("follow_up") == 3
    assert all(m["delivery"] == "outbox" for m in d["messages"] if m["status"] == "sent")
    token = d["quotes"][0]["token"]

    # Day 2: the check-in goes out on its own.
    sent = _advance(client, 2, lead["id"])
    assert [m["step"] for m in sent] == ["check_in"]
    assert "reached you" in sent[0]["body"]

    # The customer's page never shows cost or margin.
    pub = client.get(f"/api/public/{token}").json()
    assert all("cost" not in o and "margin_pct" not in o for o in pub["quote"]["options"])
    assert "rates" not in pub["quote"]

    # Day 5 knows she looked at it.
    sent = _advance(client, 3, lead["id"])
    assert [m["step"] for m in sent] == ["value"]

    inv = client.post(f"/api/public/{token}/accept", json={"tier": "better", "name": "Pat Doe"}).json()
    assert inv["status"] == "open" and len(inv["lines"]) == 2
    d = client.get(f"/api/leads/{lead['id']}").json()
    assert d["lead"]["stage"] == "won"
    assert [m["status"] for m in d["messages"] if m["step"] == "last_call"] == ["cancelled"]
    assert d["messages"][-1]["kind"] == "welcome"

    # No more follow-ups after acceptance, even weeks later.
    assert _advance(client, 10, lead["id"]) == []

    paid = client.post(f"/api/public/{token}/pay").json()
    assert paid["status"] == "paid"
    m = client.get("/api/state").json()["metrics"]
    assert m["collected"] >= paid["total"] and m["won_mrr"] >= inv["lines"][1]["amount"]


def test_reply_stops_follow_ups(client):
    client.post("/api/inbound", json={**LEAD, "company": "Reply Co (SAMPLE)"})
    lead = _lead(client, "Reply Co (SAMPLE)")
    client.post(f"/api/leads/{lead['id']}/reply", json={"text": "Call me Tuesday"})
    assert _advance(client, 10, lead["id"]) == []
    assert _lead(client, "Reply Co (SAMPLE)")["stage"] == "replied"


def test_unpriceable_lead_gets_reply_asking_for_details(client):
    client.post("/api/inbound", json={"company": "Vague Inc (SAMPLE)", "email": "a@vague.example"})
    lead = _lead(client, "Vague Inc (SAMPLE)")
    d = client.get(f"/api/leads/{lead['id']}").json()
    assert lead["stage"] == "new" and not d["quotes"]
    assert "square footage" in d["messages"][0]["body"]


def test_big_quote_held_then_sent_by_rep(client):
    client.post("/api/inbound", json={**LEAD, "company": "Big Tower (SAMPLE)", "facility_type": "office", "sqft": 90000})
    lead = _lead(client, "Big Tower (SAMPLE)")
    assert lead["stage"] == "review"
    q = client.get(f"/api/leads/{lead['id']}").json()["quotes"][0]
    assert client.post(f"/api/quotes/{q['id']}/send", json={"tier": "good"}).status_code == 200
    assert _lead(client, "Big Tower (SAMPLE)")["stage"] == "sent"


def test_demo_seed_and_live_lead(client):
    state = client.get("/api/state").json()
    assert {lead["stage"] for lead in state["leads"]} >= {"won", "sent", "review", "replied"}
    assert all(lead["sample"] for lead in state["leads"])
    one = client.post("/api/demo/lead").json()
    two = client.post("/api/demo/lead").json()
    assert one["company"] != two["company"]


def test_api_closed_without_sign_in_unless_demo(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.delenv("DEMO_MODE", raising=False)
    monkeypatch.delenv("AUTH_USERS", raising=False)
    import server
    importlib.reload(server)
    from fastapi.testclient import TestClient
    with TestClient(server.app) as c:
        assert c.get("/api/state").status_code == 503
        assert c.get("/health").status_code == 200
        assert c.get("/").status_code == 200
