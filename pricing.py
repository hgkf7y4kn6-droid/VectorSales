"""Fixed-price quoting for commercial cleaning contracts, with a cost comparison
against what the prospect pays today.

Good / Better / Best works as it does in the Cost Comparison Tool. It is a
substitution strategy, not three quality levels of the same service:

- Good   "Like-for-like": the closest match to the prospect's current service,
         with every visit a full clean.
- Better "Smart schedule": the same visits per week, but only some are full
         cleans. The rest are touch-ups (restrooms, trash, entries, breakroom).
         The space looks the same and costs less.
- Best   "Smart schedule + supplies": Better's schedule plus quarterly floor
         care and restroom consumables, moved to a lower cost-per-use format
         (hardwound roll towels instead of C-fold). One invoice covers it all, so
         it is compared with the vendor plus what they spend on supplies today.

The price is cost divided by (1 - margin), rounded up to the next $5, the same
rule as the Cost Comparison Tool's margin calculator. All rates below are
defaults; a client sets their own in Settings.
"""
import math
from typing import Any, Dict, List, Optional

WEEKS_PER_MONTH = 52 / 12
WORKDAYS_PER_MONTH = 21.7

# Square feet one cleaner finishes per labor hour, by facility type.
FACILITY_TYPES: Dict[str, Dict[str, Any]] = {
    "office": {"label": "Office", "sqft_per_hour": 3500},
    "medical": {"label": "Medical / dental", "sqft_per_hour": 2200},
    "school": {"label": "School / daycare", "sqft_per_hour": 3000},
    "retail": {"label": "Retail / showroom", "sqft_per_hour": 4000},
    "industrial": {"label": "Warehouse / industrial", "sqft_per_hour": 6000},
    "fitness": {"label": "Gym / fitness", "sqft_per_hour": 2500},
}

DEFAULT_RATES: Dict[str, float] = {
    "wage": 18.00,              # $/hour paid to cleaners
    "burden_pct": 22.0,         # payroll taxes, workers' comp, insurance
    "supplies_pct": 6.0,        # chemicals, liners, equipment wear (% of labor)
    "overhead_pct": 12.0,       # supervision, admin, travel (% of direct cost)
    "margin_pct": 30.0,         # target gross margin on the price
    "min_hours_per_visit": 1.5,
    "touchup_share": 0.35,      # a touch-up visit takes this share of a full clean
    "floor_care_per_sqft": 0.12,  # per strip/wax or extraction, hard-floor share only
    "deep_clean_per_sqft": 0.08,  # one-time initial clean
    "deep_clean_min": 350.0,
    "auto_send_under": 5000.0,  # quotes below this monthly price go out without review
}

# Restroom paper: the Cost Comparison Tool's towel substitution, per use.
TOWEL_FORMATS = {
    "cfold": {"label": "C-fold towels", "cost_per_use": 0.0120},
    "multifold": {"label": "Multifold towels", "cost_per_use": 0.0098},
    "hardwound": {"label": "Hardwound roll towels", "cost_per_use": 0.0062},
}
TOWEL_USES_PER_PERSON_DAY = 3
# Soap, tissue and liners scale with towel use; this turns towel spend into total consumables.
CONSUMABLES_MULTIPLIER = 1.8

TIERS = ("good", "better", "best")
TIER_INFO = {
    "good": {"name": "Like-for-like", "label": "Good",
             "summary": "Matches your current service. Every visit is a full clean."},
    "better": {"name": "Smart schedule", "label": "Better",
               "summary": "Same visits per week. Some are full cleans, the rest are touch-ups of "
                          "restrooms, trash, entries and breakroom. Looks the same, costs less."},
    "best": {"name": "Smart schedule + supplies", "label": "Best",
             "summary": "Smart schedule plus quarterly floor care, and restroom supplies included "
                        "on a lower cost-per-use format. One invoice for everything."},
}


def _num(value: Any, default: float = 0.0) -> float:
    try:
        n = float(value)
    except (TypeError, ValueError):
        return default
    return n if math.isfinite(n) else default


def round_up(amount: float, step: float = 5.0) -> float:
    """Round up to the next step, ignoring float noise (100.0000001 stays 100)."""
    return math.ceil(amount / step - 1e-9) * step


def price_for(cost: float, margin_pct: float, step: float = 5.0) -> float:
    margin = min(95.0, max(0.0, margin_pct)) / 100
    return round_up(cost / (1 - margin), step)


def full_cleans_per_week(visits: int, tier: str) -> int:
    """How many of a week's visits are full cleans in each tier."""
    if tier == "good" or visits <= 2:
        return visits
    return max(2, math.ceil(visits * 0.6))


