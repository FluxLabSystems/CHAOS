# Command line

> **Advanced. You do not need this.**
>
> Everything in this document has a GUI path in
> [The desktop shell](./desktop-shell.md) and
> [The operator console](./operator-console.md), except where it says
> otherwise. The command line exists for four situations:
>
> - a **headless secondary node** with no display;
> - an **unattended install** or a scheduled task;
> - a **remote session** over a slow or text-only link;
> - a **partially deployed node** where the interface will not come up at all.
>
> If you are sitting in front of a working install, close this and use the
> shell.

Related: [Container deployment](./advanced-container-deployment.md) ·
[Windows packaging](./advanced-windows-packaging.md) ·
[Operations](./operations.md)

---

## 1. What it is

`chaos` is the platform's own operator command line. Its design rules explain
why it exists at all:

- **Subsystem imports are lazy.** `--help`, `init-db`, `status`, `backup`,
  `restore` and `export` keep working even when the registry loader, energy manager, alarm
  engine, ingest or simulator packages are absent or broken. A missing subsystem
  is reported with an actionable message and a dedicated exit code, never a
  traceback.
- **Every failure exits non-zero.**
- **Nothing invents state.** Counts, energy state and alarms are read from the
  database; where a table does not exist yet the CLI says so rather than
  printing a reassuring zero.

That is what makes it usable on a node where nothing else is.

### Where it lives

| Install | Path |
|---|---|
| The packaged Windows product | `<install root>\python\Scripts\chaos.exe` |
| A development checkout | `chaos` on the path after an editable install, or `python -m chaos.cli` |

### Global options

| Option | Effect |
|---|---|
| `--database-url` | Override the configured database for this invocation |
| `--data-dir` | Override the design-package directory (default `data/`) |
| `--log-level` | `CRITICAL`, `ERROR`, `WARNING`, `INFO` (default), `DEBUG` |
| `--version` | Print the platform version |

Every setting is also overridable through `CHAOS_*` environment variables. Note
the casing: the **platform** binds `CHAOS_<SCREAMING_SNAKE_CASE>`
(`CHAOS_DATABASE_URL`, `CHAOS_DATA_DIR`); the **gateway** binds
`CHAOS_<PascalCase>`. They share a prefix and are kept apart by the underscores.

### Exit codes

| Code | Meaning |
|---:|---|
| 0 | Success |
| 1 | Runtime failure |
| 2 | Usage error |
| 3 | Subsystem unavailable on this node |

Exit code 3 is the one that matters for scripting: it distinguishes "this node
does not have that subsystem installed" from "that subsystem failed".

---

## 2. The subcommands

| Command | Purpose | GUI equivalent |
|---|---|---|
| `init-db` | Create every platform table | First-run setup |
| `load-registry` | Load the design package into the registry | First-run setup |
| `load-all` | Registry + load schedule + alarm definitions | First-run setup |
| `validate` | Validate the design package against its schemas | Runs in CI |
| `serve` | Run the platform API | The gateway starts it (see section 8) |
| `simulate` | Delegate to the simulator | None |
| `retention` | Apply the data-retention policy | None — scheduled task |
| `backup` | Write a portable registry and configuration archive | None — scheduled task |
| `restore` | Restore a backup archive into an empty database (or, with `--force`, a populated one) | None — recovery procedure |
| `status` | Node role, database, counts, energy state, active alarms | The console's Home and Assets screens |
| `export` | Open-format export of registry and history | None |

---

## 3. Setting up a node by hand

This is what the gateway's first-run setup does for you. Run it by hand only on
a node with no gateway, or when you want the error message on your terminal
instead of in a log.

```sh
chaos validate            # the design package is intact
chaos init-db             # idempotent; creates tables, never alters them
chaos load-all            # registry + load schedule + alarm definitions
chaos status              # confirm what landed
```

`load-all --skip-missing` skips stages whose subsystem is not installed rather
than failing — which is what the gateway uses, because a node without the alarm
engine should still get a registry.

Expected after a successful load:

```text
Project CHAOS 0.4.0
  node role        : primary
  physical control : disabled

Registry and history
  assets                 90
  points                 702
  point_bindings         245
  power_load_profiles    12
  alarm_definitions      40
```

`physical control : disabled` is correct and should stay that way until a
subsystem has passed [commissioning](./commissioning.md).

The banner reads `Project CHAOS` followed by the platform version. Older
builds printed the project's former name here; if you see that, the install
predates the rename.

---

## 4. Status

```sh
chaos status
chaos status --json
```

`status` prints the node role, site, database URL, whether physical control is
enabled, the broker configuration, the historian backend, which services are
enabled, the registry and history counts, the energy state and the active
alarms.

`--json` is the machine-readable form, and it is what the gateway's first-run
setup reads to decide whether a database needs setting up.

