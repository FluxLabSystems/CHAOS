/* ---------------------------------------------------------------------------
   SDD 17.4 control presentation.

   "Every control should show: actual state, requested state, local/remote
   authority, interlocks preventing operation, last command and issuer, manual
   override status, impact on energy and resource budgets."

   All seven are laid out as primary content on this screen. None of them is a
   tooltip, a hover, or hidden behind a disclosure triangle, because each one is
   a reason an operator might decide *not* to press the button — and a control
   screen that hides its refusals teaches people to distrust it.

   The command console below deliberately shows its preflight: exactly what will
   be written to the audit log, and every reason the platform would refuse.
--------------------------------------------------------------------------- */

import {
  askReason, card, clear, confirmAction, fmtAge, fmtDateTime, fmtNumber, h, kv, requireRole,
  resultProblem, statusChip, toast,
} from '../app.js';
import { api } from '../api.js';

/** SDD 11 mode set, in the order the API lists it (chaos.commands.modes.MODES). */
const MODES = ['off', 'manual', 'automatic', 'scheduled', 'maintenance', 'degraded', 'emergency'];

const MODE_NOTES = {
  maintenance: 'Inhibits automatic starts and modifies alarms with the lockout visible — use this for planned work instead of suppressing alarms.',
  emergency: 'Latches. Only protective commands run, and leaving it needs a named operator asserting the condition is gone.',
  off: 'The most restrictive non-latching mode: nothing in this scope is commanded.',
  automatic: 'The default: the platform may request, local controllers still decide.',
};

const AUTHORITY_COPY = {
  local: 'Local — the equipment or its own controller has authority right now.',
  remote_supervisory: 'Remote supervisory — the platform may request, the local controller still decides.',
  vendor_native: 'Vendor-native — commands go through the manufacturer’s own controller.',
  not_controllable: 'Not controllable — this asset is monitored only.',
  unknown: 'Unknown — nothing is reporting who holds control.',
};

/* --------------------------------------------------------------- picker --- */

async function renderPicker(root, ctx) {
  clear(root);
  root.appendChild(h('div', { class: 'page-head' },
    h('div', null,
      h('h2', { text: 'Control' }),
      h('p', { class: 'lede', text: 'Site and domain operating modes, the maintainer reloads, and the per-asset SDD 17.4 control presentation: actual and requested state, authority, interlocks, last command, manual override and budget impact.' }))));

  const panels = h('div', { class: 'grid grid-2', style: 'margin-bottom:var(--gap)' });
  root.appendChild(panels);
  await paintSitePanels(panels, ctx);

  const result = await api.assets({ limit: 500 });
  if (!result.ok) {
    root.appendChild(resultProblem(result, 'the asset list'));
    return { async refresh() {}, destroy() {} };
  }
  const items = (result.data.items || []).filter((asset) => asset.control_authority !== 'none');
  if (!items.length) {
    root.appendChild(h('div', { class: 'empty' },
      h('h3', null, statusChip('not_deployed', 'No controllable assets')),
      h('p', { text: 'No asset in the registry declares a control authority yet. Monitoring-only assets are still browsable from the asset registry.' }),
      h('p', null, h('a', { href: '#/assets', text: 'Open the asset registry →' }))));
    return { async refresh() {}, destroy() {} };
  }

  root.appendChild(h('div', { class: 'table-wrap' },
    h('table', null,
      h('caption', { text: `${items.length} assets declare a control authority` }),
      h('thead', null, h('tr', null,
        h('th', { text: 'Asset' }), h('th', { text: 'Domain' }), h('th', { text: 'Authority' }),
        h('th', { text: 'Criticality' }), h('th', { text: 'Status' }), h('th', { text: '' }))),
      h('tbody', null, items.map((asset) => h('tr', null,
        h('td', null, h('a', { href: `#/control/${encodeURIComponent(asset.asset_id)}`, text: asset.name }),
          h('div', { class: 'alarm-key mono', text: asset.asset_id })),
        h('td', { text: asset.domain }),
        h('td', { text: asset.control_authority }),
        h('td', { text: asset.criticality }),
        h('td', { text: asset.status }),
        h('td', null, h('a', {
          class: 'btn btn-sm', href: `#/control/${encodeURIComponent(asset.asset_id)}`, text: 'Open',
        }))))))));

  return { async refresh() {}, destroy() {} };
}

/* ------------------------------------------------------- operating modes --- */

function scopeKey(scope) {
  // The API resolves the literal "site" to the configured site id; a domain or
  // an asset is named explicitly so nothing is guessed from the segment.
  return scope.type === 'site' ? 'site' : scope.id;
}

/**
 * One form that sets an operating mode on a site, domain or asset scope
 * (POST /operating-modes/{scope}). The reason dialog comes first because the
 * API refuses a mode change without one, and emergency's reset policy is
 * spelled out before the request is sent rather than learned from a 409.
 */
