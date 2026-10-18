// app.js - Controller for PM & QA Cockpit

// Session token handling for remote/tunnel connections
(function initTokenAuth() {
  try {
    const params = new URLSearchParams(window.location.search);
    const token = params.get('token');
    if (token) {
      sessionStorage.setItem('cockpit_token', token);
      params.delete('token');
      const newQuery = params.toString() ? '?' + params.toString() : '';
      const cleanUrl = window.location.pathname + newQuery + window.location.hash;
      window.history.replaceState({}, document.title, cleanUrl);
    }
  } catch (_) {}

  // Authenticate all fetch calls when session token is stored
  const origFetch = window.fetch;
  window.fetch = function(url, options) {
    const token = sessionStorage.getItem('cockpit_token');
    if (token) {
      options = options || {};
      const headers = new Headers(options.headers || {});
      if (!headers.has('X-Cockpit-Token')) {
        headers.set('X-Cockpit-Token', token);
      }
      options.headers = headers;
    }
    return origFetch.apply(this, [url, options]);
  };
})();

// -----------------------------------------------------------------------------
// Phosphor Icon Mapping & Compatibility Engine
// -----------------------------------------------------------------------------
const LUCIDE_TO_PHOSPHOR = {
  'shield-check': 'ph-shield-check',
  'shield-alert': 'ph-shield-warning',
  'folder-git-2': 'ph-folder-notch',
  'chevrons-up-down': 'ph-caret-up-down',
  'plus': 'ph-plus',
  'refresh-cw': 'ph-arrows-clockwise',
  'rotate-cw': 'ph-arrow-clockwise',
  'qr-code': 'ph-qr-code',
  'kanban': 'ph-kanban',
  'list-checks': 'ph-check-square-offset',
  'list-ordered': 'ph-list-numbers',
  'list-plus': 'ph-list-plus',
  'milestone': 'ph-signpost',
  'activity': 'ph-pulse',
  'search': 'ph-magnifying-glass',
  'book-open': 'ph-book-open',
  'file-check': 'ph-file-check',
  'file-diff': 'ph-file-text',
  'git-commit': 'ph-git-commit',
  'git-branch': 'ph-git-branch',
  'git-merge': 'ph-git-merge',
  'calendar': 'ph-calendar-blank',
  'check': 'ph-check',
  'check-circle': 'ph-check-circle',
  'check-circle-2': 'ph-check-circle',
  'check-square': 'ph-check-square',
  'check-check': 'ph-checks',
  'x': 'ph-x',
  'x-circle': 'ph-x-circle',
  'alert-circle': 'ph-warning-circle',
  'layers': 'ph-stack',
  'clock': 'ph-clock',
  'history': 'ph-clock-counter-clockwise',
  'play': 'ph-play',
  'play-circle': 'ph-play-circle',
  'square': 'ph-stop',
  'copy': 'ph-copy',
  'external-link': 'ph-arrow-square-out',
  'bot': 'ph-robot',
  'sparkles': 'ph-sparkle',
  'clipboard-check': 'ph-clipboard-text',
  'alert-triangle': 'ph-warning',
  'loader': 'ph-spinner',
  'loader-2': 'ph-spinner',
  'filter': 'ph-funnel',
};

window.lucide = {
  createIcons: function () {
    document.querySelectorAll('[data-lucide]').forEach((el) => {
      const name = el.getAttribute('data-lucide');
      const phClass = LUCIDE_TO_PHOSPHOR[name] || `ph-${name}`;
      el.classList.add('ph', phClass);
      el.removeAttribute('data-lucide');
    });
  },
};

let state = {
  cards: [],
  sprints: [],
  activity: {},
  activeTab: 'pipeline',
  selectedQaId: null,
  activeSprintId: 'ACTIVE',
  sprintDodData: null,
  heavyGateWorker: null,
  heavyGateInfo: null,
  sprintGateStatus: [],
  sprintAuditShowAll: false,
  sprintAuditFilter: 'needs_attention',
  currentReviews: [],
  currentReviewIndex: 0,
  changelogSortOrder: 'asc',
  roadmapFilter: 'ALL',
  roadmapSearch: '',
  projects: [],
  currentProject: null,
  activeProjectSlug: null,
  projectsError: null,
  filters: {
    sprint: 'ALL',
    type: 'ALL',
    search: '',
  },
};


// -----------------------------------------------------------------------------
// Visual Loading & Feedback Helpers
// -----------------------------------------------------------------------------
function showLoadingFeedback(isLoading) {
  const bar = document.getElementById('loadingBar');
  if (bar) {
    if (isLoading) bar.classList.remove('opacity-0');
    else bar.classList.add('opacity-0');
  }
  const icon = document.getElementById('refreshIcon');
  if (icon) {
    if (isLoading) icon.classList.add('animate-spin');
    else icon.classList.remove('animate-spin');
  }
}

function renderLoadingSkeletons() {
  const cols = ['backlog', 'sprint-backlog', 'in-progress', 'qa', 'completed'];
  cols.forEach((colId) => {
    const el = document.getElementById(`cards-${colId}`);
    if (el && state.cards.length === 0) {
      el.innerHTML = `
        <div class="board-skeleton-card p-3.5 rounded-xl bg-slate-900/60 border border-slate-800/80 animate-pulse space-y-2.5 my-2">
          <div class="flex justify-between items-center">
            <div class="h-3 bg-slate-800 rounded w-16"></div>
            <div class="h-3 bg-slate-800 rounded w-12"></div>
          </div>
          <div class="h-4 bg-slate-800/80 rounded w-4/5"></div>
          <div class="h-2 bg-slate-800/50 rounded w-full"></div>
          <div class="h-2 bg-slate-800/40 rounded w-1/3 pt-1"></div>
        </div>
        <div class="board-skeleton-card p-3.5 rounded-xl bg-slate-900/40 border border-slate-800/50 animate-pulse space-y-2.5 my-2 opacity-50">
          <div class="flex justify-between items-center">
            <div class="h-3 bg-slate-800 rounded w-14"></div>
            <div class="h-3 bg-slate-800 rounded w-10"></div>
          </div>
          <div class="h-4 bg-slate-800/80 rounded w-3/5"></div>
        </div>
      `;
    }
  });
}

// -----------------------------------------------------------------------------
// Data Fetching
// -----------------------------------------------------------------------------
async function fetchAll() {
  showLoadingFeedback(true);
  if (state.cards.length === 0) {
    renderLoadingSkeletons();
  }

  const errBanner = document.getElementById('globalErrorBanner');
  if (errBanner) errBanner.classList.add('hidden');

  try {
    const [cardsRes, sprintsRes, actRes] = await Promise.all([
      fetch('/api/cards').then((r) => {
        if (!r.ok) throw new Error(`HTTP ${r.status} fetching cards`);
        return r.json();
      }),
      fetch('/api/sprints').then((r) => {
        if (!r.ok) throw new Error(`HTTP ${r.status} fetching sprints`);
        return r.json();
      }),
      fetch('/api/activity').then((r) => {
        if (!r.ok) throw new Error(`HTTP ${r.status} fetching activity`);
        return r.json();
      }),
    ]);

    state.cards = cardsRes.cards || [];
    state.sprints = sprintsRes.roadmap || [];
    state.activity = actRes || {};

    try { updateHeader(); } catch (e) { console.error('Error in updateHeader:', e); }
    try { populateSprintFilter(); } catch (e) { console.error('Error in populateSprintFilter:', e); }
    try { renderPipeline(); } catch (e) { console.error('Error in renderPipeline:', e); }
    try { renderQaStation(); } catch (e) { console.error('Error in renderQaStation:', e); }
    try { renderRoadmap(); } catch (e) { console.error('Error in renderRoadmap:', e); }
    try { renderActivity(); } catch (e) { console.error('Error in renderActivity:', e); }

    await Promise.allSettled([
      fetchActiveSprint(),
      fetchHeavyGateStatus(),
      fetchSprintGateStatus(),
    ]);
  } catch (err) {
    console.error('Failed to load dashboard data:', err);
    if (errBanner) {
      const errMsg = document.getElementById('globalErrorMessage');
      if (errMsg) errMsg.textContent = `Connection error: ${err.message || 'Server unreachable'}. Check that server is running.`;
      errBanner.classList.remove('hidden');
    }
  } finally {
    showLoadingFeedback(false);
    if (window.lucide) lucide.createIcons();
  }
}

// -----------------------------------------------------------------------------
// Header & Metrics
// -----------------------------------------------------------------------------
function updateHeader() {
  renderProjectHeader();
  document.getElementById('headerBranch').textContent = `Branch: ${state.activity.branch || 'unknown'}`;
  document.getElementById('headerTotalCount').textContent = state.cards.length;

  const qaCards = state.cards.filter((c) => c.status === 'qa');
  document.getElementById('headerQaCount').textContent = qaCards.length;

  const qaBadge = document.getElementById('qaTabBadge');
  if (qaCards.length > 0) {
    qaBadge.textContent = qaCards.length;
    qaBadge.classList.remove('hidden');
  } else {
    qaBadge.classList.add('hidden');
  }

  // Find latest active sprint
  let activeSprintTitle = 'None';
  if (state.sprintDodData && state.sprintDodData.sprint_name) {
    activeSprintTitle = state.sprintDodData.sprint_name;
  } else {
    const revRoadmap = [...state.sprints].reverse();
    const active = revRoadmap.find(
      (s) =>
        !s.is_complete &&
        !s.is_partial &&
        !s.sprint.toLowerCase().includes('backlog') &&
        !s.sprint.toLowerCase().includes('unscheduled')
    );
    activeSprintTitle = active
      ? active.sprint
      : state.sprints.length > 0
        ? state.sprints[state.sprints.length - 1].sprint
        : 'None';
  }
  document.getElementById('headerActiveSprint').textContent = storyLabel(activeSprintTitle);
}

function populateSprintFilter() {
  const select = document.getElementById('pipelineSprintFilter');
  const currentVal = select.value;
  const sprints = new Set();
  state.cards.forEach((c) => {
    if (c.sprint) sprints.add(c.sprint);
  });

  select.innerHTML = '<option value="ALL">All Stories</option>';
  Array.from(sprints)
    .sort()
    .forEach((s) => {
      const opt = document.createElement('option');
      opt.value = s;
      opt.textContent = `Story ${s}`;
      select.appendChild(opt);
    });

  select.value = currentVal;
}

// -----------------------------------------------------------------------------
// Pipeline (Kanban) Board
// -----------------------------------------------------------------------------
function renderPipeline() {
  const cols = {
    backlog: document.getElementById('cards-backlog'),
    'sprint-backlog': document.getElementById('cards-sprint-backlog'),
    'in-progress': document.getElementById('cards-in-progress'),
    qa: document.getElementById('cards-qa'),
    completed: document.getElementById('cards-completed'),
  };

  const counts = {
    backlog: document.getElementById('count-backlog'),
    'sprint-backlog': document.getElementById('count-sprint-backlog'),
    'in-progress': document.getElementById('count-in-progress'),
    qa: document.getElementById('count-qa'),
    completed: document.getElementById('count-completed'),
  };

  // Clear containers
  Object.values(cols).forEach((el) => {
    if (el) el.innerHTML = '';
  });

  const filtered = state.cards.filter((card) => {
    if (state.filters.sprint !== 'ALL' && card.sprint !== state.filters.sprint) return false;
    if (state.filters.type !== 'ALL' && card.type !== state.filters.type) return false;
    if (state.filters.search) {
      const q = state.filters.search.toLowerCase();
      const matchId = card.mn_id.toLowerCase().includes(q);
      const matchTitle = card.title.toLowerCase().includes(q);
      const matchDesc = card.description.toLowerCase().includes(q);
      if (!matchId && !matchTitle && !matchDesc) return false;
    }
    return true;
  });

  // Sort cards by created_date desc (newest first), tie-breaking by ID asc
  filtered.sort((a, b) => {
    const dateA = a.created_date || '';
    const dateB = b.created_date || '';
    if (dateB !== dateA) return dateB.localeCompare(dateA);
    return (a.mn_id || '').localeCompare(b.mn_id || '');
  });

  const colCounts = { backlog: 0, 'sprint-backlog': 0, 'in-progress': 0, qa: 0, completed: 0 };

  filtered.forEach((card) => {
    const status = card.status || 'backlog';
    if (cols[status]) {
      colCounts[status]++;
      const cardEl = createCardElement(card);
      cols[status].appendChild(cardEl);
    }
  });

  // Update column count badges
  Object.entries(counts).forEach(([k, el]) => {
    if (el) el.textContent = colCounts[k] || 0;
    const mEl = document.getElementById(`mcount-${k}`);
    if (mEl) mEl.textContent = colCounts[k] || 0;
  });
}

function createCardElement(card) {
  const div = document.createElement('div');
  div.className = 'task-card group';
  div.draggable = true;
  div.addEventListener('dragstart', (e) => {
    e.dataTransfer.setData('text/plain', card.mn_id);
    div.classList.add('opacity-40');
  });
  div.addEventListener('dragend', () => {
    div.classList.remove('opacity-40');
    document.querySelectorAll('.board-column').forEach((col) => col.classList.remove('drag-over'));
  });
  div.onclick = () => {
    if (card.status === 'qa') {
      switchTab('qa');
      selectQaCard(card.mn_id, { forceScrollTop: true });
    } else {
      openCardModal(card);
    }
  };

  let typeClass = 'pill-feature';
  if (card.type === 'bug') typeClass = 'pill-bug';
  if (card.type === 'improvement') typeClass = 'pill-improvement';

  const dodBar =
    card.dod_total > 0
      ? `
    <div class="mt-2.5">
      <div class="flex justify-between text-[10px] text-[var(--text-tertiary)] mb-1">
        <span>DoD Criteria</span>
        <span class="font-mono tabular-nums">${card.dod_checked}/${card.dod_total}</span>
      </div>
      <div class="w-full bg-[var(--bg-surface-tertiary)] rounded-full h-1.5 overflow-hidden">
        <div class="bg-emerald-500 h-1.5 rounded-full transition-all" style="width: ${card.progress_pct}%"></div>
      </div>
    </div>
  `
      : '';

  const escId = escapeHtml(card.mn_id);
  const escTitle = escapeHtml(card.title);
  const escSprint = escapeHtml(card.sprint);
  const escType = escapeHtml(card.type);
  const escDate = escapeHtml(card.created_date || 'N/A');

  div.innerHTML = `
    <div class="flex items-center justify-between gap-1 mb-1.5">
      <span class="text-xs font-mono font-bold text-[var(--text-tertiary)]">${escId}</span>
      <div class="flex items-center gap-1">
        ${card.sprint ? `<span class="text-[10px] font-semibold px-1.5 py-0.5 rounded bg-[var(--bg-surface-secondary)] text-[var(--text-secondary)] border border-[var(--border-subtle)]">Story ${escSprint}</span>` : ''}
        <span class="text-[10px] font-semibold px-1.5 py-0.5 rounded ${typeClass}">${escType}</span>
      </div>
    </div>
    <h3 class="text-sm font-semibold text-[var(--text-primary)] line-clamp-2 group-hover:text-emerald-500 transition-colors">${escTitle}</h3>
    ${dodBar}
    <div class="mt-2 pt-1.5 border-t border-[var(--border-subtle)] flex items-center justify-between text-xs text-[var(--text-tertiary)] font-mono">
      <span class="flex items-center"><i class="ph ph-calendar-blank text-[var(--text-quaternary)] mr-1 text-xs"></i>${escDate}</span>
      ${card.validated ? `<span class="text-emerald-500 flex items-center"><i class="ph ph-check mr-0.5 text-xs"></i>Done</span>` : ''}
    </div>
    ${closeoutButtonHtml(card, 'mt-2 w-full justify-center')}
  `;
  bindCloseoutButton(div, card);

  return div;
}

// -----------------------------------------------------------------------------
// QA Testing Station
// -----------------------------------------------------------------------------
function getCardCompletedDate(card) {
  if (card.end_time) return card.end_time;
  if (card.changelog && card.changelog.length > 0) {
    const last = card.changelog[card.changelog.length - 1];
    const dt = last.datetime || `${last.date || ''} ${last.time || ''}`.trim();
    if (dt) return dt;
  }
  if (card.updated_at) return String(card.updated_at).replace('T', ' ').slice(0, 16);
  if (card.created_date) return `${card.created_date} 00:00`;
  return '';
}

function renderQaStation() {
  const qaCards = state.cards
    .filter((c) => c.status === 'qa')
    .sort((a, b) => {
      const dateA = getCardCompletedDate(a);
      const dateB = getCardCompletedDate(b);
      const cmp = dateB.localeCompare(dateA);
      if (cmp !== 0) return cmp;
      return (b.mn_id || '').localeCompare(a.mn_id || '');
    });
  document.getElementById('qaStationCount').textContent = qaCards.length;

  const container = document.getElementById('qaListContainer');
  const prevListScroll = container ? container.scrollTop : 0;
  container.innerHTML = '';

  if (qaCards.length === 0) {
    container.innerHTML = `
      <div class="p-4 text-center text-slate-500 text-xs">
        <i data-lucide="sparkles" class="w-6 h-6 mx-auto mb-2 text-emerald-400/60"></i>
        No cards currently awaiting QA!
      </div>
    `;
    return;
  }

  qaCards.forEach((card) => {
    const item = document.createElement('div');
    const isSelected = state.selectedQaId === card.mn_id;
    item.dataset.qaCardId = card.mn_id;
    item.className = `p-3.5 rounded-lg border cursor-pointer transition ${
      isSelected
        ? 'bg-slate-800 border-amber-500/50 shadow-md ring-1 ring-amber-500/30'
        : 'bg-slate-900 border-slate-800 hover:border-slate-700'
    }`;
    item.onclick = () => {
      const isNew = state.selectedQaId !== card.mn_id;
      selectQaCard(card.mn_id, { forceScrollTop: isNew });
    };

    const escId = escapeHtml(card.mn_id);
    const escSprint = escapeHtml(card.sprint || 'N/A');
    const escTitle = escapeHtml(card.title);
    const completedDate = getCardCompletedDate(card);
    const displayDate = completedDate || card.created_date;

    item.innerHTML = `
      <div class="flex items-center justify-between mb-1.5">
        <span class="font-mono font-bold text-amber-400 text-xs">${escId}</span>
        <span class="text-xs px-2 py-0.5 rounded bg-slate-800 text-slate-300 border border-slate-700/60">Story ${escSprint}</span>
      </div>
      <p class="font-semibold text-slate-200 text-sm line-clamp-1">${escTitle}</p>
      <div class="mt-2.5 flex items-center justify-between text-xs text-slate-400 font-mono">
        <span>DoD: ${card.dod_checked}/${card.dod_total}</span>
        ${displayDate ? `<span class="text-slate-400 flex items-center gap-1" title="Completed: ${escapeHtml(displayDate)}"><i data-lucide="calendar" class="w-3.5 h-3.5 text-slate-500"></i>${escapeHtml(displayDate)}</span>` : ''}
        <span class="text-amber-400 flex items-center"><i data-lucide="clock" class="w-3.5 h-3.5 mr-1"></i> Ready for QA</span>
      </div>
      ${closeoutButtonHtml(card, 'mt-2')}
    `;
    bindCloseoutButton(item, card);

    container.appendChild(item);
  });

  if (container && prevListScroll > 0) {
    container.scrollTop = prevListScroll;
  }

  // If none selected, auto-select first card and move to top
  if (!state.selectedQaId && qaCards.length > 0) {
    selectQaCard(qaCards[0].mn_id, { forceScrollTop: true });
  } else if (state.selectedQaId) {
    const stillInQa = qaCards.some((c) => c.mn_id === state.selectedQaId);
    if (stillInQa) {
      selectQaCard(state.selectedQaId, { preserveScroll: true });
    } else if (qaCards.length > 0) {
      selectQaCard(qaCards[0].mn_id, { forceScrollTop: true });
    }
  }
}

