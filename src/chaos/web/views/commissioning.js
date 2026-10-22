/* ---------------------------------------------------------------------------
   Commissioning (SDD 19).

   Every subsystem passes a twelve-step sequence before it goes under automatic
   supervisory control, and the platform enforces it: automatic control on a
   binding is refused, with a reason, until steps 1–8 and 9 have passed for the
   asset. This screen is the form for that record — pick an asset, see where it
   stands, record a step with what was injected and what was observed, and
   commission a point binding when the gate allows it.

   Nothing on this screen performs a test. Steps 1, 3, 4, 5, 8 and 11 happen at
   the equipment, with tools, by a person; the screen records that they did, and
   it draws a `blocked` or `not_run` as exactly that rather than as progress.
--------------------------------------------------------------------------- */

import {
  askReason, card, clear, fmtAge, fmtDateTime, h, kv, requireRole, resultProblem, statusChip, toast,
} from '../app.js';
import { api } from '../api.js';

/** The four outcomes the API accepts, drawn so `blocked` is never read as pass. */
const RESULT_TONE = { pass: 'ok', fail: 'alarm', blocked: 'degraded', not_run: 'no_data' };
const RESULTS = ['pass', 'fail', 'blocked', 'not_run'];

/** Binding statuses the registry loader and the commissioning module write. */
const BINDING_TONE = {
  commissioned: 'ok', verified: 'ok', active: 'ok', bound: 'stale',
  tbd: 'design_only', unbound: 'no_data',
};

const PICKER = { q: '', domain: '' };

/* ---------------------------------------------------------------- picker --- */

async function renderPicker(root, ctx) {
  clear(root);
  root.appendChild(h('div', { class: 'page-head' },
    h('div', null,
      h('h2', { text: 'Commissioning' }),
      h('p', { class: 'lede', text: 'Pick an asset to see its twelve-step record, record a step outcome, or commission a point binding. The platform refuses automatic control until steps 1–8 and 9 have passed; this is where that record is kept.' }))));

  const body = h('div');
  root.appendChild(body);
  const result = await api.assets({ limit: 500 });
  if (!result.ok) {
    body.appendChild(resultProblem(result, 'the asset list'));
    return { async refresh() {}, destroy() {} };
  }
  const all = result.data.items || [];
  const domains = Array.from(new Set(all.map((asset) => asset.domain))).sort();

  function draw() {
    clear(body);
    const needle = PICKER.q.trim().toLowerCase();
    const items = all.filter((asset) =>
      (!PICKER.domain || asset.domain === PICKER.domain)
      && (!needle || asset.asset_id.toLowerCase().includes(needle) || asset.name.toLowerCase().includes(needle)));
    body.appendChild(h('form', { class: 'filters', onsubmit: (event) => { event.preventDefault(); draw(); } },
      h('label', null, h('span', { text: 'Search' }),
        h('input', { type: 'search', class: 'input', placeholder: 'asset id or name', value: PICKER.q,
          oninput: (event) => { PICKER.q = event.target.value; } })),
      h('label', null, h('span', { text: 'Domain' }),
        h('select', { class: 'select-sm', onchange: (event) => { PICKER.domain = event.target.value; draw(); } },
          h('option', { value: '', text: 'All domains' }),
          domains.map((domain) => h('option', { value: domain, text: domain, selected: PICKER.domain === domain })))),
      h('button', { class: 'btn btn-sm', type: 'submit', text: 'Apply' })));
    if (!items.length) {
      body.appendChild(h('div', { class: 'empty' },
        h('h3', { text: 'No assets match' }),
        h('p', { text: all.length ? 'Adjust the filters above.' : 'The registry is empty. Load the design package first.' })));
      return;
    }
    body.appendChild(h('div', { class: 'table-wrap' },
      h('table', null,
        h('caption', { text: `${items.length} asset${items.length === 1 ? '' : 's'} — the record is per asset, so each row has its own twelve steps` }),
        h('thead', null, h('tr', null,
          h('th', { text: 'Asset' }), h('th', { text: 'Domain' }), h('th', { text: 'Status' }),
          h('th', { text: 'Criticality' }), h('th', { text: 'Control authority' }), h('th', { text: '' }))),
        h('tbody', null, items.map((asset) => h('tr', null,
          h('td', null, h('a', { href: `#/commissioning/${encodeURIComponent(asset.asset_id)}`, text: asset.name }),
            h('div', { class: 'alarm-key mono', text: asset.asset_id })),
          h('td', { text: asset.domain }),
          h('td', { text: asset.status }),
          h('td', { text: asset.criticality }),
          h('td', { text: asset.control_authority }),
          h('td', null, h('a', { class: 'btn btn-sm', href: `#/commissioning/${encodeURIComponent(asset.asset_id)}`, text: 'Open' }))))))));
  }
  draw();
  return { async refresh() {}, destroy() {} };
}

