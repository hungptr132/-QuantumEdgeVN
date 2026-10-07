"""Properties of the migrations themselves, not of a migrated database.

These exist because the test suite migrates a database that already exists, so it cannot
notice a migration that only works on a database someone prepared by hand. A cold start -
`createdb` then `alembic upgrade head` and nothing else - is the documented recovery path
in `docs/BACKUP.md`, and it failed the first time it was actually tried:
`ticker_alias_trgm` needs an operator class from pg_trgm, and nothing in the migration
created the extension.

`scripts/verify-cold-start.ps1` runs the real drill against a throwaway database. These
tests are the cheap guard that keeps the drill from regressing between runs.
"""

import re
import tomllib
from pathlib import Path

PIPELINE_ROOT = Path(__file__).resolve().parents[1]
MIGRATIONS = PIPELINE_ROOT / "migrations" / "versions"

# Index operator classes that only exist once an extension is installed.
EXTENSION_OPS = {
    "gin_trgm_ops": "pg_trgm",
    "gist_trgm_ops": "pg_trgm",
}


def _initial() -> str:
    found = sorted(MIGRATIONS.glob("0001_*.py"))
    assert found, "the initial migration should be named 0001_*"
    return found[0].read_text(encoding="utf-8")


def _upgrade_body(source: str) -> str:
    """The executable statements of `upgrade()`, comments stripped.

    Both matter. Scoping to `upgrade()` keeps `downgrade()` - which drops the index and so
    mentions the operator class - from being read as a use. Stripping comments keeps a
    comment *about* the operator class from counting as one, which is how the first
    version of this test failed against correct code.
    """
    start = source.index("def upgrade()")
    end = source.index("def downgrade()")
    body = source[start:end]
    lines = []
    for line in body.splitlines():
        code = line.split("#", 1)[0] if not line.lstrip().startswith("#") else ""
        lines.append(code)
    return "\n".join(lines)


class TestMigrationsAreSelfSufficient:
    def test_the_initial_migration_creates_the_extensions_it_depends_on(self) -> None:
        source = _upgrade_body(_initial())
        for op_class, extension in EXTENSION_OPS.items():
            if op_class not in source:
                continue
            pattern = rf"CREATE EXTENSION IF NOT EXISTS {extension}\b"
            assert re.search(pattern, source), (
                f"the migration uses {op_class} but never creates {extension}. "
                "A fresh database would fail with 'operator class does not exist', which "
                "says nothing about the missing extension."
            )

    def test_extensions_are_created_before_the_index_that_needs_them(self) -> None:
        """Ordering, not just presence: Postgres resolves the operator class at CREATE
        INDEX time."""
        source = _upgrade_body(_initial())
        for op_class, extension in EXTENSION_OPS.items():
            if op_class not in source:
                continue
            created = source.index(f"CREATE EXTENSION IF NOT EXISTS {extension}")
            used = source.index(op_class)
            assert created < used, f"{extension} is created after {op_class} is used"

    def test_enums_are_created_before_any_table(self) -> None:
        """Same class of failure: a table referencing a type that does not exist yet.
        Autogenerate gets this wrong, which is why the enums are emitted by hand."""
        source = _upgrade_body(_initial())
        assert "CREATE TYPE" in source
        assert source.index("CREATE TYPE") < source.index("op.create_table")

    def test_every_migration_has_a_downgrade(self) -> None:
        """Not for routine use - this project rolls forward - but a migration that cannot
        be reversed cannot be tested in isolation either."""
        for path in sorted(MIGRATIONS.glob("*.py")):
            source = path.read_text(encoding="utf-8")
            assert "def downgrade()" in source, path.name

    def test_revisions_form_a_single_chain(self) -> None:
        """Two heads make `alembic upgrade head` ambiguous, and the failure appears only
        on a fresh database where both branches must run."""
        revisions: dict[str, str | None] = {}
        for path in sorted(MIGRATIONS.glob("*.py")):
            source = path.read_text(encoding="utf-8")
            rev = re.search(r"^revision: str = ['\"]([^'\"]+)['\"]", source, re.M)
            down = re.search(r"^down_revision: [^=]+= ['\"]?([^'\"\n]+)['\"]?", source, re.M)
            assert rev, f"{path.name} has no revision id"
            revisions[rev.group(1)] = (
                None if not down or down.group(1).strip() == "None" else down.group(1)
            )

        heads = set(revisions) - {d for d in revisions.values() if d}
        assert len(heads) == 1, f"expected one head, found {sorted(heads)}"
        roots = [r for r, d in revisions.items() if d is None]
        assert len(roots) == 1, f"expected one root, found {roots}"


class TestMigrationsStayInsideTheLinter:
    """Revision 5c77d43f28ec shipped `import sqlalchemy as s` while its body called
    `sa.Column`, so every name in its generated DDL was undefined. The suite stayed green
    because the test database already had the revision applied; the failure appeared only
    on a cold start, which is the one path a recovery depends on.

    It was invisible because `extend-exclude` kept `migrations/versions` out of ruff
    entirely. The reason written beside it was about not *formatting* generated DDL, which
    is still right - excluding them from *linting* was collateral damage. F821 finds this
    class of bug in about a second, so the only thing worth guarding is the config itself.
    """

    def test_linting_is_not_excluded(self) -> None:
        config = tomllib.loads((PIPELINE_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
        ruff = config["tool"]["ruff"]

        for key in ("exclude", "extend-exclude"):
            excluded = ruff.get(key, [])
            assert not any("migrations" in pattern for pattern in excluded), (
                f"[tool.ruff] {key} hides migrations from the linter as well as the "
                "formatter; put the pattern under [tool.ruff.format] instead"
            )

    def test_formatting_is_still_excluded(self) -> None:
        """The other half: reformatting generated DDL risks changing semantics for no
        benefit, and those long column definitions are inherent."""
        config = tomllib.loads((PIPELINE_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
        excluded = config["tool"]["ruff"]["format"]["exclude"]

        assert any("migrations" in pattern for pattern in excluded)
