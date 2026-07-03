// ============================================================
//  Agent Bridge · Control Center
// ============================================================

const VALID_ID_RE = /^[a-zA-Z0-9_-]+$/;
const DISCOVER_CACHE_KEY = 'agentBridgeDiscoveredAgents';
const AGENT_COLOR_POOL = ['#5b9a4a', '#d97706', '#2563eb', '#be123c', '#7c3aed', '#0f766e', '#c2410c', '#4f46e5'];

let state = {
  messages: [], roomLogs: [], agents: [], agentIds: [], agentMap: {}, agentAlias: {},
  discoveredAgents: null, discoveredAt: '',
  archive: '__active__', searchQuery: '',
  autoRefresh: true, pollInterval: null, pollStatusTimer: null,
  editingBadge: null, popupCloseHandler: null,
  activeTab: 'chat',
  rooms: [], currentRoomId: null, chatView: 'grid',
  roomGridFingerprint: '',
  roomGridRendered: false,
  roomViewSeq: 0,
};

const $ = s => document.querySelector(s);
const chatArea = $('#chatArea');
const searchInput = $('#searchInput');
const refreshBtn = $('#refreshBtn');
const backToRoomsBtn = $('#backToRooms');
const composeAgent = $('#composeAgent');
const composeInput = $('#composeInput');
const composeBtn = $('#composeBtn');
const statCount = $('#statCount');
const statLast = $('#statLast');
const pollDot = $('#pollDot');
const pollLabel = $('#pollLabel');
const pollBtn = $('#pollBtn');
const roomLogArea = $('#roomLogArea');
const roomLogCount = $('#roomLogCount');
const roomTurnStatus = $('#roomTurnStatus');
const configBanner = $('#configBanner');
const themeToggle = $('#themeToggle');

function setupRoomGridHeader() {
  const header = document.querySelector('.room-grid-header');
  if (!header || header.dataset.enhanced === '1') return;

  const title = header.querySelector('.room-grid-title');
  if (title && title.parentElement && !header.querySelector('.room-grid-header-copy')) {
    const wrap = document.createElement('div');
    wrap.className = 'room-grid-header-copy';
    title.parentElement.insertBefore(wrap, title);
    wrap.appendChild(title);
    const subtitle = document.createElement('div');
    subtitle.className = 'room-grid-subtitle';
    subtitle.textContent = '点击进入，拖拽 Agent 到卡片里即可调整房间成员';
    wrap.appendChild(subtitle);
  }

  if (!header.querySelector('#roomGridSummary')) {
    const summary = document.createElement('div');
    summary.id = 'roomGridSummary';
    summary.className = 'room-grid-summary';
    summary.textContent = '0 个聊天室';
    header.appendChild(summary);
  }

  header.dataset.enhanced = '1';
}
setupRoomGridHeader();

// ═══ Theme ═══
const THEME_KEY = 'agentBridgeTheme';

function currentTheme() {
  return document.documentElement.dataset.theme === 'dark' ? 'dark' : 'light';
}

function applyTheme(theme) {
  const next = theme === 'dark' ? 'dark' : 'light';
  const prev = document.documentElement.dataset.theme || 'light';
  document.documentElement.dataset.theme = next;
  if (themeToggle) {
    if (prev === next) {
      themeToggle.textContent = next === 'light' ? '☾' : '☀';
      themeToggle.title = next === 'light' ? '切换到暗色模式' : '切换到亮色模式';
      themeToggle.setAttribute('aria-label', themeToggle.title);
      return;
    }
    themeToggle.style.transform = 'scale(0.8)';
    themeToggle.style.transition = 'transform 0.12s ease';
    setTimeout(() => {
      themeToggle.textContent = next === 'light' ? '☾' : '☀';
      themeToggle.title = next === 'light' ? '切换到暗色模式' : '切换到亮色模式';
      themeToggle.setAttribute('aria-label', themeToggle.title);
      themeToggle.style.transform = 'scale(1)';
    }, 80);
  }
}

function saveTheme(theme) {
  try { localStorage.setItem(THEME_KEY, theme); } catch (e) {}
}

applyTheme(currentTheme());
if (themeToggle) {
  themeToggle.addEventListener('click', () => {
    const next = currentTheme() === 'light' ? 'dark' : 'light';
    applyTheme(next);
    saveTheme(next);
  });
}

// ═══ API ═══
async function apiGet(p) { try { const r = await fetch(p); return await r.json(); } catch(e) { return null; } }
async function apiPost(p, d) {
  try { const r = await fetch(p, {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify(d)}); return await r.json(); }
  catch(e) { return null; }
}
async function apiPut(p, d) {
  try { const r = await fetch(p, {method:'PUT', headers:{'Content-Type':'application/json'}, body:JSON.stringify(d)}); return await r.json(); }
  catch(e) { return null; }
}
function delay(ms) { return new Promise(resolve => setTimeout(resolve, ms)); }

function readDiscoverCache() {
  try {
    const cache = JSON.parse(localStorage.getItem(DISCOVER_CACHE_KEY) || 'null');
    if (!cache || !Array.isArray(cache.agents)) return null;
    return cache;
  } catch (e) {
    return null;
  }
}

function writeDiscoverCache(agents) {
  const cache = {agents: agents || [], scanned_at: new Date().toISOString()};
  try { localStorage.setItem(DISCOVER_CACHE_KEY, JSON.stringify(cache)); } catch (e) {}
  return cache;
}

function randomAgentColor() {
  return AGENT_COLOR_POOL[Math.floor(Math.random() * AGENT_COLOR_POOL.length)];
}

function refreshAgentCount() {
  const count = document.querySelectorAll('.agent-config-grid .agent-config').length;
  const el = $('.agent-page-count');
  if (el) el.textContent = `${count} 个 Agent`;
}

function formatScanTime(value) {
  if (!value) return '';
  const d = new Date(value);
  if (Number.isNaN(d.getTime())) return '';
  return d.toLocaleString('zh-CN', {hour12: false});
}

function applyDiscoverStatus(agents) {
  const configured = new Set((state.agents || []).map(a => a.id));
  return (agents || []).map(a => ({...a, configured: configured.has(a.id)}));
}

// ═══ Config ═══
async function loadConfig() {
  const data = await apiGet('/api/config');
  if (!data || !data.ok) return;
  const agents = data.agents || [];
  state.agents = agents;
  state.fullAgents = agents; // full data including cursor, filter_from, wakeup
  state.agentIds = agents.map(a => a.id);
  const newMap = {};
  for (const a of agents) newMap[a.id] = {display_name: a.display_name || a.id, color: a.color || '#8888a0'};
  const oldAliases = state.agentAlias || {};
  state.agentAlias = {};
  for (const [oldId, info] of Object.entries(oldAliases)) {
    const cur = newMap[info.id] || newMap[oldId];
    if (cur) state.agentAlias[oldId] = {...cur, id: info.id || oldId};
  }
  state.agentMap = newMap;
  renderBadges();
  renderComposeSelect();
  renderConfigBanner(data);
}

function getConfigIssues(data) {
  const agents = data.agents || [];
  const ids = new Set(agents.map(a => a.id).filter(Boolean));
  const issues = [];
  if (!data.shared_dir) issues.push('共享目录未设置');
  if (!agents.length) issues.push('至少添加一个 Agent');
  if (!data.agent_id) issues.push('选择本机角色');
  else if (!ids.has(data.agent_id)) issues.push(`本机角色 ${data.agent_id} 不在 Agent 列表中`);
  for (const a of agents) {
    const name = a.display_name || a.id || '未命名 Agent';
    const adapter = a.adapter || {};
    const adapterType = adapter.type || (a.wakeup && a.wakeup.url ? 'native_http' : 'manual');
    const adapterConfig = adapter.config || {};
    if (!a.id) issues.push(`${name}: Agent ID 不能为空`);
    if (a.sample) {
      issues.push(`${name}: 示例 Agent，需替换为真实 Agent`);
      continue;
    }
    if (adapterType === 'openclaw_sessions') {
      if (!adapterConfig.url) issues.push(`${name}: 填写 OpenClaw URL`);
      if (!adapterConfig.sessions_key && !adapterConfig.sessionsKey) issues.push(`${name}: 填写 OpenClaw 会话`);
    } else if (adapterType === 'native_http') {
      if (!adapterConfig.url && (!a.wakeup || !a.wakeup.url)) issues.push(`${name}: 填写 Webhook URL`);
      if (!a.wakeup || !a.wakeup.body_template) issues.push(`${name}: 确认消息体模板`);
    }
  }
  return issues;
}

function renderConfigBanner(data) {
  const issues = getConfigIssues(data);
  if (!issues.length) {
    configBanner.className = 'config-banner';
    configBanner.innerHTML = '';
    return;
  }
  configBanner.className = 'config-banner show';
  configBanner.innerHTML = `<span>还需要完成 ${issues.length} 个配置项：${esc(issues.slice(0, 2).join('；'))}${issues.length > 2 ? '…' : ''}</span><button onclick="switchToAgent()">去配置</button>`;
}

function switchToAgent() {
  const btn = document.querySelector('.tab-btn[data-tab="agent"]');
  if (btn) btn.click();
}

// ═══ Badges (agent tab) ═══
function renderBadges() {
  const container = $('#agentBadgeContainer');
  if (!container) return;
  container.innerHTML = '';
  for (const a of state.agents) {
  const el = document.createElement('div');
  el.className = 'agent-badge'; el.dataset.agentId = a.id;
  el.innerHTML = `<span class="agent-dot" style="background:${a.color}"></span><span>${esc(a.display_name||a.id)}</span>${a.sample ? '<span class="discover-pill sample">示例</span>' : ''}`;
  container.appendChild(el);
  }
  refreshAgentCount();
}

function updateScanNote(scannedAt) {
  const note = $('#scanCacheNote');
  if (!note) return;
  const time = formatScanTime(scannedAt);
  note.textContent = time ? `上次扫描：${time}，结果已保存在本机。` : '尚未扫描，本页只在点击按钮时刷新数据。';
}

function renderDiscoverAgents(agents, scannedAt) {
  const el = $('#agentDiscoverResult');
  if (!el) return;
  const normalized = applyDiscoverStatus(agents);
  state.discoveredAgents = normalized;
  state.discoveredAt = scannedAt || '';
  updateScanNote(scannedAt);
  if (!normalized.length) {
    el.innerHTML = '<div class="discover-empty">未发现本机 Agent</div>';
    return;
  }
  el.innerHTML = `<div class="discover-grid">${normalized.map((a, idx) => {
    const status = a.configured ? '已配置' : (a.sample ? '示例' : '未配置');
    const statusClass = a.configured ? 'configured' : (a.sample ? 'sample' : '');
    const isOpenClaw = a.adapter?.type === 'openclaw_sessions' || a.kind === 'OpenClaw';
    const addAction = a.configured
      ? '<button class="scan-btn" disabled>已在 agent-bridge</button>'
      : `<button class="scan-btn" onclick="addDiscoveredAgent(${idx})">${isOpenClaw ? '添加 OpenClaw' : '添加到 agent-bridge'}</button>`;
    const openAction = discoverSourceDir(a)
      ? `<button class="scan-btn" onclick="openDiscoveredAgentDir(${idx})">打开目录</button>`
      : '';
    const action = `${openAction}${addAction}`;
    return `<div class="discover-card">
      <div class="discover-card-header">
        <span class="discover-name">📡 ${esc(a.display_name || a.id)}</span>
        <span class="discover-pill ${statusClass}">${status}</span>
      </div>
      <div class="discover-meta">${esc(a.kind || 'Agent')} · ${esc(a.id)}</div>
      <div class="discover-meta">${esc(a.details || '')}</div>
      ${isOpenClaw ? '<div class="discover-meta">将自动填好本地网关、认证和自动回写。</div>' : ''}
      <div class="discover-meta">${esc(a.source || '')}</div>
      <div class="discover-actions">${action}</div>
    </div>`;
  }).join('')}</div>`;
}

function discoverSourceDir(agent) {
  const source = agent?.source || '';
  if (agent?.source_dir) return agent.source_dir;
  if (!source) return '';
  const normalized = source.replace(/\\/g, '/');
  const slash = normalized.lastIndexOf('/');
  const name = slash >= 0 ? normalized.slice(slash + 1) : normalized;
  if (/\.(jsonl?|ya?ml)$/i.test(name)) {
    const end = source.length - name.length;
    return source.slice(0, end).replace(/[\\/]+$/, '') || source;
  }
  return source;
}

async function openDiscoveredAgentDir(index) {
  const agent = (state.discoveredAgents || [])[index];
  const path = discoverSourceDir(agent);
  if (!path) return;
  const res = await apiPost('/api/open-dir', {path});
  if (!res || !res.ok) toast(res?.error || '无法打开目录', 'error');
}

function renderCachedDiscover() {
  const cache = readDiscoverCache();
  const btn = $('#agentScanBtn');
  if (btn) btn.textContent = cache ? '重新扫描' : '开始扫描';
  if (!cache) {
    state.discoveredAgents = null;
    state.discoveredAt = '';
    updateScanNote('');
    const el = $('#agentDiscoverResult');
    if (el) el.innerHTML = '<div class="discover-empty">暂无扫描结果</div>';
    return;
  }
  renderDiscoverAgents(cache.agents, cache.scanned_at);
}

async function scanAndShowDiscover() {
  const el = $('#agentDiscoverResult');
  const btn = $('#agentScanBtn');
  if (!el) return;
  if (btn) {
    btn.disabled = true;
    btn.classList.add('loading');
    btn.textContent = '扫描中';
  }
  el.innerHTML = '<div class="discover-loading">扫描中…</div>';
  const [data] = await Promise.all([apiGet('/api/agents/discover'), delay(1200)]);
  if (!data || !data.ok) {
    el.innerHTML = '<div class="discover-empty">扫描失败</div>';
    if (btn) {
      btn.disabled = false;
      btn.classList.remove('loading');
      btn.textContent = readDiscoverCache() ? '重新扫描' : '开始扫描';
    }
    return;
  }
  const cache = writeDiscoverCache(data.agents || []);
  renderDiscoverAgents(cache.agents, cache.scanned_at);
  if (btn) {
    btn.disabled = false;
    btn.classList.remove('loading');
    btn.textContent = '重新扫描';
  }
}

