"""The prospect list builder, against canned Google and Apollo responses."""
import csv
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "prospecting"))

import build_list as bl  # noqa: E402


def place(pid, name, lat, lng, site, reviews=12, status="OPERATIONAL"):
    return {"id": pid, "displayName": {"text": name}, "formattedAddress": f"1 Main St, Bensalem, PA 19020, USA",
            "location": {"latitude": lat, "longitude": lng}, "websiteUri": site, "nationalPhoneNumber": "(215) 555-0100",
            "userRatingCount": reviews, "businessStatus": status}


def fake_http(calls):
    def http(method, url, headers, body):
        calls.append(url)
        if "places:searchText" in url:
            return {"places": [
                place("a", "Acme Janitorial LLC", 40.10, -74.95, "https://www.acmejan.example/"),
                place("b", "Tiny Maids", 40.00, -75.10, "https://tinymaids.example"),
                place("c", "Shore Clean", 39.36, -74.43, "https://shore.example"),        # Atlantic City: too far
                place("d", "Gone Cleaning", 40.0, -75.2, "https://gone.example", status="CLOSED_PERMANENTLY"),
                place("e", "Mystery Clean", 39.95, -75.16, "https://mystery.example"),
            ]}
        if "mixed_companies/search" in url:
            assert body["organization_num_employees_ranges"] == ["10,50"]
            return {"organizations": [
                {"name": "Acme Janitorial", "website_url": "http://acmejan.example", "estimated_num_employees": 18,
                 "city": "Bensalem", "state": "Pennsylvania"},
                {"name": "Pittsburgh Clean", "website_url": "http://pgh.example", "estimated_num_employees": 30,
                 "city": "Pittsburgh", "state": "Pennsylvania"},
                {"name": "Ten Person Maids", "website_url": "http://tenmaids.example", "estimated_num_employees": 10,
                 "city": "Cherry Hill", "state": "New Jersey"},
            ], "pagination": {"total_pages": 1}}
        if "organizations/enrich" in url:
            return {"organization": {"estimated_num_employees": {"tinymaids.example": 4}.get(url.split("=")[-1])}}
        if "mixed_people/search" in url:
            return {"people": [{"name": "Lee Office", "title": "Office Manager"},
                               {"name": "Jo Owner", "title": "Owner", "email": "jo@acmejan.example", "email_status": "verified"}]}
        raise AssertionError(url)
    return http


def test_pipeline(monkeypatch, tmp_path):
    calls = []
    http = fake_http(calls)
    rows = bl.google_discover("g", http=http, pages=1, log=lambda *_: None) + bl.apollo_discover("a", http=http, log=lambda *_: None)
    rows = bl.merge(rows)
    names = sorted(r["company"] for r in rows)
    assert len([n for n in names if "Acme" in n]) == 1            # Google + Apollo merged by domain
    acme = next(r for r in rows if "Acme" in r["company"])
    assert "Google Places" in acme["source"] and "Apollo" in acme["source"] and acme["employees"] == 18
    for r in rows:
        bl.apollo_enrich(r, "a", http=http)
        if r.get("employees") and 10 <= int(r["employees"]) <= 50:
            bl.apollo_contact(r, "a", http=http)
    q, unknown = bl.qualify(rows)
    qn = [r["company"] for r in q]
    assert set(qn) == {"Acme Janitorial LLC", "Ten Person Maids"}   # residential is fine at 10+
    assert "Tiny Maids" not in qn and "Pittsburgh Clean" not in qn and "Shore Clean" not in qn
    assert "Gone Cleaning" not in qn + [r["company"] for r in unknown]
    assert [r["company"] for r in unknown] == ["Mystery Clean"]
    assert acme["contact_name"] == "Jo Owner" and acme["email_status"] == "verified"
    out = tmp_path / "q.csv"
    bl.write_csv(str(out), q)
    by = {r["company"]: r for r in csv.DictReader(open(out))}
    a = by["Acme Janitorial LLC"]
    assert a["website"] == "acmejan.example" and a["employees"] == "18" and a["state"] == "PA"
    assert by["Ten Person Maids"]["state"] == "NJ"


