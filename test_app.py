#!/usr/bin/env python3
"""Unit tests for Budget Control.

Uses only the standard library (unittest), matching the app's zero-dependency
design. Run with:  python3 -m unittest -v   (or)   python3 test_app.py
"""
import os
import sqlite3
import tempfile
import threading
import unittest
from decimal import Decimal

# The app reads DB_PATH / SEED_DEMO at import time, so configure the
# environment before importing it.
_TMPDIR = tempfile.mkdtemp(prefix="budget-test-")
os.environ["DB_PATH"] = os.path.join(_TMPDIR, "test.db")
os.environ["SEED_DEMO"] = "0"

import app  # noqa: E402


def D(value):
    """Shorthand for a money literal in the tests."""
    return Decimal(value)


def _mk_metrics(approved, released, actuals, commitments, currency="EUR", fiscal_year=2026):
    """Build a budget_metrics-shaped dict for testing pure rule logic."""
    approved, released = D(approved), D(released)
    actuals, commitments = D(actuals), D(commitments)
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

    def _add_budget(self, code="B1", approved="10000.00", released="10000.00",
                    fiscal_year=2026, currency="EUR"):
        """Insert a budget line together with the reference records it needs.

        Every line points at a holder and at its own WBS element, so the project
        level is derived from the budget code to keep the WBS unique.
        """
        with app.db(write=True) as conn:
            holder_id = app.ref_get_or_create(conn, "budget_holders", "HOLDER", "Holder")
            wbs_id = app.ensure_wbs_element(conn, "", "IT", "OPS1",
                                            app.normalize_ref_code(code, app.WBS_TAIL_MAX))
            cur = conn.execute(
                """INSERT INTO budget_lines
                (code,name,fiscal_year,holder_id,cost_center_id,cost_element_id,wbs_element_id,
                 currency,initial_approved,initial_released,created_at)
                VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                (code, "Name", fiscal_year, holder_id, None, None, wbs_id, currency,
                 D(approved), D(released), "2026-01-01T00:00:00Z"),
            )
            return cur.lastrowid

    def _add_po(self, budget_id, amount, status="APPROVED", number="PO-1"):
        with app.db(write=True) as conn:
            vendor_id = app.ref_get_or_create(conn, "vendors", "VENDOR", "Vendor")
            cur = conn.execute(
                """INSERT INTO purchase_orders
                (number,budget_id,vendor_id,description,amount,status,created_at)
                VALUES (?,?,?,?,?,?,?)""",
                (number, budget_id, vendor_id, "desc", D(amount), status, "2026-01-01T00:00:00Z"),
            )
            return cur.lastrowid

    def _add_expense(self, budget_id, amount, po_id=None, expense_date="2026-01-01"):
        with app.db(write=True) as conn:
            conn.execute(
                """INSERT INTO expenses
                (budget_id,po_id,expense_date,invoice_no,description,amount,created_at)
                VALUES (?,?,?,?,?,?,?)""",
                (budget_id, po_id, expense_date, "", "desc", D(amount), "2026-01-01T00:00:00Z"),
            )

    def _set_allocations(self, budget_id, alloc):
        """Insert allocation rows from a {month: amount} mapping."""
        with app.db(write=True) as conn:
            conn.executemany(
                "INSERT INTO budget_monthly_allocations(budget_id,month,allocated) VALUES(?,?,?)",
                [(budget_id, m, D(v)) for m, v in alloc.items()],
            )


class ParseMoneyTests(unittest.TestCase):
    def test_plain_and_decimal(self):
        self.assertEqual(app.parse_money("1000"), D("1000.00"))
        self.assertEqual(app.parse_money("1000.50"), D("1000.50"))

    def test_locale_separators(self):
        self.assertEqual(app.parse_money("1000,50"), D("1000.50"))      # European decimal comma
        self.assertEqual(app.parse_money("1 000,50"), D("1000.50"))     # space thousands sep
        self.assertEqual(app.parse_money("1,234.56"), D("1234.56"))     # US grouping
        self.assertEqual(app.parse_money("1.234,56"), D("1234.56"))     # European grouping

    def test_rounding_half_up_to_two_decimals(self):
        self.assertEqual(app.parse_money("0.015"), D("0.02"))
        self.assertEqual(app.parse_money("10.005"), D("10.01"))

    def test_always_two_decimals(self):
        # The exponent is what makes it a two-decimal value, not just the digits.
        self.assertEqual(app.parse_money("7").as_tuple().exponent, -2)

    def test_rejects_invalid_and_nonpositive(self):
        for bad in ("abc", "", None, "0", "-5", "0.00"):
            with self.assertRaises(ValueError):
                app.parse_money(bad)


class DecimalStorageTests(DBTestBase):
    """Money never round-trips through a float, in memory or in SQLite."""

    def test_dec_coerces_every_sqlite_representation(self):
        self.assertEqual(app.dec(10), D("10.00"))          # INTEGER column value
        self.assertEqual(app.dec(1234.56), D("1234.56"))   # REAL column value
        self.assertEqual(app.dec("1234.56"), D("1234.56"))  # dsum() text
        self.assertEqual(app.dec(None), D("0.00"))
        self.assertEqual(app.dec(D("1.005")), D("1.01"))   # half-up at the money scale

    def test_amounts_round_trip_through_the_database(self):
        bid = self._add_budget(approved="1000.10", released="1000.10")
        with app.db() as conn:
            row = conn.execute("SELECT initial_approved FROM budget_lines WHERE id=?", (bid,)).fetchone()
        self.assertEqual(app.dec(row["initial_approved"]), D("1000.10"))

    def test_dsum_is_exact_over_many_rows(self):
        # 0.1 + 0.2 style drift: 300 cents summed as REALs would not land on
        # 3.00 reliably; dsum() accumulates Decimals instead.
        bid = self._add_budget(approved="100.00", released="100.00")
        for _ in range(300):
            self._add_expense(bid, "0.01")
        with app.db() as conn:
            total = conn.execute("SELECT dsum(amount) FROM expenses WHERE budget_id=?", (bid,)).fetchone()[0]
            m = app.budget_metrics(conn, bid)
        self.assertEqual(app.dec(total), D("3.00"))
        self.assertEqual(m["actuals"], D("3.00"))
        self.assertEqual(m["available"], D("97.00"))

    def test_dsum_of_no_rows_is_zero(self):
        with app.db() as conn:
            self.assertEqual(app.dec(conn.execute("SELECT dsum(amount) FROM expenses").fetchone()[0]),
                             D("0.00"))

    def test_no_cents_columns_remain(self):
        with app.db() as conn:
            tables = [r["name"] for r in
                      conn.execute("SELECT name FROM sqlite_master WHERE type='table'")]
            for table in tables:
                for column in app.table_columns(conn, table):
                    self.assertFalse(column.endswith("_cents"), f"{table}.{column}")
                    self.assertFalse(column.endswith("_micro"), f"{table}.{column}")


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


class WbsCodingTests(unittest.TestCase):
    """The coding standard: FF(2) / SSSS(4) / project.extension (15 in total)."""

    def test_build_full_wbs(self):
        self.assertEqual(app.build_wbs("", "IT", "OPS1", "INFRA"), "IT/OPS1/INFRA")
        self.assertEqual(app.build_wbs("", "IT", "OPS1", "INFRA", "01"), "IT/OPS1/INFRA.01")

    def test_full_wbs_starts_with_the_prefix_code(self):
        self.assertTrue(app.build_wbs("C", "IT", "OPS1", "INFRA").startswith("C"))
        self.assertEqual(app.build_wbs("C", "IT", "OPS1", "INFRA", "01"), "CIT/OPS1/INFRA.01")

    def test_accepts_a_valid_combination(self):
        app.validate_wbs_parts("IT", "OPS1", "INFRA", "01")      # must not raise
        app.validate_wbs_parts("99", "0001", "A" * 15)           # exactly at the limit

    def test_function_and_subfunction_lengths(self):
        for function in ("I", "ITX", ""):
            with self.assertRaises(ValueError):
                app.validate_wbs_parts(function, "OPS1", "INFRA")
        for sub in ("OPS", "OPS12", ""):
            with self.assertRaises(ValueError):
                app.validate_wbs_parts("IT", sub, "INFRA")

    def test_project_and_extension_limited_to_fifteen_together(self):
        app.validate_wbs_parts("IT", "OPS1", "A" * 13, "01")     # 13 + 2 = 15
        with self.assertRaises(ValueError):
            app.validate_wbs_parts("IT", "OPS1", "A" * 14, "01")  # 14 + 2 = 16
        with self.assertRaises(ValueError):
            app.validate_wbs_parts("IT", "OPS1", "A" * 16)

    def test_project_is_required(self):
        with self.assertRaises(ValueError):
            app.validate_wbs_parts("IT", "OPS1", "")

    def test_charset_is_restricted(self):
        for bad in ("IN FRA", "инфра", "INFRA/X", "INFRA.X"):
            with self.assertRaises(ValueError):
                app.validate_wbs_parts("IT", "OPS1", bad)

    def test_normalize_ref_code(self):
        self.assertEqual(app.normalize_ref_code("IT Operations"), "IT-OPERATIONS")
        self.assertEqual(app.normalize_ref_code(" it-ops "), "IT-OPS")
        self.assertEqual(app.normalize_ref_code("Отдел ИТ"), "")
        self.assertEqual(app.normalize_ref_code("abcdef", 3), "ABC")

    def test_fit_tail_keeps_project_plus_extension_within_the_limit(self):
        self.assertEqual(app.fit_tail("A" * 20), "A" * 15)
        self.assertEqual(app.fit_tail("A" * 20, "01"), "A" * 13)


class WbsElementTests(DBTestBase):
    def test_element_is_created_with_its_levels(self):
        with app.db(write=True) as conn:
            wbs_id = app.ensure_wbs_element(conn, "", "IT", "OPS1", "INFRA")
            row = conn.execute("SELECT * FROM wbs_elements WHERE id=?", (wbs_id,)).fetchone()
            function = conn.execute("SELECT code FROM functions WHERE id=?", (row["function_id"],)).fetchone()
        self.assertEqual(row["code"], "IT/OPS1/INFRA")
        self.assertEqual(function["code"], "IT")

    def test_duplicate_code_is_rejected(self):
        with app.db(write=True) as conn:
            app.ensure_wbs_element(conn, "", "IT", "OPS1", "INFRA")
        with app.db(write=True) as conn:
            with self.assertRaises(sqlite3.IntegrityError):
                conn.execute(
                    """INSERT INTO wbs_elements(code,function_id,sub_function_id,project_id,
                                                extension,name,is_active,created_at)
                       SELECT code,function_id,sub_function_id,project_id,extension,name,1,created_at
                       FROM wbs_elements""")

    def test_collision_bumps_the_extension(self):
        # Two legacy lines carrying the same WBS text must both survive.
        with app.db(write=True) as conn:
            first = app.ensure_wbs_element(conn, "", "IT", "OPS1", "INFRA")
            second = app.ensure_wbs_element(conn, "", "IT", "OPS1", "INFRA")
            codes = {r["id"]: r["code"] for r in conn.execute("SELECT id, code FROM wbs_elements")}
        self.assertEqual(codes[first], "IT/OPS1/INFRA")
        self.assertEqual(codes[second], "IT/OPS1/INFRA.2")

    def test_a_wbs_element_carries_at_most_one_budget(self):
        bid = self._add_budget(code="B1")
        with app.db() as conn:
            wbs_id = conn.execute("SELECT wbs_element_id FROM budget_lines WHERE id=?", (bid,)).fetchone()[0]
            holder_id = conn.execute("SELECT id FROM budget_holders").fetchone()[0]
        with app.db(write=True) as conn:
            with self.assertRaises(sqlite3.IntegrityError):
                conn.execute(
                    """INSERT INTO budget_lines
                    (code,name,fiscal_year,holder_id,wbs_element_id,currency,initial_approved,
                     initial_released,created_at)
                    VALUES (?,?,?,?,?,?,?,?,?)""",
                    ("B2", "Name", 2026, holder_id, wbs_id, "EUR", D("1.00"), D("1.00"),
                     "2026-01-01T00:00:00Z"),
                )

    def test_rebuild_after_prefix_change(self):
        with app.db(write=True) as conn:
            app.ensure_wbs_element(conn, "", "IT", "OPS1", "INFRA")
            app.set_setting(conn, "wbs_prefix", "C")
            app.rebuild_wbs_codes(conn)
            code = conn.execute("SELECT code FROM wbs_elements").fetchone()[0]
        self.assertEqual(code, "CIT/OPS1/INFRA")

    def test_elements_without_a_budget_are_reported(self):
        self._add_budget(code="B1")
        with app.db(write=True) as conn:
            app.ensure_wbs_element(conn, "", "HR", "PAY1", "SALARY")
        with app.db() as conn:
            pending = app.wbs_without_budget(conn)
        self.assertEqual([r["code"] for r in pending], ["HR/PAY1/SALARY"])


class ReferenceCatalogTests(DBTestBase):
    def test_get_or_create_is_idempotent(self):
        with app.db(write=True) as conn:
            first = app.ref_get_or_create(conn, "vendors", "Example Vendor", "Example Vendor")
            second = app.ref_get_or_create(conn, "vendors", "example vendor", "Other name")
            rows = conn.execute("SELECT code, name FROM vendors").fetchall()
        self.assertEqual(first, second)
        self.assertEqual([(r["code"], r["name"]) for r in rows], [("EXAMPLE-VENDOR", "Example Vendor")])

    def test_subfunction_codes_are_unique_within_a_function(self):
        with app.db(write=True) as conn:
            it = app.ref_get_or_create(conn, "functions", "IT", "IT")
            hr = app.ref_get_or_create(conn, "functions", "HR", "HR")
            # The same code under another function is a different record...
            self.assertNotEqual(app.subfunction_get_or_create(conn, it, "OPS1"),
                                app.subfunction_get_or_create(conn, hr, "OPS1"))
            # ...but repeating it under the same function is not.
            self.assertEqual(app.subfunction_get_or_create(conn, it, "OPS1"),
                             app.subfunction_get_or_create(conn, it, "OPS1"))

    def test_duplicate_code_is_rejected_per_catalog(self):
        with app.db(write=True) as conn:
            app.ref_get_or_create(conn, "cost_centers", "CC-IT", "IT")
        with app.db(write=True) as conn:
            with self.assertRaises(sqlite3.IntegrityError):
                conn.execute(
                    "INSERT INTO cost_centers(code,name,is_active,created_at) VALUES(?,?,1,?)",
                    ("CC-IT", "Duplicate", "2026-01-01T00:00:00Z"))

    def test_every_spec_matches_the_schema(self):
        # The generic CRUD builds SQL from these specs, so a typo in a table or
        # column name would only surface as a 500 at runtime.
        with app.db() as conn:
            tables = app.table_names(conn)
            for slug, spec in app.REFERENCES.items():
                self.assertIn(spec["table"], tables, slug)
                columns = app.table_columns(conn, spec["table"])
                for column in ("id", "code", "name", "is_active", "created_at"):
                    self.assertIn(column, columns, f"{slug}.{column}")
                for field in spec["fields"]:
                    self.assertIn(field["col"], columns, slug)
                for used_table, used_column in spec["usage"]:
                    self.assertIn(used_table, tables, slug)
                    self.assertIn(used_column, app.table_columns(conn, used_table), slug)

    def test_reference_titles_are_translated(self):
        for spec in app.REFERENCES.values():
            for lang in app.LANGUAGES:
                self.assertIn(spec["title"], app.TRANSLATIONS[lang])
                self.assertIn(spec["code_hint"], app.TRANSLATIONS[lang])


class OperationDeltaTests(unittest.TestCase):
    def test_supplement(self):
        src = _mk_metrics("1000.00", "1000.00", "0.00", "0.00")
        self.assertEqual(app.compute_operation_deltas("SUPPLEMENT", D("500.00"), src, None),
                         (D("500.00"), D("500.00"), D("0.00"), D("0.00")))

    def test_release_within_and_beyond_approved(self):
        src = _mk_metrics(approved="1000.00", released="400.00", actuals="0.00", commitments="0.00")
        self.assertEqual(app.compute_operation_deltas("RELEASE", D("300.00"), src, None),
                         (D("0.00"), D("300.00"), D("0.00"), D("0.00")))
        with self.assertRaises(ValueError):
            app.compute_operation_deltas("RELEASE", D("700.00"), src, None)  # exceeds approved

    def test_reduction_guard(self):
        src = _mk_metrics("1000.00", "1000.00", "600.00", "200.00")  # used = 800
        self.assertEqual(app.compute_operation_deltas("REDUCTION", D("200.00"), src, None),
                         (D("-200.00"), D("-200.00"), D("0.00"), D("0.00")))
        with self.assertRaises(ValueError):
            app.compute_operation_deltas("REDUCTION", D("300.00"), src, None)

    def test_return_guard(self):
        src = _mk_metrics("1000.00", "1000.00", "600.00", "200.00")
        self.assertEqual(app.compute_operation_deltas("RETURN", D("200.00"), src, None),
                         (D("0.00"), D("-200.00"), D("0.00"), D("0.00")))
        with self.assertRaises(ValueError):
            app.compute_operation_deltas("RETURN", D("300.00"), src, None)

    def test_transfer(self):
        src = _mk_metrics("1000.00", "1000.00", "0.00", "0.00")
        tgt = _mk_metrics("1000.00", "1000.00", "0.00", "0.00")
        self.assertEqual(app.compute_operation_deltas("TRANSFER", D("400.00"), src, tgt),
                         (D("-400.00"), D("-400.00"), D("400.00"), D("400.00")))

    def test_transfer_requires_target_and_same_currency(self):
        src = _mk_metrics("1000.00", "1000.00", "0.00", "0.00")
        with self.assertRaises(ValueError):
            app.compute_operation_deltas("TRANSFER", D("400.00"), src, None)
        tgt_usd = _mk_metrics("1000.00", "1000.00", "0.00", "0.00", currency="USD")
        with self.assertRaises(ValueError):
            app.compute_operation_deltas("TRANSFER", D("400.00"), src, tgt_usd)

    def test_carry_forward_year_direction(self):
        src = _mk_metrics("1000.00", "1000.00", "0.00", "0.00", fiscal_year=2026)
        earlier = _mk_metrics("1000.00", "1000.00", "0.00", "0.00", fiscal_year=2025)
        later = _mk_metrics("1000.00", "1000.00", "0.00", "0.00", fiscal_year=2027)
        with self.assertRaises(ValueError):
            app.compute_operation_deltas("CARRY_FORWARD", D("100.00"), src, earlier)
        self.assertEqual(app.compute_operation_deltas("CARRY_FORWARD", D("100.00"), src, later),
                         (D("-100.00"), D("-100.00"), D("100.00"), D("100.00")))

    def test_unknown_operation(self):
        with self.assertRaises(ValueError):
            app.compute_operation_deltas("BOGUS", D("100.00"),
                                         _mk_metrics("1.00", "1.00", "0.00", "0.00"), None)


class BudgetMetricsTests(DBTestBase):
    def test_available_formula(self):
        bid = self._add_budget(released="10000.00", approved="10000.00")
        po = self._add_po(bid, amount="2500.00", status="APPROVED")
        self._add_expense(bid, amount="700.00", po_id=po)
        with app.db() as conn:
            m = app.budget_metrics(conn, bid)
        # commitment = max(2500.00 - 700.00, 0) = 1800.00; actuals = 700.00
        self.assertEqual(m["actuals"], D("700.00"))
        self.assertEqual(m["commitments"], D("1800.00"))
        self.assertEqual(m["available"], D("7500.00"))

    def test_fractional_amounts_stay_exact(self):
        bid = self._add_budget(released="1000.00", approved="1000.00")
        po = self._add_po(bid, amount="250.10", status="APPROVED")
        self._add_expense(bid, amount="7.07", po_id=po)
        with app.db() as conn:
            m = app.budget_metrics(conn, bid)
        self.assertEqual(m["commitments"], D("243.03"))
        self.assertEqual(m["available"], D("749.90"))

    def test_operation_deltas_are_applied(self):
        bid = self._add_budget(released="1000.00", approved="1000.00")
        with app.db(write=True) as conn:
            conn.execute(
                """INSERT INTO budget_operations(operation_type,source_budget_id,target_budget_id,
                   amount,approved_delta_source,released_delta_source,approved_delta_target,
                   released_delta_target,note,created_by,created_at)
                   VALUES('SUPPLEMENT',?,NULL,?,?,?,0,0,'','tester','2026-01-01T00:00:00Z')""",
                (bid, D("10.25"), D("10.25"), D("10.25")),
            )
        with app.db() as conn:
            m = app.budget_metrics(conn, bid)
        self.assertEqual(m["approved"], D("1010.25"))
        self.assertEqual(m["released"], D("1010.25"))

    def test_only_approved_po_creates_commitment(self):
        bid = self._add_budget()
        self._add_po(bid, amount="2500.00", status="DRAFT", number="PO-DRAFT")
        with app.db() as conn:
            m = app.budget_metrics(conn, bid)
        self.assertEqual(m["commitments"], D("0.00"))

    def test_row_exposes_reference_codes(self):
        bid = self._add_budget(code="B1")
        with app.db() as conn:
            m = app.budget_metrics(conn, bid)
        self.assertEqual(m["row"]["wbs"], "IT/OPS1/B1")
        self.assertEqual(m["row"]["holder_name"], "Holder")

    def test_missing_budget_returns_none(self):
        with app.db() as conn:
            self.assertIsNone(app.budget_metrics(conn, 999_999))


class SpreadEvenlyTests(unittest.TestCase):
    def test_sum_and_shape(self):
        for total in ("0.00", "0.01", "0.11", "0.12", "1.00", "100000.00", "100000.07"):
            values = app.spread_evenly(D(total))
            self.assertEqual(len(values), 12)
            self.assertEqual(sum(values), D(total))
            self.assertLessEqual(max(values) - min(values), D("0.01"))

    def test_remainder_goes_to_earliest_months(self):
        self.assertEqual(app.spread_evenly(D("0.14")),
                         [D("0.02"), D("0.02")] + [D("0.01")] * 10)


class ParseMoneyOrZeroTests(unittest.TestCase):
    def test_blank_and_zero_mean_zero(self):
        for value in ("", "   ", None, "0", "0.00", "0,00", "000"):
            self.assertEqual(app.parse_money_or_zero(value), D("0.00"))

    def test_amounts_parse_like_parse_money(self):
        self.assertEqual(app.parse_money_or_zero("10.50"), D("10.50"))
        self.assertEqual(app.parse_money_or_zero("1 000,50"), D("1000.50"))

    def test_rejects_negative_and_garbage(self):
        for bad in ("-5", "abc", "1.2.3"):
            with self.assertRaises(ValueError):
                app.parse_money_or_zero(bad)


class MonthlyMetricsTests(DBTestBase):
    def test_missing_budget_returns_none(self):
        with app.db() as conn:
            self.assertIsNone(app.monthly_metrics(conn, 999_999))

    def test_no_plan_keeps_legacy_behavior(self):
        bid = self._add_budget()
        self._add_expense(bid, "3.00", expense_date="2026-01-15")
        with app.db() as conn:
            mm = app.monthly_metrics(conn, bid)
        self.assertFalse(mm["has_plan"])
        self.assertEqual(mm["allocated_total"], D("0.00"))
        self.assertTrue(all(m["allocated"] == D("0.00") for m in mm["months"]))
        # Actuals are still bucketed, but nothing is flagged as over plan.
        self.assertEqual(mm["months"][0]["actuals"], D("3.00"))
        self.assertFalse(any(m["over"] for m in mm["months"]))

    def test_actuals_grouped_by_month(self):
        bid = self._add_budget()
        self._add_expense(bid, "3.00", expense_date="2026-01-15")
        self._add_expense(bid, "2.00", expense_date="2026-02-02")
        self._add_expense(bid, "2.00", expense_date="2026-02-20")
        with app.db() as conn:
            mm = app.monthly_metrics(conn, bid)
        self.assertEqual(mm["months"][0]["actuals"], D("3.00"))
        self.assertEqual(mm["months"][1]["actuals"], D("4.00"))
        self.assertEqual(mm["actuals_in_year"], D("7.00"))

    def test_out_of_fiscal_year_expenses_excluded_from_buckets(self):
        bid = self._add_budget(fiscal_year=2026)
        self._add_expense(bid, "5.00", expense_date="2025-12-31")
        with app.db() as conn:
            mm = app.monthly_metrics(conn, bid)
            annual = app.budget_metrics(conn, bid)
        self.assertEqual(sum(m["actuals"] for m in mm["months"]), D("0.00"))
        self.assertEqual(mm["actuals_out_of_year"], D("5.00"))
        # The annual hard control still counts every expense of the line.
        self.assertEqual(annual["actuals"], D("5.00"))

    def test_remaining_and_over_flag(self):
        bid = self._add_budget()
        self._set_allocations(bid, {1: "1.00", 2: "1.00"})
        self._add_expense(bid, "1.50", expense_date="2026-01-10")
        self._add_expense(bid, "0.30", expense_date="2026-03-10")  # month without allocation
        with app.db() as conn:
            mm = app.monthly_metrics(conn, bid)
        self.assertTrue(mm["has_plan"])
        jan, feb, mar = mm["months"][0], mm["months"][1], mm["months"][2]
        self.assertEqual((jan["remaining"], jan["over"]), (D("-0.50"), True))
        self.assertEqual((feb["remaining"], feb["over"]), (D("1.00"), False))
        # With a plan in place, spending in an unallocated month is over plan.
        self.assertEqual((mar["allocated"], mar["over"]), (D("0.00"), True))
        self.assertEqual(mm["allocated_total"], D("2.00"))


class MonthOverspentTests(DBTestBase):
    def test_false_without_plan_or_outside_year(self):
        bid = self._add_budget(fiscal_year=2026)
        self._add_expense(bid, "9.99", expense_date="2026-01-01")
        with app.db() as conn:
            self.assertFalse(app.month_overspent(conn, bid, "2026-01-01"))  # no plan
        self._set_allocations(bid, {1: "1.00"})
        with app.db() as conn:
            self.assertTrue(app.month_overspent(conn, bid, "2026-01-01"))
            self.assertFalse(app.month_overspent(conn, bid, "2025-01-01"))  # other year
            self.assertFalse(app.month_overspent(conn, 999_999, "2026-01-01"))  # missing budget


class AllocationsSchemaTests(DBTestBase):
    def test_init_db_creates_missing_tables(self):
        # Simulate a DB created before the monthly feature: drop the table, then
        # re-run init_db() as an app restart would.
        bid = self._add_budget()
        with app.db(write=True) as conn:
            conn.execute("DROP TABLE budget_monthly_allocations")
        app.init_db()
        self._set_allocations(bid, {1: "1.00"})
        with app.db() as conn:
            row = conn.execute("SELECT id FROM budget_lines WHERE id=?", (bid,)).fetchone()
            mm = app.monthly_metrics(conn, bid)
        self.assertIsNotNone(row)  # existing data survived
        self.assertEqual(mm["months"][0]["allocated"], D("1.00"))

    def test_budget_with_only_allocations_is_deletable(self):
        bid = self._add_budget()
        self._set_allocations(bid, {1: "1.00", 5: "2.00"})
        # Mirror delete_budget: allocations are not linked documents, they are
        # removed together with the line.
        with app.db(write=True) as conn:
            conn.execute("DELETE FROM budget_monthly_allocations WHERE budget_id=?", (bid,))
            conn.execute("DELETE FROM budget_lines WHERE id=?", (bid,))
        with app.db() as conn:
            self.assertIsNone(app.monthly_metrics(conn, bid))
            left = conn.execute(
                "SELECT COUNT(*) FROM budget_monthly_allocations WHERE budget_id=?", (bid,)
            ).fetchone()[0]
        self.assertEqual(left, 0)


class MoneyInputTests(unittest.TestCase):
    def test_round_trips_through_parse_money(self):
        for amount in ("0.01", "0.50", "1.00", "1000.50", "1234.56", "999999.99"):
            self.assertEqual(app.parse_money(app.money_input(D(amount))), D(amount))

    def test_formats_two_decimals(self):
        self.assertEqual(app.money_input(D("1000")), "1000.00")
        self.assertEqual(app.money_input(D("0.05")), "0.05")
        self.assertEqual(app.money_input(5), "5.00")


class AssertBudgetOkTests(DBTestBase):
    def test_ok_for_healthy_budget(self):
        bid = self._add_budget(approved="10000.00", released="10000.00")
        self._add_expense(bid, amount="4000.00")
        with app.db() as conn:
            app.assert_budget_ok(conn, bid)  # must not raise

    def test_raises_when_available_would_go_negative(self):
        bid = self._add_budget(approved="10000.00", released="10000.00")
        self._add_expense(bid, amount="10000.00")  # available now exactly 0
        with app.db(write=True) as conn:
            # Simulate an edit that lowers released below what is already spent.
            conn.execute("UPDATE budget_lines SET initial_released=? WHERE id=?", (D("5000.00"), bid))
            with self.assertRaises(ValueError):
                app.assert_budget_ok(conn, bid)

    def test_raises_when_released_exceeds_approved(self):
        bid = self._add_budget(approved="10000.00", released="10000.00")
        with app.db(write=True) as conn:
            conn.execute("UPDATE budget_lines SET initial_released=? WHERE id=?", (D("15000.00"), bid))
            with self.assertRaises(ValueError):
                app.assert_budget_ok(conn, bid)

    def test_missing_or_none_budget_is_noop(self):
        with app.db() as conn:
            app.assert_budget_ok(conn, 999_999)  # unknown id: no raise
            app.assert_budget_ok(conn, None)      # optional target: no raise


class ConcurrencyTests(DBTestBase):
    def test_no_overspend_under_concurrent_writes(self):
        """Multiple threads each try to spend part of the budget with a
        read-check + insert. The write-locked transaction must serialize
        them so the total never exceeds what was available."""
        available = D("10000.00")
        per = D("2000.00")  # exactly 5 should fit
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
                        (budget_id,po_id,expense_date,invoice_no,description,amount,created_at)
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
        self.assertEqual(oks, int(available / per), results)
        with app.db() as conn:
            total = app.dec(conn.execute(
                "SELECT dsum(amount) FROM expenses WHERE budget_id=?", (bid,)).fetchone()[0])
            m = app.budget_metrics(conn, bid)
        self.assertLessEqual(total, available)
        self.assertGreaterEqual(m["available"], D("0.00"))


class MigrationTests(unittest.TestCase):
    """A database written by the previous version upgrades in place."""

    LEGACY_SCHEMA = """
    CREATE TABLE budget_lines (id INTEGER PRIMARY KEY AUTOINCREMENT, code TEXT NOT NULL UNIQUE,
     name TEXT NOT NULL, fiscal_year INTEGER NOT NULL, holder_name TEXT NOT NULL, holder_email TEXT,
     cost_center TEXT, wbs TEXT, cost_element TEXT, currency TEXT NOT NULL DEFAULT 'EUR',
     initial_approved_cents INTEGER NOT NULL, initial_released_cents INTEGER NOT NULL,
     created_at TEXT NOT NULL);
    CREATE TABLE budget_operations (id INTEGER PRIMARY KEY AUTOINCREMENT, operation_type TEXT NOT NULL,
     source_budget_id INTEGER, target_budget_id INTEGER, amount_cents INTEGER NOT NULL,
     approved_delta_source INTEGER NOT NULL DEFAULT 0, released_delta_source INTEGER NOT NULL DEFAULT 0,
     approved_delta_target INTEGER NOT NULL DEFAULT 0, released_delta_target INTEGER NOT NULL DEFAULT 0,
     note TEXT, created_by TEXT NOT NULL, created_at TEXT NOT NULL);
    CREATE TABLE purchase_orders (id INTEGER PRIMARY KEY AUTOINCREMENT, number TEXT NOT NULL UNIQUE,
     budget_id INTEGER NOT NULL, vendor TEXT NOT NULL, description TEXT NOT NULL,
     amount_cents INTEGER NOT NULL, status TEXT NOT NULL, created_at TEXT NOT NULL);
    CREATE TABLE expenses (id INTEGER PRIMARY KEY AUTOINCREMENT, budget_id INTEGER NOT NULL,
     po_id INTEGER, expense_date TEXT NOT NULL, invoice_no TEXT, description TEXT NOT NULL,
     amount_cents INTEGER NOT NULL, created_at TEXT NOT NULL);
    CREATE TABLE budget_monthly_allocations (budget_id INTEGER NOT NULL, month INTEGER NOT NULL,
     allocated_cents INTEGER NOT NULL DEFAULT 0, PRIMARY KEY(budget_id, month));
    CREATE TABLE currencies (code TEXT PRIMARY KEY, name TEXT NOT NULL, rate_micro INTEGER,
     is_active INTEGER NOT NULL DEFAULT 0, updated_at TEXT);
    CREATE TABLE app_settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
    INSERT INTO currencies VALUES ('RUB','Рубль',1000000,1,NULL),('EUR','Евро',89444300,1,'2026-07-01T00:00:00Z');
    INSERT INTO app_settings VALUES ('base_currency','RUB');
    INSERT INTO budget_lines (code,name,fiscal_year,holder_name,holder_email,cost_center,wbs,
     cost_element,currency,initial_approved_cents,initial_released_cents,created_at)
     VALUES ('IT-OPS-2026','IT Operations',2026,'Ann Holder','ann@example.com','CC-IT',
             'IT/OPS1/INFRA.01','IT Services','EUR',10000050,10000050,'2026-01-01T00:00:00Z'),
            ('HR-2026','HR',2026,'Bob Holder','','CC-HR','free text wbs','HR Services','EUR',
             500000,500000,'2026-01-01T00:00:00Z');
    INSERT INTO purchase_orders (number,budget_id,vendor,description,amount_cents,status,created_at)
     VALUES ('PO-1',1,'Example Vendor','support',2500000,'APPROVED','2026-01-01T00:00:00Z');
    INSERT INTO expenses (budget_id,po_id,expense_date,invoice_no,description,amount_cents,created_at)
     VALUES (1,1,'2026-03-05','INV-1','march',700033,'2026-03-05T00:00:00Z');
    INSERT INTO budget_monthly_allocations VALUES (1,3,900000);
    INSERT INTO budget_operations (operation_type,source_budget_id,target_budget_id,amount_cents,
     approved_delta_source,released_delta_source,approved_delta_target,released_delta_target,note,
     created_by,created_at)
     VALUES ('SUPPLEMENT',1,NULL,100025,100025,100025,0,0,'extra','Ann','2026-02-01T00:00:00Z');
    """

    def setUp(self):
        for suffix in ("", "-wal", "-shm"):
            try:
                os.remove(app.DB_PATH + suffix)
            except FileNotFoundError:
                pass
        conn = sqlite3.connect(app.DB_PATH)
        conn.executescript(self.LEGACY_SCHEMA)
        conn.commit()
        conn.close()
        app.init_db()

    def test_amounts_become_decimals(self):
        with app.db() as conn:
            m = app.budget_metrics(conn, 1)
            po = conn.execute("SELECT amount FROM purchase_orders WHERE number='PO-1'").fetchone()
            alloc = conn.execute(
                "SELECT allocated FROM budget_monthly_allocations WHERE budget_id=1").fetchone()
        self.assertEqual(app.dec(po["amount"]), D("25000.00"))
        self.assertEqual(app.dec(alloc["allocated"]), D("9000.00"))
        self.assertEqual(m["actuals"], D("7000.33"))
        # 100000.50 initial + 1000.25 supplement
        self.assertEqual(m["approved"], D("101000.75"))
        self.assertEqual(m["commitments"], D("17999.67"))

    def test_rate_micro_becomes_a_decimal_rate(self):
        with app.db() as conn:
            rates = app.load_rates(conn)
        self.assertEqual(rates["EUR"], D("89.444300"))
        self.assertEqual(rates["RUB"], D("1.000000"))

    def test_text_fields_move_into_catalogs(self):
        with app.db() as conn:
            row = conn.execute(f"{app.BUDGET_SELECT} WHERE b.code='IT-OPS-2026'").fetchone()
            vendors = [r["code"] for r in conn.execute("SELECT code FROM vendors")]
            holders = [r["name"] for r in conn.execute("SELECT name FROM budget_holders ORDER BY name")]
        self.assertEqual(row["holder_name"], "Ann Holder")
        self.assertEqual(row["holder_email"], "ann@example.com")
        self.assertEqual(row["cost_center"], "CC-IT")
        self.assertEqual(vendors, ["EXAMPLE-VENDOR"])
        self.assertEqual(holders, ["Ann Holder", "Bob Holder"])

    def test_standard_wbs_is_split_into_levels(self):
        with app.db() as conn:
            row = conn.execute(f"{app.BUDGET_SELECT} WHERE b.code='IT-OPS-2026'").fetchone()
            element = conn.execute(
                """SELECT f.code fcode, s.code scode, p.code pcode, w.extension
                   FROM wbs_elements w JOIN functions f ON f.id=w.function_id
                   JOIN sub_functions s ON s.id=w.sub_function_id
                   JOIN projects p ON p.id=w.project_id WHERE w.code=?""", (row["wbs"],)).fetchone()
        self.assertEqual(row["wbs"], "IT/OPS1/INFRA.01")
        self.assertEqual((element["fcode"], element["scode"], element["pcode"], element["extension"]),
                         ("IT", "OPS1", "INFRA", "01"))

    def test_unparseable_wbs_lands_under_the_placeholder_levels(self):
        with app.db() as conn:
            row = conn.execute(f"{app.BUDGET_SELECT} WHERE b.code='HR-2026'").fetchone()
        self.assertTrue(row["wbs"].startswith(f"{app.LEGACY_FUNCTION_CODE}/{app.LEGACY_SUBFUNCTION_CODE}/"))
        self.assertIn("FREE-TEXT-WBS", row["wbs"])

    def test_legacy_tables_are_gone_and_migration_is_idempotent(self):
        app.init_db()  # a second start must not migrate again
        with app.db() as conn:
            tables = app.table_names(conn)
            budgets = conn.execute("SELECT COUNT(*) FROM budget_lines").fetchone()[0]
        self.assertEqual(budgets, 2)
        self.assertFalse([t for t in tables if t.endswith("_legacy")])


class I18nTests(unittest.TestCase):
    def test_catalogs_have_identical_keys(self):
        # Both language blocks must define exactly the same keys, so no string
        # silently falls back to the default language in the other locale.
        ru_keys = set(app.TRANSLATIONS["ru"])
        en_keys = set(app.TRANSLATIONS["en"])
        self.assertEqual(ru_keys, en_keys,
                         f"ru-only: {ru_keys - en_keys}; en-only: {en_keys - ru_keys}")

    def test_no_empty_translations(self):
        for lang, catalog in app.TRANSLATIONS.items():
            for key, value in catalog.items():
                self.assertTrue(value.strip(), f"empty {lang} string for {key}")

    def test_lookup_and_fallback(self):
        self.assertEqual(app.t("en", "nav.overview"), "Overview")
        self.assertEqual(app.t("ru", "nav.overview"), "Обзор")
        # Unknown language falls back to the default language...
        self.assertEqual(app.t("de", "nav.overview"), app.t(app.DEFAULT_LANG, "nav.overview"))
        # ...and an unknown key falls back to the key itself.
        self.assertEqual(app.t("en", "does.not.exist"), "does.not.exist")

    def test_placeholder_substitution(self):
        self.assertEqual(app.t("en", "misc.h1_expense", id=7), "Expense #7")
        self.assertIn("Code", app.t("en", "error.field_required", label="Code"))
        # Missing placeholder args must not raise, just leave the text as-is.
        self.assertIsInstance(app.t("en", "error.field_required"), str)

    def test_normalize_lang(self):
        self.assertEqual(app.normalize_lang("EN"), "en")
        self.assertEqual(app.normalize_lang(" ru "), "ru")
        for bad in ("de", "", None, "e", "russian"):
            self.assertIsNone(app.normalize_lang(bad))

    def test_fmt_money_is_locale_aware(self):
        # English: comma thousands, dot decimal.
        self.assertEqual(app.fmt_money(D("1234.56"), "EUR", "en"), "1,234.56 EUR")
        # Russian: non-breaking-space (U+00A0) thousands, comma decimal.
        self.assertEqual(app.fmt_money(D("1234.56"), "EUR", "ru"), "1 234,56 EUR")
        # Values arriving straight from SQLite are formatted too.
        self.assertEqual(app.fmt_money(1234, "EUR", "en"), "1,234.00 EUR")
        # Currency is HTML-escaped in both locales.
        self.assertIn("&lt;", app.fmt_money(D("1.00"), "<b>", "en"))


_CBR_FIXTURE = """<?xml version="1.0" encoding="windows-1251"?>
<ValCurs Date="24.07.2026" name="Foreign Currency Market">
<Valute ID="R01235"><NumCode>840</NumCode><CharCode>USD</CharCode><Nominal>1</Nominal><Name>Доллар США</Name><Value>78,4049</Value><VunitRate>78,4049</VunitRate></Valute>
<Valute ID="R01239"><NumCode>978</NumCode><CharCode>EUR</CharCode><Nominal>1</Nominal><Name>Евро</Name><Value>89,4443</Value><VunitRate>89,4443</VunitRate></Valute>
<Valute ID="R01820"><NumCode>392</NumCode><CharCode>JPY</CharCode><Nominal>100</Nominal><Name>Иена</Name><Value>52,1000</Value><VunitRate>0,521</VunitRate></Valute>
</ValCurs>"""


class ParseCbrRatesTests(unittest.TestCase):
    def test_parses_char_codes_and_keeps_six_decimals(self):
        rates = app.parse_cbr_rates(_CBR_FIXTURE)
        self.assertEqual(rates["USD"], ("Доллар США", D("78.404900")))
        self.assertEqual(rates["EUR"][1], D("89.444300"))

    def test_uses_per_unit_vunitrate_for_nominal_100(self):
        # JPY is quoted per 100 units; VunitRate (0.521) is already per 1 unit.
        self.assertEqual(app.parse_cbr_rates(_CBR_FIXTURE)["JPY"][1], D("0.521000"))

    def test_falls_back_to_value_over_nominal(self):
        xml = ('<ValCurs><Valute ID="x"><CharCode>JPY</CharCode><Nominal>100</Nominal>'
               '<Name>Иена</Name><Value>52,1000</Value></Valute></ValCurs>')  # no VunitRate
        self.assertEqual(app.parse_cbr_rates(xml)["JPY"][1], D("0.521000"))

    def test_skips_malformed_or_nonpositive(self):
        xml = ('<ValCurs><Valute ID="a"><CharCode>ZZ</CharCode><Nominal>1</Nominal>'
               '<Name>two letters</Name><VunitRate>5,0</VunitRate></Valute>'
               '<Valute ID="b"><CharCode>ZZZ</CharCode><Nominal>1</Nominal>'
               '<Name>zero rate</Name><VunitRate>0,0000</VunitRate></Valute></ValCurs>')
        self.assertEqual(app.parse_cbr_rates(xml), {})


class ConvertMoneyTests(unittest.TestCase):
    RATES = {"RUB": D("1.000000"), "USD": D("78.404900"), "EUR": D("89.444300")}

    def test_identity_when_same_currency(self):
        self.assertEqual(app.convert_money(D("123.45"), "USD", "USD", self.RATES), D("123.45"))

    def test_to_and_from_rub_round_trips(self):
        self.assertEqual(app.convert_money(D("100.00"), "USD", "RUB", self.RATES), D("7840.49"))
        self.assertEqual(app.convert_money(D("7840.49"), "RUB", "USD", self.RATES), D("100.00"))

    def test_cross_currency_via_rub(self):
        # 100.00 USD -> EUR = 78.4049 / 89.4443 * 100.00 ≈ 87.66.
        self.assertEqual(app.convert_money(D("100.00"), "USD", "EUR", self.RATES), D("87.66"))

    def test_missing_rate_returns_none_either_side(self):
        self.assertIsNone(app.convert_money(D("100.00"), "USD", "GBP", self.RATES))
        self.assertIsNone(app.convert_money(D("100.00"), "GBP", "USD", self.RATES))

    def test_rounding_half_up(self):
        rates = {"AAA": D("3.000000"), "BBB": D("2.000000")}
        # 0.01 AAA -> BBB = 0.015 -> HALF_UP -> 0.02.
        self.assertEqual(app.convert_money(D("0.01"), "AAA", "BBB", rates), D("0.02"))


class SettingsTests(DBTestBase):
    def test_base_currency_default_is_rub(self):
        with app.db() as conn:
            self.assertEqual(app.get_setting(conn, "base_currency"), "RUB")

    def test_wbs_prefix_defaults_to_empty(self):
        with app.db() as conn:
            self.assertEqual(app.get_setting(conn, "wbs_prefix"), "")

    def test_set_and_get_with_upsert(self):
        with app.db(write=True) as conn:
            app.set_setting(conn, "base_currency", "USD")
            app.set_setting(conn, "base_currency", "EUR")  # second call overwrites
        with app.db() as conn:
            self.assertEqual(app.get_setting(conn, "base_currency"), "EUR")
            self.assertEqual(app.get_setting(conn, "missing", "fallback"), "fallback")


class CurrencySeedTests(DBTestBase):
    def test_rub_seeded_active_with_unit_rate(self):
        with app.db() as conn:
            row = conn.execute("SELECT rate,is_active FROM currencies WHERE code='RUB'").fetchone()
        self.assertEqual((app.dec(row["rate"], app.RATE_Q), row["is_active"]), (D("1.000000"), 1))

    def test_load_rates_includes_rub_but_not_unrated(self):
        with app.db() as conn:
            rates = app.load_rates(conn)
        self.assertEqual(rates["RUB"], D("1.000000"))
        self.assertNotIn("USD", rates)  # seeded without a rate until a refresh


class RefreshRatesTests(DBTestBase):
    def test_upserts_new_inactive_and_preserves_rub(self):
        feed = {"USD": ("Доллар США", D("78.404900")), "TRY": ("Турецкая лира", D("2.000000"))}
        with app.db(write=True) as conn:
            n = app.refresh_rates(conn, fetch=lambda: feed)
        self.assertEqual(n, 2)
        with app.db() as conn:
            rates = app.load_rates(conn)
            new = conn.execute("SELECT rate,is_active FROM currencies WHERE code='TRY'").fetchone()
            updated = app.get_setting(conn, "rates_updated_at")
        self.assertEqual(rates["USD"], D("78.404900"))    # existing rate refreshed
        self.assertEqual(rates["RUB"], D("1.000000"))     # RUB base untouched
        self.assertEqual((app.dec(new["rate"], app.RATE_Q), new["is_active"]),
                         (D("2.000000"), 0))              # new -> inactive
        self.assertTrue(updated)                          # timestamp recorded

    def test_rub_in_feed_is_ignored(self):
        with app.db(write=True) as conn:
            app.refresh_rates(conn, fetch=lambda: {"RUB": ("x", D("999.000000")),
                                                   "USD": ("y", D("5.000000"))})
        with app.db() as conn:
            rates = app.load_rates(conn)
        self.assertEqual(rates["RUB"], D("1.000000"))  # never overwritten from the feed


if __name__ == "__main__":
    unittest.main(verbosity=2)
