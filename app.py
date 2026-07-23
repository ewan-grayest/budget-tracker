#!/usr/bin/env python3
import base64
import hmac
import html
import json
import os
import re
import secrets
import sqlite3
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlencode, urlparse

APP_NAME = "Budget Control"
DB_PATH = os.getenv("DB_PATH", "/data/budget.db")
HOST = os.getenv("HOST", "0.0.0.0")
PORT = int(os.getenv("PORT", "8080"))
APP_USER = os.getenv("APP_USER", "")
APP_PASSWORD = os.getenv("APP_PASSWORD", "")
SEED_DEMO = os.getenv("SEED_DEMO", "1") == "1"


def db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    return conn


def init_db():
    os.makedirs(os.path.dirname(DB_PATH) or ".", exist_ok=True)
    with db() as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS budget_lines (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                code TEXT NOT NULL UNIQUE,
                name TEXT NOT NULL,
                fiscal_year INTEGER NOT NULL,
                holder_name TEXT NOT NULL,
                holder_email TEXT,
                cost_center TEXT,
                wbs TEXT,
                cost_element TEXT,
                currency TEXT NOT NULL DEFAULT 'EUR',
                initial_approved_cents INTEGER NOT NULL CHECK(initial_approved_cents >= 0),
                initial_released_cents INTEGER NOT NULL CHECK(initial_released_cents >= 0),
                created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS budget_operations (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                operation_type TEXT NOT NULL,
                source_budget_id INTEGER,
                target_budget_id INTEGER,
                amount_cents INTEGER NOT NULL CHECK(amount_cents > 0),
                approved_delta_source INTEGER NOT NULL DEFAULT 0,
                released_delta_source INTEGER NOT NULL DEFAULT 0,
                approved_delta_target INTEGER NOT NULL DEFAULT 0,
                released_delta_target INTEGER NOT NULL DEFAULT 0,
                note TEXT,
                created_by TEXT NOT NULL,
                created_at TEXT NOT NULL,
                FOREIGN KEY(source_budget_id) REFERENCES budget_lines(id),
                FOREIGN KEY(target_budget_id) REFERENCES budget_lines(id)
            );

            CREATE TABLE IF NOT EXISTS purchase_orders (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                number TEXT NOT NULL UNIQUE,
                budget_id INTEGER NOT NULL,
                vendor TEXT NOT NULL,
                description TEXT NOT NULL,
                amount_cents INTEGER NOT NULL CHECK(amount_cents > 0),
                status TEXT NOT NULL CHECK(status IN ('DRAFT','APPROVED','CLOSED','CANCELLED')),
                created_at TEXT NOT NULL,
                FOREIGN KEY(budget_id) REFERENCES budget_lines(id)
            );

            CREATE TABLE IF NOT EXISTS expenses (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                budget_id INTEGER NOT NULL,
                po_id INTEGER,
                expense_date TEXT NOT NULL,
                invoice_no TEXT,
                description TEXT NOT NULL,
                amount_cents INTEGER NOT NULL CHECK(amount_cents > 0),
                created_at TEXT NOT NULL,
                FOREIGN KEY(budget_id) REFERENCES budget_lines(id),
                FOREIGN KEY(po_id) REFERENCES purchase_orders(id)
            );
            """
        )
        count = conn.execute("SELECT COUNT(*) FROM budget_lines").fetchone()[0]
        if SEED_DEMO and count == 0:
            now = datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
            conn.execute(
                """INSERT INTO budget_lines
                (code,name,fiscal_year,holder_name,holder_email,cost_center,wbs,cost_element,currency,
                 initial_approved_cents,initial_released_cents,created_at)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
                ("IT-OPS-2026", "IT Operations", 2026, "Budget Holder", "holder@example.com",
                 "CC-IT", "WBS-IT-OPS", "IT Services", "EUR", 10000000, 10000000, now),
            )
            budget_id = conn.execute("SELECT id FROM budget_lines WHERE code='IT-OPS-2026'").fetchone()[0]
            conn.execute(
                """INSERT INTO purchase_orders
                (number,budget_id,vendor,description,amount_cents,status,created_at)
                VALUES (?,?,?,?,?,?,?)""",
                ("PO-2026-0001", budget_id, "Example Vendor", "Infrastructure support, limit PO", 2500000, "APPROVED", now),
            )
            po_id = conn.execute("SELECT id FROM purchase_orders WHERE number='PO-2026-0001'").fetchone()[0]
            conn.execute(
                """INSERT INTO expenses
                (budget_id,po_id,expense_date,invoice_no,description,amount_cents,created_at)
                VALUES (?,?,?,?,?,?,?)""",
                (budget_id, po_id, date.today().isoformat(), "INV-DEMO-001", "Monthly support services", 700000, now),
            )


def money_to_cents(value):
    text = (value or "").strip().replace(" ", "").replace(",", ".")
    try:
        amount = Decimal(text).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    except (InvalidOperation, ValueError):
        raise ValueError("Некорректная сумма")
    if amount <= 0:
        raise ValueError("Сумма должна быть больше нуля")
    return int(amount * 100)


def fmt_money(cents, currency="EUR"):
    value = Decimal(int(cents)) / 100
    return f"{value:,.2f}".replace(",", " ") + f" {html.escape(currency)}"


