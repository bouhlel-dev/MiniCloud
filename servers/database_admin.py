"""PostgreSQL administration helpers for the visual database manager.

The managed PostgreSQL container publishes its port on the host, so the control
plane can connect to it directly with psycopg instead of shelling out to
``psql``.  Every identifier that comes from the UI is composed with
``psycopg.sql.Identifier``; values are always sent as bound parameters.
"""

import re
import time
from datetime import date, datetime, time as time_value
from decimal import Decimal
from uuid import UUID

import psycopg
from django.conf import settings
from psycopg import sql
from psycopg.rows import dict_row, tuple_row

from .models import Server


class DatabaseAdminError(Exception):
    pass


IDENTIFIER_PATTERN = re.compile(r"^[A-Za-z_][A-Za-z0-9_$]*$")
COLUMN_TYPE_PATTERN = re.compile(r"^[A-Za-z][A-Za-z0-9_ ]*(\(\s*\d+\s*(,\s*\d+\s*)?\))?(\[\])?$")
MAX_QUERY_ROWS = 500
QUERY_TIMEOUT_MS = 15000

TABLES_QUERY = """
    SELECT
        n.nspname AS schema,
        c.relname AS name,
        GREATEST(c.reltuples, 0)::bigint AS estimated_rows,
        pg_total_relation_size(c.oid) AS size_bytes,
        obj_description(c.oid, 'pg_class') AS description
    FROM pg_class c
    JOIN pg_namespace n ON n.oid = c.relnamespace
    WHERE c.relkind IN ('r', 'p')
      AND n.nspname NOT IN ('pg_catalog', 'information_schema')
      AND n.nspname NOT LIKE 'pg_toast%%'
    ORDER BY n.nspname, c.relname
"""

DATABASES_QUERY = """
    SELECT
        d.datname AS name,
        pg_database_size(d.datname) AS size_bytes,
        pg_get_userbyid(d.datdba) AS owner,
        pg_encoding_to_char(d.encoding) AS encoding
    FROM pg_database d
    WHERE d.datallowconn AND NOT d.datistemplate
    ORDER BY d.datname
"""

COLUMNS_QUERY = """
    SELECT
        a.attname AS name,
        format_type(a.atttypid, a.atttypmod) AS type,
        NOT a.attnotnull AS nullable,
        pg_get_expr(d.adbin, d.adrelid) AS default_value,
        a.attidentity AS identity,
        a.attgenerated AS generated,
        EXISTS (
            SELECT 1
            FROM pg_index i
            WHERE i.indrelid = a.attrelid
              AND i.indisprimary
              AND a.attnum = ANY(i.indkey)
        ) AS primary_key,
        a.attnum AS position
    FROM pg_attribute a
    JOIN pg_class c ON c.oid = a.attrelid
    JOIN pg_namespace n ON n.oid = c.relnamespace
    LEFT JOIN pg_attrdef d ON d.adrelid = a.attrelid AND d.adnum = a.attnum
    WHERE n.nspname = %(schema)s
      AND c.relname = %(table)s
      AND c.relkind IN ('r', 'p', 'v', 'm', 'f')
      AND a.attnum > 0
      AND NOT a.attisdropped
    ORDER BY a.attnum
"""


def _connection(server, database=None):
    if server.image != Server.Image.POSTGRES:
        raise DatabaseAdminError("Database tools are only available for PostgreSQL servers.")
    if server.status != Server.Status.RUNNING:
        raise DatabaseAdminError("Start the server before using the database tools.")
    if not server.host_port:
        raise DatabaseAdminError("This database does not have a published port.")

    environment = server.env_vars or {}
    password = environment.get("POSTGRES_PASSWORD")
    if not password:
        raise DatabaseAdminError("PostgreSQL credentials are missing for this server.")

    host = settings.MINICLOUD_PUBLIC_HOST or "127.0.0.1"
    if host == "localhost":
        host = "127.0.0.1"

    try:
        return psycopg.connect(
            host=host,
            port=server.host_port,
            dbname=database or environment.get("POSTGRES_DB", "postgres"),
            user=environment.get("POSTGRES_USER", "postgres"),
            password=str(password),
            connect_timeout=5,
            autocommit=True,
            row_factory=dict_row,
        )
    except psycopg.Error as error:
        raise DatabaseAdminError(f"Could not connect to PostgreSQL: {_clean_message(error)}") from error


