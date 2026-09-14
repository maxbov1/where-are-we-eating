const API_BASE = window.WAE_API_BASE || 'http://127.0.0.1:8000';
const surveyId = new URLSearchParams(location.search).get('survey');
const organizerId = localStorage.getItem('organizerId') || 'local-organizer';
const $ = (id) => document.getElementById(id);
const escapeHtml = (value) => String(value).replaceAll('&','&amp;').replaceAll('<','&lt;').replaceAll('>','&gt;').replaceAll('"','&quot;');
const safeUrl = (value) => { try { const parsed = new URL(value, location.origin); return (parsed.protocol === 'https:' || parsed.protocol === 'http:') ? parsed.href : '#'; } catch { return '#'; } };
let runId = null;
const runStorageKey = surveyId ? `recommendation-run:${surveyId}` : null;
let scrollFrame = null;

function updateMessageOpacity() {
  scrollFrame = null;
  document.querySelectorAll('.agent-message').forEach((message) => {
    const distanceFromTop = message.getBoundingClientRect().bottom;
    const fadeRange = 220;
    const opacity = distanceFromTop < 0
      ? 0.22
      : Math.min(1, 0.22 + Math.max(0, distanceFromTop) / fadeRange * 0.78);
    message.style.opacity = opacity.toFixed(2);
  });
}

function scheduleScrollEffects() {
  if (scrollFrame === null) scrollFrame = requestAnimationFrame(updateMessageOpacity);
}

function updateScrollRails() {
  const documentHeight = document.documentElement.scrollHeight;
  const viewportRatio = Math.min(1, window.innerHeight / Math.max(documentHeight, window.innerHeight));
  const thumbRatio = Math.max(viewportRatio, 0.12);
  const limit = documentHeight - window.innerHeight;
  const progress = limit > 0 ? window.scrollY / limit : 0;
  document.querySelectorAll('.scroll-rail span').forEach((thumb) => {
    thumb.style.height = `${thumbRatio * 100}%`;
    thumb.style.top = `${progress * (1 - thumbRatio) * 100}%`;
  });
  scheduleScrollEffects();
}

window.addEventListener('scroll', updateScrollRails, {passive:true});
window.addEventListener('resize', updateScrollRails);
window.addEventListener('load', updateScrollRails);

const stageProgress = {
  queued: 12,
  agent_reasoning: 26,
  restaurant_discovery: 42,
  restaurant_hydration: 58,
  reservation_scan: 72,
  reservation_inspection: 84,
  reservation_availability: 92,
};

function conversationHtml(messages) {
  const visibleMessageKey = (message) => String(message.content || '').replace(/\s+/g, ' ').trim().toLowerCase();
  const uniqueMessages = messages.filter((message, index, all) => all.findIndex((candidate) => visibleMessageKey(candidate) === visibleMessageKey(message)) === index);
  const lastStatus = uniqueMessages.reduce((last, message, index) => message.kind === 'status' ? index : last, -1);
  return uniqueMessages.map((message, index) => {
    const state = message.kind === 'status'
      ? (index === lastStatus ? 'agent-message-current' : 'agent-message-history')
      : '';
    return `<p class="agent-message agent-message-${escapeHtml(message.kind || message.role || 'assistant')} ${state}">${escapeHtml(message.content || '')}</p>`;
  }).join('');
}

function renderConversation(result, includeProgress = true) {
  const messages = conversationHtml(result.conversation || []);
  const progress = includeProgress
    ? `<div class="conversation-status"><p class="status-copy">${escapeHtml(result.message || 'Researching the best options…')}</p><span class="status-line" style="width:${stageProgress[result.stage] || 30}%" aria-hidden="true"></span></div>`
    : '';
  $('recommendations').innerHTML = `${messages ? `<div class="agent-conversation">${messages}</div>` : ''}${progress}`;
  updateScrollRails();
}

