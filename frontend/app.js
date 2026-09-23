'use strict';

/** Rút thông báo lỗi có thể đọc được từ mọi hình dạng lỗi FastAPI trả về:
 *  chuỗi, {"detail": "..."}, hoặc {"detail": [{msg, loc, ...}, ...]} (lỗi
 *  validate tự động của Pydantic khi body không khớp schema). */
function errorMessage(data, fallback) {
  if (typeof data === 'string' && data.trim()) return data;
  const detail = data?.detail;
  if (typeof detail === 'string' && detail.trim()) return detail;
  if (Array.isArray(detail) && detail.length) {
    return detail.map(d => {
      if (typeof d === 'string') return d;
      const field = Array.isArray(d?.loc) ? d.loc.filter(x => x !== 'body').join('.') : '';
      return field ? `${field}: ${d?.msg || JSON.stringify(d)}` : (d?.msg || JSON.stringify(d));
    }).join('; ');
  }
  // FastAPI 409-style detail: {"message": "...", "linked_agents": [...]}
  if (detail && typeof detail === 'object' && typeof detail.message === 'string') return detail.message;
  return fallback;
}

const api = async (path, opts = {}) => {
  const res = await fetch(path, opts);
  if (res.status === 204) return null;
  const text = await res.text();
  let data; try { data = text ? JSON.parse(text) : null; } catch { data = text; }
  if (!res.ok) {
    const err = new Error(errorMessage(data, res.statusText));
    err.status = res.status;
    err.data = data;               // callers can read e.g. data.detail.linked_agents
    throw err;
  }
  return data;
};
const el = (id) => document.getElementById(id);
const esc = (s) => String(s ?? '').replace(/[&<>"]/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
const j = (o) => esc(JSON.stringify(o, null, 1));

const state = {
  residents: [], sessionId: null, resident: null,
  tickets: [], ticketId: null, source: null, members: [],
  agents: [], agent: null, tools: [], domain: null,
  chatSource: null, chatTicket: null,
  deciding: new Set(),
};

/* ------------------------------------------------------------------ tabs */
function showTab(name) {
  const btn = document.querySelector(`.tab[data-tab="${name}"]`);
  if (!btn) return;
  document.querySelectorAll('.tab').forEach(b => b.classList.remove('tab-active'));
  btn.classList.add('tab-active');
  document.querySelectorAll('.tabpane').forEach(p => p.classList.add('hidden'));
  el('tab-' + name).classList.remove('hidden');
  if (location.hash.slice(1) !== name) history.replaceState(null, '', '#' + name);
  ({ room: loadTickets, builder: loadAgents, tools: loadTools, approvals: loadApprovals }[name] || (() => {}))();
}
document.querySelectorAll('.tab').forEach(btn => { btn.onclick = () => showTab(btn.dataset.tab); });
window.addEventListener('hashchange', () => showTab(location.hash.slice(1) || 'resident'));

/* ============================== TAB 1 : CƯ DÂN ============================== */
const newSession = () => {
  state.sessionId = 'ss-' + Math.random().toString(36).slice(2, 10);
  el('chatLog').innerHTML = '';
  el('ticketBanner').classList.add('hidden');
  if (state.chatSource) { state.chatSource.close(); state.chatSource = null; }
  state.chatTicket = null;
  bubble('receptionist', `Xin chào, ${state.domain?.self_reference || 'chúng tôi'} có thể giúp gì cho ${state.domain?.honorific || 'bạn'}?`);
};

/** Hiện trạng thái "đang xử lý nội bộ" trong khung chat. */
function setWorking(on, text) {
  let node = el('chatWorking');
  if (!on) { node?.remove(); return; }
  if (!node) {
    node = document.createElement('div');
    node.id = 'chatWorking';
    el('chatLog').appendChild(node);
  }
  node.className = 'text-xs italic ' + (text ? 'text-amber-600' : 'text-slate-400');
  node.textContent = text || 'Các bộ phận đang trao đổi nội bộ…';
  el('chatLog').scrollTop = el('chatLog').scrollHeight;
}

/**
 * Theo dõi ticket vừa tạo để tin của Lễ tân xuất hiện ngay trong khung chat.
 * Nội dung gửi người báo thuộc về giao diện người dùng, không phải timeline phòng họp.
 */
function followTicket(ticketId) {
  if (state.chatSource) state.chatSource.close();
  state.chatTicket = ticketId;
  setWorking(true);
  const src = new EventSource(`/api/tickets/${ticketId}/events`);
  state.chatSource = src;
  src.addEventListener('receptionist_reply', (e) => {
    if (state.chatTicket !== ticketId) return;
    setWorking(false);
    bubble('receptionist', JSON.parse(e.data).payload.tin_nhan);
  });
  // Phòng họp dừng chờ duyệt: người báo thấy rõ là đang chờ xem xét, không phải treo.
  src.addEventListener('room_waiting', () => {
    if (state.chatTicket === ticketId) setWorking(true, 'Một hành động đang chờ ban quản lý xem xét…');
  });
  src.addEventListener('room_resumed', () => {
    if (state.chatTicket === ticketId) setWorking(true);
  });
  src.addEventListener('room_end', () => { if (state.chatTicket === ticketId) setWorking(false); });
  src.onerror = () => setWorking(false);
}

function bubble(role, text) {
  const mine = role === 'user';
  const div = document.createElement('div');
  div.className = 'flex ' + (mine ? 'justify-end' : 'justify-start');
  div.innerHTML = `<div class="${mine ? 'bg-blue-600 text-white' : 'bg-slate-100'} rounded-lg px-3 py-2 text-sm max-w-[80%] whitespace-pre-wrap">${esc(text)}</div>`;
  el('chatLog').appendChild(div);
  el('chatLog').scrollTop = el('chatLog').scrollHeight;
}

el('newSession').onclick = newSession;
el('residentSel').onchange = () => {
  state.resident = state.residents.find(r => r.ma_cu_dan === el('residentSel').value);
  el('residentInfo').innerHTML = state.resident ? `
    <div>Căn hộ: <b>${esc(state.resident.ma_can_ho)}</b></div>
    <div>Điện thoại: ${esc(state.resident.dien_thoai)}</div>
    <div class="text-xs text-slate-400">Mã: ${esc(state.resident.ma_cu_dan)} · ${esc(state.resident.vai_tro)}</div>` : '';
  newSession();
};

el('chatForm').onsubmit = async (e) => {
  e.preventDefault();
  const msg = el('chatInput').value.trim();
  if (!msg || !state.resident) return;
  el('chatInput').value = '';
  bubble('user', msg);
  el('chatSend').disabled = true;
  const wait = document.createElement('div');
  wait.className = 'text-xs text-slate-400'; wait.textContent = 'Lễ tân đang soạn…';
  el('chatLog').appendChild(wait);
  try {
    const r = await api('/api/chat', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ session_id: state.sessionId, resident_id: state.resident.ma_cu_dan, message: msg }),
    });
    wait.remove();
    bubble('receptionist', r.tra_loi);
    if (r.ticket) {
      followTicket(r.ticket.id);
      el('ticketBanner').classList.remove('hidden');
      el('ticketBanner').innerHTML =
        `Đã tạo phản ánh <b>${esc(r.ticket.id)}</b> · ưu tiên <b>${esc(r.ticket.priority)}</b><br>
         <button class="mt-2 text-blue-700 underline" onclick="window.openRoom('${esc(r.ticket.id)}')">Xem phòng họp trực tiếp →</button>`;
    }
  } catch (err) { wait.remove(); bubble('receptionist', 'Lỗi: ' + err.message); }
  el('chatSend').disabled = false;
};

