'use strict';

const $ = (id) => document.getElementById(id);
const state = {
  caps: null,
  jobs: [],
  activeId: null,
  active: null,       // full job with segments
  liveSegments: [],   // segments streamed while a job is still running
  tab: 'transcript',
  chats: {},          // questions asked per recording, kept for this session
  asking: false,
  source: null,       // EventSource for the active job
  model: 'turbo',
  search: '',         // filters the recordings list, by filename or transcript text
  uploading: false,   // true while a file is mid-upload — blocks a second, duplicate one
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
    'Needs a local model (Ollama, LM Studio) or an API key');
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
  dz.onclick = () => { if (!state.uploading) input.click(); };
  input.onchange = () => { upload([...input.files]); input.value = ''; };
  ['dragenter', 'dragover'].forEach((ev) => dz.addEventListener(ev, (e) => {
    e.preventDefault();
    if (!state.uploading) dz.classList.add('drag');
  }));
  ['dragleave', 'drop'].forEach((ev) => dz.addEventListener(ev, (e) => {
    e.preventDefault(); dz.classList.remove('drag');
  }));
  dz.addEventListener('drop', (e) => {
    if (state.uploading) return; // a drop mid-upload must not start a second one
    upload([...(e.dataTransfer?.files || [])]);
  });
}

/* XMLHttpRequest, not fetch: fetch has no upload-progress event, and without
   visible feedback a slow copy of a large recording looks identical to a drop
   that silently failed — which is exactly what invites a second, duplicate
   upload of the same file. */
function uploadOne(file, onProgress) {
  const fd = new FormData();
  fd.append('file', file);
  fd.append('model', state.model);
  fd.append('language', $('opt-language').value.trim());
  fd.append('diarize', $('opt-diarize').checked);
  fd.append('summarize', $('opt-summary').checked);
  fd.append('num_speakers', $('opt-speakers').value.trim());

  return new Promise((resolve, reject) => {
    const xhr = new XMLHttpRequest();
    xhr.open('POST', '/api/jobs');
    xhr.upload.onprogress = (e) => { if (e.lengthComputable) onProgress(e.loaded / e.total); };
    xhr.onload = () => {
      let data = {};
      try { data = JSON.parse(xhr.responseText); } catch { /* non-JSON error body */ }
      if (xhr.status >= 200 && xhr.status < 300) resolve(data);
      else reject(new Error(data.detail || `Upload failed (${xhr.status})`));
    };
    xhr.onerror = () => reject(new Error('Upload failed — network error.'));
    xhr.send(fd);
  });
}

async function upload(files) {
  if (!files.length) return;
  state.uploading = true;
  $('dropzone').classList.add('busy');
  $('upload-idle').hidden = true;
  $('upload-progress').hidden = false;

  for (let i = 0; i < files.length; i++) {
    const file = files[i];
    $('upload-name').textContent = files.length > 1
      ? `Uploading ${i + 1} of ${files.length}: ${file.name}` : `Uploading ${file.name}`;
    $('upload-bar').style.width = '0%';
    $('upload-pct').textContent = '0%';
    try {
      const job = await uploadOne(file, (frac) => {
        const pct = Math.round(frac * 100);
        $('upload-bar').style.width = `${pct}%`;
        $('upload-pct').textContent = `${pct}%`;
      });
      await refreshJobs();
      select(job.id);
    } catch (err) {
      alert(`${file.name}: ${err.message}`);
    }
  }

  state.uploading = false;
  $('dropzone').classList.remove('busy');
  $('upload-idle').hidden = false;
  $('upload-progress').hidden = true;
}

/* ---------------- job list ---------------- */

async function refreshJobs() {
  const url = state.search ? `/api/jobs?q=${encodeURIComponent(state.search)}` : '/api/jobs';
  state.jobs = await (await fetch(url)).json();
  renderJobs();
}

function wireSearch() {
  const input = $('job-search');
  let debounce;
  input.addEventListener('input', () => {
    clearTimeout(debounce);
    debounce = setTimeout(() => {
      state.search = input.value.trim();
      refreshJobs();
    }, 200);
  });
}

