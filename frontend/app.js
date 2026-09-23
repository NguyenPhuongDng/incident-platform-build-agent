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
const show = (node, on) => { if (node) node.hidden = !on; };
/** Số tiền/số lượng hiện cho người báo đọc: 450000 -> "450.000". */
const fmt = (v) => (typeof v === 'number' && Number.isFinite(v)) ? v.toLocaleString('vi-VN') : String(v ?? '');
/** Bỏ dấu để tìm kiếm tiếng Việt không phụ thuộc dấu (cùng cách Resident-Local làm). */
const fold = (s) => String(s ?? '').normalize('NFD').replace(/[̀-ͯ]/g, '').toLowerCase();


/* Icon nội tuyến (kiểu Lucide, stroke theo currentColor). Bản gốc Resident-Local dùng
 * Lucide; ở đây nhúng thẳng vài icon cần dùng để khỏi kéo thêm thư viện — và để không
 * phụ thuộc emoji, vì font Inter cục bộ không có glyph ⏸/⌕ nên chúng rơi về ô vuông. */
const SVG = (d) => '<svg viewBox="0 0 24 24" width="17" height="17" fill="none" stroke="currentColor"'
  + ' stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">' + d + '</svg>';
const ICON = {
  pause: SVG('<rect x="7" y="5" width="3.4" height="14" rx="1"/><rect x="13.6" y="5" width="3.4" height="14" rx="1"/>'),
  alert: SVG('<path d="M10.3 3.9 1.9 18a2 2 0 0 0 1.7 3h16.8a2 2 0 0 0 1.7-3L13.7 3.9a2 2 0 0 0-3.4 0Z"/><path d="M12 9v4"/><path d="M12 17h.01"/>'),
  next: SVG('<path d="M5 12h14"/><path d="m12 5 7 7-7 7"/>'),
  done: SVG('<path d="M20 6 9 17l-5-5"/>'),
};

const state = {
  residents: [], sessionId: null, resident: null,
  tickets: [], ticketId: null, ticket: null, source: null, members: [], speaking: null,
  events: [], tasks: [], waiting: null, turns: null, maxTurns: null, usedTurns: 0,
  agents: [], agent: null, tools: [], domain: null,
  chatSource: null, chatTicket: null, deciding: new Set(),
  roomTab: 'chat', ticketFilter: 'all', ticketQuery: '',
  toolFilter: 'all', toolQuery: '',
  // Mỗi hàng đợi duyệt giữ bộ lọc riêng; vai trò do domain pack khai, không cứng ở đây.
  roles: [], apprFilter: { bql: 'cho_duyet', don_vi: 'cho_duyet' },
  chain: [], residentActions: [],
};
/** Hàng đợi hiện trong bảng điều khiển (BQL, đơn vị thực hiện…). */
const consoleRoles = () => state.roles.filter(r => r.hien_o !== 'resident').map(r => r.id);
const roleLabel = (id) => state.roles.find(r => r.id === id)?.label || id;

const STATUS = { dang_tiep_nhan:'Đang tiếp nhận', dang_xu_ly:'Đang xử lý', cho_duyet:'Chờ duyệt',
                 cho_cu_dan:'Chờ người báo', hoan_tat:'Hoàn tất' };
const STATUS_CLS = { cho_duyet:'badge-warn', cho_cu_dan:'badge-warn', hoan_tat:'badge-ok',
                     dang_xu_ly:'badge-info', dang_tiep_nhan:'badge' };
const PRIO_CLS = { KHAN_CAP:'badge-err', BINH_THUONG:'badge-info', THAP:'badge' };
const ACT_CLS = { cho_duyet:'badge-warn', da_duyet:'badge-info', da_thuc_hien:'badge-ok',
                  tu_choi:'badge', loi:'badge-err' };

/* ------------------------------------------------------------------ shell */
const TAB_TITLE = { resident:'Cư dân', room:'Phòng họp', builder:'Agent Studio',
                    tools:'Tool catalog', bql:'BQL duyệt', don_vi:'Đơn vị duyệt' };

window.toggleSide = (on) => {
  el('side').classList.toggle('is-open', on);
  show(el('backdrop'), on);
};
window.toggleQueue = () => el('queue').classList.toggle('is-open');

window.showTab = function showTab(name) {
  if (!TAB_TITLE[name]) name = 'resident';
  document.querySelectorAll('.nav-item').forEach(b => b.classList.toggle('is-active', b.dataset.tab === name));
  Object.keys(TAB_TITLE).forEach(t => show(el('tab-' + t), t === name));
  el('topbarTitle').textContent = TAB_TITLE[name];
  if (location.hash.slice(1) !== name) history.replaceState(null, '', '#' + name);
  toggleSide(false);
  ({ room: loadTickets, builder: loadAgents, tools: loadTools,
     bql: () => loadApprovals('bql'), don_vi: () => loadApprovals('don_vi') }[name] || (() => {}))();
};
document.querySelectorAll('.nav-item').forEach(btn => { btn.onclick = () => showTab(btn.dataset.tab); });
window.addEventListener('hashchange', () => showTab(location.hash.slice(1) || 'resident'));

/** Segments dùng chung: đổi nút active rồi gọi lại hàm render. */
function bindSegments(id, onPick) {
  el(id).querySelectorAll('button').forEach(b => {
    b.onclick = () => {
      el(id).querySelectorAll('button').forEach(x => x.classList.toggle('is-active', x === b));
      onPick(b.dataset.f);
    };
  });
}

/* ============================== TAB 1 : CƯ DÂN ============================== */
const newSession = () => {
  state.sessionId = 'ss-' + Math.random().toString(36).slice(2, 10);
  el('chatLog').innerHTML = '';
  show(el('ticketBanner'), false);
  if (state.chatSource) { state.chatSource.close(); state.chatSource = null; }
  state.chatTicket = null;
  bubble('receptionist', `Xin chào, ${state.domain?.self_reference || 'chúng tôi'} có thể giúp gì cho ${state.domain?.honorific || 'bạn'}?`);
};

/** Hiện trạng thái "đang xử lý nội bộ" trong khung chat. */
function setWorking(on, text) {
  let node = el('chatWorking');
  if (!on) { node?.remove(); el('chatStatus').textContent = 'Trực tuyến · trả lời trong ít phút'; return; }
  if (!node) {
    node = document.createElement('div');
    node.id = 'chatWorking';
    el('chatLog').appendChild(node);
  }
  node.className = 'r-hint' + (text ? ' is-wait' : '');
  node.textContent = text || 'Các bộ phận đang trao đổi nội bộ…';
  el('chatStatus').textContent = text ? 'Đang chờ quản lý duyệt' : 'Các bộ phận đang họp nội bộ';
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
  src.addEventListener('room_waiting', (e) => {
    if (state.chatTicket !== ticketId) return;
    const p = JSON.parse(e.data).payload || {};
    const mine = (state.roles.find(r => r.id === p.vai_tro_duyet) || {}).hien_o === 'resident';
    setWorking(true, mine ? 'Ban quản lý đang chờ anh/chị xác nhận ở dưới…'
                          : 'Một hành động đang chờ ban quản lý xem xét…');
    loadResidentApprovals();
  });
  src.addEventListener('room_resumed', () => {
    if (state.chatTicket === ticketId) { setWorking(true); loadResidentApprovals(); }
  });
  src.addEventListener('room_end', () => {
    if (state.chatTicket === ticketId) { setWorking(false); loadResidentApprovals(); }
  });
  src.onerror = () => setWorking(false);
}

function bubble(role, text) {
  const mine = role === 'user';
  const div = document.createElement('div');
  div.className = 'r-row' + (mine ? ' is-me' : '');
  div.innerHTML = `<div class="r-bubble">${esc(text)}</div>`;
  el('chatLog').appendChild(div);
  el('chatLog').scrollTop = el('chatLog').scrollHeight;
}

