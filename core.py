"""Signal Stack core: DB, LLM, robots-aware crawler, discovery, analysis, brief, audit."""
import os, re, io, json, time, hashlib, sqlite3, datetime as dt, urllib.robotparser as rp  # stdlib + robots parser
from urllib.parse import urlparse, quote_plus, urljoin  # URL helpers
import requests, feedparser  # HTTP + RSS/Atom parsing
from bs4 import BeautifulSoup  # HTML -> text
from dateutil import parser as dparse  # tolerant date parsing

DB = os.getenv("DB_PATH", "signalstack.db")  # sqlite file path
UA = "SignalStackBot/1.0 (research; honors robots.txt)"  # honest crawler identity
GROQ_KEY, MODEL = os.getenv("GROQ_API_KEY", ""), os.getenv("GROQ_MODEL", "llama-3.3-70b-versatile")  # free-tier LLM
CATS = ["customers", "competitors", "market", "internal"]  # the four Signal Stack categories
now = lambda: dt.datetime.utcnow().isoformat(timespec="seconds")  # UTC ISO timestamp

SCHEMA = """
CREATE TABLE IF NOT EXISTS companies(id INTEGER PRIMARY KEY, name, url, research, profile, context, settings, created);
CREATE TABLE IF NOT EXISTS qa(id INTEGER PRIMARY KEY, company_id, idx, area, question, answer, skipped INTEGER DEFAULT 0);
CREATE TABLE IF NOT EXISTS competitors(id INTEGER PRIMARY KEY, company_id, name, url, why, profile, updated);
CREATE TABLE IF NOT EXISTS sources(id INTEGER PRIMARY KEY, company_id, category, name, url, kind, reason,
  status DEFAULT 'pending', fail_count INTEGER DEFAULT 0, last_status, last_checked, last_text, dup_count INTEGER DEFAULT 0, created, competitor_id INTEGER);
CREATE TABLE IF NOT EXISTS source_checks(id INTEGER PRIMARY KEY, source_id, ts, ok INTEGER, new_records INTEGER, dups INTEGER, detail);
CREATE TABLE IF NOT EXISTS records(id INTEGER PRIMARY KEY, company_id, source_id, category, title, url, published,
  discovered, snippet, hash, analysis, score REAL, UNIQUE(company_id, hash));
CREATE TABLE IF NOT EXISTS briefs(id INTEGER PRIMARY KEY, company_id, kind, created, body, meta);
CREATE TABLE IF NOT EXISTS actions(id INTEGER PRIMARY KEY, brief_id, company_id, text, owner, priority, done INTEGER DEFAULT 0);
CREATE TABLE IF NOT EXISTS runs(id INTEGER PRIMARY KEY, company_id, kind, started, status, detail);
"""
DEFAULT_SETTINGS = {"brief_day": "mon", "brief_time": "08:00", "tz": "Asia/Kolkata",
                    "crawl_hours": 12,          # how often approved sources are monitored
                    "source_refresh_days": 7,   # how often NEW sources are discovered (pending approval)
                    "audit_day": 1}             # day of month for source-health audit

def db():
    c = sqlite3.connect(DB); c.row_factory = sqlite3.Row; return c  # rows behave like dicts
def init():
    with db() as c:
        c.executescript(SCHEMA)  # create tables if missing
        cols = [r[1] for r in c.execute("PRAGMA table_info(sources)")]  # migrate older DBs
        if "competitor_id" not in cols: c.execute("ALTER TABLE sources ADD COLUMN competitor_id INTEGER")
def q(sql, a=(), one=False):  # SELECT helper -> list of dicts (or one dict)
    with db() as c: rows = [dict(r) for r in c.execute(sql, a).fetchall()]
    return (rows[0] if rows else None) if one else rows
def ex(sql, a=()):  # write helper -> lastrowid
    with db() as c: return c.execute(sql, a).lastrowid
def J(x): return json.dumps(x, ensure_ascii=False)  # compact JSON dump