function populateAgentCard(card, agent) {
  if (!card || !agent) return;
  const color = agent.color || '#8888a0';
  card.querySelector('.ag-id').value = agent.id || '';
  card.querySelector('.ag-name').value = agent.display_name || agent.id || '';
  card.querySelector('.ag-color').value = color;
  const preview = card.querySelector('.color-preview');
  if (preview) preview.style.background = color;
  const picker = card.querySelector('.agent-color-picker');
  if (picker && /^#[0-9a-fA-F]{6}$/.test(color)) picker.value = color;
  const dot = card.querySelector('.agent-config-name .dot');
  if (dot) dot.style.background = color;
  const label = card.querySelector('.agent-display-label');
  if (label) label.textContent = agent.display_name || agent.id || '新 Agent';
  updateAgentTokenPlaceholders(card);
  const cursor = card.querySelector('.ag-cursor');
  if (cursor) cursor.value = agent.cursor || 'line';

  const adapter = agent.adapter || adapterFromWakeup(agent.wakeup || {});
  const simpleType = card.querySelector('.agent-type-select');
  if (simpleType) {
    simpleType.value = agentTypeFromAdapter(adapter, agent.wakeup || {});
    agentTypeChange(simpleType);
  }
  const adapterSection = card.querySelector('.adapter-section');
  if (adapterSection) {
    const type = adapter.type || 'manual';
    const cfg = adapter.config || {};
    const auth = adapter.auth || {};
    const response = adapter.response || {};
    const typeSelect = adapterSection.querySelector('.ad-type');
    if (typeSelect) {
      typeSelect.value = type;
      adapterTypeChange(typeSelect);
    }
    const adUrl = adapterSection.querySelector('.ad-url');
    const sessionsKey = adapterSection.querySelector('.ad-sessions-key');
    const adAuthType = adapterSection.querySelector('.ad-auth-type');
    const adTokenPath = adapterSection.querySelector('.ad-token-path');
    const adTokenJsonpath = adapterSection.querySelector('.ad-token-jsonpath');
    const adTokenEnv = adapterSection.querySelector('.ad-token-env');
    const responseMode = adapterSection.querySelector('.ad-response-mode');
    const timeoutSeconds = adapterSection.querySelector('.ad-timeout-seconds');
    if (adUrl) adUrl.value = cfg.url || '';
    if (sessionsKey) sessionsKey.value = cfg.sessions_key || cfg.sessionsKey || '';
    if (adAuthType) {
      adAuthType.value = auth.type === 'bearer' ? 'bearer' : 'none';
      authTypeChange(adAuthType);
    }
    if (adTokenPath) adTokenPath.value = auth.token_path || '';
    if (adTokenJsonpath) adTokenJsonpath.value = auth.token_jsonpath || '';
    if (adTokenEnv) adTokenEnv.value = auth.token_env || '';
    if (responseMode) responseMode.value = response.mode || (type === 'openclaw_sessions' ? 'callback' : 'callback');
    if (timeoutSeconds) timeoutSeconds.value = response.timeout_seconds ?? 180;
  }
  const simpleSessionsKey = card.querySelector('.simple-sessions-key');
  if (simpleSessionsKey) simpleSessionsKey.value = (adapter.config || {}).sessions_key || (adapter.config || {}).sessionsKey || 'agent:main:main';

  const wu = agent.wakeup || {};
  const section = card.querySelector('.wakeup-section');
  if (!section) return;
  section.querySelector('.wu-url').value = wu.url || '';
  section.querySelector('.wu-method').value = wu.method || 'POST';
  const auth = wu.auth || {};
  const authType = section.querySelector('.wu-auth-type');
  authType.value = auth.type === 'bearer' ? 'bearer' : 'none';
  authTypeChange(authType);
  const tokenPath = section.querySelector('.wu-token-path');
  const tokenJsonpath = section.querySelector('.wu-token-jsonpath');
  const tokenEnv = section.querySelector('.wu-token-env');
  if (tokenPath) tokenPath.value = auth.token_path || '';
  if (tokenJsonpath) tokenJsonpath.value = auth.token_jsonpath || '';
  if (tokenEnv) tokenEnv.value = auth.token_env || '';

  const headers = wu.headers || {};
  const headerBox = section.querySelector('.wu-headers');
  if (headerBox) {
    headerBox.innerHTML = '';
    const entries = Object.entries(headers);
    if (!entries.length) entries.push(['', '']);
    for (const [k, v] of entries) {
      const row = document.createElement('div');
      row.className = 'kv-row';
      row.innerHTML = '<input type="text" class="header-key" placeholder="Key"/><input type="text" class="header-val" placeholder="Value"/><button class="kv-del" onclick="this.parentElement.remove()" title="删除">×</button>';
      row.querySelector('.header-key').value = k;
      row.querySelector('.header-val').value = v;
      headerBox.appendChild(row);
    }
  }
  const body = section.querySelector('.wu-body');
  if (body) body.value = bodyTemplateToText(wu.body_template || {"message": "{{message}}"});
  updateAgentSummary(card);
}

function addDiscoveredAgent(index) {
  const agent = (state.discoveredAgents || [])[index];
  if (!agent || agent.configured) return;
  const area = $('#agentArea');
  const grid = area?.querySelector('.agent-config-grid');
  if (!grid) return;
  const exists = Array.from(grid.querySelectorAll('.ag-id')).some(input => input.value.trim() === agent.id);
  if (exists) {
    toast('该 Agent 已在配置表单中', 'warning');
    return;
  }
  addAgent();
  const cards = grid.querySelectorAll('.agent-config');
  const card = cards[cards.length - 1];
  populateAgentCard(card, {...agent, color: agent.color && agent.color !== '#8888a0' ? agent.color : randomAgentColor()});
  const status = $('#saveStatus');
  if (status) {
    status.textContent = '已添加到表单，请在该 Agent 卡片中保存配置';
    status.className = 'save-status ok';
  }
}

// ═══ Compose select ═══
function renderComposeSelect() {
  // Compose select is now populated per-room in openRoomConversation()
}

composeInput.addEventListener('keydown', e => { if (e.key === 'Enter') sendMessage(); });
composeBtn.addEventListener('click', sendMessage);

async function sendMessage() {
  const aid = composeAgent.value;
  const text = composeInput.value.trim();
  if (!text) return;
  composeBtn.disabled = true;
  const result = state.currentRoomId
    ? await apiPost(`/api/rooms/${encodeURIComponent(state.currentRoomId)}/send`, {agent_id: aid, text})
    : await apiPost('/api/send', {agent_id: aid, text});
  composeBtn.disabled = false;
  if (result && result.ok) {
    composeInput.value = '';
    if (state.currentRoomId) loadRoomMessages();
    else if (state.activeTab === 'chat') loadMessages();
  }
}

// ═══ Edit popup ═══
function openEditPopup(el, agent) {
  closeEditPopup();
  state.editingBadge = agent.id;
  el.classList.add('editing');
  const popup = document.createElement('div');
  popup.className = 'edit-popup open';
  popup.dataset.agentId = agent.id;
  popup.innerHTML = `
    <div class="edit-field">
      <label>Agent ID</label>
      <input type="text" class="edit-id-input" value="${esc(agent.id)}" placeholder="e.g. momo">
      <div class="edit-error-msg"></div>
    </div>
    <div class="edit-field">
      <label>名称</label>
      <input type="text" class="edit-name-input" value="${esc(agent.display_name||agent.id)}" placeholder="Display name">
    </div>
    <div class="edit-field">
      <label>颜色</label>
      <input type="text" class="edit-color-input" value="${agent.color}" placeholder="#rrggbb" style="font-family:var(--font);font-size:0.8em">
    </div>
    <div class="edit-rename-warn" style="display:none">⚠️ 修改 ID 后需同步更新对方配置中的 filter_from</div>
    <div class="edit-popup-actions">
      <button class="edit-btn cancel-btn">取消</button>
      <button class="edit-btn primary save-btn">保存</button>
    </div>
    <div class="edit-status"></div>`;
  el.appendChild(popup);
  const idI = popup.querySelector('.edit-id-input');
  idI.focus(); idI.setSelectionRange(idI.value.length, idI.value.length);
  idI.addEventListener('input', () => {
    popup.querySelector('.edit-rename-warn').style.display = (idI.value.trim() !== agent.id) ? 'block' : 'none';
    validateId(idI);
  });
  popup.querySelector('.save-btn').addEventListener('click', () => saveAgent(agent, popup));
  popup.querySelector('.cancel-btn').addEventListener('click', closeEditPopup);
  popup.querySelectorAll('input').forEach(inp => {
    inp.addEventListener('keydown', e => { if (e.key === 'Enter') saveAgent(agent, popup); if (e.key === 'Escape') closeEditPopup(); });
  });
  setTimeout(() => {
    if (state.popupCloseHandler) document.removeEventListener('click', state.popupCloseHandler);
    state.popupCloseHandler = e => { if (!e.target.closest('.edit-popup') && !e.target.closest('.agent-badge.editing')) closeEditPopup(); };
    document.addEventListener('click', state.popupCloseHandler);
  }, 10);
}

function validateId(i) {
  const err = i.parentElement.querySelector('.edit-error-msg');
  const v = i.value.trim();
  if (!v) { err.textContent = '不能为空'; i.classList.add('field-error'); return false; }
  if (!VALID_ID_RE.test(v)) { err.textContent = '字母、数字、-、_ 仅'; i.classList.add('field-error'); return false; }
  err.textContent = ''; i.classList.remove('field-error'); return true;
}

function closeEditPopup() {
  if (state.popupCloseHandler) { document.removeEventListener('click', state.popupCloseHandler); state.popupCloseHandler = null; }
  document.querySelectorAll('.edit-popup').forEach(e => e.remove());
  document.querySelectorAll('.agent-badge.editing').forEach(e => e.classList.remove('editing'));
  state.editingBadge = null;
}

async function saveAgent(agent, popup) {
  const st = popup.querySelector('.edit-status');
  const idI = popup.querySelector('.edit-id-input');
  const nameI = popup.querySelector('.edit-name-input');
  const colI = popup.querySelector('.edit-color-input');
  const newId = idI.value.trim();
  const newName = nameI.value.trim();
  const newColor = colI.value.trim();
  if (!validateId(idI)) return;
  if (!/^#[0-9a-fA-F]{6}$/.test(newColor)) { st.textContent = '颜色格式 #rrggbb'; st.className='edit-status error'; return; }
  st.textContent = '保存…'; st.className = 'edit-status';
  const payload = state.agents.map(a => {
    if (a.id === agent.id) return {id: newId, old_id: (newId!==agent.id)?agent.id:undefined, display_name: newName||newId, color: newColor};
    return {id: a.id, display_name: a.display_name, color: a.color};
  });
  const r = await apiPost('/api/config', {agents: payload});
  if (r && r.ok) {
    st.textContent = '✓ 已保存'; st.className = 'edit-status ok';
    await loadConfig();
    if (state.chatView === 'conversation' && state.currentRoomId) {
      refreshCurrentRoomConversation();
    }
    await loadMessages();
    setTimeout(closeEditPopup, 700);
  } else { st.textContent = '失败: '+(r?.error||'unknown'); st.className='edit-status error'; }
}

// ═══ Messages ═══
function resolveAgent(sender) { return state.agentMap[sender] || state.agentAlias[sender] || {display_name:sender,color:'#8888a0'}; }

async function loadMessages() {
  if (state.chatView === 'conversation' && state.currentRoomId) {
    await loadRoomMessages();
    return;
  }
  const p = new URLSearchParams();
  if (state.archive !== '__active__') p.set('archive', state.archive);
  if (state.searchQuery) p.set('q', state.searchQuery);
  p.set('limit','500');
  const data = await apiGet(`/api/messages?${p}`);
  if (!data || !data.ok) return;
  state.messages = data.messages || [];
  // Build aliases
  for (const m of state.messages) {
    const s = m.from; if (!s || state.agentMap[s] || state.agentAlias[s]) continue;
    let best = null, bestScore = 0;
    for (const [kid] of Object.entries(state.agentMap)) {
      if (s.startsWith(kid) || kid.startsWith(s)) { const sc = Math.min(s.length, kid.length); if (sc > bestScore) { bestScore = sc; best = kid; } }
    }
    state.agentAlias[s] = best ? {...state.agentMap[best], id: best} : {display_name: s, color: '#8888a0', id: s};
  }
  renderMessages(); updateStats();
}

function renderSkeletonChat() {
  chatArea.innerHTML = `
    <div class="skeleton-bubble" style="width:65%"></div>
    <div class="skeleton-bubble right"></div>
    <div class="skeleton-bubble" style="width:70%"></div>
    <div class="skeleton-bubble right" style="width:48%"></div>
    <div class="skeleton-bubble" style="width:58%"></div>
  `;
}

function renderMessages() {
  const msgs = state.messages;
  if (!msgs.length) {
    chatArea.innerHTML = '<div class="empty-state"><div class="icon">💬</div><h3>暂无消息</h3><p>等待 agent 开始通信后，消息将在这里显示。</p></div>';
    return;
  }
  let html = '', lastDate = '';
  for (const m of msgs) {
    const sender = m.from || 'unknown', ts = m.ts || '', date = ts.slice(0,10), time = ts.slice(11,19);
    const text = m.msg || '', source = m._source === 'active' ? '' : `📦 ${m._source}`;
    if (date !== lastDate) { html += `<div class="date-separator"><span>${date}</span></div>`; lastDate = date; }
    const idx = state.agentIds.indexOf(sender);
    const side = (idx >=0 && state.agentIds.length>=2) ? (idx%2===0?'left':'right') : 'left';
    const info = resolveAgent(sender), color = info.color;
    html += `<div class="message-group ${side}">
      <div class="msg-avatar ${side}" style="background:${color}22;color:${color};border-color:${color}44">${esc(sender[0].toUpperCase())}</div>
      <div class="msg-bubble ${side}">
        <div class="msg-header">
          <span class="msg-sender" style="color:${color}">${esc(info.display_name)}</span>
          <span class="msg-time">${time}</span>
          ${source?`<span class="msg-source">${source}</span>`:''}
        </div>
        <div class="msg-text">${esc(text)}</div>
      </div></div>`;
  }
  chatArea.innerHTML = html;
  // Staggered entrance
  const groups = chatArea.querySelectorAll('.message-group');
  groups.forEach((g, i) => {
    g.style.opacity = '0';
    g.style.transform = 'translateY(6px)';
    g.style.transition = `opacity 0.2s ease ${i * 0.015}s, transform 0.25s var(--ease-out-expo) ${i * 0.015}s`;
    requestAnimationFrame(() => {
      g.style.opacity = '1';
      g.style.transform = 'translateY(0)';
    });
  });
}

function updateStats() {
  const ms = state.messages;
  statCount.textContent = ms.length;
  statLast.textContent = ms.length ? '· 最后 ' + (ms[ms.length-1].ts||'').slice(11,19) : '';
}

// ═══ Search / Refresh ═══
let searchTimeout;
searchInput.addEventListener('input', () => {
  clearTimeout(searchTimeout);
  searchTimeout = setTimeout(() => { state.searchQuery = searchInput.value.trim(); loadMessages(); }, 300);
});
refreshBtn.addEventListener('click', refreshAll);

// ═══ Room Management ═══
function getCurrentRoom() {
  if (!state.currentRoomId) return null;
  return state.rooms.find(r => r.id === state.currentRoomId) || null;
}

function refreshCurrentRoomConversation() {
  const room = getCurrentRoom();
  if (!room) return;

  const agents = state.agents || [];
  const order = room.order && room.order.length ? room.order : room.agents;
  const selectedAgentId = composeAgent ? composeAgent.value : '';

  const convName = $('#convRoomName');
  if (convName) convName.textContent = room.name || room.id;

  const badges = $('#convAgentBadges');
  if (badges) {
    badges.innerHTML = order.map(aid => {
      const a = agents.find(x => x.id === aid);
      if (!a) return '';
      return `<span class="conv-agent-badge"><span class="dot" style="background:${a.color}"></span>${esc(a.display_name || aid)}</span>`;
    }).join('');
  }

  const controls = $('#roomRunControls');
  if (controls) {
    const running = room.status === 'running';
    controls.innerHTML = `
      <button class="scan-btn" onclick="setRoomRunning('${escAttr(room.id)}', ${running ? 'false' : 'true'})">${running ? '暂停' : '开始'}</button>
      <button class="scan-btn" onclick="tickRoom('${escAttr(room.id)}')">Tick</button>
    `;
  }

  const sel = $('#composeAgent');
  if (sel) {
    sel.innerHTML = order.map(aid => {
      const a = agents.find(x => x.id === aid);
      return `<option value="${escAttr(aid)}">${esc(a ? a.display_name : aid)}</option>`;
    }).join('');
    if (selectedAgentId && order.includes(selectedAgentId)) {
      sel.value = selectedAgentId;
    } else if (order.length) {
      sel.value = order[0];
    }
  }
}

function normalizeRoomForFingerprint(room) {
  const order = room.order && room.order.length ? room.order : room.agents;
  return {
    id: room.id || '',
    name: room.name || '',
    status: room.status || '',
    policy: room.policy || '',
    max_turns: room.max_turns ?? null,
    temporary: !!room.temporary,
    agents: Array.isArray(room.agents) ? room.agents.slice() : [],
    order: Array.isArray(order) ? order.slice() : [],
  };
}

function computeRoomGridFingerprint(rooms, agents) {
  const agentPart = (agents || [])
    .map(a => ({
      id: a.id || '',
      display_name: a.display_name || a.id || '',
      color: a.color || '#8888a0',
    }))
    .sort((a, b) => String(a.id).localeCompare(String(b.id)));
  return JSON.stringify({
    rooms: (rooms || [])
      .map(normalizeRoomForFingerprint)
      .sort((a, b) => String(a.id).localeCompare(String(b.id))),
    agents: agentPart,
  });
}

async function loadRooms() {
  const data = await apiGet('/api/rooms');
  if (!data || !data.ok) return;
  const nextRooms = data.rooms || [];
  const nextFingerprint = computeRoomGridFingerprint(nextRooms, state.agents || []);
  const shouldRender = nextFingerprint !== state.roomGridFingerprint;
  state.rooms = nextRooms;
  if (shouldRender) {
    const rendered = renderRoomGrid({ animate: !state.roomGridRendered });
    if (rendered) {
      state.roomGridFingerprint = nextFingerprint;
      state.roomGridRendered = true;
    }
  }
  if (state.chatView === 'conversation' && state.currentRoomId) {
    refreshCurrentRoomConversation();
  }
}

function renderRoomGrid(options = {}) {
  const animate = !!options.animate;
  const grid = $('#roomGrid');
  if (!grid) return false;
  const agents = state.agents || [];
  const rooms = state.rooms || [];
  const summary = document.querySelector('#roomGridSummary');
  const runningCount = rooms.filter(r => r.status === 'running').length;
  if (summary) {
    summary.textContent = `${rooms.length} 个聊天室 · ${runningCount} 个运行中`;
  }

  let html = '';

  // Agent drag source panel
  html += `<div class="agent-source-panel" style="grid-column:1/-1">
    <div class="agent-source-panel-title">拖拽 Agent 到聊天室</div>
    <div class="agent-source-list">
      ${agents.map(a => `<div class="agent-drag-chip" draggable="true" data-agent-id="${escAttr(a.id)}">
        <span class="dot" style="background:${a.color}"></span>
        ${esc(a.display_name || a.id)}
      </div>`).join('')}
    </div>
  </div>`;

  // Room cards
  for (const room of state.rooms) {
    const isReady = room.agents.length >= 2;
    const running = room.status === 'running';
    const statusClass = running ? 'active' : (isReady ? 'ready' : 'waiting');
    const statusText = running ? '运行中' : (isReady ? '已就绪' : '等待 Agent');
    const isTemporary = !!room.temporary;
    const runText = running ? '暂停' : '开始';
    html += `<div class="room-card ${statusClass}" data-room-id="${escAttr(room.id)}">
      <div class="room-card-header">
        <div class="room-card-name">${esc(room.name || room.id)}${isTemporary ? ' <span class="room-meta-pill room-temp-pill">测试房间</span>' : ''}</div>
        <span class="room-status ${statusClass}">${statusText}</span>
      </div>
      <div class="room-card-agents">${renderAgentSlots(room)}</div>
      <div class="room-card-footer">
        <div class="room-card-footer-left">
          <button class="room-card-action enter" onclick="event.stopPropagation(); openRoomConversation('${escAttr(room.id)}')">进入房间</button>
          <button class="room-card-action primary" onclick="event.stopPropagation(); setRoomRunning('${escAttr(room.id)}', ${running ? 'false' : 'true'})">${runText}</button>
        </div>
        <button class="room-card-action danger room-del-btn" onclick="event.stopPropagation(); ${isTemporary ? `cleanupTestRoom('${escAttr(room.id)}')` : `deleteRoom('${escAttr(room.id)}')`}" title="${isTemporary ? '清理测试房间' : '删除聊天室'}">${isTemporary ? '清理' : '删除'}</button>
      </div>
    </div>`;
  }

  // Add room card (square)
  html += `<div class="room-card-add" data-add-room-card>
    <div class="room-card-add-top">
      <div class="add-icon">+</div>
      <div>
        <div class="add-label">新增聊天室</div>
        <div class="add-desc">输入名称后直接创建</div>
      </div>
    </div>
    <div class="room-card-add-body">
      <div class="room-card-add-row">
        <input type="text" class="room-card-add-input" placeholder="聊天室名称" maxlength="40" />
        <button type="button" class="room-card-action primary room-card-add-submit">创建</button>
      </div>
    </div>
  </div>`;

  grid.innerHTML = html;
  setupDragAndDrop();
  setupRoomAddCard();
  if (animate) {
    // Only play the entrance animation on the initial render.
    const cards = grid.querySelectorAll('.room-card, .room-card-add');
    cards.forEach((card, i) => {
      card.style.opacity = '0';
      card.style.transform = 'translateY(10px)';
      card.style.transition = `opacity 0.25s ease ${i * 0.04}s, transform 0.3s var(--ease-out-expo) ${i * 0.04}s`;
      requestAnimationFrame(() => {
        card.style.opacity = '1';
        card.style.transform = 'translateY(0)';
      });
    });
  }
  return true;
}

function renderAgentSlots(room) {
  const agents = state.agents || [];
  let html = '';
  const order = room.order && room.order.length ? room.order : room.agents;
  const slotCount = Math.max(3, order.length + 1);
  for (let slot = 0; slot < slotCount; slot++) {
    const agentId = order[slot];
    const agent = agentId ? agents.find(a => a.id === agentId) : null;
    if (agent) {
      html += `<div class="room-agent-slot occupied" data-room-id="${escAttr(room.id)}" data-slot="${slot}">
        <div class="agent-chip">
          <span class="dot" style="background:${agent.color}"></span>
          ${esc(agent.display_name || agent.id)}
        </div>
      </div>`;
    } else {
      html += `<div class="room-agent-slot" data-room-id="${escAttr(room.id)}" data-slot="${slot}">
        <div class="slot-hint">拖入 Agent</div>
      </div>`;
    }
  }
  return html;
}

function setupDragAndDrop() {
  document.querySelectorAll('.agent-drag-chip').forEach(chip => {
    chip.addEventListener('dragstart', e => {
      e.dataTransfer.setData('text/plain', chip.dataset.agentId);
      e.dataTransfer.effectAllowed = 'move';
      chip.classList.add('dragging');
    });
    chip.addEventListener('dragend', () => {
      chip.classList.remove('dragging');
      document.querySelectorAll('.room-agent-slot').forEach(s => s.classList.remove('drag-over'));
    });
  });
  document.querySelectorAll('.room-agent-slot').forEach(slot => {
    slot.addEventListener('dragover', e => {
      e.preventDefault();
      e.dataTransfer.dropEffect = 'move';
      slot.classList.add('drag-over');
    });
    slot.addEventListener('dragleave', () => {
      slot.classList.remove('drag-over');
    });
    slot.addEventListener('drop', e => {
      e.preventDefault();
      slot.classList.remove('drag-over');
      const agentId = e.dataTransfer.getData('text/plain');
      const roomId = slot.dataset.roomId;
      const slotIdx = parseInt(slot.dataset.slot);
      assignAgentToRoom(roomId, slotIdx, agentId);
    });
  });
}

async function assignAgentToRoom(roomId, slotIndex, agentId) {
  const room = state.rooms.find(r => r.id === roomId);
  if (!room) return;

  const baseOrder = room.order && room.order.length ? room.order : room.agents;
  const agents = baseOrder.filter(aid => aid !== agentId);
  while (agents.length <= slotIndex) agents.push('');
  agents[slotIndex] = agentId;
  const cleanAgents = agents.filter(Boolean);

  const result = await apiPost('/api/rooms', {
    id: roomId,
    name: room.name,
    agents: cleanAgents,
    order: cleanAgents,
    policy: room.policy || 'round_robin',
    status: room.status || 'paused',
    max_turns: room.max_turns || 50,
  });

  if (result && result.ok) {
    await loadRooms();
    await loadConfig();
  } else if (result) {
    alert(result.error || '操作失败');
  }
}

function setupRoomAddCard() {
  const card = document.querySelector('[data-add-room-card]');
  if (!card) return;
  const input = card.querySelector('.room-card-add-input');
  const submit = card.querySelector('.room-card-add-submit');
  if (!input || !submit) return;
  const create = () => createRoomFromAddCard(card);
  input.onkeydown = e => {
    if (e.key === 'Enter') create();
  };
  submit.onclick = create;
}

async function createRoomFromAddCard(card) {
  const input = card?.querySelector('.room-card-add-input');
  const submit = card?.querySelector('.room-card-add-submit');
  if (!input || !submit) return;
  const name = input.value.trim();
  if (!name) {
    input.classList.add('field-error');
    input.focus();
    toast('请输入聊天室名称', 'warning');
    return;
  }
  input.classList.remove('field-error');
  submit.disabled = true;
  const oldText = submit.textContent;
  submit.textContent = '创建中…';
  const result = await apiPost('/api/rooms', { name, agents: [] });
  if (result && result.ok) {
    input.value = '';
    toast('聊天室已创建', 'ok');
    await loadRooms();
    requestAnimationFrame(() => {
      const nextInput = document.querySelector('[data-add-room-card] .room-card-add-input');
      if (nextInput) nextInput.focus();
    });
  } else {
    toast(result?.error || '创建失败', 'error');
  }
  submit.disabled = false;
  submit.textContent = oldText || '创建';
}

async function deleteRoom(roomId) {
  const room = state.rooms.find(r => r.id === roomId);
  const roomName = room?.name || roomId;
  const ok = await confirmDialog({
    title: '删除聊天室',
    message: `确认删除聊天室「${roomName}」？该操作会移除聊天室配置。`,
    confirmText: '删除聊天室',
  });
  if (!ok) return;
  const result = await apiPost('/api/rooms/delete', { id: roomId });
  if (result && result.ok) {
    if (state.currentRoomId === roomId) showRoomGrid();
    await loadRooms();
    await loadConfig();
  } else {
    toast(result?.error || '删除失败', 'error');
  }
}

async function cleanupTestRoom(roomId) {
  const room = state.rooms.find(r => r.id === roomId);
  const roomName = room?.name || roomId;
  const ok = await confirmDialog({
    title: '清理测试房间',
    message: `确认清理测试房间「${roomName}」？这会移除该临时测试聊天室。`,
    confirmText: '清理房间',
  });
  if (!ok) return;
  if (room?.status === 'running') {
    const pauseResult = await apiPost(`/api/rooms/${encodeURIComponent(roomId)}/pause`, {});
    if (!pauseResult || !pauseResult.ok) {
      toast(pauseResult?.error || '暂停测试房间失败', 'error');
      return;
    }
  }
  const result = await apiPost('/api/rooms/delete', { id: roomId });
  if (result && result.ok) {
    if (state.currentRoomId === roomId) showRoomGrid();
    await loadRooms();
    await loadConfig();
    toast('测试房间已清理', 'ok');
  } else {
    toast(result?.error || '清理失败', 'error');
  }
}

function openRoomConversation(roomId) {
  const room = state.rooms.find(r => r.id === roomId);
  if (!room) return;

  state.currentRoomId = roomId;
  state.chatView = 'conversation';
  state.roomViewSeq += 1;
  const seq = state.roomViewSeq;

  const gridView = $('#roomGridView');
  const convView = $('#roomConvView');
  if (gridView) {
    gridView.style.opacity = '0';
    gridView.style.transform = 'translateY(4px)';
    gridView.style.transition = 'opacity 0.15s ease, transform 0.15s ease';
    setTimeout(() => { gridView.style.display = 'none'; }, 150);
  }
  if (convView) {
    convView.style.display = 'block';
    convView.style.opacity = '0';
    convView.style.transform = 'translateY(4px)';
    requestAnimationFrame(() => {
      convView.style.transition = 'opacity 0.25s ease, transform 0.25s var(--ease-out-expo)';
      convView.style.opacity = '1';
      convView.style.transform = 'translateY(0)';
    });
  }

  const convName = $('#convRoomName');
  if (convName) convName.textContent = room.name || room.id;

  // Show agent badges
  const agents = state.agents || [];
  const order = room.order && room.order.length ? room.order : room.agents;
  const badges = $('#convAgentBadges');
  if (badges) {
    badges.innerHTML = order.map(aid => {
      const a = agents.find(x => x.id === aid);
      if (!a) return '';
      return `<span class="conv-agent-badge"><span class="dot" style="background:${a.color}"></span>${esc(a.display_name || aid)}</span>`;
    }).join('');
  }
  const controls = $('#roomRunControls');
  if (controls) {
    const running = room.status === 'running';
    controls.innerHTML = `
      <button class="scan-btn" onclick="setRoomRunning('${escAttr(room.id)}', ${running ? 'false' : 'true'})">${running ? '暂停' : '开始'}</button>
      <button class="scan-btn" onclick="tickRoom('${escAttr(room.id)}')">Tick</button>
    `;
  }

  state.messages = [];
  state.roomLogs = [];
  renderSkeletonChat();
  renderRoomLogs({ loading: true });
  renderRoomTurnStatus({loading: true});
  updateStats();

  // Populate compose select with room agents only
  const sel = $('#composeAgent');
  if (sel) {
    sel.innerHTML = order.map(aid => {
      const a = agents.find(x => x.id === aid);
      return `<option value="${escAttr(aid)}">${esc(a ? a.display_name : aid)}</option>`;
    }).join('');
  }

  loadRoomMessages(roomId, seq);
}

async function setRoomRunning(roomId, running) {
  const action = running ? 'start' : 'pause';
  const result = await apiPost(`/api/rooms/${encodeURIComponent(roomId)}/${action}`, {});
  if (result && result.ok) {
    toast(running ? '聊天室已开始运行' : '聊天室已暂停', 'ok');
    await loadRooms();
    if (state.currentRoomId === roomId) {
      await loadRoomMessages(roomId);
    }
  } else if (result) {
    toast(result.error || '操作失败', 'error');
  }
}

async function tickRoom(roomId) {
  const result = await apiPost(`/api/rooms/${encodeURIComponent(roomId)}/tick`, {force: true});
  if (result && result.ok) {
    await loadRooms();
    if (state.currentRoomId === roomId) {
      await loadRoomMessages(roomId);
    }
  } else if (result) {
    toast(result.result?.error || result.error || 'Tick 失败', 'error');
  }
}

function showRoomGrid() {
  state.roomViewSeq += 1;
  state.currentRoomId = null;
  state.chatView = 'grid';
  const gridView = $('#roomGridView');
  const convView = $('#roomConvView');
  if (convView) {
    convView.style.opacity = '0';
    convView.style.transform = 'translateY(4px)';
    convView.style.transition = 'opacity 0.15s ease, transform 0.15s ease';
    setTimeout(() => { convView.style.display = 'none'; }, 150);
  }
  if (gridView) {
    gridView.style.display = 'block';
    gridView.style.opacity = '0';
    gridView.style.transform = 'translateY(4px)';
    requestAnimationFrame(() => {
      gridView.style.transition = 'opacity 0.25s ease, transform 0.25s var(--ease-out-expo)';
      gridView.style.opacity = '1';
      gridView.style.transform = 'translateY(0)';
    });
  }
}

if (backToRoomsBtn) {
  backToRoomsBtn.addEventListener('click', showRoomGrid);
}

async function loadRoomMessages(roomId = state.currentRoomId, seq = null) {
  if (!roomId) return;
  const requestSeq = seq == null ? ++state.roomViewSeq : seq;
  const encodedRoomId = encodeURIComponent(roomId);
  const [data, logsData, turnData] = await Promise.all([
    apiGet(`/api/rooms/${encodedRoomId}/messages`),
    apiGet(`/api/rooms/${encodedRoomId}/logs`),
    apiGet(`/api/rooms/${encodedRoomId}/turn`),
  ]);
  if ((!data || !data.ok) && (!logsData || !logsData.ok) && (!turnData || !turnData.ok)) {
    if (state.currentRoomId === roomId && state.roomViewSeq === requestSeq && state.messages.length === 0 && state.roomLogs.length === 0) {
      renderMessages();
      renderRoomLogs();
      renderRoomTurnStatus();
      updateStats();
    }
    return;
  }
  if (state.currentRoomId !== roomId || state.roomViewSeq !== requestSeq) return;
  const q = (state.searchQuery || '').toLowerCase();
  state.messages = data && data.ok ? (data.messages || []).filter(m => !q || String(m.msg || '').toLowerCase().includes(q)) : [];
  state.roomLogs = logsData && logsData.ok ? (logsData.logs || []) : [];
  state.currentTurn = turnData && turnData.ok ? turnData : null;
  renderMessages();
  renderRoomLogs();
  renderRoomTurnStatus();
  updateStats();
}

function renderRoomTurnStatus(options = {}) {
  if (!roomTurnStatus) return;
  if (options.loading) {
    roomTurnStatus.innerHTML = '<div class="room-turn-title">当前 Turn</div><div class="room-turn-value">加载中...</div>';
    return;
  }
  const data = state.currentTurn || {};
  const turn = data.current_turn || null;
  if (!turn) {
    roomTurnStatus.innerHTML = `<div class="room-turn-title">当前 Turn</div>
      <div class="room-turn-grid">
        <div class="room-turn-row"><span class="room-turn-key">status</span><span class="room-turn-value">${esc(data.status || 'paused')}</span></div>
        <div class="room-turn-row"><span class="room-turn-key">turn_index</span><span class="room-turn-value">${esc(data.turn_index ?? '-')}</span></div>
        <div class="room-turn-row"><span class="room-turn-key">waiting</span><span class="room-turn-value">无</span></div>
      </div>`;
    return;
  }
  const waiting = turn.state === 'waiting_response' && !turn.response_message_id;
  const callbackState = turn.response_message_id ? '已收到' : (waiting ? '等待 callback' : '未完成');
  const rows = [
    ['status', data.status || ''],
    ['turn_index', data.turn_index ?? ''],
    ['turn', turn.agent_id || ''],
    ['turn_id', turn.turn_id || ''],
    ['correlation_id', turn.correlation_id || ''],
    ['waiting_response', waiting ? 'true' : 'false'],
    ['callback', callbackState],
    ['timeout_at', turn.timeout_at || ''],
  ];
  roomTurnStatus.innerHTML = `<div class="room-turn-title">当前 Turn</div>
    <div class="room-turn-grid">
      ${rows.map(([k, v]) => `<div class="room-turn-row"><span class="room-turn-key">${esc(k)}</span><span class="room-turn-value">${esc(v)}</span></div>`).join('')}
    </div>`;
}

// Runtime log
function roomLogEventLabel(event) {
  const labels = {
    room_created: '创建聊天室',
    room_saved: '保存配置',
    room_started: '开始运行',
    room_paused: '暂停运行',
    message_appended: '写入消息',
    poll_tick: '轮询开始',
    poll_skipped: '跳过轮询',
    wakeup_check: '检查唤醒',
    wakeup_skipped: '未唤醒',
    no_pending_messages: '未唤醒',
    waiting_response: '等待回复',
    response_seen: '检测到回复',
    delivery_attempt: '调用 Agent',
    delivery_succeeded: '调用成功',
    delivery_failed: '调用失败',
    delivery_blocked: '无法调用',
    max_turns_reached: '达到轮次上限',
    room_error: '运行错误',
    archived: '归档记录',
  };
  return labels[event] || event || '运行事件';
}

function roomLogLevelLabel(level) {
  if (level === 'error') return '错误';
  if (level === 'warn') return '注意';
  return '信息';
}

function roomLogMessage(log) {
  const event = String(log.event || '');
  const msg = String(log.msg || '');
  if (!msg) return roomLogEventLabel(event);
  const legacy = {
    'room configuration saved': '聊天室配置已保存',
    'room status changed to running': '聊天室已开始运行',
    'room status changed to paused': '聊天室已暂停',
    'room poll tick': '开始一次聊天室轮询',
    'room is not running': '聊天室未运行，本轮不唤醒任何 Agent',
    'max_turns reached': '已达到最大轮次，聊天室自动暂停',
  };
  if (legacy[msg]) return legacy[msg];
  let m = msg.match(/^message appended from (.+)$/);
  if (m) return `已写入来自 ${m[1]} 的消息`;
  m = msg.match(/^no pending messages for (.+)$/);
  if (m) return `未唤醒 ${m[1]}：没有待处理的新消息`;
  m = msg.match(/^waiting for (.+) response$/);
  if (m) return `正在等待 ${m[1]} 回复，本轮不会继续唤醒其他 Agent`;
  m = msg.match(/^response received from (.+)$/);
  if (m) return `已检测到 ${m[1]} 的回复，下一轮将进入后续 Agent`;
  m = msg.match(/^delivering (.+) message\(s\) to (.+)$/);
  if (m) return `准备唤醒/调用 ${m[2]}，待投递消息 ${m[1]} 条`;
  return msg;
}

function renderRoomLogs(options = {}) {
  if (!roomLogArea || !roomLogCount) return;
  const logs = state.roomLogs || [];
  roomLogCount.textContent = `${logs.length} \u6761`;
  const shouldScrollToBottom = options.loading || logs.length === 0 || (
    roomLogArea.scrollTop + roomLogArea.clientHeight >= roomLogArea.scrollHeight - 48
  );
  if (!logs.length) {
    if (options.loading) {
      roomLogArea.innerHTML = '<div class="skeleton shimmer-overlay" style="height:80px;border-radius:var(--radius)"></div><div class="skeleton shimmer-overlay" style="height:60px;border-radius:var(--radius);margin-top:8px"></div><div class="skeleton shimmer-overlay" style="height:72px;border-radius:var(--radius);margin-top:8px"></div>';
    } else {
      roomLogArea.innerHTML = '<div class="room-log-empty">&#26242;&#26080;&#36816;&#34892;&#26085;&#24535;</div>';
    }
    roomLogArea.scrollTop = 0;
    return;
  }
  roomLogArea.innerHTML = logs.map((m, idx) => {
    const ts = esc(String(m.ts || ''));
    const level = String(m.level || 'info');
    const event = String(m.event || 'runtime');
    const agent = esc(String(m.agent || 'room'));
    const levelClass = level === 'error' ? 'error' : (level === 'warn' ? 'warn' : '');
    const text = esc(roomLogMessage(m));
    const meta = m.meta && typeof m.meta === 'object' ? esc(JSON.stringify(m.meta, null, 2)) : '';
    return `<div class="room-log-item" data-log-idx="${idx}">
      <div class="room-log-meta">
        <span class="room-log-pill">${ts}</span>
        <span class="room-log-pill kind ${levelClass}">${esc(roomLogLevelLabel(level))}</span>
        <span class="room-log-pill">${esc(roomLogEventLabel(event))}</span>
        <span class="room-log-pill">${agent}</span>
      </div>
      <div class="room-log-msg">${text}</div>
      ${meta ? `<details class="room-log-detail"><summary>展开详情</summary><pre>${meta}</pre></details>` : ''}
    </div>`;
  }).join('');
  if (shouldScrollToBottom) {
    roomLogArea.scrollTop = roomLogArea.scrollHeight;
  }
  const items = roomLogArea.querySelectorAll('.room-log-item');
  items.forEach((item, i) => {
    item.style.opacity = '0';
    item.style.transform = 'translateX(8px)';
    item.style.transition = `opacity 0.2s ease ${i * 0.02}s, transform 0.2s var(--ease-out-expo) ${i * 0.02}s`;
    requestAnimationFrame(() => {
      item.style.opacity = '1';
      item.style.transform = 'translateX(0)';
    });
  });
}

async function copyRoomLogs() {
  const logs = state.roomLogs || [];
  const messages = state.messages || [];
  const roomId = state.currentRoomId || '';
  const btn = document.getElementById('copyRoomLogsBtn');
  const room = (state.rooms || []).find(r => r.id === roomId);
  const roomName = room ? (room.display_name || room.name || roomId) : roomId;

  let text = `═══ 聊天室：${roomName} ═══\n`;
  text += `导出时间：${new Date().toLocaleString('zh-CN')}\n`;
  text += `消息数：${messages.length} | 日志数：${logs.length}\n`;

  // Chat messages
  if (messages.length > 0) {
    text += '\n── 消息记录 ──\n';
    for (const m of messages) {
      const ts = m.ts || '';
      const from = m.from || '';
      const to = m.to ? `→ ${m.to}` : '';
      const msg = m.msg || '';
      text += `[${ts}] ${from}${to ? ' ' + to : ''}: ${msg}\n`;
    }
  }

  // Runtime logs
  if (logs.length > 0) {
    text += '\n── 运行日志 ──\n';
    for (const log of logs) {
      const ts = log.ts || '';
      const level = log.level || 'info';
      const event = log.event || '';
      const agent = log.agent || '';
      const msg = log.msg || '';
      const metaStr = log.meta ? ' | ' + JSON.stringify(log.meta) : '';
      text += `[${ts}] [${level}] ${agent ? agent + ' ' : ''}${event}: ${msg}${metaStr}\n`;
    }
  }

  try {
    await navigator.clipboard.writeText(text);
    if (btn) {
      btn.classList.add('copied');
      btn.innerHTML = '<svg width="14" height="14" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M3 8.5l3.5 3.5L13 5"/></svg>';
      setTimeout(() => {
        btn.classList.remove('copied');
        btn.innerHTML = '<svg width="14" height="14" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"><rect x="5" y="5" width="9" height="9" rx="1.5"/><path d="M3 11V3a1.5 1.5 0 011.5-1.5H11"/></svg>';
      }, 1500);
    }
    toast(`已复制 ${messages.length} 条消息 + ${logs.length} 条日志`, 'ok');
  } catch (e) {
    toast('复制失败：' + e.message, 'error');
  }
}

function esc(s) { return String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;'); }
function escAttr(s) { return esc(s); }

// ═══ Toast notification ═══
function toast(msg, type) {
  let container = document.querySelector('.toast-container');
  if (!container) {
    container = document.createElement('div');
    container.className = 'toast-container';
    document.body.appendChild(container);
  }
  const el = document.createElement('div');
  el.className = `toast-item ${type === 'error' ? 'error' : type === 'ok' ? 'success' : type === 'warning' ? 'warning' : ''}`;
  el.textContent = msg;
  container.appendChild(el);
  const dismiss = () => {
    if (el.parentElement) {
      el.classList.add('removing');
      setTimeout(() => { if (el.parentElement) el.remove(); }, 260);
    }
  };
  const timer = setTimeout(dismiss, 3500);
  el.addEventListener('click', () => { clearTimeout(timer); dismiss(); });
  if (container.children.length > 4) {
    const first = container.firstElementChild;
    if (first) { first.classList.add('removing'); setTimeout(() => { if (first.parentElement) first.remove(); }, 260); }
  }
}

function confirmDialog({title, message, confirmText = '删除', cancelText = '取消'} = {}) {
  return new Promise(resolve => {
    const overlay = document.createElement('div');
    overlay.className = 'test-modal-overlay';
    overlay.innerHTML = `<div class="test-modal confirm-modal" role="dialog" aria-modal="true" aria-labelledby="confirmTitle">
      <div class="confirm-modal-title" id="confirmTitle">${esc(title || '确认操作')}</div>
      <div class="confirm-modal-detail">${esc(message || '')}</div>
      <div class="confirm-modal-actions">
        <button class="test-modal-close" type="button" data-confirm-cancel>${esc(cancelText)}</button>
        <button class="confirm-danger-btn" type="button" data-confirm-ok>${esc(confirmText)}</button>
      </div>
    </div>`;
    document.body.appendChild(overlay);

    const cancelBtn = overlay.querySelector('[data-confirm-cancel]');
    const okBtn = overlay.querySelector('[data-confirm-ok]');
    const close = result => {
      document.removeEventListener('keydown', onKeyDown);
      overlay.remove();
      resolve(result);
    };
    const onKeyDown = e => {
      if (e.key === 'Escape') close(false);
      if (e.key === 'Enter') close(true);
    };

    cancelBtn.addEventListener('click', () => close(false));
    okBtn.addEventListener('click', () => close(true));
    overlay.addEventListener('click', e => { if (e.target === overlay) close(false); });
    document.addEventListener('keydown', onKeyDown);
    okBtn.focus();
  });
}

function bodyTemplateToText(bt) {
  if (!bt) return '{}';
  if (typeof bt === 'string') return bt;
  try { return JSON.stringify(bt, null, 2); } catch(e) { return String(bt); }
}

function textToBodyTemplate(text) {
  if (!text || !text.trim()) return {};
  const t = text.trim();
  try { return JSON.parse(t); } catch(e) { return t; }
}

function headersToArray(h) {
  if (!h || typeof h !== 'object') return [];
  return Object.entries(h).map(([k, v]) => ({k, v}));
}

function arrayToHeaders(arr) {
  const h = {};
  for (const item of arr) {
    if (item.k && item.k.trim()) h[item.k.trim()] = item.v;
  }
  return h;
}

function agentTokenKey(agentId) {
  const key = String(agentId || '').trim().replace(/[^a-zA-Z0-9_-]/g, '').replace(/^-+|-+$/g, '');
  return key || 'agent';
}

function agentTokenEnv(agentId) {
  return `${agentTokenKey(agentId).replace(/-/g, '_').toUpperCase()}_TOKEN`;
}

function agentTokenPath(agentId) {
  return `~/.${agentTokenKey(agentId).toLowerCase()}/token.json`;
}

function updateAgentTokenPlaceholders(card) {
  if (!card) return;
  const agentId = card.querySelector('.ag-id')?.value.trim() || '';
  const tokenPath = card.querySelector('.wu-token-path');
  const tokenEnv = card.querySelector('.wu-token-env');
  if (tokenPath) tokenPath.placeholder = agentTokenPath(agentId);
  if (tokenEnv) tokenEnv.placeholder = agentTokenEnv(agentId);
}

function updateAgentColor(input) {
  const card = input.closest('.agent-config');
  const value = input.value.trim();
  const preview = card?.querySelector('.color-preview');
  const picker = card?.querySelector('.agent-color-picker');
  const dot = card?.querySelector('.agent-config-name .dot');
  if (/^#[0-9a-fA-F]{6}$/.test(value)) {
    if (preview) preview.style.background = value;
    if (picker) picker.value = value;
    if (dot) dot.style.background = value;
  }
}

function agentTypeFromAdapter(adapter, wakeup) {
  const type = adapter?.type || (wakeup?.url ? 'native_http' : 'manual');
  if (type === 'openclaw_sessions') return 'openclaw';
  if (type === 'native_http') return 'http';
  return 'manual';
}

function responseModeLabel(mode) {
  if (mode === 'callback') return '自动回写';
  if (mode === 'pull_session') return '读取会话';
  if (mode === 'sync') return '同步返回';
  return '手动记录';
}

function agentConfigSummary(agent) {
  const adapter = agent.adapter || adapterFromWakeup(agent.wakeup || {});
  const type = agentTypeFromAdapter(adapter, agent.wakeup || {});
  const response = adapter.response || {};
  if (type === 'openclaw') {
    return `OpenClaw 将通过本地网关被唤醒，回复将${responseModeLabel(response.mode || 'callback')} Agent Bridge。`;
  }
  if (type === 'http') {
    return `Webhook 将通过配置的地址被唤醒，回复将${responseModeLabel(response.mode || 'callback')} Agent Bridge。`;
  }
  return '该 Agent 需要手动处理消息，不会自动唤醒外部程序。';
}

function renderAgentSimpleFields(a) {
  const agent = a || {};
  const adapter = agent.adapter || adapterFromWakeup(agent.wakeup || {});
  const cfg = adapter.config || {};
  const type = agentTypeFromAdapter(adapter, agent.wakeup || {});
  const sessionsKey = cfg.sessions_key || cfg.sessionsKey || 'agent:main:main';
  const health = agent.capability?.health || agent.health || '';
  const connected = health && health !== 'missing_config' && health !== 'manual';
  const stateText = type === 'manual' ? '手动连接' : (connected ? '已配置' : '待确认');
  return `<div class="agent-simple-card">
    <div class="form-grid">
      <div class="wakeup-field">
        <label>显示名称</label>
        <input type="text" class="ag-name" value="${escAttr(agent.display_name || agent.id || '')}" placeholder="例如 OpenClaw" />
      </div>
      <div class="wakeup-field">
        <label>唯一标识</label>
        <input type="text" class="ag-id" value="${escAttr(agent.id || '')}" placeholder="${type==='openclaw'?'openclaw':'例如 momo'}" oninput="updateAgentTokenPlaceholders(this.closest('.agent-config')); updateAgentSummary(this.closest('.agent-config'))" />
        <div class="field-hint">用于在聊天室里识别这个 Agent，只能用字母、数字、-、_。</div>
      </div>
      <div class="wakeup-field">
        <label>Agent 类型</label>
        <select class="agent-type-select" onchange="agentTypeChange(this)">
          <option value="openclaw" ${type==='openclaw'?'selected':''}>OpenClaw</option>
          <option value="http" ${type==='http'?'selected':''}>Webhook</option>
          <option value="manual" ${type==='manual'?'selected':''}>手动</option>
        </select>
      </div>
      <div class="wakeup-field">
        <label>连接状态</label>
        <input type="text" class="agent-connection-state" value="${escAttr(stateText)}" readonly />
      </div>
    </div>
    <div class="openclaw-card ${type==='openclaw'?'':'hidden'}">
      <div class="openclaw-card-title">OpenClaw 配置</div>
      <div class="wakeup-field">
        <label>OpenClaw 会话</label>
        <input type="text" class="simple-sessions-key" value="${escAttr(sessionsKey)}" placeholder="agent:main:main" oninput="syncSimpleSessionsKey(this)" />
      </div>
      <div class="openclaw-card-meta">已自动使用本地网关、认证文件和自动回写。通常只需要保持 agent:main:main，不确定就不要改。</div>
    </div>
    <div class="agent-summary">${esc(agentConfigSummary(agent))}</div>
    <div class="simple-actions">
      <button class="scan-btn test-agent-btn" onclick="testAgent(this)">测试连接</button>
      <button class="scan-btn test-agent-btn" onclick="runAgentIntegrationTest(this)">发送测试消息</button>
    </div>
  </div>`;
}

function pickAgentColor(input) {
  const card = input.closest('.agent-config');
  const colorInput = card?.querySelector('.ag-color');
  if (!colorInput) return;
  colorInput.value = input.value;
  updateAgentColor(colorInput);
}

function renderAgentBaseFields(a) {
  const agent = a || {};
  const color = agent.color || '#8888a0';
  return `<div class="form-grid">
    <div class="wakeup-field">
      <label>颜色</label>
      <div class="agent-color-control">
        <span class="color-preview" style="background:${color}"></span>
        <input type="text" class="ag-color" value="${color}" placeholder="#rrggbb" oninput="updateAgentColor(this)" />
        <input type="color" class="agent-color-picker" value="${color}" onchange="pickAgentColor(this)" title="选择颜色" />
      </div>
    </div>
  </div>`;
}

function renderAgentAdvancedHtml(a, idx) {
  const agent = a || {};
  const adapterType = agent.adapter?.type || '';
  const devOpen = adapterType && adapterType !== 'openclaw_sessions' && adapterType !== 'manual' ? ' open' : '';
  return `<details class="advanced-agent-settings">
    <summary>高级设置</summary>
    ${renderAgentBaseFields(agent)}
  </details>
  <details class="developer-agent-settings"${devOpen}>
    <summary>开发者配置</summary>
    <div class="form-grid">
      <div class="wakeup-field">
        <label>游标类型</label>
        <select class="ag-cursor">
          <option value="line" ${(agent.cursor||'line')==='line'?'selected':''}>按行记录</option>
          <option value="timestamp" ${agent.cursor==='timestamp'?'selected':''}>按时间记录</option>
        </select>
      </div>
    </div>
    ${renderAdapterHtml(agent.adapter, agent.wakeup, idx)}
    ${renderWakeupHtml(agent.wakeup, idx, agent.id || '')}
  </details>`;
}

function adapterFromWakeup(wu) {
  const wakeup = wu || {};
  if (!wakeup.url) return {type: 'manual', config: {}, auth: {}, response: {mode: 'manual', timeout_seconds: 180}};
  return {
    type: 'native_http',
    config: {
      url: wakeup.url || '',
      method: wakeup.method || 'POST',
      headers: wakeup.headers || {},
    },
    auth: wakeup.auth || {},
    template: wakeup.body_template || {"message": "{{message}}"},
    response: {mode: 'callback', timeout_seconds: 180},
  };
}

function renderAdapterHtml(adapter, wakeup, idx) {
  const data = adapter && adapter.type ? adapter : adapterFromWakeup(wakeup);
  const type = data.type || 'manual';
  const cfg = data.config || {};
  const auth = data.auth || {};
  const response = data.response || {};
  const authType = auth.type === 'bearer' ? 'bearer' : 'none';
  const responseMode = response.mode || (type === 'openclaw_sessions' ? 'callback' : 'callback');
  const timeoutSeconds = response.timeout_seconds ?? 180;
  const tokenPath = auth.token_path || '';
  const tokenJsonpath = auth.token_jsonpath || '';
  const tokenEnv = auth.token_env || '';
  const typeControl = type === 'openclaw_sessions'
    ? `<input type="hidden" class="ad-type" value="openclaw_sessions" /><input type="text" value="OpenClaw 专属连接" readonly />`
    : `<select class="ad-type" onchange="adapterTypeChange(this)">
        <option value="manual" ${type==='manual'?'selected':''}>手动</option>
        <option value="native_http" ${type==='native_http'?'selected':''}>Webhook</option>
      </select>`;
  return `<div class="adapter-section" data-agent-idx="${idx}">
    <div class="wakeup-label">底层适配配置</div>
    <div class="wakeup-field">
      <label>连接方式</label>
      ${typeControl}
    </div>
    <div class="adapter-fields ad-http-fields ${type==='manual'?'hidden':''}">
      <div class="wakeup-field">
        <label>调用地址</label>
        <input type="text" class="ad-url" value="${escAttr(cfg.url || '')}" placeholder="http://127.0.0.1:18789/tools/invoke" />
      </div>
      <div class="wakeup-field ad-openclaw-only ${type==='openclaw_sessions'?'':'hidden'}">
        <label>OpenClaw 会话</label>
        <input type="text" class="ad-sessions-key" value="${escAttr(cfg.sessions_key || cfg.sessionsKey || '')}" placeholder="agent:main:main" />
      </div>
      <div class="wakeup-field auth-section">
        <label>认证方式</label>
        <select class="ad-auth-type" onchange="authTypeChange(this)">
          <option value="none" ${authType==='none'?'selected':''}>无</option>
          <option value="bearer" ${authType==='bearer'?'selected':''}>Bearer Token</option>
        </select>
        <div class="auth-fields ${authType==='bearer'?'':'hidden'}">
          <label style="font-size:0.72em;color:var(--text3);margin-top:4px;display:block">Token 文件路径</label>
          <input type="text" class="ad-token-path" value="${escAttr(tokenPath)}" placeholder="~/.openclaw/openclaw.json" />
          <label style="font-size:0.72em;color:var(--text3);margin-top:4px;display:block">Token 自动提取路径</label>
          <input type="text" class="ad-token-jsonpath" value="${escAttr(tokenJsonpath)}" placeholder="gateway.auth.token" />
          <label style="font-size:0.72em;color:var(--text3);margin-top:4px;display:block">Token 环境变量</label>
          <input type="text" class="ad-token-env" value="${escAttr(tokenEnv)}" placeholder="OPENCLAW_TOKEN" />
        </div>
      </div>
      <div class="wakeup-field">
        <label>回复接收方式</label>
        <select class="ad-response-mode">
          <option value="callback" ${responseMode==='callback'?'selected':''}>自动回写</option>
          <option value="pull_session" ${responseMode==='pull_session'?'selected':''}>读取会话</option>
          <option value="sync" ${responseMode==='sync'?'selected':''}>同步返回</option>
          <option value="manual" ${responseMode==='manual'?'selected':''}>手动记录</option>
        </select>
      </div>
      <div class="wakeup-field">
        <label>等待回复超时</label>
        <input type="number" class="ad-timeout-seconds" value="${escAttr(timeoutSeconds)}" min="1" step="1" />
      </div>
    </div>
  </div>`;
}

function adapterTypeChange(sel) {
  const section = sel.closest('.adapter-section');
  if (!section) return;
  const type = sel.value;
  const fields = section.querySelector('.ad-http-fields');
  const openclaw = section.querySelector('.ad-openclaw-only');
  if (fields) fields.classList.toggle('hidden', type === 'manual');
  if (openclaw) openclaw.classList.toggle('hidden', type !== 'openclaw_sessions');
  if (type === 'openclaw_sessions') {
    const url = section.querySelector('.ad-url');
    const sessionsKey = section.querySelector('.ad-sessions-key');
    const mode = section.querySelector('.ad-response-mode');
    if (url && !url.value.trim()) url.value = 'http://127.0.0.1:18789/tools/invoke';
    if (sessionsKey && !sessionsKey.value.trim()) sessionsKey.value = 'agent:main:main';
    if (mode) mode.value = 'callback';
  }
}

function ensureOpenClawDefaults(card) {
  if (!card) return;
  const adType = card.querySelector('.ad-type');
  if (adType) adType.value = 'openclaw_sessions';
  const idInput = card.querySelector('.ag-id');
  if (idInput && !idInput.value.trim()) idInput.value = nextAgentId('openclaw');
  const fields = card.querySelector('.ad-http-fields');
  if (fields) fields.classList.remove('hidden');
  const openclaw = card.querySelector('.ad-openclaw-only');
  if (openclaw) openclaw.classList.remove('hidden');
  const url = card.querySelector('.ad-url');
  if (url && !url.value.trim()) url.value = 'http://127.0.0.1:18789/tools/invoke';
  const sessionsKey = card.querySelector('.ad-sessions-key');
  const simpleKey = card.querySelector('.simple-sessions-key');
  const key = simpleKey?.value.trim() || sessionsKey?.value.trim() || 'agent:main:main';
  if (sessionsKey) sessionsKey.value = key;
  if (simpleKey) simpleKey.value = key;
  const mode = card.querySelector('.ad-response-mode');
  if (mode) mode.value = 'callback';
  const timeout = card.querySelector('.ad-timeout-seconds');
  if (timeout && !timeout.value) timeout.value = '180';
  updateAgentTokenPlaceholders(card);
}

function agentTypeChange(sel) {
  const card = sel.closest('.agent-config');
  if (!card) return;
  const type = sel.value;
  const openclaw = card.querySelector('.openclaw-card');
  if (openclaw) openclaw.classList.toggle('hidden', type !== 'openclaw');
  const state = card.querySelector('.agent-connection-state');
  if (state) state.value = type === 'manual' ? '手动连接' : '待确认';
  const adType = card.querySelector('.ad-type');
  if (type === 'openclaw') {
    ensureOpenClawDefaults(card);
  } else if (adType) {
    adType.value = type === 'http' ? 'native_http' : 'manual';
    adapterTypeChange(adType);
  }
  updateAgentSummary(card);
}

function syncSimpleSessionsKey(input) {
  const card = input.closest('.agent-config');
  const target = card?.querySelector('.ad-sessions-key');
  if (target) target.value = input.value;
  updateAgentSummary(card);
}

function nextAgentId(base) {
  const existing = new Set(Array.from(document.querySelectorAll('.agent-config .ag-id'))
    .map(input => input.value.trim())
    .filter(Boolean));
  if (!existing.has(base)) return base;
  for (let i = 2; i < 100; i++) {
    const candidate = `${base}-${i}`;
    if (!existing.has(candidate)) return candidate;
  }
  return `${base}-${Date.now()}`;
}

function updateAgentSummary(card, saved=false) {
  const summary = card?.querySelector('.agent-summary');
  if (!summary) return;
  const data = collectAgentData(card);
  summary.textContent = agentConfigSummary(data);
  summary.classList.toggle('saved', !!saved);
}

function renderWakeupHtml(wu, idx, agentId='') {
  const wuData = wu || {};
  const url = wuData.url || '';
  const method = wuData.method || 'POST';
  const auth = wuData.auth || {};
  const authType = auth.type || 'none';
  const tokenPath = auth.token_path || '';
  const tokenJsonpath = auth.token_jsonpath || '';
  const tokenEnv = auth.token_env || '';
  const headers = headersToArray(wuData.headers);
  const bodyText = bodyTemplateToText(wuData.body_template);
  
  let headerHtml = '';
  for (let hi = 0; hi < headers.length; hi++) {
    headerHtml += `<div class="kv-row">
      <input type="text" class="header-key" value="${escAttr(headers[hi].k)}" data-idx="${idx}" placeholder="Key" />
      <input type="text" class="header-val" value="${escAttr(headers[hi].v)}" data-idx="${idx}" placeholder="Value" />
      <button class="kv-del" onclick="this.parentElement.remove()" title="删除">✕</button>
    </div>`;
  }

  return `<div class="wakeup-section" data-agent-idx="${idx}">
    <div class="wakeup-label-row">
      <div class="wakeup-label">兼容旧版 wakeup 配置（普通用户无需修改）</div>
      <button class="scan-btn test-agent-btn" onclick="testAgent(this)">测试对话</button>
    </div>
    <div class="wakeup-field">
      <label>Webhook URL</label>
      <input type="text" class="wu-url" value="${escAttr(url)}" placeholder="http://127.0.0.1:8644/webhooks/..." />
    </div>
    <div class="wakeup-field">
      <label>请求方法</label>
      <select class="wu-method">
        <option value="POST" ${method==='POST'?'selected':''}>POST</option>
        <option value="GET" ${method==='GET'?'selected':''}>GET</option>
        <option value="PUT" ${method==='PUT'?'selected':''}>PUT</option>
      </select>
    </div>
    <div class="wakeup-field auth-section">
      <label>认证方式</label>
      <select class="wu-auth-type" onchange="authTypeChange(this)">
        <option value="none" ${authType==='none'?'selected':''}>无</option>
        <option value="bearer" ${authType==='bearer'?'selected':''}>Bearer Token</option>
      </select>
      <div class="auth-fields ${authType==='bearer'?'':'hidden'}">
        <label style="font-size:0.72em;color:var(--text3);margin-top:4px;display:block">Token 文件路径</label>
        <input type="text" class="wu-token-path" value="${escAttr(tokenPath)}" placeholder="${escAttr(agentTokenPath(agentId))}" />
        <label style="font-size:0.72em;color:var(--text3);margin-top:4px;display:block">JSONPath 提取</label>
        <input type="text" class="wu-token-jsonpath" value="${escAttr(tokenJsonpath)}" placeholder="gateway.auth.password" />
        <label style="font-size:0.72em;color:var(--text3);margin-top:4px;display:block">Token 环境变量</label>
        <input type="text" class="wu-token-env" value="${escAttr(tokenEnv)}" placeholder="${escAttr(agentTokenEnv(agentId))}" />
      </div>
    </div>
    <div class="wakeup-field">
      <label>请求头</label>
      <div class="wu-headers">${headerHtml || '<div class="kv-row"><input type="text" class="header-key" placeholder="Key"/><input type="text" class="header-val" placeholder="Value"/></div>'}</div>
      <button class="add-header-btn" onclick="addHeader(this, ${idx})">＋ 添加请求头</button>
    </div>
    <div class="wakeup-field">
      <label>消息体模板 (JSON)</label>
      <select class="wu-body-preset" onchange="applyBodyPreset(this)" style="margin-bottom:4px">
        <option value="">-- 选择模板 --</option>
        <option value='{"message": "{{message}}"}'>Hermes (简单消息)</option>
        <option value='{"message": "[{{from}}] {{message}}"}'>Hermes (带来源)</option>
        <option value='{"tool": "sessions_send", "args": {"sessionKey": "agent:main:main", "message": "[消息通道·{{from}}] {{message}}"}}'>OpenClaw (sessions_send)</option>
        <option value='{"text": "{{message}}", "from": "{{from}}"}'>通用 JSON</option>
      </select>
      <textarea class="wu-body body-editor" rows="3">${escAttr(bodyText)}</textarea>
    </div>
  </div>`;
}

function applyBodyPreset(selectEl) {
  const val = selectEl.value;
  if (!val) return; // '-- 选择模板 --' selected, no action
  const textarea = selectEl.parentElement.querySelector('.wu-body');
  if (!textarea) return;
  try {
    const obj = JSON.parse(val);
    textarea.value = JSON.stringify(obj, null, 2);
  } catch(e) {
    textarea.value = val;
  }
}

function authTypeChange(sel) {
  const fields = sel.parentElement.querySelector('.auth-fields');
  if (sel.value === 'bearer') { fields.classList.remove('hidden'); }
  else { fields.classList.add('hidden'); }
}

function addHeader(btn, idx) {
  const container = btn.previousElementSibling;
  const row = document.createElement('div');
  row.className = 'kv-row';
  row.innerHTML = '<input type="text" class="header-key" placeholder="Key"/><input type="text" class="header-val" placeholder="Value"/><button class="kv-del" onclick="this.parentElement.remove()" title="删除">✕</button>';
  container.appendChild(row);
}

function collectAdapterData(container, wakeup) {
  const section = container.querySelector('.adapter-section');
  if (!section) return adapterFromWakeup(wakeup);
  const uiType = container.querySelector('.agent-type-select')?.value || '';
  const type = uiType === 'openclaw'
    ? 'openclaw_sessions'
    : (uiType === 'http' ? 'native_http' : (uiType === 'manual' ? 'manual' : (section.querySelector('.ad-type')?.value || 'manual')));
  if (type === 'manual') return {type: 'manual', config: {}, auth: {}, response: {mode: 'manual', timeout_seconds: 180}};

  const url = section.querySelector('.ad-url')?.value.trim() || (type === 'openclaw_sessions' ? 'http://127.0.0.1:18789/tools/invoke' : '');
  const authType = section.querySelector('.ad-auth-type')?.value || 'none';
  const tokenPath = section.querySelector('.ad-token-path')?.value.trim() || '';
  const tokenJsonpath = section.querySelector('.ad-token-jsonpath')?.value.trim() || '';
  const tokenEnv = section.querySelector('.ad-token-env')?.value.trim() || '';
  const responseMode = section.querySelector('.ad-response-mode')?.value || 'callback';
  const timeoutRaw = section.querySelector('.ad-timeout-seconds')?.value || '180';
  const timeoutSeconds = Math.max(1, parseInt(timeoutRaw, 10) || 180);
  const auth = {};
  if (authType === 'bearer' && tokenPath) {
    auth.type = 'bearer';
    auth.token_path = tokenPath;
    if (tokenJsonpath) auth.token_jsonpath = tokenJsonpath;
  } else if (authType === 'bearer' && tokenEnv) {
    auth.type = 'bearer';
    auth.token_env = tokenEnv;
  }

  if (type === 'openclaw_sessions') {
    const sessionsKey = container.querySelector('.simple-sessions-key')?.value.trim()
      || section.querySelector('.ad-sessions-key')?.value.trim()
      || 'agent:main:main';
    return {
      type,
      config: {url, sessions_key: sessionsKey, timeout: 60},
      auth,
      response: {mode: 'callback', timeout_seconds: timeoutSeconds},
    };
  }

  return {
    type: 'native_http',
    config: {url, method: wakeup.method || 'POST', headers: wakeup.headers || {}},
    auth,
    template: wakeup.body_template || {"message": "{{message}}"},
    response: {mode: responseMode, timeout_seconds: timeoutSeconds},
  };
}

function wakeupForAdapter(adapter, fallbackWakeup) {
  const type = adapter?.type || 'manual';
  if (type === 'native_http') {
    const cfg = adapter.config || {};
    const wakeup = {
      url: cfg.url || '',
      method: cfg.method || fallbackWakeup.method || 'POST',
      headers: cfg.headers || fallbackWakeup.headers || {"Content-Type": "application/json"},
      body_template: adapter.template || fallbackWakeup.body_template || {"message": "{{message}}"},
    };
    if (adapter.auth && adapter.auth.type) wakeup.auth = adapter.auth;
    return wakeup;
  }
  if (type === 'openclaw_sessions') {
    const cfg = adapter.config || {};
    const wakeup = {
      url: cfg.url || '',
      method: 'POST',
      headers: {"Content-Type": "application/json"},
      body_template: {
        tool: "sessions_send",
        args: {sessionKey: cfg.sessions_key || cfg.sessionsKey || '', message: "{{message}}"},
      },
    };
    if (adapter.auth && adapter.auth.type) wakeup.auth = adapter.auth;
    return wakeup;
  }
  return fallbackWakeup;
}

function collectAgentData(container) {
  const idx = container.dataset.agentIdx;
  const oldId = container.dataset.agentId || '';
  const id = container.querySelector('.ag-id').value.trim();
  const displayName = container.querySelector('.ag-name').value.trim();
  const color = container.querySelector('.ag-color').value.trim();
  const cursor = container.querySelector('.ag-cursor').value;

  const wuSection = container.querySelector('.wakeup-section');
  const url = wuSection ? wuSection.querySelector('.wu-url').value.trim() : '';
  const method = wuSection ? wuSection.querySelector('.wu-method').value : 'POST';
  const authType = wuSection ? wuSection.querySelector('.wu-auth-type').value : 'none';
  const tokenPath = wuSection ? wuSection.querySelector('.wu-token-path')?.value.trim() || '' : '';
  const tokenJsonpath = wuSection ? wuSection.querySelector('.wu-token-jsonpath')?.value.trim() || '' : '';
  const tokenEnv = wuSection ? wuSection.querySelector('.wu-token-env')?.value.trim() || '' : '';
  
  const headers = {};
  if (wuSection) {
    wuSection.querySelectorAll('.kv-row').forEach(row => {
      const k = row.querySelector('.header-key')?.value.trim();
      const v = row.querySelector('.header-val')?.value.trim();
      if (k) headers[k] = v || '';
    });
  }
  
  const bodyText = wuSection ? wuSection.querySelector('.wu-body')?.value || '' : '';
  const bodyTemplate = textToBodyTemplate(bodyText);
  
  const wakeup = {url, method, headers, body_template: bodyTemplate};
  if (authType === 'bearer' && tokenPath) {
    wakeup.auth = {type: 'bearer', token_path: tokenPath, token_jsonpath: tokenJsonpath};
  } else if (authType === 'bearer' && tokenEnv) {
    wakeup.auth = {type: 'bearer', token_env: tokenEnv};
  }

  const adapter = collectAdapterData(container, wakeup);
  const effectiveWakeup = wakeupForAdapter(adapter, wakeup);
  const data = {id, display_name: displayName || id, color, cursor, filter_from: "", wakeup: effectiveWakeup, adapter};
  if (oldId && oldId !== id) data.old_id = oldId;
  return data;
}

async function testAgent(btn) {
  const card = btn.closest('.agent-config');
  if (!card) return;
  // Clear previous field errors
  card.querySelectorAll('.field-error').forEach(el => el.classList.remove('field-error'));
  const data = collectAgentData(card);
  if (!data.wakeup || !data.wakeup.url) {
    toast('请先配置连接地址', 'error');
    const urlField = card.querySelector('.wu-url');
    if (urlField) {
      urlField.classList.add('field-error');
      urlField.scrollIntoView({behavior:'smooth', block:'center'});
      urlField.focus();
    }
    return;
  }
  const agentName = data.display_name || data.id || 'Agent';
  const adapterType = data.adapter?.type || 'manual';
  const testingText = adapterType === 'openclaw_sessions'
    ? '正在测试本地 OpenClaw 网关是否可达…'
    : (adapterType === 'manual' ? '正在检查手动 Agent 配置…' : '正在测试 Webhook 是否可达…');
  btn.disabled = true;

  const overlay = document.createElement('div');
  overlay.className = 'test-modal-overlay';
  overlay.innerHTML = `<div class="test-modal">
    <div class="test-modal-icon loading"></div>
    <div class="test-modal-title">正在测试 ${esc(agentName)}</div>
    <div class="test-modal-detail">${esc(testingText)}</div>
  </div>`;
  document.body.appendChild(overlay);

  const res = await apiPost('/api/agent/test', {wakeup: data.wakeup, adapter: data.adapter});
  const modal = overlay.querySelector('.test-modal');
  const icon = modal.querySelector('.test-modal-icon');
  const title = modal.querySelector('.test-modal-title');
  const detail = modal.querySelector('.test-modal-detail');

  if (res && res.ok) {
    const state = card.querySelector('.agent-connection-state');
    if (state) state.value = '连接正常';
    icon.className = 'test-modal-icon success';
    icon.textContent = '✓';
    title.textContent = '连接可达';
    detail.className = 'test-modal-detail success';
    detail.textContent = agentName + ' 的连接入口响应正常（' + (res.status || '') + '）。这不代表 callback 已完成回写。';
  } else {
    const state = card.querySelector('.agent-connection-state');
    if (state) state.value = '连接失败';
    icon.className = 'test-modal-icon failure';
    icon.textContent = '✗';
    title.textContent = '连通失败';
    detail.className = 'test-modal-detail failure';
    detail.textContent = res?.error || '请求失败，请检查配置';
  }
  modal.insertAdjacentHTML('beforeend',
    '<div class="test-modal-actions"><button class="test-modal-close">关闭</button></div>');
  const close = () => { overlay.remove(); btn.disabled = false; };
  modal.querySelector('.test-modal-close').onclick = close;
  overlay.onclick = e => { if (e.target === overlay) close(); };
}

async function runAgentIntegrationTest(btn) {
  const card = btn.closest('.agent-config');
  if (!card) return;
  const data = collectAgentData(card);
  const agentName = data.display_name || data.id || 'Agent';
  if (!data.id) {
    setAgentCardStatus(card, '请先填写唯一标识并保存 Agent', 'error');
    return;
  }
  btn.disabled = true;
  const openOverlay = (titleText, detailText) => {
    const overlay = document.createElement('div');
    overlay.className = 'test-modal-overlay';
    overlay.innerHTML = `<div class="test-modal">
      <div class="test-modal-icon loading"></div>
      <div class="test-modal-title">${esc(titleText)}</div>
      <div class="test-modal-detail">${esc(detailText)}</div>
    </div>`;
    document.body.appendChild(overlay);
    const modal = overlay.querySelector('.test-modal');
    const icon = modal.querySelector('.test-modal-icon');
    const title = modal.querySelector('.test-modal-title');
    const detail = modal.querySelector('.test-modal-detail');
    const finish = (ok, titleText2, detailText2) => {
      icon.className = 'test-modal-icon ' + (ok ? 'success' : 'failure');
      icon.textContent = ok ? '✓' : '✗';
      title.textContent = titleText2;
      detail.className = 'test-modal-detail ' + (ok ? 'success' : 'failure');
      detail.textContent = detailText2;
      modal.insertAdjacentHTML('beforeend', '<div class="test-modal-actions"><button class="test-modal-close">关闭</button></div>');
      const close = () => { overlay.remove(); btn.disabled = false; };
      modal.querySelector('.test-modal-close').onclick = close;
      overlay.onclick = e => { if (e.target === overlay) close(); };
    };
    const close = () => { overlay.remove(); btn.disabled = false; };
    return {overlay, detail, finish, close};
  };
  const runTest = async payload => {
    btn.disabled = true;
    const ui = openOverlay(
      payload.auto_create_room ? `正在创建临时测试聊天室并联调 ${esc(agentName)}` : `正在联调 ${esc(agentName)}`,
      payload.auto_create_room ? '将自动创建一个临时测试聊天室，并通过 Runtime V2 发送测试消息。' : '将通过 Runtime V2 发送一条测试消息，并等待 callback 回写。'
    );
    const started = await apiPost('/api/agent/integration-test', payload);
    if (!started || !started.ok) {
      if (started?.needs_room) {
        ui.finish(false, '需要聊天室', started.error || '请先把该 Agent 加入一个聊天室');
        return {needs_room: true};
      }
      ui.finish(false, '联调未开始', started?.error || '请先保存 Agent，并把它加入一个聊天室');
      return {ok: false};
    }
    if (started.response_received) {
      const state = card.querySelector('.agent-connection-state');
      if (state) state.value = '联调通过';
      ui.finish(true, '已收到同步回复', `${agentName} 已通过 Runtime V2 完成一次测试消息回复。${started.room_created ? ' 临时测试聊天室已自动创建。' : ''}`);
      return {ok: true, started};
    }
    const turnId = started.turn_id || '';
    const roomId = started.room_id || '';
    ui.detail.textContent = `测试消息已发送到 ${roomId}，正在等待 callback 回写…`;
    for (let i = 0; i < 30; i++) {
      await delay(1000);
      const messages = await apiGet(`/api/rooms/${encodeURIComponent(roomId)}/messages`);
      const reply = (messages?.messages || []).find(m =>
        m.from === data.id && (m.reply_to === turnId || m.correlation_id === started.correlation_id)
      );
      if (reply) {
        const state = card.querySelector('.agent-connection-state');
        if (state) state.value = '联调通过';
        ui.finish(true, 'callback 已回写', `${agentName} 已通过 Runtime V2 完成一次测试消息回写。${started.room_created ? ' 临时测试聊天室已自动创建。' : ''}`);
        return {ok: true, started};
      }
    }
    ui.finish(false, '仍在等待 callback', `${agentName} 已收到测试投递，但 30 秒内没有看到 callback 回写。`);
    return {ok: false, started};
  };

  const roomsData = await apiGet('/api/rooms');
  const inRoom = !!roomsData && (roomsData.rooms || []).some(room => (room.agents || []).includes(data.id));
  if (roomsData && !inRoom) {
    btn.disabled = false;
    const ok = await confirmDialog({
      title: '自动创建测试聊天室？',
      message: `${agentName} 还没有加入任何聊天室。是否自动创建一个临时测试聊天室并继续发送测试消息？`,
      confirmText: '自动创建',
    });
    if (!ok) {
      setAgentCardStatus(card, '已取消自动创建测试聊天室', 'warning');
      return;
    }
    await runTest({agent_id: data.id, auto_create_room: true});
    return;
  }
  await runTest({agent_id: data.id});
}

async function loadSettings() {
  const d = await apiGet('/api/settings');
  if (!d || !d.ok) {
    const status = $('#settingsStatus');
    if (status) { status.textContent = '加载失败'; status.className = 'settings-status error'; }
    return;
  }
  const portEl = $('#settingsPort');
  const dirEl = $('#settingsSharedDir');
  const intervalEl = $('#settingsPollInterval');
  const autoEl = $('#settingsAutoPoll');
  const logsEl = $('#settingsMaxLogs');
  const verEl = $('#settingsVersion');
  const linkEl = $('#settingsProjectUrl');

  if (portEl) portEl.textContent = d.port;
  if (dirEl) { dirEl.textContent = d.shared_dir; dirEl.title = d.shared_dir; }
  if (intervalEl) intervalEl.value = d.poll_interval;
  if (autoEl) autoEl.checked = d.auto_start_poll;
  if (logsEl) logsEl.value = d.max_log_entries;
  if (verEl) verEl.textContent = 'v' + d.version;
  if (linkEl) { linkEl.textContent = d.project_url; linkEl.href = d.project_url; }
}

async function saveSettings() {
  const status = $('#settingsStatus');
  const btn = $('#settingsSaveBtn');
  if (!btn || !status) return;

  const interval = parseInt($('#settingsPollInterval')?.value, 10);
  const autoPoll = $('#settingsAutoPoll')?.checked;
  const maxLogs = parseInt($('#settingsMaxLogs')?.value, 10);

  if (isNaN(interval) || interval < 5 || interval > 3600) {
    status.textContent = '轮询间隔需在 5-3600 之间';
    status.className = 'settings-status error';
    return;
  }
  if (isNaN(maxLogs) || maxLogs < 10 || maxLogs > 100000) {
    status.textContent = '日志保留条数需在 10-100000 之间';
    status.className = 'settings-status error';
    return;
  }

  btn.disabled = true;
  btn.classList.add('saving');
  btn.textContent = '保存中';

  const d = await apiPut('/api/settings', {
    poll_interval: interval,
    auto_start_poll: autoPoll,
    max_log_entries: maxLogs,
  });

  btn.disabled = false;
  btn.classList.remove('saving');
  btn.textContent = '保存设置';

  if (!d) {
    status.textContent = '网络错误';
    status.className = 'settings-status error';
    return;
  }
  if (!d.ok) {
    status.textContent = d.error || '保存失败';
    status.className = 'settings-status error';
    return;
  }
  status.textContent = d.message || '设置已保存';
  status.className = 'settings-status ok';
  setTimeout(() => { status.textContent = ''; }, 3000);
  loadPollStatus();
}

async function openSettingsDir() {
  const dir = $('#settingsSharedDir');
  if (!dir || !dir.textContent || dir.textContent === '—') return;
  const res = await apiPost('/api/open-dir', { path: dir.textContent });
  if (!res || !res.ok) {
    const status = $('#settingsStatus');
    if (status) { status.textContent = res?.error || '无法打开目录'; status.className = 'settings-status error'; }
  }
}

async function loadAgentConfig() {
  const area = $('#agentArea');
  const data = await apiGet('/api/config');
  if (!data || !data.ok) {
    area.innerHTML = '<div class="empty-state"><div style="font-size:2.5em;opacity:0.4">⚠</div><h3 style="color:var(--text2);font-weight:500;margin-top:8px;font-size:1em">无法加载配置</h3></div>';
    return;
  }
  renderAgentConfig(data, area);
  renderBadges();
}

function renderAgentConfig(data, area) {
  const agents = data.agents || [];
  const issues = getConfigIssues(data);
  const checklistHtml = issues.length ? `<div class="config-checklist">
    <div class="config-checklist-title">启动前需要完成</div>
    ${issues.map(i => `<div>• ${esc(i)}</div>`).join('')}
  </div>` : `<div class="config-checklist">
    <div class="config-checklist-title">配置已具备基本条件</div>
    <div>可以发送消息；轮询会按本机角色和 Webhook 配置工作。</div>
  </div>`;

  let agentHtml = '';
  for (let i = 0; i < agents.length; i++) {
    const a = agents[i];
    const color = a.color || '#8888a0';
    agentHtml += `<div class="agent-config" data-agent-idx="${i}" data-agent-id="${escAttr(a.id)}">
      <div class="agent-config-header">
        <div class="agent-config-name">
          <span class="agent-config-icon">🤖</span>
          <span class="dot" style="background:${color}"></span>
          <span class="agent-display-label">${esc(a.display_name || a.id)}</span>
        </div>
        <div class="agent-card-actions">
          <button class="agent-card-btn agent-card-btn-save" onclick="saveAgentCard(this)">保存</button>
          <button class="agent-card-btn agent-card-btn-delete" onclick="removeAgent(this)">删除</button>
        </div>
      </div>
      ${renderAgentSimpleFields(a)}
      ${renderAgentAdvancedHtml(a, i)}
      <span class="agent-card-status"></span>
    </div>`;
  }

  area.innerHTML = `<div class="settings-panel agent-page">
    <div class="agent-page-head">
      <div>
        <div class="agent-page-title">
          <img class="page-logo logo-light" src="/icon/light.png" alt="" aria-hidden="true">
          <img class="page-logo logo-dark" src="/icon/dark.png" alt="" aria-hidden="true">
          <span>Agent 管理</span>
        </div>
        <div class="agent-page-subtitle">配置、保存、扫描分区处理</div>
      </div>
      <div class="agent-page-count">${agents.length} 个 Agent</div>
    </div>
    <div class="agent-block">
      <div class="agent-block-header">
        <div>
          <div class="agent-block-title">已配置 Agent</div>
          <div class="agent-block-meta">保存后写入 bridge.yaml</div>
        </div>
      </div>
      ${checklistHtml}
      <div class="badge-list agent-page-badges" id="agentBadgeContainer"></div>
      <div class="agent-config-grid">${agentHtml}</div>
      <div class="settings-actions">
        <button class="add-agent-btn" onclick="addAgent()">＋ 添加 Agent</button>
        <span class="save-status" id="saveStatus"></span>
      </div>
    </div>
    <div class="agent-block scan-panel">
      <div class="agent-block-header">
        <div>
          <div class="scan-title-row">
            <span class="scan-icon">📡</span>
            <div>
              <div class="agent-block-title">扫描本机 Agent</div>
              <div class="scan-copy">扫描结果保存在本机，仅手动刷新。</div>
            </div>
          </div>
        </div>
        <button id="agentScanBtn" class="scan-btn" onclick="scanAndShowDiscover()">开始扫描</button>
      </div>
      <div id="agentDiscoverResult"></div>
      <div id="scanCacheNote" class="scan-cache-note"></div>
      <details style="margin-top:12px;font-size:0.74em;color:var(--text3)">
        <summary style="cursor:pointer;color:var(--text2);font-size:0.78em;margin-bottom:6px">
          扫描原理说明
        </summary>
        <div style="line-height:1.7;padding:6px 0">
          <p style="margin:0 0 6px 0"><b>扫描范围：</b>仅检查当前用户主目录下的已知配置路径，不遍历整个文件系统。</p>
          <p style="margin:0 0 4px 0"><b>扫描方式：</b></p>
          <ul style="margin:0 0 6px 0;padding-left:18px">
            <li><b>消息记录</b> — 读取 active.jsonl 中出现过的发送者</li>
            <li><b>bridge.yaml</b> — 当前已配置的 Agent</li>
            <li><b>Hermes Agent</b> — 检测 ~/.hermes/config.yaml，提取 webhook 地址和认证信息</li>
            <li><b>OpenClaw</b> — 检测 ~/.openclaw/openclaw.json，提取认证 token</li>
            <li><b>其他 Agent</b> — 检测 ~/.claude、~/.codex、~/.gemini、~/.qwen 等目录是否存在</li>
          </ul>
          <p style="margin:0"><b>安全说明：</b>所有扫描仅在本地进行，不会发送网络请求或上传数据。</p>
        </div>
      </details>
    </div>
  </div>`;
  renderCachedDiscover();
}

function addAgent() {
  const area = $('#agentArea');
  const container = area.querySelector('.settings-panel');
  if (!container) return;
  
  const agents = container.querySelectorAll('.agent-config');
  const idx = agents.length;
  
  const card = document.createElement('div');
  card.className = 'agent-config';
  card.dataset.agentIdx = idx;
  const color = randomAgentColor();
  card.innerHTML = `<div class="agent-config-header">
    <div class="agent-config-name">
      <span class="agent-config-icon">🤖</span>
      <span class="dot" style="background:${color}"></span>
      <span class="agent-display-label">新 Agent</span>
    </div>
    <div class="agent-card-actions">
      <button class="agent-card-btn agent-card-btn-save" onclick="saveAgentCard(this)">保存</button>
      <button class="agent-card-btn agent-card-btn-delete" onclick="removeAgent(this)">删除</button>
    </div>
  </div>
  ${renderAgentSimpleFields({color, adapter:{type:'manual', config:{}, auth:{}, response:{mode:'manual', timeout_seconds:180}}})}
  ${renderAgentAdvancedHtml({color, cursor:'line'}, idx)}
  <span class="agent-card-status"></span>`;
  
  const grid = container.querySelector('.agent-config-grid');
  if (grid) grid.appendChild(card);
  refreshAgentCount();
  card.scrollIntoView({behavior:'smooth', block:'center'});
}

async function removeAgent(btn) {
  const card = btn.closest('.agent-config');
  if (!card) return;
  const agentId = card.dataset.agentId || card.querySelector('.ag-id')?.value.trim() || '';
  if (agentId) {
    const roomsData = await apiGet('/api/rooms');
    const blockers = (roomsData?.rooms || [])
      .filter(r => r.status === 'running' && (r.agents || []).includes(agentId));
    if (blockers.length) {
      toast(`请先暂停正在使用 ${agentId} 的聊天室：${blockers.map(r => r.name || r.id).join('、')}`, 'error');
      return;
    }
  }
  const agentName = card.querySelector('.ag-name')?.value.trim()
    || card.querySelector('.ag-id')?.value.trim()
    || '此 Agent';
  const ok = await confirmDialog({
    title: '删除 Agent',
    message: `确认删除「${agentName}」？该操作会先从当前表单移除，保存配置后生效。`,
    confirmText: '删除 Agent',
  });
  if (!ok) return;
  card.remove();
  refreshAgentCount();
  const status = $('#saveStatus');
  const result = await saveVisibleAgentConfig(status);
  if (result.ok) {
    if (status) {
      status.textContent = 'Agent 已删除';
      status.className = 'save-status ok';
      setTimeout(() => { status.textContent = ''; }, 2500);
    }
    await loadConfig();
    renderCachedDiscover();
  }
}

function setAgentCardStatus(card, msg, type) {
  const status = card?.querySelector('.agent-card-status');
  if (!status) return;
  status.textContent = msg;
  status.className = 'agent-card-status' + (type ? ` ${type}` : '');
}

function validateAgentData(data) {
  if (!data.id) return 'Agent ID 不能为空';
  if (!VALID_ID_RE.test(data.id)) return `无效 ID: ${data.id}`;
  if (!/^#[0-9a-fA-F]{6}$/.test(data.color)) return `颜色格式错误: ${data.id}`;
  const adapter = data.adapter || {};
  const adapterType = adapter.type || 'manual';
  const cfg = adapter.config || {};
  if (adapterType === 'openclaw_sessions') {
    if (!cfg.url) return `${data.id}: OpenClaw URL 不能为空`;
    if (!cfg.sessions_key && !cfg.sessionsKey) return `${data.id}: OpenClaw 会话不能为空`;
  } else if (adapterType === 'native_http' && !cfg.url) {
    return `${data.id}: Webhook URL 不能为空`;
  }
  return '';
}

function collectVisibleAgentData() {
  const area = $('#agentArea');
  const container = area.querySelector('.settings-panel');
  if (!container) return {agents: [], errors: ['表单未加载']};

  const agentCards = container.querySelectorAll('.agent-config-grid .agent-config');
  const agents = [];
  const errors = [];
  const ids = new Set();
  
  for (const card of agentCards) {
    const data = collectAgentData(card);
    const error = validateAgentData(data);
    if (error) { errors.push(error); continue; }
    if (ids.has(data.id)) { errors.push(`Agent ID 重复: ${data.id}`); continue; }
    ids.add(data.id);
    agents.push(data);
  }
  return {agents, errors};
}

async function saveVisibleAgentConfig(status) {
  if (status) {
    status.textContent = '保存中…';
    status.className = 'save-status';
  }
  const {agents, errors} = collectVisibleAgentData();
  
  if (errors.length) {
    if (status) {
      status.textContent = errors.join('; ');
      status.className = 'save-status error';
    }
    return {ok: false, error: errors.join('; ')};
  }
  
  const agentId = ( $('#settingsAgentId') || {} ).value;
  
  const r = await apiPost('/api/config/full', {
    agents: agents,
    agent_id: agentId,
  });
  if (!r || !r.ok) {
    const error = r?.error || '服务器错误';
    if (status) {
      status.textContent = '保存失败: ' + error;
      status.className = 'save-status error';
    }
    return {ok: false, error};
  }
  return {ok: true, agents};
}

function buildAgentPayloadForCard(card) {
  const data = collectAgentData(card);
  const error = validateAgentData(data);
  if (error) return {error};
  const sameIdCard = Array.from(document.querySelectorAll('.agent-config-grid .agent-config'))
    .find(other => other !== card && other.querySelector('.ag-id')?.value.trim() === data.id);
  if (sameIdCard) return {error: `Agent ID 重复: ${data.id}`};

  const oldId = card.dataset.agentId || '';
  const agents = (state.fullAgents || state.agents || [])
    .filter(a => a.id !== oldId && a.id !== data.id)
    .map(a => ({...a}));
  if (oldId && oldId !== data.id) data.old_id = oldId;
  agents.push(data);
  return {agents, data};
}

async function saveAgentCard(btn) {
  const card = btn.closest('.agent-config');
  if (!card) return;
  setAgentCardStatus(card, '保存中…', '');
  btn.disabled = true;
  const payload = buildAgentPayloadForCard(card);
  if (payload.error) {
    btn.disabled = false;
    setAgentCardStatus(card, payload.error, 'error');
    return;
  }
  const result = await apiPost('/api/config/full', {agents: payload.agents});
  btn.disabled = false;
  if (!result || !result.ok) {
    setAgentCardStatus(card, '保存失败: ' + (result?.error || '服务器错误'), 'error');
    return;
  }
  const data = payload.data;
  card.dataset.agentId = data.id;
  const label = card.querySelector('.agent-display-label');
  if (label) label.textContent = data.display_name || data.id;
  const summary = agentConfigSummary(data);
  setAgentCardStatus(card, '已保存。下一步：去聊天室添加该 Agent 并点击开始。当前配置：' + summary, 'ok');
  updateAgentSummary(card, true);
  await loadConfig();
  renderCachedDiscover();
  setTimeout(() => setAgentCardStatus(card, '', ''), 5000);
}

async function saveFullConfig() {
  const status = $('#saveStatus');
  const result = await saveVisibleAgentConfig(status);
  if (result.ok) {
    if (status) {
      status.textContent = '已保存。下一步：去聊天室添加该 Agent 并点击开始。当前配置：' + (result.agents || []).map(agentConfigSummary).slice(0, 1).join('');
      status.className = 'save-status ok';
      setTimeout(() => { status.textContent = ''; }, 5000);
    }
    await loadConfig();
    renderCachedDiscover();
  }
}

// ═══ Poll control ═══
async function loadPollStatus() {
  const d = await apiGet('/api/poll');
  if (!d || !d.ok) return;
  const run = d.running;
  pollDot.className = 'poll-dot ' + (run ? 'active' : 'stopped');
  pollLabel.textContent = run ? '轮询中' : '已暂停';
  pollBtn.textContent = run ? '∥' : '▶';
}
pollBtn.addEventListener('click', async () => {
  const d = await apiGet('/api/poll');
  if (d && d.running) await apiPost('/api/poll/stop', {});
  else await apiPost('/api/poll/start', {});
  await loadPollStatus();
});
pollBtn.addEventListener('dblclick', async () => {
  const r = await apiPost('/api/poll/now', {});
  if (r && r.ok) { await refreshAll(); }
});

// ═══ Tab switching ═══
document.querySelectorAll('.tab-btn').forEach(btn => {
  btn.addEventListener('click', () => {
    document.querySelectorAll('.tab-btn').forEach(b => b.classList.remove('active'));
    btn.classList.add('active');
    document.querySelectorAll('.tab-pane').forEach(p => p.classList.remove('active'));
    const pane = document.getElementById('pane-' + btn.dataset.tab);
    if (pane) pane.classList.add('active');
    state.activeTab = btn.dataset.tab;
    // Lazy load
    if (btn.dataset.tab === 'chat') { loadRooms(); if (state.chatView === 'conversation') loadMessages(); }
    if (btn.dataset.tab === 'agent') loadAgentConfig();
    if (btn.dataset.tab === 'settings') loadSettings();
  });
});

// ═══ Auto refresh ═══
function startAutoRefresh() {
  stopAutoRefresh();
  state.pollInterval = setInterval(() => { if (!document.hidden) refreshAll(); }, 5000);
  state.pollStatusTimer = setInterval(() => { if (!document.hidden) loadPollStatus(); }, 3000);
}
function stopAutoRefresh() {
  if (state.pollInterval) { clearInterval(state.pollInterval); state.pollInterval = null; }
  if (state.pollStatusTimer) { clearInterval(state.pollStatusTimer); state.pollStatusTimer = null; }
}
document.addEventListener('visibilitychange', () => { if (!document.hidden && state.autoRefresh) refreshAll(); });
async function refreshAll() { await Promise.all([loadConfig(), loadMessages(), loadRooms(), loadStatus(), loadPollStatus()]); }
async function loadStatus() {
  const d = await apiGet('/api/status');
  if (!d || !d.ok) return;
  statCount.textContent = d.active.count;
}

// ═══ Init ═══
function checkProtocol() {
  if (location.protocol === 'file:') {
    document.body.innerHTML = `
      <div style="display:flex;align-items:center;justify-content:center;height:100vh;background:#1a1a2e;color:#e0e0e0;font-family:-apple-system,BlinkMacSystemFont,sans-serif">
        <div style="text-align:center;max-width:480px;padding:32px">
          <div style="font-size:3em;margin-bottom:16px">⚠️</div>
          <h2 style="font-size:1.3em;font-weight:600;margin:0 0 12px">不能直接打开 HTML 文件</h2>
          <p style="color:#8888a0;line-height:1.6;margin:0 0 20px">
            此页面需要后端 API 支持。<br>
            请通过 server.py 启动后访问：
          </p>
          <code style="display:block;background:#0f0f23;padding:12px 16px;border-radius:8px;font-family:monospace;font-size:0.85em;color:#4ecdc4;text-align:left">
            cd ~/Documents/Project/agent-bridge<br>
            python3 ui/server.py --dir ~/.shared-chat
          </code>
          <p style="color:#8888a0;line-height:1.6;margin:16px 0 0;font-size:0.85em">
            然后浏览器打开 <a href="http://127.0.0.1:7899" style="color:#4ecdc4">http://127.0.0.1:7899</a>
          </p>
        </div>
      </div>`;
    return false;
  }
  return true;
}
if (checkProtocol()) {
 (async function init() {
   await loadConfig();
   await loadRooms();
   await refreshAll();
   startAutoRefresh();
   loadPollStatus();
 })();
}
