'use strict';
// 원문·모델 출력은 textContent로만 넣는다(HTML로 실행하지 않음).
const $ = (s) => document.querySelector(s);
const h = (tag, attrs = {}, ...kids) => {
  const el = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (k.startsWith('on')) el.addEventListener(k.slice(2), v);
    else if (v !== undefined && v !== null && v !== false) el.setAttribute(k, v === true ? '' : v);
  }
  for (const kid of kids.flat(Infinity)) if (kid !== null && kid !== undefined) el.append(kid);
  return el;
};
async function api(path, body, method) {
  const res = await fetch(path, {
    method: method || (body ? 'POST' : 'GET'),
    headers: body ? { 'Content-Type': 'application/json' } : {},
    body: body ? JSON.stringify(body) : undefined,
  });
  const data = await res.json();
  if (!res.ok) throw new Error(data.error || res.statusText);
  return data;
}

const VERDICT = { eligible: '적격', ineligible: '부적격', undetermined: '판정 불가(정보 부족)', no_condition: '승인 조건 없음' };
const CSTATUS = { pass: '충족', fail: '미충족', unknown: '정보 없음' };
const RSTATUS = { pending: '검토 대기', approved: '승인', rejected: '반려' };
let vocab = null, current = null, tab = 'review', poll = null;
const profileDraft = {};

const supplyName = (s) => (s === 'all' ? '공통(전체 공급유형)' : vocab.supplies[s] || s);
function condText(c) {
  const a = vocab.attributes[c.attr];
  if (a.type === 'bool') return `${a.label}: ${(c.value === 1) === (c.op === 'eq') ? '예' : '아니오'}`;
  if (a.type === 'region') return `${a.label}: ${c.value} (하위 지역 포함)`;
  return c.op === 'eq' ? `${a.label} ${c.value}${a.unit}` : `${a.label} ${c.value}${a.unit} ${vocab.ops[c.op]}`;
}

async function loadNotices() {
  const list = await api('/api/notices');
  const ul = $('#notices');
  ul.replaceChildren(...list.map((n) => h('li', { class: current && current.id === n.id ? 'active' : '', onclick: () => openNotice(n.id) },
    n.title, h('small', {}, `승인 ${n.approved} · 대기 ${n.pending} · ${n.chars.toLocaleString()}자`))));
  if (!list.length) ul.replaceChildren(h('li', { class: 'empty' }, '등록된 공고가 없습니다.'));
}

async function openNotice(id) {
  current = await api(`/api/notices/${id}`);
  render();
  loadNotices();
  const running = current.runs.some((r) => r.status === 'running');
  clearTimeout(poll);
  if (running) poll = setTimeout(() => openNotice(id), 3000);
}

function render() {
  const n = current;
  const tabs = h('div', { class: 'tabs' }, ...[['review', '조건 검토'], ['judge', '자격 판정'], ['source', '공고 원문'], ['runs', '실행 기록']]
    .map(([k, label]) => h('button', { class: tab === k ? 'on' : '', onclick: () => { tab = k; render(); } }, label)));
  const body = { review: renderReview, judge: renderJudge, source: renderSource, runs: renderRuns }[tab]();
  $('#main').replaceChildren(h('section', { class: 'card' },
    h('div', { class: 'row', style: 'justify-content:space-between' }, h('h2', {}, n.title),
      h('button', { class: 'danger', onclick: (e) => deleteNotice(e) }, '공고 삭제')),
    h('p', { class: 'meta' }, `${n.filename} · SHA-256 ${n.sha256.slice(0, 16)}… · ${n.text.length.toLocaleString()}자`),
    tabs, body));
}

async function deleteNotice(e) {
  const btn = e.target;
  if (btn.dataset.armed !== '1') { btn.dataset.armed = '1'; btn.textContent = '한 번 더 누르면 삭제'; return; }
  await api(`/api/notices/${current.id}`, null, 'DELETE');
  current = null;
  $('#main').replaceChildren(h('p', { class: 'empty' }, '삭제했습니다.'));
  loadNotices();
}

