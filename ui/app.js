/* ════════════════════════════════════════════════════════════════
   Agent Bridge · WebUI 应用逻辑（全量重写）
   ── Store · Router · Poller · Renderer · Actions · Components · Events
   ════════════════════════════════════════════════════════════════ */
'use strict';

/* ═══ 常量 ═══════════════════════════════════════════════════════ */
const VALID_ID_RE = /^[a-zA-Z0-9_-]+$/;
const AGENT_COLOR_POOL = ['#5b9a4a', '#d97706', '#2563eb', '#be123c', '#7c3aed', '#0f766e', '#c2410c', '#4f46e5'];
const THEME_KEY = 'agentBridgeTheme';
const POLL_FAST = 3000;   // 房间视图内：turn 状态频繁刷新
const POLL_BASE = 5000;   // 默认轮询间隔

/* ── Agent 分类（按已知 ID / 名称自动打标签） ── */
const AGENT_CATEGORY = {
  general: { label: '通用 Agent',  ids: ['openclaw', 'hermes', 'cherry studio', 'cherrystudio', 'cherry'] },
  coding:  { label: 'Coding Agent', ids: ['claude code', 'claudecode', 'claude', 'opencode', 'open-code'] },
};

function categorizeAgent(agent) {
  if (!agent) return null;
  const id = (agent.id || '').toLowerCase();
  const name = (agent.display_name || '').toLowerCase();
  const haystack = `${id} ${name}`;
  for (const [key, info] of Object.entries(AGENT_CATEGORY)) {
    if (info.ids.some(k => haystack.includes(k))) return key;
  }
  return null;
}

function categoryBadge(category) {
  if (!category) return '';
  const label = AGENT_CATEGORY[category].label;
  const cls = category === 'coding' ? 'badge-coding' : 'badge-general';
  return `<span class="badge ${cls}">${esc(label)}</span>`;
}

/* ═══ 工具函数 ═══════════════════════════════════════════════════ */
const $  = (s, root = document) => root.querySelector(s);
const $$ = (s, root = document) => Array.from(root.querySelectorAll(s));

function esc(s) {
  if (s == null) return '';
  return String(s).replace(/[&<>"']/g, c => ({
    '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'
  }[c]));
}

function escAttr(s) { return esc(s); }

async function api(path, opts = {}) {
  const init = {
    method: opts.method || 'GET',
    headers: { 'Content-Type': 'application/json' },
  };
  if (opts.body !== undefined) init.body = JSON.stringify(opts.body);
  const res = await fetch(path, init);
  let data;
  try { data = await res.json(); } catch (e) { data = { ok: false, error: 'invalid response' }; }
  if (!res.ok && data && !data.error) data.error = `HTTP ${res.status}`;
  return data;
}

function initials(name, id) {
  const src = (name || id || '?').trim();
  // 中文取首字，英文取首字母
  const ch = src.charAt(0);
  return ch.toUpperCase();
}

function fmtTime(ts) {
  if (!ts) return '';
  // ts 形如 "2026-07-07 14:30:00"，截取时分
  const m = ts.match(/(\d{2}:\d{2})/);
  return m ? m[1] : ts;
}

function statusToBadge(status) {
  const map = {
    running: 'running', paused: 'paused', error: 'error', archived: 'archived'
  };
  const cls = map[status] || 'paused';
  const label = { running: '运行中', paused: '已暂停', error: '错误', archived: '已归档' }[status] || status;
  return `<span class="badge-status ${cls}">${label}</span>`;
}

/* ═══ Store ══════════════════════════════════════════════════════
   单一 state + subscribe 订阅机制
   ════════════════════════════════════════════════════════════════ */
const Store = {
  state: {
    // 配置
    config: null,
    settings: null,
    pollStatus: null,
    statusData: null,
    // 路由
    activeTab: 'chat',
    currentRoomId: null,
    chatSubview: 'grid',  // 'grid' | 'conversation'
    // 数据
    rooms: [],
    agents: [],
    agentMap: {},
    // 会话视图数据
    currentMessages: [],
    currentLogs: [],
    currentTurn: null,
    // UI 状态
    searchQuery: '',
    discoveredAgents: null,
    discovering: false,
    configIssues: [],
    // 渲染追踪
    renderedMessageIds: new Set(),
    roomGridFirstRender: true,
    theme: document.documentElement.dataset.theme || 'light',
  },
  listeners: [],
  subscribe(fn) { this.listeners.push(fn); },
  emit() { this.listeners.forEach(fn => fn(this.state)); },
  set(patch) {
    Object.assign(this.state, patch);
    this.emit();
  },
  get agents() { return this.state.agents; },
  getAgent(id) { return this.state.agentMap[id]; },
};

/* ═══ Router ═════════════════════════════════════════════════════
   hash 路由：#/chat, #/chat/<room_id>, #/agents, #/settings
   ════════════════════════════════════════════════════════════════ */
const Router = {
  init() {
    window.addEventListener('hashchange', () => this.handle());
    this.handle();
  },
  handle() {
    const hash = location.hash.slice(1) || '/chat';
    const parts = hash.split('/').filter(Boolean);  // ['chat'] or ['chat','room_xxx']
    let tab = parts[0] || 'chat';
    // 防御：未知 tab 回退到 chat（避免 pane 全部 display:none 的空白）
    if (!['chat', 'agent', 'settings'].includes(tab)) tab = 'chat';
    const roomId = (tab === 'chat' && parts[1]) ? parts[1] : null;

    const changes = {};
    if (tab !== Store.state.activeTab) changes.activeTab = tab;
    if (roomId !== Store.state.currentRoomId) changes.currentRoomId = roomId;
    if (tab === 'chat') {
      changes.chatSubview = roomId ? 'conversation' : 'grid';
    } else {
      changes.chatSubview = 'grid';
    }
    if (Object.keys(changes).length) {
      Store.set(changes);
      // 切换视图时重新渲染
      Renderer.renderTab();
      if (tab === 'chat' && roomId) {
        Actions.loadRoomConversation(roomId);
      }
    }
  },
  go(path) {
    if (location.hash !== '#' + path) {
      location.hash = path;
    } else {
      this.handle();
    }
  },
  goChat() { this.go('/chat'); },
  goRoom(roomId) { this.go('/chat/' + roomId); },
  goAgents() { this.go('/agent'); },
  goSettings() { this.go('/settings'); },
};

/* ═══ Poller ═════════════════════════════════════════════════════
   按当前路由精准轮询，不再全量打所有接口
   ════════════════════════════════════════════════════════════════ */
