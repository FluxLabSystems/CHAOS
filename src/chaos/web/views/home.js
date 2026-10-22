/* ---------------------------------------------------------------------------
   SDD 17.1 home screen.

   One request (`GET /overview`) paints this whole page, because the wall
   display has to come up on a local network with no internet and no patience.

   The screen's job is to be honest at a glance. Three different kinds of
   "nothing" are drawn differently and never as a zero:

     Not yet deployed — no such asset exists on the property yet
     No data         — the asset is registered but has never reported
     Live            — a real measurement, with its age and quality attached
--------------------------------------------------------------------------- */

import {
  card, clear, confirmAction, fmtAge, fmtDateTime, fmtDuration, fmtNumber, h, kv,
  meterBar, metricReadout, provenance, requireRole, resultProblem, severityChip, statusChip, toast,
} from '../app.js';
import { api } from '../api.js';

const SITE_STATE_COPY = {
  nominal:        { status: 'ok',          headline: 'Nominal' },
  degraded:       { status: 'degraded',    headline: 'Degraded' },
  alarm:          { status: 'alarm',       headline: 'Critical alarm active' },
  emergency:      { status: 'alarm',       headline: 'EMERGENCY' },
  pre_deployment: { status: 'design_only', headline: 'Pre-deployment' },
  unknown:        { status: 'unknown',     headline: 'Unknown' },
};

/** A metric tile: big number when live, the reason when not. */
function metricTile(title, metric, { digits = 1, extra = null, unit } = {}) {
  const status = metric ? metric.status : 'unknown';
  return card({
    title,
    status,
    children: [
      metricReadout(metric, { digits, unit }),
      extra,
      provenance(metric),
    ],
  });
}

function siteHero(data) {
  const site = data.site || {};
  const state = site.operating_state || {};
  const mode = site.operating_mode || {};
  const copy = SITE_STATE_COPY[state.state] || SITE_STATE_COPY.unknown;

  return h('section', { class: 'card', dataset: { status: copy.status } },
    h('div', { class: 'card-head' },
      h('h3', { text: 'Property operating state' }),
      statusChip(copy.status, state.label || copy.headline, true)),
    h('div', { class: 'stat-row', style: 'margin:.3rem 0 .6rem' },
      h('span', null, h('b', { text: String(state.assets_total ?? 0) }), ' assets registered'),
      h('span', null, h('b', { text: String(state.assets_deployed ?? 0) }), ' installed'),
      h('span', null, 'Lifecycle: ', h('b', { text: state.lifecycle_phase || 'unknown' }))),
    kvMode(mode),
    (state.basis || []).length
      ? h('ul', { class: 'provenance' }, (state.basis || []).map((line) => h('li', { text: line })))
      : null);
}

function kvMode(mode) {
  if (!mode || !mode.available) {
    return h('p', { class: 'card-note' },
      statusChip('no_data', 'Operating mode not set'), ' ',
      mode && mode.note ? mode.note : 'No site operating mode has been recorded.');
  }
  return h('p', { class: 'card-note' },
    h('span', { class: 'chip chip-ok' },
      h('span', { class: 'chip-glyph', 'aria-hidden': 'true', text: '⚙' }), `Mode: ${mode.mode}`),
    ` set by ${mode.changed_by || 'unknown'} ${mode.changed_at ? fmtAge(mode.changed_at) : ''}`
    + (mode.reason ? ` — ${mode.reason}` : ''));
}

