/* ---------------------------------------------------------------------------
   Alarm screen (SDD 14).

   The SDD is explicit that a flood is a design failure: "A power-container
   outage should not create hundreds of separate notifications without a parent
   incident." So this screen groups by incident first and only then lists the
   member alarms. Alarms with no incident are collected under one honest
   "not correlated" group rather than being scattered.

   Severity is never carried by colour alone — every badge is glyph + word +
   colour, and the lifecycle state is spelled out.

   Every action collects a mandatory reason before it is sent, because the API
   audits the actor and the reason (SDD 15.2) and the operator should learn that
   from the dialog, not from a rejection.
--------------------------------------------------------------------------- */

import {
  askReason, card, clear, fmtAge, fmtDateTime, h, requireRole, resultProblem,
  severityChip, statusChip, toast,
} from '../app.js';
import { api } from '../api.js';

/* ------------------------------------------------- notification delivery --- */

/*
   SDD FR-007: every notification attempt on every channel is a row in the
   delivery log, so "why did nobody get called" is answerable afterwards. The
   status vocabulary is the notifier's own: `sent` only when the relay or push
   server accepted the message, `failed` with the error when it did not, and
   `not_configured` — naming what is missing — for a channel that has never
   been given its settings. Nothing here promotes a row to "delivered".
*/

const DELIVERY_STATUS = {
  sent:           { status: 'ok',          label: 'sent' },
  failed:         { status: 'alarm',       label: 'failed' },
  not_configured: { status: 'design_only', label: 'not configured' },
};
const LOG_PAGE = 50;
const LOG_FILTERS = { channel: '', status: '' };

function deliveryChip(status) {
  const meta = DELIVERY_STATUS[status] || { status: 'unknown', label: status || 'unknown' };
  return statusChip(meta.status, meta.label);
}

function deliveryDetail(row) {
  const detail = row.detail && typeof row.detail === 'object' ? row.detail : {};
  return detail.channel_detail || detail.error || detail.raw || '';
}

function deliveryStage(row) {
  const detail = row.detail && typeof row.detail === 'object' ? row.detail : {};
  return detail.stage === undefined || detail.stage === null ? '—' : `stage ${detail.stage}`;
}

function deliveryTable(rows, { withSubject = true } = {}) {
  return h('div', { class: 'table-wrap' },
    h('table', null,
      h('thead', null, h('tr', null,
        h('th', { text: 'When' }), h('th', { text: 'Channel' }), h('th', { text: 'Stage' }),
        h('th', { text: 'Status' }), h('th', { text: 'Recipient' }),
        withSubject ? h('th', { text: 'Subject' }) : null, h('th', { text: 'Detail' }))),
      h('tbody', null, rows.map((row) => h('tr', null,
        h('td', { text: row.sent_at ? `${fmtDateTime(row.sent_at)} · ${fmtAge(row.sent_at)}` : '—' }),
        h('td', null, h('span', { class: 'tag', text: row.channel })),
        h('td', { text: deliveryStage(row) }),
        h('td', null, deliveryChip(row.status)),
        h('td', { text: row.recipient || '—' }),
        withSubject ? h('td', { text: row.subject || '—' }) : null,
        h('td', { text: deliveryDetail(row) }))))));
}

/** Per-incident deliveries, fetched from GET /incidents/{id} when opened. */
function incidentDeliveries(incidentId) {
  const body = h('div', { style: 'margin-top:.4rem' });
  let loaded = false;
  const details = h('details', { class: 'raw', style: 'padding:.4rem .9rem .6rem' },
    h('summary', { text: 'Notification deliveries for this incident' }),
    body);
  details.addEventListener('toggle', async () => {
    if (!details.open || loaded) return;
    loaded = true;
    body.replaceChildren(h('p', { class: 'loading', style: 'padding:.3rem 0', text: 'Loading deliveries…' }));
    const result = await api.incident(incidentId);
    clear(body);
    if (!result.ok) {
      body.appendChild(resultProblem(result, 'the incident'));
      loaded = false;
      return;
    }
    const rows = result.data.notifications || [];
    if (!rows.length) {
      body.appendChild(h('p', { class: 'card-note', style: 'margin:0', text: 'No delivery has been attempted for this incident yet. The notifier runs with the alarm engine; a first-stage attempt follows activation within one engine cycle.' }));
      return;
    }
    body.appendChild(deliveryTable(rows, { withSubject: false }));
  });
  return details;
}

