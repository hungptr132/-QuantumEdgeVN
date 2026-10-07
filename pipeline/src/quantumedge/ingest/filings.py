"""Filings from a local folder — the authoritative fundamentals path.

This is the one source where the document is the company's own filed statement rather
than a third party's rendering of it, which makes it the most authoritative input the
pipeline has. It is also the only one that needs no API: files are placed in
`pipeline/data/filings/<SYMBOL>/` by a person, from the company's IR page or HOSE.

That matters beyond convenience. HOSE's statements endpoint answers HTTP 200 with an
empty set unless the caller sends the SPA's own access token, and replaying someone's
token is a decision for the project owner, not a tooling choice (see
`docs/data-sources.md`). A folder of downloaded filings reaches the same numbers from the
same documents and needs no such decision.

**Ingest stores bytes and nothing else.** The file is hashed, copied into the raw store
under its hash, and recorded — no parsing, no interpretation. Extraction then runs against
captured bytes, which is what makes it safe to ship an imperfect extractor: a parsing fix
is a re-run, never a re-fetch, and the document cannot change underneath it.

Expected layout, where the period and type come from the filename so a human filing a
document does not have to touch a config file:

    pipeline/data/filings/AGM/2024-Y-audited-balance-sheet.xlsx
    pipeline/data/filings/AGM/2024-Q3-income-statement.pdf
                           ^    ^   ^
                           |    |   free text, kept as the title
                           |    period: Y, H1, H2, Q1..Q4
                           fiscal year
"""

import re
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import structlog

from quantumedge.hashing import sha256_hex
from quantumedge.ingest.base import RawPayload

log = structlog.get_logger(__name__)

SOURCE_CODE = "company_filing"
DOC_TYPE = "financial_statement"

# Formats worth attempting, in order of how reliably they extract. A spreadsheet carries
# the figures as numbers; a PDF carries a picture of them, and a scanned PDF carries a
# picture of a picture. YAML is a statement transcribed by hand, for the scanned case -
# cheaper than OCR and far more honest, since every figure cites its page.
SUPPORTED_SUFFIXES = {".xlsx", ".xls", ".pdf", ".yaml", ".yml"}

# <fiscal_year>-<period>-<free text>
FILENAME_PATTERN = re.compile(
    r"^(?P<year>(?:19|20)\d{2})[-_](?P<period>Y|H1|H2|Q[1-4])[-_](?P<title>.+)$",
    re.IGNORECASE,
)

PERIOD_TYPES = {"Y": "Y", "H1": "H", "H2": "H", "Q1": "Q", "Q2": "Q", "Q3": "Q", "Q4": "Q"}
FISCAL_QUARTERS = {"Q1": 1, "Q2": 2, "Q3": 3, "Q4": 4}


class FilingNameError(ValueError):
    """The filename does not say which period it covers."""


@dataclass(frozen=True)
class ParsedName:
    fiscal_year: int
    period_label: str
    period_type: str
    fiscal_quarter: int | None
    title: str

    @property
    def period_key(self) -> str:
        """Identity of the period this document covers, for the raw-store unique key."""
        return f"{self.fiscal_year}-{self.period_label}"


def parse_filename(stem: str) -> ParsedName:
    """Read the fiscal period out of a filename, or refuse.

    Refusing is deliberate. Inferring the period from the file's contents would mean
    parsing before storing, and guessing it from the modification date would attach real
    figures to the wrong year — a wrong number with clean provenance.
    """
    m = FILENAME_PATTERN.match(stem)
    if not m:
        raise FilingNameError(
            f"cannot read a fiscal period from '{stem}'. "
            "Expected <year>-<period>-<description>, e.g. 2024-Y-audited-financial-statements "
            "or 2024-Q3-income-statement. Period is one of Y, H1, H2, Q1..Q4."
        )
    label = m.group("period").upper()
    return ParsedName(
        fiscal_year=int(m.group("year")),
        period_label=label,
        period_type=PERIOD_TYPES[label],
        fiscal_quarter=FISCAL_QUARTERS.get(label),
        title=m.group("title").replace("-", " ").replace("_", " ").strip(),
    )


@dataclass
class FilingFound:
    symbol: str
    path: Path
    parsed: ParsedName
    content_hash: str
    byte_size: int
    stored_path: Path | None = None


@dataclass
class ScanOutcome:
    found: list[FilingFound] = field(default_factory=list)
    rejected: list[str] = field(default_factory=list)

    def as_stats(self) -> dict[str, Any]:
        stats: dict[str, Any] = {"filings_found": len(self.found)}
        if self.rejected:
            stats["rejected"] = self.rejected[:20]
        return stats


def scan(filings_dir: Path, *, symbols: list[str] | None = None) -> ScanOutcome:
    """Find filings on disk and hash them. Reads bytes; writes nothing."""
    outcome = ScanOutcome()
    if not filings_dir.exists():
        return outcome

    wanted = {s.upper() for s in symbols} if symbols else None

    for symbol_dir in sorted(p for p in filings_dir.iterdir() if p.is_dir()):
        symbol = symbol_dir.name.upper()
        if wanted and symbol not in wanted:
            continue

        for path in sorted(symbol_dir.iterdir()):
            if not path.is_file():
                continue
            if path.suffix.lower() not in SUPPORTED_SUFFIXES:
                outcome.rejected.append(f"{symbol}/{path.name}: unsupported type")
                continue
            try:
                parsed = parse_filename(path.stem)
            except FilingNameError as exc:
                outcome.rejected.append(f"{symbol}/{path.name}: {exc}")
                continue

            data = path.read_bytes()
            outcome.found.append(
                FilingFound(
                    symbol=symbol,
                    path=path,
                    parsed=parsed,
                    # The bytes themselves, not a canonical JSON form: this is a binary
                    # document, and its hash is its identity.
                    content_hash=sha256_hex(data),
                    byte_size=len(data),
                )
            )

    log.info("filings.scanned", found=len(outcome.found), rejected=len(outcome.rejected))
    return outcome


def archive(found: FilingFound, store_dir: Path) -> Path:
    """Copy a filing into the raw store, named by its hash.

    Content-addressed on purpose: the same document filed twice under different filenames
    lands in one place, and a stored file can never be silently replaced by different
    bytes — `raw_document` is append-only and a changed document is a new hash, so both
    versions remain inspectable.
    """
    store_dir.mkdir(parents=True, exist_ok=True)
    target = store_dir / f"{found.content_hash}{found.path.suffix.lower()}"
    if not target.exists():
        shutil.copy2(found.path, target)
    return target


def to_payload(found: FilingFound, stored: Path) -> RawPayload:
    """The raw_document row for a stored filing.

    `payload_path` rather than `payload`: the body is a spreadsheet or a PDF, not JSON.
    The small JSON body carries only what is needed to find and describe the file.
    """
    return RawPayload(
        source_code=SOURCE_CODE,
        doc_type=DOC_TYPE,
        body={
            "stored_path": str(stored),
            "original_filename": found.path.name,
            "title": found.parsed.title,
            "fiscal_year": found.parsed.fiscal_year,
            "period_label": found.parsed.period_label,
            "period_type": found.parsed.period_type,
            "fiscal_quarter": found.parsed.fiscal_quarter,
            "byte_size": found.byte_size,
            "file_sha256": found.content_hash,
        },
        symbol=found.symbol,
        period_key=found.parsed.period_key,
        source_url=None,
        http_status=None,
    )