function renderReview() {
  const n = current;
  const agentSel = h('select', {}, ...Object.entries(vocab.agents).map(([k, a]) => h('option', { value: k, disabled: !a.installed }, `${a.name}${a.installed ? '' : ' (미설치)'}`)));
  const model = h('input', { placeholder: '모델 ID(비우면 CLI 기본값)' });
  const msg = h('p', { class: 'msg' });
  const running = n.runs.some((r) => r.status === 'running');
  const extract = h('div', {},
    h('h3', {}, '1. LLM으로 조건 후보 추출'),
    h('div', { class: 'row' }, agentSel, model, h('button', { disabled: running, onclick: async () => {
      try { await api(`/api/notices/${n.id}/extract`, { agent: agentSel.value, model: model.value }); openNotice(n.id); }
      catch (e) { msg.className = 'msg err'; msg.textContent = e.message; }
    } }, running ? '추출 중…' : '추출 시작')),
    msg,
    h('p', { class: 'meta' }, '공고 원문이 선택한 모델 제공자에게 전달됩니다. 원문에 그대로 없는 인용·어휘 밖의 항목은 저장하지 않고 실행 기록에 남깁니다.'));

  const reviewer = h('input', { placeholder: '검토자 이름', value: localStorage.getItem('reviewer') || '' });
  const reason = h('input', { placeholder: '공통 사유(선택)' });
  const checks = new Map();
  const rmsg = h('p', { class: 'msg' });
  async function review(action, items) {
    try {
      localStorage.setItem('reviewer', reviewer.value);
      await api('/api/review', { items, action, reviewer: reviewer.value, reason: reason.value });
      openNotice(n.id);
    } catch (e) { rmsg.className = 'msg err'; rmsg.textContent = e.message; }
  }
  const selected = () => n.conditions.filter((c) => checks.get(c.id).checked).map((c) => ({ id: c.id, revision: c.revision }));
  const rows = n.conditions.map((c) => {
    const box = h('input', { type: 'checkbox', style: 'width:auto' });
    checks.set(c.id, box);
    const editor = h('textarea', { rows: 6, style: 'display:none' });
    const editBtn = h('button', { class: 'ghost', onclick: async () => {
      if (editor.style.display === 'none') {
        editor.value = JSON.stringify({ supply: c.supply, group: c.group, attr: c.attr, op: c.op, value: c.value, label: c.label, quote: c.quote }, null, 1);
        editor.style.display = ''; editBtn.textContent = '수정 저장'; return;
      }
      try {
        await api(`/api/conditions/${c.id}/revise`, { revision: c.revision, condition: JSON.parse(editor.value), reviewer: reviewer.value, reason: reason.value });
        openNotice(n.id);
      } catch (e) { rmsg.className = 'msg err'; rmsg.textContent = e.message; }
    } }, '수정');
    return h('tr', {},
      h('td', {}, box),
      h('td', {}, supplyName(c.supply), h('div', { class: 'meta' }, `그룹 ${c.group}`)),
      h('td', {}, h('b', {}, condText(c)), c.label ? h('div', { class: 'meta' }, c.label) : null, h('div', { class: 'quote' }, c.quote), editor),
      h('td', {}, h('span', { class: `badge b-${c.status}` }, RSTATUS[c.status]), h('div', { class: 'meta' }, `r${c.revision}`)),
      h('td', { style: 'white-space:nowrap' },
        h('button', { class: 'ghost', onclick: () => review('approved', [{ id: c.id, revision: c.revision }]) }, '승인'), ' ',
        h('button', { class: 'ghost', onclick: () => review('rejected', [{ id: c.id, revision: c.revision }]) }, '반려'), ' ', editBtn));
  });
  const table = n.conditions.length
    ? h('table', {}, h('tr', {}, h('th', {}, ''), h('th', {}, '공급유형'), h('th', {}, '조건 · 공고 근거'), h('th', {}, '상태'), h('th', {}, '')), rows)
    : h('p', { class: 'empty' }, '아직 조건이 없습니다. 위에서 추출을 실행하세요.');
  const supplies = n.supplies.length ? h('p', {}, '모집 공급유형: ', n.supplies.map((s) => supplyName(s.supply)).join(', ')) : null;
  const notes = n.notes.length ? h('div', {}, h('h3', {}, '규칙으로 표현하지 못한 요건(직접 확인)'),
    n.notes.map((x) => h('div', { class: 'cond unknown' }, x.text, h('div', { class: 'quote' }, x.quote)))) : null;
  return h('div', {}, extract,
    h('h3', {}, '2. 조건 검토'),
    h('p', { class: 'meta' }, '같은 그룹의 조건은 "하나만 충족하면 되는" 대안입니다. 승인한 조건만 판정에 쓰입니다. 수정하면 새 리비전이 되어 다시 검토해야 합니다.'),
    supplies,
    h('div', { class: 'row' }, reviewer, reason,
      h('button', { class: 'ghost', onclick: () => checks.forEach((b, id) => { b.checked = n.conditions.find((c) => c.id === id).status === 'pending'; }) }, '검토 대기 전체 선택'),
      h('button', { onclick: () => review('approved', selected()) }, '선택 승인'),
      h('button', { class: 'danger', onclick: () => review('rejected', selected()) }, '선택 반려')),
    rmsg, table, notes);
}

