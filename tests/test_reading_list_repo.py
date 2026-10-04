"""The reading-list repository against SQLite (and an unreachable Postgres for failure paths).

No real Neon database is used. Postgres-specific behavior is limited to URL handling and the
connection-failure path, which needs no server (it connects to a closed loopback port).
"""

import logging
from datetime import UTC, datetime, timedelta
from unittest.mock import patch

import pytest
from sqlalchemy import inspect
from sqlalchemy.exc import OperationalError

from app.schemas import Paper
from app.storage.reading_list import (
    ReadingListRepo,
    StorageError,
    make_engine,
    metadata,
    normalize_database_url,
)
from tests.fakes import DATABASE_SECRETS, failing_repo

CLIENT_A = "11111111-1111-4111-8111-111111111111"
CLIENT_B = "22222222-2222-4222-8222-222222222222"
T0 = datetime(2026, 10, 3, 12, 0, tzinfo=UTC)


def paper(doi="10.1000/one", **overrides) -> Paper:
    fields = dict(
        doi=doi,
        title="Software testing with language models",
        url=f"https://doi.org/{doi}",
        crossref_rank=0,
        authors=["Ada Lovelace", "Grace Hopper"],
        year=2024,
        venue="Journal of Testing",
        abstract="We study testing.",
        work_type="journal-article",
        missing_fields=[],
    )
    return Paper(**{**fields, **overrides})


class Ticking:
    """A clock that advances one minute per call, so insertion order is the time order."""

    def __init__(self):
        self.current = T0

    def __call__(self):
        self.current += timedelta(minutes=1)
        return self.current


@pytest.fixture
def repo(tmp_path):
    repository = ReadingListRepo(make_engine(f"sqlite:///{(tmp_path / 'list.db').as_posix()}"))
    yield repository
    repository.dispose()


def test_a_saved_paper_round_trips_with_every_field(repo):
    saved, created = repo.add(CLIENT_A, paper(authors=["Zoë Ångström", "李 雷"]))

    assert created is True
    assert (saved.doi, saved.title, saved.year, saved.venue) == (
        "10.1000/one",
        "Software testing with language models",
        2024,
        "Journal of Testing",
    )
    assert saved.authors == ["Zoë Ångström", "李 雷"]
    assert saved.url == "https://doi.org/10.1000/one" and saved.work_type == "journal-article"
    assert repo.list_papers(CLIENT_A) == [saved]
    assert repo.get(CLIENT_A, "10.1000/one") == saved


def test_missing_metadata_stays_missing_and_is_not_filled_in(repo):
    bare = paper(title=None, authors=[], year=None, venue=None, abstract=None, work_type=None)
    bare.missing_fields = ["title", "authors", "year", "abstract"]

    saved, _ = repo.add(CLIENT_A, bare)

    assert (saved.title, saved.authors, saved.year, saved.venue, saved.abstract) == (
        None,
        [],
        None,
        None,
        None,
    )
    assert saved.missing_fields == ["title", "authors", "year", "abstract"]


def test_saving_twice_is_not_an_error_and_keeps_the_original_row(repo):
    first, created_first = repo.add(CLIENT_A, paper(title="Original title"))
    second, created_second = repo.add(CLIENT_A, paper(title="Changed title"))

    assert (created_first, created_second) == (True, False)
    assert second == first and second.title == "Original title"
    assert len(repo.list_papers(CLIENT_A)) == 1


def test_list_is_newest_first(tmp_path):
    repository = ReadingListRepo(
        make_engine(f"sqlite:///{(tmp_path / 'order.db').as_posix()}"), now=Ticking()
    )
    for doi in ("10.1000/a", "10.1000/b", "10.1000/c"):
        repository.add(CLIENT_A, paper(doi))

    assert [p.doi for p in repository.list_papers(CLIENT_A)] == [
        "10.1000/c",
        "10.1000/b",
        "10.1000/a",
    ]
    repository.dispose()


def test_saved_at_is_utc_iso_8601(repo):
    saved, _ = repo.add(CLIENT_A, paper())

    parsed = datetime.fromisoformat(saved.saved_at)
    assert parsed.tzinfo is not None and parsed.utcoffset() == timedelta(0)


def test_remove_reports_whether_anything_was_removed(repo):
    repo.add(CLIENT_A, paper())

    assert repo.remove(CLIENT_A, "10.1000/one") is True
    assert repo.remove(CLIENT_A, "10.1000/one") is False
    assert repo.list_papers(CLIENT_A) == []


def test_clients_have_separate_lists(repo):
    repo.add(CLIENT_A, paper("10.1000/shared"))
    repo.add(CLIENT_B, paper("10.1000/shared", title="B's copy"))
    repo.add(CLIENT_A, paper("10.1000/only-a"))

    assert {p.doi for p in repo.list_papers(CLIENT_A)} == {"10.1000/shared", "10.1000/only-a"}
    assert [p.doi for p in repo.list_papers(CLIENT_B)] == ["10.1000/shared"]
    assert repo.remove(CLIENT_B, "10.1000/only-a") is False  # not B's paper
    assert repo.remove(CLIENT_A, "10.1000/shared") is True
    assert [p.doi for p in repo.list_papers(CLIENT_B)] == ["10.1000/shared"]  # B unaffected
    assert repo.get(CLIENT_B, "10.1000/only-a") is None


