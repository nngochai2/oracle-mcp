"""
oracle_mcp/tests/test_oracle_mcp_server.py
------------------------------------------
Unit tests for the Oracle MCP server.

All Oracle connectivity is mocked — no live database required.
Run with:  pytest oracle_mcp/tests/ -v
"""

import json
import os
import pytest
from unittest.mock import MagicMock, patch, call

# Provide minimal env before importing the server module
os.environ.setdefault("ORACLE_USER",    "test_user")
os.environ.setdefault("ORACLE_PASSWORD","test_pass")
os.environ.setdefault("ORACLE_SERVICE", "TEST")
os.environ.setdefault("ORACLE_HOST",    "localhost")
os.environ.setdefault("ORACLE_PORT",    "1521")
os.environ.setdefault("MAX_ROWS",       "50")

import sys
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..', 'src')))

# Patch oracledb before the server module initialises it
with patch("oracledb.init_oracle_client"), \
     patch("oracledb.create_pool"):
    import oracle_mcp_server as srv


# ─────────────────────────────────────────────────────────
# SQL Guard
# ─────────────────────────────────────────────────────────

class TestSQLGuard:
    def test_select_allowed(self):
        srv._guard("SELECT * FROM dual")          # should not raise

    @pytest.mark.parametrize("sql", [
        "INSERT INTO t VALUES (1)",
        "UPDATE t SET x=1",
        "DELETE FROM t",
        "DROP TABLE t",
        "CREATE TABLE t (id NUMBER)",
        "ALTER TABLE t ADD col VARCHAR2(10)",
        "TRUNCATE TABLE t",
        "GRANT SELECT ON t TO user2",
        "EXEC my_proc",
        "BEGIN my_proc; END;",
        "COMMIT",
        "ROLLBACK",
        "  \n  DELETE FROM invoices",         # leading whitespace
    ])
    def test_mutating_blocked(self, sql):
        with pytest.raises(ValueError, match="Only SELECT"):
            srv._guard(sql)


# ─────────────────────────────────────────────────────────
# Schema helpers
# ─────────────────────────────────────────────────────────

class TestSchemaHelpers:
    def test_resolve_schema_explicit(self):
        assert srv._resolve_schema("myschema") == "MYSCHEMA"

    def test_resolve_schema_default(self):
        original = srv.Config.DEFAULT_SCHEMA
        srv.Config.DEFAULT_SCHEMA = "INVOICING"
        result = srv._resolve_schema(None)
        srv.Config.DEFAULT_SCHEMA = original
        assert result == "INVOICING"

    def test_schema_in_clause_empty(self):
        original = srv.Config.ALLOWED_SCHEMAS
        srv.Config.ALLOWED_SCHEMAS = []
        assert srv._schema_in_clause() == ""
        srv.Config.ALLOWED_SCHEMAS = original

    def test_schema_in_clause_populated(self):
        original = srv.Config.ALLOWED_SCHEMAS
        srv.Config.ALLOWED_SCHEMAS = ["INVOICING", "REF"]
        clause = srv._schema_in_clause()
        assert "'INVOICING'" in clause
        assert "'REF'" in clause
        srv.Config.ALLOWED_SCHEMAS = original


# ─────────────────────────────────────────────────────────
# Tool: execute_query
# ─────────────────────────────────────────────────────────

class TestExecuteQuery:
    @pytest.mark.asyncio
    async def test_returns_rows(self):
        mock_cursor = MagicMock()
        mock_cursor.description = [("ID",), ("NAME",)]
        mock_cursor.fetchmany.return_value = [(1, "Alpha"), (2, "Beta")]

        mock_conn = MagicMock()
        mock_conn.cursor.return_value = mock_cursor

        with patch("oracle_mcp_server._conn") as mock_ctx:
            mock_ctx.return_value.__enter__ = MagicMock(return_value=mock_conn)
            mock_ctx.return_value.__exit__  = MagicMock(return_value=False)

            result = await srv.execute_query("SELECT id, name FROM t")

        data = json.loads(result)
        assert data["row_count"] == 2
        assert data["rows"][0]["name"] == "Alpha"

    @pytest.mark.asyncio
    async def test_blocks_dml(self):
        result = await srv.execute_query("DELETE FROM invoices")
        data = json.loads(result)
        assert "error" in data
        assert "Only SELECT" in data["error"]

    @pytest.mark.asyncio
    async def test_caps_rows(self):
        """Request more rows than MAX_ROWS — should cap silently."""
        mock_cursor = MagicMock()
        mock_cursor.description = [("X",)]
        mock_cursor.fetchmany.return_value = [(i,) for i in range(50)]

        mock_conn = MagicMock()
        mock_conn.cursor.return_value = mock_cursor

        with patch("oracle_mcp_server._conn") as mock_ctx:
            mock_ctx.return_value.__enter__ = MagicMock(return_value=mock_conn)
            mock_ctx.return_value.__exit__  = MagicMock(return_value=False)

            result = await srv.execute_query("SELECT x FROM t", max_rows=9999)

        data = json.loads(result)
        assert data["capped_at"] == srv.Config.MAX_ROWS


