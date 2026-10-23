// app.js - Controller for PM & QA Cockpit

// Session token handling for remote/tunnel connections
(function initTokenAuth() {
  function getStoredToken() {
    try {
      return sessionStorage.getItem('cockpit_token') || localStorage.getItem('cockpit_token') || '';
    } catch (_) {
      return '';
    }
  }

  function setStoredToken(t) {
    if (!t) return;
    try { sessionStorage.setItem('cockpit_token', t); } catch (_) {}
    try { localStorage.setItem('cockpit_token', t); } catch (_) {}
  }

  // Also check if document.cookie contains token
  try {
    const match = document.cookie.match(/(?:^|;\s*)cockpit_token=([^;]+)/);
    if (match && match[1]) {
      setStoredToken(decodeURIComponent(match[1]));
    }
  } catch (_) {}

  try {
    const params = new URLSearchParams(window.location.search);
    const token = params.get('token');
    if (token) {
      setStoredToken(token);
      params.delete('token');
      const newQuery = params.toString() ? '?' + params.toString() : '';
      const cleanUrl = window.location.pathname + newQuery + window.location.hash;
      window.history.replaceState({}, document.title, cleanUrl);
    }
  } catch (_) {}

  // Authenticate all fetch calls when session token is stored
  const origFetch = window.fetch;
  window.fetch = function(url, options) {
    const token = getStoredToken();
    if (token) {
      // 1. Append token to query parameter for API endpoints if not present
      if (typeof url === 'string' && (url.startsWith('/api/') || url.includes('/api/'))) {
        try {
          const u = new URL(url, window.location.origin);
          if (!u.searchParams.has('token')) {
            u.searchParams.set('token', token);
            url = u.pathname + u.search + u.hash;
          }
        } catch (_) {}
      }
      // 2. Also send X-Cockpit-Token and Authorization headers
      options = options || {};
      const headers = new Headers(options.headers || {});
      if (!headers.has('X-Cockpit-Token')) {
        headers.set('X-Cockpit-Token', token);
      }
      if (!headers.has('Authorization')) {
        headers.set('Authorization', 'Bearer ' + token);
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
  serverInfo: null,
  serverInfoTime: 0,
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
    tags: Object.create(null),
  },
};


// -----------------------------------------------------------------------------
// Visual Loading & Feedback Helpers
// -----------------------------------------------------------------------------
function showToast(message, type = 'info', duration = 3500) {
  const container = document.getElementById('toastContainer');
  if (!container) {
    console.log(`[Toast ${type}]`, message);
    return;
  }
  const toast = document.createElement('div');
  toast.className = 'toast-banner flex items-center gap-3 px-4 py-2.5 rounded-2xl pointer-events-auto cursor-pointer transition-all duration-300 ease-out transform -translate-y-4 opacity-0 max-w-md w-full shadow-lg';

  let iconHtml = '<i class="ph-fill ph-info text-sky-500 text-lg shrink-0"></i>';
  if (type === 'success') {
    iconHtml = '<i class="ph-fill ph-check-circle text-emerald-500 text-lg shrink-0"></i>';
  } else if (type === 'error' || type === 'danger') {
    iconHtml = '<i class="ph-fill ph-x-circle text-rose-500 text-lg shrink-0"></i>';
  } else if (type === 'warning') {
    iconHtml = '<i class="ph-fill ph-warning text-amber-500 text-lg shrink-0"></i>';
  }

  toast.innerHTML = `
    ${iconHtml}
    <div class="text-xs sm:text-sm font-medium text-[var(--text-primary)] leading-tight select-none flex-1 break-words">${escapeHtml(message)}</div>
    <button class="text-[var(--text-tertiary)] hover:text-[var(--text-primary)] shrink-0 ml-1 transition-colors p-1" aria-label="Dismiss toast">
      <i class="ph ph-x text-xs"></i>
    </button>
  `;

  container.appendChild(toast);

  // Trigger enter animation on next frame
  requestAnimationFrame(() => {
    toast.classList.remove('-translate-y-4', 'opacity-0');
    toast.classList.add('translate-y-0', 'opacity-100');
  });

  let dismissTimer = null;
  const dismiss = () => {
    if (dismissTimer) clearTimeout(dismissTimer);
    toast.classList.remove('translate-y-0', 'opacity-100');
    toast.classList.add('-translate-y-2', 'opacity-0');
    setTimeout(() => {
      if (toast.parentElement) toast.parentElement.removeChild(toast);
    }, 300);
  };

  const startTimer = () => {
    if (duration > 0) {
      dismissTimer = setTimeout(dismiss, duration);
    }
  };

  startTimer();

  toast.addEventListener('mouseenter', () => {
    if (dismissTimer) clearTimeout(dismissTimer);
  });
  toast.addEventListener('mouseleave', () => {
    startTimer();
  });
  toast.addEventListener('click', dismiss);
}

// Cross-window PostMessage Toast Bridge (from embedded close-out artifacts)
window.addEventListener('message', (e) => {
  if (e.data && e.data.type === 'pmqa:toast' && e.data.message) {
    showToast(e.data.message, e.data.level || 'info');
  }
});

// =============================================================================
// Web Audio Synth Engine & Sound Controls (Story 8)
// =============================================================================
let audioContext = null;
let soundEnabled = true;

function getAudioContext() {
  if (!audioContext) {
    const AudioContextClass = window.AudioContext || window.webkitAudioContext;
    if (AudioContextClass) {
      audioContext = new AudioContextClass();
    }
  }
  if (audioContext && audioContext.state === 'suspended') {
    audioContext.resume().catch(() => {});
  }
  return audioContext;
}

function unlockAudioContextOnGesture() {
  const unlock = () => {
    const ctx = getAudioContext();
    if (ctx && ctx.state === 'running') {
      ['click', 'keydown', 'touchstart'].forEach((evt) => {
        window.removeEventListener(evt, unlock);
      });
    }
  };
  ['click', 'keydown', 'touchstart'].forEach((evt) => {
    window.addEventListener(evt, unlock, { passive: true });
  });
}

function playNote(ctx, freq, startTime, duration, maxGain = 0.22) {
  try {
    const osc = ctx.createOscillator();
    const gain = ctx.createGain();
    osc.type = 'sine';
    osc.frequency.setValueAtTime(freq, startTime);

    gain.gain.setValueAtTime(0.0001, startTime);
    gain.gain.linearRampToValueAtTime(maxGain, startTime + 0.012);
    gain.gain.exponentialRampToValueAtTime(0.0001, startTime + duration);

    osc.connect(gain);
    gain.connect(ctx.destination);

    // Subtle second harmonic for acoustic chime texture
    const harm = ctx.createOscillator();
    const harmGain = ctx.createGain();
    harm.type = 'sine';
    harm.frequency.setValueAtTime(freq * 2, startTime);
    harmGain.gain.setValueAtTime(0.0001, startTime);
    harmGain.gain.linearRampToValueAtTime(maxGain * 0.18, startTime + 0.012);
    harmGain.gain.exponentialRampToValueAtTime(0.0001, startTime + duration * 0.7);

    harm.connect(harmGain);
    harmGain.connect(ctx.destination);

    osc.start(startTime);
    harm.start(startTime);
    osc.stop(startTime + duration);
    harm.stop(startTime + duration);
  } catch (_) {}
}

function playChime(type = 'qa') {
  if (!soundEnabled) return;
  const ctx = getAudioContext();
  if (!ctx) return;

  try {
    if (ctx.state === 'suspended') {
      ctx.resume().catch(() => {});
    }
    const t = ctx.currentTime;
    if (type === 'qa') {
      // Ascending 2-tone chime: D5 (587.33 Hz) -> A5 (880.00 Hz)
      playNote(ctx, 587.33, t, 0.25, 0.2);
      playNote(ctx, 880.00, t + 0.14, 0.6, 0.25);
    } else if (type === 'completed') {
      // Celebratory major triad chord with top octave: C5, E5, G5, C6
      playNote(ctx, 523.25, t, 1.2, 0.16);
      playNote(ctx, 659.25, t + 0.04, 1.2, 0.16);
      playNote(ctx, 783.99, t + 0.08, 1.4, 0.16);
      playNote(ctx, 1046.50, t + 0.12, 1.6, 0.22);
    } else if (type === 'test') {
      playNote(ctx, 659.25, t, 0.18, 0.18);
      playNote(ctx, 880.00, t + 0.12, 0.35, 0.22);
    }
  } catch (err) {
    console.warn('Audio chime playback failed:', err);
  }
}

function updateSoundToggleUi() {
  const icon = document.getElementById('soundToggleIcon');
  const label = document.getElementById('soundToggleLabel');
  const btn = document.getElementById('soundToggleBtn');
  if (!icon || !btn) return;

  if (soundEnabled) {
    icon.className = 'ph ph-speaker-high text-xs text-emerald-600 dark:text-emerald-400';
    if (label) label.textContent = 'Sound';
    btn.title = 'Sound Alerts Active (Click to mute)';
  } else {
    icon.className = 'ph ph-speaker-slash text-xs text-[var(--text-tertiary)]';
    if (label) label.textContent = 'Muted';
    btn.title = 'Sound Alerts Muted (Click to unmute)';
  }
}

async function toggleSoundMute() {
  soundEnabled = !soundEnabled;
  localStorage.setItem('pmqa_sound_enabled', soundEnabled ? 'true' : 'false');
  updateSoundToggleUi();
  if (soundEnabled) {
    playChime('test');
    showToast('Completion sound alerts enabled.', 'info');
  } else {
    showToast('Completion sound alerts muted.', 'info');
  }

  try {
    const slug = state.activeProjectSlug || (state.currentProject && state.currentProject.slug);
    fetch('/api/notifications/config', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ slug, sound_enabled: soundEnabled }),
    }).catch(() => {});
  } catch (_) {}
}

async function initSoundControl() {
  unlockAudioContextOnGesture();
  const stored = localStorage.getItem('pmqa_sound_enabled');
  if (stored !== null) {
    soundEnabled = stored === 'true';
  } else {
    soundEnabled = true;
  }
  updateSoundToggleUi();

  try {
    const res = await fetch('/api/notifications/config').then((r) => r.json());
    if (res && res.success && res.config) {
      if (stored === null && typeof res.config.sound_enabled === 'boolean') {
        soundEnabled = res.config.sound_enabled;
        localStorage.setItem('pmqa_sound_enabled', soundEnabled ? 'true' : 'false');
        updateSoundToggleUi();
      }
    }
  } catch (_) {}
}

// =============================================================================
// Mobile Haptics & Browser Web Notifications (Story 8)
// =============================================================================
function triggerHaptic(type = 'default') {
  if (!navigator.vibrate) return;
  try {
    if (type === 'qa') {
      navigator.vibrate([100, 60, 140]);
    } else if (type === 'completed') {
      navigator.vibrate([150, 80, 200, 80, 250]);
    } else {
      navigator.vibrate(100);
    }
  } catch (_) {}
}

function updateNotificationPermissionUi() {
  const badge = document.getElementById('browserNotificationStatusBadge');
  const btn = document.getElementById('enableNotificationsBtn');
  if (!badge) return;

  if (!('Notification' in window)) {
    badge.textContent = 'Unsupported';
    badge.className = 'px-2 py-0.5 rounded text-[10px] font-medium bg-[var(--bg-surface)] text-[var(--text-tertiary)] border border-[var(--border-subtle)]';
    if (btn) btn.disabled = true;
    return;
  }

  const perm = Notification.permission;
  if (perm === 'granted') {
    badge.textContent = 'Granted';
    badge.className = 'px-2 py-0.5 rounded text-[10px] font-medium bg-emerald-500/15 text-emerald-600 dark:text-emerald-400 border border-emerald-500/30';
    if (btn) {
      btn.textContent = 'Notifications Active';
      btn.classList.add('opacity-75');
      btn.disabled = true;
    }
  } else if (perm === 'denied') {
    badge.textContent = 'Blocked';
    badge.className = 'px-2 py-0.5 rounded text-[10px] font-medium bg-rose-500/15 text-rose-600 dark:text-rose-400 border border-rose-500/30';
    if (btn) {
      btn.textContent = 'Blocked in Browser';
      btn.disabled = true;
    }
  } else {
    badge.textContent = 'Not Enabled';
    badge.className = 'px-2 py-0.5 rounded text-[10px] font-medium bg-amber-500/15 text-amber-600 dark:text-amber-400 border border-amber-500/30';
    if (btn) {
      btn.textContent = 'Enable Notifications';
      btn.disabled = false;
    }
  }
}

async function requestNotificationPermission() {
  if (!('Notification' in window)) {
    showToast('Web Notifications are not supported in this browser.', 'warning');
    return 'unsupported';
  }
  try {
    const perm = await Notification.requestPermission();
    updateNotificationPermissionUi();
    if (perm === 'granted') {
      showToast('Browser notifications enabled!', 'success');
      showWebNotification('Notifications Enabled', {
        body: 'You will receive desktop and mobile alerts when stories are ready for QA or completed.',
      });
    } else if (perm === 'denied') {
      showToast('Notifications blocked in browser settings.', 'warning');
    }
    return perm;
  } catch (err) {
    console.warn('Failed to request notification permission:', err);
    return 'error';
  }
}

function showWebNotification(title, options = {}) {
  if (!('Notification' in window) || Notification.permission !== 'granted') return;
  try {
    const notif = new Notification(title, {
      icon: '/favicon.ico',
      badge: '/favicon.ico',
      ...options,
    });
    notif.onclick = function (event) {
      event.preventDefault();
      window.focus();
      if (options.data && options.data.url) {
        window.location.href = options.data.url;
      }
    };
  } catch (err) {
    console.warn('Web notification failed to display:', err);
  }
}

// =============================================================================
// Mobile Connect Modal Notification Panel Helpers (Story 8)
// =============================================================================
async function loadNotificationModalConfig() {
  updateNotificationPermissionUi();
  const topicInput = document.getElementById('ntfyTopicInput');
  const subLink = document.getElementById('ntfySubscribeLink');
  const curSlug = state.activeProjectSlug || (state.currentProject && state.currentProject.slug) || 'project';
  const defaultTopic = `pmqa-${curSlug}`;

  if (topicInput) {
    if (!topicInput.value) topicInput.value = defaultTopic;
  }
  if (subLink) {
    const topic = (topicInput && topicInput.value) ? topicInput.value : defaultTopic;
    subLink.href = `https://ntfy.sh/${encodeURIComponent(topic)}`;
  }

  try {
    const res = await fetch(`/api/notifications/config?slug=${encodeURIComponent(curSlug)}`).then((r) => r.json());
    if (res && res.success && res.config) {
      if (topicInput && res.config.ntfy_topic) {
        topicInput.value = res.config.ntfy_topic;
      }
      if (subLink) {
        const topic = (topicInput && topicInput.value) ? topicInput.value : defaultTopic;
        subLink.href = `https://ntfy.sh/${encodeURIComponent(topic)}`;
      }
    }
  } catch (_) {}
}

async function saveNotificationConfig() {
  const topicInput = document.getElementById('ntfyTopicInput');
  const subLink = document.getElementById('ntfySubscribeLink');
  const curSlug = state.activeProjectSlug || (state.currentProject && state.currentProject.slug) || 'project';
  const topic = topicInput ? topicInput.value.trim().replace(/^\/+/, '') : `pmqa-${curSlug}`;

  if (!topic) {
    showToast('Please enter a valid push notification topic.', 'warning');
    return;
  }

  if (subLink) {
    subLink.href = `https://ntfy.sh/${encodeURIComponent(topic)}`;
  }

  try {
    const res = await fetch('/api/notifications/config', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ slug: curSlug, ntfy_topic: topic, push_enabled: true }),
    }).then((r) => r.json());

    if (res && res.success) {
      showToast(`Cellular push topic saved: ${topic}`, 'success');
    } else {
      showToast(`Failed to save config: ${res.error || 'Server error'}`, 'error');
    }
  } catch (err) {
    console.error('Failed to save notification config:', err);
    showToast('Failed to save notification configuration.', 'error');
  }
}

async function sendTestAlert() {
  const btn = document.getElementById('testAlertBtn');
  const origHtml = btn ? btn.innerHTML : '';
  if (btn) {
    btn.disabled = true;
    btn.innerHTML = '<i class="ph ph-spinner animate-spin text-xs"></i><span>Testing...</span>';
  }

  playChime('test');
  triggerHaptic('qa');
  showToast('Testing audio chime & remote cellular push...', 'info');

  showWebNotification('PM & QA Board — Test Alert', {
    body: '🔔 Audio, haptic, and mobile notification test succeeded!',
  });

  const topicInput = document.getElementById('ntfyTopicInput');
  const curSlug = state.activeProjectSlug || (state.currentProject && state.currentProject.slug) || 'project';
  const topic = topicInput && topicInput.value.trim() ? topicInput.value.trim() : `pmqa-${curSlug}`;

  try {
    const res = await fetch('/api/notifications/test', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ slug: curSlug, topic }),
    }).then((r) => r.json());

    if (res && res.success) {
      showToast(`Remote push delivered to ntfy.sh/${topic}!`, 'success');
    } else {
      showToast(`Push warning: ${res.error || 'Check network connection'}`, 'warning');
    }
  } catch (err) {
    console.warn('Test push request error:', err);
    showToast('Test push alert dispatched (local sound & haptics active).', 'info');
  } finally {
    if (btn) {
      btn.disabled = false;
      btn.innerHTML = origHtml;
    }
  }
}

// =============================================================================
// Client-side Card Transition Watcher & Deep Link Navigation (Story 8)
// =============================================================================
let previousCardStatuses = null;
let cardWatcherInitialized = false;

function detectCardTransitions(cards) {
  if (!Array.isArray(cards)) return;

  const currentMap = new Map();
  cards.forEach((c) => {
    const id = c.mn_id || c.id;
    if (id) currentMap.set(id, c.status);
  });

  // Skip notifications on initial page load
  if (!cardWatcherInitialized || previousCardStatuses === null) {
    previousCardStatuses = currentMap;
    cardWatcherInitialized = true;
    return;
  }

  cards.forEach((c) => {
    const id = c.mn_id || c.id;
    if (!id) return;
    const oldStatus = previousCardStatuses.get(id);
    const newStatus = c.status;

    if (oldStatus && oldStatus !== newStatus) {
      const cardTitle = c.name || c.title || id;
      if (newStatus === 'qa') {
        playChime('qa');
        triggerHaptic('qa');
        showToast(`Ready for QA: ${id} - ${cardTitle}`, 'info');
        showWebNotification(`Ready for QA: ${id}`, {
          body: `${cardTitle} has finished building and is awaiting QA verification.`,
          data: { url: `/?card=${encodeURIComponent(id)}#qa` },
        });
      } else if (newStatus === 'completed') {
        playChime('completed');
        triggerHaptic('completed');
        showToast(`Story Completed: ${id}`, 'success');
        showWebNotification(`Story Completed: ${id}`, {
          body: `${cardTitle} has been validated and completed.`,
          data: { url: `/?card=${encodeURIComponent(id)}#pipeline` },
        });
      }
    }
  });

  previousCardStatuses = currentMap;
}

function handleUrlDeepLink() {
  const urlParams = new URLSearchParams(window.location.search);
  const targetCardId = urlParams.get('card');
  if (!targetCardId) return;

  const cleanTarget = targetCardId.trim().toLowerCase();
  const card = (state.cards || []).find((c) => {
    const id = (c.mn_id || c.id || '').toLowerCase();
    const story = String(c.story || c.sprint || '').toLowerCase();
    return id === cleanTarget || story === cleanTarget;
  });

  if (!card) return;

  const cardId = card.mn_id || card.id;
  if (card.status === 'qa') {
    switchTab('qa');
    openQaDrawerById(cardId);
    selectQaCard(cardId, { forceScrollTop: true });
  } else {
    switchTab('pipeline');
    openCardModalById(cardId);
  }
}

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

function renderLoadingSkeletons(force = false) {
  const activeTab = state.activeTab || 'pipeline';

  if (activeTab === 'analytics') {
    renderAnalyticsSkeleton();
  } else if (activeTab === 'pipeline') {
    const cols = ['backlog', 'sprint-backlog', 'in-progress', 'qa', 'completed'];
    cols.forEach((colId) => {
      const el = document.getElementById(`cards-${colId}`);
      if (el && (force || state.cards.length === 0)) {
        el.innerHTML = `
          <div class="board-skeleton-card p-3.5 rounded-xl bg-[var(--bg-surface-secondary)] border border-[var(--border-subtle)] animate-pulse space-y-2.5 my-2">
            <div class="flex justify-between items-center">
              <div class="h-3 bg-[var(--bg-surface-tertiary)] rounded w-16"></div>
              <div class="h-3 bg-[var(--bg-surface-tertiary)] rounded w-12"></div>
            </div>
            <div class="h-4 bg-[var(--bg-surface-tertiary)] rounded w-4/5"></div>
            <div class="h-2 bg-[var(--bg-surface-tertiary)] rounded w-full"></div>
            <div class="h-2 bg-[var(--bg-surface-tertiary)] rounded w-1/3 pt-1"></div>
          </div>
          <div class="board-skeleton-card p-3.5 rounded-xl bg-[var(--bg-surface-secondary)] border border-[var(--border-subtle)] animate-pulse space-y-2.5 my-2 opacity-50">
            <div class="flex justify-between items-center">
              <div class="h-3 bg-[var(--bg-surface-tertiary)] rounded w-14"></div>
              <div class="h-3 bg-[var(--bg-surface-tertiary)] rounded w-10"></div>
            </div>
            <div class="h-4 bg-[var(--bg-surface-tertiary)] rounded w-3/5"></div>
          </div>
        `;
      }
    });
  } else if (activeTab === 'qa') {
    const qaGrid = document.getElementById('qaCardsGrid');
    if (qaGrid) {
      qaGrid.innerHTML = `
        <div class="p-4 rounded-xl bg-[var(--bg-surface-secondary)] border border-[var(--border-subtle)] animate-pulse space-y-3 shadow-sm">
          <div class="flex justify-between items-center">
            <div class="h-3 bg-[var(--bg-surface-tertiary)] rounded w-16"></div>
            <div class="h-3 bg-[var(--bg-surface-tertiary)] rounded w-14"></div>
          </div>
          <div class="h-4 bg-[var(--bg-surface-tertiary)] rounded w-3/4"></div>
          <div class="h-3 bg-[var(--bg-surface-tertiary)] rounded w-1/2"></div>
        </div>
        <div class="p-4 rounded-xl bg-[var(--bg-surface-secondary)] border border-[var(--border-subtle)] animate-pulse space-y-3 shadow-sm">
          <div class="flex justify-between items-center">
            <div class="h-3 bg-[var(--bg-surface-tertiary)] rounded w-14"></div>
            <div class="h-3 bg-[var(--bg-surface-tertiary)] rounded w-10"></div>
          </div>
          <div class="h-4 bg-[var(--bg-surface-tertiary)] rounded w-2/3"></div>
          <div class="h-3 bg-[var(--bg-surface-tertiary)] rounded w-1/3"></div>
        </div>
        <div class="p-4 rounded-xl bg-[var(--bg-surface-secondary)] border border-[var(--border-subtle)] animate-pulse space-y-3 shadow-sm hidden md:block">
          <div class="flex justify-between items-center">
            <div class="h-3 bg-[var(--bg-surface-tertiary)] rounded w-16"></div>
            <div class="h-3 bg-[var(--bg-surface-tertiary)] rounded w-12"></div>
          </div>
          <div class="h-4 bg-[var(--bg-surface-tertiary)] rounded w-4/5"></div>
          <div class="h-3 bg-[var(--bg-surface-tertiary)] rounded w-2/5"></div>
        </div>
      `;
    }
    const qaList = document.getElementById('qaListContainer');
    if (qaList) {
      qaList.innerHTML = `
        <div class="p-3.5 rounded-xl bg-[var(--bg-surface-secondary)] border border-[var(--border-subtle)] animate-pulse space-y-2 my-1">
          <div class="flex justify-between items-center">
            <div class="h-3 bg-[var(--bg-surface-tertiary)] rounded w-16"></div>
            <div class="h-3 bg-[var(--bg-surface-tertiary)] rounded w-12"></div>
          </div>
          <div class="h-4 bg-[var(--bg-surface-tertiary)] rounded w-3/4"></div>
          <div class="h-2.5 bg-[var(--bg-surface-tertiary)] rounded w-1/2"></div>
        </div>
        <div class="p-3.5 rounded-xl bg-[var(--bg-surface-secondary)] border border-[var(--border-subtle)] animate-pulse space-y-2 my-1 opacity-60">
          <div class="flex justify-between items-center">
            <div class="h-3 bg-[var(--bg-surface-tertiary)] rounded w-14"></div>
            <div class="h-3 bg-[var(--bg-surface-tertiary)] rounded w-10"></div>
          </div>
          <div class="h-4 bg-[var(--bg-surface-tertiary)] rounded w-2/3"></div>
        </div>
      `;
    }
    const qaDetail = document.getElementById('qaDetailContainer');
    if (qaDetail) {
      qaDetail.innerHTML = `
        <div class="h-full flex flex-col items-center justify-center text-[var(--text-tertiary)]">
          <i class="ph ph-stack text-4xl mb-3 text-[var(--text-quaternary)]"></i>
          <p class="text-sm">Select a card on the left to inspect acceptance criteria and validate work.</p>
        </div>
      `;
    }
    const auditTbody = document.getElementById('sprintAuditTableBody');
    if (auditTbody) {
      auditTbody.innerHTML = `
        <tr class="animate-pulse">
          <td class="py-3 px-4"><div class="h-3 bg-[var(--bg-surface-tertiary)] rounded w-12"></div></td>
          <td class="py-3 px-4"><div class="h-3 bg-[var(--bg-surface-tertiary)] rounded w-48"></div></td>
          <td class="py-3 px-4"><div class="h-3 bg-[var(--bg-surface-tertiary)] rounded w-16"></div></td>
          <td class="py-3 px-4"><div class="h-4 bg-[var(--bg-surface-tertiary)] rounded w-20"></div></td>
          <td class="py-3 px-4"><div class="h-3 bg-[var(--bg-surface-tertiary)] rounded w-24"></div></td>
          <td class="py-3 px-4"><div class="h-3 bg-[var(--bg-surface-tertiary)] rounded w-16"></div></td>
        </tr>
        <tr class="animate-pulse opacity-50">
          <td class="py-3 px-4"><div class="h-3 bg-[var(--bg-surface-tertiary)] rounded w-12"></div></td>
          <td class="py-3 px-4"><div class="h-3 bg-[var(--bg-surface-tertiary)] rounded w-36"></div></td>
          <td class="py-3 px-4"><div class="h-3 bg-[var(--bg-surface-tertiary)] rounded w-16"></div></td>
          <td class="py-3 px-4"><div class="h-4 bg-[var(--bg-surface-tertiary)] rounded w-20"></div></td>
          <td class="py-3 px-4"><div class="h-3 bg-[var(--bg-surface-tertiary)] rounded w-24"></div></td>
          <td class="py-3 px-4"><div class="h-3 bg-[var(--bg-surface-tertiary)] rounded w-16"></div></td>
        </tr>
      `;
    }
  } else if (activeTab === 'sprint-dod') {
    const emptyState = document.getElementById('activeStreamsEmptyState');
    if (emptyState) emptyState.classList.add('hidden');
    const streamsGrid = document.getElementById('activeStreamsGrid');
    if (streamsGrid) {
      streamsGrid.classList.remove('hidden');
      streamsGrid.innerHTML = `
        <div class="stream-card p-5 rounded-2xl bg-[var(--bg-surface)] border border-[var(--border-subtle)] animate-pulse space-y-4 my-2">
          <div class="flex justify-between items-center">
            <div class="h-4 bg-[var(--bg-surface-tertiary)] rounded w-40"></div>
            <div class="h-4 bg-[var(--bg-surface-tertiary)] rounded w-20"></div>
          </div>
          <div class="h-2 bg-[var(--bg-surface-tertiary)] rounded w-full"></div>
          <div class="space-y-2 pt-2">
            <div class="h-3 bg-[var(--bg-surface-tertiary)] rounded w-3/4"></div>
            <div class="h-3 bg-[var(--bg-surface-tertiary)] rounded w-2/3"></div>
            <div class="h-3 bg-[var(--bg-surface-tertiary)] rounded w-1/2"></div>
          </div>
        </div>
      `;
    }
  } else if (activeTab === 'sprints') {
    const roadmapTbody = document.getElementById('sprintRoadmapTable');
    if (roadmapTbody) {
      roadmapTbody.innerHTML = `
        <tr class="animate-pulse">
          <td class="py-3 px-4"><div class="h-3 bg-[var(--bg-surface-tertiary)] rounded w-14"></div></td>
          <td class="py-3 px-4"><div class="h-3 bg-[var(--bg-surface-tertiary)] rounded w-64"></div></td>
          <td class="py-3 px-4"><div class="h-4 bg-[var(--bg-surface-tertiary)] rounded w-20"></div></td>
          <td class="py-3 px-4 text-right"><div class="h-3 bg-[var(--bg-surface-tertiary)] rounded w-16 ml-auto"></div></td>
        </tr>
        <tr class="animate-pulse opacity-50">
          <td class="py-3 px-4"><div class="h-3 bg-[var(--bg-surface-tertiary)] rounded w-14"></div></td>
          <td class="py-3 px-4"><div class="h-3 bg-[var(--bg-surface-tertiary)] rounded w-48"></div></td>
          <td class="py-3 px-4"><div class="h-4 bg-[var(--bg-surface-tertiary)] rounded w-20"></div></td>
          <td class="py-3 px-4 text-right"><div class="h-3 bg-[var(--bg-surface-tertiary)] rounded w-16 ml-auto"></div></td>
        </tr>
      `;
    }
    const roadmapMobileList = document.getElementById('sprintRoadmapMobileList');
    if (roadmapMobileList) {
      roadmapMobileList.innerHTML = `
        <div class="p-3.5 rounded-xl bg-[var(--bg-surface-secondary)] border border-[var(--border-subtle)] animate-pulse space-y-2.5">
          <div class="flex items-center justify-between">
            <div class="flex items-center gap-2">
              <div class="h-4 bg-[var(--bg-surface-tertiary)] rounded w-16"></div>
              <div class="h-4 bg-[var(--bg-surface-tertiary)] rounded w-14"></div>
            </div>
            <div class="h-6 bg-[var(--bg-surface-tertiary)] rounded w-20"></div>
          </div>
          <div class="h-3 bg-[var(--bg-surface-tertiary)] rounded w-full"></div>
          <div class="h-3 bg-[var(--bg-surface-tertiary)] rounded w-2/3"></div>
        </div>
        <div class="p-3.5 rounded-xl bg-[var(--bg-surface-secondary)] border border-[var(--border-subtle)] animate-pulse opacity-50 space-y-2.5">
          <div class="flex items-center justify-between">
            <div class="flex items-center gap-2">
              <div class="h-4 bg-[var(--bg-surface-tertiary)] rounded w-16"></div>
              <div class="h-4 bg-[var(--bg-surface-tertiary)] rounded w-14"></div>
            </div>
            <div class="h-6 bg-[var(--bg-surface-tertiary)] rounded w-20"></div>
          </div>
          <div class="h-3 bg-[var(--bg-surface-tertiary)] rounded w-full"></div>
          <div class="h-3 bg-[var(--bg-surface-tertiary)] rounded w-1/2"></div>
        </div>
      `;
    }
  } else if (activeTab === 'activity') {
    const fileList = document.getElementById('activityFileList');
    if (fileList) {
      fileList.innerHTML = `
        <div class="p-3 rounded-xl bg-[var(--bg-surface-secondary)] border border-[var(--border-subtle)] animate-pulse space-y-2">
          <div class="h-3 bg-[var(--bg-surface-tertiary)] rounded w-3/4"></div>
          <div class="h-2.5 bg-[var(--bg-surface-tertiary)] rounded w-1/3"></div>
        </div>
      `;
    }
    const commitList = document.getElementById('activityCommitList');
    if (commitList) {
      commitList.innerHTML = `
        <div class="p-3 rounded-xl bg-[var(--bg-surface-secondary)] border border-[var(--border-subtle)] animate-pulse space-y-2">
          <div class="h-3 bg-[var(--bg-surface-tertiary)] rounded w-3/4"></div>
          <div class="h-2.5 bg-[var(--bg-surface-tertiary)] rounded w-1/3"></div>
        </div>
        <div class="p-3 rounded-xl bg-[var(--bg-surface-secondary)] border border-[var(--border-subtle)] animate-pulse space-y-2 opacity-50">
          <div class="h-3 bg-[var(--bg-surface-tertiary)] rounded w-2/3"></div>
          <div class="h-2.5 bg-[var(--bg-surface-tertiary)] rounded w-1/4"></div>
        </div>
      `;
    }
  }
}

// -----------------------------------------------------------------------------
// Data Fetching
// -----------------------------------------------------------------------------
let currentFetchAbortController = null;
let pollTimeoutId = null;
let isPollingActive = true;
let isFetching = false;
let consecutivePollErrors = 0;

function scheduleNextPoll(delayMs = 10000) {
  if (pollTimeoutId) {
    clearTimeout(pollTimeoutId);
    pollTimeoutId = null;
  }
  if (!isPollingActive) return;

  pollTimeoutId = setTimeout(async () => {
    if (isFetching || document.hidden) {
      scheduleNextPoll(isFetching ? 2000 : 10000);
      return;
    }
    isFetching = true;
    try {
      await fetchCurrentViewData();
      consecutivePollErrors = 0;
      scheduleNextPoll(10000);
    } catch (err) {
      if (err.name !== 'AbortError') {
        consecutivePollErrors = Math.min(consecutivePollErrors + 1, 5);
        console.warn('Polling error:', err);
      }
      const backoff = 10000 + (consecutivePollErrors * 5000);
      scheduleNextPoll(backoff);
    } finally {
      isFetching = false;
    }
  }, delayMs);
}

// -----------------------------------------------------------------------------
// View-Scoped Data Fetching
// -----------------------------------------------------------------------------
async function fetchBoardData(signal = undefined) {
  showLoadingFeedback(true);
  const errBanner = document.getElementById('globalErrorBanner');
  if (errBanner) errBanner.classList.add('hidden');

  try {
    const fetchPromises = [
      fetch('/api/cards', { signal }).then((r) => {
        if (!r.ok) throw new Error(`HTTP ${r.status} fetching cards`);
        return r.json();
      }),
    ];
    if (state.sprints.length === 0) {
      fetchPromises.push(
        fetch('/api/sprints', { signal }).then((r) => {
          if (!r.ok) throw new Error(`HTTP ${r.status} fetching sprints`);
          return r.json();
        })
      );
    }
    const [cardsRes, sprintsRes] = await Promise.all(fetchPromises);
    state.cards = cardsRes.cards || [];
    if (sprintsRes) {
      state.sprints = sprintsRes.roadmap || [];
    }

    try {
      detectCardTransitions(state.cards);
    } catch (e) {
      console.error('Error in detectCardTransitions:', e);
    }

    try {
      const activeCount = state.cards.filter((c) => c.status === 'in-progress').length;
      if (state.currentProject) state.currentProject.building_count = activeCount;
      const curChoice = (state.projects || []).find((p) => p.slug === state.activeProjectSlug);
      if (curChoice) curChoice.building_count = activeCount;
      renderProjectUi();
    } catch (_) {}

    try { updateHeader(); } catch (e) { console.error('Error in updateHeader:', e); }
    try { populateSprintFilter(); } catch (e) { console.error('Error in populateSprintFilter:', e); }
    try { renderPipeline(); } catch (e) { console.error('Error in renderPipeline:', e); }

    if (!window._initialDeepLinkHandled) {
      window._initialDeepLinkHandled = true;
      try {
        handleUrlDeepLink();
      } catch (e) {
        console.error('Error in handleUrlDeepLink:', e);
      }
    }
  } catch (err) {
    if (err.name !== 'AbortError') {
      console.error('Failed to load board data:', err);
      if (errBanner) {
        const errMsg = document.getElementById('globalErrorMessage');
        if (errMsg) errMsg.textContent = `Connection error: ${err.message || 'Server unreachable'}. Check that server is running.`;
        errBanner.classList.remove('hidden');
      }
      throw err;
    }
  } finally {
    showLoadingFeedback(false);
    if (window.lucide) lucide.createIcons();
  }
}