window.openRoom = (id) => {
  showTab('room');
  setTimeout(() => selectTicket(id), 100);
};

/* ============================== TAB 2 : PHÒNG HỌP ============================== */
const PRIO = { KHAN_CAP: 'bg-red-100 text-red-700', BINH_THUONG: 'bg-amber-100 text-amber-700', THAP: 'bg-slate-100 text-slate-600' };
const STATUS = { dang_tiep_nhan:'Đang tiếp nhận', dang_xu_ly:'Đang xử lý', cho_duyet:'Chờ duyệt', cho_cu_dan:'Chờ người báo', hoan_tat:'Hoàn tất' };

async function loadTickets() {
  state.tickets = await api('/api/tickets');
  el('ticketList').innerHTML = state.tickets.length ? state.tickets.map(t => `
    <button class="w-full text-left border rounded p-2 hover:bg-slate-50 ${t.id === state.ticketId ? 'border-blue-500 bg-blue-50' : ''}" onclick="selectTicket('${t.id}')">
      <div class="flex justify-between gap-1 items-center">
        <span class="font-mono text-xs">${esc(t.id)}</span>
        <span class="text-[10px] px-1.5 py-0.5 rounded ${PRIO[t.priority] || ''}">${esc(t.priority)}</span>
      </div>
      <div class="text-xs mt-1 line-clamp-2">${esc(t.summary || t.fields?.mo_ta || '')}</div>
      <div class="text-[10px] text-slate-500 mt-1">${esc(STATUS[t.status] || t.status)}</div>
    </button>`).join('') : '<div class="text-sm text-slate-400 p-2">Chưa có phản ánh nào.</div>';
}
el('refreshTickets').onclick = loadTickets;

window.selectTicket = async (id) => {
  state.ticketId = id;
  loadTickets();
  el('timeline').innerHTML = '';
  el('replyBox').innerHTML = '';
  setRoomPause(null);
  const t = await api('/api/tickets/' + id);
  el('roomHeader').innerHTML = `<span class="font-mono">${esc(t.id)}</span> · ${esc(STATUS[t.status] || t.status)} ·
    <span class="text-xs">${esc(t.summary || '')}</span>`;
  state.members = [];
  if (state.source) state.source.close();
  state.source = new EventSource(`/api/tickets/${id}/events`);
  ['room_start','router_decision','agent_start','rag_hits','tool_call','tool_result','action_pending',
   'agent_output','guard_triggered','room_end','receptionist_reply','action_executed',
   'room_waiting','room_resumed']
    .forEach(type => state.source.addEventListener(type, (e) => renderEvent(JSON.parse(e.data))));
};

function renderMembers(active) {
  el('memberList').innerHTML = state.members.map(m =>
    `<div class="px-2 py-1 rounded ${m.id === active ? 'bg-blue-100 text-blue-800 font-medium' : 'bg-slate-50'}">${esc(m.display_name)}</div>`
  ).join('') || '<div class="text-xs text-slate-400">—</div>';
}

const EV = {
  room_start:        { c:'border-slate-400',  t:'Mở phòng họp' },
  router_decision:   { c:'border-purple-500', t:'Điều phối' },
  agent_start:       { c:'border-blue-500',   t:'Agent phát biểu' },
  rag_hits:          { c:'border-teal-500',   t:'Tra tài liệu' },
  tool_call:         { c:'border-orange-500', t:'Gọi tool' },
  tool_result:       { c:'border-orange-300', t:'Kết quả tool' },
  action_pending:    { c:'border-red-500',    t:'Chờ duyệt' },
  agent_output:      { c:'border-green-500',  t:'Kết luận agent' },
  guard_triggered:   { c:'border-yellow-500', t:'Guard' },
  room_end:          { c:'border-slate-500',  t:'Kết thúc' },
  receptionist_reply:{ c:'border-indigo-500', t:'Lễ tân trả lời' },
  action_executed:   { c:'border-green-600',  t:'Đã thực hiện' },
  room_waiting:      { c:'border-amber-500',  t:'Tạm dừng chờ duyệt' },
  room_resumed:      { c:'border-amber-300',  t:'Họp tiếp' },
};

/** Băng-rôn "phòng họp đang đứng im": chỗ duyệt ngay tại tab Phòng họp. */
function setRoomPause(p) {
  const box = el('roomPause');
  if (!p) { box.classList.add('hidden'); box.innerHTML = ''; return; }
  box.classList.remove('hidden');
  box.innerHTML = `
    <div class="font-semibold text-amber-900">⏸ Phòng họp đang dừng, chờ quản lý duyệt</div>
    <div class="mt-1">Tool <span class="font-mono">${esc(p.tool)}</span> · hành động <b>${esc(p.action_id)}</b>
      ${p.han_cho_giay ? `· tự chạy tiếp sau ${Math.round(p.han_cho_giay)}s nếu không có quyết định` : ''}</div>
    <pre class="text-xs bg-white border rounded p-2 mt-2">${j(p.args || {})}</pre>
    <div class="mt-2 flex gap-2">
      <button class="bg-green-600 text-white text-sm px-3 py-1 rounded" onclick="decide('${p.action_id}','approve')">Duyệt và họp tiếp</button>
      <button class="border border-red-300 text-red-600 text-sm px-3 py-1 rounded" onclick="decide('${p.action_id}','reject')">Từ chối</button>
    </div>`;
}

