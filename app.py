import os
import hmac
import datetime as dt
import psycopg2
import psycopg2.extras
from dotenv import load_dotenv
from flask import Flask, jsonify, request, render_template, redirect, session

load_dotenv()
app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "dev-only-change-me")
app.permanent_session_lifetime = dt.timedelta(days=30)
PASSWORD = os.environ.get("APP_PASSWORD", "")

BASE = 29000  # expected monthly amount
BUDGET = {
    "Tithe": 3500, "Rent": 8000, "Personal Shopping": 2700, "Maintenance": 2000,
    "Food Shopping": 3000, "Daily Food": 2500, "Saving": 3000,
    "Cat Food": 1500, "WiFi": 720,
}
BUDGET["Cash at Hand"] = BASE - sum(BUDGET.values())  # the rest = 2,080


def run(sql, args=(), fetch=True):
    conn = psycopg2.connect(os.environ["DATABASE_URL"], cursor_factory=psycopg2.extras.RealDictCursor)
    try:
        with conn, conn.cursor() as cur:
            cur.execute(sql, args)
            return cur.fetchall() if fetch else None
    finally:
        conn.close()


_ready = False


def init_db():
    global _ready
    if _ready:
        return
        run("""
        CREATE TABLE IF NOT EXISTS expenses(
          id SERIAL PRIMARY KEY, month CHAR(7) NOT NULL, category TEXT NOT NULL,
          amount NUMERIC(12,2) NOT NULL, note TEXT DEFAULT '', spent_on DATE NOT NULL);
        CREATE TABLE IF NOT EXISTS income(
          id SERIAL PRIMARY KEY, month CHAR(7) NOT NULL,
          amount NUMERIC(12,2) NOT NULL, note TEXT DEFAULT '', received_on DATE NOT NULL);
        """, fetch=False)
    _ready = True


@app.before_request
def gate():
    if request.endpoint in ("login", "static"):
        return None
    if PASSWORD and not session.get("ok"):
        if request.path.startswith("/api/"):
            return jsonify(error="Please log in"), 401
        return redirect("/login")
    init_db()


@app.route("/login", methods=["GET", "POST"])
def login():
    if not PASSWORD:
        return redirect("/")
    err = None
    if request.method == "POST":
        if hmac.compare_digest(request.form.get("password", "").encode(), PASSWORD.encode()):
            session.permanent = True
            session["ok"] = True
            return redirect("/")
        err = "That password isn't right, try again"
    return render_template("login.html", err=err)


@app.get("/logout")
def logout():
    session.clear()
    return redirect("/login")


def status(spent, budget):
    if spent < budget:
        return "good", f"Yay queen! Under budget by KES {budget - spent:,.0f}. You're crushing it!"
    if spent > budget:
        return "bad", f"Warning: Over budget by KES {spent - budget:,.0f}. Time to slow down, babe."
    return "par", "Right on the line. You could do better!"


@app.get("/")
def home():
    return render_template("index.html", categories=list(BUDGET))