function renderContract(result) {
  runId = result.run_id || runId;
  if (runStorageKey && runId) localStorage.setItem(runStorageKey, runId);
  const response = result.response || {}, recommendation = response.recommendation;
  const conversation = conversationHtml(result.conversation || []);
  if (!recommendation) { $('recommendations').innerHTML = `${conversation ? `<div class="agent-conversation">${conversation}</div>` : ''}<article class="agent-answer"><div class="card-kicker">Recommendation unavailable</div><p>We couldn’t receive a complete structured recommendation. Please try again.</p></article>`; return; }
  const option = (item, primary) => {
    const restaurantUrl = item.restaurant_url ? safeUrl(item.restaurant_url) : null;
    const preview = restaurantUrl ? `<div class="restaurant-preview"><iframe src="${escapeHtml(restaurantUrl)}" title="${escapeHtml(item.name)} website preview" loading="lazy" referrerpolicy="strict-origin-when-cross-origin"></iframe></div>` : '';
    const facts = [item.rating != null ? `★ ${Number(item.rating).toFixed(1)}` : '', item.review_count != null ? `${Number(item.review_count).toLocaleString()} reviews` : '', item.price_level || '', item.address || '', item.hours_summary || ''].filter(Boolean);
    const profile = restaurantUrl ? `<a class="restaurant-profile" href="${escapeHtml(restaurantUrl)}" target="_blank" rel="noreferrer">${preview}<h2>${escapeHtml(item.name)} ↗</h2><p class="recommendation-description">${escapeHtml(item.description || '')}</p>${facts.length ? `<p class="recommendation-facts">${facts.map((fact) => escapeHtml(fact)).join(' · ')}</p>` : ''}${item.traits?.length ? `<p class="recommendation-traits">${item.traits.map((trait) => escapeHtml(trait)).join(' · ')}</p>` : ''}${item.tradeoff ? `<p class="recommendation-tradeoff">${escapeHtml(item.tradeoff)}</p>` : ''}</a>` : `<h2>${escapeHtml(item.name)}</h2><p class="recommendation-description">${escapeHtml(item.description || '')}</p>${facts.length ? `<p class="recommendation-facts">${facts.map((fact) => escapeHtml(fact)).join(' · ')}</p>` : ''}${item.traits?.length ? `<p class="recommendation-traits">${item.traits.map((trait) => escapeHtml(trait)).join(' · ')}</p>` : ''}${item.tradeoff ? `<p class="recommendation-tradeoff">${escapeHtml(item.tradeoff)}</p>` : ''}`;
    return `<article class="recommendation-card ${primary ? 'recommendation-primary' : ''}"><div class="card-kicker">${primary ? 'Best fit for the group' : 'Another option'}</div>${profile}<p class="recommendation-availability"><strong>${item.availability?.status === 'verified' ? 'Availability verified' : 'Availability not verified'}:</strong> ${escapeHtml(item.availability?.summary || 'Unknown')}</p>${item.reservation?.url ? `<a class="recommendation-action" href="${escapeHtml(safeUrl(item.reservation.url))}" target="_blank" rel="noreferrer">${escapeHtml(item.reservation.label || `Get ${item.name}'s reservation`)} ↗</a>` : '<span class="recommendation-unavailable">Reservation path not verified</span>'}</article>`;
  };
  const blocker = recommendation.status === 'blocked' && recommendation.blocker ? `<aside class="recommendation-blocker"><strong>${escapeHtml(recommendation.blocker.title || "I can't complete further than this")}</strong><p>${escapeHtml(recommendation.blocker.explanation || '')}</p><p>${escapeHtml(recommendation.blocker.next_step || "I can't complete further than this.")}</p></aside>` : '';
  $('recommendations').innerHTML = `${conversation ? `<div class="agent-conversation">${conversation}</div>` : ''}<div class="recommendation-set"><p class="recommendation-fit">${escapeHtml(recommendation.group_fit || '')}</p>${option(recommendation.primary,true)}${(recommendation.alternatives || []).slice(0,2).map((item) => option(item,false)).join('')}${blocker}</div>`;
  updateScrollRails();
  renderActions(response.actions || result.actions || []);
}

function renderActions(actions) {
  $('booking-handoff').innerHTML = actions.map((action) => action.url ? `<a class="quick-action" href="${escapeHtml(safeUrl(action.url))}" target="_blank" rel="noreferrer">${escapeHtml(action.label)} ↗</a>` : `<button class="quick-action" type="button" data-action="${escapeHtml(action.id)}">${escapeHtml(action.label)}</button>`).join('');
  $('booking-handoff').classList.toggle('hidden', !actions.length);
  $('booking-handoff').querySelectorAll('[data-action]').forEach((button) => button.addEventListener('click', () => continueRun(button, button.dataset.action)));
  $('booking-handoff').querySelectorAll('[data-action="show_alternatives"]').forEach((button) => button.addEventListener('click', () => document.querySelector('.recommendation-card:not(.recommendation-primary)')?.scrollIntoView({behavior:'smooth',block:'center'})));
}

async function continueRun(button, actionId = 'refresh_research') {
  if (!runId) return;
  button.disabled = true; button.textContent = 'Continuing…';
  try {
    const response = await fetch(`${API_BASE}/api/recommendations/${encodeURIComponent(runId)}/actions`, {method:'POST',headers:{'Content-Type':'application/json','X-Organizer-Id':organizerId},body:JSON.stringify({action_id:actionId})});
    if (!response.ok) throw new Error((await response.json()).detail || 'Could not continue the research');
    await poll(runId);
  } catch (error) { button.disabled = false; button.textContent = 'Try again'; $('recommendations').insertAdjacentHTML('afterbegin', `<p class="error-message">${escapeHtml(error.message || 'The follow-up failed.')}</p>`); }
}

async function poll(id) {
  const response = await fetch(`${API_BASE}/api/recommendations/${encodeURIComponent(id)}`, {headers:{'X-Organizer-Id':organizerId}}), result = await response.json();
  if (!response.ok) throw new Error(result.detail || 'Could not read recommendation progress');
  if (result.status === 'queued' || result.status === 'running') { renderConversation(result); await new Promise((resolve) => setTimeout(resolve,1200)); return poll(id); }
  renderContract(result);
}

if (surveyId) { $('back-overview').href = `event-overview.html?survey=${encodeURIComponent(surveyId)}`; pollStart(); } else { $('recommendations').innerHTML = '<p class="error-message">This recommendation link is missing an event.</p>'; }
async function pollStart() {
  try {
    const savedRunId = runStorageKey && localStorage.getItem(runStorageKey);
    if (savedRunId) {
      const existing = await fetch(`${API_BASE}/api/recommendations/${encodeURIComponent(savedRunId)}`, {headers:{'X-Organizer-Id':organizerId}});
      if (existing.ok) { runId = savedRunId; await poll(runId); return; }
      localStorage.removeItem(runStorageKey);
    }
    const response = await fetch(`${API_BASE}/api/surveys/${encodeURIComponent(surveyId)}/recommendations`, {method:'POST',headers:{'Content-Type':'application/json','X-Organizer-Id':organizerId}}), data = await response.json();
    if (!response.ok) throw new Error(data.detail || 'Could not start research');
    runId = data.run_id;
    if (runStorageKey) localStorage.setItem(runStorageKey, runId);
    await poll(runId);
  } catch (error) { $('recommendations').innerHTML = `<p class="error-message">${escapeHtml(error.message || 'Could not start the recommendation.')}</p>`; }
}