function renderEvent(ev) {
  const meta = EV[ev.type] || { c:'border-slate-300', t:ev.type };
  const p = ev.payload || {};
  let body = '';

  if (ev.type === 'room_start') {
    state.members = p.thanh_vien || []; renderMembers(null);
    body = `${p.so_thanh_vien} thành viên · tối đa ${p.max_turns} lượt · engine <b>${esc(p.engine || '-')}</b>`;
  } else if (ev.type === 'router_decision') {
    body = p.hanh_dong === 'ket_thuc'
      ? `<b>Kết thúc.</b> ${esc(p.ly_do)}`
      : `Chọn <b>${esc(p.agent_id)}</b><div class="text-slate-600 mt-1">Chỉ dẫn: ${esc(p.chi_dan)}</div><div class="text-xs text-slate-500">Lý do: ${esc(p.ly_do)}</div>`;
  } else if (ev.type === 'agent_start') {
    renderMembers(p.agent_id);
    body = `<b>${esc(p.display_name)}</b> bắt đầu lượt`;
  } else if (ev.type === 'rag_hits') {
    body = p.so_ket_qua
      ? `${p.so_ket_qua} đoạn: ` + (p.ket_qua || []).map(h => `<span class="font-mono text-xs bg-teal-50 px-1 rounded">${esc(h.filename)}</span>`).join(' ') +
        `<details class="mt-1"><summary class="text-xs cursor-pointer text-slate-500">xem trích đoạn</summary><pre class="text-xs bg-slate-50 p-2 rounded mt-1">${esc((p.ket_qua||[]).map(h=>`[${h.filename}] ${h.trich}`).join('\n\n'))}</pre></details>`
      : '<span class="text-slate-500">không có tài liệu khớp</span>';
  } else if (ev.type === 'tool_call') {
    body = `<span class="font-mono">${esc(p.tool)}</span> <span class="text-xs text-slate-500">(${esc(p.provider)})</span>
      ${p.tham_so_bi_ghi_de?.length ? `<div class="text-xs text-red-600 mt-1">Hệ thống ghi đè tham số: ${esc(p.tham_so_bi_ghi_de.join(', '))}</div>` : ''}
      <details class="mt-1"><summary class="text-xs cursor-pointer text-slate-500">tham số</summary><pre class="text-xs bg-slate-50 p-2 rounded mt-1">${j(p.args)}</pre></details>`;
  } else if (ev.type === 'tool_result') {
    body = `<span class="font-mono">${esc(p.tool)}</span><details class="mt-1"><summary class="text-xs cursor-pointer text-slate-500">kết quả</summary><pre class="text-xs bg-slate-50 p-2 rounded mt-1">${j(p.ket_qua)}</pre></details>`;
  } else if (ev.type === 'action_pending') {
    body = `<span class="font-mono">${esc(p.tool)}</span> → <b>${esc(p.action_id)}</b> đang chờ quản lý duyệt
      ${p.phong_hop_dang_cho ? '<span class="text-xs text-amber-700">· phòng họp dừng lại chờ quyết định</span>' : ''}
      <pre class="text-xs bg-slate-50 p-2 rounded mt-1">${j(p.args)}</pre>`;
    loadApprovals();
  } else if (ev.type === 'room_waiting') {
    setRoomPause(p);
    body = `Tạm dừng ở tool <span class="font-mono">${esc(p.tool)}</span>, chờ duyệt <b>${esc(p.action_id)}</b>
      <span class="text-xs text-slate-500">(hạn chờ ${Math.round(p.han_cho_giay || 0)}s)</span>`;
    loadApprovals();
  } else if (ev.type === 'room_resumed') {
    setRoomPause(null);
    const QD = { da_duyet:'quản lý đã duyệt', tu_choi:'quản lý từ chối', cho_duyet:'hết hạn chờ, chạy tiếp' };
    body = `Họp tiếp sau ${p.cho_bao_lau_giay}s — <b>${esc(QD[p.quyet_dinh] || p.quyet_dinh)}</b>
      <span class="text-xs text-slate-500">(${esc(p.action_id)} · ${esc(p.tool)})</span>`;
    loadTickets(); loadApprovals();
  } else if (ev.type === 'agent_output') {
    const o = p.output || {};
    body = `<b>${esc(p.display_name)}</b><div class="mt-1">${esc(o.ket_luan)}</div>
      ${o.da_thuc_hien?.length ? `<div class="text-xs mt-1">Đã làm: ${esc(o.da_thuc_hien.join('; '))}</div>` : ''}
      ${o.de_xuat?.length ? `<div class="text-xs">Đề xuất: ${esc(o.de_xuat.join('; '))}</div>` : ''}
      ${o.can_them_agent?.length ? `<div class="text-xs text-purple-700">Cần thêm: ${esc(o.can_them_agent.join(', '))}</div>` : ''}
      ${o.nguon?.length ? `<div class="text-xs text-teal-700">Nguồn: ${esc(o.nguon.join(', '))}</div>` : ''}
      <details class="mt-1"><summary class="text-xs cursor-pointer text-slate-500">JSON</summary><pre class="text-xs bg-slate-50 p-2 rounded mt-1">${j(o)}</pre></details>`;
  } else if (ev.type === 'guard_triggered') {
    body = `<b>${esc(p.guard)}</b> → ${esc(p.xu_ly || '')}<pre class="text-xs bg-yellow-50 p-2 rounded mt-1">${j(p)}</pre>`;
  } else if (ev.type === 'room_end') {
    renderMembers(null);
    body = `${p.so_luot} lượt · ${esc(p.ly_do_ket_thuc || p.loi || '')} · ticket → <b>${esc(STATUS[p.trang_thai_ticket] || p.trang_thai_ticket || '')}</b>`;
    setRoomPause(null);
    loadTickets(); loadApprovals();
  } else if (ev.type === 'receptionist_reply') {
    // Nội dung gửi người báo hiển thị ở tab Cư dân; ở đây chỉ đánh dấu đã gửi.
    body = `<span class="text-slate-500">Đã gửi tin cho người báo${p.loai === 'cap_nhat' ? ' (cập nhật)' : ''} — xem tab <b>Cư dân</b></span>`;
    el('replyBox').innerHTML += `<div class="border rounded p-2 bg-indigo-50 whitespace-pre-wrap">${esc(p.tin_nhan)}</div>`;
  } else if (ev.type === 'action_executed') {
    body = `<span class="font-mono">${esc(p.tool)}</span> ${p.thanh_cong ? 'thành công' : 'lỗi'}<pre class="text-xs bg-slate-50 p-2 rounded mt-1">${j(p.ket_qua)}</pre>`;
  } else {
    body = `<pre class="text-xs">${j(p)}</pre>`;
  }

  const d = document.createElement('div');
  d.className = `ev ${meta.c} bg-white rounded-r pl-3 pr-2 py-2 text-sm shadow-sm`;
  d.innerHTML = `<div class="text-[10px] uppercase tracking-wide text-slate-400 mb-0.5">#${ev.seq} · ${meta.t}${ev.actor ? ' · ' + esc(ev.actor) : ''}</div>${body}`;
  el('timeline').appendChild(d);
  d.scrollIntoView({ block: 'nearest' });
}

