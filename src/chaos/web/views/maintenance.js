/* ---------------------------------------------------------------------------
   Maintenance (SDD 18).

   The work-management surface the API has carried since the maintenance
   router landed and the console never showed: plans and what they say is
   due, work orders, inspections, calibrations and spare-part stock. Lists
   first, then the primary actions — raise and complete a work order, record
   an inspection or a calibration, add a spare part, generate the work that
   due plans imply.

   Role gating follows the router, not the role table in the documentation:
   work orders and inspections are operator writes; generating work,
   calibrations and spare parts need a maintainer. Every list names its
   endpoint; an empty table is drawn as empty, never as "all good".
--------------------------------------------------------------------------- */

import {
  card, clear, confirmAction, fmtAge, fmtDateTime, fmtNumber, h, requireRole, resultProblem,
  statusChip, toast,
} from '../app.js';
import { api } from '../api.js';

const PRIORITY_TONE = { urgent: 'alarm', high: 'degraded', normal: 'ok', low: 'no_data' };
const WORK_STATE_TONE = { open: 'degraded', in_progress: 'stale', completed: 'ok', cancelled: 'not_deployed' };
const INSPECTION_TONE = { pass: 'ok', fail: 'alarm', advisory: 'degraded' };
const FILTERS = { workState: 'open', belowMinimum: false };

/* ------------------------------------------------------------ helpers ---- */

function field(label, input, style) {
  return h('label', { style: style || null }, h('span', { text: label }), input);
}

function text(placeholder, extra = {}) {
  return h('input', { type: 'text', class: 'input', placeholder, ...extra });
}

function number(placeholder, extra = {}) {
  return h('input', { type: 'number', class: 'input', placeholder, ...extra });
}

let datalistSerial = 0;

/** A text input with the registry's asset ids as suggestions. Each form gets
 *  its own datalist id, so two forms on the page never share one element. */
function assetInput(assets, placeholder = 'asset id') {
  datalistSerial += 1;
  const id = `maintenance-assets-${datalistSerial}`;
  const input = h('input', { type: 'text', class: 'input', list: id, placeholder });
  return { input, datalist: assets.length
    ? h('datalist', { id }, assets.map((asset) => h('option', { value: asset.asset_id, text: asset.name })))
    : null };
}

function isoOrUndefined(input) {
  return input.value ? new Date(input.value).toISOString() : undefined;
}

function numberOrUndefined(input) {
  return input.value.trim() === '' ? undefined : Number(input.value);
}

/** `key=value` lines → an object, numbers parsed when they look like numbers. */
function parseMeasurements(textarea) {
  const out = {};
  textarea.value.split('\n').forEach((line) => {
    const index = line.indexOf('=');
    if (index < 1) return;
    const key = line.slice(0, index).trim();
    const raw = line.slice(index + 1).trim();
    if (!key) return;
    const asNumber = Number(raw);
    out[key] = raw !== '' && !Number.isNaN(asNumber) ? asNumber : raw;
  });
  return out;
}

/** `part_number,quantity` lines → the parts_used list the API accepts. */
function parseParts(textarea) {
  return textarea.value.split('\n').map((line) => line.trim()).filter(Boolean).map((line) => {
    const [partNumber, quantity] = line.split(',').map((part) => part.trim());
    const entry = { part_number: partNumber };
    if (quantity !== undefined && quantity !== '' && !Number.isNaN(Number(quantity))) entry.quantity = Number(quantity);
    return entry;
  });
}

function outcomeLine(host, response, okText) {
  clear(host);
  if (response.ok) {
    host.appendChild(h('p', { style: 'margin:0' }, statusChip('ok', okText)));
  } else {
    host.appendChild(h('p', { style: 'margin:0' },
      statusChip('alarm', response.forbidden ? 'Refused' : (response.missing ? 'Endpoint missing' : `HTTP ${response.status}`)),
      ' ', h('span', { text: response.error || '' })));
  }
}

