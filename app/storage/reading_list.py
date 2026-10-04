"""Reading-list persistence: SQLAlchemy Core over SQLite (local/tests) or Postgres (Neon).

One table, explicit schema, parameterized queries only (no SQL string building). Every database
failure becomes a `StorageError` with a fixed message: connection strings, hostnames, user names
and driver error text never leave this module.

Rows are scoped by `client_id`, a random id the browser generates. That is separation between
browsers, NOT authentication or authorization: anyone holding an id can read and change that
list, and the stored data is public bibliographic metadata only.
"""

import json
import logging
import threading
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import TypeVar

from sqlalchemy import (
    Column,
    DateTime,
    Engine,
    Index,
    Integer,
    MetaData,
    PrimaryKeyConstraint,
    String,
    Table,
    Text,
    create_engine,
    delete,
    insert,
    select,
    text,
)
from sqlalchemy.engine import make_url
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.pool import StaticPool

from app.schemas import Paper, SavedPaper

log = logging.getLogger(__name__)

T = TypeVar("T")

metadata = MetaData()

saved_papers = Table(
    "saved_papers",
    metadata,
    Column("client_id", String(36), nullable=False),  # browser-generated UUID (not a credential)
    Column("doi", String(255), nullable=False),  # lowercase, validated by normalize_doi
    Column("title", Text),  # NULL when Crossref has no title: missing stays missing
    Column("authors", Text, nullable=False),  # JSON array of display names
    Column("year", Integer),
    Column("venue", Text),
    Column("url", Text, nullable=False),
    Column("abstract", Text),
    Column("work_type", String(64)),
    Column("missing_fields", Text, nullable=False),  # JSON array
    Column("saved_at", DateTime(timezone=True), nullable=False),
    PrimaryKeyConstraint("client_id", "doi", name="pk_saved_papers"),  # one row per paper per list
    Index("ix_saved_papers_client_saved_at", "client_id", "saved_at"),
)


class StorageError(Exception):
    """The database is unavailable or a query failed. Safe to show; carries no detail."""

    code = "storage_unavailable"
    message = "The reading list is temporarily unavailable. Please try again shortly."
    retryable = True


def normalize_database_url(url: str) -> str:
    """Neon and Render hand out `postgres://` / `postgresql://`; we use the psycopg 3 driver."""
    if url.startswith("postgres://"):
        url = "postgresql://" + url[len("postgres://") :]
    if url.startswith("postgresql://"):
        url = "postgresql+psycopg://" + url[len("postgresql://") :]
    return url


def make_engine(database_url: str) -> Engine:
    url = make_url(normalize_database_url(database_url))
    if url.get_backend_name() == "sqlite":
        in_memory = url.database in (None, "", ":memory:")
        if not in_memory:
            Path(url.database).parent.mkdir(parents=True, exist_ok=True)
        return create_engine(
            url,
            connect_args={"check_same_thread": False},  # FastAPI runs sync code in worker threads
            poolclass=StaticPool if in_memory else None,
        )
    return create_engine(
        url,
        pool_pre_ping=True,  # Neon suspends idle databases and drops their connections
        pool_recycle=240,
        connect_args={"connect_timeout": 10},
    )


