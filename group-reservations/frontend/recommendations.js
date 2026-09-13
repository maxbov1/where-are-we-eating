const API_BASE = window.WAE_API_BASE || 'http://127.0.0.1:8000';
const surveyId = new URLSearchParams(location.search).get('survey');
const organizerId = localStorage.getItem('organizerId') || 'local-organizer';
const $ = (id) => document.getElementById(id);
const escapeHtml = (value) => String(value).replaceAll('&','&amp;').replaceAll('<','&lt;').replaceAll('>','&gt;').replaceAll('"','&quot;');
const safeUrl = (value) => { try { const parsed = new URL(value, location.origin); return (parsed.protocol === 'https:' || parsed.protocol === 'http:') ? parsed.href : '#'; } catch { return '#'; } };
let runId = null;

function renderContract(result) {
  runId = result.run_id || runId;
  const response = result.response || {}, recommendation = response.recommendation;
  if (!recommendation) { $('recommendations').innerHTML = '<article class="agent-answer"><div class="card-kicker">Recommendation unavailable</div><p>We couldn’t receive a complete structured recommendation. Please try again.</p></article>'; return; }
  const conversation = (result.conversation || []).map((message) => `<div class="agent-message agent-message-${escapeHtml(message.role || 'assistant')}"><span>${message.role === 'user' ? 'You' : 'Where Are We Eating?'}</span><p>${escapeHtml(message.content || '')}</p></div>`).join('');
  const option = (item, primary) => `<article class="recommendation-card ${primary ? 'recommendation-primary' : ''}"><div class="card-kicker">${primary ? 'Best fit for the group' : 'Another good option'}</div><h3>${item.restaurant_url ? `<a class="recommendation-name" href="${escapeHtml(safeUrl(item.restaurant_url))}" target="_blank" rel="noreferrer">${escapeHtml(item.name)} ↗</a>` : escapeHtml(item.name)}</h3><p class="recommendation-description">${escapeHtml(item.description || '')}</p>${item.traits?.length ? `<div class="recommendation-traits">${item.traits.map((trait) => `<span>${escapeHtml(trait)}</span>`).join('')}</div>` : ''}${item.tradeoff ? `<p class="recommendation-tradeoff">${escapeHtml(item.tradeoff)}</p>` : ''}<p class="recommendation-availability"><strong>${item.availability?.status === 'verified' ? 'Availability verified' : 'Availability not verified'}:</strong> ${escapeHtml(item.availability?.summary || 'Unknown')}</p>${item.reservation?.url ? `<a class="button ${primary ? 'primary' : 'secondary'}" href="${escapeHtml(safeUrl(item.reservation.url))}" target="_blank" rel="noreferrer">${escapeHtml(item.reservation.label || `Get ${item.name}'s reservation`)} ↗</a>` : '<span class="recommendation-unavailable">Reservation path not verified</span>'}</article>`;
  const blocker = recommendation.status === 'blocked' && recommendation.blocker ? `<aside class="recommendation-blocker"><strong>${escapeHtml(recommendation.blocker.title || "I can't complete further than this")}</strong><p>${escapeHtml(recommendation.blocker.explanation || '')}</p><p>${escapeHtml(recommendation.blocker.next_step || "I can't complete further than this.")}</p></aside>` : '';
  $('recommendations').innerHTML = `<div class="recommendation-set">${conversation ? `<div class="agent-conversation">${conversation}</div>` : ''}<p class="recommendation-fit">${escapeHtml(recommendation.group_fit || '')}</p>${option(recommendation.primary,true)}${(recommendation.alternatives || []).slice(0,2).map((item) => option(item,false)).join('')}${blocker}</div>`;
  renderActions(response.actions || result.actions || []);
}

function renderActions(actions) {
  $('booking-handoff').innerHTML = actions.map((action) => action.url ? `<a class="button secondary" href="${escapeHtml(safeUrl(action.url))}" target="_blank" rel="noreferrer">${escapeHtml(action.label)} ↗</a>` : `<button class="text-button" type="button" data-action="${escapeHtml(action.id)}">${escapeHtml(action.label)}</button>`).join('');
  $('booking-handoff').classList.toggle('hidden', !actions.length);
  $('booking-handoff').querySelectorAll('[data-action="refresh_research"]').forEach((button) => button.addEventListener('click', () => continueRun(button)));
  $('booking-handoff').querySelectorAll('[data-action="show_alternatives"]').forEach((button) => button.addEventListener('click', () => document.querySelector('.recommendation-card:not(.recommendation-primary)')?.scrollIntoView({behavior:'smooth',block:'center'})));
}

async function continueRun(button) {
  if (!runId) return;
  button.disabled = true; button.textContent = 'Continuing…';
  try {
    const response = await fetch(`${API_BASE}/api/recommendations/${encodeURIComponent(runId)}/actions`, {method:'POST',headers:{'Content-Type':'application/json','X-Organizer-Id':organizerId},body:JSON.stringify({action_id:'refresh_research'})});
    if (!response.ok) throw new Error((await response.json()).detail || 'Could not continue the research');
    await poll(runId);
  } catch (error) { button.disabled = false; button.textContent = 'Try again'; $('recommendations').insertAdjacentHTML('afterbegin', `<p class="error-message">${escapeHtml(error.message || 'The follow-up failed.')}</p>`); }
}

async function poll(id) {
  const response = await fetch(`${API_BASE}/api/recommendations/${encodeURIComponent(id)}`, {headers:{'X-Organizer-Id':organizerId}}), result = await response.json();
  if (!response.ok) throw new Error(result.detail || 'Could not read recommendation progress');
  if (result.status === 'queued' || result.status === 'running') { $('recommendations').querySelector('.response-summary').textContent = result.message || 'Researching…'; await new Promise((resolve) => setTimeout(resolve,1200)); return poll(id); }
  renderContract(result);
}

if (surveyId) { $('back-overview').href = `event-overview.html?survey=${encodeURIComponent(surveyId)}`; pollStart(); } else { $('recommendations').innerHTML = '<p class="error-message">This recommendation link is missing an event.</p>'; }
async function pollStart() { try { const response = await fetch(`${API_BASE}/api/surveys/${encodeURIComponent(surveyId)}/recommendations`, {method:'POST',headers:{'Content-Type':'application/json','X-Organizer-Id':organizerId}}), data = await response.json(); if (!response.ok) throw new Error(data.detail || 'Could not start research'); runId = data.run_id; await poll(runId); } catch (error) { $('recommendations').innerHTML = `<p class="error-message">${escapeHtml(error.message || 'Could not start the recommendation.')}</p>`; } }