function assetLink(assetId, name) {
  if (!assetId) return h('span', { class: 'card-note', style: 'margin:0', text: 'site-wide' });
  return h('a', { class: 'mono', href: `#/assets/${encodeURIComponent(assetId)}`, text: name || assetId });
}

function listProblem(result, what) {
  return resultProblem(result, what);
}

/* ---------------------------------------------------------------- due ---- */

function dueCard(due, reload) {
  if (!due.ok) return card({ title: 'Work due', status: 'unknown', children: listProblem(due, 'the due list') });
  const now = due.data.due_now || [];
  const upcoming = due.data.upcoming || [];
  const outcome = h('div', { style: 'margin-top:.4rem' });
  const button = h('button', { class: 'btn btn-primary btn-sm', type: 'button', text: 'Generate work orders…' });
  button.addEventListener('click', async () => {
    if (!requireRole('maintainer')) return;
    const yes = await confirmAction({
      title: 'Generate work orders from due plans',
      note: `Raises one work order for each plan that is due now (${now.length} at the last evaluation), including alarm-triggered plans with an open alarm. A plan that already has an open work order is skipped and listed as skipped. The API records who asked.`,
      confirmLabel: 'Generate',
    });
    if (!yes) return;
    button.disabled = true;
    const response = await api.generateWorkOrders();
    button.disabled = false;
    clear(outcome);
    if (response.ok) {
      const d = response.data;
      outcome.appendChild(h('p', { style: 'margin:0 0 .3rem' }, statusChip('ok', `${d.created_count} work order${d.created_count === 1 ? '' : 's'} created`)));
      if ((d.skipped || []).length) {
        outcome.appendChild(h('ul', { class: 'provenance' }, d.skipped.map((item) => h('li', { text: `skipped ${item.plan_id}: ${item.reason}` }))));
      }
      toast('success', 'Work generated', `${d.created_count} created, ${(d.skipped || []).length} skipped.`);
      await reload();
    } else {
      outcomeLine(outcome, response, '');
      toast('error', 'Nothing generated', response.error || `HTTP ${response.status}`);
    }
  });
  return card({
    title: 'Work due',
    status: now.length ? 'degraded' : 'ok',
    statusText: now.length ? `${now.length} due now` : 'Nothing due',
    children: [
      now.length
        ? h('ul', null, now.map((item) => h('li', { class: 'provenance', style: 'font-size:.82rem' },
            h('b', { text: item.name }), ' on ', assetLink(item.asset_id), ` — ${item.reason}`)))
        : h('p', { class: 'card-note', style: 'margin-top:0', text: 'No plan is due at the last evaluation.' }),
      upcoming.length
        ? h('details', { class: 'raw' },
            h('summary', { text: `Upcoming within 30 days (${upcoming.length})` }),
            h('ul', null, upcoming.map((item) => h('li', { class: 'provenance' },
              `${item.name} on ${item.asset_id} · due ${fmtDateTime(item.next_due_at)}`))))
        : null,
      h('p', { class: 'card-note', text: `Evaluated ${fmtDateTime(due.data.evaluated_at)} · GET /maintenance/due. Alarm-triggered plans are raised by the alarm engine itself as alarms activate.` }),
      h('div', { class: 'pill-row', style: 'margin-top:.5rem' }, button),
      outcome,
    ],
  });
}

/* -------------------------------------------------------------- plans ---- */