def budget_metrics(conn, budget_id):
    row = conn.execute("SELECT * FROM budget_lines WHERE id=?", (budget_id,)).fetchone()
    if not row:
        return None
    op = conn.execute(
        """SELECT
           COALESCE(SUM(CASE WHEN source_budget_id=? THEN approved_delta_source ELSE 0 END),0) +
           COALESCE(SUM(CASE WHEN target_budget_id=? THEN approved_delta_target ELSE 0 END),0) AS approved_delta,
           COALESCE(SUM(CASE WHEN source_budget_id=? THEN released_delta_source ELSE 0 END),0) +
           COALESCE(SUM(CASE WHEN target_budget_id=? THEN released_delta_target ELSE 0 END),0) AS released_delta
           FROM budget_operations""",
        (budget_id, budget_id, budget_id, budget_id),
    ).fetchone()
    actuals = conn.execute(
        "SELECT COALESCE(SUM(amount_cents),0) FROM expenses WHERE budget_id=?", (budget_id,)
    ).fetchone()[0]
    commitments = conn.execute(
        """SELECT COALESCE(SUM(MAX(po.amount_cents - COALESCE(e.spent,0),0)),0)
           FROM purchase_orders po
           LEFT JOIN (SELECT po_id, SUM(amount_cents) spent FROM expenses WHERE po_id IS NOT NULL GROUP BY po_id) e
             ON e.po_id=po.id
           WHERE po.budget_id=? AND po.status='APPROVED'""",
        (budget_id,),
    ).fetchone()[0]
    approved = row["initial_approved_cents"] + op["approved_delta"]
    released = row["initial_released_cents"] + op["released_delta"]
    available = released - actuals - commitments
    return {
        "row": row,
        "approved": approved,
        "released": released,
        "actuals": actuals,
        "commitments": commitments,
        "available": available,
    }


def all_budget_metrics(conn):
    rows = conn.execute("SELECT id FROM budget_lines ORDER BY fiscal_year DESC, code").fetchall()
    return [budget_metrics(conn, r["id"]) for r in rows]


def esc(value):
    return html.escape(str(value or ""))


CSS = r"""
:root { --bg:#f4f6f8; --panel:#fff; --text:#18212b; --muted:#64748b; --line:#dbe2ea; --accent:#2457d6; --good:#137a4f; --warn:#9a6700; --bad:#b42318; }
*{box-sizing:border-box} body{margin:0;font-family:Inter,Segoe UI,Arial,sans-serif;background:var(--bg);color:var(--text)}
a{color:var(--accent);text-decoration:none} a:hover{text-decoration:underline}
header{background:#111827;color:#fff;padding:0 24px}.top{max-width:1280px;margin:auto;display:flex;align-items:center;justify-content:space-between;min-height:62px}
.brand{font-weight:700}.nav{display:flex;gap:18px;flex-wrap:wrap}.nav a{color:#dbeafe}.container{max-width:1280px;margin:24px auto;padding:0 18px}
.grid{display:grid;gap:16px}.cards{grid-template-columns:repeat(auto-fit,minmax(190px,1fr))}.card,.panel{background:var(--panel);border:1px solid var(--line);border-radius:10px;padding:18px;box-shadow:0 1px 2px rgba(0,0,0,.03)}
.metric{font-size:25px;font-weight:700;margin-top:8px}.label{color:var(--muted);font-size:13px}.good{color:var(--good)}.bad{color:var(--bad)}.warn{color:var(--warn)}
h1{font-size:26px;margin:0 0 18px}h2{font-size:19px;margin:0 0 14px}h3{font-size:16px;margin:0 0 10px}
table{width:100%;border-collapse:collapse;background:#fff}th,td{text-align:left;border-bottom:1px solid var(--line);padding:11px 10px;vertical-align:top}th{font-size:12px;text-transform:uppercase;color:var(--muted);background:#f8fafc}
.table-wrap{overflow-x:auto;border:1px solid var(--line);border-radius:10px}.toolbar{display:flex;justify-content:space-between;gap:12px;align-items:center;margin-bottom:14px;flex-wrap:wrap}
form.inline{display:inline}.form-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(210px,1fr));gap:12px}.full{grid-column:1/-1}
label{display:block;font-size:13px;color:#475569;margin-bottom:5px}input,select,textarea{width:100%;padding:10px;border:1px solid #cbd5e1;border-radius:7px;background:#fff;font:inherit}textarea{min-height:76px;resize:vertical}
button,.button{display:inline-block;border:0;border-radius:7px;padding:10px 14px;background:var(--accent);color:#fff;font-weight:600;cursor:pointer}.button.secondary,button.secondary{background:#475569}.button.danger,button.danger{background:var(--bad)}
.badge{display:inline-block;padding:4px 8px;border-radius:999px;font-size:12px;font-weight:700;background:#e2e8f0}.badge.APPROVED{background:#dcfce7;color:#166534}.badge.DRAFT{background:#fef3c7;color:#92400e}.badge.CLOSED{background:#e0e7ff;color:#3730a3}.badge.CANCELLED{background:#fee2e2;color:#991b1b}
.flash{padding:12px 14px;border-radius:8px;margin-bottom:16px;background:#dbeafe;color:#1e3a8a}.flash.error{background:#fee2e2;color:#991b1b}.muted{color:var(--muted)}.small{font-size:12px}.split{display:grid;grid-template-columns:2fr 1fr;gap:16px}@media(max-width:850px){.split{grid-template-columns:1fr}.nav{gap:10px}.top{align-items:flex-start;padding:14px 0;flex-direction:column}}
.progress{height:9px;background:#e2e8f0;border-radius:999px;overflow:hidden}.progress>span{display:block;height:100%;background:var(--accent)}.footer{color:var(--muted);font-size:12px;margin:26px 0}
"""