function alarmTile(data, ctx) {
  const alarms = data.alarms || {};
  const urgent = (alarms.top || []).filter((a) => ['emergency', 'critical', 'major'].includes(a.severity));
  const total = (alarms.emergency_active || 0) + (alarms.critical_active || 0) + (alarms.major_active || 0);
  const status = total ? 'alarm' : (alarms.active_total ? 'degraded' : 'ok');

  const body = [];
  body.push(h('div', { class: 'stat-row', style: 'margin-bottom:.5rem' },
    h('span', null, h('b', { text: String(alarms.critical_active + alarms.emergency_active || 0) }), ' critical'),
    h('span', null, h('b', { text: String(alarms.major_active || 0) }), ' major'),
    h('span', null, h('b', { text: String(alarms.unacknowledged || 0) }), ' unacknowledged'),
    h('span', null, h('b', { text: String(alarms.open_incidents || 0) }), ' open incidents')));

  if (!urgent.length) {
    body.push(h('p', { class: 'card-note', text: alarms.active_total
      ? 'No critical or major alarms. Lower-severity alarms are on the alarms screen.'
      : 'No active alarms.' }));
  } else {
    body.push(h('ul', null, urgent.slice(0, 5).map((alarm) => h('li', {
      class: 'alarm-row', style: 'grid-template-columns:auto 1fr; padding:.35rem 0; border-bottom:none',
    },
      severityChip(alarm.severity, null, alarm.severity === 'critical' || alarm.severity === 'emergency'),
      h('div', { class: 'alarm-main' },
        h('div', { class: 'alarm-msg', text: alarm.message || alarm.alarm_key }),
        h('div', { class: 'alarm-key', text: `${alarm.asset_name || alarm.asset_id || 'site'} · ${alarm.state} · ${fmtAge(alarm.detected_at)}` }))))));
  }
  body.push(h('button', {
    class: 'linkish', style: 'margin-top:.5rem',
    onclick: () => ctx.navigate('/alarms'),
    text: 'Open the alarm screen →',
  }));
  return card({ title: 'Active alarms', status, children: body });
}

function energyTiles(data) {
  const energy = data.energy || {};
  const tiles = [];

  const reserve = energy.reserve_pct || {};
  const autonomy = energy.autonomy_current_h || {};
  const autonomyCritical = energy.autonomy_critical_h || {};
  tiles.push(card({
    title: 'Battery reserve',
    status: reserve.status || 'unknown',
    children: [
      metricReadout(reserve, { digits: 1, unit: '%' }),
      meterBar(reserve.available ? reserve.value : null, { hatched: !reserve.available }),
      h('dl', { class: 'kv', style: 'margin-top:.6rem' },
        h('dt', { text: 'Autonomy now' }),
        h('dd', null, autonomy.available ? fmtDuration(autonomy.value) : statusChip(autonomy.status || 'no_data')),
        h('dt', { text: 'Critical loads only' }),
        h('dd', null, autonomyCritical.available ? fmtDuration(autonomyCritical.value) : statusChip(autonomyCritical.status || 'no_data')),
        h('dt', { text: 'Energy state' }),
        h('dd', null, energy.state
          ? h('span', { class: 'tag', text: energy.state })
          : statusChip('no_data', 'EMS not reporting'))),
      provenance(reserve),
    ],
  }));

  tiles.push(metricTile('PV production', energy.pv_production_kw, { unit: 'kW' }));
  tiles.push(metricTile('Total site load', energy.site_load_kw, { unit: 'kW' }));

  const net = energy.net_power_kw || {};
  tiles.push(card({
    title: 'Net power (calculated)',
    status: net.status || 'no_data',
    children: [
      net.available
        ? h('div', { class: 'readout' },
            h('span', { class: 'value num', text: `${net.value > 0 ? '+' : ''}${fmtNumber(net.value, 1)}` }),
            h('span', { class: 'unit', text: 'kW' }))
        : metricReadout(net),
      h('p', { class: 'card-note', text: net.note || 'Requires both PV production and total load.' }),
    ],
  }));

  return tiles;
}

function waterTile(data) {
  const water = data.water || {};
  return card({
    title: 'Water reserves',
    status: water.status || 'unknown',
    children: [
      metricReadout(water.reserve_pct, { unit: '%' }),
      water.reserve_pct && water.reserve_pct.available
        ? meterBar(water.reserve_pct.value)
        : meterBar(null, { hatched: true }),
      h('p', { class: 'card-note', text: water.note
        || (water.status === 'not_deployed'
          ? 'No water assets are registered yet. This is not a reading of zero.'
          : 'Water instrumentation has not reported.') }),
    ],
  });
}

function weatherTile(data) {
  const weather = data.weather || {};
  const temp = weather.outside_temperature_c || {};
  const risk = weather.risk || {};
  const riskLabel = risk.available
    ? { freeze: 'Freeze risk', heat: 'Heat risk', none: 'No freeze or heat risk' }[risk.risk] || 'Unknown'
    : null;
  return card({
    title: 'Weather and freeze risk',
    status: temp.status || 'unknown',
    children: [
      metricReadout(temp, { unit: '°C' }),
      riskLabel
        ? h('p', { style: 'margin-top:.4rem' },
            statusChip(risk.risk === 'none' ? 'ok' : 'alarm', riskLabel))
        : h('p', { class: 'card-note', text: risk.note || 'No weather station is registered yet.' }),
    ],
  });
}