const Poller = {
  timers: [],
  init() {
    this.schedule();
    // 标签页可见时立即刷新一次
    document.addEventListener('visibilitychange', () => {
      if (!document.hidden) this.refresh();
    });
  },
  clear() {
    this.timers.forEach(t => clearInterval(t));
    this.timers = [];
  },
  schedule() {
    this.clear();
    const tab = Store.state.activeTab;
    const sub = Store.state.chatSubview;

    // 全局轻量数据：rooms 列表（任何 tab 都要 sidebar 统计）
    this.timers.push(setInterval(() => this.loadGlobal(), POLL_BASE));

    if (tab === 'chat' && sub === 'conversation' && Store.state.currentRoomId) {
      // 会话视图：消息/日志/turn 快速刷新
      this.timers.push(setInterval(() => this.loadRoomData(), POLL_FAST));
    }
    // 注意：settings/agents 页不打任何轮询
  },
  async refresh() {
    await this.loadGlobal();
    if (Store.state.activeTab === 'chat' && Store.state.chatSubview === 'conversation') {
      await this.loadRoomData();
    }
  },
  async loadGlobal() {
    if (document.hidden) return;
    try {
      const [cfg, rooms, status, poll] = await Promise.all([
        api('/api/config'),
        api('/api/rooms'),
        api('/api/status'),
        api('/api/poll'),
      ]);
      if (cfg.ok) {
        const agents = cfg.agents || [];
        const agentMap = {};
        agents.forEach(a => { agentMap[a.id] = a; });
        Store.set({
          config: cfg,
          agents,
          agentMap,
          configIssues: Renderer.collectConfigIssues(cfg, status),
        });
      }
      if (rooms.ok) Store.set({ rooms: rooms.rooms || [] });
      if (status.ok) Store.set({ statusData: status });
      if (poll.ok) Store.set({ pollStatus: poll });

      // 触发渲染
      Renderer.renderSidebar();
      Renderer.renderConfigBanner();
      if (Store.state.activeTab === 'chat' && Store.state.chatSubview === 'grid') {
        Renderer.renderRoomGrid();
      }
    } catch (e) {
      console.warn('loadGlobal failed:', e);
    }
  },
  async loadRoomData() {
    if (document.hidden) return;
    const roomId = Store.state.currentRoomId;
    if (!roomId) return;
    try {
      const [msgs, logs, turn] = await Promise.all([
        api(`/api/rooms/${roomId}/messages`),
        api(`/api/rooms/${roomId}/logs`),
        api(`/api/rooms/${roomId}/turn`),
      ]);
      if (msgs.ok) {
        // diff 渲染：只追加新消息
        Renderer.renderMessagesDiff(msgs.messages || []);
        Store.state.currentMessages = msgs.messages || [];
      }
      if (logs.ok) {
        Store.state.currentLogs = logs.logs || [];
        Renderer.renderRoomLogs();
      }
      if (turn.ok) {
        Store.state.currentTurn = turn;
        Renderer.renderTurnStatus();
      }
    } catch (e) {
      console.warn('loadRoomData failed:', e);
    }
  },
};

/* ═══ Components ═════════════════════════════════════════════════
   统一的 Modal / Toast / Confirm 组件
   ════════════════════════════════════════════════════════════════ */
const Components = {
  toast(message, type = 'success', duration = 3500) {
    const root = $('#toastRoot');
    const el = document.createElement('div');
    el.className = `toast ${type}`;
    el.textContent = message;
    root.appendChild(el);
    setTimeout(() => {
      el.classList.add('exit');
      setTimeout(() => el.remove(), 200);
    }, duration);
  },
  modal({ title, body, actions = [] }) {
    const root = $('#modalRoot');
    const overlay = document.createElement('div');
    overlay.className = 'modal-overlay';
    overlay.innerHTML = `
      <div class="modal" role="dialog" aria-modal="true">
        <div class="modal-head">
          <div class="modal-title">${esc(title)}</div>
          <button class="btn-icon btn-ghost" data-action="closeModal" aria-label="关闭">✕</button>
        </div>
        <div class="modal-body">${body}</div>
        ${actions.length ? `<div class="modal-foot">
          ${actions.map((a, i) => `<button class="btn ${a.kind || ''}" data-modal-action="${i}">${esc(a.label)}</button>`).join('')}
        </div>` : ''}
      </div>
    `;
    root.appendChild(overlay);

    const close = () => overlay.remove();
    overlay.addEventListener('click', (e) => {
      if (e.target === overlay) close();
      if (e.target.dataset.action === 'closeModal') close();
    });
    const escHandler = (e) => {
      if (e.key === 'Escape') { close(); document.removeEventListener('keydown', escHandler); }
    };
    document.addEventListener('keydown', escHandler);

    actions.forEach((a, i) => {
      const btn = $(`[data-modal-action="${i}"]`, overlay);
      if (btn && a.onClick) {
        btn.addEventListener('click', () => {
          const keep = a.onClick(overlay);
          if (!keep) { close(); document.removeEventListener('keydown', escHandler); }
        });
      }
    });
    return overlay;
  },
  confirm(message, { title = '确认', okLabel = '确定', cancelLabel = '取消', danger = false } = {}) {
    return new Promise((resolve) => {
      this.modal({
        title,
        body: `<div style="padding: var(--space-2) 0; color: var(--paper);">${esc(message)}</div>`,
        actions: [
          { label: cancelLabel, kind: 'btn-ghost', onClick: () => resolve(false) },
          { label: okLabel, kind: danger ? 'btn-danger' : 'btn-primary', onClick: () => resolve(true) },
        ],
      });
    });
  },
};

/* ═══ Renderer ═══════════════════════════════════════════════════
   分视图渲染：tab/chat/agent/settings/sidebar
   消息用 diff 增量更新，不再全量 innerHTML
   ════════════════════════════════════════════════════════════════ */
