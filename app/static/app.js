'use strict';

const $ = (id) => document.getElementById(id);
const state = {
  caps: null,
  jobs: [],
  activeId: null,
  active: null,       // full job with segments
  liveSegments: [],   // segments streamed while a job is still running
  tab: 'transcript',
  source: null,       // EventSource for the active job
  model: 'turbo',
};

const clock = (s) => {
  s = Math.max(0, Math.floor(s || 0));
  const h = Math.floor(s / 3600), m = Math.floor((s % 3600) / 60), sec = s % 60;
  return h ? `${h}:${String(m).padStart(2, '0')}:${String(sec).padStart(2, '0')}`
           : `${String(m).padStart(2, '0')}:${String(sec).padStart(2, '0')}`;
};
const esc = (t) => String(t ?? '').replace(/[&<>"]/g,
  (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]));

/* ---------------- capabilities & options ---------------- */

async function loadCaps() {
  state.caps = await (await fetch('/api/capabilities')).json();
  state.model = state.caps.default_model;

  const labels = { turbo: 'Turbo', large: 'Large', medium: 'Medium', small: 'Small' };
  $('model-seg').innerHTML = state.caps.models
    .map((m) => `<button data-model="${m}" aria-selected="${m === state.model}">${labels[m] || m}</button>`)
    .join('');
  $('model-seg').onclick = (e) => {
    const btn = e.target.closest('button');
    if (!btn) return;
    state.model = btn.dataset.model;
    [...$('model-seg').children].forEach((b) =>
      b.setAttribute('aria-selected', String(b === btn)));
  };

  wireOptional('diarize', state.caps.diarization,
    'Requires pyannote + HF_TOKEN — see the README');
  wireOptional('summary', state.caps.summarization,
    'Set ANTHROPIC_API_KEY to enable');

  $('opt-diarize').onchange = (e) =>
    $('field-speakers').classList.toggle('show', e.target.checked);
}

function wireOptional(key, available, disabledHint) {
  const input = $(`opt-${key}`);
  const row = $(`row-${key}`);
  if (available) return;
  input.disabled = true;
  input.checked = false;
  row.classList.add('disabled');
  $(`${key}-hint`).textContent = disabledHint;
}

/* ---------------- upload ---------------- */

function wireUpload() {
  const dz = $('dropzone'), input = $('file-input');
  dz.onclick = () => input.click();
  input.onchange = () => { upload([...input.files]); input.value = ''; };
  ['dragenter', 'dragover'].forEach((ev) => dz.addEventListener(ev, (e) => {
    e.preventDefault(); dz.classList.add('drag');
  }));
  ['dragleave', 'drop'].forEach((ev) => dz.addEventListener(ev, (e) => {
    e.preventDefault(); dz.classList.remove('drag');
  }));
  dz.addEventListener('drop', (e) => upload([...(e.dataTransfer?.files || [])]));
}

async function upload(files) {
  for (const file of files) {
    const fd = new FormData();
    fd.append('file', file);
    fd.append('model', state.model);
    fd.append('language', $('opt-language').value.trim());
    fd.append('diarize', $('opt-diarize').checked);
    fd.append('summarize', $('opt-summary').checked);
    fd.append('num_speakers', $('opt-speakers').value.trim());
    try {
      const res = await fetch('/api/jobs', { method: 'POST', body: fd });
      if (!res.ok) { alert((await res.json()).detail || 'Upload failed'); continue; }
      const job = await res.json();
      await refreshJobs();
      select(job.id);
    } catch (err) {
      alert(`Upload failed: ${err.message}`);
    }
  }
}

/* ---------------- job list ---------------- */

async function refreshJobs() {
  state.jobs = await (await fetch('/api/jobs')).json();
  renderJobs();
}

function renderJobs() {
  $('job-list').innerHTML = state.jobs.map((j) => {
    const pct = Math.round((j.progress || 0) * 100);
    const running = j.status === 'running';
    const stage = { prepare: 'Decoding', transcribe: 'Transcribing',
                    diarize: 'Speakers', summarize: 'Summarizing' }[j.stage] || j.stage;
    const line = running ? `${stage} ${pct}%`
      : j.status === 'error' ? 'Failed'
      : j.meta?.duration ? clock(j.meta.duration) : 'Queued';
    return `<div class="job ${j.id === state.activeId ? 'active' : ''}" data-id="${j.id}">
      <div class="job-name">${esc(j.filename)}</div>
      <div class="job-meta"><span class="dot ${j.status}"></span>${esc(line)}</div>
      ${running ? `<div class="bar"><i style="width:${pct}%"></i></div>` : ''}
    </div>`;
  }).join('') || '<p style="font-size:12.5px;color:var(--text-dim)">Nothing yet.</p>';

  $('job-list').onclick = (e) => {
    const el = e.target.closest('.job');
    if (el) select(el.dataset.id);
  };
}

/* ---------------- detail ---------------- */