async function deliveryLogCard() {
  const result = await api.notifications({ limit: LOG_PAGE, channel: LOG_FILTERS.channel, status: LOG_FILTERS.status });
  const select = (name, label, options) => h('label', null,
    h('span', { text: label }),
    h('select', { class: 'select-sm', onchange: (event) => { LOG_FILTERS[name] = event.target.value; } },
      h('option', { value: '', text: `All ${label.toLowerCase()}s` }),
      options.map((value) => h('option', { value, text: value, selected: LOG_FILTERS[name] === value }))));
  const host = h('div');
  const form = h('form', { class: 'filters', style: 'margin-bottom:.6rem',
    onsubmit: async (event) => {
      event.preventDefault();
      host.replaceChildren(await deliveryLogCard());
    } },
    select('channel', 'Channel', ['log', 'email', 'push', 'voice']),
    select('status', 'Status', Object.keys(DELIVERY_STATUS)),
    h('button', { class: 'btn btn-sm', type: 'submit', text: 'Apply' }));

  const children = [form];
  if (!result.ok) {
    children.push(resultProblem(result, 'the notification delivery log'));
  } else {
    const rows = result.data.notifications || [];
    const counts = rows.reduce((acc, row) => { acc[row.status] = (acc[row.status] || 0) + 1; return acc; }, {});
    children.push(h('div', { class: 'stat-row', style: 'margin-bottom:.6rem' },
      h('span', null, h('b', { text: String(result.data.count ?? rows.length) }), ` shown of the latest ${LOG_PAGE}`),
      Object.entries(counts).map(([status, count]) => h('span', null, h('b', { text: String(count) }), ` ${(DELIVERY_STATUS[status] || { label: status }).label}`))));
    if (!rows.length) {
      children.push(h('div', { class: 'empty' },
        h('h3', null, statusChip('no_data', 'No deliveries recorded')),
        h('p', { text: LOG_FILTERS.channel || LOG_FILTERS.status
          ? 'Nothing matches these filters.'
          : 'No notification has been attempted on this node. An alarm that activated without a row here was never notified — see Troubleshooting § It fired and nobody was told.' })));
    } else {
      children.push(deliveryTable(rows));
    }
    children.push(h('p', { class: 'card-note', text: 'Source: GET /notifications, newest first. A `not configured` row names the setting the channel is missing; a `failed` row carries the relay or push server’s error and is retried on a later engine cycle, up to the retry limit.' }));
  }
  host.appendChild(card({ title: 'Notification delivery log', children }));
  return host;
}

const SEVERITY_RANK = { emergency: 0, critical: 1, major: 2, warning: 3, info: 4 };

/** Lifecycle transitions an operator can drive (SDD 14.2). */
const ACTIONS = [
  { key: 'acknowledge', label: 'Acknowledge', from: ['detected', 'active'], role: 'operator',
    note: 'Acknowledging records that a named operator has seen this alarm. It does not clear the condition.' },
  { key: 'mitigate', label: 'Mitigate', from: ['detected', 'active', 'acknowledged'], role: 'operator',
    note: 'Mitigated means the immediate risk has been contained but the underlying condition may persist.' },
  { key: 'clear', label: 'Clear', from: ['detected', 'active', 'acknowledged', 'mitigated'], role: 'operator',
    danger: true,
    note: 'Clearing asserts the condition is gone. If the trigger is still true the alarm engine will raise it again.' },
];

function normalise(raw) {
  return {
    id: raw.id || raw.alarm_id,
    alarm_key: raw.alarm_key,
    severity: raw.severity || 'info',
    state: raw.state || 'active',
    asset_id: raw.asset_id || null,
    asset_name: raw.asset_name || null,
    domain: raw.domain || null,
    point_id: raw.point_id || null,
    message: raw.message || raw.name || raw.alarm_key,
    detected_at: raw.detected_at || raw.occurred_at || null,
    acknowledged_at: raw.acknowledged_at || null,
    acknowledged_by: raw.acknowledged_by || null,
    suppressed: Boolean(raw.suppressed),
    incident_id: raw.incident_id || null,
    incident_title: raw.incident_title || null,
  };
}