function plansCard(plans) {
  if (!plans.ok) return card({ title: 'Maintenance plans', status: 'unknown', children: listProblem(plans, 'the plan list') });
  const rows = plans.data || [];
  return card({
    title: 'Maintenance plans',
    children: [
      rows.length
        ? h('div', { class: 'table-wrap' },
            h('table', null,
              h('thead', null, h('tr', null,
                h('th', { text: 'Plan' }), h('th', { text: 'Asset' }), h('th', { text: 'Trigger' }),
                h('th', { text: 'Interval' }), h('th', { text: 'Next due' }), h('th', { text: 'Last completed' }), h('th', { text: 'Enabled' }))),
              h('tbody', null, rows.map((plan) => h('tr', null,
                h('td', { text: plan.name }),
                h('td', null, assetLink(plan.asset_id)),
                h('td', null, h('span', { class: 'tag', text: plan.trigger_type }),
                  plan.condition_point ? h('div', { class: 'alarm-key mono', text: plan.condition_point }) : null),
                h('td', { text: plan.interval_days ? `${plan.interval_days} d`
                  : plan.interval_runtime_h ? `${plan.interval_runtime_h} h runtime`
                    : plan.interval_cycles ? `${plan.interval_cycles} cycles` : (plan.season || '—') }),
                h('td', { text: plan.next_due_at ? fmtDateTime(plan.next_due_at) : '—' }),
                h('td', { text: plan.last_completed_at ? fmtDateTime(plan.last_completed_at) : 'never' }),
                h('td', null, plan.enabled ? statusChip('ok', 'yes') : statusChip('not_deployed', 'no')))))))
        : h('div', { class: 'empty' },
            h('h3', null, statusChip('no_data', 'No plans')),
            h('p', { text: 'No maintenance plan is recorded. Plans are created through POST /maintenance/plans; the design package carries none.' })),
      h('p', { class: 'card-note', text: 'GET /maintenance/plans. Creating and deleting plans stays an API call.' }),
    ],
  });
}

/* -------------------------------------------------------- work orders ---- */

function completeForm(order, reload) {
  const note = h('textarea', { rows: '2', placeholder: 'what was done, what was found' });
  const parts = h('textarea', { rows: '2', placeholder: 'parts used, one per line: part_number,quantity' });
  const outcome = h('div');
  const form = h('form', { style: 'margin:.4rem 0', onsubmit: async (event) => {
    event.preventDefault();
    if (!requireRole('operator')) return;
    const yes = await confirmAction({
      title: `Complete work order "${order.title}"`,
      note: 'Marks the work order completed under your name, writes the completion note and the parts used, and advances the plan that raised it. It cannot be reopened.',
      confirmLabel: 'Complete',
    });
    if (!yes) return;
    const body = { parts_used: parseParts(parts) };
    if (note.value.trim()) body.note = note.value.trim();
    const response = await api.completeWorkOrder(order.id, body);
    if (response.ok) {
      toast('success', 'Work order completed', order.title);
      await reload();
    } else {
      outcomeLine(outcome, response, '');
      toast('error', 'Not completed', response.error || `HTTP ${response.status}`);
    }
  } },
    h('div', { class: 'filters', style: 'margin:0 0 .3rem' },
      field('Completion note', note, 'flex:1 1 16rem'),
      field('Parts used', parts, 'flex:1 1 12rem')),
    h('div', { class: 'pill-row' },
      h('button', { class: 'btn btn-primary btn-sm', type: 'submit', text: 'Complete…' })),
    outcome);
  return form;
}