async function fetchQaData(signal = undefined) {
  showLoadingFeedback(true);
  try {
    const [cardsRes, gateStatusRes, sprintGateRes] = await Promise.allSettled([
      fetch('/api/cards', { signal }).then((r) => r.ok ? r.json() : Promise.reject(new Error(`HTTP ${r.status}`))),
      fetch('/api/heavy-gate/status', { signal }).then((r) => r.ok ? r.json() : null),
      fetch('/api/heavy-gate/sprints', { signal }).then((r) => r.ok ? r.json() : null),
    ]);
    if (cardsRes.status === 'fulfilled' && cardsRes.value) {
      state.cards = cardsRes.value.cards || [];
      try { detectCardTransitions(state.cards); } catch (_) {}
    }
    if (gateStatusRes.status === 'fulfilled' && gateStatusRes.value) {
      state.heavyGateWorker = gateStatusRes.value.worker;
      state.heavyGateInfo = gateStatusRes.value.gate_info || gateStatusRes.value.info;
      if (gateStatusRes.value.gates) {
        state.projectGates = gateStatusRes.value.gates;
      }
      try {
        updateHeavyGateUi();
      } catch (gateErr) {
        console.warn('Failed to update heavy gate UI:', gateErr);
      }
    }
    if (sprintGateRes.status === 'fulfilled' && sprintGateRes.value) {
      state.sprintGateStatus = sprintGateRes.value.sprints || [];
      if (sprintGateRes.value.gates) {
        state.projectGates = sprintGateRes.value.gates;
      }
      try {
        renderSprintAuditTable();
      } catch (auditErr) {
        console.warn('Failed to render sprint audit table:', auditErr);
      }
    }
    try {
      renderQaStation();
    } catch (qaErr) {
      console.error('Failed to render QA station:', qaErr);
    }
    try {
      updateHeader();
    } catch (headerErr) {
      console.warn('Failed to update header:', headerErr);
    }
  } catch (err) {
    if (err.name !== 'AbortError') {
      console.error('Failed to load QA data:', err);
      throw err;
    }
  } finally {
    showLoadingFeedback(false);
    if (window.lucide) lucide.createIcons();
  }
}

async function fetchRoadmapData(signal = undefined) {
  showLoadingFeedback(true);
  try {
    const fetchPromises = [
      fetch('/api/sprints', { signal }).then((r) => r.ok ? r.json() : Promise.reject(new Error(`HTTP ${r.status}`))),
    ];
    if (state.cards.length === 0) {
      fetchPromises.push(
        fetch('/api/cards', { signal }).then((r) => r.ok ? r.json() : Promise.reject(new Error(`HTTP ${r.status}`)))
      );
    }
    const [sprintsRes, cardsRes] = await Promise.all(fetchPromises);
    state.sprints = sprintsRes.roadmap || [];
    if (cardsRes) {
      state.cards = cardsRes.cards || [];
    }
    renderRoadmap();
    updateHeader();
  } catch (err) {
    if (err.name !== 'AbortError') {
      console.error('Failed to load roadmap data:', err);
      throw err;
    }
  } finally {
    showLoadingFeedback(false);
    if (window.lucide) lucide.createIcons();
  }
}

async function fetchActivityData(signal = undefined) {
  showLoadingFeedback(true);
  try {
    const [actRes, syncRes] = await Promise.allSettled([
      fetch('/api/activity', { signal }).then((r) => r.ok ? r.json() : Promise.reject(new Error(`HTTP ${r.status}`))),
      fetch('/api/git/sync-status', { signal }).then((r) => r.ok ? r.json() : null),
    ]);
    if (actRes.status === 'fulfilled' && actRes.value) {
      state.activity = actRes.value || {};
    }
    if (syncRes.status === 'fulfilled' && syncRes.value) {
      state.gitSync = syncRes.value || {};
    }
    renderActivity();
  } catch (err) {
    if (err.name !== 'AbortError') {
      console.error('Failed to load activity data:', err);
      throw err;
    }
  } finally {
    showLoadingFeedback(false);
    if (window.lucide) lucide.createIcons();
  }
}

async function fetchActiveSprintData(sprintId = null, signal = undefined) {
  showLoadingFeedback(true);
  if (sprintId !== null) {
    state.activeSprintId = sprintId;
  }
  try {
    const param = state.activeSprintId && state.activeSprintId !== 'ACTIVE'
      ? `?sprint=${encodeURIComponent(state.activeSprintId)}`
      : '';
    const fetchPromises = [
      fetch(`/api/active-sprint${param}`, { signal }).then((r) => r.ok ? r.json() : Promise.reject(new Error(`HTTP ${r.status}`))),
    ];
    if (state.cards.length === 0) {
      fetchPromises.push(
        fetch('/api/cards', { signal }).then((r) => r.ok ? r.json() : Promise.reject(new Error(`HTTP ${r.status}`)))
      );
    }
    const [dodRes, cardsRes] = await Promise.all(fetchPromises);
    state.sprintDodData = dodRes;
    if (cardsRes) {
      state.cards = cardsRes.cards || [];
    }
    renderActiveSprintDod();
    updateHeader();
  } catch (err) {
    if (err.name !== 'AbortError') {
      console.error('Failed to load active sprint data:', err);
      throw err;
    }
  } finally {
    showLoadingFeedback(false);
    if (window.lucide) lucide.createIcons();
  }
}

async function fetchCurrentViewData(signal = undefined) {
  const currentTab = state.activeTab || 'pipeline';
  switch (currentTab) {
    case 'pipeline':
      return await fetchBoardData(signal);
    case 'qa':
      return await fetchQaData(signal);
    case 'sprints':
      return await fetchRoadmapData(signal);
    case 'activity':
      return await fetchActivityData(signal);
    case 'sprint-dod':
      return await fetchActiveSprintData(state.activeSprintId, signal);
    case 'analytics':
      return await loadAnalytics(state.analyticsWindow || '6w', false, signal);
    default:
      return await fetchBoardData(signal);
  }
}

async function fetchAll() {
  return await fetchCurrentViewData();
}

// -----------------------------------------------------------------------------
// Header & Metrics
// -----------------------------------------------------------------------------
function updateHeader() {
  renderProjectHeader();
  const branchEl = document.getElementById('headerBranch');
  if (branchEl) branchEl.textContent = `Branch: ${state.activity.branch || 'unknown'}`;

  const totalEl = document.getElementById('headerTotalCount');
  if (totalEl) totalEl.textContent = state.cards.length;

  const inProgCards = state.cards.filter((c) => c.status === 'in-progress');
  const inProgEl = document.getElementById('headerInProgressCount');
  if (inProgEl) inProgEl.textContent = inProgCards.length;

  const qaCards = state.cards.filter((c) => c.status === 'qa');
  const qaCountEl = document.getElementById('headerQaCount');
  if (qaCountEl) qaCountEl.textContent = qaCards.length;

  const qaBadge = document.getElementById('qaTabBadge');
  if (qaBadge) {
    if (qaCards.length > 0) {
      qaBadge.textContent = qaCards.length;
      qaBadge.classList.remove('hidden');
    } else {
      qaBadge.classList.add('hidden');
    }
  }

  const dodNavBadge = document.getElementById('sprintDodNavBadge');
  if (dodNavBadge) {
    dodNavBadge.textContent = `${inProgCards.length} active`;
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
  const activeSprintEl = document.getElementById('headerActiveSprint');
  if (activeSprintEl) activeSprintEl.textContent = storyLabel(activeSprintTitle);
}

function extraTagText(tag) {
  const value = String(tag.value);
  return /^\d+$/.test(value) ? `${tag.label} ${value}` : value;
}

function extraTagPillsHtml(card, size = 'sm') {
  const tags = (card && card.extra_tags) || [];
  if (!tags.length) return '';
  const sizeClass = size === 'lg' ? 'text-xs font-semibold px-2.5 py-0.5' : 'text-[10px] font-semibold px-1.5 py-0.5';
  const pillClass = `${sizeClass} rounded bg-[var(--bg-surface-secondary)] text-[var(--text-secondary)] border border-[var(--border-subtle)]`;
  return tags
    .map((tag) => {
      const text = escapeHtml(extraTagText(tag));
      const title = escapeHtml(tag.label);
      if (typeof tag.link === 'string' && /^https?:\/\//i.test(tag.link)) {
        return `<a class="${pillClass} hover:text-emerald-500 transition-colors" href="${escapeHtml(tag.link)}" target="_blank" rel="noopener noreferrer" title="${title}" onclick="event.stopPropagation()">${text}</a>`;
      }
      return `<span class="${pillClass}" title="${title}">${text}</span>`;
    })
    .join('');
}

function setExtraTagsHeader(id, card) {
  const el = document.getElementById(id);
  if (!el) return;
  const html = extraTagPillsHtml(card, 'lg');
  el.innerHTML = html;
  el.classList.toggle('hidden', !html);
}

function declaredFilterTags() {
  const tags = (state.currentProject && state.currentProject.extra_tags) || [];
  const seen = new Set();
  return tags.filter((t) => t.filter && !seen.has(t.key) && seen.add(t.key));
}

function cardTagValue(card, key) {
  const tag = (card.extra_tags || []).find((t) => t.key === key);
  return tag ? tag.value : null;
}

function populateExtraTagFilters() {
  const container = document.getElementById('pipelineExtraTagFilters');
  if (!container) return;
  const options = declaredFilterTags().map((tag) => {
    const values = new Set();
    state.cards.forEach((c) => {
      const v = cardTagValue(c, tag.key);
      if (v !== null) values.add(v);
    });
    return { tag, values: Array.from(values).sort((a, b) => a.localeCompare(b, undefined, { numeric: true })) };
  });
  // A filter must never stay active without a control to clear it.
  Object.keys(state.filters.tags).forEach((key) => {
    const opt = options.find((o) => o.tag.key === key);
    if (!opt || !opt.values.includes(state.filters.tags[key])) delete state.filters.tags[key];
  });
  const signature = JSON.stringify(options.map((o) => [o.tag.key, o.tag.label, o.values]));
  if (container.dataset.signature !== signature) {
    container.dataset.signature = signature;
    container.innerHTML = '';
    options.forEach(({ tag, values }) => {
      const select = document.createElement('select');
      select.className = 'text-xs rounded-lg px-2 py-1.5 bg-[var(--bg-surface)] text-[var(--text-primary)] border border-[var(--border-medium)]';
      select.setAttribute('aria-label', tag.label);
      select.dataset.tagKey = tag.key;
      const all = document.createElement('option');
      all.value = '';
      all.textContent = `All ${tag.label}`;
      select.appendChild(all);
      values.forEach((v) => {
        const opt = document.createElement('option');
        opt.value = v;
        opt.textContent = extraTagText({ label: tag.label, value: v });
        select.appendChild(opt);
      });
      select.addEventListener('change', (e) => {
        if (e.target.value === '') delete state.filters.tags[tag.key];
        else state.filters.tags[tag.key] = e.target.value;
        renderPipeline();
      });
      container.appendChild(select);
    });
  }
  container.querySelectorAll('select').forEach((select) => {
    const cur = state.filters.tags[select.dataset.tagKey];
    select.value = cur === undefined ? '' : cur;
  });
  container.classList.toggle('hidden', !options.length);
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
  populateExtraTagFilters();
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
    for (const [key, val] of Object.entries(state.filters.tags)) {
      if (cardTagValue(card, key) !== val) return false;
    }
    if (state.filters.search) {
      const q = state.filters.search.toLowerCase();
      const matchId = card.mn_id.toLowerCase().includes(q);
      const matchTitle = card.title.toLowerCase().includes(q);
      const matchDesc = card.description.toLowerCase().includes(q);
      if (!matchId && !matchTitle && !matchDesc) return false;
    }
    return true;
  });

  // Partition cards into status columns
  const colBuckets = { backlog: [], 'sprint-backlog': [], 'in-progress': [], qa: [], completed: [] };
  const colCounts = { backlog: 0, 'sprint-backlog': 0, 'in-progress': 0, qa: 0, completed: 0 };

  filtered.forEach((card) => {
    const status = card.status || 'backlog';
    if (colBuckets[status]) {
      colBuckets[status].push(card);
    } else {
      colBuckets.backlog.push(card);
    }
  });

  function getCardSortTimestamp(c, preferField) {
    if (preferField && c[preferField]) return String(c[preferField]);
    if (c.created_at) return String(c.created_at);
    if (c.created_date && c.created_time) return `${c.created_date} ${c.created_time}`;
    if (c.created_date) return `${c.created_date} 00:00`;
    return '';
  }

  // 1. Ready to Build (sprint-backlog): Ascending (FIFO oldest to newest), tie-break ID asc, missing timestamps at bottom
  colBuckets['sprint-backlog'].sort((a, b) => {
    const tA = getCardSortTimestamp(a, 'created_at');
    const tB = getCardSortTimestamp(b, 'created_at');
    if (!tA && !tB) return (a.mn_id || '').localeCompare(b.mn_id || '', undefined, { numeric: true });
    if (!tA) return 1;
    if (!tB) return -1;
    const cmp = tA.localeCompare(tB);
    if (cmp !== 0) return cmp;
    return (a.mn_id || '').localeCompare(b.mn_id || '', undefined, { numeric: true });
  });

  // 2. QA Test (qa): Descending by qa_entered_at, tie-break ID desc
  colBuckets.qa.sort((a, b) => {
    const tA = a.qa_entered_at || (a.updated_at ? String(a.updated_at).replace('T', ' ').slice(0, 16) : '') || getCardSortTimestamp(a, 'created_at');
    const tB = b.qa_entered_at || (b.updated_at ? String(b.updated_at).replace('T', ' ').slice(0, 16) : '') || getCardSortTimestamp(b, 'created_at');
    if (!tA && !tB) return (b.mn_id || '').localeCompare(a.mn_id || '', undefined, { numeric: true });
    if (!tA) return 1;
    if (!tB) return -1;
    const cmp = tB.localeCompare(tA);
    if (cmp !== 0) return cmp;
    return (b.mn_id || '').localeCompare(a.mn_id || '', undefined, { numeric: true });
  });

  // 3. In Progress (in-progress): Descending by start_time / latest activity / updated_at, tie-break ID desc
  colBuckets['in-progress'].sort((a, b) => {
    const tA = a.start_time || (a.updated_at ? String(a.updated_at).replace('T', ' ').slice(0, 16) : '') || getCardSortTimestamp(a, 'created_at');
    const tB = b.start_time || (b.updated_at ? String(b.updated_at).replace('T', ' ').slice(0, 16) : '') || getCardSortTimestamp(b, 'created_at');
    if (!tA && !tB) return (b.mn_id || '').localeCompare(a.mn_id || '', undefined, { numeric: true });
    if (!tA) return 1;
    if (!tB) return -1;
    const cmp = tB.localeCompare(tA);
    if (cmp !== 0) return cmp;
    return (b.mn_id || '').localeCompare(a.mn_id || '', undefined, { numeric: true });
  });

  // 4. Backlog (backlog): Descending by created_at, tie-break ID desc
  colBuckets.backlog.sort((a, b) => {
    const tA = getCardSortTimestamp(a, 'created_at');
    const tB = getCardSortTimestamp(b, 'created_at');
    if (!tA && !tB) return (b.mn_id || '').localeCompare(a.mn_id || '', undefined, { numeric: true });
    if (!tA) return 1;
    if (!tB) return -1;
    const cmp = tB.localeCompare(tA);
    if (cmp !== 0) return cmp;
    return (b.mn_id || '').localeCompare(a.mn_id || '', undefined, { numeric: true });
  });

  // 5. Completed (completed): Descending by completion time, tie-break ID desc
  colBuckets.completed.sort((a, b) => {
    const tA = getCardCompletedDate(a);
    const tB = getCardCompletedDate(b);
    if (!tA && !tB) return (b.mn_id || '').localeCompare(a.mn_id || '', undefined, { numeric: true });
    if (!tA) return 1;
    if (!tB) return -1;
    const cmp = tB.localeCompare(tA);
    if (cmp !== 0) return cmp;
    return (b.mn_id || '').localeCompare(a.mn_id || '', undefined, { numeric: true });
  });

  // Render cards in their sorted column order
  Object.keys(cols).forEach((status) => {
    const bucket = colBuckets[status] || [];
    colCounts[status] = bucket.length;
    bucket.forEach((card) => {
      const cardEl = createCardElement(card);
      cols[status].appendChild(cardEl);
    });
  });

  // Update column count badges
  Object.entries(counts).forEach(([k, el]) => {
    if (el) el.textContent = colCounts[k] || 0;
    const mEl = document.getElementById(`mcount-${k}`);
    if (mEl) mEl.textContent = colCounts[k] || 0;
  });

  // Update active sprint filter pill banner
  const activeFilterPill = document.getElementById('pipelineActiveFilterPill');
  if (activeFilterPill) {
    const tagLabels = Object.entries(state.filters.tags).map(([key, value]) => {
      const t = declaredFilterTags().find((d) => d.key === key);
      return extraTagText({ label: t ? t.label : key, value });
    });
    const sprintActive = state.filters.sprint && state.filters.sprint !== 'ALL';
    if (sprintActive || tagLabels.length) {
      const parts = sprintActive ? [`Story ${state.filters.sprint}`] : [];
      tagLabels.forEach((l) => parts.push(l));
      activeFilterPill.className = 'mb-3 flex items-center justify-between p-2.5 px-3 rounded-xl bg-purple-500/10 border border-purple-500/20 text-xs';
      activeFilterPill.innerHTML = `
        <div class="flex items-center gap-2 text-purple-700 dark:text-purple-300 font-medium">
          <i class="ph ph-funnel text-sm text-purple-500"></i>
          <span>Filtered by: <strong class="font-bold">${parts.map(escapeHtml).join(' · ')}</strong></span>
        </div>
        <button onclick="clearPipelineSprintFilter()" data-action="clear-sprint-filter" class="inline-flex items-center gap-1 px-2.5 py-1 rounded-lg bg-[var(--bg-surface)] hover:bg-[var(--bg-surface-secondary)] text-[var(--text-primary)] border border-[var(--border-medium)] text-xs font-semibold shadow-xs transition cursor-pointer">
          <i class="ph ph-x text-xs"></i>
          <span>Clear</span>
        </button>
      `;
    } else {
      activeFilterPill.className = 'hidden mb-3';
      activeFilterPill.innerHTML = '';
    }
  }
}

function formatSmartTimestamp(dateStr, timeStr) {
  if (!dateStr) return { relative: 'N/A', full: 'N/A' };
  const cleanDate = String(dateStr).trim().split('T')[0];
  let cleanTime = timeStr ? String(timeStr).trim().slice(0, 5) : '';
  if (!cleanTime && String(dateStr).includes('T')) {
    cleanTime = String(dateStr).split('T')[1].slice(0, 5);
  } else if (!cleanTime && String(dateStr).includes(' ')) {
    cleanTime = String(dateStr).split(' ')[1].slice(0, 5);
  }

  const full = cleanTime ? `${cleanDate} ${cleanTime}` : cleanDate;
  if (!cleanTime) {
    return { relative: cleanDate, full };
  }

  try {
    const [y, m, d] = cleanDate.split('-').map(Number);
    const [hh, mm] = cleanTime.split(':').map(Number);
    if (!y || !m || !d || isNaN(hh) || isNaN(mm)) {
      return { relative: cleanDate, full };
    }
    const cardDate = new Date(y, m - 1, d, hh, mm);
    const now = new Date();
    const diffMs = now.getTime() - cardDate.getTime();

    if (diffMs < -60000) {
      const futureMin = Math.round(Math.abs(diffMs) / 60000);
      if (futureMin < 60) {
        return { relative: `In ${futureMin}m`, full, icon: 'ph-clock-countdown', isFuture: true };
      }
      const futureH = Math.round(futureMin / 60);
      return { relative: `In ${futureH}h`, full, icon: 'ph-clock-countdown', isFuture: true };
    }

    if (diffMs < 0 && diffMs >= -60000) {
      return { relative: 'Just now', full };
    }

    const diffMin = Math.floor(diffMs / 60000);
    if (diffMin < 5 && diffMin >= 0) {
      return { relative: 'Just now', full };
    }
    if (diffMin < 60 && diffMin >= 5) {
      return { relative: `${diffMin}m ago`, full };
    }

    const today = new Date(now.getFullYear(), now.getMonth(), now.getDate());
    const cardDay = new Date(y, m - 1, d);
    const dayDiffMs = today.getTime() - cardDay.getTime();
    const dayDiffDays = Math.round(dayDiffMs / 86400000);

    if (dayDiffDays === 0) {
      return { relative: `Today ${cleanTime}`, full };
    }
    if (dayDiffDays === 1) {
      return { relative: `Yesterday ${cleanTime}`, full };
    }

    return { relative: cleanDate, full };
  } catch (e) {
    return { relative: cleanDate, full };
  }
}

