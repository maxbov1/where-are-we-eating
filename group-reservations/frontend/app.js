const state = { event: null, responses: [], organizerId: localStorage.getItem('organizerId'), guestOrigin: null, aggregate: null };
const $ = (id) => document.getElementById(id);
const screens = { signup: $('signup-screen'), organizer: $('organizer-screen'), survey: $('survey-screen') };
const API_BASE = window.WAE_API_BASE || 'http://127.0.0.1:8000';

function show(screen) { Object.values(screens).forEach((node) => node.classList.remove('active')); screens[screen].classList.add('active'); window.scrollTo({ top: 0, behavior: 'smooth' }); }
function defaultDates() { const today = new Date(); const friday = new Date(today); friday.setDate(today.getDate() + ((5 - today.getDay() + 7) % 7 || 7)); return [friday.toISOString().slice(0, 10)]; }
function prettyDate(value) { return new Intl.DateTimeFormat('en-US', { weekday: 'short', month: 'short', day: 'numeric' }).format(new Date(`${value}T12:00:00`)); }
function prettyTime(value) { const [hours, minutes] = value.split(':'); return new Intl.DateTimeFormat('en-US', { hour:'numeric', minute:'2-digit' }).format(new Date(2000, 0, 1, Number(hours), Number(minutes))); }
function prettyDateTime(value) { return new Intl.DateTimeFormat('en-US', { weekday:'short', month:'short', day:'numeric', hour:'numeric', minute:'2-digit' }).format(new Date(value)); }
function escapeHtml(value) { return String(value).replaceAll('&', '&amp;').replaceAll('<', '&lt;').replaceAll('>', '&gt;').replaceAll('"', '&quot;'); }
function recommendationCacheKey(event, responseCount) { return `recommendation:${event?.surveyId || 'unknown'}:${responseCount || 0}`; }
function readRecommendationCache(event, responseCount) { try { const cached = JSON.parse(localStorage.getItem(recommendationCacheKey(event, responseCount)) || 'null'); return cached?.answer ? cached : null; } catch (error) { return null; } }
function writeRecommendationCache(event, responseCount, result) { try { localStorage.setItem(recommendationCacheKey(event, responseCount), JSON.stringify({ answer:result.answer || '', actions:result.response?.actions || [], fallback:Boolean(result.fallback) })); } catch (error) { /* Optional optimization. */ } }
function setupLocationPicker(inputId, menuId, { citiesOnly = false, locationContext = () => null, onSelect = () => {} } = {}) {
  const input = $(inputId); const menu = $(menuId); let sessionToken = crypto.randomUUID(); let timer;
  input.addEventListener('input', () => {
    onSelect(null); clearTimeout(timer); menu.innerHTML = ''; menu.classList.add('hidden');
    if (input.value.trim().length < 2) return;
    timer = setTimeout(async () => {
      try {
        const query = new URLSearchParams({ input: input.value.trim(), cities_only: String(citiesOnly), session_token: sessionToken });
        const context = locationContext() || {};
        if (context.latitude != null && context.longitude != null) { query.set('near_lat', context.latitude); query.set('near_lng', context.longitude); query.set('radius_miles', '75'); }
        const response = await fetch(`${API_BASE}/api/locations/autocomplete?${query}`); if (!response.ok) return;
        const data = await response.json();
        menu.innerHTML = (data.predictions || []).map((item) => `<button type="button" class="location-option" data-place-id="${escapeHtml(item.place_id)}" data-label="${escapeHtml(item.text)}"><strong>${escapeHtml(item.main_text || item.text)}</strong><span>${escapeHtml(item.secondary_text || '')}</span></button>`).join('');
        menu.classList.toggle('hidden', !menu.children.length);
      } catch (error) { menu.classList.add('hidden'); }
    }, 250);
  });
  menu.addEventListener('click', async (event) => {
    const option = event.target.closest('.location-option'); if (!option) return;
    try {
      const response = await fetch(`${API_BASE}/api/locations/details`, { method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({ place_id:option.dataset.placeId, session_token:sessionToken }) });
      if (!response.ok) return;
      const details = await response.json(); input.value = details.label || option.dataset.label; onSelect(details); menu.classList.add('hidden'); sessionToken = crypto.randomUUID();
    } catch (error) { menu.classList.add('hidden'); }
  });
  document.addEventListener('click', (event) => { if (!event.target.closest(`#${inputId}`) && !event.target.closest(`#${menuId}`)) menu.classList.add('hidden'); });
}

