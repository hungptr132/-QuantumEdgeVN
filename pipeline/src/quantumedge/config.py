from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

# config.py -> quantumedge -> src -> pipeline -> repo root
REPO_ROOT = Path(__file__).resolve().parents[3]


class Settings(BaseSettings):
    """Configuration loaded from the repo-root .env.

    Every component reads its DSN from here, and nothing in the codebase references
    localhost, a port, or Docker — so moving to managed Postgres is a one-variable change.
    """

    model_config = SettingsConfigDict(
        env_file=REPO_ROOT / ".env",
        env_prefix="QE_",
        extra="ignore",
    )

    database_url: str
    test_database_url: str | None = None

    # All timestamps are stored UTC. This is applied only at the business-date
    # boundary, so a developer's machine timezone can never change a computed metric.
    timezone: str = "Asia/Ho_Chi_Minh"

    data_dir: Path = Field(default=REPO_ROOT / "pipeline" / "data")
    rules_dir: Path = Field(default=REPO_ROOT / "pipeline" / "rules")
    seeds_dir: Path = Field(default=REPO_ROOT / "pipeline" / "seeds")
    factsheets_dir: Path = Field(default=REPO_ROOT / "factsheets")
    contracts_dir: Path = Field(default=REPO_ROOT / "contracts")


def load_settings() -> Settings:
    return Settings()  # type: ignore[call-arg]