function selectQaCard(mn_id, { forceScrollTop = false, preserveScroll = false } = {}) {
  const detailEl = document.getElementById('qaDetailContainer');
  const prevScrollTop = detailEl ? detailEl.scrollTop : 0;
  const isNewCard = state.selectedQaId !== mn_id;
  const shouldScrollToTop = forceScrollTop || (isNewCard && !preserveScroll);

  state.selectedQaId = mn_id;
  const card = state.cards.find((c) => c.mn_id === mn_id);

  // Update selection highlight in the sidebar list
  const qaContainer = document.getElementById('qaListContainer');
  if (qaContainer) {
    qaContainer.querySelectorAll('[data-qa-card-id]').forEach((el) => {
      const isSelected = el.dataset.qaCardId === mn_id;
      el.className = `p-3.5 rounded-lg border cursor-pointer transition ${
        isSelected
          ? 'bg-slate-800 border-amber-500/50 shadow-md ring-1 ring-amber-500/30'
          : 'bg-slate-900 border-slate-800 hover:border-slate-700'
      }`;
    });
  }

  if (!card) {
    if (detailEl) {
      detailEl.innerHTML = `
        <div class="h-full flex flex-col items-center justify-center text-slate-500">
          <p class="text-base">Select a card to view verification details.</p>
        </div>
      `;
      if (shouldScrollToTop) {
        detailEl.scrollTop = 0;
      }
    }
    return;
  }

  // Preserve user notes only if we are re-rendering the same card
  const existingNotesEl = document.getElementById('qaValidateNotes');
  const preservedNote = (!isNewCard && existingNotesEl) ? existingNotesEl.value : '';

  // Render detail view
  const escId = escapeHtml(card.mn_id);
  const escSprint = escapeHtml(card.sprint);
  const escType = escapeHtml(card.type);
  const escTitle = escapeHtml(card.title);
  const escFileName = escapeHtml(card.file_name);
  const escDesc = escapeHtml(card.description || 'No description provided.');

  const changelogHtml =
    card.changelog && card.changelog.length > 0
      ? `
    <div class="mb-6">
      <h4 class="text-sm font-bold text-slate-300 uppercase tracking-wider mb-2 flex items-center gap-1.5">
        <i data-lucide="git-commit" class="w-4 h-4 text-emerald-400"></i>
        <span>Implementation Changelog & Evidence (${card.changelog.length})</span>
      </h4>
      ${renderChangelogTable(card.changelog, card)}
    </div>
  `
      : '';

  detailEl.innerHTML = `
    <div class="flex items-start justify-between border-b border-slate-800 pb-4 mb-4">
      <div>
        <div class="flex items-center space-x-2 mb-2 flex-wrap gap-y-1">
          <span class="text-sm font-mono font-bold px-2.5 py-0.5 rounded bg-amber-500/10 text-amber-400 border border-amber-500/20">${escId}</span>
          <span class="text-xs font-semibold px-2.5 py-0.5 rounded bg-slate-800 text-slate-300">Story ${escSprint}</span>
          <span class="text-xs font-semibold px-2.5 py-0.5 rounded bg-blue-500/10 text-blue-400 border border-blue-500/20">${escType}</span>
          <span class="text-xs font-mono px-2.5 py-0.5 rounded bg-slate-800/80 text-slate-400 border border-slate-700/60 flex items-center gap-1">
            <i data-lucide="calendar" class="w-3.5 h-3.5 text-slate-500"></i>
            <span>Created: ${escapeHtml(card.created_date || 'N/A')}</span>
          </span>
          ${
            card.sprint
              ? `<button data-action="open-sprint-doc" data-sprint="${escSprint}" class="inline-flex items-center gap-1.5 px-2.5 py-0.5 rounded bg-slate-800 hover:bg-slate-700 text-slate-200 border border-slate-700 hover:border-slate-600 text-xs font-medium transition ml-1 group">
                  <i data-lucide="book-open" class="w-3.5 h-3.5 text-emerald-400 group-hover:scale-110 transition-transform"></i>
                  <span>View Story Spec</span>
                </button>`
              : ''
          }
          ${closeoutButtonHtml(card, '', true)}
          ${
            (card.review_count > 0 || (card.reviews && card.reviews.length > 0))
              ? `<button onclick="openReviewModal('${escId}')" class="inline-flex items-center gap-1.5 px-2.5 py-0.5 rounded bg-purple-950/40 hover:bg-purple-900/50 text-purple-300 border border-purple-500/30 hover:border-purple-400 text-xs font-medium transition ml-1 group">
                  <i data-lucide="shield-check" class="w-3.5 h-3.5 text-purple-400 group-hover:scale-110 transition-transform"></i>
                  <span>Reviews (${card.review_count || card.reviews.length})</span>
                </button>`
              : ''
          }
        </div>
        <h2 class="text-xl font-bold text-[var(--text-primary)] tracking-tight">${escTitle}</h2>
      </div>
      <div class="text-right text-xs text-[var(--text-tertiary)] font-mono">
        <div>${escFileName}</div>
        ${card.created_date ? `<div class="text-xs text-[var(--text-tertiary)] mt-0.5">Created: ${escapeHtml(card.created_date)}</div>` : ''}
      </div>
    </div>

    <!-- Description -->
    <div class="mb-6">
      <h4 class="text-sm font-bold text-[var(--text-secondary)] uppercase tracking-wider mb-2">Description / User Intent</h4>
      <div class="bg-[var(--bg-subtle)] p-4 rounded-lg border border-[var(--border-subtle)] break-words text-[var(--text-primary)]">
        ${renderMarkdown(card.description || 'No description provided.')}
      </div>
    </div>

    <!-- Acceptance Criteria (Interactive Checkboxes) -->
    <div class="mb-6">
      <div class="flex items-center justify-between mb-2">
        <h4 class="text-sm font-bold text-[var(--text-secondary)] uppercase tracking-wider">Definition of Done / Acceptance Criteria</h4>
        <span class="text-sm font-mono text-emerald-500 font-bold">${card.dod_checked}/${card.dod_total} Complete</span>
      </div>
      <div class="bg-[var(--bg-subtle)] p-3.5 rounded-lg border border-[var(--border-subtle)] space-y-2">
        ${
          card.dod.length > 0
            ? card.dod
                .map((item, idx) => {
                  const isPending = !item.checked;
                  const itemClass = isPending ? 'qa-pending-item p-2' : 'p-2 hover:bg-[var(--bg-surface-secondary)]';
                  return `
          <label class="flex items-start justify-between space-x-3 rounded cursor-pointer text-sm transition touch-target-item ${itemClass}">
            <div class="flex items-start space-x-3 flex-1">
              <input type="checkbox" ${item.checked ? 'checked' : ''} data-action="toggle-dod" data-card-id="${escId}" data-idx="${idx}" class="mt-0.5 w-4 h-4 rounded border-[var(--border-medium)] text-emerald-500 focus:ring-emerald-500/20 bg-[var(--bg-surface)] cursor-pointer">
              <span class="${item.checked ? 'text-[var(--text-tertiary)] line-through' : 'text-[var(--text-primary)] font-medium'} text-sm leading-relaxed">${formatRichText(item.text)}</span>
            </div>
            ${isPending ? '<span class="text-[10px] font-semibold px-2 py-0.5 rounded bg-amber-500/20 text-amber-600 dark:text-amber-300 ml-2 whitespace-nowrap border border-amber-500/30">Needs Live Validation</span>' : ''}
          </label>
        `;
                })
                .join('')
            : '<p class="text-sm text-[var(--text-tertiary)] italic">No checklist items defined.</p>'
        }
      </div>
    </div>

    <!-- Human QA Test Steps -->
    ${(() => {
      const hasInstr = card.qa_instructions && card.qa_instructions.trim();
      const hasItems = card.qa_items && card.qa_items.length > 0;
      if (!hasInstr && !hasItems) {
        return `
    <div class="mb-6">
      <h4 class="text-sm font-bold text-amber-500 uppercase tracking-wider mb-2 flex items-center gap-1.5">
        <i data-lucide="clipboard-list" class="w-4 h-4"></i>
        <span>Human QA Test Steps</span>
      </h4>
      <div class="bg-amber-500/5 border border-amber-500/25 rounded-lg p-4 flex items-start gap-3">
        <i data-lucide="alert-triangle" class="w-4 h-4 text-amber-500 mt-0.5 shrink-0"></i>
        <div class="text-sm text-amber-700 dark:text-amber-200/90 leading-relaxed">
          <span class="font-semibold">No Human QA Test section found in this card.</span><br>
          Ask the dev agent to add a <code class="font-mono text-amber-600 dark:text-amber-300 bg-amber-500/10 px-1 rounded text-xs">## Human QA Test</code> section with explicit step-by-step instructions (where to go, what to click, what to verify).
        </div>
      </div>
    </div>`;
      }
      const instrHtml = hasInstr
        ? `<div class="bg-[var(--bg-subtle)] p-4 rounded-lg border border-[var(--border-subtle)] mb-3 text-[var(--text-primary)]">${renderMarkdown(card.qa_instructions)}</div>`
        : '';
      const itemsHtml = hasItems
        ? `<div class="bg-[var(--bg-subtle)] p-3.5 rounded-lg border border-[var(--border-subtle)] space-y-2">
            ${card.qa_items.map((item, idx) => `
              <label class="flex items-start space-x-3 rounded cursor-pointer text-sm transition p-2 hover:bg-[var(--bg-surface-secondary)] touch-target-item">
                <input type="checkbox" ${item.checked ? 'checked' : ''} data-action="toggle-qa" data-card-id="${escapeHtml(card.mn_id)}" data-idx="${idx}" class="mt-0.5 w-4 h-4 rounded border-amber-600/50 text-amber-500 focus:ring-amber-500/20 bg-[var(--bg-surface)] cursor-pointer">
                <span class="${item.checked ? 'text-[var(--text-tertiary)] line-through' : 'text-[var(--text-primary)] font-medium'} text-sm leading-relaxed">${formatRichText(item.text)}</span>
              </label>`).join('')}
          </div>`
        : '';
      return `
    <div class="mb-6">
      <h4 class="text-sm font-bold text-amber-500 uppercase tracking-wider mb-2 flex items-center gap-1.5">
        <i data-lucide="clipboard-list" class="w-4 h-4"></i>
        <span>Human QA Test Steps</span>
        ${hasItems ? `<span class="text-xs px-2 py-0.5 rounded font-semibold bg-amber-500/10 text-amber-600 dark:text-amber-300 border border-amber-500/20 ml-1.5">${card.qa_items.filter(i => i.checked).length}/${card.qa_items.length} done</span>` : ''}
      </h4>
      ${instrHtml}${itemsHtml}
    </div>`;
    })()}

    <!-- Implementation Changelog & Evidence -->
    ${changelogHtml}

    <!-- Tester Verification Action -->
    <div class="mt-auto pt-5 border-t border-[var(--border-subtle)]">
      <h4 class="text-sm font-bold text-[var(--text-secondary)] uppercase tracking-wider mb-2">QA Sign-off</h4>
      <div class="flex flex-col sm:flex-row gap-3">
        <input type="text" id="qaValidateNotes" placeholder="Optional QA validation note (e.g. 'Verified Wise import flow live')" class="flex-1 bg-[var(--bg-surface)] text-[var(--text-primary)] text-sm rounded-lg px-3.5 py-2.5 border border-[var(--border-medium)] focus:outline-none focus:border-emerald-500">
        <button data-action="validate-card" data-card-id="${escId}" class="px-5 py-2.5 bg-emerald-600 hover:bg-emerald-500 text-white text-sm font-bold rounded-lg shadow-lg shadow-emerald-900/30 flex items-center justify-center space-x-2 transition">
          <i data-lucide="check-check" class="w-4 h-4"></i>
          <span>Validate & Complete Work</span>
        </button>
      </div>
    </div>
  `;

  // Scroll management: ONLY move to top when a new card is clicked to be viewed
  if (detailEl) {
    if (shouldScrollToTop) {
      detailEl.scrollTop = 0;
      requestAnimationFrame(() => {
        if (detailEl) detailEl.scrollTop = 0;
      });
    } else if (prevScrollTop > 0) {
      detailEl.scrollTop = prevScrollTop;
      requestAnimationFrame(() => {
        if (detailEl) detailEl.scrollTop = prevScrollTop;
      });
    }
  }

  // Restore preserved user input (for current card)
  if (preservedNote) {
    const newNotesEl = document.getElementById('qaValidateNotes');
    if (newNotesEl) newNotesEl.value = preservedNote;
  }

  if (window.lucide) lucide.createIcons();
}

async function toggleDod(mn_id, index, checked) {
  try {
    const res = await fetch(`/api/cards/${mn_id}/dod`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ index, checked }),
    }).then((r) => r.json());

    if (res.success) {
      await fetchAll();
      const modal = document.getElementById('cardModal');
      if (modal && !modal.classList.contains('hidden')) {
        const updatedCard = state.cards.find((c) => c.mn_id === mn_id);
        if (updatedCard) openCardModal(updatedCard);
      }
    } else {
      alert(`Error updating DoD: ${res.error || 'Server error'}`);
      await fetchAll();
    }
  } catch (err) {
    console.error('Failed to toggle DoD item:', err);
    alert('Failed to communicate with server.');
    await fetchAll();
  }
}

async function toggleQa(mn_id, index, checked) {
  try {
    const res = await fetch(`/api/cards/${mn_id}/qa-item`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ index, checked }),
    }).then((r) => r.json());

    if (res.success) {
      await fetchAll();
      const modal = document.getElementById('cardModal');
      if (modal && !modal.classList.contains('hidden')) {
        const updatedCard = state.cards.find((c) => c.mn_id === mn_id);
        if (updatedCard) openCardModal(updatedCard);
      }
    } else {
      alert(`Error updating QA item: ${res.error || 'Server error'}`);
      await fetchAll();
    }
  } catch (err) {
    console.error('Failed to toggle QA item:', err);
    alert('Failed to communicate with server.');
    await fetchAll();
  }
}

async function validateCard(mn_id) {
  const notesInput = document.getElementById('qaValidateNotes');
  const notes = notesInput ? notesInput.value : '';

  if (!confirm(`Mark ${mn_id} as verified and completed? This will stamp frontmatter and create a local git commit.`)) {
    return;
  }

  try {
    const res = await fetch(`/api/cards/${mn_id}/validate`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ notes, commit: true }),
    }).then((r) => r.json());

    if (res.success) {
      alert(`Success: ${res.message}`);
      await fetchAll();
      renderQaStation();
    } else {
      alert(`Error: ${res.error || 'Failed to validate card'}`);
    }
  } catch (err) {
    console.error('Failed to validate card:', err);
    alert('Failed to validate card.');
  }
}

// -----------------------------------------------------------------------------
// Sprint Roadmap & Decisions
// -----------------------------------------------------------------------------
// -----------------------------------------------------------------------------
// Sprint Roadmap & Decisions (Sorting & Filtering)
// -----------------------------------------------------------------------------
function parseSprintTokens(sprintStr) {
  if (!sprintStr) return { type: 0, tokens: [] };
  const s = sprintStr.trim();
  if (/backlog/i.test(s) || /unscheduled/i.test(s)) return { type: 1, tokens: [] };
  const mPhase = s.match(/^Phase\s+(\d+)/i);
  if (mPhase) return { type: 2, tokens: [parseInt(mPhase[1], 10)] };
  const mSprint = s.replace(/^(?:Sprint|Story)\s+/i, '');
  const rawTokens = mSprint.match(/(\d+|[a-zA-Z]+)/g) || [];
  const tokens = rawTokens.map((t) => (/^\d+$/.test(t) ? parseInt(t, 10) : t.toLowerCase()));
  return { type: 3, tokens };
}

function compareSprintsDesc(a, b) {
  const ra = parseSprintTokens(a.sprint);
  const rb = parseSprintTokens(b.sprint);
  if (ra.type !== rb.type) return rb.type - ra.type; // Sprints (3) > Phases (2) > Backlog (1)
  const len = Math.max(ra.tokens.length, rb.tokens.length);
  for (let i = 0; i < len; i++) {
    const ta = ra.tokens[i];
    const tb = rb.tokens[i];
    if (ta === undefined) return 1; // shorter is smaller, so in desc, b comes first
    if (tb === undefined) return -1; // a has more sub-version, so a is larger
    if (typeof ta === 'number' && typeof tb === 'number') {
      if (ta !== tb) return tb - ta;
    } else if (typeof ta === 'string' && typeof tb === 'string') {
      if (ta !== tb) return tb.localeCompare(ta);
    } else {
      return typeof ta === 'number' ? -1 : 1;
    }
  }
  return 0;
}

function setRoadmapFilter(filterKey) {
  state.roadmapFilter = filterKey;
  document.querySelectorAll('.roadmap-filter-btn').forEach((btn) => {
    if (btn.getAttribute('data-filter') === filterKey) {
      btn.classList.add('active');
    } else {
      btn.classList.remove('active');
    }
  });
  renderRoadmap();
}

function onRoadmapSearch(query) {
  state.roadmapSearch = (query || '').trim().toLowerCase();
  renderRoadmap();
}

function filterBoardBySprint(sprintNum) {
  state.filters.sprint = sprintNum;
  const filterSelect = document.getElementById('pipelineSprintFilter') || document.getElementById('filterSprint');
  if (filterSelect) filterSelect.value = sprintNum;
  switchTab('pipeline');
  renderPipeline();
}

function renderRoadmap() {
  const table = document.getElementById('sprintRoadmapTable');
  if (!table) return;
  table.innerHTML = '';

  // 1. Calculate filter counts
  let countAll = state.sprints.length;
  let countActive = 0;
  let countPlanned = 0;
  let countNextToDefine = 0;
  let countComplete = 0;
  let countBacklog = 0;

  state.sprints.forEach((s) => {
    const sName = (s.sprint || '').toLowerCase();
    const isBacklog = sName.includes('backlog') || sName.includes('unscheduled');
    const sprintIdNum = s.sprint.replace(/^(?:Sprint|Story|Phase)\s+/i, '').trim();
    const assignedCards = state.cards.filter((c) => c.sprint === sprintIdNum || c.sprint === s.sprint);
    const hasActiveCards = assignedCards.some((c) => c.status === 'in-progress' || c.status === 'qa');
    const hasSprintBacklogCards = assignedCards.some((c) => c.status === 'sprint-backlog');

    if (s.is_complete) {
      countComplete++;
    }
    if (isBacklog) {
      countBacklog++;
    }
    if (s.is_built || hasActiveCards) {
      countActive++;
    }
    if (!s.is_complete && !s.is_built && !hasActiveCards && !isBacklog && hasSprintBacklogCards) {
      countPlanned++;
    }
    if (!s.is_complete && !s.is_built && !hasActiveCards && !isBacklog && !hasSprintBacklogCards) {
      countNextToDefine++;
    }
  });

  const elAll = document.getElementById('rfCountAll');
  if (elAll) elAll.textContent = countAll;
  const elActive = document.getElementById('rfCountActive');
  if (elActive) elActive.textContent = countActive;
  const elPlanned = document.getElementById('rfCountPlanned');
  if (elPlanned) elPlanned.textContent = countPlanned;
  const elNext = document.getElementById('rfCountNextToDefine');
  if (elNext) elNext.textContent = countNextToDefine;
  const elComp = document.getElementById('rfCountComplete');
  if (elComp) elComp.textContent = countComplete;
  const elBacklog = document.getElementById('rfCountBacklog');
  if (elBacklog) elBacklog.textContent = countBacklog;

  // 2. Filter sprints
  const filtered = state.sprints.filter((sprint) => {
    const sName = (sprint.sprint || '').toLowerCase();
    const isBacklog = sName.includes('backlog') || sName.includes('unscheduled');
    const sprintIdNum = sprint.sprint.replace(/^(?:Sprint|Story|Phase)\s+/i, '').trim();
    const assignedCards = state.cards.filter((c) => c.sprint === sprintIdNum || c.sprint === sprint.sprint);
    const hasActiveCards = assignedCards.some((c) => c.status === 'in-progress' || c.status === 'qa');

    // Status filter
    if (state.roadmapFilter === 'ACTIVE') {
      if (!sprint.is_built && !hasActiveCards) return false;
    } else if (state.roadmapFilter === 'PLANNED') {
      const hasSprintBacklogCards = assignedCards.some((c) => c.status === 'sprint-backlog');
      if (sprint.is_complete || sprint.is_built || hasActiveCards || isBacklog || !hasSprintBacklogCards) return false;
    } else if (state.roadmapFilter === 'NEXT_TO_DEFINE') {
      const hasSprintBacklogCards = assignedCards.some((c) => c.status === 'sprint-backlog');
      if (sprint.is_complete || isBacklog || hasActiveCards || sprint.is_built || hasSprintBacklogCards) return false;
    } else if (state.roadmapFilter === 'COMPLETE') {
      if (!sprint.is_complete) return false;
    } else if (state.roadmapFilter === 'BACKLOG') {
      if (!isBacklog) return false;
    }

    // Search filter
    if (state.roadmapSearch) {
      const q = state.roadmapSearch;
      const matchSprint = (sprint.sprint || '').toLowerCase().includes(q);
      const matchScope = (sprint.scope || '').toLowerCase().includes(q);
      if (!matchSprint && !matchScope) return false;
    }

    return true;
  });

  // 3. Sort in Sprint number descending order
  const sorted = [...filtered].sort(compareSprintsDesc);

  if (sorted.length === 0) {
    const tr = document.createElement('tr');
    tr.innerHTML = `
      <td colspan="5" class="py-8 text-center text-slate-500 text-xs">
        <i data-lucide="filter" class="w-6 h-6 mx-auto mb-2 text-slate-600"></i>
        No stories match the current filter or search criteria.
      </td>
    `;
    table.appendChild(tr);
    if (window.lucide) lucide.createIcons();
    return;
  }

  // 4. Render sorted rows
  sorted.forEach((sprint) => {
    const tr = document.createElement('tr');
    tr.className = 'hover:bg-slate-800/40 transition-colors';

    const sName = (sprint.sprint || '').toLowerCase();
    const isBacklog = sName.includes('backlog') || sName.includes('unscheduled');
    const sprintIdNum = sprint.sprint.replace(/^(?:Sprint|Story|Phase)\s+/i, '').trim();
    const assignedCards = state.cards.filter((c) => c.sprint === sprintIdNum || c.sprint === sprint.sprint);
    const hasActiveCards = assignedCards.some((c) => c.status === 'in-progress' || c.status === 'qa');
    const hasSprintBacklogCards = assignedCards.some((c) => c.status === 'sprint-backlog');

    let badge = '';
    if (sprint.is_complete) {
      badge = '<span class="text-[10px] font-semibold px-2 py-0.5 rounded bg-emerald-500/10 text-emerald-400 border border-emerald-500/20">Complete ✅</span>';
    } else if (sprint.is_built || hasActiveCards) {
      badge = '<span class="text-[10px] font-semibold px-2 py-0.5 rounded bg-amber-500/10 text-amber-400 border border-amber-500/20">Built / In QA</span>';
    } else if (isBacklog) {
      badge = '<span class="text-[10px] font-semibold px-2 py-0.5 rounded bg-slate-800/80 text-slate-400 border border-slate-700/50">Backlog</span>';
    } else if (!hasSprintBacklogCards) {
      badge = '<span class="badge-next-to-define"><i data-lucide="sparkles" class="w-3 h-3 mr-1 inline"></i>Next to Define</span>';
    } else if (sprint.is_partial) {
      badge = '<span class="text-[10px] font-semibold px-2 py-0.5 rounded bg-purple-500/10 text-purple-400 border border-purple-500/20">Partial ⚠️</span>';
    } else {
      badge = '<span class="text-[10px] font-semibold px-2 py-0.5 rounded bg-blue-500/10 text-blue-300 border border-blue-500/20">Planned</span>';
    }

    // Card column representation
    let cardsHtml = '';
    if (assignedCards.length > 0) {
      cardsHtml = `
        <button data-action="filter-board-by-sprint" data-sprint="${escapeHtml(sprintIdNum)}" title="Click to view cards in Board" class="inline-flex items-center gap-1 font-mono text-emerald-400 hover:text-emerald-300 hover:underline font-semibold text-xs">
          <span>${assignedCards.length} cards</span>
          <i data-lucide="external-link" class="w-3 h-3 opacity-60"></i>
        </button>
      `;
    } else if (!sprint.is_complete && !isBacklog) {
      cardsHtml = `<span class="badge-next-to-define"><i data-lucide="sparkles" class="w-3 h-3 mr-1 inline"></i>Next to Define</span>`;
    } else {
      cardsHtml = `<span class="text-slate-500 font-mono text-[11px]">0 cards</span>`;
    }

    const escSprint = escapeHtml(sprint.sprint);
    const escScope = escapeHtml(sprint.scope);

    tr.innerHTML = `
      <td class="py-3 px-4 font-bold text-slate-200 whitespace-nowrap">${escapeHtml(storyLabel(sprint.sprint))}</td>
      <td class="py-3 px-4 text-slate-300 max-w-md leading-relaxed">${escScope}</td>
      <td class="py-3 px-4 whitespace-nowrap">${cardsHtml}</td>
      <td class="py-3 px-4 whitespace-nowrap">${badge}</td>
      <td class="py-3 px-4 text-right whitespace-nowrap">
        <button data-action="open-sprint-doc" data-sprint="${escSprint}" class="inline-flex items-center gap-1.5 px-2.5 py-1 rounded bg-slate-800 hover:bg-slate-700 text-slate-200 border border-slate-700 hover:border-slate-600 transition text-[11px] font-medium shadow-sm group">
          <i data-lucide="book-open" class="w-3.5 h-3.5 text-emerald-400 group-hover:scale-110 transition-transform"></i>
          <span>View Spec</span>
        </button>
      </td>
    `;

    table.appendChild(tr);
  });

  if (window.lucide) lucide.createIcons();
}