function formatScheduleDate(value) { return value ? prettyDate(value) : 'Choose a date'; }
function renderScheduleEditor(values = defaultDates()) {
  const editor = $('schedule-editor');
  const schedules = values.slice(0, 3).map((item) => typeof item === 'string'
    ? { date:item, times:['18:00'] }
    : { date:item.date || '', times:item.times?.length ? item.times.slice(0, 3) : ['18:00'] });
  editor.innerHTML = schedules.map((schedule, index) => `<div class="schedule-editor-row" data-schedule-index="${index}"><label>Date ${index + 1}<input required type="date" class="schedule-date" value="${schedule.date || ''}" /></label><div class="schedule-times"><div class="schedule-times-heading"><span>Available times</span><small>Up to 3 for this date</small></div><div class="schedule-time-list">${schedule.times.map((time) => `<label class="time-row"><input required class="time-input" type="time" value="${time || '18:00'}" /><button class="remove-time" type="button" aria-label="Remove time">×</button></label>`).join('')}</div><button class="add-time" type="button" data-add-schedule-time>+ Add another time</button></div></div>`).join('') + (schedules.length < 3 ? '<button class="add-date" type="button" data-add-schedule-date>+ Add another date</button>' : '');
}
function hydrateDates() { renderScheduleEditor(defaultDates()); }
const QUESTION_DEFAULTS = {
  cuisine: ['Italian', 'Japanese', 'Mexican', 'Thai', 'Indian', 'Surprise me'],
  price: ['$0–20 per person', '$20–40 per person', '$40–60 per person', '$60–80 per person', '$80+ per person'],
  vibe: ['Easygoing & casual', 'Make it special', 'Lively and social', "I'm along for the ride"],
  distance: ['1', '3', '5', '10', '15', '20', '30'],
  dietary: ['Vegetarian', 'Vegan', 'Gluten-free', 'Nut-free', 'No restrictions'],
};
const QUESTION_LABELS = { cuisine:'Cuisine', price:'Budget', vibe:'Vibe', distance:'Distance', dietary:'Dietary needs' };
const questionState = Object.fromEntries(Object.entries(QUESTION_DEFAULTS).map(([key, options]) => [key, [...options]]));
const questionEnabled = Object.fromEntries(Object.entries(questionState).map(([key, options]) => [key, new Set(options)]));
function selectedTopics() { const inputs = [...document.querySelectorAll('#question-topics input')]; return inputs.length ? inputs.filter((input) => input.checked).map((input) => input.value) : ['cuisine', 'price', 'vibe', 'distance']; }
let activeQuestion = null;
function resetBuilderForm() {
  state.event = null;
  state.aggregate = null;
  state.responses = [];
  $('event-form').reset();
  $('event-location').dataset.placeId = '';
  $('event-location').dataset.lat = '';
  $('event-location').dataset.lng = '';
  Object.entries(QUESTION_DEFAULTS).forEach(([key, options]) => {
    questionState[key] = [...options];
    questionEnabled[key] = new Set(options);
  });
  activeQuestion = null;
  hydrateDates();
  renderQuestionOptions();
  $('share-modal').classList.add('hidden');
  $('results-card').classList.add('hidden');
  $('event-overview').classList.add('hidden');
  $('question-drawer').classList.add('hidden');
  showEventBuilder();
  $('event-name').focus();
}
function renderQuestionOptions() {
  $('question-topics').innerHTML = Object.keys(QUESTION_DEFAULTS).map((key) => { const enabled = selectedTopics().includes(key); const count = questionEnabled[key].size; return `<div class="topic-row ${enabled ? 'is-enabled' : ''}"><label class="topic-check"><input type="checkbox" value="${key}" ${enabled ? 'checked' : ''} /><span><strong>${QUESTION_LABELS[key]}</strong><small>${enabled ? `${count} choices ready` : 'Not included'}</small></span></label><button class="topic-open" type="button" data-open-question="${key}" ${enabled ? '' : 'disabled'} aria-label="Edit ${QUESTION_LABELS[key]} choices">Edit <span>›</span></button></div>`; }).join('');
  if (!activeQuestion || !selectedTopics().includes(activeQuestion)) { $('question-drawer').classList.add('hidden'); $('question-options').innerHTML = ''; return; }
  const key = activeQuestion;
  $('question-drawer-title').textContent = QUESTION_LABELS[key];
  const optionLabel = (option) => key === 'distance' ? formatDistance(option) : option;
  $('question-options').innerHTML = `<div class="option-toggle-list">${questionState[key].map((option, index) => `<label class="option-toggle"><input type="checkbox" data-question="${key}" data-option-index="${index}" ${questionEnabled[key].has(option) ? 'checked' : ''} /> <span>${optionLabel(option)}</span><b>✓</b></label>`).join('')}</div><button class="drawer-add" type="button" data-add-option="${key}">+ Add a choice</button><p class="drawer-note">${key === 'cuisine' ? 'Guests can choose up to two.' : key === 'distance' ? 'Distance is measured from the meetup location. The last step keeps the search useful for spread-out groups.' : 'Keep the choices clear and easy to scan.'}</p>`;
  $('question-drawer').classList.remove('hidden');
}
renderQuestionOptions();
$('event-location').dataset.placeId = '';
setupLocationPicker('event-location', 'organizer-location-menu', { citiesOnly: true, onSelect: (details) => { $('event-location').dataset.placeId = details?.place_id || ''; $('event-location').dataset.lat = details?.latitude ?? ''; $('event-location').dataset.lng = details?.longitude ?? ''; } });
setupLocationPicker('guest-origin', 'guest-origin-menu', { locationContext: () => ({ latitude: state.event?.location_lat, longitude: state.event?.location_lng }), onSelect: (details) => { state.guestOrigin = details; } });
$('question-topics').addEventListener('change', renderQuestionOptions);
$('question-topics').addEventListener('click', (event) => { const button = event.target.closest('[data-open-question]'); if (!button) return; activeQuestion = button.dataset.openQuestion; renderQuestionOptions(); });
$('question-options').addEventListener('change', (event) => {
  if (!event.target.matches('input[data-question]')) return;
  const key = event.target.dataset.question;
  const index = Number(event.target.dataset.optionIndex);
  const option = questionState[key][index];
  if (event.target.checked) questionEnabled[key].add(option); else questionEnabled[key].delete(option);
  if (!questionEnabled[key].size) { event.target.checked = true; questionEnabled[key].add(option); }
});
$('question-options').addEventListener('click', (event) => { const addButton = event.target.closest('[data-add-option]'); if (!addButton) return; const key = addButton.dataset.addOption; const value = window.prompt(`Add a ${QUESTION_LABELS[key].toLowerCase()} choice`); if (!value?.trim() || questionState[key].includes(value.trim())) return; if (questionState[key].length >= 10) return alert('Keep each question to 10 choices or fewer.'); questionState[key].push(value.trim()); questionEnabled[key].add(value.trim()); renderQuestionOptions(); });
document.querySelectorAll('[data-close-question]').forEach((node) => node.addEventListener('click', () => { activeQuestion = null; $('question-drawer').classList.add('hidden'); }));
function renderSurveyQr(url) {
  const image = $('qr-code');
  if (!window.QRCode) {
    image.removeAttribute('src');
    image.alt = 'Survey link available in the field below';
    return;
  }
  window.QRCode.toDataURL(url, { width: 180, margin: 1, errorCorrectionLevel: 'M' })
    .then((dataUrl) => { image.src = dataUrl; image.alt = 'QR code for the group survey'; })
    .catch((error) => {
      console.error('Could not generate survey QR code', error);
      image.removeAttribute('src');
      image.alt = 'Survey link available in the field below';
    });
}

