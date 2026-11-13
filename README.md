# VectorSales

**Quote-to-cash for commercial cleaning and facility-service companies.** A lead
comes in, a fixed-price quote goes out, and the follow-up happens on its own,
through to a signed proposal and a paid first invoice.

```
website form / CRM / ads ──► instant reply (seconds) ──► 3-option fixed-price quote
        ──► auto-sent (or one-click approval for big / low-margin jobs)
        ──► follow-ups day 2, 5, 9 (stop on reply, accept or decline)
        ──► customer accepts online ──► invoice + welcome email ──► paid
```

## Run the 5-minute demo

```bash
pip install -r requirements.txt
DEMO_MODE=1 uvicorn server:app --reload
# open http://localhost:8000 and press the first step, or ▶ Autoplay
```

Demo mode loads a **clearly labeled sample pipeline**. Every company has
"(SAMPLE)" in its name, emails use reserved `.example` domains, phone numbers
are in the fictional 555-01xx range, and banners on every page say it's sample
data. Sample leads never leave the outbox, even with email configured. **Reset
sample data** rebuilds the pipeline. The talk track is in
[docs/demo-script.md](docs/demo-script.md).

## What it does

| Stage | How |
|---|---|
| **Lead intake** | Public quote form at `/request` (embed or link it from the client's site), or `POST /api/inbound` from Zapier, a CRM, Facebook/Google lead forms. Honeypot field and per-IP rate limit. |
| **Speed to lead** | Instant acknowledgement from the client's name. If the request has size and frequency, it says the quote is minutes away; otherwise it asks for those details. |
| **Pricing** (`pricing.py`) | Labor-based: square feet ÷ production rate for the facility type, × visits, × wage + burden, + supplies + overhead, then price = cost ÷ (1 − target margin), rounded up to $5. Plus a one-time initial deep clean. |
| **Good / Better / Best** | Carried over from the Cost Comparison Tool as a *substitution* strategy: **Like-for-like** (all full cleans), **Smart schedule** (some visits become touch-ups, so it costs less and looks the same), **Smart schedule + supplies** (adds quarterly floor care and restroom consumables moved to a lower cost-per-use towel format). |
| **Cost comparison** | Each option against what the prospect pays their current vendor (Best is compared with the vendor plus their supply spend), and restroom supplies by format on cost per use. |
| **Guardrails** | Quotes at or above the auto-send limit, or under target margin, wait for one-click approval. Cost and margin (🔒) are internal only: the customer API strips them. |
| **Follow-up** (`workflow.py`) | Day 2 check-in, day 5 value email (leads with their savings), day 9 last call. Each is written when it sends, from what the prospect has done (e.g. whether they opened the proposal). Stops on reply, acceptance, decline or a re-quote. |
| **Close** | Proposal page at `/p/<token>`: pick an option, type a name to accept, or decline with a reason. Printable to PDF. |
| **Cash** | Acceptance creates the invoice (deep clean + first month) and sends it with the welcome email. Customers can pay from the same page. Sample invoices pay without a charge; live payment is a Stripe/QuickBooks hook-up per client. |
| **Reporting** | Speed to lead, lead-to-quote time, open pipeline, won MRR, automated follow-ups, collected, win rate. |

## Reused from the Cost Comparison Tool

To keep the build fast and lean, VectorSales takes these straight from
`Cost-Comparison-Tool`: the sign-in module (`auth.py`, unchanged), the design
system (ink and brass, DM Serif Display + Plus Jakarta Sans, self-hosted
fonts, dark mode), the margin rule (cost ÷ (1 − margin), rounded up), the
good/better/best substitution and cost-per-use comparison, the security
headers, and the "Claude when configured, rules otherwise" pattern for writing.
It has two dependencies (FastAPI, uvicorn) and none of the original's
torch/CLIP/OCR stack, so it runs on the smallest Render plan.

## Configure a client install

| Variable | Purpose |
|---|---|
| `AUTH_USERS`, `AUTH_SECRET`, `AUTH_MANAGERS` | Sign-in, same as the Cost Comparison Tool. Make an entry with `python auth.py hash-password`. With no users, the API refuses requests unless `DEMO_MODE=1`. |
| `PUBLIC_URL` | Base for links in emails, e.g. `https://quotes.clientdomain.com`. |
| `SMTP_HOST`, `SMTP_PORT`, `SMTP_USER`, `SMTP_PASSWORD`, `SMTP_FROM` | Real email delivery (STARTTLS). Without them, messages stay in the outbox. |
| `ANTHROPIC_API_KEY` (optional), `WRITER_AI_MODEL` | Lightly personalizes quote and follow-up emails from the prospect's own notes. Prices and links are checked and kept; if anything is off or slow, the template is used. Never used for sample leads. |
| `DATA_DIR` | Where the SQLite database lives (a Render disk). |

The company name, sender, phone, labor rates, burden, overhead, target margin,
floor-care and deep-clean rates, and auto-send limit are set in **Settings**.

## API

`POST /api/inbound` (public) takes `company`, `email` (required), `contact`,
`phone`, `facility_type` (`office`, `medical`, `school`, `retail`,
`industrial`, `fitness`), `sqft`, `visits_per_week`, `occupants`,
`current_monthly`, `city`, `state`, `notes`, `source`. Everything else is
under `/api/` behind sign-in; the customer side is `/api/public/<token>`.

## Tests

```bash
pip install -r requirements-test.txt && python -m pytest tests -q
```

The flow test drives a lead from the form to a paid invoice through the HTTP
API, moving the clock to check each follow-up fires, and that they stop on
acceptance and on reply.

## Offer

The productized package ($5K, two-week build; $1.5–2K/month to run) is in
[docs/offer.md](docs/offer.md).

## Prospecting: who to sell VectorSales to

`prospecting/build_list.py` builds the target list: cleaning companies
(commercial or residential) with **10–50 employees** within about 45 miles of
Philadelphia, covering the PA collar counties, South Jersey and Wilmington.

1. **Discover** with Google Places Text Search around 16 sub-territory centers
   (`--provider google`, the default) or Geoapify's OpenStreetMap-based Places
   API (`--provider geoapify`), or both, plus Apollo's organization search,
   which can filter by headcount itself.
2. **Screen**: still operating, inside the radius (measured from City Hall),
   de-duplicated by website domain. `--crawl` reads each homepage, respecting
   robots.txt, to tag commercial or residential and catch "team of 25"-type
   size clues.
3. **Enrich** with Apollo by domain for headcount, then the owner or
   operations decision-maker.
4. **Output** `prospects_qualified.csv` (10–50 confirmed, best fit first, up
   to 200) and `prospects_needs_check.csv` (headcount unknown). The columns
   match the Cost Comparison Tool's prospect import. When you import, leave
   "drop rows outside 20–500 employees" off, or the 10–19 companies go.

```bash
export GOOGLE_MAPS_API_KEY=... GEOAPIFY_API_KEY=... APOLLO_API_KEY=...   # whichever you use
python prospecting/build_list.py --dry-run     # shows the call budget, no calls
python prospecting/build_list.py --crawl --out lists/
```

**Google or Geoapify?** Google finds more small businesses, with websites and
phones included. Geoapify is the safer choice against a surprise bill: its free
plan (3,000 credits a day, no card) suspends rather than charges. It has no
janitorial category, so it searches by company name ("clean", "janitorial",
"maid", ...), and websites and phones come from OpenStreetMap contributors, so
many small firms will come back without them. Use `--provider both` to merge
the two. Its terms require OpenStreetMap attribution, and say nothing about
storing results; the underlying OSM data is open (ODbL), and an internal
prospect list isn't a published map. With Google, set a daily quota on the
Places API in the Cloud Console. Budget alerts only email you.

The default run makes up to 96 Google searches and caps Apollo enrich/people
calls at 400 (`--max-enrich`). Free-tier allowances and which Apollo endpoints
a plan can call change often, so check both before a big run. Apollo locks
most emails until you spend a credit to reveal them. Email outreach must
follow CAN-SPAM: a real sender, a physical address and a working opt-out.