function workOrdersCard(orders, assets, reload) {
  const stateSelect = h('select', { class: 'select-sm',
    onchange: (event) => { FILTERS.workState = event.target.value; reload(); } },
    [['open', 'open'], ['completed', 'completed'], ['', 'all']].map(([value, label]) =>
      h('option', { value, text: label, selected: FILTERS.workState === value })));
  const children = [h('div', { class: 'filters', style: 'margin-bottom:.5rem' }, field('State', stateSelect))];

  if (!orders.ok) {
    children.push(listProblem(orders, 'the work-order list'));
  } else {
    const rows = orders.data || [];
    if (!rows.length) {
      children.push(h('div', { class: 'empty' },
        h('h3', null, statusChip('ok', FILTERS.workState === 'open' ? 'No open work orders' : 'No work orders')),
        h('p', { text: 'None recorded for this filter. Work is raised by due plans, failed inspections, alarm-triggered plans and the form below.' })));
    } else {
      children.push(h('div', { class: 'table-wrap' },
        h('table', null,
          h('caption', { text: `${rows.length} shown, newest first · GET /work-orders` }),
          h('thead', null, h('tr', null,
            h('th', { text: 'Work order' }), h('th', { text: 'Asset' }), h('th', { text: 'Priority' }),
            h('th', { text: 'State' }), h('th', { text: 'Opened' }), h('th', { text: 'Due' }),
            h('th', { text: 'Assigned' }), h('th', { text: '' }))),
          h('tbody', null, rows.flatMap((order) => {
            const formRow = h('tr', { hidden: true }, h('td', { colspan: '8' }, completeForm(order, reload)));
            const toggle = order.state === 'completed' ? null
              : h('button', { class: 'btn btn-sm', type: 'button', text: 'Complete…',
                  onclick: () => { formRow.hidden = !formRow.hidden; } });
            return [
              h('tr', null,
                h('td', null, h('div', { text: order.title }),
                  order.description ? h('div', { class: 'alarm-key', text: order.description }) : null,
                  order.completion_note ? h('div', { class: 'alarm-key', text: `Completed: ${order.completion_note}` }) : null,
                  h('div', { class: 'alarm-key mono', text: order.id })),
                h('td', null, assetLink(order.asset_id, order.asset_name)),
                h('td', null, statusChip(PRIORITY_TONE[order.priority] || 'unknown', order.priority)),
                h('td', null, statusChip(WORK_STATE_TONE[order.state] || 'unknown', order.state)),
                h('td', { text: `${fmtDateTime(order.opened_at)} · ${fmtAge(order.opened_at)}` }),
                h('td', { text: order.due_at ? fmtDateTime(order.due_at) : '—' }),
                h('td', { text: order.assigned_to || '—' }),
                h('td', null, toggle)),
              order.state === 'completed' ? null : formRow,
            ];
          })))));
    }
  }

  // Raise a work order (operator).
  const asset = assetInput(assets);
  const title = text('what needs doing', { required: true });
  const description = h('textarea', { rows: '2', placeholder: 'detail (optional)' });
  const priority = h('select', { class: 'select-sm' },
    ['urgent', 'high', 'normal', 'low'].map((value) => h('option', { value, text: value, selected: value === 'normal' })));
  const due = h('input', { type: 'datetime-local', class: 'input' });
  const assigned = text('person (optional)');
  const outcome = h('div', { style: 'margin-top:.4rem' });
  const form = h('form', { onsubmit: async (event) => {
    event.preventDefault();
    if (!requireRole('operator')) return;
    const body = { title: title.value.trim(), priority: priority.value };
    if (asset.input.value.trim()) body.asset_id = asset.input.value.trim();
    if (description.value.trim()) body.description = description.value.trim();
    if (due.value) body.due_at = isoOrUndefined(due);
    if (assigned.value.trim()) body.assigned_to = assigned.value.trim();
    const response = await api.createWorkOrder(body);
    outcomeLine(outcome, response, response.ok ? `Work order ${response.data.id} opened` : '');
    if (response.ok) {
      toast('success', 'Work order raised', body.title);
      title.value = ''; description.value = ''; asset.input.value = ''; assigned.value = ''; due.value = '';
      await reload();
    } else {
      toast('error', 'Work order not raised', response.error || `HTTP ${response.status}`);
    }
  } },
    h('div', { class: 'filters', style: 'margin:0 0 .3rem' },
      field('Asset', asset.input, 'flex:1 1 14rem'), asset.datalist,
      field('Title', title, 'flex:2 1 16rem'),
      field('Priority', priority),
      field('Due', due),
      field('Assigned to', assigned)),
    h('div', { class: 'filters', style: 'margin:0 0 .3rem' }, field('Description', description, 'flex:1 1 20rem')),
    h('div', { class: 'pill-row' }, h('button', { class: 'btn btn-primary btn-sm', type: 'submit', text: 'Raise work order' })),
    outcome);
  children.push(h('h4', { class: 'card-note', style: 'margin-top:.9rem;text-transform:uppercase;letter-spacing:.06em', text: 'Raise a work order (operator)' }));
  children.push(form);

  return card({ title: 'Work orders', children });
}

/* --------------------------------------------------------- inspections ---- */