---

## 5. Backup

```sh
chaos backup                       # portable registry + configuration archive
chaos backup --output /mnt/offsite/
chaos backup --include-history     # also telemetry samples and dead letters (large)
```

Default output is `var/backups/homestead-<role>-<UTC>.tar.gz`.

**Credentials are never included.** The archive holds the registry,
configuration and the design package — enough to rebuild the platform on new
hardware — as JSON and YAML that can be read with nothing but a text editor.

The archive contains a manifest with counts, provenance and node role; a restore
procedure; one JSON file per table; and the design package the registry was
built from.

### Restore

```sh
chaos restore var/backups/homestead-primary-<UTC>.tar.gz            # into an empty database
chaos restore var/backups/homestead-primary-<UTC>.tar.gz --dry-run  # validate the archive only
chaos restore var/backups/homestead-primary-<UTC>.tar.gz --force    # replace a populated database
```

`restore` reads the manifest and the per-table JSON straight from the archive
(nothing is extracted to disk), checks the archive kind, format version and row
counts against the files, refuses undeclared tables, and refuses a populated
database unless `--force` is given — in which case the archived tables are
cleared first. Rows are inserted in dependency order inside one transaction and
the counts are checked against the manifest before it commits, so a failure
part way through leaves the database as it was.

### The fuller backup set

