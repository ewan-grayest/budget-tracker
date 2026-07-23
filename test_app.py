#!/usr/bin/env python3
"""Unit tests for Budget Control.

Uses only the standard library (unittest), matching the app's zero-dependency
design. Run with:  python3 -m unittest -v   (or)   python3 test_app.py
"""
import os
import tempfile
import threading
import unittest

# The app reads DB_PATH / SEED_DEMO at import time, so configure the
# environment before importing it.
_TMPDIR = tempfile.mkdtemp(prefix="budget-test-")
os.environ["DB_PATH"] = os.path.join(_TMPDIR, "test.db")
os.environ["SEED_DEMO"] = "0"

import app  # noqa: E402


def _mk_metrics(approved, released, actuals, commitments, currency="EUR", fiscal_year=2026):
    """Build a budget_metrics-shaped dict for testing pure rule logic."""
    return {
        "row": {"currency": currency, "fiscal_year": fiscal_year},
        "approved": approved,
        "released": released,
        "actuals": actuals,
        "commitments": commitments,
        "available": released - actuals - commitments,
    }


class DBTestBase(unittest.TestCase):
    def setUp(self):
        for suffix in ("", "-wal", "-shm"):
            try:
                os.remove(app.DB_PATH + suffix)
            except FileNotFoundError:
                pass
        app.init_db()

    def _add_budget(self, code="B1", approved=1_000_000, released=1_000_000,
                    fiscal_year=2026, currency="EUR"):
        with app.db(write=True) as conn:
            cur = conn.execute(
                """INSERT INTO budget_lines
                (code,name,fiscal_year,holder_name,holder_email,cost_center,wbs,cost_element,
                 currency,initial_approved_cents,initial_released_cents,created_at)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
                (code, "Name", fiscal_year, "Holder", "", "", "", "", currency,
                 approved, released, "2026-01-01T00:00:00Z"),
            )
            return cur.lastrowid

    def _add_po(self, budget_id, amount, status="APPROVED", number="PO-1"):
        with app.db(write=True) as conn:
            cur = conn.execute(
                """INSERT INTO purchase_orders
                (number,budget_id,vendor,description,amount_cents,status,created_at)
                VALUES (?,?,?,?,?,?,?)""",
                (number, budget_id, "Vendor", "desc", amount, status, "2026-01-01T00:00:00Z"),
            )
            return cur.lastrowid

    def _add_expense(self, budget_id, amount, po_id=None):
        with app.db(write=True) as conn:
            conn.execute(
                """INSERT INTO expenses
                (budget_id,po_id,expense_date,invoice_no,description,amount_cents,created_at)
                VALUES (?,?,?,?,?,?,?)""",
                (budget_id, po_id, "2026-01-01", "", "desc", amount, "2026-01-01T00:00:00Z"),
            )


class MoneyToCentsTests(unittest.TestCase):
    def test_plain_and_decimal(self):
        self.assertEqual(app.money_to_cents("1000"), 100_000)
        self.assertEqual(app.money_to_cents("1000.50"), 100_050)

    def test_locale_separators(self):
        self.assertEqual(app.money_to_cents("1000,50"), 100_050)      # European decimal comma
        self.assertEqual(app.money_to_cents("1 000,50"), 100_050)     # space thousands sep
        self.assertEqual(app.money_to_cents("1,234.56"), 123_456)     # US grouping
        self.assertEqual(app.money_to_cents("1.234,56"), 123_456)     # European grouping

    def test_rounding_half_up(self):
        self.assertEqual(app.money_to_cents("0.015"), 2)

    def test_rejects_invalid_and_nonpositive(self):
        for bad in ("abc", "", None, "0", "-5", "0.00"):
            with self.assertRaises(ValueError):
                app.money_to_cents(bad)


class ParseHelperTests(unittest.TestCase):
    def test_parse_int(self):
        self.assertEqual(app.parse_int("42", "x"), 42)
        self.assertEqual(app.parse_int(" 7 ", "x"), 7)
        for bad in ("abc", None, "", "1.5"):
            with self.assertRaises(ValueError):
                app.parse_int(bad, "поле")

    def test_parse_date(self):
        self.assertEqual(app.parse_date("2026-07-23"), "2026-07-23")
        for bad in ("not-a-date", "2026-13-01", "", None, "23/07/2026"):
            with self.assertRaises(ValueError):
                app.parse_date(bad)

    def test_require(self):
        self.assertEqual(app.require({"a": "  x "}, "a", "A"), "x")
        for data in ({}, {"a": ""}, {"a": "   "}, {"a": None}):
            with self.assertRaises(ValueError):
                app.require(data, "a", "A")


class OperationDeltaTests(unittest.TestCase):
    def test_supplement(self):
        src = _mk_metrics(1_000, 1_000, 0, 0)
        self.assertEqual(app.compute_operation_deltas("SUPPLEMENT", 500, src, None), (500, 500, 0, 0))

    def test_release_within_and_beyond_approved(self):
        src = _mk_metrics(approved=1_000, released=400, actuals=0, commitments=0)
        self.assertEqual(app.compute_operation_deltas("RELEASE", 300, src, None), (0, 300, 0, 0))
        with self.assertRaises(ValueError):
            app.compute_operation_deltas("RELEASE", 700, src, None)  # exceeds approved

    def test_reduction_guard(self):
        src = _mk_metrics(1_000, 1_000, 600, 200)  # used = 800
        self.assertEqual(app.compute_operation_deltas("REDUCTION", 200, src, None), (-200, -200, 0, 0))
        with self.assertRaises(ValueError):
            app.compute_operation_deltas("REDUCTION", 300, src, None)  # would drop below used

    def test_return_guard(self):
        src = _mk_metrics(1_000, 1_000, 600, 200)
        self.assertEqual(app.compute_operation_deltas("RETURN", 200, src, None), (0, -200, 0, 0))
        with self.assertRaises(ValueError):
            app.compute_operation_deltas("RETURN", 300, src, None)

    def test_transfer(self):
        src = _mk_metrics(1_000, 1_000, 0, 0)
        tgt = _mk_metrics(1_000, 1_000, 0, 0)
        self.assertEqual(app.compute_operation_deltas("TRANSFER", 400, src, tgt), (-400, -400, 400, 400))

    def test_transfer_requires_target_and_same_currency(self):
        src = _mk_metrics(1_000, 1_000, 0, 0)
        with self.assertRaises(ValueError):
            app.compute_operation_deltas("TRANSFER", 400, src, None)
        tgt_usd = _mk_metrics(1_000, 1_000, 0, 0, currency="USD")
        with self.assertRaises(ValueError):
            app.compute_operation_deltas("TRANSFER", 400, src, tgt_usd)

    def test_carry_forward_year_direction(self):
        src = _mk_metrics(1_000, 1_000, 0, 0, fiscal_year=2026)
        earlier = _mk_metrics(1_000, 1_000, 0, 0, fiscal_year=2025)
        later = _mk_metrics(1_000, 1_000, 0, 0, fiscal_year=2027)
        with self.assertRaises(ValueError):
            app.compute_operation_deltas("CARRY_FORWARD", 100, src, earlier)
        self.assertEqual(app.compute_operation_deltas("CARRY_FORWARD", 100, src, later), (-100, -100, 100, 100))

    def test_unknown_operation(self):
        with self.assertRaises(ValueError):
            app.compute_operation_deltas("BOGUS", 100, _mk_metrics(1, 1, 0, 0), None)


class BudgetMetricsTests(DBTestBase):
    def test_available_formula(self):
        bid = self._add_budget(released=1_000_000, approved=1_000_000)
        po = self._add_po(bid, amount=250_000, status="APPROVED")
        self._add_expense(bid, amount=70_000, po_id=po)
        with app.db() as conn:
            m = app.budget_metrics(conn, bid)
        # commitment = max(250_000 - 70_000, 0) = 180_000; actuals = 70_000
        self.assertEqual(m["actuals"], 70_000)
        self.assertEqual(m["commitments"], 180_000)
        self.assertEqual(m["available"], 1_000_000 - 70_000 - 180_000)

    def test_only_approved_po_creates_commitment(self):
        bid = self._add_budget()
        self._add_po(bid, amount=250_000, status="DRAFT", number="PO-DRAFT")
        with app.db() as conn:
            m = app.budget_metrics(conn, bid)
        self.assertEqual(m["commitments"], 0)

    def test_missing_budget_returns_none(self):
        with app.db() as conn:
            self.assertIsNone(app.budget_metrics(conn, 999_999))


class ConcurrencyTests(DBTestBase):
    def test_no_overspend_under_concurrent_writes(self):
        """Multiple threads each try to spend part of the budget with a
        read-check + insert. The write-locked transaction must serialize
        them so the total never exceeds what was available."""
        available = 1_000_000
        per = 200_000  # exactly 5 should fit
        bid = self._add_budget(released=available, approved=available)
        threads_n = 8
        results = [None] * threads_n
        barrier = threading.Barrier(threads_n)

        def worker(idx):
            barrier.wait()  # maximise contention
            try:
                with app.db(write=True) as conn:
                    m = app.budget_metrics(conn, bid)
                    if per > m["available"]:
                        results[idx] = "rejected"
                        return
                    conn.execute(
                        """INSERT INTO expenses
                        (budget_id,po_id,expense_date,invoice_no,description,amount_cents,created_at)
                        VALUES (?,?,?,?,?,?,?)""",
                        (bid, None, "2026-01-01", "", "x", per, "2026-01-01T00:00:00Z"),
                    )
                    results[idx] = "ok"
            except Exception as exc:  # pragma: no cover - surfaces lock errors
                results[idx] = f"error:{exc!r}"

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(threads_n)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        oks = results.count("ok")
        self.assertEqual(oks, available // per, results)
        with app.db() as conn:
            total = conn.execute("SELECT COALESCE(SUM(amount_cents),0) FROM expenses WHERE budget_id=?", (bid,)).fetchone()[0]
            m = app.budget_metrics(conn, bid)
        self.assertLessEqual(total, available)
        self.assertGreaterEqual(m["available"], 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