el('newSession').onclick = newSession;
el('residentSel').onchange = () => {
  state.resident = state.residents.find(r => r.ma_cu_dan === el('residentSel').value);
  el('residentInfo').innerHTML = state.resident ? `
    <div><span>Căn hộ</span><b>${esc(state.resident.ma_can_ho)}</b></div>
    <div><span>Điện thoại</span><span>${esc(state.resident.dien_thoai)}</span></div>
    <div><span>Vai trò</span><span class="r-chip">${esc(state.resident.vai_tro)}</span></div>
    <div><span>Mã cư dân</span><span class="mono">${esc(state.resident.ma_cu_dan)}</span></div>` : '';
  newSession();
};

el('chatQuick').querySelectorAll('button').forEach(b => {
  b.onclick = () => { el('chatInput').value = b.textContent.trim(); el('chatInput').focus(); };
});
// Enter gửi, Shift+Enter xuống dòng; bỏ qua khi IME tiếng Việt đang gõ dở.
el('chatInput').addEventListener('keydown', (e) => {
  if (e.key === 'Enter' && !e.shiftKey && !e.isComposing) {
    e.preventDefault();
    el('chatForm').requestSubmit();
  }
});

el('chatForm').onsubmit = async (e) => {
  e.preventDefault();
  const msg = el('chatInput').value.trim();
  if (!msg || !state.resident) return;
  el('chatInput').value = '';
  bubble('user', msg);
  el('chatSend').disabled = true;
  const wait = document.createElement('div');
  wait.className = 'r-hint'; wait.textContent = 'Lễ tân đang soạn…';
  el('chatLog').appendChild(wait);
  el('chatLog').scrollTop = el('chatLog').scrollHeight;
  try {
    const r = await api('/api/chat', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ session_id: state.sessionId, resident_id: state.resident.ma_cu_dan, message: msg }),
    });
    wait.remove();
    bubble('receptionist', r.tra_loi);
    if (r.nguon?.length) {
      const src = document.createElement('div');
      src.className = 'r-hint';
      src.textContent = 'Nguồn: ' + r.nguon.join(', ');
      el('chatLog').appendChild(src);
    }
    if (r.ticket) {
      followTicket(r.ticket.id);
      const banner = el('ticketBanner');
      show(banner, true);
      banner.innerHTML =
        `Đã tạo phản ánh <b>${esc(r.ticket.id)}</b><br>ưu tiên <b>${esc(r.ticket.priority)}</b>
         <button type="button" onclick="window.openRoom('${esc(r.ticket.id)}')">Xem phòng họp trực tiếp →</button>`;
    }
  } catch (err) { wait.remove(); bubble('receptionist', 'Lỗi: ' + err.message); }
  el('chatSend').disabled = false;
  el('chatInput').focus();
};

/** Phần việc của NGƯỜI BÁO trong quy trình hiện ngay trong khung chat của họ:
 *  chốt phương án trước khi làm, nghiệm thu sau khi làm xong. Vai trò nào hiện ở đây
 *  là do domain pack khai (`hien_o: resident`), không cứng trong giao diện. */
async function loadResidentApprovals() {
  const holder = el('residentApprovals');
  if (!holder) return;
  const roles = state.roles.filter(r => r.hien_o === 'resident').map(r => r.id);
  if (!roles.length || !state.chatTicket) { holder.innerHTML = ''; return; }
  const rows = [];
  for (const r of roles) {
    const got = await api(`/api/actions?status=cho_duyet&role=${encodeURIComponent(r)}&ticket_id=${encodeURIComponent(state.chatTicket)}`);
    rows.push(...got);
  }
  state.residentActions = rows;
  holder.innerHTML = rows.map(a => {
    const step = state.domain?.quy_trinh_xac_nhan?.find(x => x.tools.includes(a.tool));
    const fields = Object.entries(a.args || {})
      .filter(([k]) => !['ticket_id', 'ma_can_ho', 'ma_cu_dan', 'sandbox'].includes(k))
      .map(([k, v]) => `<div><dt>${esc(k.replace(/_/g, ' '))}</dt><dd>${esc(fmt(v))}</dd></div>`).join('');
    return `<div class="r-approve">
      <strong>${esc(step?.label || 'Ban quản lý cần anh/chị xác nhận')}</strong>
      <div>Ban quản lý đang chờ anh/chị đồng ý thì mới làm bước tiếp theo.</div>
      <dl>${fields}</dl>
      <div class="actions">
        <button class="btn btn-ok btn-compact" onclick="decide('${a.id}','approve')">Đồng ý</button>
        <button class="btn btn-danger btn-compact" onclick="decide('${a.id}','reject')">Chưa đồng ý</button>
      </div>
    </div>`;
  }).join('');
  if (rows.length) el('chatLog').scrollTop = el('chatLog').scrollHeight;
}

window.openRoom = (id) => {
  showTab('room');
  setTimeout(() => selectTicket(id), 100);
};

/* ============================== TAB 2 : PHÒNG HỌP ============================== */
async function loadTickets() {
  state.tickets = await api('/api/tickets');
  renderQueue();
}
el('refreshTickets').onclick = loadTickets;
bindSegments('ticketFilter', (f) => { state.ticketFilter = f; renderQueue(); });
el('ticketSearch').oninput = (e) => { state.ticketQuery = e.target.value; renderQueue(); };

const EMPTY_FEED = '<div class="empty" data-empty="1">Phản ánh này chưa có diễn biến phòng họp nào.</div>';
const EMPTY_LOG = '<div class="empty" data-empty="1">Chưa có sự kiện nào được ghi.</div>';
const clearEmpty = (node) => { const f = node.firstElementChild; if (f && f.dataset.empty) node.innerHTML = ''; };

function renderQueue() {
  const q = fold(state.ticketQuery);
  const rows = state.tickets.filter(t => {
    if (state.ticketFilter !== 'all' && t.status !== state.ticketFilter) return false;
    if (!q) return true;
    return fold(`${t.id} ${t.summary} ${t.fields?.mo_ta || ''} ${t.apartment_id}`).includes(q);
  });
  el('ticketCount').textContent = `${rows.length}/${state.tickets.length}`;
  el('queueSummary').textContent = state.ticketId || 'chưa chọn';
  el('ticketList').innerHTML = rows.length ? rows.map(t => `
    <button class="q-item ${t.id === state.ticketId ? 'is-selected' : ''}" onclick="selectTicket('${t.id}')">
      <span class="q-meta">
        <span class="mono">${esc(t.id)}</span>
        <span class="badge ${PRIO_CLS[t.priority] || ''}">${esc(t.priority)}</span>
      </span>
      <strong>${esc(t.summary || t.fields?.mo_ta || '(chưa có tóm tắt)')}</strong>
      <p>${esc(t.fields?.loi_nguoi_bao || t.fields?.mo_ta || '')}</p>
      <small>${esc(STATUS[t.status] || t.status)} · ${esc(t.apartment_id || '')}</small>
    </button>`).join('') : '<div class="empty">Không có phản ánh nào khớp.</div>';
}

window.selectTicket = async (id) => {
  state.ticketId = id;
  state.events = []; state.members = []; state.speaking = null; state.waiting = null; state.tasks = []; state.turns = null; state.usedTurns = 0; state.maxTurns = null;
  renderQueue();
  el('queue').classList.remove('is-open');
  // Chỗ trống có lời giải thích, thay vì một vùng trắng không biết là đang tải hay rỗng.
  el('chatFeed').innerHTML = EMPTY_FEED;
  el('timeline').innerHTML = EMPTY_LOG;
  el('replyBox').innerHTML = '';
  el('taskList').innerHTML = '';
  show(el('roomEmpty'), false);
  show(el('roomHeader'), true);
  show(el('roomTabs'), true);
  const t = await api('/api/tickets/' + id);
  state.ticket = t;
  renderRoomHeader();
  loadTicketTasks();
  loadChain();
  if (state.source) state.source.close();
  state.source = new EventSource(`/api/tickets/${id}/events`);
  ['room_start','router_decision','agent_start','rag_hits','tool_call','tool_result','action_pending',
   'agent_output','guard_triggered','room_end','receptionist_reply','action_executed',
   'room_waiting','room_resumed']
    .forEach(type => state.source.addEventListener(type, (e) => onEvent(JSON.parse(e.data))));
  showRoomTab(state.roomTab);
};