const ICON_PAUSE = '<svg viewBox="0 0 16 16" width="11" height="11" aria-hidden="true"><path d="M3 1.5h4v13H3zM9 1.5h4v13H9z"/></svg>';
const ICON_RESUME = '<svg viewBox="0 0 16 16" width="11" height="11" aria-hidden="true"><path d="M3 1.5v13l11-6.5z"/></svg>';

function renderJobs() {
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

    // A quick action for every state, so a stray or duplicate upload can be
    // stopped or cleared straight from the list — no need to open it first.
    const actions = [];
    if (j.status === 'running' || j.status === 'queued') {
      actions.push(`<button class="job-action" data-action="pause" data-id="${j.id}"
        title="Pause" aria-label="Pause">${ICON_PAUSE}</button>`);
    } else if (j.status === 'paused') {
      actions.push(`<button class="job-action" data-action="resume" data-id="${j.id}"
        title="Resume" aria-label="Resume">${ICON_RESUME}</button>`);
    } else if (j.status === 'error' || j.status === 'canceled') {
      actions.push(`<button class="job-action" data-action="retry" data-id="${j.id}"
        title="Retry" aria-label="Retry">&#8635;</button>`);
    }
    actions.push(`<button class="job-action job-delete" data-action="delete" data-id="${j.id}"
      title="Delete recording" aria-label="Delete recording">&times;</button>`);

    return `<div class="job ${j.id === state.activeId ? 'active' : ''}" data-id="${j.id}">
      <div class="job-actions">${actions.join('')}</div>
      <div class="job-name">${esc(j.filename)}</div>
      <div class="job-meta"><span class="dot ${j.status}"></span>${esc(line)}</div>
      ${running ? `<div class="bar"><i style="width:${pct}%"></i></div>` : ''}
    </div>`;
  }).join('') || `<p style="font-size:12.5px;color:var(--text-dim)">${
    state.search ? 'No matches.' : 'Nothing yet.'}</p>`;

  $('job-list').onclick = (e) => {
    const btn = e.target.closest('.job-action');
    if (btn) {
      e.stopPropagation();
      if (btn.dataset.action === 'delete') deleteJob(btn.dataset.id);
      else quickJobAction(btn.dataset.id, btn.dataset.action);
      return;
    }
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

/* Pause/resume/retry triggered from the list rather than the detail pane —
   same endpoints as control() below, but not tied to state.activeId, and
   without stealing focus into that job's detail view. */
async function quickJobAction(id, action) {
  try {
    const res = await fetch(`/api/jobs/${id}/${action}`, { method: 'POST' });
    if (!res.ok) throw new Error((await res.json()).detail || 'Failed');
  } catch (err) {
    alert(err.message);
    return;
  }
  if (state.activeId === id) {
    state.active = await (await fetch(`/api/jobs/${id}`)).json();
    if (action === 'resume' || action === 'retry') {
      if (state.source) state.source.close();
      listen(id);
    }
    renderDetail();
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
  // loadedmetadata will set the real duration once the new file is probed;
  // reset now so the old recording's numbers don't linger on screen.
  $('player-scrub').value = 0;
  $('player-elapsed').textContent = '0:00';
  $('player-duration').textContent = '0:00';
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
  $('tab-ask').onclick = () => { state.tab = 'ask'; renderTabs(); renderContent(); };
  $('tab-ask').style.display = (job.segments?.length && job.status !== 'running') ? '' : 'none';
  if (state.tab === 'ask' && $('tab-ask').style.display === 'none') state.tab = 'transcript';
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
  $('tab-ask').setAttribute('aria-selected', String(state.tab === 'ask'));
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
  } else if (state.tab === 'ask') {
    out.push(renderAsk(job));
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
    // Editing races the worker thread still appending to job.segments, so it's
    // only offered once the job has stopped changing under it.
    const editable = ['done', 'error', 'canceled'].includes(job.status);
    if (editable) out.push(renderReviewBar(job));
    out.push(renderTranscript(segments, job.status, editable));
  }
  $('content').innerHTML = out.join('');

  $('content').onclick = (e) => {
    const editBtn = e.target.closest('.turn-edit');
    if (editBtn) { startEditTurn(editBtn.closest('.turn')); return; }
    const saveBtn = e.target.closest('.turn-save');
    if (saveBtn) { saveEditTurn(saveBtn.closest('.turn')); return; }
    if (e.target.closest('.turn-cancel')) { renderContent(); return; }
    const okBtn = e.target.closest('.turn-ok');
    if (okBtn) { turnAction(okBtn.closest('.turn'), 'POST', '/confirm'); return; }
    const dropBtn = e.target.closest('.turn-drop');
    if (dropBtn) { turnAction(dropBtn.closest('.turn'), 'DELETE', ''); return; }
    const nav = e.target.closest('.review-nav');
    if (nav) { jumpFlag(Number(nav.dataset.dir)); return; }
    const stamp = e.target.closest('.stamp');
    if (!stamp) return;
    const player = $('player');
    player.currentTime = parseFloat(stamp.dataset.t);
    player.play().catch(() => {});
  };
  const btn = $('run-summary');
  if (btn) btn.onclick = runSummary;
  const form = $('ask-form');
  if (form) {
    form.onsubmit = (e) => { e.preventDefault(); askQuestion(); };
    if (!state.asking) $('ask-input').focus();
  }
}

/* ---------------- ask ---------------- */

// "[12:34]" or "[1:02:03]" in an answer becomes a button that seeks the audio,
// so every claim the model makes can be checked against what was said.
function linkTimes(text) {
  return esc(text).replace(/\[(\d{1,2}(?::\d{2}){1,2})\]/g, (whole, stamp) => {
    const secs = stamp.split(':').reduce((acc, part) => acc * 60 + Number(part), 0);
    return `<button class="stamp cite" data-t="${secs}">${stamp}</button>`;
  });
}

function renderAnswer(item) {
  if (item.error) return `<div class="notice err">${esc(item.error)}</div>`;
  const body = item.answer
    ? linkTimes(item.answer).replace(/\n/g, '<br>')
    : '<span class="spinner"></span> <span style="color:var(--text-dim)">Reading the transcript…</span>';
  const src = item.sources?.length
    ? `<details class="ask-sources"><summary>Based on ${item.sources.length} part${item.sources.length === 1 ? '' : 's'} of the meeting</summary>
        ${item.sources.map((x) => `<div><button class="stamp" data-t="${x.start}">${esc(x.clock)}</button>
          ${esc(x.text)}${x.text.length >= 220 ? '…' : ''}</div>`).join('')}</details>`
    : '';
  return `<div class="ask-a">${body}</div>${src}`;
}

function renderAsk(job) {
  if (!state.caps?.ask) {
    return `<div class="ask-setup">
      <p><strong>Ask questions about this meeting</strong> — answered by a model
      running on your own computer, so the transcript stays private.</p>
      <p>No local model is running right now (${esc(state.caps?.local_model || 'none found')}).
      To turn this on:</p>
      <ol>
        <li>Install <a href="https://ollama.com" target="_blank" rel="noopener">Ollama</a>
            or LM Studio on this computer.</li>
        <li>Load a model, for example: <code>ollama pull qwen2.5:3b</code></li>
        <li>Reload this page.</li>
      </ol>
      <p style="color:var(--text-dim)">Already have a model on another computer? Set
      <code>LOCAL_LLM_URL</code> in <code>.env</code> to its address.</p></div>`;
  }
  const chat = state.chats[job.id] || [];
  const history = chat.map((item, i) => `<div class="ask-item">
      <div class="ask-q">${esc(item.question)}</div>
      <div id="ask-a-${i}">${renderAnswer(item)}</div></div>`).join('');
  const hint = chat.length ? '' : `<p class="ask-hint">Ask anything about this recording —
    who agreed to what, what was said about a topic, what is still open. Answers
    come from <strong>${esc(state.caps.local_model)}</strong> and link to the moment they were said.</p>`;
  return `<div class="ask">${hint}${history}
    <form id="ask-form" class="ask-form">
      <input id="ask-input" type="text" autocomplete="off" maxlength="1000"
             placeholder="Ask about this meeting…" ${state.asking ? 'disabled' : ''}>
      <button class="btn" type="submit" ${state.asking ? 'disabled' : ''}>Ask</button>
    </form></div>`;
}

// Read a response that sends one JSON object per line as they become ready.
async function readLines(res, onItem) {
  const reader = res.body.getReader();
  const decoder = new TextDecoder();
  let buffer = '';
  for (;;) {
    const { done, value } = await reader.read();
    if (done) break;
    buffer += decoder.decode(value, { stream: true });
    let nl;
    while ((nl = buffer.indexOf('\n')) >= 0) {
      const line = buffer.slice(0, nl).trim();
      buffer = buffer.slice(nl + 1);
      if (line) onItem(JSON.parse(line));
    }
  }
  if (buffer.trim()) onItem(JSON.parse(buffer));
}

async function askQuestion() {
  const input = $('ask-input');
  const question = input.value.trim();
  if (!question || state.asking) return;
  const jobId = state.activeId;
  const chat = (state.chats[jobId] = state.chats[jobId] || []);
  const item = { question, answer: '', sources: [], error: null };
  chat.push(item);
  state.asking = true;
  renderContent();

  // While the answer streams in, touch only its own element: re-rendering the
  // whole tab on every word would make the page jump.
  const paint = () => {
    if (state.activeId !== jobId || state.tab !== 'ask') return;
    const el = $(`ask-a-${chat.length - 1}`);
    if (el) el.innerHTML = renderAnswer(item);
  };
  try {
    const res = await fetch(`/api/jobs/${jobId}/ask`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ question }),
    });
    if (!res.ok) throw new Error((await res.json()).detail || 'Failed');
    await readLines(res, (msg) => {
      if (msg.sources) item.sources = msg.sources;
      if (msg.delta) item.answer += msg.delta;
      if (msg.error) item.error = msg.error;
      paint();
    });
    if (!item.answer && !item.error) item.error = 'The model returned nothing.';
  } catch (err) {
    item.error = err.message;
  }
  state.asking = false;
  if (state.activeId === jobId && state.tab === 'ask') renderContent();
}

