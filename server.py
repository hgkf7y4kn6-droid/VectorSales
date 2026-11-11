"""VectorSales: the web app and API in one FastAPI service.

Run the demo locally:   DEMO_MODE=1 uvicorn server:app --reload
Then open http://localhost:8000 and press "Run the 5-minute demo".

Sign-in uses the same AUTH_USERS / AUTH_SECRET setup as the Cost Comparison
Tool (see auth.py). With no users configured the API refuses requests, unless
DEMO_MODE=1, which opens it for a sample-data demo.
"""
import asyncio
import contextlib
import os
import time
from typing import Any, Dict, List

from fastapi import Body, FastAPI, HTTPException, Request
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

import auth
import pricing
import sample
import workflow
from store import Store

STATIC = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")
DEMO_MODE = os.environ.get("DEMO_MODE") == "1"
store = Store()
authenticator = auth.from_environment()

PUBLIC_PREFIXES = ("/api/public/", "/api/inbound", "/auth/login", "/health", "/api/config")


@contextlib.asynccontextmanager
async def lifespan(_app: FastAPI):
    if DEMO_MODE and not store.list("leads"):
        sample.reset(store, os.environ.get("PUBLIC_URL", "http://localhost:8000"))

    async def tick() -> None:
        while True:
            await asyncio.sleep(60)
            with contextlib.suppress(Exception):
                workflow.run_due(store)

    task = asyncio.create_task(tick())
    yield
    task.cancel()


app = FastAPI(title="VectorSales", lifespan=lifespan)
app.add_middleware(GZipMiddleware, minimum_size=1024)


def base_url(request: Request) -> str:
    return os.environ.get("PUBLIC_URL") or str(request.base_url).rstrip("/")


@app.middleware("http")
async def guard(request: Request, call_next):
    path = request.url.path
    if path.startswith("/api/") and not path.startswith(PUBLIC_PREFIXES):
        if authenticator.configured:
            header = request.headers.get("authorization", "")
            user = authenticator.verify_token(header[7:].strip()) if header.lower().startswith("bearer ") else None
            if not user:
                return JSONResponse({"detail": "Please sign in."}, status_code=401)
        elif not DEMO_MODE:
            return JSONResponse({"detail": "Sign-in isn't set up (AUTH_USERS is empty). "
                                           "Set DEMO_MODE=1 to run the sample-data demo."}, status_code=503)
    response = await call_next(request)
    h = response.headers
    h.setdefault("X-Content-Type-Options", "nosniff")
    h.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
    # Same-origin framing only: the demo shows the customer's proposal beside the rep's view.
    h.setdefault("X-Frame-Options", "SAMEORIGIN")
    h.setdefault("Content-Security-Policy", "frame-ancestors 'self'")
    if not path.startswith("/api/"):
        h.setdefault("Cache-Control", "no-cache")
    return response


def _fail(exc: ValueError):
    raise HTTPException(status_code=400, detail=str(exc))


# --- sign-in and config --------------------------------------------------------

@app.get("/health")
def health():
    return {"status": "ok"}


@app.get("/api/config")
def config():
    return {"demo": DEMO_MODE and not authenticator.configured, "auth": authenticator.configured,
            "ai": bool(os.environ.get("ANTHROPIC_API_KEY")), "email": workflow._smtp_configured()}


@app.post("/auth/login")
def login(request: Request, body: Dict[str, str] = Body(...)):
    ip = request.client.host if request.client else "?"
    if authenticator.locked_out(body.get("email", ""), ip):
        raise HTTPException(429, "Too many attempts. Try again in 15 minutes.")
    email = authenticator.login(body.get("email", ""), body.get("password", ""), ip)
    if not email:
        raise HTTPException(401, "That email and password don't match.")
    token, expires = authenticator.issue_token(email)
    return {"token": token, "expires": expires, "email": email}


# --- the rep's workspace -------------------------------------------------------

