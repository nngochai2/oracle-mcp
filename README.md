# oracle-mcp

A collection of [Model Context Protocol (MCP)](https://modelcontextprotocol.io/) servers for Oracle databases. Each subdirectory targets a specific Oracle version, accounting for the driver differences, SQL dialect features, and data dictionary quirks that vary across major releases.

AI coding assistants (GitHub Copilot, Claude) use these servers to introspect a live Oracle schema directly — browsing tables, views, PL/SQL source, constraints, indexes, and dependencies — without any manual documentation.

## Repository structure

```
oracle-mcp/
├── oracle_11g/    # Oracle 11g (thick mode — Instant Client required)
└── ...            # future: oracle_12c/, oracle_19c/, oracle_21c/, ...
```

Each subdirectory is a self-contained implementation with its own:
- `src/oracle_mcp_server.py` — MCP server and tool definitions
- `tests/` — unit tests (mocked Oracle connection)
- `db_setup.sql` — DBA script to create a read-only MCP user
- `mcp.json` — VS Code / GitHub Copilot server declaration
- `requirements.txt` — pinned dependencies
- `.env.example` — configuration template

## Available implementations

| Directory | Oracle version | Driver mode | Notes |
|---|---|---|---|
| [`oracle_11g/`](oracle_11g/) | 11g (11.2+) | Thick (Instant Client) | `LONG` column handling; no thin-mode support |

## Why separate implementations per version?

Oracle introduces meaningful differences across major versions that affect MCP server behaviour:

- **Driver mode**: Oracle 11g requires thick mode (Oracle Instant Client). Oracle 12.1+ supports the thin wire protocol — no client installation needed.
- **Data dictionary**: Views like `ALL_VIEWS`, `ALL_SOURCE`, and `ALL_CONSTRAINTS` change column types and availability across versions.
- **SQL features**: `LISTAGG`, `FETCH FIRST`, JSON columns, and other syntax have version-specific availability.
- **DBMS_METADATA**: DDL formatting and grant handling differ between versions.

A single server trying to paper over all these differences would be fragile. Version-specific implementations stay simple and testable.

## Adding a new version

1. Copy the closest existing implementation as a starting point:
   ```bash
   cp -r oracle_11g oracle_19c
   ```

2. Update `src/oracle_mcp_server.py`:
   - Switch driver mode if targeting 12.1+ (thin mode — remove `init_oracle_client()`)
   - Replace `LONG` column handlers with `CLOB` where applicable
   - Add or remove SQL features available in the target version

3. Update `db_setup.sql` for any privilege or data dictionary differences in the target version.

4. Update `requirements.txt` with an appropriate `oracledb` version constraint.

5. Add the new directory to the table above.

## Common design principles

All implementations share the same security and architecture decisions:

- **Read-only enforcement at two layers**: a dedicated DB user with `SELECT`-only grants, plus an application-level regex guard that blocks `INSERT`, `UPDATE`, `DELETE`, `DROP`, `CREATE`, `ALTER`, `TRUNCATE`, `GRANT`, `EXEC`, `BEGIN`, `COMMIT`, and `ROLLBACK`.
- **Schema allowlist**: `ALLOWED_SCHEMAS` in `.env` constrains all tool results to named schemas, regardless of what the AI requests.
- **No hard-coded credentials**: all connection details come from `.env` (git-ignored).
- **stdio transport**: the server runs as a local process per developer — no shared network port required for local usage.

## Getting started

See the README inside the relevant version directory for full setup instructions:

- [oracle_11g — setup & usage](oracle_11g/README.md)
