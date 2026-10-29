---
title: Install and run
description: Install PowerContext 1.4.1 and run the local Server.
---

# Install and run

For an Agent connecting from another machine, see [Connect to a remote Server](../operate/connect-remote-server.md)
for guided URL confirmation, unattended setup, and endpoint-bound HTTP consent.

Start with the [Quick Start](quickstart.md) for your first session. This page covers version selection,
platforms, installation roles, startup, diagnostics, and updates.

## Platform support

| Platform | Status |
| --- | --- |
| macOS, Linux | Supported |
| Windows | `experimental` |

Windows CLI, Server, and personal-service support is experimental. Each Agent Host still has its own platform
requirements. Examples using Bash syntax require a Bash environment and cannot be pasted directly into PowerShell.
Embedded seekDB is unavailable on Windows.

## Choose a version

These instructions use PowerContext 1.4.1. Keep the package and Agent integration on
the same version: package `1.4.1` and Git tag `powercontext-v1.4.1`.

```bash
uv tool install --force "powercontext[cli,server]==1.4.1"
powercontext setup codex --ref powercontext-v1.4.1
```

Check the [capability matrix](../integrations/capabilities.md) for host support and maintenance status.
Capabilities marked `experimental` remain experimental in this release.

## Install the application

You need Python 3.11 or newer, Git, and [`uv`](https://docs.astral.sh/uv/) on macOS, Linux, or Windows. Then install
PowerContext from PyPI:

```bash
uv tool install --force "powercontext[cli,server]==1.4.1"
```

For a source installation of the same version:

```bash
uv tool install --force "powercontext[cli,server] @ git+https://github.com/oceanbase/powercontext.git@powercontext-v1.4.1"
```

For a machine that only connects to a remote Server, the `cli` extra includes `setup`, `doctor`, and remote content
commands. Add `server` to run a local Server or use the `service` commands. If a command provider has a missing
dependency, the CLI reports the provider and missing module so the installation can be repaired.

The Git command does not leave a repository checkout for you to manage. Git uses its normal credential configuration,
including credential helpers and SSH settings. For an SSH-based install, replace the HTTPS URL with the Git URL
approved for your environment. `--force` also refreshes an existing tool from the current commit behind the selected
Git ref; without it, `uv` may report the same requirement as already installed without fetching a newer `master`.

To install another branch or tag, replace the ref after the final `@`. The `master` branch can include unreleased changes.
Follow the [guide for each integration](../integrations/index.md) for Agent installation, connection options, and verification, using the same ref as the Server.

## Run the local Server

```bash
powercontext server run
```

Without environment variables or an environment file, the Server:

- binds to `127.0.0.1:8000`;
- enables Streamable HTTP MCP at `/mcp`;
- creates a default Scope;
- creates a persistent SQLite database in the operating system's user data directory;
- supports explicit Memory operations without an inference provider.

`Ctrl-C` performs a clean shutdown. Restarting the command reopens the same database.

The Dashboard is an optional content viewer for personal use and demonstrations. It is disabled by default and needs
no separate frontend installation or model configuration. To enable it locally without a token, save these settings
in an environment file:

```dotenv
POWERCONTEXT_SERVER_HTTP_HOST=127.0.0.1
POWERCONTEXT_SERVER_DASHBOARD_ENABLED=true
POWERCONTEXT_SERVER_ACCESS_MODE=disabled
```

To require authentication, set `POWERCONTEXT_SERVER_ACCESS_MODE=enforced` and set `POWERCONTEXT_SERVER_AUTH_TOKEN` to
your own long random credential. The [configuration wizard](configure-server-environment.md#local-dashboard-and-optional-authentication)
also offers this choice when enabling Dashboard locally.

```bash
chmod 600 /path/to/powercontext.env
powercontext config validate --env-file /path/to/powercontext.env
powercontext server run --env-file /path/to/powercontext.env
```

Open `http://127.0.0.1:8000/dashboard/home`, using the actual port if you change it. With authentication disabled, the
page opens directly. When enabled, sign in with the Server token and configure connected Agents to use it for API
and MCP requests. The CLI does not automatically load a directory's `.env` file.

The first visit selects the Server default Scope. Pages are empty until content is saved. Save a Memory through an
Agent or public API, then refresh Memories in the same Scope. Experiences, skills, handoffs, and usage also come from
saved records. The Dashboard does not capture sessions, run generation, or approve candidates. The Dashboard and Agent
must use the same Server and Scope.

Open **Profile** to read the saved profile, inspect **Version history**, or verify its sources. Selecting a historical
revision does not change the current profile. In **Handoff**, use **Export Markdown** on a collection entry or its
detail page to download that exact revision, including its full text, omissions, and citations. If sign-in expires,
sign in again to return to the selected detail, then repeat the download.

When authentication is enabled, all token holders use one identity. Multi-user RBAC deployments should leave the
Dashboard disabled and use the API, MCP, or host integrations. See [Deploy the Server](../operate/deploy-server.md)
for network and credential configuration.

This minimal launch does not enable model-backed extraction or vector search. To generate and validate one explicit
environment file for those capabilities, continue with the
[Enable extraction and vector search](configure-models.md).

## Use embedded seekDB

Embedded seekDB is available on Linux and macOS when a compatible `pylibseekdb` wheel is available. Windows does not
support this embedded backend. Install or replace the tool with the optional seekDB extra:

```bash
uv tool install --force "powercontext[cli,server,seekdb]==1.4.1"
```

When switching from SQLite, remove `POWERCONTEXT_SERVER_DATABASE_URL` from the Server process environment. An explicit
SQLAlchemy database URL is not valid for seekDB. Then select the backend and start the Server:

```bash
unset POWERCONTEXT_SERVER_DATABASE_URL
export POWERCONTEXT_SERVER_DATABASE_KIND=seekdb
powercontext server run
```

`server run` loads `.env` from the current directory when present. Export values in the shell to override that file,
pass `--env-file <path>` to select another file, or pass `--no-env-file` to ignore environment files. Process managers
and containers should normally provide an explicit environment instead of relying on their working directory.

PowerContext always uses seekDB's built-in `test` database. Leave `POWERCONTEXT_SERVER_DATABASE_PATH` unset to store
the instance in the `seekdb` subdirectory of the PowerContext user data directory. If `POWERCONTEXT_HOME` is set, the
default is `$POWERCONTEXT_HOME/seekdb`; set `POWERCONTEXT_SERVER_DATABASE_PATH` only when a different location is
required.

In another terminal, verify that the Server and database are ready:

```bash
powercontext doctor
powercontext ready
powercontext capabilities
```

## Verify the installation

```bash
powercontext doctor
powercontext ready
powercontext capabilities
```

`doctor` checks the installed package, Server liveness, and Server readiness without requiring an integration. Server
readiness covers the database and each configured inference provider. Runtime or database failures return
`not_ready`; an inference failure returns `degraded` without removing database-backed operations from traffic.
`ready` and `capabilities` show the readiness and enabled capabilities of the running service.
For Agent diagnostics, use the [guide for each integration](../integrations/index.md). For Server status definitions and recovery steps, see [Troubleshoot](../operate/troubleshoot.md).

For a long-running process, Docker, authentication, or remote access, continue with
[Deploy the Server](../operate/deploy-server.md).

## Update or replace an installation

<!-- upgrade-support:start -->
Upgrades from 1.3.0 and 1.4.0 require passing full Server acceptance on SQLite, seekDB and OceanBase. These migration paths are not yet verified; do not upgrade existing databases.
<!-- upgrade-support:end -->

Keep existing deployments on their working version until their source version and backend have an accepted upgrade
path. To evaluate a fresh installation, use a separate program environment, database location, and port. Replacing
the installed package does not migrate its database. Backups do not make an unsupported migration supported.

The support declaration is checked against the release matrix and installed-artifact acceptance; see
[Preparing a release](../../development/releasing.md). Domain-specific
[Processing](../operate/artifact-processing-migration.md) and
[Atomic Memory](../operate/atomic-memory-migration.md) maintenance guides do not establish full Server readiness.

Review the [Dream coordinated upgrade requirements](../workflows/review-dream-revisions.md#coordinated-upgrade)
before upgrading an existing database. The unified Candidate routes replace `/v1/artifact-candidates/*` with
`/v1/candidates/*` without aliases, and Dream Clients require contract 2. Existing Dream storage needs an explicit
migration after unfinished runs have drained. [Full Server migration](../operate/database-migrations.md) requires
`state=ready` and `server_ready=true`; partial Artifact or Atomic Memory verification cannot replace it. Do not bypass
the migration gate.

To install 1.4.1 in a separate environment for a fresh deployment:

```bash
uv venv /path/to/powercontext-preview
uv pip install --python /path/to/powercontext-preview/bin/python "powercontext[cli,server]==1.4.1"
```

To install another Git ref in the separate environment:

```bash
uv pip install --python /path/to/powercontext-preview/bin/python "powercontext[cli,server] @ git+https://github.com/oceanbase/powercontext.git@<ref>"
```

Use the matching ref for clients and Agent integrations connected to the fresh deployment. Preserve the original
deployment's database location and configuration; do not point the new Server at it as an installation check.
On Windows, the virtual environment interpreter is at `Scripts/python.exe`.

## Install a Python role

An application that imports the async Client SDK should add it to that application's environment:

```bash
uv add "powercontext[client]==1.4.1"
```

Use `builtin` for in-process Python composition, `server` for the service, `client` for the Python SDK, or `cli` for
the Server-backed command line. An extra that is only present in the isolated `uv tool` environment is not importable
by an unrelated Python project.