# ---------- LLM (Groq free tier) ----------
def llm(system, user, js=True):
    if not GROQ_KEY: raise RuntimeError("GROQ_API_KEY not set")  # fail loudly, no silent fake output
    for i in range(4):  # retry on rate limit
        r = requests.post("https://api.groq.com/openai/v1/chat/completions",
            headers={"Authorization": f"Bearer {GROQ_KEY}"}, timeout=90,
            json={"model": MODEL, "temperature": 0.2,  # low temp = less invention
                  "messages": [{"role": "system", "content": system}, {"role": "user", "content": user[:22000]}],  # cap tokens for free tier
                  **({"response_format": {"type": "json_object"}} if js else {})})
        if r.status_code == 429: time.sleep(6 * (i + 1)); continue  # free-tier limit: back off
        r.raise_for_status(); t = r.json()["choices"][0]["message"]["content"]
        return json.loads(t) if js else t  # parsed JSON or raw text
    raise RuntimeError("Groq rate limit persisted")

# ---------- polite fetching ----------
_last, _robots = {}, {}  # per-domain last-hit time; cached robots parsers
def allowed(url):
    p = urlparse(url); base = f"{p.scheme}://{p.netloc}"  # robots.txt lives at site root
    if base not in _robots:  # fetch each robots.txt once
        r = rp.RobotFileParser(); r.set_url(base + "/robots.txt")
        try: r.read()
        except Exception: r = None  # unreachable robots -> treat as no rules
        _robots[base] = r
    r = _robots[base]
    return True if r is None else r.can_fetch(UA, url)  # honor disallow
def fetch(url):
    if not allowed(url): raise PermissionError("blocked by robots.txt")  # never bypass restrictions
    d = urlparse(url).netloc; w = 2 - (time.time() - _last.get(d, 0))  # >=2s between hits per domain
    if w > 0: time.sleep(w)
    _last[d] = time.time()
    r = requests.get(url, headers={"User-Agent": UA}, timeout=20); r.raise_for_status(); return r  # raise on 4xx/5xx
def text_of(html):
    s = BeautifulSoup(html, "html.parser")
    for t in s(["script", "style", "nav", "footer", "noscript"]): t.decompose()  # drop non-content
    return re.sub(r"\s+", " ", s.get_text(" ")).strip()  # collapse whitespace
def find_feed(html, base):  # autodiscover RSS/Atom <link>
    l = BeautifulSoup(html, "html.parser").find("link", type=re.compile("rss|atom"))
    return urljoin(base, l["href"]) if l and l.get("href") else None
gnews = lambda qs: f"https://news.google.com/rss/search?q={quote_plus(qs)}&hl=en-IN&gl=IN&ceid=IN:en"  # news search feed
hn = lambda qs: f"https://hn.algolia.com/api/v1/search_by_date?query={quote_plus(qs)}&tags=story&hitsPerPage=20"  # HackerNews API
se = lambda qs, site: f"https://api.stackexchange.com/2.3/search/advanced?order=desc&sort=creation&q={quote_plus(qs)}&site={site}&pagesize=20"  # StackExchange API

# ---------- company research ----------
def profile_text(cid):  # compact company context handed to every LLM call
    c = q("SELECT * FROM companies WHERE id=?", (cid,), one=True)
    return J({"name": c["name"], "url": c["url"], "research": json.loads(c["research"] or "{}"),
              "profile": json.loads(c["profile"] or "{}"), "latest_context_update": c["context"]})