/* ============================== TAB 3 : BUILDER ============================== */
const PROMPT_TEMPLATE = `## Vai trò
(Bộ phận này chịu trách nhiệm gì?)

## Nhiệm vụ
1. …
2. …

## Quy tắc riêng
- …`;

async function loadAgents() {
  state.agents = await api('/api/agents');
  if (!state.tools.length) state.tools = await api('/api/tools');
  const badge = (a) => a.is_core ? '<span class="text-[10px] bg-slate-200 px-1 rounded">lõi</span>'
    : a.status === 'active' ? '<span class="text-[10px] bg-green-100 text-green-700 px-1 rounded">active</span>'
    : a.status === 'draft' ? '<span class="text-[10px] bg-amber-100 text-amber-700 px-1 rounded">draft</span>'
    : '<span class="text-[10px] bg-slate-200 px-1 rounded">tắt</span>';
  el('agentList').innerHTML = state.agents.map(a => `
    <button class="w-full text-left border rounded p-2 hover:bg-slate-50 ${a.id === state.agent?.id ? 'border-blue-500 bg-blue-50' : ''}" onclick="editAgent('${a.id}')">
      <div class="flex justify-between items-center gap-1"><span class="text-sm font-medium">${esc(a.display_name)}</span>${badge(a)}</div>
      <div class="text-[10px] font-mono text-slate-500">${esc(a.id)} · v${a.version} · ${a.tools.length} tool</div>
    </button>`).join('') || '<div class="text-sm text-slate-400 p-2">Chưa có agent nào.</div>';
}

function renderToolPicker(selected) {
  const groups = { platform: 'Platform', domain: 'Domain', business: 'Nghiệp vụ' };
  el('toolPicker').innerHTML = Object.entries(groups).map(([scope, label]) => {
    const rows = state.tools.filter(t => t.scope === scope);
    if (!rows.length) return '';
    return `<div><div class="text-xs uppercase text-slate-400 mb-1">${label}</div>` + rows.map(t => `
      <label class="flex items-start gap-2 py-0.5 ${t.available ? '' : 'opacity-50'}">
        <input type="checkbox" class="toolchk mt-1" value="${esc(t.name)}" ${selected.includes(t.name) ? 'checked' : ''} ${t.available ? '' : 'disabled'}>
        <span><span class="font-mono text-xs">${esc(t.name)}</span>
        ${t.requires_approval ? '<span title="cần duyệt">🔒</span>' : ''}
        ${t.available ? '' : '<span class="text-[10px] text-red-600">(không khả dụng)</span>'}
        <div class="text-xs text-slate-500">${esc(t.manager_description)}</div></span>
      </label>`).join('') + '</div>';
  }).join('');
  document.querySelectorAll('.toolchk').forEach(c => c.onchange = updateToolCount);
  updateToolCount();
}
function selectedTools() { return [...document.querySelectorAll('.toolchk:checked')].map(c => c.value); }
function updateToolCount() {
  const n = selectedTools().length;
  el('toolCount').textContent = n;
  if (n > 10) { alert('Tối đa 10 tool cho mỗi agent.'); event?.target && (event.target.checked = false); el('toolCount').textContent = selectedTools().length; }
}

/** Đếm ký tự mô tả năng lực trực tiếp khi gõ, tô đỏ tới khi đủ 20 ký tự (khớp
 *  yêu cầu của backend), và tự động rút gọn mã agent về đúng dạng slug hợp lệ. */
function updateCapCount() {
  const n = el('agentForm').capability.value.trim().length;
  const span = el('capCount');
  span.textContent = `${n}/20`;
  span.className = 'shrink-0 pl-2 ' + (n >= 20 ? 'text-green-600' : 'text-red-500');
}
el('agentForm').capability.addEventListener('input', updateCapCount);
el('agentForm').id.addEventListener('input', (e) => {
  e.target.value = e.target.value.toLowerCase().replace(/[^a-z0-9_-]/g, '');
});

/** Trạng thái chốt chặn đánh giá cho nút Bật — đọc trực tiếp lịch sử chạy đánh
 *  giá, không đoán; lý do chính xác (kể cả trượt hồi quy) vẫn hiện khi bấm Bật
 *  thật và bị 409. */
async function renderEvalGateStatus(a) {
  const box = el('evalGateStatus');
  if (a.is_core) { box.innerHTML = ''; return; }
  try {
    const runs = await api(`/api/eval/agents/${a.id}/runs`);
    const passed = runs.find(r => r.passed === true && r.agent_version === a.version);
    if (a.status === 'active') {
      box.innerHTML = '<span class="text-green-700">● Đang hoạt động</span>';
    } else if (passed) {
      box.innerHTML = `<span class="text-green-700">✓ Đã đạt đánh giá trên v${a.version} (lần chạy ${passed.id})</span>`;
    } else {
      box.innerHTML = `<span class="text-amber-700">○ Chưa có lần đánh giá nào đạt trên v${a.version} — bấm Bật sẽ bị chặn trừ khi ép buộc</span>`;
    }
  } catch { box.innerHTML = ''; }
}

window.editAgent = async (id) => {
  const a = await api('/api/agents/' + id);
  state.agent = a;
  el('builderEmpty').classList.add('hidden');
  el('builderAiBox').classList.add('hidden');
  el('agentForm').classList.remove('hidden');
  el('sandboxOut').innerHTML = '';
  const f = el('agentForm');
  f.display_name.value = a.display_name; f.id.value = a.id; f.id.readOnly = true;
  f.capability.value = a.capability; f.business_prompt.value = a.business_prompt || '';
  el('coreWarn').classList.toggle('hidden', !a.is_core);
  [...f.elements].forEach(x => { if (x.type !== 'button') x.disabled = a.is_core; });
  renderToolPicker(a.tools);
  if (a.is_core) document.querySelectorAll('.toolchk').forEach(c => c.disabled = true);
  renderDocs(a.knowledge || []);
  updateCapCount();
  renderEvalGateStatus(a);
  loadAgents();
};

