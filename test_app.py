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


if __name__ == "__main__":
    unittest.main(verbosity=2)