def research(cid):
    c = q("SELECT * FROM companies WHERE id=?", (cid,), one=True); base = c["url"]
    home = fetch(base); host = urlparse(base).netloc  # homepage first
    links = {urljoin(base, a["href"]) for a in BeautifulSoup(home.text, "html.parser").find_all("a", href=True)
             if re.search(r"about|product|service|pricing|solution|program|blog|news", a["href"], re.I)}  # likely informative pages
    pages = [(base, text_of(home.text))]
    for u in [l for l in links if urlparse(l).netloc == host][:6]:  # same-site only, max 6
        try: pages.append((u, text_of(fetch(u).text)))
        except Exception: pass  # skip blocked/broken pages
    news = []
    try: news = [(e.title, e.link) for e in feedparser.parse(fetch(gnews(f'"{c["name"]}"')).content).entries[:6]]  # public context
    except Exception: pass
    r = llm("You research companies. Use ONLY the supplied page text. Return JSON: {summary, industry, products:[], customer_segments:[], "
            "positioning, keywords:[], facts:[{claim, source_url, quote}] (quote = exact substring copied from page text), "
            "assumptions:[{claim, basis}] (anything inferred, not stated on pages)}.",
            "\n\n".join(f"URL: {u}\n{t[:3000]}" for u, t in pages) + "\nNEWS: " + J(news))
    blob = " ".join(re.sub(r"\s+", " ", t).lower() for _, t in pages)  # searchable page corpus
    ok, bad = [], list(r.get("assumptions", []))
    for f in r.get("facts", []):  # "verified" only if the quote really appears on fetched pages
        if f.get("quote") and re.sub(r"\s+", " ", f["quote"]).lower().strip() in blob: ok.append(f)
        else: bad.append({"claim": f.get("claim"), "basis": "quote not found on fetched pages"})  # demote to assumption
    r["facts"], r["assumptions"], r["recent_news"] = ok, bad, news
    ex("UPDATE companies SET research=? WHERE id=?", (J(r), cid)); return r

# ---------- adaptive interview ----------
AREAS = "business lines & revenue; best customer segments; unmet customer needs; why customers choose us; competitors/substitutes; market shifts; material threats; 6-12 month growth priorities; internal data that should influence decisions; decisions the system should support"
def next_question(cid):
    pend = q("SELECT * FROM qa WHERE company_id=? AND answer IS NULL AND skipped=0 ORDER BY idx LIMIT 1", (cid,), one=True)
    if pend: return pend  # resume unanswered question
    done = q("SELECT * FROM qa WHERE company_id=? ORDER BY idx", (cid,))
    if len(done) >= 10: return None  # interview finished
    r = llm(f"You interview a founder to configure a business-intelligence system. Ask ONE question (#{len(done)+1} of 10), specific to this company's "
            f"research and prior answers. Never repeat a covered topic; if the last answer was vague or surprising, ask a follow-up. Cover over the 10: {AREAS}. "
            "Skipped questions mean confidential: do not re-ask. Return JSON {question, area}.",
            profile_text(cid) + "\nQA SO FAR: " + J([{"q": d["question"], "a": d["answer"], "skipped": d["skipped"]} for d in done]))  # memory of prior answers
    i = ex("INSERT INTO qa(company_id,idx,area,question) VALUES(?,?,?,?)", (cid, len(done) + 1, r.get("area"), r["question"]))
    return q("SELECT * FROM qa WHERE id=?", (i,), one=True)
def build_profile(cid):  # synthesize structured profile from research + answers
    qa = q("SELECT question,answer,skipped FROM qa WHERE company_id=? ORDER BY idx", (cid,))
    p = llm("Build a structured company intelligence profile from research + founder answers. Return JSON {business_lines, revenue_streams, top_segments, "
            "customer_problems, differentiators, competitors_named, market_changes, growth_priorities, internal_info, decisions_to_support, key_terms:[]}. "
            "Mark anything the founder did not say as 'assumption:'.", profile_text(cid) + "\nQA: " + J(qa))
    ex("UPDATE companies SET profile=? WHERE id=?", (J(p), cid)); return p

# ---------- source discovery ----------
PLAN = ("Plan intelligence sources for this company. Return JSON: {competitors:[{name,url,why}] (5-10 real competitors/substitutes, homepage URL), "
        "customer_sources:[{kind:'hn'|'se', query, site (stackexchange site slug if se), why}] (4-6; where customers discuss the PROBLEM, not the brand), "
        "market_queries:[{query, why}] (6-8: regulation, industry, economy, technology, country-level, consolidation), "
        "official_sites:[{name,url,why}] (3-5 regulator/government/industry-body pages likely to change)}. "
        "Every 'why' must cite something specific in the profile. Do not repeat existing sources.")
