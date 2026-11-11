"""Sample data for demos. Every company, person, phone number and email here is
made up: emails use the reserved example.com/.test domains, phone numbers the
555-01xx range, and every record is flagged sample=True, which the app shows
as a SAMPLE badge and which keeps it out of real email delivery."""
from typing import Any, Dict

import workflow
from store import Store

# The lead that "comes in" live during the demo.
DEMO_LEAD: Dict[str, Any] = {
    "company": "Harborview Dental Group (SAMPLE)",
    "contact": "Priya Shah",
    "title": "Practice Manager",
    "email": "priya.shah@harborview-dental.example",
    "phone": "(555) 010-0187",
    "city": "Cherry Hill", "state": "NJ",
    "facility_type": "medical", "sqft": 8400, "visits_per_week": 5, "occupants": 28,
    "current_vendor": "Current vendor", "current_monthly": 3350,
    "notes": "Two locations, starting with this one. Current crew misses the operatories and "
             "restrooms are a constant complaint from patients. Need someone by the 1st.",
    "source": "Website quote form",
}

# Background pipeline so the dashboard isn't empty when the demo starts.
HISTORY = [
    ({"company": "Keystone Logistics Hub (SAMPLE)", "contact": "Marcus Bell", "title": "Facilities Lead",
      "email": "m.bell@keystone-logistics.example", "phone": "(555) 010-0121", "city": "Allentown", "state": "PA",
      "facility_type": "industrial", "sqft": 42000, "visits_per_week": 3, "current_monthly": 3200,
      "source": "Google Ads landing page"}, 12, "won"),
    ({"company": "Maple Street Montessori (SAMPLE)", "contact": "Elena Ruiz", "title": "Director",
      "email": "elena@maplestreet-montessori.example", "phone": "(555) 010-0133", "city": "Lancaster", "state": "PA",
      "facility_type": "school", "sqft": 11000, "visits_per_week": 5, "current_monthly": 2650,
      "source": "Referral"}, 4, "following"),
    ({"company": "Summit Tower Offices (SAMPLE)", "contact": "Jordan Lee", "title": "Property Manager",
      "email": "jlee@summit-tower.example", "phone": "(555) 010-0156", "city": "Philadelphia", "state": "PA",
      "facility_type": "office", "sqft": 68000, "visits_per_week": 5, "current_monthly": 13900,
      "source": "Website quote form"}, 1, "review"),
    ({"company": "Ironworks Fitness (SAMPLE)", "contact": "Sam Okafor", "title": "Owner",
      "email": "sam@ironworks-fitness.example", "phone": "(555) 010-0164", "city": "Princeton", "state": "NJ",
      "facility_type": "fitness", "sqft": 6500, "visits_per_week": 7,
      "source": "Facebook lead form"}, 6, "replied"),
]


def reset(store: Store, base_url: str) -> None:
    """Wipe everything and load the sample pipeline, built by the real workflow."""
    store.clear()
    for payload, days_ago, outcome in HISTORY:
        store.set_setting("clock_offset", -int(days_ago * 86400))
        lead = workflow.intake(store, payload, base_url, sample=True)
        q = store.find("quotes", lead_id=lead["id"])[0]
        if outcome == "won":
            workflow.advance_clock(store, 2)
            workflow.record_view(store, q)
            workflow.advance_clock(store, 1)
            inv = workflow.accept(store, store.get("quotes", q["id"]), "better", payload["contact"])
            workflow.advance_clock(store, 4)
            workflow.pay(store, inv, "ACH (sample)")
        elif outcome == "following":
            workflow.advance_clock(store, 1)
            workflow.record_view(store, store.get("quotes", q["id"]))
        elif outcome == "replied":
            workflow.advance_clock(store, 2.2)
            workflow.record_reply(store, lead["id"], "Can you do Sunday mornings instead of Saturday?")
    store.set_setting("clock_offset", 0)
    workflow.run_due(store)


def demo_lead(store: Store, base_url: str) -> Dict[str, Any]:
    # Fresh each run, so the demo can be repeated without a reset.
    payload = dict(DEMO_LEAD)
    runs = sum(1 for lead in store.list("leads") if lead["company"].startswith("Harborview Dental"))
    if runs:
        payload["company"] = payload["company"].replace("(SAMPLE)", f"#{runs + 1} (SAMPLE)")
    return workflow.intake(store, payload, base_url, sample=True)