function createCardElement(card) {
  const div = document.createElement('div');
  div.className = 'task-card group';

  const isGrill = card.is_grill || card.type === 'grill' || (card.mn_id && card.mn_id.startsWith('GRILL-'));
  if (isGrill) {
    div.draggable = false;
    div.style.cursor = 'pointer';
    div.onclick = (e) => {
      e.stopPropagation();
      switchTab('streams');
      const streamEl = document.getElementById(`stream-${card.mn_id}`);
      if (streamEl) {
        streamEl.scrollIntoView({ behavior: 'smooth', block: 'center' });
        streamEl.classList.add('ring-2', 'ring-purple-500');
        setTimeout(() => streamEl.classList.remove('ring-2', 'ring-purple-500'), 2000);
      }
    };
  } else {
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
        openQaDrawer(card);
        selectQaCard(card.mn_id, { forceScrollTop: true });
      } else {
        openCardModal(card);
      }
    };
  }

  let typeClass = 'pill-feature';
  if (card.type === 'bug') typeClass = 'pill-bug';
  if (card.type === 'improvement') typeClass = 'pill-improvement';
  if (isGrill) typeClass = 'pill-grill';

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
  const escType = isGrill ? 'grill' : escapeHtml(card.type);
  const smartTime = formatSmartTimestamp(card.created_date, card.created_time || (card.created_at ? card.created_at.slice(11, 16) : ''));
  const timeIcon = smartTime.icon || ((card.created_time || (card.created_at && card.created_at.includes(':'))) ? 'ph-clock' : 'ph-calendar-blank');

  const typePillHtml = isGrill
    ? `<span class="text-[10px] font-semibold px-1.5 py-0.5 rounded pill-grill flex items-center gap-1"><i class="ph ph-flame text-xs"></i>Grill</span>`
    : `<span class="text-[10px] font-semibold px-1.5 py-0.5 rounded ${typeClass}">${escType}</span>`;

  div.innerHTML = `
    <div class="flex items-center justify-between gap-1 mb-1.5">
      <span class="text-xs font-mono font-bold text-[var(--text-tertiary)]">${escId}</span>
      <div class="flex items-center gap-1">
        ${card.sprint ? `<span class="text-[10px] font-semibold px-1.5 py-0.5 rounded bg-[var(--bg-surface-secondary)] text-[var(--text-secondary)] border border-[var(--border-subtle)]">Story ${escSprint}</span>` : ''}
        ${typePillHtml}
      </div>
    </div>
    ${(card.extra_tags || []).length ? `<div class="flex flex-wrap items-center gap-1 mb-1.5">${extraTagPillsHtml(card)}</div>` : ''}
    <h3 class="text-sm font-semibold text-[var(--text-primary)] line-clamp-2 group-hover:text-emerald-500 transition-colors">${escTitle}</h3>
    ${dodBar}
    <div class="mt-2 pt-1.5 border-t border-[var(--border-subtle)] flex items-center justify-between text-xs text-[var(--text-tertiary)] font-mono">
      <span class="flex items-center cursor-help" title="${escapeHtml(smartTime.full)}"><i class="ph ${timeIcon} text-[var(--text-quaternary)] mr-1 text-xs"></i>${escapeHtml(smartTime.relative)}</span>
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
  const countEl = document.getElementById('qaStationCount');
  if (countEl) countEl.textContent = qaCards.length;

  // Responsive QA Card Grid (Story 23)
  const grid = document.getElementById('qaCardsGrid');
  if (grid) {
    if (qaCards.length === 0) {
      grid.innerHTML = `
        <div class="col-span-full py-12 text-center text-[var(--text-tertiary)]">
          <i class="ph ph-sparkle text-3xl mx-auto mb-3 text-emerald-500/70 block"></i>
          <p class="text-sm font-semibold text-[var(--text-secondary)]">No cards currently awaiting QA!</p>
          <p class="text-xs text-[var(--text-tertiary)] mt-1">All ready-to-test work has been validated.</p>
        </div>
      `;
    } else {
      grid.innerHTML = '';
      qaCards.forEach((card) => {
        const escId = escapeHtml(card.mn_id);
        const escSprint = escapeHtml(card.sprint || 'N/A');
        const escType = escapeHtml(card.type || 'feature');
        const escTitle = escapeHtml(card.title || 'Untitled');
        const completedDate = getCardCompletedDate(card);
        const displayDate = completedDate || card.created_date;
        const pillTypeClass = card.type === 'bug' ? 'pill-bug' : card.type === 'improvement' ? 'pill-improvement' : 'pill-feature';

        const qaItems = card.qa_items || [];
        const qaChecked = qaItems.filter((i) => i.checked).length;
        const qaTotal = qaItems.length;

        const cardEl = document.createElement('div');
        cardEl.className = 'p-4 rounded-xl bg-[var(--card-bg)] border border-[var(--card-border)] hover:border-amber-500/60 hover:shadow-[var(--card-hover-shadow)] transition-all cursor-pointer shadow-[var(--card-shadow)] flex flex-col justify-between group';
        cardEl.setAttribute('data-qa-card-id', card.mn_id);
        cardEl.onclick = () => openQaDrawer(card);

        cardEl.innerHTML = `
          <div>
            <div class="flex items-center justify-between gap-2 mb-2">
              <div class="flex items-center gap-1.5 min-w-0">
                <span class="font-mono font-bold text-amber-500 text-xs shrink-0">${escId}</span>
                <span class="text-xs px-2 py-0.5 rounded bg-[var(--bg-surface-secondary)] text-[var(--text-secondary)] border border-[var(--border-subtle)] shrink-0">Story ${escSprint}</span>
              </div>
              <span class="text-xs px-2 py-0.5 rounded font-semibold ${pillTypeClass} shrink-0">${escType}</span>
            </div>
            <h3 class="font-semibold text-[var(--text-primary)] text-sm leading-snug line-clamp-2 group-hover:text-amber-500 transition-colors mb-3">${escTitle}</h3>
          </div>
          <div class="space-y-2.5 pt-2 border-t border-[var(--border-subtle)]">
            <div class="flex flex-wrap items-center gap-2 text-xs">
              <span class="text-xs font-mono px-2 py-0.5 rounded bg-emerald-500/10 text-emerald-600 dark:text-emerald-400 border border-emerald-500/20 font-medium" title="Definition of Done">
                DoD: ${card.dod_checked}/${card.dod_total} Complete
              </span>
              ${qaTotal > 0 ? `
                <span class="text-xs font-mono px-2 py-0.5 rounded bg-amber-500/10 text-amber-600 dark:text-amber-400 border border-amber-500/20 font-medium" title="Human QA Items">
                  QA: ${qaChecked}/${qaTotal} done
                </span>` : ''}
              ${card.changelog && card.changelog.length > 0 ? `
                <span class="text-xs font-mono px-2 py-0.5 rounded bg-[var(--bg-surface-secondary)] text-[var(--text-secondary)] border border-[var(--border-subtle)] flex items-center gap-1" title="Changelog Commits">
                  <i class="ph ph-git-commit text-xs text-emerald-500"></i>
                  <span>${card.changelog.length}</span>
                </span>` : ''}
              ${(card.review_count > 0 || (card.reviews && card.reviews.length > 0)) ? `
                <span class="text-xs font-mono px-2 py-0.5 rounded bg-purple-500/10 text-purple-600 dark:text-purple-400 border border-purple-500/30 flex items-center gap-1" title="Reviews">
                  <i class="ph ph-shield-check text-xs"></i>
                  <span>${card.review_count || card.reviews.length}</span>
                </span>` : ''}
            </div>
            <div class="flex items-center justify-between text-xs text-[var(--text-tertiary)] font-mono">
              ${displayDate ? `<span class="flex items-center gap-1"><i class="ph ph-calendar-blank text-[var(--text-quaternary)] text-xs"></i>${escapeHtml(displayDate)}</span>` : '<span></span>'}
              <span class="text-amber-600 dark:text-amber-400 flex items-center gap-1 text-[11px] font-medium"><i class="ph ph-clock text-xs"></i>Ready for QA</span>
            </div>
            ${closeoutButtonHtml(card, 'mt-1 w-full justify-center')}
          </div>
        `;
        bindCloseoutButton(cardEl, card);
        grid.appendChild(cardEl);
      });
    }
  }

  // Update open QA drawer if present
  const qaDrawer = document.getElementById('qaCardDrawer');
  if (qaDrawer && !qaDrawer.classList.contains('translate-x-full')) {
    const currentDrawerId = document.getElementById('qaDrawerId') ? document.getElementById('qaDrawerId').textContent.trim() : null;
    const stillInQa = currentDrawerId ? qaCards.find((c) => c.mn_id === currentDrawerId) : null;
    if (stillInQa) {
      openQaDrawer(stillInQa, { preserveState: true });
    } else {
      closeQaDrawer();
    }
  }

  // Backwards-compatibility for preserved hidden containers & legacy tests
  const container = document.getElementById('qaListContainer');
  const prevListScroll = container ? container.scrollTop : 0;
  if (container) container.innerHTML = '';

  if (qaCards.length === 0) {
    state.selectedQaId = null;
    if (container) {
      container.innerHTML = `
        <div class="p-4 text-center text-[var(--text-tertiary)] text-xs">
          <i class="ph ph-sparkle text-lg mx-auto mb-2 text-emerald-500/70 block"></i>
          No cards currently awaiting QA!
        </div>
      `;
    }
    const detailContainer = document.getElementById('qaDetailContainer');
    if (detailContainer) {
      detailContainer.innerHTML = `
        <div class="h-full flex flex-col items-center justify-center text-[var(--text-tertiary)]">
          <i class="ph ph-stack text-4xl mb-3 text-[var(--text-quaternary)]"></i>
          <p class="text-sm">Select a card on the left to inspect acceptance criteria and validate work.</p>
        </div>
      `;
    }
    return;
  }

  qaCards.forEach((card) => {
    if (!container) return;
    const item = document.createElement('div');
    const isSelected = state.selectedQaId === card.mn_id;
    item.dataset.qaCardId = card.mn_id;
    item.className = `p-3.5 rounded-xl border cursor-pointer transition-all shadow-[var(--card-shadow)] ${
      isSelected
        ? 'bg-[var(--card-bg)] border-amber-500 ring-2 ring-amber-500/25'
        : 'bg-[var(--card-bg)] border-[var(--card-border)] hover:border-[var(--card-hover-border)] hover:shadow-[var(--card-hover-shadow)]'
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
        <span class="font-mono font-bold text-amber-500 text-xs">${escId}</span>
        <span class="text-xs px-2 py-0.5 rounded bg-[var(--bg-surface-secondary)] text-[var(--text-secondary)] border border-[var(--border-subtle)]">Story ${escSprint}</span>
      </div>
      <p class="font-semibold text-[var(--text-primary)] text-sm line-clamp-1">${escTitle}</p>
      <div class="mt-2.5 flex items-center justify-between text-xs text-[var(--text-secondary)] font-mono">
        <span>DoD: ${card.dod_checked}/${card.dod_total}</span>
        ${displayDate ? `<span class="text-[var(--text-secondary)] flex items-center gap-1" title="Completed: ${escapeHtml(displayDate)}"><i class="ph ph-calendar-blank text-[var(--text-quaternary)] text-xs"></i>${escapeHtml(displayDate)}</span>` : ''}
        <span class="text-amber-600 dark:text-amber-400 flex items-center"><i class="ph ph-clock mr-1 text-xs"></i> Ready for QA</span>
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
      el.className = `p-3.5 rounded-xl border cursor-pointer transition-all shadow-[var(--card-shadow)] ${
        isSelected
          ? 'bg-[var(--card-bg)] border-amber-500 ring-2 ring-amber-500/25'
          : 'bg-[var(--card-bg)] border-[var(--card-border)] hover:border-[var(--card-hover-border)] hover:shadow-[var(--card-hover-shadow)]'
      }`;
    });
  }

  if (!card) {
    if (detailEl) {
      detailEl.innerHTML = `
        <div class="h-full flex flex-col items-center justify-center text-[var(--text-tertiary)]">
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
      <h4 class="text-sm font-bold text-[var(--text-secondary)] uppercase tracking-wider mb-2 flex items-center gap-1.5">
        <i class="ph ph-git-commit text-emerald-500 text-base"></i>
        <span>Implementation Changelog & Evidence (${card.changelog.length})</span>
      </h4>
      ${renderChangelogTable(card.changelog, card)}
    </div>
  `
      : '';

  detailEl.innerHTML = `
    <div class="flex items-start justify-between border-b border-[var(--border-subtle)] pb-4 mb-4">
      <div>
        <div class="flex items-center space-x-2 mb-2 flex-wrap gap-y-1">
          <span class="text-sm font-mono font-bold px-2.5 py-0.5 rounded bg-amber-500/10 text-amber-500 border border-amber-500/20">${escId}</span>
          <span class="text-xs font-semibold px-2.5 py-0.5 rounded bg-[var(--bg-surface-secondary)] text-[var(--text-secondary)] border border-[var(--border-subtle)]">Story ${escSprint}</span>
          ${extraTagPillsHtml(card, 'lg')}
          <span class="text-xs font-semibold px-2.5 py-0.5 rounded bg-blue-500/10 text-blue-600 dark:text-blue-400 border border-blue-500/20">${escType}</span>
          <span class="text-xs font-mono px-2.5 py-0.5 rounded bg-[var(--bg-surface-secondary)] text-[var(--text-secondary)] border border-[var(--border-subtle)] flex items-center gap-1">
            <i class="ph ph-calendar-blank text-[var(--text-quaternary)] text-xs"></i>
            <span>Created: ${escapeHtml(card.created_date || 'N/A')}</span>
          </span>
          ${
            card.sprint
              ? `<button data-action="open-sprint-doc" data-sprint="${escSprint}" class="inline-flex items-center gap-1.5 px-2.5 py-1 rounded-lg bg-[var(--bg-surface-secondary)] hover:bg-[var(--bg-surface-tertiary)] text-[var(--text-primary)] border border-[var(--border-medium)] text-xs font-medium transition ml-1 shadow-xs group">
                  <i class="ph ph-book-open text-emerald-500 group-hover:scale-110 transition-transform text-xs"></i>
                  <span>View Story Spec</span>
                </button>`
              : ''
          }
          ${closeoutButtonHtml(card, '', true)}
          ${
            (card.review_count > 0 || (card.reviews && card.reviews.length > 0))
              ? `<button onclick="openReviewModal('${escId}')" class="inline-flex items-center gap-1.5 px-2.5 py-1 rounded-lg bg-purple-500/10 hover:bg-purple-500/20 text-purple-600 dark:text-purple-300 border border-purple-500/30 hover:border-purple-500/50 text-xs font-medium transition ml-1 shadow-xs group">
                  <i class="ph ph-shield-check text-purple-500 group-hover:scale-110 transition-transform text-xs"></i>
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
      <div class="bg-[var(--bg-subtle)] p-4 rounded-xl border border-[var(--border-subtle)] break-words text-[var(--text-primary)]">
        ${renderMarkdown(card.description || 'No description provided.')}
      </div>
    </div>

    <!-- Acceptance Criteria (Interactive Checkboxes) -->
    <div class="mb-6">
      <div class="flex items-center justify-between mb-2">
        <h4 class="text-sm font-bold text-[var(--text-secondary)] uppercase tracking-wider">Definition of Done / Acceptance Criteria</h4>
        <span class="text-sm font-mono text-emerald-500 font-bold">${card.dod_checked}/${card.dod_total} Complete</span>
      </div>
      <div class="bg-[var(--bg-subtle)] p-3.5 rounded-xl border border-[var(--border-subtle)] space-y-2">
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

    <!-- Implementation Changelog & Evidence -->
    ${changelogHtml}

    <!-- Human QA Test Steps -->
    <div class="mb-8 pb-4">
      ${(() => {
        const hasInstr = card.qa_instructions && card.qa_instructions.trim();
        const hasItems = card.qa_items && card.qa_items.length > 0;
        if (!hasInstr && !hasItems) {
          return `
      <div>
        <h4 class="text-sm font-bold text-amber-500 uppercase tracking-wider mb-2 flex items-center gap-1.5">
          <i class="ph ph-clipboard-text text-base"></i>
          <span>Human QA Test Steps</span>
        </h4>
        <div class="bg-amber-500/5 border border-amber-500/25 rounded-xl p-4 flex items-start gap-3">
          <i class="ph ph-warning text-amber-500 mt-0.5 shrink-0 text-base"></i>
          <div class="text-sm text-amber-700 dark:text-amber-200/90 leading-relaxed">
            <span class="font-semibold">No Human QA Test section found in this card.</span><br>
            Ask the dev agent to add a <code class="font-mono text-amber-600 dark:text-amber-300 bg-amber-500/10 px-1 rounded text-xs">## Human QA Test</code> section with explicit step-by-step instructions (where to go, what to click, what to verify).
          </div>
        </div>
      </div>`;
        }
        const instrHtml = hasInstr
          ? `<div class="bg-[var(--bg-subtle)] p-4 rounded-xl border border-[var(--border-subtle)] mb-3 text-[var(--text-primary)]">${renderMarkdown(card.qa_instructions)}</div>`
          : '';
        const itemsHtml = hasItems
          ? `<div class="bg-[var(--bg-subtle)] p-3.5 rounded-xl border border-[var(--border-subtle)] space-y-2">
              ${card.qa_items.map((item, idx) => `
                <label class="flex items-start space-x-3 rounded cursor-pointer text-sm transition p-2 hover:bg-[var(--bg-surface-secondary)] touch-target-item">
                  <input type="checkbox" ${item.checked ? 'checked' : ''} data-action="toggle-qa" data-card-id="${escapeHtml(card.mn_id)}" data-idx="${idx}" class="mt-0.5 w-4 h-4 rounded border-amber-600/50 text-amber-500 focus:ring-amber-500/20 bg-[var(--bg-surface)] cursor-pointer">
                  <span class="${item.checked ? 'text-[var(--text-tertiary)] line-through' : 'text-[var(--text-primary)] font-medium'} text-sm leading-relaxed">${formatRichText(item.text)}</span>
                </label>`).join('')}
            </div>`
          : '';
        return `
      <div>
        <h4 class="text-sm font-bold text-amber-500 uppercase tracking-wider mb-2 flex items-center gap-1.5">
          <i class="ph ph-clipboard-text text-base"></i>
          <span>Human QA Test Steps</span>
          ${hasItems ? `<span class="text-xs px-2 py-0.5 rounded font-semibold bg-amber-500/10 text-amber-600 dark:text-amber-300 border border-amber-500/20 ml-1.5">${card.qa_items.filter(i => i.checked).length}/${card.qa_items.length} done</span>` : ''}
        </h4>
        ${instrHtml}${itemsHtml}
      </div>`;
      })()}
    </div>

    <!-- Tester Verification Action (Sticky Action Bar) -->
    <div class="sticky bottom-0 z-10 bg-[var(--bg-surface)]/95 backdrop-blur-md -mx-6 -mb-6 px-6 pt-3 pb-3 border-t border-[var(--border-subtle)] shadow-md mt-auto">
      <h4 class="text-sm font-bold text-[var(--text-secondary)] uppercase tracking-wider mb-2">QA Sign-off</h4>
      <div class="flex flex-col sm:flex-row gap-3">
        <input type="text" id="qaValidateNotes" placeholder="Optional QA validation note (e.g. 'Verified Wise import flow live')" class="flex-1 bg-[var(--bg-surface)] text-[var(--text-primary)] text-sm rounded-lg px-3.5 py-2.5 border border-[var(--border-medium)] focus:outline-none focus:border-emerald-500">
        <button id="qaValidateCardBtn" data-action="validate-card" data-card-id="${escId}" class="px-5 py-2.5 bg-emerald-600 hover:bg-emerald-500 text-white text-sm font-bold rounded-lg shadow-lg shadow-emerald-900/30 flex items-center justify-center space-x-2 transition cursor-pointer">
          <i class="ph ph-checks text-base"></i>
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
      if (window.innerWidth < 1024) {
        detailEl.scrollIntoView({ behavior: 'smooth', block: 'start' });
      }
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
// -----------------------------------------------------------------------------
// Dedicated QA Slide-Over Drawer (Story 23)
// -----------------------------------------------------------------------------
function openQaDrawer(card, { preserveState = false } = {}) {
  const drawer = document.getElementById('qaCardDrawer');
  const backdrop = document.getElementById('qaDrawerBackdrop');
  if (!drawer || !card) return;

  state.selectedQaId = card.mn_id;

  const escId = escapeHtml(card.mn_id);
  const escSprint = escapeHtml(card.sprint || 'N/A');
  const escType = escapeHtml(card.type || 'feature');
  const escFileName = escapeHtml(card.file_name || '');
  const completedDate = getCardCompletedDate(card);
  const displayDate = completedDate || card.created_date;

  // Header badges
  const idEl = document.getElementById('qaDrawerId');
  if (idEl) idEl.textContent = card.mn_id;

  const sprintEl = document.getElementById('qaDrawerSprint');
  if (sprintEl) sprintEl.textContent = card.sprint ? `Story ${card.sprint}` : 'Unassigned';
  setExtraTagsHeader('qaDrawerExtraTags', card);

  const typeEl = document.getElementById('qaDrawerType');
  if (typeEl) {
    typeEl.textContent = card.type || 'feature';
    typeEl.className = `text-xs px-2.5 py-0.5 rounded font-semibold ${
      card.type === 'bug' ? 'pill-bug' : card.type === 'improvement' ? 'pill-improvement' : 'pill-feature'
    }`;
  }

  const dateEl = document.getElementById('qaDrawerDate');
  if (dateEl) {
    dateEl.innerHTML = `<i class="ph ph-calendar-blank text-[var(--text-quaternary)] text-xs"></i><span>${escapeHtml(displayDate || card.created_date || 'N/A')}</span>`;
  }

  // Header actions: Spec, Close-Out, Reviews
  const specBtn = document.getElementById('qaDrawerViewSpecBtn');
  if (specBtn) {
    specBtn.classList.toggle('hidden', !(card.sprint && card.sprint !== 'Unassigned'));
  }

  const closeoutBtn = document.getElementById('qaDrawerCloseoutBtn');
  if (closeoutBtn) {
    closeoutBtn.classList.toggle('hidden', !(card.closeout && card.closeout.url));
    closeoutBtn.dataset.cardId = card.mn_id;
  }

  const reviewsBtn = document.getElementById('qaDrawerReviewsBtn');
  const reviewsLabel = document.getElementById('qaDrawerReviewsBtnLabel');
  if (reviewsBtn) {
    const revCount = card.review_count || (card.reviews ? card.reviews.length : 0);
    if (revCount > 0) {
      reviewsBtn.classList.remove('hidden');
      if (reviewsLabel) reviewsLabel.textContent = `Reviews (${revCount})`;
    } else {
      reviewsBtn.classList.add('hidden');
    }
  }

  // Preserve state if requested
  const bodyEl = document.getElementById('qaDrawerBody');
  const prevScrollTop = (preserveState && bodyEl) ? bodyEl.scrollTop : 0;
  const notesInput = document.getElementById('qaDrawerValidateNotes');
  const preservedNote = (preserveState && notesInput) ? notesInput.value : '';

  // Title and file metadata
  const titleEl = document.getElementById('qaDrawerTitle');
  if (titleEl) titleEl.textContent = card.title;

  const datesEl = document.getElementById('qaDrawerDates');
  if (datesEl) {
    datesEl.innerHTML = `
      <span class="flex items-center"><i class="ph ph-calendar-blank text-[var(--text-quaternary)] mr-1"></i>Created: ${escapeHtml(card.created_date || 'N/A')}</span>
      ${displayDate ? `<span class="flex items-center text-amber-500"><i class="ph ph-clock mr-1"></i>Ready for QA: ${escapeHtml(displayDate)}</span>` : ''}
      <span class="text-[var(--text-tertiary)] truncate max-w-xs">File: ${escFileName}</span>
    `;
  }

  // Description / User Intent
  const descEl = document.getElementById('qaDrawerDesc');
  if (descEl) {
    descEl.innerHTML = renderMarkdown(card.description || 'No description provided.');
  }

  // Definition of Done / Acceptance Criteria (interactive checkboxes)
  const dodCountEl = document.getElementById('qaDrawerDodCount');
  const dodListEl = document.getElementById('qaDrawerDodList');
  if (dodCountEl) dodCountEl.textContent = `${card.dod_checked}/${card.dod_total} Complete`;

  if (dodListEl) {
    if (card.dod && card.dod.length > 0) {
      dodListEl.innerHTML = card.dod.map((item, idx) => {
        const isPending = !item.checked;
        const itemClass = isPending ? 'qa-pending-item p-2' : 'p-2 hover:bg-[var(--bg-surface-secondary)]';
        return `
          <label class="flex items-start justify-between space-x-3 rounded-lg cursor-pointer text-sm transition touch-target-item ${itemClass}">
            <div class="flex items-start space-x-3 flex-1">
              <input type="checkbox" ${item.checked ? 'checked' : ''} data-action="toggle-dod" data-card-id="${escId}" data-idx="${idx}" class="mt-0.5 w-4 h-4 rounded border-[var(--border-medium)] text-emerald-500 focus:ring-emerald-500/20 bg-[var(--bg-surface)] cursor-pointer">
              <span class="${item.checked ? 'text-[var(--text-tertiary)] line-through' : 'text-[var(--text-primary)] font-medium'} text-sm leading-relaxed">${formatRichText(item.text)}</span>
            </div>
            ${isPending ? '<span class="text-[10px] font-semibold px-2 py-0.5 rounded bg-amber-500/20 text-amber-600 dark:text-amber-300 ml-2 whitespace-nowrap border border-amber-500/30">Needs Live Validation</span>' : ''}
          </label>
        `;
      }).join('');
    } else {
      dodListEl.innerHTML = `<p class="text-sm text-[var(--text-tertiary)] italic p-2">No checklist items defined.</p>`;
    }
  }

  // Implementation Changelog & Evidence
  const clContainer = document.getElementById('qaDrawerChangelogContainer');
  const clList = document.getElementById('qaDrawerChangelog');
  const clHeading = document.getElementById('qaDrawerChangelogHeading');
  if (clContainer && clList) {
    if (card.changelog && card.changelog.length > 0) {
      clContainer.classList.remove('hidden');
      if (clHeading) clHeading.textContent = `Implementation Changelog & Evidence (${card.changelog.length})`;
      clList.innerHTML = renderChangelogTable(card.changelog, card);
    } else {
      clContainer.classList.add('hidden');
    }
  }

  // Human QA Test Steps
  const qaContainer = document.getElementById('qaDrawerQaContainer');
  const qaItemsCountEl = document.getElementById('qaDrawerQaItemsCount');
  const qaInstrEl = document.getElementById('qaDrawerQaInstructions');
  const qaListEl = document.getElementById('qaDrawerQaList');
  if (qaContainer) {
    const hasInstr = card.qa_instructions && card.qa_instructions.trim();
    const hasItems = card.qa_items && card.qa_items.length > 0;
    if (!hasInstr && !hasItems) {
      if (qaItemsCountEl) qaItemsCountEl.classList.add('hidden');
      if (qaInstrEl) {
        qaInstrEl.classList.remove('hidden');
        qaInstrEl.innerHTML = `
          <div class="flex items-start gap-2.5">
            <i class="ph ph-warning text-amber-500 mt-0.5 shrink-0 text-base"></i>
            <div class="text-xs text-amber-700 dark:text-amber-200/90 leading-relaxed">
              <span class="font-semibold">No Human QA Test section found in this card.</span><br>
              Ask the dev agent to add a <code class="font-mono text-amber-600 dark:text-amber-300 bg-amber-500/10 px-1 rounded text-[11px]">## Human QA Test</code> section with explicit step-by-step instructions.
            </div>
          </div>
        `;
      }
      if (qaListEl) qaListEl.innerHTML = '';
    } else {
      if (hasItems && qaItemsCountEl) {
        const doneCount = card.qa_items.filter((i) => i.checked).length;
        qaItemsCountEl.textContent = `${doneCount}/${card.qa_items.length} done`;
        qaItemsCountEl.classList.remove('hidden');
      } else if (qaItemsCountEl) {
        qaItemsCountEl.classList.add('hidden');
      }
      if (qaInstrEl) {
        if (hasInstr) {
          qaInstrEl.innerHTML = renderMarkdown(card.qa_instructions);
          qaInstrEl.classList.remove('hidden');
        } else {
          qaInstrEl.classList.add('hidden');
        }
      }
      if (qaListEl) {
        if (hasItems) {
          qaListEl.innerHTML = card.qa_items.map((item, idx) => `
            <label class="flex items-start space-x-3 p-2.5 rounded-lg bg-[var(--bg-surface-secondary)] border border-[var(--border-subtle)] hover:border-amber-500/40 cursor-pointer text-sm transition touch-target-item">
              <input type="checkbox" ${item.checked ? 'checked' : ''} data-action="toggle-qa" data-card-id="${escId}" data-idx="${idx}" class="mt-0.5 w-4 h-4 rounded border-amber-600/50 text-amber-500 focus:ring-amber-500/20 bg-[var(--bg-surface)] cursor-pointer">
              <span class="${item.checked ? 'text-emerald-500 font-medium' : 'text-[var(--text-primary)] font-medium'} text-sm leading-relaxed">${formatRichText(item.text)}</span>
            </label>
          `).join('');
        } else {
          qaListEl.innerHTML = '';
        }
      }
    }
  }

  // Footer: Validate button binding
  const validateBtn = document.getElementById('qaDrawerValidateBtn');
  if (validateBtn) {
    validateBtn.setAttribute('data-card-id', card.mn_id);
    validateBtn.disabled = false;
    validateBtn.classList.remove('opacity-50', 'pointer-events-none', 'cursor-not-allowed');
    validateBtn.innerHTML = `<i class="ph ph-checks text-sm"></i><span>Validate &amp; Complete</span>`;
  }

  // Restore preserved notes and scroll
  if (notesInput) {
    notesInput.value = preservedNote;
  }
  if (bodyEl) {
    if (preserveState && prevScrollTop > 0) {
      bodyEl.scrollTop = prevScrollTop;
    } else {
      bodyEl.scrollTop = 0;
    }
  }

  // Slide-over drawer animation
  drawer.classList.remove('translate-x-full', 'pointer-events-none');
  drawer.classList.add('translate-x-0');
  if (backdrop) {
    backdrop.classList.remove('opacity-0', 'pointer-events-none');
    backdrop.classList.add('opacity-100');
  }

  if (window.lucide) lucide.createIcons();
}

function openQaDrawerById(cardId) {
  const card = (state.cards || []).find((c) => (c.mn_id || c.id) === cardId);
  if (card) openQaDrawer(card);
}

function closeQaDrawer() {
  const drawer = document.getElementById('qaCardDrawer');
  const backdrop = document.getElementById('qaDrawerBackdrop');
  if (drawer) {
    drawer.classList.remove('translate-x-0');
    drawer.classList.add('translate-x-full', 'pointer-events-none');
  }
  if (backdrop) {
    backdrop.classList.remove('opacity-100');
    backdrop.classList.add('opacity-0', 'pointer-events-none');
  }
}

function onQaDrawerViewSpec() {
  const sprintEl = document.getElementById('qaDrawerSprint');
  const sprintText = sprintEl ? sprintEl.textContent.replace(/^(?:Sprint|Story)\s+/i, '').trim() : '';
  if (sprintText && sprintText !== 'Unassigned') {
    closeQaDrawer();
    openSprintDoc(sprintText);
  }
}

function onQaDrawerViewReviews() {
  const cardId = document.getElementById('qaDrawerId') ? document.getElementById('qaDrawerId').textContent.trim() : '';
  const sprintEl = document.getElementById('qaDrawerSprint');
  const sprintText = sprintEl ? sprintEl.textContent.replace(/^(?:Sprint|Story)\s+/i, '').trim() : '';
  const target = cardId || sprintText;
  if (target) {
    openReviewModal(target);
  }
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
      const qaDrawer = document.getElementById('qaCardDrawer');
      if (qaDrawer && !qaDrawer.classList.contains('translate-x-full')) {
        const updatedCard = state.cards.find((c) => c.mn_id === mn_id);
        if (updatedCard) openQaDrawer(updatedCard, { preserveState: true });
      }
    } else {
      showToast(`Error updating DoD: ${res.error || 'Server error'}`, 'error');
      await fetchAll();
    }
  } catch (err) {
    console.error('Failed to toggle DoD item:', err);
    showToast('Failed to communicate with server.', 'error');
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
      const qaDrawer = document.getElementById('qaCardDrawer');
      if (qaDrawer && !qaDrawer.classList.contains('translate-x-full')) {
        const updatedCard = state.cards.find((c) => c.mn_id === mn_id);
        if (updatedCard) openQaDrawer(updatedCard, { preserveState: true });
      }
    } else {
      showToast(`Error updating QA item: ${res.error || 'Server error'}`, 'error');
      await fetchAll();
    }
  } catch (err) {
    console.error('Failed to toggle QA item:', err);
    showToast('Failed to communicate with server.', 'error');
    await fetchAll();
  }
}

let isCardValidationInFlight = false;

async function validateCard(mn_id) {
  if (isCardValidationInFlight) return;

  const btn = document.querySelector(`button[data-action="validate-card"][data-card-id="${mn_id}"]`) || document.getElementById('qaDrawerValidateBtn') || document.getElementById('qaValidateCardBtn');
  const originalHtml = btn ? btn.innerHTML : '';

  if (btn) {
    btn.disabled = true;
    btn.classList.add('opacity-50', 'pointer-events-none', 'cursor-not-allowed');
    btn.innerHTML = `<i class="ph ph-circle-notch animate-spin text-base"></i><span>Validating...</span>`;
  }
  isCardValidationInFlight = true;

  const notesInput = document.getElementById('qaDrawerValidateNotes') || document.getElementById('qaValidateNotes');
  const notes = notesInput ? notesInput.value : '';

  try {
    const res = await fetch(`/api/cards/${mn_id}/validate`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ notes, commit: true }),
    }).then((r) => r.json());

    if (res.success) {
      showToast(res.message, 'success');
      playChime('completed');
      if (navigator.vibrate) navigator.vibrate([15, 30, 20]);
      closeQaDrawer();
      isCardValidationInFlight = false;
      await fetchAll();
      renderQaStation();
      renderPipeline();
    } else {
      showToast(`Error: ${res.error || 'Failed to validate card'}`, 'error');
      if (btn) {
        btn.disabled = false;
        btn.classList.remove('opacity-50', 'pointer-events-none', 'cursor-not-allowed');
        btn.innerHTML = originalHtml;
      }
      isCardValidationInFlight = false;
    }
  } catch (err) {
    console.error('Failed to validate card:', err);
    showToast('Failed to validate card.', 'error');
    if (btn) {
      btn.disabled = false;
      btn.classList.remove('opacity-50', 'pointer-events-none', 'cursor-not-allowed');
      btn.innerHTML = originalHtml;
    }
    isCardValidationInFlight = false;
  }
}

// -----------------------------------------------------------------------------
// Sprint Roadmap & Decisions
// -----------------------------------------------------------------------------
// -----------------------------------------------------------------------------
// Sprint Roadmap & Decisions (Sorting & Filtering)
// -----------------------------------------------------------------------------
function parseSprintTokens(sprintStr) {
  if (sprintStr === null || sprintStr === undefined) return { type: 0, tokens: [] };
  const s = String(sprintStr).trim();
  if (!s) return { type: 0, tokens: [] };
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

function clearPipelineSprintFilter() {
  state.filters.sprint = 'ALL';
  state.filters.type = 'ALL';
  state.filters.search = '';
  state.filters.tags = Object.create(null);
  const searchInput = document.getElementById('pipelineSearch');
  if (searchInput) searchInput.value = '';
  const typeSelectEl = document.getElementById('pipelineTypeSelect');
  if (typeSelectEl) typeSelectEl.value = 'ALL';
  document.querySelectorAll('#pipelineTypeFilters .type-filter-pill, #pipelineTypeFilters .segmented-control-btn').forEach((p) => {
    p.classList.toggle('active', p.getAttribute('data-type') === 'ALL');
  });
  const filterSelect = document.getElementById('pipelineSprintFilter') || document.getElementById('filterSprint');
  if (filterSelect) filterSelect.value = 'ALL';
  renderPipeline();
}
window.clearPipelineSprintFilter = clearPipelineSprintFilter;

function filterBoardBySprint(sprintNum) {
  state.filters.sprint = sprintNum;
  const filterSelect = document.getElementById('pipelineSprintFilter') || document.getElementById('filterSprint');
  if (filterSelect) filterSelect.value = sprintNum;
  switchTab('pipeline');
  renderPipeline();
}

function getStoryStatusInfo(sprint, cards) {
  const sName = (sprint.sprint || '').toLowerCase();
  const isBacklog = sName.includes('backlog') || sName.includes('unscheduled');
  const sprintIdNum = sprint.sprint.replace(/^(?:Sprint|Story|Phase)\s+/i, '').trim();
  const assignedCards = (cards || []).filter((c) => c.sprint === sprintIdNum || c.sprint === sprint.sprint);

  if (assignedCards.length > 0) {
    const allCompleted = assignedCards.every((c) => c.status === 'completed');
    const hasQa = assignedCards.some((c) => c.status === 'qa');
    const hasInProgress = assignedCards.some((c) => c.status === 'in-progress');
    const hasStoryBacklog = assignedCards.some((c) => c.status === 'story-backlog' || c.status === 'sprint-backlog');

    if (allCompleted) {
      return {
        status: 'COMPLETE',
        badge: '<span class="badge-status-complete">Complete ✅</span>',
        assignedCards,
      };
    }
    if (hasQa) {
      return {
        status: 'ACTIVE',
        badge: '<span class="badge-status-qa">Built / In QA</span>',
        assignedCards,
      };
    }
    if (hasInProgress) {
      return {
        status: 'ACTIVE',
        badge: '<span class="badge-status-in-progress">In Progress</span>',
        assignedCards,
      };
    }
    if (hasStoryBacklog) {
      return {
        status: 'PLANNED',
        badge: '<span class="text-[10px] font-semibold px-2 py-0.5 rounded bg-blue-500/10 text-blue-600 dark:text-blue-400 border border-blue-500/20">Planned</span>',
        assignedCards,
      };
    }
    return {
      status: 'PLANNED',
      badge: '<span class="text-[10px] font-semibold px-2 py-0.5 rounded bg-blue-500/10 text-blue-600 dark:text-blue-400 border border-blue-500/20">Planned</span>',
      assignedCards,
    };
  }

  // Fallback to PLAN.md metadata when no cards exist for this story
  if (sprint.is_complete) {
    return {
      status: 'COMPLETE',
      badge: '<span class="badge-status-complete">Complete ✅</span>',
      assignedCards,
    };
  }
  if (sprint.is_built) {
    return {
      status: 'ACTIVE',
      badge: '<span class="badge-status-qa">Built / In QA</span>',
      assignedCards,
    };
  }
  if (isBacklog) {
    return {
      status: 'BACKLOG',
      badge: '<span class="text-[10px] font-semibold px-2 py-0.5 rounded bg-[var(--bg-surface-secondary)] text-[var(--text-secondary)] border border-[var(--border-subtle)]">Backlog</span>',
      assignedCards,
    };
  }
  if (sprint.is_partial) {
    return {
      status: 'ACTIVE',
      badge: '<span class="text-[10px] font-semibold px-2 py-0.5 rounded bg-purple-500/10 text-purple-600 dark:text-purple-400 border border-purple-500/20">Partial ⚠️</span>',
      assignedCards,
    };
  }
  return {
    status: 'NEXT_TO_DEFINE',
    badge: '<span class="badge-next-to-define"><i class="ph ph-sparkle text-xs mr-1 inline"></i>Next to Define</span>',
    assignedCards,
  };
}

function renderRoadmap() {
  const table = document.getElementById('sprintRoadmapTable');
  const mobileList = document.getElementById('sprintRoadmapMobileList');
  if (!table && !mobileList) return;
  if (table) table.innerHTML = '';
  if (mobileList) mobileList.innerHTML = '';

  // 1. Calculate filter counts
  let countAll = state.sprints.length;
  let countActive = 0;
  let countPlanned = 0;
  let countNextToDefine = 0;
  let countComplete = 0;
  let countBacklog = 0;

  state.sprints.forEach((s) => {
    const info = getStoryStatusInfo(s, state.cards);
    if (info.status === 'COMPLETE') countComplete++;
    else if (info.status === 'ACTIVE') countActive++;
    else if (info.status === 'PLANNED') countPlanned++;
    else if (info.status === 'NEXT_TO_DEFINE') countNextToDefine++;
    else if (info.status === 'BACKLOG') countBacklog++;
    else if (s.sprint && (s.sprint.toLowerCase().includes('backlog') || s.sprint.toLowerCase().includes('unscheduled'))) countBacklog++;
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
    const info = getStoryStatusInfo(sprint, state.cards);
    const sName = (sprint.sprint || '').toLowerCase();
    const isBacklog = sName.includes('backlog') || sName.includes('unscheduled');

    // Status filter
    if (state.roadmapFilter === 'ACTIVE') {
      if (info.status !== 'ACTIVE') return false;
    } else if (state.roadmapFilter === 'PLANNED') {
      if (info.status !== 'PLANNED') return false;
    } else if (state.roadmapFilter === 'NEXT_TO_DEFINE') {
      if (info.status !== 'NEXT_TO_DEFINE') return false;
    } else if (state.roadmapFilter === 'COMPLETE') {
      if (info.status !== 'COMPLETE') return false;
    } else if (state.roadmapFilter === 'BACKLOG') {
      if (info.status !== 'BACKLOG' && !isBacklog) return false;
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
    if (table) {
      const tr = document.createElement('tr');
      tr.innerHTML = `
        <td colspan="4" class="py-8 text-center text-[var(--text-tertiary)] text-xs">
          <i class="ph ph-funnel text-xl mx-auto mb-2 text-[var(--text-quaternary)] block"></i>
          No stories match the current filter or search criteria.
        </td>
      `;
      table.appendChild(tr);
    }
    if (mobileList) {
      mobileList.innerHTML = `
        <div class="py-8 text-center text-[var(--text-tertiary)] text-xs">
          <i class="ph ph-funnel text-xl mx-auto mb-2 text-[var(--text-quaternary)] block"></i>
          No stories match the current filter or search criteria.
        </div>
      `;
    }
    if (window.lucide) lucide.createIcons();
    return;
  }

  // 4. Render sorted rows
  sorted.forEach((sprint) => {
    const info = getStoryStatusInfo(sprint, state.cards);
    const badge = info.badge;
    const escSprint = escapeHtml(sprint.sprint);
    const escScope = escapeHtml(sprint.scope);

    // Desktop table row (clean 4-column layout: Story, Scope & Objectives, Status, Spec / Doc)
    if (table) {
      const tr = document.createElement('tr');
      tr.className = 'hover:bg-[var(--bg-surface-secondary)] transition-colors';
      tr.innerHTML = `
        <td class="py-3 px-4 font-bold text-[var(--text-primary)] whitespace-nowrap">${escapeHtml(storyLabel(sprint.sprint))}</td>
        <td class="py-3 px-4 text-[var(--text-secondary)] max-w-md leading-relaxed">${escScope}</td>
        <td class="py-3 px-4 whitespace-nowrap">${badge}</td>
        <td class="py-3 px-4 text-right whitespace-nowrap">
          <button data-action="open-sprint-doc" data-sprint="${escSprint}" class="inline-flex items-center gap-1.5 px-2.5 py-1 rounded-lg bg-[var(--bg-surface-secondary)] hover:bg-[var(--bg-surface-tertiary)] text-[var(--text-primary)] border border-[var(--border-medium)] transition text-xs font-medium shadow-xs group cursor-pointer">
            <i class="ph ph-book-open text-emerald-500 group-hover:scale-110 transition-transform text-xs"></i>
            <span>View Spec</span>
          </button>
        </td>
      `;
      table.appendChild(tr);
    }

    // Mobile responsive card (< 768px)
    if (mobileList) {
      const cardDiv = document.createElement('div');
      cardDiv.className = 'p-3.5 rounded-xl bg-[var(--bg-surface-secondary)] border border-[var(--border-subtle)] space-y-2.5';
      cardDiv.innerHTML = `
        <div class="flex items-center justify-between gap-2 flex-wrap">
          <div class="flex items-center gap-2">
            <span class="font-bold text-sm text-[var(--text-primary)]">${escapeHtml(storyLabel(sprint.sprint))}</span>
            ${badge}
          </div>
          <button data-action="open-sprint-doc" data-sprint="${escSprint}" class="inline-flex items-center gap-1.5 px-2.5 py-1 rounded-lg bg-[var(--bg-surface)] hover:bg-[var(--bg-surface-tertiary)] text-[var(--text-primary)] border border-[var(--border-medium)] transition text-xs font-medium shadow-xs group cursor-pointer">
            <i class="ph ph-book-open text-emerald-500 group-hover:scale-110 transition-transform text-xs"></i>
            <span>View Spec</span>
          </button>
        </div>
        <div class="text-[var(--text-secondary)] text-xs leading-relaxed">
          ${escScope}
        </div>
      `;
      mobileList.appendChild(cardDiv);
    }
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
  const commitBtn = document.getElementById('activityCommitBtn');

  const modified = state.activity.modified_files || [];
  fileCount.textContent = `${modified.length} files`;

  if (commitBtn) {
    if (modified.length > 0) {
      commitBtn.classList.remove('hidden');
    } else {
      commitBtn.classList.add('hidden');
    }
  }

  if (modified.length === 0) {
    fileList.innerHTML = '<p class="text-[var(--text-tertiary)] italic">Working directory is clean.</p>';
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

        let statusColor = 'text-amber-500';
        if (status === '??') statusColor = 'text-sky-500';
        else if (status.includes('D')) statusColor = 'text-rose-500';
        else if (status.includes('A')) statusColor = 'text-emerald-500';
        else if (status.includes('R')) statusColor = 'text-purple-500';

        return `
          <div class="flex items-center space-x-2 py-1.5 border-b border-[var(--border-subtle)] font-mono text-xs">
            <span class="${statusColor} font-bold w-6 shrink-0">${escapeHtml(status)}</span>
            <span class="text-[var(--text-primary)] font-medium truncate" title="${escapeHtml(path)}">${escapeHtml(path)}</span>
          </div>
        `;
      })
      .join('');
  }

  renderGitSyncBar();

  const commits = state.activity.commits || [];
  commitList.innerHTML = commits
    .map(
      (c) => `
    <div class="p-3 bg-[var(--bg-surface-secondary)] rounded-xl border border-[var(--border-subtle)] hover:border-[var(--border-medium)] text-xs shadow-2xs transition">
      <div class="flex items-center justify-between mb-1 font-mono text-[10px] text-[var(--text-tertiary)]">
        <span class="text-emerald-600 dark:text-emerald-400 font-bold">${escapeHtml(c.sha)}</span>
        <span class="flex items-center gap-1 text-[var(--text-secondary)]"><i class="ph ph-clock text-[var(--text-quaternary)] text-xs"></i>${escapeHtml(c.date)}</span>
      </div>
      <p class="font-medium text-[var(--text-primary)]">${escapeHtml(c.message)}</p>
      <div class="mt-1 text-[10px] text-[var(--text-tertiary)]">${escapeHtml(c.author)}</div>
    </div>
  `
    )
    .join('');
  if (window.lucide) lucide.createIcons();
}

function renderGitSyncBar() {
  const sync = state.gitSync || {};
  const branchEl = document.getElementById('gitSyncBranch');
  const badgeEl = document.getElementById('gitSyncStatusBadge');
  const pushBtn = document.getElementById('gitPushBtn');
  const pullBtn = document.getElementById('gitPullBtn');

  if (branchEl) {
    branchEl.textContent = sync.branch || 'main';
  }

  const ahead = sync.ahead_count || 0;
  const behind = sync.behind_count || 0;

  if (badgeEl) {
    if (ahead > 0 && behind > 0) {
      badgeEl.textContent = `Ahead ${ahead} • Behind ${behind}`;
      badgeEl.className = 'font-mono text-[10px] px-2 py-0.5 rounded font-semibold bg-amber-500/15 text-amber-600 dark:text-amber-400 border border-amber-500/30';
      badgeEl.classList.remove('hidden');
    } else if (ahead > 0) {
      badgeEl.textContent = `Ahead by ${ahead}`;
      badgeEl.className = 'font-mono text-[10px] px-2 py-0.5 rounded font-semibold bg-emerald-500/15 text-emerald-600 dark:text-emerald-400 border border-emerald-500/30';
      badgeEl.classList.remove('hidden');
    } else if (behind > 0) {
      badgeEl.textContent = `Behind by ${behind}`;
      badgeEl.className = 'font-mono text-[10px] px-2 py-0.5 rounded font-semibold bg-blue-500/15 text-blue-600 dark:text-blue-400 border border-blue-500/30';
      badgeEl.classList.remove('hidden');
    } else if (sync.branch) {
      badgeEl.textContent = 'Up to date';
      badgeEl.className = 'font-mono text-[10px] px-2 py-0.5 rounded font-medium bg-[var(--bg-surface-secondary)] text-[var(--text-tertiary)] border border-[var(--border-subtle)]';
      badgeEl.classList.remove('hidden');
    } else {
      badgeEl.classList.add('hidden');
    }
  }

  if (pushBtn) {
    if (ahead > 0) {
      pushBtn.classList.remove('hidden');
      pushBtn.innerHTML = `<i class="ph ph-arrow-up text-xs"></i><span>Push (${ahead})</span>`;
    } else {
      pushBtn.classList.add('hidden');
    }
  }

  if (pullBtn) {
    if (behind > 0) {
      pullBtn.classList.remove('hidden');
      pullBtn.innerHTML = `<i class="ph ph-arrow-down text-xs"></i><span>Pull (${behind})</span>`;
    } else {
      pullBtn.classList.add('hidden');
    }
  }
}

function openCommitModal() {
  const modal = document.getElementById('gitCommitModal');
  const fileList = document.getElementById('gitCommitFileList');
  const msgInput = document.getElementById('gitCommitMessageInput');
  const selectAll = document.getElementById('gitCommitSelectAll');
  if (!modal || !fileList) return;

  const modified = state.activity.modified_files || [];
  if (modified.length === 0) {
    showToast('No uncommitted changes to commit.', 'info');
    return;
  }

  if (selectAll) selectAll.checked = true;
  if (msgInput) msgInput.value = '';

  fileList.innerHTML = modified.map((f) => {
    let status = '';
    let path = '';
    if (typeof f === 'string') {
      if (f.length >= 4 && f.charAt(2) === ' ') {
        status = f.slice(0, 2).trim();
        path = f.slice(3).trim();
      } else {
        const firstSpace = f.indexOf(' ');
        status = firstSpace !== -1 ? f.slice(0, firstSpace).trim() : f.trim();
        path = firstSpace !== -1 ? f.slice(firstSpace + 1).trim() : '';
      }
    } else if (f && typeof f === 'object') {
      status = f.status || '';
      path = f.path || '';
    }
    if (path.startsWith('"') && path.endsWith('"') && path.length >= 2) {
      path = path.slice(1, -1);
    }

    let statusColor = 'text-amber-500';
    if (status === '??') statusColor = 'text-sky-500';
    else if (status.includes('D')) statusColor = 'text-rose-500';
    else if (status.includes('A')) statusColor = 'text-emerald-500';

    return `
      <label class="flex items-center space-x-2.5 py-1.5 px-2 rounded-lg hover:bg-[var(--bg-surface-tertiary)] cursor-pointer">
        <input type="checkbox" checked data-commit-file="${escapeHtml(path)}" onchange="updateCommitSelectedCount()" class="commit-file-checkbox rounded border-[var(--border-medium)] text-emerald-600 focus:ring-emerald-500/20 cursor-pointer">
        <span class="${statusColor} font-bold w-5 shrink-0 text-[10px]">${escapeHtml(status)}</span>
        <span class="text-[var(--text-primary)] truncate font-medium flex-1 text-[11px]" title="${escapeHtml(path)}">${escapeHtml(path)}</span>
      </label>
    `;
  }).join('');

  updateCommitSelectedCount();
  modal.classList.remove('hidden');
  if (msgInput) msgInput.focus();
}

function closeCommitModal() {
  const modal = document.getElementById('gitCommitModal');
  if (modal) modal.classList.add('hidden');
}

function toggleCommitSelectAll(checked) {
  const checkboxes = document.querySelectorAll('.commit-file-checkbox');
  checkboxes.forEach((cb) => { cb.checked = checked; });
  updateCommitSelectedCount();
}

function updateCommitSelectedCount() {
  const checkboxes = Array.from(document.querySelectorAll('.commit-file-checkbox'));
  const checked = checkboxes.filter((cb) => cb.checked);
  const countEl = document.getElementById('gitCommitSelectedCount');
  const selectAll = document.getElementById('gitCommitSelectAll');
  const submitBtn = document.getElementById('gitCommitSubmitBtn');

  if (countEl) {
    countEl.textContent = `${checked.length} of ${checkboxes.length} selected`;
  }
  if (selectAll) {
    selectAll.checked = checked.length === checkboxes.length && checkboxes.length > 0;
    selectAll.indeterminate = checked.length > 0 && checked.length < checkboxes.length;
  }
  if (submitBtn) {
    submitBtn.disabled = checked.length === 0;
    submitBtn.classList.toggle('opacity-50', checked.length === 0);
  }
}

async function submitGitCommit() {
  const msgInput = document.getElementById('gitCommitMessageInput');
  const message = (msgInput ? msgInput.value : '').trim();
  if (!message) {
    showToast('Please enter a commit message.', 'warning');
    if (msgInput) msgInput.focus();
    return;
  }

  const checkboxes = Array.from(document.querySelectorAll('.commit-file-checkbox'));
  const checkedFiles = checkboxes.filter((cb) => cb.checked).map((cb) => cb.getAttribute('data-commit-file'));

  if (checkedFiles.length === 0) {
    showToast('Please select at least one file to commit.', 'warning');
    return;
  }

  const submitBtn = document.getElementById('gitCommitSubmitBtn');
  if (submitBtn) {
    submitBtn.disabled = true;
    submitBtn.innerHTML = '<i class="ph ph-circle-notch animate-spin text-xs"></i><span>Committing...</span>';
  }

  try {
    const res = await fetch('/api/git/commit', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ message, files: checkedFiles }),
    });
    const data = await res.json();
    if (!res.ok || !data.success) {
      throw new Error(data.error || 'Commit failed');
    }

    const shortSha = data.sha ? data.sha.slice(0, 7) : '';
    showToast(`Committed ${shortSha}: ${message.slice(0, 40)}`, 'success');
    closeCommitModal();
    await fetchActivityData();
  } catch (err) {
    showToast(err.message || 'Failed to commit changes.', 'error');
  } finally {
    if (submitBtn) {
      submitBtn.disabled = false;
      submitBtn.innerHTML = '<i class="ph ph-check text-xs"></i><span>Commit Changes</span>';
    }
  }
}

