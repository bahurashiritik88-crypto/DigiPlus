import os, re, json, time, uuid, sqlite3, logging
from logging.handlers import RotatingFileHandler
from datetime import date
from flask import Flask, request, jsonify, g, render_template, has_request_context

BASE = os.path.dirname(os.path.abspath(__file__))
if os.path.exists(os.path.join(BASE, ".env")):
    for l in open(os.path.join(BASE, ".env")):
        if "=" in l and not l.startswith("#"):
            k, v = l.strip().split("=", 1); os.environ.setdefault(k, v)
MODEL = os.getenv("AI_MODEL", "claude-sonnet-5-5")
os.makedirs(os.path.join(BASE, "data"), exist_ok=True); os.makedirs(os.path.join(BASE, "logs"), exist_ok=True)
DB = os.path.join(BASE, "data", "app.db")

# ---------- structured logging (JSON lines, request id, no secrets) ----------
class JsonFmt(logging.Formatter):
    def format(self, r):
        d = {"ts": self.formatTime(r), "level": r.levelname, "request_id": getattr(r, "rid", "-"), "msg": r.getMessage()}
        d.update(getattr(r, "ctx", {}))
        if r.exc_info: d["traceback"] = self.formatException(r.exc_info)
        return re.sub(r"sk-[A-Za-z0-9_\-]{8,}", "[REDACTED]", json.dumps(d))
logger = logging.getLogger("app"); logger.setLevel(logging.INFO)
_h = RotatingFileHandler(os.path.join(BASE, "logs", "app.log"), maxBytes=2_000_000, backupCount=3)
_h.setFormatter(JsonFmt()); logger.addHandler(_h)
_c = logging.StreamHandler(); _c.setFormatter(JsonFmt()); logger.addHandler(_c)
def log(level, msg, **ctx):
    rid = g.get("rid", "-") if has_request_context() else "-"
    logger.log(level, msg, extra={"rid": rid, "ctx": ctx})

app = Flask(__name__)
class ApiError(Exception):
    def __init__(s, msg, code=400): s.msg, s.code = msg, code

SCHEMA = """
create table if not exists meetings(id integer primary key, title text, kind text, agenda text, participants text,
 notes text, summary text, decisions text default '[]', links text default '[]', status text default 'planned',
 meeting_date text, prev_id integer, created_at text default current_timestamp);
create table if not exists items(id integer primary key, meeting_id integer, title text, owner text, due text,
 status text default 'open', outcome text default '', created_at text default current_timestamp);
create table if not exists history(id integer primary key, item_id integer, event text, ts text default current_timestamp);
create table if not exists docs(id integer primary key, title text, content text, created_at text default current_timestamp);
"""
def init_db():
    c = sqlite3.connect(DB); c.executescript(SCHEMA)
    if not c.execute("select count(*) from docs").fetchone()[0]:
        for t, ct in [("Release Process", "Releases ship every second Thursday. QA sign-off and rollback plan required before deploy."),
                      ("Customer Onboarding Playbook", "New customers get a kickoff call, data import, training session and 30 day review. Owner: customer success."),
                      ("Vendor Contracts Policy", "All vendor contracts above 5000 USD need legal review and finance approval before signature.")]:
            c.execute("insert into docs(title,content) values(?,?)", (t, ct))
        c.execute("insert into meetings(title,kind,agenda,participants,notes,summary,status,meeting_date) values(?,?,?,?,?,?,?,?)",
                  ("Weekly Product Sync", "meeting", "Release status, vendor contract", '["Asha","Ravi","Meera"]', "", "Release slipping by 2 days; vendor contract pending legal.", "held", date.today().isoformat()))
        c.execute("insert into items(meeting_id,title,owner,due) values(1,'Send vendor contract to legal','Ravi','2026-10-25')")
        c.execute("insert into items(meeting_id,title,owner,due) values(1,'Prepare rollback plan for release','Meera','2026-11-05')")
        c.execute("insert into history(item_id,event) values(1,'created'),(2,'created')")
    c.commit(); c.close()
init_db()