function modeSetter({ scopes, current, onDone, note }) {
  const scopeSelect = h('select', { class: 'select-sm' },
    scopes.map((scope) => h('option', { value: `${scope.type}:${scope.id}`, text: scope.label })));
  const modeSelect = h('select', { class: 'select-sm' },
    MODES.map((mode) => h('option', { value: mode, text: mode, selected: mode === current })));
  const expires = h('input', { type: 'datetime-local', class: 'input' });
  const clearBox = h('input', { type: 'checkbox' });
  const hint = h('p', { class: 'card-note', style: 'margin:.2rem 0 0', text: MODE_NOTES[modeSelect.value] || '' });
  modeSelect.addEventListener('change', () => { hint.textContent = MODE_NOTES[modeSelect.value] || ''; });
  const button = h('button', { class: 'btn btn-primary btn-sm', type: 'button', text: 'Set mode…' });
  const outcome = h('div', { style: 'margin-top:.5rem' });

  button.addEventListener('click', async () => {
    if (!requireRole('operator')) return;
    const [type, id] = scopeSelect.value.split(/:(.*)/s);
    const scope = scopes.find((s) => s.type === type && s.id === id);
    const mode = modeSelect.value;
    const leavingEmergency = current === 'emergency' && mode !== 'emergency';
    const reason = await askReason({
      title: `Set ${scope.type} ${scope.id} to ${mode}`,
      note: `${MODE_NOTES[mode] || ''} The change is recorded with your name and this reason`
        + `${expires.value ? `, and expires ${fmtDateTime(new Date(expires.value).toISOString())}` : ''}.`
        + (leavingEmergency
          ? (clearBox.checked
            ? ' You are asserting that the emergency condition is gone.'
            : ' Emergency is latched: tick "condition cleared" or the API will refuse this.')
          : ''),
      confirmLabel: 'Set mode',
      danger: mode === 'emergency' || mode === 'off' || leavingEmergency,
    });
    if (!reason) return;
    const body = { mode, reason, condition_clear: clearBox.checked };
    if (scope.type !== 'site') body.scope_type = scope.type;
    if (expires.value) body.expires_at = new Date(expires.value).toISOString();
    button.disabled = true;
    const response = await api.setOperatingMode(scopeKey(scope), body);
    button.disabled = false;
    showModeOutcome(outcome, response, scope, mode);
    if (response.ok) {
      toast('success', 'Mode set', `${scope.type} ${scope.id} is now ${response.data.mode}.`);
      if (onDone) await onDone();
    } else if (response.forbidden) {
      toast('error', 'Refused', `${response.error} — check your operator role.`);
    } else if (response.missing) {
      toast('warn', 'Mode endpoint not available', 'This node does not expose POST /operating-modes. Nothing was changed.');
    } else {
      toast('error', 'Mode not changed', typeof response.error === 'string' ? response.error : `HTTP ${response.status}`);
    }
  });

  return h('div', null,
    note ? h('p', { class: 'card-note', style: 'margin:0 0 .4rem', text: note }) : null,
    h('div', { class: 'filters', style: 'margin:0' },
      h('label', null, h('span', { text: 'Scope' }), scopeSelect),
      h('label', null, h('span', { text: 'Mode' }), modeSelect),
      h('label', null, h('span', { text: 'Expires (optional)' }), expires),
      h('label', { style: 'flex-direction:row;align-items:center;gap:.4rem' },
        clearBox, h('span', { text: 'Emergency condition cleared' })),
      button),
    hint,
    outcome);
}

function showModeOutcome(host, response, scope, mode) {
  clear(host);
  if (response.ok) {
    const row = response.data;
    host.appendChild(h('div', null,
      h('p', { style: 'margin:0 0 .3rem' }, statusChip('ok', `${row.scope_type} ${row.scope_id}: ${row.mode}`)),
      kv([
        ['Previous mode', row.previous_mode || '—'],
        ['Set by', `${row.changed_by} · ${fmtAge(row.changed_at)}`],
        ['Reason', row.reason || '—'],
        ['Expires', row.expires_at ? fmtDateTime(row.expires_at) : 'no expiry'],
        ['Latched', row.latched ? statusChip('alarm', 'yes — needs a deliberate reset') : 'no'],
      ])));
    return;
  }
  const detail = response.data && typeof response.data === 'object' && response.data.detail
    && typeof response.data.detail === 'object' ? response.data.detail : null;
  host.appendChild(h('div', null,
    h('p', { style: 'margin:0 0 .3rem' },
      statusChip('alarm', response.forbidden ? 'Refused' : `HTTP ${response.status}`), ' ',
      h('span', { text: detail ? detail.message : (response.error || `could not set ${scope.type} ${scope.id} to ${mode}`) })),
    detail && Array.isArray(detail.required)
      ? h('ul', { class: 'provenance' }, detail.required.map((item) => h('li', { text: `Required: ${item}` })))
      : null));
}

async function paintSitePanels(host, ctx) {
  clear(host);
  host.appendChild(h('p', { class: 'loading', text: 'Loading operating modes…' }));
  const modes = await api.operatingModes();
  clear(host);
  host.appendChild(modesCard(modes, ctx, () => paintSitePanels(host, ctx)));
  host.appendChild(maintainerCard());
}