async function renderBuilderDocPicker() {
  const docs = await api('/api/knowledge');
  el('builderDocPicker').innerHTML = docs.length
    ? docs.map(d => `<label class="flex items-center gap-1.5">
        <input type="checkbox" class="builderDocChk" value="${esc(d.id)}">
        <span class="font-mono">${esc(d.filename)}</span>
        <span class="text-slate-400">(${esc(d.scope)})</span>
      </label>`).join('')
    : '<span class="text-slate-400">Thư viện chưa có tài liệu nào.</span>';
}

el('newAgent').onclick = () => {
  state.agent = null;
  el('builderEmpty').classList.add('hidden');
  el('agentForm').classList.remove('hidden');
  el('coreWarn').classList.add('hidden');
  el('sandboxOut').innerHTML = '';
  el('evalGateStatus').innerHTML = '';
  const f = el('agentForm');
  f.reset(); f.id.readOnly = false;
  [...f.elements].forEach(x => x.disabled = false);
  f.business_prompt.value = PROMPT_TEMPLATE;
  renderToolPicker([]);
  el('docList').innerHTML = '<div class="text-xs text-slate-400">Lưu agent trước rồi mới tải tài liệu được.</div>';
  updateCapCount();

  // Ô "Tạo bằng mô tả" chỉ có ý nghĩa khi đang tạo agent MỚI.
  el('builderAiBox').classList.remove('hidden');
  el('builderYeuCau').value = '';
  el('builderAiResult').innerHTML = '';
  el('builderAiStatus').textContent = '';
  renderBuilderDocPicker();
};

/** Hiển thị giải thích/cảnh báo Builder trả về — quản lý đọc trước khi Lưu. */
function renderBuilderOutput(out) {
  const rows = [];
  const tools = out.giai_thich?.tools || {};
  if (Object.keys(tools).length) {
    rows.push('<div class="font-medium">Vì sao chọn tool này:</div>' +
      Object.entries(tools).map(([t, why]) =>
        `<div class="text-xs pl-2">• <span class="font-mono">${esc(t)}</span>: ${esc(why)}</div>`).join(''));
  }
  if (out.thieu_tool?.length) {
    rows.push(`<div class="bg-amber-50 border border-amber-300 rounded p-2 text-xs">
      <b>Thiếu tool phù hợp</b> — Builder cố ý để trống thay vì chọn nhầm: ${esc(out.thieu_tool.join('; '))}</div>`);
  }
  if (out.chong_lan?.length) {
    rows.push(`<div class="bg-orange-50 border border-orange-300 rounded p-2 text-xs">
      <b>Có thể chồng lấn với agent khác:</b><br>${out.chong_lan.map(c =>
        `• ${esc(c.agent_id)} (${esc(c.muc_do)}): ${esc(c.diem_trung)}`).join('<br>')}</div>`);
  }
  if (out.canh_bao?.length) {
    rows.push(`<div class="bg-red-50 border border-red-300 rounded p-2 text-xs">
      <b>Cảnh báo:</b><br>${out.canh_bao.map(esc).join('<br>')}</div>`);
  }
  if (out.thay_doi?.length) {
    rows.push(`<div class="bg-blue-50 border border-blue-300 rounded p-2 text-xs">
      <b>Builder đã tự sửa:</b><br>${out.thay_doi.map(esc).join('<br>')}</div>`);
  }
  el('builderAiResult').innerHTML = rows.join('');
}

function fillFormFromDraft(draft) {
  const f = el('agentForm');
  f.display_name.value = draft.display_name || '';
  f.id.value = draft.id || '';
  f.capability.value = draft.capability || '';
  f.business_prompt.value = draft.business_prompt || '';
  renderToolPicker(draft.tools || []);
  updateCapCount();
}

el('btnBuilderDraft').onclick = async () => {
  const yeu_cau = el('builderYeuCau').value.trim();
  if (yeu_cau.length < 5) return alert('Mô tả yêu cầu ngắn quá, viết rõ hơn một chút.');
  el('btnBuilderDraft').disabled = true;
  el('builderAiStatus').textContent = 'Builder đang soạn nháp…';
  try {
    const out = await api('/api/builder/draft', { method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ yeu_cau }) });
    fillFormFromDraft(out.draft);
    renderBuilderOutput(out);
    el('builderAiStatus').textContent = 'Đã soạn xong — xem lại bên dưới, sửa nếu cần rồi bấm Lưu.';
  } catch (err) {
    el('builderAiStatus').textContent = '';
    alert('Lỗi: ' + err.message);
  }
  el('btnBuilderDraft').disabled = false;
};

el('btnBuilderAuto').onclick = async () => {
  const yeu_cau = el('builderYeuCau').value.trim();
  if (yeu_cau.length < 5) return alert('Mô tả yêu cầu ngắn quá, viết rõ hơn một chút.');
  const doc_ids = [...document.querySelectorAll('.builderDocChk:checked')].map(c => c.value);
  if (!confirm('Builder sẽ tự soạn, Evaluator sẽ tự sinh case và chấm, lặp lại nếu chưa đạt '
    + '(tối đa 2 vòng). Việc này có thể mất TỚI 15–20 PHÚT do phải chạy nhiều phòng họp thật và '
    + 'kiểm tra định tuyến trên toàn bộ case đã duyệt. Tiếp tục?')) return;

  el('btnBuilderDraft').disabled = true;
  el('btnBuilderAuto').disabled = true;
  el('builderAiStatus').textContent = 'Đang chạy vòng lặp Builder + Evaluator… có thể mất nhiều phút, đừng đóng tab.';
  el('builderAiResult').innerHTML = '';
  try {
    const result = await api('/api/builder/auto', { method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ yeu_cau, doc_ids }) });
    el('builderAiStatus').textContent = result.passed
      ? `✓ Đạt sau ${result.history.length} vòng.`
      : `✗ Chưa đạt sau ${result.history.length} vòng — vẫn ở trạng thái draft, xem chi tiết bên dưới.`;
    el('builderAiResult').innerHTML = result.history.map((h, i) => `
      <div class="border rounded p-2 text-xs ${h.passed && h.regression_dat ? 'bg-green-50' : 'bg-slate-50'}">
        <b>Vòng ${h.round}</b> — đánh giá: ${h.passed ? '✓ đạt' : '✗ chưa đạt'},
        hồi quy: ${h.regression_dat ? '✓ đạt' : '✗ chưa đạt'}
        ${h.case_fail?.length ? `<br>Case chưa đạt: ${h.case_fail.join(', ')}` : ''}
      </div>`).join('');
    if (result.agent_id) {
      await editAgent(result.agent_id);
      toast('Đã tạo agent "' + result.agent_id + '" ở trạng thái draft — kiểm tra rồi tự bấm Bật.');
    }
  } catch (err) {
    el('builderAiStatus').textContent = '';
    alert('Lỗi: ' + err.message);
  }
  el('btnBuilderDraft').disabled = false;
  el('btnBuilderAuto').disabled = false;
};