def _lead_summary(lead: Dict[str, Any]) -> Dict[str, Any]:
    quotes = store.list("quotes", lead["id"])
    q = quotes[-1] if quotes else None
    opt = next((o for o in q["options"] if o["tier"] == q["selected"]), None) if q else None
    pending = [m for m in store.list("messages", lead["id"]) if m["status"] == "scheduled"]
    return {**lead, "quote": q and {"id": q["id"], "number": q["number"], "status": q["status"],
                                    "monthly": opt and opt["monthly"], "tier": q["selected"],
                                    "viewed": bool(q.get("viewed_at"))},
            "next_follow_up": min((m["scheduled_for"] for m in pending), default=None)}


@app.get("/api/state")
def state():
    workflow.run_due(store)
    leads = sorted(store.list("leads"), key=lambda lead: lead.get("updated_at") or lead["created_at"], reverse=True)
    return {
        "now": workflow.iso(workflow.now(store)),
        "clock_offset_days": round(store.setting("clock_offset", 0) / 86400, 2),
        "leads": [_lead_summary(lead) for lead in leads],
        "metrics": workflow.metrics(store),
        "profile": workflow.profile(store),
        "rates": workflow.rates(store),
        "facility_types": {k: v["label"] for k, v in pricing.FACILITY_TYPES.items()},
    }


@app.get("/api/leads/{lead_id}")
def lead_detail(lead_id: str):
    lead = store.get("leads", lead_id)
    if not lead:
        raise HTTPException(404, "No such lead.")
    return {"lead": _lead_summary(lead), "quotes": store.list("quotes", lead_id),
            "messages": store.list("messages", lead_id), "events": store.list("events", lead_id),
            "invoices": store.list("invoices", lead_id)}


@app.post("/api/leads")
def add_lead(request: Request, body: Dict[str, Any] = Body(...)):
    try:
        return workflow.intake(store, {**body, "source": body.get("source") or "Entered by rep"}, base_url(request))
    except ValueError as exc:
        _fail(exc)


@app.post("/api/leads/{lead_id}/requote")
def requote(lead_id: str, body: Dict[str, Any] = Body(default={})):
    lead = store.get("leads", lead_id)
    if not lead:
        raise HTTPException(404, "No such lead.")
    for k in ("facility_type", "sqft", "visits_per_week", "occupants", "current_monthly"):
        if k in body and body[k] not in (None, ""):
            lead[k] = body[k] if k == "facility_type" else float(body[k])
    store.put("leads", lead)
    if not workflow.can_price(lead):
        raise HTTPException(400, "Add the facility type, square feet and visits per week to price it.")
    for q in store.find("quotes", lead_id=lead_id, status="sent"):
        q["status"] = "superseded"
        store.put("quotes", q)
        workflow._stop_sequence(store, lead_id, "quote revised")
    q = workflow.draft_quote(store, lead)
    workflow._set_stage(store, lead, "review")
    return q


@app.post("/api/quotes/{quote_id}/send")
def send(quote_id: str, request: Request, body: Dict[str, Any] = Body(default={})):
    q = store.get("quotes", quote_id)
    if q and body.get("tier") in pricing.TIERS:
        q["selected"] = body["tier"]
        store.put("quotes", q)
    try:
        return workflow.send_quote(store, quote_id, base_url(request), by="rep")
    except ValueError as exc:
        _fail(exc)


@app.post("/api/leads/{lead_id}/reply")
def reply(lead_id: str, body: Dict[str, Any] = Body(default={})):
    try:
        return workflow.record_reply(store, lead_id, str(body.get("text", "")))
    except ValueError as exc:
        _fail(exc)


@app.post("/api/clock/advance")
def advance(body: Dict[str, Any] = Body(...)):
    days = float(body.get("days", 1))
    if not 0 < days <= 30:
        raise HTTPException(400, "Move the clock 1 to 30 days at a time.")
    return {"sent": workflow.advance_clock(store, days)}