function agricultureTile(data) {
  const ag = data.agriculture || {};
  const rows = [['Greenhouse', ag.greenhouse || {}], ['Orchard / food forest', ag.orchard || {}]];
  return card({
    title: 'Greenhouse and orchard',
    status: ag.status || 'unknown',
    children: [
      h('dl', { class: 'kv' }, rows.flatMap(([label, block]) => [
        h('dt', { text: label }),
        h('dd', null, statusChip(block.status || 'not_deployed'),
          block.reporting ? ` · ${block.reporting} points reporting` : ''),
      ])),
      h('p', { class: 'card-note', text: ag.note || '' }),
    ],
  });
}

function commsTile(data) {
  const comms = data.communications || {};
  const platform = comms.platform || {};
  const values = Object.entries(comms.by_value || {});
  return card({
    title: 'Communications and server',
    status: comms.status || 'unknown',
    children: [
      values.length
        ? h('div', { class: 'pill-row', style: 'margin-bottom:.5rem' },
            values.map(([value, count]) => h('span', {
              class: `chip chip-${value === 'online' ? 'ok' : value === 'offline' ? 'alarm' : 'stale'}`,
            },
              h('span', { class: 'chip-glyph', 'aria-hidden': 'true', text: value === 'online' ? '●' : '○' }),
              `${value}: ${count}`)))
        : h('p', { class: 'card-note', text: comms.note || 'No communications assets are reporting.' }),
      h('dl', { class: 'kv' },
        h('dt', { text: 'This node' }),
        h('dd', { text: `${platform.node_role || '?'} · v${platform.version || '?'}` }),
        h('dt', { text: 'Broker' }),
        h('dd', null, platform.mqtt_enabled
          ? `${platform.mqtt_host || 'configured'}`
          : statusChip('not_deployed', 'MQTT disabled')),
        h('dt', { text: 'Historian' }),
        h('dd', { text: platform.historian_backend || '—' }),
        h('dt', { text: 'Physical control' }),
        h('dd', null, platform.physical_control_enabled
          ? statusChip('ok', 'Enabled')
          : statusChip('design_only', 'Disabled until commissioning'))),
    ],
  });
}

function securityTile(data) {
  const security = data.security || {};
  const safety = data.safety || {};
  return card({
    title: 'Security and safety',
    status: security.status === 'ok' && safety.status === 'ok' ? 'ok' : (security.status || 'unknown'),
    children: [
      h('dl', { class: 'kv' },
        h('dt', { text: 'Security' }),
        h('dd', null, statusChip(security.status || 'not_deployed'),
          security.asset_count ? ` · ${security.asset_count} assets` : ''),
        h('dt', { text: 'Safety' }),
        h('dd', null, statusChip(safety.status || 'not_deployed'),
          safety.asset_count ? ` · ${safety.asset_count} assets` : '')),
      Object.keys(security.by_value || {}).length
        ? h('div', { class: 'pill-row', style: 'margin-top:.5rem' },
            Object.entries(security.by_value).map(([value, count]) =>
              h('span', { class: 'tag', text: `${value}: ${count}` })))
        : h('p', { class: 'card-note', text: security.note || '' }),
    ],
  });
}

function subsystemGrid(data, ctx) {
  const rows = data.subsystems || [];
  const grid = h('div', { class: 'grid grid-auto' });
  rows.forEach((entry) => {
    const assets = entry.assets || {};
    const alarms = entry.alarms || {};
    const points = entry.points || {};
    grid.appendChild(card({
      title: entry.label,
      status: entry.health,
      children: [
        h('div', { class: 'stat-row', style: 'font-size:.8rem' },
          h('span', null, h('b', { text: String(assets.total || 0) }), ' assets'),
          assets.deployed ? h('span', null, h('b', { text: String(assets.deployed) }), ' installed') : null,
          alarms.active_total ? h('span', null, h('b', { text: String(alarms.active_total) }), ' alarms') : null,
          points.with_data ? h('span', null, h('b', { text: String(points.with_data) }), ' live points') : null),
        entry.open_fields && entry.open_fields.unresolved_count
          ? h('p', { class: 'card-note', text: `${entry.open_fields.unresolved_count} unresolved design fields` })
          : null,
      ],
    }));
  });
  return h('section', null,
    h('div', { class: 'page-head', style: 'margin:1.4rem 0 .6rem' },
      h('div', null,
        h('h2', { style: 'font-size:1.05rem', text: 'Subsystem health' }),
        h('p', { class: 'lede', text: 'Every SDD 25.4 domain, including the ones the property does not have yet.' })),
      h('button', { class: 'linkish', onclick: () => ctx.navigate('/assets'), text: 'Browse the registry →' })),
    grid);
}