function renderSummary(job) {
  if (!job.summary) {
    if (job.status !== 'done') return '<p style="color:var(--text-dim)">Not available yet.</p>';
    if (!state.caps?.summarization) {
      return `<p style="color:var(--text-dim)">No summary yet. Start a local model
        (Ollama or LM Studio) and reload, or add an API key to <code>.env</code>.
        The Ask tab has setup steps.</p>`;
    }
    return `<p style="color:var(--text-dim);margin-bottom:14px">
      No summary was generated for this recording.</p>
      <button class="btn" id="run-summary">Summarize</button>
      <span style="color:var(--text-dim);font-size:13px;margin-left:8px">${esc(state.caps.summary_provider || '')}</span>`;
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

function renderTranscript(segments, status, editable) {
  if (!segments?.length) {
    return status === 'running'
      ? '<p style="color:var(--text-dim)">Listening… text will appear as it is decoded.</p>'
      : '<p style="color:var(--text-dim)">No transcript.</p>';
  }
  // Merge Whisper's prosody-sized fragments into readable paragraphs. Cut on a
  // speaker change, or on a pause once the turn is long enough to stand alone —
  // without that second rule an undiarized recording becomes one giant block.
  // Mirrors formats.group_by_turns on the server — same constants, same
  // iteration order — so a turn's index here matches the index the PATCH
  // /api/jobs/{id}/turns/{index} endpoint groups from.
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
      last.segs.push(seg);
    } else {
      turns.push({ speaker: who, start: seg.start, end: seg.end, text: seg.text, segs: [seg] });
    }
  }
  // Stashed so the edit handlers can read a turn's raw (unescaped) text by
  // index without round-tripping it through an HTML attribute.
  state.turns = turns;

  const html = turns.map((t, i) => {
    const cls = t.speaker ? `sp${speakers.indexOf(t.speaker) % 6}` : '';
    const who = t.speaker ? `<div class="who ${cls}">${esc(t.speaker)}</div>` : '';
    // Mark the newest turn while a job streams, so it is obvious that text is
    // still arriving rather than the view having stalled.
    const live = status === 'running' && i === turns.length - 1 ? ' is-live' : '';
    const flags = t.segs.filter((g) => g.flag);
    const artifact = flags.some((g) => g.flag.level === 'artifact');
    const flagCls = flags.length ? (artifact ? ' has-flag is-artifact' : ' has-flag') : '';
    let actions = '';
    if (editable) {
      if (flags.length) {
        actions += `<button class="turn-ok" title="This is right — clear the flag" aria-label="Mark as correct">&#10003;</button>`;
      }
      if (artifact) {
        actions += `<button class="turn-drop" title="Remove — this was never said" aria-label="Remove this line">&#10005;</button>`;
      }
      actions += `<button class="turn-edit" title="Edit this text" aria-label="Edit this text">&#9998;</button>`;
    }
    const text = t.segs.map(renderSegment).join(' ');
    return `<div class="turn${live}${flagCls}" data-turn="${i}" data-start="${t.start}" data-end="${t.end}">
      <button class="stamp" data-t="${t.start}">${clock(t.start)}</button>
      <div class="body">${who}<div class="turn-text">${text}</div></div>
      <div class="turn-actions">${actions}</div>
    </div>`;
  }).join('');

  const tail = status === 'running'
    ? '<p class="live-note"><span class="live-dot"></span>Transcribing…</p>'
    : '';
  return `<div class="transcript">${html}${tail}</div>`;
}

