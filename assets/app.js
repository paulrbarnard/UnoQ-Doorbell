// Doorbell test page: live view, zones, status, events, settings.
'use strict';

const $ = (id) => document.getElementById(id);
const api = (path, opts = {}) =>
  fetch(path, { cache: 'no-store', ...opts }).then((r) => {
    if (!r.ok) throw new Error(`${path}: HTTP ${r.status}`);
    return r.json();
  });
const post = (path, body) =>
  api(path, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body ?? {}) });

let config = null;
let status = null;
let editZone = '';
let drag = null; // {x0, y0, x1, y1} fractions while drawing

const ZONE_COLOURS = { approach: '#00c8ff', doorstep: '#ffc800', postbox: '#ff50c8' };

// ---- live view + overlay ------------------------------------------------

// When the app restarts, the MJPEG connection just ends without an error
// event and the image stays blank, so the status poll restarts the stream
// whenever the board comes back, and so does returning to the tab.
function startStream() {
  const img = $('stream');
  img.onerror = () => setTimeout(startStream, 2000);
  img.src = '/stream.mjpg?' + Date.now();
}

function drawOverlay() {
  const canvas = $('overlay');
  const rect = canvas.getBoundingClientRect();
  canvas.width = rect.width * devicePixelRatio;
  canvas.height = rect.height * devicePixelRatio;
  const ctx = canvas.getContext('2d');
  const w = canvas.width, h = canvas.height;
  ctx.lineWidth = 2 * devicePixelRatio;
  ctx.font = `${12 * devicePixelRatio}px system-ui`;
  if (config) {
    for (const [name, z] of Object.entries(config.zones)) {
      const zone = drag && name === editZone ? [drag.x0, drag.y0, drag.x1, drag.y1] : z;
      ctx.strokeStyle = ctx.fillStyle = ZONE_COLOURS[name] || '#ccc';
      ctx.setLineDash(name === editZone ? [8, 4] : []);
      ctx.strokeRect(zone[0] * w, zone[1] * h, (zone[2] - zone[0]) * w, (zone[3] - zone[1]) * h);
      ctx.fillText(name, zone[0] * w + 4, zone[1] * h + 14 * devicePixelRatio);
    }
  }
  ctx.setLineDash([]);
  for (const p of status?.people ?? []) {
    const [x0, y0, x1, y1] = p.box;
    ctx.strokeStyle = ctx.fillStyle = p.at_door ? '#22c55e' : '#f97316';
    ctx.strokeRect(x0 * w, y0 * h, (x1 - x0) * w, (y1 - y0) * h);
    ctx.fillText(`${Math.round(p.confidence * 100)}%`, x0 * w + 2, y0 * h - 4);
  }
}

function pointer(ev) {
  const r = $('overlay').getBoundingClientRect();
  return {
    x: Math.min(1, Math.max(0, (ev.clientX - r.left) / r.width)),
    y: Math.min(1, Math.max(0, (ev.clientY - r.top) / r.height)),
  };
}

function setupZoneEditor() {
  const canvas = $('overlay');
  $('zone-select').onchange = (e) => {
    editZone = e.target.value;
    $('view').classList.toggle('editing', !!editZone);
    drawOverlay();
  };
  canvas.addEventListener('pointerdown', (ev) => {
    if (!editZone) return;
    const p = pointer(ev);
    drag = { sx: p.x, sy: p.y, x0: p.x, y0: p.y, x1: p.x, y1: p.y };
    canvas.setPointerCapture(ev.pointerId);
  });
  canvas.addEventListener('pointermove', (ev) => {
    if (!drag) return;
    const p = pointer(ev);
    Object.assign(drag, {
      x0: Math.min(drag.sx, p.x), x1: Math.max(drag.sx, p.x),
      y0: Math.min(drag.sy, p.y), y1: Math.max(drag.sy, p.y),
    });
    drawOverlay();
  });
  canvas.addEventListener('pointerup', async () => {
    if (!drag) return;
    const zone = [drag.x0, drag.y0, drag.x1, drag.y1];
    drag = null;
    if (zone[2] - zone[0] < 0.02 || zone[3] - zone[1] < 0.02) return drawOverlay();
    try {
      config = await post('/api/config', { zones: { [editZone]: zone } });
      $('zone-hint').textContent = `Saved ${editZone} zone.`;
    } catch (e) {
      $('zone-hint').textContent = e.message;
    }
    drawOverlay();
  });
  new ResizeObserver(drawOverlay).observe($('view'));
}