function renderRoomHeader() {
  const t = state.ticket;
  if (!t) return;
  const f = t.fields || {};
  el('roomHeader').innerHTML = `
    <div class="room-tags">
      <span class="badge ${PRIO_CLS[t.priority] || ''}">${esc(t.priority)}</span>
      <span class="badge ${STATUS_CLS[t.status] || 'badge'}">${esc(STATUS[t.status] || t.status)}</span>
      <span class="badge mono">${esc(t.id)}</span>
      ${t.is_eval ? '<span class="badge badge-warn">vé đánh giá</span>' : ''}
    </div>
    <h2>${esc(t.summary || f.mo_ta || 'Phản ánh chưa có tóm tắt')}</h2>
    <div class="room-sub">Mở lúc ${esc((t.created_at || '').replace('T', ' ').slice(0, 19))}</div>
    <div class="facts">
      <span>Căn hộ <b>${esc(t.apartment_id || '—')}</b></span>
      <span>Người báo <b>${esc(t.resident_id || '—')}</b></span>
      <span>Vị trí <b>${esc(f.vi_tri || '—')}</b></span>
      <span>Lượt đã dùng <b id="factTurns">${esc(state.turns ?? "—")}</b></span>
    </div>`;
  renderNextAction();
}

/** Một dòng duy nhất trả lời: việc tiếp theo là gì, hoặc đang chờ ai. */
async function loadChain() {
  if (!state.ticketId) return;
  try {
    const wf = await api(`/api/tickets/${state.ticketId}/workflow`);
    state.chain = wf.cac_buoc || [];
    renderChain();
  } catch { /* vé vừa bị xóa */ }
}

/** Dải quy trình xác nhận: một lượt nhìn biết đang kẹt ở bước nào, chờ bên nào. */
function renderChain() {
  const box = el('roomChain');
  if (!box) return;
  const steps = state.chain || [];
  if (!steps.length) { box.hidden = true; return; }
  const CLS = { xong:'is-done', cho_duyet:'is-waiting', tu_choi:'is-rejected', chua_toi:'' };
  box.hidden = false;
  box.innerHTML = steps.map((st, i) => `
    <span class="chain-step ${CLS[st.trang_thai] || ''}" title="${esc(st.vai_tro_label)} · ${esc(st.tools.join(', '))}">
      <span class="chain-no">${st.trang_thai === 'xong' ? '✓' : i + 1}</span>
      <b>${esc(st.label)}</b>
      <span class="chain-role">${esc(st.vai_tro_label)}</span>
    </span>`).join('');
}

function setFact() { const n = el("factTurns"); if (n) n.textContent = state.turns ?? "—"; }

function renderNextAction() {
  const box = el('roomPause');
  const t = state.ticket;
  if (!t) { show(box, false); return; }
  show(box, true);
  if (state.waiting) {
    const p = state.waiting;
    box.className = 'next-action needs-attention';
    box.innerHTML = `
      ${ICON.pause}
      <div>
        <strong>Phòng họp đang dừng, chờ ${esc(roleLabel(p.vai_tro_duyet || 'bql'))}</strong>
        <p>Tool <span class="mono">${esc(p.tool)}</span> · hành động <b>${esc(p.action_id)}</b>${
          p.han_cho_giay ? ` · tự chạy tiếp sau ${Math.round(p.han_cho_giay)}s nếu không có quyết định` : ''}</p>
        <pre class="code-block">${j(p.args || {})}</pre>
      </div>
      <div class="actions">
        <button class="btn btn-ok btn-compact" onclick="decide('${p.action_id}','approve')">Duyệt và họp tiếp</button>
        <button class="btn btn-danger btn-compact" onclick="decide('${p.action_id}','reject')">Từ chối</button>
      </div>`;
    return;
  }
  const pending = state.tasks.filter(a => a.status === 'cho_duyet');
  if (pending.length) {
    box.className = 'next-action needs-attention';
    box.innerHTML = `${ICON.alert}<div><strong>Tiếp theo: quản lý quyết định ${pending.length} hành động</strong>
      <p>Phiên họp đã kết thúc, các hành động này vẫn chờ duyệt — mở tab Công việc để xử lý.</p></div>
      <button class="btn btn-compact" onclick="showRoomTab('tasks')">Mở Công việc</button>`;
    return;
  }
  const note = {
    dang_xu_ly: ['Đang họp nội bộ', 'Các bộ phận đang trao đổi; tin cho người báo sẽ do Lễ tân gửi cuối phiên.'],
    dang_tiep_nhan: ['Đang tiếp nhận', 'Lễ tân còn đang thu thập thông tin từ người báo.'],
    cho_cu_dan: ['Tiếp theo: chờ người báo trả lời', 'Phòng họp đã ghim câu hỏi và chờ thông tin bổ sung.'],
    cho_duyet: ['Tiếp theo: chờ quản lý duyệt', 'Có hành động cần quyết định trước khi tiếp tục.'],
    hoan_tat: ['Đã hoàn tất', 'Không còn việc nào đang chờ trong phòng này.'],
  }[state.ticket.status] || ['Trạng thái', state.ticket.status];
  box.className = 'next-action' + (state.ticket.status === 'dang_xu_ly' ? ' is-live' : '');
  box.innerHTML = `${state.ticket.status === 'hoan_tat' ? ICON.done : ICON.next}<div><strong>${esc(note[0])}</strong><p>${esc(note[1])}</p></div>`;
}

window.showRoomTab = function showRoomTab(name) {
  state.roomTab = name;
  el('roomTabs').querySelectorAll('button').forEach(b =>
    b.setAttribute('aria-selected', String(b.dataset.rt === name)));
  ['chat','tasks','log','members'].forEach(t => show(el('panel-' + t), t === name));
};
el('roomTabs').querySelectorAll('button').forEach(b => { b.onclick = () => showRoomTab(b.dataset.rt); });
el('logFilter').onchange = renderLog;

async function loadTicketTasks() {
  if (!state.ticketId) return;
  const all = await api('/api/actions?status=');
  state.tasks = all.filter(a => a.ticket_id === state.ticketId);
  renderTasks();
  renderNextAction();
}

function renderTasks() {
  el('cntTasks').textContent = state.tasks.length;
  if (!state.tasks.length) {
    el('taskList').innerHTML = '<div class="empty">Phòng này chưa sinh hành động nào cần duyệt.</div>';
    return;
  }
  el('taskList').innerHTML = state.tasks.map(a => `
    <div class="task-row">
      <span class="avatar" aria-hidden="true">${esc((a.agent_id || '?').slice(0, 2).toUpperCase())}</span>
      <div>
        <strong class="mono">${esc(a.tool)}</strong>
        <p>Do <b>${esc(a.agent_id)}</b> yêu cầu · chờ <b>${esc(roleLabel(a.vai_tro_duyet))}</b>
          · ${esc((a.created_at || '').replace('T', ' ').slice(0, 19))}</p>
        ${a.phong_hop_dang_cho ? `<p class="badge badge-warn">${ICON.pause} phòng họp đang dừng chờ quyết định này</p>` : ''}
        <details class="det"><summary>tham số</summary><pre class="code-block">${j(a.args)}</pre></details>
        ${a.result && Object.keys(a.result).length
          ? `<details class="det"><summary>kết quả</summary><pre class="code-block">${j(a.result)}</pre></details>` : ''}
        ${a.status === 'cho_duyet' ? `<div class="actions" style="margin-top:9px">
            <button class="btn btn-ok btn-compact" onclick="decide('${a.id}','approve')">${
              a.phong_hop_dang_cho ? 'Duyệt và họp tiếp' : 'Duyệt'}</button>
            <button class="btn btn-danger btn-compact" onclick="decide('${a.id}','reject')">Từ chối</button>
          </div>` : ''}
      </div>
      <span class="badge ${ACT_CLS[a.status] || 'badge'}">${esc(a.status)}</span>
    </div>`).join('');
}