function modesCard(modes, ctx, reload) {
  const overview = ctx.latestOverview().data;
  const siteId = overview && overview.site ? overview.site.site_id : null;
  const siteMode = overview && overview.site && overview.site.operating_mode
    ? overview.site.operating_mode.mode : null;
  const domains = overview
    ? (overview.subsystems || []).map((entry) => entry.domain).filter(Boolean)
    : [];
  const rows = modes.ok ? (Array.isArray(modes.data) ? modes.data : []) : [];
  rows.forEach((row) => { if (row.scope_type === 'domain' && !domains.includes(row.scope_id)) domains.push(row.scope_id); });

  const children = [];
  if (!modes.ok) {
    children.push(resultProblem(modes, 'the operating-mode list'));
  } else if (!rows.length) {
    children.push(h('p', { class: 'card-note', style: 'margin-top:0' },
      statusChip('no_data', 'No mode stored'),
      ' No scope has had a mode set yet; every scope is implicitly automatic.'));
  } else {
    children.push(h('div', { class: 'table-wrap', style: 'margin-bottom:.7rem' },
      h('table', null,
        h('thead', null, h('tr', null,
          h('th', { text: 'Scope' }), h('th', { text: 'Mode' }), h('th', { text: 'Set by' }),
          h('th', { text: 'Reason' }), h('th', { text: 'Expires' }))),
        h('tbody', null, rows.map((row) => h('tr', null,
          h('td', null, h('span', { class: 'tag', text: row.scope_type }), ' ',
            row.scope_type === 'asset'
              ? h('a', { class: 'mono', href: `#/control/${encodeURIComponent(row.scope_id)}`, text: row.scope_id })
              : h('span', { class: 'mono', text: row.scope_id })),
          h('td', null, statusChip(row.mode === 'emergency' ? 'alarm' : (row.mode === 'maintenance' ? 'degraded' : 'ok'), row.mode),
            row.latched ? ' latched' : ''),
          h('td', { text: row.changed_by ? `${row.changed_by} · ${fmtAge(row.changed_at)}` : '—' }),
          h('td', { text: row.reason || '—' }),
          h('td', { text: row.expires_at ? fmtDateTime(row.expires_at) : '—' })))))));
  }

  const scopes = [{ type: 'site', id: siteId || 'site', label: `site — ${siteId || 'this site'}` }]
    .concat(domains.sort().map((domain) => ({ type: 'domain', id: domain, label: `domain — ${domain}` })));
  children.push(modeSetter({
    scopes,
    current: siteMode || 'automatic',
    onDone: async () => { await reload(); ctx.refreshOverview(); },
    note: 'Set the mode of the whole site or of one domain. An asset’s own mode is set from its control panel below.',
  }));
  return card({
    title: 'Operating modes',
    status: rows.some((row) => row.mode === 'emergency') ? 'alarm' : (rows.some((row) => row.mode === 'maintenance') ? 'degraded' : 'ok'),
    statusText: siteMode ? `site: ${siteMode}` : 'site: not set',
    children,
  });
}

/* ----------------------------------------------------- maintainer actions --- */

function countsKv(payload, keys) {
  return kv(keys.filter((key) => key in payload).map((key) => [key.replace(/_/g, ' '),
    Array.isArray(payload[key]) ? String(payload[key].length) : String(payload[key] ?? '—')]));
}

function maintainerCard() {
  const registryOutcome = h('div', { style: 'margin-top:.4rem' });
  const alarmOutcome = h('div', { style: 'margin-top:.4rem' });
  const strict = h('input', { type: 'checkbox', checked: true });

  function showReload(host, response, what, keys) {
    clear(host);
    if (response.ok) {
      const payload = response.data;
      host.appendChild(h('p', { style: 'margin:0 0 .3rem' }, statusChip('ok', `${what} reloaded`)));
      host.appendChild(countsKv(payload, keys));
      const warnings = payload.warnings || [];
      if (warnings.length) {
        host.appendChild(h('details', { class: 'raw' },
          h('summary', { text: `${warnings.length} warning${warnings.length === 1 ? '' : 's'}` }),
          h('ul', { class: 'provenance' }, warnings.map((line) => h('li', { text: String(line) })))));
      }
      return;
    }
    const detail = response.data && typeof response.data === 'object' && typeof response.data.detail === 'object'
      ? response.data.detail : null;
    host.appendChild(h('p', { style: 'margin:0' },
      statusChip('alarm', response.forbidden ? 'Refused' : `HTTP ${response.status}`), ' ',
      h('span', { text: detail && detail.message ? detail.message : (response.error || 'failed') })));
    if (detail && Array.isArray(detail.warnings) && detail.warnings.length) {
      host.appendChild(h('ul', { class: 'provenance' }, detail.warnings.map((line) => h('li', { text: String(line) }))));
    }
  }

  async function reloadRegistry() {
    if (!requireRole('maintainer')) return;
    const yes = await confirmAction({
      title: 'Reload the registry from the design package',
      note: 'Re-runs the idempotent package loader against data/ on this node. Nothing is dropped; edited assets, points and bindings are converged onto the files. The reload is recorded under your name.',
      confirmLabel: 'Reload registry',
    });
    if (!yes) return;
    registryOutcome.replaceChildren(h('p', { class: 'loading', text: 'Reloading…' }));
    showReload(registryOutcome, await api.reloadRegistry(), 'Registry',
      ['assets', 'points', 'bindings', 'relationships', 'asset_classes', 'point_definitions', 'locations', 'skipped_assets', 'package_version', 'revision']);
  }

  async function reloadAlarms() {
    if (!requireRole('maintainer')) return;
    const yes = await confirmAction({
      title: 'Reload the alarm definitions',
      note: `Replaces the trip thresholds and escalation paths with data/alarm_definitions.yaml as it is on disk${strict.checked ? '; strict mode rejects the whole reload if any point or asset does not resolve' : ''}. Active alarms are not cleared.`,
      confirmLabel: 'Reload definitions',
      danger: true,
    });
    if (!yes) return;
    alarmOutcome.replaceChildren(h('p', { class: 'loading', text: 'Reloading…' }));
    showReload(alarmOutcome, await api.reloadAlarmDefinitions(strict.checked), 'Alarm definitions',
      ['total', 'created', 'updated', 'unchanged', 'disabled', 'reloaded_by', 'source']);
  }

  return card({
    title: 'Maintainer actions',
    children: [
      h('p', { class: 'card-note', style: 'margin-top:0', text: 'Both reloads need the maintainer role and converge the database onto the files in the design package. Neither takes a reason; the API records who asked.' }),
      h('div', { class: 'filters', style: 'margin:0 0 .3rem' },
        h('button', { class: 'btn btn-sm', type: 'button', onclick: reloadRegistry, text: 'Reload registry…' })),
      registryOutcome,
      h('div', { class: 'filters', style: 'margin:.6rem 0 .3rem' },
        h('label', { style: 'flex-direction:row;align-items:center;gap:.4rem' }, strict, h('span', { text: 'Strict' })),
        h('button', { class: 'btn btn-sm', type: 'button', onclick: reloadAlarms, text: 'Reload alarm definitions…' })),
      alarmOutcome,
    ],
  });
}

/* ------------------------------------------------------ budget leases ------ */