function inspectionsCard(inspections, assets, reload) {
  const children = [];
  if (!inspections.ok) {
    children.push(listProblem(inspections, 'the inspection list'));
  } else {
    const rows = inspections.data || [];
    children.push(rows.length
      ? h('div', { class: 'table-wrap' },
          h('table', null,
            h('caption', { text: `${rows.length} shown, newest first · GET /maintenance/inspections` }),
            h('thead', null, h('tr', null,
              h('th', { text: 'Inspected' }), h('th', { text: 'Asset' }), h('th', { text: 'Inspector' }),
              h('th', { text: 'Result' }), h('th', { text: 'Findings' }))),
            h('tbody', null, rows.map((row) => h('tr', null,
              h('td', { text: `${fmtDateTime(row.inspected_at)} · ${fmtAge(row.inspected_at)}` }),
              h('td', null, assetLink(row.asset_id)),
              h('td', { text: row.inspector || '—' }),
              h('td', null, statusChip(INSPECTION_TONE[row.result] || 'unknown', row.result)),
              h('td', null, h('div', { text: row.findings || '—' }),
                Object.keys(row.measurements || {}).length
                  ? h('div', { class: 'alarm-key mono', text: Object.entries(row.measurements).map(([k, v]) => `${k}=${v}`).join(' · ') })
                  : null))))))
      : h('p', { class: 'card-note', style: 'margin-top:0' }, statusChip('no_data', 'No inspections recorded'), ' GET /maintenance/inspections returned nothing.'));
  }

  const asset = assetInput(assets);
  const result = h('select', { class: 'select-sm' },
    ['pass', 'fail', 'advisory'].map((value) => h('option', { value, text: value })));
  const findings = h('textarea', { rows: '2', placeholder: 'what was seen' });
  const measurements = h('textarea', { rows: '2', placeholder: 'one per line: key=value' });
  const workOrder = text('work order id (optional)');
  const outcome = h('div', { style: 'margin-top:.4rem' });
  const form = h('form', { onsubmit: async (event) => {
    event.preventDefault();
    if (!requireRole('operator')) return;
    if (!asset.input.value.trim()) { asset.input.reportValidity(); return; }
    const body = { asset_id: asset.input.value.trim(), result: result.value, measurements: parseMeasurements(measurements) };
    if (findings.value.trim()) body.findings = findings.value.trim();
    if (workOrder.value.trim()) body.work_order_id = workOrder.value.trim();
    const response = await api.recordInspection(body);
    clear(outcome);
    if (response.ok) {
      const d = response.data;
      outcome.appendChild(h('p', { style: 'margin:0' },
        statusChip(INSPECTION_TONE[d.result] || 'ok', `${d.result} recorded`),
        d.follow_up_work_order_id ? ` — follow-up work order ${d.follow_up_work_order_id} raised` : ''));
      toast('success', 'Inspection recorded', d.follow_up_work_order_id ? 'A follow-up work order was raised.' : body.asset_id);
      findings.value = ''; measurements.value = ''; workOrder.value = '';
      await reload();
    } else {
      outcomeLine(outcome, response, '');
      toast('error', 'Inspection not recorded', response.error || `HTTP ${response.status}`);
    }
  } },
    h('div', { class: 'filters', style: 'margin:0 0 .3rem' },
      field('Asset', asset.input, 'flex:1 1 14rem'), asset.datalist,
      field('Result', result),
      field('Work order', workOrder)),
    h('div', { class: 'filters', style: 'margin:0 0 .3rem' },
      field('Findings', findings, 'flex:1 1 16rem'),
      field('Measurements', measurements, 'flex:1 1 12rem')),
    h('div', { class: 'pill-row' }, h('button', { class: 'btn btn-primary btn-sm', type: 'submit', text: 'Record inspection' })),
    outcome);
  children.push(h('h4', { class: 'card-note', style: 'margin-top:.9rem;text-transform:uppercase;letter-spacing:.06em', text: 'Record an inspection (operator)' }));
  children.push(h('p', { class: 'card-note', style: 'margin:0 0 .4rem', text: 'A fail or advisory result raises a follow-up work order by itself.' }));
  children.push(form);
  return card({ title: 'Inspections', children });
}