// One segment's text, with the words Whisper doubted marked individually and
// the whole line wrapped when it carries a verdict.
function renderSegment(seg) {
  const text = seg.text || '';
  if (!seg.flag) return esc(text);
  let html = '', at = 0;
  for (const [a, b] of [...(seg.flag.words || [])].sort((x, y) => x[0] - y[0])) {
    if (a < at) continue;
    html += esc(text.slice(at, a)) + `<mark class="doubt">${esc(text.slice(a, b))}</mark>`;
    at = b;
  }
  html += esc(text.slice(at));
  return `<span class="flagged ${seg.flag.level}" title="${esc(seg.flag.why)}">${html}</span>`;
}

// The review bar: how many lines need a look, and buttons to walk them.
function renderReviewBar(job) {
  const bits = [];
  if (job.audio) {
    const pct = Math.round(job.audio.confidence * 100);
    bits.push(`<span class="grade ${job.audio.label}" title="Median word confidence ${pct}%">
      ${job.audio.label} audio</span>`);
  }
  if (job.flagged) {
    bits.push(`<span class="review-count">${job.flagged} line${job.flagged === 1 ? '' : 's'} to check</span>
      <button class="btn review-nav" data-dir="-1" aria-label="Previous flagged line">&uarr;</button>
      <button class="btn review-nav" data-dir="1" aria-label="Next flagged line">&darr;</button>`);
  } else if (job.audio) {
    bits.push('<span class="review-count">nothing flagged</span>');
  }
  return bits.length ? `<div class="review-bar">${bits.join('')}</div>` : '';
}

