"""Matching Vietnamese statement rows to fact codes.

Three failures this guards against, in descending order of how much damage they do:

1. A figure bound to the wrong meaning — `total_equity` filled from a row that means
   something else. Worse than a missing figure, because it looks authoritative.
2. A figure parsed wrongly — "1.234.567" read as 1.234, six orders of magnitude out,
   because Vietnamese groups thousands with dots.
3. A sign lost — Vietnamese statements write negatives in parentheses.
"""

from decimal import Decimal
from pathlib import Path

import pytest

from quantumedge.config import load_settings
from quantumedge.extract.mapping.vas import (
    check_identities,
    fold,
    match,
    parse_amount,
    spec_for,
)


@pytest.fixture(scope="module")
def spec():
    return spec_for(load_settings().rules_dir)


class TestFolding:
    """Diacritics are stripped before comparing, because a PDF text layer loses them
    often enough that requiring them would reject correct rows."""

    def test_diacritics_are_removed(self) -> None:
        assert fold("Nợ phải trả") == "no phai tra"
        assert fold("TỔNG CỘNG TÀI SẢN") == "tong cong tai san"

    def test_d_with_stroke_is_handled_explicitly(self) -> None:
        """`đ` is a distinct letter, not `d` plus a combining mark, so NFD leaves it
        alone and it needs its own replacement."""
        assert fold("Đầu tư") == "dau tu"
        assert "d" in fold("đồng")

    def test_whitespace_is_collapsed(self) -> None:
        assert fold("  Lợi   nhuận\tgộp \n") == "loi nhuan gop"


class TestMatchingRequiresBothCodeAndLabel:
    def test_a_correct_row_maps(self, spec) -> None:
        result = match(spec, "270", "TỔNG CỘNG TÀI SẢN")
        assert result.ok
        assert result.line is not None
        assert result.line.fact_code == "total_assets"

    def test_the_label_must_agree_with_the_code(self, spec) -> None:
        """The failure that matters. If a mapping entry has the wrong code — written from
        the circular's structure rather than from a real filing — the label guard is what
        catches it, and the line is refused instead of silently mapped."""
        result = match(spec, "270", "Vốn chủ sở hữu")
        assert not result.ok
        assert result.reason is not None
        assert result.reason.startswith("label_code_mismatch:270")
        # The reason names what it expected, so the mapping can be corrected.
        assert "tong cong tai san" in result.reason

    def test_an_unmapped_code_is_reported_not_failed(self, spec) -> None:
        """Most rows of a statement are not in the registry, and that is normal: the
        registry is deliberately small. The caller counts these rather than erroring."""
        result = match(spec, "132", "Trả trước cho người bán ngắn hạn")
        assert not result.ok
        assert result.reason == "unmapped_vas_code:132"

    def test_a_row_with_no_code_is_refused(self, spec) -> None:
        """Section headers and subtotals have no Mã số. Without a code there is no key,
        and matching on the label alone is the fuzzy matching this project refuses."""
        assert match(spec, "", "Một dòng nào đó").reason == "no_vas_code"
        assert match(spec, "   ", "x").reason == "no_vas_code"

    def test_matching_tolerates_real_world_label_noise(self, spec) -> None:
        """Companies prefix numbering, change case, and lose diacritics in PDF text. The
        keywords have to survive all three or the guard rejects correct rows."""
        for label in (
            "TỔNG CỘNG TÀI SẢN (270 = 100 + 200)",
            "tong cong tai san",
            "   Tổng cộng Tài sản   ",
        ):
            assert match(spec, "270", label).ok, label

    def test_every_mapped_fact_code_exists_in_the_registry(self, spec) -> None:
        """financial_fact.fact_code is a foreign key, so a mapping naming a code the
        registry never declared would fail at write time, on real data, after a filing had
        already been parsed."""
        import yaml

        registry = yaml.safe_load(
            (load_settings().rules_dir / "fact_codes.yaml").read_text(encoding="utf-8")
        )
        declared = {f["code"] for f in registry["fact_codes"]}
        mapped = {line.fact_code for line in spec.lines.values()}
        assert mapped <= declared, f"not in fact_codes.yaml: {sorted(mapped - declared)}"


