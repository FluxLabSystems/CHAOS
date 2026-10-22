# Operations runbooks

Procedures for running the property. Each runbook states what it assumes, what
to do, and what it does **not** cover.

These are organised by **event**. If you are staring at a symptom and want to
know what is wrong, go to [Troubleshooting](./troubleshooting.md) instead.

---

## Read this first

**The platform is a supervisor, not a protection system.** If a runbook here
conflicts with what the equipment in front of you is telling you, the equipment
wins. Level 0 protection — breakers, relief valves, float switches, high-limit
thermostats, emergency stops — is not something software gets a vote on.

**Nothing in this repository has ever been connected to real plant.** Every
runbook below has been exercised against the simulator and the in-memory bus.
None has been exercised against real equipment, because no part of this platform
has. That changes one subsystem at a time as
[commissioning](./commissioning.md) proceeds.

---

## 0. Daily and weekly

### Daily — two minutes, on the console's Home screen

Look at four things:

| Look at | Bad sign |
|---|---|
| Active alarms | Anything critical or major that nobody has acknowledged |
| The dead-letter count (the **Dead letters** card, with the list beneath it) | A count that **grows every day**. That is a binding that was never commissioned properly, not noise |
| The energy state and how old its evaluation is | A state derived from stale inputs. The screen shows the data quality alongside the state precisely so this is visible |
| The stale-point count (the **Stale points** card) | Points the platform admits it cannot see |