/* -------------------------------------------------------- calibrations ---- */

function calibrationsCard(calibrations, reload) {
  const children = [];
  if (!calibrations.ok) {
    children.push(listProblem(calibrations, 'the calibration list'));
  } else {
    const rows = calibrations.data || [];
    children.push(rows.length
      ? h('div', { class: 'table-wrap' },
          h('table', null,
            h('caption', { text: `${rows.length} shown, newest first · GET /maintenance/calibrations` }),
            h('thead', null, h('tr', null,
              h('th', { text: 'Calibrated' }), h('th', { text: 'Point' }), h('th', { text: 'By' }),
              h('th', { text: 'Method' }), h('th', { class: 'num', text: 'As found' }), h('th', { class: 'num', text: 'As left' }),
              h('th', { text: 'Next due' }), h('th', { text: 'Valid' }))),
            h('tbody', null, rows.map((row) => h('tr', null,
              h('td', { text: `${fmtDateTime(row.calibrated_at)} · ${fmtAge(row.calibrated_at)}` }),
              h('td', null, h('span', { class: 'mono', text: row.point_id })),
              h('td', { text: row.calibrated_by || '—' }),
              h('td', { text: row.method || '—' }),
              h('td', { class: 'num', text: row.pre_error === null || row.pre_error === undefined ? '—' : fmtNumber(row.pre_error, 3) }),
              h('td', { class: 'num', text: row.post_error === null || row.post_error === undefined ? '—' : fmtNumber(row.post_error, 3) }),
              h('td', { text: row.next_due_at ? fmtDateTime(row.next_due_at) : '—' }),
              h('td', null, row.valid ? statusChip('ok', 'valid') : statusChip('alarm', 'invalid')))))))
      : h('p', { class: 'card-note', style: 'margin-top:0' }, statusChip('no_data', 'No calibrations recorded'), ' GET /maintenance/calibrations returned nothing.'));
  }

  const pointId = text('<asset_id>/<point_name>', { required: true });
  const method = text('method');
  const reference = text('reference standard');
  const pre = number('as-found error', { step: 'any' });
  const post = number('as-left error', { step: 'any' });
  const nextDue = h('input', { type: 'datetime-local', class: 'input' });
  const valid = h('input', { type: 'checkbox', checked: true });
  const outcome = h('div', { style: 'margin-top:.4rem' });
  const form = h('form', { onsubmit: async (event) => {
    event.preventDefault();
    if (!requireRole('maintainer')) return;
    const body = { point_id: pointId.value.trim(), valid: valid.checked, curve: {} };
    if (method.value.trim()) body.method = method.value.trim();
    if (reference.value.trim()) body.reference_standard = reference.value.trim();
    if (numberOrUndefined(pre) !== undefined) body.pre_error = numberOrUndefined(pre);
    if (numberOrUndefined(post) !== undefined) body.post_error = numberOrUndefined(post);
    if (nextDue.value) body.next_due_at = isoOrUndefined(nextDue);
    const response = await api.recordCalibration(body);
    outcomeLine(outcome, response, response.ok ? `Calibration ${response.data.calibration_id} recorded` : '');
    if (response.ok) {
      toast('success', 'Calibration recorded', body.point_id);
      await reload();
    } else {
      toast('error', 'Calibration not recorded', response.error || `HTTP ${response.status}`);
    }
  } },
    h('div', { class: 'filters', style: 'margin:0 0 .3rem' },
      field('Point', pointId, 'flex:2 1 18rem'),
      field('Method', method), field('Reference', reference)),
    h('div', { class: 'filters', style: 'margin:0 0 .3rem' },
      field('As found', pre), field('As left', post), field('Next due', nextDue),
      h('label', { style: 'flex-direction:row;align-items:center;gap:.4rem' }, valid, h('span', { text: 'Valid' }))),
    h('div', { class: 'pill-row' }, h('button', { class: 'btn btn-primary btn-sm', type: 'submit', text: 'Record calibration' })),
    outcome);
  children.push(h('h4', { class: 'card-note', style: 'margin-top:.9rem;text-transform:uppercase;letter-spacing:.06em', text: 'Record a calibration (maintainer)' }));
  children.push(h('p', { class: 'card-note', style: 'margin:0 0 .4rem', text: 'As-found and as-left are the two numbers that tell you next year whether the sensor is drifting (commissioning step 3).' }));
  children.push(form);
  return card({ title: 'Calibrations', children });
}