el('agentForm').onsubmit = async (e) => {
  e.preventDefault();
  const f = e.target;
  // Bắt lỗi ngay tại form (id đúng dạng slug, mô tả năng lực đủ 20 ký tự...) trước
  // khi gửi lên server, để không phải chờ 422 mới biết trường nào sai.
  if (!f.reportValidity()) return;
  const body = {
    display_name: f.display_name.value.trim(),
    capability: f.capability.value.trim(),
    business_prompt: f.business_prompt.value,
    tools: selectedTools(),
  };
  try {
    if (state.agent) {
      await api('/api/agents/' + state.agent.id, { method: 'PUT', headers: { 'Content-Type':'application/json' }, body: JSON.stringify(body) });
      await editAgent(state.agent.id);
    } else {
      const created = await api('/api/agents', { method: 'POST', headers: { 'Content-Type':'application/json' },
        body: JSON.stringify({ ...body, id: f.id.value.trim(), status: 'draft' }) });
      await editAgent(created.id);
    }
    toast('Đã lưu.');
  } catch (err) { alert('Lỗi: ' + err.message); }
};

const agentAction = (path, ok) => async () => {
  if (!state.agent) return;
  try { await api(`/api/agents/${state.agent.id}/${path}`, { method: 'POST' }); await editAgent(state.agent.id); toast(ok); }
  catch (err) { alert('Lỗi: ' + err.message); }
};

el('btnActivate').onclick = async () => {
  if (!state.agent) return;
  try {
    await api(`/api/agents/${state.agent.id}/activate`, { method: 'POST' });
    await editAgent(state.agent.id);
    toast('Đã bật agent.');
  } catch (err) {
    // 409 = chưa qua chốt chặn đánh giá (chưa đánh giá đạt, hoặc trượt hồi quy
    // định tuyến) — cho quản lý xem lý do thật rồi tự quyết định có ép bật không.
    if (err.status === 409) {
      if (confirm(`Không bật được: ${err.message}\n\nVẫn muốn BẬT BẤT CHẤP (force)? Việc này sẽ được ghi log cảnh báo.`)) {
        try {
          const r = await api(`/api/agents/${state.agent.id}/activate?force=true`, { method: 'POST' });
          await editAgent(state.agent.id);
          toast(r.forced ? 'Đã bật (force — bỏ qua chốt chặn đánh giá).' : 'Đã bật agent.');
        } catch (err2) { alert('Lỗi: ' + err2.message); }
      }
    } else {
      alert('Lỗi: ' + err.message);
    }
  }
};
el('btnDisable').onclick = agentAction('disable', 'Đã tắt agent.');

el('btnDelete').onclick = async () => {
  if (!state.agent || !confirm(`Xóa agent "${state.agent.display_name}"? Không hoàn tác được.`)) return;
  try { await api('/api/agents/' + state.agent.id, { method: 'DELETE' }); state.agent = null;
    el('agentForm').classList.add('hidden'); el('builderEmpty').classList.remove('hidden'); loadAgents(); }
  catch (err) { alert('Lỗi: ' + err.message); }
};

el('btnVersions').onclick = async () => {
  if (!state.agent) return;
  const vs = await api(`/api/agents/${state.agent.id}/versions`);
  el('sandboxOut').innerHTML = `<div class="border rounded p-3 bg-slate-50"><h3 class="font-semibold text-sm mb-2">Lịch sử phiên bản</h3>` +
    vs.map(v => `<div class="flex items-center justify-between border-b py-1 text-sm">
      <span>v${v.version} · <span class="text-xs text-slate-500">${esc(v.created_at)}</span></span>
      <button class="text-xs border rounded px-2 py-0.5 hover:bg-white" onclick="rollback(${v.version})">Khôi phục</button>
    </div>`).join('') + '</div>';
};
window.rollback = async (v) => {
  try { await api(`/api/agents/${state.agent.id}/rollback/${v}`, { method: 'POST' }); await editAgent(state.agent.id); toast('Đã khôi phục v' + v); }
  catch (err) { alert('Lỗi: ' + err.message); }
};

el('btnTryAgent').onclick = async () => {
  if (!state.agent) return;
  const text = prompt('Nội dung phản ánh giả để chạy thử:', 'Rác tồn đọng ở hành lang tầng 12 hai ngày nay chưa ai dọn');
  if (!text) return;
  el('sandboxOut').innerHTML = '<div class="text-sm text-slate-500">Đang chạy thử…</div>';
  try {
    const r = await api(`/api/sandbox/agent/${state.agent.id}`, { method: 'POST', headers: { 'Content-Type':'application/json' }, body: JSON.stringify({ ticket_text: text }) });
    el('sandboxOut').innerHTML = `<div class="border rounded p-3 bg-slate-50 text-sm space-y-2">
      <h3 class="font-semibold">Kết quả chạy thử</h3>
      <div><b>Kết luận:</b> ${esc(r.output.ket_luan)}</div>
      <div><b>Tool đã gọi:</b> ${r.tool_calls.length ? r.tool_calls.map(t => `<span class="font-mono text-xs">${esc(t.tool)}</span>`).join(', ') : '(không)'}</div>
      <div><b>Tài liệu khớp:</b> ${r.rag_hits.length ? r.rag_hits.map(h => esc(h.filename)).join(', ') : '(không)'}</div>
      <details><summary class="text-xs cursor-pointer text-slate-500">chi tiết JSON</summary><pre class="text-xs bg-white p-2 rounded mt-1">${j(r)}</pre></details></div>`;
  } catch (err) { el('sandboxOut').innerHTML = `<div class="text-sm text-red-600">Lỗi: ${esc(err.message)}</div>`; }
};