function renderJudge() {
  const n = current;
  const fields = Object.entries(vocab.attributes).map(([k, a]) => {
    let input;
    if (a.type === 'bool') {
      input = h('select', {}, h('option', { value: '' }, '모름'), h('option', { value: '1' }, '예'), h('option', { value: '0' }, '아니오'));
    } else if (a.type === 'region') {
      input = h('select', {}, h('option', { value: '' }, '모름'), ...vocab.regions.map((r) => h('option', { value: r }, r)));
    } else {
      input = h('input', { type: 'number', step: 1, placeholder: a.unit ? `단위: ${a.unit}` : '' });
    }
    if (profileDraft[k] !== undefined) input.value = String(profileDraft[k]);
    input.addEventListener('change', () => {
      if (input.value === '') delete profileDraft[k];
      else profileDraft[k] = a.type === 'region' ? input.value : Number(input.value);
    });
    return h('div', {}, h('label', {}, `${a.label}${a.unit ? ` (${a.unit})` : ''}`), input);
  });
  const out = h('div', {});
  const msg = h('p', { class: 'msg' });
  const run = h('button', { onclick: async () => {
    try { const r = await api(`/api/notices/${n.id}/judge`, { profile: profileDraft }); out.replaceChildren(renderResult(r)); msg.textContent = ''; }
    catch (e) { msg.className = 'msg err'; msg.textContent = e.message; }
  } }, '판정');
  const history = n.judgments.length ? h('div', {}, h('h3', {}, '이전 판정'), n.judgments.slice(0, 10).map((j) =>
    h('div', { class: 'row' }, h('button', { class: 'ghost', onclick: () => out.replaceChildren(renderResult(j)) }, j.created_at),
      h('span', { class: 'meta' }, j.results.map((r) => `${r.supply_name}: ${VERDICT[r.verdict]}`).join(' · '))))) : null;
  return h('div', {}, h('p', { class: 'meta' }, '모르는 항목은 비워 두세요. 비운 항목이 필요한 조건은 "정보 없음"으로 남고 부적격으로 처리하지 않습니다.'),
    h('div', { class: 'grid' }, fields), h('div', { class: 'row' }, run), msg, out, history);
}