The container deployment ships a script that additionally dumps the database,
the broker configuration, the dashboards and the deployment configuration, with
a manifest and checksums. Its default destination is `/var/backups/homestead`,
overridable with `BACKUP_DIR`. See
[Container deployment § Backups](./advanced-container-deployment.md#7-backups).

**Keep three copies**: local, on the secondary node in another structure, and
offline/off-property. See [Operations § Backup and restore](./operations.md#4-backup-and-restore).

---

## 6. Retention

```sh
chaos retention --dry-run          # the default: computes and rolls back
chaos retention --apply
```

Run it daily, off-peak, **after** the backup.

Two warnings:

- The dry run rolls its transaction back, so an implementation that committed
  internally would still persist. **Check the printed counts, not just the exit
  code.**
- Retention is a **deletion**. Never run it for the first time against a full
  production historian without a dump in hand.

The policy itself is in [Operations § Data retention](./operations.md#5-data-retention).

---

## 7. Export

```sh
chaos export --format yaml --include registry -o registry.yaml
chaos export --include state,history --since 2026-08-01T00:00:00Z -o baseline.json
chaos export --include all --format json -o everything.json
```

Groups: `registry`, `state`, `history`, `alarms`, `commands`, `energy`,
`maintenance`, `all`. Default is `registry` to stdout as JSON.

`--since` / `--until` take ISO-8601 timestamps; `--limit` caps rows per table.

This is the open-format export requirement, and it is also how commissioning
step 12 captures a subsystem's baseline.

---

## 8. Running the backend for development

**You normally do not.** The gateway starts the Python backend itself —
`Chaos:SuperviseBackend` is on by default — resolving the embedded runtime or,
in a checkout, the Python on your `PATH` with `src` on `PYTHONPATH`. See
[Visual Studio § What happens when you press F5](./visual-studio.md#what-happens-when-you-press-f5).

Run it yourself when you want it under its own debugger. Tell the gateway to
leave it alone and start it on the port the gateway expects:

```sh
CHAOS_SuperviseBackend=false    # in the launch profile, or the shell's environment
chaos serve --host 127.0.0.1 --port 8081
```

Then press F5 on `Chaos.Host` (or `Chaos.Shell`) and everything works: the
gateway proxies to it, `/health` returns 200 with `backend: "up"`, and the
launcher reaches *Project CHAOS is ready*. `/host/info` reports
`supervisor.registered: false` in that mode, which is the gateway saying it is
not managing the process — not that the process is absent.

`serve --reload` restarts on source changes, which is useful while working on
the Python half and pointless otherwise.

**Bind it to loopback.** The backend is an unauthenticated control API. It is
reached through the gateway or not at all — the gateway refuses a non-loopback
backend address unless explicitly configured otherwise.

### How the supervisor stops it

`serve` drives uvicorn through its `Server` object rather than `uvicorn.run`,
and registers a shutdown hook on the application. The backend's own
`POST /_supervisor/stop` route calls that hook, which flips uvicorn's
`should_exit` flag: the server then shuts down exactly as it would on Ctrl-C
— the background services and the bus are stopped in the lifespan's `finally`
— and `serve` exits 0. That is how the gateway's process supervisor stops the
backend without killing it.

The route is guarded three ways, and all three are deliberate:

| Guard | Effect |
|---|---|
| `CHAOS_SUPERVISOR_STOP_SECRET` unset | The route does not exist: 404, like any unknown path. A container or a development checkout has no supervisor, so nothing can stop it this way |
| Loopback peer only | 403 from any other address. uvicorn trusts `X-Forwarded-For` from `127.0.0.1` and the gateway always sets it, so a request *relayed* through the gateway presents the LAN client's address and is refused — only the supervisor's own connection to the backend's loopback listener qualifies |
| `X-Chaos-Supervisor-Secret` header | Compared in constant time against the setting; 403 on a mismatch |

A successful call answers `202 {"stopping": true}` and the stop follows once the
response is out. The gateway never proxies the route: `/_supervisor` is outside
every prefix in the route-ownership manifest, and an unclaimed path is not
forwarded. The route is absent from `/openapi.json` for the same reason it is
absent from [API reference](./api.md#13-unlisted-operational-routes): it is
not part of the platform API.

With `--reload`, uvicorn runs the application in a child process it restarts on
source change, so no hook can be handed to it; the route there falls back to
interrupting the worker. Development only, as the flag says.

---

## 9. The simulator

```sh
chaos simulate -- --list-scenarios
chaos simulate -- --scenario <name> --offline
```

A simulated site — solar, battery, inverter, generator, loads, rack, weather —
publishing real envelopes on real topics, either to a real broker or to the
in-process bus. `--offline` uses the in-process bus and prints a summary.

This is how every control path in the platform has been exercised, and it is
what the "bench test" commissioning step means. It is also the fastest way to
give a fresh install something to look at.

---

## 10. Development targets

A `Makefile` at the repository root wraps the common Python-side tasks —
installing the package, running pytest, running ruff, validating the design
package, and the container stacks. `make` with no target prints its own help.

It exists for CI and for headless work. **Building and testing the solution is
done in Visual Studio** — see [Building in Visual Studio 2026](./visual-studio.md).

---

## 11. The gateway's own switches

`Chaos.Host.exe` is the Windows service and the gateway in one executable. It
takes four switches of its own, parsed before anything else sees the command
line; every other argument goes to the host's configuration (for example
`--Chaos:ListenUrl=http://0.0.0.0:8080`).

| Switch | Does | Exit code |
|---|---|---|
| `--install-service` | Registers the `ChaosHost` Windows service for *this* executable under the virtual account `NT SERVICE\ChaosHost`, with automatic start, a dependency on `Tcpip`, the recovery policy (restart after 5 s, 15 s, then every 60 s, failure count reset daily), **recovery on a stop with an error as well as on a crash** (`sc failureflag … 1`), and the `Project CHAOS` Event Log source. Needs an elevated prompt | `0` done · `1` a command failed (usually: not elevated) · `3` not Windows |
| `--uninstall-service` | Stops and removes the service and the Event Log source. "It was not there" is not an error | as above |
| `--console` | Runs the gateway in the foreground with a console and without the Windows service lifetime — for commissioning and for watching the backend's output directly | the gateway's |
| `--service` | What the registered service passes. Runs as the service; consumed so it never reaches configuration | the gateway's |
| `--help` | Prints the switches and exits | `0` · `2` when switches contradict |

Each registration step is printed as the `sc.exe` command it runs, so the
output doubles as a script. Off Windows, `--install-service` prints the plan it
would have run and exits `3` rather than pretending.

**You normally never type these.** The MSI registers the service itself
(`ChaosServiceRegistration=msi`, the default) and owns its rollback; the
switches are the hand path, and the installer's alternative
`ChaosServiceRegistration=host` build calls exactly the same two commands.

When the service stops, the gateway's supervisor asks the backend to stop
first — `POST /_supervisor/stop` on the backend's loopback listener, with the
secret both halves read from `CHAOS_SUPERVISOR_STOP_SECRET` — and only kills
the process if that is not answered. See
[§ 8 How the supervisor stops it](#how-the-supervisor-stops-it).

One difference between the two paths is worth knowing: the MSI's recovery
policy restarts the service after a crash, but does not set the failure flag, so
a service that *stops itself with an error* — which is what the backend
supervisor does when its circuit breaker trips, see
[Troubleshooting § It starts, then stops](./troubleshooting.md#56-it-starts-then-stops-then-starts-again)
— is restarted by SCM only after `sc failureflag ChaosHost 1` has been run once,
elevated. `--install-service` does that for you.

---

## 12. Related reading

| Document | Why |
|---|---|
| [Container deployment](./advanced-container-deployment.md) | The Docker stacks, the image, upgrades |
| [Windows packaging](./advanced-windows-packaging.md) | The embedded runtime and the PowerShell build scripts |
| [Secondary control node](./secondary-control-node.md) | The node this document mostly exists for |
| [Operations](./operations.md) | What to do with the output |
| [Building in Visual Studio 2026](./visual-studio.md) | The normal way to build, run and test |
