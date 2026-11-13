"""Build a list of cleaning companies near Philadelphia with 10-50 employees,
to sell VectorSales to.

    Discover   Google Places Text Search around each sub-territory, plus Apollo's
               organization search (which can filter by headcount itself)
    Screen     Within MAX_MILES of Philadelphia City Hall, still operating, has a
               website. Optionally reads the homepage (robots.txt respected) to
               tag commercial / residential and pick up "team of 25"-type clues
    Enrich     Apollo organization enrich by domain gives the headcount; Apollo
               people search gives the owner / operations decision-maker
    Output     prospects_qualified.csv (10-50 employees, best first, up to 200),
               prospects_needs_check.csv (headcount unknown, check by hand)

Residential cleaners are kept: the only size rule is 10-50 employees.
The CSVs use the Cost Comparison Tool's prospect import columns.

    export GOOGLE_MAPS_API_KEY=...  APOLLO_API_KEY=...  GEOAPIFY_API_KEY=...
    python prospecting/build_list.py --crawl
    python prospecting/build_list.py --provider geoapify    # OpenStreetMap data, free plan, no card
    python prospecting/build_list.py --provider both        # union of the two
    python prospecting/build_list.py --skip-google          # Apollo only
    python prospecting/build_list.py --dry-run              # show the plan, no calls

Stdlib only. Each source is optional: without a key, that step is skipped.
"""
import argparse
import csv
import json
import math
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import urllib.robotparser
from typing import Any, Callable, Dict, Iterable, List, Optional, Tuple

PHILLY = (39.9526, -75.1652)  # City Hall
MAX_MILES = 45.0              # about 45-60 minutes' drive
MIN_EMPLOYEES, MAX_EMPLOYEES = 10, 50
TARGET = 200
USER_AGENT = "VectorSales-prospecting/1.0"

# Sub-territory centers for Google searches (lat, lng). Each search is biased to a
# 15 km circle, so together they cover Philly, the PA collar counties and South Jersey.
TERRITORIES: Dict[str, Tuple[float, float]] = {
    "Philadelphia, PA": (39.9526, -75.1652),
    "Northeast Philadelphia, PA": (40.0810, -75.0270),
    "King of Prussia, PA": (40.0893, -75.3963),
    "Conshohocken, PA": (40.0793, -75.3016),
    "Norristown, PA": (40.1215, -75.3399),
    "Horsham, PA": (40.1784, -75.1285),
    "Doylestown, PA": (40.3101, -75.1299),
    "Bensalem, PA": (40.1046, -74.9513),
    "West Chester, PA": (39.9607, -75.6055),
    "Exton, PA": (40.0290, -75.6207),
    "Media, PA": (39.9168, -75.3877),
    "Cherry Hill, NJ": (39.9348, -75.0307),
    "Mount Laurel, NJ": (39.9340, -74.8910),
    "Marlton, NJ": (39.8912, -74.9218),
    "Deptford, NJ": (39.8384, -75.1188),
    "Wilmington, DE": (39.7391, -75.5398),
}
# Other towns inside the radius, for rows that have a city but no coordinates.
METRO_TOWNS = {t.lower() for t in (
    "Abington", "Ambler", "Ardmore", "Bala Cynwyd", "Blue Bell", "Bristol", "Broomall", "Bryn Mawr",
    "Chester", "Chesterbrook", "Collegeville", "Downingtown", "Drexel Hill", "Feasterville", "Fort Washington",
    "Glenside", "Hatboro", "Hatfield", "Havertown", "Huntingdon Valley", "Jenkintown", "Kennett Square",
    "Lansdale", "Langhorne", "Levittown", "Malvern", "Newtown", "Newtown Square", "Oaks", "Paoli",
    "Phoenixville", "Plymouth Meeting", "Pottstown", "Quakertown", "Sellersville", "Southampton", "Springfield",
    "Trevose", "Upper Darby", "Warminster", "Warrington", "Wayne", "Willow Grove", "Yardley", "Audubon",
    "Blackwood", "Burlington", "Camden", "Collingswood", "Glassboro", "Haddonfield", "Lumberton", "Maple Shade",
    "Medford", "Moorestown", "Mount Holly", "Pennsauken", "Sewell", "Sicklerville", "Voorhees", "Westmont",
    "Williamstown", "Willingboro", "Woodbury", "New Castle", "Newark", "Claymont", "Trenton", "Hamilton",
)}
QUERIES = ("commercial cleaning company", "janitorial service", "cleaning service")
COUNTIES_NOTE = "Philadelphia, Montgomery, Bucks, Chester, Delaware; Camden, Burlington, Gloucester"

