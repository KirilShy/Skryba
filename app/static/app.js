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

/* Upload settings persist per browser: most people transcribe the same
   language and model every time, and retyping it each upload is friction.
   Storage can throw in private windows, so every access is guarded. */
const PREFS_KEY = 'skryba.prefs';
function loadPrefs() {
  try { return JSON.parse(localStorage.getItem(PREFS_KEY)) || {}; } catch { return {}; }
}
function savePrefs() {
  try {
    localStorage.setItem(PREFS_KEY, JSON.stringify({
      model: state.model,
      language: $('opt-language').value.trim(),
      diarize: $('opt-diarize').checked,
      summarize: $('opt-summary').checked,
      speakers: $('opt-speakers').value.trim(),
    }));
  } catch { /* private window, or storage disabled — not worth surfacing */ }
}

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
    savePrefs();
  };

  wireOptional('diarize', state.caps.diarization,
    'Requires pyannote + HF_TOKEN — see the README');
  wireOptional('summary', state.caps.summarization,
    'Set OPENROUTER_API_KEY or ANTHROPIC_API_KEY to enable');
  if (state.caps.summarization && state.caps.summary_provider) {
    $('summary-hint').textContent = state.caps.summary_provider;
  }

  const prefs = loadPrefs();
  if (prefs.model && state.caps.models.includes(prefs.model)) state.model = prefs.model;
  if (prefs.language) $('opt-language').value = prefs.language;
  if (prefs.speakers) $('opt-speakers').value = prefs.speakers;
  // Only restore a toggle the backend can actually honour right now.
  if (prefs.diarize && state.caps.diarization) $('opt-diarize').checked = true;
  if (prefs.summarize && state.caps.summarization) $('opt-summary').checked = true;
  $('field-speakers').classList.toggle('show', $('opt-diarize').checked);
  [...$('model-seg').children].forEach((b) =>
    b.setAttribute('aria-selected', String(b.dataset.model === state.model)));

  $('opt-diarize').onchange = (e) => {
    $('field-speakers').classList.toggle('show', e.target.checked);
    savePrefs();
  };
  ['opt-summary', 'opt-language', 'opt-speakers'].forEach((id) => {
    $(id).addEventListener('change', savePrefs);
  });
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
  // Deleting a job mid-run would race the worker thread still writing to its
  // file, so only offer it once nothing is actively touching that job.
  const canDelete = (j) => !['running', 'queued', 'paused'].includes(j.status);

  $('job-list').innerHTML = state.jobs.map((j) => {
    const pct = Math.round((j.progress || 0) * 100);
    const running = j.status === 'running';
    const stage = { prepare: 'Decoding', transcribe: 'Transcribing',
                    diarize: 'Speakers', summarize: 'Summarizing' }[j.stage] || j.stage;
    const line = running ? `${stage} ${pct}%`
      : j.status === 'paused' ? `Paused ${pct}%`
      : j.status === 'canceled' ? 'Canceled'
      : j.status === 'error' ? 'Failed'
      : j.meta?.duration ? clock(j.meta.duration) : 'Queued';
    return `<div class="job ${j.id === state.activeId ? 'active' : ''}" data-id="${j.id}">
      ${canDelete(j) ? `<button class="job-delete" data-delete-id="${j.id}"
        title="Delete recording" aria-label="Delete recording">&times;</button>` : ''}
      <div class="job-name">${esc(j.filename)}</div>
      <div class="job-meta"><span class="dot ${j.status}"></span>${esc(line)}</div>
      ${running ? `<div class="bar"><i style="width:${pct}%"></i></div>` : ''}
    </div>`;
  }).join('') || '<p style="font-size:12.5px;color:var(--text-dim)">Nothing yet.</p>';

  $('job-list').onclick = (e) => {
    const delBtn = e.target.closest('.job-delete');
    if (delBtn) { e.stopPropagation(); deleteJob(delBtn.dataset.deleteId); return; }
    const el = e.target.closest('.job');
    if (el) select(el.dataset.id);
  };
}