/* ------------------------------------------------------- daily check ---- */

/*
   docs/operations.md § 0 asks the operator to read four things on this
   screen every day: active alarms (above), the energy state (above), the
   dead-letter count and the stale-point count. The last two come from the
   telemetry API, not from /overview, and the backup card beside them answers
   the weekly question — did the nightly archive run — without a shell.

   Every number here names its source, because the platform has two notions
   of "stale": a point whose quality code the ingest marker set to stale or
   bad (/telemetry/stale), and a point whose last value is older than its
   stale window right now (/overview). They usually agree; when they do not,
   the difference is the diagnosis.
*/

const DEAD_LETTER_PAGE = 25;
const STALE_LIMIT = 500;
const BACKUP_STALE_AFTER_H = 36;

const ops = {
  loaded: false,
  stats: null,
  stale: null,
  deadLetters: null,
  backups: null,
  offset: 0,
  filters: { topic: '', reason: '' },
  host: null,
  lastBackup: null,
  busy: false,
};

async function loadOps(ctx) {
  const [stats, stale, deadLetters, backups] = await Promise.all([
    api.telemetryStats(),
    api.stalePoints(STALE_LIMIT),
    api.deadLetters({ limit: DEAD_LETTER_PAGE, offset: ops.offset, ...ops.filters }),
    api.backups(),
  ]);
  Object.assign(ops, { stats, stale, deadLetters, backups, loaded: true });
  paintOps(ctx);
}

async function loadDeadLetters(ctx) {
  ops.deadLetters = await api.deadLetters({ limit: DEAD_LETTER_PAGE, offset: ops.offset, ...ops.filters });
  paintOps(ctx);
}

function opsSection(ctx) {
  const host = h('section', { id: 'daily-check', style: 'margin-top:1.4rem' });
  ops.host = host;
  paintOps(ctx);
  return host;
}

function paintOps(ctx) {
  const host = ops.host;
  if (!host) return;
  clear(host);
  host.appendChild(h('div', { class: 'page-head', style: 'margin:0 0 .6rem' },
    h('div', null,
      h('h2', { style: 'font-size:1.05rem', text: 'Ingest, stale points and backups' }),
      h('p', { class: 'lede', text: 'The rest of the daily check: messages the platform rejected, points it admits it cannot see, and whether the last archive exists. Each count says where it came from.' }))));
  if (!ops.loaded) {
    host.appendChild(h('p', { class: 'loading', text: 'Loading ingest statistics…' }));
    return;
  }
  host.appendChild(h('div', { class: 'grid grid-wide' },
    deadLetterCard(), staleCard(ctx), backupCard(ctx)));
  host.appendChild(h('div', { style: 'margin-top:var(--gap)' }, deadLetterTable(ctx)));
}

/** The reason string starts with the ingest writer's own code, e.g.
 *  `unresolved_topic: …`; that prefix is the category, nothing is invented. */
function deadLetterCategory(reason) {
  const match = String(reason || '').match(/^[A-Za-z0-9_]+/);
  return match ? match[0] : 'other';
}

function deadLetterCard() {
  const result = ops.stats;
  if (!result.ok) {
    return card({ title: 'Dead letters', status: 'unknown', children: resultProblem(result, 'ingest statistics') });
  }
  const database = result.data.database || {};
  // `ingest` is the IngestService.stats() block, or null on a node without
  // ingest; its counters sit under `counters` with the writer's own names.
  const ingest = result.data.ingest;
  const counters = ingest && ingest.counters ? ingest.counters : null;
  const total = database.dead_letters;
  const sinceStart = counters && typeof counters.dead_lettered === 'number' ? counters.dead_lettered : null;
  const status = sinceStart ? 'degraded' : (total ? 'stale' : 'ok');
  return card({
    title: 'Dead letters',
    status,
    statusText: sinceStart ? 'Growing' : (total ? 'Recorded' : 'None'),
    children: [
      h('div', { class: 'readout' },
        h('span', { class: 'value num', text: String(total ?? '—') }),
        h('span', { class: 'unit', text: 'in the database' })),
      h('p', { class: 'card-note', text: sinceStart
        ? `${sinceStart} since the ingest service started — a count that grows every day is a binding that was never commissioned properly, not noise.`
        : (total
          ? 'None since the ingest service started. Older rows are listed below; retention prunes them with the historian.'
          : 'No message has been rejected. During commissioning that is the result to protect.') }),
      kv([
        ['Ingest service', result.data.ingest_running
          ? statusChip('ok', 'running')
          : statusChip(ingest ? 'degraded' : 'not_deployed', ingest ? 'stopped' : 'not on this node')],
        ['Received / written', counters ? `${counters.messages_received ?? '—'} / ${counters.messages_written ?? '—'}` : '—'],
        ['Out of order · stale-marked', counters ? `${counters.out_of_order ?? '—'} · ${counters.stale_marked ?? '—'}` : '—'],
        ['Last message', ingest && ingest.last_message_at ? fmtAge(ingest.last_message_at) : (ingest ? 'none since start' : '—')],
        ['Last error', ingest && ingest.last_error ? ingest.last_error : 'none'],
        ['Source', 'GET /telemetry/stats'],
      ]),
    ],
  });
}