def _labor_cost(hours: float, r: Dict[str, float]) -> float:
    return hours * r["wage"] * (1 + r["burden_pct"] / 100)


def consumables(occupants: int, fmt: str) -> float:
    uses = occupants * WORKDAYS_PER_MONTH * TOWEL_USES_PER_PERSON_DAY
    return uses * TOWEL_FORMATS[fmt]["cost_per_use"] * CONSUMABLES_MULTIPLIER


def quote(lead: Dict[str, Any], rates: Optional[Dict[str, float]] = None) -> Dict[str, Any]:
    """Three priced options for a lead, plus the comparison with current spend.

    The lead needs facility_type, sqft and visits_per_week. Optional:
    occupants (restroom supplies), current_monthly (what they pay a vendor now),
    current_supplies_monthly (what they spend on restroom paper and soap).
    """
    r = {**DEFAULT_RATES, **(rates or {})}
    ftype = lead.get("facility_type") if lead.get("facility_type") in FACILITY_TYPES else "office"
    sqft = max(500.0, _num(lead.get("sqft"), 10000))
    visits = int(min(7, max(1, _num(lead.get("visits_per_week"), 5))))
    occupants = int(max(0, _num(lead.get("occupants"), round(sqft / 250))))
    current = _num(lead.get("current_monthly"))
    current_supplies = _num(lead.get("current_supplies_monthly")) or round(consumables(occupants, "cfold"), 2)

    full_hours = max(r["min_hours_per_visit"], sqft / FACILITY_TYPES[ftype]["sqft_per_hour"])
    touch_hours = max(1.0, full_hours * r["touchup_share"])
    floor_care = sqft * 0.5 * r["floor_care_per_sqft"] * 4 / 12

    options: List[Dict[str, Any]] = []
    for tier in TIERS:
        full = full_cleans_per_week(visits, tier)
        touch = visits - full
        hours = (full * full_hours + touch * touch_hours) * WEEKS_PER_MONTH
        labor = _labor_cost(hours, r)
        supplies = labor * r["supplies_pct"] / 100
        extras = 0.0
        lines = [f"{visits} visits per week" + (f" ({full} full cleans, {touch} touch-ups)" if touch else ", all full cleans")]
        supply_program = 0.0
        if tier == "best":
            extras += floor_care
            supply_program = consumables(occupants, "hardwound")
            extras += supply_program
            lines += ["Quarterly floor care (hard floors scrubbed and refinished, carpets extracted)",
                      f"Restroom supplies included: {TOWEL_FORMATS['hardwound']['label'].lower()}, tissue, soap and liners"]
        direct = labor + supplies + extras
        cost = direct * (1 + r["overhead_pct"] / 100)
        price = price_for(cost, r["margin_pct"])
        # What the prospect spends today for the same coverage: the vendor, plus
        # restroom supplies when Best takes those over.
        compare_to = current + (current_supplies if tier == "best" else 0)
        savings = round(compare_to - price) if current else None
        options.append({
            "tier": tier,
            **TIER_INFO[tier],
            "monthly": price,
            "annual": price * 12,
            "per_sqft": round(price / sqft, 3),
            "includes": lines,
            "full_cleans": full,
            "touchups": touch,
            "labor_hours": round(hours, 1),
            # Internal only: never shown on the customer's proposal.
            "cost": round(cost, 2),
            "margin_pct": round((price - cost) / price * 100, 1) if price else 0,
            "compare_to": round(compare_to, 2) if current else None,
            "savings_monthly": savings,
            "savings_annual": round(savings * 12, 2) if savings is not None else None,
            "supply_program": round(supply_program, 2),
        })

    deep_clean = max(r["deep_clean_min"], round_up(sqft * r["deep_clean_per_sqft"]))
    supplies_compare = {
        "occupants": occupants,
        "formats": [{"format": k, **v, "monthly": round(consumables(occupants, k), 2)} for k, v in TOWEL_FORMATS.items()],
    }
    recommended = "better" if visits > 2 else "good"
    return {
        "facility_type": ftype,
        "facility_label": FACILITY_TYPES[ftype]["label"],
        "sqft": sqft,
        "visits_per_week": visits,
        "current_monthly": current or None,
        "current_supplies_monthly": round(current_supplies, 2),
        "options": options,
        "recommended": recommended,
        "deep_clean": deep_clean,
        "supplies_compare": supplies_compare,
        "rates": r,
    }


def needs_review(q: Dict[str, Any], rates: Optional[Dict[str, float]] = None) -> bool:
    r = {**DEFAULT_RATES, **(rates or {})}
    rec = next(o for o in q["options"] if o["tier"] == q["recommended"])
    return rec["monthly"] >= r["auto_send_under"] or rec["margin_pct"] < r["margin_pct"] - 0.5
