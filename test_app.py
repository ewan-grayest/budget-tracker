#!/usr/bin/env python3
"""Unit tests for Budget Control.

Uses only the standard library (unittest), matching the app's zero-dependency
design. Run with:  python3 -m unittest -v   (or)   python3 test_app.py
"""
import http.cookiejar
import os
import tempfile
import threading
import unittest
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from http.server import ThreadingHTTPServer

# The app reads DB_PATH at import time, so configure the environment before
# importing it.
_TMPDIR = tempfile.mkdtemp(prefix="budget-test-")
os.environ["DB_PATH"] = os.path.join(_TMPDIR, "test.db")

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

    def _add_expense(self, budget_id, amount, po_id=None, expense_date="2026-01-01"):
        with app.db(write=True) as conn:
            conn.execute(
                """INSERT INTO expenses
                (budget_id,po_id,expense_date,invoice_no,description,amount_cents,created_at)
                VALUES (?,?,?,?,?,?,?)""",
                (budget_id, po_id, expense_date, "", "desc", amount, "2026-01-01T00:00:00Z"),
            )

    def _set_allocations(self, budget_id, alloc):
        """Insert allocation rows from a {month: cents} mapping."""
        with app.db(write=True) as conn:
            conn.executemany(
                "INSERT INTO budget_monthly_allocations(budget_id,month,allocated_cents) VALUES(?,?,?)",
                [(budget_id, m, v) for m, v in alloc.items()],
            )

    def _seed_demo(self):
        """Recreate the demo records earlier versions wrote into an empty
        database, and clear the purge marker so purge_demo_data() runs as it
        would on a database created by such a version."""
        now = "2026-01-01T00:00:00Z"
        with app.db(write=True) as conn:
            cur = conn.execute(
                "INSERT INTO budget_lines({},created_at) VALUES({},?)".format(
                    ",".join(app.DEMO_BUDGET), ",".join("?" * len(app.DEMO_BUDGET))),
                (*app.DEMO_BUDGET.values(), now),
            )
            budget_id = cur.lastrowid
            cur = conn.execute(
                "INSERT INTO purchase_orders({},budget_id,created_at) VALUES({},?,?)".format(
                    ",".join(app.DEMO_PO), ",".join("?" * len(app.DEMO_PO))),
                (*app.DEMO_PO.values(), budget_id, now),
            )
            po_id = cur.lastrowid
            conn.execute(
                "INSERT INTO expenses({},budget_id,po_id,expense_date,created_at) VALUES({},?,?,?,?)".format(
                    ",".join(app.DEMO_EXPENSE), ",".join("?" * len(app.DEMO_EXPENSE))),
                (*app.DEMO_EXPENSE.values(), budget_id, po_id, "2026-01-01", now),
            )
            conn.executemany(
                "INSERT INTO budget_monthly_allocations(budget_id,month,allocated_cents) VALUES(?,?,?)",
                [(budget_id, i + 1, v) for i, v in enumerate(app.spread_evenly(10000000))],
            )
            conn.execute("DELETE FROM app_settings WHERE key='demo_purged'")
        return budget_id, po_id

    def _counts(self):
        with app.db() as conn:
            return tuple(conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                         for table in ("budget_lines", "purchase_orders", "expenses",
                                       "budget_monthly_allocations"))


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


class SpreadEvenlyTests(unittest.TestCase):
    def test_sum_and_shape(self):
        for total in (0, 1, 11, 12, 100, 10_000_000, 10_000_007):
            values = app.spread_evenly(total)
            self.assertEqual(len(values), 12)
            self.assertEqual(sum(values), total)
            self.assertLessEqual(max(values) - min(values), 1)

    def test_remainder_goes_to_earliest_months(self):
        self.assertEqual(app.spread_evenly(14), [2, 2] + [1] * 10)


class MoneyToCentsOrZeroTests(unittest.TestCase):
    def test_blank_and_zero_mean_zero(self):
        for value in ("", "   ", None, "0", "0.00", "0,00", "000"):
            self.assertEqual(app.money_to_cents_or_zero(value), 0)

    def test_amounts_parse_like_money_to_cents(self):
        self.assertEqual(app.money_to_cents_or_zero("10.50"), 1_050)
        self.assertEqual(app.money_to_cents_or_zero("1 000,50"), 100_050)

    def test_rejects_negative_and_garbage(self):
        for bad in ("-5", "abc", "1.2.3"):
            with self.assertRaises(ValueError):
                app.money_to_cents_or_zero(bad)