async function select(id) {
  state.activeId = id;
  state.liveSegments = [];
  renderJobs();
  $('empty').hidden = true;
  $('detail').hidden = false;

  const job = await (await fetch(`/api/jobs/${id}`)).json();
  state.active = job;
  $('player').src = `/api/jobs/${id}/audio`;
  renderDetail();

  if (state.source) state.source.close();
  if (job.status === 'running' || job.status === 'queued') listen(id);
}

function listen(id) {
  const src = new EventSource(`/api/jobs/${id}/events`);
  state.source = src;
  src.onmessage = (e) => {
    const msg = JSON.parse(e.data);
    if (state.activeId !== id) return;

    if (msg.type === 'segment') {
      state.liveSegments.push(msg.segment);
      if (state.tab === 'transcript') renderContent();
    } else if (msg.type === 'progress') {
      Object.assign(state.active, { progress: msg.progress, stage: msg.stage });
      const j = state.jobs.find((x) => x.id === id);
      if (j) Object.assign(j, { progress: msg.progress, stage: msg.stage });
      renderJobs(); renderSub();
    } else if (msg.type === 'state') {
      Object.assign(state.active, msg.job);
      const idx = state.jobs.findIndex((x) => x.id === id);
      if (idx >= 0) state.jobs[idx] = { ...state.jobs[idx], ...msg.job };
      renderJobs(); renderDetail();
    } else if (msg.type === 'done') {
      state.active = msg.job;
      state.liveSegments = [];
      src.close();
      refreshJobs();
      renderDetail();
    }
  };
  src.onerror = () => src.close();
}

function renderDetail() {
  const job = state.active;
  if (!job) return;
  $('d-title').textContent = job.meta?.title || job.filename;
  renderSub();

  const done = job.status === 'done';
  $('exports').innerHTML = done ? `
    <a href="/read/${job.id}" target="_blank" title="Clean reading view">Read</a>
    <a href="/api/jobs/${job.id}/view/txt" target="_blank" title="Open the raw text">.txt</a>
    <button class="btn" id="save-folder" title="Write all formats to the transcripts folder">Save to folder</button>
    <span style="width:1px;height:18px;background:var(--border);margin:0 4px"></span>
    ` + ['md', 'txt', 'srt', 'vtt', 'json']
      .map((f) => `<a href="/api/jobs/${job.id}/download/${f}">${f.toUpperCase()}</a>`)
      .join('') : '';

  const saveBtn = $('save-folder');
  if (saveBtn) saveBtn.onclick = saveToFolder;

  const hasSummary = !!job.summary;
  $('tab-summary').style.display = (hasSummary || done) ? '' : 'none';
  if (!hasSummary && state.tab === 'summary' && !done) state.tab = 'transcript';
  $('tab-summary').onclick = () => { state.tab = 'summary'; renderTabs(); renderContent(); };
  $('tab-transcript').onclick = () => { state.tab = 'transcript'; renderTabs(); renderContent(); };
  renderTabs();
  renderContent();
}

function renderSub() {
  const job = state.active;
  const bits = [];
  if (job.meta?.duration) bits.push(clock(job.meta.duration));
  if (job.meta?.language) bits.push(job.meta.language.toUpperCase());
  if (job.meta?.speakers) bits.push(`${job.meta.speakers} speakers`);
  if (job.status === 'running') {
    const stage = { prepare: 'Decoding audio', transcribe: 'Transcribing',
                    diarize: 'Identifying speakers', summarize: 'Summarizing' }[job.stage] || job.stage;
    bits.push(`${stage}… ${Math.round((job.progress || 0) * 100)}%`);
  }
  $('d-sub').textContent = bits.join(' · ');
}

function renderTabs() {
  $('tab-summary').setAttribute('aria-selected', String(state.tab === 'summary'));
  $('tab-transcript').setAttribute('aria-selected', String(state.tab === 'transcript'));
}

function renderContent() {
  const job = state.active;
  const out = [];

  if (job.status === 'error') {
    out.push(`<div class="notice err">${esc(job.error || 'Something went wrong.')}</div>`);
  }
  if (job.meta?.diarization_error) {
    out.push(`<div class="notice warn"><strong>Speaker labels unavailable.</strong>
      ${esc(job.meta.diarization_error)}</div>`);
  }
  if (job.meta?.summary_error) {
    out.push(`<div class="notice warn"><strong>Summary unavailable.</strong>
      ${esc(job.meta.summary_error)}</div>`);
  }

  if (state.tab === 'summary') {
    out.push(renderSummary(job));
  } else {
    const segments = job.segments?.length ? job.segments : state.liveSegments;
    out.push(renderTranscript(segments, job.status));
  }
  $('content').innerHTML = out.join('');

  $('content').onclick = (e) => {
    const stamp = e.target.closest('.stamp');
    if (!stamp) return;
    const player = $('player');
    player.currentTime = parseFloat(stamp.dataset.t);
    player.play().catch(() => {});
  };
  const btn = $('run-summary');
  if (btn) btn.onclick = runSummary;
}