function promptGitPush() {
  const sync = state.gitSync || {};
  const ahead = sync.ahead_count || 1;
  const branch = sync.branch || 'main';

  const modal = document.getElementById('gitConfirmModal');
  const heading = document.getElementById('gitConfirmModalHeading');
  const msg = document.getElementById('gitConfirmModalMessage');
  const btn = document.getElementById('gitConfirmActionBtn');

  if (heading) heading.textContent = 'Push Commits';
  if (msg) msg.textContent = `Are you sure you want to push ${ahead} commit(s) on branch '${branch}' to remote?`;
  if (btn) {
    btn.textContent = 'Push Commits';
    btn.className = 'px-3.5 py-1.5 rounded-lg bg-emerald-600 hover:bg-emerald-500 text-white font-semibold shadow-xs transition text-xs cursor-pointer flex items-center gap-1.5';
    btn.onclick = executeGitPush;
  }
  if (modal) modal.classList.remove('hidden');
}

function promptGitPull() {
  const sync = state.gitSync || {};
  const behind = sync.behind_count || 1;
  const branch = sync.branch || 'main';

  const modal = document.getElementById('gitConfirmModal');
  const heading = document.getElementById('gitConfirmModalHeading');
  const msg = document.getElementById('gitConfirmModalMessage');
  const btn = document.getElementById('gitConfirmActionBtn');

  if (heading) heading.textContent = 'Pull Updates';
  if (msg) msg.textContent = `Are you sure you want to pull ${behind} update(s) for branch '${branch}' from remote?`;
  if (btn) {
    btn.textContent = 'Pull Updates';
    btn.className = 'px-3.5 py-1.5 rounded-lg bg-blue-600 hover:bg-blue-500 text-white font-semibold shadow-xs transition text-xs cursor-pointer flex items-center gap-1.5';
    btn.onclick = executeGitPull;
  }
  if (modal) modal.classList.remove('hidden');
}

function closeGitConfirmModal() {
  const modal = document.getElementById('gitConfirmModal');
  if (modal) modal.classList.add('hidden');
}

async function executeGitPush() {
  closeGitConfirmModal();
  showToast('Pushing commits to remote...', 'info');
  try {
    const res = await fetch('/api/git/push', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({}),
    });
    const data = await res.json();
    if (!res.ok || !data.success) {
      throw new Error(data.error || 'Push failed');
    }
    showToast('Commits pushed to remote successfully!', 'success');
    await fetchActivityData();
  } catch (err) {
    showToast(err.message || 'Failed to push commits.', 'error');
  }
}

async function executeGitPull() {
  closeGitConfirmModal();
  showToast('Pulling updates from remote...', 'info');
  try {
    const res = await fetch('/api/git/pull', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({}),
    });
    const data = await res.json();
    if (!res.ok || !data.success) {
      throw new Error(data.error || 'Pull failed');
    }
    showToast('Updates pulled successfully!', 'success');
    await fetchActivityData();
  } catch (err) {
    showToast(err.message || 'Failed to pull updates.', 'error');
  }
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
    'analytics': 'Analytics & Trends',
  };
  const titleEl = document.getElementById('viewTitle');
  if (titleEl && viewTitles[tabId]) {
    titleEl.textContent = viewTitles[tabId];
  }

  if (typeof closeMobileSidebar === 'function') {
    closeMobileSidebar();
  }

  if (tabId === 'qa' && state.cards && state.cards.length > 0) {
    try { renderQaStation(); } catch (_) {}
  }

  fetchCurrentViewData().catch((e) => console.warn('fetchCurrentViewData tab switch error:', e));
  scheduleNextPoll(10000);

  if (window.lucide) lucide.createIcons();
}

// -----------------------------------------------------------------------------
// Active Sprint & Live DoD Tracker
// -----------------------------------------------------------------------------
async function fetchActiveSprint(targetSprint = null) {
  return await fetchActiveSprintData(targetSprint);
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
              ? '<span class="text-xs px-2.5 py-0.5 rounded-full font-semibold bg-purple-500/15 text-purple-600 dark:text-purple-300 border border-purple-500/30 flex items-center gap-1"><i class="ph ph-flame text-sm text-purple-500"></i>Design Grill</span>'
              : '<span class="badge-status-building text-xs px-2.5 py-0.5 flex items-center gap-1"><span class="w-2 h-2 rounded-full bg-amber-500 animate-pulse"></span>🔨 Building...</span>';

            const sprintBadgeHtml = `<span class="text-xs px-2 py-0.5 rounded bg-[var(--bg-surface-secondary)] text-[var(--text-secondary)] border border-[var(--border-subtle)] font-mono font-medium">Story ${escSprint}</span>`;

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
              durationBadgeHtml = `<span class="text-xs px-2.5 py-0.5 rounded-full font-medium bg-purple-500/10 text-purple-600 dark:text-purple-300 border border-purple-500/30 flex items-center gap-1 font-mono" title="${escapeHtml(timeRange)}">${escapeHtml(durText)}</span>`;
            }

            // Git Commits Proof
            const gitCommitsHtml = (s.git_commits && s.git_commits.length > 0)
              ? `
                <div class="mb-3">
                  <div class="text-[10px] font-semibold text-[var(--text-tertiary)] uppercase tracking-wider mb-1.5 flex items-center justify-between">
                    <span class="flex items-center gap-1"><i class="ph ph-git-branch text-purple-500"></i> Git Commits Proof</span>
                    <span class="text-[9px] text-[var(--text-tertiary)] font-mono">${s.git_commits.length} recent</span>
                  </div>
                  <div class="space-y-1">
                    ${s.git_commits.map((c) => {
                      const commitDt = escapeHtml(c.datetime || (c.date && c.time ? `${c.date} ${c.time}` : c.date) || '');
                      return `
                      <div class="flex items-center gap-2 py-1 px-2 rounded bg-[var(--bg-surface-secondary)] border border-[var(--border-subtle)] text-[11px]">
                        <span class="font-mono text-[10px] text-purple-600 dark:text-purple-400 font-bold bg-purple-500/10 px-1 py-0.2 rounded border border-purple-500/20 shrink-0">${escapeHtml(c.sha)}</span>
                        <span class="text-[var(--text-primary)] truncate flex-1 font-medium text-[11px]" title="${escapeHtml(c.message)}">${escapeHtml(c.message)}</span>
                        <span class="text-[var(--text-tertiary)] font-mono text-[9px] shrink-0" title="${commitDt}">${commitDt}</span>
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
                  <div class="text-[10px] font-semibold text-[var(--text-tertiary)] uppercase tracking-wider mb-1.5 flex items-center gap-1">
                    <i class="ph ph-shield-check text-emerald-500"></i> Review Gate Evidence
                  </div>
                  <div class="space-y-1">
                    ${s.reviews.map((r) => `
                      <div class="flex items-center justify-between py-1 px-2 rounded-lg bg-emerald-500/10 border border-emerald-500/20 text-xs">
                        <div class="flex items-center gap-1.5">
                          <span class="font-bold text-emerald-600 dark:text-emerald-400 text-[11px]">Round ${escapeHtml(String(r.round))}</span>
                          <span class="text-[var(--text-primary)] font-medium text-[11px]">${escapeHtml(r.verdict || 'PASS')}</span>
                          <span class="text-[var(--text-tertiary)] text-[10px]">(${escapeHtml(r.reviewer || 'Reviewer')})</span>
                        </div>
                        <button class="text-purple-600 dark:text-purple-400 hover:underline text-[10px] font-mono cursor-pointer" data-action="open-review-modal" data-sprint="${escapeHtml(r.sprint)}">View Report →</button>
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
                  <div class="flex items-center justify-between text-[10px] text-[var(--text-tertiary)] mb-1">
                    <span class="font-semibold text-purple-600 dark:text-purple-300 flex items-center gap-1">
                      <i class="ph ph-git-commit text-purple-500"></i> ${eAgent}
                    </span>
                    <span class="font-mono text-[9px] text-[var(--text-tertiary)]" title="${eDt}">${eDt}</span>
                  </div>
                  <p class="text-[11px] text-[var(--text-primary)] leading-snug line-clamp-3">${eWhat}</p>
                </div>
              `;
            }).join('');

            // DoD Items
            const dodItemsHtml = (s.dod || [])
              .map((item, idx) => {
                const escText = formatRichText(item.text);
                const itemIdx = item.index !== undefined ? item.index : idx;
                return `
                  <label class="flex items-start space-x-2.5 py-1.5 px-2 rounded-lg hover:bg-[var(--bg-surface-secondary)] cursor-pointer text-xs transition group">
                    <input type="checkbox" ${item.checked ? 'checked' : ''}
                           data-action="toggle-dod"
                           data-card-id="${escId}"
                           data-idx="${itemIdx}"
                           class="mt-0.5 rounded border-[var(--border-medium)] text-purple-500 focus:ring-purple-500/20 bg-[var(--bg-surface)] cursor-pointer w-4 h-4">
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
      statusBadge.textContent = data.status_raw || (data.is_complete ? 'Complete ✅' : 'Building...');
      statusBadge.className = data.is_complete ? 'badge-status-complete' : 'badge-status-building';
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
            <label class="flex items-start space-x-3 p-2.5 rounded-lg bg-[var(--bg-surface-secondary)] hover:bg-[var(--bg-surface-tertiary)] border border-[var(--border-subtle)] hover:border-[var(--border-medium)] cursor-pointer text-xs transition group">
              <input type="checkbox" ${item.checked ? 'checked' : ''} data-action="toggle-sprint-dod" data-sprint-id="${escSprintId}" data-idx="${idx}" class="mt-0.5 rounded border-[var(--border-medium)] text-emerald-500 focus:ring-emerald-500/20 bg-[var(--bg-surface)] cursor-pointer">
              <span class="flex-1 ${item.checked ? 'text-[var(--text-tertiary)] line-through' : 'text-[var(--text-primary)] font-medium'} group-hover:text-emerald-600 dark:group-hover:text-emerald-400 transition-colors">${formatRichText(item.text)}</span>
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
          <div class="p-8 text-center text-[var(--text-tertiary)] text-xs">
            <i class="ph ph-tray text-2xl mx-auto mb-2 text-[var(--text-quaternary)] block"></i>
            No cards assigned to Story ${escapeHtml(data.sprint_id)} yet.
          </div>
        `;
      } else {
        cardsContainerEl.innerHTML = data.cards
          .map((c) => {
            const escId = escapeHtml(c.mn_id);
            const escTitle = escapeHtml(c.title);
            let statusBadge = '<span class="text-[10px] px-2 py-0.5 rounded bg-[var(--bg-surface-secondary)] text-[var(--text-secondary)] border border-[var(--border-subtle)]">Backlog</span>';
            if (c.status === 'in-progress') {
              statusBadge = '<span class="badge-status-in-progress">In Progress</span>';
            } else if (c.status === 'qa') {
              statusBadge = '<span class="badge-status-qa">Awaiting QA</span>';
            } else if (c.status === 'completed') {
              statusBadge = '<span class="badge-status-complete">Done</span>';
            }

            const dodItemsHtml = (c.dod || [])
              .map((item, subIdx) => {
                return `
                <label class="flex items-start space-x-2 py-1 px-1.5 rounded hover:bg-[var(--bg-surface-tertiary)] cursor-pointer text-[11px] transition touch-target-item">
                  <input type="checkbox" ${item.checked ? 'checked' : ''} data-action="toggle-dod" data-card-id="${escId}" data-idx="${subIdx}" class="mt-0.5 rounded border-[var(--border-medium)] text-emerald-500 focus:ring-emerald-500/20 bg-[var(--bg-surface)] cursor-pointer">
                  <span class="${item.checked ? 'text-[var(--text-tertiary)] line-through' : 'text-[var(--text-primary)]'}">${formatRichText(item.text)}</span>
                </label>
              `;
              })
              .join('');

            return `
            <div class="p-3.5 bg-[var(--card-bg)] border border-[var(--card-border)] rounded-xl shadow-[var(--card-shadow)] hover:border-[var(--border-medium)] transition shadow-xs">
              <div class="flex items-center justify-between gap-2 mb-1.5">
                <div class="flex items-center gap-2">
                  <span class="font-mono font-bold text-xs text-emerald-600 dark:text-emerald-400 cursor-pointer hover:underline" data-action="open-card-modal" data-card-id="${escId}">${escId}</span>
                  <h4 class="text-xs font-semibold text-[var(--text-primary)] cursor-pointer hover:text-emerald-500 transition-colors" data-action="open-card-modal" data-card-id="${escId}">${escTitle}</h4>
                </div>
                <div>${statusBadge}</div>
              </div>
              ${c.dod && c.dod.length > 0 ? `
                <div class="mt-2.5 pt-2 border-t border-[var(--border-subtle)] space-y-1">
                  <div class="text-[10px] text-[var(--text-tertiary)] uppercase tracking-wider font-semibold mb-1 flex items-center justify-between">
                    <span>Card DoD Criteria:</span>
                    <span class="font-mono text-emerald-600 dark:text-emerald-400">${c.dod_checked}/${c.dod_total}</span>
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
      showToast(`Error updating Story DoD: ${res.error || 'Server error'}`, 'error');
    }
  } catch (err) {
    console.error('Failed to toggle sprint DoD:', err);
    showToast('Failed to update Story DoD.', 'error');
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
    if (res.gates) state.projectGates = res.gates;

    const prevStatus = lastKnownHeavyStatus;
    const currentStatus = state.heavyGateWorker.status || 'idle';
    lastKnownHeavyStatus = currentStatus;

    updateHeavyGateUi();

    // If transitioned from running to finished (completed or failed), refresh the sprint audit matrix and logs
    if (prevStatus === 'running' && currentStatus !== 'running') {
      fetchHeavyGateLog();
      fetchSprintGateStatus();
      const isFast = state.projectGates && state.projectGates.mode === 'fast';
      if (isFast) {
        const suiteBtn = document.getElementById('runTestSuiteBtn');
        if (suiteBtn) {
          suiteBtn.disabled = false;
          const span = suiteBtn.querySelector('span');
          if (span) span.textContent = 'Run Test Suite';
        }
        const fastInd = document.getElementById('fastTerminalIndicator');
        if (fastInd) {
          fastInd.className = currentStatus === 'completed'
            ? 'w-2.5 h-2.5 rounded-full bg-emerald-500 inline-block'
            : 'w-2.5 h-2.5 rounded-full bg-rose-500 inline-block';
        }
        if (currentStatus === 'completed') {
          showToast('Test suite run completed successfully! ✅', 'success');
        } else if (currentStatus === 'failed') {
          showToast('Test suite run finished with failures ❌', 'error');
        }
      }
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
  const isFastMode = state.projectGates && state.projectGates.mode === 'fast';

  const heavySection = document.getElementById('heavyGateRunnerSection');
  const runTestSuiteBtn = document.getElementById('runTestSuiteBtn');
  const runAllPendingBtn = document.getElementById('runAllPendingSprintsBtn');
  const gateStatusHeader = document.getElementById('qaGateStatusHeader');
  const subtitleEl = document.getElementById('sprintAuditSubtitle');

  if (isFastMode) {
    if (heavySection) heavySection.classList.add('hidden');
    if (runTestSuiteBtn) runTestSuiteBtn.classList.remove('hidden');
    if (runAllPendingBtn) runAllPendingBtn.classList.add('hidden');
    if (gateStatusHeader) gateStatusHeader.textContent = 'Gate Status';
    if (subtitleEl) subtitleEl.innerHTML = 'Audit past stories and view inline test suite gate metrics recorded in <code class="font-mono text-[10px] text-emerald-500">docs/QUALITY.md</code>.';
  } else {
    if (heavySection) heavySection.classList.remove('hidden');
    if (runTestSuiteBtn) runTestSuiteBtn.classList.add('hidden');
    if (runAllPendingBtn) runAllPendingBtn.classList.remove('hidden');
    if (gateStatusHeader) gateStatusHeader.textContent = 'Gate 5 Status';
    if (subtitleEl) subtitleEl.innerHTML = 'Audit past stories in isolated detached worktrees (<code class="font-mono text-[10px]">.gate-worktrees/</code>) and auto-record metrics to <code class="font-mono text-[10px] text-emerald-500">docs/QUALITY.md</code>.';
  }

  const badgeEl = document.getElementById('heavyGateStatusBadge');
  const timerEl = document.getElementById('heavyGateTimer');
  const fastTimerEl = document.getElementById('fastGateTimer');
  const runBtn = document.getElementById('runHeavyGateBtn');
  const stopBtn = document.getElementById('stopHeavyGateBtn');
  const indicator = document.getElementById('terminalIndicator');
  const fastIndicator = document.getElementById('fastTerminalIndicator');

  // Status & timer
  if (worker.status === 'running') {
    const elapsed = Math.floor(worker.elapsed_seconds || 0);
    const mins = String(Math.floor(elapsed / 60)).padStart(2, '0');
    const secs = String(elapsed % 60).padStart(2, '0');
    const timeStr = `${mins}:${secs}`;

    if (badgeEl) {
      badgeEl.textContent = 'Running ⚡';
      badgeEl.className = 'text-[10px] px-2 py-0.5 rounded-full font-bold bg-amber-500/20 text-amber-300 border border-amber-500/40 animate-pulse';
    }
    if (timerEl) {
      timerEl.classList.remove('hidden');
      timerEl.textContent = timeStr;
    }
    if (fastTimerEl) {
      fastTimerEl.classList.remove('hidden');
      fastTimerEl.textContent = timeStr;
    }
    if (runBtn) {
      runBtn.disabled = true;
      const span = runBtn.querySelector('span');
      if (span) span.textContent = 'Running...';
    }
    if (runTestSuiteBtn) {
      runTestSuiteBtn.disabled = true;
      const span = runTestSuiteBtn.querySelector('span');
      if (span) span.textContent = 'Running Suite...';
    }
    if (stopBtn) stopBtn.classList.remove('hidden');
    if (indicator) indicator.className = 'w-2.5 h-2.5 rounded-full bg-amber-400 animate-ping inline-block';
    if (fastIndicator) fastIndicator.className = 'w-2.5 h-2.5 rounded-full bg-amber-400 animate-ping inline-block';
  } else {
    if (timerEl) timerEl.classList.add('hidden');
    if (fastTimerEl) fastTimerEl.classList.add('hidden');
    if (runBtn) {
      runBtn.disabled = false;
      const span = runBtn.querySelector('span');
      if (span) span.textContent = 'Run Heavy Gate';
    }
    if (runTestSuiteBtn) {
      runTestSuiteBtn.disabled = false;
      const span = runTestSuiteBtn.querySelector('span');
      if (span) span.textContent = 'Run Test Suite';
    }
    if (stopBtn) stopBtn.classList.add('hidden');

    if (worker.status === 'completed') {
      if (badgeEl) {
        badgeEl.textContent = 'Passed ✅';
        badgeEl.className = 'text-[10px] px-2 py-0.5 rounded-full font-bold bg-emerald-500/20 text-emerald-300 border border-emerald-500/40';
      }
      if (indicator) indicator.className = 'w-2.5 h-2.5 rounded-full bg-emerald-500 inline-block';
      if (fastIndicator) fastIndicator.className = 'w-2.5 h-2.5 rounded-full bg-emerald-500 inline-block';
    } else if (worker.status === 'failed') {
      if (badgeEl) {
        badgeEl.textContent = 'Failed ❌';
        badgeEl.className = 'text-[10px] px-2 py-0.5 rounded-full font-bold bg-rose-500/20 text-rose-300 border border-rose-500/40';
      }
      if (indicator) indicator.className = 'w-2.5 h-2.5 rounded-full bg-rose-500 inline-block';
      if (fastIndicator) fastIndicator.className = 'w-2.5 h-2.5 rounded-full bg-rose-500 inline-block';
    } else {
      if (badgeEl) {
        badgeEl.textContent = 'Idle';
        badgeEl.className = 'text-[10px] px-2 py-0.5 rounded-full font-semibold bg-[var(--bg-surface-secondary)] text-[var(--text-secondary)] border border-[var(--border-subtle)]';
      }
      if (indicator) indicator.className = 'w-2.5 h-2.5 rounded-full bg-slate-400 dark:bg-slate-600 inline-block';
      if (fastIndicator) fastIndicator.className = 'w-2.5 h-2.5 rounded-full bg-slate-400 dark:bg-slate-600 inline-block';
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
      queueListEl.innerHTML = '<p class="text-[var(--text-tertiary)] italic">No bespoke checks in scripts/heavy-gate-queue/.</p>';
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
        ok ? 'bg-emerald-500/20 text-emerald-600 dark:text-emerald-400 border border-emerald-500/30' : 'bg-rose-500/20 text-rose-600 dark:text-rose-400 border border-rose-500/30'
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
      latestBadge.className = 'text-[10px] px-1.5 py-0.2 rounded font-semibold bg-[var(--bg-surface-secondary)] text-[var(--text-secondary)] border border-[var(--border-subtle)]';
    }
    if (latestSha) latestSha.textContent = 'SHA: --';
    if (latestTime) latestTime.textContent = '--';
  }
}

async function fetchHeavyGateLog() {
  try {
    const res = await fetch('/api/heavy-gate/log?limit=300').then((r) => r.json());
    const terminal = document.getElementById('heavyGateTerminal');
    const fastTerminal = document.getElementById('fastGateTerminal');
    const text = res.lines && res.lines.length > 0 ? res.lines.join('\n') : '';
    if (terminal && text) {
      terminal.textContent = text;
      const autoScroll = document.getElementById('autoScrollTerminal');
      if (autoScroll && autoScroll.checked) {
        terminal.scrollTop = terminal.scrollHeight;
      }
    }
    if (fastTerminal && text) {
      fastTerminal.textContent = text;
      const fastAutoScroll = document.getElementById('fastAutoScrollTerminal');
      if (fastAutoScroll && fastAutoScroll.checked) {
        fastTerminal.scrollTop = fastTerminal.scrollHeight;
      }
    }
  } catch (err) {
    console.error('Failed to fetch heavy gate log:', err);
  }
}

async function startHeavyGate() {
  const runBtn = document.getElementById('runHeavyGateBtn');
  const terminal = document.getElementById('heavyGateTerminal');
  showToast('Heavy Gate 5 started in background', 'info');
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
      showToast(`Error starting heavy gate: ${res.error || 'Server error'}`, 'error');
      if (runBtn) {
        runBtn.disabled = false;
        const span = runBtn.querySelector('span');
        if (span) span.textContent = 'Run Heavy Gate';
      }
    }
  } catch (err) {
    console.error('Failed to start heavy gate:', err);
    showToast('Failed to start heavy gate execution.', 'error');
    if (runBtn) {
      runBtn.disabled = false;
      const span = runBtn.querySelector('span');
      if (span) span.textContent = 'Run Heavy Gate';
    }
  }
}

async function startTestSuiteRun() {
  const btn = document.getElementById('runTestSuiteBtn');
  const drawer = document.getElementById('fastGateDrawer');
  const terminal = document.getElementById('fastGateTerminal');
  const indicator = document.getElementById('fastTerminalIndicator');

  if (drawer) drawer.classList.remove('hidden');
  if (btn) {
    btn.disabled = true;
    const span = btn.querySelector('span');
    if (span) span.textContent = 'Starting Suite...';
  }
  if (terminal) {
    terminal.textContent = '[Starting inline test suite runner in background...]\n[Collecting tests & streaming output...]\n';
  }
  if (indicator) indicator.className = 'w-2.5 h-2.5 rounded-full bg-amber-400 animate-ping inline-block';

  showToast('Test suite execution started', 'info');

  try {
    const res = await fetch('/api/heavy-gate/start', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({}),
    }).then((r) => r.json());

    if (res.success) {
      await fetchHeavyGateStatus();
      await fetchHeavyGateLog();
    } else {
      showToast(`Error starting test suite: ${res.error || 'Server error'}`, 'error');
      if (btn) {
        btn.disabled = false;
        const span = btn.querySelector('span');
        if (span) span.textContent = 'Run Test Suite';
      }
      if (indicator) indicator.className = 'w-2.5 h-2.5 rounded-full bg-rose-500 inline-block';
    }
  } catch (err) {
    console.error('Failed to start test suite:', err);
    showToast('Failed to start test suite execution.', 'error');
    if (btn) {
      btn.disabled = false;
      const span = btn.querySelector('span');
      if (span) span.textContent = 'Run Test Suite';
    }
    if (indicator) indicator.className = 'w-2.5 h-2.5 rounded-full bg-rose-500 inline-block';
  }
}

function toggleFastDrawer() {
  const drawer = document.getElementById('fastGateDrawer');
  if (drawer) drawer.classList.toggle('hidden');
}

function clearFastLog() {
  const terminal = document.getElementById('fastGateTerminal');
  if (terminal) terminal.textContent = '';
}