function createEvent(event) {
  state.event = event;
  $('survey-link').value = event.url;
  renderSurveyQr(event.url);
  $('share-message').value = `🍽️ Help us pick ${event.name} in ${event.location}!\n\nVote here (30 seconds): ${event.url}\n\nPick the dates and vibe that work for you — we’ll find the best table for everyone.`;
  $('expiry-note').textContent = event.expiresAt ? `Responses close ${prettyDateTime(event.expiresAt)}.` : '';
  $('results-card').classList.remove('hidden');
  $('results-card').classList.remove('overview-results');
  $('results-title').textContent = `${event.name} is ready for votes.`;
  $('share-modal').classList.remove('hidden');
}

function showEventBuilder() { $('organizer-hub').classList.add('hidden'); $('event-overview').classList.add('hidden'); $('event-builder').classList.remove('hidden'); $('results-card').classList.remove('overview-results'); }
function renderOverview(aggregate) {
  const report = aggregate.report || {};
  const schedule = report.schedule || {};
  const preferences = report.preferences || {};
  const pairs = (schedule.recommended_pairs || []).slice(0, 3);
  const stats = [
    [`${aggregate.response_count || 0}`, 'responses'],
    [schedule.pair_consensus === 'tie' ? 'Tied' : 'Clear', 'schedule signal'],
    [report.confidence?.overall?.label || 'Building', 'group confidence'],
  ];
  $('overview-stats').innerHTML = stats.map(([value, label]) => `<div class="overview-stat"><strong>${escapeHtml(value)}</strong><span>${escapeHtml(label)}</span></div>`).join('');
  const scheduleMarkup = pairs.length ? `<section class="overview-block"><h3>When the group can meet</h3><div class="pair-list">${pairs.map((pair, index) => `<div class="pair-row"><span>${index === 0 ? 'Leading option' : 'Alternative'} · ${prettyDate(pair.date)} at ${prettyTime(pair.time)}</span><b>${pair.votes} vote${pair.votes === 1 ? '' : 's'}</b></div>`).join('')}</div></section>` : '';
  const preferenceBlocks = Object.entries(preferences).filter(([, item]) => item?.leaders?.length).slice(0, 4).map(([key, item]) => { const leaders = item.leaders.slice(0, 3); const max = Math.max(...leaders.map((leader) => leader.votes), 1); return `<section class="overview-block"><h3>${escapeHtml(QUESTION_LABELS[key] || key)}</h3><div class="preference-list">${leaders.map((leader) => `<div class="preference-row"><div class="preference-label"><span>${escapeHtml(leader.value)}</span><b>${leader.votes}</b></div><div class="preference-bar"><i data-bar-width="${Math.round((leader.votes / max) * 100)}"></i></div></div>`).join('')}</div></section>`; }).join('');
  const dietary = (report.constraints?.dietary_requirements || []).join(', ');
  const dietaryMarkup = dietary ? `<p class="overview-constraint"><strong>Dietary notes:</strong> ${escapeHtml(dietary)}</p>` : '';
  $('overview-grid').innerHTML = scheduleMarkup + preferenceBlocks + dietaryMarkup;
  const animateBars = () => document.querySelectorAll('#overview-grid [data-bar-width]').forEach((bar) => { const width = `${bar.dataset.barWidth}%`; if (window.motionAnimate) window.motionAnimate(bar, { width: ['0%', width] }, { duration:.55, delay:.08 }); else { bar.style.width = width; } });
  requestAnimationFrame(animateBars);
}
async function showEventOverview(event, aggregate) {
  state.event = event;
  state.aggregate = aggregate;
  $('organizer-hub').classList.add('hidden'); $('event-builder').classList.add('hidden'); $('event-overview').classList.remove('hidden');
  $('results-card').classList.add('hidden'); $('results-card').classList.add('overview-results'); $('results-card').classList.remove('recommendation-page-mode'); $('recommendation-back').classList.add('hidden'); $('results-title').textContent = `${event.name} recommendations`; $('response-summary').textContent = `${aggregate.response_count || 0} response${aggregate.response_count === 1 ? '' : 's'} collected.`; $('recommendations').innerHTML = ''; $('booking-handoff').classList.add('hidden');
  $('overview-title').textContent = event.name; $('overview-location').textContent = event.location; renderOverview(aggregate); $('event-overview').scrollIntoView({ behavior:'smooth', block:'start' });
}
function renderRecommendationResult(result) {
  const actions = result.response?.actions || result.actions || [];
  const fallbackText = result.error_code === 'RECOMMENDATION_TIMEOUT'
    ? 'Live research took longer than the demo window. Showing the seeded recommendation.'
    : result.error_code === 'CONTEXT_WINDOW_OVERFLOW'
      ? 'Live research was stopped because the browser evidence was too large. Showing the seeded recommendation.'
      : 'Live research was unavailable. Showing the seeded recommendation.';
  const fallbackNote = result.fallback ? `<p class="response-summary">${fallbackText}</p>` : '';
  if (result.response?.recommendation) {
    renderStructuredRecommendation(result.response.recommendation, fallbackNote);
    renderAgentActions(actions.filter((action) => !action.url));
    return;
  }
  renderAgentActions(actions);
  $('recommendations').innerHTML = `${fallbackNote}<article class="agent-answer"><div class="card-kicker">/ agent response</div><div>${formatAgentAnswer(result.answer || '')}</div></article>`;
}