el('btnTryRouter').onclick = async () => {
  const text = prompt('Phản ánh giả để thử định tuyến:', 'Rác tồn đọng ở hành lang tầng 12 hai ngày nay chưa ai dọn');
  if (!text) return;
  el('sandboxOut').innerHTML = '<div class="text-sm text-slate-500">Đang hỏi Điều phối…</div>';
  try {
    const r = await api('/api/sandbox/router', { method: 'POST', headers: { 'Content-Type':'application/json' },
      body: JSON.stringify({ ticket_text: text, include_draft_id: state.agent?.id || null }) });
    const d = r.quyet_dinh;
    el('sandboxOut').innerHTML = `<div class="border rounded p-3 bg-slate-50 text-sm space-y-1">
      <h3 class="font-semibold">Điều phối sẽ chọn</h3>
      <div class="text-lg">${d.hanh_dong === 'ket_thuc' ? '(kết thúc, không gọi ai)' : `<b class="font-mono">${esc(d.agent_id)}</b>`}</div>
      <div class="text-xs text-slate-600">Lý do: ${esc(d.ly_do)}</div>
      <div class="text-xs text-slate-600">Chỉ dẫn: ${esc(d.chi_dan)}</div>
      <div class="text-xs text-slate-400 mt-2">Đã xét: ${r.thanh_vien_xet_den.map(m => esc(m.id)).join(', ')}</div></div>`;
  } catch (err) { el('sandboxOut').innerHTML = `<div class="text-sm text-red-600">Lỗi: ${esc(err.message)}</div>`; }
};

el('btnEvaluate').onclick = async () => {
  if (!state.agent) return alert('Lưu agent trước đã.');
  const btn = el('btnEvaluate');
  btn.disabled = true;
  el('sandboxOut').innerHTML = '<div class="text-sm text-slate-500">Đang chuẩn bị case đánh giá…</div>';
  try {
    let cases = await api(`/api/eval/agents/${state.agent.id}/cases`);
    let approved = cases.filter(c => c.approved);
    if (!approved.length) {
      el('sandboxOut').innerHTML = '<div class="text-sm text-slate-500">Chưa có case nào — đang nhờ Evaluator sinh case mới…</div>';
      const gen = await api(`/api/eval/agents/${state.agent.id}/generate`, {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ auto_approve: true }),
      });
      approved = (gen.cases || []).filter(c => c.approved);
      if (!approved.length) throw new Error('Evaluator không sinh được case nào để chạy.');
    }
    el('sandboxOut').innerHTML = `<div class="text-sm text-slate-500">Đang chạy ${approved.length} case qua phòng họp thật ở chế độ sandbox — có thể mất vài phút…</div>`;
    const run = await api(`/api/eval/agents/${state.agent.id}/run`, {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ trigger: 'manual' }),
    });
    renderEvalRun(run);
    await renderEvalGateStatus(state.agent);
  } catch (err) {
    el('sandboxOut').innerHTML = `<div class="text-sm text-red-600">Lỗi: ${esc(err.message)}</div>`;
  } finally {
    btn.disabled = false;
  }
};

function renderEvalRun(run) {
  const results = run.results || [];
  const summary = run.summary || {};
  el('sandboxOut').innerHTML = `<div class="border rounded p-3 ${run.passed ? 'bg-green-50' : 'bg-amber-50'} text-sm space-y-2">
    <h3 class="font-semibold">${run.passed ? '✓ Đạt đánh giá' : '✗ Chưa đạt đánh giá'} — ${summary.so_dat ?? 0}/${summary.so_case ?? results.length} case đạt</h3>
    ${results.map(r => `
      <details class="border rounded p-2 bg-white">
        <summary class="cursor-pointer text-xs font-mono ${r.passed ? 'text-green-700' : 'text-red-600'}">${r.passed ? '✓' : '✗'} ${esc(r.case_id)}</summary>
        <div class="text-xs mt-1 space-y-1">
          ${r.checks && r.checks.final_reply ? `<div class="text-slate-700">${esc(r.checks.final_reply)}</div>` : ''}
          ${r.checks && r.checks.detail ? `<pre class="bg-slate-50 p-1 rounded overflow-x-auto">${j(r.checks.detail)}</pre>` : ''}
          ${r.judge && r.judge.diem && Object.keys(r.judge.diem).length ? `<div>Judge: ${Object.entries(r.judge.diem).map(([k, v]) => `${esc(k)}=${v}`).join(', ')}</div>` : ''}
        </div>
      </details>`).join('')}
    ${!run.passed ? '<div class="text-xs text-amber-700 pt-1 border-t">Sửa agent (nhiệm vụ / quy tắc / tool) rồi bấm Đánh giá lại. Bấm Bật khi chưa đạt sẽ bị chặn trừ khi ép buộc.</div>' : ''}
  </div>`;
}

function renderDocs(docs) {
  el('docList').innerHTML = docs.length ? docs.map(d => `
    <div class="flex items-center justify-between border rounded px-2 py-1">
      <span>${esc(d.filename)} <span class="text-xs text-slate-500">· ${d.num_chunks} đoạn</span></span>
      <button class="text-xs text-red-600 hover:underline" onclick="delDoc('${esc(d.id)}')">Xóa</button>
    </div>`).join('') : '<div class="text-xs text-slate-400">Chưa có tài liệu.</div>';
}
window.delDoc = async (docId) => {
  await api(`/api/agents/${state.agent.id}/knowledge/${docId}`, { method: 'DELETE' });
  await editAgent(state.agent.id);
};
el('docUpload').onclick = async () => {
  if (!state.agent) return alert('Lưu agent trước đã.');
  const file = el('docFile').files[0];
  if (!file) return alert('Chọn tệp .txt .md .pdf hoặc .docx');
  el('docStatus').textContent = 'Đang nạp và tính embedding…';
  const fd = new FormData(); fd.append('file', file);
  try {
    const r = await api(`/api/agents/${state.agent.id}/knowledge`, { method: 'POST', body: fd });
    el('docStatus').textContent = `Đã nạp ${r.num_chunks} đoạn.`;
    el('docFile').value = '';
    await editAgent(state.agent.id);
  } catch (err) { el('docStatus').textContent = 'Lỗi: ' + err.message; }
};