function renderMembers() {
  el('cntMembers').textContent = state.members.length;
  el('memberList').innerHTML = state.members.length ? state.members.map(m => `
    <div class="member-row ${m.id === state.speaking ? 'is-speaking' : ''}">
      <span class="avatar" aria-hidden="true">${esc(m.display_name.slice(0, 2).toUpperCase())}</span>
      <div>
        <strong>${esc(m.display_name)}</strong>
        <small class="mono">${esc(m.id)}</small>
      </div>
      ${m.id === state.speaking ? '<span class="badge badge-info">đang phát biểu</span>' : ''}
    </div>`).join('') : '<div class="empty">Phòng chưa mở hoặc chưa có thành viên nào.</div>';
}

/* --------------------------------------------- sự kiện phòng họp: chat + nhật ký */
const EV = {
  room_start:        { t:'Mở phòng họp',        c:'#8a97aa' },
  router_decision:   { t:'Điều phối',           c:'#5b3fa6' },
  agent_start:       { t:'Agent phát biểu',     c:'#284e93' },
  rag_hits:          { t:'Tra tài liệu',        c:'#0f7f74' },
  tool_call:         { t:'Gọi tool',            c:'#b4600f' },
  tool_result:       { t:'Kết quả tool',        c:'#d09a4f' },
  action_pending:    { t:'Chờ duyệt',           c:'#ab2334' },
  room_waiting:      { t:'Tạm dừng chờ duyệt',  c:'#c07a16' },
  room_resumed:      { t:'Họp tiếp',            c:'#c9a35a' },
  agent_output:      { t:'Kết luận agent',      c:'#267148' },
  guard_triggered:   { t:'Guard',               c:'#b48a12' },
  room_end:          { t:'Kết thúc',            c:'#596679' },
  receptionist_reply:{ t:'Lễ tân trả lời',      c:'#3f51b5' },
  action_executed:   { t:'Đã thực hiện',        c:'#267148' },
};
const LOG_GROUP = {
  agent: ['agent_start', 'agent_output', 'rag_hits'],
  tool: ['tool_call', 'tool_result', 'action_pending', 'action_executed', 'room_waiting', 'room_resumed'],
};
const clock = (iso) => String(iso || '').slice(11, 19);

function onEvent(ev) {
  state.events.push(ev);
  const p = ev.payload || {};

  if (ev.type === 'room_start') {
    state.members = p.thanh_vien || []; state.speaking = null; renderMembers();
    state.maxTurns = p.max_turns ?? null; state.usedTurns = 0; state.turns = `0/${state.maxTurns ?? '—'}`; setFact();
  } else if (ev.type === 'agent_start') {
    state.speaking = p.agent_id; renderMembers();
    state.usedTurns += 1; state.turns = `${state.usedTurns}/${state.maxTurns ?? state.domain?.max_room_turns ?? '—'}`; setFact();
  } else if (ev.type === 'agent_output') {
    state.speaking = null; renderMembers();
  } else if (ev.type === 'room_waiting') {
    state.waiting = p; renderNextAction(); loadTicketTasks(); loadApprovals(); loadChain();
  } else if (ev.type === 'room_resumed') {
    state.waiting = null; loadTicketTasks(); loadApprovals(); loadTickets();
    refreshTicketStatus();
  } else if (ev.type === 'action_pending') {
    loadTicketTasks(); loadApprovals();
  } else if (ev.type === 'action_executed') {
    loadTicketTasks(); loadApprovals(); loadChain();
  } else if (ev.type === 'room_end') {
    state.speaking = null; state.waiting = null; renderMembers();
    state.turns = `${p.so_luot ?? state.usedTurns}/${state.maxTurns ?? state.domain?.max_room_turns ?? '—'}`; setFact();
    loadTickets(); loadApprovals(); loadTicketTasks(); refreshTicketStatus();
  } else if (ev.type === 'receptionist_reply') {
    el('replyBox').innerHTML += `<div class="surface pad" style="padding:13px 15px;margin-bottom:10px;white-space:pre-wrap">${esc(p.tin_nhan)}</div>`;
  }

  pushChat(ev);
  pushLog(ev);
  el('cntLog').textContent = state.events.length;
}

async function refreshTicketStatus() {
  if (!state.ticketId) return;
  try {
    const t = await api('/api/tickets/' + state.ticketId);
    state.ticket = t;
    renderRoomHeader();
  } catch { /* ticket có thể vừa bị xóa: bỏ qua */ }
}

/** Trao đổi: phát biểu của Điều phối/agent/Lễ tân là tin nhắn; còn lại là dòng sự kiện ngắn. */
function pushChat(ev) {
  const p = ev.payload || {};
  const feed = el('chatFeed');
  let html = '';

  if (ev.type === 'router_decision') {
    html = message('is-router', 'ĐP', 'Điều phối', ev, p.hanh_dong === 'ket_thuc'
      ? `<b>Kết thúc phiên.</b> ${esc(p.ly_do || '')}`
      : `Mời <b>${esc(p.agent_id)}</b> phát biểu.<br>${esc(p.chi_dan || '')}`,
      p.ly_do && p.hanh_dong !== 'ket_thuc' ? [`Lý do: ${p.ly_do}`] : []);
  } else if (ev.type === 'agent_output') {
    const o = p.output || {};
    const extra = [];
    if (o.da_thuc_hien?.length) extra.push('Đã làm: ' + o.da_thuc_hien.join('; '));
    if (o.de_xuat?.length) extra.push('Đề xuất: ' + o.de_xuat.join('; '));
    if (o.can_them_agent?.length) extra.push('Cần thêm bộ phận: ' + o.can_them_agent.join(', '));
    if (o.nguon?.length) extra.push('Nguồn: ' + o.nguon.join(', '));
    html = message('is-agent', (p.display_name || p.agent_id || '?').slice(0, 2).toUpperCase(),
      p.display_name || p.agent_id, ev, esc(o.ket_luan || ''), extra,
      `<details class="det"><summary>JSON kết luận</summary><pre class="code-block">${j(o)}</pre></details>`);
  } else if (ev.type === 'receptionist_reply') {
    html = message('is-human', 'LT', 'Lễ tân → người báo', ev, esc(p.tin_nhan || ''),
      p.loai === 'cap_nhat' ? ['tin cập nhật sau khi duyệt'] : []);
  } else if (ev.type === 'tool_call') {
    html = eventRow(ev, `Gọi tool <span class="mono">${esc(p.tool)}</span> <span class="muted">(${esc(p.provider || '')})</span>`
      + (p.tham_so_bi_ghi_de?.length
          ? `<br><span class="badge badge-err">hệ thống ghi đè: ${esc(p.tham_so_bi_ghi_de.join(', '))}</span>` : '')
      + `<details class="det"><summary>tham số thực tế</summary><pre class="code-block">${j(p.args)}</pre></details>`);
  } else if (ev.type === 'tool_result') {
    html = eventRow(ev, `Kết quả <span class="mono">${esc(p.tool)}</span>`
      + `<details class="det"><summary>xem kết quả</summary><pre class="code-block">${j(p.ket_qua)}</pre></details>`);
  } else if (ev.type === 'action_pending') {
    html = eventRow(ev, `<span class="badge badge-warn">chờ duyệt</span> <span class="mono">${esc(p.tool)}</span> → <b>${esc(p.action_id)}</b>`
      + (p.phong_hop_dang_cho ? ' · phòng họp dừng lại chờ quyết định' : ''));
  } else if (ev.type === 'room_waiting') {
    html = eventRow(ev, `<b>Phòng họp tạm dừng</b> ở <span class="mono">${esc(p.tool)}</span>, chờ duyệt <b>${esc(p.action_id)}</b>`
      + ` <span class="muted">(hạn chờ ${Math.round(p.han_cho_giay || 0)}s)</span>`);
  } else if (ev.type === 'room_resumed') {
    const QD = { da_duyet:'quản lý đã duyệt', tu_choi:'quản lý từ chối', cho_duyet:'hết hạn chờ, chạy tiếp' };
    html = eventRow(ev, `<b>Họp tiếp</b> sau ${p.cho_bao_lau_giay}s — ${esc(QD[p.quyet_dinh] || p.quyet_dinh || '')}`);
  } else if (ev.type === 'action_executed') {
    html = eventRow(ev, `<span class="badge ${p.thanh_cong ? 'badge-ok' : 'badge-err'}">${p.thanh_cong ? 'đã thực hiện' : 'lỗi'}</span>`
      + ` <span class="mono">${esc(p.tool)}</span>`
      + `<details class="det"><summary>kết quả</summary><pre class="code-block">${j(p.ket_qua)}</pre></details>`);
  } else if (ev.type === 'guard_triggered') {
    html = eventRow(ev, `<span class="badge badge-warn">guard</span> <b>${esc(p.guard)}</b> → ${esc(p.xu_ly || '')}`);
  } else if (ev.type === 'room_start') {
    html = eventRow(ev, `Mở phòng họp với <b>${p.so_thanh_vien}</b> thành viên · tối đa ${p.max_turns} lượt · engine <b>${esc(p.engine || '-')}</b>`);
  } else if (ev.type === 'room_end') {
    html = eventRow(ev, `<b>Kết thúc phiên</b> sau ${p.so_luot} lượt — ${esc(p.ly_do_ket_thuc || p.loi || '')}`
      + ` · phản ánh → <b>${esc(STATUS[p.trang_thai_ticket] || p.trang_thai_ticket || '')}</b>`);
  }
  if (!html) return;
  clearEmpty(feed);
  const wrap = document.createElement('div');
  wrap.innerHTML = html;
  const node = wrap.firstElementChild;
  feed.appendChild(node);
  el('cntChat').textContent = feed.children.length;
  const nearBottom = feed.scrollHeight - feed.scrollTop - feed.clientHeight < 160;
  if (nearBottom) feed.scrollTop = feed.scrollHeight;
}