class MonthlyMetricsTests(DBTestBase):
    def test_missing_budget_returns_none(self):
        with app.db() as conn:
            self.assertIsNone(app.monthly_metrics(conn, 999_999))

    def test_no_plan_keeps_legacy_behavior(self):
        bid = self._add_budget()
        self._add_expense(bid, 300, expense_date="2026-01-15")
        with app.db() as conn:
            mm = app.monthly_metrics(conn, bid)
        self.assertFalse(mm["has_plan"])
        self.assertEqual(mm["allocated_total"], 0)
        self.assertTrue(all(m["allocated"] == 0 for m in mm["months"]))
        # Actuals are still bucketed, but nothing is flagged as over plan.
        self.assertEqual(mm["months"][0]["actuals"], 300)
        self.assertFalse(any(m["over"] for m in mm["months"]))

    def test_actuals_grouped_by_month(self):
        bid = self._add_budget()
        self._add_expense(bid, 300, expense_date="2026-01-15")
        self._add_expense(bid, 200, expense_date="2026-02-02")
        self._add_expense(bid, 200, expense_date="2026-02-20")
        with app.db() as conn:
            mm = app.monthly_metrics(conn, bid)
        self.assertEqual(mm["months"][0]["actuals"], 300)
        self.assertEqual(mm["months"][1]["actuals"], 400)
        self.assertEqual(sum(m["actuals"] for m in mm["months"]), 700)
        self.assertEqual(mm["actuals_in_year"], 700)

    def test_out_of_fiscal_year_expenses_excluded_from_buckets(self):
        bid = self._add_budget(fiscal_year=2026)
        self._add_expense(bid, 500, expense_date="2025-12-31")
        with app.db() as conn:
            mm = app.monthly_metrics(conn, bid)
            annual = app.budget_metrics(conn, bid)
        self.assertEqual(sum(m["actuals"] for m in mm["months"]), 0)
        self.assertEqual(mm["actuals_out_of_year"], 500)
        # The annual hard control still counts every expense of the line.
        self.assertEqual(annual["actuals"], 500)

    def test_remaining_and_over_flag(self):
        bid = self._add_budget()
        self._set_allocations(bid, {1: 100, 2: 100})
        self._add_expense(bid, 150, expense_date="2026-01-10")
        self._add_expense(bid, 30, expense_date="2026-03-10")  # month without allocation
        with app.db() as conn:
            mm = app.monthly_metrics(conn, bid)
        self.assertTrue(mm["has_plan"])
        jan, feb, mar = mm["months"][0], mm["months"][1], mm["months"][2]
        self.assertEqual((jan["remaining"], jan["over"]), (-50, True))
        self.assertEqual((feb["remaining"], feb["over"]), (100, False))
        # With a plan in place, spending in an unallocated month is over plan.
        self.assertEqual((mar["allocated"], mar["over"]), (0, True))
        self.assertEqual(mm["allocated_total"], 200)


class MonthOverspentTests(DBTestBase):
    def test_false_without_plan_or_outside_year(self):
        bid = self._add_budget(fiscal_year=2026)
        self._add_expense(bid, 999, expense_date="2026-01-01")
        with app.db() as conn:
            self.assertFalse(app.month_overspent(conn, bid, "2026-01-01"))  # no plan
        self._set_allocations(bid, {1: 100})
        with app.db() as conn:
            self.assertTrue(app.month_overspent(conn, bid, "2026-01-01"))
            self.assertFalse(app.month_overspent(conn, bid, "2025-01-01"))  # other year
            self.assertFalse(app.month_overspent(conn, 999_999, "2026-01-01"))  # missing budget