def db():
    if "db" not in g: g.db = sqlite3.connect(DB); g.db.row_factory = sqlite3.Row
    return g.db
def q(sql, a=(), one=False):
    r = db().execute(sql, a).fetchall(); db().commit()
    return (r[0] if r else None) if one else r
def ins(sql, a):
    c = db().execute(sql, a); db().commit(); return c.lastrowid

@app.before_request
def _start():
    g.rid = request.headers.get("X-Request-ID") or uuid.uuid4().hex[:12]; g.t0 = time.time()
@app.after_request
def _end(r):
    r.headers["X-Request-ID"] = g.rid
    log(logging.INFO, "request", method=request.method, path=request.path, status=r.status_code, ms=int((time.time() - g.t0) * 1000))
    return r
@app.teardown_request
def _close(e):
    d = g.pop("db", None)
    if d: d.close()
@app.errorhandler(ApiError)
def _api(e):
    log(logging.WARNING, "validation_error", error=e.msg); return jsonify(error=e.msg, request_id=g.rid), e.code
@app.errorhandler(Exception)
def _any(e):
    logger.error("unhandled", exc_info=e, extra={"rid": g.get("rid", "-"), "ctx": {"path": request.path}})
    return jsonify(error="Internal error", request_id=g.rid), 500

# ---------- AI layer ----------
def ai_json(task, system, user, fallback):
    t = time.time(); key = os.getenv("ANTHROPIC_API_KEY")
    if not key:
        log(logging.WARNING, "ai_skipped_no_key", task=task); return fallback(), False
    try:
        import anthropic
        r = anthropic.Anthropic(api_key=key, timeout=40, max_retries=1).messages.create(
            model=MODEL, max_tokens=1500, system=system + " Respond with ONLY a valid JSON object, no prose.",
            messages=[{"role": "user", "content": user}])
        out = json.loads(re.search(r"\{.*\}", r.content[0].text, re.S).group(0))
        log(logging.INFO, "ai_call", task=task, model=MODEL, outcome="ok", latency_ms=int((time.time() - t) * 1000)); return out, True
    except Exception as e:
        log(logging.ERROR, "ai_call", task=task, model=MODEL, outcome="failed", latency_ms=int((time.time() - t) * 1000), error=str(e)[:200])
        return fallback(), False

def vdate(s):
    if not s: return ""
    try: date.fromisoformat(s); return s
    except Exception: raise ApiError("Date must be YYYY-MM-DD")
def plist(v): return [p.strip() for p in (v.split(",") if isinstance(v, str) else v or []) if str(p).strip()]
STATUSES, CLOSED = ["open", "in_progress", "done", "dropped"], ["done", "dropped"]

def hist(i, ev): ins("insert into history(item_id,event) values(?,?)", (i, ev))
def item_dict(r):
    d = dict(r); d["history"] = [dict(h) for h in q("select event,ts from history where item_id=? order by id", (r["id"],))]
    d["overdue"] = bool(r["due"] and r["status"] not in CLOSED and r["due"] < date.today().isoformat()); return d
def mtg(i):
    m = q("select * from meetings where id=?", (i,), one=True)
    if not m: raise ApiError("Meeting not found", 404)
    d = dict(m)
    for k in ("participants", "decisions", "links"): d[k] = json.loads(d[k] or "[]")
    d["items"] = [item_dict(r) for r in q("select * from items where meeting_id=? order by id", (i,))]
    d["links"] = [dict(l, title=(q("select title from docs where id=?", (l["doc_id"],), one=True) or {"title": "?"})["title"]) for l in d["links"]]
    return d
def add_item(mid, title, owner, due):
    i = ins("insert into items(meeting_id,title,owner,due) values(?,?,?,?)", (mid, title, owner or "Unassigned", due)); hist(i, f"created (owner {owner or 'Unassigned'})")
    log(logging.INFO, "item_created", item_id=i, meeting_id=mid, owner=owner); return i

# ---------- routes ----------
@app.get("/")
def index(): return render_template("index.html")