def test_data_survives_a_restart(tmp_path):
    url = f"sqlite:///{(tmp_path / 'restart.db').as_posix()}"
    before = ReadingListRepo(make_engine(url))
    saved, _ = before.add(CLIENT_A, paper())
    before.dispose()  # the "server" stops

    after = ReadingListRepo(make_engine(url))  # a new process, same database

    assert after.list_papers(CLIENT_A) == [saved]
    after.dispose()


def test_sql_looking_text_is_stored_as_data(repo):
    nasty = "x'); DROP TABLE saved_papers; --"
    repo.add(CLIENT_A, paper(title=nasty, venue=nasty, authors=[nasty]))

    saved = repo.list_papers(CLIENT_A)[0]
    assert saved.title == nasty and saved.authors == [nasty]
    assert repo.list_papers(CLIENT_B) == []  # the table still exists and works


def test_schema_has_a_composite_primary_key_and_a_list_index(repo):
    repo.ensure_schema()

    inspector = inspect(repo._engine)
    assert inspector.get_pk_constraint("saved_papers")["constrained_columns"] == [
        "client_id",
        "doi",
    ]
    indexed = {tuple(index["column_names"]) for index in inspector.get_indexes("saved_papers")}
    assert ("client_id", "saved_at") in indexed
    required = {"client_id", "doi", "url", "authors", "missing_fields", "saved_at"}
    assert required <= {c.name for c in metadata.tables["saved_papers"].columns if not c.nullable}


def test_the_database_enforces_one_row_per_paper_per_list(repo):
    from sqlalchemy import insert
    from sqlalchemy.exc import IntegrityError

    from app.storage.reading_list import saved_papers

    repo.add(CLIENT_A, paper())
    row = dict(
        client_id=CLIENT_A,
        doi="10.1000/one",
        authors="[]",
        url="u",
        missing_fields="[]",
        saved_at=T0,
    )

    with pytest.raises(IntegrityError), repo._engine.begin() as conn:
        conn.execute(insert(saved_papers).values(**row))


@pytest.mark.parametrize(
    ("given", "expected"),
    [
        ("postgres://u:p@host/db", "postgresql+psycopg://u:p@host/db"),
        (
            "postgresql://u:p@host/db?sslmode=require",
            "postgresql+psycopg://u:p@host/db?sslmode=require",
        ),
        ("postgresql+psycopg://u:p@host/db", "postgresql+psycopg://u:p@host/db"),
        ("sqlite:///./data/app.db", "sqlite:///./data/app.db"),
    ],
)
def test_database_urls_from_neon_and_render_are_mapped_to_the_psycopg_driver(given, expected):
    assert normalize_database_url(given) == expected


def test_sqlite_file_databases_get_their_directory_created(tmp_path):
    target = tmp_path / "nested" / "dir" / "app.db"

    repository = ReadingListRepo(make_engine(f"sqlite:///{target.as_posix()}"))
    repository.add(CLIENT_A, paper())

    assert target.exists()
    repository.dispose()


# --- Failure behavior ----------------------------------------------------------------------------


def test_a_driver_error_full_of_connection_details_becomes_a_detail_free_storage_error(caplog):
    repository = failing_repo()

    with caplog.at_level(logging.DEBUG), pytest.raises(StorageError) as exc_info:
        repository.list_papers(CLIENT_A)

    shown = f"{exc_info.value} {exc_info.value.message} {exc_info.value.code} {caplog.text}"
    for secret in DATABASE_SECRETS:
        assert secret not in shown
    assert exc_info.value.code == "storage_unavailable" and exc_info.value.retryable is True
    assert exc_info.value.__cause__ is None and exc_info.value.__suppress_context__
    assert "OperationalError" in caplog.text  # the class is logged, nothing else


@pytest.mark.parametrize("operation", ["add", "get", "list", "remove"])
def test_every_operation_reports_database_failure_as_storage_error(operation):
    repository = failing_repo()
    calls = {
        "add": lambda: repository.add(CLIENT_A, paper()),
        "get": lambda: repository.get(CLIENT_A, "10.1000/one"),
        "list": lambda: repository.list_papers(CLIENT_A),
        "remove": lambda: repository.remove(CLIENT_A, "10.1000/one"),
    }

    with pytest.raises(StorageError):
        calls[operation]()


def test_ping_is_false_when_the_database_is_down_and_never_raises():
    assert failing_repo().ping() is False


def test_ping_is_true_for_a_working_database(repo):
    assert repo.ping() is True


def test_schema_creation_is_retried_after_a_failure(repo):
    failure = OperationalError("CREATE TABLE ...", {}, Exception("boom with secret-host"))
    real_create_all = metadata.create_all
    attempts = []

    def flaky(bind, **kwargs):
        attempts.append(1)
        if len(attempts) == 1:
            raise failure  # the database was not reachable on first use
        return real_create_all(bind, **kwargs)

    with patch.object(metadata, "create_all", side_effect=flaky):
        with pytest.raises(StorageError):
            repo.list_papers(CLIENT_A)
        assert repo._schema_ready is False

        assert repo.list_papers(CLIENT_A) == []  # the retry created the table and worked
        assert repo._schema_ready is True