class ReadingListRepo:
    def __init__(self, engine: Engine, *, now: Callable[[], datetime] | None = None):
        self._engine = engine
        self._now = now or (lambda: datetime.now(UTC))
        self._schema_ready = False
        self._schema_lock = threading.Lock()

    # ---- operations ---------------------------------------------------------------------------

    def get(self, client_id: str, doi: str) -> SavedPaper | None:
        def run() -> SavedPaper | None:
            with self._engine.connect() as conn:
                row = conn.execute(self._by_key(client_id, doi)).mappings().first()
            return _to_saved(row) if row else None

        return self._guarded(run)

    def add(self, client_id: str, paper: Paper) -> tuple[SavedPaper, bool]:
        """Save `paper` to this client's list. Returns (saved paper, created).

        `created` is False when it was already saved, in which case the stored row is returned
        unchanged. Saving twice is not an error.
        """

        def run() -> tuple[SavedPaper, bool]:
            values = {
                "client_id": client_id,
                "doi": paper.doi,
                "title": paper.title,
                "authors": json.dumps(paper.authors, ensure_ascii=False),
                "year": paper.year,
                "venue": paper.venue,
                "url": paper.url,
                "abstract": paper.abstract,
                "work_type": paper.work_type,
                "missing_fields": json.dumps(paper.missing_fields),
                "saved_at": self._now(),
            }
            try:
                with self._engine.begin() as conn:
                    if conn.execute(self._by_key(client_id, paper.doi)).first() is None:
                        conn.execute(insert(saved_papers).values(**values))
                        created = True
                    else:
                        created = False
            except IntegrityError:
                created = False  # a concurrent request saved the same paper first
            with self._engine.connect() as conn:
                row = conn.execute(self._by_key(client_id, paper.doi)).mappings().one()
            return _to_saved(row), created

        return self._guarded(run)

    def list_papers(self, client_id: str) -> list[SavedPaper]:
        """This client's papers, newest first."""

        def run() -> list[SavedPaper]:
            query = (
                select(saved_papers)
                .where(saved_papers.c.client_id == client_id)
                .order_by(saved_papers.c.saved_at.desc(), saved_papers.c.doi.asc())
            )
            with self._engine.connect() as conn:
                return [_to_saved(row) for row in conn.execute(query).mappings()]

        return self._guarded(run)

    def remove(self, client_id: str, doi: str) -> bool:
        """Remove a paper from this client's list. False if it wasn't there."""

        def run() -> bool:
            statement = delete(saved_papers).where(
                saved_papers.c.client_id == client_id, saved_papers.c.doi == doi
            )
            with self._engine.begin() as conn:
                return conn.execute(statement).rowcount > 0

        return self._guarded(run)

    # ---- lifecycle ----------------------------------------------------------------------------

    def ensure_schema(self) -> None:
        """Create the table if needed. Idempotent, and retried on later calls if it fails."""
        self._guarded(lambda: None)

    def ping(self) -> bool:
        """True if the database answers a trivial query. Never raises."""
        try:
            self._guarded(self._ping)
        except StorageError:
            return False
        return True

    def dispose(self) -> None:
        self._engine.dispose()

    @property
    def backend(self) -> str:
        """ "sqlite" or "postgresql": which database this repository is using."""
        return self._engine.dialect.name

    # ---- internals ----------------------------------------------------------------------------

    def _ping(self) -> None:
        with self._engine.connect() as conn:
            conn.execute(text("SELECT 1"))

    @staticmethod
    def _by_key(client_id: str, doi: str):
        return select(saved_papers).where(
            saved_papers.c.client_id == client_id, saved_papers.c.doi == doi
        )

    def _guarded(self, run: Callable[[], T]) -> T:
        """Run a database operation; turn any database failure into a detail-free StorageError."""
        try:
            self._create_schema_once()
            return run()
        except SQLAlchemyError as exc:
            # The exception class only: driver messages can contain hosts and user names.
            log.error("Reading-list database error (%s)", type(exc).__name__)
            raise StorageError() from None

    def _create_schema_once(self) -> None:
        if self._schema_ready:
            return
        with self._schema_lock:
            if not self._schema_ready:
                metadata.create_all(self._engine)
                self._schema_ready = True


def _to_saved(row) -> SavedPaper:
    saved_at = row["saved_at"]
    if saved_at.tzinfo is None:  # SQLite returns naive datetimes; everything is stored as UTC
        saved_at = saved_at.replace(tzinfo=UTC)
    return SavedPaper(
        doi=row["doi"],
        title=row["title"],
        url=row["url"],
        authors=_json_list(row["authors"]),
        year=row["year"],
        venue=row["venue"],
        abstract=row["abstract"],
        work_type=row["work_type"],
        missing_fields=_json_list(row["missing_fields"]),
        saved_at=saved_at.astimezone(UTC).isoformat(),
    )


def _json_list(value: str) -> list[str]:
    try:
        data = json.loads(value)
    except (TypeError, ValueError):
        return []
    return [item for item in data if isinstance(item, str)] if isinstance(data, list) else []
