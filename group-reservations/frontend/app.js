const state = { event: null, responses: [], organizerId: localStorage.getItem('organizerId'), guestOrigin: null };
const $ = (id) => document.getElementById(id);
const screens = { signup: $('signup-screen'), organizer: $('organizer-screen'), survey: $('survey-screen') };
const API_BASE = window.WAE_API_BASE || 'http://127.0.0.1:8000';

function show(screen) { Object.values(screens).forEach((node) => node.classList.remove('active')); screens[screen].classList.add('active'); window.scrollTo({ top: 0, behavior: 'smooth' }); }
function defaultDates() { const today = new Date(); const friday = new Date(today); friday.setDate(today.getDate() + ((5 - today.getDay() + 7) % 7 || 7)); return [0, 7, 14].map((offset) => { const date = new Date(friday); date.setDate(friday.getDate() + offset); return date.toISOString().slice(0, 10); }); }
function prettyDate(value) { return new Intl.DateTimeFormat('en-US', { weekday: 'short', month: 'short', day: 'numeric' }).format(new Date(`${value}T12:00:00`)); }
function prettyTime(value) { const [hours, minutes] = value.split(':'); return new Intl.DateTimeFormat('en-US', { hour:'numeric', minute:'2-digit' }).format(new Date(2000, 0, 1, Number(hours), Number(minutes))); }
function escapeHtml(value) { return String(value).replaceAll('&', '&amp;').replaceAll('<', '&lt;').replaceAll('>', '&gt;').replaceAll('"', '&quot;'); }
function setupLocationPicker(inputId, menuId, { citiesOnly = false, onSelect = () => {} } = {}) {
  const input = $(inputId); const menu = $(menuId); let sessionToken = crypto.randomUUID(); let timer;
  input.addEventListener('input', () => {
    onSelect(null); clearTimeout(timer); menu.innerHTML = ''; menu.classList.add('hidden');
    if (input.value.trim().length < 2) return;
    timer = setTimeout(async () => {
      try {
        const query = new URLSearchParams({ input: input.value.trim(), cities_only: String(citiesOnly), session_token: sessionToken });
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
  editor.innerHTML = values.slice(0, 3).map((date, index) => `<div class="schedule-editor-row" data-schedule-index="${index}"><label>Date ${index + 1}<input required type="date" class="schedule-date" value="${date || ''}" /></label><div class="schedule-times"><div class="schedule-times-heading"><span>Available times</span><small>Up to 3 for this date</small></div><div class="schedule-time-list">${['18:00', '19:00', '20:00'].map((time) => `<label class="time-row"><input required class="time-input" type="time" value="${time}" /><button class="remove-time" type="button" aria-label="Remove time">×</button></label>`).join('')}</div><button class="add-time" type="button" data-add-schedule-time>+ Add another time</button></div></div>`).join('');
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
setupLocationPicker('guest-origin', 'guest-origin-menu', { onSelect: (details) => { state.guestOrigin = details; } });
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
function createEvent(event) {
  state.event = event;
  $('survey-link').value = event.url;
  $('qr-code').src = `https://api.qrserver.com/v1/create-qr-code/?size=180x180&data=${encodeURIComponent(event.url)}`;
  $('share-message').value = `🍽️ Help us pick ${event.name} in ${event.location}!\n\nVote here (30 seconds): ${event.url}\n\nPick the dates and vibe that work for you — we’ll find the best table for everyone.`;
  $('results-card').classList.remove('hidden');
  $('results-title').textContent = `${event.name} is ready for votes.`;
  $('share-modal').classList.remove('hidden');
}

$('signup-form').addEventListener('submit', async (event) => { event.preventDefault(); const email = $('organizer-email').value; const response = await fetch(`${API_BASE}/api/users`, { method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({ email }) }); const user = await response.json(); if (!response.ok) return alert(user.detail || 'Could not create organizer'); state.organizerId = user.id; localStorage.setItem('organizerEmail', email); localStorage.setItem('organizerId', user.id); hydrateDates(); show('organizer'); });
$('event-form').addEventListener('submit', async (event) => { event.preventDefault(); const scheduleRows = [...document.querySelectorAll('.schedule-editor-row')]; const availability = Object.fromEntries(scheduleRows.map((row) => [row.querySelector('.schedule-date').value, [...row.querySelectorAll('.time-input')].map((input) => input.value).filter(Boolean)]).filter(([date, slots]) => date && slots.length)); const dates = Object.keys(availability); const times = [...new Set(Object.values(availability).flat())]; const questions = Object.fromEntries(selectedTopics().map((key) => [key, questionState[key].filter((option) => questionEnabled[key].has(option))])); if (dates.length < 1 || dates.length > 3) return alert('Choose between one and three dates.'); if (new Set(dates).size !== dates.length) return alert('Choose a different date for each row.'); if (Object.values(availability).some((slots) => !slots.length || new Set(slots).size !== slots.length)) return alert('Give each date at least one unique time.'); if (Object.values(questions).some((options) => !options.length)) return alert('Keep at least one answer option in each question.'); const location = $('event-location'); const payload = { organizer_id:state.organizerId || 'local-organizer', event_name:$('event-name').value, location:location.value, location_place_id:location.dataset.placeId || null, location_lat:location.dataset.lat ? Number(location.dataset.lat) : null, location_lng:location.dataset.lng ? Number(location.dataset.lng) : null, dates, times, availability, questions }; const response = await fetch(`${API_BASE}/api/surveys`, { method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify(payload) }); const data = await response.json(); if (!response.ok) return alert(data.detail || 'Could not create survey'); state.event = { name:payload.event_name, location:payload.location, dates, times, availability, questions:payload.questions, surveyId:data.id, publicToken:data.public_token, url:data.share_url }; createEvent(state.event); });
$('schedule-editor').addEventListener('click', (event) => { const row = event.target.closest('.schedule-editor-row'); if (!row) return; if (event.target.matches('[data-add-schedule-time]')) { const list = row.querySelector('.schedule-time-list'); if (list.children.length >= 3) return; const time = document.createElement('label'); time.className = 'time-row'; time.innerHTML = '<input required class="time-input" type="time" value="21:00" /><button class="remove-time" type="button" aria-label="Remove time">×</button>'; list.appendChild(time); } if (event.target.classList.contains('remove-time') && row.querySelectorAll('.time-row').length > 1) event.target.closest('.time-row').remove(); });
$('copy-message').addEventListener('click', async () => { await navigator.clipboard?.writeText($('share-message').value); $('copied-note').classList.remove('hidden'); setTimeout(() => $('copied-note').classList.add('hidden'), 2400); });
document.querySelectorAll('[data-close-modal]').forEach((node) => node.addEventListener('click', () => $('share-modal').classList.add('hidden')));
$('open-survey').addEventListener('click', () => { $('share-modal').classList.add('hidden'); prepareSurvey(); show('survey'); });
$('back-organizer').addEventListener('click', () => show('organizer'));
$('reset-app').addEventListener('click', () => { state.event = null; state.responses = []; $('share-modal').classList.add('hidden'); $('results-card').classList.add('hidden'); show('signup'); });
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
function prepareSurvey() { const event = state.event || { name:'Friday dinner', location:'San Francisco', dates:defaultDates(), times:['19:00'], availability:Object.fromEntries(defaultDates().map((date) => [date, ['19:00']])), questions:{ cuisine:['Italian','Japanese','Mexican','Surprise me'] } }; state.guestOrigin = null; $('guest-origin').value = ''; const availability = event.availability || Object.fromEntries(event.dates.map((date) => [date, event.times])); const questions = event.questions || {}; $('survey-title').innerHTML = `Help pick <em>${event.name}.</em>`; $('survey-location').textContent = `${event.location} · about 30 seconds · no sign-up`; $('survey-schedule').innerHTML = renderSurveySchedule(availability); const questionMarkup = { cuisine:['What sounds good?', 'Pick up to two.', 'checkbox'], distance:['How far should we search?', 'Choose the maximum restaurant radius from the meetup spot. 30+ miles keeps this useful when everyone is spread out.', 'range'], vibe:["What's the vibe?", 'Choose one.', 'radio'], price:["What's the budget?", 'Per person, before drinks.', 'radio'], dietary:['Anything we should know?', 'Choose what the table should know.', 'checkbox'] }; $('survey-question-fields').innerHTML = Object.entries(questions).filter(([, options]) => options?.length).map(([key, options]) => { const [title, help, type] = questionMarkup[key] || [QUESTION_LABELS[key] || key, 'Choose what works for you.', 'radio']; const limit = key === 'cuisine' ? ' Pick up to two.' : ''; const control = type === 'range' ? renderDistanceQuestion(options) : options.map((option) => `<label class="survey-choice"><input ${type === 'radio' ? 'required' : ''} type="${type}" name="${key}" value="${option}" /> <span>${option}</span><b>${type === 'radio' ? '✓' : ''}</b></label>`).join(''); return `<fieldset><legend>${title}</legend><p class="question-help">${help}${limit}</p><div class="survey-choices">${control}</div></fieldset>`; }).join(''); }
$('survey-question-fields').addEventListener('input', (event) => { if (!event.target.matches('input[type="range"][data-distance-values]')) return; const values = event.target.dataset.distanceValues.split(','); const value = values[Number(event.target.value)]; event.target.parentElement.querySelector('output').textContent = formatDistance(value); event.target.parentElement.querySelector('input[type="hidden"]').value = value; });
$('survey-form').addEventListener('change', (event) => { if (event.target.name === 'cuisine' && document.querySelectorAll('input[name="cuisine"]:checked').length > 2) event.target.checked = false; });
$('survey-form').addEventListener('submit', async (event) => { event.preventDefault(); const availability = {}; document.querySelectorAll('[data-availability-date]').forEach((input) => { if (input.checked) (availability[input.dataset.availabilityDate] ||= []).push(input.value); }); const dates = Object.keys(availability); const times = [...new Set(Object.values(availability).flat())]; if (!dates.length || !times.length || !state.event?.publicToken) return; const origin = state.guestOrigin || {}; const answer = { dates, times, availability, cuisines:[...document.querySelectorAll('input[name="cuisine"]:checked')].map((input) => input.value), dietary:[...document.querySelectorAll('input[name="dietary"]:checked')].map((input) => input.value), distance:document.querySelector('input[name="distance"]:checked, input[type="hidden"][name="distance"]')?.value, vibe:document.querySelector('input[name="vibe"]:checked')?.value, price:document.querySelector('input[name="price"]:checked')?.value, origin_place_id:origin.place_id || null, origin_label:origin.label || null, origin_lat:origin.latitude ?? null, origin_lng:origin.longitude ?? null, respondent_token:localStorage.getItem('respondentToken') || crypto.randomUUID() }; localStorage.setItem('respondentToken', answer.respondent_token); const response = await fetch(`${API_BASE}/api/surveys/${state.event.publicToken}/responses`, { method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify(answer) }); if (!response.ok) return alert('Could not save your response. Please try again.'); state.responses.push(answer); $('survey-form').classList.add('hidden'); $('survey-thanks').classList.remove('hidden'); updateResponseSummary(); });
function updateResponseSummary() { if (!state.event) return; $('response-summary').textContent = `${state.responses.length} response${state.responses.length === 1 ? '' : 's'} collected · structured answers are ready for the recommendation agent.`; }
$('run-agent').addEventListener('click', async () => {
  if (!state.event) return;
  const button = $('run-agent');
  button.disabled = true;
  button.innerHTML = 'Asking the agent <span>…</span>';
  $('recommendations').innerHTML = '<p class="response-summary">Google Places is finding and hydrating candidates. OpenTable availability will be checked next.</p>';
  try {
    const response = await fetch(`${API_BASE}/api/surveys/${state.event.surveyId}/recommendations`, { method:'POST', headers:{'Content-Type':'application/json','X-Organizer-Id':state.organizerId || 'local-organizer'} });
    const data = await response.json();
    if (!response.ok) throw new Error(data.detail || 'Agent request failed');
    const answer = data.answer || '';
    const bookingUrl = findBookingUrl(answer);
    $('booking-handoff').innerHTML = bookingUrl ? `<a class="button secondary" href="${bookingUrl}" target="_blank" rel="noreferrer">Continue to booking <span>↗</span></a><p>Review the date, time, and party size, then confirm with the restaurant.</p>` : '';
    $('booking-handoff').classList.toggle('hidden', !bookingUrl);
    $('recommendations').innerHTML = `<article class="agent-answer"><div class="card-kicker">✦ / agent response</div><div>${formatAgentAnswer(answer)}</div></article>`;
  } catch (error) {
    $('recommendations').innerHTML = `<p class="error-message">Could not reach the agent API. Start it with <code>PYTHONPATH=src uvicorn groupreservations.api:app --reload --port 8000</code>.<br /><small>${error.message}</small></p>`;
  } finally {
    button.disabled = false;
    button.innerHTML = 'Find our top 3 <span>✦</span>';
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

async function loadPublicSurvey() { const token = new URLSearchParams(location.search).get('survey'); if (!token) return; const response = await fetch(`${API_BASE}/api/surveys/${token}`); const survey = await response.json(); if (!response.ok) return alert(survey.detail || 'Survey not found'); state.event = { name:survey.event_name, location:survey.location, dates:survey.dates, times:survey.times, availability:survey.availability, questions:survey.questions, publicToken:survey.public_token, surveyId:survey.id, url:location.href }; prepareSurvey(); show('survey'); }
loadPublicSurvey();