/** Request a power-budget lease for this asset (POST /energy/load-budgets/reservations).
 *  A denial comes back 201 with `granted: false` and a reason: it is an answer,
 *  not an error, and is shown as one. */
function leaseConsole(data, reload) {
  const asset = data.asset;
  const kwInput = h('input', { type: 'number', class: 'input', min: '0.1', step: '0.1', placeholder: 'e.g. 1.5' });
  const minutes = h('input', { type: 'number', class: 'input', min: '1', step: '1', placeholder: 'until revoked' });
  const priority = h('select', { class: 'select-sm' },
    [0, 1, 2, 3, 4].map((level) => h('option', { value: String(level), text: `${level}${level === 0 ? ' (highest)' : level === 4 ? ' (lowest)' : ''}`, selected: level === 3 })));
  const revocable = h('input', { type: 'checkbox', checked: true });
  const button = h('button', { class: 'btn btn-primary btn-sm', type: 'button', text: 'Request lease…' });
  const outcome = h('div', { style: 'margin-top:.5rem' });

  button.addEventListener('click', async () => {
    if (!requireRole('operator')) return;
    const kw = Number(kwInput.value);
    if (!(kw > 0)) { kwInput.reportValidity(); toast('warn', 'Requested power required', 'Enter the kW to reserve.'); return; }
    const reason = await askReason({
      title: `Request ${fmtNumber(kw, 2)} kW for ${asset.name}`,
      note: 'A lease is an allocation of the site power budget, never a safety permissive (SDD 31.4). The energy manager grants or denies it against the current state and surplus, and records either answer with your name.',
      confirmLabel: 'Request lease',
    });
    if (!reason) return;
    const body = {
      asset_id: asset.asset_id, requested_kw: kw, reason,
      priority: Number(priority.value), revocable: revocable.checked,
    };
    if (Number(minutes.value) > 0) body.duration_s = Math.round(Number(minutes.value) * 60);
    button.disabled = true;
    const response = await api.requestLease(body);
    button.disabled = false;
    clear(outcome);
    if (response.ok) {
      const decision = response.data;
      const lease = decision.lease;
      outcome.appendChild(h('p', { style: 'margin:0 0 .3rem' },
        decision.granted ? statusChip('ok', `Granted ${fmtNumber(lease ? lease.granted_kw : kw, 2)} kW`) : statusChip('alarm', 'Denied'),
        ' ', h('span', { text: decision.reason || '' })));
      outcome.appendChild(kv([
        ['Energy state', decision.energy_state || '—'],
        ['Available', decision.available_kw !== null && decision.available_kw !== undefined ? `${fmtNumber(decision.available_kw, 2)} kW` : '—'],
        ['Already granted', decision.already_granted_kw !== null && decision.already_granted_kw !== undefined ? `${fmtNumber(decision.already_granted_kw, 2)} kW` : '—'],
        ['Lease', lease ? `${lease.lease_id} · expires ${lease.expires_at ? fmtDateTime(lease.expires_at) : 'when revoked'}` : '—'],
      ]));
      toast(decision.granted ? 'success' : 'info', decision.granted ? 'Lease granted' : 'Lease denied', decision.reason || '');
      if (decision.granted) await reload();
    } else if (response.missing) {
      toast('warn', 'Lease endpoint not available', 'This node does not expose the reservation route. Nothing was requested.');
    } else {
      outcome.appendChild(h('p', { style: 'margin:0' },
        statusChip('alarm', response.forbidden ? 'Refused' : `HTTP ${response.status}`), ' ', h('span', { text: response.error || '' })));
      toast('error', 'Lease not requested', response.error || `HTTP ${response.status}`);
    }
  });

  return h('div', { style: 'margin-top:.5rem' },
    h('div', { class: 'filters', style: 'margin:0' },
      h('label', null, h('span', { text: 'Power (kW)' }), kwInput),
      h('label', null, h('span', { text: 'Duration (min)' }), minutes),
      h('label', null, h('span', { text: 'Priority' }), priority),
      h('label', { style: 'flex-direction:row;align-items:center;gap:.4rem' }, revocable, h('span', { text: 'Revocable' })),
      button),
    outcome);
}

function leaseRow(lease, reload) {
  const revoke = h('button', { class: 'btn btn-sm btn-danger', type: 'button', text: 'Revoke…' });
  revoke.addEventListener('click', async () => {
    if (!requireRole('operator')) return;
    const reason = await askReason({
      title: `Revoke lease ${lease.lease_id}`,
      note: 'Revocation withdraws the allocation only. No stop command is issued; the subsystem stays safe under its own controller (SDD 31.4).',
      confirmLabel: 'Revoke',
      danger: true,
    });
    if (!reason) return;
    const response = await api.revokeLease(lease.lease_id, reason);
    if (response.ok) {
      toast('success', 'Lease revoked', response.data.note || lease.lease_id);
      await reload();
    } else {
      toast('error', 'Lease not revoked', response.error || `HTTP ${response.status}`);
    }
  });
  return h('li', { class: 'provenance', style: 'display:flex;gap:.6rem;align-items:center;flex-wrap:wrap' },
    h('span', { text: `${lease.granted_kw} kW · ${lease.state || 'active'} · priority ${lease.priority} · expires ${lease.expires_at ? fmtAge(lease.expires_at) : 'when revoked'} · ${lease.reason} (requested by ${lease.requested_by || 'unknown'})` }),
    lease.revocable === false ? h('span', { class: 'tag', text: 'not revocable' }) : revoke);
}

/* ---------------------------------------------------------------- panel --- */

function valueText(view) {
  if (!view) return '—';
  if (view.value === null || view.value === undefined) return null;
  if (typeof view.value === 'boolean') return view.value ? 'ON' : 'OFF';
  if (typeof view.value === 'number') return `${fmtNumber(view.value, 2)}${view.unit ? ' ' + view.unit : ''}`;
  return String(view.value);
}

