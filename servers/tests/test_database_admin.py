from types import SimpleNamespace
from unittest.mock import patch

import pytest
from rest_framework.test import APIClient

from accounts.models import User
from servers import database_admin
from servers.database_admin import DatabaseAdminError
from servers.models import Server, ServerTemplate


@pytest.fixture
def api_client(settings):
    settings.ALLOWED_HOSTS = ["testserver"]
    return APIClient()


@pytest.fixture
def owner(db):
    return User.objects.create_user(username="db-owner", password="password")


@pytest.fixture
def template(db):
    return ServerTemplate.objects.create(name="Database", cpu_limit="0.50", memory_mb=256)


@pytest.fixture
def postgres_server(owner, template):
    server = Server.objects.create(
        owner=owner,
        name="database",
        image=Server.Image.POSTGRES,
        template=template,
        host_port=15432,
        env_vars={
            "POSTGRES_DB": "app",
            "POSTGRES_USER": "app",
            "POSTGRES_PASSWORD": "secret",
        },
    )
    server.transition_to(Server.Status.RUNNING)
    return server


class FakeCursor:
    def __init__(self, connection, result):
        self.connection = connection
        self.result = result
        self.description = None
        self.statusmessage = "SELECT 1"

    def execute(self, statement, parameters=None):
        if not isinstance(statement, str):
            statement = statement.as_string()
        self.connection.statements.append(statement)
        self.connection.parameters.append(parameters)
        if self.result["columns"]:
            self.description = [SimpleNamespace(name=name) for name in self.result["columns"]]
        return self

    def fetchone(self):
        return self.result["row"]

    def fetchall(self):
        return self.result["rows"]

    def fetchmany(self, size):
        return self.result["rows"][:size]

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


class FakeConnection:
    """Minimal psycopg stand-in that returns canned result sets in order."""

    def __init__(self, *results):
        self.results = list(results)
        self.statements = []
        self.parameters = []

    def _next(self):
        if self.results:
            return self.results.pop(0)
        return {"rows": [], "row": None, "columns": []}

    def execute(self, statement, parameters=None):
        return FakeCursor(self, self._next()).execute(statement, parameters)

    def cursor(self, **kwargs):
        return FakeCursor(self, self._next())

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


def connect_to(monkeypatch, *results):
    connection = FakeConnection(*results)
    monkeypatch.setattr(database_admin, "_connection", lambda *args, **kwargs: connection)
    return connection


def test_connection_rejects_non_postgres_servers(owner, template):
    server = Server.objects.create(
        owner=owner, name="web", image=Server.Image.NGINX, template=template, host_port=15433
    )
    server.transition_to(Server.Status.RUNNING)

    with pytest.raises(DatabaseAdminError, match="only available for PostgreSQL"):
        database_admin.get_overview(server)


def test_connection_rejects_stopped_databases(postgres_server):
    postgres_server.transition_to(Server.Status.STOPPED)

    with pytest.raises(DatabaseAdminError, match="Start the server"):
        database_admin.get_overview(postgres_server)


def test_connection_rejects_missing_password(postgres_server):
    postgres_server.env_vars = {"POSTGRES_DB": "app", "POSTGRES_USER": "app"}
    postgres_server.save(update_fields=["env_vars"])

    with pytest.raises(DatabaseAdminError, match="credentials are missing"):
        database_admin.get_overview(postgres_server)


def test_identifier_validation_blocks_injection():
    assert database_admin._validate_identifier("public") == "public"
    assert database_admin._validate_identifier("my_table2") == "my_table2"

    for invalid in ("", "users; DROP TABLE x", "users--", 'users"table', "1table", "a b"):
        with pytest.raises(DatabaseAdminError):
            database_admin._validate_identifier(invalid)


def test_column_type_validation_accepts_common_postgres_types():
    for column_type in ("serial", "integer", "varchar(255)", "numeric(10,2)", "text[]", "timestamp with time zone"):
        assert database_admin._validate_column_type(column_type) == column_type

    for invalid in ("", "text; DROP TABLE x", "varchar(abc)", "1nteger", "text--"):
        with pytest.raises(DatabaseAdminError):
            database_admin._validate_column_type(invalid)