def discover(cid):
    have = {s["url"] for s in q("SELECT url FROM sources WHERE company_id=?", (cid,))}  # skip known URLs
    plan = llm(PLAN, profile_text(cid) + "\nEXISTING: " + J(list(have))[:3000]); added = []
    def put(cat, name, url, kind, why, comp_id=None):  # insert one pending source
        if url in have or not why: return  # no duplicates, no unjustified sources
        have.add(url); added.append(ex("INSERT INTO sources(company_id,category,name,url,kind,reason,created,competitor_id) VALUES(?,?,?,?,?,?,?,?)",
                                       (cid, cat, name, url, kind, why, now(), comp_id)))
    for c in plan.get("competitors", [])[:10]:
        try: r = fetch(c["url"])  # validation drops hallucinated competitors
        except Exception: continue
        row = q("SELECT id FROM competitors WHERE company_id=? AND url=?", (cid, c["url"]), one=True)  # competitor profile row
        comp_id = row["id"] if row else ex("INSERT INTO competitors(company_id,name,url,why,updated) VALUES(?,?,?,?,?)", (cid, c["name"], c["url"], c["why"], now()))
        put("competitors", c["name"] + " website", c["url"], "page", c["why"], comp_id)  # change-detect homepage
        f = find_feed(r.text, c["url"])
        if f: put("competitors", c["name"] + " feed", f, "rss", c["why"], comp_id)  # their own blog/news feed
        put("competitors", c["name"] + " news", gnews(f'"{c["name"]}"'), "rss", c["why"] + " (news mentions: launches, pricing, hiring)", comp_id)
    for s in plan.get("customer_sources", []):  # customer-problem sources
        if s.get("kind") == "se": put("customers", f"StackExchange/{s.get('site')}: {s['query']}", se(s["query"], s.get("site", "stackoverflow")), "se", s["why"])
        else: put("customers", "HackerNews: " + s["query"], hn(s["query"]), "hn", s["why"])
    for m in plan.get("market_queries", []): put("market", "News: " + m["query"], gnews(m["query"]), "rss", m["why"])  # market/regulatory news
    for o in plan.get("official_sites", []):
        try: fetch(o["url"])  # must load and be robots-allowed
        except Exception: continue
        put("market", o["name"], o["url"], "page", o["why"])
    return q("SELECT * FROM sources WHERE id IN (%s)" % ",".join(map(str, added or [0])))

# ---------- competitor profiles ----------
def competitor_profile(cid, comp_id):
    comp = q("SELECT * FROM competitors WHERE id=? AND company_id=?", (comp_id, cid), one=True)
    page = text_of(fetch(comp["url"]).text)[:4000]  # homepage text = primary evidence
    recs = q("SELECT r.id,r.title,r.url,r.published,r.snippet FROM records r JOIN sources s ON s.id=r.source_id WHERE s.competitor_id=? ORDER BY r.id DESC LIMIT 15", (comp_id,))
    out = llm("Build a competitor profile using ONLY the homepage text and records given. Return JSON {positioning, offerings:[], pricing_evidence (exact quote or 'not found'), "
              "strengths:[], weaknesses:[], recent_moves:[{record_id, summary}], why_it_matters (strategic, labelled interpretation), threat_level 1-5, evidence_gaps}. "
              "Do not invent facts; say 'not found' when absent.", profile_text(cid) + "\nCOMPETITOR: " + comp["name"] + "\nHOMEPAGE: " + page + "\nRECORDS: " + J(recs))
    ids = {r["id"] for r in recs}
    out["recent_moves"] = [m for m in out.get("recent_moves", []) if m.get("record_id") in ids]  # drop moves citing unknown records
    ex("UPDATE competitors SET profile=?, updated=? WHERE id=?", (J(out), now(), comp_id)); return out