function requestedText(raw) {
  if (raw === null || raw === undefined) return null;
  if (typeof raw === 'object') {
    if ('value' in raw) return requestedText(raw.value);
    return JSON.stringify(raw);
  }
  if (typeof raw === 'boolean') return raw ? 'ON' : 'OFF';
  return String(raw);
}

/** 17.4 items 1 and 2, side by side, with the mismatch called out. */
function actualVsRequested(data) {
  const actual = data.actual_state || {};
  const requested = data.requested_state || {};
  const pending = data.pending_requests || [];

  const rows = [];
  const seen = new Set();

  Object.entries(actual).forEach(([name, view]) => {
    seen.add(name);
    const requestKey = name.replace('_actual', '_requested');
    const requestView = requested[requestKey];
    const pendingEntry = pending.find((p) => p.point_name === requestKey || p.point_name === name);
    rows.push({
      label: name,
      actual: valueText(view),
      actualStatus: view ? view.status : 'no_data',
      actualAge: view ? view.ts : null,
      requested: requestView ? valueText(requestView) : (pendingEntry ? requestedText(pendingEntry.requested_value) : null),
      requestedAt: pendingEntry ? pendingEntry.requested_at : (requestView ? requestView.requested_at : null),
    });
  });

  Object.entries(requested).forEach(([name, view]) => {
    const base = name.replace('_requested', '_actual');
    if (seen.has(base)) return;
    rows.push({
      label: name,
      actual: null,
      actualStatus: 'no_data',
      requested: valueText(view),
      requestedAt: view.requested_at,
    });
  });

  if (!rows.length) {
    return h('div', { class: 'empty' },
      h('h3', null, statusChip('no_points', 'No state points')),
      h('p', { text: 'This asset has no state or command points registered, so there is no actual or requested state to compare. That is a registry gap, not an off state.' }));
  }

  return h('div', null, rows.map((row) => {
    const mismatch = row.actual !== null && row.requested !== null && row.actual !== row.requested;
    return h('div', { style: 'margin-bottom:.8rem' },
      h('div', { class: 'card-note', style: 'margin:0 0 .25rem', text: row.label }),
      h('div', { class: 'state-compare' },
        h('div', { class: 'state-box' },
          h('div', { class: 'label', text: 'Actual (measured)' }),
          h('div', { class: 'val' }, row.actual !== null ? row.actual : statusChip(row.actualStatus || 'no_data')),
          row.actualAge ? h('div', { class: 'card-note', style: 'margin:0', text: fmtAge(row.actualAge) }) : null),
        h('div', { class: 'arrow', 'aria-hidden': 'true', text: '⇄' }),
        h('div', { class: 'state-box', dataset: { mismatch: mismatch ? 'yes' : 'no' } },
          h('div', { class: 'label', text: 'Requested' }),
          h('div', { class: 'val' }, row.requested !== null ? row.requested : statusChip('no_data', 'None pending')),
          row.requestedAt ? h('div', { class: 'card-note', style: 'margin:0', text: fmtAge(row.requestedAt) }) : null)),
      mismatch
        ? h('p', { class: 'card-note', style: 'color:var(--sev-major)' },
            'Requested and actual disagree — the request has not taken effect. Check the interlocks and the last command result below.')
        : null);
  }));
}

/** 17.4 item 4 — the refusals, first-class. */
function interlockBlock(data) {
  const block = data.interlocks || {};
  const evaluated = block.evaluated || [];
  const blocking = block.blocking || [];

  const children = [];
  if (!evaluated.length) {
    children.push(h('p', { class: 'card-note' },
      statusChip('no_data', 'Never evaluated'), ' ',
      block.note || 'No interlock evaluation has been recorded for this asset.'));
  } else {
    if (blocking.length) {
      children.push(h('p', { style: 'margin-bottom:.5rem' },
        statusChip('alarm', `${blocking.length} interlock${blocking.length === 1 ? '' : 's'} would refuse a command`)));
    } else {
      children.push(h('p', { style: 'margin-bottom:.5rem' }, statusChip('ok', 'All evaluated interlocks passed')));
    }
    if (block.dispatch_blocker_count) {
      children.push(h('p', { class: 'card-note', style: 'margin-top:0' },
        `${block.dispatch_blocker_count} check${block.dispatch_blocker_count === 1 ? '' : 's'} would stop dispatch even when passing (for example, physical control being globally disabled).`));
    }
    children.push(h('div', null, evaluated.map((entry) => h('div', { class: 'interlock' },
      entry.blocking === null || entry.blocking === undefined
        ? statusChip('no_data', 'unknown')
        : (entry.blocking ? statusChip('alarm', 'BLOCKS') : statusChip('ok', 'passes')),
      h('div', null,
        h('div', { class: 'name', text: entry.name }),
        entry.detail ? h('div', { class: 'detail', text: entry.detail }) : null,
        entry.blocks_dispatch
          ? h('div', { class: 'detail', style: 'color:var(--sev-major)', text: 'Blocks dispatch.' })
          : null,
        h('div', { class: 'detail', style: 'color:var(--text-faint)',
          text: `${entry.source === 'live_point' ? 'Live point' : 'From command'}`
            + (entry.command_id ? ` ${entry.command_id}` : '')
            + (entry.evaluated_at ? ` · ${fmtAge(entry.evaluated_at)}` : '') }))))));
  }
  return card({
    title: 'Interlocks preventing operation',
    status: blocking.length ? 'alarm' : (evaluated.length ? 'ok' : 'no_data'),
    children,
  });
}