function jumpFlag(dir) {
  const flagged = [...document.querySelectorAll('.turn.has-flag')];
  if (!flagged.length) return;
  const cur = flagged.findIndex((el) => el.classList.contains('flag-focus'));
  const next = cur < 0 ? (dir > 0 ? 0 : flagged.length - 1)
    : (cur + dir + flagged.length) % flagged.length;
  flagged.forEach((el) => el.classList.remove('flag-focus'));
  flagged[next].classList.add('flag-focus');
  flagged[next].scrollIntoView({ block: 'center', behavior: 'smooth' });
}

async function turnAction(turnEl, method, suffix) {
  const idx = Number(turnEl.dataset.turn);
  try {
    const res = await fetch(`/api/jobs/${state.activeId}/turns/${idx}${suffix}`, { method });
    if (!res.ok) throw new Error((await res.json()).detail || 'Failed');
    state.active = await res.json();
  } catch (err) {
    alert(err.message);
  }
  renderContent();
}

function startEditTurn(turnEl) {
  const turn = state.turns[Number(turnEl.dataset.turn)];
  const body = turnEl.querySelector('.body');
  turnEl.querySelector('.turn-actions')?.remove();

  const editor = document.createElement('textarea');
  editor.className = 'turn-editor';
  editor.value = turn.text;
  body.querySelector('.turn-text').replaceWith(editor);
  editor.focus();
  editor.setSelectionRange(editor.value.length, editor.value.length);

  const actions = document.createElement('div');
  actions.className = 'turn-edit-actions';
  actions.innerHTML = `<button class="btn turn-save">Save</button>
                        <button class="btn turn-cancel">Cancel</button>`;
  body.appendChild(actions);
}