@app.get("/api/meetings")
def list_meetings():
    return jsonify([dict(r) | {"open_items": q("select count(*) c from items where meeting_id=? and status in ('open','in_progress')", (r["id"],), one=True)["c"]}
                    for r in q("select id,title,kind,status,meeting_date,participants from meetings order by id desc")])

@app.post("/api/meetings")
def create_meeting():
    d = request.get_json(silent=True) or {}; t = (d.get("title") or "").strip()
    if not t or len(t) > 200: raise ApiError("Title is required (max 200 chars)")
    if d.get("kind", "meeting") not in ("meeting", "handover"): raise ApiError("kind must be meeting or handover")
    i = ins("insert into meetings(title,kind,agenda,participants,notes,meeting_date) values(?,?,?,?,?,?)",
            (t, d.get("kind", "meeting"), d.get("agenda", ""), json.dumps(plist(d.get("participants"))), d.get("notes", ""), vdate(d.get("meeting_date")) or date.today().isoformat()))
    log(logging.INFO, "meeting_created", meeting_id=i, kind=d.get("kind", "meeting")); return jsonify(mtg(i)), 201

@app.get("/api/meetings/<int:i>")
def get_meeting(i): return jsonify(mtg(i))

@app.put("/api/meetings/<int:i>")
def edit_meeting(i):
    m = mtg(i); d = request.get_json(silent=True) or {}
    if d.get("status", m["status"]) not in ("planned", "held", "closed"): raise ApiError("Invalid status")
    q("update meetings set title=?,agenda=?,participants=?,notes=?,summary=?,status=?,meeting_date=? where id=?",
      (d.get("title", m["title"]), d.get("agenda", m["agenda"]), json.dumps(plist(d.get("participants", m["participants"]))), d.get("notes", m["notes"]),
       d.get("summary", m["summary"]), d.get("status", m["status"]), vdate(d.get("meeting_date", m["meeting_date"])), i))
    if d.get("status") and d["status"] != m["status"]: log(logging.INFO, "meeting_status_changed", meeting_id=i, frm=m["status"], to=d["status"])
    return jsonify(mtg(i))

def fb_recap(raw, parts):
    lines = [l.strip(" -*\t") for l in raw.splitlines() if l.strip()]; dec, act = [], []
    for l in lines:
        body = l.split(":", 1)[-1].strip()
        if re.match(r"(?i)decision\s*[:\-]", l): dec.append(body)
        elif re.match(r"(?i)(action|todo|task)\s*[:\-]", l):
            o = re.search(r"@(\w+)", body); dd = re.search(r"\d{4}-\d{2}-\d{2}", body)
            act.append({"title": re.sub(r"@\w+|\d{4}-\d{2}-\d{2}", "", body).strip(), "owner": o.group(1) if o else "Unassigned", "due": dd.group(0) if dd else None})
    return {"summary": " ".join(lines[:2])[:400], "decisions": dec, "action_items": act}

@app.post("/api/meetings/<int:i>/recap")
def recap(i):
    m = mtg(i); raw = ((request.get_json(silent=True) or {}).get("raw") or m["notes"] or "").strip()
    if len(raw) < 20: raise ApiError("Provide at least 20 characters of notes/transcript/email text")
    sysmsg = ('You turn raw meeting notes, chats or emails into a structured recap. Schema: {"summary": str (max 80 words), "decisions": [str], '
              '"action_items": [{"title": str, "owner": str (use a participant name or "Unassigned"), "due": "YYYY-MM-DD" or null}]}. Never invent facts.')
    out, used = ai_json("recap", sysmsg, f"Today: {date.today()}\nParticipants: {m['participants']}\nRaw input:\n{raw[:12000]}", lambda: fb_recap(raw, m["participants"]))
    try:
        summ, decs = str(out.get("summary", ""))[:1000], [str(x) for x in out.get("decisions", [])][:30]; acts = out.get("action_items", [])[:40]
    except Exception: out, used = fb_recap(raw, m["participants"]), False; summ, decs, acts = out["summary"], out["decisions"], out["action_items"]
    q("update meetings set notes=?,summary=?,decisions=?,status='held' where id=?", (raw, summ, json.dumps(decs), i))
    for a in acts:
        try: due = vdate(a.get("due") or "")
        except ApiError: due = ""
        if str(a.get("title", "")).strip(): add_item(i, str(a["title"]).strip()[:300], str(a.get("owner") or "Unassigned")[:60], due)
    log(logging.INFO, "recap_applied", meeting_id=i, ai_used=used, decisions=len(decs), items=len(acts))
    return jsonify(mtg(i) | {"ai_used": used})