// -----------------------------------------------------------------------------
// Activity & Git
// -----------------------------------------------------------------------------
function renderActivity() {
  const fileList = document.getElementById('activityFileList');
  const commitList = document.getElementById('activityCommitList');
  const fileCount = document.getElementById('activityFileCount');

  const modified = state.activity.modified_files || [];
  fileCount.textContent = `${modified.length} files`;

  if (modified.length === 0) {
    fileList.innerHTML = '<p class="text-slate-500 italic">Working directory is clean.</p>';
  } else {
    fileList.innerHTML = modified
      .map((f) => {
        let status = '';
        let path = '';
        if (typeof f === 'string') {
          if (f.length >= 4 && f.charAt(2) === ' ') {
            status = f.slice(0, 2).trim();
            path = f.slice(3).trim();
          } else {
            const firstSpace = f.indexOf(' ');
            if (firstSpace !== -1) {
              status = f.slice(0, firstSpace).trim();
              path = f.slice(firstSpace + 1).trim();
            } else {
              status = f.trim();
              path = '';
            }
          }
        } else if (f && typeof f === 'object') {
          status = f.status || '';
          path = f.path || '';
        }

        if (path.startsWith('"') && path.endsWith('"') && path.length >= 2) {
          path = path.slice(1, -1);
        }

        let statusColor = 'text-amber-400';
        if (status === '??') statusColor = 'text-cyan-400';
        else if (status.includes('D')) statusColor = 'text-rose-400';
        else if (status.includes('A')) statusColor = 'text-emerald-400';
        else if (status.includes('R')) statusColor = 'text-purple-400';

        return `
          <div class="flex items-center space-x-2 py-1 border-b border-slate-800/40 font-mono text-xs">
            <span class="${statusColor} font-bold w-6 shrink-0">${escapeHtml(status)}</span>
            <span class="text-slate-300 truncate" title="${escapeHtml(path)}">${escapeHtml(path)}</span>
          </div>
        `;
      })
      .join('');
  }

  const commits = state.activity.commits || [];
  commitList.innerHTML = commits
    .map(
      (c) => `
    <div class="p-3 bg-slate-950 rounded-lg border border-slate-800/70 text-xs">
      <div class="flex items-center justify-between mb-1 font-mono text-[10px] text-slate-400">
        <span class="text-emerald-400 font-bold">${c.sha}</span>
        <span class="flex items-center gap-1 text-slate-400"><i data-lucide="clock" class="w-3 h-3 text-slate-500"></i>${c.date}</span>
      </div>
      <p class="font-medium text-slate-200">${c.message}</p>
      <div class="mt-1 text-[10px] text-slate-500">${c.author}</div>
    </div>
  `
    )
    .join('');
  if (window.lucide) lucide.createIcons();
}

// -----------------------------------------------------------------------------
// Navigation & Tab Switching
// -----------------------------------------------------------------------------
function switchTab(tabId) {
  state.activeTab = tabId;

  document.querySelectorAll('.nav-tab').forEach((tab) => {
    if (tab.getAttribute('data-tab') === tabId) {
      tab.classList.add('active-tab');
    } else {
      tab.classList.remove('active-tab');
    }
  });

  document.querySelectorAll('.tab-content').forEach((content) => {
    content.classList.add('hidden');
  });

  const activeContent = document.getElementById(`tab-${tabId}`);
  if (activeContent) activeContent.classList.remove('hidden');

  const viewTitles = {
    'pipeline': 'Pipeline Board',
    'sprint-dod': 'Active Work & Live DoD',
    'qa': 'QA & Heavy Gates',
    'sprints': 'Story Roadmap & Decisions',
    'activity': 'Agent Activity & Git',
  };
  const titleEl = document.getElementById('viewTitle');
  if (titleEl && viewTitles[tabId]) {
    titleEl.textContent = viewTitles[tabId];
  }

  if (typeof closeMobileSidebar === 'function') {
    closeMobileSidebar();
  }

  if (window.lucide) lucide.createIcons();
}

// -----------------------------------------------------------------------------
// Active Sprint & Live DoD Tracker
// -----------------------------------------------------------------------------
async function fetchActiveSprint(targetSprint = null) {
  if (targetSprint !== null) {
    state.activeSprintId = targetSprint;
  }
  try {
    const param =
      state.activeSprintId && state.activeSprintId !== 'ACTIVE'
        ? `?sprint=${encodeURIComponent(state.activeSprintId)}`
        : '';
    const data = await fetch(`/api/active-sprint${param}`).then((r) => r.json());
    state.sprintDodData = data;
    renderActiveSprintDod();
    updateHeader();
  } catch (err) {
    console.error('Failed to fetch active sprint DoD:', err);
  }
}

// -----------------------------------------------------------------------------
// Active Streams Column Layout Preference (Sprint 17.11.1)
// -----------------------------------------------------------------------------
function getActiveStreamsColsPref() {
  const saved = localStorage.getItem('pm_active_streams_cols');
  return saved === '1' ? 1 : 2;
}

function setActiveStreamsCols(cols) {
  localStorage.setItem('pm_active_streams_cols', String(cols));
  applyActiveStreamsCols(cols);
}

function applyActiveStreamsCols(preferredCols, activeCount) {
  const gridEl = document.getElementById('activeStreamsGrid');
  const btn1 = document.getElementById('btnStreamCols1');
  const btn2 = document.getElementById('btnStreamCols2');

  const pref = preferredCols !== undefined ? preferredCols : getActiveStreamsColsPref();
  const count = activeCount !== undefined ? activeCount : (state.lastActiveStreamsCount || 0);

  // Auto-single fallback: when activeCount === 1, force full-width 1 column
  const effectiveCols = (count === 1) ? 1 : pref;

  if (btn1 && btn2) {
    if (pref === 1) {
      btn1.className = 'px-2.5 py-1 text-xs font-semibold rounded-md bg-purple-600 text-white shadow-sm transition cursor-pointer';
      btn2.className = 'px-2.5 py-1 text-xs font-medium rounded-md text-slate-400 hover:text-slate-200 transition cursor-pointer';
    } else {
      btn1.className = 'px-2.5 py-1 text-xs font-medium rounded-md text-slate-400 hover:text-slate-200 transition cursor-pointer';
      btn2.className = 'px-2.5 py-1 text-xs font-semibold rounded-md bg-purple-600 text-white shadow-sm transition cursor-pointer';
    }
  }

  if (gridEl) {
    if (effectiveCols === 1) {
      gridEl.classList.remove('xl:grid-cols-2');
      gridEl.classList.add('grid-cols-1');
    } else {
      gridEl.classList.remove('grid-cols-1');
      gridEl.classList.add('grid-cols-1', 'xl:grid-cols-2');
    }
  }
}