async function saveEditTurn(turnEl) {
  const idx = Number(turnEl.dataset.turn);
  const editor = turnEl.querySelector('.turn-editor');
  const text = editor.value.trim();
  if (!text) { alert('Text cannot be empty.'); return; }

  const saveBtn = turnEl.querySelector('.turn-save');
  const cancelBtn = turnEl.querySelector('.turn-cancel');
  saveBtn.disabled = true; cancelBtn.disabled = true; saveBtn.textContent = 'Saving…';
  try {
    const res = await fetch(`/api/jobs/${state.activeId}/turns/${idx}`, {
      method: 'PATCH',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ text }),
    });
    if (!res.ok) throw new Error((await res.json()).detail || 'Save failed');
    state.active = await res.json();
    const li = state.jobs.findIndex((j) => j.id === state.activeId);
    if (li >= 0) state.jobs[li] = { ...state.jobs[li], ...state.active };
  } catch (err) {
    alert(err.message);
  }
  renderContent();
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
    const jobId = state.activeId;
    const res = await fetch(`/api/jobs/${jobId}/summarize`, { method: 'POST' });
    if (!res.ok) throw new Error((await res.json()).detail || 'Failed');
    let summary = null, failure = null;
    await readLines(res, (msg) => {
      if (msg.total) {
        const live = $('run-summary');
        if (live) live.textContent = `Summarizing… part ${Math.min(msg.done + 1, msg.total)} of ${msg.total}`;
      }
      if (msg.summary) summary = msg.summary;
      if (msg.error) failure = msg.error;
    });
    if (failure) throw new Error(failure);
    if (!summary) throw new Error('No summary came back.');
    if (state.activeId === jobId) {
      state.active.summary = summary;
      delete state.active.meta.summary_error;
      renderContent();
    }
  } catch (err) {
    const live = $('run-summary');
    if (live) { live.disabled = false; live.textContent = 'Summarize'; }
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

// ---------- player ----------
// The <audio> element carries no `controls` — this drives a themed bar
// instead, driven purely by the element's own events so it stays in sync
// no matter what triggers playback (this button, a stamp click, a keyboard
// shortcut the browser handles natively).
function wirePlayer() {
  const audio = $('player');
  const toggle = $('player-toggle');
  const scrub = $('player-scrub');
  const elapsed = $('player-elapsed');
  const duration = $('player-duration');
  let scrubbing = false;

  // Which icon shows and the button's fill both key off .is-playing (CSS) —
  // there's no JS icon-hidden toggling, since setting .hidden on an inline
  // <svg> doesn't reliably reflect onto the element the way it does on a
  // plain HTMLElement.
  toggle.onclick = () => { audio.paused ? audio.play().catch(() => {}) : audio.pause(); };
  audio.addEventListener('play', () => {
    toggle.classList.add('is-playing'); toggle.setAttribute('aria-label', 'Pause');
  });
  audio.addEventListener('pause', () => {
    toggle.classList.remove('is-playing'); toggle.setAttribute('aria-label', 'Play');
  });
  audio.addEventListener('ended', () => toggle.classList.remove('is-playing'));
  audio.addEventListener('loadedmetadata', () => {
    scrub.max = audio.duration || 0;
    duration.textContent = clock(audio.duration);
  });
  audio.addEventListener('timeupdate', () => {
    if (scrubbing) return;
    scrub.value = audio.currentTime;
    elapsed.textContent = clock(audio.currentTime);
  });
  // 'input' fires continuously while dragging (update the label optimistically);
  // 'change' fires once on release, which is when the seek should actually land —
  // committing on every 'input' tick would fight the audio's own timeupdate.
  scrub.addEventListener('input', () => { scrubbing = true; elapsed.textContent = clock(scrub.value); });
  scrub.addEventListener('change', () => { audio.currentTime = parseFloat(scrub.value); scrubbing = false; });
}

(async function init() {
  wireTheme();
  wirePlayer();
  await loadCaps();
  wireUpload();
  wireSearch();
  await refreshJobs();
  // Land on something useful: reattach to whatever is still running, otherwise
  // open the most recent recording so a finished transcript is right there.
  const running = state.jobs.find((j) => j.status === 'running' || j.status === 'queued');
  const target = running || state.jobs[0];
  if (target) select(target.id);
})();
