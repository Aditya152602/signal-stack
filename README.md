# Signal Stack — Company Intelligence & Executive Briefing Engine
Enter any company + URL → research → adaptive 10-Q interview → generated Signal Stack → crawl → analyse/rank → Monday Brief.
No company, competitor or source is hard-coded. All sources are LLM-proposed from the profile, URL-validated, then human-approved.

## Run
```
pip install -r requirements.txt
cp .env.example .env   # add free Groq key; then: export $(cat .env | xargs)
python app.py          # http://localhost:5000
```
Deploy: Render/Railway free tier (`gunicorn app:app --workers 1`; keep 1 worker so APScheduler runs once; use a persistent disk for `DB_PATH`).

## Demo flow (MeetUniversity — https://www.meetuniversity.com)
1. Setup → name + URL → Research (verified facts only kept if quote found on fetched page; rest = assumptions).
2. Interview tab → 10 adaptive Qs (skip allowed; edit answers in Profile).
3. Stack → Discover → review "why" → Approve (need ≥20 approved) → Crawl now.
4. Feed → filter/inspect evidence → Brief → Generate. Repeat for 2 other companies (e.g. a SaaS and a D2C brand).
5. Profile → "Business context changed?" → Reassess (drops/adds sources, writes change_log).
6. Brief tab → Run source audit; Runs panel shows failures.

## Architecture (core.py)
| Stage | How |
|---|---|
| Research | Fetch homepage + about/product/pricing pages + Google News RSS; LLM extracts claims; quote-substring check separates verified vs assumption |
| Interview | One Q at a time; LLM sees research + prior Q&A; follow-ups; skip; editable; builds structured profile |
| Discovery | LLM plan → competitors (homepage fetch-validated, feed autodiscovery, news RSS), customer-problem sources (HN Algolia, StackExchange APIs), market news queries, official regulator pages |
| Crawl | robots.txt honored, 2 s/domain rate limit, RSS/JSON/page-diff, URL-hash + title-Jaccard dedupe, history kept, health counters |
| Analysis | LLM sees only stored evidence; outputs evidence vs interpretation, type, urgency |
| Ranking | score = .25 relevance + .25 impact + .15 urgency + .15 source reliability + .10 recency + .10 novelty. Separate stored fields: `source_reliability`, `evidence_strength` (snippet/date/link/verbatim quote), `strategic_relevance`. LLM confidence never used as truth |
| Brief | Sections per spec; empty category → "No meaningful development"; Source Appendix built deterministically from DB (not LLM) |
| Internal | CSV/XLSX/PDF/TXT → objections, lost-deal reasons, requests as records |
| Competitor profiles | Per-competitor page from homepage text + stored records; moves citing unknown record ids are dropped |
| Citation check | Every `[id]` in the brief is validated against real record ids; fakes become `[ref-invalid]`, summary printed under appendix |
| Source history | Every crawl logged per source (ok/fail, new, dups); duplicate view in Sources tab |
| Scheduler | APScheduler: weekly brief (day/time/tz), crawl every N h, source refresh every N days (new sources → pending), monthly audit; all runs logged in `runs`, failures shown in dashboard |

## Free tier / cost: ₹0
Flask, SQLite, APScheduler, BeautifulSoup (OSS). Groq free API (rate/token limits; app backs off on 429, batches 8 items/call). Google News RSS, HN Algolia, StackExchange APIs (no key; StackExchange ~300 req/day unauthenticated).

## Honest limitations
- Reddit/Quora/LinkedIn not crawled (robots/ToS). Customer voice comes from HN + StackExchange + news; thin for niche B2C markets — brief states this rather than inventing.
- First crawl of a page = baseline only; page "changes" appear from 2nd crawl onward.
- Google News RSS may rate-limit; failed sources show in health and audit.
- Record counts (≥30) and 20 approved sources are reached only by actually running the crawl; none are shipped pre-filled.
- Single-user, no auth. Email delivery not built: brief is exportable `.md` (email-ready).
- Not tested against live Groq here (no key/network in build sandbox); DB, API, dedupe smoke-tested.