function renderActiveSprintDod() {
  const data = state.sprintDodData;
  if (!data) return;

  const titleEl = document.getElementById('sprintDodTitle');
  const scopeEl = document.getElementById('sprintDodScope');
  const statusBadge = document.getElementById('sprintDodStatusBadge');
  const navBadge = document.getElementById('sprintDodNavBadge');

  const activeStreamsViewEl = document.getElementById('activeStreamsView');
  const historicalSprintViewEl = document.getElementById('historicalSprintView');

  // Populate sprint select dropdown
  const sprintSelect = document.getElementById('activeSprintSelect');
  if (sprintSelect && data.available_sprints) {
    const currentVal = sprintSelect.value;
    sprintSelect.innerHTML = '<option value="ACTIVE">⚡ All Active Streams</option>';
    data.available_sprints.forEach((s) => {
      const opt = document.createElement('option');
      opt.value = s;
      opt.textContent = `Story ${s}`;
      if (s === data.sprint_id && currentVal !== 'ACTIVE') {
        opt.selected = true;
      }
      sprintSelect.appendChild(opt);
    });
    if (state.activeSprintId && state.activeSprintId !== 'ACTIVE') {
      sprintSelect.value = state.activeSprintId;
    }
  }

  const isMultiStream = data.mode === 'multi-stream';

  if (isMultiStream) {
    // -------------------------------------------------------------------------
    // Mode A: Multi-Stream Live Work Grid
    // -------------------------------------------------------------------------
    state.lastActiveStreamsCount = data.active_count || 0;
    applyActiveStreamsCols(undefined, data.active_count);

    if (navBadge) {
      navBadge.textContent = `${data.active_count} active`;
      navBadge.className = 'ml-2 px-1.5 py-0.2 text-[10px] font-bold rounded-full bg-purple-500/20 text-purple-300 border border-purple-500/30';
    }

    if (titleEl) titleEl.textContent = 'Active Work Streams';
    if (scopeEl) scopeEl.textContent = 'Concurrent active stories, builder lanes, and design grills in flight.';

    if (statusBadge) {
      statusBadge.textContent = `${data.active_count} Active`;
      statusBadge.className = 'text-[10px] px-2 py-0.5 rounded-full font-semibold border bg-purple-500/10 text-purple-400 border-purple-500/20';
    }

    // Metric counts
    const streamsCountEl = document.getElementById('activeStreamsMetricCount');
    if (streamsCountEl) streamsCountEl.textContent = data.active_count;

    // Aggregate DoD progress across all active streams
    const totalDodChecked = (data.streams || []).reduce((acc, s) => acc + (s.dod_checked || 0), 0);
    const totalDodTotal = (data.streams || []).reduce((acc, s) => acc + (s.dod_total || 0), 0);
    const totalDodPct = totalDodTotal > 0 ? Math.round((totalDodChecked / totalDodTotal) * 100) : 0;

    const cardRatioEl = document.getElementById('cardDodRatio');
    const cardBarEl = document.getElementById('cardDodProgressBar');
    if (cardRatioEl) cardRatioEl.textContent = `${totalDodChecked}/${totalDodTotal} (${totalDodPct}%)`;
    if (cardBarEl) cardBarEl.style.width = `${totalDodPct}%`;

    const inProgCountEl = document.getElementById('sprintInProgressCount');
    const qaCountEl = document.getElementById('sprintQaCount');
    const inProgCards = (state.cards || []).filter((c) => c.status === 'in-progress');
    const qaCards = (state.cards || []).filter((c) => c.status === 'qa');
    if (inProgCountEl) inProgCountEl.textContent = inProgCards.length;
    if (qaCountEl) qaCountEl.textContent = qaCards.length;

    // Show View A, Hide View B
    if (activeStreamsViewEl) activeStreamsViewEl.classList.remove('hidden');
    if (historicalSprintViewEl) historicalSprintViewEl.classList.add('hidden');

    // Render Stream Cards Grid
    const gridEl = document.getElementById('activeStreamsGrid');
    const emptyStateEl = document.getElementById('activeStreamsEmptyState');

    if (!data.streams || data.streams.length === 0) {
      if (emptyStateEl) emptyStateEl.classList.remove('hidden');
      if (gridEl) gridEl.innerHTML = '';
    } else {
      if (emptyStateEl) emptyStateEl.classList.add('hidden');
      if (gridEl) {
        gridEl.innerHTML = data.streams
          .map((s) => {
            const escId = escapeHtml(s.mn_id || s.id);
            const escTitle = escapeHtml(s.title);
            const escSprint = escapeHtml(s.sprint || '');
            const escScope = escapeHtml(s.scope || s.description || '');
            const escDate = escapeHtml(s.created_date || 'N/A');
            const isGrill = Boolean(s.is_grill);

            let typeClass = 'pill-feature';
            if (s.type === 'bug') typeClass = 'pill-bug';
            if (s.type === 'improvement') typeClass = 'pill-improvement';
            if (isGrill) typeClass = 'pill-grill';

            const statusBadgeHtml = isGrill
              ? '<span class="text-xs px-2.5 py-0.5 rounded-full font-semibold bg-purple-500/15 text-purple-300 border border-purple-500/30 flex items-center gap-1"><i data-lucide="flame" class="w-3.5 h-3.5 text-purple-400"></i>Design Grill</span>'
              : '<span class="text-xs px-2.5 py-0.5 rounded-full font-semibold bg-emerald-500/15 text-emerald-400 border border-emerald-500/30 flex items-center gap-1"><span class="w-2 h-2 rounded-full bg-emerald-400 animate-pulse"></span>In Progress</span>';

            const sprintBadgeHtml = `<span class="text-xs px-2 py-0.5 rounded bg-slate-800 text-slate-300 border border-slate-700 font-mono font-medium">Story ${escSprint}</span>`;

            const agentAct = s.latest_activity || {};
            const agentName = escapeHtml(agentAct.agent || (isGrill ? 'Lead (Grill)' : 'Lead'));
            const agentAction = escapeHtml(agentAct.what || 'In progress');
            const agentDt = escapeHtml(agentAct.datetime || (agentAct.time ? `${agentAct.date} ${agentAct.time}` : agentAct.date) || '');

            // Duration badge in header (Sprint 17.11.1)
            const durationFormatted = s.duration_formatted || '';
            let durationBadgeHtml = '';
            if (durationFormatted) {
              const durText = (s.status === 'in-progress' || isGrill)
                ? `⏱️ ${durationFormatted} in progress`
                : `⏱️ Completed in ${durationFormatted}`;
              const timeRange = s.start_time ? `Started: ${s.start_time}${s.end_time ? ' • Ended: ' + s.end_time : ''}` : '';
              durationBadgeHtml = `<span class="text-xs px-2.5 py-0.5 rounded-full font-medium bg-slate-800 text-purple-300 border border-purple-500/30 flex items-center gap-1 font-mono" title="${escapeHtml(timeRange)}">${escapeHtml(durText)}</span>`;
            }

            // Git Commits Proof
            const gitCommitsHtml = (s.git_commits && s.git_commits.length > 0)
              ? `
                <div class="mb-3">
                  <div class="text-[10px] font-semibold text-slate-400 uppercase tracking-wider mb-1.5 flex items-center justify-between">
                    <span class="flex items-center gap-1"><i data-lucide="git-branch" class="w-3 h-3 text-purple-400"></i> Git Commits Proof</span>
                    <span class="text-[9px] text-slate-500 font-mono">${s.git_commits.length} recent</span>
                  </div>
                  <div class="space-y-1">
                    ${s.git_commits.map((c) => {
                      const commitDt = escapeHtml(c.datetime || (c.date && c.time ? `${c.date} ${c.time}` : c.date) || '');
                      return `
                      <div class="flex items-center gap-2 py-1 px-2 rounded bg-slate-900/80 border border-slate-800 text-[11px]">
                        <span class="font-mono text-[10px] text-purple-400 font-bold bg-purple-500/10 px-1 py-0.2 rounded border border-purple-500/20 shrink-0">${escapeHtml(c.sha)}</span>
                        <span class="text-slate-300 truncate flex-1 font-medium text-[11px]" title="${escapeHtml(c.message)}">${escapeHtml(c.message)}</span>
                        <span class="text-slate-500 font-mono text-[9px] shrink-0" title="${commitDt}">${commitDt}</span>
                      </div>
                    `;}).join('')}
                  </div>
                </div>
              `
              : '';

            // Review Gate Evidence
            const reviewsHtml = (s.reviews && s.reviews.length > 0)
              ? `
                <div class="mb-3">
                  <div class="text-[10px] font-semibold text-slate-400 uppercase tracking-wider mb-1.5 flex items-center gap-1">
                    <i data-lucide="shield-check" class="w-3 h-3 text-emerald-400"></i> Review Gate Evidence
                  </div>
                  <div class="space-y-1">
                    ${s.reviews.map((r) => `
                      <div class="flex items-center justify-between py-1 px-2 rounded-lg bg-emerald-500/10 border border-emerald-500/20 text-xs">
                        <div class="flex items-center gap-1.5">
                          <span class="font-bold text-emerald-400 text-[11px]">Round ${escapeHtml(String(r.round))}</span>
                          <span class="text-slate-200 font-medium text-[11px]">${escapeHtml(r.verdict || 'PASS')}</span>
                          <span class="text-slate-500 text-[10px]">(${escapeHtml(r.reviewer || 'Reviewer')})</span>
                        </div>
                        <button class="text-purple-400 hover:text-purple-300 text-[10px] font-mono cursor-pointer" data-action="open-review-modal" data-sprint="${escapeHtml(r.sprint)}">View Report →</button>
                      </div>
                    `).join('')}
                  </div>
                </div>
              `
              : '';

            // Changelog & Evidence feed (reverse chronological)
            const changelogEntries = (s.changelog || []).slice().reverse();
            const changelogEntriesHtml = changelogEntries.slice(0, 4).map((entry) => {
              const eDt = escapeHtml(entry.datetime || (entry.time ? `${entry.date} ${entry.time}` : entry.date) || '');
              const eAgent = escapeHtml(entry.agent || 'Lead');
              const eWhat = escapeHtml(entry.what || '');
              return `
                <div class="stream-changelog-item">
                  <div class="flex items-center justify-between text-[10px] text-slate-400 mb-1">
                    <span class="font-semibold text-purple-300 flex items-center gap-1">
                      <i data-lucide="git-commit" class="w-3 h-3 text-purple-400"></i> ${eAgent}
                    </span>
                    <span class="font-mono text-[9px] text-slate-500" title="${eDt}">${eDt}</span>
                  </div>
                  <p class="text-[11px] text-slate-300 leading-snug line-clamp-3">${eWhat}</p>
                </div>
              `;
            }).join('');

            // DoD Items
            const dodItemsHtml = (s.dod || [])
              .map((item, idx) => {
                const escText = formatRichText(item.text);
                const itemIdx = item.index !== undefined ? item.index : idx;
                return `
                  <label class="flex items-start space-x-2.5 py-1.5 px-2 rounded-lg hover:bg-slate-900/90 cursor-pointer text-xs transition group">
                    <input type="checkbox" ${item.checked ? 'checked' : ''}
                           data-action="toggle-dod"
                           data-card-id="${escId}"
                           data-idx="${itemIdx}"
                           class="mt-0.5 rounded border-slate-700 text-purple-500 focus:ring-purple-500/20 bg-slate-800 cursor-pointer w-4 h-4">
                    <span class="flex-1 ${item.checked ? 'text-[var(--text-tertiary)] line-through' : 'text-[var(--text-primary)]'} group-hover:text-[var(--text-primary)] transition-colors leading-relaxed">${escText}</span>
                  </label>
                `;
              })
              .join('');

            return `
              <div class="stream-card ${isGrill ? 'is-grill' : ''}">
                <!-- Card Header -->
                <div class="flex items-start justify-between gap-3 mb-3">
                  <div class="flex-1 min-w-0">
                    <div class="flex flex-wrap items-center gap-2 mb-1.5">
                      <span class="font-mono font-bold text-sm text-purple-600 dark:text-purple-400 cursor-pointer hover:underline flex items-center gap-1" data-action="${isGrill ? '' : 'open-card-modal'}" data-card-id="${escId}">
                        <i data-lucide="${isGrill ? 'flame' : 'file-code'}" class="w-4 h-4"></i> ${escId}
                      </span>
                      ${sprintBadgeHtml}
                      <span class="text-[10px] font-semibold px-2 py-0.5 rounded ${typeClass}">${s.type || 'feature'}</span>
                      ${durationBadgeHtml}
                    </div>
                    <h3 class="text-base font-bold text-[var(--text-primary)] leading-snug cursor-pointer hover:text-purple-600 dark:hover:text-purple-300 transition-colors" data-action="${isGrill ? '' : 'open-card-modal'}" data-card-id="${escId}">${escTitle}</h3>
                  </div>
                  <div class="shrink-0 flex items-center gap-2">${statusBadgeHtml}</div>
                </div>

                <!-- Scope Summary -->
                <div class="bg-[var(--bg-subtle)] p-3 rounded-xl border border-[var(--border-subtle)] mb-4">
                  <div class="text-[10px] font-semibold text-[var(--text-tertiary)] uppercase tracking-wider mb-1 flex items-center gap-1.5">
                    <i data-lucide="target" class="w-3.5 h-3.5 text-purple-500"></i> Scope & Objective
                  </div>
                  <p class="text-xs text-[var(--text-secondary)] leading-relaxed">${escScope || 'No explicit description provided.'}</p>
                </div>

                <!-- Card Body Grid (2-column layout inside each stream card) -->
                <div class="grid grid-cols-1 lg:grid-cols-12 gap-4 flex-1">
                  
                  <!-- Left Column: Agent Activity & Implementation Changelog (5 cols) -->
                  <div class="lg:col-span-5 flex flex-col bg-[var(--bg-subtle)] p-3.5 rounded-xl border border-[var(--border-subtle)]">
                    <div class="flex items-center justify-between mb-2 pb-1.5 border-b border-[var(--border-subtle)]">
                      <span class="text-xs font-semibold text-[var(--text-primary)] flex items-center gap-1.5">
                        <i data-lucide="cpu" class="w-3.5 h-3.5 text-purple-500"></i> Agent Activity & Proof
                      </span>
                      <span class="text-[10px] text-[var(--text-tertiary)] font-mono">${(s.changelog || []).length} logged</span>
                    </div>

                    <!-- Current Focus Box -->
                    <div class="mb-3 p-2.5 rounded-lg bg-purple-500/10 border border-purple-500/20 text-xs">
                      <div class="text-[10px] text-purple-600 dark:text-purple-300 font-semibold mb-1 flex items-center justify-between">
                        <span class="flex items-center gap-1"><i data-lucide="play-circle" class="w-3 h-3 text-purple-500"></i> Current Focus: ${agentName}</span>
                        ${agentDt ? `<span class="font-mono text-[9px] text-[var(--text-tertiary)]" title="${agentDt}">${agentDt}</span>` : ''}
                      </div>
                      <p class="text-[var(--text-primary)] font-medium text-[11px] leading-snug line-clamp-3">
                        ${agentAction}
                      </p>
                    </div>

                    <!-- Git Commits Proof -->
                    ${gitCommitsHtml}

                    <!-- Review Gate Evidence -->
                    ${reviewsHtml}

                    <!-- Changelog Feed -->
                    <div class="text-[10px] font-semibold text-[var(--text-tertiary)] uppercase tracking-wider mb-1.5 flex items-center justify-between">
                      <span>Implementation Milestones</span>
                      <span class="text-[9px] text-[var(--text-tertiary)] lowercase">newest first</span>
                    </div>
                    <div class="flex-1 overflow-y-auto max-h-[220px] space-y-2 pr-1 stream-dod-container">
                      ${changelogEntriesHtml || '<div class="text-[var(--text-tertiary)] italic text-xs py-3 text-center">No changelog entries recorded yet.</div>'}
                    </div>
                  </div>

                  <!-- Right Column: Live Definition of Done Checklist (7 cols, taller) -->
                  <div class="lg:col-span-7 flex flex-col bg-[var(--bg-subtle)] p-3.5 rounded-xl border border-[var(--border-subtle)]">
                    <div class="flex items-center justify-between mb-2">
                      <span class="text-xs font-semibold text-[var(--text-primary)] flex items-center gap-1.5">
                        <i data-lucide="check-square" class="w-3.5 h-3.5 text-emerald-500"></i> Definition of Done
                      </span>
                      <span class="font-mono text-purple-600 dark:text-purple-400 font-bold text-xs">${s.dod_checked}/${s.dod_total} (${s.progress_pct}%)</span>
                    </div>

                    <!-- Progress bar -->
                    <div class="w-full bg-[var(--bg-surface-tertiary)] rounded-full h-2 overflow-hidden mb-3">
                      <div class="bg-gradient-to-r from-purple-500 to-emerald-400 h-2 rounded-full transition-all duration-300" style="width: ${s.progress_pct}%"></div>
                    </div>

                    <!-- DoD Items (Taller height so many items fit without scrolling!) -->
                    <div class="flex-1 overflow-y-auto max-h-[380px] space-y-1 pr-1 stream-dod-container border-t border-[var(--border-subtle)] pt-2">
                      ${dodItemsHtml || '<div class="text-xs text-[var(--text-tertiary)] italic py-6 text-center">No criteria checklist defined.</div>'}
                    </div>
                  </div>

                </div>

                <!-- Card Footer -->
                <div class="mt-4 pt-2.5 border-t border-[var(--border-subtle)] flex items-center justify-between text-xs text-[var(--text-tertiary)] font-mono">
                  <span class="flex items-center gap-1.5"><i data-lucide="calendar" class="w-3.5 h-3.5 text-[var(--text-tertiary)]"></i> Created ${escDate}</span>
                  <button class="text-purple-600 dark:text-purple-400 hover:text-purple-500 flex items-center gap-1 text-xs font-medium transition cursor-pointer" data-action="${isGrill ? '' : 'open-card-modal'}" data-card-id="${escId}">
                    <span>Open Story Details</span>
                    <i data-lucide="arrow-up-right" class="w-3.5 h-3.5"></i>
                  </button>
                </div>
              </div>
            `;
          })
          .join('');
      }
    }
  } else {
    // -------------------------------------------------------------------------
    // Mode B: Historical Single Sprint Audit View
    // -------------------------------------------------------------------------
    if (titleEl) titleEl.textContent = storyLabel(data.sprint_name) || `Story ${data.sprint_id}`;
    if (scopeEl) scopeEl.textContent = data.scope || 'No explicit scope defined in PLAN.md.';

    if (statusBadge) {
      statusBadge.textContent = data.status_raw || (data.is_complete ? 'Complete ✅' : 'Active');
      statusBadge.className = `text-[10px] px-2 py-0.5 rounded-full font-semibold border ${
        data.is_complete
          ? 'bg-emerald-500/10 text-emerald-400 border-emerald-500/20'
          : 'bg-amber-500/10 text-amber-400 border-amber-500/20'
      }`;
    }

    if (navBadge) {
      navBadge.textContent = `${data.sprint_dod_pct}%`;
      navBadge.className = 'ml-2 px-1.5 py-0.2 text-[10px] font-bold rounded-full bg-emerald-500/20 text-emerald-300 border border-emerald-500/30';
    }

    // Story / Cards DoD progress bar
    const cardRatioEl = document.getElementById('cardDodRatio') || document.getElementById('sprintDodRatio');
    const cardBarEl = document.getElementById('cardDodProgressBar') || document.getElementById('sprintDodProgressBar');
    if (cardRatioEl) cardRatioEl.textContent = `${data.card_dod_checked || data.sprint_dod_checked}/${data.card_dod_total || data.sprint_dod_total} (${data.card_dod_pct || data.sprint_dod_pct}%)`;
    if (cardBarEl) cardBarEl.style.width = `${data.card_dod_pct || data.sprint_dod_pct}%`;

    // Status counts
    const inProgCards = (data.cards_by_status && data.cards_by_status['in-progress']) || [];
    const qaCards = (data.cards_by_status && data.cards_by_status['qa']) || [];
    const inProgCountEl = document.getElementById('sprintInProgressCount');
    const qaCountEl = document.getElementById('sprintQaCount');
    if (inProgCountEl) inProgCountEl.textContent = inProgCards.length;
    if (qaCountEl) qaCountEl.textContent = qaCards.length;

    // Show View B, Hide View A
    if (activeStreamsViewEl) activeStreamsViewEl.classList.add('hidden');
    if (historicalSprintViewEl) historicalSprintViewEl.classList.remove('hidden');

    // Render Story DoD items (Left Column)
    const sprintDodListEl = document.getElementById('sprintDodList');
    if (sprintDodListEl) {
      if (!data.sprint_dod || data.sprint_dod.length === 0) {
        sprintDodListEl.innerHTML = `
          <div class="p-6 text-center text-slate-500 text-xs">
            <i data-lucide="info" class="w-6 h-6 mx-auto mb-2 text-slate-600"></i>
            No explicit Definition of Done checklist found in PLAN.md for Story ${escapeHtml(data.sprint_id)}.
          </div>
        `;
      } else {
        sprintDodListEl.innerHTML = data.sprint_dod
          .map((item, idx) => {
            const escSprintId = escapeHtml(data.sprint_id);
            return `
            <label class="flex items-start space-x-3 p-2.5 rounded-lg bg-slate-950/70 hover:bg-slate-950 border border-slate-800/80 hover:border-slate-700 cursor-pointer text-xs transition group">
              <input type="checkbox" ${item.checked ? 'checked' : ''} data-action="toggle-sprint-dod" data-sprint-id="${escSprintId}" data-idx="${idx}" class="mt-0.5 rounded border-slate-700 text-emerald-500 focus:ring-emerald-500/20 bg-slate-800 cursor-pointer">
              <span class="flex-1 ${item.checked ? 'text-slate-400 line-through' : 'text-slate-200 font-medium'} group-hover:text-emerald-300 transition-colors">${formatRichText(item.text)}</span>
            </label>
          `;
          })
          .join('');
      }
    }

    // Render Cards Breakdown (Right Column)
    const cardsContainerEl = document.getElementById('sprintCardsContainer');
    const cardsTotalBadge = document.getElementById('sprintCardsTotalBadge');
    if (cardsTotalBadge) cardsTotalBadge.textContent = `${data.cards ? data.cards.length : 0} tasks`;

    if (cardsContainerEl) {
      if (!data.cards || data.cards.length === 0) {
        cardsContainerEl.innerHTML = `
          <div class="p-8 text-center text-slate-500 text-xs">
            <i data-lucide="inbox" class="w-8 h-8 mx-auto mb-2 text-slate-600"></i>
            No cards assigned to Story ${escapeHtml(data.sprint_id)} yet.
          </div>
        `;
      } else {
        cardsContainerEl.innerHTML = data.cards
          .map((c) => {
            const escId = escapeHtml(c.mn_id);
            const escTitle = escapeHtml(c.title);
            let statusBadge = '<span class="text-[10px] px-2 py-0.5 rounded bg-slate-800 text-slate-300">Backlog</span>';
            if (c.status === 'in-progress') {
              statusBadge = '<span class="text-[10px] px-2 py-0.5 rounded bg-purple-500/10 text-purple-400 border border-purple-500/20">In Progress</span>';
            } else if (c.status === 'qa') {
              statusBadge = '<span class="text-[10px] px-2 py-0.5 rounded bg-amber-500/10 text-amber-400 border border-amber-500/20">Awaiting QA</span>';
            } else if (c.status === 'completed') {
              statusBadge = '<span class="text-[10px] px-2 py-0.5 rounded bg-emerald-500/10 text-emerald-400 border border-emerald-500/20">Done</span>';
            }

            const dodItemsHtml = (c.dod || [])
              .map((item, subIdx) => {
                return `
                <label class="flex items-start space-x-2 py-1 px-1.5 rounded hover:bg-slate-900 cursor-pointer text-[11px] transition touch-target-item">
                  <input type="checkbox" ${item.checked ? 'checked' : ''} data-action="toggle-dod" data-card-id="${escId}" data-idx="${subIdx}" class="mt-0.5 rounded border-slate-700 text-emerald-500 focus:ring-emerald-500/20 bg-slate-800 cursor-pointer">
                  <span class="${item.checked ? 'text-slate-500 line-through' : 'text-slate-300'}">${formatRichText(item.text)}</span>
                </label>
              `;
              })
              .join('');

            return `
            <div class="bg-slate-950/70 p-3.5 rounded-xl border border-slate-800/80 hover:border-slate-700 transition">
              <div class="flex items-center justify-between gap-2 mb-1.5">
                <div class="flex items-center gap-2">
                  <span class="font-mono font-bold text-xs text-emerald-400 cursor-pointer hover:underline" data-action="open-card-modal" data-card-id="${escId}">${escId}</span>
                  <h4 class="text-xs font-semibold text-white cursor-pointer hover:text-emerald-400 transition-colors" data-action="open-card-modal" data-card-id="${escId}">${escTitle}</h4>
                </div>
                <div>${statusBadge}</div>
              </div>
              ${c.dod && c.dod.length > 0 ? `
                <div class="mt-2.5 pt-2 border-t border-slate-900 space-y-1">
                  <div class="text-[10px] text-slate-500 uppercase tracking-wider font-semibold mb-1 flex items-center justify-between">
                    <span>Card DoD Criteria:</span>
                    <span class="font-mono text-emerald-400">${c.dod_checked}/${c.dod_total}</span>
                  </div>
                  ${dodItemsHtml}
                </div>
              ` : '<div class="text-[11px] text-slate-500 italic mt-1">No subtasks defined.</div>'}
            </div>
          `;
          })
          .join('');
      }
    }
  }

  if (window.lucide) lucide.createIcons();
}

async function toggleSprintDodItem(sprintId, itemIndex, checked) {
  try {
    const res = await fetch(`/api/sprints/${encodeURIComponent(sprintId)}/dod`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ index: itemIndex, checked }),
    }).then((r) => r.json());

    if (res.success) {
      await fetchActiveSprint();
    } else {
      alert(`Error updating Story DoD: ${res.error || 'Server error'}`);
    }
  } catch (err) {
    console.error('Failed to toggle sprint DoD:', err);
    alert('Failed to update Story DoD.');
  }
}

function openCardModalById(mn_id) {
  const card = state.cards.find((c) => c.mn_id === mn_id);
  if (card) openCardModal(card);
}

// -----------------------------------------------------------------------------
// Heavy Gates (finalgates.ps1) Runner & Monitoring
// -----------------------------------------------------------------------------
let isHeavyPolling = false;
let heavyGatePollTimer = null;
let lastKnownHeavyStatus = 'idle';

async function fetchHeavyGateStatus() {
  try {
    const res = await fetch('/api/heavy-gate/status').then((r) => r.json());
    state.heavyGateWorker = res.worker || {};
    state.heavyGateInfo = res.gate_info || {};

    const prevStatus = lastKnownHeavyStatus;
    const currentStatus = state.heavyGateWorker.status || 'idle';
    lastKnownHeavyStatus = currentStatus;

    updateHeavyGateUi();

    // If transitioned from running to finished (completed or failed), refresh the sprint audit matrix and logs
    if (prevStatus === 'running' && currentStatus !== 'running') {
      fetchHeavyGateLog();
      fetchSprintGateStatus();
    }

    // Schedule next poll if still running
    if (currentStatus === 'running') {
      scheduleHeavyGatePoll();
    } else {
      stopHeavyGatePoll();
    }
  } catch (err) {
    console.error('Failed to fetch heavy gate status:', err);
  }
}

function scheduleHeavyGatePoll() {
  if (heavyGatePollTimer) return;
  heavyGatePollTimer = setTimeout(async () => {
    heavyGatePollTimer = null;
    if (isHeavyPolling) return;
    isHeavyPolling = true;
    try {
      await fetchHeavyGateLog();
      await fetchHeavyGateStatus();
    } finally {
      isHeavyPolling = false;
    }
  }, 2000);
}

function stopHeavyGatePoll() {
  if (heavyGatePollTimer) {
    clearTimeout(heavyGatePollTimer);
    heavyGatePollTimer = null;
  }
}

function updateHeavyGateUi() {
  const worker = state.heavyGateWorker || { status: 'idle' };
  const info = state.heavyGateInfo || { queue_count: 0, queue_items: [], latest_run: null };

  const badgeEl = document.getElementById('heavyGateStatusBadge');
  const timerEl = document.getElementById('heavyGateTimer');
  const runBtn = document.getElementById('runHeavyGateBtn');
  const stopBtn = document.getElementById('stopHeavyGateBtn');
  const indicator = document.getElementById('terminalIndicator');

  // Status & timer
  if (worker.status === 'running') {
    if (badgeEl) {
      badgeEl.textContent = 'Running ⚡';
      badgeEl.className = 'text-[10px] px-2 py-0.5 rounded-full font-bold bg-amber-500/20 text-amber-300 border border-amber-500/40 animate-pulse';
    }
    if (timerEl) {
      timerEl.classList.remove('hidden');
      const elapsed = Math.floor(worker.elapsed_seconds || 0);
      const mins = String(Math.floor(elapsed / 60)).padStart(2, '0');
      const secs = String(elapsed % 60).padStart(2, '0');
      timerEl.textContent = `${mins}:${secs}`;
    }
    if (runBtn) {
      runBtn.disabled = true;
      const span = runBtn.querySelector('span');
      if (span) span.textContent = 'Running...';
    }
    if (stopBtn) stopBtn.classList.remove('hidden');
    if (indicator) indicator.className = 'w-2.5 h-2.5 rounded-full bg-amber-400 animate-ping inline-block';
  } else {
    if (timerEl) timerEl.classList.add('hidden');
    if (runBtn) {
      runBtn.disabled = false;
      const span = runBtn.querySelector('span');
      if (span) span.textContent = 'Run Heavy Gate';
    }
    if (stopBtn) stopBtn.classList.add('hidden');

    if (worker.status === 'completed') {
      if (badgeEl) {
        badgeEl.textContent = 'Passed ✅';
        badgeEl.className = 'text-[10px] px-2 py-0.5 rounded-full font-bold bg-emerald-500/20 text-emerald-300 border border-emerald-500/40';
      }
      if (indicator) indicator.className = 'w-2.5 h-2.5 rounded-full bg-emerald-500 inline-block';
    } else if (worker.status === 'failed') {
      if (badgeEl) {
        badgeEl.textContent = 'Failed ❌';
        badgeEl.className = 'text-[10px] px-2 py-0.5 rounded-full font-bold bg-rose-500/20 text-rose-300 border border-rose-500/40';
      }
      if (indicator) indicator.className = 'w-2.5 h-2.5 rounded-full bg-rose-500 inline-block';
    } else {
      if (badgeEl) {
        badgeEl.textContent = 'Idle';
        badgeEl.className = 'text-[10px] px-2 py-0.5 rounded-full font-semibold bg-slate-800 text-slate-400 border border-slate-700';
      }
      if (indicator) indicator.className = 'w-2.5 h-2.5 rounded-full bg-slate-600 inline-block';
    }
  }

  // Bespoke Queue
  const queueCountEl = document.getElementById('heavyQueueCount');
  const queueListEl = document.getElementById('heavyQueueList');
  if (queueCountEl) queueCountEl.textContent = `${info.queue_count} queued`;
  if (queueListEl) {
    if (info.queue_items && info.queue_items.length > 0) {
      queueListEl.innerHTML = info.queue_items
        .map((f) => `<div class="text-amber-300 truncate py-0.5">📄 ${escapeHtml(f.file_name)}</div>`)
        .join('');
    } else {
      queueListEl.innerHTML = '<p class="text-slate-500 italic">No bespoke checks in scripts/heavy-gate-queue/.</p>';
    }
  }

  // Latest Run
  const latestBadge = document.getElementById('latestRunBadge');
  const latestSha = document.getElementById('latestRunSha');
  const latestTime = document.getElementById('latestRunTime');
  const latest = info.latest_run;

  if (latest) {
    if (latestBadge) {
      const ok = latest.overall_ok !== undefined
        ? Boolean(latest.overall_ok)
        : (Boolean(latest.pytest_ok) && (latest.vitest_ok === undefined || latest.vitest_ok === null || Boolean(latest.vitest_ok)));
      latestBadge.textContent = ok ? 'PASSED ✅' : 'FAILED ❌';
      latestBadge.className = `text-[10px] px-1.5 py-0.2 rounded font-semibold ${
        ok ? 'bg-emerald-500/20 text-emerald-400 border border-emerald-500/30' : 'bg-rose-500/20 text-rose-400 border border-rose-500/30'
      }`;
    }
    if (latestSha) latestSha.textContent = `SHA: ${(latest.sha || 'latest').slice(0, 7)}`;
    if (latestTime) {
      let timeStr = '--';
      if (latest.timestamp) {
        const d = new Date(latest.timestamp);
        // Format as YYYY-MM-DD HH:MM:SS in 24-hour format in browser's local timezone
        const datePart = d.toLocaleDateString(undefined, { year: 'numeric', month: '2-digit', day: '2-digit' });
        const timePart = d.toLocaleTimeString(undefined, { hour: '2-digit', minute: '2-digit', second: '2-digit', hour12: false });
        timeStr = `${datePart} ${timePart}`;
      }
      const dur = latest.elapsed_sec ? ` (${Math.round(latest.elapsed_sec)}s)` : '';
      latestTime.textContent = `${timeStr}${dur}`;
      latestTime.title = latest.timestamp ? `Full ISO: ${latest.timestamp}` : '';
    }
  } else {
    if (latestBadge) {
      latestBadge.textContent = 'No runs yet';
      latestBadge.className = 'text-[10px] px-1.5 py-0.2 rounded font-semibold bg-slate-800 text-slate-400';
    }
    if (latestSha) latestSha.textContent = 'SHA: --';
    if (latestTime) latestTime.textContent = '--';
  }
}