class TestAmountParsing:
    """Vietnamese groups thousands with dots and uses a comma for decimals — the opposite
    of English. Getting this wrong is not a rounding error."""

    def test_dot_grouped_thousands(self) -> None:
        """The trap: float("1.234.567") raises, but a naive parser that strips the second
        dot or takes the first segment reads 1,234,567 as 1.234 — six orders of magnitude
        out, on a page a person would act on."""
        assert parse_amount("1.234.567") == Decimal(1234567)
        assert parse_amount("40.435.850") == Decimal(40435850)
        assert parse_amount("6.215.976.000") == Decimal(6215976000)

    def test_comma_is_the_decimal_separator(self) -> None:
        assert parse_amount("1.234,56") == Decimal("1234.56")
        assert parse_amount("0,25") == Decimal("0.25")

    def test_parentheses_mean_negative(self) -> None:
        """How a published Vietnamese statement writes a loss. Dropping the parentheses
        and keeping the digits would turn a loss into a profit."""
        assert parse_amount("(1.234.567)") == Decimal(-1234567)
        assert parse_amount("(5.000)") == Decimal(-5000)

    def test_leading_minus_also_means_negative(self) -> None:
        assert parse_amount("-1.234") == Decimal(-1234)

    def test_a_dash_is_absent_not_zero(self) -> None:
        """VAS forms write "-" for a line with no amount. Zero is a different claim: it
        asserts the company reported nothing there, rather than that we found nothing."""
        for blank in ("-", "–", "—", "", "   ", None):
            assert parse_amount(blank) is None

    def test_numeric_cells_pass_through(self) -> None:
        """openpyxl returns numbers for numeric cells, so most XLSX rows never hit the
        string path at all."""
        assert parse_amount(1234567) == Decimal(1234567)
        assert parse_amount(-5000) == Decimal(-5000)
        assert parse_amount(1234.5) == Decimal("1234.5")

    def test_non_breaking_spaces_are_handled(self) -> None:
        """PDF text layers are full of them, and they are invisible in a diff."""
        assert parse_amount("1\xa0234\xa0567") == Decimal(1234567)
        assert parse_amount("1 234 567") == Decimal(1234567)
        # Mixed: a dot-grouped figure whose gaps came through as NBSP.
        assert parse_amount("(1.234\xa0567)") == Decimal(-1234567)

    def test_unparseable_text_is_none_not_an_exception(self) -> None:
        """A statement contains prose rows. They must not take down a run."""
        assert parse_amount("Thuyết minh") is None
        assert parse_amount("n/a") is None


class TestIdentityChecks:
    """The document validates itself. A parse that takes the wrong column or loses a sign
    will almost always break the balance sheet identity, which makes this a far stronger
    check than trusting the parser."""

    def test_a_balanced_sheet_passes(self, spec) -> None:
        facts = {
            "total_assets": Decimal(1_000_000_000),
            "total_liabilities": Decimal(400_000_000),
            "total_equity": Decimal(600_000_000),
            "total_liabilities_and_equity": Decimal(1_000_000_000),
        }
        results = check_identities(spec, facts)
        assert results
        assert all(r.passed for r in results), [r.detail for r in results if not r.passed]

    def test_an_unbalanced_sheet_fails_and_says_by_how_much(self, spec) -> None:
        facts = {
            "total_assets": Decimal(1_000_000_000),
            "total_liabilities": Decimal(400_000_000),
            "total_equity": Decimal(600_000_000),
            # A column misread: this should equal total_assets.
            "total_liabilities_and_equity": Decimal(900_000_000),
        }
        failed = [r for r in check_identities(spec, facts) if not r.passed]
        assert failed
        assert "gap=100000000" in failed[0].detail

    def test_an_identity_with_missing_inputs_is_skipped_not_failed(self, spec) -> None:
        """Untested is not the same as failed. A statement where we only read two lines
        has not disproved anything."""
        results = check_identities(spec, {"total_assets": Decimal(1)})
        assert results == []

    def test_rounding_within_tolerance_passes(self, spec) -> None:
        facts = {
            "total_assets": Decimal(1_000_000_000),
            "total_liabilities_and_equity": Decimal(1_000_000_500),
        }
        balance = [r for r in check_identities(spec, facts) if r.name == "balance_sheet_balances"]
        assert balance and balance[0].passed

    def test_a_negated_term_expresses_subtraction(self, spec) -> None:
        """`_negative_cost_of_goods_sold` lets the YAML say "revenue minus COGS" without
        needing an operator grammar."""
        facts = {
            "revenue": Decimal(1_000_000),
            "cost_of_goods_sold": Decimal(700_000),
            "gross_profit": Decimal(300_000),
        }
        gross = [r for r in check_identities(spec, facts) if r.name == "gross_profit_definition"]
        assert gross and gross[0].passed

    def test_the_gross_profit_identity_catches_a_sign_error(self, spec) -> None:
        """If cost of goods sold were parsed as negative — parentheses handled twice —
        the identity breaks rather than the figure passing."""
        facts = {
            "revenue": Decimal(1_000_000),
            "cost_of_goods_sold": Decimal(-700_000),
            "gross_profit": Decimal(300_000),
        }
        gross = [r for r in check_identities(spec, facts) if r.name == "gross_profit_definition"]
        assert gross and not gross[0].passed


class TestSpecLoading:
    def test_assumed_lines_are_flagged(self, spec) -> None:
        """Nothing has been confirmed against a real filing yet, so every line should
        currently report as assumed. This test will change when the first filing arrives,
        which is the point: promotion to `verified` has to be deliberate."""
        assert all(line.is_assumed for line in spec.lines.values())

    def test_the_spec_file_is_the_only_source_of_mappings(self) -> None:
        """No mapping may be hardcoded in Python: a provider's rename must be a data edit."""
        source = Path(
            load_settings().rules_dir.parent / "src/quantumedge/extract/mapping/vas.py"
        ).read_text(encoding="utf-8")
        # No fact code should appear as a literal in the matcher.
        for code in ("total_assets", "net_profit_after_tax", "interest_expense"):
            assert f'"{code}"' not in source, f"{code} is hardcoded in vas.py"

    def test_identities_only_reference_mapped_or_negated_codes(self, spec) -> None:
        mapped = {line.fact_code for line in spec.lines.values()}
        for identity in spec.identities:
            for term in (*identity.left, *identity.right):
                code = term.removeprefix("_negative_")
                assert code in mapped, f"identity {identity.name} references unmapped {code}"