async function deleteJob(id) {
  const job = state.jobs.find((j) => j.id === id);
  if (!confirm(`Delete "${job ? job.filename : 'this recording'}"? This removes the recording and its transcript permanently.`)) return;
  try {
    const res = await fetch(`/api/jobs/${id}`, { method: 'DELETE' });
    if (!res.ok) throw new Error((await res.json()).detail || 'Delete failed');
  } catch (err) {
    alert(err.message);
    return;
  }
  if (state.activeId === id) {
    if (state.source) { state.source.close(); state.source = null; }
    state.activeId = null;
    state.active = null;
    $('detail').hidden = true;
    $('empty').hidden = false;
  }
  await refreshJobs();
}

/* ---------------- detail ---------------- */

async function select(id) {
  state.activeId = id;
  state.liveSegments = [];
  renderJobs();
  $('empty').hidden = true;
  $('detail').hidden = false;
  $('content').innerHTML = `<div class="loading">
    <span class="spinner"></span>Loading transcript…</div>`;

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
      if (state.tab === 'transcript') {
        const main = $('main');
        // Only auto-scroll if the user is already near the bottom — otherwise
        // they are reading something further up and we must not yank them away.
        const nearBottom =
          main.scrollHeight - main.scrollTop - main.clientHeight < 120;
        renderContent();
        if (nearBottom) main.scrollTop = main.scrollHeight;
      }
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

  // transport controls for a job that is still in flight.
  // A pause/cancel request only takes effect at the next chunk boundary, so
  // the button's busy state is driven off job.control (server truth, arrives
  // over SSE almost immediately) rather than a local flag — otherwise the very
  // next render (from the request's own response, or a later SSE tick) would
  // put back a live Pause/Cancel pair and make the click look like a no-op.
  const c = $('controls') || { };
  if (job.status === 'running' || job.status === 'queued') {
    if (job.control === 'cancel') {
      c.innerHTML = `<button class="btn" disabled>Cancelling…</button>`;
    } else if (job.control === 'pause') {
      c.innerHTML = `<button class="btn" disabled>Pausing…</button>`;
    } else {
      c.innerHTML = `<button class="btn" id="btn-pause">Pause</button>
                     <button class="btn" id="btn-cancel">Cancel</button>`;
      $('btn-pause').onclick = () => control('pause', 'Pausing…');
      $('btn-cancel').onclick = () => {
        if (confirm('Cancel this transcription? Finished parts are kept.')) control('cancel', 'Cancelling…');
      };
    }
  } else if (job.status === 'paused') {
    if (job.control === 'cancel') {
      c.innerHTML = `<button class="btn" disabled>Cancelling…</button>`;
    } else {
      c.innerHTML = `<button class="btn" id="btn-resume">Resume</button>
                     <button class="btn" id="btn-cancel">Cancel</button>`;
      $('btn-resume').onclick = () => control('resume', 'Resuming…');
      $('btn-cancel').onclick = () => control('cancel', 'Cancelling…');
    }
  } else if (job.status === 'error' || job.status === 'canceled') {
    c.innerHTML = `<button class="btn" id="btn-retry">Retry</button>
                   <button class="btn btn-danger" id="btn-delete">Delete</button>`;
    $('btn-retry').onclick = () => control('retry', 'Queueing…');
    $('btn-delete').onclick = () => deleteJob(job.id);
  } else if (job.status === 'done') {
    c.innerHTML = `<button class="btn btn-danger" id="btn-delete">Delete</button>`;
    $('btn-delete').onclick = () => deleteJob(job.id);
  } else {
    c.innerHTML = '';
  }

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
    // Persisted segments and the live stream have to be MERGED, not chosen
    // between: once the first chunk lands, job.segments is non-empty, and
    // preferring it would freeze the live text for the rest of a long job.
    // Live segments at or before the last persisted end are already saved.
    const persisted = job.segments || [];
    const lastEnd = persisted.length ? persisted[persisted.length - 1].end : 0;
    const live = state.liveSegments.filter((s) => s.start >= lastEnd - 0.01);
    const segments = persisted.concat(live);
    if (job.status === 'paused' && job.chunks?.length) {
      out.push(`<div class="notice">Paused after ${job.next_chunk} of ${job.chunks.length}
        parts. The text below is what finished; Resume continues from there.</div>`);
    }
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
  const html = turns.map((t, i) => {
    const cls = t.speaker ? `sp${speakers.indexOf(t.speaker) % 6}` : '';
    const who = t.speaker ? `<div class="who ${cls}">${esc(t.speaker)}</div>` : '';
    // Mark the newest turn while a job streams, so it is obvious that text is
    // still arriving rather than the view having stalled.
    const live = status === 'running' && i === turns.length - 1 ? ' is-live' : '';
    return `<div class="turn${live}" data-start="${t.start}" data-end="${t.end}">
      <button class="stamp" data-t="${t.start}">${clock(t.start)}</button>
      <div class="body">${who}<div>${esc(t.text)}</div></div>
    </div>`;
  }).join('');

  const tail = status === 'running'
    ? '<p class="live-note"><span class="live-dot"></span>Transcribing…</p>'
    : '';
  return `<div class="transcript">${html}${tail}</div>`;
}

