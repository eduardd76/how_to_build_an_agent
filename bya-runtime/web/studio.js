'use strict';
// BYA Diagram Studio: draw an agent as blocks, attachments and flow wires; the local runtime validates and runs it.

const $ = s => document.querySelector(s);
const CATEGORIES = [['trigger', 'Triggers'], ['agent', 'Agent'], ['tool', 'Tools (attach to an agent)'], ['memory', 'Memory (attach to an agent)'], ['guard', 'Guardrails'], ['output', 'Outputs']];
const COLORS = {trigger: 'var(--cat-trigger)', agent: 'var(--cat-agent)', tool: 'var(--cat-tool)', memory: 'var(--cat-memory)', guard: 'var(--cat-guard)', output: 'var(--cat-output)'};
const ID_PREFIX = {'trigger': 'start', 'agent': 'agent', 'tool': 'tool', 'memory': 'memory', 'guard.output_check': 'check', 'guard.approval': 'approval', 'guard.policy': 'policy', 'guard.redact': 'redact', 'output': 'out'};
const STORE_KEY = 'bya-studio-diagram-v1';
const BLOCK_W = 176;

let types = {};              // type -> catalogue entry
let diagram = blank();
let selected = null;         // {kind: 'block', id} | {kind: 'wire', list: 'flow'|'attachments', index}
let violations = [];
let token = null;
let running = false;
let currentRun = null;

function blank() {
  return {schema_version: '1.0', name: 'Untitled agent', blocks: [], attachments: [], flow: [], layout: {}};
}
function esc(s) {
  return String(s ?? '').replace(/[&<>"']/g, c => ({'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'}[c]));
}
function toast(text) {
  const t = $('#toast'); t.textContent = text; t.hidden = false;
  clearTimeout(toast.timer); toast.timer = setTimeout(() => { t.hidden = true; }, 4200);
}
async function api(path, body) {
  const opts = body === undefined ? {} : {method: 'POST', headers: {'Content-Type': 'application/json', 'X-BYA-Token': token}, body: JSON.stringify(body)};
  const res = await fetch(path, opts);
  const data = await res.json();
  if (!res.ok) throw new Error(data.error || 'Request failed.');
  return data;
}
const block = id => diagram.blocks.find(b => b.id === id);
const spec = b => types[b?.type];
const category = b => spec(b)?.category;

// ---------- persistence ----------
function save() {
  diagram.name = $('#diagram-name').value.trim() || 'Untitled agent';
  try { localStorage.setItem(STORE_KEY, JSON.stringify(diagram)); } catch {}
}
function setDiagram(doc) {
  if (!doc || !Array.isArray(doc.blocks)) throw new Error('Not a diagram file.');
  diagram = {...blank(), ...structuredClone(doc)};
  diagram.layout = diagram.layout || {};
  diagram.blocks.forEach((b, i) => { b.config = b.config || {}; diagram.layout[b.id] ??= freeSpot(i); });
  $('#diagram-name').value = diagram.name;
  selected = null;
  evalReport = null;
  if (!$('#evals-panel').hidden) renderEvals();
  renderAll();
  changed();
}

// ---------- palette ----------
function renderPalette() {
  const byCat = {};
  Object.values(types).forEach(t => (byCat[t.category] ??= []).push(t));
  $('#palette').innerHTML = CATEGORIES.map(([cat, title]) => `<h3>${title}</h3>` + (byCat[cat] || []).map(t =>
    `<button class="pal-item" style="--c:${COLORS[cat]}" data-type="${esc(t.type)}"><strong>${esc(t.label)}</strong><span>${esc(t.description)}</span></button>`
  ).join('')).join('');
  document.querySelectorAll('.pal-item').forEach(el => {
    el.addEventListener('pointerdown', e => paletteDrag(e, el.dataset.type));
    el.addEventListener('keydown', e => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); addBlock(el.dataset.type); } });
  });
}
function paletteDrag(e, type) {
  if (e.button !== 0) return;
  const start = {x: e.clientX, y: e.clientY};
  let ghost = null;
  const move = ev => {
    if (!ghost && Math.hypot(ev.clientX - start.x, ev.clientY - start.y) > 6) {
      ghost = document.createElement('div');
      ghost.className = 'block';
      ghost.style.cssText = `position:fixed;pointer-events:none;opacity:.85;z-index:5;--c:${COLORS[types[type].category]}`;
      ghost.innerHTML = `<span class="cat">${esc(types[type].category)}</span><span class="title">${esc(types[type].label)}</span>`;
      document.body.appendChild(ghost);
    }
    if (ghost) { ghost.style.left = ev.clientX - 60 + 'px'; ghost.style.top = ev.clientY - 20 + 'px'; }
  };
  const up = ev => {
    window.removeEventListener('pointermove', move);
    window.removeEventListener('pointerup', up);
    if (!ghost) { addBlock(type); return; }
    ghost.remove();
    const r = $('#canvas').getBoundingClientRect(), w = $('#canvas-wrap').getBoundingClientRect();
    if (ev.clientX >= w.left && ev.clientX <= w.right && ev.clientY >= w.top && ev.clientY <= w.bottom)
      addBlock(type, {x: Math.max(10, ev.clientX - r.left - 60), y: Math.max(10, ev.clientY - r.top - 20)});
  };
  window.addEventListener('pointermove', move);
  window.addEventListener('pointerup', up);
}
// Place new blocks inside the visible part of the canvas, left to right, then the next row.
function freeSpot(i = diagram.blocks.length) {
  const wrap = $('#canvas-wrap');
  const cols = Math.max(1, Math.floor(((wrap?.clientWidth || 1000) - 40) / 220));
  return {x: (wrap?.scrollLeft || 0) + 40 + (i % cols) * 220, y: (wrap?.scrollTop || 0) + 60 + Math.floor(i / cols) * 170};
}
function newId(type) {
  const prefix = ID_PREFIX[type] || ID_PREFIX[types[type].category] || 'block';
  let n = 1;
  while (block(prefix + n)) n++;
  return prefix + n;
}
function addBlock(type, pos) {
  const t = types[type];
  const config = {};
  t.fields.forEach(f => { if (f.default !== undefined && f.default !== '') config[f.key] = structuredClone(f.default); });
  const id = newId(type);
  diagram.blocks.push({id, type, config});
  diagram.layout[id] = pos || freeSpot();
  selected = {kind: 'block', id};
  renderAll();
  changed();
  toast(`Added ${t.label}.` + (t.attachable ? ' Attach it to an agent.' : ''));
}