class AllocationsSchemaTests(DBTestBase):
    def test_init_db_upgrades_existing_database(self):
        # Simulate a DB created before the monthly feature: drop the new table,
        # then re-run init_db() as an app restart would.
        bid = self._add_budget()
        with app.db(write=True) as conn:
            conn.execute("DROP TABLE budget_monthly_allocations")
        app.init_db()
        self._set_allocations(bid, {1: 100})
        with app.db() as conn:
            row = conn.execute("SELECT id FROM budget_lines WHERE id=?", (bid,)).fetchone()
            mm = app.monthly_metrics(conn, bid)
        self.assertIsNotNone(row)  # existing data survived
        self.assertEqual(mm["months"][0]["allocated"], 100)

    def test_budget_with_only_allocations_is_deletable(self):
        bid = self._add_budget()
        self._set_allocations(bid, {1: 100, 5: 200})
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


class CentsToInputTests(unittest.TestCase):
    def test_round_trips_through_money_to_cents(self):
        for cents in (1, 50, 100, 100_050, 123_456, 999_999_99):
            self.assertEqual(app.money_to_cents(app.cents_to_input(cents)), cents)

    def test_formats_two_decimals(self):
        self.assertEqual(app.cents_to_input(100_000), "1000.00")
        self.assertEqual(app.cents_to_input(5), "0.05")


class AssertBudgetOkTests(DBTestBase):
    def test_ok_for_healthy_budget(self):
        bid = self._add_budget(approved=1_000_000, released=1_000_000)
        self._add_expense(bid, amount=400_000)
        with app.db() as conn:
            app.assert_budget_ok(conn, bid)  # must not raise

    def test_raises_when_available_would_go_negative(self):
        bid = self._add_budget(approved=1_000_000, released=1_000_000)
        self._add_expense(bid, amount=1_000_000)  # available now exactly 0
        with app.db(write=True) as conn:
            # Simulate an edit that lowers released below what is already spent.
            conn.execute("UPDATE budget_lines SET initial_released_cents=500000 WHERE id=?", (bid,))
            with self.assertRaises(ValueError):
                app.assert_budget_ok(conn, bid)

    def test_raises_when_released_exceeds_approved(self):
        bid = self._add_budget(approved=1_000_000, released=1_000_000)
        with app.db(write=True) as conn:
            conn.execute("UPDATE budget_lines SET initial_released_cents=1500000 WHERE id=?", (bid,))
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
        self.assertEqual(app.fmt_money(123_456, "EUR", "en"), "1,234.56 EUR")
        # Russian: non-breaking-space (U+00A0) thousands, comma decimal.
        self.assertEqual(app.fmt_money(123_456, "EUR", "ru"), "1 234,56 EUR")
        # Currency is HTML-escaped in both locales.
        self.assertIn("&lt;", app.fmt_money(100, "<b>", "en"))


_CBR_FIXTURE = """<?xml version="1.0" encoding="windows-1251"?>
<ValCurs Date="24.07.2026" name="Foreign Currency Market">
<Valute ID="R01235"><NumCode>840</NumCode><CharCode>USD</CharCode><Nominal>1</Nominal><Name>Доллар США</Name><Value>78,4049</Value><VunitRate>78,4049</VunitRate></Valute>
<Valute ID="R01239"><NumCode>978</NumCode><CharCode>EUR</CharCode><Nominal>1</Nominal><Name>Евро</Name><Value>89,4443</Value><VunitRate>89,4443</VunitRate></Valute>
<Valute ID="R01820"><NumCode>392</NumCode><CharCode>JPY</CharCode><Nominal>100</Nominal><Name>Иена</Name><Value>52,1000</Value><VunitRate>0,521</VunitRate></Valute>
</ValCurs>"""