/* --------------------------------------------------------- spare parts ---- */

function sparePartsCard(parts, reload) {
  const below = h('input', { type: 'checkbox', checked: FILTERS.belowMinimum,
    onchange: (event) => { FILTERS.belowMinimum = event.target.checked; reload(); } });
  const children = [h('div', { class: 'filters', style: 'margin-bottom:.5rem' },
    h('label', { style: 'flex-direction:row;align-items:center;gap:.4rem' }, below, h('span', { text: 'Only parts at or below minimum' })))];
  if (!parts.ok) {
    children.push(listProblem(parts, 'the spare-part list'));
  } else {
    const rows = parts.data || [];
    const short = rows.filter((row) => row.below_minimum).length;
    children.push(rows.length
      ? h('div', { class: 'table-wrap' },
          h('table', null,
            h('caption', { text: `${rows.length} shown · ${short} at or below minimum · GET /maintenance/spare-parts` }),
            h('thead', null, h('tr', null,
              h('th', { text: 'Part' }), h('th', { text: 'Description' }), h('th', { class: 'num', text: 'On hand' }),
              h('th', { class: 'num', text: 'Minimum' }), h('th', { text: 'Stock' }), h('th', { text: 'Location' }), h('th', { text: 'Linked assets' }))),
            h('tbody', null, rows.map((row) => h('tr', null,
              h('td', null, h('span', { class: 'mono', text: row.part_number })),
              h('td', { text: row.description }),
              h('td', { class: 'num', text: String(row.quantity_on_hand) }),
              h('td', { class: 'num', text: String(row.minimum_quantity) }),
              h('td', null, row.below_minimum ? statusChip('degraded', 'reorder') : statusChip('ok', 'stocked')),
              h('td', { text: row.location || '—' }),
              h('td', null, (row.linked_assets || []).length
                ? row.linked_assets.map((assetId) => h('span', { class: 'tag' }, assetLink(assetId)))
                : '—'))))))
      : h('p', { class: 'card-note', style: 'margin-top:0' }, statusChip('no_data', FILTERS.belowMinimum ? 'Nothing below minimum' : 'No spare parts recorded'),
          FILTERS.belowMinimum ? ' Every recorded part is above its minimum.' : ' GET /maintenance/spare-parts returned nothing.'));
  }

  const partNumber = text('part number', { required: true });
  const description = text('description', { required: true });
  const quantity = number('on hand', { min: '0', step: '1', value: '0' });
  const minimum = number('minimum', { min: '0', step: '1', value: '0' });
  const location = text('where it is kept');
  const supplier = text('supplier');
  const unitCost = number('unit cost', { step: 'any' });
  const linked = text('linked asset ids, comma separated');
  const outcome = h('div', { style: 'margin-top:.4rem' });
  const form = h('form', { onsubmit: async (event) => {
    event.preventDefault();
    if (!requireRole('maintainer')) return;
    const body = {
      part_number: partNumber.value.trim(),
      description: description.value.trim(),
      quantity_on_hand: Number(quantity.value) || 0,
      minimum_quantity: Number(minimum.value) || 0,
      linked_assets: linked.value.split(',').map((item) => item.trim()).filter(Boolean),
    };
    if (location.value.trim()) body.location = location.value.trim();
    if (supplier.value.trim()) body.supplier = supplier.value.trim();
    if (numberOrUndefined(unitCost) !== undefined) body.unit_cost = numberOrUndefined(unitCost);
    const response = await api.createSparePart(body);
    outcomeLine(outcome, response, response.ok ? `Part ${response.data.part_number} added` : '');
    if (response.ok) {
      toast('success', 'Spare part added', body.part_number);
      partNumber.value = ''; description.value = ''; linked.value = '';
      await reload();
    } else {
      toast('error', 'Spare part not added', response.error || `HTTP ${response.status}`);
    }
  } },
    h('div', { class: 'filters', style: 'margin:0 0 .3rem' },
      field('Part number', partNumber), field('Description', description, 'flex:2 1 16rem'),
      field('On hand', quantity), field('Minimum', minimum)),
    h('div', { class: 'filters', style: 'margin:0 0 .3rem' },
      field('Location', location), field('Supplier', supplier), field('Unit cost', unitCost),
      field('Linked assets', linked, 'flex:1 1 16rem')),
    h('div', { class: 'pill-row' }, h('button', { class: 'btn btn-primary btn-sm', type: 'submit', text: 'Add spare part' })),
    outcome);
  children.push(h('h4', { class: 'card-note', style: 'margin-top:.9rem;text-transform:uppercase;letter-spacing:.06em', text: 'Add a spare part (maintainer)' }));
  children.push(h('p', { class: 'card-note', style: 'margin:0 0 .4rem', text: 'Stock is set when the part is recorded; the API has no separate stock adjustment, so a changed quantity is a new record of the same part number until one exists.' }));
  children.push(form);
  return card({ title: 'Spare parts', children });
}