/** 17.4 item 5. */
function commandBlock(data) {
  const last = data.last_command;
  const recent = data.recent_commands || [];
  if (!last) {
    return card({
      title: 'Last command and issuer',
      status: 'no_data',
      children: h('p', { class: 'card-note', text: 'No supervisory command has ever been issued to this asset.' }),
    });
  }
  const tone = ({ succeeded: 'ok', acknowledged: 'ok', dispatched: 'stale', pending: 'stale' })[last.state]
    || (['rejected', 'failed', 'expired'].includes(last.state) ? 'alarm' : 'unknown');
  return card({
    title: 'Last command and issuer',
    status: tone,
    statusText: last.state,
    children: [
      kv([
        ['Command', `${last.command}${last.value !== null && last.value !== undefined ? ' = ' + JSON.stringify(last.value) : ''}`],
        ['Issued by', `${last.issued_by} (${last.issued_by_kind})`],
        ['Reason', last.reason],
        ['Operating mode', last.operating_mode || '—'],
        ['Issued', `${fmtDateTime(last.issued_at)} · ${fmtAge(last.issued_at)}`],
        ['Result', last.state_reason ? `${last.state} — ${last.state_reason}` : last.state],
        ['Point', last.point_id || '—'],
        ['Command id', last.command_id],
      ]),
      recent.length > 1
        ? h('details', { class: 'raw' },
            h('summary', { text: `Command history (${recent.length})` }),
            h('div', { class: 'table-wrap', style: 'margin-top:.4rem' },
              h('table', null,
                h('thead', null, h('tr', null,
                  h('th', { text: 'When' }), h('th', { text: 'Command' }), h('th', { text: 'By' }),
                  h('th', { text: 'State' }), h('th', { text: 'Reason' }))),
                h('tbody', null, recent.map((cmd) => h('tr', null,
                  h('td', { text: fmtAge(cmd.issued_at) }),
                  h('td', { text: cmd.command }),
                  h('td', { text: cmd.issued_by }),
                  h('td', { text: cmd.state }),
                  h('td', { text: cmd.state_reason || cmd.reason || '' })))))))
        : null,
    ],
  });
}

/** 17.4 item 7. */
function budgetBlock(data, reload) {
  const budget = data.budget_impact || {};
  const profile = budget.load_profile;
  const measured = budget.measured_power_kw;

  const children = [];
  if (!profile) {
    children.push(h('p', { class: 'card-note' },
      statusChip('no_data', 'No load profile'), ' ',
      budget.load_profile_note || 'The energy impact of operating this asset is unknown.'));
  } else {
    children.push(kv([
      ['Load tier', `${profile.effective_tier}${profile.effective_tier !== profile.base_tier ? ` (base ${profile.base_tier})` : ''}`],
      ['Tier override', profile.tier_override_reason
        ? `${profile.tier_override_reason} (expires ${fmtAge(profile.tier_override_expires_at)})` : '—'],
      ['Criticality', profile.criticality],
      ['Rated power', profile.rated_power_kw !== null ? `${fmtNumber(profile.rated_power_kw, 2)} kW` : statusChip('no_data', 'not recorded')],
      ['Estimated power', profile.estimated_power_kw !== null ? `${fmtNumber(profile.estimated_power_kw, 2)} kW` : statusChip('no_data', 'not recorded')],
      ['Measured power', measured && measured.status === 'ok'
        ? `${fmtNumber(measured.value, 2)} kW` : statusChip((measured && measured.status) || 'no_data')],
      ['Control method', profile.control_method],
      ['Shed group', profile.shed_group ? `${profile.shed_group} (order ${profile.shed_order ?? '—'})` : '—'],
      ['Restoration group', profile.restoration_group ? `${profile.restoration_group} (order ${profile.restoration_order ?? '—'})` : '—'],
      ['Minimum on / off', `${profile.minimum_on_time_s ?? '—'} s / ${profile.minimum_off_time_s ?? '—'} s`],
      ['Data status', profile.data_status],
    ]));
    if ((profile.open_fields || []).length) {
      children.push(h('p', { class: 'card-note' },
        statusChip('design_only', 'Unresolved'), ' ', profile.open_fields.join(', ')));
    }
  }

  children.push(h('h4', { class: 'card-note', style: 'margin-top:.8rem;text-transform:uppercase;letter-spacing:.06em', text: 'Site energy context' }));
  children.push(kv([
    ['Energy state', budget.energy_state || statusChip('no_data', 'EMS not reporting')],
    ['Shed groups active', (budget.shed_groups_active || []).length ? budget.shed_groups_active.join(', ') : 'none'],
    ['Currently shed', budget.currently_shed
      ? h('span', null, statusChip('alarm', 'YES'), ' this load sits in an active shed group')
      : statusChip('ok', 'no')],
  ]));

  const leases = budget.active_leases || [];
  children.push(h('h4', { class: 'card-note', style: 'margin-top:.8rem;text-transform:uppercase;letter-spacing:.06em', text: 'Power-budget leases' }));
  children.push(leases.length
    ? h('ul', null, leases.map((lease) => leaseRow(lease, reload)))
    : h('p', { class: 'card-note', text: 'No power budget has been granted to this asset.' }));
  children.push(leaseConsole(data, reload));

  return card({
    title: 'Impact on energy and resource budgets',
    status: budget.currently_shed ? 'degraded' : (profile ? 'ok' : 'no_data'),
    children,
  });
}