def test_jsonable_converts_database_types():
    from datetime import datetime, timezone
    from decimal import Decimal
    from uuid import UUID

    value = {
        "amount": Decimal("12.50"),
        "when": datetime(2026, 1, 2, 3, 4, tzinfo=timezone.utc),
        "id": UUID("00000000-0000-0000-0000-000000000001"),
        "blob": b"\x01\xff",
    }

    assert database_admin._jsonable(value) == {
        "amount": "12.50",
        "when": "2026-01-02T03:04:00+00:00",
        "id": "00000000-0000-0000-0000-000000000001",
        "blob": "01ff",
    }


def test_create_table_quotes_identifiers_and_applies_defaults(monkeypatch, postgres_server):
    connection = connect_to(monkeypatch)

    database_admin.create_table(
        postgres_server,
        "public",
        "customers",
        [
            {"name": "id", "type": "serial", "nullable": False, "primary_key": True},
            {"name": "name", "type": "varchar(80)", "nullable": False, "default": "'anonymous'"},
        ],
    )

    statement = connection.statements[0]
    assert 'CREATE TABLE "public"."customers"' in statement
    assert '"id" serial NOT NULL' in statement
    assert '"name" varchar(80) NOT NULL DEFAULT \'anonymous\'' in statement
    assert 'PRIMARY KEY ("id")' in statement


def test_create_table_rejects_defaults_with_multiple_statements(monkeypatch, postgres_server):
    connect_to(monkeypatch)

    with pytest.raises(DatabaseAdminError, match="defaults cannot contain"):
        database_admin.create_table(
            postgres_server,
            "public",
            "customers",
            [{"name": "id", "type": "integer", "default": "1; DROP TABLE x"}],
        )


def test_create_table_requires_columns(monkeypatch, postgres_server):
    connect_to(monkeypatch)

    with pytest.raises(DatabaseAdminError, match="at least one column"):
        database_admin.create_table(postgres_server, "public", "customers", [])


def test_get_table_paginates_and_uses_primary_key(monkeypatch, postgres_server):
    columns = [
        {"name": "id", "type": "integer", "nullable": False, "default_value": None,
         "identity": "", "generated": "", "primary_key": True, "position": 1},
        {"name": "name", "type": "text", "nullable": True, "default_value": None,
         "identity": "", "generated": "", "primary_key": False, "position": 2},
    ]
    connection = connect_to(
        monkeypatch,
        {"rows": columns, "row": None, "columns": ["name"]},
        {"rows": [], "row": {"count": 12}, "columns": ["count"]},
        {"rows": [{"id": 1, "name": "Ada"}], "row": None, "columns": ["id", "name"]},
    )

    result = database_admin.get_table(postgres_server, "public", "customers", limit=5, offset=10)

    assert result["row_key"] == ["id"]
    assert result["total_rows"] == 12
    assert result["rows"] == [{"id": 1, "name": "Ada"}]
    select_statement = connection.statements[-1]
    assert select_statement.startswith('SELECT * FROM "public"."customers"')
    assert select_statement.endswith("ORDER BY \"id\" LIMIT %s OFFSET %s")
    assert connection.parameters[-1] == (5, 10)


def test_get_table_falls_back_to_ctid_without_primary_key(monkeypatch, postgres_server):
    columns = [
        {"name": "label", "type": "text", "nullable": True, "default_value": None,
         "identity": "", "generated": "", "primary_key": False, "position": 1},
    ]
    connection = connect_to(
        monkeypatch,
        {"rows": columns, "row": None, "columns": ["name"]},
        {"rows": [], "row": {"count": 0}, "columns": ["count"]},
        {"rows": [], "row": None, "columns": ["label", "__ctid"]},
    )

    result = database_admin.get_table(postgres_server, "public", "logs")

    assert result["row_key"] == ["__ctid"]
    assert "ctid::text AS __ctid" in connection.statements[-1]
    assert connection.statements[-1].endswith("ORDER BY ctid LIMIT %s OFFSET %s")