function message(kind, initials, who, ev, bodyHtml, bullets = [], tail = '') {
  return `<div class="msg ${kind}">
    <span class="avatar" aria-hidden="true">${esc(initials)}</span>
    <div>
      <header><strong>${esc(who)}</strong><time>${esc(clock(ev.created_at))}</time>
        <span class="badge">#${ev.seq}</span></header>
      <p>${bodyHtml}</p>
      ${bullets.length ? '<ul>' + bullets.map(b => `<li>${esc(b)}</li>`).join('') + '</ul>' : ''}
      ${tail}
    </div></div>`;
}
function eventRow(ev, html) {
  const meta = EV[ev.type] || { t: ev.type };
  return `<div class="msg is-event">
    <span class="avatar" aria-hidden="true">●</span>
    <div>
      <header><strong>${esc(meta.t)}</strong><time>${esc(clock(ev.created_at))}</time>
        ${ev.actor ? `<span class="badge mono">${esc(ev.actor)}</span>` : ''}</header>
      <p>${html}</p>
    </div></div>`;
}

function pushLog(ev) {
  if (!logVisible(ev)) return;
  clearEmpty(el('timeline'));
  el('timeline').appendChild(logNode(ev));
}
function logVisible(ev) {
  const f = el('logFilter').value;
  if (!f) return true;
  if (LOG_GROUP[f]) return LOG_GROUP[f].includes(ev.type);
  return ev.type === f;
}
function logNode(ev) {
  const meta = EV[ev.type] || { t: ev.type, c: '#8a97aa' };
  const d = document.createElement('div');
  d.className = 'log-item';
  d.innerHTML = `
    <time>${esc(clock(ev.created_at))}</time>
    <div>
      <span class="log-kind" style="color:${meta.c}"><span class="log-dot" style="background:${meta.c}"></span>${esc(meta.t)}</span>
      <p>#${ev.seq}${ev.actor ? ' · ' + esc(ev.actor) : ''}</p>
      <details class="det"><summary>payload</summary><pre class="code-block">${j(ev.payload)}</pre></details>
    </div>`;
  return d;
}
function renderLog() {
  const rows = state.events.filter(logVisible);
  el('timeline').innerHTML = rows.length ? '' : EMPTY_LOG;
  rows.forEach(ev => el('timeline').appendChild(logNode(ev)));
}

/* ============================== TAB 3 : AGENT STUDIO ============================== */
const PROMPT_TEMPLATE = `## Vai trò
(Bộ phận này chịu trách nhiệm gì?)

## Nhiệm vụ
1. …
2. …

## Quy tắc riêng
- …`;

// Mẫu mô tả sẵn: chỉ điền chữ vào ô mô tả cho nhanh, KHÔNG phải cấu hình dựng sẵn —
// Builder vẫn là bên soạn bản nháp từ đúng câu chữ này.
const DESC_TEMPLATES = [
  { ten: 'Vệ sinh môi trường', mo_ta: 'Tôi cần một bộ phận lo vệ sinh: rác tồn đọng ở khu vực chung, thu gom rác sinh hoạt, dọn vệ sinh hành lang và sảnh.' },
  { ten: 'Cây xanh · cảnh quan', mo_ta: 'Tôi cần một bộ phận lo cây xanh: cắt tỉa, sâu bệnh, hệ thống tưới, cây nghiêng đổ sau mưa bão.' },
  { ten: 'Hỗ trợ cư dân', mo_ta: 'Tôi cần một bộ phận trả lời thắc mắc chung của cư dân về nội quy, thủ tục và hướng dẫn sử dụng tiện ích.' },
];

function setSteps(n) {
  show(el('builderSteps'), true);
  el('builderSteps').querySelectorAll('li').forEach(li => {
    const i = Number(li.dataset.st);
    li.classList.toggle('is-current', i === n);
    li.classList.toggle('is-done', i < n);
  });
}

async function loadAgents() {
  state.agents = await api('/api/agents');
  if (!state.tools.length) state.tools = await api('/api/tools');
  el('agentCount').textContent = `${state.agents.length} agent`;
  const badge = (a) => a.is_core ? '<span class="badge">lõi</span>'
    : a.status === 'active' ? '<span class="badge badge-ok">active</span>'
    : a.status === 'draft' ? '<span class="badge badge-warn">draft</span>'
    : '<span class="badge">tắt</span>';
  el('agentList').innerHTML = state.agents.map(a => `
    <button class="agent-row ${a.id === state.agent?.id ? 'is-selected' : ''}" onclick="editAgent('${a.id}')">
      <span class="avatar" aria-hidden="true">${esc(a.display_name.slice(0, 2).toUpperCase())}</span>
      <span class="agent-row-copy">
        <strong>${esc(a.display_name)}</strong>
        <small class="mono">${esc(a.id)} · v${a.version} · ${a.tools.length} tool</small>
      </span>
      ${badge(a)}
    </button>`).join('') || '<div class="empty">Chưa có agent nào.</div>';
}