async function control(action, busyLabel) {
  const btn = $(`btn-${action}`);
  if (btn) { btn.disabled = true; btn.textContent = busyLabel; }
  try {
    const res = await fetch(`/api/jobs/${state.activeId}/${action}`, { method: 'POST' });
    if (!res.ok) throw new Error((await res.json()).detail || 'Failed');
    // Resuming reopens the event stream; the others are reflected by SSE state.
    if (action === 'resume' || action === 'retry') { if (state.source) state.source.close(); listen(state.activeId); }
  } catch (err) {
    alert(err.message);
  }
  const job = await (await fetch(`/api/jobs/${state.activeId}`)).json();
  state.active = job;
  await refreshJobs();
  renderDetail();
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

// A thrown error used to leave the page blank with no clue why. Surfacing it
// costs nothing and turns "is it loading or broken?" into an answer.
window.addEventListener('error', (e) => {
  const c = document.getElementById('content');
  if (!c) return;
  c.innerHTML = `<div class="notice err"><strong>Something broke in the page.</strong>
    <div style="margin-top:6px;font-family:ui-monospace,monospace;font-size:12px">
    ${String(e.message || e.error || 'Unknown error').replace(/[<>&]/g, '')}</div>
    <div style="margin-top:6px">Reload to try again.</div></div>`;
});

// ---------- theme ----------
// Three states: explicit light, explicit dark, or follow the system. Only the
// explicit ones stamp data-theme; "system" removes it so the media query rules.
function applyTheme(choice) {
  const root = document.documentElement;
  if (choice === 'light' || choice === 'dark') root.setAttribute('data-theme', choice);
  else root.removeAttribute('data-theme');
  document.querySelectorAll('[data-theme-set]').forEach((b) => {
    b.setAttribute('aria-pressed', String(b.dataset.themeSet === choice));
  });
  try { localStorage.setItem('skryba.theme', choice); } catch { /* private mode */ }
}

function wireTheme() {
  let saved = 'system';
  try { saved = localStorage.getItem('skryba.theme') || 'system'; } catch { /* ignore */ }
  applyTheme(saved);
  document.querySelectorAll('[data-theme-set]').forEach((b) => {
    b.onclick = () => applyTheme(b.dataset.themeSet);
  });
}

(async function init() {
  wireTheme();
  await loadCaps();
  wireUpload();
  await refreshJobs();
  // Land on something useful: reattach to whatever is still running, otherwise
  // open the most recent recording so a finished transcript is right there.
  const running = state.jobs.find((j) => j.status === 'running' || j.status === 'queued');
  const target = running || state.jobs[0];
  if (target) select(target.id);
})();