class ParseCbrRatesTests(unittest.TestCase):
    def test_parses_char_codes_and_scales_rate(self):
        rates = app.parse_cbr_rates(_CBR_FIXTURE)
        # VunitRate 78,4049 (decimal comma) -> 78.4049 * 1e6.
        self.assertEqual(rates["USD"], ("Доллар США", 78_404_900))
        self.assertEqual(rates["EUR"][1], 89_444_300)

    def test_uses_per_unit_vunitrate_for_nominal_100(self):
        # JPY is quoted per 100 units; VunitRate (0.521) is already per 1 unit.
        self.assertEqual(app.parse_cbr_rates(_CBR_FIXTURE)["JPY"][1], 521_000)

    def test_falls_back_to_value_over_nominal(self):
        xml = ('<ValCurs><Valute ID="x"><CharCode>JPY</CharCode><Nominal>100</Nominal>'
               '<Name>Иена</Name><Value>52,1000</Value></Valute></ValCurs>')  # no VunitRate
        self.assertEqual(app.parse_cbr_rates(xml)["JPY"][1], 521_000)  # 52.10/100 * 1e6

    def test_skips_malformed_or_nonpositive(self):
        xml = ('<ValCurs><Valute ID="a"><CharCode>ZZ</CharCode><Nominal>1</Nominal>'
               '<Name>two letters</Name><VunitRate>5,0</VunitRate></Valute>'
               '<Valute ID="b"><CharCode>ZZZ</CharCode><Nominal>1</Nominal>'
               '<Name>zero rate</Name><VunitRate>0,0000</VunitRate></Valute></ValCurs>')
        self.assertEqual(app.parse_cbr_rates(xml), {})


class ConvertCentsTests(unittest.TestCase):
    RATES = {"RUB": 1_000_000, "USD": 78_404_900, "EUR": 89_444_300}

    def test_identity_when_same_currency(self):
        self.assertEqual(app.convert_cents(12_345, "USD", "USD", self.RATES), 12_345)

    def test_to_and_from_rub_round_trips(self):
        self.assertEqual(app.convert_cents(10_000, "USD", "RUB", self.RATES), 784_049)
        self.assertEqual(app.convert_cents(784_049, "RUB", "USD", self.RATES), 10_000)

    def test_cross_currency_via_rub(self):
        # 100.00 USD -> EUR = 78.4049 / 89.4443 * 100.00 ≈ 87.66.
        self.assertEqual(app.convert_cents(10_000, "USD", "EUR", self.RATES), 8_766)

    def test_missing_rate_returns_none_either_side(self):
        self.assertIsNone(app.convert_cents(10_000, "USD", "GBP", self.RATES))
        self.assertIsNone(app.convert_cents(10_000, "GBP", "USD", self.RATES))

    def test_rounding_half_up(self):
        rates = {"AAA": 3_000_000, "BBB": 2_000_000}
        # 1 cent AAA -> BBB = 1 * 3/2 = 1.5 -> HALF_UP -> 2.
        self.assertEqual(app.convert_cents(1, "AAA", "BBB", rates), 2)


class SettingsTests(DBTestBase):
    def test_base_currency_default_is_rub(self):
        with app.db() as conn:
            self.assertEqual(app.get_setting(conn, "base_currency"), "RUB")

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
            row = conn.execute("SELECT rate_micro,is_active FROM currencies WHERE code='RUB'").fetchone()
        self.assertEqual((row["rate_micro"], row["is_active"]), (1_000_000, 1))

    def test_load_rates_includes_rub_but_not_unrated(self):
        with app.db() as conn:
            rates = app.load_rates(conn)
        self.assertEqual(rates["RUB"], 1_000_000)
        self.assertNotIn("USD", rates)  # seeded without a rate until a refresh


class RefreshRatesTests(DBTestBase):
    def test_upserts_new_inactive_and_preserves_rub(self):
        feed = {"USD": ("Доллар США", 78_404_900), "TRY": ("Турецкая лира", 2_000_000)}
        with app.db(write=True) as conn:
            n = app.refresh_rates(conn, fetch=lambda: feed)
        self.assertEqual(n, 2)
        with app.db() as conn:
            usd = conn.execute("SELECT rate_micro FROM currencies WHERE code='USD'").fetchone()["rate_micro"]
            rub = conn.execute("SELECT rate_micro FROM currencies WHERE code='RUB'").fetchone()["rate_micro"]
            new = conn.execute("SELECT rate_micro,is_active FROM currencies WHERE code='TRY'").fetchone()
            updated = app.get_setting(conn, "rates_updated_at")
        self.assertEqual(usd, 78_404_900)                 # existing rate refreshed
        self.assertEqual(rub, 1_000_000)                  # RUB base untouched
        self.assertEqual((new["rate_micro"], new["is_active"]), (2_000_000, 0))  # new -> inactive
        self.assertTrue(updated)                          # timestamp recorded

    def test_rub_in_feed_is_ignored(self):
        with app.db(write=True) as conn:
            app.refresh_rates(conn, fetch=lambda: {"RUB": ("x", 999), "USD": ("y", 5_000_000)})
        with app.db() as conn:
            rub = conn.execute("SELECT rate_micro FROM currencies WHERE code='RUB'").fetchone()["rate_micro"]
        self.assertEqual(rub, 1_000_000)  # never overwritten from the feed