/* ---------------------------------------------------------------- status --- */

function resultChip(result) {
  return statusChip(RESULT_TONE[result] || 'unknown', result ? result.replace('_', ' ') : 'unknown');
}

function statusCard(status) {
  if (!status.ok) {
    return card({ title: 'Commissioning status', status: 'unknown', children: resultProblem(status, 'the commissioning status') });
  }
  const s = status.data;
  const tone = s.fully_commissioned ? 'ok'
    : (s.steps_failed || []).length ? 'alarm'
      : s.supervisory_control_permitted ? 'ok'
        : s.steps_passed ? 'degraded' : 'design_only';
  const statusText = s.fully_commissioned ? 'Fully commissioned'
    : s.supervisory_control_permitted ? 'Supervisory control permitted'
      : (s.steps_failed || []).length ? 'Steps failed'
        : s.steps_passed ? 'In progress' : 'Not started';
  return card({
    title: 'Commissioning status',
    status: tone,
    statusText,
    children: [
      h('div', { class: 'readout' },
        h('span', { class: 'value num', text: `${s.steps_passed} / ${s.steps_total}` }),
        h('span', { class: 'unit', text: 'steps passed' })),
      kv([
        ['Prerequisites (1–8)', s.prerequisites_met ? statusChip('ok', 'all passed') : statusChip('design_only', 'not yet')],
        ['Supervisory control (9)', s.supervisory_control_permitted
          ? statusChip('ok', 'permitted') : statusChip('design_only', 'refused until 1–8 and 9 pass')],
        ['Fully commissioned', s.fully_commissioned ? statusChip('ok', 'yes') : statusChip('design_only', 'no')],
        ['Failed steps', (s.steps_failed || []).length
          ? h('span', null, statusChip('alarm', s.steps_failed.join(', ')), ' — stop, fix, re-run')
          : 'none'],
      ]),
      (s.outstanding || []).length
        ? h('div', { style: 'margin-top:.6rem' },
            h('div', { class: 'card-note', style: 'margin:0 0 .25rem', text: 'Outstanding' }),
            h('div', { class: 'pill-row' }, s.outstanding.map((item) =>
              h('span', { class: `chip chip-${RESULT_TONE[item.result] || 'unknown'}`, title: item.result },
                h('span', { class: 'chip-glyph', 'aria-hidden': 'true', text: String(item.step) }),
                item.step_name))))
        : h('p', { class: 'card-note', text: 'Every step has passed.' }),
      h('p', { class: 'card-note', text: 'Source: GET /commissioning/{asset_id}/status. Only pass counts; blocked and not_run are outstanding.' }),
    ],
  });
}

function recordsTable(records) {
  if (!records.ok) return resultProblem(records, 'the commissioning records');
  const rows = records.data || [];
  if (!rows.length) {
    return h('div', { class: 'empty' },
      h('h3', null, statusChip('no_data', 'No records yet')),
      h('p', { text: 'The twelve not-yet-run records are created the first time a step is recorded for this asset.' }));
  }
  return h('div', { class: 'table-wrap' },
    h('table', null,
      h('thead', null, h('tr', null,
        h('th', { text: '#' }), h('th', { text: 'Step' }), h('th', { text: 'Result' }),
        h('th', { text: 'Performed' }), h('th', { text: 'Observed' }))),
      h('tbody', null, rows.map((row) => h('tr', null,
        h('td', { class: 'num', text: String(row.step) }),
        h('td', null, h('span', { class: 'mono', text: row.step_name })),
        h('td', null, resultChip(row.result)),
        h('td', { text: row.performed_by ? `${row.performed_by} · ${fmtDateTime(row.performed_at)}` : '—' }),
        h('td', null,
          h('div', { text: row.observed || '—' }),
          (row.preconditions || row.injected_condition || row.expected_sequence || (row.evidence || []).length)
            ? h('details', { class: 'raw' },
                h('summary', { text: 'Test record' }),
                kv([
                  ['Preconditions', row.preconditions || '—'],
                  ['Injected condition', row.injected_condition || '—'],
                  ['Expected sequence', row.expected_sequence || '—'],
                  ['Evidence', (row.evidence || []).length
                    ? h('ul', null, row.evidence.map((item) => h('li', { class: 'mono', text: String(item) })))
                    : '—'],
                ]))
            : null))))));
}