// ---- status ------------------------------------------------------------

const ago = (t) => (t ? `${Math.max(0, Math.round(Date.now() / 1000 - t))}s ago` : 'never');
const clock = (t) => new Date(t * 1000).toLocaleString();

function chip(text, cls) {
  const s = document.createElement('span');
  s.className = `chip ${cls}`;
  s.textContent = text;
  return s;
}

function renderStatus() {
  const s = status;
  const cam = s.camera, det = s.detector, visit = s.visit, hk = s.homekit;
  const hkFresh = hk.seen && Date.now() / 1000 - hk.seen < 60;
  $('chips').replaceChildren(
    chip(`camera ${cam.state}`, cam.state === 'streaming' ? 'ok' : 'bad'),
    chip(`detector ${det.state}`, det.state === 'running' ? 'ok' : 'warn'),
    chip(visit.state === 'idle' ? 'nobody at door' : `visit: ${visit.state}`, visit.state === 'idle' ? '' : 'hot'),
    chip(s.bridge_ok ? 'button link ok' : 'button link down', s.bridge_ok ? 'ok' : 'warn'),
    chip(hkFresh ? (hk.paired ? 'HomeKit paired' : 'HomeKit not paired') : 'HomeKit offline', hkFresh && hk.paired ? 'ok' : 'warn'),
    chip(s.push_enabled ? 'push on' : 'push off', s.push_enabled ? 'ok' : ''),
  );
  const rows = [
    ['Camera', `${cam.size ? cam.size.join('×') : '?'} @ ${cam.fps} fps${cam.error ? ' — ' + cam.error : ''}`],
    ['Detector', `${det.fps} fps, ${det.last_ms ?? '?'} ms/frame, ${det.people} people${det.ignored ? `, ${det.ignored} ignored (not moving)` : ''}${det.error ? ' — ' + det.error : ''}`],
    ['Visit', visit.state === 'idle' ? 'idle' : `${visit.state} since ${clock(visit.since)}, bells ${visit.bells}`],
    ['Doorstep reference', s.reference_age_s == null ? 'none yet (needs an empty doorstep)' : `${s.reference_age_s}s old`],
    ['Last push', s.push.time ? `${s.push.ok ? 'ok' : 'failed'} ${ago(s.push.time)} (${s.push.detail})` : s.push.detail],
    ['HomeKit', hk.seen ? `${hk.paired ? 'paired' : 'not paired'}${hk.setup_code ? ', code ' + hk.setup_code : ''}, seen ${ago(hk.seen)}` : 'companion not running'],
  ];
  $('status').replaceChildren(
    ...rows.flatMap(([k, v]) => {
      const dt = document.createElement('dt');
      const dd = document.createElement('dd');
      dt.textContent = k;
      dd.textContent = v;
      return [dt, dd];
    }),
  );
  drawOverlay();
}

let boardDown = false;

async function pollStatus() {
  try {
    status = await api('/api/status');
    if (boardDown) startStream();
    boardDown = false;
    renderStatus();
  } catch (e) {
    boardDown = true;
    $('chips').replaceChildren(chip('board unreachable', 'bad'));
  }
  setTimeout(pollStatus, 1000);
}

// ---- events ------------------------------------------------------------

const imgUrl = (ev, name) => `/api/events/${ev.id}/${name}`;
const KIND_TEXT = { bell: 'Bell', delivery: 'Delivery', post: 'Post', visitor: 'Visitor', caller: 'Caller' };

