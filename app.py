"""Flask API + APScheduler. Run: python app.py"""
import json, threading, traceback
from flask import Flask, request, jsonify, send_from_directory, Response
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
import core as C

app = Flask(__name__, static_folder="static"); C.init()
sched = BackgroundScheduler()

def run_job(cid, kind, fn, *a):
    """Execute + log every run; failures are stored and shown in dashboard."""
    rid = C.ex("INSERT INTO runs(company_id,kind,started,status) VALUES(?,?,?,'running')", (cid, kind, C.now()))
    try: res = fn(cid, *a); C.ex("UPDATE runs SET status='ok', detail=? WHERE id=?", (str(res), rid))
    except Exception as e: C.ex("UPDATE runs SET status='failed', detail=? WHERE id=?", (f"{e}\n{traceback.format_exc()[-800:]}", rid))
def bg(cid, kind, fn, *a): threading.Thread(target=run_job, args=(cid, kind, fn, *a), daemon=True).start()

def settings(cid):
    c = C.q("SELECT settings FROM companies WHERE id=?", (cid,), one=True)
    return {**C.DEFAULT_SETTINGS, **json.loads(c["settings"] or "{}")}
def schedule(cid):
    s = settings(cid); h, m = s["brief_time"].split(":")
    for jid in (f"brief-{cid}", f"audit-{cid}", f"crawl-{cid}", f"refresh-{cid}"):  # clear old jobs before re-adding
        if sched.get_job(jid): sched.remove_job(jid)
    sched.add_job(bg, CronTrigger(day_of_week=s["brief_day"], hour=int(h), minute=int(m), timezone=s["tz"]), id=f"brief-{cid}", args=[cid, "weekly_brief", C.brief])
    sched.add_job(bg, CronTrigger(day=int(s["audit_day"]), hour=3, timezone=s["tz"]), id=f"audit-{cid}", args=[cid, "monthly_audit", C.audit])
    sched.add_job(bg, "interval", days=int(s["source_refresh_days"]), id=f"refresh-{cid}", args=[cid, "source_refresh", C.discover])  # discover new sources (pending)
    sched.add_job(bg, "interval", hours=int(s["crawl_hours"]), id=f"crawl-{cid}", args=[cid, "crawl", lambda c: (C.collect_all(c), C.analyze(c))])
for c in C.q("SELECT id FROM companies"): schedule(c["id"])
sched.start()

@app.get("/")
def home(): return send_from_directory("static", "index.html")  # serve dashboard

@app.get("/api/companies")
def companies(): return jsonify(C.q("SELECT id,name,url FROM companies"))  # list companies
@app.post("/api/companies")
def new_company():  # create company + research it
    d = request.json; cid = C.ex("INSERT INTO companies(name,url,settings,created) VALUES(?,?,?,?)", (d["name"], d["url"], "{}", C.now()))
    schedule(cid)
    try: C.research(cid)
    except Exception as e: return jsonify(id=cid, error=f"research failed: {e}"), 207
    return jsonify(id=cid)
@app.get("/api/companies/<int:cid>")
def company(cid):  # company + profile + QA + settings
    c = C.q("SELECT * FROM companies WHERE id=?", (cid,), one=True)
    c.update(research=json.loads(c["research"] or "{}"), profile=json.loads(c["profile"] or "{}"), settings=settings(cid),
             qa=C.q("SELECT * FROM qa WHERE company_id=? ORDER BY idx", (cid,))); return jsonify(c)
@app.post("/api/companies/<int:cid>/interview")
def interview(cid):  # submit answer/skip, get next question; builds profile after Q10
    d = request.json or {}
    if d.get("id"):  # submit answer or skip
        C.ex("UPDATE qa SET answer=?, skipped=? WHERE id=?", (None if d.get("skip") else d.get("answer"), 1 if d.get("skip") else 0, d["id"]))
    n = C.next_question(cid)
    if n is None: C.build_profile(cid)
    return jsonify(next=n, done=n is None)
@app.put("/api/qa/<int:i>")
def edit_qa(i): return jsonify(ok=1)  # founder edits an answer C.ex("UPDATE qa SET answer=?, skipped=0 WHERE id=?", (request.json["answer"], i));
@app.post("/api/companies/<int:cid>/profile/rebuild")
def rebuild(cid): return jsonify(C.build_profile(cid))  # regenerate profile from answers
@app.put("/api/companies/<int:cid>/profile")
def put_profile(cid): return jsonify(ok=1)  # founder edits profile JSON C.ex("UPDATE companies SET profile=? WHERE id=?", (C.J(request.json), cid));
@app.put("/api/companies/<int:cid>/settings")
def put_settings(cid): return jsonify(ok=1)  # save settings + reschedule jobs C.ex("UPDATE companies SET settings=? WHERE id=?", (C.J(request.json), cid)); schedule(cid);

@app.post("/api/companies/<int:cid>/discover")
def discover(cid): return jsonify(C.discover(cid))  # discover sources (pending approval)
@app.get("/api/companies/<int:cid>/sources")
def sources(cid):  # list sources with record counts
    return jsonify(C.q("SELECT s.*,(SELECT COUNT(*) FROM records r WHERE r.source_id=s.id) records FROM sources s WHERE company_id=? AND status!='removed' ORDER BY category,id", (cid,)))