// ---------- canvas ----------
function summary(b) {
  const c = b.config;
  switch (b.type) {
    case 'agent': return c.model ? `Model: ${c.model}` : 'Model: LLM_MODEL';
    case 'trigger.alert': return `${c.source || '?'} · sensor ${c.sensor_id || '?'}`;
    case 'tool.builtin': return (c.functions || []).join(', ');
    case 'tool.http': return `${c.method || 'GET'} ${c.name || ''} · ${c.access || '?'}`;
    case 'tool.mcp': return `${(c.command || []).join(' ')} · ${c.access || '?'}`;
    case 'tool.device': return `${c.scope_source === 'list' ? `${(c.devices || []).length} devices` : `${c.scope_source || 'ssot'} ${Object.values(c.scope_filter || {}).join(', ')}`} · ${(c.allow || []).length} allowed`;
    case 'guard.approval': return `Expires in ${Math.round((c.expires_s || 3600) / 60)} min`;
    case 'guard.output_check': return c.allowed_citations === 'seen_in_tool_results' ? 'Citations from tools only' : 'Action & root-cause checks';
    case 'memory.kv': return `Facts · ${c.namespace || '?'}`;
    case 'memory.conversation': return `Last ${c.max_items || 5} runs · ${c.namespace || '?'}`;
    case 'memory.documents': return `knowledge/${c.folder && c.folder !== '.' ? c.folder : ''}`;
    case 'guard.policy': return `Max ${c.max_tool_calls || '∞'} calls` + ((c.deny_tools || []).length ? ` · denies ${c.deny_tools.length}` : '');
    case 'guard.redact': return ['secrets', c.emails !== false ? 'emails' : '', c.ipv4 ? 'IPs' : ''].filter(Boolean).join(', ');
    case 'output.file': return c.path || '';
    case 'output.slack': return `Channel from ${c.channel_env || '?'}`;
    default: return spec(b)?.label || b.type;
  }
}
function renderBlocks() {
  const counts = {};
  violations.forEach(v => { if (v.block) counts[v.block] = (counts[v.block] || 0) + 1; });
  $('#blocks').innerHTML = diagram.blocks.map(b => {
    const s = spec(b), p = diagram.layout[b.id] || {x: 40, y: 40}, cat = s?.category || 'output';
    const isSel = selected?.kind === 'block' && selected.id === b.id, n = counts[b.id] || 0;
    const ports = !s ? '' : [
      s.inputs.length ? '<span class="port in" aria-hidden="true"></span>' : '',
      s.output ? `<button class="port out" data-port="out" aria-label="Connect ${esc(b.id)} to the next block" title="Drag to the next block"></button>` : '',
      b.type === 'agent' ? `<button class="port attach" data-port="attach" aria-label="Attach a tool to ${esc(b.id)}" title="Drag to a tool to attach it"></button>` : '',
      s.attachable ? `<button class="port attach-in" data-port="attach-in" aria-label="Attach ${esc(b.id)} to an agent" title="Drag to an agent"></button>` : '',
    ].join('');
    return `<div class="block ${isSel ? 'selected' : ''} ${n ? 'error' : ''}" tabindex="0" data-id="${esc(b.id)}" style="left:${p.x}px;top:${p.y}px;--c:${COLORS[cat]}"
      aria-label="${esc(s?.label || b.type)} ${esc(b.id)}${n ? `, ${n} problem${n > 1 ? 's' : ''}` : ''}">
      <span class="cat">${esc(s?.label || 'Unknown block')}</span><span class="title">${esc(b.id)}</span><span class="sub">${esc(summary(b))}</span>
      ${n ? `<span class="badge" aria-hidden="true">${n}</span>` : ''}${ports}</div>`;
  }).join('');
  $('#empty').hidden = diagram.blocks.length > 0;
  document.querySelectorAll('.block').forEach(el => {
    el.addEventListener('pointerdown', e => blockPointer(e, el));
    el.addEventListener('keydown', e => {
      if ((e.key === 'Enter' || e.key === ' ') && e.target === el) { e.preventDefault(); select({kind: 'block', id: el.dataset.id}); }
    });
  });
  drawWires();
}
function anchor(id, where) {
  const el = document.querySelector(`.block[data-id="${CSS.escape(id)}"]`), p = diagram.layout[id];
  if (!el || !p) return null;
  const w = el.offsetWidth, h = el.offsetHeight;
  return {out: {x: p.x + w, y: p.y + 42}, in: {x: p.x, y: p.y + 42}, bottom: {x: p.x + 88, y: p.y + h}, top: {x: p.x + 88, y: p.y}}[where];
}
function curve(a, b, vertical) {
  if (vertical) { const d = Math.max(40, Math.abs(b.y - a.y) / 2); return `M${a.x} ${a.y} C${a.x} ${a.y + d},${b.x} ${b.y - d},${b.x} ${b.y}`; }
  const d = b.x >= a.x ? Math.max(40, (b.x - a.x) / 2) : 70;  // backward wires: short, tight loop
  return `M${a.x} ${a.y} C${a.x + d} ${a.y},${b.x - d} ${b.y},${b.x} ${b.y}`;
}
function drawWires(temp) {
  let out = '';
  for (const list of ['flow', 'attachments']) {
    diagram[list].forEach(([a, b], index) => {
      const p1 = list === 'flow' ? anchor(a, 'out') : anchor(a, 'bottom');
      const p2 = list === 'flow' ? anchor(b, 'in') : anchor(b, 'top');
      if (!p1 || !p2) return;
      const d = curve(p1, p2, list === 'attachments');
      const sel = selected?.kind === 'wire' && selected.list === list && selected.index === index;
      out += `<path class="wire ${list === 'attachments' ? 'attach' : ''} ${sel ? 'selected' : ''}" d="${d}"/>`;
      out += `<path class="hit" d="${d}" data-list="${list}" data-index="${index}"><title>${esc(a)} → ${esc(b)}</title></path>`;
    });
  }
  if (temp) out += `<path class="temp" d="${curve(temp.a, temp.b, temp.vertical)}"/>`;
  $('#wires').innerHTML = out;
  document.querySelectorAll('#wires path.hit').forEach(p => p.addEventListener('click', () =>
    select({kind: 'wire', list: p.dataset.list, index: Number(p.dataset.index)})));
}
function blockPointer(e, el) {
  if (e.button !== 0) return;
  const id = el.dataset.id, port = e.target.closest('.port')?.dataset.port;
  if (port === 'out' || port === 'attach' || port === 'attach-in') { connectDrag(e, id, port); return; }
  const start = {x: e.clientX, y: e.clientY}, p0 = {...diagram.layout[id]};
  let moved = false;
  el.setPointerCapture(e.pointerId);
  const move = ev => {
    const dx = ev.clientX - start.x, dy = ev.clientY - start.y;
    if (!moved && Math.hypot(dx, dy) < 4) return;
    moved = true;
    diagram.layout[id] = {x: Math.max(0, Math.round(p0.x + dx)), y: Math.max(0, Math.round(p0.y + dy))};
    el.style.left = diagram.layout[id].x + 'px'; el.style.top = diagram.layout[id].y + 'px';
    drawWires();
  };
  const up = () => {
    el.removeEventListener('pointermove', move); el.removeEventListener('pointerup', up);
    if (moved) save(); else select({kind: 'block', id});
  };
  el.addEventListener('pointermove', move); el.addEventListener('pointerup', up);
}
function connectDrag(e, id, port) {
  e.preventDefault();
  const r = $('#canvas').getBoundingClientRect();
  const a = port === 'out' ? anchor(id, 'out') : port === 'attach' ? anchor(id, 'bottom') : anchor(id, 'top');
  const move = ev => drawWires({a, b: {x: ev.clientX - r.left, y: ev.clientY - r.top}, vertical: port !== 'out'});
  const up = ev => {
    window.removeEventListener('pointermove', move); window.removeEventListener('pointerup', up);
    const target = document.elementFromPoint(ev.clientX, ev.clientY)?.closest('.block')?.dataset.id;
    drawWires();
    if (!target || target === id) return;
    if (port === 'out') connect('flow', id, target);
    else if (port === 'attach') connect('attachments', id, target);
    else connect('attachments', target, id);
  };
  window.addEventListener('pointermove', move); window.addEventListener('pointerup', up);
}
// Refuse connections the rules can never allow, and say why (the validator checks the rest).
function connectionProblem(list, a, b) {
  const A = block(a), B = block(b), sa = spec(A), sb = spec(B);
  if (!sa || !sb) return 'Unknown block.';
  if (diagram[list].some(([x, y]) => x === a && y === b)) return 'These blocks are already connected.';
  if (list === 'attachments') {
    if (A.type !== 'agent') return 'Only an agent can have tools, memory or a policy attached.';
    if (!sb.attachable) return `${sb.label} is part of the flow; connect it with a flow wire, not an attachment.`;
    return null;
  }
  if (sa.attachable || sb.attachable) return 'Tools, memory and policies attach to an agent (dashed line); they are not part of the flow.';
  if (!sa.output) return `${sa.label} is an end block; nothing comes after it.`;
  if (!sb.inputs.includes(sa.output)) {
    if (sb.category === 'output') return `${sb.label} only accepts an approved draft. Put an output check and a human approval in front of it.`;
    if (B.type === 'guard.approval') return 'Human approval only accepts a checked draft. Put an output check in front of it.';
    return `${sb.label} cannot follow ${sa.label}.`;
  }
  if (diagram.flow.some(([, y]) => y === b)) return `${b} already has an input; each block takes one incoming wire.`;
  return null;
}
function connect(list, a, b) {
  const problem = connectionProblem(list, a, b);
  if (problem) { toast(problem); return false; }
  diagram[list].push([a, b]);
  renderBlocks(); renderInspector(); changed();
  return true;
}
function removeBlock(id) {
  diagram.blocks = diagram.blocks.filter(b => b.id !== id);
  diagram.flow = diagram.flow.filter(([a, b]) => a !== id && b !== id);
  diagram.attachments = diagram.attachments.filter(([a, b]) => a !== id && b !== id);
  delete diagram.layout[id];
  selected = null;
  renderAll(); changed();
}
function removeWire(list, index) {
  diagram[list].splice(index, 1);
  selected = null;
  renderAll(); changed();
}
function renameBlock(oldId, newId) {
  if (!/^[A-Za-z0-9_-]{1,40}$/.test(newId)) return 'Use 1–40 letters, digits, "_" or "-".';
  if (newId !== oldId && block(newId)) return 'Another block already has this name.';
  block(oldId).id = newId;
  for (const list of ['flow', 'attachments']) diagram[list] = diagram[list].map(([a, b]) => [a === oldId ? newId : a, b === oldId ? newId : b]);
  diagram.layout[newId] = diagram.layout[oldId]; if (newId !== oldId) delete diagram.layout[oldId];
  selected = {kind: 'block', id: newId};
  return null;
}