function renderToolPicker(selected) {
  const groups = { platform: 'Platform', domain: 'Domain', business: 'Nghiệp vụ' };
  el('toolPicker').innerHTML = Object.entries(groups).map(([scope, label]) => {
    const rows = state.tools.filter(t => t.scope === scope);
    if (!rows.length) return '';
    return `<div class="eyebrow" style="margin-top:12px">${label}</div>` + rows.map(t => `
      <label class="check-row" style="${t.available ? '' : 'opacity:.55'}">
        <input type="checkbox" class="toolchk" value="${esc(t.name)}" ${selected.includes(t.name) ? 'checked' : ''} ${t.available ? '' : 'disabled'}>
        <span>
          <strong class="mono">${esc(t.name)} ${t.requires_approval ? '<span title="cần duyệt">🔒</span>' : ''}</strong>
          <small>${esc(t.manager_description)}</small>
        </span>
        ${t.available ? '' : '<span class="badge badge-err">không khả dụng</span>'}
      </label>`).join('');
  }).join('');
  document.querySelectorAll('.toolchk').forEach(c => { c.onchange = (e) => updateToolCount(e.target); });
  updateToolCount();
}
function selectedTools() { return [...document.querySelectorAll('.toolchk:checked')].map(c => c.value); }
function updateToolCount(changed) {
  let n = selectedTools().length;
  if (n > 10 && changed) {
    alert('Tối đa 10 tool cho mỗi agent.');
    changed.checked = false;
    n = selectedTools().length;
  }
  el('toolCount').textContent = n;
}

/** Đếm ký tự mô tả năng lực trực tiếp khi gõ, tô đỏ tới khi đủ 20 ký tự (khớp
 *  yêu cầu của backend), và tự động rút gọn mã agent về đúng dạng slug hợp lệ. */
function updateCapCount() {
  const n = el('agentForm').capability.value.trim().length;
  const span = el('capCount');
  span.textContent = `${n}/20`;
  span.style.color = n >= 20 ? 'var(--ok-ink)' : 'var(--err-ink)';
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
  box.className = 'gate';
  if (a.is_core) { box.textContent = 'Agent lõi — không qua chốt chặn đánh giá của Studio.'; return; }
  try {
    const runs = await api(`/api/eval/agents/${a.id}/runs`);
    const passed = runs.find(r => r.passed === true && r.agent_version === a.version);
    if (a.status === 'active') {
      box.className = 'gate is-ok';
      box.textContent = '● Đang hoạt động — Điều phối thấy agent này ở mỗi lượt.';
      setSteps(3);
    } else if (passed) {
      box.className = 'gate is-ok';
      box.textContent = `✓ Đã đạt đánh giá trên v${a.version} (lần chạy ${passed.id}) — bật được.`;
      setSteps(3);
    } else {
      box.className = 'gate is-warn';
      box.textContent = `○ Chưa có lần đánh giá nào đạt trên v${a.version} — bấm Bật sẽ bị chặn trừ khi ép buộc.`;
      setSteps(2);
    }
  } catch { box.textContent = 'Không đọc được lịch sử đánh giá.'; }
}

window.editAgent = async (id) => {
  const a = await api('/api/agents/' + id);
  state.agent = a;
  show(el('builderEmpty'), false);
  show(el('builderAiBox'), false);
  show(el('agentForm'), true);
  el('sandboxOut').innerHTML = '';
  setSteps(2);
  const f = el('agentForm');
  f.display_name.value = a.display_name; f.id.value = a.id; f.id.readOnly = true;
  f.capability.value = a.capability; f.business_prompt.value = a.business_prompt || '';
  show(el('coreWarn'), !!a.is_core);
  [...f.elements].forEach(x => { if (x.type !== 'button' && x.type !== 'submit') x.disabled = a.is_core; });
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
    ? docs.map(d => `<label>
        <input type="checkbox" class="builderDocChk" value="${esc(d.id)}">
        <span class="mono">${esc(d.filename)}</span>
        <span class="muted">(${esc(d.scope)})</span>
      </label>`).join('')
    : '<span class="muted">Thư viện chưa có tài liệu nào.</span>';
}

el('newAgent').onclick = () => {
  state.agent = null;
  show(el('builderEmpty'), false);
  show(el('agentForm'), true);
  show(el('coreWarn'), false);
  el('sandboxOut').innerHTML = '';
  el('evalGateStatus').className = 'gate';
  el('evalGateStatus').textContent = 'Chưa chạy đánh giá lần nào — lưu agent rồi bấm Đánh giá.';
  const f = el('agentForm');
  f.reset(); f.id.readOnly = false;
  [...f.elements].forEach(x => x.disabled = false);
  f.business_prompt.value = PROMPT_TEMPLATE;
  renderToolPicker([]);
  el('docList').innerHTML = '<div class="muted">Lưu agent trước rồi mới tải tài liệu được.</div>';
  updateCapCount();
  setSteps(1);

  // Ô "Tạo bằng mô tả" chỉ có ý nghĩa khi đang tạo agent MỚI.
  show(el('builderAiBox'), true);
  el('builderYeuCau').value = '';
  el('builderAiResult').innerHTML = '';
  el('builderAiStatus').textContent = '';
  el('builderTemplates').innerHTML = DESC_TEMPLATES.map((t, i) => `
    <button type="button" class="tmpl" onclick="useDescTemplate(${i})">
      <strong>${esc(t.ten)}</strong>
      <small>${esc(t.mo_ta)}</small>
    </button>`).join('');
  renderBuilderDocPicker();
  loadAgents();
};
window.useDescTemplate = (i) => {
  el('builderYeuCau').value = DESC_TEMPLATES[i].mo_ta;
  el('builderYeuCau').focus();
};