# ---------- crawling ----------
def toks(t): return set(re.findall(r"\w{3,}", t.lower()))  # word set for similarity
def jac(a, b): return len(a & b) / max(1, len(a | b))  # Jaccard similarity
def store(cid, src, items):  # insert new records; returns (new, dups)
    recent = [toks(r["title"]) for r in q("SELECT title FROM records WHERE company_id=? ORDER BY id DESC LIMIT 300", (cid,))]; n = d = 0
    for title, link, pub, snip in items:
        if not title or not link: continue
        h = hashlib.sha1((link.split("#")[0]).encode()).hexdigest(); t = toks(title)  # URL hash = identity
        if q("SELECT 1 FROM records WHERE company_id=? AND hash=?", (cid, h)) or any(jac(t, o) >= .8 for o in recent):  # exact or near duplicate
            ex("UPDATE sources SET dup_count=dup_count+1 WHERE id=?", (src["id"],)); d += 1; continue
        ex("INSERT INTO records(company_id,source_id,category,title,url,published,discovered,snippet,hash) VALUES(?,?,?,?,?,?,?,?,?)",
           (cid, src["id"], src["category"], title[:300], link, pub, now(), (snip or "")[:600], h)); recent.append(t); n += 1
    return n, d
def collect(src):  # fetch one source -> list of (title, link, published, snippet)
    k, u = src["kind"], src["url"]
    if k == "rss":
        return [(e.get("title"), e.get("link"), e.get("published", ""), text_of(e.get("summary", ""))) for e in feedparser.parse(fetch(u).content).entries[:25]]
    if k == "hn":
        return [(h.get("title"), f"https://news.ycombinator.com/item?id={h['objectID']}", h.get("created_at", ""), text_of(h.get("story_text") or "")) for h in fetch(u).json()["hits"]]
    if k == "se":
        return [(i["title"], i["link"], dt.datetime.utcfromtimestamp(i["creation_date"]).isoformat(), "tags: " + ",".join(i.get("tags", []))) for i in fetch(u).json()["items"]]
    if k == "page":  # change detection: keep only sentences absent from previous snapshot
        txt = text_of(fetch(u).text); lines = [l.strip() for l in re.split(r"(?<=[.!?]) ", txt) if len(l.strip()) > 30]
        old = set(json.loads(src["last_text"] or "[]")); ex("UPDATE sources SET last_text=? WHERE id=?", (J(lines[:400]), src["id"]))
        new = [l for l in lines if l not in old]
        if not old or not new: return []  # first crawl = baseline only, no invented "change"
        return [(f"Page changed: {src['name']}", u + "#" + hashlib.sha1(" ".join(new).encode()).hexdigest()[:10], now(), " | ".join(new)[:600])]
    return []
def collect_all(cid):
    total = 0
    for s in q("SELECT * FROM sources WHERE company_id=? AND status='approved' AND kind!='upload'", (cid,)):
        try:
            n, d = store(cid, s, collect(s)); total += n
            ex("UPDATE sources SET fail_count=0,last_status='ok',last_checked=? WHERE id=?", (now(), s["id"]))  # healthy
            ex("INSERT INTO source_checks(source_id,ts,ok,new_records,dups,detail) VALUES(?,?,1,?,?,'ok')", (s["id"], now(), n, d))  # history row
        except Exception as e:
            ex("UPDATE sources SET fail_count=fail_count+1,last_status=?,last_checked=? WHERE id=?", (str(e)[:200], now(), s["id"]))  # failure counter
            ex("INSERT INTO source_checks(source_id,ts,ok,new_records,dups,detail) VALUES(?,?,0,0,0,?)", (s["id"], now(), str(e)[:200]))
    return total
def duplicates(cid):  # duplicate view for Source Manager
    seen, dupsrc = {}, []
    for s in q("SELECT id,name,url,dup_count FROM sources WHERE company_id=? AND status!='removed'", (cid,)):
        k = s["url"].split("#")[0].rstrip("/").lower()
        if k in seen: dupsrc.append({"source": s["id"], "duplicate_of": seen[k], "name": s["name"]})  # same URL twice
        else: seen[k] = s["id"]
    return {"duplicate_sources": dupsrc, "records_dropped_as_duplicates": q("SELECT id,name,dup_count FROM sources WHERE company_id=? AND dup_count>0", (cid,))}