const Renderer = {
  renderTab() {
    const tab = Store.state.activeTab;
    $$('.tab-btn').forEach(btn => {
      btn.classList.toggle('active', btn.dataset.tab === tab);
    });
    $$('.tab-pane').forEach(pane => {
      pane.classList.toggle('active', pane.id === 'pane-' + tab);
    });
    if (tab === 'chat') {
      this.renderChat();
    } else if (tab === 'agent') {
      this.renderAgentPage();
    } else if (tab === 'settings') {
      this.renderSettings();
    }
    Poller.schedule();  // 重新调度轮询
  },

  renderChat() {
    const view = $('#chatView');
    if (Store.state.chatSubview === 'conversation' && Store.state.currentRoomId) {
      this.renderRoomConversation(view);
    } else {
      this.renderRoomGridPage(view);
    }
  },

  /* ── 房间网格页 ── */
  renderRoomGridPage(view) {
    const rooms = Store.state.rooms;
    view.innerHTML = `
      <div class="page-header">
        <div>
          <div class="page-title">聊天室</div>
          <div class="page-subtitle">${rooms.length} 个房间 · 点击进入对话</div>
        </div>
        <button class="btn btn-primary" data-action="newRoom">+ 新建聊天室</button>
      </div>
      <div class="room-grid" id="roomGrid"></div>
    `;
    this.renderRoomGrid();
  },

  renderRoomGrid() {
    const grid = $('#roomGrid');
    if (!grid) return;
    const rooms = Store.state.rooms;
    const firstRender = Store.state.roomGridFirstRender;
    let html = rooms.map((room, i) => this.tplRoomCard(room, firstRender && i < 12)).join('');
    html += this.tplAddRoomCard();
    grid.innerHTML = html;
    Store.state.roomGridFirstRender = false;
  },

  tplRoomCard(room, animate) {
    const agents = (room.agents || []).map(aid => {
      const a = Store.getAgent(aid);
      return a ? `<span class="agent-chip agent-chip-sm" style="background:${escAttr(a.color)}" title="${escAttr(a.display_name)}">${esc(initials(a.display_name, aid))}</span>` : `<span class="badge">${esc(aid)}</span>`;
    }).join('') || '<span class="room-card-agents-empty">未配置 Agent</span>';
    const status = room.status || 'paused';
    const canControl = (room.agents || []).length > 0;
    const isRunning = status === 'running';
    return `
      <article class="room-card ${animate ? 'entering' : ''}" data-room-id="${escAttr(room.id)}" data-action="openRoom" data-room-id-target="${escAttr(room.id)}">
        <div class="room-card-head">
          <div>
            <div class="room-card-name">${esc(room.name || room.id)}</div>
            <div class="room-card-meta">${esc(room.id)} · ${(room.agents || []).length} 角色</div>
          </div>
          ${statusToBadge(status)}
        </div>
        <div class="room-card-agents">${agents}</div>
        <div class="room-card-foot">
          <div class="room-card-actions">
            ${canControl ? `<button class="btn btn-sm ${isRunning ? '' : 'btn-primary'}" data-action="toggleRoom" data-room-id="${escAttr(room.id)}" data-status="${escAttr(status)}">${isRunning ? '暂停' : '开始'}</button>` : ''}
            ${!isRunning ? `<button class="btn btn-sm btn-ghost" data-action="deleteRoom" data-room-id="${escAttr(room.id)}" data-room-name="${escAttr(room.name || room.id)}" title="删除聊天室">删除</button>` : ''}
          </div>
          <button class="btn btn-sm btn-ghost" data-action="openRoom" data-room-id="${escAttr(room.id)}">进入 →</button>
        </div>
      </article>
    `;
  },

  tplAddRoomCard() {
    return `
      <div class="room-card room-card-add" data-action="newRoom">
        <input type="text" class="room-card-add-input" placeholder="+ 新建聊天室…" id="newRoomInput" maxlength="40" data-action="newRoomInput">
      </div>
    `;
  },

  /* ── 会话视图 ── */
  renderRoomConversation(view) {
    const room = Store.state.rooms.find(r => r.id === Store.state.currentRoomId);
    if (!room) {
      view.innerHTML = `<div class="empty-state"><div class="icon">🔍</div><h3>房间不存在</h3><p>它可能已被删除</p><p><button class="btn" data-action="backToRooms">返回聊天室列表</button></p></div>`;
      return;
    }
    const agents = (room.agents || []).map(aid => {
      const a = Store.getAgent(aid);
      return a
        ? `<span class="badge" style="border-color:${escAttr(a.color)}"><span class="agent-chip agent-chip-sm" style="background:${escAttr(a.color)}; width:18px; height:18px; font-size:9px;">${esc(initials(a.display_name, aid))}</span>${esc(a.display_name)}</span>`
        : `<span class="badge">${esc(aid)} <span class="muted">(未配置)</span></span>`;
    }).join('');
    const status = room.status || 'paused';
    const isRunning = status === 'running';

    view.innerHTML = `
      <div class="room-conv">
        <div class="conv-head">
          <button class="back-btn" data-action="backToRooms">← 聊天室</button>
          <div>
            <div class="conv-room-name">${esc(room.name || room.id)}</div>
            <div class="room-card-meta">${esc(room.id)}</div>
          </div>
          <div class="conv-agent-badges">${agents}</div>
          <div class="conv-controls">
            ${statusToBadge(status)}
            <button class="btn btn-sm ${isRunning ? '' : 'btn-primary'}" data-action="toggleRoom" data-room-id="${escAttr(room.id)}" data-status="${escAttr(status)}">${isRunning ? '暂停' : '开始'}</button>
            <button class="btn btn-sm btn-ghost" data-action="tickRoom" data-room-id="${escAttr(room.id)}" title="手动推进一次">Tick</button>
          </div>
        </div>
        <div class="room-conv-body">
          <div class="room-conv-main">
            <div class="compose-bar">
              <select class="select" id="composeAgent" aria-label="选择发送者">
                <option value="user">我（用户）</option>
                ${(room.agents || []).map(aid => `<option value="${escAttr(aid)}">${esc(Store.getAgent(aid)?.display_name || aid)}</option>`).join('')}
              </select>
              <input type="text" class="input" id="composeInput" placeholder="输入消息，按 Enter 发送…" aria-label="消息输入">
              <button class="btn btn-primary" data-action="sendMessage" data-room-id="${escAttr(room.id)}">发送</button>
            </div>
            <div class="chat-controls">
              <input class="input" id="searchInput" type="text" placeholder="搜索消息…" data-action="search" value="${escAttr(Store.state.searchQuery)}">
              <button class="btn-icon btn-ghost" data-action="refreshRoom" title="刷新">⟳</button>
            </div>
            <div class="chat-area" id="chatArea"></div>
          </div>
          <aside class="room-log-panel" aria-label="运行日志">
            <div class="room-log-head">
              <span class="room-log-title">运行日志</span>
              <div style="display:flex;align-items:center;gap:6px">
                <span class="room-log-count" id="roomLogCount">0 条</span>
                <button class="btn-icon btn-ghost" data-action="copyLogs" title="复制运行日志">
                  <svg width="14" height="14" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"><rect x="5" y="5" width="9" height="9" rx="1.5"/><path d="M3 11V3a1.5 1.5 0 011.5-1.5H11"/></svg>
                </button>
              </div>
            </div>
            <div id="roomTurnStatus" class="room-turn-status"></div>
            <div id="roomLogArea" class="room-log-list"></div>
          </aside>
        </div>
      </div>
    `;
    // 重置消息 diff 追踪（新房间）
    Store.state.renderedMessageIds = new Set();
    $('#chatArea').innerHTML = '';
  },

  /* ── 消息渲染：diff 增量更新（核心改进，不再全量 innerHTML） ── */
  renderMessagesDiff(messages) {
    const area = $('#chatArea');
    if (!area) return;

    const q = Store.state.searchQuery.toLowerCase();
    const filtered = q
      ? messages.filter(m => (m.msg || '').toLowerCase().includes(q) || (m.from || '').toLowerCase().includes(q))
      : messages;

    const existingIds = new Set(Store.state.renderedMessageIds);
    const newIds = new Set(filtered.map(m => m.id || `${m.ts}_${m.from}`));

    // 删除不再存在的消息（搜索过滤 / 房间清空场景）
    Array.from(area.children).forEach(el => {
      const id = el.dataset.msgId;
      if (id && !newIds.has(id)) el.remove();
    });

    // 追加新消息（保持顺序）
    const currentAgentId = Store.state.config?.agent_id;
    filtered.forEach(msg => {
      const id = msg.id || `${msg.ts}_${msg.from}`;
      if (!existingIds.has(id)) {
        const el = this.tplMessageElement(msg, currentAgentId);
        area.appendChild(el);
        Store.state.renderedMessageIds.add(id);
      }
    });

    // 自动滚到底（仅当用户已在底部时）
    const nearBottom = area.scrollHeight - area.scrollTop - area.clientHeight < 100;
    if (nearBottom) area.scrollTop = area.scrollHeight;
  },

  tplMessageElement(msg, currentAgentId) {
    const el = document.createElement('div');
    const isSelf = msg.from === currentAgentId;
    const kind = msg.kind || 'agent';
    el.className = `msg msg-kind-${kind} ${isSelf ? 'msg-right' : ''}`;
    el.dataset.msgId = msg.id || `${msg.ts}_${msg.from}`;

    const agent = Store.getAgent(msg.from);
    const name = msg.from === 'user' ? '我' : (agent?.display_name || msg.from);
    const color = msg.from === 'user' ? '#5C6B62' : (agent?.color || '#7c3aed');
    const avatar = kind === 'system'
      ? ''
      : `<span class="agent-chip msg-avatar" style="background:${escAttr(color)}">${esc(initials(name, msg.from))}</span>`;
    const meta = kind === 'system'
      ? ''
      : `<div class="msg-meta"><span class="msg-name">${esc(name)}</span><span class="msg-time">${esc(fmtTime(msg.ts))}</span></div>`;

    el.innerHTML = `
      ${avatar}
      <div class="msg-body">
        ${meta}
        <div class="msg-bubble">${esc(msg.msg || '')}</div>
      </div>
    `;
    return el;
  },

  /* ── 运行日志渲染 ── */
  renderRoomLogs() {
    const area = $('#roomLogArea');
    if (!area) return;
    const logs = Store.state.currentLogs;
    const count = $('#roomLogCount');
    if (count) count.textContent = `${logs.length} 条`;

    area.innerHTML = logs.slice(-200).map(log => {
      const cls = `log-entry ${log.level === 'error' ? 'level-error' : (log.level === 'warning' ? 'level-warning' : '')}`;
      const evt = log.event ? `<span class="log-entry-event">${esc(log.event)}</span>` : '';
      return `<div class="${cls}"><span class="log-entry-ts">${esc(fmtTime(log.ts))}</span>${evt}${esc(log.msg || '')}</div>`;
    }).join('');
    area.scrollTop = area.scrollHeight;
  },

  /* ── Turn 状态渲染 ── */
  renderTurnStatus() {
    const el = $('#roomTurnStatus');
    if (!el) return;
    const turn = Store.state.currentTurn;
    if (!turn) { el.innerHTML = ''; return; }
    const ct = turn.current_turn;
    const rows = [];
    rows.push(['房间状态', turn.status || '—']);
    rows.push(['轮次序号', String(turn.turn_index ?? '—')]);
    if (ct) {
      rows.push(['Turn 状态', ct.state || '—']);
      if (ct.agent_id) rows.push(['当前 Agent', ct.agent_id]);
      if (ct.started_at) rows.push(['开始于', fmtTime(ct.started_at)]);
      if (ct.timeout_at) rows.push(['超时于', fmtTime(ct.timeout_at)]);
      if (ct.response_message_id) rows.push(['已收到回复', '✓']);
      if (ct.last_error) rows.push(['错误', ct.last_error]);
    }
    el.innerHTML = rows.map(([k, v]) => `<div class="turn-status-row"><span class="turn-status-key">${esc(k)}</span><span class="turn-status-val">${esc(v)}</span></div>`).join('');
  },

  /* ═══ Agent 页 ═══════════════════════════════════════════════ */
  renderAgentPage() {
    const view = $('#agentView');
    const agents = Store.state.agents;
    if (!Store.state.config) {
      view.innerHTML = `<div class="empty-state"><div class="icon">⏳</div><h3>加载中…</h3></div>`;
      return;
    }
    view.innerHTML = `
      <div class="page-header">
        <div>
          <div class="page-title">Agent 配置</div>
          <div class="page-subtitle">${agents.length} 个 Agent · 点击卡片编辑</div>
        </div>
        <button class="btn" data-action="discoverAgents">⟳ 扫描本机 Agent</button>
      </div>
      <div id="scanPanel"></div>
      <div class="agent-grid">
        ${agents.map(a => this.tplAgentCard(a)).join('')}
        <div class="agent-card room-card-add" style="border-style: dashed; cursor: pointer;" data-action="addAgent">
          + 添加 Agent
        </div>
      </div>
    `;
    if (Store.state.discoveredAgents) {
      this.renderScanResults(Store.state.discoveredAgents);
    }
  },

  tplAgentCard(a) {
    const adapter = a.adapter || {};
    const type = adapter.type || 'manual';
    const category = categorizeAgent(a);
    const isMcp = type === 'mcp_tool';

    // MCP 接入 vs HTTP 接入：卡片内容分支
    const connectionSection = isMcp ? `
      <div class="form-row full">
        <label class="form-label">接入方式</label>
        <div class="mcp-info-box">
          <div class="mcp-info-icon">🔌</div>
          <div>
            <div style="font-weight: 600; color: var(--moss); margin-bottom: 4px;">MCP 接入（零配置）</div>
            <div class="muted" style="font-size: 12px; line-height: 1.6;">
              此 Agent 通过 MCP 协议直接接入，无需 HTTP webhook。<br>
              请将下面的配置添加到 Agent 的 mcpServers 设置中。
            </div>
          </div>
        </div>
        <div class="mcp-config-block">
          <div class="mcp-config-label">
            <span>MCP Server 配置（复制到 Agent 设置）</span>
            <button class="btn btn-sm btn-ghost" data-action="copyMcpConfig" data-agent-id="${escAttr(a.id)}">复制</button>
          </div>
          <pre class="mcp-config-pre" id="mcpConfig-${escAttr(a.id)}">{
  "mcpServers": {
    "agent-bridge": {
      "command": "python3",
      "args": ["core/mcp_server.py", "--shared-dir", "~/.agent-bridge"]
    }
  }
}</pre>
        </div>
      </div>
    ` : `
      <div class="form-row full">
        <label class="form-label">Webhook / 调用 URL</label>
        <input class="input mono" data-agent-field="wakeup_url" data-agent-id="${escAttr(a.id)}" value="${escAttr(a.wakeup?.url || adapter.config?.url || '')}" placeholder="http://127.0.0.1:8644/webhooks/...">
      </div>
    `;

    return `
      <div class="agent-card" data-agent-card="${escAttr(a.id)}">
        <div class="agent-card-head">
          <span class="agent-chip agent-chip-lg" style="background:${escAttr(a.color)}">${esc(initials(a.display_name, a.id))}</span>
          <div class="agent-card-name">${esc(a.display_name)}</div>
          ${categoryBadge(category)}
          ${isMcp ? '<span class="badge badge-mcp">MCP</span>' : ''}
          <span class="agent-card-id">@${esc(a.id)}</span>
        </div>
        <div class="agent-card-form">
          <div class="form-row">
            <label class="form-label">显示名</label>
            <input class="input" data-agent-field="display_name" data-agent-id="${escAttr(a.id)}" value="${escAttr(a.display_name)}">
          </div>
          <div class="form-row">
            <label class="form-label">ID</label>
            <input class="input mono" data-agent-field="id" data-agent-id="${escAttr(a.id)}" value="${escAttr(a.id)}">
          </div>
          <div class="form-row">
            <label class="form-label">颜色</label>
            <input class="input" type="color" data-agent-field="color" data-agent-id="${escAttr(a.id)}" value="${escAttr(a.color)}">
          </div>
          <div class="form-row">
            <label class="form-label">适配器类型</label>
            <select class="select" data-agent-field="adapter_type" data-agent-id="${escAttr(a.id)}">
              ${['native_http', 'openclaw_sessions', 'cli', 'file_mailbox', 'mcp_tool', 'manual'].map(t =>
                `<option value="${t}" ${t === type ? 'selected' : ''}>${t}</option>`
              ).join('')}
            </select>
          </div>
          ${connectionSection}
        </div>
        ${!isMcp ? `<details class="agent-adapter-config">
          <summary>高级配置（认证 / body 模板）</summary>
          <div class="form-row full" style="margin-top: var(--space-3)">
            <label class="form-label">认证 Token 路径（可选）</label>
            <input class="input mono" data-agent-field="token_path" data-agent-id="${escAttr(a.id)}" value="${escAttr(a.wakeup?.auth?.token_path || '')}" placeholder="~/.openclaw/openclaw.json">
          </div>
          <div class="form-row full">
            <label class="form-label">Token JSONPath</label>
            <input class="input mono" data-agent-field="token_jsonpath" data-agent-id="${escAttr(a.id)}" value="${escAttr(a.wakeup?.auth?.token_jsonpath || '')}" placeholder="gateway.auth.password">
          </div>
        </details>` : ''}
        <div class="agent-card-actions">
          <button class="btn btn-sm btn-ghost" data-action="testAgent" data-agent-id="${escAttr(a.id)}">测试</button>
          <button class="btn btn-sm btn-danger" data-action="removeAgent" data-agent-id="${escAttr(a.id)}">删除</button>
          <button class="btn btn-sm btn-primary" data-action="saveAgent" data-agent-id="${escAttr(a.id)}">保存</button>
        </div>
      </div>
    `;
  },

  renderScanResults(discovered) {
    const panel = $('#scanPanel');
    if (!panel) return;
    if (!discovered || !discovered.length) {
      panel.innerHTML = '';
      return;
    }
    panel.innerHTML = `
      <div class="scan-panel">
        <div class="scan-panel-head">
          <div>
            <div class="page-title" style="font-size: 15px;">扫描结果</div>
            <div class="page-subtitle">发现 ${discovered.length} 个本机 Agent</div>
          </div>
          <button class="btn btn-sm btn-ghost" data-action="closeScan">关闭</button>
        </div>
        <div class="scan-results">
          ${discovered.map(d => `
            <div class="scan-result">
              <span class="agent-chip" style="background:${escAttr(d.color || AGENT_COLOR_POOL[0])}">${esc(initials(d.display_name, d.id))}</span>
              <div class="scan-result-info">
                <div class="scan-result-name">${esc(d.display_name)} <span class="muted">@${esc(d.id)}</span></div>
                <div class="scan-result-meta">${esc(d.kind || '')} · ${esc(d.source_dir || '')}</div>
              </div>
              ${d.configured ? '<span class="badge">已配置</span>' : `<button class="btn btn-sm btn-primary" data-action="addDiscovered" data-agent='${escAttr(JSON.stringify(d))}'>添加</button>`}
            </div>
          `).join('')}
        </div>
      </div>
    `;
  },

  /* ═══ Settings 页 ════════════════════════════════════════════ */
  renderSettings() {
    const view = $('#settingsView');
    const s = Store.state.settings || {};
    view.innerHTML = `
      <div class="settings-page">
        <div class="page-header">
          <div>
            <div class="page-title">设置</div>
            <div class="page-subtitle">配置 Agent Bridge 运行参数</div>
          </div>
        </div>

        <div class="settings-block">
          <div class="settings-block-head">
            <span class="settings-block-title">基础设置</span>
            <span class="settings-block-meta">只读</span>
          </div>
          <div class="settings-row">
            <div class="settings-row-left">
              <span class="settings-label">端口</span>
              <span class="settings-desc">当前服务监听端口</span>
            </div>
            <span class="settings-value">${esc(s.port || '—')}</span>
          </div>
          <div class="settings-row">
            <div class="settings-row-left">
              <span class="settings-label">数据目录</span>
              <span class="settings-desc">共享消息目录路径</span>
            </div>
            <button class="settings-path-btn" data-action="openSharedDir" title="在文件管理器中打开">${esc(s.shared_dir || '—')}</button>
          </div>
        </div>

        <div class="settings-block">
          <div class="settings-block-head">
            <span class="settings-block-title">轮询设置</span>
          </div>
          <div class="settings-row">
            <div class="settings-row-left">
              <span class="settings-label">轮询间隔</span>
              <span class="settings-desc">自动轮询的时间间隔（5-3600 秒）</span>
            </div>
            <div class="settings-input-group">
              <input type="number" class="settings-input" id="settingsPollInterval" min="5" max="3600" value="${esc(s.poll_interval ?? 180)}" aria-label="轮询间隔">
              <span class="settings-unit">秒</span>
            </div>
          </div>
          <div class="settings-row">
            <div class="settings-row-left">
              <span class="settings-label">自动启动轮询</span>
              <span class="settings-desc">启动服务时自动开始轮询</span>
            </div>
            <label class="switch">
              <input type="checkbox" id="settingsAutoPoll" ${s.auto_start_poll ? 'checked' : ''}>
              <span class="switch-track"></span>
            </label>
          </div>
        </div>

        <div class="settings-block">
          <div class="settings-block-head">
            <span class="settings-block-title">日志设置</span>
          </div>
          <div class="settings-row">
            <div class="settings-row-left">
              <span class="settings-label">运行日志保留条数</span>
              <span class="settings-desc">聊天室运行日志最大保留数量（10-100000）</span>
            </div>
            <div class="settings-input-group">
              <input type="number" class="settings-input" id="settingsMaxLogs" min="10" max="100000" value="${esc(s.max_log_entries ?? 1000)}" aria-label="日志保留条数">
              <span class="settings-unit">条</span>
            </div>
          </div>
        </div>

        <div class="settings-block">
          <div class="settings-block-head">
            <span class="settings-block-title">关于</span>
          </div>
          <div class="settings-row">
            <div class="settings-row-left"><span class="settings-label">版本号</span></div>
            <span class="settings-value">${esc(s.version || '—')}</span>
          </div>
          <div class="settings-row">
            <div class="settings-row-left"><span class="settings-label">项目链接</span></div>
            <a class="settings-value" href="${escAttr(s.project_url || '#')}" target="_blank" rel="noopener">${esc(s.project_url || '—')}</a>
          </div>
        </div>

        <div class="settings-save-bar">
          <button class="btn btn-primary" data-action="saveSettings">保存设置</button>
        </div>
      </div>
    `;
  },

  /* ═══ Sidebar 渲染 ════════════════════════════════════════════ */
  renderSidebar() {
    const status = Store.state.statusData;
    if (status && status.active) {
      const c = $('#statCount');
      if (c) c.textContent = String(status.active.count ?? '—');
      const sub = $('#statLast');
      if (sub) sub.textContent = status.history_count ? `${status.history_count} 条历史` : '';
    }
    const poll = Store.state.pollStatus;
    const dot = $('#pollDot');
    const label = $('#pollLabel');
    const btn = $('#pollBtn');
    if (dot && poll) {
      dot.classList.remove('running', 'error');
      if (poll.running) dot.classList.add('running');
      else if (poll.last_result && poll.last_result.error) dot.classList.add('error');
    }
    if (label && poll) label.textContent = poll.running ? '轮询中' : '已停止';
    if (btn && poll) btn.textContent = poll.running ? '⏸' : '▶';
  },

  /* ═══ 配置 banner（显示全部 issues，不再截断） ══════════════════ */
  collectConfigIssues(cfg, status) {
    const issues = [];
    if (!cfg || !cfg.agents || cfg.agents.length === 0) issues.push('尚未配置任何 Agent');
    if (!cfg.agent_id) issues.push('未设置本机 agent_id');
    if (cfg.agents && cfg.agents.length === 1) issues.push('只有 1 个 Agent，需要至少 2 个才能对话');
    cfg.agents.forEach(a => {
      const adapter = a.adapter || {};
      const wu = a.wakeup || {};
      if (adapter.type === 'native_http' && !(adapter.config?.url || wu.url)) {
        issues.push(`Agent "${a.display_name || a.id}" 缺少 webhook URL`);
      }
    });
    return issues;
  },

  renderConfigBanner() {
    const banner = $('#configBanner');
    if (!banner) return;
    const issues = Store.state.configIssues;
    if (!issues.length) {
      banner.classList.remove('show');
      banner.innerHTML = '';
      return;
    }
    banner.classList.add('show');
    banner.innerHTML = issues.map(i => `<div class="config-banner-item">${esc(i)}</div>`).join('');
  },

  /* ═══ 主题切换按钮 ════════════════════════════════════════════ */
  renderThemeBtn() {
    const btn = $('.theme-toggle');
    if (!btn) return;
    const isDark = Store.state.theme === 'dark';
    btn.textContent = isDark ? '☀' : '☾';
  },
};

