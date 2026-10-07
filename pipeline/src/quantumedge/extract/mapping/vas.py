"""Match a row of a Vietnamese statement to a `fact_code`, or refuse.

Vietnamese statements follow forms prescribed by Circular 200/2014/TT-BTC, and every line
carries a *Mã số* — a numeric code fixed by the circular, identical across every company
filing under VAS. That code is the match key.

The label is a **guard, not a key**. Labels differ between companies in wording,
abbreviation, capitalisation and diacritics, and differ again between the XLSX and PDF of
one filing; the code does not. But a code read from the wrong column, or a mapping entry
written from the circular's structure rather than from a real document, would silently
bind a figure to the wrong meaning. `total_equity` filled from a row that means something
else is far worse than `total_equity` missing — so the code must be found *and* the label
must contain the expected keywords, or the line is refused.

Diacritics are stripped before comparing. A PDF text layer loses or mangles them often
enough that requiring them would reject correct rows, and "no phai tra" is unambiguous
enough to match on.
"""

import unicodedata
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml


@dataclass(frozen=True)
class VasLine:
    """One mapping entry from `rules/vas_lines.yaml`."""

    vas_code: str
    fact_code: str
    statement: str
    label_keywords: tuple[str, ...]
    status: str
    # Lines a filing presents as a deduction, which some companies print in parentheses
    # and others as a plain positive. Stored as a magnitude either way, because the
    # identity that uses it already subtracts: `revenue - cost_of_goods_sold`. Without
    # this, a parenthesised "Giá vốn hàng bán" made gross profit come out at revenue PLUS
    # cost, the identity failed, and a perfectly good balance sheet in the same workbook
    # was thrown away with it.
    is_deduction: bool = False

    @property
    def is_assumed(self) -> bool:
        """True when this entry has never been confirmed against a real filing."""
        return self.status != "verified"


@dataclass(frozen=True)
class Identity:
    """A self-check from the circular's own arithmetic."""

    name: str
    description: str
    left: tuple[str, ...]
    right: tuple[str, ...]
    tolerance_vnd: Decimal


@dataclass(frozen=True)
class VasSpec:
    lines: dict[str, VasLine]
    identities: tuple[Identity, ...]


def fold(text: str) -> str:
    """Lowercase, strip diacritics, and collapse whitespace.

    `đ` needs explicit handling: it is a distinct letter rather than `d` plus a combining
    mark, so NFD decomposition leaves it untouched and "no phai tra" would not match
    "Nợ phải trả" without this.
    """
    lowered = text.lower().replace("đ", "d")
    decomposed = unicodedata.normalize("NFD", lowered)
    stripped = "".join(c for c in decomposed if not unicodedata.combining(c))
    return " ".join(stripped.split())


def load_spec(path: Path) -> VasSpec:
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    lines: dict[str, VasLine] = {}
    for row in data.get("lines", []):
        line = VasLine(
            vas_code=str(row["vas_code"]).strip(),
            fact_code=row["fact_code"],
            statement=row["statement"],
            label_keywords=tuple(fold(k) for k in row.get("label_keywords", ())),
            status=row.get("status", "assumed"),
            is_deduction=bool(row.get("is_deduction", False)),
        )
        # Last-write-wins would drop a mapping silently. The registry is expected to grow -
        # short_term_debt and long_term_debt are still pending, with codes that differ
        # between revisions of the circular - so a duplicated code is a likely mistake and
        # must not be absorbed quietly.
        if line.vas_code in lines:
            raise ValueError(
                f"duplicate vas_code {line.vas_code!r} in {path.name}: "
                f"{lines[line.vas_code].fact_code} and {line.fact_code}"
            )
        lines[line.vas_code] = line

    identities = tuple(
        Identity(
            name=i["name"],
            description=i["description"],
            left=tuple(i["left"]),
            right=tuple(i["right"]),
            tolerance_vnd=Decimal(str(i.get("tolerance_vnd", 1000))),
        )
        for i in data.get("identities", [])
    )
    return VasSpec(lines=lines, identities=identities)


@lru_cache(maxsize=4)
def _cached_spec(path_str: str) -> VasSpec:
    return load_spec(Path(path_str))


def spec_for(rules_dir: Path) -> VasSpec:
    return _cached_spec(str(rules_dir / "vas_lines.yaml"))


@dataclass(frozen=True)
class MatchResult:
    """Either a mapped line, or the reason it was refused. Never a guess."""

    line: VasLine | None
    reason: str | None = None

    @property
    def ok(self) -> bool:
        return self.line is not None


def match(spec: VasSpec, vas_code: str, label: str) -> MatchResult:
    """Map a (Mã số, label) pair to a fact code, or say why not."""
    code = str(vas_code).strip()
    if not code:
        return MatchResult(None, "no_vas_code")

    line = spec.lines.get(code)
    if line is None:
        # Most statement rows are not mapped, and that is normal rather than an error:
        # the registry is deliberately small. The caller counts these, it does not fail.
        return MatchResult(None, f"unmapped_vas_code:{code}")

    folded = fold(label)
    missing = [kw for kw in line.label_keywords if kw not in folded]
    if missing:
        return MatchResult(
            None,
            f"label_code_mismatch:{code}:expected[{'|'.join(missing)}]:got[{folded[:60]}]",
        )

    return MatchResult(line)