# ---------- analysis & ranking ----------
def cred(src, url):  # source reliability heuristic by source type/domain
    h = urlparse(url).netloc
    if re.search(r"\.(gov|edu|nic)\b|\.gov\.|\.ac\.", h) or src["kind"] == "upload": return .9  # official / internal
    return {"page": .7, "rss": .6, "hn": .4, "se": .45}.get(src["kind"], .5)
ASYS = ("Analyse items for the company. Use ONLY the supplied item text; never add outside facts. Return JSON {items:[{id, relevant(bool), what, evidence_quote "
        "(verbatim substring of snippet/title or ''), verified('verified' if the text itself states it as fact from a primary/credible source, else 'unverified'), "
        "interpretation (your strategic reading, clearly opinion), why_matters, business_area, type('opportunity'|'threat'|'neutral'), relevance 1-5, impact 1-5, urgency 1-5, action}]}")
def analyze(cid, limit=40):
    recs = q("SELECT r.*, s.kind, s.name sname FROM records r JOIN sources s ON s.id=r.source_id WHERE r.company_id=? AND r.analysis IS NULL LIMIT ?", (cid, limit))
    prof = profile_text(cid)
    for i in range(0, len(recs), 8):  # batch 8 per call = fewer free-tier requests
        b = recs[i:i + 8]
        out = llm(ASYS, prof + "\nITEMS: " + J([{"id": r["id"], "category": r["category"], "source": r["sname"], "title": r["title"], "snippet": r["snippet"], "published": r["published"]} for r in b]))
        by = {x["id"]: x for x in out.get("items", [])}
        for r in b:
            a = by.get(r["id"]) or {"relevant": False}  # missing LLM answer = not relevant
            try: days = (dt.datetime.utcnow() - dparse.parse(r["published"]).replace(tzinfo=None)).days; rec = max(0, 1 - days / 30)  # 30-day decay
            except Exception: rec = .5  # unknown date: neutral
            prior = [toks(x["title"]) for x in q("SELECT title FROM records WHERE company_id=? AND id<? ORDER BY id DESC LIMIT 200", (cid, r["id"]))]
            nov = 1 - max([jac(toks(r["title"]), p) for p in prior] or [0])  # novelty vs history
            quote_ok = bool(a.get("evidence_quote")) and a["evidence_quote"].lower() in (r["snippet"] + " " + r["title"]).lower()  # quote really in evidence
            ev = .3 * bool(r["snippet"]) + .3 * bool(r["published"]) + .2 + .2 * quote_ok  # evidence strength (NOT AI confidence)
            a.update(source_reliability=round(cred(r, r["url"]), 2), evidence_strength=round(ev, 2), recency=round(rec, 2), novelty=round(nov, 2))
            f = lambda k: (a.get(k, 1) or 1) / 5  # normalize 1-5 -> 0-1
            a["strategic_relevance"] = round(f("relevance"), 2)
            score = 100 * (.25 * f("relevance") + .25 * f("impact") + .15 * f("urgency") + .15 * a["source_reliability"] + .1 * rec + .1 * nov) if a.get("relevant") else 0
            a["why_ranked"] = f"relevance {a.get('relevance')}/5, impact {a.get('impact')}/5, urgency {a.get('urgency')}/5, source reliability {a['source_reliability']}, recency {a['recency']}, novelty {a['novelty']}"
            ex("UPDATE records SET analysis=?, score=? WHERE id=?", (J(a), round(score, 1), r["id"]))
    return len(recs)