def test_crawl_tags_segment_and_team_size():
    r = {"website": "https://x.example"}
    bl.crawl(r, fetch=lambda u: "<p>Residential and commercial cleaning. Our team of 22 cleaning pros.</p>", delay=0)
    assert r["segment"] == "commercial + residential" and r["employees"] == 22 and r["headcount_source"] == "company website"


def test_dry_run_makes_no_calls(capsys, monkeypatch):
    monkeypatch.delenv("GOOGLE_MAPS_API_KEY", raising=False)
    assert bl.main(["--dry-run"]) == 0
    assert "Text Search calls" in capsys.readouterr().out


def geo_http(calls):
    def http(method, url, headers, body):
        calls.append((method, url, headers, body))
        assert headers == {"x-api-key": "k"}          # the key goes in a header, never the URL
        if url.endswith("/v2/places"):
            assert body["filter"]["type"] == "circle" and body["limit"] == 100
            feats = [
                {"properties": {"name": "Acme Janitorial LLC", "place_id": "p1", "city": "Bensalem", "state_code": "PA",
                                "street": "Main St", "housenumber": "1", "postcode": "19020", "lat": 40.10, "lon": -74.95,
                                "categories": ["office.company"]}},
                {"properties": {"name": "Suds Dry Cleaners", "place_id": "p2", "lat": 40.0, "lon": -75.1,
                                "categories": ["service.cleaning.dry_cleaning"]}},
                {"properties": {"name": "Shore Maids", "place_id": "p3", "city": "Atlantic City", "state_code": "NJ",
                                "lat": 39.36, "lon": -74.43, "categories": ["service"]}},
            ]
            return {"features": feats}
        assert "place-details" in url
        return {"features": [{"properties": {"website": "https://acmejan.example",
                                              "contact": {"phone": "+1 215 555 0100", "email": "hello@acmejan.example"}}}]}
    return http


def test_geoapify_discovery_filters_and_details():
    calls = []
    http = geo_http(calls)
    rows = bl.geoapify_discover("k", http=http, delay=0, log=lambda *_: None)
    assert len(calls) == len(bl.TERRITORIES) * len(bl.GEO_NAME_TERMS)
    assert sorted(r["company"] for r in rows) == ["Acme Janitorial LLC", "Shore Maids"]   # dry cleaner dropped
    in_range = [r for r in rows if bl.in_territory(r)]
    assert [r["company"] for r in in_range] == ["Acme Janitorial LLC"]                   # Atlantic City too far
    r = in_range[0]
    bl.geoapify_details(r, "k", http=http, delay=0)
    assert r["website"] == "https://acmejan.example" and r["phone"] == "+1 215 555 0100"
    assert r["address"] == "1 Main St" and r["state"] == "PA" and r["source"].startswith("Geoapify")


def test_geoapify_row_merges_with_apollo_row_by_domain():
    g = {"company": "Acme Janitorial LLC", "website": "https://www.acmejan.example/", "city": "Bensalem",
         "state": "PA", "lat": 40.1, "lng": -74.95, "source": "Geoapify (OpenStreetMap)", "status": "OPERATIONAL"}
    a = {"company": "Acme Janitorial", "website": "http://acmejan.example", "employees": 18, "source": "Apollo search"}
    merged = bl.merge([g, a])
    assert len(merged) == 1 and merged[0]["employees"] == 18 and "Apollo" in merged[0]["source"]


def test_dry_run_geoapify(capsys, monkeypatch):
    monkeypatch.delenv("GEOAPIFY_API_KEY", raising=False)
    assert bl.main(["--dry-run", "--provider", "geoapify"]) == 0
    out = capsys.readouterr().out
    assert "Geoapify: 80 searches" in out and "Google:" not in out