def test_run_query_reports_rows_and_truncation(monkeypatch, postgres_server):
    monkeypatch.setattr(database_admin, "MAX_QUERY_ROWS", 2)
    connect_to(
        monkeypatch,
        {"rows": [(1, "a"), (2, "b"), (3, "c")], "row": None, "columns": ["id", "name"]},
    )

    result = database_admin.run_query(postgres_server, "SELECT id, name FROM customers")

    assert result["columns"] == ["id", "name"]
    assert result["row_count"] == 2
    assert result["truncated"] is True
    assert result["rows"] == [[1, "a"], [2, "b"]]
    assert result["duration_ms"] >= 0


def test_run_query_rejects_empty_statements(postgres_server):
    with pytest.raises(DatabaseAdminError, match="Enter a SQL statement"):
        database_admin.run_query(postgres_server, "   ")


def test_run_query_wraps_database_errors(monkeypatch, postgres_server):
    connection = FakeConnection(
        {"rows": [], "row": None, "columns": []},
    )

    class FailingCursor(FakeCursor):
        def execute(self, statement, parameters=None):
            raise database_admin.psycopg.Error('relation "nope" does not exist')

    monkeypatch.setattr(database_admin, "_connection", lambda *args, **kwargs: connection)
    monkeypatch.setattr(connection, "cursor", lambda **kwargs: FailingCursor(connection, {"rows": []}))

    with pytest.raises(DatabaseAdminError, match="does not exist"):
        database_admin.run_query(postgres_server, "SELECT * FROM nope")


def test_database_overview_endpoint_delegates(api_client, owner, postgres_server):
    api_client.force_authenticate(user=owner)
    payload = {"database": "app", "server_version": "16.12", "databases": [], "tables": []}
    with patch("api.views.database_admin.get_overview", return_value=payload) as overview:
        response = api_client.get(f"/api/servers/{postgres_server.pk}/database/?database=app")

    assert response.status_code == 200
    assert response.data == payload
    overview.assert_called_once_with(postgres_server, "app")


def test_database_overview_endpoint_returns_readable_errors(api_client, owner, postgres_server):
    api_client.force_authenticate(user=owner)
    postgres_server.transition_to(Server.Status.STOPPED)

    response = api_client.get(f"/api/servers/{postgres_server.pk}/database/")

    assert response.status_code == 400
    assert "Start the server" in response.data["detail"]


def test_database_endpoints_are_scoped_to_the_owner(api_client, postgres_server):
    other = User.objects.create_user(username="db-other", password="password")
    api_client.force_authenticate(user=other)

    response = api_client.get(f"/api/servers/{postgres_server.pk}/database/")

    assert response.status_code == 404


def test_database_query_endpoint_passes_sql(api_client, owner, postgres_server):
    api_client.force_authenticate(user=owner)
    result = {"columns": ["n"], "rows": [[1]], "row_count": 1, "truncated": False, "command": "SELECT 1"}
    with patch("api.views.database_admin.run_query", return_value=result) as run:
        response = api_client.post(
            f"/api/servers/{postgres_server.pk}/database/query/",
            {"sql": "SELECT 1 AS n", "database": "app"},
            format="json",
        )

    assert response.status_code == 200
    run.assert_called_once_with(postgres_server, "SELECT 1 AS n", "app")


def test_database_table_delete_endpoint_drops_table(api_client, owner, postgres_server):
    api_client.force_authenticate(user=owner)
    with patch("api.views.database_admin.drop_table", return_value={"schema": "public", "table": "customers"}) as drop:
        response = api_client.delete(f"/api/servers/{postgres_server.pk}/database/tables/public/customers/")

    assert response.status_code == 200
    drop.assert_called_once_with(postgres_server, "public", "customers", None)


def test_database_insert_row_endpoint_uses_row_payload(api_client, owner, postgres_server):
    api_client.force_authenticate(user=owner)
    with patch("api.views.database_admin.insert_row", return_value={"row": {"id": 1}}) as insert:
        response = api_client.post(
            f"/api/servers/{postgres_server.pk}/database/tables/public/customers/rows/",
            {"values": {"name": "Ada"}, "database": "app"},
            format="json",
        )

    assert response.status_code == 201
    insert.assert_called_once_with(postgres_server, "public", "customers", {"name": "Ada"}, "app")