function renderRestaurantLink(option) {
  const name = escapeHtml(option.name || 'Restaurant');
  return option.restaurant_url
    ? `<a class="recommendation-name" href="${escapeHtml(option.restaurant_url)}" target="_blank" rel="noreferrer">${name} <span aria-hidden="true">↗</span></a>`
    : `<span class="recommendation-name">${name}</span>`;
}

function renderRestaurantAction(option, index) {
  if (!option.booking_url) return '<span class="recommendation-unavailable">Reservation path not verified</span>';
  const label = option.booking_label || `Get ${option.name}'s reservation`;
  return `<a class="button ${index === 0 ? 'primary' : 'secondary'}" href="${escapeHtml(option.booking_url)}" target="_blank" rel="noreferrer">${escapeHtml(label)} <span aria-hidden="true">↗</span></a>`;
}

function renderStructuredRecommendation(recommendation, fallbackNote = '') {
  const primary = recommendation.primary;
  const alternatives = (recommendation.alternatives || []).slice(0, 2);
  const optionCard = (option, index, primaryCard = false) => `<article class="recommendation-card ${primaryCard ? 'recommendation-primary' : ''}">
    <div class="card-kicker">${primaryCard ? 'Best fit for the group' : 'Another good option'}</div>
    <h3>${renderRestaurantLink(option)}</h3>
    <p class="recommendation-description">${escapeHtml(option.description || '')}</p>
    ${option.tradeoff ? `<p class="recommendation-tradeoff">${escapeHtml(option.tradeoff)}</p>` : ''}
    ${option.availability ? `<p class="recommendation-availability"><strong>Availability:</strong> ${escapeHtml(option.availability)}</p>` : ''}
    <div class="recommendation-card-action">${renderRestaurantAction(option, index)}</div>
  </article>`;
  $('recommendations').innerHTML = `${fallbackNote}<div class="recommendation-set">
    <p class="recommendation-fit">${escapeHtml(recommendation.group_fit || '')}</p>
    ${optionCard(primary, 0, true)}
    ${alternatives.map((option, index) => optionCard(option, index + 1)).join('')}
  </div>`;
}
function openRecommendationPage() {
  if (!state.event) return;
  $('share-modal').classList.add('hidden'); $('event-overview').classList.add('hidden'); $('results-card').classList.remove('hidden'); $('results-card').classList.add('recommendation-page-mode'); $('recommendation-back').classList.remove('hidden'); $('results-title').textContent = `${state.event.name} recommendation`;
  const responseCount = state.aggregate?.response_count || state.responses.length || 0;
  $('response-summary').textContent = '';
  const cached = readRecommendationCache(state.event, responseCount);
  if (cached) { renderRecommendationResult(cached); $('run-agent').disabled = false; $('run-agent').innerHTML = 'Refresh recommendation <span>→</span>'; return; }
  $('recommendations').innerHTML = ''; $('booking-handoff').classList.add('hidden'); $('run-agent').click(); $('results-card').scrollIntoView({ behavior:'smooth', block:'start' });
}
function renderEventShelf(events) {
  const shelf = $('event-shelf');
  if (!events.length) { shelf.innerHTML = '<p class="response-summary">No events yet. Start with a quick dinner plan.</p>'; return; }
  shelf.innerHTML = events.map((event) => `<button type="button" class="event-shelf-card" data-event-id="${escapeHtml(event.id)}"><span><strong>${escapeHtml(event.event_name)}</strong><small>${escapeHtml(event.location)} · ${event.response_count} response${event.response_count === 1 ? '' : 's'}</small></span><b>${event.is_open ? 'Open' : 'Closed'} <span aria-hidden="true">→</span></b></button>`).join('');
}
async function loadOrganizerEvents() {
  if (!state.organizerId) return;
  try {
    const response = await fetch(`${API_BASE}/api/organizers/${encodeURIComponent(state.organizerId)}/surveys`, { headers:{'X-Organizer-Id':state.organizerId} });
    const data = await response.json();
    if (response.ok) renderEventShelf(data.events || []);
  } catch (error) { $('event-shelf').innerHTML = '<p class="response-summary">Your event shelf is unavailable right now. You can still start a new event.</p>'; }
}
async function openOrganizerEvent(id) {
  const [eventResponse, aggregateResponse] = await Promise.all([fetch(`${API_BASE}/api/surveys/${encodeURIComponent(id)}`), fetch(`${API_BASE}/api/surveys/${encodeURIComponent(id)}/aggregate`)]);
  const event = await eventResponse.json(); const aggregate = await aggregateResponse.json();
  if (!eventResponse.ok) return alert(event.detail || 'Could not open event');
  if (!aggregateResponse.ok) return alert(aggregate.detail || 'Could not load event summary');
  await showEventOverview({ name:event.event_name, location:event.location, dates:event.dates, times:event.times, availability:event.availability, questions:event.questions, surveyId:event.id, publicToken:event.public_token, url:`${location.origin}/?survey=${event.public_token}`, expiresAt:event.expires_at, isOpen:event.is_open }, aggregate);
}