def _clean_message(error):
    message = str(error).strip()
    return message.splitlines()[0] if message else error.__class__.__name__


def _jsonable(value):
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, (datetime, date, time_value)):
        return value.isoformat()
    if isinstance(value, UUID):
        return str(value)
    if isinstance(value, (bytes, bytearray, memoryview)):
        return bytes(value).hex()
    if isinstance(value, (list, tuple)):
        return [_jsonable(item) for item in value]
    if isinstance(value, dict):
        return {str(key): _jsonable(item) for key, item in value.items()}
    return str(value)


def _validate_identifier(value, label="name"):
    value = str(value or "").strip()
    if not IDENTIFIER_PATTERN.match(value):
        raise DatabaseAdminError(f"Invalid {label}. Use letters, digits, and underscores.")
    return value


def _validate_column_type(value):
    value = str(value or "").strip()
    if not COLUMN_TYPE_PATTERN.match(value):
        raise DatabaseAdminError(f"Unsupported column type: {value or '(empty)'}.")
    return value


def _columns(cursor, schema, table):
    cursor.execute(COLUMNS_QUERY, {"schema": schema, "table": table})
    columns = []
    for column in cursor.fetchall():
        column["identity"] = column.get("identity") or ""
        column["generated"] = column.get("generated") or ""
        columns.append({key: _jsonable(value) for key, value in column.items()})
    return columns


def _row_key_columns(columns):
    primary_key = [column["name"] for column in columns if column["primary_key"]]
    return primary_key or ["__ctid"]


def _table_relation(schema, table):
    return sql.SQL("{}.{}").format(sql.Identifier(schema), sql.Identifier(table))


def get_overview(server, database=None):
    with _connection(server, database) as connection:
        try:
            current = connection.execute("SELECT current_database() AS name").fetchone()["name"]
            databases = connection.execute(DATABASES_QUERY).fetchall()
            tables = connection.execute(TABLES_QUERY).fetchall()
            version = connection.execute("SHOW server_version").fetchone()["server_version"]
        except psycopg.Error as error:
            raise DatabaseAdminError(_clean_message(error)) from error

    return {
        "database": current,
        "server_version": version,
        "databases": [_jsonable(row) for row in databases],
        "tables": [_jsonable(row) for row in tables],
    }


def get_table(server, schema, table, database=None, limit=50, offset=0):
    schema = _validate_identifier(schema, "schema")
    table = _validate_identifier(table, "table")
    limit = max(1, min(int(limit or 50), 500))
    offset = max(0, int(offset or 0))

    with _connection(server, database) as connection:
        with connection.cursor() as cursor:
            try:
                columns = _columns(cursor, schema, table)
            except psycopg.Error as error:
                raise DatabaseAdminError(_clean_message(error)) from error
        if not columns:
            raise DatabaseAdminError(f"Table {schema}.{table} does not exist.")

        key_columns = _row_key_columns(columns)
        selection = sql.SQL("*") if key_columns != ["__ctid"] else sql.SQL("*, ctid::text AS __ctid")
        order = sql.SQL(", ").join(sql.Identifier(name) for name in key_columns) if key_columns != ["__ctid"] else sql.SQL("ctid")

        try:
            total = connection.execute(
                sql.SQL("SELECT count(*) AS count FROM {}").format(_table_relation(schema, table))
            ).fetchone()["count"]
            rows = connection.execute(
                sql.SQL("SELECT {} FROM {} ORDER BY {} LIMIT %s OFFSET %s").format(
                    selection, _table_relation(schema, table), order
                ),
                (limit, offset),
            ).fetchall()
        except psycopg.Error as error:
            raise DatabaseAdminError(_clean_message(error)) from error

    return {
        "schema": schema,
        "table": table,
        "columns": columns,
        "primary_key": [column["name"] for column in columns if column["primary_key"]],
        "row_key": key_columns,
        "total_rows": total,
        "limit": limit,
        "offset": offset,
        "rows": [_jsonable(row) for row in rows],
    }