class DemoPurgeTests(DBTestBase):
    def test_fresh_database_starts_empty(self):
        # init_db() no longer seeds anything: a new deployment shows no records.
        self.assertEqual(self._counts(), (0, 0, 0, 0))

    def test_removes_untouched_demo_records(self):
        self._seed_demo()
        self.assertEqual(self._counts(), (1, 1, 1, 12))
        app.purge_demo_data()
        self.assertEqual(self._counts(), (0, 0, 0, 0))
        with app.db() as conn:
            self.assertEqual(app.get_setting(conn, "demo_purged"), "1")

    def test_is_idempotent(self):
        self._seed_demo()
        app.purge_demo_data()
        app.purge_demo_data()  # marker set: second run is a no-op, not an error
        self.assertEqual(self._counts(), (0, 0, 0, 0))

    def test_no_op_without_demo_data(self):
        bid = self._add_budget(code="REAL-2026")
        with app.db(write=True) as conn:
            conn.execute("DELETE FROM app_settings WHERE key='demo_purged'")
        app.purge_demo_data()
        with app.db() as conn:
            self.assertIsNotNone(conn.execute("SELECT 1 FROM budget_lines WHERE id=?", (bid,)).fetchone())

    def test_keeps_edited_demo_budget(self):
        # Somebody adopted the demo line for real budgeting: it no longer
        # matches the seeded values, so nothing of it may be deleted.
        budget_id, _ = self._seed_demo()
        with app.db(write=True) as conn:
            conn.execute("UPDATE budget_lines SET name='Real IT budget' WHERE id=?", (budget_id,))
        app.purge_demo_data()
        self.assertEqual(self._counts(), (1, 1, 1, 12))

    def test_keeps_demo_budget_carrying_own_expenses(self):
        # The demo expense and PO still match and go; the budget line stays
        # because a real expense hangs off it.
        budget_id, _ = self._seed_demo()
        self._add_expense(budget_id, 5000, expense_date="2026-03-01")
        app.purge_demo_data()
        with app.db() as conn:
            self.assertIsNotNone(conn.execute("SELECT 1 FROM budget_lines WHERE id=?", (budget_id,)).fetchone())
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM expenses").fetchone()[0], 1)
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM purchase_orders").fetchone()[0], 0)


class RatesNeedRefreshTests(DBTestBase):
    FEED = {"USD": ("Доллар США", 78_404_900), "EUR": ("Евро", 89_444_300)}

    def _refresh(self):
        with app.db(write=True) as conn:
            app.refresh_rates(conn, fetch=lambda: self.FEED)

    def test_true_when_an_active_currency_has_no_rate(self):
        # Fresh catalog: USD and EUR are active but unrated.
        with app.db() as conn:
            self.assertTrue(app.rates_need_refresh(conn))

    def test_false_right_after_a_refresh(self):
        self._refresh()
        with app.db() as conn:
            # GBP/CNY/KZT stay unrated but are inactive, so they do not force a fetch.
            self.assertFalse(app.rates_need_refresh(conn))

    def test_true_once_older_than_max_age(self):
        self._refresh()
        later = datetime.now(timezone.utc) + timedelta(hours=25)
        with app.db() as conn:
            self.assertFalse(app.rates_need_refresh(conn, now=later, max_age_hours=48))
            self.assertTrue(app.rates_need_refresh(conn, now=later, max_age_hours=24))

    def test_true_when_timestamp_is_missing_or_broken(self):
        self._refresh()
        for value in ("", "not-a-timestamp"):
            with app.db(write=True) as conn:
                app.set_setting(conn, "rates_updated_at", value)
            with app.db() as conn:
                self.assertTrue(app.rates_need_refresh(conn), value)

    def test_parse_iso_utc(self):
        self.assertIsNone(app.parse_iso_utc("garbage"))
        self.assertIsNone(app.parse_iso_utc(None))
        self.assertEqual(app.parse_iso_utc("2026-07-28T10:00:00Z"),
                         datetime(2026, 7, 28, 10, tzinfo=timezone.utc))
        # A naive timestamp is read as UTC rather than local time.
        self.assertEqual(app.parse_iso_utc("2026-07-28T10:00:00"),
                         datetime(2026, 7, 28, 10, tzinfo=timezone.utc))