/** The command console — including every reason the platform would refuse. */
function commandConsole(data, reload) {
  const commandable = data.commandable || {};
  const points = data.control_points || [];
  const children = [];

  children.push(h('div', { class: 'pill-row', style: 'margin-bottom:.6rem' },
    commandable.allowed
      ? statusChip('ok', 'Commands permitted')
      : statusChip('design_only', 'Commands not permitted')));

  if ((commandable.reasons || []).length) {
    children.push(h('ul', { class: 'provenance' },
      commandable.reasons.map((reason) => h('li', { text: reason }))));
  }

  children.push(kv([
    ['Physical control (global)', commandable.physical_control_enabled
      ? statusChip('ok', 'enabled') : statusChip('design_only', 'disabled until commissioning')],
    ['Control points', String(commandable.control_point_count ?? 0)],
    ['Any binding commissioned', commandable.any_binding_commissioned
      ? statusChip('ok', 'yes') : statusChip('design_only', 'no')],
    ['Blocking interlocks', String(commandable.blocking_interlocks ?? 0)],
  ]));

  if (!points.length) {
    children.push(h('p', { class: 'card-note', text: 'This asset exposes no command points, so there is nothing to issue.' }));
    return card({ title: 'Command console', status: 'design_only', children });
  }

  const select = h('select', { class: 'select-sm', id: 'cmd-point' },
    points.map((point) => h('option', {
      value: point.point_name,
      text: `${point.point_name} (${point.data_type}${point.unit ? ', ' + point.unit : ''})`,
    })));
  const commandInput = h('input', { type: 'text', class: 'input', id: 'cmd-name', value: 'set_value',
                                    placeholder: 'e.g. set_mode, set_enabled' });
  const valueInput = h('input', { type: 'text', class: 'input', id: 'cmd-value',
                                  placeholder: 'e.g. true, 21.5, "reduced_power"' });
  const outcome = h('div', { style: 'margin-top:.7rem' });

  function parseValue() {
    if (!valueInput.value.trim()) return undefined;
    try { return JSON.parse(valueInput.value); } catch { return valueInput.value; }
  }

  /** Render whatever the command service said, including its interlock verdicts. */
  function showOutcome(response, wasDryRun) {
    clear(outcome);
    const detail = response.data && response.data.detail && typeof response.data.detail === 'object'
      ? response.data.detail
      : (response.data && typeof response.data === 'object' ? response.data : null);

    const verdicts = detail && Array.isArray(detail.interlocks_evaluated) ? detail.interlocks_evaluated : [];
    const denied = detail && Array.isArray(detail.denied_by) ? detail.denied_by : [];

    outcome.appendChild(h('h4', { class: 'card-note',
      style: 'margin:0 0 .3rem;text-transform:uppercase;letter-spacing:.06em',
      text: wasDryRun ? 'Dry-run result — nothing was published' : 'Command result' }));
    outcome.appendChild(h('p', { style: 'margin:0 0 .4rem' },
      response.ok ? statusChip('ok', detail && detail.state ? detail.state : 'accepted')
        : statusChip('alarm', (detail && detail.refused_by) || `HTTP ${response.status}`),
      ' ',
      h('span', { text: (detail && (detail.message || detail.state_reason)) || response.error || '' })));

    if (denied.length) {
      outcome.appendChild(h('p', { class: 'card-note', text: `Denied by: ${denied.join(', ')}` }));
    }
    if (verdicts.length) {
      outcome.appendChild(h('div', null, verdicts.map((entry) => h('div', { class: 'interlock' },
        entry.allowed === false ? statusChip('alarm', 'BLOCKS') : statusChip('ok', 'passes'),
        h('div', null,
          h('div', { class: 'name', text: entry.code || entry.name || 'interlock' }),
          h('div', { class: 'detail', text: entry.reason || '' }),
          entry.blocks_dispatch
            ? h('div', { class: 'detail', style: 'color:var(--sev-major)', text: 'Would stop dispatch of a real command.' })
            : null)))));
    }
  }

  async function send(dryRun) {
    if (!dryRun && !requireRole('operator')) return;
    if (dryRun && !requireRole('viewer')) return;
    const pointName = select.value;
    const reason = dryRun
      ? `Dry-run preflight from the operator console for ${pointName}`
      : await askReason({
        title: `Issue ${commandInput.value} to ${data.asset.name}`,
        note: `This writes a command record with your name, your role, this reason and the current`
          + ` operating mode (${(data.operating_mode || {}).mode || 'unset'}). Point: ${pointName}.`
          + (commandable.allowed ? '' : ' The platform is expected to refuse this command — the reasons are listed above.'),
        confirmLabel: 'Issue command',
        danger: true,
      });
    if (!reason) return;

    const response = await api.issueCommand({
      assetId: data.asset.asset_id,
      pointName,
      command: commandInput.value || 'set_value',
      value: parseValue(),
      reason,
      dryRun,
    });

    if (response.missing) {
      toast('warn', 'Command endpoint not available',
        'This node does not expose POST /commands. Nothing was sent to any equipment.');
      return;
    }
    if (response.forbidden) {
      toast('error', 'Refused', `${response.error} — your role is not sufficient.`);
      return;
    }
    showOutcome(response, dryRun);
    if (response.ok && !dryRun) {
      toast('success', 'Command accepted', 'Watch the requested vs actual comparison above.');
      await reload();
    } else if (!response.ok) {
      toast(dryRun ? 'info' : 'error', dryRun ? 'Dry run: command would be refused' : 'Command rejected',
        response.error || `HTTP ${response.status}`);
    }
  }

  const dryButton = h('button', { class: 'btn btn-sm', text: 'Preflight (dry run)' });
  const sendButton = h('button', { class: 'btn btn-primary btn-sm', text: 'Issue command…' });

  // Guard against a double press firing two real commands at the equipment.
  async function guarded(dryRun) {
    dryButton.disabled = true;
    sendButton.disabled = true;
    try { await send(dryRun); } finally {
      dryButton.disabled = false;
      sendButton.disabled = false;
    }
  }
  dryButton.addEventListener('click', () => guarded(true));
  sendButton.addEventListener('click', () => guarded(false));

  children.push(h('div', { class: 'filters', style: 'margin-top:.8rem' },
    h('label', null, h('span', { text: 'Point' }), select),
    h('label', null, h('span', { text: 'Command' }), commandInput),
    h('label', null, h('span', { text: 'Value (JSON)' }), valueInput),
    dryButton, sendButton));
  children.push(outcome);

  children.push(h('p', { class: 'card-note',
    text: 'Every command is recorded with the issuer, the reason, the operating mode and the interlocks evaluated (SDD 5.7, FR-004). A reason is mandatory before the request is sent.' }));

  return card({ title: 'Command console', status: commandable.allowed ? 'ok' : 'design_only', children });
}