/* ═══ Actions ════════════════════════════════════════════════════
   所有用户操作的入口
   ════════════════════════════════════════════════════════════════ */
const Actions = {
  async switchTab(tabName) {
    if (tabName === 'chat') Router.goChat();
    else if (tabName === 'agent') { Router.goAgents(); await this.loadConfig(); }
    else if (tabName === 'settings') { Router.goSettings(); await this.loadSettings(); }
  },

  async loadConfig() {
    const cfg = await api('/api/config');
    if (cfg.ok) {
      const agents = cfg.agents || [];
      const agentMap = {};
      agents.forEach(a => { agentMap[a.id] = a; });
      Store.set({ config: cfg, agents, agentMap });
      Renderer.renderAgentPage();
    }
  },

  async loadSettings() {
    const s = await api('/api/settings');
    if (s.ok) {
      Store.set({ settings: s });
      Renderer.renderSettings();
    }
  },

  async loadRoomConversation(roomId) {
    Store.state.currentRoomId = roomId;
    Store.state.chatSubview = 'conversation';
    Renderer.renderChat();
    await Poller.loadRoomData();
  },

  backToRooms() { Router.goChat(); },

  /* ── 房间操作 ── */
  async newRoom() {
    const input = $('#newRoomInput');
    if (input && input.value.trim()) {
      return this.createRoom(input.value.trim());
    }
    // 通过 modal 输入
    Components.modal({
      title: '新建聊天室',
      body: `
        <div class="form-row">
          <label class="form-label">房间名称</label>
          <input class="input" id="newRoomModalInput" placeholder="例如：苏苏与墨墨的茶话会" maxlength="40">
          <div class="form-desc">仅允许字母、数字、连字符、下划线；留空将自动生成 ID</div>
        </div>
      `,
      actions: [
        { label: '取消', kind: 'btn-ghost', onClick: () => false },
        { label: '创建', kind: 'btn-primary', onClick: (overlay) => {
          const name = $('#newRoomModalInput', overlay)?.value.trim();
          if (!name) return true;  // 保持打开
          this.createRoom(name);
          return false;
        }},
      ],
    });
  },

  async createRoom(name) {
    let id = name;
    if (!VALID_ID_RE.test(id)) {
      // 中文名或带空格的：自动生成 id
      id = 'room_' + Math.random().toString(36).slice(2, 10);
    }
    const res = await api('/api/rooms', {
      method: 'POST',
      body: { id, name, agents: [], order: [], policy: 'round_robin', status: 'paused' },
    });
    if (res.ok) {
      Components.toast('已创建聊天室');
      await Poller.refresh();
      Router.goRoom(id);
    } else {
      Components.toast(res.error || '创建失败', 'error');
    }
  },

  async openRoom(roomId) {
    Router.goRoom(roomId);
  },

  async toggleRoom(roomId, currentStatus) {
    const isRunning = currentStatus === 'running';
    const action = isRunning ? 'pause' : (currentStatus === 'error' ? 'resume' : 'start');
    const res = await api(`/api/rooms/${roomId}/${action}`, { method: 'POST', body: {} });
    if (res.ok) {
      Components.toast(isRunning ? '已暂停' : '已开始');
      await Poller.refresh();
    } else {
      Components.toast(res.error || '操作失败', 'error');
    }
  },

  async deleteRoom(roomId, roomName) {
    const confirmed = await Components.confirm(
      `确认删除聊天室「${roomName}」？\n该房间内的所有消息将永久丢失，无法恢复。`,
      { title: '删除聊天室', okLabel: '删除', danger: true }
    );
    if (!confirmed) return;
    const res = await api('/api/rooms/delete', { method: 'POST', body: { id: roomId } });
    if (res.ok) {
      Components.toast('已删除');
      // 若正在该房间会话视图，返回网格
      if (Store.state.currentRoomId === roomId) Router.goChat();
      await Poller.refresh();
    } else {
      // 运行中不能删等错误
      Components.toast(res.error || '删除失败', 'error');
    }
  },

  async tickRoom(roomId) {
    const res = await api(`/api/rooms/${roomId}/tick`, { method: 'POST', body: { force: true } });
    if (res.ok) {
      Components.toast('已手动推进');
      await Poller.loadRoomData();
    } else {
      Components.toast(res.error || '推进失败', 'error');
    }
  },

  async refreshRoom() {
    await Poller.loadRoomData();
    Components.toast('已刷新');
  },

  async sendMessage(roomId) {
    const input = $('#composeInput');
    const select = $('#composeAgent');
    if (!input) return;
    const text = input.value.trim();
    if (!text) return;
    const from = select?.value || 'user';

    // 乐观本地插入：立即在 UI 显示
    const tempId = `local_${Date.now()}`;
    const tempMsg = {
      id: tempId, ts: new Date().toISOString().replace('T', ' ').slice(0, 19),
      from, kind: from === 'user' ? 'user' : 'agent', msg: text,
    };
    Store.state.currentMessages.push(tempMsg);
    Renderer.renderMessagesDiff(Store.state.currentMessages);
    input.value = '';

    const res = await api(`/api/rooms/${roomId}/send`, {
      method: 'POST',
      body: { agent_id: from, text, kind: from === 'user' ? 'user' : 'agent' },
    });
    if (res.ok) {
      // 替换 tempId 为真实 id（下次轮询自然会带上正确 id）
      const el = $(`[data-msg-id="${tempId}"]`);
      if (el && res.message?.id) {
        el.dataset.msgId = res.message.id;
        Store.state.renderedMessageIds.delete(tempId);
        Store.state.renderedMessageIds.add(res.message.id);
      }
    } else {
      Components.toast(res.error || '发送失败', 'error');
      // 失败时移除乐观消息
      const el = $(`[data-msg-id="${tempId}"]`);
      if (el) el.remove();
      const idx = Store.state.currentMessages.findIndex(m => m.id === tempId);
      if (idx >= 0) Store.state.currentMessages.splice(idx, 1);
      Store.state.renderedMessageIds.delete(tempId);
    }
  },

  async copyLogs() {
    const logs = Store.state.currentLogs;
    const text = logs.map(l => `[${l.ts}] ${l.event || ''} ${l.msg || ''}`).join('\n');
    try {
      await navigator.clipboard.writeText(text);
      Components.toast('日志已复制');
    } catch (e) {
      Components.toast('复制失败', 'error');
    }
  },

  /* ── Agent 操作 ── */
  async discoverAgents() {
    Store.set({ discovering: true });
    Components.toast('扫描中…', 'success', 1500);
    const res = await api('/api/agents/discover');
    Store.set({ discovering: false, discoveredAgents: res.ok ? (res.agents || []) : [] });
    if (res.ok) {
      Renderer.renderScanResults(Store.state.discoveredAgents);
      Components.toast(`发现 ${res.agents?.length || 0} 个 Agent`);
    } else {
      Components.toast(res.error || '扫描失败', 'error');
    }
  },

  closeScan() {
    Store.set({ discoveredAgents: null });
    Renderer.renderScanResults(null);
  },

  async addDiscovered(agentJson) {
    try {
      const agent = JSON.parse(agentJson);
      // 追加到现有 agents
      const current = Store.state.agents.slice();
      if (current.find(a => a.id === agent.id)) {
        Components.toast(`Agent @${agent.id} 已存在`, 'warning');
        return;
      }
      const newAgent = {
        id: agent.id,
        display_name: agent.display_name || agent.id,
        color: agent.color || AGENT_COLOR_POOL[current.length % AGENT_COLOR_POOL.length],
        cursor: 'line', filter_from: '',
        wakeup: agent.wakeup || { url: '', method: 'POST', headers: { 'Content-Type': 'application/json' }, body_template: { message: '{{message}}' } },
        adapter: agent.adapter || { type: 'native_http', config: { url: agent.wakeup?.url || '' }, template: { message: '{{message}}' } },
        old_id: '',
      };
      const res = await api('/api/config/full', {
        method: 'POST',
        body: { agents: [...current, newAgent], agent_id: Store.state.config.agent_id, shared_dir: Store.state.config.shared_dir },
      });
      if (res.ok) {
        Components.toast(`已添加 @${agent.id}`);
        await Actions.loadConfig();
      } else {
        Components.toast(res.error || '添加失败', 'error');
      }
    } catch (e) {
      Components.toast('数据格式错误', 'error');
    }
  },

  addAgent() {
    // 弹 modal 让用户输入新 Agent 信息
    Components.modal({
      title: '添加 Agent',
      body: `
        <div class="form-row"><label class="form-label">ID（唯一标识）</label><input class="input mono" id="newAgentId" placeholder="例如：hermes"></div>
        <div class="form-row"><label class="form-label">显示名</label><input class="input" id="newAgentName" placeholder="例如：Hermes Agent"></div>
        <div class="form-row"><label class="form-label">Webhook URL</label><input class="input mono" id="newAgentUrl" placeholder="http://127.0.0.1:8644/..."></div>
      `,
      actions: [
        { label: '取消', kind: 'btn-ghost', onClick: () => false },
        { label: '添加', kind: 'btn-primary', onClick: async (overlay) => {
          const id = $('#newAgentId', overlay)?.value.trim();
          const name = $('#newAgentName', overlay)?.value.trim() || id;
          if (!id || !VALID_ID_RE.test(id)) {
            Components.toast('ID 只允许字母、数字、连字符、下划线', 'error');
            return true;
          }
          const current = Store.state.agents.slice();
          if (current.find(a => a.id === id)) {
            Components.toast(`@${id} 已存在`, 'error');
            return true;
          }
          const newAgent = {
            id, display_name: name, color: AGENT_COLOR_POOL[current.length % AGENT_COLOR_POOL.length],
            cursor: 'line', filter_from: '',
            wakeup: { url: '', method: 'POST', headers: { 'Content-Type': 'application/json' }, body_template: { message: '{{message}}' } },
            adapter: { type: 'native_http', config: { url: '' }, template: { message: '{{message}}' } },
          };
          const res = await api('/api/config/full', {
            method: 'POST',
            body: { agents: [...current, newAgent], agent_id: Store.state.config.agent_id, shared_dir: Store.state.config.shared_dir },
          });
          if (res.ok) {
            Components.toast(`已添加 @${id}`);
            await Actions.loadConfig();
            return false;
          } else {
            Components.toast(res.error || '添加失败', 'error');
            return true;
          }
        }},
      ],
    });
  },

  async saveAgent(agentId) {
    const card = $(`[data-agent-card="${agentId}"]`);
    if (!card) return;
    const get = field => $(`[data-agent-field="${field}"][data-agent-id="${agentId}"]`, card)?.value?.trim() || '';
    const agent = Store.getAgent(agentId);
    if (!agent) return;

    const newId = get('id');
    if (!VALID_ID_RE.test(newId)) {
      Components.toast('ID 只允许字母、数字、连字符、下划线', 'error');
      return;
    }

    const adapterType = get('adapter_type');
    const wakeupUrl = get('wakeup_url');
    const tokenPath = get('token_path');
    const tokenJsonpath = get('token_jsonpath');

    // 用新的单 agent 更新端点
    const body = {
      id: newId,
      old_id: newId !== agentId ? agentId : '',
      display_name: get('display_name'),
      color: get('color'),
      adapter: {
        type: adapterType,
        config: { url: wakeupUrl, method: 'POST', headers: { 'Content-Type': 'application/json' } },
        template: { message: '{{message}}' },
      },
      wakeup: {
        url: wakeupUrl, method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body_template: { message: '{{message}}' },
        ...(tokenPath ? { auth: { type: 'bearer', token_path: tokenPath, token_jsonpath: tokenJsonpath } } : {}),
      },
    };

    const res = await api(`/api/agents/${agentId}`, { method: 'PUT', body });
    if (res.ok) {
      Components.toast(`已保存 @${newId}`);
      await Actions.loadConfig();
      // 如果改名了，路由里的旧 id 也要刷新
      if (newId !== agentId && Store.state.currentRoomId) {
        await Poller.refresh();
      }
    } else {
      Components.toast(res.error || '保存失败', 'error');
    }
  },

  async removeAgent(agentId) {
    const agent = Store.getAgent(agentId);
    if (!agent) return;
    // 检查是否在 running 房间
    const inRunning = Store.state.rooms.filter(r =>
      r.status === 'running' && (r.agents || []).includes(agentId)
    );
    if (inRunning.length) {
      const names = inRunning.map(r => r.name || r.id).join(', ');
      const proceed = await Components.confirm(
        `Agent @${agentId} 正在运行中的房间（${names}）中使用。\n请先暂停这些房间再删除。是否前往聊天室？`,
        { title: '无法删除', okLabel: '前往聊天室', danger: true }
      );
      if (proceed) Router.goChat();
      return;
    }
    const confirmed = await Components.confirm(
      `确认删除 Agent @${agentId}（${agent.display_name}）？\n此操作不可撤销。`,
      { title: '删除 Agent', okLabel: '删除', danger: true }
    );
    if (!confirmed) return;

    // 用全量替换删除
    const remaining = Store.state.agents.filter(a => a.id !== agentId);
    const res = await api('/api/config/full', {
      method: 'POST',
      body: {
        agents: remaining.map(a => ({
          id: a.id, display_name: a.display_name, color: a.color,
          cursor: a.cursor || 'line', filter_from: a.filter_from || '',
          wakeup: a.wakeup || {}, adapter: a.adapter || {},
        })),
        agent_id: Store.state.config.agent_id,
        shared_dir: Store.state.config.shared_dir,
      },
    });
    if (res.ok) {
      Components.toast(`已删除 @${agentId}`);
      await Actions.loadConfig();
    } else {
      Components.toast(res.error || '删除失败', 'error');
    }
  },

  async copyMcpConfig(agentId) {
    // 优先用后端 /api/mcp/config 生成的真实配置（含绝对路径）
    try {
      const res = await api('/api/mcp/config');
      const cfg = res.ok ? JSON.stringify(res.stdio_config, null, 2) : '';
      if (cfg) {
        await navigator.clipboard.writeText(cfg);
        Components.toast('MCP 配置已复制到剪贴板');
        return;
      }
    } catch (e) {}
    // 兜底：用卡片里的 pre 文本
    const pre = $(`#mcpConfig-${agentId}`);
    if (pre) {
      try {
        await navigator.clipboard.writeText(pre.textContent);
        Components.toast('MCP 配置已复制');
      } catch (e) {
        Components.toast('复制失败，请手动选中复制', 'error');
      }
    }
  },

  async testAgent(agentId) {
    const agent = Store.getAgent(agentId);
    if (!agent) return;
    Components.modal({
      title: `测试 ${agent.display_name}`,
      body: `<div id="testAgentResult" style="text-align:center; padding: var(--space-4) 0; color: var(--ink);">测试中…</div>`,
    });
    const res = await api('/api/agent/integration-test', {
      method: 'POST',
      body: { agent_id: agentId, auto_create_room: true, text: 'hello from agent-bridge ui test' },
    });
    const result = $('#testAgentResult');
    if (!result) return;
    if (res.ok) {
      result.innerHTML = `
        <div style="color: var(--moss); margin-bottom: var(--space-3);">✓ 连接成功</div>
        <div class="mono" style="font-size: 11px; color: var(--ink); text-align: left; background: var(--bg-subtle); padding: var(--space-3); border-radius: 6px;">
          turn_id: ${esc(res.turn_id || '—')}<br>
          response_received: ${esc(String(res.response_received ?? false))}<br>
          ${res.result?.detail ? `detail: ${esc(res.result.detail)}` : ''}
        </div>`;
    } else {
      result.innerHTML = `<div style="color: var(--danger); margin-bottom: var(--space-3);">✗ 测试失败</div><div class="muted" style="font-size: 12px;">${esc(res.error || '未知错误')}</div>`;
    }
  },

  /* ── Settings 操作 ── */
  async openSharedDir() {
    const res = await api('/api/open-dir', {
      method: 'POST',
      body: { path: Store.state.settings?.shared_dir || '' },
    });
    if (!res.ok) Components.toast('打开目录失败', 'error');
  },

  async saveSettings() {
    const pollInterval = parseInt($('#settingsPollInterval')?.value);
    const autoPoll = $('#settingsAutoPoll')?.checked;
    const maxLogs = parseInt($('#settingsMaxLogs')?.value);
    const res = await api('/api/settings', {
      method: 'PUT',
      body: { poll_interval: pollInterval, auto_start_poll: autoPoll, max_log_entries: maxLogs },
    });
    if (res.ok) {
      Components.toast('设置已保存');
      await Actions.loadSettings();
    } else {
      Components.toast(res.error || '保存失败', 'error');
    }
  },

  /* ── 搜索 ── */
  search(query) {
    Store.state.searchQuery = query;
    Renderer.renderMessagesDiff(Store.state.currentMessages);
  },

  /* ── 主题切换 ── */
  toggleTheme() {
    const cur = Store.state.theme;
    const next = cur === 'dark' ? 'light' : 'dark';
    document.documentElement.dataset.theme = next;
    Store.state.theme = next;
    try { localStorage.setItem(THEME_KEY, next); } catch (e) {}
    Renderer.renderThemeBtn();
  },

  /* ── 轮询开关 ── */
  async togglePoll() {
    const running = Store.state.pollStatus?.running;
    const action = running ? 'stop' : 'start';
    const res = await api(`/api/poll/${action}`, { method: 'POST', body: {} });
    if (res.ok) {
      Components.toast(running ? '已停止轮询' : '已启动轮询');
      await Poller.loadGlobal();
    }
  },
};

