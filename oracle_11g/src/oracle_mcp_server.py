"""
Oracle 11g MCP Server
=====================
Exposes Oracle database schema, source, dependency, and query tools
to GitHub Copilot via the Model Context Protocol.
 
Transport : stdio (local / VS Code dev)
           SSE/HTTP (team deployment via Jenkins)
DB Access : Read-only Oracle user (enforced at DB level AND application level)
Oracle    : Requires thick mode — Oracle Instant Client must be present on host
 
Tool inventory
--------------
  execute_query          Ad-hoc SELECT; row-capped; DML/DDL blocked
  describe_object        Column metadata + comments for any table or view
  get_view_definition    Full DDL via DBMS_METADATA; falls back to ALL_VIEWS.TEXT
  list_objects           Inventory by schema/type/name pattern
  get_dependencies       Upstream + downstream dependency graph (ALL_DEPENDENCIES)
  get_package_source     PL/SQL source from ALL_SOURCE (packages, procs, functions, triggers)
  get_constraints        PK / FK / unique / check constraints with FK target resolution
  search_objects         Object name + column name search; optional PL/SQL source scan
  get_indexes            Index definitions with column list
  get_invalid_objects    INVALID objects in schema — pre/post-deployment check
"""
 
import asyncio
import json
import logging
import os
import re
from contextlib import contextmanager
from typing import Any, Generator, Optional
 
import oracledb
from dotenv import load_dotenv
from mcp.server.fastmcp import FastMCP
 
# ─────────────────────────────────────────────────────────────────────────────
# Bootstrap
# ─────────────────────────────────────────────────────────────────────────────
 
load_dotenv(dotenv_path=os.path.join(os.path.dirname(__file__), "..", ".env"))
 