def create_table(server, schema, name, columns, database=None):
    schema = _validate_identifier(schema, "schema")
    name = _validate_identifier(name, "table name")
    if not isinstance(columns, list) or not columns:
        raise DatabaseAdminError("Add at least one column to create a table.")

    definitions = []
    primary_key = []
    for column in columns:
        if not isinstance(column, dict):
            raise DatabaseAdminError("Column definitions must be objects.")
        column_name = _validate_identifier(column.get("name"), "column name")
        column_type = _validate_column_type(column.get("type"))
        definition = sql.SQL("{} {}").format(sql.Identifier(column_name), sql.SQL(column_type))
        if not column.get("nullable", True):
            definition += sql.SQL(" NOT NULL")
        default = str(column.get("default") or "").strip()
        if default:
            if any(token in default for token in (";", "--", "/*")):
                raise DatabaseAdminError("Column defaults cannot contain SQL comments or multiple statements.")
            definition += sql.SQL(" DEFAULT ") + sql.SQL(default)
        definitions.append(definition)
        if column.get("primary_key"):
            primary_key.append(sql.Identifier(column_name))

    if primary_key:
        definitions.append(sql.SQL("PRIMARY KEY ({})").format(sql.SQL(", ").join(primary_key)))

    statement = sql.SQL("CREATE TABLE {} ({})").format(
        _table_relation(schema, name),
        sql.SQL(", ").join(definitions),
    )
    with _connection(server, database) as connection:
        try:
            connection.execute(statement)
        except psycopg.Error as error:
            raise DatabaseAdminError(_clean_message(error)) from error
    return {"schema": schema, "name": name}


def drop_table(server, schema, table, database=None):
    schema = _validate_identifier(schema, "schema")
    table = _validate_identifier(table, "table")
    with _connection(server, database) as connection:
        try:
            connection.execute(sql.SQL("DROP TABLE {}").format(_table_relation(schema, table)))
        except psycopg.Error as error:
            raise DatabaseAdminError(_clean_message(error)) from error
    return {"schema": schema, "table": table}


def truncate_table(server, schema, table, database=None):
    schema = _validate_identifier(schema, "schema")
    table = _validate_identifier(table, "table")
    with _connection(server, database) as connection:
        try:
            connection.execute(sql.SQL("TRUNCATE {}").format(_table_relation(schema, table)))
        except psycopg.Error as error:
            raise DatabaseAdminError(_clean_message(error)) from error
    return {"schema": schema, "table": table}


def _table_columns(connection, schema, table):
    with connection.cursor() as cursor:
        try:
            columns = _columns(cursor, schema, table)
        except psycopg.Error as error:
            raise DatabaseAdminError(_clean_message(error)) from error
    if not columns:
        raise DatabaseAdminError(f"Table {schema}.{table} does not exist.")
    return columns


def _writable(columns):
    return [column for column in columns if not column["identity"] and not column["generated"]]


def insert_row(server, schema, table, values, database=None):
    schema = _validate_identifier(schema, "schema")
    table = _validate_identifier(table, "table")
    if not isinstance(values, dict):
        raise DatabaseAdminError("Row values must be an object.")

    with _connection(server, database) as connection:
        columns = _writable(_table_columns(connection, schema, table))
        available = {column["name"] for column in columns}
        chosen = [name for name in values if name in available]
        unknown = set(values) - available
        if unknown:
            raise DatabaseAdminError(f"Unknown column(s): {', '.join(sorted(unknown))}.")
        try:
            with connection.cursor() as cursor:
                if not chosen:
                    cursor.execute(
                        sql.SQL("INSERT INTO {} DEFAULT VALUES RETURNING *").format(_table_relation(schema, table))
                    )
                else:
                    cursor.execute(
                        sql.SQL("INSERT INTO {} ({}) VALUES ({}) RETURNING *").format(
                            _table_relation(schema, table),
                            sql.SQL(", ").join(sql.Identifier(name) for name in chosen),
                            sql.SQL(", ").join(sql.Placeholder() * len(chosen)),
                        ),
                        [values[name] for name in chosen],
                    )
                row = cursor.fetchone()
        except psycopg.Error as error:
            raise DatabaseAdminError(_clean_message(error)) from error
    return {"row": _jsonable(row)}