/* ═══ Events ═════════════════════════════════════════════════════
   顶层事件委托：document 单点监听，data-action 分发
   替代 inline onclick + 全局函数
   ════════════════════════════════════════════════════════════════ */
const Events = {
  init() {
    // click 委托
    document.addEventListener('click', (e) => {
      const target = e.target.closest('[data-action]');
      if (!target) return;
      const action = target.dataset.action;
      const agent = target.dataset.agent;
      const handler = this.handlers[action];
      if (handler) {
        e.preventDefault();
        handler.call(this, target, e);
      }
    });

    // Enter 键发送消息（compose 输入框）
    document.addEventListener('keydown', (e) => {
      if (e.key === 'Enter' && e.target?.id === 'composeInput' && !e.shiftKey) {
        e.preventDefault();
        const btn = $('[data-action="sendMessage"]');
        if (btn) btn.click();
      }
    });

    // 搜索框防抖
    let searchTimer;
    document.addEventListener('input', (e) => {
      if (e.target?.dataset.action === 'search') {
        clearTimeout(searchTimer);
        searchTimer = setTimeout(() => Actions.search(e.target.value), 200);
      }
    });
  },

  handlers: {
    switchTab(target) { Actions.switchTab(target.dataset.tabName); },
    newRoom() { Actions.newRoom(); },
    newRoomInput(target, e) {
      if (e.type === 'keydown' && e.key === 'Enter') Actions.createRoom(target.value.trim());
    },
    openRoom(target) {
      const id = target.dataset.roomId || target.dataset.roomIdTarget;
      if (id) Actions.openRoom(id);
    },
    toggleRoom(target) {
      const id = target.dataset.roomId;
      const status = target.dataset.status;
      if (id && status) Actions.toggleRoom(id, status);
    },
    deleteRoom(target) {
      Actions.deleteRoom(target.dataset.roomId, target.dataset.roomName);
    },
    tickRoom(target) { Actions.tickRoom(target.dataset.roomId); },
    backToRooms() { Actions.backToRooms(); },
    sendMessage(target) { Actions.sendMessage(target.dataset.roomId); },
    copyLogs() { Actions.copyLogs(); },
    refreshRoom() { Actions.refreshRoom(); },
    toggleTheme() { Actions.toggleTheme(); },
    togglePoll() { Actions.togglePoll(); },
    discoverAgents() { Actions.discoverAgents(); },
    closeScan() { Actions.closeScan(); },
    addDiscovered(target) { Actions.addDiscovered(target.dataset.agent); },
    addAgent() { Actions.addAgent(); },
    saveAgent(target) { Actions.saveAgent(target.dataset.agentId); },
    removeAgent(target) { Actions.removeAgent(target.dataset.agentId); },
    testAgent(target) { Actions.testAgent(target.dataset.agentId); },
    copyMcpConfig(target) { Actions.copyMcpConfig(target.dataset.agentId); },
    saveSettings() { Actions.saveSettings(); },
    openSharedDir() { Actions.openSharedDir(); },
    closeModal() { target.closest('.modal-overlay')?.remove(); },
  },
};

/* ═══ 启动 ══════════════════════════════════════════════════════ */
function boot() {
  Events.init();
  Router.init();
  Poller.init();
  Renderer.renderThemeBtn();
  // 首屏立即拉一次数据
  Poller.refresh().then(() => {
    Renderer.renderTab();
  });
}

if (document.readyState === 'loading') {
  document.addEventListener('DOMContentLoaded', boot);
} else {
  boot();
}
