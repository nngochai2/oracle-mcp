-- ==============================================================================
-- db_setup.sql
-- Oracle 11g — MCP read-only user setup
--
-- Run as DBA (SYSDBA or a privileged account).
-- Adjust schema names and tablespace to match your environment.
-- ==============================================================================


-- 1. Create the dedicated MCP read-only user
--    Use a strong password; this user will be in .env on developer machines.
CREATE USER mcp_reader
    IDENTIFIED BY "change_me_to_strong_password"
    DEFAULT   TABLESPACE users
    TEMPORARY TABLESPACE temp
    PROFILE   default;

-- 2. Minimum session privileges
GRANT CREATE SESSION TO mcp_reader;

-- 3. Read access to target schemas
--    Repeat for each schema the MCP server should expose.
--    Avoid granting SELECT ANY TABLE — keep it schema-scoped.
GRANT SELECT ANY TABLE TO mcp_reader;               -- OR use per-schema grants below

-- Preferred: explicit per-schema grants (more restrictive, recommended)
-- GRANT SELECT ON [[SchemaName]].[[TableName]] TO mcp_reader;
-- GRANT SELECT ON [[SchemaName]].[[ViewName]]  TO mcp_reader;
-- ... or use a schema-level grant if supported by your DBA policy:
-- GRANT SELECT ANY TABLE TO mcp_reader;            -- wide but bounded by ALLOWED_SCHEMAS in .env

-- 4. Data dictionary views (ALL_* views are accessible by default to
--    any user with CREATE SESSION, but make these explicit if your DBA
--    has locked down the data dictionary).
--    The following are READ by the MCP server:
--      ALL_OBJECTS, ALL_TAB_COLUMNS, ALL_COL_COMMENTS, ALL_TAB_COMMENTS,
--      ALL_VIEWS, ALL_SOURCE, ALL_DEPENDENCIES, ALL_CONSTRAINTS,
--      ALL_CONS_COLUMNS, ALL_INDEXES, ALL_IND_COLUMNS

-- 5. DBMS_METADATA (enables get_view_definition primary path)
--    The MCP server falls back to ALL_VIEWS.TEXT if this is denied,
--    but DBMS_METADATA produces cleaner, complete DDL.
GRANT EXECUTE ON DBMS_METADATA TO mcp_reader;

-- 6. Optional: restrict DBMS_METADATA to DDL only (defence in depth)
--    Oracle 11g does not support fine-grained DBMS_METADATA roles natively,
--    but the MCP server only calls GET_DDL which is read-only by nature.

-- 7. Verify
SELECT username, account_status, profile
FROM   dba_users
WHERE  username = 'MCP_READER';

SELECT privilege FROM dba_sys_privs  WHERE grantee = 'MCP_READER';
SELECT privilege FROM dba_tab_privs  WHERE grantee = 'MCP_READER';