COLUMNS = ["company", "address", "city", "county", "state", "zip", "industry", "employees", "website",
           "email", "phone", "contact_name", "contact_title", "email_status", "source",
           # extras (the importer ignores columns it doesn't know)
           "miles_from_philly", "segment", "google_rating", "google_reviews", "linkedin_url",
           "headcount_source", "size_clues", "fit_score"]

DECISION_TITLES = ["owner", "president", "ceo", "founder", "general manager",
                   "director of operations", "operations manager", "vice president of operations"]

Http = Callable[[str, str, Dict[str, str], Optional[dict]], Any]


# --- plumbing ------------------------------------------------------------------

def http_json(method: str, url: str, headers: Dict[str, str], body: Optional[dict] = None) -> Any:
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method,
                                 headers={"Content-Type": "application/json", "User-Agent": USER_AGENT, **headers})
    for attempt in range(4):
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:  # noqa: S310 - fixed API hosts
                return json.loads(resp.read() or b"{}")
        except urllib.error.HTTPError as exc:
            if exc.code == 429 or exc.code >= 500:
                time.sleep(2 ** (attempt + 1))
                continue
            detail = exc.read()[:300].decode(errors="replace")
            raise RuntimeError(f"{method} {url.split('?')[0]} -> HTTP {exc.code}: {detail}") from exc
    raise RuntimeError(f"{method} {url.split('?')[0]} kept failing (rate limited?)")