@app.post("/api/meetings/<int:i>/items")
def new_item(i):
    mtg(i); d = request.get_json(silent=True) or {}
    if not (d.get("title") or "").strip(): raise ApiError("Item title is required")
    add_item(i, d["title"].strip()[:300], (d.get("owner") or "").strip(), vdate(d.get("due"))); return jsonify(mtg(i)), 201

@app.patch("/api/items/<int:i>")
def patch_item(i):
    it = q("select * from items where id=?", (i,), one=True)
    if not it: raise ApiError("Item not found", 404)
    d = request.get_json(silent=True) or {}; ch = {}
    if "status" in d:
        if d["status"] not in STATUSES: raise ApiError("Invalid status")
        ch["status"] = d["status"]
    if "owner" in d:
        if not str(d["owner"]).strip(): raise ApiError("Owner cannot be empty")
        ch["owner"] = str(d["owner"]).strip()
    if "due" in d: ch["due"] = vdate(d["due"])
    if "outcome" in d: ch["outcome"] = str(d["outcome"]).strip()[:1000]
    if ch.get("status", it["status"]) in CLOSED and not ch.get("outcome", it["outcome"]): raise ApiError("Record an outcome before closing an item")
    for k, v in ch.items():
        if v != it[k]:
            hist(i, f"{k}: {it[k] or '-'} -> {v}")
            if k == "owner": log(logging.INFO, "item_reassigned", item_id=i, frm=it[k], to=v)
            if k == "status" and v in CLOSED: log(logging.INFO, "item_closed", item_id=i, status=v)
    if ch: q(f"update items set {','.join(k + '=?' for k in ch)} where id=?", (*ch.values(), i))
    return jsonify(item_dict(q("select * from items where id=?", (i,), one=True)))

@app.get("/api/items")
def items():
    sql, a = "select * from items where 1=1", []
    if request.args.get("owner"): sql += " and lower(owner)=lower(?)"; a.append(request.args["owner"])
    if request.args.get("status"): sql += " and status=?"; a.append(request.args["status"])
    return jsonify([item_dict(r) for r in q(sql + " order by due is null, due", a)])

@app.post("/api/meetings/<int:i>/carry")
def carry(i):
    m = mtg(i); d = request.get_json(silent=True) or {}
    open_items = [x for x in m["items"] if x["status"] not in CLOSED]
    n = ins("insert into meetings(title,kind,agenda,participants,meeting_date,prev_id) values(?,?,?,?,?,?)",
            ((d.get("title") or f"Follow-up: {m['title']}")[:200], m["kind"], "Carried-forward open items:\n" + "\n".join("- " + x["title"] for x in open_items),
             json.dumps(m["participants"]), vdate(d.get("meeting_date")) or date.today().isoformat(), i))
    for x in open_items:
        q("update items set meeting_id=? where id=?", (n, x["id"])); hist(x["id"], f"carried forward from meeting #{i} to #{n}")
    log(logging.INFO, "items_carried_forward", from_meeting=i, to_meeting=n, count=len(open_items)); return jsonify(mtg(n)), 201

# ---------- knowledge base ----------
@app.get("/api/docs")
def docs(): return jsonify([dict(r) for r in q("select * from docs order by id desc")])
@app.post("/api/docs")
def add_doc():
    d = request.get_json(silent=True) or {}
    if not (d.get("title") or "").strip() or len((d.get("content") or "").strip()) < 10: raise ApiError("Title and content (min 10 chars) required")
    i = ins("insert into docs(title,content) values(?,?)", (d["title"].strip()[:200], d["content"].strip())); log(logging.INFO, "doc_created", doc_id=i); return jsonify(id=i), 201

