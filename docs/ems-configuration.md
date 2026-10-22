# EMS configuration

Every number the energy manager acts on — reserve thresholds, dwell times,
shed and restore timing, generator permissives, lease limits, the forecast
margin — is a field of `EmsConfig` in `src/chaos/ems/config.py`. SDD 30.2 is
explicit that these are **commissioning parameters, not facts about the
homestead**: the design portfolio still holds two incompatible energy baselines
([DD-001](./design-decisions/DD-001-energy-capacity-revision.md), unresolved),
so the defaults are documented starting points that have to be re-commissioned
against the selected battery chemistry, inverters, generator, climate and the
measured site loads. No threshold is written at a call site; every module takes
the one `EmsConfig`, which is what makes a change to it a single auditable
event (SDD 39 test `EMS-T015`).

This page covers how the effective configuration is built, where a deployment
overrides it, what happens when the override is wrong, how a change is
recorded, and how to read it all back.

Related: [The design package](./design-package.md) ·
[Integration findings](./integration-findings.md) ·
[Control](./control.md) · [Operations](./operations.md)

---

## 1. The effective configuration

`chaos.ems.config.load_ems_config()` builds it in two steps:

1. the built-in defaults — `EmsConfig()`, the values in the source file;
2. the overrides in the file named by the `CHAOS_EMS_CONFIG_FILE` environment
   variable, when it is set.

Two consumers call it, and both reconcile the result against the same audit
table (§5), so whichever runs first records the revision and the other finds it
already there:

| Consumer | When | Why |
|---|---|---|
| `EnergyManagerService` — the background EMS, primary node only | When it is built at platform start, then **at the start of every tick** (5 s by default) | So a threshold change takes effect at the next evaluation, without a restart |
| `GET /api/v1/energy/config` and every other `/api/v1/energy/*` read | On each request | So the dashboard evaluates with the configuration the service runs on, and a node that runs no EMS (the secondary) still reports what the configuration would be |

### Where the variable is read

`CHAOS_EMS_CONFIG_FILE` is read by the **EMS module** (`chaos.ems.config`)
from the process environment. It is not a field of the platform `Settings`
object in `chaos.config`, which ignores `CHAOS_*` names it does not define —
the EMS owns its tunables without widening the platform settings surface.

Two consequences:

- Set it in the environment of the process that runs the platform: the
  container's environment, the systemd unit, the shell that runs
  `chaos serve`. A line in `.env` is **not** enough — `Settings` reads `.env`
  for its own fields and does not export it to the process.
- A relative path resolves against the working directory of that process.
  Prefer an absolute path.

Unset, or set to an empty string, means the built-in defaults are in force and
the API says so (`source.kind: defaults`).

---

## 2. The file

YAML (any suffix but `.json`) or JSON (`.json`). A flat mapping of `EmsConfig`
field names to values, plus two optional attribution keys that are recorded on
the revision and never applied as tunables:

```yaml
changed_by: j.bloggs
reason: BMS manual rev C, section 4.2; warning threshold lowered for the installed chemistry
battery_temp_warning_c: 42
thermal_derate_battery_curve:
  - [42, 0]
  - [48, 30]
  - [55, 100]
surplus_minimum_window_min: 45
shed_states: [CONSERVE, CRITICAL_RESERVE, EMERGENCY, GENERATOR_SUPPORT]
```

The rules, each of which is a deliberate choice:

- **A field that is not in `EmsConfig` is rejected**, by name. A misspelt
  field never silently does nothing.
- **Values are checked against the field's type.** Float fields accept
  integers; int fields do not accept `1.5`; booleans must be booleans, not
  `1`; strings must be strings. Nothing else is coerced.
- **Lists stand in for tuples.** `shed_states: [CONSERVE, CRITICAL_RESERVE]`
  replaces the whole tuple. A mapping field (`restore_stagger_s`) replaces the
  whole mapping and must keep the `medium` class, which is the fallback
  stagger.
- **The thermal derate curves take a list of `[temperature_c, derate_pct]`
  pairs** — at least two, each derate within 0–100 %. Omitting a curve, or
  setting it to `null`, keeps the threshold-derived default (§4).
- **Combinations `EmsConfig` itself refuses are refused here too**, with the
  same message: a warning threshold at or above its emergency threshold, for
  example.
- **Every problem is listed in one message**, so a file with three mistakes is
  fixed in one pass.

---

## 3. What happens when the file is wrong

The EMS does **not** fall back to the defaults the file was meant to override.
A file that is missing, unreadable, malformed, or carries a value that fails
the rules above is an error:

| When | What happens |
|---|---|
| At platform start | The energy manager service is not built. The log reads `Energy manager unavailable` followed by every problem, and every `/api/v1/energy/*` read answers **500** with the same text until the file is fixed. Nothing else on the node is affected |
| While the EMS is running | The last good configuration stays in force — a typo must not stop supervisory control. The problem is logged once per distinct message, and the tick result (`last_tick.config_error`) carries it until the file is readable again |

A missing file is an error, not "defaults": if the variable names a file, the
file is the configuration.

---

## 4. The thermal derate curves

`thermal_derate_pct` (SDD 30.6) is a piecewise-linear derate over the battery
and container temperatures, worst of the two. By default each curve is
**derived from the two thresholds it sits between**: 0 % at the warning
threshold (`battery_temp_warning_c`, `container_temp_warning_c`) rising to
100 % at the emergency threshold (`battery_temp_emergency_c`,
`container_temp_emergency_c`), flat beyond both ends. Change a threshold and
the ramp moves with it; a warning threshold at or above its emergency threshold
is refused.

A curve given explicitly — the battery manufacturer's curve, the container HVAC
capacity measured at commissioning — wins over the derivation. Whichever is in
force, the derived value's `assumptions` name it, and `GET /api/v1/energy/config`
shows the curve as resolved.

The defaults (45 → 55 °C for the battery, 40 → 50 °C for the container) are
commissioning placeholders, not manufacturer data.

---

## 5. The configuration revision (SDD 39 `EMS-T015`)

`EMS-T015` — *configuration threshold changed* — expects the revision, the
author, the old and new values and the approval to be recorded. The platform
records the first four in `configuration_revisions`, the same table the
registry loader writes (SDD 5.6):

| Column | Value |
|---|---|
| `target_type` | `ems_config` |
| `target_id` | the site (`CHAOS_SITE_ID`) |
| `revision` | counts up per site, from 1 |
| `changed_by` | the file's `changed_by`; `unattributed (CHAOS_EMS_CONFIG_FILE)` when the file gives none; `chaos.ems.config (built-in defaults)` when no file is set |
| `reason` | the file's `reason`, or a generated one naming the file |
| `diff.old` | the previous effective configuration, `null` for the first revision |
| `diff.new` | the effective configuration, every field |
| `diff.changes` | per changed field, `{old, new}` — compared with the previous revision, or with the built-in defaults for the first |
| `diff.source` | kind, path, the fields the file set, attribution |
| `diff.approved_by` | **always `null`** — see below |

A revision is appended **only when the effective configuration differs from
the last one recorded** for the site. Restarts, repeated reads of the API and a
file re-saved with the same values add nothing; a file re-saved with the same
values but a new `reason` adds nothing either, because nothing the EMS acts on
changed. Reverting to the defaults is a change like any other and is recorded
with the fields that moved back.

**Approval is an owner decision made outside the platform.** Nothing here can
grant it: there is no approver role, no write endpoint and no workflow, and
inventing one would present a platform click as a decision the owner has not
taken. Every revision therefore records `approved_by: null` and the API says
so. Cite the approving record — a signed commissioning sheet, a design decision
record that has moved to `accepted` — in the file's `reason`; that is the
auditable link.

The order of events for a threshold change is: edit the file → the next tick
re-reads it → the revision is appended → the new configuration is in force on
every coordinator → a `WARNING` log line names the revision, the reason and
the author.

---

## 6. Reading it back

`GET /api/v1/energy/config` is read-only. There is no endpoint that changes a
threshold: the file is the change, and it is a deployment artefact under the
owner's control.

| Field | Meaning |
|---|---|
| `config` | Every `EmsConfig` field as resolved, tuples as lists |
| `source.kind` | `defaults` or `file` |
| `source.path`, `source.env_var` | The file in force and the variable that named it |
| `source.overrides` | The fields the file set, with the values as applied |
| `source.changed_by`, `source.reason` | The file's attribution, or the generated placeholders |
| `revision.id`, `revision.revision` | The `configuration_revisions` row the effective configuration matches |
| `revision.recorded_by_this_load` | `true` when this request appended the revision; `false` when it was already there |
| `target` | `ems_config` and the site |
| `approved_by` | `null` (§5) |
| `notes` | The three rules above, restated for whoever reads the payload without this page |

The same reconciliation runs on every other `/api/v1/energy/*` read, so the
dashboard, the load budgets and the lease decisions are always computed with
the configuration this endpoint reports.

---

## 7. What this does not do

- It does not schedule or profile thresholds by time of day or season. One
  configuration is in force at a time.
- It does not change equipment-native limits. A BMS charge limit, an inverter
  rating or a generator controller's attempt policy is not an `EmsConfig`
  field and is never overridden from here (SDD 30.9, 34.3).
- It does not hold the forecast's inputs. Where the site is and how the PV
  array is rated come from the registry, not from this file — see the
  [design package](./design-package.md#5-preserved-conflicts) for the array
  rating's status.