async function drawPanel(root, ctx, assetId) {
  const result = await api.control(assetId);
  clear(root);

  root.appendChild(h('div', { class: 'page-head' },
    h('div', null,
      h('p', { class: 'card-note', style: 'margin:0' }, h('a', { href: '#/control', text: '← Control' })),
      h('h2', { text: result.ok ? result.data.asset.name : assetId }),
      h('p', { class: 'lede mono', text: assetId })),
    h('div', { class: 'pill-row' },
      h('a', { class: 'btn btn-sm', href: `#/assets/${encodeURIComponent(assetId)}`, text: 'Registry record' }),
      h('button', { class: 'btn btn-sm', onclick: () => drawPanel(root, ctx, assetId), text: 'Reload' }))));

  if (!result.ok) {
    root.appendChild(resultProblem(result, `the control presentation for ${assetId}`));
    return;
  }

  const data = result.data;
  const asset = data.asset;
  const authority = data.authority || {};
  const mode = data.operating_mode || {};
  const override = data.manual_override || {};

  root.appendChild(h('div', { class: 'pill-row', style: 'margin-bottom:var(--gap)' },
    h('span', { class: 'tag', text: asset.domain }),
    h('span', { class: 'tag', text: asset.asset_class }),
    h('span', { class: 'tag', text: `status: ${asset.status}` }),
    h('span', { class: 'tag', text: `criticality: ${asset.criticality}` }),
    (asset.open_fields || []).length
      ? statusChip('design_only', `${asset.open_fields.length} unresolved fields`)
      : null));

  const stateCard = card({
    title: 'Actual state vs requested state',
    children: actualVsRequested(data),
  });

  const authorityCard = card({
    title: 'Control authority and operating mode',
    status: authority.local_or_remote === 'not_controllable' ? 'not_deployed' : 'ok',
    statusText: authority.local_or_remote,
    children: [
      h('p', { class: 'card-note', style: 'margin-top:0',
        text: AUTHORITY_COPY[authority.local_or_remote] || AUTHORITY_COPY.unknown }),
      kv([
        ['Declared authority', authority.declared],
        ['Reported owner', authority.reported_owner || statusChip('no_data', 'no control_owner point')],
        ['Effective mode', mode.mode
          ? `${mode.mode} (scope: ${mode.scope})` : statusChip('no_data', 'no mode set at any scope')],
        ['Mode set by', mode.changed_by ? `${mode.changed_by} · ${fmtAge(mode.changed_at)}` : '—'],
        ['Mode reason', mode.reason || '—'],
      ]),
      authority.note ? h('p', { class: 'card-note', text: authority.note }) : null,
      h('h4', { class: 'card-note', style: 'margin-top:.8rem;text-transform:uppercase;letter-spacing:.06em', text: 'Set operating mode' }),
      modeSetter({
        scopes: [
          { type: 'asset', id: asset.asset_id, label: `asset — ${asset.name}` },
          { type: 'domain', id: asset.domain, label: `domain — ${asset.domain}` },
          { type: 'site', id: 'site', label: 'site — whole property' },
        ],
        current: mode.mode || 'automatic',
        onDone: async () => { await drawPanel(root, ctx, assetId); ctx.refreshOverview(); },
      }),
    ],
  });

  const overrideCard = card({
    title: 'Manual override',
    status: override.active === null ? 'no_data' : (override.active ? 'degraded' : 'ok'),
    statusText: override.active === null ? 'Unknown' : (override.active ? 'ACTIVE' : 'Not active'),
    children: [
      override.active
        ? h('p', { text: 'Manual override is active at the equipment. Supervisory commands may be ignored, and a shed request against this load may silently fail (SDD 39 EMS-T013).' })
        : h('p', { class: 'card-note', text: override.note || 'No manual override is asserted.' }),
      Object.keys(override.record || {}).length
        ? kv(Object.entries(override.record).map(([key, value]) => [key, typeof value === 'object' ? JSON.stringify(value) : String(value)]))
        : h('p', { class: 'card-note', text: 'No manual-override method is documented for this asset (MVP acceptance criterion 9 requires one for every critical asset).' }),
      override.live_point
        ? h('p', { class: 'card-note', text: `Live point ${override.live_point.point_id}: ${valueText(override.live_point) ?? 'no value'}` })
        : null,
    ],
  });

  root.appendChild(h('div', { class: 'control-grid' },
    stateCard, authorityCard, interlockBlock(data), commandBlock(data), overrideCard,
    budgetBlock(data, () => drawPanel(root, ctx, assetId))));

  root.appendChild(h('div', { style: 'margin-top:var(--gap)' },
    commandConsole(data, () => drawPanel(root, ctx, assetId))));

  root.appendChild(h('p', { class: 'card-note', style: 'margin-top:1rem',
    text: `Control presentation generated ${fmtDateTime(data.generated_at)} — SDD 17.4 requires all seven of these elements on every control.` }));
}

export default {
  title: 'Control',
  async mount(root, ctx) {
    if (!ctx.params.assetId) return renderPicker(root, ctx);
    root.appendChild(h('p', { class: 'loading', text: 'Loading control presentation…' }));
    await drawPanel(root, ctx, ctx.params.assetId);
    return {
      async refresh() { await drawPanel(root, ctx, ctx.params.assetId); },
      destroy() {},
    };
  },
};