function staleCard(ctx) {
  const result = ops.stale;
  if (!result.ok) {
    return card({ title: 'Stale points', status: 'unknown', children: resultProblem(result, 'the stale-point list') });
  }
  const rows = Array.isArray(result.data) ? result.data : [];
  const capped = rows.length >= STALE_LIMIT;
  const overview = ctx.latestOverview().data;
  const subsystems = overview ? (overview.subsystems || []) : [];
  const byAge = subsystems.reduce((sum, entry) => sum + ((entry.points || {}).stale || 0), 0);
  const withData = subsystems.reduce((sum, entry) => sum + ((entry.points || {}).with_data || 0), 0);
  const byQuality = rows.reduce((acc, row) => {
    acc[row.quality] = (acc[row.quality] || 0) + 1;
    return acc;
  }, {});
  return card({
    title: 'Stale points',
    status: rows.length ? 'stale' : 'ok',
    statusText: rows.length ? `${capped ? `${STALE_LIMIT}+` : rows.length} not trusted` : 'All trusted',
    children: [
      h('div', { class: 'readout' },
        h('span', { class: 'value num', text: capped ? `${STALE_LIMIT}+` : String(rows.length) }),
        h('span', { class: 'unit', text: 'quality stale or bad' })),
      h('p', { class: 'card-note', text: rows.length
        ? 'Every point here is unknown, not normal. Treat the last value as history.'
        : 'No current value carries a stale or bad quality code.' }),
      kv([
        ['By quality', Object.keys(byQuality).length
          ? Object.entries(byQuality).map(([quality, count]) => `${quality}: ${count}`).join(' · ')
          : '—'],
        ['Older than stale window', overview
          ? `${byAge} of ${withData} reporting points (GET /overview, evaluated now)`
          : statusChip('no_data', 'overview not loaded')],
        ['Source', `GET /telemetry/stale (limit ${STALE_LIMIT})`],
      ]),
      rows.length
        ? h('details', { class: 'raw' },
            h('summary', { text: `Stale points (${capped ? `first ${STALE_LIMIT}` : rows.length})` }),
            h('div', { class: 'table-wrap', style: 'margin-top:.4rem' },
              h('table', null,
                h('thead', null, h('tr', null,
                  h('th', { text: 'Point' }), h('th', { text: 'Quality' }), h('th', { text: 'Last value' }))),
                h('tbody', null, rows.slice(0, 100).map((row) => h('tr', null,
                  h('td', null, h('a', { class: 'mono', href: `#/assets/${encodeURIComponent(row.asset_id)}`,
                                         text: row.point_id })),
                  h('td', null, statusChip(row.quality === 'stale' ? 'stale' : 'alarm', row.quality)),
                  h('td', { text: row.ts ? `${fmtAge(row.ts)} (${fmtDateTime(row.ts)})` : 'never' })))))))
        : null,
    ],
  });
}

function fmtMiB(bytes) {
  if (typeof bytes !== 'number') return '—';
  return `${fmtNumber(bytes / (1024 * 1024), 2)} MiB`;
}