async function fetchHeavyGateLog() {
  try {
    const res = await fetch('/api/heavy-gate/log?limit=300').then((r) => r.json());
    const terminal = document.getElementById('heavyGateTerminal');
    if (terminal && res.lines && res.lines.length > 0) {
      terminal.textContent = res.lines.join('\n');
      const autoScroll = document.getElementById('autoScrollTerminal');
      if (autoScroll && autoScroll.checked) {
        terminal.scrollTop = terminal.scrollHeight;
      }
    }
  } catch (err) {
    console.error('Failed to fetch heavy gate log:', err);
  }
}

async function startHeavyGate() {
  if (!confirm('Start full Heavy Gate 5 (powershell scripts/finalgates.ps1) in background? This runs full test suites and coverage.')) {
    return;
  }
  const runBtn = document.getElementById('runHeavyGateBtn');
  const terminal = document.getElementById('heavyGateTerminal');
  try {
    if (runBtn) {
      runBtn.disabled = true;
      const span = runBtn.querySelector('span');
      if (span) span.textContent = 'Starting...';
    }
    if (terminal) {
      terminal.textContent = '[Starting finalgates.ps1 in background worker...]\n[Pytest suite initializing & collecting tests... output will stream below]\n';
    }

    const res = await fetch('/api/heavy-gate/start', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({}),
    }).then((r) => r.json());

    if (res.success) {
      await fetchHeavyGateStatus();
      await fetchHeavyGateLog();
    } else {
      alert(`Error starting heavy gate: ${res.error || 'Server error'}`);
      if (runBtn) {
        runBtn.disabled = false;
        const span = runBtn.querySelector('span');
        if (span) span.textContent = 'Run Heavy Gate';
      }
    }
  } catch (err) {
    console.error('Failed to start heavy gate:', err);
    alert('Failed to start heavy gate execution.');
    if (runBtn) {
      runBtn.disabled = false;
      const span = runBtn.querySelector('span');
      if (span) span.textContent = 'Run Heavy Gate';
    }
  }
}

async function stopHeavyGate() {
  if (!confirm('Are you sure you want to terminate the running heavy gate process?')) {
    return;
  }
  try {
    const res = await fetch('/api/heavy-gate/stop', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
    }).then((r) => r.json());

    if (res.success) {
      await fetchHeavyGateStatus();
      fetchHeavyGateLog();
    } else {
      alert(`Error stopping heavy gate: ${res.error || 'Server error'}`);
    }
  } catch (err) {
    console.error('Failed to stop heavy gate:', err);
  }
}

function clearHeavyLog() {
  const terminal = document.getElementById('heavyGateTerminal');
  if (terminal) terminal.textContent = '';
}

// -----------------------------------------------------------------------------
// Sprint Quality Auditor (Historical Verification per Sprint)
// -----------------------------------------------------------------------------
async function fetchSprintGateStatus() {
  try {
    const res = await fetch('/api/heavy-gate/sprints');
    if (!res.ok) {
      const tbody = document.getElementById('sprintAuditTableBody');
      if (tbody) {
        tbody.innerHTML = `
          <tr>
            <td colspan="6" class="text-center py-6 text-amber-400 text-xs">
              <div class="flex items-center justify-center gap-2">
                <i data-lucide="alert-triangle" class="w-4 h-4 text-amber-400"></i>
                <span>Server restart required: endpoint <code class="bg-slate-900 px-1.5 py-0.5 rounded text-amber-300 font-mono">/api/heavy-gate/sprints</code> not found. Please restart <code class="bg-slate-900 px-1.5 py-0.5 rounded text-amber-300 font-mono">scripts/pm.ps1</code>.</span>
              </div>
            </td>
          </tr>
        `;
        if (window.lucide) lucide.createIcons();
      }
      return;
    }
    const data = await res.json();
    state.sprintGateStatus = data.sprints || [];
    renderSprintAuditTable();
  } catch (err) {
    console.error('Failed to fetch sprint gate audit status:', err);
    const tbody = document.getElementById('sprintAuditTableBody');
    if (tbody) {
      tbody.innerHTML = `
        <tr>
          <td colspan="6" class="text-center py-6 text-rose-400 text-xs">
            <div class="flex items-center justify-center gap-2">
              <i data-lucide="alert-circle" class="w-4 h-4 text-rose-400"></i>
              <span>Failed to fetch story audit status. If server is stale, restart <code class="bg-slate-900 px-1.5 py-0.5 rounded font-mono">scripts/pm.ps1</code>.</span>
            </div>
          </td>
        </tr>
      `;
      if (window.lucide) lucide.createIcons();
    }
  }
}

function setSprintAuditFilter(filterKey) {
  state.sprintAuditFilter = filterKey;
  document.querySelectorAll('.audit-filter-pill').forEach((btn) => {
    if (btn.getAttribute('data-filter') === filterKey) {
      btn.classList.add('active');
    } else {
      btn.classList.remove('active');
    }
  });
  renderSprintAuditTable();
}

function renderSprintAuditTable() {
  const tbody = document.getElementById('sprintAuditTableBody');
  const badgeEl = document.getElementById('sprintAuditPendingBadge');
  const allPendingBtn = document.getElementById('runAllPendingSprintsBtn');
  const toggleEl = document.getElementById('sprintAuditShowAllToggle');
  if (!tbody) return;

  const sprints = state.sprintGateStatus || [];
  const countFailed = sprints.filter((s) => s.status === 'Failed').length;
  const countOwed = sprints.filter((s) => s.status === 'Owed').length;
  const countPassed = sprints.filter((s) => s.verified || (s.status && s.status.startsWith('Passed'))).length;
  const countNeedsAttention = countFailed + countOwed;
  const countAll = sprints.length;

  const elNA = document.getElementById('auditCountNeedsAttention');
  if (elNA) elNA.textContent = countNeedsAttention;
  const elF = document.getElementById('auditCountFailed');
  if (elF) elF.textContent = countFailed;
  const elO = document.getElementById('auditCountOwed');
  if (elO) elO.textContent = countOwed;
  const elP = document.getElementById('auditCountPassed');
  if (elP) elP.textContent = countPassed;
  const elA = document.getElementById('auditCountAll');
  if (elA) elA.textContent = countAll;

  if (badgeEl) {
    badgeEl.textContent = `${countOwed} Owed${countFailed > 0 ? ` • ${countFailed} Failed` : ''}`;
    badgeEl.className = `text-[10px] px-1.5 py-0.2 rounded font-semibold ${
      countNeedsAttention === 0
        ? 'bg-emerald-500/10 text-emerald-400 border border-emerald-500/20'
        : countFailed > 0
        ? 'bg-rose-500/10 text-rose-300 border border-rose-500/20'
        : 'bg-amber-500/10 text-amber-300 border border-amber-500/20'
    }`;
  }

  if (allPendingBtn) {
    allPendingBtn.disabled = countNeedsAttention === 0 || (state.heavyGateWorker && state.heavyGateWorker.is_running);
  }

  const filterKey = state.sprintAuditFilter || 'needs_attention';
  const displaySprints = sprints.filter((s) => {
    const isF = s.status === 'Failed';
    const isO = s.status === 'Owed';
    const isP = s.verified || (s.status && s.status.startsWith('Passed'));
    if (filterKey === 'needs_attention') return isF || isO;
    if (filterKey === 'failed') return isF;
    if (filterKey === 'owed') return isO;
    if (filterKey === 'passed') return isP;
    return true; // 'all'
  });

  // Sort by date completed descending (most recent first)
  displaySprints.sort((a, b) => {
    const dateA = a.completed_date || '';
    const dateB = b.completed_date || '';
    if (dateA && dateB && dateA !== dateB) {
      return dateB.localeCompare(dateA);
    }
    if (dateA && !dateB) return -1;
    if (!dateA && dateB) return 1;
    return compareSprintsDesc(a, b);
  });

  if (sprints.length === 0) {
    tbody.innerHTML = `<tr><td colspan="6" class="py-4 text-center text-slate-500 font-mono text-xs">No completed roadmap stories found.</td></tr>`;
    if (toggleEl) toggleEl.innerHTML = '';
    return;
  }

  if (displaySprints.length === 0) {
    const emptyMsg =
      filterKey === 'needs_attention'
        ? 'All stories verified — nothing needs attention ✅'
        : filterKey === 'failed'
        ? 'No failed stories found ✅'
        : filterKey === 'owed'
        ? 'No owed stories found ✅'
        : 'No stories found matching filter';
    tbody.innerHTML = `
      <tr>
        <td colspan="6" class="py-6 text-center">
          <div class="flex flex-col items-center gap-1.5 text-emerald-400">
            <i data-lucide="check-circle-2" class="w-5 h-5"></i>
            <span class="text-xs font-semibold">${emptyMsg}</span>
            ${countPassed > 0 ? `
              <div class="mt-2 text-xs text-slate-400">
                <span>All ${countPassed} past roadmap stories verified.</span>
                <button onclick="setSprintAuditFilter('passed')" class="text-emerald-400 hover:text-emerald-300 underline font-semibold ml-1 cursor-pointer">View Passed (${countPassed})</button>
                <span class="mx-1 text-slate-600">•</span>
                <button onclick="setSprintAuditFilter('all')" class="text-slate-300 hover:text-white underline font-semibold cursor-pointer">View All (${countAll})</button>
              </div>
            ` : ''}
          </div>
        </td>
      </tr>`;
  } else {
    tbody.innerHTML = displaySprints
      .map((s) => {
        const isRunning = state.heavyGateWorker && state.heavyGateWorker.is_running;
        const isCurrentRun = isRunning && state.heavyGateWorker.current_sprint === s.sprint;
        const isVerified = s.verified;
        const isFailed = s.status === 'Failed';

        let statusBadge;
        if (isCurrentRun) {
          statusBadge = `<span class="inline-flex items-center gap-1 text-[10px] px-2 py-0.5 rounded-full font-bold bg-amber-500/20 text-amber-300 border border-amber-500/40 animate-pulse">Running ⚡</span>`;
        } else if (isFailed) {
          const failCount = s.failure_count || (s.failed_tests ? s.failed_tests.length : 0);
          const countBadge = failCount > 0 ? ` (${failCount} tests)` : '';
          statusBadge = `<button onclick="openFailureModal('${escapeHtml(s.sprint)}', '${escapeHtml(s.run_id || '')}')" class="inline-flex items-center gap-1 text-[10px] px-2 py-0.5 rounded-full font-bold bg-rose-500/20 hover:bg-rose-500/30 text-rose-300 border border-rose-500/40 cursor-pointer transition shadow-sm hover:scale-105" title="Click to view failure details & copy pytest command">Failed ❌${countBadge}</button>`;
        } else if (isVerified) {
          let viaTip = '';
          if (s.subsumed_by) {
            viaTip = ` <span class="text-[9px] text-slate-400 font-normal">via ${escapeHtml(s.subsumed_by)}</span>`;
          } else if (s.status && s.status.includes('via')) {
            const mVia = s.status.match(/via\s+(.+)$/i);
            if (mVia) viaTip = ` <span class="text-[9px] text-slate-400 font-normal">via ${escapeHtml(mVia[1])}</span>`;
          }
          const titleTip = s.subsumed_by
            ? (/^[0-9a-f]{7,40}$/i.test(s.subsumed_by)
                ? `Verified via cumulative Gate 5 run on commit ${escapeHtml(s.subsumed_by)}`
                : `Verified via cumulative Gate 5 run on Story ${escapeHtml(s.subsumed_by)}`)
            : 'Verified Gate 5 run';
          const priorFailNotice = s.prior_failure
            ? `<span class="ml-1 text-[9px] text-amber-300 font-mono" title="Prior failure on earlier commit was resolved by descendant Gate 5 pass">⚠️</span>`
            : '';
          statusBadge = `<span class="inline-flex items-center gap-1 text-[10px] px-2 py-0.5 rounded-full font-bold bg-emerald-500/15 text-emerald-300 border border-emerald-500/30" title="${escapeHtml(titleTip)}">Passed ✅${viaTip}${priorFailNotice}</span>`;
        } else {
          statusBadge = `<span class="inline-flex items-center gap-1 text-[10px] px-2 py-0.5 rounded-full font-bold bg-amber-500/15 text-amber-300 border border-amber-500/30">Owed ⚠️</span>`;
        }

        let metrics = '';
        if (s.pass_count !== null || s.coverage !== null) {
          const p = s.pass_count !== null ? `${s.pass_count} passed` : '';
          const c = s.coverage ? `${s.coverage} cov` : '';
          const colorClass = isFailed ? 'text-rose-400' : 'text-emerald-400';
          const metricText = [p, c].filter(Boolean).join(' • ');
          let metricTitle = '';
          if (s.subsumed_by) {
            metricTitle = `Inherited from Gate 5 run on ${s.subsumed_by}` + (s.run_id ? ` (${s.run_id})` : '');
          } else if (s.run_id) {
            metricTitle = `Measured in Gate 5 run ${s.run_id}` + (s.sha ? ` at ${s.sha.slice(0, 7)}` : '');
          }
          const titleAttr = metricTitle ? ` title="${escapeHtml(metricTitle)}"` : '';
          metrics = `<span class="${colorClass} font-mono text-[11px] font-semibold cursor-help"${titleAttr}>${escapeHtml(metricText)}</span>`;
        } else if (isVerified) {
          metrics = '<span class="text-slate-600 font-mono text-[11px]" title="Verified without recorded test numbers">—</span>';
        } else {
          metrics = '<span class="text-slate-500 italic text-[11px]">Unverified</span>';
        }

        const shortSha = s.sha ? s.sha.slice(0, 7) : '--';

        const actionBtn = isCurrentRun
          ? `<span class="text-[10px] px-2 py-1 rounded bg-amber-500/20 text-amber-300 border border-amber-500/40 font-mono animate-pulse">Running...</span>`
          : isVerified
          ? `<span class="text-[10px] px-2 py-1 rounded bg-slate-800 text-slate-500 border border-slate-700 font-mono">Done</span>`
          : isFailed
          ? `<button onclick="startRunSprintGate('${escapeHtml(s.sprint)}', '${escapeHtml(s.sha || '')}')" ${
              isRunning ? 'disabled' : ''
            } class="px-2.5 py-1 bg-rose-950/60 hover:bg-rose-900 active:bg-rose-950 text-rose-200 hover:text-white text-[11px] font-medium rounded border border-rose-800/60 transition disabled:opacity-40 disabled:cursor-not-allowed">
              Retry Gate 5
            </button>`
          : `<button onclick="startRunSprintGate('${escapeHtml(s.sprint)}', '${escapeHtml(s.sha || '')}')" ${
              isRunning ? 'disabled' : ''
            } class="px-2.5 py-1 bg-slate-800 hover:bg-slate-700 active:bg-slate-900 text-slate-300 hover:text-white text-[11px] font-medium rounded border border-slate-700 transition disabled:opacity-40 disabled:cursor-not-allowed">
              Run Gate 5
            </button>`;

        const rowOpacity = isVerified ? 'opacity-60' : '';

        return `
          <tr class="hover:bg-slate-900/40 transition ${rowOpacity}">
            <td class="py-2 px-3 font-mono font-bold text-slate-200">
              Story ${escapeHtml(s.sprint)}
            </td>
            <td class="py-2 px-3 text-slate-300 max-w-xs truncate" title="${escapeHtml(s.scope)}">
              ${escapeHtml(s.scope)}
            </td>
            <td class="py-2 px-3 font-mono text-slate-400 text-[11px]">
              ${escapeHtml(shortSha)}
            </td>
            <td class="py-2 px-3 whitespace-nowrap">
              ${statusBadge}
            </td>
            <td class="py-2 px-3 whitespace-nowrap">
              ${metrics}
            </td>
            <td class="py-2 px-3 text-right whitespace-nowrap">
              ${actionBtn}
            </td>
          </tr>
        `;
      })
      .join('');
  }

  if (toggleEl) toggleEl.innerHTML = '';
  if (window.lucide) lucide.createIcons();
}

async function openFailureModal(sprintId, runId) {
  const modal = document.getElementById('failureDetailsModal');
  if (!modal) return;

  const titleEl = document.getElementById('failureModalTitle');
  const badgeEl = document.getElementById('failureModalSprintBadge');
  const bodyEl = document.getElementById('failureModalBody');

  if (titleEl) titleEl.textContent = `Gate 5 Failures — Story ${sprintId}`;
  if (badgeEl) badgeEl.textContent = runId ? `Run: ${runId}` : `Story ${sprintId}`;
  if (bodyEl) {
    bodyEl.innerHTML = `
      <div class="py-8 text-center text-slate-400 font-mono text-xs">
        <i data-lucide="loader-2" class="w-5 h-5 animate-spin mx-auto mb-2 text-rose-400"></i>
        Loading failure details...
      </div>
    `;
  }
  modal.classList.remove('hidden');
  if (window.lucide) lucide.createIcons();

  try {
    const params = new URLSearchParams();
    if (sprintId) params.set('sprint', sprintId);
    if (runId) params.set('run', runId);
    const data = await fetch(`/api/heavy-gate/failures?${params.toString()}`).then((r) => r.json());

    let failures = data.failed_tests || [];
    if (!failures.length && data.failures && data.failures.length) {
      const match = data.failures.find((f) => f.sprint === sprintId || f.run_id === runId);
      if (match) failures = match.failed_tests || [];
    }

    if (!failures || failures.length === 0) {
      bodyEl.innerHTML = `
        <div class="p-6 text-center text-slate-400">
          <i data-lucide="check-circle" class="w-8 h-8 text-emerald-400 mx-auto mb-2"></i>
          <p class="font-semibold text-slate-200">No active test failure details found</p>
          <p class="text-xs text-slate-500 mt-1">Tests may have been resolved in a later run or log was cleared.</p>
        </div>
      `;
      if (window.lucide) lucide.createIcons();
      return;
    }

    bodyEl.innerHTML = `
      <div class="mb-3 text-xs text-rose-300 font-medium">
        Found <span class="font-mono font-bold">${failures.length}</span> failed test${failures.length > 1 ? 's' : ''}:
      </div>
      <div class="space-y-3">
        ${failures.map((f, idx) => {
          const testName = escapeHtml(f.test_name || 'Unknown test');
          const filePath = escapeHtml(f.file_path || '');
          const cmd = escapeHtml(f.command || `uv run pytest ${f.file_path} -k ${f.test_name}`);
          const msg = escapeHtml(f.message || 'No failure message recorded');
          return `
            <div class="failure-test-card">
              <div class="flex flex-col sm:flex-row sm:items-center justify-between gap-2 mb-1.5">
                <div class="font-mono text-xs font-bold text-rose-300 flex items-center gap-1.5">
                  <span class="text-slate-500">${idx + 1}.</span>
                  <span class="break-all">${testName}</span>
                </div>
                <button onclick="copyToClipboard('${cmd}', this)" class="shrink-0 inline-flex items-center gap-1.5 px-2.5 py-1 rounded bg-slate-800 hover:bg-slate-700 text-slate-200 border border-slate-700 text-[11px] font-mono transition group" title="Copy exact pytest command">
                  <i data-lucide="copy" class="w-3 h-3 text-slate-400 group-hover:text-white"></i>
                  <span>Copy command</span>
                </button>
              </div>
              <div class="text-[11px] font-mono text-slate-400 mb-2 flex items-center gap-1">
                <i data-lucide="file-code" class="w-3 h-3 text-slate-500"></i>
                <span class="break-all">${filePath}</span>
              </div>
              <pre class="bg-slate-950 p-2.5 rounded border border-rose-950/60 text-rose-200 font-mono text-[11px] overflow-x-auto leading-relaxed whitespace-pre-wrap">${msg}</pre>
            </div>
          `;
        }).join('')}
      </div>
    `;
  } catch (err) {
    console.error('Failed to load failure details:', err);
    bodyEl.innerHTML = `
      <div class="p-4 text-center text-rose-400">
        <p class="font-semibold">Failed to fetch failure details</p>
        <p class="text-xs text-slate-500 mt-1">${escapeHtml(err.message)}</p>
      </div>
    `;
  }
  if (window.lucide) lucide.createIcons();
}

function closeFailureModal() {
  const modal = document.getElementById('failureDetailsModal');
  if (modal) modal.classList.add('hidden');
}

function copyToClipboard(text, btn) {
  if (navigator.clipboard && navigator.clipboard.writeText) {
    navigator.clipboard.writeText(text).then(() => {
      if (btn) {
        const origHtml = btn.innerHTML;
        btn.innerHTML = `<i data-lucide="check" class="w-3 h-3 text-emerald-400"></i><span class="text-emerald-400">Copied!</span>`;
        if (window.lucide) lucide.createIcons();
        setTimeout(() => {
          btn.innerHTML = origHtml;
          if (window.lucide) lucide.createIcons();
        }, 2000);
      }
    }).catch(() => fallbackCopy(text, btn));
  } else {
    fallbackCopy(text, btn);
  }
}

function fallbackCopy(text, btn) {
  const textarea = document.createElement('textarea');
  textarea.value = text;
  textarea.style.position = 'fixed';
  textarea.style.opacity = '0';
  document.body.appendChild(textarea);
  textarea.select();
  try {
    document.execCommand('copy');
    if (btn) {
      const origHtml = btn.innerHTML;
      btn.innerHTML = `<i data-lucide="check" class="w-3 h-3 text-emerald-400"></i><span class="text-emerald-400">Copied!</span>`;
      if (window.lucide) lucide.createIcons();
      setTimeout(() => {
        btn.innerHTML = origHtml;
        if (window.lucide) lucide.createIcons();
      }, 2000);
    }
  } catch (err) {
    console.error('Fallback copy failed', err);
  }
  document.body.removeChild(textarea);
}