$('signup-form').addEventListener('submit', async (event) => { event.preventDefault(); const email = $('organizer-email').value; const response = await fetch(`${API_BASE}/api/users`, { method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({ email }) }); const user = await response.json(); if (!response.ok) return alert(user.detail || 'Could not create organizer'); state.organizerId = user.id; localStorage.setItem('organizerEmail', email); localStorage.setItem('organizerId', user.id); hydrateDates(); show('organizer'); loadOrganizerEvents(); });
$('start-event').addEventListener('click', resetBuilderForm);
$('new-event').addEventListener('click', resetBuilderForm);
$('event-shelf').addEventListener('click', (event) => { const card = event.target.closest('[data-event-id]'); if (card) openOrganizerEvent(card.dataset.eventId); });
$('find-recommendation').addEventListener('click', openRecommendationPage);
$('event-form').addEventListener('submit', async (event) => { event.preventDefault(); const scheduleRows = [...document.querySelectorAll('.schedule-editor-row')]; const availability = Object.fromEntries(scheduleRows.map((row) => [row.querySelector('.schedule-date').value, [...row.querySelectorAll('.time-input')].map((input) => input.value).filter(Boolean)]).filter(([date, slots]) => date && slots.length)); const dates = Object.keys(availability); const times = [...new Set(Object.values(availability).flat())]; const questions = Object.fromEntries(selectedTopics().map((key) => [key, questionState[key].filter((option) => questionEnabled[key].has(option))])); if (dates.length < 1 || dates.length > 3) return alert('Choose between one and three dates.'); if (new Set(dates).size !== dates.length) return alert('Choose a different date for each row.'); if (Object.values(availability).some((slots) => !slots.length || new Set(slots).size !== slots.length)) return alert('Give each date at least one unique time.'); if (Object.values(questions).some((options) => !options.length)) return alert('Keep at least one answer option in each question.'); const location = $('event-location'); const expiryDays = Number($('event-expiry').value) || 2; const expiresAt = new Date(Date.now() + expiryDays * 86400000).toISOString(); const payload = { organizer_id:state.organizerId || 'local-organizer', event_name:$('event-name').value, location:location.value, location_place_id:location.dataset.placeId || null, location_lat:location.dataset.lat ? Number(location.dataset.lat) : null, location_lng:location.dataset.lng ? Number(location.dataset.lng) : null, dates, times, availability, questions, expires_at:expiresAt }; const response = await fetch(`${API_BASE}/api/surveys`, { method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify(payload) }); const data = await response.json(); if (!response.ok) return alert(data.detail || 'Could not create survey'); state.event = { name:payload.event_name, location:payload.location, dates, times, availability, questions:payload.questions, surveyId:data.id, publicToken:data.public_token, url:data.share_url, expiresAt:data.expires_at || expiresAt, isOpen:true }; createEvent(state.event); });
$('schedule-editor').addEventListener('click', (event) => { if (event.target.matches('[data-add-schedule-date]')) { const rows = [...$('schedule-editor').querySelectorAll('.schedule-editor-row')]; if (rows.length >= 3) return; const schedules = rows.map((item) => ({ date:item.querySelector('.schedule-date').value, times:[...item.querySelectorAll('.time-input')].map((input) => input.value) })); const lastDate = schedules.at(-1)?.date; const nextDate = lastDate ? new Date(`${lastDate}T12:00:00`) : new Date(); nextDate.setDate(nextDate.getDate() + 7); schedules.push({ date:nextDate.toISOString().slice(0, 10), times:['18:00'] }); renderScheduleEditor(schedules); return; } const row = event.target.closest('.schedule-editor-row'); if (!row) return; if (event.target.matches('[data-add-schedule-time]')) { const list = row.querySelector('.schedule-time-list'); if (list.children.length >= 3) return; const time = document.createElement('label'); time.className = 'time-row'; time.innerHTML = '<input required class="time-input" type="time" value="21:00" /><button class="remove-time" type="button" aria-label="Remove time">×</button></label>'; list.appendChild(time); } if (event.target.classList.contains('remove-time') && row.querySelectorAll('.time-row').length > 1) event.target.closest('.time-row').remove(); });
$('copy-message').addEventListener('click', async () => { await navigator.clipboard?.writeText($('share-message').value); $('copied-note').classList.remove('hidden'); setTimeout(() => $('copied-note').classList.add('hidden'), 2400); });
document.querySelectorAll('[data-close-modal]').forEach((node) => node.addEventListener('click', () => $('share-modal').classList.add('hidden')));
$('open-survey').addEventListener('click', () => { $('share-modal').classList.add('hidden'); prepareSurvey(); show('survey'); });
$('view-results').addEventListener('click', () => { $('share-modal').classList.add('hidden'); $('results-card').scrollIntoView({ behavior:'smooth', block:'start' }); });
$('recommendation-back').addEventListener('click', () => { $('results-card').classList.add('hidden'); $('results-card').classList.remove('recommendation-page-mode'); $('results-card').classList.add('overview-results'); $('recommendation-back').classList.add('hidden'); $('response-summary').textContent = `${state.aggregate?.response_count || 0} response${(state.aggregate?.response_count || 0) === 1 ? '' : 's'} collected.`; $('recommendations').innerHTML = ''; $('booking-handoff').classList.add('hidden'); $('event-overview').classList.remove('hidden'); $('event-overview').scrollIntoView({ behavior:'smooth', block:'start' }); });
$('back-organizer').addEventListener('click', () => show('organizer'));
$('copy-link').addEventListener('click', async () => { await navigator.clipboard?.writeText($('survey-link').value); $('copied-note').textContent = 'Link copied — ready for the group chat.'; $('copied-note').classList.remove('hidden'); setTimeout(() => $('copied-note').classList.add('hidden'), 2400); });