function backupCard(ctx) {
  const result = ops.backups;
  if (!result.ok) {
    return card({ title: 'Backups', status: 'unknown', children: resultProblem(result, 'the backup listing') });
  }
  const listing = result.data;
  const archives = listing.archives || [];
  const newest = archives.find((entry) => entry.manifest) || null;
  const newestAt = newest ? newest.manifest.created_at : null;
  const ageH = newestAt ? (Date.now() - new Date(newestAt).getTime()) / 3600000 : null;
  const status = !newest ? 'no_data' : (ageH !== null && ageH > BACKUP_STALE_AFTER_H ? 'stale' : 'ok');
  const statusText = !newest ? 'No archive' : (status === 'stale' ? 'Last archive is old' : 'Recent archive');

  const children = [];
  if (newest) {
    const m = newest.manifest;
    children.push(kv([
      ['Last archive', h('span', { class: 'mono', text: newest.name })],
      ['Taken', `${fmtDateTime(m.created_at)} · ${fmtAge(m.created_at)}`],
      ['Size', fmtMiB(newest.size_bytes)],
      ['Contents', `${m.tables} tables · ${m.rows} rows${m.includes_history ? ' · includes history' : ''}`],
      ['Written by', `${m.node_role || '?'} node, platform v${m.platform_version || '?'}`],
      ['Restorable', m.restorable ? statusChip('ok', 'yes') : statusChip('alarm', 'format not recognised')],
    ]));
    if (status === 'stale') {
      children.push(h('p', { class: 'card-note', style: 'color:var(--status-stale)',
        text: `More than ${BACKUP_STALE_AFTER_H} hours old. If the nightly job is scheduled, it did not run.` }));
    }
  } else {
    children.push(h('p', { class: 'card-note' },
      statusChip('no_data', 'No archive'), ' ',
      listing.exists
        ? `${listing.directory} holds no backup archive.`
        : `${listing.directory} does not exist yet — nothing has been backed up on this node.`));
  }
  children.push(h('p', { class: 'card-note', text: `${archives.length} archive${archives.length === 1 ? '' : 's'} in ${listing.directory}. This copy sits in the same failure domain as the database: keep one on the secondary node and one offline.` }));

  if (archives.length) {
    children.push(h('details', { class: 'raw' },
      h('summary', { text: `All archives (${archives.length})` }),
      h('div', { class: 'table-wrap', style: 'margin-top:.4rem' },
        h('table', null,
          h('thead', null, h('tr', null,
            h('th', { text: 'Archive' }), h('th', { text: 'Taken' }), h('th', { class: 'num', text: 'Size' }),
            h('th', { text: 'Contents' }))),
          h('tbody', null, archives.map((entry) => h('tr', null,
            h('td', null, h('span', { class: 'mono', text: entry.name })),
            h('td', { text: entry.manifest ? fmtDateTime(entry.manifest.created_at) : `modified ${fmtDateTime(entry.modified_at)}` }),
            h('td', { class: 'num', text: fmtMiB(entry.size_bytes) }),
            h('td', null, entry.manifest
              ? `${entry.manifest.tables} tables · ${entry.manifest.rows} rows`
              : statusChip('alarm', entry.error || 'unreadable')))))))));
  }

  children.push(backupAction(ctx));
  return card({ title: 'Backups', status, statusText, children });
}

function backupAction(ctx) {
  const history = h('input', { type: 'checkbox', id: 'backup-history' });
  const button = h('button', { class: 'btn btn-primary btn-sm', text: 'Take a backup now…', disabled: ops.busy });
  const outcome = h('div', { style: 'margin-top:.5rem' });

  if (ops.lastBackup) showBackupOutcome(outcome, ops.lastBackup);

  button.addEventListener('click', async () => {
    if (!requireRole('maintainer')) return;
    const includeHistory = history.checked;
    const yes = await confirmAction({
      title: 'Write a backup archive on this node',
      note: `This runs the same code as "chaos backup" and writes one .tar.gz into the backup directory`
        + `${includeHistory ? ', including telemetry samples and dead letters (large)' : ' (registry, alarms, commands, energy, maintenance and current state; no history)'}.`
        + ' The archive holds no credentials. The write is audited against your name.',
      confirmLabel: 'Write archive',
    });
    if (!yes) return;
    ops.busy = true;
    button.disabled = true;
    const response = await api.createBackup(includeHistory);
    ops.busy = false;
    ops.lastBackup = response;
    if (response.ok) {
      toast('success', 'Backup written', `${response.data.name} · ${fmtMiB(response.data.size_bytes)}`);
      ops.backups = await api.backups();
      paintOps(ctx);
    } else if (response.missing) {
      toast('warn', 'Backup endpoint not available', 'This node does not expose POST /backup. Nothing was written.');
      paintOps(ctx);
    } else {
      toast('error', 'Backup failed', response.error || `HTTP ${response.status}`);
      paintOps(ctx);
    }
  });

  return h('div', { style: 'margin-top:.7rem' },
    h('div', { class: 'filters', style: 'margin-bottom:.3rem' },
      h('label', { style: 'flex-direction:row;align-items:center;gap:.4rem' },
        history, h('span', { text: 'Include history (telemetry samples and dead letters — large)' })),
      button),
    outcome);
}