The freshness indicator in the top-right tells you whether what you are reading
is live at all. If the *API unreachable* banner is showing, you are looking at a
snapshot — see [Troubleshooting § The console has no data](./troubleshooting.md#6-the-console-loads-but-has-no-data).

### Also worth a glance: the annunciator

The `OUT OF SVC` count in the panel header is the honest measure of alarm
coverage. If it changes, something changed in the design package. See
[The annunciator panel](./annunciator.md).

### Weekly

- **Confirm the nightly backup ran.** Home → **Backups** shows the newest
  archive with its manifest and goes amber when it is more than 36 hours old;
  for the full backup set on the container stack, read its manifest for a
  failure count of zero. A maintainer can take an archive from the same card
  — see section 4.
- **Confirm the secondary node's replica is current.** Point the desktop shell's
  **Settings → Gateway** at the secondary node and compare its Assets counts
  with the primary's. Change it back afterwards.
- **Check the binding statuses** on the console's Assets screen. Anything still
  `tbd` is un-commissioned, whatever the wiring looks like.

### Monthly

- **Restore the latest backup into a scratch database and confirm the counts
  match the manifest** (`chaos init-db` on a scratch `CHAOS_DATABASE_URL`, then
  `chaos restore <archive>`, then `chaos status`; section 4.3). A backup you
  have never restored is a hypothesis.

---

## 1. Black start

Recovery from a de-energized property.

This runbook covers **the platform's part only**. The electrical sequence is
executed at the equipment by Level 0/1 controls, and at least one local
controller must be able to execute it **without the primary server rack**. If
the platform is required for a black start, the black start design is wrong.

### Prerequisites — verify physically, not on a screen

1. BMS and inverter controls have protected DC power or an approved manual
   source.
2. Battery temperature and voltage are within black-start limits.
3. Fire, smoke and emergency-stop conditions are clear.
4. Critical distribution can be isolated from non-critical branches.
5. A local controller can run the sequence unaided.

### The sequence — platform steps only

| Step | Action | Platform involvement |
|---:|---|---|
| 1–4 | Verify isolation and safety; energise BMS/inverter control power; close the battery contactor via native precharge; start the master inverter group | **None.** Do not attempt any of this from the console |
| 5 | Energise the critical control/communications bus | None |
| 6 | Start the secondary control node, core switch/router, minimum broker and time services | Start the secondary node. Time first — see section 6 |
| 7 | Validate battery, inverter, frequency and critical-bus measurements | **Read them at the equipment.** Then, once ingest is up, cross-check on the console's Energy screen |
| 8 | Energise minimum refrigeration, water protection, greenhouse survival and security loads, **staggered** | Manual. Simultaneous restoration is forbidden, and the energy manager is not running yet |
| 9 | Start the generator if reserve or battery limits require it | At the generator controller |
| 10 | Start primary rack services once the critical bus and container environment are stable | Launch the desktop shell; press **Start platform** if it is not already up |
| 11 | Reconcile actual asset states with the digital twin | Section 1.1 |

### 1.1 State reconciliation

**The platform must not assume that retained desired state equals physical
state.**

Once the platform is up:

1. **Expire stale commands.** Anything pending or dispatched from before the
   outage is meaningless. On the console's Control screen, cancel anything that
   has not already expired on its own time-to-live.
2. **Distrust retained bus state.** Retained topics may describe the world as it
   was before the lights went out.
3. **Find what the platform cannot see.** The stale-point list (Home →
   **Stale points**). Every point there is *unknown*, not *normal*.
4. **Walk the plant.** Confirm actual breaker, valve and equipment states against
   what the twin believes. **Correct the twin, not the plant.**
5. **Only then** let the energy manager resume. It must not begin dispatching
   against a state model that is a mixture of pre-outage memory and post-outage
   guesswork.

Do not restart workshop machinery, spa equipment or any attended load
unattended after a black start.

---

## 2. Communications loss

### 2.1 A gateway goes quiet

**Symptom:** points stale for one asset group; an availability message arrived;
alarms firing for that group.

1. **Is it the gateway or the network?** If ingest counts are still rising for
   other assets, the broker and ingest are fine and the problem is that device.
2. **Check the broker's log for repeated reconnects.** A gateway that reconnects
   in a loop is usually a credential or topic-permission problem, not a radio
   problem.
3. **Check for denied publishes** — a topic permission that was edited without
   being re-tested.
4. **The subsystem keeps running.** Level 1 retains control. **Loss of telemetry
   is loss of visibility, not loss of control.** Do not start power-cycling
   equipment to restore a dashboard.
5. **Record the outage against the asset.** If it recurs, it is a
   communications-loss commissioning step that was passed too generously.

### 2.2 Broker loss

Every gateway's telemetry stops at once, and commands cannot be dispatched.

1. Restart the broker. Persistent sessions and queued messages survive.
2. If it will not start, check its configuration volume — a missing password
   file stops it dead, and that is by design.
3. Expect a burst of retained-state and queued traffic on recovery, and expect
   state reconciliation (section 1.1) to matter.

Broker administration is a headless task; see
[Container deployment](./advanced-container-deployment.md).

### 2.3 Internet loss

**The platform is unaffected.** Local-first is the point.

| Stops | Continues |
|---|---|
| Outbound email and push | Ingest |
| Upstream time synchronisation | The energy manager |
| Remote VPN access | Alarm evaluation |
| | The console and the annunciator |
| | Commands |
| | Local notification |

The console fetches **nothing** from the internet — no CDN, no fonts, no map
tiles, no charting library — so it renders identically with the uplink down.

**But:** the MVP criterion requiring critical alarms to work during internet
loss is met only where the `email` or `push` channel has been configured
against an SMTP relay or push server **inside the property network** and proven
at commissioning step 10. With only `log` configured a log line is not an
alert, and `voice` is not implemented. See
[Alarms § Notification](./alarms.md#7-notification-and-the-honest-part).

### 2.4 Loss of the primary node

See [Secondary control node § What it can and cannot do](./secondary-control-node.md#3-what-it-can-and-cannot-do-when-the-power-container-is-lost).

Summary: the secondary node observes and alerts; local controllers keep the
property running; **nothing automatically takes over supervisory control**, and
that is deliberate.

To look at the secondary node from the desktop shell, point **Settings →
Gateway** at it. It will report its role as secondary, with the energy manager
suppressed and physical control disabled.

---

## 3. Alarm floods

**Symptom:** dozens or hundreds of alarms in seconds. Usually one root cause — a
power event, a comms failure, a gateway reboot.

### Do not

- **Do not bulk-acknowledge to clear the screen.** Acknowledgement is a record
  that a human saw it; bulk-acknowledging destroys the only evidence of what the
  operator actually knew.
- **Do not suppress the alarm definition.** That hides the next occurrence too.

### Do

1. **Find the incident, not the alarms.** The console's Alarms screen groups by
   incident first, precisely so one container outage does not read as hundreds
   of independent events. The annunciator shows the same thing as a panel: one
   lit window per condition.
2. **Sort by severity and time.** The earliest critical alarm is usually the
   cause; the rest are consequences.
3. **Ask whether the platform is the problem.** A flood with no plant symptoms is
   often ingest: a gateway republishing history with old timestamps, or a binding
   pointed at the wrong point. Check the dead-letter list (Home →
   **Dead-letter list**).
4. **Silence, then acknowledge, in that order.** On the annunciator, SILENCE HORN
   quiets the room without touching a single lamp, so you can think. Then
   acknowledge deliberately, tile by tile or as a set.
5. **Afterwards, review.** A flood is usually an alarm-design failure: a missing
   dead-band, a missing on-delay, or an alarm on a derived value that should have
   been an alarm on its input. Fix `data/alarm_definitions.yaml` and re-import
   from the launcher's **Run again**.

### Planned work: use maintenance mode, not suppression

On the console's Control screen, set the scope's operating mode to
`maintenance` — the setter on the asset's panel for one asset, or the one at
the top of the Control page for a domain or the whole site. That inhibits
automatic starts and modifies alarms **with the lockout visible**.

Suppressing an alarm makes the lockout invisible, which is how a subsystem gets
left in maintenance mode for three weeks.

---

## 4. Backup and restore

A backup is taken three ways, and all three write the same archive:

- **On a schedule, unattended** — the right shape for a task that should run
  at 02:00. On Windows the gateway schedules it; on the container stack cron
  runs `deploy/backup/backup.sh`, which wraps `chaos backup` in the fuller set
  described below — see
  [Container deployment § Backups](./advanced-container-deployment.md#7-backups).
- **From the console** — Home → **Backups** → *Take a backup now*, with the
  `maintainer` role. The action confirms first, writes one archive into this
  node's backup directory, lists it, and is audited under your name.
- **From the command line** — `chaos backup`, in
  [Command line § Backup](./advanced-command-line.md#5-backup).

All three write into this node's backup directory: `CHAOS_BACKUP_DIRECTORY`
when it is set, else `var/backups` under the workspace. On Windows the gateway
hands the backend its own scheduled-backup folder, so the schedule, the
console, the CLI and the backup-age monitor behind `backup_overdue` agree on
one place without anyone editing `homestead.env`.

Restoring stays a command-line procedure (4.3): it replaces the database under
a running platform and is done against a stopped node.

What matters operationally:

### 4.1 What a backup set contains

A database dump, a portable registry-and-configuration archive, the broker
configuration (**sensitive**), the dashboard configuration, the deployment
configuration, a manifest and a checksum file.

The environment file holding live secrets is **excluded deliberately**: a backup
that quietly contains every credential on the property is a liability.

### 4.2 Keep three copies

Local, **on the secondary node in another structure**, and offline/off-property.

A backup written to a disk inside the power container is not a backup. It is a
second copy in the same failure domain — and that domain is the one the whole
design assumes can be destroyed.

### 4.3 Verify

Check the checksums and read the manifest for a failure count of zero. Then,
monthly, **restore it into a scratch database and compare the counts against the
manifest.** A backup you have never restored is a hypothesis.

The drill is three commands against a scratch database that nothing else uses:

```sh
export CHAOS_DATABASE_URL=sqlite:////tmp/restore-drill.db
chaos init-db
chaos restore /var/backups/homestead/<archive>.tar.gz
chaos status            # counts should match manifest.json inside the archive
```

`chaos restore` validates `manifest.json` against the archive's contents before
touching anything, restores every table in foreign-key dependency order inside
one transaction, and checks the row counts against the manifest before it
commits — a restore that fails half-way leaves the database as it was. It
**refuses a database that already holds rows**; `--force` deletes the rows of
every table in the archive first (and only those, apart from foreign-key
cascades). `--dry-run` reports what would happen and writes nothing.

### 4.4 Rebuilding from the design package instead

Often faster and often better: `data/` in version control is the source of truth
for registry content. Point a fresh install's **Settings → Data directory** at
it and let first-run setup import it.

**History is not recoverable this way** — only the registry.

### 4.5 Restoring onto a machine with nothing

Install the platform, point `CHAOS_DATABASE_URL` at the new database, then
`chaos init-db` and `chaos restore <archive>`. Everything the archive holds —
registry, alarm and command history, work orders, notification log, current
state, and the historian if the backup was taken with `--include-history` —
comes back; credentials do not (4.6).

If even that is not possible, the portable archive is JSON plus YAML. Untar it
and read it: a manifest with counts, provenance and node role; `RESTORE.txt`;
one JSON file per table; and the design package the registry was built from.

That is the case where the container is gone and you have a laptop.

### 4.6 Credentials are re-issued, not restored

Broker identities, dashboard admin, database passwords. If the container was
destroyed or compromised, the secrets inside it should be assumed readable.
**Re-issue them**, and record the new identities in the registry.

---

## 5. Data retention

| Class | Policy |
|---|---|
| Raw high-frequency telemetry | 90 days |
| Downsampled 1-minute | 2 years |
| Downsampled hourly/daily | Indefinite |
| **Alarm and command audit** | **Indefinite — never pruned** |
| Maintenance and asset history | Indefinite |
| Camera footage | Separate policy, outside this platform |

Retention runs as a scheduled task, not from the console —
[Command line § Retention](./advanced-command-line.md#6-retention).

Two things worth knowing:

- The dry run rolls its transaction back, so **check the printed counts, not
  just the exit code**.
- Retention is a **deletion**. Run it on a schedule, after backups, and never
  for the first time on a full production historian without a dump in hand.

The retention limits are an initial policy, not a measured one — the decision
about what storage and power budget can actually support is unresolved.

---

## 6. Time synchronisation

Every timestamp the platform stores is UTC; display conversion happens in the
interface.

**Clock skew is a subtle failure.** It makes alarm correlation wrong, command
expiries wrong, and time-windowed queries silently return nothing. **If a screen
is empty while ingest counts are rising, check clocks before anything else.**

All servers, gateways, PLCs, cameras and field nodes should use the homestead's
own time service, with an external source when available and a local holdover
source when isolated.

**That time service is not deployed by this repository, and nothing here
provides holdover.** It is an open item.

---

## 7. Enabling physical control

**Do not skip to this section.**

1. The subsystem has passed all twelve commissioning steps. Confirm it on the
   console — the commissioning status for the asset must report supervisory
   control permitted.
2. Enable the specific binding from the console's Commissioning screen (or
   the commissioning endpoint). **The platform refuses if the prerequisites
   have not passed**, and that refusal is the point of the endpoint.
3. Only then turn on the platform-wide gate, and restart the platform so it
   takes effect.
4. **Verify** that the platform reports physical control as enabled — the
   console's Control screen and `/health` both carry it.
5. **Issue one command**, watch it acknowledge, and read the audit record before
   issuing a second.

Turning on the global gate does not arm anything that has not been commissioned.
That is deliberate: one switch should not be able to arm the property. See
[Control § The two safety gates](./control.md#6-the-two-safety-gates).

### Revoking it in an emergency

**Fastest: revoke at the broker.** Remove the command-topic write permission for
the affected identity and reload the broker. Application state is irrelevant if
the command cannot leave the bus.

**Then, properly:** turn the platform-wide gate off and restart the platform.

Then find out why, and **write it down**.

The broker step is a headless action; the exact commands are in
[Container deployment](./advanced-container-deployment.md).

---

## 8. Monitoring the platform itself

The platform exposes its own counters at `GET /metrics` in the Prometheus text
format, and the compose stack's Prometheus scrapes it (`deploy/prometheus/prometheus.yml`,
job `chaos`). What is there is deliberately narrow — counts the platform already
holds, never point values and never credentials:

| Series | What it tells you |
|---|---|
| `chaos_alarms_active{severity}` and `chaos_alarms_active_suppressed` | The same numbers as the console's alarm header, scrapeable |
| `chaos_incidents_open` | Correlated incidents still open |
| `chaos_ingest_dead_letters` | The dead-letter count from the daily check above — the one that must not grow every day |
| `chaos_ingest_events_total{event}` | The ingest subscriber's counters: received, written, dead-lettered, out of order, stale-marked |
| `chaos_notifications_total{channel,status}` | Delivery attempts by outcome. A rising `failed` count on `email` or `push` is a relay or push server problem, not an alarm problem |
| `chaos_mirror_readings_total{source,outcome}` | What each plugin mirror source published or dropped, with the drop reason |
| `chaos_plugin_health{plugin,state}` | One series per plugin per health state; the `1` is where the plugin currently is |

A subsystem that is not running on this node (the EMS on the secondary, ingest
with MQTT disabled) contributes **no series** and a comment line saying so, so
"ingest received nothing" and "there is no ingest here" stay distinguishable.

Two of the shipped alarms watch the platform itself, and both are live:

- **`backup_overdue`** is fed by the `backup_age_h` point the primary publishes
  every 30 s from the newest archive in its backup directory
  (`CHAOS_BACKUP_DIRECTORY`, the folder `chaos backup` and the console's Backup
  action write to; on the Windows product the gateway hands the backend its
  own scheduled-backup folder). A node that has never been backed up publishes
  a lower bound with `uncertain` quality, so the alarm still raises.
- **`service_unavailable`** fires for the API's own asset: with MQTT on, the
  backend registers a retained `offline` last will on that asset's availability
  topic and publishes `online` on connect, so a backend that dies without
  saying goodbye shows up on the panel.

On the Windows product `/metrics` is served on loopback only and is not routed
by the gateway (see [API § 12](./api.md#12-surface-size)); exposing it is the
gateway owner's decision, not a default.

---

## 9. What these runbooks do not cover

- **Electrical work.** Black start, generator start, transfer switching, battery
  isolation. Equipment procedures, not platform procedures.
- **Water, greenhouse, spa, nitrogen storage.** No coordinator exists for these
  yet. The water system's *design* is written up in
  [Water-system control narrative](./water-control-narrative.md); nothing
  implements it.
- **Camera and NVR operations.** Not integrated.
- **Network device recovery.** Switch, router and firewall procedures.
- **Anything requiring hardware that has never been connected.** Which is
  everything, today.

---

## 10. Related reading

| Document | Why |
|---|---|
| [Troubleshooting](./troubleshooting.md) | Same problems, indexed by symptom |
| [Commissioning](./commissioning.md) | The path from "observing" to "controlling" |
| [Control](./control.md) | Interlocks, modes and the safety gates |
| [Alarms](./alarms.md) | The model behind floods and incidents |
| [Secondary control node](./secondary-control-node.md) | What survives losing the power container |
| [Command line](./advanced-command-line.md) | Backup, retention and export mechanics |
