"""Tests for the admin eligibility parsing and the issued-codes filters.

The repo has no CI, so run these before deploying:
    pip install -r requirements-dev.txt && pytest

Everything here is a PURE function — no Postgres and no network. The endpoints
that wrap them are thin (auth, bind, serialise); the logic worth locking down is
the parsing of a bank-supplied sheet into loan amounts, which decides what a
farmer is offered and what their approval SMS says.
"""
import importlib
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
os.environ.setdefault("PORTAL_PASSWORD", "test")
os.environ.setdefault("ADMIN_PASSWORD", "test-admin")

import app as portal  # noqa: E402


def _reload(**env):
    """Re-import the module under a different environment. The amount limit is read
    at import time, so a test that needs one has to rebuild the module."""
    for k, v in env.items():
        if v is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = v
    return importlib.reload(portal)


@pytest.fixture(autouse=True)
def _clean_env():
    yield
    _reload(MAX_LOAN_AMOUNT_LIMIT=None)


def _csv(*rows, header="CODE,MOBILE NO,SABHSAD NAME,MAX LOAN AMOUNT"):
    return ("\n".join([header, *rows]) + "\n").encode()


# ── the amount column ────────────────────────────────────────────────────────
class TestMaxLoanAmount:
    def test_amount_is_parsed_and_kept_per_row(self):
        rows, skipped = portal._parse_eligibility_bytes(
            "f.csv", _csv("1,9876543210,A,12000"), source_batch="b")
        assert skipped == []
        assert rows[0]["max_loan_amount"] == "12000"

    def test_blank_amount_loads_as_null(self):
        """Blank is legitimate — it means 'give this farmer the standard amount',
        which the backends resolve from LOAN_MAX_AMOUNT on a NULL."""
        rows, skipped = portal._parse_eligibility_bytes(
            "f.csv", _csv("1,9876543210,A,"), source_batch="b")
        assert skipped == []
        assert rows[0]["max_loan_amount"] is None

    def test_a_sheet_without_the_column_still_loads(self):
        """Every sheet the bank has uploaded so far predates the column. Those rows
        must keep loading, with no amount, or this change breaks the existing flow."""
        rows, skipped = portal._parse_eligibility_bytes(
            "f.csv", _csv("1,9876543210,A", header="CODE,MOBILE NO,SABHSAD NAME"),
            source_batch="b")
        assert skipped == []
        assert rows[0]["max_loan_amount"] is None

    @pytest.mark.parametrize("cell,fragment", [
        ("abc", "not a number"),
        ("Rs 5000", "not a number"),
        ("0", "greater than 0"),
        ("-100", "greater than 0"),
    ])
    def test_unusable_amounts_reject_the_row_and_say_why(self, cell, fragment):
        """Rejected, not coerced and not silently dropped: this number becomes the
        loan the farmer is offered, so a row nobody meant must not load at all."""
        rows, skipped = portal._parse_eligibility_bytes(
            "f.csv", _csv(f"1,9876543210,A,{cell}"), source_batch="b")
        assert rows == []
        assert len(skipped) == 1 and skipped[0]["row"] == 2
        assert fragment in skipped[0]["reason"]

    def test_one_bad_row_does_not_take_the_good_ones_down(self):
        rows, skipped = portal._parse_eligibility_bytes(
            "f.csv", _csv("1,9876543210,A,5000", "2,9876543211,B,abc", "3,9876543212,C,7000"),
            source_batch="b")
        assert [r["phone"] for r in rows] == ["9876543210", "9876543212"]
        assert [s["row"] for s in skipped] == [3]

    def test_thousands_separators_are_accepted(self):
        """Operators type 12,000 in a spreadsheet without thinking about it."""
        rows, _ = portal._parse_eligibility_bytes(
            "f.csv", _csv('1,9876543210,A,"12,000"'), source_batch="b")
        assert rows[0]["max_loan_amount"] == "12000"


class TestAmountLimit:
    def test_no_limit_configured_accepts_any_positive_amount(self):
        m = _reload(MAX_LOAN_AMOUNT_LIMIT=None)
        assert m.MAX_LOAN_AMOUNT_LIMIT is None
        rows, skipped = m._parse_eligibility_bytes(
            "f.csv", _csv("1,9876543210,A,500000"), source_batch="b")
        assert skipped == [] and rows[0]["max_loan_amount"] == "500000"

    def test_amount_above_the_limit_is_rejected(self):
        m = _reload(MAX_LOAN_AMOUNT_LIMIT="10000")
        rows, skipped = m._parse_eligibility_bytes(
            "f.csv", _csv("1,9876543210,A,12000"), source_batch="b")
        assert rows == []
        assert "above the configured limit" in skipped[0]["reason"]

    def test_amount_at_the_limit_is_accepted(self):
        """The limit is a ceiling the bank may use, not one it must stay under."""
        m = _reload(MAX_LOAN_AMOUNT_LIMIT="10000")
        rows, skipped = m._parse_eligibility_bytes(
            "f.csv", _csv("1,9876543210,A,10000"), source_batch="b")
        assert skipped == [] and rows[0]["max_loan_amount"] == "10000"

    def test_the_limit_applies_to_the_quick_add_too(self):
        """Both entry points share one validator; a limit enforced on only the bulk
        path would be trivially bypassed by adding the number by hand."""
        m = _reload(MAX_LOAN_AMOUNT_LIMIT="10000")
        assert m._coerce_max_loan_amount("12000")[1] is not None
        assert m._coerce_max_loan_amount("9000") == ("9000", None)


# ── the downloadable template ────────────────────────────────────────────────
class TestTemplate:
    def test_template_round_trips_through_the_parser(self):
        """Headers are derived from the parser's map, and this is what proves the
        derivation holds: the sheet we hand the bank must load without edits."""
        rows, skipped = portal._parse_eligibility_bytes(
            "sample.xlsx", portal._build_sample_xlsx(), source_batch="b")
        assert skipped == []
        assert len(rows) == 1
        assert rows[0]["max_loan_amount"] is not None

    def test_template_carries_the_amount_column_and_its_note(self):
        assert "MAX LOAN AMOUNT" in portal._SAMPLE_HEADERS
        note = next(n for n in portal._SAMPLE_NOTES if n.startswith("MAX LOAN AMOUNT"))
        assert "blank" in note.lower()
