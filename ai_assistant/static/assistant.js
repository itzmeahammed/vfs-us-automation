/* AI assistant widget — self-mounting, framework-free, isolated in a shadow root.
 *
 *   <script src="/assistant/assistant.js" data-endpoint="/assistant" defer></script>
 *
 * Talks to POST {endpoint}/ask and renders the NDJSON event stream. Everything
 * the model writes is HTML-escaped before the small Markdown renderer below adds
 * its own tags, so neither the model nor text it quotes from logs can inject
 * markup or script into the page.
 */
(() => {
  'use strict';
  if (window.__aiAssistantMounted) return;
  window.__aiAssistantMounted = true;

  const script = document.currentScript;
  const API = (script && script.dataset.endpoint) || '/assistant';
  const STORE = 'ai-assistant:' + location.pathname;
  const MAX_TURNS_SENT = 6;

  /* ---------------- markdown (escape first, then a strict subset) -------- */
  const esc = s => String(s).replace(/[&<>"']/g, c =>
    ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));

  function inline(s) {
    // s is already escaped
    const codes = [];
    s = s.replace(/`([^`]+)`/g, (_, c) => { codes.push(c); return `\u0000${codes.length - 1}\u0000`; });
    s = s.replace(/\*\*([^*]+)\*\*/g, '<strong>$1</strong>')
         .replace(/(^|[^*])\*([^*\s][^*]*)\*/g, '$1<em>$2</em>')
         .replace(/\[([^\]]+)\]\((https?:\/\/[^)\s]+)\)/g,
                  '<a href="$2" target="_blank" rel="noopener noreferrer">$1</a>');
    return s.replace(/\u0000(\d+)\u0000/g, (_, i) => `<code>${codes[+i]}</code>`);
  }

  function isTableSep(line) { return /^\s*\|?\s*:?-{2,}:?\s*(\|\s*:?-{2,}:?\s*)*\|?\s*$/.test(line); }
  function cells(line) {
    return line.trim().replace(/^\|/, '').replace(/\|$/, '').split('|').map(c => c.trim());
  }

  function md(src) {
    const lines = esc(src).split('\n');
    const out = [];
    let i = 0;
    while (i < lines.length) {
      const line = lines[i];
      if (/^```/.test(line)) {                                   // fenced code
        const buf = []; i++;
        while (i < lines.length && !/^```/.test(lines[i])) buf.push(lines[i++]);
        i++; out.push(`<pre><code>${buf.join('\n')}</code></pre>`); continue;
      }
      if (line.includes('|') && i + 1 < lines.length && isTableSep(lines[i + 1])) {
        const head = cells(line); i += 2;
        const rows = [];
        while (i < lines.length && lines[i].includes('|') && lines[i].trim()) rows.push(cells(lines[i++]));
        const num = c => /^[-+]?[\d.,]+\s*(%|MB|GB|h|d|s|ms)?$/.test(c);
        out.push('<div class="tw"><table><thead><tr>' +
          head.map(h => `<th>${inline(h)}</th>`).join('') + '</tr></thead><tbody>' +
          rows.map(r => '<tr>' + head.map((_, j) => {
            const c = r[j] || ''; return `<td${num(c) ? ' class="n"' : ''}>${inline(c)}</td>`;
          }).join('') + '</tr>').join('') + '</tbody></table></div>');
        continue;
      }
      const h = line.match(/^(#{1,4})\s+(.*)$/);
      if (h) { out.push(`<h${Math.min(6, h[1].length + 2)}>${inline(h[2])}</h${Math.min(6, h[1].length + 2)}>`); i++; continue; }
      if (/^\s*([-*•]|\d+[.)])\s+/.test(line)) {                 // lists
        const ordered = /^\s*\d+[.)]/.test(line);
        const items = [];
        while (i < lines.length && /^\s*([-*•]|\d+[.)])\s+/.test(lines[i]))
          items.push(lines[i++].replace(/^\s*([-*•]|\d+[.)])\s+/, ''));
        const tag = ordered ? 'ol' : 'ul';
        out.push(`<${tag}>` + items.map(t => `<li>${inline(t)}</li>`).join('') + `</${tag}>`);
        continue;
      }
      if (/^\s*(---|\*\*\*)\s*$/.test(line)) { out.push('<hr>'); i++; continue; }
      if (!line.trim()) { i++; continue; }
      const para = [];
      while (i < lines.length && lines[i].trim() && !/^(```|#{1,4}\s|\s*([-*•]|\d+[.)])\s)/.test(lines[i])
             && !(lines[i].includes('|') && i + 1 < lines.length && isTableSep(lines[i + 1])))
        para.push(lines[i++]);
      out.push(`<p>${inline(para.join('<br>'))}</p>`);
    }
    return out.join('');
  }

  /* ---------------- state ------------------------------------------------ */
  let turns = [];          // [{q, a, tools:[], error, ms}]
  let busy = null;         // AbortController while streaming
  let meta = { title: 'Assistant', suggestions: [] };
  try { turns = JSON.parse(sessionStorage.getItem(STORE) || '[]') || []; } catch (e) { turns = []; }
  const save = () => { try { sessionStorage.setItem(STORE, JSON.stringify(turns.slice(-30))); } catch (e) {} };

  /* ---------------- DOM -------------------------------------------------- */
  const host = document.createElement('div');
  host.id = 'ai-assistant-root';
  document.body.appendChild(host);
  const root = host.attachShadow({ mode: 'open' });
  root.innerHTML = `
    <link rel="stylesheet" href="${esc(API)}/assistant.css">
    <button class="fab" part="launcher" aria-label="Open the assistant" title="Ask AI (Alt+A)">
      <svg viewBox="0 0 24 24" aria-hidden="true"><path d="M12 3l1.9 4.6L18.5 9.5l-4.6 1.9L12 16l-1.9-4.6L5.5 9.5l4.6-1.9z"/><path d="M19 15l.9 2.1 2.1.9-2.1.9L19 21l-.9-2.1-2.1-.9 2.1-.9z"/></svg>
      <span>Ask AI</span>
    </button>
    <aside class="panel" role="dialog" aria-modal="false" aria-labelledby="ttl" hidden>
      <header>
        <div class="t"><b id="ttl">Assistant</b><span class="model" id="model"></span></div>
        <div class="hb">
          <button class="ib" id="clear" title="New conversation" aria-label="New conversation">
            <svg viewBox="0 0 24 24"><path d="M4 7h16M9 7V4h6v3M6 7l1 13h10l1-13"/></svg></button>
          <button class="ib" id="close" title="Close (Esc)" aria-label="Close">
            <svg viewBox="0 0 24 24"><path d="M6 6l12 12M18 6L6 18"/></svg></button>
        </div>
      </header>
      <div class="log" id="log" aria-live="polite"></div>
      <form id="form" autocomplete="off">
        <textarea id="q" rows="1" maxlength="2000" placeholder="Ask about runs, slots, accounts, data use…" aria-label="Question"></textarea>
        <button type="submit" class="send" id="send" aria-label="Send">
          <svg viewBox="0 0 24 24"><path d="M5 12h13M13 6l6 6-6 6"/></svg></button>
      </form>
      <footer>Read-only · secrets &amp; e-mails are masked before anything is sent to OpenAI</footer>
    </aside>`;

  const $ = s => root.querySelector(s);
  const panel = $('.panel'), log = $('#log'), q = $('#q'), sendBtn = $('#send');

  function open() {
    panel.hidden = false; $('.fab').hidden = true;
    render(); setTimeout(() => q.focus(), 30);
    if (!meta.loaded) loadMeta();
  }
  function close() { panel.hidden = true; $('.fab').hidden = false; }
  $('.fab').addEventListener('click', open);
  $('#close').addEventListener('click', close);
  $('#clear').addEventListener('click', () => { if (busy) busy.abort(); turns = []; save(); render(); q.focus(); });
  document.addEventListener('keydown', e => {
    if (e.altKey && (e.key === 'a' || e.key === 'A')) { e.preventDefault(); panel.hidden ? open() : close(); }
  });
  root.addEventListener('keydown', e => { if (e.key === 'Escape') close(); });

  q.addEventListener('input', autosize);
  q.addEventListener('keydown', e => {
    if (e.key === 'Enter' && !e.shiftKey && !e.isComposing) { e.preventDefault(); submit(); }
  });
  $('#form').addEventListener('submit', e => { e.preventDefault(); busy ? busy.abort() : submit(); });
  function autosize() { q.style.height = 'auto'; q.style.height = Math.min(q.scrollHeight, 160) + 'px'; }

  async function loadMeta() {
    try {
      const r = await fetch(API + '/meta', { credentials: 'same-origin' });
      if (r.ok) { meta = { ...(await r.json()), loaded: true }; }
    } catch (e) {}
    $('#ttl').textContent = meta.title || 'Assistant';
    $('#model').textContent = meta.model || '';
    if (!turns.length) render();
  }

  /* ---------------- rendering ------------------------------------------- */
  function toolChips(t) {
    if (!t.tools || !t.tools.length) return '';
    return '<div class="chips">' + t.tools.map(x =>
      `<span class="chip ${x.ok ? '' : 'bad'}" title="${esc(JSON.stringify(x.args || {}))} · ${x.ms} ms">` +
      `${x.ok ? '✓' : '✕'} ${esc(x.label || x.name)}</span>`).join('') + '</div>';
  }
  function turnHTML(t, idx) {
    const live = busy && idx === turns.length - 1;
    return `<div class="u"><div class="bubble">${esc(t.q)}</div></div>
      <div class="a">${toolChips(t)}
        ${t.a ? `<div class="md">${md(t.a)}</div>` : ''}
        ${t.refused ? `<div class="note md">${md(t.refused)}</div>` : ''}
        ${live && t.status ? `<div class="status"><i></i>${esc(t.status)}</div>` : ''}
        ${t.error ? `<div class="err">${esc(t.error)}</div>` : ''}
        ${!live && t.a ? `<div class="acts"><button class="lk" data-copy="${idx}">Copy</button>` +
          (t.ms ? `<span>${(t.ms / 1000).toFixed(1)} s</span>` : '') + '</div>' : ''}
      </div>`;
  }
  function render() {
    if (!turns.length) {
      const sug = (meta.suggestions || []).map(s =>
        `<button class="sug" data-q="${esc(s)}">${esc(s)}</button>`).join('');
      log.innerHTML = `<div class="empty"><p>Ask anything about this system. Answers are built
        from live data — I read stats, logs and the database, then summarise.</p>${sug}</div>`;
    } else {
      log.innerHTML = turns.map(turnHTML).join('');
    }
    log.scrollTop = log.scrollHeight;
  }
  log.addEventListener('click', e => {
    const s = e.target.closest('[data-q]');
    if (s) { q.value = s.dataset.q; submit(); return; }
    const c = e.target.closest('[data-copy]');
    if (c) {
      const t = turns[+c.dataset.copy];
      navigator.clipboard && navigator.clipboard.writeText(t.a).then(() => {
        c.textContent = 'Copied'; setTimeout(() => { c.textContent = 'Copy'; }, 1200);
      }).catch(() => {});
    }
  });

  let raf = 0;
  const scheduleRender = () => { if (!raf) raf = requestAnimationFrame(() => { raf = 0; render(); }); };

  /* ---------------- ask -------------------------------------------------- */
  async function submit() {
    const question = q.value.trim();
    if (!question || busy) return;
    const history = [];
    turns.slice(-MAX_TURNS_SENT).forEach(t => {
      if (t.a && !t.error) { history.push({ role: 'user', content: t.q }, { role: 'assistant', content: t.a }); }
    });
    const turn = { q: question, a: '', tools: [], status: 'Connecting…' };
    turns.push(turn);
    q.value = ''; autosize();
    busy = new AbortController();
    panel.classList.add('busy');
    sendBtn.setAttribute('aria-label', 'Stop');
    render();
    try {
      const r = await fetch(API + '/ask', {
        method: 'POST', credentials: 'same-origin', signal: busy.signal,
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ question, history }),
      });
      if (!r.ok) {
        let msg = 'Request failed (' + r.status + ')';
        try { msg = (await r.json()).error || msg; } catch (e) {}
        throw new Error(msg);
      }
      const reader = r.body.getReader();
      const dec = new TextDecoder();
      let buf = '';
      for (;;) {
        const { value, done } = await reader.read();
        if (done) break;
        buf += dec.decode(value, { stream: true });
        let nl;
        while ((nl = buf.indexOf('\n')) >= 0) {
          const line = buf.slice(0, nl).trim(); buf = buf.slice(nl + 1);
          if (!line) continue;
          let ev; try { ev = JSON.parse(line); } catch (e) { continue; }
          if (ev.type === 'delta') { turn.a += ev.text; turn.status = ''; }
          else if (ev.type === 'status') turn.status = ev.text;
          else if (ev.type === 'tool') turn.tools.push(ev);
          else if (ev.type === 'error') turn.error = ev.message;
          else if (ev.type === 'refused') { turn.refused = ev.text; turn.status = ''; }
          else if (ev.type === 'done') turn.ms = ev.ms;
          scheduleRender();
        }
      }
      if (!turn.a && !turn.error && !turn.refused) turn.error = 'No answer came back.';
    } catch (e) {
      turn.error = e.name === 'AbortError' ? (turn.a ? '' : 'Stopped.') : (e.message || 'Network error');
      if (e.name === 'AbortError' && turn.a) turn.a += '\n\n*(stopped)*';
    } finally {
      busy = null; turn.status = '';
      panel.classList.remove('busy');
      sendBtn.setAttribute('aria-label', 'Send');
      save(); render(); q.focus();
    }
  }
})();