function renderResult(r) {
  const cond = (c) => h('div', { class: `cond ${c.status}` },
    h('span', { class: `badge b-${c.status}` }, CSTATUS[c.status]), ' ', h('b', {}, c.requirement),
    h('span', { class: 'meta' }, ` · 입력값: ${c.applicant_value === null ? '없음' : c.applicant_value}${c.scope === 'all' ? ' · 공통 조건' : ''}`),
    h('div', { class: 'quote' }, c.quote));
  return h('div', {},
    r.results.map((s) => h('div', { class: 'verdict' },
      h('h3', {}, s.supply_name, h('span', { class: `badge b-${s.verdict}` }, VERDICT[s.verdict])),
      s.groups.map((g) => (g.any_of ? h('div', { class: 'anyof' }, h('div', { class: 'meta' }, `다음 중 하나 충족 (${CSTATUS[g.status]})`), g.conditions.map(cond)) : g.conditions.map(cond))))),
    r.manual_checks && r.manual_checks.length ? h('div', { class: 'verdict' }, h('h3', {}, '직접 확인할 요건'),
      r.manual_checks.map((x) => h('div', { class: 'cond unknown' }, x.text, h('div', { class: 'quote' }, x.quote)))) : null,
    r.pending_conditions ? h('p', { class: 'msg err' }, `검토 대기 조건 ${r.pending_conditions}개는 판정에 쓰지 않았습니다.`) : null,
    h('p', { class: 'meta' }, `엔진 ${r.engine} ${r.engine_version} · 규칙 ${r.rule_id} (${r.rule_sha256.slice(0, 12)}) · 조건 ${r.conditions_sha256.slice(0, 12)} · 신청자 ${r.profile_sha256.slice(0, 12)}`),
    h('p', { class: 'meta' }, r.scope));
}

const renderSource = () => h('div', { class: 'source' }, current.text);

function renderRuns() {
  if (!current.runs.length) return h('p', { class: 'empty' }, '실행 기록이 없습니다.');
  return h('table', {}, h('tr', {}, h('th', {}, '시각'), h('th', {}, '에이전트'), h('th', {}, '상태'), h('th', {}, '결과')),
    current.runs.map((r) => h('tr', {},
      h('td', {}, r.created_at), h('td', {}, `${r.agent}${r.model ? ` / ${r.model}` : ''}`),
      h('td', {}, h('span', { class: `badge b-${r.status === 'success' ? 'approved' : r.status === 'running' ? 'pending' : 'rejected'}` }, r.status)),
      h('td', {}, r.error ? h('div', { class: 'msg err' }, r.error) : null,
        r.summary ? h('div', {}, `유효 조건 ${r.summary.conditions} · 새로 추가 ${r.summary.added}` + (r.summary.rejected.length ? ` · 버린 항목 ${r.summary.rejected.length}` : '')) : null,
        r.summary && r.summary.rejected.length ? r.summary.rejected.map((x) => h('div', { class: 'meta' }, `버림(${x.kind}): ${x.reason}`)) : null))));
}

$('#up-btn').addEventListener('click', async () => {
  const msg = $('#up-msg');
  msg.className = 'msg'; msg.textContent = '등록 중…';
  try {
    const file = $('#up-file').files[0];
    const title = $('#up-title').value;
    let body;
    if (file) {
      const buf = new Uint8Array(await file.arrayBuffer());
      let bin = '';
      for (let i = 0; i < buf.length; i += 0x8000) bin += String.fromCharCode(...buf.subarray(i, i + 0x8000));
      body = { title, filename: file.name, content_base64: btoa(bin) };
    } else {
      body = { title, text: $('#up-text').value };
    }
    const { id } = await api('/api/notices', body);
    msg.textContent = '등록했습니다.';
    $('#up-text').value = ''; $('#up-file').value = ''; $('#up-title').value = '';
    tab = 'review';
    openNotice(id);
  } catch (e) { msg.className = 'msg err'; msg.textContent = e.message; }
});

(async () => { vocab = await api('/api/vocab'); loadNotices(); })();