/* ---------------------------------------------------------------- page ---- */

async function draw(root, ctx) {
  const [due, plans, orders, inspections, calibrations, parts, assets] = await Promise.all([
    api.maintenanceDue(30),
    api.maintenancePlans(),
    api.workOrders({ state: FILTERS.workState || undefined, limit: 200 }),
    api.inspections({ limit: 100 }),
    api.calibrations({ limit: 100 }),
    api.spareParts(FILTERS.belowMinimum),
    api.assets({ limit: 500 }),
  ]);
  const assetList = assets.ok ? (assets.data.items || []) : [];
  const reload = () => draw(root, ctx);

  clear(root);
  root.appendChild(h('div', { class: 'page-head' },
    h('div', null,
      h('h2', { text: 'Maintenance' }),
      h('p', { class: 'lede', text: 'Plans and what they say is due, work orders, inspections, calibrations and spare-part stock (SDD 18). Lists come straight from the maintenance API; the forms are its primary writes.' })),
    h('div', { class: 'pill-row' },
      h('button', { class: 'btn btn-sm', onclick: reload, text: 'Reload' }))));

  if (!assets.ok) {
    root.appendChild(h('p', { class: 'card-note', text: 'The asset list could not be loaded, so the asset fields below have no suggestions; an asset id can still be typed.' }));
  }
  root.appendChild(h('div', { class: 'grid grid-2' }, dueCard(due, reload), plansCard(plans)));
  root.appendChild(h('div', { style: 'margin-top:var(--gap)' }, workOrdersCard(orders, assetList, reload)));
  root.appendChild(h('div', { class: 'grid grid-2', style: 'margin-top:var(--gap)' },
    inspectionsCard(inspections, assetList, reload), calibrationsCard(calibrations, reload)));
  root.appendChild(h('div', { style: 'margin-top:var(--gap)' }, sparePartsCard(parts, reload)));
  root.appendChild(h('p', { class: 'card-note', style: 'margin-top:1rem',
    text: `Loaded ${fmtDateTime(new Date().toISOString())}. Work orders and inspections are operator writes; generating work, calibrations and spare parts need the maintainer role — the gates here are the API’s own.` }));
}

export default {
  title: 'Maintenance',
  async mount(root, ctx) {
    root.appendChild(h('p', { class: 'loading', text: 'Loading maintenance records…' }));
    await draw(root, ctx);
    return {
      // No polling: a half-filled form must not be redrawn under the operator.
      async refresh() {},
      destroy() {},
    };
  },
};