@app.put("/api/settings")
def settings(body: Dict[str, Any] = Body(...)):
    if "profile" in body:
        store.set_setting("profile", {k: str(v)[:120] for k, v in body["profile"].items()
                                      if k in workflow.DEFAULT_PROFILE})
    if "rates" in body:
        store.set_setting("rates", {k: float(v) for k, v in body["rates"].items() if k in pricing.DEFAULT_RATES})
    return {"profile": workflow.profile(store), "rates": workflow.rates(store)}


@app.post("/api/demo/reset")
def demo_reset(request: Request):
    sample.reset(store, base_url(request))
    return {"ok": True}


@app.post("/api/demo/lead")
def demo_lead(request: Request):
    return sample.demo_lead(store, base_url(request))


# --- inbound leads (public: website form, Zapier, CRM webhook) -----------------

_recent: Dict[str, List[float]] = {}


@app.post("/api/inbound")
def inbound(request: Request, body: Dict[str, Any] = Body(...)):
    ip = request.client.host if request.client else "?"
    now = time.time()
    hits = [t for t in _recent.get(ip, []) if now - t < 600]
    if len(hits) >= 10:
        raise HTTPException(429, "Too many requests. Please call us instead.")
    if len(_recent) > 5000:  # bounded under a flood
        _recent.clear()
    _recent[ip] = hits + [now]
    if body.get("website"):  # honeypot field the form hides from people
        return {"ok": True}
    try:
        lead = workflow.intake(store, {**body, "source": body.get("source") or "Website quote form"},
                               base_url(request), sample=DEMO_MODE)
    except ValueError as exc:
        _fail(exc)
    return {"ok": True, "quoted": lead["stage"] == "sent"}


# --- the customer's side (public, by unguessable token) ------------------------

INTERNAL = {"cost", "margin_pct", "labor_hours"}


def _quote_by_token(token: str) -> Dict[str, Any]:
    found = store.find("quotes", token=token)
    if not found:
        raise HTTPException(404, "This proposal link isn't valid.")
    return found[0]


@app.get("/api/public/{token}")
def public_quote(token: str):
    workflow.run_due(store)
    q = _quote_by_token(token)
    workflow.record_view(store, q)
    lead = store.get("leads", q["lead_id"])
    invoices = store.find("invoices", quote_id=q["id"])
    return {
        "quote": {k: v for k, v in q.items() if k not in ("rates", "base_url", "token", "needs_review")}
        | {"options": [{k: v for k, v in o.items() if k not in INTERNAL} for o in q["options"]]},
        "customer": {k: lead.get(k) for k in ("company", "contact", "title", "city", "state", "sample")},
        "invoice": invoices[0] if invoices else None,
        "profile": workflow.profile(store),
    }


@app.post("/api/public/{token}/accept")
def public_accept(token: str, body: Dict[str, Any] = Body(...)):
    try:
        return workflow.accept(store, _quote_by_token(token), body.get("tier", ""), str(body.get("name", "")))
    except ValueError as exc:
        _fail(exc)


@app.post("/api/public/{token}/decline")
def public_decline(token: str, body: Dict[str, Any] = Body(default={})):
    try:
        workflow.decline(store, _quote_by_token(token), str(body.get("reason", "")))
    except ValueError as exc:
        _fail(exc)
    return {"ok": True}


@app.post("/api/public/{token}/pay")
def public_pay(token: str):
    q = _quote_by_token(token)
    invoices = store.find("invoices", quote_id=q["id"])
    if not invoices:
        raise HTTPException(400, "There's no invoice to pay yet.")
    if not invoices[0].get("sample"):
        # Real payments go through the client's processor (Stripe payment link, QuickBooks).
        raise HTTPException(400, "Online card payment isn't connected yet. Please pay by ACH or check.")
    return workflow.pay(store, invoices[0], "Card (sample, no charge)")


# --- pages ---------------------------------------------------------------------

@app.get("/p/{token}")
def proposal_page(token: str):
    return FileResponse(os.path.join(STATIC, "proposal.html"))


@app.get("/request")
def request_page():
    return FileResponse(os.path.join(STATIC, "request.html"))


app.mount("/", StaticFiles(directory=STATIC, html=True), name="static")