function describe(ev) {
  if (ev.kind === 'bell') return `Button (${ev.source})`;
  const parts = [`${ev.dwell_s}s at door`];
  if (ev.bells) parts.push(`rang ${ev.bells}×`);
  if (ev.parcel_left != null) parts.push(`doorstep change ${Math.round(ev.doorstep_change * 100)}%`);
  if (ev.from_house) parts.push('came from the house');
  if (ev.into_house) parts.push('went into the house');
  if (ev.doorstep_unsettled) parts.push('doorstep never went still, parcel unknown');
  return parts.join(', ');
}

async function loadEvents() {
  const events = await api('/api/events?limit=60');
  $('events').replaceChildren(
    ...events.map((ev) => {
      const li = document.createElement('li');
      const thumb = ev.kind === 'post' && ev.images.includes('postbox') ? 'postbox' : ev.images.includes('arrival') ? 'arrival' : ev.images[0];
      li.innerHTML = `<img loading="lazy" alt="" /><div><div class="kind"></div><div class="meta"></div><div class="meta"></div></div>`;
      if (thumb) li.querySelector('img').src = imgUrl(ev, thumb);
      li.querySelector('.kind').textContent = KIND_TEXT[ev.kind] || ev.kind;
      li.querySelector('.kind').classList.add(ev.kind);
      const metas = li.querySelectorAll('.meta');
      metas[0].textContent = clock(ev.time);
      metas[1].textContent = describe(ev);
      li.onclick = () => showEvent(ev);
      return li;
    }),
  );
}

function showEvent(ev) {
  $('event-title').textContent = `${KIND_TEXT[ev.kind] || ev.kind} — ${clock(ev.time)}`;
  $('event-detail').textContent = describe(ev);
  $('event-shots').replaceChildren(
    ...ev.images.map((name) => {
      const fig = document.createElement('figure');
      fig.innerHTML = '<img alt="" /><figcaption></figcaption>';
      fig.querySelector('img').src = imgUrl(ev, name);
      fig.querySelector('figcaption').textContent = name.replace('_', ' ');
      return fig;
    }),
  );
  $('event-dialog').showModal();
}

// ---- settings ----------------------------------------------------------

function fillSettings() {
  for (const el of $('settings').elements) {
    if (!el.name) continue;
    const [section, key] = el.name.split('.');
    const v = config[section]?.[key];
    if (el.type === 'checkbox') el.checked = !!v;
    else el.value = v ?? '';
  }
}

async function saveSettings(ev) {
  ev.preventDefault();
  const body = {};
  for (const el of $('settings').elements) {
    if (!el.name) continue;
    const [section, key] = el.name.split('.');
    body[section] ??= {};
    body[section][key] = el.type === 'checkbox' ? el.checked : el.type === 'number' ? Number(el.value) : el.value.trim();
  }
  try {
    config = await post('/api/config', body);
    $('settings-msg').textContent = 'Saved.';
  } catch (e) {
    $('settings-msg').textContent = e.message;
  }
}

// ---- wiring ------------------------------------------------------------

async function init() {
  startStream();
  setupZoneEditor();
  config = await api('/api/config');
  fillSettings();
  $('settings').onsubmit = saveSettings;
  $('btn-bell').onclick = () => post('/api/test/bell').then(loadEvents);
  $('btn-delivery').onclick = () => post('/api/test/delivery').then(loadEvents);
  $('btn-push').onclick = () =>
    post('/api/test/push').then((r) => alert(r.enabled ? 'Test push sent with the bell sound.' : 'Set the Pushover token and user key in Settings first.'));
  pollStatus();
  document.addEventListener('visibilitychange', () => {
    if (document.visibilityState === 'visible') startStream();
  });
  loadEvents();
  setInterval(loadEvents, 30000);
  try {
    const ui = new WebUI();
    ui.on_message('event', loadEvents);
  } catch (e) {
    console.warn('socket.io unavailable (dev server?)', e);
  }
}

init();
