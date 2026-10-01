from collections.abc import Iterator
from contextlib import contextmanager

from sqlalchemy import Engine, create_engine
from sqlalchemy.orm import Session, sessionmaker

from quantumedge.config import Settings, load_settings

_engine: Engine | None = None
_session_factory: sessionmaker[Session] | None = None


def get_engine(settings: Settings | None = None) -> Engine:
    """Return the process-wide engine, creating it on first use."""
    global _engine
    if _engine is None:
        settings = settings or load_settings()
        _engine = create_engine(
            settings.database_url,
            # Pipeline runs are batch jobs, not a web server: a small pool is plenty,
            # and pre-ping avoids surprises when a run spans a cluster restart.
            pool_size=5,
            max_overflow=2,
            pool_pre_ping=True,
            future=True,
        )
    return _engine


@contextmanager
def session_scope() -> Iterator[Session]:
    """Transactional scope: commits on success, rolls back on any exception."""
    global _session_factory
    if _session_factory is None:
        _session_factory = sessionmaker(bind=get_engine(), expire_on_commit=False)
    session = _session_factory()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()