function showBackupOutcome(host, response) {
  clear(host);
  if (response.ok) {
    const d = response.data;
    host.appendChild(h('div', null,
      h('p', { style: 'margin:0 0 .3rem' }, statusChip('ok', 'Archive written'), ` by ${d.written_by || 'you'}`),
      kv([
        ['Path', h('span', { class: 'mono', text: d.path })],
        ['Size', fmtMiB(d.size_bytes)],
        ['Tables · rows', `${d.tables} · ${d.rows}`],
        ['Taken', fmtDateTime(d.created_at)],
        ['SHA-256', h('span', { class: 'mono', text: d.sha256 })],
        ['History', d.includes_history ? 'included' : 'not included'],
        ['Absent tables', (d.tables_absent || []).length ? d.tables_absent.join(', ') : 'none'],
      ]),
      h('p', { class: 'card-note', text: d.note || '' })));
  } else {
    host.appendChild(h('p', { style: 'margin:0' },
      statusChip('alarm', response.forbidden ? 'Refused' : 'Failed'), ' ',
      h('span', { text: response.error || `HTTP ${response.status}` })));
  }
}

function deadLetterTable(ctx) {
  const result = ops.deadLetters;
  const stats = ops.stats;
  const total = stats && stats.ok ? (stats.data.database || {}).dead_letters : null;
  const filtered = Boolean(ops.filters.topic || ops.filters.reason);

  const topic = h('input', { type: 'search', class: 'input', placeholder: 'topic or point', value: ops.filters.topic,
    oninput: (event) => { ops.filters.topic = event.target.value; } });
  const reason = h('input', { type: 'search', class: 'input', placeholder: 'reason', value: ops.filters.reason,
    oninput: (event) => { ops.filters.reason = event.target.value; } });
  const filters = h('form', { class: 'filters', style: 'margin-bottom:.6rem',
    onsubmit: (event) => { event.preventDefault(); ops.offset = 0; loadDeadLetters(ctx); } },
    h('label', null, h('span', { text: 'Topic' }), topic),
    h('label', null, h('span', { text: 'Reason' }), reason),
    h('button', { class: 'btn btn-sm', type: 'submit', text: 'Apply' }),
    filtered ? h('button', { class: 'btn btn-sm', type: 'button', text: 'Clear',
      onclick: () => { ops.filters = { topic: '', reason: '' }; ops.offset = 0; loadDeadLetters(ctx); } }) : null);

  const children = [filters];
  if (!result.ok) {
    children.push(resultProblem(result, 'the dead-letter list'));
    return card({ title: 'Dead-letter list', status: 'unknown', children });
  }
  const rows = Array.isArray(result.data) ? result.data : [];
  if (!rows.length) {
    children.push(h('div', { class: 'empty' },
      h('h3', null, statusChip('ok', filtered || ops.offset ? 'No matching dead letters' : 'No dead letters')),
      h('p', { text: filtered
        ? 'Nothing matches these filters.'
        : 'No ingest message has been rejected, or retention has pruned the ones that were. A silent point with no dead letter is a device that never published, not a message the platform dropped.' })));
  } else {
    children.push(h('div', { class: 'table-wrap' },
      h('table', null,
        h('caption', { text: `${rows.length} shown, newest first${total !== null && !filtered ? ` of ${total} recorded` : ''}` }),
        h('thead', null, h('tr', null,
          h('th', { text: 'When' }), h('th', { text: 'Category' }), h('th', { text: 'Topic / point' }),
          h('th', { text: 'Reason' }))),
        h('tbody', null, rows.map((row) => h('tr', null,
          h('td', { text: row.created_at ? `${fmtDateTime(row.created_at)} · ${fmtAge(row.created_at)}` : '—' }),
          h('td', null, h('span', { class: 'tag', text: deadLetterCategory(row.reason) })),
          h('td', null, h('span', { class: 'mono', text: row.topic })),
          h('td', null,
            h('div', { text: row.reason }),
            row.payload
              ? h('details', { class: 'raw' }, h('summary', { text: 'Payload' }), h('pre', { text: row.payload }))
              : null)))))));
  }

  const pageEnd = ops.offset + rows.length;
  const more = filtered || total === null ? rows.length === DEAD_LETTER_PAGE : pageEnd < total;
  if (ops.offset > 0 || more) {
    children.push(h('div', { class: 'pill-row', style: 'margin-top:.7rem' },
      h('button', { class: 'btn btn-sm', disabled: ops.offset === 0,
        onclick: () => { ops.offset = Math.max(0, ops.offset - DEAD_LETTER_PAGE); loadDeadLetters(ctx); },
        text: '← Previous' }),
      h('span', { class: 'card-note', style: 'margin:0',
        text: rows.length
          ? `${ops.offset + 1}–${pageEnd}${total !== null && !filtered ? ` of ${total}` : ''}`
          : `offset ${ops.offset}` }),
      h('button', { class: 'btn btn-sm', disabled: !more,
        onclick: () => { ops.offset += DEAD_LETTER_PAGE; loadDeadLetters(ctx); },
        text: 'Next →' })));
  }
  children.push(h('p', { class: 'card-note', text: 'Source: GET /telemetry/dead-letters. The category is the ingest writer’s own reason code; the troubleshooting guide reads this list when a message arrived but failed to resolve.' }));
  return card({ title: 'Dead-letter list', status: rows.length ? 'degraded' : 'ok', children });
}