async function startRunSprintGate(sprintId, commitSha) {
  if (!confirm(`Run Heavy Gate 5 for Story ${sprintId} in isolated worktree? This runs full pytest & coverage against commit ${commitSha || 'resolved SHA'}.`)) {
    return;
  }
  try {
    const terminal = document.getElementById('heavyGateTerminal');
    if (terminal) {
      terminal.textContent = `[Starting finalgates.ps1 for Story ${sprintId} in background worker...]\n[Worktree preparing & pytest suite initializing... output will stream below]\n`;
    }

    const res = await fetch('/api/heavy-gate/start', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ sprint: sprintId, sha: commitSha || undefined }),
    }).then((r) => r.json());

    if (res.success) {
      await fetchHeavyGateStatus();
      await fetchHeavyGateLog();
    } else {
      alert(`Error starting heavy gate: ${res.error || 'Server error'}`);
    }
  } catch (err) {
    console.error(`Failed to start heavy gate for sprint ${sprintId}:`, err);
    alert('Failed to start heavy gate execution.');
  }
}

async function startRunAllPendingSprints() {
  const unverified = (state.sprintGateStatus || []).filter((s) => !s.verified);
  if (unverified.length === 0) {
    alert('All completed stories have verified Gate 5 runs.');
    return;
  }
  const count = unverified.length;
  if (!confirm(`Run full Gate 5 sequentially for ALL ${count} pending stor(ies) in isolated worktrees? This will run each story from oldest to newest.`)) {
    return;
  }

  try {
    const terminal = document.getElementById('heavyGateTerminal');
    if (terminal) terminal.textContent = `[Queuing ${count} pending sprint(s) for sequential execution in background...]\n`;

    const res = await fetch('/api/heavy-gate/start', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ all_pending: true }),
    }).then((r) => r.json());

    if (res.success) {
      await fetchHeavyGateStatus();
      await fetchSprintGateStatus();
      fetchHeavyGateLog();
    } else {
      alert(`Error queuing pending sprints: ${res.error || 'Server error'}`);
    }
  } catch (err) {
    console.error('Failed to run all pending sprints:', err);
    alert('Failed to start sequential heavy gate runs.');
  }
}

// -----------------------------------------------------------------------------
// Event Listeners
// -----------------------------------------------------------------------------
document.addEventListener('DOMContentLoaded', () => {
  initTheme();
  initSidebarState();
  applyActiveStreamsCols();

  document.querySelectorAll('.nav-tab').forEach((tab) => {
    tab.addEventListener('click', () => {
      switchTab(tab.getAttribute('data-tab'));
    });
  });

  const refreshBtn = document.getElementById('refreshBtn');
  if (refreshBtn) refreshBtn.addEventListener('click', fetchAll);

  const sprintFilter = document.getElementById('pipelineSprintFilter');
  if (sprintFilter) {
    sprintFilter.addEventListener('change', (e) => {
      state.filters.sprint = e.target.value;
      renderPipeline();
    });
  }

  const sprintSelect = document.getElementById('activeSprintSelect');
  if (sprintSelect) {
    sprintSelect.addEventListener('change', (e) => {
      fetchActiveSprint(e.target.value);
    });
  }

  const typeFilters = document.getElementById('pipelineTypeFilters');
  if (typeFilters) {
    typeFilters.addEventListener('click', (e) => {
      const btn = e.target.closest('.type-filter-pill, .segmented-control-btn');
      if (btn) {
        typeFilters.querySelectorAll('.type-filter-pill, .segmented-control-btn').forEach((p) => p.classList.remove('active'));
        btn.classList.add('active');
        state.filters.type = btn.getAttribute('data-type');
        renderPipeline();
      }
    });
  }

  const searchInput = document.getElementById('pipelineSearch');
  if (searchInput) {
    searchInput.addEventListener('input', (e) => {
      state.filters.search = e.target.value;
      renderPipeline();
    });
  }

  // New card modal form inputs
  const cardTitleInput = document.getElementById('newCardTitleInput');
  const cardTypeSelect = document.getElementById('newCardTypeSelect');
  const cardStoryInput = document.getElementById('newCardStoryInput');
  if (cardTitleInput) cardTitleInput.addEventListener('input', updateNewCardPreview);
  if (cardTypeSelect) cardTypeSelect.addEventListener('change', updateNewCardPreview);
  if (cardStoryInput) cardStoryInput.addEventListener('input', updateNewCardPreview);

  initProjectUi();
  initMobileKanbanSwipe();
  initKanbanDragDrop();
  fetchProjects().then(() => fetchAll());
  // Auto refresh every 10 seconds
  setInterval(fetchAll, 10000);

  // Global Keyboard Shortcuts
  document.addEventListener('keydown', (e) => {
    // Spotlight Search shortcut: /
    if (e.key === '/' && !['INPUT', 'TEXTAREA', 'SELECT'].includes(document.activeElement.tagName)) {
      const sInput = document.getElementById('pipelineSearch');
      if (sInput) {
        e.preventDefault();
        sInput.focus();
        sInput.select();
      }
      return;
    }

    // Toggle Sidebar shortcut: Cmd+B or Ctrl+B
    if ((e.metaKey || e.ctrlKey) && (e.key === 'b' || e.key === 'B')) {
      e.preventDefault();
      toggleSidebarCollapse();
      return;
    }

    // Dismiss modals and drawers: Escape
    if (e.key === 'Escape') {
      if (isCloseoutOpen()) { closeCloseout(); return; }
      if (isConfigureOpen()) { closeConfigureProject(); return; }
      closeNewCardModal();
      closeMobileConnectModal();
      closeSprintDoc();
      closeCardModal();
      closeFailureModal();
      closeReviewModal();
      closeMobileSidebar();
    }
  });
});

// -----------------------------------------------------------------------------
// Sprint Spec / Document Modal
// -----------------------------------------------------------------------------
async function openSprintDoc(sprintId) {
  const modal = document.getElementById('sprintDocModal');
  const titleEl = document.getElementById('sprintDocTitle');
  const sourceEl = document.getElementById('sprintDocSource');
  const subtitleEl = document.getElementById('sprintDocSubtitle');
  const bodyEl = document.getElementById('sprintDocBody');

  if (!modal) return;

  titleEl.textContent = `${sprintId}`;
  sourceEl.textContent = 'Loading...';
  subtitleEl.textContent = 'Fetching specification and settled rationale...';
  bodyEl.innerHTML = `
    <div class="py-12 text-center text-slate-500">
      <i data-lucide="loader" class="w-7 h-7 mx-auto mb-2 animate-spin text-emerald-400"></i>
      <p class="text-xs">Loading story documentation...</p>
    </div>
  `;
  modal.classList.remove('hidden');
  if (window.lucide) lucide.createIcons();

  try {
    const encoded = encodeURIComponent(sprintId);
    const resp = await fetch(`/api/sprints/${encoded}/doc`);
    if (!resp.ok) {
      if (resp.status === 404) {
        sourceEl.textContent = 'Not Found';
        bodyEl.innerHTML = `
          <div class="py-10 text-center text-amber-400/90">
            <i data-lucide="alert-circle" class="w-8 h-8 mx-auto mb-2 text-amber-400"></i>
            <p class="font-medium text-sm">Specification not found for ${escapeHtml(sprintId)}</p>
            <p class="text-slate-500 text-xs mt-1">This story does not have an isolated section in PLAN.md or docs/sprints/.</p>
          </div>
        `;
        if (window.lucide) lucide.createIcons();
        return;
      }
      throw new Error(`HTTP ${resp.status}: ${resp.statusText || 'Server Error'}`);
    }
    const res = await resp.json();

    if (res.content) {
      titleEl.textContent = res.title || sprintId;
      sourceEl.textContent = res.source || 'PLAN.md';
      subtitleEl.textContent = `Authoritative scope, decisions, and definition of done`;

      const rawHtml =
        window.marked && typeof marked.parse === 'function'
          ? marked.parse(res.content)
          : `<pre class="whitespace-pre-wrap font-sans text-xs text-slate-300 leading-relaxed">${escapeHtml(res.content)}</pre>`;
      bodyEl.innerHTML =
        window.DOMPurify && typeof DOMPurify.sanitize === 'function'
          ? DOMPurify.sanitize(rawHtml)
          : rawHtml;
    } else {
      sourceEl.textContent = 'Not Found';
      bodyEl.innerHTML = `
        <div class="py-10 text-center text-amber-400/90">
          <i data-lucide="alert-circle" class="w-8 h-8 mx-auto mb-2 text-amber-400"></i>
          <p class="font-medium text-sm">Specification not found for ${escapeHtml(sprintId)}</p>
          <p class="text-slate-500 text-xs mt-1">This story does not have an isolated section in PLAN.md or docs/sprints/.</p>
        </div>
      `;
    }
  } catch (err) {
    console.error('Failed to load sprint doc:', err);
    bodyEl.innerHTML = `
      <div class="py-10 text-center text-rose-400">
        <i data-lucide="x-circle" class="w-8 h-8 mx-auto mb-2"></i>
        <p class="font-medium text-sm">Error loading document</p>
        <p class="text-slate-500 text-xs mt-1">${escapeHtml(err.message)}</p>
      </div>
    `;
  }

  if (window.lucide) lucide.createIcons();
}

function closeSprintDoc() {
  const modal = document.getElementById('sprintDocModal');
  if (modal) modal.classList.add('hidden');
}

// -----------------------------------------------------------------------------
// Card Details Modal
// -----------------------------------------------------------------------------
function openCardModal(card) {
  const modal = document.getElementById('cardModal');
  const backdrop = document.getElementById('cardDrawerBackdrop');
  if (!modal) return;

  document.getElementById('cardModalId').textContent = card.mn_id;
  document.getElementById('cardModalSprint').textContent = card.sprint ? `Story ${card.sprint}` : 'Unassigned';

  const typeEl = document.getElementById('cardModalType');
  typeEl.textContent = card.type || 'feature';
  typeEl.className = `text-xs px-2.5 py-0.5 rounded font-semibold ${
    card.type === 'bug' ? 'pill-bug' : card.type === 'improvement' ? 'pill-improvement' : 'pill-feature'
  }`;

  document.getElementById('cardModalTitle').textContent = card.title;
  document.getElementById('cardModalDates').innerHTML = `
    <span class="flex items-center"><i class="ph ph-calendar-blank text-[var(--text-quaternary)] mr-1"></i>Created: ${card.created_date || 'N/A'}</span>
    ${card.validated ? `<span class="text-emerald-500 flex items-center"><i class="ph ph-check mr-1"></i>Validated: ${card.validated}</span>` : ''}
    <span class="text-[var(--text-tertiary)] truncate max-w-xs">File: ${card.file_name}</span>
  `;

  document.getElementById('cardModalDesc').innerHTML = renderMarkdown(card.description || 'No description provided.');

  // DoD list (interactive checkboxes)
  const dodContainer = document.getElementById('cardModalDodList');
  if (card.dod && card.dod.length > 0) {
    document.getElementById('cardModalDodContainer').classList.remove('hidden');
    const escId = escapeHtml(card.mn_id);
    dodContainer.innerHTML = card.dod
      .map(
        (item, idx) => `
      <label class="flex items-start space-x-3 p-2 rounded-lg hover:bg-[var(--bg-glass-hover)] cursor-pointer text-sm transition touch-target-item">
        <input type="checkbox" ${item.checked ? 'checked' : ''} data-action="toggle-dod" data-card-id="${escId}" data-idx="${idx}" class="mt-0.5 w-4 h-4 rounded border-[var(--border-medium)] text-emerald-500 focus:ring-emerald-500/20 bg-[var(--bg-surface)] cursor-pointer">
        <span class="${item.checked ? 'text-[var(--text-tertiary)] line-through' : 'text-[var(--text-primary)] font-medium'} text-sm leading-relaxed">${formatRichText(item.text)}</span>
      </label>
    `
      )
      .join('');
  } else {
    document.getElementById('cardModalDodContainer').classList.add('hidden');
  }

  // Human QA Test section
  const qaContainer = document.getElementById('cardModalQaContainer');
  const qaInstrEl = document.getElementById('cardModalQaInstructions');
  const qaListEl = document.getElementById('cardModalQaList');
  if (qaContainer) {
    if ((card.qa_items && card.qa_items.length > 0) || (card.qa_instructions && card.qa_instructions.length > 0)) {
      qaContainer.classList.remove('hidden');
      if (qaInstrEl) {
        if (card.qa_instructions) {
          qaInstrEl.innerHTML = renderMarkdown(card.qa_instructions);
          qaInstrEl.classList.remove('hidden');
        } else {
          qaInstrEl.classList.add('hidden');
        }
      }
      if (qaListEl) {
        const escId = escapeHtml(card.mn_id);
        qaListEl.innerHTML = (card.qa_items || [])
          .map(
            (item, idx) => `
          <label class="flex items-start space-x-3 p-2.5 rounded-lg bg-[var(--bg-surface-secondary)] border border-[var(--border-subtle)] hover:border-amber-500/40 cursor-pointer text-sm transition touch-target-item">
            <input type="checkbox" ${item.checked ? 'checked' : ''} data-action="toggle-qa" data-card-id="${escId}" data-idx="${idx}" class="mt-0.5 w-4 h-4 rounded border-[var(--border-medium)] text-amber-500 focus:ring-amber-500/20 bg-[var(--bg-surface)] cursor-pointer">
            <span class="${item.checked ? 'text-emerald-500 font-medium' : 'text-[var(--text-primary)] font-medium'} text-sm leading-relaxed">${formatRichText(item.text)}</span>
          </label>
        `
          )
          .join('');
      }
    } else {
      qaContainer.classList.add('hidden');
    }
  }

  // Changelog
  const clContainer = document.getElementById('cardModalChangelog');
  if (card.changelog && card.changelog.length > 0) {
    document.getElementById('cardModalChangelogContainer').classList.remove('hidden');
    clContainer.innerHTML = renderChangelogTable(card.changelog, card);
  } else {
    document.getElementById('cardModalChangelogContainer').classList.add('hidden');
  }

  // Story Close-Out button
  const coBtn = document.getElementById('cardModalCloseoutBtn');
  if (coBtn) {
    coBtn.classList.toggle('hidden', !(card.closeout && card.closeout.url));
    coBtn.dataset.cardId = card.mn_id;
  }

  // View Story Spec button visibility
  const specBtn = document.getElementById('cardModalViewSpecBtn');
  if (specBtn) {
    if (card.sprint && card.sprint !== 'Unassigned') {
      specBtn.classList.remove('hidden');
    } else {
      specBtn.classList.add('hidden');
    }
  }

  // View Reviews button in card header
  const reviewsBtn = document.getElementById('cardModalReviewsBtn');
  const reviewsLabel = document.getElementById('cardModalReviewsBtnLabel');
  if (reviewsBtn) {
    const revCount = card.review_count || (card.reviews ? card.reviews.length : 0);
    if (revCount > 0) {
      reviewsBtn.classList.remove('hidden');
      if (reviewsLabel) reviewsLabel.textContent = `Reviews (${revCount})`;
    } else {
      reviewsBtn.classList.add('hidden');
    }
  }

  // Slide-over drawer display
  modal.classList.remove('translate-x-full', 'pointer-events-none');
  modal.classList.add('translate-x-0');
  if (backdrop) {
    backdrop.classList.remove('opacity-0', 'pointer-events-none');
    backdrop.classList.add('opacity-100');
  }

  if (window.lucide) lucide.createIcons();
}

function onCardModalViewSpec() {
  const sprintEl = document.getElementById('cardModalSprint');
  const sprintText = sprintEl ? sprintEl.textContent.replace(/^(?:Sprint|Story)\s+/i, '').trim() : '';
  if (sprintText && sprintText !== 'Unassigned') {
    closeCardModal();
    openSprintDoc(sprintText);
  }
}

function onCardModalViewReviews() {
  const cardId = document.getElementById('cardModalId').textContent.trim();
  const sprintEl = document.getElementById('cardModalSprint');
  const sprintText = sprintEl ? sprintEl.textContent.replace(/^(?:Sprint|Story)\s+/i, '').trim() : '';
  const target = cardId || sprintText;
  if (target) {
    openReviewModal(target);
  }
}

function closeCardModal() {
  const modal = document.getElementById('cardModal');
  const backdrop = document.getElementById('cardDrawerBackdrop');
  if (modal) {
    modal.classList.remove('translate-x-0');
    modal.classList.add('translate-x-full', 'pointer-events-none');
  }
  if (backdrop) {
    backdrop.classList.remove('opacity-100');
    backdrop.classList.add('opacity-0', 'pointer-events-none');
  }
}

// -----------------------------------------------------------------------------
// Dual-Theme Engine (Apple Daylight / Carbon Zinc)
// -----------------------------------------------------------------------------
function initTheme() {
  const isDark = document.documentElement.classList.contains('dark');
  updateThemeIcon(isDark);
}

function toggleTheme() {
  const isDark = document.documentElement.classList.toggle('dark');
  localStorage.setItem('pmqa_theme', isDark ? 'dark' : 'light');
  updateThemeIcon(isDark);
}

function updateThemeIcon(isDark) {
  const btn = document.getElementById('themeToggleBtn');
  if (!btn) return;
  btn.innerHTML = isDark
    ? '<i class="ph ph-sun text-amber-500 text-sm"></i><span class="theme-label hidden sm:inline text-xs">Light</span>'
    : '<i class="ph ph-moon text-indigo-500 text-sm"></i><span class="theme-label hidden sm:inline text-xs">Dark</span>';
  btn.setAttribute('title', isDark ? 'Switch to Apple Daylight (Light)' : 'Switch to Carbon/Zinc (Dark)');
}

// -----------------------------------------------------------------------------
// Sidebar Collapse & Mobile Drawer
// -----------------------------------------------------------------------------
function toggleSidebarCollapse() {
  const sidebar = document.getElementById('appSidebar');
  if (!sidebar) return;
  const isCollapsed = sidebar.classList.toggle('collapsed');
  localStorage.setItem('pmqa_sidebar_collapsed', isCollapsed ? 'true' : 'false');
}

function initSidebarState() {
  const sidebar = document.getElementById('appSidebar');
  if (!sidebar) return;
  if (localStorage.getItem('pmqa_sidebar_collapsed') === 'true') {
    sidebar.classList.add('collapsed');
  }
}

function toggleMobileSidebar() {
  const sidebar = document.getElementById('appSidebar');
  const backdrop = document.getElementById('sidebarBackdrop');
  if (!sidebar || !backdrop) return;
  const isOpen = sidebar.classList.contains('translate-x-0');
  if (isOpen) {
    closeMobileSidebar();
  } else {
    sidebar.classList.remove('-translate-x-full');
    sidebar.classList.add('translate-x-0');
    backdrop.classList.remove('hidden');
    setTimeout(() => backdrop.classList.remove('opacity-0'), 10);
  }
}

function closeMobileSidebar() {
  const sidebar = document.getElementById('appSidebar');
  const backdrop = document.getElementById('sidebarBackdrop');
  if (sidebar) {
    sidebar.classList.remove('translate-x-0');
    sidebar.classList.add('-translate-x-full');
  }
  if (backdrop) {
    backdrop.classList.add('opacity-0');
    setTimeout(() => backdrop.classList.add('hidden'), 300);
  }
}

// -----------------------------------------------------------------------------
// New Card Modal / Quick Generator
// -----------------------------------------------------------------------------
function openNewCardModal() {
  const modal = document.getElementById('newCardModal');
  if (!modal) return;
  const storyInput = document.getElementById('newCardStoryInput');
  if (storyInput && state.sprintDodData && state.sprintDodData.sprint_name) {
    storyInput.value = state.sprintDodData.sprint_name.replace(/^(?:Story|Sprint)\s+/i, '');
  }
  updateNewCardPreview();
  modal.classList.remove('hidden');
}

function closeNewCardModal() {
  const modal = document.getElementById('newCardModal');
  if (modal) modal.classList.add('hidden');
}

function updateNewCardPreview() {
  const title = (document.getElementById('newCardTitleInput')?.value || 'Title of the card').trim();
  const type = document.getElementById('newCardTypeSelect')?.value || 'feature';
  const story = (document.getElementById('newCardStoryInput')?.value || '1').trim();
  const pfx = (state.currentProject && state.currentProject.prefix) ? state.currentProject.prefix : 'PQC';
  const today = new Date().toISOString().slice(0, 10);

  const cardId = `${pfx}-${story}`;
  const template = `---
id: ${cardId}
story: "${story}"
type: ${type}
status: story-backlog
created: ${today}
validated: false
---

# ${cardId} - ${title}

## Description

${title}

## Definition of Done

- [ ] Implementation completed and verified
- [ ] Fast gates pass: uv run --system-certs --group dev pytest -q
- [ ] Cold review PASS (pm-review)
- [ ] User confirmed in chat

## Human QA Test

### Verification Instructions
1. Inspect functionality and verify expected behavior.

### Verification Check
- [ ] Human QA test completed and verified

## Changelog

| Date & Time | Agent | What |
|---|---|---|
| ${today} 12:00 | Lead | Created card |
`;

  const previewEl = document.getElementById('newCardPreview');
  if (previewEl) previewEl.textContent = template;
}

function copyNewCardTemplate() {
  const previewEl = document.getElementById('newCardPreview');
  if (!previewEl) return;
  copyToClipboard(previewEl.textContent, document.getElementById('copyNewCardBtn'));
}