def update_row(server, schema, table, key, values, database=None):
    schema = _validate_identifier(schema, "schema")
    table = _validate_identifier(table, "table")
    if not isinstance(key, dict) or not key:
        raise DatabaseAdminError("A row key is required to update a row.")
    if not isinstance(values, dict) or not values:
        raise DatabaseAdminError("No changes were provided.")

    with _connection(server, database) as connection:
        columns = _table_columns(connection, schema, table)
        names = {column["name"] for column in columns}
        unknown = ({name for name in key if name != "__ctid"} | set(values)) - names
        if unknown:
            raise DatabaseAdminError(f"Unknown column(s): {', '.join(sorted(unknown))}.")

        assignments = sql.SQL(", ").join(
            sql.SQL("{} = %s").format(sql.Identifier(name)) for name in values
        )
        conditions = sql.SQL(" AND ").join(
            sql.SQL("ctid = %s::tid") if name == "__ctid" else sql.SQL("{} IS NOT DISTINCT FROM %s").format(sql.Identifier(name))
            for name in key
        )
        parameters = [values[name] for name in values] + [key[name] for name in key]
        try:
            with connection.cursor() as cursor:
                cursor.execute(
                    sql.SQL("UPDATE {} SET {} WHERE {} RETURNING *").format(
                        _table_relation(schema, table), assignments, conditions
                    ),
                    parameters,
                )
                row = cursor.fetchone()
        except psycopg.Error as error:
            raise DatabaseAdminError(_clean_message(error)) from error
    if row is None:
        raise DatabaseAdminError("The row could not be found; refresh the table and try again.")
    return {"row": _jsonable(row)}


def delete_row(server, schema, table, key, database=None):
    schema = _validate_identifier(schema, "schema")
    table = _validate_identifier(table, "table")
    if not isinstance(key, dict) or not key:
        raise DatabaseAdminError("A row key is required to delete a row.")

    with _connection(server, database) as connection:
        columns = _table_columns(connection, schema, table)
        names = {column["name"] for column in columns}
        unknown = {name for name in key if name != "__ctid"} - names
        if unknown:
            raise DatabaseAdminError(f"Unknown column(s): {', '.join(sorted(unknown))}.")
        conditions = sql.SQL(" AND ").join(
            sql.SQL("ctid = %s::tid") if name == "__ctid" else sql.SQL("{} IS NOT DISTINCT FROM %s").format(sql.Identifier(name))
            for name in key
        )
        try:
            with connection.cursor() as cursor:
                cursor.execute(
                    sql.SQL("DELETE FROM {} WHERE {} RETURNING *").format(
                        _table_relation(schema, table), conditions
                    ),
                    [key[name] for name in key],
                )
                row = cursor.fetchone()
        except psycopg.Error as error:
            raise DatabaseAdminError(_clean_message(error)) from error
    if row is None:
        raise DatabaseAdminError("The row could not be found; refresh the table and try again.")
    return {"row": _jsonable(row)}


def run_query(server, statement, database=None):
    if not isinstance(statement, str) or not statement.strip():
        raise DatabaseAdminError("Enter a SQL statement to run.")
    if len(statement) > 20000:
        raise DatabaseAdminError("SQL statements are limited to 20,000 characters.")

    started = time.monotonic()
    with _connection(server, database) as connection:
        try:
            with connection.cursor(row_factory=tuple_row) as cursor:
                cursor.execute(f"SET statement_timeout = {QUERY_TIMEOUT_MS}")
                cursor.execute(statement)
                columns = [description.name for description in cursor.description] if cursor.description else []
                rows = cursor.fetchmany(MAX_QUERY_ROWS + 1) if columns else []
                status = cursor.statusmessage or ""
        except psycopg.Error as error:
            raise DatabaseAdminError(_clean_message(error)) from error

    truncated = len(rows) > MAX_QUERY_ROWS
    rows = rows[:MAX_QUERY_ROWS]
    return {
        "columns": columns,
        "rows": [[_jsonable(value) for value in row] for row in rows],
        "row_count": len(rows),
        "truncated": truncated,
        "command": status,
        "duration_ms": round((time.monotonic() - started) * 1000, 1),
    }