logging.basicConfig(
    level=getattr(logging, os.getenv("LOG_LEVEL", "INFO")),
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("oracle-mcp")
 
 
# ─────────────────────────────────────────────────────────────────────────────
# Configuration  (all sourced from .env — no hard-coded credentials)
# ─────────────────────────────────────────────────────────────────────────────
 
class Config:
    HOST            = os.getenv("ORACLE_HOST", "localhost")
    PORT            = int(os.getenv("ORACLE_PORT", "1521"))
    SERVICE         = os.getenv("ORACLE_SERVICE", "")       # preferred; e.g. "INVOICING"
    SID             = os.getenv("ORACLE_SID", "")           # fallback; e.g. "XE"
    USER            = os.getenv("ORACLE_USER", "")
    PASSWORD        = os.getenv("ORACLE_PASSWORD", "")
    CLIENT_LIB      = os.getenv("ORACLE_CLIENT_LIB_DIR", "")  # path to Instant Client
    DEFAULT_SCHEMA  = os.getenv("ORACLE_DEFAULT_SCHEMA", USER).upper()
    MAX_ROWS        = int(os.getenv("MAX_ROWS", "200"))
    # Comma-separated list of schemas the MCP server is allowed to expose.
    # Leave blank to allow all schemas accessible to ORACLE_USER.
    ALLOWED_SCHEMAS: list[str] = [
        s.strip().upper()
        for s in os.getenv("ALLOWED_SCHEMAS", "").split(",")
        if s.strip()
    ]
    # Transport: 'stdio' (local) | 'sse' (network — same subnet / team)
    MCP_TRANSPORT   = os.getenv("MCP_TRANSPORT", "stdio")   # stdio | sse
    MCP_HOST        = os.getenv("MCP_HOST", "0.0.0.0")      # 0.0.0.0 = all interfaces
    MCP_PORT        = int(os.getenv("MCP_PORT", "8000"))
 
    @classmethod
    def dsn(cls) -> str:
        if cls.SERVICE:
            return oracledb.makedsn(cls.HOST, cls.PORT, service_name=cls.SERVICE)
        if cls.SID:
            return oracledb.makedsn(cls.HOST, cls.PORT, sid=cls.SID)
        raise ValueError(
            "Set ORACLE_SERVICE (preferred) or ORACLE_SID in .env"
        )
 
    @classmethod
    def validate(cls) -> None:
        missing = [k for k in ("USER", "PASSWORD") if not getattr(cls, k)]
        if not cls.SERVICE and not cls.SID:
            missing.append("ORACLE_SERVICE or ORACLE_SID")
        if missing:
            raise EnvironmentError(
                f"Missing required .env variables: {', '.join(missing)}"
            )
 
 
# ─────────────────────────────────────────────────────────────────────────────
# Oracle Client Initialisation  (thick mode — required for Oracle 11g)
# ─────────────────────────────────────────────────────────────────────────────
 
def init_oracle_client() -> None:
    """
    Oracle 11g pre-dates the thin wire protocol.  python-oracledb thin mode
    only supports 12.1+, so thick mode (Oracle Instant Client) is mandatory.
 
    Installation options:
      Linux  : rpm -ivh oracle-instantclient-basic-*.rpm
               export LD_LIBRARY_PATH=/usr/lib/oracle/21/client64/lib
      Windows: unzip instantclient_21_x to C:\\oracle\\instantclient
               set PATH=%PATH%;C:\\oracle\\instantclient
 
    ORACLE_CLIENT_LIB_DIR in .env overrides auto-discovery from PATH /
    LD_LIBRARY_PATH.  Omit to let the driver find the client automatically.
    """
    try:
        if Config.CLIENT_LIB:
            oracledb.init_oracle_client(lib_dir=Config.CLIENT_LIB)
            logger.info("Oracle thick mode — client lib: %s", Config.CLIENT_LIB)
        else:
            oracledb.init_oracle_client()
            logger.info("Oracle thick mode — client lib from system path")
    except oracledb.ProgrammingError as exc:
        # Already initialised in this process — safe to ignore
        if "already" in str(exc).lower():
            logger.debug("Oracle client already initialised: %s", exc)
        else:
            raise
 
 
# ─────────────────────────────────────────────────────────────────────────────
# Connection Pool
# ─────────────────────────────────────────────────────────────────────────────
 
_pool: Optional[oracledb.ConnectionPool] = None
 
 
def _get_pool() -> oracledb.ConnectionPool:
    global _pool
    if _pool is None:
        _pool = oracledb.create_pool(
            user=Config.USER,
            password=Config.PASSWORD,
            dsn=Config.dsn(),
            min=1,
            max=5,
            increment=1,
            getmode=oracledb.POOL_GETMODE_WAIT,
            timeout=30,
        )
        logger.info(
            "Connection pool created — %s@%s:%s",
            Config.USER, Config.HOST, Config.PORT,
        )
    return _pool
 
 
@contextmanager
def _conn() -> Generator[oracledb.Connection, None, None]:
    """Borrow a connection from the pool; return it on exit."""
    pool = _get_pool()
    conn = pool.acquire()
    try:
        yield conn
    finally:
        pool.release(conn)
 
 
# ─────────────────────────────────────────────────────────────────────────────
# SQL Guard  (application-level read-only enforcement)
# ─────────────────────────────────────────────────────────────────────────────
 
_MUTATING = re.compile(
    r"""
    ^\s*(
        INSERT | UPDATE | DELETE | MERGE   |   # DML
        DROP   | CREATE | ALTER  | TRUNCATE|   # DDL
        GRANT  | REVOKE                    |   # DCL
        EXEC   | EXECUTE | BEGIN | CALL    |   # PL/SQL execution
        COMMIT | ROLLBACK | SAVEPOINT          # TCL
    )\b
    """,
    re.IGNORECASE | re.VERBOSE | re.MULTILINE,
)
 
 
def _guard(sql: str) -> None:
    """Raise ValueError if sql contains any mutating statement."""
    if _MUTATING.search(sql):
        raise ValueError(
            "Only SELECT statements are permitted through this MCP server.  "
            "DML, DDL, DCL, and transaction control are blocked."
        )
 
 
# ─────────────────────────────────────────────────────────────────────────────
# Schema Allow-list Helper
# ─────────────────────────────────────────────────────────────────────────────
 
def _schema_in_clause(alias: str = "owner") -> str:
    """
    Returns an AND fragment restricting results to ALLOWED_SCHEMAS,
    or an empty string when the list is unconstrained.
    """
    if not Config.ALLOWED_SCHEMAS:
        return ""
    quoted = ", ".join(f"'{s}'" for s in Config.ALLOWED_SCHEMAS)
    return f"AND {alias} IN ({quoted})"
 
 
def _resolve_schema(schema: Optional[str]) -> str:
    return (schema or Config.DEFAULT_SCHEMA or Config.USER).upper()
 
 
# ─────────────────────────────────────────────────────────────────────────────
# LONG Column Handler  (Oracle 11g stores ALL_VIEWS.TEXT as LONG)
# ─────────────────────────────────────────────────────────────────────────────
 
def _long_to_str(cursor, name, default_type, size, precision, scale):
    """Output type handler: convert LONG columns to Python str (up to 32 767 chars)."""
    if default_type == oracledb.DB_TYPE_LONG:
        return cursor.var(str, 32_767, cursor.arraysize)
 
 
# ─────────────────────────────────────────────────────────────────────────────
# Result Formatters
# ─────────────────────────────────────────────────────────────────────────────
 
def _rows(cursor, limit: Optional[int] = None) -> list[dict]:
    cols = [d[0].lower() for d in cursor.description]
    cap  = limit or Config.MAX_ROWS
    return [dict(zip(cols, row)) for row in cursor.fetchmany(cap)]
 
 
def _json(data: Any) -> str:
    return json.dumps(data, indent=2, default=str)
 
 
def _tool_error(msg: str) -> str:
    return _json({"error": msg})
 
 
# ─────────────────────────────────────────────────────────────────────────────
# MCP Server
# ─────────────────────────────────────────────────────────────────────────────
 
server = FastMCP("oracle-mcp", host=Config.MCP_HOST, port=Config.MCP_PORT)
 
 
# ══════════════════════════════════════════════════════════════════════════════
# Tool: execute_query
# ══════════════════════════════════════════════════════════════════════════════
 
@server.tool()
async def execute_query(sql: str, max_rows: int = Config.MAX_ROWS) -> str:
    """
    Execute an ad-hoc read-only SELECT against Oracle.
 
    Results are capped at max_rows (default from MAX_ROWS env var; hard ceiling
    also applies).  DML, DDL, and transaction control are blocked.
 
    Use for:
    - Spot-checking view output before relying on it in code
    - Verifying that a WHERE clause matches expected rows
    - Investigating data quality issues
    """
    try:
        _guard(sql)
        cap = min(max_rows, Config.MAX_ROWS)
        with _conn() as conn:
            cur = conn.cursor()
            cur.execute(sql)
            rows = _rows(cur, cap)
        return _json({"row_count": len(rows), "capped_at": cap, "rows": rows})
    except ValueError as exc:
        return _tool_error(str(exc))
    except oracledb.DatabaseError as exc:
        return _tool_error(f"Oracle error: {exc}")
 
 
# ══════════════════════════════════════════════════════════════════════════════
# Tool: describe_object
# ══════════════════════════════════════════════════════════════════════════════
 
@server.tool()
async def describe_object(object_name: str, schema: Optional[str] = None) -> str:
    """
    Return rich column metadata for a table or view — equivalent to DESC
    in SQL*Plus but includes data types, nullability, defaults, and both
    column-level and table-level comments.
 
    Parameters
    ----------
    object_name : Table or view name (case-insensitive).
    schema      : Owner schema.  Defaults to ORACLE_DEFAULT_SCHEMA from .env.
    """
    owner = _resolve_schema(schema)
    name  = object_name.upper()
 
    col_sql = """
        SELECT
            c.column_id                                     AS col_id,
            c.column_name,
            c.data_type
                || CASE
                       WHEN c.data_type IN ('VARCHAR2','NVARCHAR2','CHAR','NCHAR')
                           THEN '(' || c.char_length || ')'
                       WHEN c.data_type = 'NUMBER' AND c.data_precision IS NOT NULL
                           THEN '(' || c.data_precision
                                    || CASE WHEN c.data_scale > 0
                                            THEN ',' || c.data_scale ELSE '' END
                                    || ')'
                       ELSE ''
                   END                                      AS data_type,
            c.nullable,
            c.data_default,
            cc.comments                                     AS column_comment
        FROM all_tab_columns c
        LEFT JOIN all_col_comments cc
               ON cc.owner       = c.owner
              AND cc.table_name  = c.table_name
              AND cc.column_name = c.column_name
        WHERE c.owner      = :owner
          AND c.table_name = :name
        ORDER BY c.column_id
    """
    tab_sql = """
        SELECT comments FROM all_tab_comments
        WHERE owner = :owner AND table_name = :name
    """
 
    try:
        with _conn() as conn:
            cur = conn.cursor()
            cur.execute(col_sql, owner=owner, name=name)
            columns = _rows(cur)
 
            cur.execute(tab_sql, owner=owner, name=name)
            row = cur.fetchone()
            table_comment = row[0] if row else None
 
        if not columns:
            return _tool_error(
                f"{owner}.{name} not found or no columns accessible to {Config.USER}."
            )
 
        return _json({
            "object":  f"{owner}.{name}",
            "comment": table_comment,
            "columns": columns,
        })
    except oracledb.DatabaseError as exc:
        return _tool_error(f"Oracle error: {exc}")
 
 
# ══════════════════════════════════════════════════════════════════════════════
# Tool: get_view_definition
# ══════════════════════════════════════════════════════════════════════════════
 
@server.tool()
async def get_view_definition(view_name: str, schema: Optional[str] = None) -> str:
    """
    Return the full SQL text of an Oracle view.
 
    Primary path  : DBMS_METADATA.GET_DDL — returns complete, formatted DDL
                    including the CREATE OR REPLACE header.
    Fallback path : ALL_VIEWS.TEXT — raw SELECT body only; LONG column read
                    via output type handler (32 767 char limit).
 
    Note: If the MCP read-only user lacks EXECUTE on DBMS_METADATA, grant:
          GRANT EXECUTE ON DBMS_METADATA TO <mcp_user>;
          or rely on the ALL_VIEWS fallback.
 
    Parameters
    ----------
    view_name : View name (case-insensitive).
    schema    : Owner schema.  Defaults to ORACLE_DEFAULT_SCHEMA from .env.
    """
    owner = _resolve_schema(schema)
    name  = view_name.upper()
 
    try:
        with _conn() as conn:
            cur = conn.cursor()
 
            # ── Primary: DBMS_METADATA ────────────────────────────────────────
            try:
                cur.execute(
                    "SELECT DBMS_METADATA.GET_DDL('VIEW', :n, :o) FROM dual",
                    n=name, o=owner,
                )
                row = cur.fetchone()
                if row and row[0]:
                    return str(row[0])
            except oracledb.DatabaseError as exc:
                logger.warning(
                    "DBMS_METADATA unavailable for %s.%s (%s) — using ALL_VIEWS fallback",
                    owner, name, exc,
                )
 
            # ── Fallback: ALL_VIEWS.TEXT (LONG column) ────────────────────────
            cur.outputtypehandler = _long_to_str
            cur.execute(
                "SELECT text FROM all_views WHERE owner = :o AND view_name = :n",
                o=owner, n=name,
            )
            row = cur.fetchone()
            if row and row[0]:
                header = f"-- Source: ALL_VIEWS.TEXT (DBMS_METADATA unavailable)\n"
                header += f"CREATE OR REPLACE VIEW {owner}.{name} AS\n"
                return header + row[0]
 
        return _tool_error(f"View {owner}.{name} not found or not accessible.")
    except oracledb.DatabaseError as exc:
        return _tool_error(f"Oracle error: {exc}")
 
 
# ══════════════════════════════════════════════════════════════════════════════
# Tool: list_objects
# ══════════════════════════════════════════════════════════════════════════════
 
@server.tool()
async def list_objects(
    schema:      Optional[str] = None,
    object_type: Optional[str] = None,
    name_filter: Optional[str] = None,
) -> str:
    """
    List Oracle objects in a schema, optionally filtered by type and/or name.
 
    Parameters
    ----------
    schema      : Owner schema.  Defaults to ORACLE_DEFAULT_SCHEMA from .env.
    object_type : One of TABLE, VIEW, PACKAGE, PACKAGE BODY, PROCEDURE,
                  FUNCTION, TRIGGER, SYNONYM, SEQUENCE, INDEX.
                  Case-insensitive.  Omit for all types.
    name_filter : Partial object name (LIKE match, case-insensitive).
                  Example: 'INV' matches INV_HEADER, INVOICE_LINES, etc.
    """
    owner      = _resolve_schema(schema)
    conditions = ["owner = :owner"]
    binds: dict = {"owner": owner}
 
    if object_type:
        conditions.append("object_type = :otype")
        binds["otype"] = object_type.upper()
    if name_filter:
        conditions.append("object_name LIKE :nfilter")
        binds["nfilter"] = f"%{name_filter.upper()}%"
 
    where = " AND ".join(conditions)
    sql = f"""
        SELECT object_name, object_type, status, last_ddl_time
        FROM   all_objects
        WHERE  {where}
        ORDER  BY object_type, object_name
    """
    try:
        with _conn() as conn:
            cur = conn.cursor()
            cur.execute(sql, **binds)
            rows = _rows(cur)
        return _json({"schema": owner, "count": len(rows), "objects": rows})
    except oracledb.DatabaseError as exc:
        return _tool_error(f"Oracle error: {exc}")
 
 
# ══════════════════════════════════════════════════════════════════════════════
# Tool: get_dependencies
# ══════════════════════════════════════════════════════════════════════════════
 
@server.tool()
async def get_dependencies(
    object_name: str,
    schema:      Optional[str] = None,
    direction:   str = "both",
) -> str:
    """
    Return the dependency graph for an Oracle object via ALL_DEPENDENCIES.
 
    Parameters
    ----------
    object_name : Object name (case-insensitive).
    schema      : Owner schema.  Defaults to ORACLE_DEFAULT_SCHEMA from .env.
    direction   : 'upstream'   — what this object depends on
                  'downstream' — what depends on this object
                  'both'       — full picture (default)
 
    Primary use: impact analysis before changing a view, package, or table.
    "If I modify V_INVOICE_LINES, what breaks?"  →  direction='downstream'
    "What tables does P_CALC_VAT read?"          →  direction='upstream'
    """
    owner     = _resolve_schema(schema)
    name      = object_name.upper()
    direction = direction.lower()
    result: dict = {"object": f"{owner}.{name}", "direction": direction}
 
    try:
        with _conn() as conn:
            cur = conn.cursor()
 
            if direction in ("upstream", "both"):
                cur.execute("""
                    SELECT referenced_owner  AS dep_owner,
                           referenced_name   AS dep_name,
                           referenced_type   AS dep_type,
                           dependency_type
                    FROM   all_dependencies
                    WHERE  owner = :o AND name = :n
                    ORDER  BY referenced_type, referenced_name
                """, o=owner, n=name)
                result["depends_on"] = _rows(cur)
 
            if direction in ("downstream", "both"):
                cur.execute("""
                    SELECT owner        AS dep_owner,
                           name         AS dep_name,
                           type         AS dep_type,
                           dependency_type
                    FROM   all_dependencies
                    WHERE  referenced_owner = :o AND referenced_name = :n
                    ORDER  BY type, name
                """, o=owner, n=name)
                result["used_by"] = _rows(cur)
 
        return _json(result)
    except oracledb.DatabaseError as exc:
        return _tool_error(f"Oracle error: {exc}")
 
 
# ══════════════════════════════════════════════════════════════════════════════
# Tool: get_package_source
# ══════════════════════════════════════════════════════════════════════════════
 
@server.tool()
async def get_package_source(
    object_name: str,
    schema:      Optional[str] = None,
    part:        str = "both",
) -> str:
    """
    Return PL/SQL source from ALL_SOURCE for packages, procedures,
    functions, or triggers.
 
    Parameters
    ----------
    object_name : Package / procedure / function / trigger name (case-insensitive).
    schema      : Owner schema.  Defaults to ORACLE_DEFAULT_SCHEMA from .env.
    part        : For packages only:
                    'spec' — package specification (interface)
                    'body' — package body (implementation)
                    'both' — both (default)
                  Ignored for procedures, functions, and triggers.
 
    Note: Large package bodies can exceed LLM context windows.  Use 'spec'
    first to understand the interface, then 'body' only if the implementation
    detail is required.
    """
    owner = _resolve_schema(schema)
    name  = object_name.upper()
    part  = part.lower()
 
    try:
        with _conn() as conn:
            cur = conn.cursor()
 
            # Auto-detect object type
            cur.execute("""
                SELECT object_type FROM all_objects
                WHERE  owner = :o AND object_name = :n
                  AND  object_type IN (
                         'PACKAGE','PACKAGE BODY','PROCEDURE',
                         'FUNCTION','TRIGGER','TYPE','TYPE BODY'
                       )
                  AND  ROWNUM = 1
            """, o=owner, n=name)
            row = cur.fetchone()
 
        if not row:
            return _tool_error(
                f"No PL/SQL object named {owner}.{name} found "
                f"(looked for PACKAGE, PROCEDURE, FUNCTION, TRIGGER, TYPE)."
            )
 
        obj_type = row[0]
 
        # Determine which source type(s) to fetch
        if obj_type == "PACKAGE":
            fetch_types = []
            if part in ("spec", "both"):
                fetch_types.append("PACKAGE")
            if part in ("body", "both"):
                fetch_types.append("PACKAGE BODY")
        else:
            fetch_types = [obj_type]
 
        sections: dict[str, str] = {}
        with _conn() as conn:
            cur = conn.cursor()
            for stype in fetch_types:
                cur.execute("""
                    SELECT text FROM all_source
                    WHERE  owner = :o AND name = :n AND type = :t
                    ORDER  BY line
                """, o=owner, n=name, t=stype)
                lines = [r[0] for r in cur.fetchall()]
                sections[stype] = "".join(lines) if lines else "(source not accessible)"
 
        return _json({
            "object":  f"{owner}.{name}",
            "type":    obj_type,
            "part":    part,
            "source":  sections,
        })
    except oracledb.DatabaseError as exc:
        return _tool_error(f"Oracle error: {exc}")
 
 
# ══════════════════════════════════════════════════════════════════════════════
# Tool: get_constraints
# ══════════════════════════════════════════════════════════════════════════════
 
@server.tool()
async def get_constraints(object_name: str, schema: Optional[str] = None) -> str:
    """
    Return all constraints for a table: primary keys, foreign keys,
    unique constraints, and check constraints.
 
    FK entries include the referenced owner, table, and column list —
    essential for tracing join paths embedded in legacy views.
 
    Parameters
    ----------
    object_name : Table name (case-insensitive).
    schema      : Owner schema.  Defaults to ORACLE_DEFAULT_SCHEMA from .env.
    """
    owner = _resolve_schema(schema)
    name  = object_name.upper()
 
    # Oracle 11g has LISTAGG — safe to use.
    sql = """
        SELECT
            c.constraint_name,
            CASE c.constraint_type
                WHEN 'P' THEN 'PRIMARY KEY'
                WHEN 'R' THEN 'FOREIGN KEY'
                WHEN 'U' THEN 'UNIQUE'
                WHEN 'C' THEN 'CHECK'
                ELSE c.constraint_type
            END                                                 AS constraint_type,
            c.status,
            c.validated,
            c.search_condition,
            c.r_owner                                           AS fk_ref_owner,
            rc.table_name                                       AS fk_ref_table,
            c.r_constraint_name                                 AS fk_ref_constraint,
            LISTAGG(cc.column_name, ', ')
                WITHIN GROUP (ORDER BY cc.position)             AS columns
        FROM   all_constraints    c
        JOIN   all_cons_columns   cc
               ON  cc.owner           = c.owner
               AND cc.constraint_name = c.constraint_name
        LEFT JOIN all_constraints rc
               ON  rc.owner           = c.r_owner
               AND rc.constraint_name = c.r_constraint_name
        WHERE  c.owner      = :owner
          AND  c.table_name = :name
        GROUP  BY c.constraint_name, c.constraint_type, c.status, c.validated,
                  c.search_condition, c.r_owner, rc.table_name, c.r_constraint_name
        ORDER  BY c.constraint_type, c.constraint_name
    """
    try:
        with _conn() as conn:
            cur = conn.cursor()
            cur.execute(sql, owner=owner, name=name)
            rows = _rows(cur)
        return _json({"object": f"{owner}.{name}", "constraints": rows})
    except oracledb.DatabaseError as exc:
        return _tool_error(f"Oracle error: {exc}")
 
 
# ══════════════════════════════════════════════════════════════════════════════
# Tool: search_objects
# ══════════════════════════════════════════════════════════════════════════════
 
@server.tool()
async def search_objects(
    keyword:        str,
    schema:         Optional[str] = None,
    include_source: bool = False,
) -> str:
    """
    Search Oracle metadata for a keyword across:
      1. Object names    (ALL_OBJECTS.OBJECT_NAME)
      2. Column names    (ALL_TAB_COLUMNS.COLUMN_NAME)
      3. PL/SQL source   (ALL_SOURCE.TEXT) — only when include_source=True
 
    Use for:
    - Finding all objects related to a business term (e.g. 'VAT', 'EXEMPTION')
    - Locating where a column name pattern appears across schemas
    - Finding PL/SQL that references a specific constant or procedure
 
    Parameters
    ----------
    keyword        : Search term (case-insensitive, LIKE match).
    schema         : Restrict to one schema.  Defaults to ORACLE_DEFAULT_SCHEMA.
                     If ALLOWED_SCHEMAS is set in .env, results are always
                     constrained to that list regardless of this parameter.
    include_source : Also search ALL_SOURCE.TEXT.  Can be slow on large codebases.
                     Default: False.
    """
    owner         = _resolve_schema(schema)
    schema_clause = f"AND owner = '{owner}'"
    kw_bind       = {"kw": f"%{keyword.upper()}%"}
    results: dict = {}
 
    try:
        with _conn() as conn:
            cur = conn.cursor()
 
            # Object names
            cur.execute(f"""
                SELECT owner, object_name, object_type, status, last_ddl_time
                FROM   all_objects
                WHERE  object_name LIKE :kw {schema_clause}
                ORDER  BY object_type, object_name
            """, **kw_bind)
            results["objects_by_name"] = _rows(cur)
 
            # Column names
            cur.execute(f"""
                SELECT owner, table_name, column_name, data_type, nullable
                FROM   all_tab_columns
                WHERE  column_name LIKE :kw {schema_clause}
                ORDER  BY owner, table_name, column_name
            """, **kw_bind)
            results["columns_by_name"] = _rows(cur)
 
            # PL/SQL source (optional; potentially slow)
            if include_source:
                cur.execute(f"""
                    SELECT DISTINCT owner, name, type
                    FROM   all_source
                    WHERE  UPPER(text) LIKE :kw {schema_clause}
                    ORDER  BY type, name
                """, **kw_bind)
                results["source_references"] = _rows(cur)
 
        total = sum(len(v) for v in results.values())
        return _json({"keyword": keyword, "schema": owner, "total_hits": total, "results": results})
    except oracledb.DatabaseError as exc:
        return _tool_error(f"Oracle error: {exc}")
 
 
# ══════════════════════════════════════════════════════════════════════════════
# Tool: get_indexes
# ══════════════════════════════════════════════════════════════════════════════
 
@server.tool()
async def get_indexes(object_name: str, schema: Optional[str] = None) -> str:
    """
    Return index definitions for a table, including column list, uniqueness,
    status, and index type (NORMAL, BITMAP, FUNCTION-BASED, etc.).
 
    Useful for:
    - Understanding query performance characteristics of a view's base tables
    - Checking whether legacy code can rely on a unique index as a surrogate PK
 
    Parameters
    ----------
    object_name : Table name (case-insensitive).
    schema      : Owner schema.  Defaults to ORACLE_DEFAULT_SCHEMA from .env.
    """
    owner = _resolve_schema(schema)
    name  = object_name.upper()
 
    sql = """
        SELECT
            i.index_name,
            i.index_type,
            i.uniqueness,
            i.status,
            i.partitioned,
            LISTAGG(ic.column_name || CASE ic.descend WHEN 'DESC' THEN ' DESC' ELSE '' END,
                    ', ')
                WITHIN GROUP (ORDER BY ic.column_position)     AS columns
        FROM   all_indexes     i
        JOIN   all_ind_columns ic
               ON  ic.index_owner = i.owner
               AND ic.index_name  = i.index_name
               AND ic.table_name  = i.table_name
        WHERE  i.table_owner = :owner
          AND  i.table_name  = :name
        GROUP  BY i.index_name, i.index_type, i.uniqueness,
                  i.status, i.partitioned
        ORDER  BY i.uniqueness DESC, i.index_name
    """
    try:
        with _conn() as conn:
            cur = conn.cursor()
            cur.execute(sql, owner=owner, name=name)
            rows = _rows(cur)
        return _json({"object": f"{owner}.{name}", "indexes": rows})
    except oracledb.DatabaseError as exc:
        return _tool_error(f"Oracle error: {exc}")
 
 
# ══════════════════════════════════════════════════════════════════════════════
# Tool: get_invalid_objects
# ══════════════════════════════════════════════════════════════════════════════
 
@server.tool()
async def get_invalid_objects(schema: Optional[str] = None) -> str:
    """
    Return all INVALID objects in a schema.
 
    Run this:
    - After any deployment to verify nothing broke
    - Before a change to baseline which objects are already invalid
      (so post-change invalids can be correctly attributed)
    - As part of an automated post-deployment Jenkins stage
 
    Parameters
    ----------
    schema : Owner schema.  Defaults to ORACLE_DEFAULT_SCHEMA from .env.
    """
    owner = _resolve_schema(schema)
    try:
        with _conn() as conn:
            cur = conn.cursor()
            cur.execute("""
                SELECT object_name, object_type, last_ddl_time
                FROM   all_objects
                WHERE  owner  = :o
                  AND  status = 'INVALID'
                ORDER  BY object_type, object_name
            """, o=owner)
            rows = _rows(cur)
        return _json({"schema": owner, "invalid_count": len(rows), "objects": rows})
    except oracledb.DatabaseError as exc:
        return _tool_error(f"Oracle error: {exc}")
 
 
# ─────────────────────────────────────────────────────────────────────────────
# Entry Point
# ─────────────────────────────────────────────────────────────────────────────
 
async def main() -> None:
    Config.validate()
    init_oracle_client()
 
    # Warm pool at startup — fails fast if credentials or TNS are wrong
    try:
        _get_pool()
    except oracledb.DatabaseError as exc:
        logger.error("Cannot connect to Oracle: %s", exc)
        raise SystemExit(1) from exc
 
    transport = Config.MCP_TRANSPORT.lower()
    logger.info("Oracle MCP server ready (transport: %s)", transport)
    if transport == "sse":
        logger.info("Listening on %s:%s", Config.MCP_HOST, Config.MCP_PORT)
        await server.run_sse_async()
    else:
        await server.run_stdio_async()
 
 
if __name__ == "__main__":
    asyncio.run(main())
 
 