function renderSummary(job) {
  if (!job.summary) {
    if (job.status !== 'done') return '<p style="color:var(--text-dim)">Not available yet.</p>';
    if (!state.caps?.summarization) {
      return `<p style="color:var(--text-dim)">No summary. Set
        <code>ANTHROPIC_API_KEY</code> in <code>.env</code> and restart to enable this.</p>`;
    }
    return `<p style="color:var(--text-dim);margin-bottom:14px">
      No summary was generated for this recording.</p>
      <button class="btn" id="run-summary">Summarize with Claude</button>`;
  }
  const s = job.summary;
  const parts = [];
  if (s.headline) parts.push(`<div class="headline">${esc(s.headline)}</div>`);
  if (s.summary) {
    parts.push('<h3>Summary</h3>');
    parts.push(s.summary.split(/\n{2,}/).map((p) => `<p>${esc(p)}</p>`).join(''));
  }
  const list = (title, items) => items?.length
    ? `<h3>${title}</h3><ul>${items.map((i) => `<li>${esc(i)}</li>`).join('')}</ul>` : '';
  parts.push(list('Key points', s.key_points));
  parts.push(list('Decisions', s.decisions));
  if (s.action_items?.length) {
    parts.push(`<h3>Action items</h3><table>
      <thead><tr><th>Owner</th><th>Task</th><th>Due</th></tr></thead><tbody>
      ${s.action_items.map((a) => `<tr><td>${esc(a.owner || '—')}</td>
        <td>${esc(a.task)}</td><td>${esc(a.due || '—')}</td></tr>`).join('')}
      </tbody></table>`);
  }
  parts.push(list('Open questions', s.open_questions));
  return parts.join('');
}

function renderTranscript(segments, status) {
  if (!segments?.length) {
    return status === 'running'
      ? '<p style="color:var(--text-dim)">Listening… text will appear as it is decoded.</p>'
      : '<p style="color:var(--text-dim)">No transcript.</p>';
  }
  // Merge Whisper's prosody-sized fragments into readable paragraphs. Cut on a
  // speaker change, or on a pause once the turn is long enough to stand alone —
  // without that second rule an undiarized recording becomes one giant block.
  const MAX_TURN = 35, PAUSE = 1.2;
  const turns = [];
  const speakers = [];
  for (const seg of segments) {
    const who = seg.speaker || null;
    if (who && !speakers.includes(who)) speakers.push(who);
    const last = turns[turns.length - 1];
    const sameSpeaker = last && last.speaker === who;
    const shouldBreak = sameSpeaker &&
      ((seg.end - last.start >= MAX_TURN) || (seg.start - last.end >= PAUSE));
    if (sameSpeaker && !shouldBreak) {
      last.text += ' ' + seg.text;
      last.end = seg.end;
    } else {
      turns.push({ speaker: who, start: seg.start, end: seg.end, text: seg.text });
    }
  }
  return turns.map((t) => {
    const cls = t.speaker ? `sp${speakers.indexOf(t.speaker) % 6}` : '';
    const who = t.speaker ? `<div class="who ${cls}">${esc(t.speaker)}</div>` : '';
    return `<div class="turn" data-start="${t.start}" data-end="${t.end}">
      <button class="stamp" data-t="${t.start}">${clock(t.start)}</button>
      <div class="body">${who}<div>${esc(t.text)}</div></div>
    </div>`;
  }).join('');
}

async function saveToFolder() {
  const btn = $('save-folder');
  const original = btn.textContent;
  btn.disabled = true;
  btn.textContent = 'Saving…';
  try {
    const res = await fetch(`/api/jobs/${state.activeId}/save`, { method: 'POST' });
    const data = await res.json();
    if (!res.ok) throw new Error(data.detail || 'Failed');
    btn.textContent = data.revealed ? 'Revealed in Finder' : 'Saved';
    setTimeout(() => { btn.textContent = original; btn.disabled = false; }, 2500);
  } catch (err) {
    btn.textContent = original;
    btn.disabled = false;
    alert(err.message);
  }
}

async function runSummary() {
  const btn = $('run-summary');
  btn.disabled = true;
  btn.textContent = 'Summarizing…';
  try {
    const res = await fetch(`/api/jobs/${state.activeId}/summarize`, { method: 'POST' });
    const data = await res.json();
    if (!res.ok) throw new Error(data.detail || 'Failed');
    state.active.summary = data.summary;
    delete state.active.meta.summary_error;
    renderContent();
  } catch (err) {
    btn.disabled = false;
    btn.textContent = 'Summarize with Claude';
    alert(err.message);
  }
}

/* highlight the turn currently playing */
$('player').addEventListener('timeupdate', () => {
  const t = $('player').currentTime;
  for (const el of document.querySelectorAll('.turn')) {
    el.classList.toggle('playing',
      t >= parseFloat(el.dataset.start) && t < parseFloat(el.dataset.end));
  }
});

(async function init() {
  await loadCaps();
  wireUpload();
  await refreshJobs();
  // Land on something useful: reattach to whatever is still running, otherwise
  // open the most recent recording so a finished transcript is right there.
  const running = state.jobs.find((j) => j.status === 'running' || j.status === 'queued');
  const target = running || state.jobs[0];
  if (target) select(target.id);
})();