# Characters a published Vietnamese statement uses around numbers.
_PAREN_NEGATIVE = ("(", ")")


def parse_amount(raw: Any) -> Decimal | None:
    """Parse a statement figure, or return None.

    Vietnamese statements group thousands with dots and use a comma for any decimal, and
    they write negatives in parentheses: `(1.234.567)`. A naive float() would read
    "1.234.567" as 1.234 and silently shrink a figure by six orders of magnitude, which
    is exactly the class of error that must not reach a page.

    Returns None rather than zero for a blank or a dash. VAS forms use "-" for a line
    with no amount, and zero is a different claim from absent.
    """
    if raw is None:
        return None
    if isinstance(raw, Decimal):
        return raw
    if isinstance(raw, int):
        return Decimal(raw)
    if isinstance(raw, float):
        # openpyxl hands back floats for numeric cells. str() first so the Decimal is the
        # value as displayed, not the binary float's full expansion.
        return Decimal(str(raw))

    text = str(raw).strip()
    if not text or text in {"-", "–", "—", "_"}:
        return None

    negative = text.startswith(_PAREN_NEGATIVE[0]) and text.endswith(_PAREN_NEGATIVE[1])
    if negative:
        text = text[1:-1].strip()
    if text.startswith("-"):
        negative = True
        text = text[1:].strip()

    # Dots group thousands; a comma is the decimal separator.
    cleaned = text.replace(".", "").replace(",", ".").replace(" ", "").replace("\xa0", "")
    if not cleaned:
        return None

    try:
        value = Decimal(cleaned)
    except InvalidOperation:
        return None

    return -value if negative else value


@dataclass
class IdentityCheck:
    name: str
    description: str
    passed: bool
    detail: str


def check_identities(spec: VasSpec, facts: dict[str, Decimal]) -> list[IdentityCheck]:
    """Verify the statement against the circular's own arithmetic.

    This is the strongest validation available here, because the document checks itself:
    total assets must equal total liabilities plus equity, and a parse that gets a column
    or a sign wrong will almost always break that. An identity whose inputs are absent is
    skipped rather than failed — it was not tested, which is different from failing.
    """
    results: list[IdentityCheck] = []

    for identity in spec.identities:
        needed = [
            t.lstrip("_").removeprefix("negative_") for t in (*identity.left, *identity.right)
        ]
        if any(code not in facts for code in needed):
            continue

        def total(terms: tuple[str, ...]) -> Decimal:
            out = Decimal(0)
            for term in terms:
                # A leading `_negative_` inverts the term, so an identity can express
                # "revenue minus cost of goods sold" without the YAML needing operators.
                if term.startswith("_negative_"):
                    out -= facts[term.removeprefix("_negative_")]
                else:
                    out += facts[term]
            return out

        left, right = total(identity.left), total(identity.right)
        gap = abs(left - right)
        results.append(
            IdentityCheck(
                name=identity.name,
                description=identity.description,
                passed=gap <= identity.tolerance_vnd,
                detail=f"left={left} right={right} gap={gap} tol={identity.tolerance_vnd}",
            )
        )

    return results


# ---------------------------------------------------------------------------------------
# Denomination
#
# The identities above are all scale-invariant: a statement printed in millions balances
# exactly as well as one printed in dong. So this is the one class of parse error the
# self-validation story cannot catch, and it understates every figure by a factor of a
# million while looking perfect.
#
# Vietnamese statements declare their unit in a line near the top, e.g.
# "Đơn vị tính: triệu đồng". A statement in anything other than dong is REFUSED rather
# than rescaled: applying a multiplier would mean trusting this detection to be right, and
# being wrong would produce a figure a thousand or a million out with full provenance.
# Refusing says "we could not read this", which is true and harmless.

_UNIT_SCALES: tuple[tuple[str, str], ...] = (
    ("nghin ty dong", "nghìn tỷ đồng"),
    ("ty dong", "tỷ đồng"),
    ("trieu dong", "triệu đồng"),
    ("nghin dong", "nghìn đồng"),
)

# Spellings that mean plain dong, which is what the extractor assumes and can handle.
_UNIT_DONG = ("dong viet nam", "vnd", "dong")


def detect_unit(sample_text: str) -> str | None:
    """The non-dong denomination a statement declares, or None.

    None covers two different cases on purpose - "declared as dong" and "no declaration
    found" - because the caller treats them the same way: proceed on the assumption of
    dong, which is overwhelmingly the common case for the primary statements. What must
    never happen silently is proceeding when the document says millions.
    """
    folded = fold(sample_text)
    if "don vi" not in folded:
        return None

    # Only look at the declaration itself. The words "triệu đồng" appear in note prose all
    # over a filing, so scanning the whole document would refuse almost everything.
    start = folded.index("don vi")
    window = folded[start : start + 60]

    for needle, pretty in _UNIT_SCALES:
        if needle in window:
            return pretty
    if any(d in window for d in _UNIT_DONG):
        return None
    return None