async function stopHeavyGate() {
  showToast('Heavy gate process termination requested', 'info');
  try {
    const res = await fetch('/api/heavy-gate/stop', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
    }).then((r) => r.json());

    if (res.success) {
      await fetchHeavyGateStatus();
      fetchHeavyGateLog();
    } else {
      showToast(`Error stopping heavy gate: ${res.error || 'Server error'}`, 'error');
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
            <td colspan="6" class="text-center py-6 text-amber-500 text-xs">
              <div class="flex items-center justify-center gap-2">
                <i class="ph ph-warning text-amber-500 text-base"></i>
                <span>Server restart required: endpoint <code class="bg-[var(--bg-surface-tertiary)] text-[var(--status-blue)] px-1.5 py-0.5 rounded border border-[var(--border-medium)] font-mono">/api/heavy-gate/sprints</code> not found. Please restart <code class="bg-[var(--bg-surface-tertiary)] text-[var(--status-blue)] px-1.5 py-0.5 rounded border border-[var(--border-medium)] font-mono">scripts/pm.ps1</code>.</span>
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
    if (data.gates) state.projectGates = data.gates;
    renderSprintAuditTable();
  } catch (err) {
    console.error('Failed to fetch sprint gate audit status:', err);
    const tbody = document.getElementById('sprintAuditTableBody');
    if (tbody) {
      tbody.innerHTML = `
        <tr>
          <td colspan="6" class="text-center py-6 text-rose-500 text-xs">
            <div class="flex items-center justify-center gap-2">
              <i class="ph ph-warning-circle text-rose-500 text-base"></i>
              <span>Failed to fetch story audit status. If server is stale, restart <code class="bg-[var(--bg-surface-tertiary)] text-[var(--status-blue)] px-1.5 py-0.5 rounded border border-[var(--border-medium)] font-mono">scripts/pm.ps1</code>.</span>
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
  document.querySelectorAll('.audit-filter-pill').forEach((btn) => {
    if (btn.getAttribute('data-filter') === filterKey) {
      btn.classList.add('active');
    } else {
      btn.classList.remove('active');
    }
  });
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
        const isFast = state.projectGates && state.projectGates.mode === 'fast';

        let statusBadge;
        if (isCurrentRun) {
          statusBadge = `<span class="inline-flex items-center gap-1 text-[10px] px-2 py-0.5 rounded-full font-bold bg-amber-500/20 text-amber-600 dark:text-amber-300 border border-amber-500/40 animate-pulse">Running ⚡</span>`;
        } else if (isFailed) {
          const failCount = s.failure_count || (s.failed_tests ? s.failed_tests.length : 0);
          const countBadge = failCount > 0 ? ` (${failCount} tests)` : '';
          statusBadge = `<button onclick="openFailureModal('${escapeHtml(s.sprint)}', '${escapeHtml(s.run_id || '')}')" class="inline-flex items-center gap-1 text-[10px] px-2 py-0.5 rounded-full font-bold bg-rose-50 text-rose-700 border border-rose-200 hover:bg-rose-100 dark:bg-rose-500/20 dark:hover:bg-rose-500/30 dark:text-rose-300 dark:border-rose-500/40 cursor-pointer transition shadow-sm hover:scale-105" title="Click to view failure details & copy pytest command">Failed ❌${countBadge}</button>`;
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
                ? `Verified via cumulative Gate run on commit ${escapeHtml(s.subsumed_by)}`
                : `Verified via cumulative Gate run on Story ${escapeHtml(s.subsumed_by)}`)
            : 'Verified Gate run';
          const priorFailNotice = s.prior_failure
            ? `<span class="ml-1 text-[9px] text-amber-500 dark:text-amber-300 font-mono" title="Prior failure on earlier commit was resolved by descendant Gate pass">⚠️</span>`
            : '';
          statusBadge = `<span class="inline-flex items-center gap-1 text-[10px] px-2 py-0.5 rounded-full font-bold bg-emerald-50 text-emerald-700 border border-emerald-200 dark:bg-emerald-500/15 dark:text-emerald-300 dark:border-emerald-500/30" title="${escapeHtml(titleTip)}">Passed ✅${viaTip}${priorFailNotice}</span>`;
        } else {
          statusBadge = `<span class="inline-flex items-center gap-1 text-[10px] px-2 py-0.5 rounded-full font-bold bg-amber-50 text-amber-700 border border-amber-200 dark:bg-amber-500/15 dark:text-amber-300 dark:border-amber-500/30">Owed ⚠️</span>`;
        }

        let metrics = '';
        if (s.pass_count !== null || s.coverage !== null) {
          const parts = [];
          if (s.pass_count !== null) parts.push(`${s.pass_count} passed`);
          if (s.xfail_count) parts.push(`${s.xfail_count} xfail`);
          if (s.coverage) parts.push(`${s.coverage} cov`);
          const colorClass = isFailed ? 'text-rose-600 dark:text-rose-400' : 'text-emerald-600 dark:text-emerald-400';
          const metricText = parts.length > 0 ? parts.join(' • ') : (s.pass_count !== null ? `${s.pass_count} passed` : '');
          let metricTitle = '';
          if (s.subsumed_by) {
            metricTitle = `Inherited from Gate run on ${s.subsumed_by}` + (s.run_id ? ` (${s.run_id})` : '');
          } else if (s.run_id) {
            metricTitle = `Measured in Gate run ${s.run_id}` + (s.sha ? ` at ${s.sha.slice(0, 7)}` : '');
          } else if (s.tested_sha) {
            metricTitle = `Measured at commit ${s.tested_sha.slice(0, 7)}`;
          }
          if (s.duration_sec) {
            metricTitle += (metricTitle ? ' — ' : '') + `Runtime: ${s.duration_sec}s`;
          }
          const titleAttr = metricTitle ? ` title="${escapeHtml(metricTitle)}"` : '';
          metrics = `<span class="${colorClass} font-mono text-[11px] font-semibold cursor-help"${titleAttr}>${escapeHtml(metricText)}</span>`;
        } else if (isVerified) {
          metrics = '<span class="text-slate-500 dark:text-slate-500 font-mono text-[11px]" title="Verified without recorded test numbers">—</span>';
        } else {
          metrics = '<span class="text-slate-400 dark:text-slate-500 italic text-[11px]">Unverified</span>';
        }

        const shortSha = s.sha ? s.sha.slice(0, 7) : '--';
        const retryLabel = isFast ? 'Retry Suite' : 'Retry Gate 5';
        const runLabel = isFast ? 'Run Suite' : 'Run Gate 5';

        const actionBtn = isCurrentRun
          ? `<span class="text-[10px] px-2 py-1 rounded bg-amber-500/20 text-amber-600 dark:text-amber-300 border border-amber-500/40 font-mono animate-pulse">Running...</span>`
          : isVerified
          ? `<span class="text-[10px] px-2 py-1 rounded bg-[var(--bg-surface-secondary)] text-[var(--text-tertiary)] border border-[var(--border-subtle)] font-mono">Done</span>`
          : isFailed
          ? `<button onclick="${isFast ? 'startTestSuiteRun()' : `startRunSprintGate('${escapeHtml(s.sprint)}', '${escapeHtml(s.sha || '')}')`}" ${
              isRunning ? 'disabled' : ''
            } class="px-2.5 py-1 bg-rose-500/10 hover:bg-rose-500/20 active:bg-rose-500/30 text-rose-600 dark:text-rose-300 text-[11px] font-medium rounded-lg border border-rose-500/30 transition shadow-xs disabled:opacity-40 disabled:cursor-not-allowed cursor-pointer">
              ${retryLabel}
            </button>`
          : `<button onclick="${isFast ? 'startTestSuiteRun()' : `startRunSprintGate('${escapeHtml(s.sprint)}', '${escapeHtml(s.sha || '')}')`}" ${
              isRunning ? 'disabled' : ''
            } class="px-2.5 py-1 bg-[var(--bg-surface-secondary)] hover:bg-[var(--bg-surface-tertiary)] text-[var(--text-primary)] text-[11px] font-medium rounded-lg border border-[var(--border-medium)] transition shadow-xs disabled:opacity-40 disabled:cursor-not-allowed cursor-pointer">
              ${runLabel}
            </button>`;

        return `
          <tr class="hover:bg-[var(--bg-surface-secondary)] transition">
            <td class="py-2 px-3 font-mono font-bold text-[var(--text-primary)]">
              Story ${escapeHtml(s.sprint)}
            </td>
            <td class="py-2 px-3 text-[var(--text-secondary)] max-w-xs truncate" title="${escapeHtml(s.scope)}">
              ${escapeHtml(s.scope)}
            </td>
            <td class="py-2 px-3 font-mono text-[var(--text-tertiary)] text-[11px]">
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
                <button onclick="copyToClipboard('${cmd}', this)" class="shrink-0 inline-flex items-center gap-1.5 px-2.5 py-1 rounded-lg bg-[var(--bg-surface-secondary)] hover:bg-[var(--bg-surface-tertiary)] text-[var(--text-primary)] border border-[var(--border-medium)] text-[11px] font-mono transition shadow-xs group cursor-pointer" title="Copy exact pytest command">
                  <i class="ph ph-copy text-xs text-[var(--text-quaternary)] group-hover:text-[var(--text-primary)]"></i>
                  <span>Copy command</span>
                </button>
              </div>
              <div class="text-[11px] font-mono text-[var(--text-tertiary)] mb-2 flex items-center gap-1">
                <i class="ph ph-file-code text-[var(--text-quaternary)] text-xs"></i>
                <span class="break-all">${filePath}</span>
              </div>
              <pre class="bg-[var(--bg-surface-secondary)] p-2.5 rounded-lg border border-rose-500/30 text-rose-600 dark:text-rose-300 font-mono text-[11px] overflow-x-auto leading-relaxed whitespace-pre-wrap">${msg}</pre>
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

function copyToClipboard(text, btn, toastMessage = '') {
  const onSuccess = () => {
    if (btn) {
      const origHtml = btn.innerHTML;
      btn.innerHTML = `<i class="ph ph-check text-emerald-500"></i> <span class="text-emerald-500 font-medium">Copied!</span>`;
      setTimeout(() => {
        btn.innerHTML = origHtml;
      }, 2000);
    }
    const finalToast = toastMessage || (btn && btn.id === 'copyMobileUrlBtn' ? 'Mobile URL copied to clipboard' : '');
    if (finalToast) {
      showToast(finalToast, 'success');
    }
  };

  if (navigator.clipboard && navigator.clipboard.writeText) {
    navigator.clipboard.writeText(text).then(onSuccess).catch(() => fallbackCopy(text, btn, onSuccess));
  } else {
    fallbackCopy(text, btn, onSuccess);
  }
}

function fallbackCopy(text, btn, onSuccess) {
  const textarea = document.createElement('textarea');
  textarea.value = text;
  textarea.style.position = 'fixed';
  textarea.style.opacity = '0';
  document.body.appendChild(textarea);
  textarea.select();
  try {
    document.execCommand('copy');
    if (onSuccess) {
      onSuccess();
    } else if (btn) {
      const origHtml = btn.innerHTML;
      btn.innerHTML = `<i class="ph ph-check text-emerald-500"></i> <span class="text-emerald-500 font-medium">Copied!</span>`;
      setTimeout(() => {
        btn.innerHTML = origHtml;
      }, 2000);
    }
  } catch (err) {
    console.error('Fallback copy failed', err);
  }
  if (textarea.parentNode) {
    textarea.parentNode.removeChild(textarea);
  }
}


async function startRunSprintGate(sprintId, commitSha) {
  showToast(`Starting Gate 5 run for Story ${sprintId} in background...`, 'info');
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
      showToast(`Error starting heavy gate: ${res.error || 'Server error'}`, 'error');
    }
  } catch (err) {
    console.error(`Failed to start heavy gate for sprint ${sprintId}:`, err);
    showToast('Failed to start heavy gate execution.', 'error');
  }
}

async function startRunAllPendingSprints() {
  const unverified = (state.sprintGateStatus || []).filter((s) => !s.verified);
  if (unverified.length === 0) {
    showToast('All completed stories have verified Gate 5 runs.', 'info');
    return;
  }
  const count = unverified.length;
  showToast(`Queued ${count} pending stor(ies) for sequential Gate 5 runs`, 'info');

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
      showToast(`Error queuing pending sprints: ${res.error || 'Server error'}`, 'error');
    }
  } catch (err) {
    console.error('Failed to run all pending sprints:', err);
    showToast('Failed to start sequential heavy gate runs.', 'error');
  }
}

// -----------------------------------------------------------------------------
// Event Listeners
// -----------------------------------------------------------------------------
document.addEventListener('DOMContentLoaded', () => {
  initTheme();
  initSidebarState();
  applyActiveStreamsCols();
  checkSystemUpdates(false);

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
  const typeSelect = document.getElementById('pipelineTypeSelect');

  function syncTypeFilter(typeVal) {
    state.filters.type = typeVal;
    if (typeSelect && typeSelect.value !== typeVal) {
      typeSelect.value = typeVal;
    }
    if (typeFilters) {
      typeFilters.querySelectorAll('.type-filter-pill, .segmented-control-btn').forEach((p) => {
        p.classList.toggle('active', p.getAttribute('data-type') === typeVal);
      });
    }
    renderPipeline();
  }

  if (typeFilters) {
    typeFilters.addEventListener('click', (e) => {
      const btn = e.target.closest('.type-filter-pill, .segmented-control-btn');
      if (btn) {
        syncTypeFilter(btn.getAttribute('data-type'));
      }
    });
  }

  if (typeSelect) {
    typeSelect.addEventListener('change', (e) => {
      syncTypeFilter(e.target.value);
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
  initMobileMenuSwipe();
  initKanbanDragDrop();
  initSoundControl();
  fetchServerInfo();
  fetchProjects().then(() => {
    fetchAll();
    scheduleNextPoll(10000);
  });

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
      closeQaDrawer();
      closeFailureModal();
      closeReviewModal();
      closeMobileSidebar();
      closeTicketDrawer();
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
  setExtraTagsHeader('cardModalExtraTags', card);

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
function updateSidebarCollapseUi(isCollapsed) {
  const collapseBtn = document.getElementById('sidebarCollapseBtn');
  if (collapseBtn) {
    collapseBtn.innerHTML = isCollapsed
      ? '<i class="ph ph-arrow-line-right text-sm"></i>'
      : '<i class="ph ph-sidebar-simple text-sm"></i>';
    collapseBtn.setAttribute(
      'title',
      isCollapsed ? 'Expand Sidebar (Cmd/Ctrl + B)' : 'Collapse Sidebar (Cmd/Ctrl + B)'
    );
    collapseBtn.setAttribute(
      'aria-label',
      isCollapsed ? 'Expand Sidebar (Cmd/Ctrl + B)' : 'Collapse Sidebar (Cmd/Ctrl + B)'
    );
  }
  const brandBtn = document.getElementById('brandLogoBtn');
  if (brandBtn) {
    brandBtn.setAttribute(
      'title',
      isCollapsed ? 'Expand Sidebar (Cmd/Ctrl + B)' : 'Collapse Sidebar (Cmd/Ctrl + B)'
    );
    brandBtn.setAttribute(
      'aria-label',
      isCollapsed ? 'Expand Sidebar (Cmd/Ctrl + B)' : 'Collapse Sidebar (Cmd/Ctrl + B)'
    );
  }
}

function toggleSidebarCollapse() {
  const sidebar = document.getElementById('appSidebar');
  if (!sidebar) return;
  const isCollapsed = sidebar.classList.toggle('collapsed');
  localStorage.setItem('pmqa_sidebar_collapsed', isCollapsed ? 'true' : 'false');
  updateSidebarCollapseUi(isCollapsed);
}

function initSidebarState() {
  const sidebar = document.getElementById('appSidebar');
  if (!sidebar) return;
  const isCollapsed = localStorage.getItem('pmqa_sidebar_collapsed') === 'true';
  if (isCollapsed) {
    sidebar.classList.add('collapsed');
  }
  updateSidebarCollapseUi(isCollapsed);
}

function toggleMobileSidebar() {
  const sidebar = document.getElementById('appSidebar');
  const backdrop = document.getElementById('sidebarBackdrop');
  if (!sidebar || !backdrop) return;
  const isOpen = sidebar.classList.contains('translate-x-0') || sidebar.classList.contains('mobile-open');
  if (isOpen) {
    closeMobileSidebar();
  } else {
    sidebar.classList.remove('-translate-x-full');
    sidebar.classList.add('translate-x-0', 'mobile-open');
    backdrop.classList.remove('hidden');
    setTimeout(() => backdrop.classList.remove('opacity-0'), 10);
  }
}

function closeMobileSidebar() {
  const sidebar = document.getElementById('appSidebar');
  const backdrop = document.getElementById('sidebarBackdrop');
  if (sidebar) {
    sidebar.classList.remove('translate-x-0', 'mobile-open');
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
  s = s.replace(/\[([^\]]+)\]\((https?:\/\/[^\s)]+|\/[^\s)]+)\)/g, '<a href="$2" target="_blank" rel="noopener noreferrer" class="text-emerald-600 dark:text-emerald-400 hover:underline font-medium">$1</a>');
  // 4. Convert bold **text** or __text__
  s = s.replace(/\*\*([^*]+)\*\*/g, '<strong class="font-semibold text-[var(--text-primary)]">$1</strong>');
  s = s.replace(/__([^_]+)__/g, '<strong class="font-semibold text-[var(--text-primary)]">$1</strong>');
  // 5. Convert inline code `code` to styled code pill
  s = s.replace(/`([^`]+)`/g, '<code class="px-1.5 py-0.5 rounded bg-[var(--bg-surface-tertiary)] text-[var(--status-blue)] font-mono text-[11px] border border-[var(--border-medium)]">$1</code>');
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
      const qaDrawer = document.getElementById('qaCardDrawer');
      if (qaDrawer && !qaDrawer.classList.contains('translate-x-full')) {
        openQaDrawer(card, { preserveState: true });
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
    <div class="bg-[var(--bg-surface-secondary)] rounded-xl border border-[var(--border-medium)] overflow-x-auto shadow-xs">
      <table class="w-full text-left border-collapse min-w-[500px]">
        <thead>
          <tr class="bg-[var(--bg-surface-tertiary)] border-b border-[var(--border-medium)] text-xs font-bold text-[var(--text-secondary)] uppercase tracking-wider">
            <th class="py-2.5 px-3 w-40 whitespace-nowrap cursor-pointer select-none hover:text-emerald-500 transition-colors group" onclick="toggleChangelogSort('${card ? escapeHtml(card.mn_id) : ''}')" title="Click to toggle ascending/descending order">
              <div class="inline-flex items-center gap-1.5">
                <span>Date &amp; Time</span>
                <span class="text-[10px] font-mono text-emerald-500 group-hover:scale-110 transition-transform">${order === 'asc' ? '▲ (Asc)' : '▼ (Desc)'}</span>
              </div>
            </th>
            <th class="py-2.5 px-3 w-44 whitespace-nowrap">Agent</th>
            <th class="py-2.5 px-3">Work & Evidence</th>
          </tr>
        </thead>
        <tbody class="divide-y divide-[var(--border-subtle)] text-sm">
          ${sorted
            .map(
              (cl) => {
                const targetRef = card ? (card.sprint || card.mn_id) : '';
                const reviewBtn = cl.has_review
                  ? `<button onclick="openReviewModal('${escapeHtml(targetRef)}', ${cl.review_round || 1})" class="inline-flex items-center gap-1.5 px-2.5 py-1 rounded-lg bg-purple-500/10 hover:bg-purple-500/20 text-purple-600 dark:text-purple-300 border border-purple-500/30 text-xs font-semibold transition ml-2 shrink-0 shadow-xs group cursor-pointer">
                      <i class="ph ph-shield-check text-purple-500 group-hover:scale-110 transition-transform text-xs"></i>
                      <span>View Review</span>
                    </button>`
                  : '';

                const timeHtml = cl.time
                  ? `<div class="flex items-center gap-1 text-[var(--text-tertiary)] font-mono text-xs mt-0.5">
                      <i class="ph ph-clock text-[var(--text-quaternary)] text-xs shrink-0"></i>
                      <span>${escapeHtml(cl.time)}</span>
                    </div>`
                  : '';

                return `
            <tr class="hover:bg-[var(--bg-surface-tertiary)] transition align-top">
              <td class="py-2.5 px-3 font-mono text-[var(--text-secondary)] font-medium text-xs whitespace-nowrap">
                <div class="flex flex-col pt-0.5">
                  <div class="flex items-center gap-1.5">
                    <i class="ph ph-calendar-blank text-[var(--text-quaternary)] text-xs shrink-0"></i>
                    <span>${escapeHtml(cl.date)}</span>
                  </div>
                  ${timeHtml}
                </div>
              </td>
              <td class="py-2.5 px-3 whitespace-nowrap">
                <span class="inline-block px-2.5 py-0.5 rounded bg-emerald-500/10 text-emerald-600 dark:text-emerald-400 border border-emerald-500/20 font-mono text-xs font-semibold whitespace-nowrap leading-snug">
                  ${escapeHtml(cl.agent)}
                </span>
              </td>
              <td class="py-2.5 px-3 text-[var(--text-primary)] font-sans text-xs leading-relaxed break-words">
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
          ? `<span class="font-mono text-[10px] text-sky-600 dark:text-sky-300 bg-sky-500/10 border border-sky-500/20 px-1.5 py-0.5 rounded ml-2 shrink-0">resolved @ ${escapeHtml(resSha)}</span>`
          : '';
        const textClass = f.checked ? 'text-[var(--text-tertiary)] line-through' : 'text-[var(--text-primary)] font-medium';
        return `
          <div class="review-finding-item">
            <input type="checkbox" ${checkedAttr} disabled class="mt-0.5 rounded border-purple-500/50 text-purple-500 bg-[var(--bg-surface)] cursor-not-allowed">
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
  } else if (action === 'clear-sprint-filter') {
    clearPipelineSprintFilter();
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
  } else if (action === 'open-qa-drawer') {
    const cardId = trigger.getAttribute('data-card-id');
    if (cardId) openQaDrawerById(cardId);
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
  const badgeEl = document.getElementById('headerProjectBuildingBadge');
  const pillEl = document.getElementById('activeProjectPill');
  const cur = state.currentProject;
  if (nameEl) {
    nameEl.textContent = cur ? projectLabel(cur) : (state.projectsError ? 'Projects unavailable' : 'No project');
    nameEl.title = cur ? `${cur.name} — ${cur.code_path || ''}` : '';
  }
  if (badgeEl) {
    const bCount = cur ? (cur.building_count || 0) : 0;
    if (bCount > 0) {
      badgeEl.textContent = `${bCount}`;
      badgeEl.title = `${bCount} stories building`;
      badgeEl.classList.remove('hidden');
    } else {
      badgeEl.classList.add('hidden');
    }
  }
  if (pillEl) {
    const pName = cur ? (cur.prefix || cur.name) : 'No project';
    const bCount = cur ? (cur.building_count || 0) : 0;
    pillEl.innerHTML = `<span>${escapeHtml(pName)}</span>${
      bCount > 0 ? `<span class="ml-1.5 px-1.5 py-0.2 text-[9px] font-bold rounded-full bg-purple-500/20 text-purple-600 dark:text-purple-300 border border-purple-500/30">${bCount}</span>` : ''
    }`;
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
          const bCount = p.building_count || 0;
          const buildingTag = bCount > 0
            ? `<span class="ml-auto shrink-0 px-1.5 py-0.2 text-[10px] font-bold rounded-full bg-purple-500/15 text-purple-600 dark:text-purple-400 border border-purple-500/25" title="${bCount} stories building">${bCount}</span>`
            : '';
          return `<button role="menuitemradio" aria-checked="${active}" data-project-slug="${escapeHtml(p.slug)}" title="${escapeHtml(p.code_path || '')}"
            class="w-full flex items-center gap-2 px-3 py-2 text-left text-xs rounded-lg transition ${
              active ? 'bg-emerald-500/10 text-emerald-600 dark:text-emerald-300 font-semibold' : 'text-[var(--text-primary)] hover:bg-[var(--bg-surface-secondary)]'
            }">
            <span class="w-3.5 shrink-0">${active ? '<i class="ph ph-check text-xs"></i>' : ''}</span>
            <span class="truncate font-semibold">${escapeHtml(p.name)}</span>
            ${p.prefix ? `<span class="shrink-0 font-mono text-[10px] text-[var(--text-tertiary)]">${escapeHtml(p.prefix)}</span>` : ''}
            ${externalMarkerHtml(p)}
            ${buildingTag}
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
            const bCount = p.building_count || 0;
            const bBadge = bCount > 0
              ? `<span class="ml-1.5 px-1.5 py-0.2 text-[10px] font-bold rounded-full bg-purple-500/15 text-purple-600 dark:text-purple-400 border border-purple-500/25" title="${bCount} stories building">${bCount}</span>`
              : '';
            return `<button class="type-filter-pill max-w-[12rem] truncate ${active ? 'active' : ''}" data-project-slug="${escapeHtml(p.slug)}" aria-pressed="${active}" title="${escapeHtml(projectLabel(p))}">${escapeHtml(p.name)}${p.prefix ? ` <span class="font-mono opacity-70">${escapeHtml(p.prefix)}</span>` : ''}${bBadge}</button>`;
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

  // 1. Abort in-flight requests from the previous project
  if (currentFetchAbortController) {
    try { currentFetchAbortController.abort(); } catch (_) {}
    currentFetchAbortController = null;
  }

  // 2. Immediate optimistic feedback
  const targetProject = (state.projects || []).find((p) => p.slug === slug);
  const targetName = targetProject ? targetProject.name : slug;
  const nameEl = document.getElementById('headerProjectName');
  if (nameEl) {
    nameEl.innerHTML = `<span class="inline-flex items-center gap-1.5"><i class="ph ph-spinner animate-spin text-emerald-500"></i> ${escapeHtml(targetName)}</span>`;
  }
  showLoadingFeedback(true);
  state.pendingProjectSlug = slug;
  resetProjectScopedState();
  if (!(state.activeTab === 'analytics' && restoreCachedAnalytics())) renderLoadingSkeletons(true);

  try {
    const res = await fetch('/api/projects/select', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ slug }),
    });
    const data = await res.json().catch(() => ({}));
    if (!res.ok || !data.success) throw new Error(data.error || `HTTP ${res.status}`);

    // Populate project state directly from response, skipping redundant GET /api/projects
    state.activeProjectSlug = slug;
    if (data.projects) state.projects = data.projects;
    if (data.current) state.currentProject = data.current;
    renderProjectUi();

    // Fetch view-scoped data for active tab
    await fetchCurrentViewData();
    state.pendingProjectSlug = null;
    scheduleNextPoll(10000);
  } catch (err) {
    state.pendingProjectSlug = null;
    if (err.name !== 'AbortError') {
      showToast(`Could not switch project: ${err.message}`, 'error');
    }
    showLoadingFeedback(false);
  }
}

function resetProjectScopedState() {
  state.cards = [];
  state.sprints = [];
  state.activity = {};
  state.gitSync = {};
  state.selectedQaId = null;
  state.analytics = null;
  clearTimeout(state._analyticsBackfillTimer);
  state.analyticsFilter = null;
  state.activeSprintId = 'ACTIVE';
  state.sprintDodData = null;
  state.heavyGateWorker = null;
  state.heavyGateInfo = null;
  state.sprintGateStatus = [];
  state.projectGates = null;
  state.sprintAuditFilter = 'needs_attention';
  state.currentReviews = [];
  state.currentReviewIndex = 0;
  state.roadmapSearch = '';
  state.filters.sprint = 'ALL';
  state.filters.search = '';
  state.filters.tags = Object.create(null);

  const pipelineSearch = document.getElementById('pipelineSearch');
  if (pipelineSearch) pipelineSearch.value = '';
  const roadmapSearch = document.getElementById('roadmapSearchInput');
  if (roadmapSearch) roadmapSearch.value = '';
  const pipelineSprintFilter = document.getElementById('pipelineSprintFilter');
  if (pipelineSprintFilter) pipelineSprintFilter.value = 'ALL';

  document.querySelectorAll('.audit-filter-pill').forEach((btn) => {
    if (btn.getAttribute('data-filter') === 'needs_attention') {
      btn.classList.add('active');
    } else {
      btn.classList.remove('active');
    }
  });

  const qaDetail = document.getElementById('qaDetailContainer');
  if (qaDetail) {
    qaDetail.innerHTML = `
      <div class="h-full flex flex-col items-center justify-center text-[var(--text-tertiary)]">
        <i class="ph ph-stack text-4xl mb-3 text-[var(--text-quaternary)]"></i>
        <p class="text-sm">Select a card on the left to inspect acceptance criteria and validate work.</p>
      </div>
    `;
  }
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
      const sidebar = document.getElementById('appSidebar');
      if (sidebar && sidebar.classList.contains('collapsed')) {
        toggleSidebarCollapse();
      }
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
  return `<button type="button" ${attrs} class="inline-flex items-center gap-1.5 px-2.5 py-1 rounded-lg bg-emerald-50 text-emerald-700 border border-emerald-200 hover:bg-emerald-100 shadow-xs dark:bg-emerald-950/40 dark:text-emerald-300 dark:border-emerald-500/30 dark:hover:bg-emerald-900/50 text-xs font-medium transition ${extraClass}" title="Open the Story Close-Out report"><i class="ph ph-file-check text-xs shrink-0"></i><span>Story Close-Out</span></button>`;
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
    col.scrollIntoView({ behavior: 'smooth', inline: 'start', block: 'nearest' });
  }
  document.querySelectorAll('.mobile-col-pill').forEach((btn) => {
    btn.classList.toggle('active', btn.getAttribute('data-col') === colId);
  });
}