/* ============================== TAB 4 : TOOL CATALOG ============================== */
async function loadTools() {
  state.tools = await api('/api/tools');
  el('toolTable').innerHTML = state.tools.map(t => `
    <tr class="border-b ${t.available ? '' : 'bg-red-50'}">
      <td class="py-2 font-mono text-xs">${esc(t.name)}</td>
      <td class="text-xs">${esc(t.provider)}</td>
      <td class="text-xs">${esc(t.scope)}</td>
      <td class="text-xs">${t.requires_approval ? '🔒 có' : '—'}</td>
      <td class="text-xs font-mono text-slate-500">${esc((t.context_params || []).join(', ') || '—')}</td>
      <td class="text-xs">${t.available ? '<span class="text-green-700">khả dụng</span>' : `<span class="text-red-600" title="${esc(t.unavailable_reason)}">không khả dụng</span>`}</td>
    </tr>`).join('');
}
el('refreshTools').onclick = async () => { await api('/api/tools/refresh', { method: 'POST' }); loadTools(); toast('Đã kết nối lại MCP.'); };

/* ============================== TAB 5 : CHỜ DUYỆT ============================== */
async function loadApprovals() {
  const status = el('apprFilter').value;
  const rows = await api('/api/actions' + (status ? '?status=' + status : '?status='));
  const pending = status === 'cho_duyet' ? rows.length : (await api('/api/actions?status=cho_duyet')).length;
  el('apprCount').textContent = pending;
  el('apprCount').classList.toggle('hidden', !pending);
  el('apprList').innerHTML = rows.length ? rows.map(a => `
    <div class="border rounded p-3">
      <div class="flex justify-between items-start gap-2 flex-wrap">
        <div>
          <span class="font-mono text-sm">${esc(a.tool)}</span>
          <span class="text-xs text-slate-500">· ${esc(a.agent_id)} · phản ánh ${esc(a.ticket_id)}</span>
        </div>
        <span class="text-xs px-2 py-0.5 rounded ${a.status === 'cho_duyet' ? 'bg-amber-100 text-amber-700' : a.status === 'da_thuc_hien' ? 'bg-green-100 text-green-700' : 'bg-slate-200'}">${esc(a.status)}</span>
      </div>
      ${a.phong_hop_dang_cho ? '<div class="mt-2 text-xs text-amber-800 bg-amber-50 border border-amber-300 rounded px-2 py-1">⏸ Phòng họp đang dừng chờ quyết định này — duyệt hoặc từ chối để phiên họp chạy tiếp.</div>' : ''}
      <pre class="text-xs bg-slate-50 p-2 rounded mt-2">${j(a.args)}</pre>
      ${a.result && Object.keys(a.result).length ? `<details class="mt-1"><summary class="text-xs cursor-pointer text-slate-500">kết quả</summary><pre class="text-xs bg-slate-50 p-2 rounded mt-1">${j(a.result)}</pre></details>` : ''}
      ${a.status === 'cho_duyet' ? `<div class="mt-2 flex gap-2">
        <button class="bg-green-600 text-white text-sm px-3 py-1 rounded" onclick="decide('${a.id}','approve')">${a.phong_hop_dang_cho ? 'Duyệt và họp tiếp' : 'Duyệt'}</button>
        <button class="border border-red-300 text-red-600 text-sm px-3 py-1 rounded" onclick="decide('${a.id}','reject')">Từ chối</button>
      </div>` : ''}
    </div>`).join('') : '<div class="text-sm text-slate-400">Không có hành động nào.</div>';
}
el('apprFilter').onchange = loadApprovals;
window.decide = async (id, what) => {
  // Trong lúc chờ: nút bị nhân đôi (băng-rôn phòng họp + tab chờ duyệt), nên khóa
  // theo action_id để không gửi hai lệnh duyệt cho cùng một hành động.
  if (state.deciding.has(id)) return;
  state.deciding.add(id);
  document.querySelectorAll(`[onclick*="${id}"]`).forEach(b => { b.disabled = true; b.classList.add('opacity-50'); });
  try {
    const res = await api(`/api/actions/${id}/${what}`, { method: 'POST' });
    if (what === 'approve') {
      toast(res.phong_hop_tiep_tuc ? 'Đã duyệt — phòng họp đang họp tiếp với kết quả thật.' : 'Đã duyệt và thực thi.');
    } else {
      toast(res.phong_hop_tiep_tuc ? 'Đã từ chối — phòng họp họp tiếp, agent phải tìm phương án khác.' : 'Đã từ chối.');
    }
    loadApprovals();
  } catch (err) {
    alert('Lỗi: ' + err.message);
    loadApprovals();
  } finally {
    state.deciding.delete(id);
  }
};

/** Badge "chờ duyệt" phải tự đổi khi một phòng họp nền dừng lại chờ người duyệt,
 *  kể cả khi đang mở tab khác — không có stream chung nên hỏi ngắn theo chu kỳ. */
async function pollApprovalBadge() {
  try {
    const rows = await api('/api/actions?status=cho_duyet');
    el('apprCount').textContent = rows.length;
    el('apprCount').classList.toggle('hidden', !rows.length);
    const blocking = rows.some(r => r.phong_hop_dang_cho);
    el('apprCount').classList.toggle('animate-pulse', blocking);
    if (blocking && !el('tab-approvals').classList.contains('hidden')) loadApprovals();
  } catch { /* backend chưa lên: lần sau thử lại */ }
}
setInterval(pollApprovalBadge, 5000);

/* ------------------------------------------------------------------ misc */
function toast(msg) {
  const d = document.createElement('div');
  d.className = 'fixed bottom-4 right-4 bg-slate-900 text-white text-sm px-4 py-2 rounded shadow-lg z-50';
  d.textContent = msg;
  document.body.appendChild(d);
  setTimeout(() => d.remove(), 2200);
}

(async function init() {
  try {
    const [residents, domain, health] = await Promise.all([
      api('/api/residents'), api('/api/domain'), api('/api/health'),
    ]);
    state.residents = residents; state.domain = domain;
    el('domainName').textContent = domain.display_name;
    el('engineBadge').textContent = `${health.chat_model} · phòng họp: ${health.room_engine}`;
    el('residentSel').innerHTML = residents.map(r =>
      `<option value="${esc(r.ma_cu_dan)}">${esc(r.ho_ten)} — ${esc(r.ma_can_ho)}</option>`).join('');
    el('residentSel').onchange();
    showTab(location.hash.slice(1) || 'resident');
    loadApprovals();
  } catch (err) {
    document.body.insertAdjacentHTML('afterbegin',
      `<div class="bg-red-600 text-white p-3 text-sm">Không kết nối được backend: ${esc(err.message)}</div>`);
  }
})();