class EnsureRatesTests(DBTestBase):
    FEED = {"USD": ("Доллар США", 78_404_900), "EUR": ("Евро", 89_444_300)}

    def setUp(self):
        super().setUp()
        self.calls = 0

    def _fetch(self):
        self.calls += 1
        return self.FEED

    def _boom(self):
        self.calls += 1
        raise ValueError("connection refused")

    def test_loads_missing_rates_and_stops_once_fresh(self):
        self.assertEqual(app.ensure_rates(fetch=self._fetch), 2)
        with app.db() as conn:
            self.assertEqual(app.load_rates(conn)["USD"], 78_404_900)
            self.assertTrue(app.get_setting(conn, "rates_updated_at"))
        # Rates are fresh now: no second network call.
        self.assertIsNone(app.ensure_rates(fetch=self._fetch))
        self.assertEqual(self.calls, 1)

    def test_force_refetches_fresh_rates(self):
        app.ensure_rates(fetch=self._fetch)
        self.assertEqual(app.ensure_rates(fetch=self._fetch, force=True), 2)
        self.assertEqual(self.calls, 2)

    def test_failure_keeps_stored_rates_and_records_the_reason(self):
        app.ensure_rates(fetch=self._fetch)
        with self.assertRaises(ValueError):
            app.ensure_rates(fetch=self._boom, force=True)
        with app.db() as conn:
            self.assertEqual(app.load_rates(conn)["USD"], 78_404_900)  # previous rates survive
            self.assertIn("connection refused", app.get_setting(conn, "rates_last_error"))

    def test_success_clears_a_previous_error(self):
        with self.assertRaises(ValueError):
            app.ensure_rates(fetch=self._boom)
        app.ensure_rates(fetch=self._fetch)
        with app.db() as conn:
            self.assertEqual(app.get_setting(conn, "rates_last_error"), "")


class PostRouteTests(unittest.TestCase):
    def test_every_route_resolves_with_ids_and_a_question(self):
        for path, handler, ids in (
                ("/settings", "save_settings", []),
                ("/settings/refresh-rates", "refresh_rates_action", []),
                ("/budgets/new", "create_budget", []),
                ("/budgets/7/operation", "create_operation", [7]),
                ("/budgets/7/allocations", "save_allocations", [7]),
                ("/budgets/7/edit", "update_budget", [7]),
                ("/budgets/7/delete", "delete_budget", [7]),
                ("/pos/new", "create_po", []),
                ("/pos/3/status", "change_po_status", [3]),
                ("/pos/3/edit", "update_po", [3]),
                ("/pos/3/delete", "delete_po", [3]),
                ("/expenses/new", "create_expense", []),
                ("/expenses/5/edit", "update_expense", [5]),
                ("/expenses/5/delete", "delete_expense", [5]),
                ("/operations/9/edit", "update_operation", [9]),
                ("/operations/9/delete", "delete_operation", [9])):
            with self.subTest(path=path):
                resolved, question, _danger, _kind, resolved_ids = app.match_post_route(path)
                self.assertEqual((resolved, resolved_ids), (handler, ids))
                self.assertTrue(hasattr(app.AppHandler, resolved))
                # Every mutating route must carry a real confirmation prompt.
                self.assertIn(question, app.TRANSLATIONS["en"])

    def test_unknown_paths_do_not_match(self):
        for path in ("/", "/budgets", "/budgets/7", "/nope", "/budgets/x/delete"):
            self.assertIsNone(app.match_post_route(path))

    def test_deletions_are_marked_destructive(self):
        for _pattern, handler, _question, danger, _kind in app.POST_ROUTES:
            self.assertEqual(danger, handler.startswith("delete_"), handler)