/* -------------------------------------------------------- record a step --- */

function stepForm(assetId, steps, reload) {
  const stepOptions = steps.ok ? steps.data : [];
  const stepSelect = h('select', { class: 'select-sm' },
    stepOptions.map((step) => h('option', { value: String(step.step), text: `${step.step} — ${step.step_name}` })));
  const resultSelect = h('select', { class: 'select-sm' },
    RESULTS.map((value) => h('option', { value, text: value })));
  const preconditions = h('input', { type: 'text', class: 'input', placeholder: 'state before the test' });
  const injected = h('input', { type: 'text', class: 'input', placeholder: 'what was done to it' });
  const expected = h('input', { type: 'text', class: 'input', placeholder: 'what should have happened' });
  const observed = h('textarea', { rows: '2', placeholder: 'what actually happened (required for pass or fail)' });
  const evidence = h('textarea', { rows: '2', placeholder: 'one reference per line, e.g. photo:IMG_4471' });
  const button = h('button', { class: 'btn btn-primary btn-sm', type: 'submit', text: 'Record step' });
  const outcome = h('div', { style: 'margin-top:.5rem' });

  const form = h('form', { onsubmit: async (event) => {
    event.preventDefault();
    if (!requireRole('maintainer')) return;
    if (!stepOptions.length) { toast('warn', 'Steps unavailable', 'The step list could not be loaded.'); return; }
    const result = resultSelect.value;
    if ((result === 'pass' || result === 'fail') && !observed.value.trim()) {
      observed.setCustomValidity('Say what was observed — a pass with no observation is not a record.');
      observed.reportValidity();
      return;
    }
    const body = { step: Number(stepSelect.value), result };
    if (preconditions.value.trim()) body.preconditions = preconditions.value.trim();
    if (injected.value.trim()) body.injected_condition = injected.value.trim();
    if (expected.value.trim()) body.expected_sequence = expected.value.trim();
    if (observed.value.trim()) body.observed = observed.value.trim();
    const lines = evidence.value.split('\n').map((line) => line.trim()).filter(Boolean);
    if (lines.length) body.evidence = lines;
    button.disabled = true;
    const response = await api.recordCommissioningStep(assetId, body);
    button.disabled = false;
    clear(outcome);
    if (response.ok) {
      const d = response.data;
      outcome.appendChild(h('p', { style: 'margin:0' },
        resultChip(d.result), ` step ${d.step} (${d.step_name}) recorded · ${d.status.steps_passed}/${d.status.steps_total} passed`
          + (d.status.supervisory_control_permitted ? ' · supervisory control permitted' : '')));
      toast('success', 'Step recorded', `${d.step_name}: ${d.result}`);
      observed.value = ''; evidence.value = ''; preconditions.value = ''; injected.value = ''; expected.value = '';
      await reload();
    } else if (response.missing) {
      toast('warn', 'Commissioning endpoint not available', 'This node does not expose POST /commissioning/{asset}/steps.');
    } else {
      outcome.appendChild(h('p', { style: 'margin:0' },
        statusChip('alarm', response.forbidden ? 'Refused' : `HTTP ${response.status}`), ' ', h('span', { text: response.error || '' })));
      toast('error', 'Step not recorded', response.error || `HTTP ${response.status}`);
    }
  } },
    h('div', { class: 'filters', style: 'margin-bottom:.4rem' },
      h('label', null, h('span', { text: 'Step' }), stepSelect),
      h('label', null, h('span', { text: 'Result' }), resultSelect)),
    h('div', { class: 'filters', style: 'margin-bottom:.4rem' },
      h('label', { style: 'flex:1 1 14rem' }, h('span', { text: 'Preconditions' }), preconditions),
      h('label', { style: 'flex:1 1 14rem' }, h('span', { text: 'Injected condition' }), injected),
      h('label', { style: 'flex:1 1 14rem' }, h('span', { text: 'Expected sequence' }), expected)),
    h('div', { class: 'filters', style: 'margin-bottom:.4rem' },
      h('label', { style: 'flex:1 1 18rem' }, h('span', { text: 'Observed' }), observed),
      h('label', { style: 'flex:1 1 14rem' }, h('span', { text: 'Evidence' }), evidence)),
    h('div', { class: 'pill-row' }, button),
    outcome);
  observed.addEventListener('input', () => observed.setCustomValidity(''));

  return card({
    title: 'Record a step outcome',
    children: [
      h('p', { class: 'card-note', style: 'margin-top:0', text: 'SDD 39: each test record carries its preconditions, the injected condition, the expected sequence, what was observed and the result. Recording blocked is honest — it says the step could not be run. Fields left blank keep what the record already holds. Needs the maintainer role; the API records who performed it.' }),
      form,
    ],
  });
}