// ---------- inspector ----------
function select(sel, focusBlock = true) {
  selected = sel;
  renderBlocks(); renderInspector();
  if (focusBlock && sel?.kind === 'block') document.querySelector(`.block[data-id="${CSS.escape(sel.id)}"]`)?.focus({preventScroll: true});
}
function fieldHtml(f, value, i) {
  // Help text sits outside the label and is linked with aria-describedby, so the field's name stays short.
  const id = `f-${i}`, helpId = `${id}-help`;
  const help = f.help ? `<small id="${helpId}">${esc(f.help)}</small>` : '';
  const desc = f.help ? ` aria-describedby="${helpId}"` : '';
  const wrap = control => `<div class="field"><label for="${id}">${esc(f.label)}</label>${control}${help}</div>`;
  switch (f.kind) {
    case 'textarea': return wrap(`<textarea class="prose" id="${id}" rows="${f.key === 'instructions' ? 7 : 3}"${desc}>${esc(value ?? '')}</textarea>`);
    case 'number': return wrap(`<input id="${id}" type="number" min="${f.min ?? ''}" max="${f.max ?? ''}" value="${esc(value ?? '')}"${desc}>`);
    case 'select': return wrap(`<select id="${id}"${desc}>${f.options.map(o => `<option value="${esc(o)}" ${o === (value ?? '') ? 'selected' : ''}>${esc(o || '(none)')}</option>`).join('')}</select>`);
    case 'checkbox': return `<label class="check-row"><input id="${id}" type="checkbox" ${value ? 'checked' : ''}${desc}> ${esc(f.label)}</label>${help}`;
    case 'multiselect': return `<fieldset class="field" id="${id}"${desc}><legend>${esc(f.label)}</legend>${f.options.map((o, j) =>
      `<label class="check-row"><input type="checkbox" value="${esc(o)}" id="${id}-${j}" ${(value || []).includes(o) ? 'checked' : ''}> ${esc(o)}</label>`).join('')}${help}</fieldset>`;
    case 'list': return wrap(`<textarea id="${id}" rows="3"${desc}>${esc((value || []).join('\n'))}</textarea>`);
    case 'json': return `<div class="field"><label for="${id}">${esc(f.label)}</label><textarea id="${id}" rows="4"${desc}>${esc(value === undefined ? '' : JSON.stringify(value, null, 2))}</textarea>${help}<small class="json-error" role="alert" hidden></small></div>`;
    default: return wrap(`<input id="${id}" value="${esc(value ?? '')}" autocomplete="off"${desc}>`);
  }
}
function bindField(f, i, b) {
  const set = v => {
    if (v === '' || v === undefined || (Array.isArray(v) && !v.length && f.kind !== 'multiselect')) delete b.config[f.key];
    else b.config[f.key] = v;
    const el = document.querySelector(`.block[data-id="${CSS.escape(b.id)}"] .sub`);
    if (el) el.textContent = summary(b);
    changed();
  };
  const el = document.getElementById(`f-${i}`);
  if (f.kind === 'multiselect') {
    el.addEventListener('change', () => set([...el.querySelectorAll('input:checked')].map(x => x.value)));
  } else if (f.kind === 'checkbox') {
    el.addEventListener('change', () => set(el.checked));
  } else if (f.kind === 'number') {
    el.addEventListener('input', () => { const n = parseInt(el.value, 10); set(Number.isFinite(n) ? n : undefined); });
  } else if (f.kind === 'list') {
    el.addEventListener('input', () => set(el.value.split('\n').map(s => s.trim()).filter(Boolean)));
  } else if (f.kind === 'json') {
    const err = el.closest('.field').querySelector('.json-error');
    el.addEventListener('input', () => {
      if (!el.value.trim()) { err.hidden = true; set(undefined); return; }
      try { set(JSON.parse(el.value)); err.hidden = true; } catch { err.hidden = false; err.textContent = 'Not valid JSON yet; the last valid value is kept.'; }
    });
  } else {
    el.addEventListener(f.kind === 'select' ? 'change' : 'input', () => set(el.value));
  }
}
function renderInspector() {
  const box = $('#inspector');
  if (selected?.kind === 'wire') {
    const pair = diagram[selected.list][selected.index];
    if (!pair) { selected = null; return renderInspector(); }
    box.innerHTML = `<p class="desc">${selected.list === 'flow' ? 'Flow wire' : 'Attachment'}: <strong>${esc(pair[0])}</strong> → <strong>${esc(pair[1])}</strong></p>
      <button class="btn danger" id="remove-wire">Remove connection</button>`;
    $('#remove-wire').onclick = () => removeWire(selected.list, selected.index);
    return;
  }
  const b = selected?.kind === 'block' ? block(selected.id) : null;
  if (!b) {
    const n = diagram.blocks.length;
    box.innerHTML = `<p class="desc">${n ? `${n} block${n > 1 ? 's' : ''}. Select one to edit it.` : 'Add blocks from the palette.'}</p>
      <p class="desc"><strong>Solid arrows</strong> are the flow: what happens in order.<br><strong>Dashed lines</strong> attach tools, memory and a permission policy to an agent: what it may use.</p>
      <p class="desc">Every output must come after an <strong>output check</strong> and a <strong>human approval</strong>. The canvas refuses anything else.</p>`;
    return;
  }
  const s = spec(b), mine = violations.filter(v => v.block === b.id);
  const flowTargets = diagram.blocks.filter(x => x.id !== b.id && !connectionProblem('flow', b.id, x.id));
  const attachTargets = b.type === 'agent' ? diagram.blocks.filter(x => !connectionProblem('attachments', b.id, x.id)) : [];
  const links = [...diagram.flow.map((p, i) => ({p, i, list: 'flow'})), ...diagram.attachments.map((p, i) => ({p, i, list: 'attachments'}))]
    .filter(({p}) => p.includes(b.id));
  box.innerHTML = `<p class="desc"><strong>${esc(s?.label || b.type)}</strong><br>${esc(s?.description || '')}</p>
    ${mine.length ? `<ul class="issues">${mine.map(v => `<li>${esc(v.message)}</li>`).join('')}</ul>` : ''}
    ${b.type === 'tool.device' ? `<p class="warn">Live mode runs the allowed commands over SSH from this machine, with your SSH keys and config. Sample mode never contacts a device: it replays <code>lab/</code> recordings. Open <strong>Reach</strong> to see every device in scope.</p>` : ''}
    ${b.type === 'tool.mcp' ? `<p class="warn">Running this diagram starts a local program: <code>${esc((b.config.command || []).join(' '))}</code>. Only run MCP servers you trust.</p>` : ''}
    <div class="field"><label for="block-id">Name</label><input id="block-id" value="${esc(b.id)}" autocomplete="off"><small class="rename-error" role="alert" hidden></small></div>
    ${(s?.fields || []).map((f, i) => fieldHtml(f, b.config[f.key], i)).join('')}
    ${flowTargets.length ? `<div class="field"><label for="connect-to">Connect to (flow)</label><select id="connect-to"><option value="">Choose a block…</option>${flowTargets.map(x => `<option>${esc(x.id)}</option>`).join('')}</select></div>` : ''}
    ${attachTargets.length ? `<div class="field"><label for="attach-to">Attach tool</label><select id="attach-to"><option value="">Choose a tool…</option>${attachTargets.map(x => `<option>${esc(x.id)}</option>`).join('')}</select></div>` : ''}
    ${links.length ? `<p class="desc">Connections</p><ul class="muted">${links.map(({p, i, list}) =>
      `<li>${esc(p[0])} ${list === 'flow' ? '→' : '⋯'} ${esc(p[1])} <button class="btn small" data-unlink="${list}:${i}" aria-label="Remove connection ${esc(p[0])} to ${esc(p[1])}">Remove</button></li>`).join('')}</ul>` : ''}
    <div class="row"><button class="btn danger" id="delete-block">Delete block</button></div>`;
  (s?.fields || []).forEach((f, i) => bindField(f, i, b));
  const idInput = $('#block-id'), idErr = box.querySelector('.rename-error');
  idInput.addEventListener('change', () => {
    const problem = renameBlock(b.id, idInput.value.trim());
    if (problem) { idErr.hidden = false; idErr.textContent = problem; idInput.value = b.id; return; }
    idErr.hidden = true; renderBlocks(); renderInspector(); changed();
  });
  $('#connect-to')?.addEventListener('change', e => { if (e.target.value) connect('flow', b.id, e.target.value); });
  $('#attach-to')?.addEventListener('change', e => { if (e.target.value) connect('attachments', b.id, e.target.value); });
  box.querySelectorAll('[data-unlink]').forEach(btn => btn.addEventListener('click', () => {
    const [list, i] = btn.dataset.unlink.split(':'); removeWire(list, Number(i)); select({kind: 'block', id: b.id});
  }));
  $('#delete-block').onclick = () => removeBlock(b.id);
}

