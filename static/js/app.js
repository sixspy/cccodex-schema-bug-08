const msg = document.getElementById('msg');
const view = document.getElementById('view');
let contracts = [];
let lastCheck = null;

async function api(path, opts={}) {
  const r = await fetch(path, {
    headers: {'Content-Type': 'application/json', ...(opts.headers||{})},
    ...opts
  });
  const j = await r.json().catch(() => ({}));
  if (!r.ok) {
    const err = new Error(j.error || ('HTTP '+r.status));
    err.payload = j;
    err.status = r.status;
    throw err;
  }
  return j;
}

function setMsg(t, isErr=false) {
  msg.textContent = t || '';
  msg.className = isErr ? 'err' : 'muted';
}

function esc(s) {
  return String(s == null ? '' : s).replace(/[&<>"']/g, c => (
    {'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]
  ));
}

function chip(text, kind='') {
  if (!text) return '';
  return `<span class="chip ${kind}">${esc(text)}</span>`;
}

function winChip(w) {
  return w
    ? chip('窗口内', 'ok')
    : chip('窗口外', 'warn');
}

async function loadContracts() {
  const data = await api('/api/contracts');
  contracts = data.items || [];
}

function renderContracts() {
  view.innerHTML = `<div class="grid">${contracts.map(c => `
    <article class="card">
      <div class="row">
        <h3>${esc(c.table_name)} · v${esc(c.version_no)}</h3>
        <span class="chip">${esc(c.status)}</span>
        ${c.chip ? chip(c.chip, c.archived ? 'ok' : 'warn') : ''}
      </div>
      <p class="muted">${esc(c.note || '')} · 字段 ${esc(c.field_count)}</p>
    </article>`).join('')}</div>`;
}

function draftOptions() {
  return contracts.filter(c => c.status === '草稿')
    .map(c => `<option value="${esc(c.id)}">${esc(c.table_name)} v${esc(c.version_no)}</option>`).join('');
}

function renderCheck() {
  view.innerHTML = `
    <div class="card grid">
      <label>草稿契约
        <select id="cid">${draftOptions()}</select>
      </label>
      <button class="primary" id="btn-check" type="button">运行破坏性检查</button>
      <pre id="out" class="muted"></pre>
    </div>`;
  document.getElementById('btn-check').onclick = async () => {
    try {
      const cid = document.getElementById('cid').value;
      const j = await api(`/api/contracts/${cid}/breaking-check`, {method:'POST', body:'{}'});
      lastCheck = j.check;
      document.getElementById('out').textContent = JSON.stringify(j.check, null, 2);
      setMsg(`检查结论：${j.check.verdict}`);
    } catch (e) { setMsg(e.message, true); }
  };
}

function renderApply() {
  view.innerHTML = `
    <div class="card grid">
      <label>草稿契约
        <select id="cid">${draftOptions()}</select>
      </label>
      <button class="primary" id="btn-apply" type="button">换版应用</button>
    </div>`;
  document.getElementById('btn-apply').onclick = async () => {
    try {
      const cid = document.getElementById('cid').value;
      const j = await api(`/api/contracts/${cid}/apply`, {method:'POST', body:'{}'});
      setMsg(`已应用 ${j.contract.table_name} v${j.contract.version_no}`);
      await loadContracts();
      renderApply();
    } catch (e) { setMsg(e.message, true); }
  };
}

// ---------------------------------------------------------------- 回滚台

function outcomeChip(ev) {
  if (ev.status === '已拦') return chip('误退待收口', 'warn');
  if (ev.status === '已撤回') return chip('误退已撤回', 'muted-chip');
  if (ev.status === '已留仓') return chip('误退留仓可读', 'ok');
  return '';
}

async function renderRollback() {
  const items = (await api('/api/rollback-console')).items || [];
  view.innerHTML = `<div class="grid">${items.map(t => `
    <article class="card">
      <div class="row">
        <h3>${esc(t.table_name)}</h3>
        ${chip(`现行 v${t.live_version ?? '—'}`, 'ok')}
        ${chip(`目录 v${t.catalog_version ?? '—'}`, t.aligned ? 'ok' : 'bad')}
        ${t.aligned ? chip('已对齐', 'ok') : chip('未对齐·拒绝操作', 'bad')}
        ${winChip(t.within_window)}
      </div>
      <p class="muted">
        退前版 ${t.prev_version == null ? '—' : 'v'+esc(t.prev_version)} ·
        窗口 ${esc(t.window_hours)}h · 截止 ${esc(t.window_deadline || '—')} ·
        近次换版 ${esc(t.applied_at || '—')}
      </p>
      <div class="row">
        <button type="button" class="primary" data-roll="${esc(t.table_name)}"
          ${t.can_rollback ? '' : 'disabled'}>退版到 v${t.prev_version ?? '—'}</button>
        ${t.open_count ? `<span class="chip warn">未收口误退 ${t.open_count}</span>` : ''}
        ${(t.recent_misrollback || []).filter(e => e.status === '已撤回' || e.status === '已留仓')
          .map(e => chip(`#${e.id} ${e.status === '已留仓' ? '留仓' : '撤回'}`, e.status === '已留仓' ? 'ok' : 'muted-chip'))
          .join('')}
      </div>
      <div class="grid" id="ev-${esc(t.table_name)}">
        ${(t.open_misrollback || []).map(ev => `
          <div class="card inner">
            <div class="row">
              <strong>误退 #${esc(ev.id)}</strong>
              ${outcomeChip(ev)}
              <span class="muted">v${esc(ev.from_version)} → v${esc(ev.to_version)} · ${esc(ev.created_at)}</span>
            </div>
            <p class="muted">窗口外退版已拦，只能选一条收口结局；选定后另一端不可再挂。</p>
            <div class="row">
              <input id="reason-${esc(ev.id)}" placeholder="收口备注（可选）" />
              <button type="button" data-resolve="撤回" data-ev="${esc(ev.id)}">撤回误退</button>
              <button type="button" class="primary" data-resolve="留仓" data-ev="${esc(ev.id)}">留仓转只读</button>
            </div>
          </div>`).join('')}
      </div>
    </article>`).join('') || '<p class="muted">暂无可退版表</p>'}</div>`;

  view.querySelectorAll('[data-roll]').forEach(btn => {
    btn.onclick = async () => {
      const table = btn.dataset.roll;
      try {
        const j = await api(`/api/contracts/${encodeURIComponent(table)}/rollback`, {
          method:'POST', body:'{}'
        });
        setMsg(`${table} 已退至 v${j.result.contract_version}，目录同步 v${j.result.catalog_version}`);
      } catch (e) {
        const p = e.payload || {};
        if (p.event_id) {
          setMsg(`${e.message}（误退 #${p.event_id} 已登记，请在上方收口）`, true);
        } else {
          setMsg(e.message, true);
        }
      }
      await renderRollback();
    };
  });

  view.querySelectorAll('[data-resolve]').forEach(btn => {
    btn.onclick = async () => {
      const evId = btn.dataset.ev;
      const outcome = btn.dataset.resolve;
      const reason = (document.getElementById(`reason-${evId}`) || {}).value || '';
      try {
        const j = await api(`/api/misrollback/${evId}/resolve`, {
          method:'POST', body: JSON.stringify({outcome, reason})
        });
        const ev = j.event;
        setMsg(
          outcome === '留仓'
            ? `误退 #${evId} 已留仓：v${ev.archived_version} 进只读仓仍可读，现行挂回 v${ev.restored_version}`
            : `误退 #${evId} 已撤回：整笔恢复 v${ev.restored_version}，误退版不再可读`
        );
      } catch (e) {
        setMsg(e.message, true);
      }
      await renderRollback();
    };
  });
}

// ---------------------------------------------------------------- 时间线

async function renderTimeline() {
  const items = (await api('/api/timeline')).items || [];
  view.innerHTML = `
    <div class="card grid">
      <div class="row">
        <h3>编排留痕</h3>
        <span class="muted">换版 / 退版 / 误退拦截与收口，按时间倒序</span>
      </div>
      ${items.map(it => `
        <details class="card inner">
          <summary>
            <span class="chip">${esc(it.kind_label)}</span>
            <strong>${esc(it.table_name)}</strong>
            <span class="muted">${esc(it.summary)} · ${esc(it.at)}</span>
          </summary>
          <pre class="muted">${esc(JSON.stringify(it.raw, null, 2))}</pre>
        </details>`).join('') || '<p class="muted">暂无留痕</p>'}
    </div>`;
}

// ---------------------------------------------------------------- 目录席

async function renderCatalog() {
  const items = (await api('/api/catalog')).items || [];
  view.innerHTML = `<div class="grid">${items.map(t => `
    <article class="card">
      <div class="row">
        <h3>${esc(t.table_name)}</h3>
        ${chip(`契约 v${t.contract_version ?? '—'}`, 'ok')}
        ${chip(`目录 v${t.catalog_version ?? '—'}`, t.aligned ? 'ok' : 'bad')}
        ${t.aligned ? chip('已对齐', 'ok') : chip('拧花·拒绝', 'bad')}
        ${t.misrollback_open ? chip(`误退待收口 ${t.misrollback_open}`, 'warn') : ''}
      </div>
      <p class="muted">留仓误退版：
        ${(t.archived_versions || []).length
          ? t.archived_versions.map(a => `${esc(a.version_no)} (${esc(a.reason)})`).join('、')
          : '无'}
      </p>
    </article>`).join('') || '<p class="muted">暂无目录</p>'}</div>`;
}

// ---------------------------------------------------------------- 对账

async function renderReconcile() {
  const reports = (await api('/api/reconcile-reports')).items || [];
  view.innerHTML = `
    <div class="card grid">
      <div class="row">
        <button class="primary" id="btn-rec" type="button">跑一轮对账</button>
        <button type="button" id="btn-clean">跑一轮清洗复验</button>
      </div>
      <pre id="clean-out" class="muted"></pre>
      <div class="grid" id="reps">${reports.map(r => `
        <article class="card">
          <div class="row">
            <strong>${esc(r.as_of)}</strong>
            ${r.drifted ? chip('对账漂移', 'bad') : chip('对齐', 'ok')}
            ${r.misrollback_open ? chip(`误退待收口 ${r.misrollback_open}`, 'warn') : ''}
            ${r.misrollback_closed ? chip(`误退已收口 ${r.misrollback_closed}`, 'muted-chip') : ''}
          </div>
          <p>${esc(r.summary)}</p>
        </article>`).join('') || '<p class="muted">暂无对账</p>'}</div>
    </div>`;
  document.getElementById('btn-rec').onclick = async () => {
    try {
      const j = await api('/api/reconcile-reports', {
        method:'POST', body: JSON.stringify({as_of: '2026-11-18'})
      });
      setMsg(j.report.summary);
      await renderReconcile();
    } catch (e) { setMsg(e.message, true); }
  };
  document.getElementById('btn-clean').onclick = async () => {
    try {
      const j = await api('/api/cleaning', {method:'POST', body:'{}'});
      document.getElementById('clean-out').textContent =
        JSON.stringify(j.cleaning, null, 2);
      setMsg(j.cleaning.verdict, !j.cleaning.ok);
    } catch (e) { setMsg(e.message, true); }
  };
}

async function show(tab) {
  setMsg('');
  await loadContracts();
  if (tab === 'contracts') renderContracts();
  if (tab === 'check') renderCheck();
  if (tab === 'apply') renderApply();
  if (tab === 'rollback') await renderRollback();
  if (tab === 'timeline') await renderTimeline();
  if (tab === 'catalog') await renderCatalog();
  if (tab === 'reconcile') await renderReconcile();
}

document.querySelectorAll('.pills button').forEach(btn => {
  btn.onclick = () => {
    document.querySelectorAll('.pills button').forEach(b => b.classList.remove('on'));
    btn.classList.add('on');
    show(btn.dataset.tab);
  };
});
document.getElementById('btn-logout').onclick = async () => {
  await api('/api/logout', {method:'POST', body:'{}'});
  location.href = '/login';
};
show('contracts');
