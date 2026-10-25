'use strict';

const ui = { data: null, connected: false, selected: null, selectedJob: null, filter: 'all', query: '', view: 'radar', busy: false };
const $ = selector => document.querySelector(selector);
const names = { dexscreener: 'DEX Screener', dex_discovery: 'Token discovery', pool_verification: 'Pool cross-check', x_stream: 'X stream', solana_stream: 'Solana stream' };
const statuses = { ok: 'Connected', error: 'Interrupted', degraded: 'Partial', unconfigured: 'Not configured', connecting: 'Connecting', waiting: 'Waiting', capped: 'Daily cap' };
const kinds = { volume_acceleration: 'Volume acceleration', price_move: 'Price move', liquidity_change: 'Liquidity change', new_active_pool: 'New active pool', source_post: 'Source post', watched_transaction: 'Watched transaction' };
const evidenceNames = { provider_observation: 'Provider observation', firsthand_post: 'Source claim', transaction_log: 'Transaction log' };

function el(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined && text !== null) node.textContent = String(text);
  return node;
}
function time(value) {
  if (!value) return '—';
  const prefix = ui.data && value.slice(0,10) !== ui.data.now.slice(0,10) ? value.slice(0,10) + ' ' : '';
  return prefix + new Date(value).toLocaleTimeString('en-GB', { timeZone: 'UTC', hour12: false }) + ' UTC';
}
function age(value) {
  const seconds = Math.max(0, Math.floor((Date.now() - new Date(value).getTime()) / 1000));
  return seconds < 60 ? `${seconds}s ago` : seconds < 3600 ? `${Math.floor(seconds / 60)}m ago` : `${Math.floor(seconds / 3600)}h ago`;
}
function money(value) {
  if (typeof value !== 'number') return String(value ?? '—');
  return new Intl.NumberFormat('en-CA', { style: 'currency', currency: 'USD', notation: value >= 1000 ? 'compact' : 'standard', maximumFractionDigits: 1 }).format(value);
}
function metric(name, value) {
  if (/liquidity|volume|pace/i.test(name) && typeof value === 'number') return money(value);
  if (name === 'Acceleration') return `${value}×`;
  if (/change/i.test(name) && typeof value === 'number') return `${value > 0 ? '+' : ''}${value}%`;
  return String(value ?? 'Unknown');
}
function dot(status) {
  return el('span', 'dot ' + (status === 'ok' ? '' : status === 'error' || status === 'degraded' ? 'bad' : 'dim'));
}
function sourceStatus(source) {
  if (!ui.connected) return 'Unknown';
  return source?.status === 'ok' && !source.fresh ? 'Stale' : statuses[source?.status] || 'Waiting';
}
function notify(message) {
  $('#toast').textContent = message;
  $('#toast').hidden = false;
  clearTimeout(notify.timer);
  notify.timer = setTimeout(() => { $('#toast').hidden = true; }, 5500);
}
function safeLink(value, text) {
  try {
    const url = new URL(value);
    if (url.protocol !== 'https:' || url.username || url.password) return el('span', '', 'Source link unavailable');
    const link = el('a', 'source-link', text);
    link.href = url.href;
    link.target = '_blank';
    link.rel = 'noopener noreferrer';
    return link;
  } catch { return el('span', '', 'Source link unavailable'); }
}
function empty(title, detail) {
  const box = el('div', 'empty');
  box.append(el('strong', '', title), el('p', '', detail));
  return box;
}
function preserveFocus(container) {
  const previous = document.activeElement;
  if (!previous || !container.contains(previous)) return () => {};
  const id = previous.id;
  const candidateId = previous.dataset?.candidateId;
  const jobId = previous.dataset?.jobId;
  const href = previous.tagName === 'A' ? previous.href : null;
  const selection = typeof previous.selectionStart === 'number' ? [previous.selectionStart, previous.selectionEnd] : null;
  return () => {
    let replacement = id ? document.getElementById(id) : null;
    if (candidateId) replacement = [...container.querySelectorAll('[data-candidate-id]')].find(node => node.dataset.candidateId === candidateId);
    if (jobId) replacement = [...container.querySelectorAll('[data-job-id]')].find(node => node.dataset.jobId === jobId);
    if (href) replacement = [...container.querySelectorAll('a')].find(node => node.href === href);
    if (!replacement || replacement.disabled) { container.tabIndex = -1; replacement = container; }
    replacement.focus({preventScroll:true});
    if (selection && typeof replacement.setSelectionRange === 'function') replacement.setSelectionRange(...selection);
  };
}
function candidates() {
  if (!ui.data) return [];
  return ui.data.candidates.filter(item => {
    const active = ui.connected && item.fresh && item.status !== 'dismissed';
    const matches = !ui.query || `${item.headline} ${item.token} ${item.symbol} ${item.kind}`.toLowerCase().includes(ui.query);
    return matches && (ui.filter === 'all' || (ui.filter === 'drafted' ? item.status === 'drafted' : active && ['new', 'queued', 'researching'].includes(item.status)));
  }).sort((a, b) => Number(b.fresh && b.status !== 'dismissed') - Number(a.fresh && a.status !== 'dismissed') || b.score - a.score);
}
function renderQueue() {
  const items = candidates();
  if (!items.some(item => item.id === ui.selected)) ui.selected = items[0]?.id ?? null;
  $('#candidate-count').textContent = `${ui.connected ? items.filter(item => item.fresh && item.status !== 'dismissed').length : 0} active`;
  const list = $('#candidate-list');
  const restoreFocus = preserveFocus(list);
  const scroll = list.scrollTop;
  list.replaceChildren();
  if (!items.length) list.append(empty('No qualifying signals.', ui.query ? 'Try a different token, contract or filter.' : 'The radar is waiting for fresh activity above its thresholds. Quiet is a valid result.'));
  for (const [index, item] of items.entries()) {
    const row = el('button', 'candidate' + (item.id === ui.selected ? ' selected' : '') + (!item.fresh ? ' stale' : ''));
    row.setAttribute('aria-pressed', String(item.id === ui.selected));
    row.dataset.candidateId = item.id;
    row.append(el('span', 'rank', String(index + 1).padStart(2, '0')));
    const body = el('div');
    body.append(el('div', 'eyeline', `${kinds[item.kind] || item.kind} · ${age(item.first_seen)} · ${!item.fresh && item.status !== 'expired' ? 'expired · ' : ''}${item.status.replaceAll('_', ' ')}`), el('h3', '', item.headline), el('p', '', item.reason));
    const metrics = el('div', 'row-metrics');
    for (const [name, value] of Object.entries(item.metrics || {}).slice(0, 2)) metrics.append(el('span', '', `${metric(name, value)}${name === '5m volume' ? ' / 5m' : name === 'Acceleration' ? ' baseline' : ''}`));
    body.append(metrics);
    row.append(body);
    row.addEventListener('click', () => { ui.selected = item.id; renderQueue(); renderInspector(); });
    list.append(row);
  }
  list.scrollTop = scroll;
  restoreFocus();
}
function renderInspector() {
  const inspector = $('#inspector');
  const restoreFocus = preserveFocus(inspector);
  inspector.replaceChildren(el('h2', '', 'Evidence'));
  const item = ui.data?.candidates.find(candidate => candidate.id === ui.selected);
  if (!item) { inspector.append(empty('A lead needs receipts.', 'Select a candidate to inspect its source, age, observed values and investigation status.')); restoreFocus(); return; }
  inspector.append(el('h3', '', item.headline), el('span', 'evidence-tag', evidenceNames[item.evidence_type] || item.evidence_type),
    el('p', 'source-name', names[item.source] || item.source), el('p', 'fetched', `Fetched ${time(item.fetched_at)}`));
  if (item.evidence_type === 'provider_observation') inspector.append(el('p', 'fetched', 'Provider update time unknown · fetch time is not data age'));
  const table = el('dl', 'metric-table');
  for (const [name, value] of Object.entries(item.metrics || {})) table.append(el('dt', '', name), el('dd', '', metric(name, value)));
  inspector.append(table, el('h4', '', 'Why this surfaced'), el('p', 'reason', item.reason), safeLink(item.source_url, 'Open source'));
  if (item.token) {
    inspector.append(el('label', 'contract-label', 'Contract address'));
    const field = el('div', 'contract');
    const input = el('input');
    input.id = 'token-contract'; input.value = item.token; input.readOnly = true; input.setAttribute('aria-label', 'Token contract address');
    const copy = el('button', '', 'Copy');
    copy.id = 'copy-contract';
    copy.addEventListener('click', async () => { try { await navigator.clipboard.writeText(item.token); notify('Contract copied.'); } catch { input.select(); notify('Select and copy the contract address.'); } });
    field.append(input, copy); inspector.append(field);
  }
  const handled = !['new', 'no_story'].includes(item.status);
  const capped = ui.data.budget.total >= ui.data.budget.limit || ui.data.budget.triggered >= ui.data.budget.triggered_limit;
  const priorJobs = ui.data.jobs.filter(job => {
    const target = job.details.candidate;
    return job.state !== 'cancelled' && (['queued','researching'].includes(job.state) || job.started_at) && target && (target.subject === item.subject || (item.token && target.token === item.token));
  });
  const until = Math.max(0, ...priorJobs.map(job => new Date(job.started_at || job.created_at).getTime() + (ui.data.subject_cooldown_seconds || 1800) * 1000));
  const cooling = until > Date.now();
  const actionText = handled ? item.status.replaceAll('_', ' ').replace(/^./, letter => letter.toUpperCase()) : !item.fresh ? 'Expired signal' : capped ? 'Daily research cap reached' : cooling ? 'Research cooldown' : 'Queue investigation';
  const action = el('button', 'primary', actionText);
  action.id = 'queue-investigation';
  action.disabled = !ui.connected || handled || !item.fresh || capped || cooling;
  action.addEventListener('click', () => act(item.id, 'queue'));
  const explanation = !ui.connected ? 'Connection lost. Freshness must be confirmed before queuing research.' : !item.fresh ? 'This signal has expired. Its evidence stays inspectable; it cannot be queued.' : !handled && capped ? 'The research budget resets on the next UTC day. Collection continues.' : !handled && cooling ? `Research for this subject resumes at ${time(new Date(until).toISOString())}.` : ui.data.research.enabled ? 'No explanation has been independently verified yet.' : 'Research is paused. Queued work will wait; collection continues.';
  inspector.append(action, el('p', 'investigation-note', explanation));
  const dismiss = el('button', 'secondary', 'Dismiss candidate');
  dismiss.id = 'dismiss-candidate';
  dismiss.disabled = !ui.connected || item.status === 'researching';
  dismiss.addEventListener('click', () => act(item.id, 'dismiss'));
  inspector.append(dismiss);
  const job = ui.data.jobs.find(record => record.candidate_id === item.id);
  if (job) {
    const info = el('div', 'draft-copy');
    info.append(el('h4', '', 'Investigation'), el('p', '', job.state.replaceAll('_', ' ')));
    if (job.details.error) info.append(el('p', '', job.details.error));
    for (const storyId of job.details.story_ids || []) {
      const story = ui.data.stories.find(record => record.id === storyId);
      if (!story) continue;
      const expired = new Date(story.expires_at).getTime() <= Date.now();
      info.append(el('h4', '', story.headline), el('p', '', story.text_short), el('p', 'investigation-note', `Source-attributed draft · ${expired ? 'EXPIRED' : 'expires'} ${time(story.expires_at)}`));
    }
    inspector.append(info);
  }
  restoreFocus();
}
async function act(id, action) {
  try {
    const response = await fetch('/api/candidate', { method: 'POST', headers: { 'Content-Type': 'application/json', 'X-Radar-Token': ui.data.csrf_token }, body: JSON.stringify({ id, action }) });
    const result = await response.json();
    if (!response.ok) throw new Error(result.error || 'Action unavailable.');
    notify(action === 'queue' ? 'Investigation queued within the daily cap.' : 'Candidate dismissed.');
    await refresh();
  } catch (error) { notify(error.message); }
}
function renderJobs() {
  const view = $('#jobs-view'); const restoreFocus = preserveFocus(view); const scroll = view.scrollTop; view.replaceChildren();
  if (!ui.data.jobs.length) { view.append(empty('No investigations yet.', 'Candidates are collected continuously. Research jobs appear here with their completion or failure state.')); return; }
  const table = el('table', 'records');
  const head = el('tr'); for (const name of ['Investigation', 'State', 'Started / UTC']) head.append(el('th', '', name));
  const thead = el('thead'); thead.append(head); table.append(thead);
  const body = el('tbody');
  for (const job of ui.data.jobs) {
    const row = el('tr'); const titleCell = el('td');
    const item = ui.data.candidates.find(candidate => candidate.id === job.candidate_id);
    const title = el('button', '', job.details.candidate?.headline || item?.headline || 'Background newsroom sweep');
    title.dataset.jobId = job.id;
    title.setAttribute('aria-pressed', String(ui.selectedJob === job.id));
    title.addEventListener('click', () => { ui.selectedJob = job.id; renderJobs(); });
    titleCell.append(title, el('span', 'record-detail', job.details.error || (job.details.story_ids?.length ? `${job.details.story_ids.length} source-attributed draft(s)` : `${job.lane} · reserved calls include failed or uncertain requests`)));
    row.append(titleCell, el('td', '', job.state.replaceAll('_', ' ')), el('td', 'time-cell', time(job.started_at || job.created_at)));
    body.append(row);
  }
  table.append(body); view.append(table);
  const selectedJob = ui.data.jobs.find(job => job.id === ui.selectedJob);
  if (selectedJob) {
    const detail = el('section', 'job-evidence');
    detail.append(el('h2', '', 'Investigation evidence'), el('p', 'fetched', `${selectedJob.state.replaceAll('_', ' ')} · ${time(selectedJob.finished_at || selectedJob.started_at)}`));
    if (selectedJob.details.error) detail.append(el('p', 'reason', selectedJob.details.error));
    for (const storyId of selectedJob.details.story_ids || []) {
      const story = ui.data.stories.find(record => record.id === storyId);
      if (!story) continue;
      const article = el('article', 'job-story');
      const expired = new Date(story.expires_at).getTime() <= Date.now();
      article.append(el('h3', '', story.headline), el('p', 'fetched', `Source-attributed draft · ${expired ? 'EXPIRED' : 'expires'} ${time(story.expires_at)}`), el('p', 'reason', story.text_medium));
      if (story.text_extra) article.append(el('p', 'reason', story.text_extra));
      for (const [index, url] of (story.source_urls || []).entries()) article.append(safeLink(url, `Open source ${index + 1}`));
      detail.append(article);
    }
    view.append(detail);
  }
  view.scrollTop = scroll;
  restoreFocus();
}
function renderSources() {
  const view = $('#sources-view'); view.replaceChildren();
  const table = el('table', 'records'); const head = el('tr');
  for (const title of ['Source', 'Status', 'Last successful fetch / UTC']) head.append(el('th', '', title));
  const thead = el('thead'); thead.append(head); table.append(thead); const body = el('tbody');
  for (const source of ui.data.sources) {
    const row = el('tr'); const name = el('td', '', names[source.id] || source.id);
    name.append(el('span', 'record-detail', source.details.note || ''));
    row.append(name, el('td', '', sourceStatus(source)), el('td', 'time-cell', time(source.last_success))); body.append(row);
  }
  table.append(body); view.append(table);
}
function setView(view) {
  ui.view = view;
  $('.toolbar').hidden = view !== 'radar';
  for (const id of ['radar', 'jobs', 'sources']) $(`#${id}-view`).hidden = view !== id;
  $('#page-title').textContent = view === 'radar' ? 'The tape, with receipts.' : view === 'jobs' ? 'Follow the investigation.' : 'Know what we can see.';
  $('#page-subtitle').textContent = view === 'radar' ? 'Signals first. Stories when the evidence holds.' : view === 'jobs' ? 'Every request has a state. Failed calls stay counted.' : 'Transport health and data freshness are separate.';
  document.querySelectorAll('[data-view]').forEach(button => { const current = button.dataset.view === view; button.classList.toggle('active', current); if (current) button.setAttribute('aria-current', 'page'); else button.removeAttribute('aria-current'); });
  render();
}
function render() {
  if (!ui.data) return;
  for (const name of ['total', 'background', 'triggered']) $(`#budget-${name}`).textContent = `${ui.data.budget[name]} / ${ui.data.budget[name === 'total' ? 'limit' : name + '_limit']}`;
  $('#budget-note').textContent = `${ui.data.budget.day} UTC · ${ui.data.budget.manual} manual · failed calls counted`;
  const collector = ui.data.collector;
  const alive = ui.connected && collector.status === 'collecting' && Date.now() - new Date(collector.heartbeat).getTime() < Math.max(90000, ui.data.poll_seconds * 3000);
  const status = $('#collector-status'); status.replaceChildren(dot(!ui.connected ? 'error' : alive ? 'ok' : 'waiting'), document.createTextNode(!ui.connected ? 'Disconnected' : alive ? 'Collecting' : collector.status === 'error' ? 'Collector error' : 'Collector stopped'));
  $('#last-scan').textContent = collector.last_scan_at ? `Last scan ${time(collector.last_scan_at)}` : 'Waiting for the first scan';
  document.querySelectorAll('[data-filter]').forEach(button => { const selected = button.dataset.filter === ui.filter; button.classList.toggle('selected', selected); button.setAttribute('aria-pressed', String(selected)); });
  renderQueue(); renderInspector(); renderJobs(); renderSources();
  const footer = $('#sourcebar'); footer.replaceChildren(el('strong', '', 'Source health'));
  for (const id of ['dexscreener', 'x_stream', 'solana_stream']) {
    const source = ui.data.sources.find(value => value.id === id);
    const entry = el('span', 'source-status'); entry.append(dot(ui.connected && source?.fresh ? source.status : 'waiting'), document.createTextNode(names[id]), el('small', '', sourceStatus(source))); footer.append(entry);
  }
}
async function refresh() {
  if (ui.busy) return;
  ui.busy = true;
  try {
    const response = await fetch('/api/state', { cache: 'no-store' });
    if (!response.ok) throw new Error('Newsroom unavailable.');
    ui.data = await response.json();
    ui.connected = true;
    $('#connection-error').hidden = true; render();
  } catch {
    ui.connected = false;
    if (ui.data) render();
    $('#connection-error').hidden = false;
    const status = $('#collector-status'); status.replaceChildren(dot('error'), document.createTextNode('Disconnected'));
    if (!ui.data) $('#candidate-list').replaceChildren(empty('Waiting for the newsroom.', 'The local collector may still be starting. This view reconnects automatically.'));
  } finally { ui.busy = false; }
}
document.querySelectorAll('[data-view]').forEach(button => button.addEventListener('click', () => setView(button.dataset.view)));
document.querySelectorAll('[data-filter]').forEach(button => button.addEventListener('click', () => { ui.filter = button.dataset.filter; render(); }));
$('#search').addEventListener('input', event => { ui.query = event.target.value.trim().toLowerCase(); renderQueue(); renderInspector(); });
refresh();
setInterval(refresh, 5000);
