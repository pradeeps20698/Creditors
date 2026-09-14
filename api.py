"""
Swift Party Reference — JSON API (zero external deps, stdlib only)
Serves `swift_party_ref` data from AWS RDS Postgres as JSON.

Run:  python3 api.py     ->  http://localhost:8501/

Endpoints
  GET /                      API index / help
  GET /health                { "status": "ok" }
  GET /data                  rows as JSON  (query params below)
  GET /summary               totals grouped by category + office
  GET /aging                 outstanding grouped into aging buckets

Query params (for /data and /summary):
  from=YYYY-MM-DD            filter ref_date >= from   (default 2026-04-01)
  to=YYYY-MM-DD              filter ref_date <= to     (default today)
  office=Gurgaon            filter by office (exact)
  category=payables|receivables
  active=true|false          filter is_active
  limit=1000                 max rows for /data (default 1000, 0 = all)
"""
import os
import json
from datetime import datetime, date, timezone
from decimal import Decimal
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

import psycopg2
from psycopg2.extras import RealDictCursor
from dotenv import load_dotenv

load_dotenv()

DB = dict(
    host=os.getenv("PGHOST"),
    user=os.getenv("PGUSER"),
    password=os.getenv("PGPASSWORD"),
    port=os.getenv("PGPORT", "5432"),
    dbname=os.getenv("PGDATABASE", "postgres"),
)
PORT = 8501
DEFAULT_FROM = "2026-04-01"

CATEGORY_SQL = {
    "payables": "Current Liabilities",
    "receivables": "Current Assets",
}


def connect():
    return psycopg2.connect(**DB, connect_timeout=15)


def jsonify(o):
    if isinstance(o, Decimal):
        return float(o)
    if isinstance(o, (datetime, date)):
        return o.isoformat()
    raise TypeError(str(type(o)))


def build_where(qs):
    """Return (where_sql, params) from query params."""
    where, params = [], []
    frm = qs.get("from", [DEFAULT_FROM])[0]
    to = qs.get("to", [date.today().isoformat()])[0]
    where.append("ref_date >= %s"); params.append(frm)
    where.append("ref_date < (%s::date + INTERVAL '1 day')"); params.append(to)
    if "office" in qs:
        where.append("office = %s"); params.append(qs["office"][0])
    if "category" in qs:
        at = CATEGORY_SQL.get(qs["category"][0].lower())
        if at:
            where.append("account_type = %s"); params.append(at)
    if "active" in qs:
        where.append("is_active = %s"); params.append(qs["active"][0].lower() == "true")
    return " WHERE " + " AND ".join(where), params


class Handler(BaseHTTPRequestHandler):
    def _send(self, payload, code=200):
        body = json.dumps(payload, default=jsonify, indent=2).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):  # quieter logs
        pass

    def do_GET(self):
        u = urlparse(self.path)
        path, qs = u.path.rstrip("/") or "/", parse_qs(u.query)
        try:
            if path == "/":
                return self._send({
                    "service": "swift_party_ref API",
                    "endpoints": {
                        "/data": "rows as JSON (params: from,to,office,category,active,limit)",
                        "/summary": "totals by category & office",
                        "/aging": "outstanding by aging bucket",
                        "/health": "liveness check",
                    },
                    "defaults": {"from": DEFAULT_FROM, "to": date.today().isoformat()},
                })
            if path == "/health":
                return self._send({"status": "ok", "time": datetime.now(timezone.utc)})

            where, params = build_where(qs)

            if path == "/data":
                limit = int(qs.get("limit", ["1000"])[0])
                lim_sql = "" if limit == 0 else f" LIMIT {limit}"
                sql = f"SELECT * FROM swift_party_ref{where} ORDER BY ref_date{lim_sql}"
                with connect() as c, c.cursor(cursor_factory=RealDictCursor) as cur:
                    cur.execute(sql, params)
                    rows = cur.fetchall()
                return self._send({"count": len(rows), "limit": limit,
                                   "filters": qs, "data": rows})

            if path == "/summary":
                sql = f"""
                    SELECT account_type, office,
                           COUNT(*) AS rows,
                           SUM(ref_amount) AS ref_amount,
                           SUM(amount_paid) AS amount_paid,
                           SUM(bal_amount) AS bal_amount
                    FROM swift_party_ref{where}
                    GROUP BY account_type, office
                    ORDER BY account_type, office"""
                with connect() as c, c.cursor(cursor_factory=RealDictCursor) as cur:
                    cur.execute(sql, params)
                    return self._send({"summary": cur.fetchall()})

            if path == "/aging":
                sql = f"""
                    SELECT CASE
                             WHEN due_date IS NULL THEN 'No due date'
                             WHEN due_date >= CURRENT_DATE THEN 'Not due'
                             WHEN CURRENT_DATE - due_date::date <= 30 THEN '1-30 days'
                             WHEN CURRENT_DATE - due_date::date <= 60 THEN '31-60 days'
                             WHEN CURRENT_DATE - due_date::date <= 90 THEN '61-90 days'
                             ELSE '90+ days' END AS bucket,
                           COUNT(*) AS rows,
                           SUM(ABS(bal_amount)) AS outstanding
                    FROM swift_party_ref{where}
                    GROUP BY bucket ORDER BY outstanding DESC"""
                with connect() as c, c.cursor(cursor_factory=RealDictCursor) as cur:
                    cur.execute(sql, params)
                    return self._send({"aging": cur.fetchall()})

            self._send({"error": "not found", "path": path}, 404)
        except Exception as e:
            self._send({"error": str(e)}, 500)


if __name__ == "__main__":
    print(f"Swift Party Ref API  ->  http://localhost:{PORT}/")
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()