@app.get("/api/summary")
def summary():
    m = request.args.get("month") or dt.date.today().strftime("%Y-%m")
    extra = float(run("SELECT COALESCE(SUM(amount),0) s FROM income WHERE month=%s", (m,))[0]["s"])
    budget = dict(BUDGET)
    budget["Cash at Hand"] += extra
    spent = {k: 0.0 for k in budget}
    for r in run("SELECT category, SUM(amount) s FROM expenses WHERE month=%s GROUP BY category", (m,)):
        spent[r["category"]] = float(r["s"])
    tb, ts = BASE + extra, sum(spent.values())
    cats = [{"name": k, "budget": budget[k], "pct": round(budget[k] / tb * 100, 1), "spent": spent[k],
             "spent_pct": round(spent[k] / ts * 100, 1) if ts else 0,
             "status": status(spent[k], budget[k])[0]} for k in budget]

    ex = {r["month"]: float(r["s"]) for r in run("SELECT month, SUM(amount) s FROM expenses GROUP BY month")}
    inc = {r["month"]: float(r["s"]) for r in run("SELECT month, SUM(amount) s FROM income GROUP BY month")}
    months = sorted(set(ex) | {m})
    traj = [{"month": x, "spent": ex.get(x, 0), "budget": BASE + inc.get(x, 0)} for x in months][-6:]
    trend = None
    if len(traj) > 1:
        d = traj[-1]["spent"] - traj[-2]["spent"]
        trend = ("Spending is up" if d > 0 else "Spending is down" if d < 0 else "Same as last month") \
            + f" by KES {abs(d):,.0f} vs last month"

    spent_cats = [c for c in cats if c["spent"] > 0]
    big = run("SELECT category, amount, note FROM expenses WHERE month=%s ORDER BY amount DESC LIMIT 1", (m,))
    year, mon = map(int, m.split("-"))
    today = dt.date.today()
    days = (today.day if (today.year, today.month) == (year, mon)
            else (dt.date(year + mon // 12, mon % 12 + 1, 1) - dt.timedelta(days=1)).day)
    report = {
        "highest": max(spent_cats, key=lambda c: c["spent"]) if spent_cats else None,
        "lowest": min(spent_cats, key=lambda c: c["spent"]) if spent_cats else None,
        "biggest": big[0] if big else None,
        "avg_day": round(ts / days, 2) if days else 0,
        "count": len(run("SELECT id FROM expenses WHERE month=%s", (m,))),
        "left": tb - ts,
        "saved": spent["Saving"],
    }
    if report["biggest"]:
        report["biggest"]["amount"] = float(report["biggest"]["amount"])
    tx = run("""SELECT id,'expense' kind,category,amount::float,note,spent_on d FROM expenses WHERE month=%s
                UNION ALL SELECT id,'income','Extra cash',amount::float,note,received_on FROM income WHERE month=%s
                ORDER BY d DESC, id DESC""", (m,m))
    for t in tx:
        t["d"] = t["d"].isoformat()
    s, msg = status(ts, tb)
    return jsonify(month=m, base=BASE, extra=extra, total_budget=tb, total_spent=ts, cats=cats,
                   status=s, message=msg, traj=traj, trend=trend, report=report, tx=tx)


def parse(kind):
    d = request.get_json()
    amount = float(d.get("amount", 0))
    day = d.get("date") or dt.date.today().isoformat()
    if amount <= 0:
        raise ValueError("Amount must be above 0")
    dt.date.fromisoformat(day)
    return d, amount, day


@app.post("/api/expense")
def add_expense():
    try:
        d, amount, day = parse("expense")
        if d.get("category") not in BUDGET:
            raise ValueError("Pick a category")
    except (ValueError, TypeError) as e:
        return jsonify(error=str(e)), 400
    run("INSERT INTO expenses(month,category,amount,note,spent_on) VALUES(%s,%s,%s,%s,%s)",
        (day[:7], d["category"], amount, d.get("note", ""), day), fetch=False)
    return jsonify(ok=True)


@app.post("/api/income")
def add_income():
    try:
        d, amount, day = parse("income")
    except (ValueError, TypeError) as e:
        return jsonify(error=str(e)), 400
    run("INSERT INTO income(month,amount,note,received_on) VALUES(%s,%s,%s,%s)",
        (day[:7], amount, d.get("note", ""), day), fetch=False)
    return jsonify(ok=True)


@app.delete("/api/<kind>/<int:rid>")
def delete(kind, rid):
    table = {"expense": "expenses", "income": "income"}.get(kind)
    if not table:
        return jsonify(error="bad kind"), 400
    run(f"DELETE FROM {table} WHERE id=%s", (rid,), fetch=False)
    return jsonify(ok=True)


if __name__ == "__main__":
    app.run(debug=True)