def miles(a: Tuple[float, float], b: Tuple[float, float]) -> float:
    lat1, lon1, lat2, lon2 = map(math.radians, (*a, *b))
    h = math.sin((lat2 - lat1) / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin((lon2 - lon1) / 2) ** 2
    return 3958.8 * 2 * math.asin(math.sqrt(h))


def domain_of(url: Optional[str]) -> str:
    if not url:
        return ""
    host = urllib.parse.urlparse(url if "//" in url else "http://" + url).netloc.lower()
    return host[4:] if host.startswith("www.") else host


def norm_name(name: str) -> str:
    n = re.sub(r"[^a-z0-9 ]", " ", (name or "").lower())
    n = re.sub(r"\b(llc|inc|co|corp|corporation|company|the|ltd|l l c)\b", " ", n)
    return re.sub(r"\s+", " ", n).strip()


def parse_address(addr: str) -> Dict[str, str]:
    """'123 Main St, Bensalem, PA 19020, USA' -> street, city, state, zip."""
    parts = [p.strip() for p in (addr or "").split(",")]
    if parts and parts[-1] in ("USA", "United States"):
        parts = parts[:-1]
    out = {"address": "", "city": "", "state": "", "zip": ""}
    if len(parts) >= 2:
        m = re.match(r"([A-Z]{2})\s*(\d{5})?", parts[-1])
        if m:
            out["state"], out["zip"] = m[1], m[2] or ""
            out["city"] = parts[-2]
            out["address"] = ", ".join(parts[:-2])
    return out


# --- discover: Google Places ---------------------------------------------------

PLACES_FIELDS = ",".join("places." + f for f in (
    "id", "displayName", "formattedAddress", "location", "websiteUri", "nationalPhoneNumber",
    "rating", "userRatingCount", "businessStatus", "types")) + ",nextPageToken"


def google_discover(key: str, http: Http = http_json, pages: int = 2, log=print) -> List[Dict[str, Any]]:
    found: Dict[str, Dict[str, Any]] = {}
    calls = 0
    for town, center in TERRITORIES.items():
        for query in QUERIES:
            token = None
            for _ in range(pages):
                body: Dict[str, Any] = {
                    "textQuery": f"{query} near {town}", "pageSize": 20,
                    "locationBias": {"circle": {"center": {"latitude": center[0], "longitude": center[1]},
                                                "radius": 15000.0}},
                }
                if token:
                    body["pageToken"] = token
                data = http("POST", "https://places.googleapis.com/v1/places:searchText",
                            {"X-Goog-Api-Key": key, "X-Goog-FieldMask": PLACES_FIELDS}, body)
                calls += 1
                for p in data.get("places", []):
                    found.setdefault(p["id"], p)
                token = data.get("nextPageToken")
                if not token:
                    break
    log(f"Google Places: {calls} searches, {len(found)} unique places")
    rows = []
    for p in found.values():
        loc = p.get("location") or {}
        rows.append({
            "company": (p.get("displayName") or {}).get("text", ""),
            **parse_address(p.get("formattedAddress", "")),
            "website": p.get("websiteUri", ""), "phone": p.get("nationalPhoneNumber", ""),
            "lat": loc.get("latitude"), "lng": loc.get("longitude"),
            "google_rating": p.get("rating", ""), "google_reviews": p.get("userRatingCount", 0),
            "status": p.get("businessStatus", "OPERATIONAL"), "source": "Google Places",
        })
    return rows


# --- discover: Geoapify (OpenStreetMap data) -----------------------------------
# Geoapify has no janitorial category (only dry cleaning / laundry), so we search the
# broad "service" and "office" categories by company name instead. Coverage of small
# cleaning firms in OpenStreetMap is patchy: expect fewer hits than Google, and
# treat Apollo as the source of truth for headcount.

GEOAPIFY = "https://api.geoapify.com/v2"
GEO_NAME_TERMS = ("clean", "janitorial", "maid", "custodial", "building services")
GEO_SKIP_CATEGORIES = ("service.cleaning.dry_cleaning", "service.cleaning.laundry", "service.cleaning.lavoir")
GEO_DELAY = 0.25  # the free plan allows 5 requests per second


def _geo_row(p: Dict[str, Any]) -> Dict[str, Any]:
    state = p.get("state_code") or _state_abbr(p.get("state") or "")
    return {
        "company": p.get("name", ""), "address": " ".join(filter(None, (p.get("housenumber"), p.get("street")))),
        "city": p.get("city") or p.get("town") or p.get("village") or "", "state": state,
        "zip": p.get("postcode") or "", "lat": p.get("lat"), "lng": p.get("lon"),
        "geoapify_id": p.get("place_id", ""), "status": "OPERATIONAL", "source": "Geoapify (OpenStreetMap)",
    }


def geoapify_discover(key: str, http: Http = http_json, delay: float = GEO_DELAY, log=print) -> List[Dict[str, Any]]:
    found: Dict[str, Dict[str, Any]] = {}
    calls = 0
    for town, (lat, lng) in TERRITORIES.items():
        for term in GEO_NAME_TERMS:
            data = http("POST", f"{GEOAPIFY}/places", {"x-api-key": key}, {
                "categories": ["service", "office"], "name": term, "limit": 100,
                "filter": {"type": "circle", "lon": lng, "lat": lat, "radius": 25000},
            })
            calls += 1
            time.sleep(delay)
            for f in data.get("features", []):
                p = f.get("properties", {})
                cats = p.get("categories") or []
                if p.get("name") and p.get("place_id") and not any(c in GEO_SKIP_CATEGORIES for c in cats):
                    found.setdefault(p["place_id"], p)
    log(f"Geoapify: {calls} searches, {len(found)} unique places")
    return [_geo_row(p) for p in found.values()]


def geoapify_details(row: Dict[str, Any], key: str, http: Http = http_json, delay: float = GEO_DELAY) -> None:
    """Website, phone and email come from the Place Details call (1 credit each), and only
    exist when someone has put them in OpenStreetMap."""
    pid = row.get("geoapify_id")
    if not pid or row.get("website"):
        return
    data = http("GET", f"{GEOAPIFY}/place-details?id={urllib.parse.quote(pid)}&features=details", {"x-api-key": key}, None)
    time.sleep(delay)
    for f in data.get("features", []):
        p = f.get("properties", {})
        row["website"] = p.get("website") or row.get("website", "")
        contact = p.get("contact") or {}
        row["phone"] = row.get("phone") or contact.get("phone", "")
        row["email"] = row.get("email") or contact.get("email", "")


# --- discover + enrich: Apollo -------------------------------------------------

APOLLO = "https://api.apollo.io/api/v1"


def apollo_discover(key: str, http: Http = http_json, max_pages: int = 5, log=print) -> List[Dict[str, Any]]:
    rows = []
    for page in range(1, max_pages + 1):
        data = http("POST", f"{APOLLO}/mixed_companies/search", {"X-Api-Key": key}, {
            "q_organization_keyword_tags": ["commercial cleaning", "janitorial", "cleaning services",
                                            "facility services", "house cleaning", "maid service"],
            "organization_locations": ["Philadelphia, Pennsylvania, United States"],
            # Apollo takes custom "min,max" ranges; 10 is the floor we want.
            "organization_num_employees_ranges": [f"{MIN_EMPLOYEES},{MAX_EMPLOYEES}"],
            "page": page, "per_page": 100,
        })
        orgs = data.get("organizations") or data.get("accounts") or []
        for o in orgs:
            rows.append(_apollo_row(o, "Apollo search"))
        total_pages = (data.get("pagination") or {}).get("total_pages", page)
        if not orgs or page >= total_pages:
            break
    log(f"Apollo search: {len(rows)} organizations")
    return rows


def _apollo_row(o: Dict[str, Any], source: str) -> Dict[str, Any]:
    return {
        "company": o.get("name", ""), "website": o.get("website_url") or o.get("primary_domain") or "",
        "phone": (o.get("primary_phone") or {}).get("sanitized_number") or o.get("sanitized_phone") or "",
        "city": o.get("city") or "", "state": _state_abbr(o.get("state") or ""), "zip": o.get("postal_code") or "",
        "address": o.get("street_address") or "", "employees": o.get("estimated_num_employees") or "",
        "linkedin_url": o.get("linkedin_url") or "", "industry": o.get("industry") or "",
        "headcount_source": "Apollo" if o.get("estimated_num_employees") else "", "source": source,
    }


_STATES = {"pennsylvania": "PA", "new jersey": "NJ", "delaware": "DE", "maryland": "MD", "new york": "NY"}


def _state_abbr(s: str) -> str:
    return _STATES.get(s.lower(), s.upper()[:2] if len(s) == 2 else s)


def apollo_enrich(row: Dict[str, Any], key: str, http: Http = http_json) -> None:
    """Headcount (and gaps) from Apollo's organization record for this domain."""
    domain = domain_of(row.get("website"))
    if not domain:
        return
    data = http("GET", f"{APOLLO}/organizations/enrich?domain={urllib.parse.quote(domain)}", {"X-Api-Key": key}, None)
    org = data.get("organization") or {}
    if not org:
        return
    a = _apollo_row(org, "")
    for k in ("employees", "linkedin_url", "industry", "zip", "address"):
        if a.get(k) and not row.get(k):
            row[k] = a[k]
    if a.get("employees"):
        row["headcount_source"] = "Apollo"


def apollo_contact(row: Dict[str, Any], key: str, http: Http = http_json) -> None:
    """The most senior owner/operations person Apollo lists at this domain.
    Search shows name and title; emails it marks locked need a credit to reveal."""
    domain = domain_of(row.get("website"))
    if not domain or row.get("contact_name"):
        return
    data = http("POST", f"{APOLLO}/mixed_people/search", {"X-Api-Key": key}, {
        "q_organization_domains_list": [domain], "person_titles": DECISION_TITLES, "page": 1, "per_page": 5})
    people = data.get("people") or data.get("contacts") or []
    if not people:
        return

    def rank(p: Dict[str, Any]) -> int:
        t = (p.get("title") or "").lower()
        return next((i for i, d in enumerate(DECISION_TITLES) if d in t), len(DECISION_TITLES))

    p = min(people, key=rank)
    row["contact_name"] = p.get("name") or " ".join(filter(None, (p.get("first_name"), p.get("last_name"))))
    row["contact_title"] = p.get("title") or ""
    email = p.get("email") or ""
    if email and "email_not_unlocked" not in email:
        row["email"] = email
        row["email_status"] = p.get("email_status") or "unverified"


# --- screen: the company's own website -----------------------------------------

COMMERCIAL = re.compile(r"\b(commercial|janitorial|office cleaning|day porter|facility|facilities|medical "
                        r"office|industrial|post[- ]construction|floor care|building maintenance)\b", re.I)
RESIDENTIAL = re.compile(r"\b(residential|house cleaning|home cleaning|maid|move[- ]in|move[- ]out|homeowners?)\b", re.I)
TEAM_SIZE = re.compile(r"\b(?:team|staff|crew|family) of (\d{2,3})\b|\b(\d{2,3})\+? (?:employees|staff members|"
                       r"cleaning professionals|team members|cleaners)\b", re.I)


def crawl(row: Dict[str, Any], fetch: Optional[Callable[[str], str]] = None, delay: float = 1.0) -> None:
    """Tag commercial/residential and pick up stated team sizes from the homepage.
    Respects robots.txt and waits `delay` seconds between sites."""
    url = row.get("website")
    if not url:
        return
    url = url if "//" in url else "https://" + url
    if fetch is None:
        robots = urllib.robotparser.RobotFileParser(urllib.parse.urljoin(url, "/robots.txt"))
        try:
            robots.read()
            if not robots.can_fetch(USER_AGENT, url):
                row["size_clues"] = "robots.txt disallows"
                return
        except (OSError, ValueError):
            pass

        def fetch(u: str) -> str:
            req = urllib.request.Request(u, headers={"User-Agent": USER_AGENT})
            with urllib.request.urlopen(req, timeout=10) as resp:  # noqa: S310 - the company's own site
                return resp.read(400_000).decode("utf-8", errors="replace")
    try:
        html = fetch(url)
    except (OSError, ValueError):
        return
    finally:
        time.sleep(delay)
    text = re.sub(r"<[^>]+>", " ", re.sub(r"(?is)<(script|style)[^>]*>.*?</\1>", " ", html))
    com, res = bool(COMMERCIAL.search(text)), bool(RESIDENTIAL.search(text))
    row["segment"] = "commercial + residential" if com and res else "commercial" if com else "residential" if res else ""
    sizes = [int(a or b) for a, b in TEAM_SIZE.findall(text)]
    if sizes:
        row["size_clues"] = f"site says team of {max(sizes)}"
        if not row.get("employees"):
            row["employees"] = max(sizes)
            row["headcount_source"] = "company website"


# --- merge, filter, score ------------------------------------------------------

def merge(rows: Iterable[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """One row per company: matched by website domain, else by normalized name + city."""
    out: Dict[str, Dict[str, Any]] = {}
    for r in rows:
        k = domain_of(r.get("website")) or f"{norm_name(r.get('company', ''))}|{(r.get('city') or '').lower()}"
        if k in out:
            cur = out[k]
            for f, v in r.items():
                if v not in (None, "", 0) and cur.get(f) in (None, "", 0):
                    cur[f] = v
            if r.get("source") and r["source"] not in cur.get("source", ""):
                cur["source"] += " + " + r["source"]
        else:
            out[k] = dict(r)
    return list(out.values())


def in_territory(r: Dict[str, Any]) -> bool:
    if r.get("lat") is not None and r.get("lng") is not None:
        r["miles_from_philly"] = round(miles(PHILLY, (r["lat"], r["lng"])), 1)
        return r["miles_from_philly"] <= MAX_MILES
    # Apollo rows have no coordinates: use the city when it's a known sub-territory.
    for town, center in TERRITORIES.items():
        if (r.get("city") or "").lower() == town.split(",")[0].lower():
            r["miles_from_philly"] = round(miles(PHILLY, center), 1)
            return r["miles_from_philly"] <= MAX_MILES
    # Inside the radius but not a search center: kept, distance unknown.
    return (r.get("city") or "").lower() in METRO_TOWNS


def fit_score(r: Dict[str, Any]) -> int:
    """0-100: confirmed headcount, a reachable decision-maker, a real web presence, proximity."""
    s = 0
    if r.get("headcount_source") == "Apollo":
        s += 30
    elif r.get("headcount_source"):
        s += 20
    if r.get("contact_name"):
        s += 20
    if r.get("email"):
        s += 15
    if r.get("website"):
        s += 10
    s += min(10, int(r.get("google_reviews") or 0) // 5)
    if r.get("miles_from_philly") not in (None, ""):
        s += max(0, 15 - int(float(r["miles_from_philly"]) // 3))
    return min(100, s)


def qualify(rows: List[Dict[str, Any]]) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    qualified, unknown = [], []
    for r in rows:
        if r.get("status") not in (None, "", "OPERATIONAL") or not in_territory(r):
            continue
        if not r.get("industry"):
            r["industry"] = "Cleaning / janitorial services"
        try:
            n = int(float(r.get("employees") or ""))
        except ValueError:
            unknown.append(r)
            continue
        if MIN_EMPLOYEES <= n <= MAX_EMPLOYEES:
            r["employees"] = n
            qualified.append(r)
    for r in qualified + unknown:
        r["fit_score"] = fit_score(r)
    key = lambda r: (-r["fit_score"], r.get("miles_from_philly") or 99)  # noqa: E731
    return sorted(qualified, key=key)[:TARGET], sorted(unknown, key=key)


def write_csv(path: str, rows: List[Dict[str, Any]]) -> None:
    with open(path, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=COLUMNS, extrasaction="ignore")
        w.writeheader()
        for r in rows:
            w.writerow({**r, "website": domain_of(r.get("website")) or r.get("website", "")})


# --- run -----------------------------------------------------------------------

def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--out", default=".", help="folder for the CSVs")
    ap.add_argument("--crawl", action="store_true", help="read each homepage for segment and team-size clues")
    ap.add_argument("--provider", choices=("google", "geoapify", "both"), default="google",
                    help="where to find places (default google)")
    ap.add_argument("--skip-google", action="store_true")
    ap.add_argument("--skip-apollo", action="store_true")
    ap.add_argument("--google-pages", type=int, default=2, help="result pages per Google search (20 each)")
    ap.add_argument("--max-enrich", type=int, default=400, help="cap on Apollo enrich calls (credits)")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args(argv)

    gkey, akey = os.environ.get("GOOGLE_MAPS_API_KEY"), os.environ.get("APOLLO_API_KEY")
    okey = os.environ.get("GEOAPIFY_API_KEY")
    use_google = args.provider in ("google", "both") and not args.skip_google
    use_geo = args.provider in ("geoapify", "both")
    searches = len(TERRITORIES) * len(QUERIES)
    print(f"Territory: {len(TERRITORIES)} centers within {MAX_MILES:.0f} mi of Philadelphia ({COUNTIES_NOTE}).")
    if use_google:
        print(f"Google: up to {searches * args.google_pages} Text Search calls"
              f"{'' if gkey else ' (skipped: no GOOGLE_MAPS_API_KEY)'}.")
    if use_geo:
        print(f"Geoapify: {len(TERRITORIES) * len(GEO_NAME_TERMS)} searches + 1 details call per company in range "
              f"(1 credit each; free plan is 3,000/day){'' if okey else ' (skipped: no GEOAPIFY_API_KEY)'}.")
    print(f"Apollo: search + up to {args.max_enrich} enrich/people calls"
          f"{'' if akey and not args.skip_apollo else ' (skipped: no APOLLO_API_KEY)' if not args.skip_apollo else ' (skipped)'}.")
    if args.dry_run:
        return 0

    rows: List[Dict[str, Any]] = []
    if use_google and gkey:
        rows += google_discover(gkey, pages=args.google_pages)
    geo_rows: List[Dict[str, Any]] = []
    if use_geo and okey:
        try:
            geo_rows = [r for r in geoapify_discover(okey) if in_territory(r)]
            for i, r in enumerate(geo_rows, 1):
                geoapify_details(r, okey)
                if i % 50 == 0:
                    print(f"  details {i}/{len(geo_rows)}")
        except RuntimeError as exc:
            print(f"Geoapify stopped ({exc}). Continuing with what was found.", file=sys.stderr)
        rows += geo_rows
    if akey and not args.skip_apollo:
        try:
            rows += apollo_discover(akey)
        except RuntimeError as exc:
            print(f"Apollo search failed ({exc}). Continuing with Google results.", file=sys.stderr)
    if not rows:
        print("Nothing to do: set GOOGLE_MAPS_API_KEY, GEOAPIFY_API_KEY and/or APOLLO_API_KEY.", file=sys.stderr)
        return 1
    rows = [r for r in merge(rows) if in_territory(r) and r.get("status") in (None, "", "OPERATIONAL")]
    print(f"{len(rows)} companies in territory after de-duplication.")

    if args.crawl:
        for i, r in enumerate(rows, 1):
            crawl(r)
            if i % 25 == 0:
                print(f"  read {i}/{len(rows)} websites")
    if akey and not args.skip_apollo:
        used = 0
        for r in sorted(rows, key=lambda r: r.get("miles_from_philly") or 99):
            if used >= args.max_enrich:
                break
            try:
                if r.get("headcount_source") != "Apollo":
                    apollo_enrich(r, akey)
                    used += 1
                n = int(float(r.get("employees") or 0))
                if MIN_EMPLOYEES <= n <= MAX_EMPLOYEES:
                    apollo_contact(r, akey)
                    used += 1
            except RuntimeError as exc:
                print(f"  Apollo stopped: {exc}", file=sys.stderr)
                break

    qualified, unknown = qualify(rows)
    os.makedirs(args.out, exist_ok=True)
    write_csv(os.path.join(args.out, "prospects_qualified.csv"), qualified)
    write_csv(os.path.join(args.out, "prospects_needs_check.csv"), unknown)
    print(f"Qualified (10-50 employees): {len(qualified)} -> prospects_qualified.csv")
    print(f"Headcount unknown: {len(unknown)} -> prospects_needs_check.csv")
    if len(qualified) < TARGET:
        print(f"Short of {TARGET}: run with --crawl, raise --google-pages, or check the needs_check list.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