// ---------- validation ----------
function changed() {
  save();
  clearTimeout(changed.timer);
  changed.timer = setTimeout(validate, 250);
}
async function validate() {
  if (!token) return;
  try {
    violations = diagram.blocks.length ? (await api('/api/diagram/validate', {diagram, mode: $('#mode').value})).violations : [];
  } catch (e) {
    violations = [{block: null, rule: 'shape', message: e.message}];
  }
  renderProblems(); renderBlocks(); if (selected?.kind === 'block') renderInspectorIssuesOnly();
}
function renderInspectorIssuesOnly() {
  const b = block(selected.id); if (!b) return;
  const mine = violations.filter(v => v.block === b.id), box = $('#inspector');
  let ul = box.querySelector('.issues');
  if (!mine.length) { ul?.remove(); return; }
  if (!ul) { ul = document.createElement('ul'); ul.className = 'issues'; box.querySelector('.desc').after(ul); }
  ul.innerHTML = mine.map(v => `<li>${esc(v.message)}</li>`).join('');
}
function renderProblems() {
  const pill = $('#status-pill');
  if (!diagram.blocks.length) { pill.className = 'pill'; pill.textContent = 'Empty'; $('#problems').innerHTML = ''; return; }
  pill.className = 'pill ' + (violations.length ? 'bad' : 'ok');
  pill.textContent = violations.length ? `${violations.length} problem${violations.length > 1 ? 's' : ''}` : 'Ready to run';
  $('#problems').innerHTML = violations.length ? violations.map((v, i) =>
    `<li><button data-i="${i}">${v.block ? `<strong>${esc(v.block)}</strong>: ` : ''}${esc(v.message)}</button></li>`).join('')
    : '<li class="ok">No problems. The diagram passes every safety rule.</li>';
  document.querySelectorAll('#problems button').forEach(btn => btn.addEventListener('click', () => {
    const v = violations[Number(btn.dataset.i)];
    if (v.block && block(v.block)) select({kind: 'block', id: v.block});
  }));
}