def toks(s): return {w for w in re.findall(r"[a-z]{4,}", (s or "").lower())}
@app.post("/api/meetings/<int:i>/link")
def link(i):
    m = mtg(i); ds = [dict(r) for r in q("select id,title,content from docs")]
    text = " ".join([m["title"], m["agenda"] or "", m["summary"] or "", " ".join(m["decisions"]), " ".join(x["title"] for x in m["items"])])
    def fb():
        sc = sorted(((len(toks(text) & toks(d["title"] + " " + d["content"])), d) for d in ds), key=lambda x: -x[0])
        return {"links": [{"doc_id": d["id"], "reason": "keyword overlap"} for s, d in sc[:3] if s > 0]}
    out, used = ai_json("link_docs", 'Pick knowledge-base documents relevant to the meeting. Schema: {"links":[{"doc_id": int, "reason": str}]} (max 4, only truly relevant).',
                        f"Meeting:\n{text[:4000]}\n\nDocuments:\n" + "\n".join(f"[{d['id']}] {d['title']}: {d['content'][:300]}" for d in ds), fb)
    ids = {d["id"] for d in ds}; ls = [{"doc_id": int(l["doc_id"]), "reason": str(l.get("reason", ""))[:200]} for l in out.get("links", []) if str(l.get("doc_id")).isdigit() and int(l["doc_id"]) in ids]
    q("update meetings set links=? where id=?", (json.dumps(ls), i)); log(logging.INFO, "docs_linked", meeting_id=i, count=len(ls), ai_used=used)
    return jsonify(mtg(i) | {"ai_used": used})

# ---------- catch-up brief ----------
@app.get("/api/catchup")
def catchup():
    p = (request.args.get("person") or "").strip()
    if not p: raise ApiError("person is required")
    ms = [mtg(r["id"]) for r in q("select id from meetings order by id desc limit 5")]
    mine = [item_dict(r) for r in q("select * from items where lower(owner)=lower(?) and status in ('open','in_progress')", (p,))]
    ctx = "\n".join(f"#{m['id']} {m['title']} ({m['meeting_date']}, {m['status']}) summary: {m['summary']} decisions: {m['decisions']}" for m in ms)
    def fb(): return {"brief": "Recent meetings:\n" + ctx, "your_open_items": [x["title"] for x in mine], "watch_out": [x["title"] for x in mine if x["overdue"]]}
    out, used = ai_json("catchup", 'Write a short catch-up brief for a person who missed meetings or is taking over work. Schema: {"brief": str (max 120 words), "your_open_items": [str], "watch_out": [str]}. Be factual; use only the context.',
                        f"Person: {p}\nToday: {date.today()}\nRecent meetings:\n{ctx}\nTheir open items: {json.dumps([{'t': x['title'], 'due': x['due'], 'overdue': x['overdue']} for x in mine])}", fb)
    log(logging.INFO, "catchup_generated", person=p, ai_used=used); return jsonify(out | {"ai_used": used, "items": mine})

# ---------- SPECIAL FEATURE: Follow-up Autopilot ----------
@app.get("/api/nudges")
def nudges():
    soon = date.fromordinal(date.today().toordinal() + 2).isoformat(); by = {}
    for r in q("select * from items where status in ('open','in_progress') and due is not null and due!='' and due<=?", (soon,)):
        by.setdefault(r["owner"], []).append({"title": r["title"], "due": r["due"], "overdue": r["due"] < date.today().isoformat()})
    if not by: return jsonify(messages=[], ai_used=False)
    def fb(): return {"messages": [{"owner": o, "message": f"Hi {o}, quick reminder on: " + "; ".join(f"{x['title']} (due {x['due']})" for x in xs) + ". Please update status or record the outcome."} for o, xs in by.items()]}
    out, used = ai_json("nudges", 'Draft one short, friendly, specific follow-up message per owner about overdue/due-soon items. Schema: {"messages":[{"owner": str, "message": str}]}', json.dumps(by), fb)
    log(logging.INFO, "nudges_generated", owners=len(by), ai_used=used); return jsonify(out | {"ai_used": used, "raw": by})

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", 5000)))