# ─────────────────────────────────────────────────────────
# Tool: describe_object
# ─────────────────────────────────────────────────────────

class TestDescribeObject:
    @pytest.mark.asyncio
    async def test_returns_columns(self):
        col_rows   = [(1, "INVOICE_ID", "NUMBER(10)", "N", None, "Primary key")]
        tab_row    = ("Invoice header table",)

        mock_cursor = MagicMock()
        mock_cursor.description = [
            ("COL_ID",),("COLUMN_NAME",),("DATA_TYPE",),
            ("NULLABLE",),("DATA_DEFAULT",),("COLUMN_COMMENT",),
        ]
        mock_cursor.fetchmany.return_value = col_rows
        mock_cursor.fetchone.return_value  = tab_row

        mock_conn = MagicMock()
        mock_conn.cursor.return_value = mock_cursor

        with patch("oracle_mcp_server._conn") as mock_ctx:
            mock_ctx.return_value.__enter__ = MagicMock(return_value=mock_conn)
            mock_ctx.return_value.__exit__  = MagicMock(return_value=False)

            result = await srv.describe_object("INVOICE_HEADER", schema="INVOICING")

        data = json.loads(result)
        assert data["object"] == "INVOICING.INVOICE_HEADER"
        assert data["comment"] == "Invoice header table"
        assert len(data["columns"]) == 1

    @pytest.mark.asyncio
    async def test_not_found(self):
        mock_cursor = MagicMock()
        mock_cursor.description = [("COL_ID",)]
        mock_cursor.fetchmany.return_value = []     # empty → not found
        mock_cursor.fetchone.return_value  = None

        mock_conn = MagicMock()
        mock_conn.cursor.return_value = mock_cursor

        with patch("oracle_mcp_server._conn") as mock_ctx:
            mock_ctx.return_value.__enter__ = MagicMock(return_value=mock_conn)
            mock_ctx.return_value.__exit__  = MagicMock(return_value=False)

            result = await srv.describe_object("NO_SUCH_TABLE")

        data = json.loads(result)
        assert "error" in data or "not found" in result.lower()


# ─────────────────────────────────────────────────────────
# Tool: get_dependencies
# ─────────────────────────────────────────────────────────

class TestGetDependencies:
    @pytest.mark.asyncio
    async def test_both_directions(self):
        upstream_rows   = [("INVOICING","BASE_TABLE","TABLE","HARD")]
        downstream_rows = [("INVOICING","REPORT_VIEW","VIEW","HARD")]

        call_count = [0]

        def fake_fetchmany(*args, **kwargs):
            i = call_count[0]
            call_count[0] += 1
            return upstream_rows if i == 0 else downstream_rows

        mock_cursor = MagicMock()
        mock_cursor.description = [
            ("DEP_OWNER",),("DEP_NAME",),("DEP_TYPE",),("DEPENDENCY_TYPE",)
        ]
        mock_cursor.fetchmany.side_effect = fake_fetchmany

        mock_conn = MagicMock()
        mock_conn.cursor.return_value = mock_cursor

        with patch("oracle_mcp_server._conn") as mock_ctx:
            mock_ctx.return_value.__enter__ = MagicMock(return_value=mock_conn)
            mock_ctx.return_value.__exit__  = MagicMock(return_value=False)

            result = await srv.get_dependencies("V_INVOICE", schema="INVOICING")

        data = json.loads(result)
        assert "depends_on" in data
        assert "used_by"    in data


# ─────────────────────────────────────────────────────────
# Tool: search_objects
# ─────────────────────────────────────────────────────────

class TestSearchObjects:
    @pytest.mark.asyncio
    async def test_finds_objects_and_columns(self):
        obj_rows = [("INVOICING","V_VAT_EXEMPT","VIEW","VALID","2024-01-01")]
        col_rows = [("INVOICING","INVOICE_LINES","VAT_EXEMPT_FLAG","VARCHAR2","Y")]

        call_count = [0]

        def fake_fetchmany(*args, **kwargs):
            i = call_count[0]
            call_count[0] += 1
            return [obj_rows, col_rows][i] if i < 2 else []

        mock_cursor = MagicMock()
        mock_cursor.description = [("A",),("B",),("C",),("D",),("E",)]
        mock_cursor.fetchmany.side_effect = fake_fetchmany

        mock_conn = MagicMock()
        mock_conn.cursor.return_value = mock_cursor

        with patch("oracle_mcp_server._conn") as mock_ctx:
            mock_ctx.return_value.__enter__ = MagicMock(return_value=mock_conn)
            mock_ctx.return_value.__exit__  = MagicMock(return_value=False)

            result = await srv.search_objects("VAT", schema="INVOICING")

        data = json.loads(result)
        assert data["keyword"] == "VAT"
        assert "objects_by_name" in data["results"]
        assert "columns_by_name" in data["results"]