function render(root, data, ctx) {
  clear(root);
  root.appendChild(h('div', { class: 'page-head' },
    h('div', null,
      h('h2', { text: 'Property overview' }),
      h('p', { class: 'lede', text: 'FR-001: subsystem health, active alarms, energy reserve, water reserve, communications and operating mode — from one request, so this screen works with no internet.' })),
    h('p', { class: 'card-note', text: `Generated ${fmtDateTime(data.generated_at)}` })));

  const sources = data.data_sources || {};
  if (sources.degraded) {
    root.appendChild(h('div', { class: 'error-box', style: 'margin-bottom:var(--gap)' },
      h('h3', { text: 'Incomplete data — some tables could not be read' }),
      h('p', { text: sources.note || 'Parts of the database are unavailable, so this screen is incomplete. Treat missing subsystems as unknown, not as healthy.' }),
      h('ul', { class: 'provenance' }, (sources.unavailable || []).map((problem) =>
        h('li', { text: `${problem.source}: ${problem.error} — ${problem.detail}` })))));
  }

  const grid = h('div', { class: 'grid grid-wide' });
  grid.appendChild(siteHero(data, ctx));
  grid.appendChild(alarmTile(data, ctx));
  energyTiles(data).forEach((tile) => grid.appendChild(tile));
  grid.appendChild(waterTile(data));
  grid.appendChild(weatherTile(data));
  grid.appendChild(agricultureTile(data));
  grid.appendChild(commsTile(data));
  grid.appendChild(securityTile(data));
  root.appendChild(grid);

  root.appendChild(subsystemGrid(data, ctx));
  root.appendChild(opsSection(ctx));

  root.appendChild(h('details', { class: 'raw' },
    h('summary', { text: 'What the status words mean' }),
    h('dl', { class: 'kv', style: 'margin-top:.5rem' },
      Object.entries(data.status_vocabulary || {}).flatMap(([key, text]) => [
        h('dt', null, statusChip(key)),
        h('dd', { text }),
      ]))));
}

export default {
  title: 'Home',
  async mount(root, ctx) {
    const unsubscribe = ctx.subscribeOverview((data, result) => {
      if (data) render(root, data, ctx);
      else if (result && !result.ok) {
        clear(root).appendChild(resultProblem(result, 'the property overview'));
      }
    });

    const current = ctx.latestOverview();
    if (!current.result) {
      root.appendChild(h('p', { class: 'loading', text: 'Loading the property overview…' }));
      await ctx.refreshOverview();
    } else if (!current.data && current.result && !current.result.ok) {
      clear(root).appendChild(resultProblem(current.result, 'the property overview'));
    }

    await loadOps(ctx);

    return {
      // The shell already refreshes /overview; the subscription repaints the
      // overview tiles. The operations block has its own requests.
      async refresh() { await loadOps(ctx); },
      destroy() { unsubscribe(); ops.host = null; },
    };
  },
};