function formatDistance(value) { const miles = Number.parseInt(value, 10); return Number.isFinite(miles) ? `${miles}${miles === 30 ? '+' : ''} mile${miles === 1 ? '' : 's'} from meetup` : value; }
function renderDistanceQuestion(options) {
  const values = options.filter((option) => /^\d+$/.test(option)).map((option) => String(Number(option))).sort((a, b) => Number(a) - Number(b));
  if (values.length < 2) return '';
  const initial = Math.min(3, values.length - 1);
  return `<div class="distance-control"><input type="range" min="0" max="${values.length - 1}" value="${initial}" data-distance-values="${values.join(',')}" aria-label="Maximum restaurant distance from meetup" /><output>${formatDistance(values[initial])}</output><input type="hidden" name="distance" value="${values[initial]}" /><div class="range-labels"><span>${formatDistance(values[0])}</span><span>${formatDistance(values[values.length - 1])}</span></div></div>`;
}
function renderSurveySchedule(availability) {
  return Object.entries(availability).map(([date, times]) => `<div class="survey-day"><h3>${prettyDate(date)}</h3><div class="survey-choices">${times.map((time) => `<label class="survey-choice"><input type="checkbox" data-availability-date="${date}" value="${time}" /> <span>${prettyTime(time)}</span><b>✓</b></label>`).join('')}</div></div>`).join('');
}
function prepareSurvey() { $('survey-closed').classList.add('hidden'); $('survey-form').classList.remove('hidden'); const event = state.event || { name:'Friday dinner', location:'San Francisco', dates:defaultDates(), times:['19:00'], availability:Object.fromEntries(defaultDates().map((date) => [date, ['19:00']])), questions:{ cuisine:['Italian','Japanese','Mexican','Surprise me'] } }; state.guestOrigin = null; $('guest-origin').value = ''; const availability = event.availability || Object.fromEntries(event.dates.map((date) => [date, event.times])); const questions = event.questions || {}; $('survey-title').innerHTML = `Help pick <em>${event.name}.</em>`; $('survey-location').textContent = `${event.location} · about 30 seconds · no sign-up`; $('survey-schedule').innerHTML = renderSurveySchedule(availability); const questionMarkup = { cuisine:['What sounds good?', 'Pick up to two.', 'checkbox'], distance:['How far should we search?', 'Choose the maximum restaurant radius from the meetup spot. 30+ miles keeps this useful when everyone is spread out.', 'range'], vibe:["What's the vibe?", 'Choose one.', 'radio'], price:["What's the budget?", 'Per person, before drinks.', 'radio'], dietary:['Anything we should know?', 'Choose what the table should know.', 'checkbox'] }; $('survey-question-fields').innerHTML = Object.entries(questions).filter(([, options]) => options?.length).map(([key, options]) => { const [title, help, type] = questionMarkup[key] || [QUESTION_LABELS[key] || key, 'Choose what works for you.', 'radio']; const control = type === 'range' ? renderDistanceQuestion(options) : options.map((option) => `<label class="survey-choice"><input ${type === 'radio' ? 'required' : ''} type="${type}" name="${key}" value="${option}" /> <span>${option}</span><b>${type === 'radio' ? '✓' : ''}</b></label>`).join(''); return `<fieldset><legend>${title}</legend><p class="question-help">${help}</p><div class="survey-choices">${control}</div></fieldset>`; }).join(''); }
$('survey-question-fields').addEventListener('input', (event) => { if (!event.target.matches('input[type="range"][data-distance-values]')) return; const values = event.target.dataset.distanceValues.split(','); const value = values[Number(event.target.value)]; event.target.parentElement.querySelector('output').textContent = formatDistance(value); event.target.parentElement.querySelector('input[type="hidden"]').value = value; });
$('survey-form').addEventListener('change', (event) => { if (event.target.name === 'cuisine' && document.querySelectorAll('input[name="cuisine"]:checked').length > 2) event.target.checked = false; });
$('survey-form').addEventListener('submit', async (event) => { event.preventDefault(); const availability = {}; document.querySelectorAll('[data-availability-date]').forEach((input) => { if (input.checked) (availability[input.dataset.availabilityDate] ||= []).push(input.value); }); const dates = Object.keys(availability); const times = [...new Set(Object.values(availability).flat())]; if (!dates.length || !times.length || !state.event?.publicToken) return; const origin = state.guestOrigin || {}; const answer = { dates, times, availability, cuisines:[...document.querySelectorAll('input[name="cuisine"]:checked')].map((input) => input.value), dietary:[...document.querySelectorAll('input[name="dietary"]:checked')].map((input) => input.value), distance:document.querySelector('input[name="distance"]:checked, input[type="hidden"][name="distance"]')?.value, vibe:document.querySelector('input[name="vibe"]:checked')?.value, price:document.querySelector('input[name="price"]:checked')?.value, origin_place_id:origin.place_id || null, origin_label:origin.label || null, origin_lat:origin.latitude ?? null, origin_lng:origin.longitude ?? null, respondent_token:localStorage.getItem('respondentToken') || crypto.randomUUID() }; localStorage.setItem('respondentToken', answer.respondent_token); const response = await fetch(`${API_BASE}/api/surveys/${state.event.publicToken}/responses`, { method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify(answer) }); if (response.status === 410) { $('survey-form').classList.add('hidden'); $('survey-closed').classList.remove('hidden'); return; } if (!response.ok) return alert('Could not save your response. Please try again.'); state.responses.push(answer); $('survey-form').classList.add('hidden'); $('survey-thanks').classList.remove('hidden'); updateResponseSummary(); });
function updateResponseSummary() { if (!state.event) return; $('response-summary').textContent = `${state.responses.length} response${state.responses.length === 1 ? '' : 's'} collected · structured answers are ready for the recommendation agent.`; }
$('run-agent').addEventListener('click', async () => {
  if (!state.event) return;
  const button = $('run-agent');
  button.disabled = true;
  button.innerHTML = 'Researching <span>…</span>';
  const stages = [
    'Reading the group’s preferences',
    'Finding restaurants with Google Places',
    'Verifying restaurant details',
    'Checking reservation paths',
    'Preparing the recommendation',
  ];
  let stageIndex = 0;
  const renderProgress = (message = stages[stageIndex]) => {
    $('recommendations').innerHTML = `<div class="agent-progress" aria-live="polite"><p class="response-summary">${escapeHtml(message)}</p><div class="progress-track"><span style="width:${Math.min(92, 18 + stageIndex * 18)}%"></span></div><ol>${stages.map((stage, index) => `<li class="${index < stageIndex ? 'done' : index === stageIndex ? 'active' : ''}">${escapeHtml(stage)}</li>`).join('')}</ol></div>`;
  };
  renderProgress();
  try {
    const response = await fetch(`${API_BASE}/api/surveys/${state.event.surveyId}/recommendations`, { method:'POST', headers:{'Content-Type':'application/json','X-Organizer-Id':state.organizerId || 'local-organizer'} });
    const data = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(data.detail || 'Agent request failed');
    if (!data.run_id) throw new Error('Recommendation run was not created');
    const pollingStartedAt = Date.now();
    const poll = async () => {
      if (Date.now() - pollingStartedAt > 570000) {
        throw new Error('Recommendation progress expired before the server returned a result. Please try again.');
      }
      const statusResponse = await fetch(`${API_BASE}/api/recommendations/${encodeURIComponent(data.run_id)}`, { headers:{'X-Organizer-Id':state.organizerId || 'local-organizer'} });
      const status = await statusResponse.json().catch(() => ({}));
      if (!statusResponse.ok) throw new Error(status.detail || 'Could not read recommendation progress');
      if (status.status === 'queued' || status.status === 'running') {
        const stageOrder = {
          queued: 0,
          agent_reasoning: 0,
          restaurant_discovery: 1,
          restaurant_hydration: 2,
          reservation_scan: 3,
          reservation_inspection: 3,
          reservation_availability: 4,
        };
        stageIndex = Math.max(stageIndex, stageOrder[status.stage] ?? 1);
        renderProgress(status.message || stages[stageIndex]);
        await new Promise((resolve) => setTimeout(resolve, 1200));
        return poll();
      }
      if (!['complete', 'fallback', 'timeout'].includes(status.status)) throw new Error(status.message || 'Recommendation run failed');
      return status;
    };
    const result = await poll();
    writeRecommendationCache(state.event, state.aggregate?.response_count || state.responses.length || 0, result);
    renderRecommendationResult(result);
  } catch (error) {
    console.error('Recommendation request failed', error);
    const message = error instanceof Error ? error.message : 'We could not reach the recommendation service. Please try again in a moment.';
    $('recommendations').innerHTML = `<p class="error-message">${escapeHtml(message)}</p>`;
  } finally {
    button.disabled = false;
    button.innerHTML = 'Find our top 3 <span>→</span>';
  }
});