function escapeHtml(str) {
  if (!str) return '';
  return String(str)
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')
    .replace(/'/g, '&#039;');
}

// Configure marked for GFM and soft line breaks
if (window.marked && typeof window.marked.setOptions === 'function') {
  window.marked.setOptions({
    breaks: true,
    gfm: true,
  });
}

function renderMarkdown(str) {
  if (!str) return '';
  let html = '';
  if (window.marked && typeof window.marked.parse === 'function') {
    try {
      html = window.marked.parse(String(str));
    } catch (e) {
      console.error('marked.parse error:', e);
      html = escapeHtml(String(str));
    }
  } else {
    html = escapeHtml(String(str));
  }
  if (window.DOMPurify && typeof window.DOMPurify.sanitize === 'function') {
    html = window.DOMPurify.sanitize(html);
  }
  return `<div class="markdown-content">${html}</div>`;
}

function formatRichText(str) {
  if (!str) return '';
  // 1. Normalize line break tags: <br>, <br/>, <br />
  let s = String(str).replace(/<br\s*\/?>/gi, '\n');
  // 2. Escape HTML entities to prevent XSS
  s = escapeHtml(s);
  // 3. Convert markdown links [text](url)
  s = s.replace(/\[([^\]]+)\]\((https?:\/\/[^\s)]+|\/[^\s)]+)\)/g, '<a href="$2" target="_blank" rel="noopener noreferrer" class="text-emerald-400 hover:underline">$1</a>');
  // 4. Convert bold **text** or __text__
  s = s.replace(/\*\*([^*]+)\*\*/g, '<strong class="font-semibold text-slate-100">$1</strong>');
  s = s.replace(/__([^_]+)__/g, '<strong class="font-semibold text-slate-100">$1</strong>');
  // 5. Convert inline code `code` to styled code pill
  s = s.replace(/`([^`]+)`/g, '<code class="px-1.5 py-0.5 rounded bg-slate-900 text-sky-300 font-mono text-[11px] border border-slate-800">$1</code>');
  // 6. Convert newlines into <br> tags
  s = s.replace(/\r?\n/g, '<br>');
  return s;
}

function toggleChangelogSort(cardId) {
  state.changelogSortOrder = (state.changelogSortOrder || 'asc') === 'asc' ? 'desc' : 'asc';
  if (cardId) {
    const card = state.cards.find((c) => c.mn_id === cardId);
    if (card) {
      const modal = document.getElementById('cardModal');
      if (modal && !modal.classList.contains('hidden')) {
        openCardModal(card);
      }
      if (state.activeTab === 'qa' && state.selectedQaId === cardId) {
        selectQaCard(cardId, { preserveScroll: true });
      }
    }
  }
}

function renderChangelogTable(changelog, card = null) {
  if (!changelog || changelog.length === 0) return '';

  const order = state.changelogSortOrder || 'asc';
  const sorted = [...changelog].sort((a, b) => {
    const keyA = `${a.date || ''} ${a.time || ''}`.trim();
    const keyB = `${b.date || ''} ${b.time || ''}`.trim();
    if (keyA !== keyB) {
      return order === 'asc' ? keyA.localeCompare(keyB) : keyB.localeCompare(keyA);
    }
    return 0;
  });

  return `
    <div class="bg-slate-950 rounded-lg border border-slate-800 overflow-x-auto shadow-sm">
      <table class="w-full text-left border-collapse min-w-[500px]">
        <thead>
          <tr class="bg-slate-900/90 border-b border-slate-800 text-xs font-bold text-slate-300 uppercase tracking-wider">
            <th class="py-2.5 px-3 w-40 whitespace-nowrap cursor-pointer select-none hover:text-emerald-400 transition-colors group" onclick="toggleChangelogSort('${card ? escapeHtml(card.mn_id) : ''}')" title="Click to toggle ascending/descending order">
              <div class="inline-flex items-center gap-1.5">
                <span>Date &amp; Time</span>
                <span class="text-[10px] font-mono text-emerald-400 group-hover:scale-110 transition-transform">${order === 'asc' ? '▲ (Asc)' : '▼ (Desc)'}</span>
              </div>
            </th>
            <th class="py-2.5 px-3 w-44 whitespace-nowrap">Agent</th>
            <th class="py-2.5 px-3">Work & Evidence</th>
          </tr>
        </thead>
        <tbody class="divide-y divide-slate-800/60 text-sm">
          ${sorted
            .map(
              (cl) => {
                const targetRef = card ? (card.sprint || card.mn_id) : '';
                const reviewBtn = cl.has_review
                  ? `<button onclick="openReviewModal('${escapeHtml(targetRef)}', ${cl.review_round || 1})" class="inline-flex items-center gap-1 px-2.5 py-0.5 rounded bg-purple-500/15 hover:bg-purple-500/25 text-purple-300 border border-purple-500/40 text-xs font-semibold transition ml-2 shrink-0 group">
                      <i data-lucide="shield-check" class="w-3.5 h-3.5 text-purple-400 group-hover:scale-110 transition-transform"></i>
                      <span>View Review</span>
                    </button>`
                  : '';

                const timeHtml = cl.time
                  ? `<div class="flex items-center gap-1.5 text-slate-400 font-mono text-xs mt-0.5">
                      <i data-lucide="clock" class="w-3 h-3 text-slate-500 shrink-0"></i>
                      <span>${escapeHtml(cl.time)}</span>
                    </div>`
                  : '';

                return `
            <tr class="hover:bg-slate-900/40 transition align-top">
              <td class="py-2.5 px-3 font-mono text-slate-300 font-semibold text-xs whitespace-nowrap">
                <div class="flex flex-col pt-0.5">
                  <div class="flex items-center gap-1.5">
                    <i data-lucide="calendar" class="w-3.5 h-3.5 text-slate-500 shrink-0"></i>
                    <span>${escapeHtml(cl.date)}</span>
                  </div>
                  ${timeHtml}
                </div>
              </td>
              <td class="py-2.5 px-3 whitespace-nowrap">
                <span class="inline-block px-2.5 py-0.5 rounded bg-emerald-500/10 text-emerald-400 border border-emerald-500/20 font-mono text-xs font-semibold whitespace-nowrap leading-snug">
                  ${escapeHtml(cl.agent)}
                </span>
              </td>
              <td class="py-2.5 px-3 text-slate-200 font-sans text-sm leading-relaxed break-words">
                <div class="flex items-start justify-between gap-2">
                  <div class="flex-1">${formatRichText(cl.what)}</div>
                  ${reviewBtn}
                </div>
              </td>
            </tr>
          `;
              }
            )
            .join('')}
        </tbody>
      </table>
    </div>
  `;
}

// -----------------------------------------------------------------------------
// Review Modal Logic
// -----------------------------------------------------------------------------
async function openReviewModal(sprintOrCard, targetRound = null) {
  const modal = document.getElementById('reviewModal');
  if (!modal) return;

  modal.classList.remove('hidden');
  const titleEl = document.getElementById('reviewModalTitle');
  const metaEl = document.getElementById('reviewModalMeta');
  const chkSection = document.getElementById('reviewModalChecklistSection');
  const bodyEl = document.getElementById('reviewModalBody');
  const roundSel = document.getElementById('reviewRoundSelect');
  const verdictBadge = document.getElementById('reviewModalVerdictBadge');
  const roundBadge = document.getElementById('reviewModalRoundBadge');

  if (titleEl) titleEl.textContent = `Cold Review — ${sprintOrCard}`;
  if (verdictBadge) verdictBadge.textContent = '';
  if (roundBadge) roundBadge.textContent = '';
  if (metaEl) metaEl.innerHTML = '';
  if (bodyEl) {
    bodyEl.innerHTML = `
      <div class="py-10 text-center text-slate-400 font-mono text-xs">
        <i data-lucide="loader-2" class="w-5 h-5 animate-spin mx-auto mb-2 text-purple-400"></i>
        Loading review report...
      </div>
    `;
  }
  if (chkSection) chkSection.classList.add('hidden');
  if (roundSel) roundSel.classList.add('hidden');
  if (window.lucide) lucide.createIcons();

  try {
    const isCard = String(sprintOrCard).startsWith('MN-');
    const url = isCard
      ? `/api/reviews/card/${encodeURIComponent(sprintOrCard)}`
      : `/api/reviews/${encodeURIComponent(sprintOrCard)}`;
    const data = await fetch(url).then((r) => r.json());
    const reviews = data.reviews || [];

    if (!reviews || reviews.length === 0) {
      if (metaEl) metaEl.innerHTML = `<span>No cold reviews found for ${escapeHtml(sprintOrCard)}</span>`;
      bodyEl.innerHTML = `
        <div class="p-8 text-center text-slate-400">
          <i data-lucide="shield-alert" class="w-8 h-8 text-amber-400 mx-auto mb-2"></i>
          <p class="font-semibold text-slate-200">No Review Report Available</p>
          <p class="text-xs text-slate-500 mt-1">No reports matching docs/reviews/MN-*.md or docs/QUALITY.md were found for this sprint/card.</p>
        </div>
      `;
      if (window.lucide) lucide.createIcons();
      return;
    }

    state.currentReviews = reviews;

    // Pick target review by round if specified, else latest
    let activeIdx = 0;
    if (targetRound) {
      const foundIdx = reviews.findIndex((r) => r.round === parseInt(targetRound, 10));
      if (foundIdx !== -1) activeIdx = foundIdx;
    }
    state.currentReviewIndex = activeIdx;

    // Setup round select if multiple
    if (reviews.length > 1 && roundSel) {
      roundSel.innerHTML = reviews.map((r, i) => `
        <option value="${i}" ${i === activeIdx ? 'selected' : ''}>Round ${r.round} (${r.verdict})</option>
      `).join('');
      roundSel.classList.remove('hidden');
    }

    renderActiveReview(reviews[activeIdx]);
  } catch (err) {
    console.error('Failed to load review report:', err);
    bodyEl.innerHTML = `
      <div class="p-4 text-center text-rose-400">
        <p class="font-semibold">Failed to fetch review</p>
        <p class="text-xs text-slate-500 mt-1">${escapeHtml(err.message)}</p>
      </div>
    `;
  }
}

function onReviewRoundChange(idxStr) {
  const idx = parseInt(idxStr, 10);
  if (!isNaN(idx) && state.currentReviews && state.currentReviews[idx]) {
    state.currentReviewIndex = idx;
    renderActiveReview(state.currentReviews[idx]);
  }
}

function renderActiveReview(rev) {
  const titleEl = document.getElementById('reviewModalTitle');
  const verdictEl = document.getElementById('reviewModalVerdictBadge');
  const roundEl = document.getElementById('reviewModalRoundBadge');
  const metaEl = document.getElementById('reviewModalMeta');
  const chkSection = document.getElementById('reviewModalChecklistSection');
  const findingsList = document.getElementById('reviewFindingsList');
  const countBadge = document.getElementById('reviewFindingsCountBadge');
  const bodyEl = document.getElementById('reviewModalBody');
  const footerInfo = document.getElementById('reviewModalFooterInfo');

  if (titleEl) titleEl.textContent = `Cold Review — Story ${rev.sprint}`;
  if (roundEl) roundEl.textContent = `Round ${rev.round}`;

  if (verdictEl) {
    const isPass = rev.verdict === 'PASS' || String(rev.verdict).includes('PASS');
    verdictEl.textContent = rev.verdict;
    verdictEl.className = `text-[10px] px-2 py-0.5 rounded font-bold font-mono ${
      isPass
        ? 'bg-emerald-500/20 text-emerald-300 border border-emerald-500/40'
        : 'bg-rose-500/20 text-rose-300 border border-rose-500/40'
    }`;
  }

  if (metaEl) {
    const revModel = escapeHtml(rev.reviewer || 'Cold Reviewer');
    const revDate = escapeHtml(rev.date || 'Unknown date');
    const range = (rev.start_sha && rev.head_sha)
      ? `${rev.start_sha}..${rev.head_sha}`
      : (rev.git_range || '');
    metaEl.innerHTML = `
      <span class="flex items-center gap-1.5 text-slate-300"><i data-lucide="bot" class="w-3.5 h-3.5 text-purple-400"></i>${revModel}</span>
      <span class="flex items-center gap-1.5 text-slate-400"><i data-lucide="calendar" class="w-3.5 h-3.5 text-slate-500"></i>${revDate}</span>
      ${range ? `<span class="flex items-center gap-1.5 text-sky-400"><i data-lucide="git-branch" class="w-3.5 h-3.5 text-sky-500"></i>Diff: ${escapeHtml(range)}</span>` : ''}
      ${rev.is_legacy ? `<span class="px-1.5 py-0.2 rounded bg-amber-500/10 text-amber-300 border border-amber-500/30 text-[10px]">Legacy Fallback (docs/QUALITY.md)</span>` : ''}
    `;
  }

  // Findings checklist
  if (chkSection && findingsList) {
    if (rev.findings && rev.findings.length > 0) {
      chkSection.classList.remove('hidden');
      const resolvedCount = rev.findings.filter((f) => f.checked).length;
      if (countBadge) countBadge.textContent = `${resolvedCount}/${rev.findings.length} resolved`;

      findingsList.innerHTML = rev.findings.map((f) => {
        const checkedAttr = f.checked ? 'checked' : '';
        const resSha = f.resolution_sha || f.sha;
        const shaBadge = resSha
          ? `<span class="font-mono text-[10px] text-sky-300 bg-sky-950/60 border border-sky-800/60 px-1.5 py-0.5 rounded ml-2 shrink-0">resolved @ ${escapeHtml(resSha)}</span>`
          : '';
        const textClass = f.checked ? 'text-slate-400 line-through' : 'text-slate-200 font-medium';
        return `
          <div class="review-finding-item">
            <input type="checkbox" ${checkedAttr} disabled class="mt-0.5 rounded border-purple-500/50 text-purple-500 bg-slate-900 cursor-not-allowed">
            <span class="flex-1 ${textClass}">${formatRichText(f.text)}</span>
            ${shaBadge}
          </div>
        `;
      }).join('');
    } else {
      chkSection.classList.add('hidden');
    }
  }

  // Body content
  if (bodyEl) {
    const reportContent = rev.content || rev.body || '';
    if (reportContent) {
      bodyEl.innerHTML = renderMarkdown(reportContent);
    } else {
      bodyEl.innerHTML = `<p class="text-slate-400 italic">No report body available.</p>`;
    }
  }

  if (footerInfo) {
    footerInfo.textContent = rev.file_path || (rev.is_legacy ? 'docs/QUALITY.md' : 'docs/reviews/');
  }

  if (window.lucide) lucide.createIcons();
}

function closeReviewModal() {
  const modal = document.getElementById('reviewModal');
  if (modal) modal.classList.add('hidden');
}

// -----------------------------------------------------------------------------
// Global Event Delegation (Safe from DOM attribute script injection)
// -----------------------------------------------------------------------------
document.addEventListener('click', (e) => {
  const trigger = e.target.closest('[data-action]');
  if (!trigger) return;
  const action = trigger.getAttribute('data-action');
  if (action === 'open-sprint-doc') {
    const sprint = trigger.getAttribute('data-sprint');
    if (sprint) openSprintDoc(sprint);
  } else if (action === 'filter-board-by-sprint') {
    const sprint = trigger.getAttribute('data-sprint');
    if (sprint) filterBoardBySprint(sprint);
  } else if (action === 'validate-card') {
    const cardId = trigger.getAttribute('data-card-id');
    if (cardId) validateCard(cardId);
  } else if (action === 'open-closeout') {
    const cardId = trigger.getAttribute('data-card-id');
    if (cardId) openCloseoutById(cardId);
  } else if (action === 'open-card-modal') {
    const cardId = trigger.getAttribute('data-card-id');
    if (cardId) openCardModalById(cardId);
  }
});

document.addEventListener('change', (e) => {
  const input = e.target.closest('input[data-action]');
  if (!input) return;
  const action = input.getAttribute('data-action');
  if (action === 'toggle-dod') {
    const cardId = input.getAttribute('data-card-id');
    const idx = parseInt(input.getAttribute('data-idx'), 10);
    if (cardId !== null && !isNaN(idx)) {
      toggleDod(cardId, idx, input.checked);
    }
  } else if (action === 'toggle-qa') {
    const cardId = input.getAttribute('data-card-id');
    const idx = parseInt(input.getAttribute('data-idx'), 10);
    if (cardId !== null && !isNaN(idx)) {
      toggleQa(cardId, idx, input.checked);
    }
  } else if (action === 'toggle-sprint-dod') {
    const sprintId = input.getAttribute('data-sprint-id');
    const idx = parseInt(input.getAttribute('data-idx'), 10);
    if (sprintId !== null && !isNaN(idx)) {
      toggleSprintDodItem(sprintId, idx, input.checked);
    }
  }
});



// -----------------------------------------------------------------------------
// Story label helper (server sends "Sprint X"; UI says "Story X")
// -----------------------------------------------------------------------------
function storyLabel(str) {
  return typeof str === 'string' ? str.replace(/^Sprint(\s|$)/, 'Story$1') : str;
}

// -----------------------------------------------------------------------------
// Projects: switcher, pills, configure modal
// -----------------------------------------------------------------------------
function projectLabel(p) {
  return p ? (p.prefix ? `${p.name} (${p.prefix})` : p.name) : '';
}

async function fetchProjects() {
  try {
    const res = await fetch('/api/projects');
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    const data = await res.json();
    state.projects = data.projects || [];
    state.currentProject = data.current || null;
    state.activeProjectSlug = (data.current && data.current.slug) || data.active_project || null;
    state.projectsError = null;
  } catch (err) {
    console.error('Failed to load projects:', err);
    state.projectsError = err.message || 'unreachable';
  }
  renderProjectUi();
}

function projectChoices() {
  // `current` may be an unregistered ad-hoc project; still offer it.
  const list = [...state.projects];
  const cur = state.currentProject;
  if (cur && !list.some((p) => p.slug === cur.slug)) list.unshift(cur);
  return list;
}

function renderProjectHeader() {
  const nameEl = document.getElementById('headerProjectName');
  const pillEl = document.getElementById('activeProjectPill');
  const cur = state.currentProject;
  if (nameEl) {
    nameEl.textContent = cur ? projectLabel(cur) : (state.projectsError ? 'Projects unavailable' : 'No project');
    nameEl.title = cur ? `${cur.name} — ${cur.code_path || ''}` : '';
  }
  if (pillEl) {
    pillEl.textContent = cur ? (cur.prefix || cur.name) : 'No project';
  }
}

function externalMarkerHtml(p) {
  return p.docs_mode === 'external'
    ? '<span class="shrink-0 text-[9px] px-1 py-0.5 rounded bg-sky-500/10 text-sky-300 border border-sky-500/30 font-semibold uppercase tracking-wide" title="Docs stored in an external folder">external docs</span>'
    : '';
}

function renderProjectUi() {
  renderProjectHeader();
  const choices = projectChoices();

  const menu = document.getElementById('projectMenuList');
  if (menu) {
    if (choices.length === 0) {
      menu.innerHTML = `<div class="px-3 py-2 text-xs text-slate-500">${
        state.projectsError ? 'Could not load projects.' : 'No projects yet. Configure one to get started.'
      }</div>`;
    } else {
      menu.innerHTML = choices
        .map((p) => {
          const active = p.slug === state.activeProjectSlug;
          return `<button role="menuitemradio" aria-checked="${active}" data-project-slug="${escapeHtml(p.slug)}" title="${escapeHtml(p.code_path || '')}"
            class="w-full flex items-center gap-2 px-3 py-2 text-left text-xs rounded-lg transition ${
              active ? 'bg-emerald-500/10 text-emerald-300' : 'text-slate-200 hover:bg-slate-800'
            }">
            <span class="w-3.5 shrink-0">${active ? '<i data-lucide="check" class="w-3.5 h-3.5"></i>' : ''}</span>
            <span class="truncate font-semibold">${escapeHtml(p.name)}</span>
            ${p.prefix ? `<span class="shrink-0 font-mono text-[10px] text-slate-400">${escapeHtml(p.prefix)}</span>` : ''}
            ${externalMarkerHtml(p)}
          </button>`;
        })
        .join('');
    }
  }

  // Pipeline pills: hidden when there is only one project (or none).
  const pills = document.getElementById('projectPills');
  if (pills) {
    if (choices.length < 2) {
      pills.classList.add('hidden');
      pills.innerHTML = '';
    } else {
      pills.classList.remove('hidden');
      pills.innerHTML =
        '<span class="text-xs font-semibold text-slate-400 uppercase tracking-wider mr-1">Project:</span>' +
        choices
          .map((p) => {
            const active = p.slug === state.activeProjectSlug;
            return `<button class="type-filter-pill max-w-[12rem] truncate ${active ? 'active' : ''}" data-project-slug="${escapeHtml(p.slug)}" aria-pressed="${active}" title="${escapeHtml(projectLabel(p))}">${escapeHtml(p.name)}${p.prefix ? ` <span class="font-mono opacity-70">${escapeHtml(p.prefix)}</span>` : ''}</button>`;
          })
          .join('') +
        '<button class="type-filter-pill" onclick="openConfigureProject()" title="Configure a project">+ Configure Project</button>';
    }
  }
  if (window.lucide) lucide.createIcons();
}

async function selectProject(slug) {
  closeProjectMenu();
  if (!slug || slug === state.activeProjectSlug) return;
  showLoadingFeedback(true);
  try {
    const res = await fetch('/api/projects/select', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ slug }),
    });
    const data = await res.json().catch(() => ({}));
    if (!res.ok || !data.success) throw new Error(data.error || `HTTP ${res.status}`);
    resetProjectScopedState();
    await fetchProjects();
    await fetchAll();
  } catch (err) {
    alert(`Could not switch project: ${err.message}`);
    showLoadingFeedback(false);
  }
}

function resetProjectScopedState() {
  state.cards = [];
  state.sprints = [];
  state.selectedQaId = null;
  state.activeSprintId = 'ACTIVE';
  state.sprintDodData = null;
  state.filters.sprint = 'ALL';
}

function isProjectMenuOpen() {
  const m = document.getElementById('projectMenu');
  return !!m && !m.classList.contains('hidden');
}
function openProjectMenu() {
  const m = document.getElementById('projectMenu');
  const b = document.getElementById('projectSwitcherBtn');
  if (!m) return;
  m.classList.remove('hidden');
  if (b) b.setAttribute('aria-expanded', 'true');
  const first = m.querySelector('button');
  if (first) first.focus();
}
function closeProjectMenu() {
  const m = document.getElementById('projectMenu');
  const b = document.getElementById('projectSwitcherBtn');
  if (m) m.classList.add('hidden');
  if (b) b.setAttribute('aria-expanded', 'false');
}

function initProjectUi() {
  const btn = document.getElementById('projectSwitcherBtn');
  const menu = document.getElementById('projectMenu');
  if (btn && menu) {
    btn.addEventListener('click', (e) => {
      e.stopPropagation();
      if (isProjectMenuOpen()) closeProjectMenu(); else openProjectMenu();
    });
    btn.addEventListener('keydown', (e) => {
      if (e.key === 'ArrowDown') { e.preventDefault(); if (!isProjectMenuOpen()) openProjectMenu(); }
    });
    menu.addEventListener('keydown', (e) => {
      const items = Array.from(menu.querySelectorAll('button'));
      const i = items.indexOf(document.activeElement);
      if (e.key === 'ArrowDown') { e.preventDefault(); items[(i + 1) % items.length].focus(); }
      else if (e.key === 'ArrowUp') { e.preventDefault(); items[(i - 1 + items.length) % items.length].focus(); }
      else if (e.key === 'Escape') { e.preventDefault(); e.stopPropagation(); closeProjectMenu(); btn.focus(); }
      else if (e.key === 'Tab') { closeProjectMenu(); }
    });
    document.addEventListener('click', (e) => {
      if (isProjectMenuOpen() && !menu.contains(e.target) && !btn.contains(e.target)) closeProjectMenu();
    });
  }
  // Menu items and pills both carry data-project-slug.
  document.addEventListener('click', (e) => {
    const el = e.target.closest('[data-project-slug]');
    if (el) selectProject(el.getAttribute('data-project-slug'));
  });

  const form = document.getElementById('configureProjectForm');
  if (form) {
    form.addEventListener('submit', submitConfigureProject);
    form.querySelectorAll('input[name="cfgDocsMode"]').forEach((r) => r.addEventListener('change', syncConfigureDocsMode));
    const prefix = document.getElementById('cfgPrefix');
    if (prefix) prefix.addEventListener('input', () => { prefix.value = prefix.value.toUpperCase().replace(/[^A-Z0-9]/g, ''); });
  }

  // The close-out artifact asks the cockpit to refresh after a QA checkbox / validate.
  window.addEventListener('message', (e) => {
    if (e.origin === location.origin && e.data && e.data.type === 'pmqa:refresh') fetchAll();
  });
}

// ---- Configure Project modal ----
function isConfigureOpen() {
  const m = document.getElementById('configureProjectModal');
  return !!m && !m.classList.contains('hidden');
}
function openConfigureProject() {
  closeProjectMenu();
  const m = document.getElementById('configureProjectModal');
  if (!m) return;
  document.getElementById('configureProjectError').classList.add('hidden');
  m.classList.remove('hidden');
  syncConfigureDocsMode();
  const n = document.getElementById('cfgName');
  if (n) n.focus();
  if (window.lucide) lucide.createIcons();
}
function closeConfigureProject() {
  const m = document.getElementById('configureProjectModal');
  if (m) m.classList.add('hidden');
}
function syncConfigureDocsMode() {
  const checked = document.querySelector('input[name="cfgDocsMode"]:checked');
  const mode = checked ? checked.value : 'in-repo';
  const dp = document.getElementById('cfgDocsPath');
  if (!dp) return;
  dp.disabled = mode !== 'external';
  dp.required = mode === 'external';
  dp.classList.toggle('opacity-50', mode !== 'external');
}
async function submitConfigureProject(e) {
  e.preventDefault();
  const errEl = document.getElementById('configureProjectError');
  const btn = document.getElementById('cfgSubmit');
  const checked = document.querySelector('input[name="cfgDocsMode"]:checked');
  const mode = checked ? checked.value : 'in-repo';
  const body = {
    name: document.getElementById('cfgName').value.trim(),
    code_path: document.getElementById('cfgCodePath').value.trim(),
    docs_mode: mode,
    activate: true,
  };
  const prefix = document.getElementById('cfgPrefix').value.trim();
  if (prefix) body.prefix = prefix;
  if (mode === 'external') body.docs_path = document.getElementById('cfgDocsPath').value.trim();
  errEl.classList.add('hidden');
  if (btn) { btn.disabled = true; btn.textContent = 'Configuring...'; }
  try {
    const res = await fetch('/api/projects/configure', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    });
    const data = await res.json().catch(() => ({}));
    if (!res.ok || !data.success) throw new Error(data.error || `HTTP ${res.status}`);
    closeConfigureProject();
    document.getElementById('configureProjectForm').reset();
    resetProjectScopedState();
    await fetchProjects();
    await fetchAll();
  } catch (err) {
    // Keep the inputs so the user can correct them.
    errEl.textContent = err.message || 'Configuration failed.';
    errEl.classList.remove('hidden');
  } finally {
    if (btn) { btn.disabled = false; btn.textContent = 'Configure'; }
  }
}

// -----------------------------------------------------------------------------
// Story Close-Out viewer
// -----------------------------------------------------------------------------
function closeoutButtonHtml(card, extraClass = '', viaAction = false) {
  if (!card || !card.closeout || !card.closeout.url) return '';
  const attrs = viaAction ? `data-action="open-closeout" data-card-id="${escapeHtml(card.mn_id)}"` : 'data-closeout="1"';
  return `<button type="button" ${attrs} class="inline-flex items-center gap-1.5 px-2.5 py-0.5 rounded bg-emerald-950/40 hover:bg-emerald-900/50 text-emerald-300 border border-emerald-500/30 hover:border-emerald-400 text-xs font-medium transition ${extraClass}" title="Open the Story Close-Out report"><i data-lucide="file-check" class="w-3.5 h-3.5"></i><span>Story Close-Out</span></button>`;
}
function bindCloseoutButton(root, card) {
  const b = root.querySelector('[data-closeout]');
  if (!b) return;
  b.addEventListener('click', (e) => {
    e.stopPropagation();
    openCloseout(card);
  });
}
function openCloseoutById(cardId) {
  const card = state.cards.find((c) => c.mn_id === cardId);
  if (card) openCloseout(card);
}
function isCloseoutOpen() {
  const m = document.getElementById('closeoutModal');
  return !!m && !m.classList.contains('hidden');
}
function openCloseout(card) {
  if (!card || !card.closeout || !card.closeout.url) return;
  const m = document.getElementById('closeoutModal');
  if (!m) return;
  document.getElementById('closeoutTitle').textContent = `Story Close-Out — ${card.mn_id}`;
  document.getElementById('closeoutNewTab').href = card.closeout.url;
  document.getElementById('closeoutFallback').classList.add('hidden');
  const frame = document.getElementById('closeoutFrame');
  frame.classList.remove('hidden');
  frame.src = card.closeout.url;
  m.classList.remove('hidden');
  document.getElementById('closeoutCloseBtn').focus();
}
function closeCloseout() {
  const m = document.getElementById('closeoutModal');
  if (m) m.classList.add('hidden');
  const frame = document.getElementById('closeoutFrame');
  if (frame) frame.src = 'about:blank';
}
function onCloseoutFrameLoad() {
  // A missing artifact still loads a same-origin error page; swap it for a friendly message.
  const frame = document.getElementById('closeoutFrame');
  if (!frame || !frame.getAttribute('src') || frame.getAttribute('src') === 'about:blank') return;
  try {
    const doc = frame.contentDocument;
    const txt = doc && doc.body ? doc.body.textContent.trim() : '';
    if (!doc || !doc.body || (txt.length < 300 && /^\{?\s*"?(404|not found|error|detail)/i.test(txt))) {
      frame.classList.add('hidden');
      document.getElementById('closeoutFallback').classList.remove('hidden');
    }
  } catch (_) { /* unreadable: leave the frame as is */ }
}

// -----------------------------------------------------------------------------
// Mobile Kanban Swipe & Column Navigation
// -----------------------------------------------------------------------------
function scrollToColumn(colId) {
  const container = document.getElementById('kanbanColumnsContainer');
  const col = document.querySelector(`.board-column[data-col="${colId}"]`);
  if (col && container) {
    col.scrollIntoView({ behavior: 'smooth', inline: 'center', block: 'nearest' });
  }
  document.querySelectorAll('.mobile-col-pill').forEach((btn) => {
    btn.classList.toggle('active', btn.getAttribute('data-col') === colId);
  });
}

function initMobileKanbanSwipe() {
  const container = document.getElementById('kanbanColumnsContainer');
  if (!container) return;
  let timeout;
  container.addEventListener(
    'scroll',
    () => {
      clearTimeout(timeout);
      timeout = setTimeout(() => {
        const cols = Array.from(container.querySelectorAll('.board-column'));
        const containerCenter = container.scrollLeft + container.clientWidth / 2;
        let closestCol = null;
        let closestDist = Infinity;
        cols.forEach((col) => {
          const colCenter = col.offsetLeft + col.offsetWidth / 2;
          const dist = Math.abs(colCenter - containerCenter);
          if (dist < closestDist) {
            closestDist = dist;
            closestCol = col;
          }
        });
        if (closestCol) {
          const colId = closestCol.getAttribute('data-col');
          document.querySelectorAll('.mobile-col-pill').forEach((btn) => {
            btn.classList.toggle('active', btn.getAttribute('data-col') === colId);
          });
        }
      }, 80);
    },
    { passive: true }
  );
}

function initKanbanDragDrop() {
  document.querySelectorAll('.board-column').forEach((col) => {
    col.addEventListener('dragover', (e) => {
      e.preventDefault();
      col.classList.add('drag-over');
    });
    col.addEventListener('dragleave', (e) => {
      if (!col.contains(e.relatedTarget)) {
        col.classList.remove('drag-over');
      }
    });
    col.addEventListener('drop', (e) => {
      e.preventDefault();
      col.classList.remove('drag-over');
      const cardId = e.dataTransfer.getData('text/plain');
      const targetCol = col.getAttribute('data-col');
      if (!cardId || !targetCol) return;
      const card = state.cards.find((c) => c.mn_id === cardId);
      if (card && card.status !== targetCol) {
        card.status = targetCol;
        renderPipeline();
      }
    });
  });
}

// -----------------------------------------------------------------------------
// Pure JavaScript QR Code Generator (Byte mode, Level L, Versions 1-6)
// -----------------------------------------------------------------------------
function generateQrSvg(text) {
  const len = text.length;
  let version = 1;
  const limits = [17, 32, 53, 78, 106, 134];
  for (let v = 1; v <= 6; v++) {
    if (len <= limits[v - 1]) {
      version = v;
      break;
    }
    version = 6;
  }

  const specs = {
    1: { totalCodewords: 26, dataCodewords: 19, ecPerBlock: 7, blocks: 1, alignPos: 0 },
    2: { totalCodewords: 44, dataCodewords: 34, ecPerBlock: 10, blocks: 1, alignPos: 18 },
    3: { totalCodewords: 70, dataCodewords: 55, ecPerBlock: 15, blocks: 1, alignPos: 22 },
    4: { totalCodewords: 100, dataCodewords: 80, ecPerBlock: 20, blocks: 1, alignPos: 26 },
    5: { totalCodewords: 134, dataCodewords: 108, ecPerBlock: 13, blocks: 2, alignPos: 30 },
    6: { totalCodewords: 172, dataCodewords: 136, ecPerBlock: 18, blocks: 2, alignPos: 34 },
  };
  const spec = specs[version];
  const { totalCodewords, dataCodewords, ecPerBlock, blocks, alignPos } = spec;

  let bitBuf = 0;
  let bitLen = 0;
  const data = [];
  function putBits(val, numBits) {
    for (let i = numBits - 1; i >= 0; i--) {
      bitBuf = (bitBuf << 1) | ((val >>> i) & 1);
      bitLen++;
      if (bitLen === 8) {
        data.push(bitBuf);
        bitBuf = 0;
        bitLen = 0;
      }
    }
  }

  putBits(4, 4); // Byte mode indicator
  putBits(len, 8); // Character count indicator
  for (let i = 0; i < len; i++) {
    putBits(text.charCodeAt(i) & 0xff, 8);
  }
  putBits(0, 4); // Terminator
  if (bitLen > 0) {
    data.push(bitBuf << (8 - bitLen));
    bitBuf = 0;
    bitLen = 0;
  }
  const padBytes = [0xec, 0x11];
  let pIdx = 0;
  while (data.length < dataCodewords) {
    data.push(padBytes[pIdx % 2]);
    pIdx++;
  }

  // Reed-Solomon Error Correction Code
  const EXP = new Uint8Array(512);
  const LOG = new Uint8Array(256);
  let x = 1;
  for (let i = 0; i < 255; i++) {
    EXP[i] = x;
    EXP[i + 255] = x;
    LOG[x] = i;
    x = (x << 1) ^ (x >= 128 ? 285 : 0);
  }
  function gfMul(a, b) {
    return a === 0 || b === 0 ? 0 : EXP[LOG[a] + LOG[b]];
  }

  let poly = [1];
  for (let i = 0; i < ecPerBlock; i++) {
    const nextP = new Array(poly.length + 1).fill(0);
    const a_i = EXP[i];
    for (let j = 0; j < poly.length; j++) {
      nextP[j] ^= poly[j];
      nextP[j + 1] ^= gfMul(poly[j], a_i);
    }
    poly = nextP;
  }

  const dataPerBlock = dataCodewords / blocks;
  const blockData = [];
  const blockEc = [];
  for (let b = 0; b < blocks; b++) {
    const bData = data.slice(b * dataPerBlock, (b + 1) * dataPerBlock);
    blockData.push(bData);

    const ecData = new Array(ecPerBlock).fill(0);
    for (let i = 0; i < bData.length; i++) {
      const factor = bData[i] ^ ecData[0];
      for (let j = 0; j < ecPerBlock - 1; j++) {
        ecData[j] = ecData[j + 1] ^ gfMul(poly[j + 1], factor);
      }
      ecData[ecPerBlock - 1] = gfMul(poly[ecPerBlock], factor);
    }
    blockEc.push(ecData);
  }

  // Interleave data codewords
  const interleavedData = [];
  for (let i = 0; i < dataPerBlock; i++) {
    for (let b = 0; b < blocks; b++) {
      interleavedData.push(blockData[b][i]);
    }
  }

  // Interleave error correction codewords
  const interleavedEc = [];
  for (let i = 0; i < ecPerBlock; i++) {
    for (let b = 0; b < blocks; b++) {
      interleavedEc.push(blockEc[b][i]);
    }
  }

  const allCodewords = interleavedData.concat(interleavedEc);
  const size = version * 4 + 17;
  const matrix = Array.from({ length: size }, () => new Int8Array(size).fill(-1));

  function setModule(r, c, val) {
    if (r >= 0 && r < size && c >= 0 && c < size) {
      matrix[r][c] = val ? 1 : 0;
    }
  }

  function placeFinder(row, col) {
    for (let r = -1; r <= 7; r++) {
      for (let c = -1; c <= 7; c++) {
        const nr = row + r;
        const nc = col + c;
        if (nr < 0 || nr >= size || nc < 0 || nc >= size) continue;
        if (r >= 0 && r <= 6 && c >= 0 && c <= 6) {
          if (r === 0 || r === 6 || c === 0 || c === 6 || (r >= 2 && r <= 4 && c >= 2 && c <= 4)) {
            setModule(nr, nc, 1);
          } else {
            setModule(nr, nc, 0);
          }
        } else {
          setModule(nr, nc, 0);
        }
      }
    }
  }
  placeFinder(0, 0);
  placeFinder(size - 7, 0);
  placeFinder(0, size - 7);

  for (let i = 8; i < size - 8; i++) {
    if (matrix[6][i] === -1) setModule(6, i, i % 2 === 0);
    if (matrix[i][6] === -1) setModule(i, 6, i % 2 === 0);
  }

  if (alignPos > 0) {
    for (let r = -2; r <= 2; r++) {
      for (let c = -2; c <= 2; c++) {
        if (matrix[alignPos + r][alignPos + c] === -1) {
          const isBlack = Math.abs(r) === 2 || Math.abs(c) === 2 || (r === 0 && c === 0);
          setModule(alignPos + r, alignPos + c, isBlack ? 1 : 0);
        }
      }
    }
  }

  setModule(4 * version + 9, 8, 1);

  // ISO/IEC 18004 standard format bits: Level L, Mask 0 ((r + c) % 2 === 0)
  const formatBits = 0x77c4;
  for (let i = 0; i < 15; i++) {
    const bit = (formatBits >>> (14 - i)) & 1;
    if (i < 6) setModule(8, i, bit);
    else if (i < 8) setModule(8, i + 1, bit);
    else if (i === 8) setModule(7, 8, bit);
    else setModule(14 - i, 8, bit);

    if (i < 8) setModule(size - 1 - i, 8, bit);
    else setModule(8, size - 15 + i, bit);
  }

  let bitIdx = 0;
  const totalBits = allCodewords.length * 8;
  for (let right = size - 1; right > 0; right -= 2) {
    if (right === 6) right--;
    const upward = ((size - 1 - right) / 2) % 2 === 0;
    for (let vert = 0; vert < size; vert++) {
      const r = upward ? size - 1 - vert : vert;
      for (let c = right; c >= right - 1; c--) {
        if (matrix[r][c] === -1) {
          let b = 0;
          if (bitIdx < totalBits) {
            const byte = allCodewords[Math.floor(bitIdx / 8)];
            b = (byte >>> (7 - (bitIdx % 8))) & 1;
            bitIdx++;
          }
          if ((r + c) % 2 === 0) b ^= 1;
          matrix[r][c] = b;
        }
      }
    }
  }

  const pad = 3;
  const fullSize = size + pad * 2;
  let rects = '';
  for (let r = 0; r < size; r++) {
    for (let c = 0; c < size; c++) {
      if (matrix[r][c] === 1) {
        rects += `<rect x="${c + pad}" y="${r + pad}" width="1" height="1" fill="#0f172a"/>`;
      }
    }
  }
  return `<svg viewBox="0 0 ${fullSize} ${fullSize}" xmlns="http://www.w3.org/2000/svg" class="w-full h-full shape-rendering-crispEdges"><rect width="100%" height="100%" fill="white"/>${rects}</svg>`;
}

// -----------------------------------------------------------------------------
// Mobile Connect Modal Controller
// -----------------------------------------------------------------------------
async function openMobileConnectModal() {
  const modal = document.getElementById('mobileConnectModal');
  if (!modal) return;
  modal.classList.remove('hidden');

  try {
    const res = await fetch('/api/server-info');
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    const info = await res.json();

    const storedToken = sessionStorage.getItem('cockpit_token');
    const token = info.token || storedToken || '';
    let mobileUrl = '';
    if (info.tunnel_url) {
      mobileUrl = info.tunnel_url + (token ? `?token=${encodeURIComponent(token)}` : '');
    } else {
      const port = info.port || 3333;
      const host = info.lan_ip || window.location.hostname;
      mobileUrl = `http://${host}:${port}` + (token ? `?token=${encodeURIComponent(token)}` : '');
    }

    const input = document.getElementById('mobileConnectUrlInput');
    if (input) input.value = mobileUrl;

    const qrContainer = document.getElementById('mobileQrGraphic');
    if (qrContainer) {
      qrContainer.innerHTML = '';
      if (typeof QRCode !== 'undefined') {
        new QRCode(qrContainer, {
          text: mobileUrl,
          width: 200,
          height: 200,
          colorDark: '#0f172a',
          colorLight: '#ffffff',
          correctLevel: QRCode.CorrectLevel.M,
        });
      } else {
        qrContainer.innerHTML = generateQrSvg(mobileUrl);
      }
    }

    const netBadge = document.getElementById('mobileNetworkModeBadge');
    if (netBadge) {
      netBadge.textContent = info.tunnel_url ? 'Cloudflare Tunnel (Public)' : 'Local Wi-Fi (LAN)';
      netBadge.className = `font-semibold ${info.tunnel_url ? 'text-amber-400' : 'text-emerald-400'}`;
    }

    const kaBadge = document.getElementById('mobileKeepAliveBadge');
    if (kaBadge) {
      kaBadge.textContent = info.keep_alive ? 'Active (Sleep Prevented)' : 'Standard';
      kaBadge.className = `font-semibold ${info.keep_alive ? 'text-cyan-400' : 'text-slate-400'}`;
    }

    const tokBadge = document.getElementById('mobileTokenBadge');
    if (tokBadge) {
      tokBadge.textContent = info.token_required ? 'Session Token Active' : 'Unauthenticated (Local)';
      tokBadge.className = `font-semibold ${info.token_required ? 'text-emerald-400' : 'text-slate-400'}`;
    }
  } catch (err) {
    console.error('Failed to load server info for mobile connect:', err);
    const input = document.getElementById('mobileConnectUrlInput');
    if (input) input.value = window.location.href;
    const qrContainer = document.getElementById('mobileQrGraphic');
    if (qrContainer) qrContainer.innerHTML = generateQrSvg(window.location.href);
  }

  if (window.lucide) lucide.createIcons();
}

function closeMobileConnectModal() {
  const modal = document.getElementById('mobileConnectModal');
  if (modal) modal.classList.add('hidden');
}

function copyMobileUrl() {
  const input = document.getElementById('mobileConnectUrlInput');
  if (!input || !input.value) return;
  copyToClipboard(input.value, document.getElementById('copyMobileUrlBtn'));
}