@app.post("/api/companies/<int:cid>/sources")
def add_source(cid):  # manual source add (auto-approved)
    d = request.json; i = C.ex("INSERT INTO sources(company_id,category,name,url,kind,reason,status,created) VALUES(?,?,?,?,?,?,'approved',?)",
        (cid, d["category"], d.get("name") or d["url"], d["url"], d.get("kind", "rss"), d.get("reason", "Added manually"), C.now())); return jsonify(id=i)
@app.patch("/api/sources/<int:sid>")
def patch_source(sid): return jsonify(ok=1)  # approve/remove source C.ex("UPDATE sources SET status=? WHERE id=?", (request.json["status"], sid));

@app.post("/api/companies/<int:cid>/crawl")
def crawl(cid): return jsonify(started=1), 202  # manual crawl + analysis (background) bg(cid, "manual_crawl", lambda c: (C.collect_all(c), C.analyze(c)));
@app.post("/api/companies/<int:cid>/brief")
def manual_brief(cid): return jsonify(started=1), 202  # manual brief (background) bg(cid, "manual_brief", C.brief, "manual");
@app.post("/api/companies/<int:cid>/audit")
def manual_audit(cid): return jsonify(started=1), 202  # manual source audit (background) bg(cid, "manual_audit", C.audit);
@app.post("/api/companies/<int:cid>/reassess")
def reassess(cid): return jsonify(started=1), 202  # context change -> re-judge sources (background) bg(cid, "context_reassess", C.reassess, request.json["context"]);
@app.post("/api/companies/<int:cid>/upload")
def upload(cid):  # internal CSV/XLSX/PDF/TXT upload
    f = request.files["file"]; return jsonify(records=C.ingest_upload(cid, f.filename, f.read()))

@app.get("/api/companies/<int:cid>/feed")
def feed(cid):
    a = request.args  # all filters come from query string
    sql = "SELECT r.*,s.name sname,s.competitor_id FROM records r JOIN sources s ON s.id=r.source_id WHERE r.company_id=?"; p = [cid]
    if a.get("category"): sql += " AND r.category=?"; p.append(a["category"])  # category filter
    if a.get("competitor"): sql += " AND s.competitor_id=?"; p.append(a["competitor"])  # competitor filter (id)
    if a.get("since"): sql += " AND r.discovered>=?"; p.append(a["since"])  # date from
    if a.get("until"): sql += " AND r.discovered<=?"; p.append(a["until"] + "T23:59:59")  # date to
    rows = C.q(sql + " ORDER BY r.discovered DESC LIMIT 500", p)
    for r in rows: r["analysis"] = json.loads(r["analysis"] or "null")  # parse analysis JSON
    an = lambda r, k: (r["analysis"] or {}).get(k)  # safe analysis getter
    if a.get("type"): rows = [r for r in rows if an(r, "type") == a["type"]]  # opportunity/threat/neutral
    if a.get("verified"): rows = [r for r in rows if an(r, "verified") == a["verified"]]  # verification status
    if a.get("min_score"): rows = [r for r in rows if (r["score"] or 0) >= float(a["min_score"])]  # overall rank
    if a.get("min_relevance"): rows = [r for r in rows if (an(r, "relevance") or 0) >= int(a["min_relevance"])]  # relevance 1-5
    if a.get("min_impact"): rows = [r for r in rows if (an(r, "impact") or 0) >= int(a["min_impact"])]  # impact 1-5
    return jsonify(rows)
@app.get("/api/companies/<int:cid>/competitors")
def competitors(cid):
    rows = C.q("SELECT * FROM competitors WHERE company_id=?", (cid,))
    for r in rows: r["profile"] = json.loads(r["profile"] or "null")
    return jsonify(rows)
@app.post("/api/companies/<int:cid>/competitors/<int:comp>/profile")
def comp_profile(cid, comp):  # build/refresh one competitor profile (sync, ~10s)
    try: return jsonify(C.competitor_profile(cid, comp))
    except Exception as e: return jsonify(error=str(e)), 500
@app.get("/api/sources/<int:sid>/history")
def history(sid): return jsonify(C.q("SELECT * FROM source_checks WHERE source_id=? ORDER BY id DESC LIMIT 50", (sid,)))  # crawl history
@app.get("/api/companies/<int:cid>/duplicates")
def dups(cid): return jsonify(C.duplicates(cid))  # duplicate view
@app.get("/api/companies/<int:cid>/briefs")
def briefs(cid): return jsonify(C.q("SELECT * FROM briefs WHERE company_id=? ORDER BY id DESC", (cid,)))  # brief + audit + change_log history
@app.get("/api/briefs/<int:b>/export")
def export(b): return Response(C.q("SELECT body FROM briefs WHERE id=?", (b,), one=True)["body"], mimetype="text/markdown", headers={"Content-Disposition": f"attachment; filename=brief-{b}.md"})  # download brief as email-ready markdown
@app.get("/api/companies/<int:cid>/actions")
def actions(cid): return jsonify(C.q("SELECT * FROM actions WHERE company_id=? ORDER BY id DESC", (cid,)))  # action tracking list
@app.patch("/api/actions/<int:i>")
def act(i): return jsonify(ok=1)  # toggle action done C.ex("UPDATE actions SET done=? WHERE id=?", (int(request.json["done"]), i));
@app.get("/api/companies/<int:cid>/runs")
def runs(cid): return jsonify(C.q("SELECT * FROM runs WHERE company_id=? ORDER BY id DESC LIMIT 50", (cid,)))  # scheduler/run log incl. failures

if __name__ == "__main__": app.run(port=5000, use_reloader=False)