function groupByIncident(alarms) {
  const groups = new Map();
  alarms.forEach((alarm) => {
    const key = alarm.incident_id || '__uncorrelated__';
    if (!groups.has(key)) {
      groups.set(key, {
        id: key,
        title: alarm.incident_id
          ? (alarm.incident_title || `Incident ${alarm.incident_id.slice(0, 8)}`)
          : 'Not correlated to an incident',
        correlated: Boolean(alarm.incident_id),
        alarms: [],
      });
    }
    groups.get(key).alarms.push(alarm);
  });
  const list = Array.from(groups.values());
  list.forEach((group) => {
    group.alarms.sort((a, b) => (SEVERITY_RANK[a.severity] ?? 9) - (SEVERITY_RANK[b.severity] ?? 9));
    group.severity = group.alarms[0] ? group.alarms[0].severity : 'info';
  });
  list.sort((a, b) => (SEVERITY_RANK[a.severity] ?? 9) - (SEVERITY_RANK[b.severity] ?? 9));
  return list;
}

function alarmRow(alarm, onAction) {
  const actions = ACTIONS.filter((action) => action.from.includes(alarm.state));
  return h('div', { class: 'alarm-row alarm-sev-bar', style: `color: var(--sev-${alarm.severity})` },
    h('div', null,
      severityChip(alarm.severity, null, alarm.severity === 'critical' || alarm.severity === 'emergency'),
      h('div', { style: 'margin-top:.3rem;color:var(--text-dim)' },
        h('span', { class: 'tag', text: alarm.state }))),
    h('div', { class: 'alarm-main', style: 'color:var(--text)' },
      h('div', { class: 'alarm-msg', text: alarm.message }),
      h('div', { class: 'alarm-key', text: alarm.alarm_key }),
      h('div', { class: 'alarm-key' },
        alarm.asset_id
          ? h('a', { href: `#/control/${encodeURIComponent(alarm.asset_id)}`,
                     text: alarm.asset_name || alarm.asset_id })
          : 'site-wide',
        ` · detected ${fmtAge(alarm.detected_at)}`,
        alarm.acknowledged_by ? ` · acknowledged by ${alarm.acknowledged_by} ${fmtAge(alarm.acknowledged_at)}` : '',
        alarm.suppressed ? ' · SUPPRESSED' : ''),
      alarm.point_id ? h('div', { class: 'alarm-key mono', text: alarm.point_id }) : null),
    h('div', { class: 'alarm-actions', style: 'color:var(--text)' },
      actions.map((action) => h('button', {
        class: `btn btn-sm${action.danger ? ' btn-danger' : ''}`,
        onclick: () => onAction(alarm, action),
        text: action.label,
      })),
      alarm.asset_id
        ? h('a', { class: 'btn btn-sm', href: `#/control/${encodeURIComponent(alarm.asset_id)}`, text: 'Control' })
        : null));
}

function incidentBlock(group, onAction) {
  const counts = group.alarms.reduce((acc, alarm) => {
    acc[alarm.severity] = (acc[alarm.severity] || 0) + 1;
    return acc;
  }, {});
  return h('section', { class: 'incident', dataset: { severity: group.severity } },
    h('header', null,
      severityChip(group.severity, null, true),
      h('h3', { text: group.title }),
      h('div', { class: 'pill-row', style: 'margin-left:auto' },
        Object.entries(counts).map(([severity, count]) =>
          h('span', { class: `chip chip-${severity}` },
            h('span', { class: 'chip-glyph', 'aria-hidden': 'true', text: '·' }), `${count} ${severity}`))),
      group.correlated
        ? null
        : h('span', { class: 'card-note', style: 'flex-basis:100%;margin:0',
                      text: 'These alarms have no parent incident. If several arrive together, the correlation rules may need work (SDD 14.2).' })),
    group.alarms.map((alarm) => alarmRow(alarm, onAction)),
    group.correlated ? incidentDeliveries(group.id) : null);
}

async function loadAlarms() {
  // The dedicated alarms router is being built in parallel; the overview
  // aggregate always exists, so it is the dependable fallback.
  const direct = await api.activeAlarms();
  if (direct.ok) {
    const items = Array.isArray(direct.data) ? direct.data : (direct.data.items || direct.data.alarms || []);
    const alarms = items.map(normalise);

    // The alarms endpoint returns incident ids; titles live on /incidents.
    const wanted = new Set(alarms.map((a) => a.incident_id).filter(Boolean));
    if (wanted.size) {
      const incidents = await api.incidents('open');
      if (incidents.ok) {
        const rows = Array.isArray(incidents.data) ? incidents.data : (incidents.data.items || incidents.data.incidents || []);
        const titles = new Map(rows.map((row) => [row.id || row.incident_id, row.title]));
        alarms.forEach((alarm) => {
          if (alarm.incident_id && titles.has(alarm.incident_id)) {
            alarm.incident_title = titles.get(alarm.incident_id);
          }
        });
      }
    }
    return { source: 'alarms API', alarms, result: direct, truncated: false };
  }
  const overview = await api.overview(100);
  if (!overview.ok) return { source: null, alarms: [], result: overview, truncated: false };
  const block = overview.data.alarms || {};
  return {
    source: 'overview aggregate',
    alarms: (block.top || []).map(normalise),
    summary: block,
    result: overview,
    truncated: (block.active_total || 0) > (block.top || []).length,
  };
}