# ---------- internal uploads ----------
def ingest_upload(cid, filename, data):
    ext = filename.lower().rsplit(".", 1)[-1]
    if ext in ("csv", "xlsx", "xls"):  # tabular -> CSV text (first 150 rows)
        import pandas as pd
        df = pd.read_csv(io.BytesIO(data)) if ext == "csv" else pd.read_excel(io.BytesIO(data))
        txt = df.head(150).to_csv(index=False)
    elif ext == "pdf":  # PDF -> text
        from pypdf import PdfReader
        txt = "\n".join(p.extract_text() or "" for p in PdfReader(io.BytesIO(data)).pages)
    else: txt = data.decode("utf-8", "ignore")  # plain text
    src = q("SELECT * FROM sources WHERE company_id=? AND kind='upload' AND name=?", (cid, filename), one=True)
    sid = src["id"] if src else ex("INSERT INTO sources(company_id,category,name,url,kind,reason,status,created) VALUES(?,?,?,?,?,?,?,?)",
        (cid, "internal", filename, "upload://" + filename, "upload", "Founder-supplied internal data", "approved", now()))
    r = llm("Extract recurring objections, lost-deal reasons, product requests, sales patterns, emerging risks from this internal data. Return JSON {insights:[{title, "
            "quote (verbatim from data), kind}]}. Only what the data shows.", txt)
    s = q("SELECT * FROM sources WHERE id=?", (sid,), one=True)
    return store(cid, s, [(i["title"], f"upload://{filename}#{hashlib.sha1(i['title'].encode()).hexdigest()[:8]}", now(), i.get("quote", "")) for i in r.get("insights", [])])[0]

# ---------- Monday brief ----------
BSYS = ("Write a Monday Morning Intelligence Brief for a founder using ONLY the provided records; cite as [id] (a record id from the list, one id per bracket). Return JSON {markdown, actions:[{text,owner,priority,reason}]}. "
        "Markdown sections in order: ## Executive Summary (top 5), ## Customer Signals, ## Competitor Intelligence, ## Market Intelligence, ## Internal Business Intelligence, "
        "## Opportunities Identified (evidence, impact, next step, priority; aim for 3 but list fewer + state the limitation if evidence is thin), ## Risks & Threats, "
        "## Recommended Actions for This Week, ## What We Should Watch. Separate 'Evidence' from 'Interpretation' in each item. If a category has no records, write "
        "'No meaningful development this period.' Never invent developments. Do NOT write a source appendix.")
def check_citations(md, valid):  # validate every [id] the LLM wrote against real record ids
    bad, cited = set(), set()
    def fix(m):
        ids = [int(x) for x in re.findall(r"\d+", m.group(1))]
        if all(i in valid for i in ids): cited.update(ids); return m.group(0)  # all ids real -> keep
        bad.update(i for i in ids if i not in valid); return "[ref-invalid]"  # fabricated id -> flag in text
    return re.sub(r"\[(\d+(?:\s*,\s*\d+)*)\]", fix, md), cited, bad
def brief(cid, kind="weekly"):
    collect_all(cid); analyze(cid)  # refresh data first
    since = (dt.datetime.utcnow() - dt.timedelta(days=14)).isoformat()
    recs = []
    for cat in CATS:  # top 6 relevant records per category
        recs += q("SELECT r.*, s.name sname FROM records r JOIN sources s ON s.id=r.source_id WHERE r.company_id=? AND r.category=? AND r.score>0 AND r.discovered>=? ORDER BY r.score DESC LIMIT 6", (cid, cat, since))
    out = llm(BSYS, profile_text(cid) + "\nRECORDS: " + J([{"id": r["id"], "category": r["category"], "title": r["title"], "published": r["published"],
        "evidence": r["snippet"][:300], "analysis": {k: v for k, v in json.loads(r["analysis"]).items() if k in ("what", "interpretation", "why_matters", "type", "urgency", "action", "verified")}, "score": r["score"]} for r in recs]))
    md, cited, bad = check_citations(out["markdown"], {r["id"] for r in recs})  # citation integrity check
    app = "\n\n## Source Appendix\n| ID | Cited in brief | Source | Link | Published | Discovered | Evidence | Verification | Reliability | Evidence strength |\n|---|---|---|---|---|---|---|---|---|---|\n"
    for r in recs:  # appendix built from DB, not from LLM text
        a = json.loads(r["analysis"]); app += f"| {r['id']} | {'yes' if r['id'] in cited else 'no'} | {r['sname']} | {r['url']} | {r['published']} | {r['discovered']} | {r['snippet'][:160].replace('|',' ')} | {a.get('verified')} | {a['source_reliability']} | {a['evidence_strength']} |\n"
    if not recs: app += "\n_No analysed records in the window; limitation: sources may be new, blocked, or quiet._"
    app += f"\n**Citation check:** {len(cited)} valid records cited, {len(bad)} invalid ids removed {sorted(bad) or ''}, {len(recs) - len(cited)} analysed records not cited.\n"
    bid = ex("INSERT INTO briefs(company_id,kind,created,body,meta) VALUES(?,?,?,?,?)", (cid, kind, now(), md + app, J({"records": len(recs), "invalid_citations": sorted(bad)})))
    for a in out.get("actions", []): ex("INSERT INTO actions(brief_id,company_id,text,owner,priority) VALUES(?,?,?,?,?)", (bid, cid, a.get("text"), a.get("owner"), a.get("priority")))  # trackable actions
    return bid