function initMobileMenuSwipe() {
  let touchStartX = 0;
  let touchStartY = 0;
  let touchStartTime = 0;
  let canSwipeOpen = false;

  document.addEventListener(
    'touchstart',
    (e) => {
      if (window.innerWidth >= 1024) return;
      if (!e.touches || e.touches.length !== 1) return;
      const target = e.target;
      if (target.closest('input, textarea, select, button, [contenteditable="true"]')) return;

      const touch = e.touches[0];
      touchStartX = touch.clientX;
      touchStartY = touch.clientY;
      touchStartTime = Date.now();

      const sidebar = document.getElementById('appSidebar');
      const isSidebarOpen = sidebar && (sidebar.classList.contains('translate-x-0') || sidebar.classList.contains('mobile-open'));

      if (isSidebarOpen) {
        canSwipeOpen = false;
        return;
      }

      // Allow swipe open if starting near left edge (<=40px) or horizontal scroll is at leftmost boundary
      const isNearEdge = touchStartX <= 40;
      const kanban = document.getElementById('kanbanColumnsContainer');
      const scrollLeft = kanban ? kanban.scrollLeft : (window.pageXOffset || document.documentElement.scrollLeft || 0);
      const isAtScrollBoundary = scrollLeft <= 0;

      canSwipeOpen = isNearEdge || isAtScrollBoundary;
    },
    { passive: true }
  );

  document.addEventListener(
    'touchend',
    (e) => {
      if (window.innerWidth >= 1024) return;
      if (!e.changedTouches || e.changedTouches.length !== 1) return;
      const target = e.target;
      if (target.closest('input, textarea, select, button, [contenteditable="true"]')) return;

      const touch = e.changedTouches[0];
      const dx = touch.clientX - touchStartX;
      const dy = touch.clientY - touchStartY;
      const dt = Date.now() - touchStartTime;

      // Gesture criteria: horizontal distance > 60px, primarily horizontal (abs(dx) > 1.5 * abs(dy)), duration < 1s
      if (Math.abs(dx) < 60 || Math.abs(dx) <= 1.5 * Math.abs(dy) || dt > 1000) return;

      const sidebar = document.getElementById('appSidebar');
      const isSidebarOpen = sidebar && (sidebar.classList.contains('translate-x-0') || sidebar.classList.contains('mobile-open'));

      if (isSidebarOpen) {
        // Leftward swipe closes the sidebar
        if (dx < -60) {
          closeMobileSidebar();
        }
      } else {
        // Rightward swipe opens the sidebar if eligible
        if (dx > 60 && canSwipeOpen) {
          if (sidebar && !sidebar.classList.contains('translate-x-0')) {
            toggleMobileSidebar();
          }
        }
      }
    },
    { passive: true }
  );
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
        const containerLeft = container.scrollLeft;
        let closestCol = null;
        let closestDist = Infinity;
        cols.forEach((col) => {
          const dist = Math.abs(col.offsetLeft - containerLeft);
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
// Mobile Connect Modal & Connection Path Switcher Controller
// -----------------------------------------------------------------------------
let currentServerInfo = null;

function renderQrCodeGraphic(container, url) {
  if (!container) return;
  container.innerHTML = '';
  try {
    if (typeof QRCode !== 'undefined') {
      new QRCode(container, {
        text: url,
        width: 200,
        height: 200,
        colorDark: '#0f172a',
        colorLight: '#ffffff',
        correctLevel: QRCode.CorrectLevel.M,
      });
      return;
    }
  } catch (err) {
    console.warn('QRCode canvas generation failed, falling back to SVG QR:', err);
  }
  container.innerHTML = generateQrSvg(url);
}

async function fetchServerInfo(force = false) {
  if (!force && state.serverInfo && (Date.now() - (state.serverInfoTime || 0) < 60000)) {
    return state.serverInfo;
  }
  try {
    let controller = null;
    let signal = undefined;
    if (typeof AbortController !== 'undefined') {
      controller = new AbortController();
      signal = controller.signal;
      setTimeout(() => {
        try { controller.abort(); } catch (_) {}
      }, 3000);
    }
    const res = await fetch('/api/server-info', { signal });
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    const info = await res.json();
    state.serverInfo = info;
    state.serverInfoTime = Date.now();
    currentServerInfo = info;
    return info;
  } catch (err) {
    console.warn('fetchServerInfo aborted or failed:', err);
    return state.serverInfo || currentServerInfo || null;
  }
}

function updateMobileBadges(info) {
  if (!info) return;
  const kaBadge = document.getElementById('mobileKeepAliveBadge');
  if (kaBadge) {
    kaBadge.textContent = info.keep_alive ? 'Active (Sleep Prevented)' : 'Standard';
    kaBadge.className = `font-semibold ${info.keep_alive ? 'text-cyan-500 dark:text-cyan-400' : 'text-slate-400'}`;
  }
  const tokBadge = document.getElementById('mobileTokenBadge');
  if (tokBadge) {
    tokBadge.textContent = info.token_required ? 'Session Token Active' : 'Unauthenticated (Local)';
    tokBadge.className = `font-semibold ${info.token_required ? 'text-emerald-500 dark:text-emerald-400' : 'text-slate-400'}`;
  }
}

function switchConnectionPath(mode) {
  const tunnelBtn = document.getElementById('pathTunnelBtn');
  const lanBtn = document.getElementById('pathLanBtn');
  const info = state.serverInfo || currentServerInfo || {};
  const hasTunnel = Boolean(info.tunnel_url);

  if (mode === 'tunnel' && !hasTunnel) {
    if (typeof showToast === 'function') {
      showToast('Public Tunnel is not active on host. Start dashboard with -Tunnel.', 'warning');
    }
    return;
  }

  const activeMode = (mode === 'tunnel' && hasTunnel) ? 'tunnel' : 'lan';
  try {
    localStorage.setItem('pmqa_connection_path', activeMode);
  } catch (_) {}

  // Update button active styling
  if (tunnelBtn && lanBtn) {
    if (activeMode === 'tunnel') {
      tunnelBtn.className = 'flex-1 py-1.5 px-3 rounded-lg text-xs font-semibold flex items-center justify-center gap-1.5 transition cursor-pointer active bg-emerald-500/15 text-emerald-600 dark:text-emerald-400 border border-emerald-500/30';
      lanBtn.className = 'flex-1 py-1.5 px-3 rounded-lg text-xs font-semibold flex items-center justify-center gap-1.5 transition cursor-pointer text-[var(--text-secondary)] hover:text-[var(--text-primary)] border border-transparent';
    } else {
      lanBtn.className = 'flex-1 py-1.5 px-3 rounded-lg text-xs font-semibold flex items-center justify-center gap-1.5 transition cursor-pointer active bg-emerald-500/15 text-emerald-600 dark:text-emerald-400 border border-emerald-500/30';
      tunnelBtn.className = 'flex-1 py-1.5 px-3 rounded-lg text-xs font-semibold flex items-center justify-center gap-1.5 transition cursor-pointer text-[var(--text-secondary)] hover:text-[var(--text-primary)] border border-transparent';
    }
    if (!hasTunnel) {
      tunnelBtn.classList.add('opacity-40', 'cursor-not-allowed');
      tunnelBtn.title = 'Public Tunnel is not active. Launch dashboard with -Tunnel.';
    } else {
      tunnelBtn.classList.remove('opacity-40', 'cursor-not-allowed');
      tunnelBtn.title = 'Public Tunnel (Cloudflare)';
    }
  }

  // Derive target URL
  const storedToken = sessionStorage.getItem('cockpit_token') || localStorage.getItem('cockpit_token') || '';
  const token = info.token || storedToken || '';
  let mobileUrl = '';
  if (activeMode === 'tunnel') {
    mobileUrl = (info.tunnel_url || window.location.origin) + (token ? `?token=${encodeURIComponent(token)}` : '');
  } else {
    const port = info.port || window.location.port || 3333;
    const host = info.lan_ip || window.location.hostname;
    mobileUrl = `${window.location.protocol}//${host}${port ? `:${port}` : ''}` + (token ? `?token=${encodeURIComponent(token)}` : '');
  }

  const input = document.getElementById('mobileConnectUrlInput');
  if (input) input.value = mobileUrl;

  const qrContainer = document.getElementById('mobileQrGraphic');
  if (qrContainer) {
    renderQrCodeGraphic(qrContainer, mobileUrl);
  }

  const netBadge = document.getElementById('mobileNetworkModeBadge');
  if (netBadge) {
    netBadge.textContent = activeMode === 'tunnel' ? 'Cloudflare Tunnel (Public)' : 'Local Wi-Fi (LAN)';
    netBadge.className = `font-semibold ${activeMode === 'tunnel' ? 'text-amber-500 dark:text-amber-400' : 'text-emerald-600 dark:text-emerald-400'}`;
  }
}

async function openMobileConnectModal() {
  const modal = document.getElementById('mobileConnectModal');
  if (!modal) return;
  modal.classList.remove('hidden');
  loadNotificationModalConfig();

  // 1. Instant (0ms) synchronous rendering using cached server info or origin fallback
  const cached = state.serverInfo || currentServerInfo;
  if (cached) {
    const savedPath = localStorage.getItem('pmqa_connection_path');
    const defaultMode = (cached.tunnel_url && savedPath !== 'lan') ? 'tunnel' : (cached.tunnel_url ? savedPath : 'lan');
    switchConnectionPath(defaultMode);
    updateMobileBadges(cached);
  } else {
    const input = document.getElementById('mobileConnectUrlInput');
    const fallbackUrl = window.location.href;
    if (input) input.value = fallbackUrl;
    const qrContainer = document.getElementById('mobileQrGraphic');
    if (qrContainer) renderQrCodeGraphic(qrContainer, fallbackUrl);
  }

  // 2. Quiet background refresh with 3s timeout
  try {
    const freshInfo = await fetchServerInfo(true);
    if (freshInfo) {
      const storedToken = sessionStorage.getItem('cockpit_token') || localStorage.getItem('cockpit_token') || '';
      const token = freshInfo.token || storedToken || '';
      if (token) {
        try { sessionStorage.setItem('cockpit_token', token); } catch (_) {}
        try { localStorage.setItem('cockpit_token', token); } catch (_) {}
      }
      const savedPath = localStorage.getItem('pmqa_connection_path');
      const defaultMode = (freshInfo.tunnel_url && savedPath !== 'lan') ? 'tunnel' : (freshInfo.tunnel_url ? savedPath : 'lan');
      switchConnectionPath(defaultMode);
      updateMobileBadges(freshInfo);
    }
  } catch (err) {
    console.warn('Background server info refresh failed:', err);
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
  copyToClipboard(input.value, document.getElementById('copyMobileUrlBtn'), 'Mobile URL copied to clipboard');
}

// -----------------------------------------------------------------------------
// Software Updates & Git Manager Controller
// -----------------------------------------------------------------------------
let cachedUpdateStatus = null;

async function checkSystemUpdates(interactive = true) {
  const icon = document.getElementById('checkUpdatesIcon');
  const text = document.getElementById('checkUpdatesText');
  const installBtn = document.getElementById('installUpdateBtn');
  const statusEl = document.getElementById('updateModalSyncStatus');
  const dirtyWarn = document.getElementById('updateDirtyWarning');
  const commitsPanel = document.getElementById('incomingCommitsPanel');
  const commitsList = document.getElementById('incomingCommitsList');
  const commitsCount = document.getElementById('incomingCommitsCount');
  const dirtyFilesList = document.getElementById('dirtyFilesList');

  if (icon) icon.className = 'ph ph-spinner ph-spin text-sm';
  if (text) text.textContent = 'Checking...';

  try {
    const res = await fetch('/api/system/update-status');
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    const data = await res.json();
    cachedUpdateStatus = data;

    // Update version displays
    const verEl = document.getElementById('updateModalVersion');
    const commitEl = document.getElementById('updateModalCommit');
    const boardVer = document.getElementById('boardVersionText');
    const boardInd = document.getElementById('boardUpdateIndicatorText');

    if (verEl && data.current_version) verEl.textContent = `v${data.current_version}`;
    if (boardVer && data.current_version) boardVer.textContent = `v${data.current_version}`;
    if (commitEl && data.current_commit) commitEl.textContent = data.current_commit.substring(0, 7);

    // Dirty worktree check
    if (data.is_dirty) {
      if (dirtyWarn) dirtyWarn.classList.remove('hidden');
      if (dirtyFilesList) dirtyFilesList.textContent = (data.dirty_files || []).join('\n');
      if (statusEl) {
        statusEl.textContent = 'Uncommitted local changes';
        statusEl.className = 'font-semibold text-amber-600 dark:text-amber-400';
      }
      if (installBtn) installBtn.disabled = true;
    } else {
      if (dirtyWarn) dirtyWarn.classList.add('hidden');
    }

    // Behind count / incoming commits
    if (data.behind_count > 0) {
      if (statusEl && !data.is_dirty) {
        statusEl.textContent = `${data.behind_count} update${data.behind_count > 1 ? 's' : ''} available`;
        statusEl.className = 'font-semibold text-emerald-600 dark:text-emerald-400';
      }
      if (boardInd) {
        boardInd.textContent = `${data.behind_count} new`;
        boardInd.className = 'text-emerald-500 font-semibold';
      }
      if (commitsPanel) commitsPanel.classList.remove('hidden');
      if (commitsCount) commitsCount.textContent = `${data.behind_count} behind`;
      if (commitsList) {
        commitsList.innerHTML = (data.incoming_commits || []).map(c => `
          <div class="p-2 rounded-lg bg-[var(--bg-surface)] border border-[var(--border-subtle)] flex items-start gap-2">
            <span class="font-mono text-[10px] px-1.5 py-0.5 rounded bg-black/10 text-emerald-600 dark:text-emerald-400 font-semibold shrink-0">${c.sha ? c.sha.substring(0, 7) : ''}</span>
            <span class="text-[11px] text-[var(--text-primary)] leading-tight truncate">${escapeHtml(c.message || '')}</span>
          </div>
        `).join('');
      }
      if (installBtn && !data.is_dirty) {
        installBtn.disabled = false;
      }
      if (interactive && typeof showToast === 'function') {
        showToast(`${data.behind_count} updates available from origin/main!`, 'info');
      }
    } else {
      if (statusEl && !data.is_dirty) {
        statusEl.textContent = 'Up to date with origin/main';
        statusEl.className = 'font-semibold text-emerald-600 dark:text-emerald-400';
      }
      if (boardInd) {
        boardInd.textContent = 'Up to date';
        boardInd.className = 'text-[var(--text-tertiary)]';
      }
      if (commitsPanel) commitsPanel.classList.add('hidden');
      if (installBtn) installBtn.disabled = true;
      if (interactive && typeof showToast === 'function') {
        showToast('PM & QA Board is up to date!', 'success');
      }
    }
  } catch (err) {
    console.error('Update check failed:', err);
    if (statusEl) {
      statusEl.textContent = 'Check failed (offline or no remote)';
      statusEl.className = 'font-semibold text-rose-500';
    }
    if (interactive && typeof showToast === 'function') {
      showToast(`Update check failed: ${err.message}`, 'error');
    }
  } finally {
    if (icon) icon.className = 'ph ph-arrows-clockwise text-sm';
    if (text) text.textContent = 'Check for Updates';
  }
}

async function applySystemUpdate() {
  const installBtn = document.getElementById('installUpdateBtn');
  const installIcon = document.getElementById('installUpdateIcon');
  const installText = document.getElementById('installUpdateText');
  const checkBtn = document.getElementById('checkForUpdatesBtn');
  const successCard = document.getElementById('updateSuccessCard');
  const summaryEl = document.getElementById('updateSuccessSummary');
  const advisoryCard = document.getElementById('skillsAdvisoryCard');
  const logPanel = document.getElementById('updateLogPanel');

  if (installBtn) installBtn.disabled = true;
  if (checkBtn) checkBtn.disabled = true;
  if (installIcon) installIcon.className = 'ph ph-spinner ph-spin text-sm';
  if (installText) installText.textContent = 'Updating...';

  try {
    const res = await fetch('/api/system/update-apply', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({}),
    });
    const data = await res.json();
    if (!res.ok || !data.success) {
      throw new Error(data.error || `HTTP ${res.status}`);
    }

    // Success state
    if (successCard) successCard.classList.remove('hidden');
    if (summaryEl) {
      const filesCount = (data.changed_files || []).length;
      summaryEl.textContent = `Applied fast-forward update to ${data.new_commit ? data.new_commit.substring(0, 7) : 'HEAD'} (${filesCount} file${filesCount === 1 ? '' : 's'} updated).`;
    }
    if (data.skills_outdated && advisoryCard) {
      advisoryCard.classList.remove('hidden');
    }

    const commitEl = document.getElementById('updateModalCommit');
    if (commitEl && data.new_commit) commitEl.textContent = data.new_commit.substring(0, 7);

    if (typeof showToast === 'function') {
      showToast('PM & QA Board updated successfully!', 'success');
    }
    if (typeof playChime === 'function') {
      playChime('completed');
    }
  } catch (err) {
    console.error('Apply update failed:', err);
    if (logPanel) {
      logPanel.classList.remove('hidden');
      logPanel.textContent = `Update Error: ${err.message}`;
    }
    if (typeof showToast === 'function') {
      showToast(`Update failed: ${err.message}`, 'error');
    }
  } finally {
    if (installIcon) installIcon.className = 'ph ph-download-simple text-sm';
    if (installText) installText.textContent = 'Install & Update';
    if (checkBtn) checkBtn.disabled = false;
  }
}

function openSoftwareUpdateModal() {
  const modal = document.getElementById('softwareUpdateModal');
  if (!modal) return;
  modal.classList.remove('hidden');
  checkSystemUpdates(false);
}

function closeSoftwareUpdateModal() {
  const modal = document.getElementById('softwareUpdateModal');
  if (modal) modal.classList.add('hidden');
}

function copySkillUpgradePrompt() {
  copyToClipboard('check for PM & QA Board upgrades', document.getElementById('copySkillPromptBtn'));
}

// -----------------------------------------------------------------------------
// Executive Analytics & Velocity Overhaul (PQC-28 / PQC-29)
// -----------------------------------------------------------------------------

state.analyticsWindow = state.analyticsWindow || '6w';
state.analyticsScope = state.analyticsScope || 'active';
state.analytics = null;
state.analyticsCache = {};
state.analyticsFilter = null;
state.analyticsCompView = 'types';
state.burnupMode = 'burnup'; // 'burnup' or 'netbug'
state.ticketDrawerSearch = '';
state.ticketDrawerType = 'all';
state.selectedPipelineStage = null;
state.expandedCohorts = new Set();
state._ticketDrawerStories = [];

function setAnalyticsWindow(windowStr) {
  state.analyticsWindow = windowStr;
  document.querySelectorAll('.analytics-window-btn').forEach((btn) => {
    if (btn.getAttribute('data-window') === windowStr) {
      btn.classList.add('active-window-pill');
      btn.classList.remove('text-[var(--text-secondary)]');
    } else {
      btn.classList.remove('active-window-pill');
      btn.classList.add('text-[var(--text-secondary)]');
    }
  });
  loadAnalytics(windowStr, false);
}

function setAnalyticsScope(scopeStr) {
  state.analyticsScope = scopeStr;
  document.querySelectorAll('.analytics-scope-btn').forEach((btn) => {
    if (btn.getAttribute('data-scope') === scopeStr) {
      btn.classList.add('active-scope-pill');
      btn.classList.remove('text-[var(--text-secondary)]');
    } else {
      btn.classList.remove('active-scope-pill');
      btn.classList.add('text-[var(--text-secondary)]');
    }
  });
  loadAnalytics(state.analyticsWindow || '6w', true);
}

function toggleCompositionView(viewStr) {
  state.analyticsCompView = viewStr;
  const btnTypes = document.getElementById('btnCompTypes');
  const btnAgents = document.getElementById('btnCompAgents');
  if (viewStr === 'types') {
    if (btnTypes) {
      btnTypes.classList.add('bg-[var(--bg-surface)]', 'shadow-xs', 'text-[var(--text-primary)]', 'font-semibold');
      btnTypes.classList.remove('text-[var(--text-secondary)]');
    }
    if (btnAgents) {
      btnAgents.classList.remove('bg-[var(--bg-surface)]', 'shadow-xs', 'text-[var(--text-primary)]', 'font-semibold');
      btnAgents.classList.add('text-[var(--text-secondary)]');
    }
  } else {
    if (btnAgents) {
      btnAgents.classList.add('bg-[var(--bg-surface)]', 'shadow-xs', 'text-[var(--text-primary)]', 'font-semibold');
      btnAgents.classList.remove('text-[var(--text-secondary)]');
    }
    if (btnTypes) {
      btnTypes.classList.remove('bg-[var(--bg-surface)]', 'shadow-xs', 'text-[var(--text-primary)]', 'font-semibold');
      btnTypes.classList.add('text-[var(--text-secondary)]');
    }
  }
  if (state.analytics && state.analytics.charts) {
    renderCompositionChart(state.analytics.charts.work_composition);
  }
}

function toggleBurnupChartMode(mode) {
  state.burnupMode = mode;
  const btnBurnup = document.getElementById('btnBurnupToggle');
  const btnNetBug = document.getElementById('btnNetBugToggle');
  const titleEl = document.getElementById('burnupChartTitle');
  const subEl = document.getElementById('burnupChartSubtitle');
  const legendEl = document.getElementById('burnupChartLegend');

  if (mode === 'netbug') {
    if (btnNetBug) {
      btnNetBug.classList.add('active-toggle-btn');
      btnNetBug.classList.remove('text-[var(--text-secondary)]');
    }
    if (btnBurnup) {
      btnBurnup.classList.remove('active-toggle-btn');
      btnBurnup.classList.add('text-[var(--text-secondary)]');
    }
    if (titleEl) {
      titleEl.innerHTML = `<i class="ph ph-bug text-rose-500"></i> Net Bug Flow (Defect Velocity)`;
    }
    if (subEl) {
      subEl.textContent = 'Weekly new defects found vs bugs resolved & drag on delivery throughput';
    }
    if (legendEl) {
      legendEl.innerHTML = `
        <span class="flex items-center gap-1"><span class="w-2.5 h-2.5 rounded-sm bg-rose-500"></span>Bugs Found</span>
        <span class="flex items-center gap-1"><span class="w-2.5 h-2.5 rounded-sm bg-emerald-500"></span>Bugs Fixed</span>
        <span class="flex items-center gap-1 font-semibold text-purple-600 dark:text-purple-400"><span class="w-3 h-0.5 bg-purple-500"></span>Net Flow</span>
      `;
    }
  } else {
    if (btnBurnup) {
      btnBurnup.classList.add('active-toggle-btn');
      btnBurnup.classList.remove('text-[var(--text-secondary)]');
    }
    if (btnNetBug) {
      btnNetBug.classList.remove('active-toggle-btn');
      btnNetBug.classList.add('text-[var(--text-secondary)]');
    }
    if (titleEl) {
      titleEl.innerHTML = `<i class="ph ph-chart-line text-purple-500"></i> Scope Burnup &amp; Delivery Trajectory`;
    }
    if (subEl) {
      subEl.textContent = 'Cumulative total created scope vs completed stories across weekly cohorts';
    }
    if (legendEl) {
      legendEl.innerHTML = `
        <span class="flex items-center gap-1"><span class="w-2.5 h-2.5 rounded-sm bg-sky-500"></span>Total Scope</span>
        <span class="flex items-center gap-1"><span class="w-2.5 h-2.5 rounded-sm bg-purple-500"></span>Done</span>
        <span class="flex items-center gap-1 text-[var(--text-tertiary)]"><span class="w-2.5 h-2.5 rounded-sm bg-amber-500/40"></span>In-Flight WIP</span>
      `;
    }
  }

  if (state.analytics) {
    const scopeBurnup = state.analytics.scope_burnup || (state.analytics.charts ? state.analytics.charts.scope_burnup || state.analytics.charts.burnup : null);
    const netBugFlow = state.analytics.net_bug_flow || (state.analytics.charts ? state.analytics.charts.net_bug_flow : null);
    renderBurnupChart(scopeBurnup, netBugFlow);
  }
}

function showAnalyticsTooltip(e, htmlContent) {
  let tip = document.getElementById('analyticsGlobalTooltip');
  if (!tip) {
    tip = document.createElement('div');
    tip.id = 'analyticsGlobalTooltip';
    tip.className = 'analytics-tooltip';
    document.body.appendChild(tip);
  }
  tip.innerHTML = htmlContent;
  tip.style.display = 'block';
  const x = Math.min(window.innerWidth - 180, Math.max(90, e.clientX));
  const y = Math.max(50, e.clientY - 12);
  tip.style.left = `${x}px`;
  tip.style.top = `${y}px`;
}

function hideAnalyticsTooltip() {
  const tip = document.getElementById('analyticsGlobalTooltip');
  if (tip) tip.style.display = 'none';
}

const ANALYTICS_REVALIDATE_MS = 5000;
const ANALYTICS_BACKFILL_POLL_MS = 3000;
state.analyticsCacheAt = {};
state._analyticsBackfillTimer = null;

function analyticsRequestKey(windowStr) {
  const scope = state.analyticsScope || 'active';
  const projParam = scope === 'all' ? 'all' : (state.pendingProjectSlug || state.currentProject?.slug || '');
  return { windowStr, projParam, cacheKey: `${windowStr}:${projParam}` };
}

// On a project switch: show that project's already-fetched analytics at once. False means a miss.
function restoreCachedAnalytics() {
  const { cacheKey } = analyticsRequestKey(state.analyticsWindow || '6w');
  const cached = state.analyticsCache[cacheKey];
  if (!cached) return false;
  applyAnalyticsData(cached, cacheKey);
  return true;
}

function renderAnalyticsSkeleton() {
  const bar = (w, h = 'h-3') => `<div class="${h} bg-[var(--bg-surface-tertiary)] rounded ${w}"></div>`;
  const strip = document.getElementById('analyticsKpiStrip');
  if (strip) strip.classList.add('kpi-skeleton');
  const chart = (id, rows) => {
    const el = document.getElementById(id);
    if (el) el.innerHTML = `<div class="w-full animate-pulse space-y-3 p-2">${rows}</div>`;
  };
  const bars = `<div class="flex items-end gap-2 h-40">${[40, 65, 50, 80, 55, 90, 70].map((h) => `<div class="flex-1 bg-[var(--bg-surface-tertiary)] rounded-t" style="height:${h}%"></div>`).join('')}</div>${bar('w-1/3')}`;
  chart('chartThroughputContainer', bars);
  chart('chartBurnupContainer', bars);
  chart('chartCompositionContainer', bar('w-full', 'h-24'));
  chart('chartQualityContainer', bar('w-2/3'));
  const row = (cells) => `<tr class="animate-pulse">${cells.map((w) => `<td class="py-3 px-3"><div class="h-3 bg-[var(--bg-surface-tertiary)] rounded ${w}"></div></td>`).join('')}</tr>`;
  const health = document.getElementById('pipelineHealthTableBody');
  if (health) health.innerHTML = [0, 1, 2].map(() => row(['w-20', 'w-8', 'w-8', 'w-8', 'w-12'])).join('');
  const cohorts = document.getElementById('weeklyCohortsTableBody');
  if (cohorts) cohorts.innerHTML = [0, 1, 2, 3].map(() => row(['w-32', 'w-16', 'w-12', 'w-24', 'w-8', 'w-16', 'w-8'])).join('');
  updateAnalyticsBackfillBadge(null);
}

function updateAnalyticsBackfillBadge(backfill) {
  const badge = document.getElementById('analyticsBackfillBadge');
  const text = document.getElementById('analyticsBackfillText');
  if (!badge) return;
  if (backfill && backfill.done === false) {
    if (text) text.textContent = `Counting history… ${backfill.counted} of ${backfill.total} commits`;
    badge.style.display = 'inline-flex';
  } else {
    badge.style.display = 'none';
  }
}

function applyAnalyticsData(data, cacheKey) {
  state.analytics = data;
  const strip = document.getElementById('analyticsKpiStrip');
  if (strip) strip.classList.remove('kpi-skeleton');
  renderAnalyticsView();
  updateAnalyticsBackfillBadge(data.git_backfill);
  clearTimeout(state._analyticsBackfillTimer);
  if (data.git_backfill && data.git_backfill.done === false) {
    // Partial numbers are shown labelled; ask again until the server has counted every commit.
    state._analyticsBackfillTimer = setTimeout(() => {
      const cur = analyticsRequestKey(state.analyticsWindow || '6w');
      if (state.activeTab === 'analytics' && cur.cacheKey === cacheKey) {
        loadAnalytics(state.analyticsWindow || '6w', true, undefined, true);
      }
    }, ANALYTICS_BACKFILL_POLL_MS);
  }
}

async function loadAnalytics(windowStr = '6w', force = false, signal = undefined, silent = false) {
  const { projParam, cacheKey } = analyticsRequestKey(windowStr);
  const cached = state.analyticsCache[cacheKey];
  const age = Date.now() - (state.analyticsCacheAt[cacheKey] || 0);

  if (cached) {
    // Browser-cache hit renders at once; older entries are re-checked quietly in the background.
    if (state.analytics !== cached) applyAnalyticsData(cached, cacheKey);
    if (!force && age < ANALYTICS_REVALIDATE_MS) return cached;
    silent = true;
  } else {
    // Miss: drop the previous view's charts right away and show skeletons until data arrives.
    state.analytics = null;
    renderAnalyticsSkeleton();
  }

  if (!silent) showLoadingFeedback(true);
  try {
    const url = `/api/analytics?window=${encodeURIComponent(windowStr)}&project=${encodeURIComponent(projParam)}`;
    const resp = await fetch(url, { signal });
    if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
    const fresh = await resp.json();
    const changed = JSON.stringify(fresh) !== JSON.stringify(cached);
    // Unchanged data keeps the cached object, so a poll neither re-renders nor closes open tooltips.
    const data = changed ? fresh : cached;
    state.analyticsCache[cacheKey] = data;
    state.analyticsCacheAt[cacheKey] = Date.now();
    // Stale guard: a slower earlier request must not overwrite the view the user is now on.
    if (analyticsRequestKey(state.analyticsWindow || '6w').cacheKey !== cacheKey) return data;
    if (changed || state.analytics === null || data.git_backfill?.done === false) applyAnalyticsData(data, cacheKey);
    return data;
  } catch (err) {
    if (err.name !== 'AbortError') {
      console.error('Failed to load analytics data:', err);
    }
  } finally {
    if (!silent) showLoadingFeedback(false);
    if (window.lucide) lucide.createIcons();
  }
}

function renderAnalyticsView() {
  if (!state.analytics) return;
  const {
    kpis,
    charts,
    pipeline_health,
    weekly_cohorts,
    cohorts,
    scope_burnup,
    net_bug_flow,
    open_defects,
    all_delivered_stories,
    stories,
    projects_summary
  } = state.analytics;

  const resolvedCohorts = weekly_cohorts || cohorts || [];
  const resolvedBurnup = scope_burnup || (charts ? charts.scope_burnup || charts.burnup : null);
  const resolvedNetBug = net_bug_flow || (charts ? charts.net_bug_flow : null);
  const resolvedStories = all_delivered_stories || stories || [];

  renderAnalyticsKpis(kpis, pipeline_health, open_defects);
  renderAnalyticsReadinessBadge(kpis, pipeline_health, resolvedCohorts);
  renderPortfolioSummary(projects_summary);

  renderDailyThroughputChart(charts ? charts.daily_throughput : null, resolvedCohorts);
  renderBurnupChart(resolvedBurnup, resolvedNetBug);
  renderPipelineHealth(pipeline_health);
  renderWeeklyCohorts(resolvedCohorts);

  if (charts) {
    renderCompositionChart(charts.work_composition);
    renderQualityChart(charts.quality_friction);
  }

  renderAnalyticsStories(resolvedStories, state.analyticsFilter);
  updateTicketDrawerData(resolvedStories);
}

function renderAnalyticsReadinessBadge(kpis, pipelineHealth, weeklyCohorts) {
  const badge = document.getElementById('analyticsReadinessBadge');
  const text = document.getElementById('analyticsReadinessText');
  if (!badge || !text) return;

  const agingCount = pipelineHealth?.summary?.aging_count || 0;
  const lastCohort = weeklyCohorts && weeklyCohorts.length > 0 ? weeklyCohorts[weeklyCohorts.length - 1] : null;

  if (agingCount > 0) {
    badge.className = 'hidden sm:inline-flex items-center gap-1.5 px-2.5 py-0.5 rounded-full text-[11px] font-semibold bg-rose-500/10 text-rose-600 dark:text-rose-400 border border-rose-500/20';
    badge.innerHTML = `<span class="w-2 h-2 rounded-full bg-rose-500 animate-pulse"></span><span>Track: Stagnation (${agingCount} stuck)</span>`;
  } else if (lastCohort && lastCohort.momentum === 'bug_drag') {
    badge.className = 'hidden sm:inline-flex items-center gap-1.5 px-2.5 py-0.5 rounded-full text-[11px] font-semibold bg-amber-500/10 text-amber-600 dark:text-amber-400 border border-amber-500/20';
    badge.innerHTML = `<span class="w-2 h-2 rounded-full bg-amber-500 animate-pulse"></span><span>Track: High Bug Drag</span>`;
  } else {
    badge.className = 'hidden sm:inline-flex items-center gap-1.5 px-2.5 py-0.5 rounded-full text-[11px] font-semibold bg-emerald-500/10 text-emerald-600 dark:text-emerald-400 border border-emerald-500/20';
    badge.innerHTML = `<span class="w-2 h-2 rounded-full bg-emerald-500 animate-pulse"></span><span>Track: Advancing</span>`;
  }
}

function renderAnalyticsKpis(kpis, pipelineHealth, openDefects) {
  if (!kpis) return;

  // 1. Throughput
  const tpValEl = document.getElementById('kpiThroughputValue');
  const tpTrendEl = document.getElementById('kpiThroughputTrend');
  if (tpValEl) tpValEl.textContent = kpis.throughput?.value ?? 0;
  if (tpTrendEl) {
    const trend = kpis.throughput?.trend;
    if (trend !== null && trend !== undefined) {
      const isPos = String(trend).startsWith('+') && trend !== '+0';
      const isNeg = String(trend).startsWith('-') && trend !== '-0';
      const colorCls = isPos ? 'text-emerald-600 dark:text-emerald-400' : (isNeg ? 'text-rose-600 dark:text-rose-400' : 'text-[var(--text-secondary)]');
      const arrow = isPos ? '▲ ' : (isNeg ? '▼ ' : '');
      tpTrendEl.innerHTML = `<span class="${colorCls} font-bold">${arrow}${escapeHtml(trend)}</span> <span class="text-[var(--text-tertiary)]">vs prior period</span>`;
    } else {
      tpTrendEl.innerHTML = `<span class="text-[var(--text-tertiary)]">Across whole period</span>`;
    }
  }

  // 2. Velocity & Capacity (+ Work Volume compat)
  const velValEl = document.getElementById('kpiVelocityValue');
  const velTrendEl = document.getElementById('kpiVelocityTrend');
  const velCapEl = document.getElementById('kpiVelocityCapacity');
  const wvValEl = document.getElementById('kpiWorkVolumeValue');
  const wvChurnEl = document.getElementById('kpiWorkVolumeChurn');

  if (velValEl) velValEl.textContent = kpis.velocity?.points ?? kpis.velocity?.value ?? 0;
  if (velCapEl) {
    const cap = kpis.velocity?.capacity_pct ?? kpis.velocity?.feature_capacity_pct ?? 0;
    velCapEl.innerHTML = `<span class="inline-flex items-center px-2 py-0.5 rounded-full text-[11px] font-semibold bg-emerald-500/10 text-emerald-600 dark:text-emerald-400 border border-emerald-500/20">${cap}% Feature Capacity</span>`;
  }
  if (velTrendEl) {
    const rate = kpis.velocity?.rate_per_week;
    const trend = kpis.velocity?.trend;
    let trendHtml = '';
    if (rate !== undefined && rate !== null) {
      trendHtml += `<strong class="text-[var(--text-primary)] font-mono">${rate}</strong> <span class="text-[var(--text-secondary)]">pts/wk</span>`;
    }
    if (trend) {
      const isPos = String(trend).startsWith('+') && trend !== '+0';
      const isNeg = String(trend).startsWith('-') && trend !== '-0';
      const colorCls = isPos ? 'text-emerald-600 dark:text-emerald-400 font-bold' : (isNeg ? 'text-rose-600 dark:text-rose-400 font-bold' : 'text-[var(--text-secondary)]');
      const arrow = isPos ? '▲ ' : (isNeg ? '▼ ' : '');
      trendHtml += ` <span class="text-[var(--text-tertiary)]">•</span> <span class="${colorCls}">${arrow}${escapeHtml(trend)} vs prior</span>`;
    }
    velTrendEl.innerHTML = trendHtml || `<span class="text-[var(--text-tertiary)]">— pts/wk</span>`;
  }
  if (wvValEl) wvValEl.textContent = kpis.work_volume?.commits ?? 0;
  if (wvChurnEl) {
    const added = kpis.work_volume?.lines_added || 0;
    const deleted = kpis.work_volume?.lines_deleted || 0;
    const formatK = (n) => n >= 1000 ? `${(n / 1000).toFixed(1)}k` : `${n}`;
    wvChurnEl.innerHTML = `<span class="text-emerald-600 dark:text-emerald-400 font-semibold">+${formatK(added)}</span> / <span class="text-rose-600 dark:text-rose-400 font-semibold">-${formatK(deleted)}</span> lines churn`;
  }

  // 3. Active Turnaround & Cycle Time
  const ctValEl = document.getElementById('kpiCycleTimeValue');
  const ctTrendEl = document.getElementById('kpiCycleTimeTrend');
  const turnaround = kpis.active_turnaround || kpis.cycle_time;
  if (ctValEl) ctValEl.textContent = turnaround?.formatted || '—';
  if (ctTrendEl) {
    const trend = turnaround?.trend;
    const label = turnaround?.label || 'Active build (in-progress ➔ QA)';
    if (trend) {
      const isFaster = String(trend).startsWith('-');
      const colorCls = isFaster ? 'text-emerald-600 dark:text-emerald-400 font-semibold' : 'text-amber-600 dark:text-amber-400 font-semibold';
      ctTrendEl.innerHTML = `<span class="${colorCls}">${escapeHtml(trend)} vs prior</span> <span class="text-[var(--text-tertiary)]">• ${escapeHtml(label)}</span>`;
    } else {
      ctTrendEl.innerHTML = `<span class="text-[var(--text-tertiary)]">${escapeHtml(label)}</span>`;
    }
  }

  // 4. QA Test Health (replaces code review 1st pass metric)
  const qrValEl = document.getElementById('kpiQualityRateValue');
  const qrUnitEl = document.getElementById('kpiQualityRateUnit');
  const qrSubEl = document.getElementById('kpiQualityRateSubtitle');
  const th = kpis.test_health;

  if (qrValEl) {
    if (th && th.total_tests > 0) {
      qrValEl.textContent = `${th.passing_tests || th.total_tests}`;
      if (qrUnitEl) qrUnitEl.textContent = `Passing (${th.pass_rate_pct ?? 100}%)`;
    } else {
      const rate = kpis.quality_rate?.first_pass_rate ?? 100;
      qrValEl.textContent = `${rate}%`;
      if (qrUnitEl) qrUnitEl.textContent = 'Passing';
    }
  }
  if (qrSubEl) {
    if (th && th.total_tests > 0) {
      const added = th.new_tests_in_window ?? 0;
      const addedStr = added >= 0 ? `+${added}` : `${added}`;
      qrSubEl.innerHTML = `<span class="text-emerald-600 dark:text-emerald-400 font-semibold">${addedStr} new tests this period</span> <span class="text-[var(--text-tertiary)]">• ${th.failed_tests || 0} failures</span>`;
    } else {
      const tCount = kpis.quality_rate?.test_count || 0;
      qrSubEl.innerHTML = `<span><strong class="text-[var(--text-primary)]">${tCount}</strong> tests passing</span>`;
    }
  }

  // 5. Pipeline Health / Stagnation
  const stagBadge = document.getElementById('kpiStagnationAlertBadge');
  const stagCounts = document.getElementById('kpiStagnationCounts');
  const agingCount = pipelineHealth?.summary?.aging_count ?? pipelineHealth?.total_aging ?? pipelineHealth?.aging_count ?? 0;
  const reopenedCount = pipelineHealth?.summary?.reopened_count ?? pipelineHealth?.reopened_count ?? 0;
  const totalWip = pipelineHealth?.summary?.total_wip ?? pipelineHealth?.total_wip ?? 0;

  if (stagBadge) {
    if (agingCount > 0) {
      stagBadge.className = 'momentum-badge stagnated';
      stagBadge.innerHTML = `🔴 ${agingCount} Aging &gt;7d`;
    } else if (reopenedCount > 0) {
      stagBadge.className = 'momentum-badge bug_drag';
      stagBadge.innerHTML = `🟡 ${reopenedCount} Reopened`;
    } else {
      stagBadge.className = 'momentum-badge advancing';
      stagBadge.innerHTML = `🟢 Advancing`;
    }
  }
  if (stagCounts) {
    const agingSpan = agingCount > 0
      ? `<span class="text-rose-600 dark:text-rose-400 font-bold">${agingCount} aging &gt; 7d</span>`
      : `<span class="text-[var(--text-tertiary)]">0 aging &gt; 7d</span>`;
    const reopenedSpan = reopenedCount > 0
      ? `<span class="text-amber-600 dark:text-amber-400 font-semibold">${reopenedCount} reopened</span>`
      : `<span class="text-[var(--text-tertiary)]">0 reopened</span>`;
    stagCounts.innerHTML = `${totalWip} active WIP • ${agingSpan} • ${reopenedSpan}`;
  }
}

function renderPortfolioSummary(projectsSummary) {
  const bar = document.getElementById('analyticsPortfolioBar');
  const list = document.getElementById('analyticsPortfolioList');
  if (!bar || !list) return;

  const isPortfolio = state.analyticsScope === 'all';
  if (!isPortfolio || !projectsSummary || projectsSummary.length <= 1) {
    bar.classList.add('hidden');
    return;
  }

  bar.classList.remove('hidden');
  list.innerHTML = projectsSummary.map((p) => {
    return `
      <div class="p-3 rounded-xl bg-[var(--bg-surface-secondary)] border border-[var(--border-subtle)] flex items-center justify-between">
        <div>
          <div class="font-bold text-xs text-[var(--text-primary)] flex items-center gap-1.5">
            <span class="w-2 h-2 rounded-full bg-purple-500"></span>
            ${escapeHtml(p.name || p.slug)}
          </div>
          <div class="text-[11px] text-[var(--text-tertiary)] font-mono mt-0.5">
            ${p.commits} commits • +${(p.churn_added >= 1000 ? (p.churn_added / 1000).toFixed(1) + 'k' : p.churn_added)} / -${(p.churn_deleted >= 1000 ? (p.churn_deleted / 1000).toFixed(1) + 'k' : p.churn_deleted)} lines
          </div>
        </div>
        <div class="text-right">
          <div class="text-xs font-bold text-emerald-600 dark:text-emerald-400 tabular-nums">${p.completed} completed</div>
          <div class="text-[10px] text-amber-600 dark:text-amber-400 font-semibold">${p.active_count || 0} active</div>
        </div>
      </div>
    `;
  }).join('');
}

// -----------------------------------------------------------------------------
// Chart 1: Daily Throughput & Commits / Weekly Cohorts Stacked Bars
// -----------------------------------------------------------------------------
function renderDailyThroughputChart(data, weeklyCohorts) {
  const container = document.getElementById('chartThroughputContainer');
  if (!container) return;

  let cohorts = weeklyCohorts && weeklyCohorts.length > 0 ? weeklyCohorts : null;
  if (cohorts) {
    // Table receives reverse-chronological (newest first).
    // Ensure chronological order (oldest first, newest last) for the left-to-right timeline chart.
    const copy = [...cohorts];
    if (copy.length >= 2) {
      const d0 = copy[0].start_date || copy[0].week_key || '';
      const dN = copy[copy.length - 1].start_date || copy[copy.length - 1].week_key || '';
      if (d0 > dN) {
        copy.reverse();
      }
    }
    cohorts = copy;
  }
  const dailySeries = data && data.series && data.series.length > 0 ? data.series : null;

  if (!cohorts && dailySeries) {
    // Group daily series into weekly cohorts so view is strictly weekly
    const groups = [];
    const step = 7;
    for (let i = 0; i < dailySeries.length; i += step) {
      const chunk = dailySeries.slice(i, i + step);
      const feat = chunk.reduce((a, b) => a + (b.feature || 0), 0);
      const fix = chunk.reduce((a, b) => a + (b.fix || 0), 0);
      const chore = chunk.reduce((a, b) => a + (b.chore || b.debt || 0), 0);
      const other = chunk.reduce((a, b) => a + (b.other || 0), 0);
      const totStories = chunk.reduce((a, b) => a + (b.total_completed || 0), 0);
      const totPts = totStories * 2;
      const startD = chunk[0].date;
      const endD = chunk[chunk.length - 1].date;
      const wIdx = Math.floor(i / step) + 1;
      groups.push({
        label: `W${wIdx} (${startD.slice(5)} - ${endD.slice(5)})`,
        week_key: `W${wIdx}`,
        total_stories: totStories,
        total_points: totPts,
        capacity_pct: totStories > 0 ? Math.round((feat / totStories) * 100) : 0,
        work_composition: { feature: feat, fix: fix, improvement: 0, chore: chore, other: other },
      });
    }
    cohorts = groups;
  }

  if (!cohorts || cohorts.length === 0) {
    container.innerHTML = `<div class="text-xs text-[var(--text-tertiary)] italic p-6 text-center">No throughput data for this window.</div>`;
    return;
  }

  const vbW = 620;
  const vbH = 240;
  const padLeft = 40;
  const padRight = 40;
  const padTop = 25;
  const padBottom = 35;
  const chartW = vbW - padLeft - padRight;
  const chartH = vbH - padTop - padBottom;

  if (cohorts) {
    // Render Weekly Delivery Cohorts with Dual-Axis Points Velocity Polyline
    const n = cohorts.length;
    const maxStories = Math.max(4, ...cohorts.map((c) => c.total_stories ?? c.stories_count ?? 0));
    const maxPoints = Math.max(5, ...cohorts.map((c) => c.total_points ?? c.velocity_points ?? 0));

    const colW = chartW / n;
    const barW = Math.max(12, Math.min(36, colW * 0.55));

    // Grid lines (3 horizontal)
    let gridHtml = '';
    for (let step = 0; step <= 2; step++) {
      const yVal = padTop + (chartH / 2) * step;
      const storiesTick = Math.round(maxStories * (1 - step / 2));
      const pointsTick = Math.round(maxPoints * (1 - step / 2));
      gridHtml += `
        <line x1="${padLeft}" y1="${yVal}" x2="${padLeft + chartW}" y2="${yVal}" stroke="var(--border-subtle)" stroke-width="1" stroke-dasharray="3,3" />
        <text x="${padLeft - 8}" y="${yVal + 3}" text-anchor="end" font-size="9" fill="var(--text-tertiary)" font-family="var(--font-mono)">${storiesTick}</text>
        <text x="${padLeft + chartW + 8}" y="${yVal + 3}" text-anchor="start" font-size="9" fill="var(--text-tertiary)" font-family="var(--font-mono)">${pointsTick}p</text>
      `;
    }

    let barsHtml = '';
    const velocityPoints = [];

    cohorts.forEach((c, idx) => {
      const cx = padLeft + (idx + 0.5) * colW;
      const bx = cx - barW / 2;
      const wc = c.work_composition || c.work_mix || { feature: 0, fix: 0, improvement: 0, chore: 0, other: 0 };
      const cStories = c.total_stories ?? c.stories_count ?? 0;
      const cPoints = c.total_points ?? c.velocity_points ?? 0;
      const cCap = c.capacity_pct ?? c.feature_capacity_pct ?? 0;
      const weekKey = c.week_key || c.id || '';

      const featH = ((wc.feature || 0) / maxStories) * chartH;
      const fixH = ((wc.fix || 0) / maxStories) * chartH;
      const impH = ((wc.improvement || 0) / maxStories) * chartH;
      const choreH = ((wc.chore || 0) / maxStories) * chartH;
      const otherH = ((wc.other || 0) / maxStories) * chartH;

      let curY = padTop + chartH;

      // Feature
      if (featH > 0) {
        curY -= featH;
        barsHtml += `<rect class="chart-bar-hover" x="${bx}" y="${curY}" width="${barW}" height="${featH}" fill="#10b981" rx="2" />`;
      }
      // Fix
      if (fixH > 0) {
        curY -= fixH;
        barsHtml += `<rect class="chart-bar-hover" x="${bx}" y="${curY}" width="${barW}" height="${fixH}" fill="#f43f5e" rx="2" />`;
      }
      // Improvement
      if (impH > 0) {
        curY -= impH;
        barsHtml += `<rect class="chart-bar-hover" x="${bx}" y="${curY}" width="${barW}" height="${impH}" fill="#0ea5e9" rx="2" />`;
      }
      // Chore
      if (choreH > 0) {
        curY -= choreH;
        barsHtml += `<rect class="chart-bar-hover" x="${bx}" y="${curY}" width="${barW}" height="${choreH}" fill="#6366f1" rx="2" />`;
      }
      // Other
      if (otherH > 0) {
        curY -= otherH;
        barsHtml += `<rect class="chart-bar-hover" x="${bx}" y="${curY}" width="${barW}" height="${otherH}" fill="#8b5cf6" rx="2" />`;
      }

      // X Label: Calendar Week + short date (e.g. W42 10/19)
      const weekPart = c.chart_label || (c.week_number ? `W${c.week_number}` : (c.label || weekKey).split(' ')[0]);
      const datePart = c.start_date ? c.start_date.slice(5).replace('-', '/') : '';
      const displayLabel = datePart ? `${weekPart} ${datePart}` : weekPart;
      barsHtml += `<text x="${cx}" y="${padTop + chartH + 18}" text-anchor="middle" font-size="9" fill="var(--text-tertiary)" font-family="var(--font-mono)">${escapeHtml(displayLabel)}</text>`;

      // Velocity point
      const cy = padTop + chartH - (cPoints / maxPoints) * chartH;
      velocityPoints.push({ x: cx, y: cy, points: cPoints, weekKey: weekKey });

      // Hitbox
      const tipData = escapeHtml(JSON.stringify({
        week: c.label || weekKey,
        stories: cStories,
        points: cPoints,
        capacity: cCap,
        momentum: c.momentum_label || c.momentum || 'Advancing',
        feat: wc.feature || 0,
        fix: wc.fix || 0,
        imp: wc.improvement || 0,
        chore: wc.chore || 0,
        net_bug_flow: c.net_bug_flow,
      }));
      barsHtml += `<rect x="${padLeft + idx * colW}" y="${padTop}" width="${colW}" height="${chartH}" fill="transparent" class="cursor-pointer" data-weekly-tip='${tipData}' data-week="${escapeHtml(weekKey)}" />`;
    });

    // Velocity Polyline
    const linePts = velocityPoints.map((p) => `${p.x},${p.y}`).join(' ');
    const velocityLineHtml = `
      <polyline points="${linePts}" fill="none" stroke="#f59e0b" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round" />
      ${velocityPoints.map((p) => `
        <circle cx="${p.x}" cy="${p.y}" r="3.5" fill="#f59e0b" stroke="var(--bg-surface)" stroke-width="1.5" />
      `).join('')}
    `;

    container.innerHTML = `
      <svg viewBox="0 0 ${vbW} ${vbH}" preserveAspectRatio="none" class="w-full h-[240px] select-none" id="svgWeeklyThroughput">
        ${gridHtml}
        ${barsHtml}
        ${velocityLineHtml}
      </svg>
    `;

    container.querySelectorAll('[data-weekly-tip]').forEach((el) => {
      el.addEventListener('mousemove', (e) => {
        try {
          const d = JSON.parse(el.getAttribute('data-weekly-tip'));
          const netBug = d.net_bug_flow;
          const netBugStr = netBug ? `${netBug.net > 0 ? '+' + netBug.net : netBug.net} (${netBug.found} found / ${netBug.fixed} fixed)` : '—';
          const html = `
            <div class="font-bold text-xs text-[var(--text-primary)] mb-1">${escapeHtml(d.week)}</div>
            <div class="space-y-0.5 text-[11px]">
              <div class="flex items-center justify-between gap-3"><span class="text-emerald-500 font-semibold">Delivered:</span> <strong>${d.stories} stories</strong></div>
              <div class="flex items-center justify-between gap-3"><span class="text-amber-500 font-semibold">Velocity:</span> <strong>${d.points} pts (${d.capacity}% cap)</strong></div>
              <div class="text-[10px] text-[var(--text-tertiary)] pt-0.5">Feat: ${d.feat} • Fix: ${d.fix} • Imp: ${d.imp} • Chore: ${d.chore}</div>
              <div class="flex items-center justify-between gap-3 mt-1 pt-1 border-t border-[var(--border-subtle)]"><span class="text-rose-500 font-semibold">Net Bug Flow:</span> <strong>${netBugStr}</strong></div>
              <div class="text-[10px] text-[var(--text-tertiary)]">Momentum: <strong class="text-[var(--text-primary)]">${escapeHtml(d.momentum)}</strong></div>
            </div>
            <div class="text-[9px] text-[var(--text-quaternary)] mt-1.5 italic">Click to expand week cohort</div>
          `;
          showAnalyticsTooltip(e, html);
        } catch (_) {}
      });
      el.addEventListener('mouseleave', hideAnalyticsTooltip);
      el.addEventListener('click', () => {
        const wk = el.getAttribute('data-week');
        if (wk) toggleCohortAccordion(wk);
      });
    });
    return;
  }

  // Daily Series Fallback
  const series = dailySeries;
  const maxCompleted = Math.max(4, ...series.map((s) => s.total_completed));
  const maxCommits = Math.max(5, ...series.map((s) => s.commits));
  const n = series.length;
  const colW = chartW / n;
  const barW = Math.max(8, Math.min(26, colW * 0.55));

  let gridHtml = '';
  for (let step = 0; step <= 2; step++) {
    const yVal = padTop + (chartH / 2) * step;
    const storiesTick = Math.round(maxCompleted * (1 - step / 2));
    const commitsTick = Math.round(maxCommits * (1 - step / 2));
    gridHtml += `
      <line x1="${padLeft}" y1="${yVal}" x2="${padLeft + chartW}" y2="${yVal}" stroke="var(--border-subtle)" stroke-width="1" stroke-dasharray="3,3" />
      <text x="${padLeft - 8}" y="${yVal + 3}" text-anchor="end" font-size="9" fill="var(--text-tertiary)" font-family="var(--font-mono)">${storiesTick}</text>
      <text x="${padLeft + chartW + 8}" y="${yVal + 3}" text-anchor="start" font-size="9" fill="var(--text-tertiary)" font-family="var(--font-mono)">${commitsTick}</text>
    `;
  }

  let barsHtml = '';
  const commitPoints = [];

  series.forEach((s, idx) => {
    const cx = padLeft + (idx + 0.5) * colW;
    const bx = cx - barW / 2;
    const featH = (s.feature / maxCompleted) * chartH;
    const fixH = (s.fix / maxCompleted) * chartH;
    const choreH = ((s.chore || s.debt || 0) / maxCompleted) * chartH;
    const otherH = (s.other / maxCompleted) * chartH;

    let curY = padTop + chartH;
    if (featH > 0) { curY -= featH; barsHtml += `<rect class="chart-bar-hover" x="${bx}" y="${curY}" width="${barW}" height="${featH}" fill="#10b981" rx="2" data-date="${s.date}" />`; }
    if (fixH > 0) { curY -= fixH; barsHtml += `<rect class="chart-bar-hover" x="${bx}" y="${curY}" width="${barW}" height="${fixH}" fill="#f43f5e" rx="2" data-date="${s.date}" />`; }
    if (choreH > 0) { curY -= choreH; barsHtml += `<rect class="chart-bar-hover" x="${bx}" y="${curY}" width="${barW}" height="${choreH}" fill="#6366f1" rx="2" data-date="${s.date}" />`; }
    if (otherH > 0) { curY -= otherH; barsHtml += `<rect class="chart-bar-hover" x="${bx}" y="${curY}" width="${barW}" height="${otherH}" fill="#8b5cf6" rx="2" data-date="${s.date}" />`; }

    const tipData = escapeHtml(JSON.stringify({
      date: s.label || s.date,
      completed: s.total_completed,
      feature: s.feature,
      fix: s.fix,
      chore: s.chore || 0,
      commits: s.commits,
      churn_added: s.lines_added,
      churn_deleted: s.lines_deleted,
    }));
    barsHtml += `<rect x="${padLeft + idx * colW}" y="${padTop}" width="${colW}" height="${chartH}" fill="transparent" class="cursor-pointer" data-tip='${tipData}' data-date="${s.date}" />`;

    const showLabel = n <= 14 || idx % 2 === 0;
    if (showLabel) {
      barsHtml += `<text x="${cx}" y="${padTop + chartH + 18}" text-anchor="middle" font-size="9" fill="var(--text-tertiary)" font-family="var(--font-mono)">${escapeHtml(s.label || s.date.slice(5))}</text>`;
    }

    const cy = padTop + chartH - (s.commits / maxCommits) * chartH;
    commitPoints.push({ x: cx, y: cy, commits: s.commits, date: s.date });
  });

  const ptsString = commitPoints.map((p) => `${p.x},${p.y}`).join(' ');
  const lineHtml = `
    <polyline points="${ptsString}" fill="none" stroke="#f59e0b" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round" />
    ${commitPoints.map((p) => `<circle cx="${p.x}" cy="${p.y}" r="3.5" fill="#f59e0b" stroke="var(--bg-surface)" stroke-width="1.5" />`).join('')}
  `;

  container.innerHTML = `
    <svg viewBox="0 0 ${vbW} ${vbH}" preserveAspectRatio="none" class="w-full h-[240px] select-none" id="svgDailyThroughput">
      ${gridHtml}
      ${barsHtml}
      ${lineHtml}
    </svg>
  `;

  container.querySelectorAll('[data-tip]').forEach((el) => {
    el.addEventListener('mousemove', (e) => {
      try {
        const d = JSON.parse(el.getAttribute('data-tip'));
        const html = `
          <div class="font-bold text-xs text-[var(--text-primary)] mb-1">${escapeHtml(d.date)}</div>
          <div class="space-y-0.5 text-[11px]">
            <div class="flex items-center justify-between gap-3"><span class="text-emerald-500 font-semibold">Completed:</span> <strong>${d.completed}</strong></div>
            <div class="text-[10px] text-[var(--text-tertiary)] pl-2">Feature: ${d.feature} • Fix: ${d.fix} • Chore: ${d.chore}</div>
            <div class="flex items-center justify-between gap-3 mt-1 pt-1 border-t border-[var(--border-subtle)]"><span class="text-amber-500 font-semibold">Git Commits:</span> <strong>${d.commits}</strong></div>
            <div class="text-[10px] text-[var(--text-tertiary)] pl-2">+${d.churn_added} / -${d.churn_deleted} lines</div>
          </div>
          <div class="text-[9px] text-[var(--text-quaternary)] mt-1.5 italic">Click bar to drill down stories</div>
        `;
        showAnalyticsTooltip(e, html);
      } catch (_) {}
    });
    el.addEventListener('mouseleave', hideAnalyticsTooltip);
    el.addEventListener('click', () => {
      const dt = el.getAttribute('data-date');
      if (dt) filterAnalyticsStoriesByDate(dt);
    });
  });
}

// -----------------------------------------------------------------------------
// Chart 2: Cumulative Scope Burnup & Net Bug Flow
// -----------------------------------------------------------------------------
function renderBurnupChart(scopeBurnupData, netBugFlowData) {
  const container = document.getElementById('chartBurnupContainer');
  if (!container) return;

  const mode = state.burnupMode || 'burnup';

  if (mode === 'netbug') {
    // Render Net Bug Flow (Bugs Found vs Bugs Fixed)
    const netSeries = (netBugFlowData && netBugFlowData.series)
      ? netBugFlowData.series
      : (state.analytics?.net_bug_flow?.series || state.analytics?.charts?.net_bug_flow?.series || []);
    if (netSeries.length === 0) {
      container.innerHTML = `<div class="text-xs text-[var(--text-tertiary)] italic p-6 text-center">No bug flow recorded in this window.</div>`;
      return;
    }

    const vbW = 620;
    const vbH = 240;
    const padLeft = 40;
    const padRight = 30;
    const padTop = 25;
    const padBottom = 35;
    const chartW = vbW - padLeft - padRight;
    const chartH = vbH - padTop - padBottom;

    const maxBugs = Math.max(3, ...netSeries.map((s) => Math.max(s.bugs_found || 0, s.bugs_fixed || 0)));
    const n = netSeries.length;
    const colW = chartW / n;
    const barW = Math.max(6, Math.min(18, colW * 0.28));

    let gridHtml = '';
    for (let step = 0; step <= 2; step++) {
      const yVal = padTop + (chartH / 2) * step;
      const bugTick = Math.round(maxBugs * (1 - step / 2));
      gridHtml += `
        <line x1="${padLeft}" y1="${yVal}" x2="${padLeft + chartW}" y2="${yVal}" stroke="var(--border-subtle)" stroke-width="1" stroke-dasharray="3,3" />
        <text x="${padLeft - 8}" y="${yVal + 3}" text-anchor="end" font-size="9" fill="var(--text-tertiary)" font-family="var(--font-mono)">${bugTick}</text>
      `;
    }

    let barsHtml = '';

    netSeries.forEach((s, idx) => {
      const cx = padLeft + (idx + 0.5) * colW;
      const bFoundX = cx - barW - 1;
      const bFixedX = cx + 1;

      const foundH = (s.bugs_found / maxBugs) * chartH;
      const fixedH = (s.bugs_fixed / maxBugs) * chartH;

      const yFound = padTop + chartH - foundH;
      const yFixed = padTop + chartH - fixedH;

      if (foundH > 0) {
        barsHtml += `<rect x="${bFoundX}" y="${yFound}" width="${barW}" height="${foundH}" fill="#ef4444" rx="2" />`;
      }
      if (fixedH > 0) {
        barsHtml += `<rect x="${bFixedX}" y="${yFixed}" width="${barW}" height="${fixedH}" fill="#10b981" rx="2" />`;
      }

      const topY = Math.min(yFound, yFixed) - 6;
      const netVal = s.net !== undefined ? s.net : (s.bugs_found - s.bugs_fixed);
      const netColor = netVal > 0 ? '#ef4444' : (netVal < 0 ? '#10b981' : 'var(--text-tertiary)');
      const netPrefix = netVal > 0 ? '+' : '';
      barsHtml += `<text x="${cx}" y="${Math.max(padTop - 5, topY)}" text-anchor="middle" font-size="8.5" font-weight="bold" fill="${netColor}" font-family="var(--font-mono)">${netPrefix}${netVal}</text>`;

      barsHtml += `<text x="${cx}" y="${padTop + chartH + 18}" text-anchor="middle" font-size="9" fill="var(--text-tertiary)" font-family="var(--font-mono)">${escapeHtml(s.label || s.week_key)}</text>`;

      const tipData = escapeHtml(JSON.stringify({
        week: s.label || s.week_key,
        found: s.bugs_found,
        fixed: s.bugs_fixed,
        net: s.net,
      }));
      barsHtml += `<rect x="${padLeft + idx * colW}" y="${padTop}" width="${colW}" height="${chartH}" fill="transparent" class="cursor-pointer" data-bug-tip='${tipData}' />`;
    });

    container.innerHTML = `
      <svg viewBox="0 0 ${vbW} ${vbH}" preserveAspectRatio="none" class="w-full h-[240px] select-none" id="svgNetBugFlow">
        ${gridHtml}
        ${barsHtml}
      </svg>
    `;

    container.querySelectorAll('[data-bug-tip]').forEach((el) => {
      el.addEventListener('mousemove', (e) => {
        try {
          const d = JSON.parse(el.getAttribute('data-bug-tip'));
          const netSign = d.net > 0 ? `+${d.net} (Defect Drag)` : (d.net < 0 ? `${d.net} (Net Burned)` : '0 (Balanced)');
          const html = `
            <div class="font-bold text-xs text-[var(--text-primary)] mb-1">${escapeHtml(d.week)}</div>
            <div class="space-y-0.5 text-[11px]">
              <div class="flex items-center justify-between gap-3"><span class="text-rose-500 font-semibold">Bugs Found:</span> <strong>${d.found}</strong></div>
              <div class="flex items-center justify-between gap-3"><span class="text-emerald-500 font-semibold">Bugs Fixed:</span> <strong>${d.fixed}</strong></div>
              <div class="flex items-center justify-between gap-3 mt-1 pt-1 border-t border-[var(--border-subtle)]"><span class="text-purple-500 font-semibold">Net Bug Flow:</span> <strong>${netSign}</strong></div>
            </div>
          `;
          showAnalyticsTooltip(e, html);
        } catch (_) {}
      });
      el.addEventListener('mouseleave', hideAnalyticsTooltip);
    });
    return;
  }

  // Scope Burnup Mode
  const burnupData = scopeBurnupData || state.analytics?.scope_burnup || (state.analytics?.charts ? state.analytics.charts.scope_burnup || state.analytics.charts.burnup : null);
  if (!burnupData || !burnupData.series || burnupData.series.length === 0) {
    container.innerHTML = `<div class="text-xs text-[var(--text-tertiary)] italic p-6 text-center">No burnup data for this window.</div>`;
    return;
  }

  const series = burnupData.series;
  const vbW = 620;
  const vbH = 240;
  const padLeft = 40;
  const padRight = 30;
  const padTop = 25;
  const padBottom = 35;
  const chartW = vbW - padLeft - padRight;
  const chartH = vbH - padTop - padBottom;

  const maxVal = Math.max(5, ...series.map((s) => Math.max(s.cumulative_created || s.cumulative_scope || 0, s.cumulative_completed || s.cumulative_done || 0)));
  const n = series.length;
  const colW = chartW / Math.max(1, n - 1);

  let gridHtml = '';
  for (let step = 0; step <= 2; step++) {
    const yVal = padTop + (chartH / 2) * step;
    const tickVal = Math.round(maxVal * (1 - step / 2));
    gridHtml += `
      <line x1="${padLeft}" y1="${yVal}" x2="${padLeft + chartW}" y2="${yVal}" stroke="var(--border-subtle)" stroke-width="1" stroke-dasharray="3,3" />
      <text x="${padLeft - 8}" y="${yVal + 3}" text-anchor="end" font-size="9" fill="var(--text-tertiary)" font-family="var(--font-mono)">${tickVal}</text>
    `;
  }

  const compPts = [];
  const scopePts = [];

  series.forEach((s, idx) => {
    const x = padLeft + idx * colW;
    const doneVal = s.cumulative_completed !== undefined ? s.cumulative_completed : (s.cumulative_done || 0);
    const scopeVal = s.cumulative_created !== undefined ? s.cumulative_created : (s.cumulative_scope || 0);
    const yComp = padTop + chartH - (doneVal / maxVal) * chartH;
    const yScope = padTop + chartH - (scopeVal / maxVal) * chartH;
    compPts.push({ x, y: yComp, s, doneVal, scopeVal });
    scopePts.push({ x, y: yScope, s });
  });

  const areaPts = [
    `${compPts[0].x},${padTop + chartH}`,
    ...compPts.map((p) => `${p.x},${p.y}`),
    `${compPts[compPts.length - 1].x},${padTop + chartH}`,
  ].join(' ');

  const compLine = compPts.map((p) => `${p.x},${p.y}`).join(' ');
  const scopeLine = scopePts.map((p) => `${p.x},${p.y}`).join(' ');

  let labelsHtml = '';
  series.forEach((s, idx) => {
    const showLabel = n <= 14 || idx % 2 === 0;
    if (showLabel) {
      const x = padLeft + idx * colW;
      labelsHtml += `<text x="${x}" y="${padTop + chartH + 18}" text-anchor="middle" font-size="9" fill="var(--text-tertiary)" font-family="var(--font-mono)">${escapeHtml(s.label || (s.date ? s.date.slice(5) : ''))}</text>`;
    }
  });

  let hitsHtml = '';
  compPts.forEach((p) => {
    const s = p.s;
    const tip = escapeHtml(JSON.stringify({
      date: s.label || s.date,
      completed: p.doneVal,
      created: p.scopeVal,
      in_flight: s.in_flight_wip !== undefined ? s.in_flight_wip : (s.in_flight || Math.max(0, p.scopeVal - p.doneVal)),
    }));
    hitsHtml += `
      <circle cx="${p.x}" cy="${p.y}" r="4" fill="#a855f7" stroke="var(--bg-surface)" stroke-width="1.5" class="cursor-pointer" data-burn-tip='${tip}' data-date="${s.date || ''}" />
    `;
  });

  container.innerHTML = `
    <svg viewBox="0 0 ${vbW} ${vbH}" preserveAspectRatio="none" class="w-full h-[240px] select-none" id="svgBurnup">
      <defs>
        <linearGradient id="burnupAreaGrad" x1="0" y1="0" x2="0" y2="1">
          <stop offset="0%" stop-color="#a855f7" stop-opacity="0.32" />
          <stop offset="100%" stop-color="#a855f7" stop-opacity="0.02" />
        </linearGradient>
      </defs>
      ${gridHtml}
      <polygon points="${areaPts}" fill="url(#burnupAreaGrad)" />
      <polyline points="${scopeLine}" fill="none" stroke="#0ea5e9" stroke-width="2" stroke-dasharray="4,4" />
      <polyline points="${compLine}" fill="none" stroke="#a855f7" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round" />
      ${labelsHtml}
      ${hitsHtml}
    </svg>
  `;

  container.querySelectorAll('[data-burn-tip]').forEach((el) => {
    el.addEventListener('mousemove', (e) => {
      try {
        const d = JSON.parse(el.getAttribute('data-burn-tip'));
        const html = `
          <div class="font-bold text-xs text-[var(--text-primary)] mb-1">${escapeHtml(d.date)}</div>
          <div class="space-y-0.5 text-[11px]">
            <div class="flex items-center justify-between gap-3"><span class="text-purple-500 font-semibold">Cumulative Done:</span> <strong>${d.completed}</strong></div>
            <div class="flex items-center justify-between gap-3"><span class="text-sky-500 font-semibold">Total Scope:</span> <strong>${d.created}</strong></div>
            <div class="flex items-center justify-between gap-3 mt-1 pt-1 border-t border-[var(--border-subtle)]"><span class="text-amber-500 font-semibold">In-Flight WIP:</span> <strong>${d.in_flight}</strong></div>
          </div>
          <div class="text-[9px] text-[var(--text-quaternary)] mt-1.5 italic">Click to view stories up to this date</div>
        `;
        showAnalyticsTooltip(e, html);
      } catch (_) {}
    });
    el.addEventListener('mouseleave', hideAnalyticsTooltip);
    el.addEventListener('click', () => {
      const dt = el.getAttribute('data-date');
      if (dt) filterAnalyticsStoriesByDate(dt);
    });
  });
}

// -----------------------------------------------------------------------------
// Pipeline Health & WIP Stagnation Monitor
// -----------------------------------------------------------------------------
function renderPipelineHealth(pipelineHealth) {
  const tbody = document.getElementById('pipelineHealthTableBody');
  const summaryBadge = document.getElementById('stagnationSummaryBadge');
  if (!pipelineHealth) return;

  if (summaryBadge && pipelineHealth) {
    const aging = pipelineHealth.summary?.aging_count ?? pipelineHealth.total_aging ?? pipelineHealth.aging_count ?? 0;
    const wip = pipelineHealth.summary?.total_wip ?? pipelineHealth.total_wip ?? 0;
    summaryBadge.innerHTML = `<span class="${aging > 0 ? 'text-rose-600 dark:text-rose-400 font-bold' : ''}">${aging} aging</span> • ${wip} active WIP`;
  }

  if (tbody) {
    const stages = pipelineHealth.stages || [];
    if (stages.length === 0) {
      tbody.innerHTML = `<tr><td colspan="5" class="py-4 text-center text-[var(--text-tertiary)] italic">No active WIP cards in pipeline.</td></tr>`;
    } else {
      tbody.innerHTML = stages.map((st) => {
        const isSelected = state.selectedPipelineStage === st.id;
        const icon = st.id === 'ready' ? 'ph-stack text-sky-500'
          : (st.id === 'development' || st.id === 'in_progress' ? 'ph-code text-amber-500'
          : (st.id === 'qa' ? 'ph-shield-check text-purple-500' : 'ph-prohibit text-rose-500'));
        const agingBadge = st.aging_count > 0
          ? `<span class="px-2 py-0.5 rounded-full bg-rose-500/15 text-rose-600 dark:text-rose-400 font-bold font-mono">🔴 ${st.aging_count}</span>`
          : `<span class="text-[var(--text-tertiary)] font-mono">0</span>`;
        const statusBadge = st.aging_count > 0
          ? `<span class="momentum-badge stagnated">Stagnated</span>`
          : `<span class="momentum-badge advancing">Healthy</span>`;

        return `
          <tr class="hover:bg-[var(--bg-glass-hover)] transition cursor-pointer ${isSelected ? 'bg-purple-500/10 font-semibold' : ''}" onclick="togglePipelineStageFilter('${escapeHtml(st.id)}')">
            <td class="py-2.5 px-2 flex items-center gap-2">
              <i class="ph ${icon} text-sm"></i>
              <span class="text-[var(--text-primary)]">${escapeHtml(st.name)}</span>
            </td>
            <td class="py-2.5 px-2 text-right font-mono font-semibold tabular-nums text-[var(--text-primary)]">${st.count}</td>
            <td class="py-2.5 px-2 text-right font-mono tabular-nums text-[var(--text-secondary)]">${st.points || 0}p</td>
            <td class="py-2.5 px-2 text-right">${agingBadge}</td>
            <td class="py-2.5 px-2 text-right">${statusBadge}</td>
          </tr>
        `;
      }).join('');
    }
  }

  renderStuckTickets(pipelineHealth.aging_tickets || pipelineHealth.stuck_tickets || []);
}

function togglePipelineStageFilter(stageId) {
  state.selectedPipelineStage = state.selectedPipelineStage === stageId ? null : stageId;
  if (state.analytics && state.analytics.pipeline_health) {
    renderPipelineHealth(state.analytics.pipeline_health);
  }
}

function renderStuckTickets(agingTickets) {
  const listEl = document.getElementById('stuckTicketsList');
  const countEl = document.getElementById('stuckTicketsCount');
  if (!listEl) return;

  let filtered = agingTickets || [];
  if (state.selectedPipelineStage) {
    filtered = filtered.filter((t) => t.stage_id === state.selectedPipelineStage);
  }

  if (countEl) countEl.textContent = `${filtered.length} tickets`;

  if (filtered.length === 0) {
    listEl.innerHTML = `
      <div class="p-4 rounded-xl bg-emerald-500/5 border border-emerald-500/20 text-center text-xs text-emerald-600 dark:text-emerald-400 font-medium">
        <i class="ph ph-check-circle mr-1"></i> No tickets exceeding 7 days in stage. Pipeline moving with healthy flow!
      </div>
    `;
    return;
  }

  listEl.innerHTML = filtered.map((t) => {
    const ageDays = t.age_days ?? t.days_in_stage ?? 0;
    const daysOver = t.days_over_threshold ?? Math.max(0, (ageDays - 7)).toFixed(1);
    const stageBadgeBg = t.is_blocked
      ? 'bg-rose-500/10 text-rose-600 dark:text-rose-400 border-rose-500/20'
      : (t.stage_id === 'qa' ? 'bg-purple-500/10 text-purple-600 dark:text-purple-400 border-purple-500/20' : 'bg-amber-500/10 text-amber-600 dark:text-amber-400 border-amber-500/20');

    return `
      <div class="stuck-ticket-card p-3 rounded-xl bg-[var(--bg-surface-secondary)] border border-[var(--border-subtle)] hover:border-amber-500/40 transition cursor-pointer" onclick="openStoryFromAnalytics('${escapeHtml(t.id)}')">
        <div class="flex items-center justify-between gap-2 mb-1.5">
          <div class="flex items-center gap-1.5 flex-wrap min-w-0">
            <span class="font-mono text-xs font-bold text-emerald-600 dark:text-emerald-400 hover:underline">${escapeHtml(t.id)}</span>
            <span class="px-1.5 py-0.2 text-[10px] font-semibold rounded border ${stageBadgeBg}">${escapeHtml(t.stage)}</span>
            ${t.is_reopened ? `<span class="px-1.5 py-0.2 text-[10px] font-semibold rounded bg-amber-500/15 text-amber-600 dark:text-amber-400 border border-amber-500/30">Reopened</span>` : ''}
            ${t.is_blocked ? `<span class="px-1.5 py-0.2 text-[10px] font-semibold rounded bg-rose-500/15 text-rose-600 dark:text-rose-400 border border-rose-500/30">Blocked</span>` : ''}
          </div>
          <span class="text-[11px] font-mono font-bold text-amber-600 dark:text-amber-400 shrink-0">${t.points || 1} pts</span>
        </div>
        <h5 class="text-xs font-semibold text-[var(--text-primary)] truncate mb-2">${escapeHtml(t.title || t.id)}</h5>
        <div class="flex items-center justify-between text-[11px] text-[var(--text-secondary)] pt-1 border-t border-[var(--border-subtle)]">
          <span class="text-rose-600 dark:text-rose-400 font-bold font-mono">
            <i class="ph ph-hourglass mr-0.5"></i> ${ageDays}d in stage
            <span class="text-rose-500/80 font-normal text-[10px]">(+${daysOver}d over SLA)</span>
          </span>
          ${t.lead ? `<span class="text-[10px] text-[var(--text-tertiary)] truncate max-w-[120px]">${escapeHtml(t.lead)}</span>` : ''}
        </div>
      </div>
    `;
  }).join('');
}

// -----------------------------------------------------------------------------
// Weekly Delivery Cohorts Matrix (Story 29 Core Table)
// -----------------------------------------------------------------------------
function renderWeeklyCohorts(weeklyCohorts) {
  const tbody = document.getElementById('weeklyCohortsTableBody');
  const totalBadge = document.getElementById('cohortsTotalBadge');
  if (!tbody) return;

  const cohorts = weeklyCohorts || [];
  if (totalBadge) totalBadge.textContent = `${cohorts.length} Cohorts`;

  if (cohorts.length === 0) {
    tbody.innerHTML = `<tr><td colspan="7" class="py-6 text-center text-[var(--text-tertiary)] italic">No weekly delivery cohorts data found.</td></tr>`;
    return;
  }

  tbody.innerHTML = cohorts.map((c) => {
    const weekKey = c.week_key || c.id || '';
    const isExpanded = state.expandedCohorts.has(weekKey);
    const wc = c.work_composition || c.work_mix || { feature: 0, fix: 0, improvement: 0, chore: 0, other: 0 };
    const totalStories = c.total_stories ?? c.stories_count ?? 0;
    const totalPoints = c.total_points ?? c.velocity_points ?? 0;
    const capPct = c.capacity_pct ?? c.feature_capacity_pct ?? 0;
    const tot = totalStories || 1;

    const pctFeat = Math.round(((wc.feature || 0) / tot) * 100);
    const pctFix = Math.round(((wc.fix || 0) / tot) * 100);
    const pctImp = Math.round(((wc.improvement || 0) / tot) * 100);
    const pctChore = Math.round(((wc.chore || 0) / tot) * 100);
    const pctOther = Math.max(0, 100 - pctFeat - pctFix - pctImp - pctChore);

    const momentumCls = c.momentum === 'stagnated' ? 'stagnated' : (c.momentum === 'bug_drag' ? 'bug_drag' : 'advancing');
    const momentumIcon = c.momentum === 'stagnated' ? '🔴' : (c.momentum === 'bug_drag' ? '🟡' : '🟢');

    const netBugs = c.net_bug_flow || { found: 0, fixed: 0, net: 0 };
    const netCls = netBugs.net > 0 ? 'text-rose-600 dark:text-rose-400 font-bold' : (netBugs.net < 0 ? 'text-emerald-600 dark:text-emerald-400 font-bold' : 'text-[var(--text-tertiary)] font-normal');
    const netPrefix = netBugs.net > 0 ? '+' : '';

    const qaRate = c.qa_rate?.first_pass_rate ?? c.qa_pass_rate ?? 100;
    const qaCls = qaRate >= 90 ? 'text-emerald-600 dark:text-emerald-400 font-bold' : 'text-amber-600 font-semibold';
    const turnaroundStr = c.active_turnaround?.formatted || c.cycle_time?.formatted || '—';

    const storiesList = c.stories || [];

    return `
      <tr class="cohort-table-row">
        <td class="py-3 px-3">
          <div class="font-bold text-[var(--text-primary)] font-mono">${escapeHtml(c.label || weekKey)}</div>
          <div class="text-[10px] text-[var(--text-tertiary)] font-mono">${escapeHtml(c.start_date || '')} → ${escapeHtml(c.end_date || '')}</div>
        </td>
        <td class="py-3 px-3 text-center">
          <span class="momentum-badge ${momentumCls}">${momentumIcon} ${escapeHtml(c.momentum_label || c.momentum || 'Advancing')}</span>
        </td>
        <td class="py-3 px-3 text-right font-mono tabular-nums">
          <div class="font-bold text-sm text-[var(--text-primary)]">${totalStories} <span class="text-[10px] font-normal text-[var(--text-tertiary)]">stories</span></div>
          <div class="text-[11px] font-semibold text-amber-600 dark:text-amber-400">${totalPoints} pts <span class="text-[10px] text-[var(--text-tertiary)] font-normal">(${capPct}% cap)</span></div>
        </td>
        <td class="py-3 px-3">
          <div class="w-28 sm:w-36 h-2 rounded-full bg-[var(--border-subtle)] overflow-hidden flex shadow-xs" title="Feat: ${wc.feature || 0}, Imp: ${wc.improvement || 0}, Fix: ${wc.fix || 0}, Chore: ${wc.chore || 0}">
            ${pctFeat > 0 ? `<div style="width: ${pctFeat}%" class="bg-emerald-500 h-full"></div>` : ''}
            ${pctImp > 0 ? `<div style="width: ${pctImp}%" class="bg-sky-500 h-full"></div>` : ''}
            ${pctFix > 0 ? `<div style="width: ${pctFix}%" class="bg-rose-500 h-full"></div>` : ''}
            ${pctChore > 0 ? `<div style="width: ${pctChore}%" class="bg-indigo-500 h-full"></div>` : ''}
            ${pctOther > 0 ? `<div style="width: ${pctOther}%" class="bg-purple-500 h-full"></div>` : ''}
          </div>
          <div class="text-[9px] text-[var(--text-tertiary)] mt-1 font-mono">F:${wc.feature || 0} • I:${wc.improvement || 0} • B:${wc.fix || 0} • C:${wc.chore || 0}</div>
        </td>
        <td class="py-3 px-3 text-center font-mono text-xs">
          <span class="${netCls}">${netPrefix}${netBugs.net}</span>
          <div class="text-[9px] text-[var(--text-tertiary)]">${netBugs.found}F / ${netBugs.fixed}X</div>
        </td>
        <td class="py-3 px-3 text-right font-mono text-xs">
          <div class="text-[var(--text-primary)] font-semibold">${escapeHtml(turnaroundStr)}</div>
          <div class="${qaCls} text-[10px]">${qaRate}% pass</div>
        </td>
        <td class="py-3 px-3 text-center">
          <button type="button" onclick="toggleCohortAccordion('${escapeHtml(weekKey)}')" class="inline-flex items-center gap-1.5 px-2.5 py-1 rounded-lg bg-[var(--bg-surface-secondary)] hover:bg-[var(--bg-surface-tertiary)] border border-[var(--border-subtle)] text-[11px] font-semibold text-[var(--text-primary)] transition cursor-pointer shadow-xs" title="Toggle cohort stories">
            <span>View ${totalStories} Stories</span>
            <i id="cohort-chevron-${escapeHtml(weekKey)}" class="ph ph-caret-down cohort-chevron ${isExpanded ? 'rotated' : ''}"></i>
          </button>
        </td>
      </tr>
      <tr id="cohort-detail-${escapeHtml(weekKey)}" class="${isExpanded ? '' : 'hidden'} bg-[var(--bg-surface-secondary)]/30">
        <td colspan="7" class="p-3 sm:p-4">
          <div class="p-3 rounded-xl bg-[var(--bg-surface)] border border-[var(--border-subtle)] shadow-xs">
            <div class="text-xs font-semibold text-[var(--text-secondary)] uppercase tracking-wider mb-2 flex items-center justify-between">
              <span>Delivered Stories in ${escapeHtml(c.label || weekKey)}</span>
              <span class="font-mono text-[10px] text-[var(--text-tertiary)] font-normal">${storiesList.length} total deliverables</span>
            </div>
            ${storiesList.length === 0 ? `<div class="text-xs text-[var(--text-tertiary)] italic py-2">No individual stories found for this cohort.</div>` : `
              <div class="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-2.5">
                ${storiesList.map((s) => {
                  const pts = s.points !== undefined ? `${s.points}p` : '—';
                  const typeBg = s.type === 'fix' ? 'bg-rose-500/10 text-rose-600 dark:text-rose-400 border-rose-500/25'
                    : (s.type === 'improvement' ? 'bg-sky-500/10 text-sky-600 dark:text-sky-400 border-sky-500/25'
                    : (s.type === 'chore' ? 'bg-indigo-500/10 text-indigo-600 dark:text-indigo-400 border-indigo-500/25' : 'bg-emerald-500/10 text-emerald-600 dark:text-emerald-400 border-emerald-500/25'));
                  return `
                    <div class="p-2.5 rounded-lg bg-[var(--bg-surface-secondary)] border border-[var(--border-subtle)] hover:border-[var(--card-hover-border)] transition flex flex-col justify-between cursor-pointer group" onclick="openStoryFromAnalytics('${escapeHtml(s.id)}')">
                      <div>
                        <div class="flex items-center justify-between gap-1 mb-1">
                          <span class="font-mono text-xs font-bold text-emerald-600 dark:text-emerald-400 hover:underline">${escapeHtml(s.id)}</span>
                          <div class="flex items-center gap-1">
                            <span class="px-1.5 py-0.2 text-[9px] font-semibold rounded border ${typeBg}">${escapeHtml(s.type || 'feat')}</span>
                            <span class="px-1 py-0.2 text-[9px] font-mono font-bold rounded bg-amber-500/10 text-amber-600 dark:text-amber-400 border border-amber-500/20">${pts}</span>
                          </div>
                        </div>
                        <h6 class="text-xs font-medium text-[var(--text-primary)] truncate group-hover:text-purple-600 dark:group-hover:text-purple-400 transition">${escapeHtml(s.title || s.id)}</h6>
                      </div>
                      <div class="flex items-center justify-between text-[10px] text-[var(--text-tertiary)] pt-1.5 mt-1 border-t border-[var(--border-subtle)] font-mono">
                        <span>${escapeHtml(s.duration || '—')}</span>
                        ${s.lead ? `<span class="truncate max-w-[100px]">${escapeHtml(s.lead)}</span>` : ''}
                      </div>
                    </div>
                  `;
                }).join('')}
              </div>
            `}
          </div>
        </td>
      </tr>
    `;
  }).join('');
}

function toggleCohortAccordion(weekKey) {
  const row = document.getElementById(`cohort-detail-${weekKey}`);
  const chev = document.getElementById(`cohort-chevron-${weekKey}`);
  if (!row) return;

  const isHidden = row.classList.contains('hidden');
  if (isHidden) {
    row.classList.remove('hidden');
    state.expandedCohorts.add(weekKey);
    if (chev) chev.classList.add('rotated');
  } else {
    row.classList.add('hidden');
    state.expandedCohorts.delete(weekKey);
    if (chev) chev.classList.remove('rotated');
  }
}

// -----------------------------------------------------------------------------
// Slide-Over All Delivered Tickets Drawer
// -----------------------------------------------------------------------------
function openTicketDrawer(query, type) {
  const drawer = document.getElementById('ticketDrawer');
  const backdrop = document.getElementById('ticketDrawerBackdrop');
  if (!drawer || !backdrop) return;

  backdrop.classList.remove('opacity-0', 'pointer-events-none');
  backdrop.classList.add('opacity-100');
  drawer.classList.remove('translate-x-full', 'pointer-events-none');

  const input = document.getElementById('ticketDrawerSearch');
  if (input) {
    if (query !== undefined) {
      input.value = query;
      const clearBtn = document.getElementById('ticketDrawerClearSearch');
      if (clearBtn) {
        if (query) clearBtn.classList.remove('hidden');
        else clearBtn.classList.add('hidden');
      }
    }
    setTimeout(() => { input.focus(); }, 100);
  }
  if (type !== undefined) {
    setTicketDrawerTypeFilter(type);
  } else {
    filterTicketDrawer();
  }
}

function toggleAllCohortsAccordion() {
  const cohorts = state.analytics?.weekly_cohorts || state.analytics?.cohorts || [];
  if (!cohorts || cohorts.length === 0) return;
  const allExpanded = cohorts.every((c) => state.expandedCohorts.has(c.week_key || c.id || ''));
  cohorts.forEach((c) => {
    const wk = c.week_key || c.id || '';
    const row = document.getElementById(`cohort-detail-${wk}`);
    const chev = document.getElementById(`cohort-chevron-${wk}`);
    if (allExpanded) {
      state.expandedCohorts.delete(wk);
      if (row) row.classList.add('hidden');
      if (chev) chev.classList.remove('rotated');
    } else {
      state.expandedCohorts.add(wk);
      if (row) row.classList.remove('hidden');
      if (chev) chev.classList.add('rotated');
    }
  });
}

function closeTicketDrawer() {
  const drawer = document.getElementById('ticketDrawer');
  const backdrop = document.getElementById('ticketDrawerBackdrop');
  if (!drawer || !backdrop) return;

  backdrop.classList.remove('opacity-100');
  backdrop.classList.add('opacity-0', 'pointer-events-none');
  drawer.classList.add('translate-x-full', 'pointer-events-none');
}

function setTicketDrawerTypeFilter(type) {
  state.ticketDrawerType = type;
  document.querySelectorAll('.ticket-filter-pill').forEach((pill) => {
    if (pill.getAttribute('data-type') === type) {
      pill.classList.add('active-ticket-pill', 'bg-[var(--bg-surface)]', 'text-[var(--text-primary)]', 'shadow-xs', 'border-[var(--border-subtle)]');
      pill.classList.remove('text-[var(--text-secondary)]', 'border-transparent');
    } else {
      pill.classList.remove('active-ticket-pill', 'bg-[var(--bg-surface)]', 'text-[var(--text-primary)]', 'shadow-xs', 'border-[var(--border-subtle)]');
      pill.classList.add('text-[var(--text-secondary)]', 'border-transparent');
    }
  });
  filterTicketDrawer();
}

function clearTicketDrawerSearch() {
  const input = document.getElementById('ticketDrawerSearch');
  if (input) input.value = '';
  const clearBtn = document.getElementById('ticketDrawerClearSearch');
  if (clearBtn) clearBtn.classList.add('hidden');
  filterTicketDrawer();
}

function updateTicketDrawerData(stories) {
  state._ticketDrawerStories = stories || [];
  updateTicketDrawerCounts(state._ticketDrawerStories);
  filterTicketDrawer();
}

function updateTicketDrawerCounts(stories) {
  const counts = { all: stories.length, feature: 0, improvement: 0, fix: 0, chore: 0 };
  stories.forEach((s) => {
    const t = String(s.type || '').toLowerCase();
    if (t === 'feature') counts.feature++;
    else if (t === 'improvement') counts.improvement++;
    else if (t === 'fix' || t === 'bug') counts.fix++;
    else if (t === 'chore' || t === 'debt') counts.chore++;
  });
  const elAll = document.getElementById('filterCountAll');
  const elFeat = document.getElementById('filterCountFeature');
  const elImp = document.getElementById('filterCountImprovement');
  const elFix = document.getElementById('filterCountFix');
  const elChore = document.getElementById('filterCountChore');
  if (elAll) elAll.textContent = counts.all;
  if (elFeat) elFeat.textContent = counts.feature;
  if (elImp) elImp.textContent = counts.improvement;
  if (elFix) elFix.textContent = counts.fix;
  if (elChore) elChore.textContent = counts.chore;
}

function filterTicketDrawer() {
  const listEl = document.getElementById('ticketDrawerList');
  const countEl = document.getElementById('ticketDrawerResultsCount');
  const input = document.getElementById('ticketDrawerSearch');
  const clearBtn = document.getElementById('ticketDrawerClearSearch');
  if (!listEl) return;

  const q = (input ? input.value : '').trim().toLowerCase();
  if (clearBtn) {
    if (q) clearBtn.classList.remove('hidden');
    else clearBtn.classList.add('hidden');
  }

  const stories = (state._ticketDrawerStories && state._ticketDrawerStories.length > 0)
    ? state._ticketDrawerStories
    : (state.analytics?.all_delivered_stories || state.analytics?.stories || []);
  const typeFilter = state.ticketDrawerType || 'all';

  const filtered = stories.filter((s) => {
    const sType = String(s.type || '').toLowerCase();
    if (typeFilter !== 'all') {
      if (typeFilter === 'fix' && sType !== 'fix' && sType !== 'bug') return false;
      else if (typeFilter === 'chore' && sType !== 'chore' && sType !== 'debt') return false;
      else if (typeFilter !== 'fix' && typeFilter !== 'chore' && sType !== typeFilter) return false;
    }
    if (!q) return true;
    const matchId = String(s.id || '').toLowerCase().includes(q);
    const matchTitle = String(s.title || '').toLowerCase().includes(q);
    const matchLead = String(s.lead || '').toLowerCase().includes(q);
    const matchType = sType.includes(q);
    return matchId || matchTitle || matchLead || matchType;
  });

  if (countEl) {
    countEl.textContent = `${filtered.length} of ${stories.length} stories found`;
  }

  if (filtered.length === 0) {
    listEl.innerHTML = `<div class="text-xs text-[var(--text-tertiary)] italic p-8 text-center">No delivered stories found matching "${escapeHtml(q)}".</div>`;
    return;
  }

  listEl.innerHTML = filtered.map((s) => {
    const pts = s.points !== undefined ? `${s.points} pts` : '—';
    const typeBg = s.type === 'fix' ? 'bg-rose-500/10 text-rose-600 dark:text-rose-400 border-rose-500/25'
      : (s.type === 'improvement' ? 'bg-sky-500/10 text-sky-600 dark:text-sky-400 border-sky-500/25'
      : (s.type === 'chore' ? 'bg-indigo-500/10 text-indigo-600 dark:text-indigo-400 border-indigo-500/25' : 'bg-emerald-500/10 text-emerald-600 dark:text-emerald-400 border-emerald-500/25'));

    return `
      <div class="p-3 sm:p-3.5 rounded-xl bg-[var(--bg-surface-secondary)] border border-[var(--border-subtle)] hover:border-[var(--card-hover-border)] transition flex flex-col sm:flex-row sm:items-center justify-between gap-2.5 group cursor-pointer" onclick="openStoryFromAnalytics('${escapeHtml(s.id)}')">
        <div class="min-w-0 flex-1">
          <div class="flex flex-wrap items-center gap-1.5 mb-1">
            <span class="font-mono text-xs font-bold text-emerald-600 dark:text-emerald-400 hover:underline">${escapeHtml(s.id)}</span>
            <span class="px-1.5 py-0.2 text-[10px] font-semibold rounded border ${typeBg}">${escapeHtml(s.type || 'feature')}</span>
            <span class="px-1.5 py-0.2 text-[10px] font-mono font-semibold rounded bg-amber-500/10 text-amber-600 dark:text-amber-400 border border-amber-500/25">${pts}</span>
            ${s.duration && s.duration !== '—' ? `<span class="text-[10px] font-mono text-[var(--text-tertiary)] flex items-center gap-0.5"><i class="ph ph-timer"></i>${escapeHtml(s.duration)}</span>` : ''}
            ${s.completed_date ? `<span class="text-[10px] text-[var(--text-tertiary)] font-mono">${escapeHtml(s.completed_date)}</span>` : ''}
          </div>
          <h4 class="text-xs sm:text-sm font-semibold text-[var(--text-primary)] truncate group-hover:text-purple-600 dark:group-hover:text-purple-400 transition">${escapeHtml(s.title || s.id)}</h4>
        </div>
        <div class="flex items-center gap-1.5 shrink-0" onclick="event.stopPropagation()">
          ${s.closeout ? `
            <a href="${s.closeout.url || '#'}" target="_blank" class="inline-flex items-center gap-1 px-2 py-1 text-[11px] font-medium rounded-lg bg-emerald-50 text-emerald-700 border border-emerald-200 hover:bg-emerald-100 dark:bg-emerald-950/40 dark:text-emerald-300 dark:border-emerald-500/30 transition shadow-xs" title="Open Story Close-Out Artifact">
              <i class="ph ph-file-check"></i>
              <span>Close-Out</span>
            </a>
          ` : ''}
          <button type="button" onclick="openStoryFromAnalytics('${escapeHtml(s.id)}')" class="inline-flex items-center gap-1 px-2 py-1 text-[11px] font-medium rounded-lg bg-[var(--bg-surface)] hover:bg-[var(--bg-surface-tertiary)] text-[var(--text-primary)] border border-[var(--border-subtle)] transition shadow-xs">
            <i class="ph ph-arrow-square-out"></i>
          </button>
        </div>
      </div>
    `;
  }).join('');
}

// -----------------------------------------------------------------------------
// Chart 3: Work Composition & Agent Distribution
// -----------------------------------------------------------------------------
function renderCompositionChart(data) {
  const container = document.getElementById('chartCompositionContainer');
  if (!container) return;
  if (!data) {
    container.innerHTML = `<div class="text-xs text-[var(--text-tertiary)] italic p-6 text-center">No composition data.</div>`;
    return;
  }

  const view = state.analyticsCompView || 'types';
  const items = view === 'types' ? (data.by_type || []) : (data.by_agent || []);
  const agentBreakdown = data.agent_work_breakdown || [];

  if (items.length === 0 && agentBreakdown.length === 0) {
    container.innerHTML = `<div class="text-xs text-[var(--text-tertiary)] italic p-6 text-center">No items recorded in this window.</div>`;
    return;
  }

  const palette = ['#10b981', '#6366f1', '#f43f5e', '#a855f7', '#0ea5e9', '#f59e0b', '#ec4899', '#64748b'];
  const totalCount = items.reduce((acc, it) => acc + (it.count || 0), 0) || 1;

  const cx = 80;
  const cy = 80;
  const radius = 55;
  const strokeW = 20;
  const circ = 2 * Math.PI * radius;

  let offset = 0;
  let donutSvgParts = '';

  items.forEach((it, idx) => {
    const pct = (it.count / totalCount);
    const strokeDash = pct * circ;
    const strokeGap = circ - strokeDash;
    const color = palette[idx % palette.length];
    const itemLabel = escapeHtml(it.type || it.agent || 'Other');
    const tipData = escapeHtml(JSON.stringify({ label: itemLabel, count: it.count, pct: it.pct || Math.round(pct * 100) }));

    donutSvgParts += `
      <circle class="chart-slice-hover" cx="${cx}" cy="${cy}" r="${radius}" fill="transparent"
        stroke="${color}" stroke-width="${strokeW}"
        stroke-dasharray="${strokeDash} ${strokeGap}"
        stroke-dashoffset="${-offset}"
        data-comp-tip='${tipData}'
        data-filter-type="${view}"
        data-filter-val="${itemLabel}"
      />
    `;
    offset += strokeDash;
  });

  const centerNum = view === 'agents' && agentBreakdown.length > 0
    ? agentBreakdown.reduce((acc, ag) => acc + (ag.total_points || 0), 0)
    : totalCount;
  const centerLabel = view === 'agents' && agentBreakdown.length > 0 ? 'Points' : (view === 'types' ? 'Stories' : 'Actions');

  const donutHtml = `
    <div class="relative w-[160px] h-[160px] shrink-0 flex items-center justify-center">
      <svg viewBox="0 0 160 160" class="w-full h-full transform -rotate-90">
        <circle cx="${cx}" cy="${cy}" r="${radius}" fill="transparent" stroke="var(--border-subtle)" stroke-width="${strokeW}" />
        ${donutSvgParts}
      </svg>
      <div class="absolute inset-0 flex flex-col items-center justify-center pointer-events-none text-center">
        <span class="text-xl font-bold text-[var(--text-primary)] tabular-nums leading-none">${centerNum}</span>
        <span class="text-[9px] uppercase tracking-wider text-[var(--text-tertiary)] mt-0.5">${centerLabel}</span>
      </div>
    </div>
  `;

  let listHtml = '';
  if (view === 'agents' && agentBreakdown.length > 0) {
    listHtml = `
      <div class="flex-1 space-y-2 max-h-[200px] overflow-y-auto pr-1">
        ${agentBreakdown.map((ag, idx) => {
          const color = palette[idx % palette.length];
          const name = escapeHtml(ag.agent);
          const pts = ag.total_points ?? 0;
          return `
            <div class="cursor-pointer group hover:bg-[var(--bg-surface-secondary)] p-2 rounded-xl border border-[var(--border-subtle)] hover:border-[var(--card-hover-border)] transition" onclick="filterAnalyticsStoriesByAgent('${name}')" title="Click to view stories delivered by ${name}">
              <div class="flex items-center justify-between text-xs mb-1">
                <span class="font-bold text-[var(--text-primary)] flex items-center gap-1.5 truncate">
                  <span class="w-2.5 h-2.5 rounded-full shrink-0" style="background-color: ${color}"></span>
                  <span class="truncate">${name}</span>
                </span>
                <div class="flex items-center gap-1 shrink-0 font-mono text-[11px]">
                  <span class="px-1.5 py-0.5 rounded bg-amber-500/10 text-amber-600 dark:text-amber-400 font-bold border border-amber-500/20">${pts} pts</span>
                  <span class="text-[var(--text-tertiary)]">(${ag.total_stories} stories)</span>
                </div>
              </div>
              <div class="flex items-center gap-1.5 flex-wrap text-[10px]">
                ${ag.feature > 0 ? `<span class="px-1.5 py-0.2 rounded font-semibold bg-emerald-500/10 text-emerald-600 dark:text-emerald-400 border border-emerald-500/20">${ag.feature} Feat</span>` : ''}
                ${ag.improvement > 0 ? `<span class="px-1.5 py-0.2 rounded font-semibold bg-sky-500/10 text-sky-600 dark:text-sky-400 border border-sky-500/20">${ag.improvement} Imp</span>` : ''}
                ${ag.fix > 0 ? `<span class="px-1.5 py-0.2 rounded font-semibold bg-rose-500/10 text-rose-600 dark:text-rose-400 border border-rose-500/20">${ag.fix} Fix</span>` : ''}
                ${ag.chore > 0 ? `<span class="px-1.5 py-0.2 rounded font-semibold bg-indigo-500/10 text-indigo-600 dark:text-indigo-400 border border-indigo-500/20">${ag.chore} Chore</span>` : ''}
              </div>
            </div>
          `;
        }).join('')}
      </div>
    `;
  } else {
    listHtml = `
      <div class="flex-1 space-y-2.5 max-h-[190px] overflow-y-auto pr-1">
        ${items.map((it, idx) => {
          const color = palette[idx % palette.length];
          const label = escapeHtml(it.type || it.agent || 'Other');
          const pct = it.pct !== undefined ? it.pct : Math.round((it.count / totalCount) * 100);
          return `
            <div class="cursor-pointer group hover:bg-[var(--bg-surface-secondary)] p-1.5 rounded-lg transition" onclick="${view === 'types' ? `filterAnalyticsStoriesByType('${label}')` : `filterAnalyticsStoriesByAgent('${label}')`}">
              <div class="flex items-center justify-between text-xs mb-1">
                <span class="font-medium text-[var(--text-primary)] flex items-center gap-1.5">
                  <span class="w-2.5 h-2.5 rounded-sm shrink-0" style="background-color: ${color}"></span>
                  <span class="truncate">${label}</span>
                </span>
                <span class="font-mono text-[var(--text-secondary)] text-[11px] tabular-nums">${it.count} <span class="text-[var(--text-quaternary)]">(${pct}%)</span></span>
              </div>
              <div class="w-full h-1.5 rounded-full bg-[var(--border-subtle)] overflow-hidden">
                <div class="h-full rounded-full transition-all duration-300" style="width: ${pct}%; background-color: ${color}"></div>
              </div>
            </div>
          `;
        }).join('')}
      </div>
    `;
  }

  container.innerHTML = `
    <div class="flex flex-col sm:flex-row items-center gap-5 w-full">
      ${donutHtml}
      ${listHtml}
    </div>
  `;

  container.querySelectorAll('[data-comp-tip]').forEach((el) => {
    el.addEventListener('mousemove', (e) => {
      try {
        const d = JSON.parse(el.getAttribute('data-comp-tip'));
        const html = `
          <div class="font-bold text-xs text-[var(--text-primary)]">${escapeHtml(d.label)}</div>
          <div class="text-[11px] text-[var(--text-secondary)] mt-0.5"><strong>${d.count}</strong> items (${d.pct}%)</div>
          <div class="text-[9px] text-[var(--text-quaternary)] mt-1 italic">Click slice to filter stories</div>
        `;
        showAnalyticsTooltip(e, html);
      } catch (_) {}
    });
    el.addEventListener('mouseleave', hideAnalyticsTooltip);
    el.addEventListener('click', () => {
      const fType = el.getAttribute('data-filter-type');
      const fVal = el.getAttribute('data-filter-val');
      if (fType === 'types') filterAnalyticsStoriesByType(fVal);
      else filterAnalyticsStoriesByAgent(fVal);
    });
  });
}

// -----------------------------------------------------------------------------
// Chart 4: Quality & Review Friction
// -----------------------------------------------------------------------------
function renderQualityChart(data) {
  const container = document.getElementById('chartQualityContainer');
  if (!container) return;
  if (!data) {
    container.innerHTML = `<div class="text-xs text-[var(--text-tertiary)] italic p-6 text-center">No quality data available.</div>`;
    return;
  }

  const rounds = data.rounds || [];
  const testGrowth = data.test_suite_growth || [];

  let testSvgHtml = '';
  if (testGrowth.length > 0) {
    const vbW = 480;
    const vbH = 130;
    const padL = 35;
    const padR = 25;
    const padT = 15;
    const padB = 25;
    const w = vbW - padL - padR;
    const h = vbH - padT - padB;

    const minT = Math.min(...testGrowth.map((t) => t.tests));
    const maxT = Math.max(1, ...testGrowth.map((t) => t.tests));
    const tSpan = Math.max(1, maxT - minT);

    const pts = testGrowth.map((t, idx) => {
      const x = padL + (idx / Math.max(1, testGrowth.length - 1)) * w;
      const y = padT + h - ((t.tests - minT) / tSpan) * h;
      return { x, y, t };
    });

    const ptsStr = pts.map((p) => `${p.x},${p.y}`).join(' ');
    const areaStr = `${padL},${padT + h} ${ptsStr} ${padL + w},${padT + h}`;

    testSvgHtml = `
      <div class="flex-1 flex flex-col justify-between">
        <div class="flex items-center justify-between text-xs mb-1">
          <span class="font-semibold text-[var(--text-secondary)] uppercase tracking-wider flex items-center gap-1.5">
            <i class="ph ph-shield-check text-purple-500"></i>
            Automated Test Suite Growth &amp; Pass Trajectory
          </span>
          <span class="font-mono text-purple-600 dark:text-purple-400 font-bold">${maxT} tests passing (100%)</span>
        </div>
        <svg viewBox="0 0 ${vbW} ${vbH}" preserveAspectRatio="none" class="w-full h-[100px] select-none">
          <defs>
            <linearGradient id="testGrowthGrad" x1="0" y1="0" x2="0" y2="1">
              <stop offset="0%" stop-color="#8b5cf6" stop-opacity="0.35"/>
              <stop offset="100%" stop-color="#8b5cf6" stop-opacity="0.02"/>
            </linearGradient>
          </defs>
          <line x1="${padL}" y1="${padT}" x2="${padL + w}" y2="${padT}" stroke="var(--border-subtle)" stroke-width="1" stroke-dasharray="2,2" />
          <line x1="${padL}" y1="${padT + h}" x2="${padL + w}" y2="${padT + h}" stroke="var(--border-subtle)" stroke-width="1" stroke-dasharray="2,2" />
          <text x="${padL - 6}" y="${padT + 4}" text-anchor="end" font-size="8" fill="var(--text-tertiary)" font-family="var(--font-mono)">${maxT}</text>
          <text x="${padL - 6}" y="${padT + h + 3}" text-anchor="end" font-size="8" fill="var(--text-tertiary)" font-family="var(--font-mono)">${minT}</text>
          <polygon points="${areaStr}" fill="url(#testGrowthGrad)" />
          <polyline points="${ptsStr}" fill="none" stroke="#8b5cf6" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round" />
          ${pts.map((p) => {
            const tipData = escapeHtml(JSON.stringify({
              story: p.t.story || 'Gate Run',
              tests: p.t.tests,
              sha: p.t.sha || ''
            }));
            return `
              <circle cx="${p.x}" cy="${p.y}" r="3.5" fill="#8b5cf6" stroke="var(--bg-surface)" stroke-width="2" class="cursor-pointer" data-qa-tip='${tipData}' />
            `;
          }).join('')}
        </svg>
        <div class="text-[10px] text-[var(--text-tertiary)] flex items-center justify-between font-mono pt-1 border-t border-[var(--border-subtle)]">
          <span>Start: <strong>${minT} tests</strong></span>
          <span class="text-emerald-600 dark:text-emerald-400 font-semibold">+${maxT - minT} new tests created</span>
          <span>Latest: <strong>${maxT} passing (100%)</strong></span>
        </div>
      </div>
    `;
  } else {
    testSvgHtml = `<div class="flex-1 text-xs text-[var(--text-tertiary)] italic flex items-center justify-center p-4">No test growth history recorded yet in QUALITY.md.</div>`;
  }

  container.innerHTML = testSvgHtml;

  container.querySelectorAll('[data-qa-tip]').forEach((el) => {
    el.addEventListener('mousemove', (e) => {
      try {
        const d = JSON.parse(el.getAttribute('data-qa-tip'));
        const html = `
          <div class="font-bold text-xs text-[var(--text-primary)]">${escapeHtml(d.story)}</div>
          <div class="text-[11px] text-purple-600 dark:text-purple-400 font-bold mt-0.5">${d.tests} passing tests (100%)</div>
          ${d.sha ? `<div class="text-[10px] text-[var(--text-tertiary)] font-mono mt-0.5">Commit: ${escapeHtml(d.sha)}</div>` : ''}
        `;
        showAnalyticsTooltip(e, html);
      } catch (_) {}
    });
    el.addEventListener('mouseleave', hideAnalyticsTooltip);
  });
}

// -----------------------------------------------------------------------------
// Drill-Down Stories List & Filtering
// -----------------------------------------------------------------------------
function filterAnalyticsStoriesByDate(dateStr) {
  state.analyticsFilter = { type: 'date', value: dateStr };
  openTicketDrawer(dateStr, 'all');
}

function filterAnalyticsStoriesByType(typeStr) {
  state.analyticsFilter = { type: 'type', value: typeStr.toLowerCase() };
  openTicketDrawer('', typeStr.toLowerCase());
}

function filterAnalyticsStoriesByAgent(agentStr) {
  state.analyticsFilter = { type: 'agent', value: agentStr };
  openTicketDrawer(agentStr, 'all');
}

function resetAnalyticsStoryFilter() {
  state.analyticsFilter = null;
  if (state.analytics) {
    renderAnalyticsStories(state.analytics.stories || state.analytics.all_delivered_stories, null);
  }
}

function renderAnalyticsStories(allStories, filter) {
  const listEl = document.getElementById('analyticsStoryList');
  const countEl = document.getElementById('analyticsDetailCount');
  const titleEl = document.getElementById('analyticsDetailTitle');
  const resetBtn = document.getElementById('analyticsResetFilterBtn');
  if (!listEl) return;

  if (!allStories || allStories.length === 0) {
    listEl.innerHTML = `<div class="text-xs text-[var(--text-tertiary)] italic p-6 text-center">No stories recorded in this period.</div>`;
    if (countEl) countEl.textContent = '0 stories';
    if (resetBtn) resetBtn.classList.add('hidden');
    return;
  }

  let filtered = allStories;
  if (filter) {
    if (resetBtn) resetBtn.classList.remove('hidden');
    if (filter.type === 'date') {
      if (titleEl) titleEl.textContent = `Stories Delivered on ${filter.value}`;
      filtered = allStories.filter((s) => s.completed_date === filter.value || s.created_date === filter.value);
    } else if (filter.type === 'type') {
      if (titleEl) titleEl.textContent = `Stories of Type: ${filter.value.toUpperCase()}`;
      filtered = allStories.filter((s) => String(s.type || '').toLowerCase() === filter.value);
    } else if (filter.type === 'agent') {
      if (titleEl) titleEl.textContent = `Stories Authored by: ${filter.value}`;
      filtered = allStories.filter((s) => String(s.lead || '').includes(filter.value));
    }
  } else {
    if (titleEl) titleEl.textContent = 'Delivered Stories in Window';
    if (resetBtn) resetBtn.classList.add('hidden');
  }

  if (countEl) countEl.textContent = `${filtered.length} stories`;

  if (filtered.length === 0) {
    listEl.innerHTML = `<div class="text-xs text-[var(--text-tertiary)] italic p-6 text-center">No stories matching this filter criteria.</div>`;
    return;
  }

  listEl.innerHTML = filtered.map((s) => {
    const isCompleted = s.status === 'completed';
    const statusBg = isCompleted
      ? 'bg-emerald-500/10 text-emerald-600 dark:text-emerald-400 border-emerald-500/25'
      : (s.status === 'qa' ? 'bg-amber-500/10 text-amber-600 dark:text-amber-400 border-amber-500/25' : 'bg-purple-500/10 text-purple-600 dark:text-purple-400 border-purple-500/25');
    const typeBg = s.type === 'fix' ? 'bg-rose-500/10 text-rose-600 dark:text-rose-400 border-rose-500/25'
      : (s.type === 'improvement' ? 'bg-sky-500/10 text-sky-600 dark:text-sky-400 border-sky-500/25'
      : (s.type === 'chore' ? 'bg-indigo-500/10 text-indigo-600 dark:text-indigo-400 border-indigo-500/25' : 'bg-emerald-500/10 text-emerald-600 dark:text-emerald-400 border-emerald-500/25'));

    return `
      <div class="p-3 sm:p-4 rounded-xl bg-[var(--bg-surface-secondary)] border border-[var(--border-subtle)] hover:border-[var(--card-hover-border)] transition flex flex-col sm:flex-row sm:items-center justify-between gap-3 group">
        <div class="min-w-0 flex-1">
          <div class="flex flex-wrap items-center gap-2 mb-1">
            <span class="font-mono text-xs font-bold text-emerald-600 dark:text-emerald-400 cursor-pointer hover:underline" onclick="openStoryFromAnalytics('${escapeHtml(s.id)}')">${escapeHtml(s.id)}</span>
            <span class="px-2 py-0.2 text-[10px] font-semibold rounded border ${typeBg}">${escapeHtml(s.type || 'feature')}</span>
            <span class="px-2 py-0.2 text-[10px] font-semibold rounded border ${statusBg}">${escapeHtml(s.status)}</span>
            ${s.duration && s.duration !== '—' ? `<span class="text-[11px] font-mono text-[var(--text-tertiary)] flex items-center gap-1"><i class="ph ph-timer"></i> ${escapeHtml(s.duration)}</span>` : ''}
            ${s.lead ? `<span class="text-[11px] text-[var(--text-secondary)] bg-[var(--bg-surface)] px-1.5 py-0.5 rounded border border-[var(--border-subtle)]">${escapeHtml(s.lead)}</span>` : ''}
          </div>
          <h4 class="text-xs sm:text-sm font-semibold text-[var(--text-primary)] truncate cursor-pointer hover:text-emerald-500 transition" onclick="openStoryFromAnalytics('${escapeHtml(s.id)}')">${escapeHtml(s.title || s.id)}</h4>
        </div>
        <div class="flex items-center gap-2 shrink-0 self-end sm:self-center">
          ${s.closeout ? `
            <a href="${s.closeout.url || '#'}" target="_blank" class="inline-flex items-center gap-1 px-2.5 py-1 text-xs font-medium rounded-lg bg-emerald-50 text-emerald-700 border border-emerald-200 hover:bg-emerald-100 dark:bg-emerald-950/40 dark:text-emerald-300 dark:border-emerald-500/30 transition shadow-xs" title="Open Story Close-Out Artifact">
              <i class="ph ph-file-check"></i>
              <span>Close-Out</span>
            </a>
          ` : ''}
          <button type="button" onclick="openStoryFromAnalytics('${escapeHtml(s.id)}')" class="inline-flex items-center gap-1 px-2.5 py-1 text-xs font-medium rounded-lg bg-[var(--bg-surface)] hover:bg-[var(--bg-surface-tertiary)] text-[var(--text-primary)] border border-[var(--border-subtle)] transition shadow-xs">
            <i class="ph ph-arrow-square-out"></i>
            <span>Card Details</span>
          </button>
        </div>
      </div>
    `;
  }).join('');
}

function openStoryFromAnalytics(cardId) {
  if (!cardId) return;
  const card = (state.cards || []).find((c) => (c.mn_id || c.id) === cardId);
  if (card) {
    if (card.status === 'qa') {
      switchTab('qa');
      openQaDrawer(card);
      selectQaCard(card.mn_id || card.id, { forceScrollTop: true });
    } else {
      switchTab('pipeline');
      openCardModal(card);
    }
  } else {
    openCardModalById(cardId);
  }
}
