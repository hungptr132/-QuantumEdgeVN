"""A rule's prose must not be able to contradict the rule.

This exists because of a bug found while running M13's own reproducibility proof: raising
the thin-liquidity threshold from 1 tỷ to 2 tỷ left every page reading "dưới 1 tỷ" while
the engine tested 2 tỷ. A page that states a false threshold is worse than one that states
none, and nothing in the pipeline caught it — the prose is human-written and the threshold
is data, and the two had no link.

They are linked now: `rules/flags.yaml` writes `{threshold}` and `db/seed.py` renders it.
The test below is what stops a future rule from re-introducing a hand-typed number.
"""

import re
from decimal import Decimal

import yaml

from quantumedge.config import load_settings
from quantumedge.db.seed import _render_rule_prose
from quantumedge.vietnamese import vn_decimal, vn_money, vn_scaled

PROSE_FIELDS = ("name_vi", "name_en", "explanation_vi")

# A digit run, which is what a hand-typed threshold looks like. Bare "3" in "3 năm" or
# "ba tháng" written as a word is fine; this looks only for figures.
DIGITS = re.compile(r"\d")


def _rules() -> list[dict]:
    path = load_settings().rules_dir / "flags.yaml"
    return list(yaml.safe_load(path.read_text(encoding="utf-8"))["flags"])


class TestNoHandTypedThresholds:
    def test_prose_with_a_figure_is_either_templated_or_justified(self) -> None:
        """Every number in a rule's prose must come from the rule, or carry a written
        reason why it cannot. The allowlist is a YAML field rather than a list in this
        file, so the justification sits next to the rule it excuses."""
        offenders = []
        for rule in _rules():
            if rule.get("prose_number_note"):
                continue
            for field in PROSE_FIELDS:
                raw = rule.get(field)
                if not isinstance(raw, str):
                    continue
                # Strip the placeholders first; what remains is hand-typed.
                stripped = re.sub(r"\{[a-z_]+\}", "", raw)
                if DIGITS.search(stripped):
                    offenders.append(f"{rule['code']}.{field}: {raw.strip()[:80]}")

        assert not offenders, (
            "hand-typed numbers in rule prose - template them with {threshold}, "
            "{threshold_pct}, {threshold_vnd} or {multiple}, or add a prose_number_note "
            "explaining why the figure is a deliberate derivation:\n  " + "\n  ".join(offenders)
        )

    def test_the_one_documented_exception_explains_itself(self) -> None:
        """The dilution rule's 25% is the cumulative 3-year rise while expr holds the
        annual CAGR. That is a real and useful translation, so it is allowed — but only
        with the reason recorded."""
        notes = {r["code"]: r.get("prose_number_note") for r in _rules()}
        documented = {code: note for code, note in notes.items() if note}

        assert documented, "the exception mechanism should have at least one user"
        for code, note in documented.items():
            assert len(note.strip()) > 30, f"{code}: note is too short to be a reason"


class TestRendering:
    def test_a_threshold_renders_into_every_prose_field(self) -> None:
        rendered = _render_rule_prose(
            {
                "code": "x",
                "name_vi": "Thanh khoản mỏng - dưới {threshold}/phiên",
                "explanation_vi": "Giá trị giao dịch dưới {threshold_vnd} mỗi phiên.",
                "expr": {"metric": "m", "op": "<", "value": 2000000000},
            }
        )
        assert rendered["name_vi"] == "Thanh khoản mỏng - dưới 2 tỷ/phiên"
        assert rendered["explanation_vi"] == "Giá trị giao dịch dưới 2 tỷ đồng mỗi phiên."

    def test_changing_the_threshold_changes_the_prose(self) -> None:
        """The whole point: the number exists once."""
        base = {"code": "x", "name_vi": "dưới {threshold}", "explanation_vi": "{threshold_vnd}"}
        one = _render_rule_prose({**base, "expr": {"value": 1000000000}})
        two = _render_rule_prose({**base, "expr": {"value": 2000000000}})

        assert one["name_vi"] == "dưới 1 tỷ"
        assert two["name_vi"] == "dưới 2 tỷ"

    def test_a_ratio_threshold_renders_as_a_percentage(self) -> None:
        rendered = _render_rule_prose(
            {"code": "x", "name_vi": "Hơn {threshold_pct} số phiên", "expr": {"value": 0.05}}
        )
        assert rendered["name_vi"] == "Hơn 5% số phiên"

    def test_a_benchmark_multiple_renders(self) -> None:
        rendered = _render_rule_prose(
            {
                "code": "x",
                "name_vi": "cao hơn {multiple} lần trung vị ngành",
                "expr": {"benchmark": {"scope": "sector"}, "multiple": 3.0},
            }
        )
        assert rendered["name_vi"] == "cao hơn 3 lần trung vị ngành"

    def test_a_rule_without_a_threshold_is_untouched(self) -> None:
        rendered = _render_rule_prose(
            {"code": "x", "name_vi": "Thay đổi kiểm toán", "expr": {"event_type": "a"}}
        )
        assert rendered["name_vi"] == "Thay đổi kiểm toán"


class TestVietnameseNumbers:
    """Vietnamese swaps the separators: 1.234.567,89. Printing 1,5 as 1.5 to a Vietnamese
    reader is not just unidiomatic, it is ambiguous about the value."""

    def test_separators_are_swapped_relative_to_english(self) -> None:
        assert vn_decimal(1234567) == "1.234.567"
        assert vn_decimal(Decimal("1234567.89"), 2) == "1.234.567,89"
        assert vn_decimal(Decimal("0.05"), 2) == "0,05"

    def test_short_scale_words(self) -> None:
        assert vn_scaled(2_000_000_000) == "2 tỷ"
        assert vn_scaled(1_369_641_865) == "1,4 tỷ"
        assert vn_scaled(40_435_850) == "40,4 triệu"
        assert vn_scaled(1_500_000_000_000) == "1,5 nghìn tỷ"

    def test_a_round_number_drops_the_decimal(self) -> None:
        """ "2,0 tỷ" says nothing "2 tỷ" does not."""
        assert vn_scaled(2_000_000_000) == "2 tỷ"
        assert vn_scaled(3_000_000) == "3 triệu"

    def test_small_values_keep_their_digits(self) -> None:
        """Scaling 847 would lose precision for no readability gain."""
        assert vn_scaled(847) == "847"
        assert vn_scaled(0) == "0"

    def test_negatives_keep_their_sign(self) -> None:
        assert vn_scaled(-2_000_000_000) == "-2 tỷ"

    def test_money_carries_the_currency(self) -> None:
        assert vn_money(2_000_000_000) == "2 tỷ đồng"