/* ------------------------------------------------------ point bindings --- */

async function bindingsCard(asset, points, reload) {
  if (!points.ok) {
    return card({ title: 'Point bindings', status: 'unknown', children: resultProblem(points, 'the asset’s points') });
  }
  const list = points.data || [];
  if (!list.length) {
    return card({ title: 'Point bindings', status: 'no_points',
      children: h('p', { class: 'card-note', text: 'This asset has no points, so there is no binding to commission.' }) });
  }
  // The binding sits on the point detail route; one request per point.
  const details = await Promise.all(list.map((point) => api.point(asset.asset_id, point.point_name)));

  const rows = list.map((point, index) => {
    const detail = details[index];
    const binding = detail.ok ? detail.data.binding : null;
    const allowBox = h('input', { type: 'checkbox', title: 'Allow automatic control' });
    const button = h('button', { class: 'btn btn-sm', type: 'button', text: 'Commission…', disabled: !binding });
    const cell = h('td', null,
      binding
        ? h('div', { class: 'pill-row', style: 'align-items:center' },
            h('label', { class: 'card-note', style: 'margin:0;display:inline-flex;align-items:center;gap:.3rem' },
              allowBox, 'automatic control'),
            button)
        : h('span', { class: 'card-note', style: 'margin:0', text: detail.ok ? 'no binding record' : (detail.error || 'binding unknown') }));
    const outcome = h('div', { class: 'card-note', style: 'margin:.2rem 0 0' });
    cell.appendChild(outcome);

    button.addEventListener('click', async () => {
      if (!requireRole('maintainer')) return;
      const allow = allowBox.checked;
      const reason = await askReason({
        title: `Commission binding ${point.point_id}`,
        note: allow
          ? 'This marks the binding commissioned AND allows automatic control through it. The API refuses the second part, with a reason, unless steps 1–8 and 9 have passed for this asset. Enable one binding at a time.'
          : 'This marks the binding commissioned without allowing automatic control; the point can be trusted as an input but not actuated.',
        confirmLabel: allow ? 'Commission and allow automatic control' : 'Commission binding',
        danger: allow,
      });
      if (!reason) return;
      button.disabled = true;
      const response = await api.commissionBinding(point.point_id, { allow_automatic_control: allow, reason });
      button.disabled = false;
      clear(outcome);
      if (response.ok) {
        const d = response.data;
        toast('success', 'Binding commissioned',
          `${d.point_id}: ${d.binding_status}${d.automatic_control_allowed ? ', automatic control allowed' : ''}`);
        await reload();
      } else {
        outcome.appendChild(h('span', null,
          statusChip('alarm', response.status === 409 ? 'Refused by the gate' : (response.forbidden ? 'Refused' : `HTTP ${response.status}`)),
          ' ', h('span', { text: response.error || '' })));
        toast(response.status === 409 ? 'warn' : 'error', 'Binding not commissioned', response.error || `HTTP ${response.status}`);
      }
    });

    return h('tr', null,
      h('td', null, h('span', { class: 'mono', text: point.point_name }),
        point.control_capable ? h('div', null, statusChip('ok', 'control-capable')) : null),
      h('td', null, binding
        ? statusChip(BINDING_TONE[binding.binding_status] || 'unknown', binding.binding_status)
        : statusChip('no_data', 'none')),
      h('td', { text: binding ? `${binding.source_protocol || '—'} ${binding.source_address || binding.mqtt_topic || ''}`.trim() : '—' }),
      h('td', null, binding
        ? (binding.automatic_control_allowed ? statusChip('ok', 'allowed') : statusChip('design_only', 'not allowed'))
        : '—'),
      h('td', { text: binding && binding.commissioned_by ? `${binding.commissioned_by} · ${fmtAge(binding.commissioned_at)}` : '—' }),
      cell);
  });

  return card({
    title: 'Point bindings',
    children: [
      h('p', { class: 'card-note', style: 'margin-top:0', text: 'Commissioning a binding records who verified its address and when. Allowing automatic control is the per-point safety gate (SDD 47) and the API refuses it until the asset’s prerequisites have passed — that refusal is the point of the endpoint. The platform-wide physical-control gate is separate and is not touched here.' }),
      h('div', { class: 'table-wrap' },
        h('table', null,
          h('thead', null, h('tr', null,
            h('th', { text: 'Point' }), h('th', { text: 'Binding' }), h('th', { text: 'Source' }),
            h('th', { text: 'Automatic control' }), h('th', { text: 'Commissioned' }), h('th', { text: '' }))),
          h('tbody', null, rows))),
    ],
  });
}