export default {
  title: 'Alarms',
  async mount(root, ctx) {
    let state = { loading: true };

    async function onAction(alarm, action) {
      if (!requireRole(action.role)) return;
      const reason = await askReason({
        title: `${action.label} — ${alarm.message}`,
        note: `${action.note} Alarm ${alarm.alarm_key} on ${alarm.asset_name || alarm.asset_id || 'the site'}.`,
        confirmLabel: action.label,
        danger: Boolean(action.danger),
      });
      if (!reason) return;

      const response = await api.alarmAction(alarm.id, action.key, reason);
      if (response.ok) {
        toast('success', `${action.label}d`, `${alarm.alarm_key} — recorded with your reason.`);
        await draw();
        ctx.refreshOverview();
      } else if (response.missing) {
        toast('warn', 'Alarm actions not available yet',
          'This node does not expose an alarm lifecycle endpoint. Nothing was changed.');
      } else if (response.forbidden) {
        toast('error', 'Refused', `${response.error} — check your operator role.`);
      } else {
        toast('error', `${action.label} failed`, response.error || `HTTP ${response.status}`);
      }
    }

    async function draw() {
      const loaded = await loadAlarms();
      state = loaded;
      clear(root);

      root.appendChild(h('div', { class: 'page-head' },
        h('div', null,
          h('h2', { text: 'Alarms' }),
          h('p', { class: 'lede', text: 'Active alarms grouped by incident so one root cause reads as one event (SDD 14.2). Severity is shown as glyph, word and colour together.' })),
        h('div', { class: 'pill-row' },
          h('button', { class: 'btn btn-sm', onclick: draw, text: 'Reload' }))));

      if (!loaded.result.ok) {
        root.appendChild(resultProblem(loaded.result, 'active alarms'));
        return;
      }

      // Counts always come from the overview aggregate so the header is the
      // same number the home screen and the nav badge show.
      const overview = ctx.latestOverview();
      const summary = loaded.summary || (overview.data ? overview.data.alarms : null);
      if (summary) {
        const s = summary;
        root.appendChild(h('div', { class: 'stat-row', style: 'margin-bottom:var(--gap)' },
          h('span', null, h('b', { text: String(s.active_total || 0) }), ' active'),
          h('span', null, h('b', { text: String((s.emergency_active || 0) + (s.critical_active || 0)) }), ' critical or emergency'),
          h('span', null, h('b', { text: String(s.major_active || 0) }), ' major'),
          h('span', null, h('b', { text: String(s.unacknowledged || 0) }), ' unacknowledged'),
          h('span', null, h('b', { text: String(s.open_incidents || 0) }), ' open incidents')));
      }

      if (!loaded.alarms.length) {
        root.appendChild(h('div', { class: 'empty' },
          h('h3', null, statusChip('ok', 'No active alarms')),
          h('p', { text: 'Nothing is detected, active, acknowledged or mitigated. Cleared and reviewed alarms are history, not active state.' })));
      } else {
        groupByIncident(loaded.alarms).forEach((group) => {
          root.appendChild(incidentBlock(group, onAction));
        });
      }

      if (loaded.truncated) {
        root.appendChild(h('p', { class: 'card-note',
          text: 'Only the highest-severity alarms are listed: this node has no dedicated alarms endpoint yet, so the overview aggregate is capping the list.' }));
      }

      root.appendChild(h('p', { class: 'card-note', style: 'margin-top:1rem',
        text: `Source: ${loaded.source}. Lifecycle: detected → active → acknowledged → mitigated → cleared → reviewed.` }));

      root.appendChild(h('div', { style: 'margin-top:var(--gap)' }, await deliveryLogCard()));
      root.appendChild(h('p', { class: 'card-note', text: `Loaded ${fmtDateTime(new Date().toISOString())}` }));
    }

    root.appendChild(h('p', { class: 'loading', text: 'Loading alarms…' }));
    await draw();

    return {
      async refresh() { if (!state.loading) await draw(); },
      destroy() {},
    };
  },
};