// ---------- running ----------
function renderRun(state) {
  currentRun = state;
  $('#run-panel').hidden = false;
  const labels = {completed: ['ok', 'Completed'], awaiting_approval: ['wait', 'Waiting for approval'], blocked: ['bad', 'Blocked by output check'],
    rejected: ['bad', 'Rejected'], expired: ['bad', 'Approval expired'], failed: ['bad', 'Failed'], invalid: ['bad', 'Diagram has problems'], running: ['wait', 'Running…']};
  const [cls, text] = labels[state.status] || ['', state.status];
  $('#run-status').className = 'pill ' + cls; $('#run-status').textContent = text;
  const rows = (state.trace || []).map(t => `<li class="${t.tool ? 'tool' : ''} ${String(t.detail).startsWith('Failed') ? 'fail' : ''} ${t.status === 'dropped' ? 'dropped' : ''}"><strong>${esc(t.block)}</strong> ${esc(t.detail)}${t.args?.command ? ` <code>${esc(t.args.device)}# ${esc(t.args.command)}</code>` : ''}${t.status === 'dropped' ? ' <span class="drop">DROP</span>' : ''}<span class="ms">${t.ms} ms</span></li>`);
  if (state.error) rows.push(`<li class="fail">${esc(state.error)}</li>`);
  (state.violations || []).forEach(v => rows.push(`<li class="fail">${esc(typeof v === 'string' ? v : v.message)}</li>`));
  $('#trace').innerHTML = rows.join('');
  const waiting = state.status === 'awaiting_approval', p = state.pending || {};
  $('#draft-box').hidden = !waiting;
  if (waiting) {
    const tool = p.kind === 'tool';
    $('#draft-title').textContent = tool ? `Agent "${p.block}" wants to call "${p.tool}" (${p.access || 'gated'})` : 'Draft waiting for your approval';
    $('#draft').textContent = tool ? prettyArgs(p.args) : String(p.value ?? '');
    $('#approve').textContent = tool ? 'Allow this call' : 'Approve';
    $('#reject').textContent = tool ? 'Deny' : 'Reject';
  }
  refreshInbox();
}
function prettyArgs(args) {
  try { return JSON.stringify(typeof args === 'string' ? JSON.parse(args) : args, null, 2); } catch { return String(args); }
}

// ---------- reach: what the agents could touch, before anything runs ----------
async function openReach() {
  const box = $('#reach-panel');
  box.hidden = false;
  box.innerHTML = '<p class="muted">Working out reach…</p>';
  let r;
  try { r = await api('/api/diagram/reach', {diagram, mode: $('#mode').value}); }
  catch (e) { box.innerHTML = `<p class="warn">${esc(e.message)}</p>`; return; }
  const s = r.summary;
  const tile = (n, label, cls) => `<div class="tile ${cls}"><strong>${n}</strong><span>${label}</span></div>`;
  const list = (items, fmt) => items.length ? `<ul>${items.map(fmt).join('')}</ul>` : '';
  box.innerHTML = `<div class="run-head"><h2>Reach</h2><button class="btn small" id="close-reach" aria-label="Close reach">×</button></div>
    <p class="desc">Everything these agents could touch, worked out from the diagram before it runs.</p>
    <div class="tiles">${tile(s.devices_readable, 'devices readable', 'read')}${tile(s.read_tools, 'read tools', 'read')}
      ${tile(s.change_paths, 'change paths, each approved', s.change_paths ? 'change' : '')}${tile(s.device_config_paths, 'device config sessions', '')}</div>
    ${s.remote_models.length ? `<p class="warn">Prompts and tool results go to a remote model: ${esc(s.remote_models.join(', '))}.</p>` : ''}
    ${r.agents.map(a => `<section class="reach-agent"><h3>${esc(a.agent)} <small>${esc(a.model)} · ${esc(a.model_location)}</small></h3>
      ${a.devices.map(d => `<div class="reach-group"><div class="eyebrow read">Devices · read only</div>
        <p>${d.error ? `<span class="warn">${esc(d.error)}</span>` : esc(d.devices.join(', ') || 'none match')}</p>
        <p class="muted">From ${esc(d.source)} ${esc(JSON.stringify(d.filter))} · max ${esc(d.max_commands)} commands per run</p>
        <p>Allowed ${d.allow.map(x => `<code class="read">${esc(x)}</code>`).join(' ')}</p>
        <p>Always blocked ${d.always_denied.map(x => `<code>${esc(x)}</code>`).join(' ')}</p></div>`).join('')}
      ${a.read.length ? `<div class="reach-group"><div class="eyebrow read">Read</div>${list(a.read, x => `<li>${esc(x.system)}: ${esc(x.what)}</li>`)}</div>` : ''}
      ${a.change.length ? `<div class="reach-group"><div class="eyebrow change">Change · each call approved</div>${list(a.change, x => `<li>${esc(x.system)}: ${esc(x.what)}</li>`)}</div>` : ''}
      ${a.memory.length ? `<div class="reach-group"><div class="eyebrow">Memory</div>${list(a.memory, m => `<li>${esc(m.type)} · ${esc(m.namespace)}</li>`)}</div>` : ''}
    </section>`).join('')}
    ${r.outputs.length ? `<div class="reach-group"><div class="eyebrow">Outputs · only after approval</div>${list(r.outputs, o => `<li>${esc(o.type)} → ${esc(o.where)}</li>`)}</div>` : ''}`;
  $('#close-reach').onclick = () => { box.hidden = true; };
}

// ---------- inbox: everything waiting for a human, across runs ----------
async function refreshInbox() {
  if (!token) return;
  try {
    const items = (await api('/api/diagram/pending')).pending;
    $('#inbox').textContent = `Inbox (${items.length})`;
    $('#inbox').classList.toggle('attention', items.length > 0);
    return items;
  } catch { return []; }
}
async function openInbox() {
  const items = await refreshInbox();
  const box = $('#inbox-panel');
  box.hidden = false;
  box.innerHTML = `<div class="run-head"><h2>Inbox</h2><span class="pill ${items.length ? 'wait' : 'ok'}">${items.length} waiting</span>
    <button class="btn small" id="close-inbox" aria-label="Close inbox">×</button></div>` + (items.length ? items.map((it, i) => `
    <div class="draft"><strong>${esc(it.diagram)} · ${it.kind === 'tool' ? `tool call "${esc(it.tool)}" by ${esc(it.block)}` : `draft at ${esc(it.block)}`}</strong>
      <pre>${esc(it.kind === 'tool' ? prettyArgs(it.args) : it.draft)}</pre>
      <div class="draft-actions"><button class="btn primary" data-i="${i}" data-ok="1">${it.kind === 'tool' ? 'Allow' : 'Approve'}</button>
      <button class="btn" data-i="${i}" data-ok="0">${it.kind === 'tool' ? 'Deny' : 'Reject'}</button>
      <span class="muted">since ${esc(new Date(it.since).toLocaleString())}</span></div></div>`).join('') : '<p class="muted">Nothing is waiting for approval.</p>');
  $('#close-inbox').onclick = () => { box.hidden = true; };
  box.querySelectorAll('[data-i]').forEach(btn => btn.addEventListener('click', async () => {
    const it = items[Number(btn.dataset.i)];
    btn.disabled = true;
    try { const state = await api('/api/diagram/approve', {id: it.id, approved: btn.dataset.ok === '1'}); if (currentRun?.id === it.id) renderRun(state); toast(`Run ${state.status.replace('_', ' ')}.`); }
    catch (e) { toast(e.message); }
    openInbox();
  }));
}
async function runDiagram() {
  if (running) return;
  const hasManual = diagram.blocks.some(b => b.type === 'trigger.manual');
  $('#run-input-field').hidden = !hasManual;
  running = true; $('#run').disabled = true; $('#run').textContent = 'Running…';
  renderRun({status: 'running', trace: []});
  try {
    renderRun(await api('/api/diagram/run', {diagram, mode: $('#mode').value, input: hasManual ? $('#run-input').value : null}));
  } catch (e) {
    renderRun({status: 'failed', trace: [], error: e.message});
  } finally {
    running = false; $('#run').disabled = false; $('#run').textContent = '▷ Run';
  }
}
async function decide(approved) {
  if (!currentRun?.id) return;
  $('#approve').disabled = $('#reject').disabled = true;
  try { renderRun(await api('/api/diagram/approve', {id: currentRun.id, approved})); }
  catch (e) { toast(e.message); }
  finally { $('#approve').disabled = $('#reject').disabled = false; }
}

// ---------- evals: test cases stored in the diagram ----------
const EXPECT_LISTS = [['contains', 'Draft must contain'], ['not_contains', 'Draft must not contain'],
  ['tools_called', 'Tools that must be called'], ['tools_not_called', 'Tools that must not be called'], ['cites_any', 'Must cite one of']];
let evalReport = null, evalExport = false;

function triggerType() { return diagram.blocks.find(b => category(b) === 'trigger')?.type; }
function caseHtml(c, i) {
  const t = triggerType(), e = c.expect || {};
  const input = t === 'trigger.alert'
    ? `<div class="field"><label for="ev-${i}-alert">Alert message (empty = sample alert)</label><input id="ev-${i}-alert" data-k="alert" value="${esc(c.alert?.message || '')}"></div>`
    : `<div class="field"><label for="ev-${i}-input">Input</label><textarea id="ev-${i}-input" data-k="input" rows="2">${esc(typeof c.input === 'string' ? c.input : c.input ? JSON.stringify(c.input) : '')}</textarea></div>`;
  return `<details ${evalReport ? '' : 'open'} data-i="${i}"><summary>${esc(c.id)}</summary>
    <div class="field"><label for="ev-${i}-id">Name</label><input id="ev-${i}-id" data-k="id" value="${esc(c.id)}"></div>
    ${input}
    <div class="field"><label for="ev-${i}-status">Expected result</label><select id="ev-${i}-status" data-k="status">
      ${[['awaiting_approval', 'Draft ready for approval'], ['blocked', 'Blocked by output check'], ['failed', 'Run fails']].map(([v, l]) => `<option value="${v}" ${(e.status || 'awaiting_approval') === v ? 'selected' : ''}>${l}</option>`).join('')}</select></div>
    <div class="grid2">${EXPECT_LISTS.map(([k, l]) => `<div class="field"><label for="ev-${i}-${k}">${l}</label><textarea id="ev-${i}-${k}" data-k="${k}" rows="2" aria-describedby="ev-help">${esc((e[k] || []).join('\n'))}</textarea></div>`).join('')}</div>
    <button class="btn small danger" data-remove="${i}">Remove case</button></details>`;
}
function readCase(el, old) {
  const val = k => el.querySelector(`[data-k="${k}"]`)?.value ?? '';
  const lines = k => val(k).split('\n').map(x => x.trim()).filter(Boolean);
  const c = {id: val('id').trim() || old.id, expect: {status: val('status')}};
  EXPECT_LISTS.forEach(([k]) => { const v = lines(k); if (v.length) c.expect[k] = v; });
  if (el.querySelector('[data-k="alert"]')) { const m = val('alert').trim(); if (m) c.alert = {...(old.alert || {}), message: m}; }
  else if (val('input').trim()) { const raw = val('input').trim(); try { c.input = triggerType() === 'trigger.webhook' ? JSON.parse(raw) : raw; } catch { c.input = raw; } }
  return c;
}
function renderEvals() {
  const box = $('#evals-panel'), cases = diagram.evals || [];
  const rep = evalReport;
  box.innerHTML = `<div class="run-head"><h2>Evals</h2>${rep ? `<span class="pill ${rep.passed === rep.total ? 'ok' : 'bad'}">${rep.passed}/${rep.total} passed</span>` : ''}
      <button class="btn small" id="close-evals" aria-label="Close evals">×</button></div>
    <p class="desc" id="ev-help">Each case runs the diagram up to the first approval. Write tools are denied and memory is isolated, so evals never change anything. One entry per line.</p>
    ${rep ? `<ul class="results">${rep.results.map(r => `<li><span class="${r.passed ? 'pass' : 'fail'}">${r.passed ? 'PASS' : 'FAIL'}</span> ${esc(r.id)}${rep.results.some(x => x.run > 1) ? ` (run ${r.run})` : ''} · ${esc(r.status)}
      ${r.passed ? '' : `<small>Failed: ${esc(Object.entries(r.checks).filter(([, v]) => !v).map(([k]) => k).join(', '))}${r.error ? ' · ' + esc(r.error) : ''}</small>`}
      <small>Tools: ${esc(r.tools.join(', ') || 'none')}</small></li>`).join('')}</ul>
      ${rep.export ? `<p class="desc">Exported Python: ${rep.export.passed}/${rep.export.total} passed · ${rep.export.matches ? 'matches the diagram' : '<strong>differs from the diagram</strong>'}</p>` : ''}` : ''}
    <div id="ev-cases">${cases.map(caseHtml).join('') || '<p class="muted">No cases yet.</p>'}</div>
    <div class="row"><button class="btn" id="ev-add">Add case</button>
      <label class="sr-only" for="ev-repeat">Runs per case</label><select id="ev-repeat"><option value="1">1 run per case</option><option value="3">3 runs per case</option></select>
      <label class="check-row"><input type="checkbox" id="ev-export" ${evalExport ? 'checked' : ''}> Also test the Python export</label>
      <button class="btn primary" id="ev-run">Run evals</button></div>`;
  const sync = () => {
    diagram.evals = [...box.querySelectorAll('#ev-cases details')].map(el => readCase(el, cases[Number(el.dataset.i)]));
    save();
  };
  box.querySelectorAll('#ev-cases input, #ev-cases textarea, #ev-cases select').forEach(el => el.addEventListener('change', sync));
  box.querySelectorAll('[data-remove]').forEach(btn => btn.addEventListener('click', () => { sync(); diagram.evals.splice(Number(btn.dataset.remove), 1); save(); renderEvals(); }));
  $('#ev-add').onclick = () => { sync(); let n = (diagram.evals || []).length + 1; while ((diagram.evals || []).some(c => c.id === 'case-' + n)) n++; (diagram.evals ??= []).push({id: 'case-' + n, expect: {status: 'awaiting_approval'}}); save(); renderEvals(); };
  $('#ev-export').onchange = e => { evalExport = e.target.checked; };
  $('#close-evals').onclick = () => { box.hidden = true; };
  $('#ev-run').onclick = async () => {
    sync();
    const btn = $('#ev-run'); btn.disabled = true; btn.textContent = 'Running…';
    try {
      const res = await api('/api/diagram/eval', {diagram, mode: $('#mode').value, repeat: Number($('#ev-repeat').value), compare_export: $('#ev-export').checked});
      if (res.status === 'invalid') { toast('Fix the diagram problems first.'); violations = res.violations; renderProblems(); renderBlocks(); }
      else { evalReport = res; renderEvals(); }
    } catch (e) { toast(e.message); btn.disabled = false; btn.textContent = 'Run evals'; }
  };
}
async function exportPython() {
  try {
    const res = await api('/api/diagram/export', {diagram});
    const url = URL.createObjectURL(new Blob([res.source], {type: 'text/x-python'}));
    const a = document.createElement('a'); a.href = url; a.download = res.filename; a.click();
    setTimeout(() => URL.revokeObjectURL(url), 1000);
    toast(`Exported ${res.filename}. It owns the flow and the agent loop; edit it freely.`);
  } catch (e) { toast(e.message); }
}

// ---------- New agent: start from the job, not an empty canvas ----------
const EXAMPLES = [
  {label: 'BGP neighbour went down', shape: 'alert', job: 'When a BGP neighbour goes down, find out which peer and circuit are affected and brief the on-call engineer.', commands: ['bgp', 'logs', 'interfaces']},
  {label: 'Interface errors rising', shape: 'alert', job: 'When interface errors rise on a WAN link, find out what the counters and logs show and brief the on-call engineer.', commands: ['interfaces', 'logs']},
  {label: 'Weekly WAN capacity note', shape: 'report', job: 'Write the weekly capacity note for the WAN links: which ones may cross 80 % soon and how reliable the forecast is.', commands: []},
  {label: 'Config against our standard', shape: 'review', job: 'Review a router config against our configuration standard and list what to fix before the change window.', commands: []},
];
const TEST_LABELS = {'does-the-job': 'Does the job with its tools', 'alert-tries-to-trick-it': 'An alert that tries to trick it changes nothing',
  'input-tries-to-trick-it': 'A request that tries to trick it changes nothing', 'cites-a-runbook': 'Cites a runbook it actually found'};
let wiz = null, wizOpts = null, wizTimer = null;

function wizDefaults() {
  return {step: 1, preview: null, spec: {
    shape: 'alert', job: '', name: '', trigger: {source: 'sample', sensor_id: wizOpts?.sensors?.[0]?.id || '1001'},
    devicesOn: true, devices: {source: 'ssot', filter: {site: wizOpts?.sensors?.[0]?.site || wizOpts?.asset_sites?.[0] || ''}, devices: [], commands: ['interfaces', 'logs']},
    asset_register: true, runbooks: true, forecast: false, configs: false, memory: true, mcpOn: false,
    mcp: [{name: '', command: [], allow_tools: []}], output: {kind: 'file', channel_env: 'SLACK_CHANNEL', url: ''},
    approver: 'the on-call engineer', model: {model: '', model_url: ''}}};
}
function wizSpec() {  // the spec the server builds from (only what is switched on)
  const s = wiz.spec;
  return {shape: s.shape, job: s.job, name: s.name, trigger: s.trigger, approver: s.approver, memory: s.memory,
    asset_register: s.asset_register, runbooks: s.runbooks, forecast: s.forecast, configs: s.configs,
    devices: s.devicesOn ? s.devices : null, mcp: s.mcpOn ? s.mcp.filter(m => m.command.length) : [],
    output: s.output, model: {model: s.model.model.trim(), model_url: s.model.model_url.trim()}};
}
async function openBuilder() {
  if (!wizOpts) {
    try { wizOpts = await (await fetch('/api/builder/options')).json(); } catch { toast('The local runtime is not reachable.'); return; }
  }
  wiz = wizDefaults();
  $('#builder').hidden = false;
  renderBuilder();
  $('#wiz-job')?.focus();
}
function closeBuilder() { $('#builder').hidden = true; }
function wizSteps() {
  const names = ['The job', 'What it may look at', 'Review and test'];
  return `<nav class="wiz-steps" aria-label="Steps">${names.map((n, i) => `<span class="${i + 1 === wiz.step ? 'now' : i + 1 < wiz.step ? 'done' : ''}" ${i + 1 === wiz.step ? 'aria-current="step"' : ''}>${i + 1 < wiz.step ? '✓' : i + 1} ${n}</span>`).join('')}</nav>`;
}
function renderBuilder() {
  const box = $('#builder'), s = wiz.spec;
  const head = `<header class="wiz-bar"><strong>New agent</strong>${s.name || s.job ? `<span>· ${esc(s.name || s.job.slice(0, 50))}</span>` : ''}
    <button class="btn" id="wiz-empty">Empty canvas instead</button><button class="btn" id="wiz-close" aria-label="Close">×</button></header>`;
  box.innerHTML = head + wizSteps() + (wiz.step === 1 ? wizStep1() : wizStep2());
  $('#wiz-close').onclick = closeBuilder;
  $('#wiz-empty').onclick = () => { if (!diagram.blocks.length || confirm('Start a new, empty diagram?')) { setDiagram(blank()); closeBuilder(); } };
  box.querySelectorAll('[data-bind]').forEach(el => el.addEventListener(el.type === 'checkbox' || el.tagName === 'SELECT' || el.type === 'radio' ? 'change' : 'input', () => wizBind(el)));
  if (wiz.step === 1) {
    box.querySelectorAll('[data-example]').forEach(b => b.onclick = () => {
      const ex = EXAMPLES[Number(b.dataset.example)];
      Object.assign(s, {shape: ex.shape, job: ex.job, forecast: ex.shape === 'report', configs: ex.shape === 'review', devicesOn: ex.commands.length > 0});
      if (ex.commands.length) s.devices.commands = ex.commands;
      renderBuilder();
    });
    $('#wiz-next').onclick = () => {
      if (s.job.trim().length < 10) { toast('Describe the job in one sentence first.'); $('#wiz-job').focus(); return; }
      wiz.step = 2; renderBuilder(); wizPreview();
    };
  } else {
    $('#wiz-back').onclick = () => { wiz.step = 1; renderBuilder(); };
    $('#wiz-build').onclick = wizBuild;
    wizRenderPreview();
  }
}
function wizBind(el) {
  const s = wiz.spec, path = el.dataset.bind.split('.');
  let v = el.type === 'checkbox' ? el.checked : el.value;
  if (el.dataset.list !== undefined) v = String(v).split(/[\n,]/).map(x => x.trim()).filter(Boolean);
  if (el.dataset.cmd) {  // command chips
    const set = new Set(s.devices.commands);
    el.checked ? set.add(el.dataset.cmd) : set.delete(el.dataset.cmd);
    s.devices.commands = [...set];
  } else {
    let o = s;
    path.slice(0, -1).forEach(k => { o = o[k]; });
    o[path.at(-1)] = v;
  }
  if (['shape', 'devicesOn', 'mcpOn', 'devices.source', 'devices.clab_topology', 'output.kind', 'asset_register', 'runbooks', 'forecast', 'configs', 'devices.commands'].includes(el.dataset.bind)) {
    const focusSel = el.dataset.cmd ? `[data-cmd="${el.dataset.cmd}"]` : `[data-bind="${el.dataset.bind}"]${el.type === 'radio' ? `[value="${el.value}"]` : ''}`;
    if (el.dataset.bind === 'devices.source') {
      s.devices.filter = s.devices.source === 'netbox' ? {site: wizOpts.netbox.sites[0]?.slug || '', role: wizOpts.netbox.roles[0]?.slug || ''}
        : s.devices.source === 'containerlab' ? {} : {site: wizOpts.asset_sites[0] || ''};
      if (s.devices.source === 'containerlab') s.devices.clab_topology = wizOpts.labs[0] || '';
    }
    if (el.dataset.bind === 'shape') { s.forecast = s.shape === 'report'; s.configs = s.shape === 'review'; }
    renderBuilder();
    $(focusSel)?.focus();
  }
  if (el.dataset.bind === 'trigger.sensor_id' && s.devices.source === 'ssot') {  // read the alerting device's site by default
    const site = wizOpts.sensors.find(o => o.id === s.trigger.sensor_id)?.site;
    if (site) s.devices.filter = {site};
  }
  if (wiz.step === 2) wizPreview();
}
function wizStep1() {
  const s = wiz.spec;
  const shapes = [['alert', 'Investigate an alert', 'Starts when monitoring alerts. Gathers evidence and briefs the on-call engineer.'],
    ['report', 'Write a regular report', 'Runs when you start it or on a schedule, for example the weekly capacity note.'],
    ['review', 'Review before a change', 'Checks a config or a plan against your standard and lists what to fix.'],
    ['ask', 'Answer when I ask', 'You type a question; it investigates and answers with its evidence.']];
  return `<div class="wiz-body"><main class="wiz-main">
    <h1>What should this agent do?</h1><p class="wiz-lede">One agent, one job. Pick the kind of job, then say it in your own words.</p>
    <fieldset class="wiz-shapes"><legend>Kind of job</legend>${shapes.map(([id, t, d]) => `<label class="wiz-card ${s.shape === id ? 'on' : ''}">
      <input type="radio" name="wiz-shape" value="${id}" data-bind="shape" ${s.shape === id ? 'checked' : ''}><span><strong>${t}</strong><small>${d}</small></span></label>`).join('')}</fieldset>
    <label class="wiz-field" for="wiz-job">The job in one sentence</label>
    <textarea id="wiz-job" rows="2" data-bind="job" placeholder="When PRTG alerts on a WAN interface, find out what's happening and tell the on-call engineer.">${esc(s.job)}</textarea>
    <div class="wiz-examples"><span>Or start from an example:</span>${EXAMPLES.map((e, i) => `<button class="chip" data-example="${i}">${esc(e.label)}</button>`).join('')}</div>
    <div class="wiz-grid">
      <label class="wiz-field">Name <input data-bind="name" value="${esc(s.name)}" placeholder="WAN interface alert"></label>
      ${s.shape === 'alert' ? `<label class="wiz-field">Starts when
        <select data-bind="trigger.source"><option value="sample" ${s.trigger.source === 'sample' ? 'selected' : ''}>A sample alert (for trying it out)</option><option value="prtg" ${s.trigger.source === 'prtg' ? 'selected' : ''}>PRTG raises an alert</option></select></label>
      <label class="wiz-field">On sensor <select data-bind="trigger.sensor_id">${(wizOpts.sensors || []).map(o => `<option value="${esc(o.id)}" ${o.id === s.trigger.sensor_id ? 'selected' : ''}>${esc(o.id)} · ${esc(o.label)}</option>`).join('')}</select></label>`
      : `<p class="wiz-note">${s.shape === 'ask' ? 'You start it by typing a question.' : 'You start it by hand; schedule it with <code>python -m bya.graph run</code> from cron or a task scheduler.'}</p>`}
    </div>
    <div class="wiz-actions"><span></span><button class="btn primary big" id="wiz-next">Next: what it may look at</button></div>
  </main>
  <aside class="wiz-side"><strong>What a good job looks like</strong>
    <div><em class="bad">Too small, that's a tool</em><span>"Check interface status"</span></div>
    <div class="good"><em>One job</em><span>"When an interface alert fires, find out what's happening and brief on-call"</span></div>
    <div><em class="bad">Too big, split it</em><span>"Handle all network incidents"</span></div>
    <p>A job is something you would write a runbook for. Narrow jobs are easier to test and need less access.</p></aside></div>`;
}
function wizStep2() {
  const s = wiz.spec, d = s.devices, nb = wizOpts.netbox;
  const opt = (v, cur, label) => `<option value="${esc(v)}" ${v === cur ? 'selected' : ''}>${esc(label ?? v)}</option>`;
  const scope = d.source === 'netbox'
    ? `<label class="wiz-field">Site <select data-bind="devices.filter.site">${nb.sites.map(x => opt(x.slug, d.filter.site, x.name)).join('')}</select></label>
       <label class="wiz-field">Role <select data-bind="devices.filter.role">${nb.roles.map(x => opt(x.slug, d.filter.role, x.name)).join('')}</select></label>`
    : d.source === 'containerlab'
      ? `<label class="wiz-field span2">Lab <select data-bind="devices.clab_topology">${wizOpts.labs.map(x => opt(x, d.clab_topology)).join('')}</select></label>`
    : d.source === 'list'
      ? `<label class="wiz-field span2">Device names, one per line <textarea rows="2" data-bind="devices.devices" data-list>${esc(d.devices.join('\n'))}</textarea></label>`
      : `<label class="wiz-field">Site (asset register) <select data-bind="devices.filter.site">${wizOpts.asset_sites.map(x => opt(x, d.filter.site)).join('')}</select></label><span></span>`;
  const card = (bind, on, title, sub, extra = '') => `<div class="wiz-card wide ${on ? 'on' : ''}"><label class="wiz-check"><input type="checkbox" data-bind="${bind}" ${on ? 'checked' : ''}><span><strong>${title}</strong><small>${sub}</small></span></label>${on ? extra : ''}</div>`;
  const devices = card('devicesOn', s.devicesOn, 'Devices', 'read-only commands over SSH', `
    <div class="wiz-grid three"><label class="wiz-field">Devices from <select data-bind="devices.source">${opt('ssot', d.source, 'Asset register')}${nb.configured ? opt('netbox', d.source, 'NetBox') : ''}${(wizOpts.labs || []).length ? opt('containerlab', d.source, 'My containerlab lab') : ''}${opt('list', d.source, 'A list I type')}</select></label>${scope}</div>
    ${nb.error ? `<p class="warn">NetBox: ${esc(nb.error)}</p>` : ''}
    <div class="wiz-field">It may run</div><div class="wiz-chips">${wizOpts.commands.map(c => `<label class="chip ${d.commands.includes(c.id) ? 'on' : ''}"><input type="checkbox" data-bind="devices.commands" data-cmd="${c.id}" ${d.commands.includes(c.id) ? 'checked' : ''}>${esc(c.label)}</label>`).join('')}</div>
    <p class="wiz-note mono">${esc(wizOpts.commands.filter(c => d.commands.includes(c.id)).flatMap(c => c.patterns).join(' · ') || 'No commands chosen')}</p>`);
  const mcp = s.mcp[0];
  return `<div class="wiz-body"><main class="wiz-main">
    <h1>What may it look at?</h1><p class="wiz-lede">Tick only what the job needs. Everything here is read-only; nothing can change a device.</p>
    ${devices}
    <div class="wiz-grid three">
      ${card('asset_register', s.asset_register, 'Asset register', 'owner, service, site')}
      ${card('runbooks', s.runbooks, 'Runbooks', 'your runbooks and documents in knowledge/')}
      ${s.shape === 'review' ? card('configs', s.configs, 'Config files', `${wizOpts.configs.length} in configs/`) : card('forecast', s.forecast, 'Metric forecast', 'when a metric may cross a threshold')}
    </div>
    ${card('mcpOn', s.mcpOn, 'One of your own tools', 'a local MCP server, read-only', `<div class="wiz-grid three">
      <label class="wiz-field">Name <input data-bind="mcp.0.name" value="${esc(mcp.name)}" placeholder="servicenow"></label>
      <label class="wiz-field">Command <input data-bind="mcp.0.command" data-list value="${esc(mcp.command.join(', '))}" placeholder="python, snow_mcp.py"></label>
      <label class="wiz-field">Only these tools <input data-bind="mcp.0.allow_tools" data-list value="${esc(mcp.allow_tools.join(', '))}" placeholder="get_incident, search_incidents"></label></div>`)}
    <div class="wiz-card wide"><div class="wiz-grid three">
      <label class="wiz-field">Send the result to <select data-bind="output.kind">${opt('file', s.output.kind, 'A file in outputs/')}${opt('slack', s.output.kind, 'Slack')}${opt('webhook', s.output.kind, 'A webhook (HTTPS)')}</select></label>
      ${s.output.kind === 'slack' ? `<label class="wiz-field">Channel variable <input data-bind="output.channel_env" value="${esc(s.output.channel_env)}"></label>`
        : s.output.kind === 'webhook' ? `<label class="wiz-field">URL <input data-bind="output.url" value="${esc(s.output.url)}" placeholder="https://"></label>` : '<span></span>'}
      <label class="wiz-field">Approved first by <input data-bind="approver" value="${esc(s.approver)}"></label>
      <label class="wiz-check"><input type="checkbox" data-bind="memory" ${s.memory ? 'checked' : ''}>Remember its last 5 runs</label>
      <label class="wiz-field">Model <input data-bind="model.model" value="${esc(s.model.model)}" placeholder="LLM_MODEL (e.g. qwen2.5:7b)"></label>
      <label class="wiz-field">Model endpoint <input data-bind="model.model_url" value="${esc(s.model.model_url)}" placeholder="LLM_BASE_URL or Ollama on this machine"></label>
    </div></div>
    <div class="wiz-actions"><button class="btn" id="wiz-back">Back</button><button class="btn primary big" id="wiz-build">Build the agent</button></div>
  </main><aside class="wiz-side" id="wiz-preview" aria-live="polite"></aside></div>`;
}
function wizPreview() {
  clearTimeout(wizTimer);
  wizTimer = setTimeout(async () => {
    try { wiz.preview = await api('/api/builder/preview', {spec: wizSpec()}); } catch (e) { wiz.preview = {problem: e.message}; }
    wizRenderPreview();
  }, 250);
}
function wizRenderPreview() {
  const box = $('#wiz-preview'), p = wiz?.preview;
  if (!box) return;
  if (!p) { box.innerHTML = '<strong>What it will be able to touch</strong><p class="muted">Working it out…</p>'; return; }
  if (p.problem) { box.innerHTML = `<strong>What it will be able to touch</strong><p class="warn">${esc(p.problem.replace('The answers do not make a valid agent yet: ', ''))}</p>`; return; }
  const s = p.summary, row = (label, n, cls = '') => `<div class="wiz-row"><span>${label}</span><strong class="${cls}">${n}</strong></div>`;
  box.innerHTML = `<strong>What it will be able to touch</strong>
    ${row('Devices, read-only', s.devices_readable, 'read')}${row('Read tools', s.read_tools, 'read')}${row('Things it can change', s.change_paths, s.change_paths ? 'change' : '')}
    ${p.devices.length ? `<p class="wiz-note">${esc(p.devices.slice(0, 8).join(', '))}${p.devices.length > 8 ? ` and ${p.devices.length - 8} more` : ''}</p>` : ''}
    ${p.scope_errors.map(e => `<p class="warn">${esc(e)}</p>`).join('')}
    ${s.remote_models.length ? `<p class="warn">Prompts go to a remote model: ${esc(s.remote_models.join(', '))}.</p>` : ''}
    <div class="wiz-safe">${s.change_paths ? 'Each change needs approval.' : '<strong>This agent can\'t change anything.</strong>'} Its result is sent only after ${esc(wiz.spec.approver || 'a person')} approves it.</div>
    <p class="wiz-note">Updates as you tick. Credentials come from this machine's settings, never from the agent.</p>`;
}
async function wizBuild() {
  const btn = $('#wiz-build');
  btn.disabled = true;
  let p;
  try { p = await api('/api/builder/preview', {spec: wizSpec()}); } catch (e) { p = {problem: e.message}; }
  btn.disabled = false;
  if (p.problem) { wiz.preview = p; wizRenderPreview(); toast('Fix the highlighted answer first.'); return; }
  setDiagram(p.diagram);
  closeBuilder();
  openReview(p);
}
function openReview(p) {
  const doc = p.diagram, agent = doc.blocks.find(b => b.type === 'agent').config;
  const out = doc.blocks.find(b => b.id === 'send');
  const box = $('#review-panel');
  box.hidden = false;
  box.innerHTML = `<div class="run-head"><h2>Here is your agent</h2><span class="pill ok">Passes all safety rules</span><button class="btn small" id="close-review" aria-label="Close">×</button></div>
    <p class="desc">Built from your answers. Change anything on the canvas; click a block to see its settings.</p>
    <h3>What BYA set up for you</h3>
    <ul class="checks">
      <li>Instructions drafted from your sentence (click the agent block to edit)</li>
      <li>Limits: ${agent.max_steps} steps, ${Math.round(agent.token_budget / 1000)}k tokens, ${Math.round(agent.timeout_s / 60)} minutes</li>
      <li>Evidence check: cites only what its tools returned; blocks "I restarted…" and root-cause guesses</li>
      <li>${esc(wiz.spec.approver || 'A person')} approves every result before it goes to ${esc(out.type === 'output.file' ? out.config.path : out.type === 'output.slack' ? 'Slack' : 'the webhook')}</li>
      ${p.summary.devices_readable ? `<li>${p.summary.devices_readable} devices, read-only; secrets masked in their output</li>` : ''}
    </ul>
    <h3>${doc.evals.length} tests, ready to run</h3>
    <ul class="tests">${doc.evals.map(c => `<li>${esc(TEST_LABELS[c.id] || c.id)}</li>`).join('')}</ul>
    <div class="draft-actions"><button class="btn primary" id="review-test">Test on sample data</button><button class="btn" id="review-run">Run once</button></div>
    <p class="muted">Sample data never contacts a device or sends a message. Go live when the tests pass.</p>`;
  $('#close-review').onclick = () => { box.hidden = true; };
  $('#review-run').onclick = () => { box.hidden = true; runDiagram(); };
  $('#review-test').onclick = () => {
    box.hidden = true;
    const panel = $('#evals-panel');
    panel.hidden = false;
    renderEvals();
    $('#ev-run').click();
  };
}

// ---------- files ----------
function download(name, text) {
  const url = URL.createObjectURL(new Blob([text], {type: 'application/json'}));
  const a = document.createElement('a'); a.href = url; a.download = name; a.click();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
}
const slug = s => (s || 'agent').toLowerCase().replace(/[^a-z0-9]+/g, '-').replace(/^-+|-+$/g, '').slice(0, 55) || 'agent';

function renderAll() { renderBlocks(); renderInspector(); renderProblems(); }

async function init() {
  try {
    const status = await (await fetch('/api/status')).json();
    token = status.token;
    types = Object.fromEntries((await api('/api/catalog')).types.map(t => [t.type, t]));
  } catch {
    document.querySelector('.stage').innerHTML = '<p class="desc" style="padding:24px">Diagram Studio needs the local runtime. Start it with <code>python server.py</code> and open <code>http://127.0.0.1:8787/studio.html</code>.</p>';
    return;
  }
  renderPalette();
  try {
    const list = (await api('/api/diagrams')).diagrams;
    $('#template').innerHTML += list.map(d => `<option value="${esc(d.file)}">${esc(d.name)}</option>`).join('');
  } catch {}
  let restored = null;
  try { restored = JSON.parse(localStorage.getItem(STORE_KEY)); } catch {}
  if (restored?.blocks) setDiagram(restored); else renderAll();

  $('#template').onchange = async e => {
    if (!e.target.value) return;
    if (diagram.blocks.length && !confirm('Replace the current diagram with this template?')) { e.target.value = ''; return; }
    try { setDiagram(await api('/api/diagrams/' + e.target.value)); toast('Template loaded.'); } catch (err) { toast(err.message); }
    e.target.value = '';
  };
  $('#new').onclick = openBuilder;
  $('#export').onclick = () => { save(); download(slug(diagram.name) + '.json', JSON.stringify(diagram, null, 2)); };
  $('#save').onclick = async () => {
    save();
    const file = prompt('Save to diagrams/ as:', slug(diagram.name) + '.json');
    if (!file) return;
    try { await api('/api/diagram/save', {file, diagram}); toast(`Saved diagrams/${file}.`); } catch (e) { toast(e.message); }
  };
  $('#import').onchange = async e => {
    const f = e.target.files[0]; e.target.value = '';
    if (!f) return;
    if (f.size > 1_000_000) { toast('File is larger than 1 MB.'); return; }
    try { setDiagram(JSON.parse(await f.text())); toast('Imported. Review any MCP commands before running.'); } catch (err) { toast('Import failed: ' + err.message); }
  };
  $('#diagram-name').addEventListener('input', () => save());
  $('#mode').onchange = () => changed();
  $('#run').onclick = runDiagram;
  $('#inbox').onclick = openInbox;
  $('#reach').onclick = () => { const p = $('#reach-panel'); if (!p.hidden) { p.hidden = true; return; } openReach(); };
  $('#evals').onclick = () => { const p = $('#evals-panel'); p.hidden = !p.hidden; if (!p.hidden) renderEvals(); };
  $('#export-py').onclick = exportPython;
  refreshInbox();
  $('#approve').onclick = () => decide(true);
  $('#reject').onclick = () => decide(false);
  $('#close-run').onclick = () => { $('#run-panel').hidden = true; };
  document.addEventListener('keydown', e => {
    if (e.target.closest('input,textarea,select')) return;
    if ((e.key === 'Delete' || e.key === 'Backspace') && selected) {
      e.preventDefault();
      if (selected.kind === 'block') removeBlock(selected.id); else removeWire(selected.list, selected.index);
    }
    if (e.key === 'Escape') select(null);
  });
  validate();
}
init();