function formatAgentAnswer(value) {
  const escaped = value.replaceAll('&', '&amp;').replaceAll('<', '&lt;').replaceAll('>', '&gt;').replaceAll('"', '&quot;');
  const linkedMarkdown = escaped.replace(/\[([^\]]+)\]\((https?:\/\/[^\s)]+)\)/g, '<a href="$2" target="_blank" rel="noreferrer">$1 ↗</a>');
  const linkedPlain = linkedMarkdown.replace(/(?<!["=])(https?:\/\/[^\s<&]+)/g, '<a href="$1" target="_blank" rel="noreferrer">$1 ↗</a>');
  return linkedPlain.replaceAll('\n', '<br />');
}

function findBookingUrl(value) {
  const line = value.split('\n').find((item) => /booking link/i.test(item) && !/unavailable/i.test(item));
  return line?.match(/https?:\/\/[^\s)]+/)?.[0] || '';
}

function renderAgentActions(actions) {
  const external = actions.filter((action) => action.url);
  const followups = actions.filter((action) => !action.url);
  $('booking-handoff').innerHTML = external.map((action) => `<a class="button ${action.kind === 'confirmation' ? 'primary' : 'secondary'}" href="${escapeHtml(action.url)}" target="_blank" rel="noreferrer">${escapeHtml(action.label)} <span>↗</span></a>`).join('') + (followups.length ? `<div class="follow-up-actions">${followups.map((action) => `<button type="button" class="text-button" data-follow-up="${escapeHtml(action.id)}">${escapeHtml(action.label)}</button>`).join('')}</div>` : '');
  $('booking-handoff').classList.toggle('hidden', !actions.length);
  $('booking-handoff').querySelectorAll('[data-follow-up]').forEach((button) => button.addEventListener('click', () => { $('recommendations').insertAdjacentHTML('afterbegin', `<p class="response-summary">${escapeHtml(button.textContent)} selected — ask the agent to continue with this request.</p>`); }));
}

async function loadPublicSurvey() { const token = new URLSearchParams(location.search).get('survey'); if (!token) return; const response = await fetch(`${API_BASE}/api/surveys/${token}`); const survey = await response.json(); if (!response.ok) return alert(survey.detail || 'Survey not found'); state.event = { name:survey.event_name, location:survey.location, location_lat:survey.location_lat, location_lng:survey.location_lng, dates:survey.dates, times:survey.times, availability:survey.availability, questions:survey.questions, publicToken:survey.public_token, surveyId:survey.id, url:location.href, expiresAt:survey.expires_at, isOpen:survey.is_open !== false }; if (survey.is_open === false) { $('survey-title').innerHTML = `Help pick <em>${survey.event_name}.</em>`; show('survey'); $('survey-form').classList.add('hidden'); $('survey-closed').classList.remove('hidden'); return; } prepareSurvey(); show('survey'); }
const openOrganizerSettings = new URLSearchParams(location.search).get('organizer') === '1' && state.organizerId;
if (openOrganizerSettings) { hydrateDates(); show('organizer'); loadOrganizerEvents(); } else loadPublicSurvey();