# ---------- monthly audit & context reassessment ----------
def audit(cid):
    srcs = q("SELECT s.*, (SELECT COUNT(*) FROM records r WHERE r.source_id=s.id) n, (SELECT AVG(score) FROM records r WHERE r.source_id=s.id) avg, "
             "(SELECT MAX(discovered) FROM records r WHERE r.source_id=s.id) lastrec FROM sources s WHERE company_id=? AND status='approved'", (cid,))
    cut = (dt.datetime.utcnow() - dt.timedelta(days=60)).isoformat(); rep = {"dead": [], "inactive": [], "declining": [], "duplicates": []}
    for s in srcs:
        if s["fail_count"] >= 3: rep["dead"].append((s["id"], s["name"], s["last_status"]))  # 3 straight failures
        elif s["kind"] != "upload" and (s["lastrec"] or "") < cut and (s["created"] or "") < cut: rep["inactive"].append((s["id"], s["name"]))  # no output 60d
        if s["n"] >= 5 and (s["avg"] or 0) < 15: rep["declining"].append((s["id"], s["name"], round(s["avg"] or 0, 1)))  # low average score
    rep["duplicates"] = [(d["source"], d["name"], f"dup of {d['duplicate_of']}") for d in duplicates(cid)["duplicate_sources"]]
    try: new = [s["name"] for s in discover(cid)]  # newly discovered, pending approval
    except Exception as e: new = [f"discovery failed: {e}"]
    body = "# Monthly Source-Health Report\n" + "".join(f"\n## {k.title()} ({len(v)})\n" + "\n".join(f"- {x}" for x in v) for k, v in rep.items()) + \
           f"\n\n## Newly discovered (pending approval) ({len(new)})\n" + "\n".join(f"- {n}" for n in new) + \
           "\n\n## Recommendation\nRemove dead/duplicate sources; review inactive and declining ones; approve useful new sources."
    return ex("INSERT INTO briefs(company_id,kind,created,body,meta) VALUES(?,?,?,?,?)", (cid, "audit", now(), body, J(rep)))
def reassess(cid, new_context):
    ex("UPDATE companies SET context=? WHERE id=?", (new_context, cid))  # store new business context
    ss = q("SELECT s.id,s.name,s.category,s.reason,(SELECT COUNT(*) FROM records r WHERE r.source_id=s.id) n FROM sources s WHERE company_id=? AND status IN ('approved','pending') AND kind!='upload'", (cid,))
    d = llm("Business context changed. Re-judge each source's relevance. Return JSON {keep:[{id,reason}], drop:[{id,reason}], summary}. Drop only sources no longer useful under the NEW context.",
            profile_text(cid) + "\nSOURCES: " + J(ss))
    for x in d.get("drop", []): ex("UPDATE sources SET status='removed', reason=reason||' | REMOVED after context change: '||? WHERE id=? AND company_id=?", (x.get("reason", ""), x.get("id"), cid))
    added = [s["name"] for s in discover(cid)]  # recommend replacements
    body = f"# Signal Stack change log\nNew context: {new_context}\n\n{d.get('summary','')}\n\n## Dropped\n" + "\n".join(f"- #{x['id']}: {x.get('reason')}" for x in d.get("drop", [])) + "\n\n## Added (pending approval)\n" + "\n".join(f"- {a}" for a in added)
    return ex("INSERT INTO briefs(company_id,kind,created,body,meta) VALUES(?,?,?,?,?)", (cid, "change_log", now(), body, "{}"))