/** Hiển thị giải thích/cảnh báo Builder trả về — quản lý đọc trước khi Lưu. */
function renderBuilderOutput(out) {
  const rows = [];
  const tools = out.giai_thich?.tools || {};
  if (Object.keys(tools).length) {
    rows.push('<div class="result-card"><h3>Vì sao chọn tool này</h3>' +
      Object.entries(tools).map(([t, why]) =>
        `<div class="result-row"><span class="mono">${esc(t)}</span><span>${esc(why)}</span></div>`).join('') + '</div>');
  }
  if (out.thieu_tool?.length) {
    rows.push(`<div class="result-card is-warn"><h3>Thiếu tool phù hợp</h3>
      <p class="muted">Builder cố ý để trống thay vì chọn nhầm: ${esc(out.thieu_tool.join('; '))}</p></div>`);
  }
  if (out.chong_lan?.length) {
    rows.push(`<div class="result-card is-warn"><h3>Có thể chồng lấn với agent khác</h3>` +
      out.chong_lan.map(c => `<div class="result-row"><span class="mono">${esc(c.agent_id)}</span>
        <span>${esc(c.diem_trung)}</span><span class="badge badge-warn">${esc(c.muc_do)}</span></div>`).join('') + '</div>');
  }
  if (out.canh_bao?.length) {
    rows.push(`<div class="result-card is-warn"><h3>Cảnh báo</h3>
      <p class="muted">${out.canh_bao.map(esc).join('<br>')}</p></div>`);
  }
  if (out.thay_doi?.length) {
    rows.push(`<div class="result-card"><h3>Builder đã tự sửa</h3>
      <p class="muted">${out.thay_doi.map(esc).join('<br>')}</p></div>`);
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
  setSteps(2);
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
    el('builderAiResult').innerHTML = result.history.map(h => `
      <div class="result-card ${h.passed && h.regression_dat ? 'is-ok' : 'is-warn'}">
        <h3>Vòng ${h.round}</h3>
        <div class="result-row"><span>Đánh giá</span><span>${h.passed ? '✓ đạt' : '✗ chưa đạt'}</span></div>
        <div class="result-row"><span>Hồi quy định tuyến</span><span>${h.regression_dat ? '✓ đạt' : '✗ chưa đạt'}</span></div>
        ${h.case_fail?.length ? `<div class="result-row"><span>Case chưa đạt</span><span class="mono">${esc(h.case_fail.join(', '))}</span></div>` : ''}
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
    show(el('agentForm'), false); show(el('builderSteps'), false); show(el('builderEmpty'), true); loadAgents(); }
  catch (err) { alert('Lỗi: ' + err.message); }
};

el('btnVersions').onclick = async () => {
  if (!state.agent) return;
  const vs = await api(`/api/agents/${state.agent.id}/versions`);
  el('sandboxOut').innerHTML = `<div class="result-card"><h3>Lịch sử phiên bản</h3>` +
    vs.map(v => `<div class="result-row">
      <span>v${v.version}</span>
      <span class="muted">${esc(v.created_at)}</span>
      <button class="btn btn-compact" onclick="rollback(${v.version})">Khôi phục</button>
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
  el('sandboxOut').innerHTML = '<div class="muted" style="margin-top:14px">Đang chạy thử…</div>';
  try {
    const r = await api(`/api/sandbox/agent/${state.agent.id}`, { method: 'POST', headers: { 'Content-Type':'application/json' }, body: JSON.stringify({ ticket_text: text }) });
    el('sandboxOut').innerHTML = `<div class="result-card">
      <h3>Kết quả chạy thử</h3>
      <div class="result-row"><span>Kết luận</span><span>${esc(r.output.ket_luan)}</span></div>
      <div class="result-row"><span>Tool đã gọi</span><span class="mono">${r.tool_calls.length ? r.tool_calls.map(t => esc(t.tool)).join(', ') : '(không)'}</span></div>
      <div class="result-row"><span>Tài liệu khớp</span><span class="mono">${r.rag_hits.length ? r.rag_hits.map(h => esc(h.filename)).join(', ') : '(không)'}</span></div>
      <details class="det"><summary>chi tiết JSON</summary><pre class="code-block">${j(r)}</pre></details></div>`;
  } catch (err) { el('sandboxOut').innerHTML = `<div class="err-box" style="margin-top:14px">Lỗi: ${esc(err.message)}</div>`; }
};

el('btnTryRouter').onclick = async () => {
  const text = prompt('Phản ánh giả để thử định tuyến:', 'Rác tồn đọng ở hành lang tầng 12 hai ngày nay chưa ai dọn');
  if (!text) return;
  el('sandboxOut').innerHTML = '<div class="muted" style="margin-top:14px">Đang hỏi Điều phối…</div>';
  try {
    const r = await api('/api/sandbox/router', { method: 'POST', headers: { 'Content-Type':'application/json' },
      body: JSON.stringify({ ticket_text: text, include_draft_id: state.agent?.id || null }) });
    const d = r.quyet_dinh;
    el('sandboxOut').innerHTML = `<div class="result-card">
      <h3>Điều phối sẽ chọn</h3>
      <div class="result-row"><span>Lượt đầu</span><span class="mono">${d.hanh_dong === 'ket_thuc' ? '(kết thúc, không gọi ai)' : esc(d.agent_id)}</span></div>
      <div class="result-row"><span>Lý do</span><span>${esc(d.ly_do)}</span></div>
      <div class="result-row"><span>Chỉ dẫn</span><span>${esc(d.chi_dan)}</span></div>
      <div class="footnote">Đã xét: ${r.thanh_vien_xet_den.map(m => esc(m.id)).join(', ')}</div></div>`;
  } catch (err) { el('sandboxOut').innerHTML = `<div class="err-box" style="margin-top:14px">Lỗi: ${esc(err.message)}</div>`; }
};

el('btnEvaluate').onclick = async () => {
  if (!state.agent) return alert('Lưu agent trước đã.');
  const btn = el('btnEvaluate');
  btn.disabled = true;
  el('sandboxOut').innerHTML = '<div class="muted" style="margin-top:14px">Đang chuẩn bị case đánh giá…</div>';
  try {
    let cases = await api(`/api/eval/agents/${state.agent.id}/cases`);
    let approved = cases.filter(c => c.approved);
    if (!approved.length) {
      el('sandboxOut').innerHTML = '<div class="muted" style="margin-top:14px">Chưa có case nào — đang nhờ Evaluator sinh case mới…</div>';
      const gen = await api(`/api/eval/agents/${state.agent.id}/generate`, {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ auto_approve: true }),
      });
      approved = (gen.cases || []).filter(c => c.approved);
      if (!approved.length) throw new Error('Evaluator không sinh được case nào để chạy.');
    }
    el('sandboxOut').innerHTML = `<div class="muted" style="margin-top:14px">Đang chạy ${approved.length} case qua phòng họp thật ở chế độ sandbox — có thể mất vài phút…</div>`;
    const run = await api(`/api/eval/agents/${state.agent.id}/run`, {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ trigger: 'manual' }),
    });
    renderEvalRun(run);
    await renderEvalGateStatus(state.agent);
  } catch (err) {
    el('sandboxOut').innerHTML = `<div class="err-box" style="margin-top:14px">Lỗi: ${esc(err.message)}</div>`;
  } finally {
    btn.disabled = false;
  }
};

function renderEvalRun(run) {
  const results = run.results || [];
  const summary = run.summary || {};
  el('sandboxOut').innerHTML = `<div class="result-card ${run.passed ? 'is-ok' : 'is-warn'}">
    <h3>${run.passed ? '✓ Đạt đánh giá' : '✗ Chưa đạt đánh giá'} — ${summary.so_dat ?? 0}/${summary.so_case ?? results.length} case đạt</h3>
    ${results.map(r => `
      <details class="det" style="border-bottom:1px solid var(--b-line);padding:9px 0">
        <summary class="mono" style="font-size:11px;color:${r.passed ? 'var(--ok-ink)' : 'var(--err-ink)'}">${r.passed ? '✓' : '✗'} ${esc(r.case_id)}</summary>
        <div style="font-size:11px;line-height:1.8">
          ${r.checks && r.checks.final_reply ? `<div>${esc(r.checks.final_reply)}</div>` : ''}
          ${r.checks && r.checks.detail ? `<pre class="code-block">${j(r.checks.detail)}</pre>` : ''}
          ${r.judge && r.judge.diem && Object.keys(r.judge.diem).length ? `<div>Judge: ${Object.entries(r.judge.diem).map(([k, v]) => `${esc(k)}=${v}`).join(', ')}</div>` : ''}
        </div>
      </details>`).join('')}
    ${!run.passed ? '<div class="footnote">Sửa agent (nhiệm vụ / quy tắc / tool) rồi bấm Đánh giá lại. Bấm Bật khi chưa đạt sẽ bị chặn trừ khi ép buộc.</div>' : ''}
  </div>`;
}

function renderDocs(docs) {
  el('docList').innerHTML = docs.length ? docs.map(d => `
    <div class="doc-row">
      <span><span class="mono">${esc(d.filename)}</span> <span class="muted">· ${d.num_chunks} đoạn</span></span>
      <button type="button" class="btn btn-compact btn-danger" onclick="delDoc('${esc(d.id)}')">Xóa</button>
    </div>`).join('') : '<div class="muted">Chưa có tài liệu.</div>';
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
  renderTools();
}
function renderTools() {
  const q = fold(state.toolQuery);
  const rows = state.tools.filter(t => {
    if (state.toolFilter === 'approval' && !t.requires_approval) return false;
    if (state.toolFilter !== 'all' && state.toolFilter !== 'approval' && t.scope !== state.toolFilter) return false;
    if (!q) return true;
    return fold(`${t.name} ${t.provider} ${t.manager_description}`).includes(q);
  });
  el('toolTable').innerHTML = rows.map(t => `
    <tr class="${t.available ? '' : 'is-off'}">
      <td class="tool-name"><span class="mono">${esc(t.name)}</span><span class="muted">${esc(t.manager_description)}</span></td>
      <td class="mono">${esc(t.provider)}</td>
      <td><span class="badge">${esc(t.scope)}</span></td>
      <td>${t.requires_approval
        ? `<span class="badge badge-warn">${esc(roleLabel(t.approval_role || 'bql'))}</span>`
        : '<span class="muted">—</span>'}</td>
      <td class="mono muted">${esc((t.context_params || []).join(', ') || '—')}</td>
      <td>${t.available ? '<span class="badge badge-ok">khả dụng</span>'
        : `<span class="badge badge-err" title="${esc(t.unavailable_reason)}">không khả dụng</span>`}</td>
    </tr>`).join('') || '<tr><td colspan="6"><div class="empty">Không có tool nào khớp.</div></td></tr>';
}
bindSegments('toolFilter', (f) => { state.toolFilter = f; renderTools(); });
el('toolSearch').oninput = (e) => { state.toolQuery = e.target.value; renderTools(); };
el('refreshTools').onclick = async () => { await api('/api/tools/refresh', { method: 'POST' }); loadTools(); toast('Đã kết nối lại MCP.'); };