/* ------------------------------------------------------------- asset page --- */

async function drawAsset(root, ctx, assetId) {
  clear(root);
  root.appendChild(h('p', { class: 'loading', text: `Loading the commissioning record for ${assetId}…` }));
  const [asset, status, records, steps, points] = await Promise.all([
    api.asset(assetId), api.commissioningStatus(assetId), api.commissioningRecords(assetId),
    api.commissioningSteps(), api.assetPoints(assetId),
  ]);
  const reload = () => drawAsset(root, ctx, assetId);

  clear(root);
  root.appendChild(h('div', { class: 'page-head' },
    h('div', null,
      h('p', { class: 'card-note', style: 'margin:0' }, h('a', { href: '#/commissioning', text: '← Commissioning' })),
      h('h2', { text: asset.ok ? asset.data.name : assetId }),
      h('p', { class: 'lede mono', text: assetId })),
    h('div', { class: 'pill-row' },
      h('a', { class: 'btn btn-sm', href: `#/control/${encodeURIComponent(assetId)}`, text: 'Control panel' }),
      h('a', { class: 'btn btn-sm', href: `#/assets/${encodeURIComponent(assetId)}`, text: 'Registry record' }),
      h('button', { class: 'btn btn-sm', onclick: reload, text: 'Reload' }))));

  if (!asset.ok) {
    root.appendChild(resultProblem(asset, `asset ${assetId}`));
    return;
  }
  const a = asset.data;
  root.appendChild(h('div', { class: 'pill-row', style: 'margin-bottom:var(--gap)' },
    h('span', { class: 'tag', text: a.domain }),
    h('span', { class: 'tag', text: a.asset_class }),
    h('span', { class: 'tag', text: `status: ${a.status}` }),
    h('span', { class: 'tag', text: `authority: ${a.control_authority}` }),
    (a.open_fields || []).length
      ? statusChip('design_only', `${a.open_fields.length} unresolved fields — not ready to commission`)
      : null));

  root.appendChild(h('div', { class: 'grid grid-2' },
    statusCard(status),
    stepForm(assetId, steps, reload)));
  root.appendChild(h('h3', { style: 'margin-top:1.3rem;font-size:.95rem', text: 'The twelve steps' }));
  root.appendChild(recordsTable(records));
  root.appendChild(h('div', { style: 'margin-top:var(--gap)' }, await bindingsCard(a, points, reload)));
  root.appendChild(h('p', { class: 'card-note', style: 'margin-top:1rem',
    text: `Loaded ${fmtDateTime(new Date().toISOString())}. Nothing here performs a test: steps 1, 3, 4, 5, 8 and 11 happen at the equipment, by a person.` }));
}

export default {
  title: 'Commissioning',
  async mount(root, ctx) {
    if (!ctx.params.assetId) return renderPicker(root, ctx);
    await drawAsset(root, ctx, ctx.params.assetId);
    return {
      // No polling: a form in progress must not be redrawn under the operator.
      async refresh() {},
      destroy() {},
    };
  },
};