class AppHandler(BaseHTTPRequestHandler):
    server_version = "BudgetControl/1.0"

    def log_message(self, fmt, *args):
        print(f"{self.client_address[0]} - {fmt % args}")

    def _authorized(self):
        if not APP_USER:
            return True
        header = self.headers.get("Authorization", "")
        if not header.startswith("Basic "):
            return False
        try:
            decoded = base64.b64decode(header[6:]).decode("utf-8")
            user, password = decoded.split(":", 1)
            return hmac.compare_digest(user, APP_USER) and hmac.compare_digest(password, APP_PASSWORD)
        except Exception:
            return False

    def _require_auth(self):
        if self._authorized():
            return True
        self.send_response(401)
        self.send_header("WWW-Authenticate", 'Basic realm="Budget Control"')
        self.end_headers()
        return False

    def csrf_token(self):
        cached = getattr(self, "_csrf_cache", None)
        if cached:
            return cached
        cookie = self.headers.get("Cookie", "")
        for part in cookie.split(";"):
            key, _, value = part.strip().partition("=")
            if key == "csrf_token" and re.fullmatch(r"[A-Za-z0-9_-]{32,128}", value or ""):
                self._csrf_cache = (value, False)
                return self._csrf_cache
        self._csrf_cache = (secrets.token_urlsafe(32), True)
        return self._csrf_cache

    def parse_post(self):
        length = int(self.headers.get("Content-Length", "0"))
        if length > 1_000_000:
            raise ValueError("Слишком большой запрос")
        body = self.rfile.read(length).decode("utf-8")
        data = {k: v[-1] for k, v in parse_qs(body, keep_blank_values=True).items()}
        token, _ = self.csrf_token()
        if not hmac.compare_digest(data.get("csrf_token", ""), token):
            raise ValueError("Ошибка CSRF. Обновите страницу и повторите действие")
        return data

    def redirect(self, path, message=None, error=False):
        if message:
            sep = "&" if "?" in path else "?"
            path += sep + urlencode({"msg": message, "kind": "error" if error else "ok"})
        self.send_response(303)
        self.send_header("Location", path)
        self.end_headers()

    def send_html(self, content, status=200):
        token, is_new = self.csrf_token()
        body = content.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "same-origin")
        if is_new:
            self.send_header("Set-Cookie", f"csrf_token={token}; Path=/; SameSite=Strict; HttpOnly")
        self.end_headers()
        self.wfile.write(body)

    def send_json(self, payload, status=200):
        body = json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def page(self, title, body):
        parsed = urlparse(self.path)
        q = parse_qs(parsed.query)
        flash = ""
        if q.get("msg"):
            cls = "flash error" if q.get("kind", [""])[0] == "error" else "flash"
            flash = f'<div class="{cls}">{esc(q["msg"][0])}</div>'
        return f"""<!doctype html><html lang="ru"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
        <title>{esc(title)} — {APP_NAME}</title><link rel="stylesheet" href="/static/style.css"></head><body>
        <header><div class="top"><div class="brand">{APP_NAME}</div><nav class="nav">
        <a href="/">Обзор</a><a href="/budgets">Бюджеты</a><a href="/pos">PO</a><a href="/expenses">Расходы</a><a href="/operations">Операции</a></nav></div></header>
        <main class="container">{flash}{body}<div class="footer">MVP. Все суммы хранятся в минимальных денежных единицах; операции сохраняются в журнале.</div></main></body></html>"""

    def csrf_input(self):
        token, _ = self.csrf_token()
        return f'<input type="hidden" name="csrf_token" value="{esc(token)}">'

    def do_GET(self):
        if not self._require_auth():
            return
        parsed = urlparse(self.path)
        path = parsed.path
        if path == "/static/style.css":
            body = CSS.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/css; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers(); self.wfile.write(body); return
        if path == "/healthz":
            self.send_json({"status": "ok"}); return
        if path == "/api/summary":
            return self.api_summary()
        if path == "/":
            return self.dashboard()
        if path == "/budgets":
            return self.budgets_page()
        m = re.fullmatch(r"/budgets/(\d+)", path)
        if m:
            return self.budget_detail(int(m.group(1)))
        if path == "/pos":
            return self.pos_page()
        if path == "/expenses":
            return self.expenses_page()
        if path == "/operations":
            return self.operations_page()
        self.send_html(self.page("Не найдено", "<h1>404</h1><p>Страница не найдена.</p>"), 404)

    def do_POST(self):
        if not self._require_auth():
            return
        path = urlparse(self.path).path
        try:
            data = self.parse_post()
            if path == "/budgets/new":
                return self.create_budget(data)
            m = re.fullmatch(r"/budgets/(\d+)/operation", path)
            if m:
                return self.create_operation(int(m.group(1)), data)
            if path == "/pos/new":
                return self.create_po(data)
            m = re.fullmatch(r"/pos/(\d+)/status", path)
            if m:
                return self.change_po_status(int(m.group(1)), data)
            if path == "/expenses/new":
                return self.create_expense(data)
            self.redirect("/", "Неизвестная операция", True)
        except (ValueError, sqlite3.IntegrityError) as exc:
            back = self.headers.get("Referer", "/")
            back_path = urlparse(back).path or "/"
            self.redirect(back_path, str(exc), True)
        except Exception as exc:
            print("ERROR", repr(exc))
            self.redirect("/", "Внутренняя ошибка", True)

    def dashboard(self):
        with db() as conn:
            metrics = all_budget_metrics(conn)
            total_approved = sum(m["approved"] for m in metrics)
            total_released = sum(m["released"] for m in metrics)
            total_actuals = sum(m["actuals"] for m in metrics)
            total_commitments = sum(m["commitments"] for m in metrics)
            total_available = sum(m["available"] for m in metrics)
            currencies = {m["row"]["currency"] for m in metrics}
            currency = next(iter(currencies)) if len(currencies) == 1 else "MIX"
            recent = conn.execute(
                """SELECT e.*, b.code, b.currency, po.number po_number FROM expenses e
                   JOIN budget_lines b ON b.id=e.budget_id LEFT JOIN purchase_orders po ON po.id=e.po_id
                   ORDER BY e.id DESC LIMIT 8"""
            ).fetchall()
        rows = "".join(
            f"<tr><td>{esc(r['expense_date'])}</td><td><a href='/budgets/{r['budget_id']}'>{esc(r['code'])}</a></td>"
            f"<td>{esc(r['description'])}</td><td>{esc(r['po_number'] or 'Без PO')}</td><td>{fmt_money(r['amount_cents'],r['currency'])}</td></tr>"
            for r in recent
        ) or "<tr><td colspan='5' class='muted'>Расходов нет</td></tr>"
        body = f"""<h1>Обзор бюджета</h1><div class="grid cards">
        <div class="card"><div class="label">Утверждено</div><div class="metric">{fmt_money(total_approved,currency)}</div></div>
        <div class="card"><div class="label">Разрешено к использованию</div><div class="metric">{fmt_money(total_released,currency)}</div></div>
        <div class="card"><div class="label">Actuals</div><div class="metric">{fmt_money(total_actuals,currency)}</div></div>
        <div class="card"><div class="label">Commitments</div><div class="metric">{fmt_money(total_commitments,currency)}</div></div>
        <div class="card"><div class="label">Доступно</div><div class="metric {'bad' if total_available < 0 else 'good'}">{fmt_money(total_available,currency)}</div></div>
        </div><br><div class="panel"><h2>Последние расходы</h2><div class="table-wrap"><table><thead><tr><th>Дата</th><th>Бюджет</th><th>Описание</th><th>PO</th><th>Сумма</th></tr></thead><tbody>{rows}</tbody></table></div></div>"""
        self.send_html(self.page("Обзор", body))

    def budgets_page(self):
        with db() as conn:
            metrics = all_budget_metrics(conn)
        rows = ""
        for m in metrics:
            r = m["row"]
            usage = 0 if m["released"] <= 0 else min(100, max(0, round((m["actuals"] + m["commitments"]) * 100 / m["released"])))
            rows += f"""<tr><td><a href="/budgets/{r['id']}"><strong>{esc(r['code'])}</strong></a><div class="small muted">{esc(r['name'])}</div></td>
            <td>{r['fiscal_year']}</td><td>{esc(r['holder_name'])}</td><td>{esc(r['cost_center'])}</td><td>{esc(r['wbs'])}</td><td>{esc(r['cost_element'])}</td>
            <td>{fmt_money(m['released'],r['currency'])}<div class="progress"><span style="width:{usage}%"></span></div></td>
            <td>{fmt_money(m['actuals'],r['currency'])}</td><td>{fmt_money(m['commitments'],r['currency'])}</td>
            <td class="{'bad' if m['available'] < 0 else 'good'}"><strong>{fmt_money(m['available'],r['currency'])}</strong></td></tr>"""
        body = f"""<div class="toolbar"><h1>Бюджеты</h1></div><div class="table-wrap"><table><thead><tr><th>Код</th><th>Год</th><th>Budget Holder</th><th>Cost Center</th><th>WBS</th><th>CE</th><th>Released</th><th>Actuals</th><th>Commitments</th><th>Доступно</th></tr></thead><tbody>{rows}</tbody></table></div>
        <br><div class="panel"><h2>Создать бюджет</h2><form method="post" action="/budgets/new">{self.csrf_input()}<div class="form-grid">
        <div><label>Код *</label><input name="code" required placeholder="IT-OPS-2027"></div><div><label>Название *</label><input name="name" required></div>
        <div><label>Финансовый год *</label><input type="number" name="fiscal_year" required value="{date.today().year}"></div><div><label>Валюта *</label><input name="currency" required value="EUR" maxlength="3"></div>
        <div><label>Budget Holder *</label><input name="holder_name" required></div><div><label>Email</label><input type="email" name="holder_email"></div>
        <div><label>Cost Center</label><input name="cost_center"></div><div><label>WBS</label><input name="wbs"></div><div><label>Cost Element</label><input name="cost_element"></div>
        <div><label>Утверждённый бюджет *</label><input name="approved" required placeholder="100000.00"></div><div><label>Released budget *</label><input name="released" required placeholder="100000.00"></div>
        <div class="full"><button type="submit">Создать бюджет</button></div></div></form></div>"""
        self.send_html(self.page("Бюджеты", body))

    def budget_detail(self, budget_id):
        with db() as conn:
            m = budget_metrics(conn, budget_id)
            if not m:
                return self.send_html(self.page("Не найдено", "<h1>Бюджет не найден</h1>"), 404)
            r = m["row"]
            budgets = conn.execute("SELECT id,code,name,currency FROM budget_lines WHERE id<>? ORDER BY code", (budget_id,)).fetchall()
            pos = conn.execute(
                """SELECT po.*, COALESCE(SUM(e.amount_cents),0) spent FROM purchase_orders po
                   LEFT JOIN expenses e ON e.po_id=po.id WHERE po.budget_id=? GROUP BY po.id ORDER BY po.id DESC""", (budget_id,)
            ).fetchall()
            expenses = conn.execute(
                """SELECT e.*, po.number po_number FROM expenses e LEFT JOIN purchase_orders po ON po.id=e.po_id
                   WHERE e.budget_id=? ORDER BY e.expense_date DESC,e.id DESC""", (budget_id,)
            ).fetchall()
            ops = conn.execute(
                """SELECT o.*, s.code source_code, t.code target_code FROM budget_operations o
                   LEFT JOIN budget_lines s ON s.id=o.source_budget_id LEFT JOIN budget_lines t ON t.id=o.target_budget_id
                   WHERE o.source_budget_id=? OR o.target_budget_id=? ORDER BY o.id DESC LIMIT 20""", (budget_id,budget_id)
            ).fetchall()
        target_options = "".join(f'<option value="{b["id"]}">{esc(b["code"])} — {esc(b["name"])}</option>' for b in budgets)
        po_rows = "".join(
            f"<tr><td>{esc(p['number'])}</td><td>{esc(p['vendor'])}</td><td>{esc(p['description'])}</td><td><span class='badge {p['status']}'>{p['status']}</span></td><td>{fmt_money(p['amount_cents'],r['currency'])}</td><td>{fmt_money(p['spent'],r['currency'])}</td></tr>" for p in pos
        ) or "<tr><td colspan='6' class='muted'>PO отсутствуют</td></tr>"
        exp_rows = "".join(
            f"<tr><td>{esc(e['expense_date'])}</td><td>{esc(e['invoice_no'])}</td><td>{esc(e['description'])}</td><td>{esc(e['po_number'] or 'Без PO')}</td><td>{fmt_money(e['amount_cents'],r['currency'])}</td></tr>" for e in expenses
        ) or "<tr><td colspan='5' class='muted'>Расходы отсутствуют</td></tr>"
        op_rows = "".join(
            f"<tr><td>{esc(o['created_at'][:10])}</td><td>{esc(o['operation_type'])}</td><td>{esc(o['source_code'])}</td><td>{esc(o['target_code'])}</td><td>{fmt_money(o['amount_cents'],r['currency'])}</td><td>{esc(o['note'])}</td></tr>" for o in ops
        ) or "<tr><td colspan='6' class='muted'>Операции отсутствуют</td></tr>"
        body = f"""<h1>{esc(r['code'])}: {esc(r['name'])}</h1><p class="muted">Budget Holder: {esc(r['holder_name'])} · Cost Center: {esc(r['cost_center'])} · WBS: {esc(r['wbs'])} · CE: {esc(r['cost_element'])}</p>
        <div class="grid cards"><div class="card"><div class="label">Approved</div><div class="metric">{fmt_money(m['approved'],r['currency'])}</div></div>
        <div class="card"><div class="label">Released</div><div class="metric">{fmt_money(m['released'],r['currency'])}</div></div>
        <div class="card"><div class="label">Actuals</div><div class="metric">{fmt_money(m['actuals'],r['currency'])}</div></div>
        <div class="card"><div class="label">Commitments</div><div class="metric">{fmt_money(m['commitments'],r['currency'])}</div></div>
        <div class="card"><div class="label">Available</div><div class="metric {'bad' if m['available']<0 else 'good'}">{fmt_money(m['available'],r['currency'])}</div></div></div><br>
        <div class="split"><div><div class="panel"><h2>PO</h2><div class="table-wrap"><table><thead><tr><th>Номер</th><th>Поставщик</th><th>Описание</th><th>Статус</th><th>Сумма</th><th>Actuals</th></tr></thead><tbody>{po_rows}</tbody></table></div></div><br>
        <div class="panel"><h2>Расходы</h2><div class="table-wrap"><table><thead><tr><th>Дата</th><th>Invoice</th><th>Описание</th><th>PO</th><th>Сумма</th></tr></thead><tbody>{exp_rows}</tbody></table></div></div></div>
        <aside><div class="panel"><h2>Операция бюджета</h2><form method="post" action="/budgets/{budget_id}/operation">{self.csrf_input()}
        <label>Тип операции</label><select name="operation_type" required><option value="SUPPLEMENT">Supplement — увеличение</option><option value="REDUCTION">Reduction — сокращение</option><option value="RELEASE">Release — разблокировка</option><option value="RETURN">Return — возврат</option><option value="TRANSFER">Transfer — перенос</option><option value="CARRY_FORWARD">Carry forward</option></select><br>
        <label>Сумма</label><input name="amount" required><br><label>Целевой бюджет для Transfer/Carry forward</label><select name="target_budget_id"><option value="">—</option>{target_options}</select><br>
        <label>Основание</label><textarea name="note" required></textarea><br><label>Исполнитель</label><input name="created_by" value="Budget Holder" required><br><button type="submit">Провести операцию</button></form></div></aside></div><br>
        <div class="panel"><h2>Журнал операций</h2><div class="table-wrap"><table><thead><tr><th>Дата</th><th>Операция</th><th>Источник</th><th>Получатель</th><th>Сумма</th><th>Основание</th></tr></thead><tbody>{op_rows}</tbody></table></div></div>"""
        self.send_html(self.page(r["code"], body))

    def pos_page(self):
        with db() as conn:
            pos = conn.execute(
                """SELECT po.*,b.code,b.currency,COALESCE(SUM(e.amount_cents),0) spent FROM purchase_orders po
                   JOIN budget_lines b ON b.id=po.budget_id LEFT JOIN expenses e ON e.po_id=po.id
                   GROUP BY po.id ORDER BY po.id DESC"""
            ).fetchall()
            budgets = all_budget_metrics(conn)
        rows = ""
        for p in pos:
            remaining = max(p["amount_cents"] - p["spent"], 0) if p["status"] == "APPROVED" else 0
            actions = ""
            if p["status"] == "DRAFT":
                actions = self.status_form(p["id"], "APPROVED", "Утвердить") + " " + self.status_form(p["id"], "CANCELLED", "Отменить", "danger")
            elif p["status"] == "APPROVED":
                actions = self.status_form(p["id"], "CLOSED", "Закрыть остаток", "secondary") + " " + self.status_form(p["id"], "CANCELLED", "Отменить", "danger")
            rows += f"<tr><td>{esc(p['number'])}</td><td><a href='/budgets/{p['budget_id']}'>{esc(p['code'])}</a></td><td>{esc(p['vendor'])}</td><td>{esc(p['description'])}</td><td><span class='badge {p['status']}'>{p['status']}</span></td><td>{fmt_money(p['amount_cents'],p['currency'])}</td><td>{fmt_money(p['spent'],p['currency'])}</td><td>{fmt_money(remaining,p['currency'])}</td><td>{actions}</td></tr>"
        budget_options = "".join(f'<option value="{m["row"]["id"]}">{esc(m["row"]["code"])} — доступно {fmt_money(m["available"],m["row"]["currency"])}</option>' for m in budgets)
        body = f"""<h1>Purchase Orders</h1><div class="table-wrap"><table><thead><tr><th>Номер</th><th>Бюджет</th><th>Поставщик</th><th>Содержание</th><th>Статус</th><th>Сумма</th><th>Actuals</th><th>Commitment</th><th>Действия</th></tr></thead><tbody>{rows}</tbody></table></div><br>
        <div class="panel"><h2>Создать PO</h2><form method="post" action="/pos/new">{self.csrf_input()}<div class="form-grid">
        <div><label>Номер PO *</label><input name="number" required placeholder="PO-2026-0002"></div><div><label>Бюджет *</label><select name="budget_id" required>{budget_options}</select></div>
        <div><label>Поставщик *</label><input name="vendor" required></div><div><label>Сумма/лимит *</label><input name="amount" required></div>
        <div><label>Статус</label><select name="status"><option value="DRAFT">Draft — без резерва</option><option value="APPROVED">Approved — резервировать</option></select></div>
        <div class="full"><label>Содержание услуг/товаров *</label><textarea name="description" required placeholder="Предмет, период, единицы/тариф либо максимальный лимит"></textarea></div>
        <div class="full"><button type="submit">Создать PO</button></div></div></form></div>"""
        self.send_html(self.page("PO", body))

    def status_form(self, po_id, status, label, cls=""):
        return f'<form class="inline" method="post" action="/pos/{po_id}/status">{self.csrf_input()}<input type="hidden" name="status" value="{status}"><button class="{cls}" type="submit">{esc(label)}</button></form>'

    def expenses_page(self):
        with db() as conn:
            expenses = conn.execute(
                """SELECT e.*,b.code,b.currency,po.number po_number FROM expenses e JOIN budget_lines b ON b.id=e.budget_id
                   LEFT JOIN purchase_orders po ON po.id=e.po_id ORDER BY e.expense_date DESC,e.id DESC"""
            ).fetchall()
            budgets = all_budget_metrics(conn)
            pos = conn.execute(
                """SELECT po.id,po.number,po.budget_id,po.amount_cents,b.currency,COALESCE(SUM(e.amount_cents),0) spent
                   FROM purchase_orders po JOIN budget_lines b ON b.id=po.budget_id LEFT JOIN expenses e ON e.po_id=po.id
                   WHERE po.status='APPROVED' GROUP BY po.id ORDER BY po.number"""
            ).fetchall()
        rows = "".join(f"<tr><td>{esc(e['expense_date'])}</td><td>{esc(e['code'])}</td><td>{esc(e['po_number'] or 'Без PO')}</td><td>{esc(e['invoice_no'])}</td><td>{esc(e['description'])}</td><td>{fmt_money(e['amount_cents'],e['currency'])}</td></tr>" for e in expenses)
        budget_options = "".join(f'<option value="{m["row"]["id"]}">{esc(m["row"]["code"])} — доступно {fmt_money(m["available"],m["row"]["currency"])}</option>' for m in budgets)
        po_options = "".join(f'<option value="{p["id"]}">{esc(p["number"])} — остаток {fmt_money(max(p["amount_cents"]-p["spent"],0),p["currency"])}</option>' for p in pos)
        body = f"""<h1>Фактические расходы</h1><div class="table-wrap"><table><thead><tr><th>Дата</th><th>Бюджет</th><th>PO</th><th>Invoice</th><th>Описание</th><th>Сумма</th></tr></thead><tbody>{rows}</tbody></table></div><br>
        <div class="panel"><h2>Внести расход</h2><form method="post" action="/expenses/new">{self.csrf_input()}<div class="form-grid">
        <div><label>Бюджет *</label><select name="budget_id" required>{budget_options}</select></div><div><label>PO</label><select name="po_id"><option value="">Без PO</option>{po_options}</select></div>
        <div><label>Дата *</label><input type="date" name="expense_date" value="{date.today().isoformat()}" required></div><div><label>Invoice</label><input name="invoice_no"></div>
        <div><label>Сумма *</label><input name="amount" required></div><div class="full"><label>Описание *</label><textarea name="description" required></textarea></div>
        <div class="full"><button type="submit">Провести расход</button></div></div></form></div>"""
        self.send_html(self.page("Расходы", body))

    def operations_page(self):
        with db() as conn:
            ops = conn.execute(
                """SELECT o.*,s.code source_code,s.currency source_currency,t.code target_code,t.currency target_currency
                   FROM budget_operations o LEFT JOIN budget_lines s ON s.id=o.source_budget_id
                   LEFT JOIN budget_lines t ON t.id=o.target_budget_id ORDER BY o.id DESC"""
            ).fetchall()
        rows = "".join(f"<tr><td>{esc(o['created_at'])}</td><td>{esc(o['operation_type'])}</td><td>{esc(o['source_code'])}</td><td>{esc(o['target_code'])}</td><td>{fmt_money(o['amount_cents'],o['source_currency'] or o['target_currency'] or '')}</td><td>{esc(o['created_by'])}</td><td>{esc(o['note'])}</td></tr>" for o in ops)
        body = f"""<h1>Журнал бюджетных операций</h1><div class="table-wrap"><table><thead><tr><th>Дата</th><th>Операция</th><th>Источник</th><th>Получатель</th><th>Сумма</th><th>Исполнитель</th><th>Основание</th></tr></thead><tbody>{rows}</tbody></table></div>"""
        self.send_html(self.page("Операции", body))

    def create_budget(self, data):
        approved = money_to_cents(data.get("approved"))
        released = money_to_cents(data.get("released"))
        if released > approved:
            raise ValueError("Released budget не может превышать утверждённый")
        currency = data.get("currency", "EUR").strip().upper()
        if not re.fullmatch(r"[A-Z]{3}", currency):
            raise ValueError("Валюта должна быть трёхбуквенным кодом")
        now = datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
        with db() as conn:
            conn.execute(
                """INSERT INTO budget_lines(code,name,fiscal_year,holder_name,holder_email,cost_center,wbs,cost_element,currency,initial_approved_cents,initial_released_cents,created_at)
                VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
                (data["code"].strip(), data["name"].strip(), int(data["fiscal_year"]), data["holder_name"].strip(), data.get("holder_email","").strip(), data.get("cost_center","").strip(), data.get("wbs","").strip(), data.get("cost_element","").strip(), currency, approved, released, now),
            )
        self.redirect("/budgets", "Бюджет создан")

    def create_operation(self, budget_id, data):
        op = data.get("operation_type", "").upper()
        amount = money_to_cents(data.get("amount"))
        target_id = int(data["target_budget_id"]) if data.get("target_budget_id") else None
        allowed = {"SUPPLEMENT","REDUCTION","RELEASE","RETURN","TRANSFER","CARRY_FORWARD"}
        if op not in allowed:
            raise ValueError("Неизвестный тип операции")
        with db() as conn:
            source = budget_metrics(conn, budget_id)
            if not source:
                raise ValueError("Бюджет не найден")
            sa = sr = ta = tr = 0
            if op == "SUPPLEMENT": sa = sr = amount
            elif op == "REDUCTION":
                if source["released"] - amount < source["actuals"] + source["commitments"]:
                    raise ValueError("Сокращение сделает доступный бюджет отрицательным")
                sa = sr = -amount
            elif op == "RELEASE":
                if source["released"] + amount > source["approved"]:
                    raise ValueError("Release превышает утверждённый бюджет")
                sr = amount
            elif op == "RETURN":
                if source["released"] - amount < source["actuals"] + source["commitments"]:
                    raise ValueError("Нельзя вернуть уже использованный или зарезервированный бюджет")
                sr = -amount
            elif op in {"TRANSFER","CARRY_FORWARD"}:
                if not target_id or target_id == budget_id:
                    raise ValueError("Укажите другой целевой бюджет")
                target = budget_metrics(conn, target_id)
                if not target:
                    raise ValueError("Целевой бюджет не найден")
                if source["row"]["currency"] != target["row"]["currency"]:
                    raise ValueError("Перенос между разными валютами не поддерживается")
                if source["released"] - amount < source["actuals"] + source["commitments"]:
                    raise ValueError("Недостаточно свободного бюджета для переноса")
                if op == "CARRY_FORWARD" and target["row"]["fiscal_year"] <= source["row"]["fiscal_year"]:
                    raise ValueError("Carry forward должен идти в более поздний финансовый год")
                sa = sr = -amount; ta = tr = amount
            now = datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
            conn.execute(
                """INSERT INTO budget_operations(operation_type,source_budget_id,target_budget_id,amount_cents,approved_delta_source,released_delta_source,approved_delta_target,released_delta_target,note,created_by,created_at)
                VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                (op,budget_id,target_id,amount,sa,sr,ta,tr,data.get("note","").strip(),data.get("created_by","Budget Holder").strip(),now),
            )
        self.redirect(f"/budgets/{budget_id}", "Операция проведена")

    def create_po(self, data):
        budget_id = int(data["budget_id"])
        amount = money_to_cents(data.get("amount"))
        status = data.get("status", "DRAFT").upper()
        if status not in {"DRAFT","APPROVED"}:
            raise ValueError("Некорректный статус PO")
        with db() as conn:
            m = budget_metrics(conn, budget_id)
            if not m:
                raise ValueError("Бюджет не найден")
            if status == "APPROVED" and amount > m["available"]:
                raise ValueError("Недостаточно доступного бюджета для утверждения PO")
            now = datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
            conn.execute(
                "INSERT INTO purchase_orders(number,budget_id,vendor,description,amount_cents,status,created_at) VALUES(?,?,?,?,?,?,?)",
                (data["number"].strip(),budget_id,data["vendor"].strip(),data["description"].strip(),amount,status,now),
            )
        self.redirect("/pos", "PO создан")

    def change_po_status(self, po_id, data):
        new_status = data.get("status", "").upper()
        if new_status not in {"APPROVED","CLOSED","CANCELLED"}:
            raise ValueError("Некорректный статус")
        with db() as conn:
            po = conn.execute("SELECT * FROM purchase_orders WHERE id=?", (po_id,)).fetchone()
            if not po:
                raise ValueError("PO не найден")
            if new_status == "APPROVED":
                if po["status"] != "DRAFT":
                    raise ValueError("Утвердить можно только Draft PO")
                m = budget_metrics(conn, po["budget_id"])
                spent = conn.execute("SELECT COALESCE(SUM(amount_cents),0) FROM expenses WHERE po_id=?", (po_id,)).fetchone()[0]
                remaining = max(po["amount_cents"] - spent, 0)
                if remaining > m["available"]:
                    raise ValueError("Недостаточно доступного бюджета")
            elif new_status in {"CLOSED","CANCELLED"} and po["status"] not in {"DRAFT","APPROVED"}:
                raise ValueError("PO уже закрыт")
            conn.execute("UPDATE purchase_orders SET status=? WHERE id=?", (new_status, po_id))
        self.redirect("/pos", "Статус PO изменён")

    def create_expense(self, data):
        budget_id = int(data["budget_id"])
        po_id = int(data["po_id"]) if data.get("po_id") else None
        amount = money_to_cents(data.get("amount"))
        with db() as conn:
            m = budget_metrics(conn, budget_id)
            if not m:
                raise ValueError("Бюджет не найден")
            if po_id:
                po = conn.execute("SELECT * FROM purchase_orders WHERE id=?", (po_id,)).fetchone()
                if not po or po["budget_id"] != budget_id:
                    raise ValueError("PO не относится к выбранному бюджету")
                if po["status"] != "APPROVED":
                    raise ValueError("Расход можно провести только по утверждённому PO")
                spent = conn.execute("SELECT COALESCE(SUM(amount_cents),0) FROM expenses WHERE po_id=?", (po_id,)).fetchone()[0]
                if spent + amount > po["amount_cents"]:
                    raise ValueError("Расход превышает остаток PO")
            else:
                if amount > m["available"]:
                    raise ValueError("Недостаточно доступного бюджета для расхода без PO")
            now = datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
            conn.execute(
                "INSERT INTO expenses(budget_id,po_id,expense_date,invoice_no,description,amount_cents,created_at) VALUES(?,?,?,?,?,?,?)",
                (budget_id,po_id,data["expense_date"],data.get("invoice_no","").strip(),data["description"].strip(),amount,now),
            )
        self.redirect("/expenses", "Расход проведён")

    def api_summary(self):
        with db() as conn:
            metrics = all_budget_metrics(conn)
        payload = []
        for m in metrics:
            r=m["row"]
            payload.append({
                "id":r["id"],"code":r["code"],"name":r["name"],"fiscal_year":r["fiscal_year"],
                "currency":r["currency"],"holder":r["holder_name"],"approved_cents":m["approved"],
                "released_cents":m["released"],"actuals_cents":m["actuals"],"commitments_cents":m["commitments"],"available_cents":m["available"]
            })
        self.send_json({"budgets": payload})


def main():
    init_db()
    server = ThreadingHTTPServer((HOST, PORT), AppHandler)
    print(f"{APP_NAME} listening on http://{HOST}:{PORT}; DB={DB_PATH}")
    server.serve_forever()


if __name__ == "__main__":
    main()