class ConfirmationHttpTests(DBTestBase):
    """End-to-end checks that no POST changes data before it is confirmed."""

    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), app.AppHandler)
        cls.base = "http://127.0.0.1:%d" % cls.server.server_address[1]
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=5)

    def setUp(self):
        super().setUp()
        self.jar = http.cookiejar.CookieJar()

        class NoRedirect(urllib.request.HTTPRedirectHandler):
            def redirect_request(self, *args, **kwargs):
                return None  # inspect the 303 instead of following it

        self.opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(self.jar), NoRedirect)
        self.opener.open(self.base + "/budgets?lang=en")  # issues the CSRF cookie

    def _csrf(self):
        return next(c.value for c in self.jar if c.name == "csrf_token")

    def _post(self, path, fields, token=None):
        body = urllib.parse.urlencode({**fields, "csrf_token": token or self._csrf()}).encode()
        request = urllib.request.Request(self.base + path, data=body,
                                         headers={"Referer": self.base + "/budgets"})
        try:
            response = self.opener.open(request)
            return response.status, response.read().decode(), response.headers.get("Location")
        except urllib.error.HTTPError as exc:
            return exc.code, exc.read().decode(), exc.headers.get("Location")

    BUDGET = {"code": "IT-2027", "name": "IT", "fiscal_year": "2027", "currency": "RUB",
              "holder_name": "Holder", "approved": "1000.00", "released": "1000.00"}

    def test_create_writes_nothing_until_confirmed(self):
        status, body, _ = self._post("/budgets/new", self.BUDGET)
        self.assertEqual(status, 200)
        self.assertIn('name="confirmed" value="1"', body)
        self.assertIn("Create a budget with these values?", body)
        self.assertIn("IT-2027", body)                       # the values are shown back
        self.assertEqual(self._counts()[0], 0)               # and nothing was written

        status, _, location = self._post("/budgets/new", {**self.BUDGET, "confirmed": "1"})
        self.assertEqual(status, 303)
        self.assertIn("/budgets", location)
        self.assertEqual(self._counts()[0], 1)

    def test_delete_writes_nothing_until_confirmed(self):
        budget_id = self._add_budget(code="TO-DELETE")
        status, body, _ = self._post("/budgets/%d/delete" % budget_id, {})
        self.assertEqual(status, 200)
        self.assertIn("Delete this budget?", body)
        self.assertIn("cannot be undone", body)              # destructive routes warn
        self.assertIn("TO-DELETE", body)                     # and name the record
        self.assertEqual(self._counts()[0], 1)

        status, _, _ = self._post("/budgets/%d/delete" % budget_id, {"confirmed": "1"})
        self.assertEqual(status, 303)
        self.assertEqual(self._counts()[0], 0)

    def test_confirmed_post_still_needs_a_valid_csrf_token(self):
        status, _, location = self._post(
            "/budgets/new", {**self.BUDGET, "confirmed": "1"}, token="wrong-token")
        self.assertEqual(status, 303)
        self.assertIn("CSRF", urllib.parse.unquote_plus(location))
        self.assertEqual(self._counts()[0], 0)

    def test_validation_error_returns_to_the_originating_page(self):
        # After confirmation the Referer is the confirmation page, so the back
        # field is what must steer the error redirect.
        status, _, location = self._post(
            "/budgets/new", {**self.BUDGET, "approved": "nonsense",
                             "confirmed": "1", "back": "/budgets"})
        self.assertEqual(status, 303)
        self.assertTrue(location.startswith("/budgets?"), location)
        self.assertEqual(self._counts()[0], 0)

    def test_back_field_cannot_redirect_off_site(self):
        status, _, location = self._post(
            "/budgets/new", {**self.BUDGET, "approved": "nonsense",
                             "confirmed": "1", "back": "//evil.example/x"})
        self.assertEqual(status, 303)
        self.assertTrue(location.startswith("/?"), location)

    def test_unknown_post_route_is_rejected(self):
        status, _, location = self._post("/nope", {"confirmed": "1"})
        self.assertEqual(status, 303)
        self.assertIn("Unknown action", urllib.parse.unquote_plus(location))


if __name__ == "__main__":
    unittest.main(verbosity=2)
