# oracle-mcp

A [Model Context Protocol (MCP)](https://modelcontextprotocol.io/) server that gives AI coding assistants (GitHub Copilot, Claude) live read-only access to an Oracle 11g database schema — without manual documentation.

## How it works

The server runs as a local `stdio` process and exposes 10 database introspection tools over MCP. AI assistants can call these tools to explore schema structure, trace dependencies, read PL/SQL source, and run ad-hoc queries — all through the same chat interface used for coding.

## Prerequisites

- Python 3.11+
- [Oracle Instant Client](https://www.oracle.com/database/technologies/instant-client.html) installed on the host machine (thick mode is required for Oracle 11g)
- A read-only Oracle user — see [`oracle_11g/db_setup.sql`](oracle_11g/db_setup.sql)

## Installation

```bash
cd oracle_11g
python -m venv .venv

# Windows
.venv\Scripts\pip install -r requirements.txt

# Linux / macOS
.venv/bin/pip install -r requirements.txt
```

## Configuration

Copy `.env.example` to `.env` and fill in your credentials:

```bash
cp oracle_11g/.env.example oracle_11g/.env
```

| Variable | Description |
|---|---|
| `ORACLE_HOST` | Database host (default: `localhost`) |
| `ORACLE_PORT` | Listener port (default: `1521`) |
| `ORACLE_SERVICE` | Service name — preferred over SID |
| `ORACLE_SID` | SID — fallback if service name unavailable |
| `ORACLE_USER` | Read-only MCP user (see `db_setup.sql`) |
| `ORACLE_PASSWORD` | Password — never commit this |
| `ORACLE_CLIENT_LIB_DIR` | Path to Instant Client; leave blank to auto-discover |
| `ORACLE_DEFAULT_SCHEMA` | Schema used when no schema argument is passed to a tool |
| `ALLOWED_SCHEMAS` | Comma-separated schema allowlist; blank = all visible schemas |
| `MAX_ROWS` | Hard row cap on `execute_query` (default: `200`) |
| `LOG_LEVEL` | `DEBUG`, `INFO`, `WARNING` (default: `INFO`) |

> `.env` is git-ignored. Never commit credentials.

## VS Code / GitHub Copilot integration

Commit [`oracle_11g/mcp.json`](oracle_11g/mcp.json) to your repository. VS Code picks it up automatically and starts the MCP server as a local `stdio` process when Copilot needs it.

Each developer must:
1. Install Oracle Instant Client
2. Create `oracle_11g/.env` from `.env.example`
3. Run `pip install -r oracle_11g/requirements.txt`

No network port is required — the server communicates over stdin/stdout.

## Available tools

| Tool | Description |
|---|---|
| `execute_query` | Run an ad-hoc SELECT; row-capped; DML/DDL blocked |
| `describe_object` | Column metadata + comments for any table or view |
| `get_view_definition` | Full view DDL via `DBMS_METADATA`, falls back to `ALL_VIEWS.TEXT` |
| `list_objects` | Inventory of schema objects filtered by type or name pattern |
| `get_dependencies` | Upstream / downstream dependency graph via `ALL_DEPENDENCIES` |
| `get_package_source` | PL/SQL source for packages, procedures, functions, triggers |
| `get_constraints` | PK / FK / unique / check constraints with FK target resolution |
| `search_objects` | Keyword search across object names, columns, and PL/SQL source |
| `get_indexes` | Index definitions with column list and type |
| `get_invalid_objects` | All `INVALID` objects in a schema — use before/after deployments |

## Security

- **DB-level**: a dedicated `mcp_reader` user with `CREATE SESSION` and schema-scoped `SELECT` grants only (see `db_setup.sql`)
- **App-level**: a regex guard blocks any SQL containing `INSERT`, `UPDATE`, `DELETE`, `DROP`, `CREATE`, `ALTER`, `TRUNCATE`, `GRANT`, `REVOKE`, `EXEC`, `BEGIN`, `COMMIT`, `ROLLBACK`, and `SAVEPOINT`
- **Schema allowlist**: `ALLOWED_SCHEMAS` in `.env` restricts all tool results to named schemas, regardless of what the AI requests

## Running tests

```bash
cd oracle_11g

# Windows
.venv\Scripts\pytest tests/ -v

# Linux / macOS
.venv/bin/pytest tests/ -v
```

All Oracle connectivity is mocked — no live database required.

## Project structure

```
oracle_11g/
├── src/
│   └── oracle_mcp_server.py   # MCP server — all tools defined here
├── tests/
│   └── test_oracle_mcp_server.py
├── db_setup.sql               # DBA script to create mcp_reader user
├── mcp.json                   # VS Code MCP server declaration
├── pytest.ini                 # pythonpath + asyncio_mode config
├── requirements.txt
└── .env.example
```

## Deployment

The server currently runs in `stdio` mode — one process per developer machine. A Jenkins pipeline ([`Jenkinsfile`](oracle_11g/Jenkinsfile)) handles linting, testing, and packaging. The pipeline includes a stubbed `Deploy SSE Wrapper` stage for a future shared-server deployment where the whole team shares one MCP server instance over HTTP/SSE.