/* ==================== TAB 5-6 : HÀNG ĐỢI DUYỆT THEO VAI TRÒ ==================== */
/* Một hàng đợi cho mỗi vai trò khai trong domain.yaml. "Đơn vị thực hiện" dùng chung
 * cho kỹ thuật, an ninh, vệ sinh và nhà thầu ngoài — thêm bên mới không phải thêm tab. */

async function loadApprovals(role) {
  const roles = role ? [role] : consoleRoles();
  for (const r of roles) await renderQueueFor(r);
  await refreshBadges();
}

async function renderQueueFor(role) {
  const box = el('list-' + role);
  if (!box) return;
  const status = state.apprFilter[role] ?? 'cho_duyet';
  const rows = await api(`/api/actions?status=${encodeURIComponent(status)}&role=${encodeURIComponent(role)}`);
  box.innerHTML = rows.length ? rows.map(a => approvalCard(a)).join('')
    : '<div class="surface pad empty">Không có hành động nào ở trạng thái này.</div>';
}

function approvalCard(a) {
  const step = state.domain?.quy_trinh_xac_nhan?.find(s => s.tools.includes(a.tool));
  return `
    <div class="${a.phong_hop_dang_cho ? 'approval-card' : 'surface pad'}" style="margin-bottom:12px">
      <div class="section-title" style="margin-bottom:10px">
        <div>
          <strong class="mono">${esc(a.tool)}</strong>
          <div class="muted">${esc(a.agent_id)} · phản ánh <span class="mono">${esc(a.ticket_id)}</span>
            · ${esc((a.created_at || '').replace('T', ' ').slice(0, 19))}</div>
          ${step ? `<div class="muted">Bước quy trình: <b>${esc(step.label)}</b></div>` : ''}
        </div>
        <span class="badge ${ACT_CLS[a.status] || 'badge'}">${esc(a.status)}</span>
      </div>
      ${a.phong_hop_dang_cho ? `<div class="notice" style="margin-bottom:12px;display:flex;gap:8px;align-items:center">${ICON.pause} Phòng họp đang dừng chờ quyết định này — duyệt hoặc từ chối để phiên họp chạy tiếp.</div>` : ''}
      <pre class="code-block">${j(a.args)}</pre>
      ${a.result && Object.keys(a.result).length
        ? `<details class="det"><summary>kết quả</summary><pre class="code-block">${j(a.result)}</pre></details>` : ''}
      <div class="actions" style="margin-top:12px">
        ${a.status === 'cho_duyet' ? `
          <button class="btn btn-ok btn-compact" onclick="decide('${a.id}','approve')">${a.phong_hop_dang_cho ? 'Duyệt và họp tiếp' : 'Duyệt'}</button>
          <button class="btn btn-danger btn-compact" onclick="decide('${a.id}','reject')">Từ chối</button>` : ''}
        <button class="btn btn-compact" onclick="window.openRoom('${esc(a.ticket_id)}')">Mở phòng họp</button>
      </div>
    </div>`;
}

/** Badge trên điều hướng: mỗi hàng đợi một con số, nháy khi đang giữ một phòng họp. */
async function refreshBadges() {
  for (const r of consoleRoles()) {
    const badge = el('cnt-' + r);
    if (!badge) continue;
    const rows = await api(`/api/actions?status=cho_duyet&role=${encodeURIComponent(r)}`);
    badge.textContent = rows.length;
    badge.hidden = !rows.length;
    const blocking = rows.some(x => x.phong_hop_dang_cho);
    badge.classList.toggle('animate-pulse', blocking);
    badge.style.background = blocking ? 'var(--warn-bg)' : 'var(--err-bg)';
    badge.style.color = blocking ? 'var(--warn-ink)' : 'var(--err-ink)';
  }
}

window.decide = async (id, what) => {
  // Nút bị nhân đôi (băng-rôn phòng họp + tab Công việc + hàng đợi + khung chat cư dân),
  // nên khóa theo action_id để không gửi hai lệnh duyệt cho cùng một hành động.
  if (state.deciding.has(id)) return;
  state.deciding.add(id);
  document.querySelectorAll(`[onclick*="${id}"]`).forEach(b => { b.disabled = true; b.classList.add('is-busy'); });
  try {
    const res = await api(`/api/actions/${id}/${what}`, { method: 'POST' });
    if (what === 'approve') {
      toast(res.phong_hop_tiep_tuc ? 'Đã duyệt — phòng họp đang họp tiếp với kết quả thật.' : 'Đã duyệt và thực thi.');
    } else {
      toast(res.phong_hop_tiep_tuc ? 'Đã từ chối — phòng họp họp tiếp, bộ phận phải tìm phương án khác.' : 'Đã từ chối.');
    }
  } catch (err) {
    alert('Lỗi: ' + err.message);
  } finally {
    state.deciding.delete(id);
    loadApprovals();
    loadResidentApprovals();
    if (state.ticketId) { loadTicketTasks(); loadChain(); }
  }
};

/** Không có stream chung, nên hỏi ngắn theo chu kỳ để badge và thẻ duyệt của cư dân
 *  tự đổi kể cả khi đang mở tab khác. */
async function pollApprovalBadge() {
  try {
    await refreshBadges();
    await loadResidentApprovals();
    for (const r of consoleRoles()) {
      if (!el('tab-' + r).hidden) await renderQueueFor(r);
    }
  } catch { /* backend chưa lên: lần sau thử lại */ }
}
setInterval(pollApprovalBadge, 5000);

/* ------------------------------------------------------------------ misc */
function toast(msg) {
  const d = document.createElement('div');
  d.className = 'toast';
  d.textContent = msg;
  document.body.appendChild(d);
  setTimeout(() => d.remove(), 2600);
}

(async function init() {
  try {
    const [residents, domain, health] = await Promise.all([
      api('/api/residents'), api('/api/domain'), api('/api/health'),
    ]);
    state.residents = residents; state.domain = domain;
    state.roles = domain.approval_roles || [];
    for (const r of consoleRoles()) {
      const title = el('title-' + r), desc = el('desc-' + r), role = state.roles.find(x => x.id === r);
      if (title && role) title.textContent = role.label;
      if (desc && role?.mo_ta) desc.textContent = role.mo_ta + ' — tool cần duyệt không bao giờ tự chạy: phòng họp dừng lại chờ quyết định rồi họp tiếp với kết quả thật.';
      if (el('filter-' + r)) bindSegments('filter-' + r, (f) => { state.apprFilter[r] = f; renderQueueFor(r); });
    }
    el('domainName').textContent = domain.display_name;
    el('engineBadge').textContent = `${health.chat_model} · phòng họp ${health.room_engine}`
      + (health.hitl_wait_for_approval ? ' · HITL bật' : '');
    el('residentSel').innerHTML = residents.map(r =>
      `<option value="${esc(r.ma_cu_dan)}">${esc(r.ho_ten)} — ${esc(r.ma_can_ho)}</option>`).join('');
    el('residentSel').onchange();
    showTab(location.hash.slice(1) || 'resident');
    loadApprovals();
    loadResidentApprovals();
  } catch (err) {
    document.body.insertAdjacentHTML('afterbegin',
      `<div style="background:var(--err-ink);color:#fff;padding:12px 16px;font-size:13px">Không kết nối được backend: ${esc(err.message)}</div>`);
  }
})();
