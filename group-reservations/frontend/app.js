const state = { event: null, responses: [], organizerId: localStorage.getItem('organizerId') };
const $ = (id) => document.getElementById(id);
const screens = { signup: $('signup-screen'), organizer: $('organizer-screen'), survey: $('survey-screen') };
const API_BASE = window.WAE_API_BASE || 'http://127.0.0.1:8000';

function show(screen) { Object.values(screens).forEach((node) => node.classList.remove('active')); screens[screen].classList.add('active'); window.scrollTo({ top: 0, behavior: 'smooth' }); }
function defaultDates() { const today = new Date(); const friday = new Date(today); friday.setDate(today.getDate() + ((5 - today.getDay() + 7) % 7 || 7)); return [0, 7, 14].map((offset) => { const date = new Date(friday); date.setDate(friday.getDate() + offset); return date.toISOString().slice(0, 10); }); }
function prettyDate(value) { return new Intl.DateTimeFormat('en-US', { weekday: 'short', month: 'short', day: 'numeric' }).format(new Date(`${value}T12:00:00`)); }
function prettyTime(value) { const [hours, minutes] = value.split(':'); return new Intl.DateTimeFormat('en-US', { hour:'numeric', minute:'2-digit' }).format(new Date(2000, 0, 1, Number(hours), Number(minutes))); }

function hydrateDates() { defaultDates().forEach((date, index) => { $(`date-${index + 1}`).value = date; }); }
const QUESTION_DEFAULTS = {
  cuisine: ['Italian', 'Japanese', 'Mexican', 'Thai', 'Indian', 'Surprise me'],
  price: ['$ · Keep it easy', '$$ · A nice night out', '$$$ · Worth the splurge', '$$$$ · Make it a night'],
  vibe: ['Easygoing & casual', 'Make it special', 'Lively and social', "I'm along for the ride"],
  distance: ['Walkable', 'A short ride is fine', 'Anywhere in the city'],
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
  $('question-options').innerHTML = `<div class="option-toggle-list">${questionState[key].map((option, index) => `<label class="option-toggle"><input type="checkbox" data-question="${key}" data-option-index="${index}" ${questionEnabled[key].has(option) ? 'checked' : ''} /> <span>${option}</span><b>✓</b></label>`).join('')}</div><button class="drawer-add" type="button" data-add-option="${key}">+ Add a choice</button><p class="drawer-note">${key === 'cuisine' ? 'Guests can choose up to two.' : 'Keep the choices clear and easy to scan.'}</p>`;
  $('question-drawer').classList.remove('hidden');
}
renderQuestionOptions();
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
$('event-form').addEventListener('submit', async (event) => { event.preventDefault(); const dates = [1,2,3].map((i) => $(`date-${i}`).value).filter(Boolean); const times = [...document.querySelectorAll('#time-options .time-input')].map((input) => input.value).filter(Boolean); const questions = Object.fromEntries(selectedTopics().map((key) => [key, questionState[key].filter((option) => questionEnabled[key].has(option))])); if (new Set(times).size !== times.length) return alert('Choose a different time for each slot.'); if (Object.values(questions).some((options) => !options.length)) return alert('Keep at least one answer option in each question.'); const payload = { organizer_id:state.organizerId || 'local-organizer', event_name:$('event-name').value, location:$('event-location').value, dates, times, questions }; const response = await fetch(`${API_BASE}/api/surveys`, { method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify(payload) }); const data = await response.json(); if (!response.ok) return alert(data.detail || 'Could not create survey'); state.event = { name:payload.event_name, location:payload.location, dates, times, questions:payload.questions, surveyId:data.id, publicToken:data.public_token, url:data.share_url }; createEvent(state.event); });
$('add-time').addEventListener('click', () => { const editor = $('time-options'); if (editor.querySelectorAll('.time-row').length >= 3) return; const row = document.createElement('label'); row.className = 'time-row'; row.innerHTML = '<input required class="time-input" type="time" value="21:00" /><button class="remove-time" type="button" aria-label="Remove time">×</button>'; editor.appendChild(row); });
$('time-options').addEventListener('click', (event) => { if (event.target.classList.contains('remove-time') && $('time-options').querySelectorAll('.time-row').length > 1) event.target.closest('.time-row').remove(); });
$('copy-message').addEventListener('click', async () => { await navigator.clipboard?.writeText($('share-message').value); $('copied-note').classList.remove('hidden'); setTimeout(() => $('copied-note').classList.add('hidden'), 2400); });
document.querySelectorAll('[data-close-modal]').forEach((node) => node.addEventListener('click', () => $('share-modal').classList.add('hidden')));
$('open-survey').addEventListener('click', () => { $('share-modal').classList.add('hidden'); prepareSurvey(); show('survey'); });
$('back-organizer').addEventListener('click', () => show('organizer'));
$('reset-app').addEventListener('click', () => { state.event = null; state.responses = []; $('share-modal').classList.add('hidden'); $('results-card').classList.add('hidden'); show('signup'); });
$('copy-link').addEventListener('click', async () => { await navigator.clipboard?.writeText($('survey-link').value); $('copied-note').textContent = 'Link copied — ready for the group chat.'; $('copied-note').classList.remove('hidden'); setTimeout(() => $('copied-note').classList.add('hidden'), 2400); });

function prepareSurvey() { const event = state.event || { name:'Friday dinner', location:'San Francisco', dates:defaultDates(), times:['19:00'], questions:{ cuisine:['Italian','Japanese','Mexican','Surprise me'] } }; const questions = event.questions || {}; $('survey-title').innerHTML = `Help pick <em>${event.name}.</em>`; $('survey-location').textContent = `${event.location} · about 30 seconds · no sign-up`; $('survey-dates').innerHTML = event.dates.map((date) => `<label class="survey-choice"><input type="checkbox" name="date" value="${date}" /> <span>${prettyDate(date)}</span><b>✓</b></label>`).join(''); $('survey-times').innerHTML = event.times.map((time) => `<label class="survey-choice"><input type="checkbox" name="time" value="${time}" /> <span>${prettyTime(time)}</span><b>✓</b></label>`).join(''); const questionMarkup = { cuisine:['What sounds good?', 'Pick up to two.', 'checkbox'], distance:['How far we going?', 'From the center of the group.', 'radio'], vibe:["What's the vibe?", 'Choose one.', 'radio'], price:["What's the budget?", 'Per person, before drinks.', 'radio'], dietary:['Anything we should know?', 'Choose what the table should know.', 'checkbox'] }; $('survey-question-fields').innerHTML = Object.entries(questions).filter(([, options]) => options?.length).map(([key, options]) => { const [title, help, type] = questionMarkup[key] || [QUESTION_LABELS[key] || key, 'Choose what works for you.', 'radio']; const limit = key === 'cuisine' ? ' Pick up to two.' : ''; return `<fieldset><legend>${title}</legend><p class="question-help">${help}${limit}</p><div class="survey-choices">${options.map((option) => `<label class="survey-choice"><input ${type === 'radio' ? 'required' : ''} type="${type}" name="${key}" value="${option}" /> <span>${option}</span><b>${type === 'radio' ? '✓' : ''}</b></label>`).join('')}</div></fieldset>`; }).join(''); }
$('survey-form').addEventListener('change', (event) => { if (event.target.name === 'cuisine' && document.querySelectorAll('input[name="cuisine"]:checked').length > 2) event.target.checked = false; });
$('survey-form').addEventListener('submit', async (event) => { event.preventDefault(); const selected = [...document.querySelectorAll('input[name="date"]:checked')]; const selectedTimes = [...document.querySelectorAll('input[name="time"]:checked')]; if (!selected.length || !selectedTimes.length || !state.event?.publicToken) return; const answer = { dates:selected.map((input) => input.value), times:selectedTimes.map((input) => input.value), cuisines:[...document.querySelectorAll('input[name="cuisine"]:checked')].map((input) => input.value), dietary:[...document.querySelectorAll('input[name="dietary"]:checked')].map((input) => input.value), distance:document.querySelector('input[name="distance"]:checked')?.value, vibe:document.querySelector('input[name="vibe"]:checked')?.value, price:document.querySelector('input[name="price"]:checked')?.value, respondent_token:localStorage.getItem('respondentToken') || crypto.randomUUID() }; localStorage.setItem('respondentToken', answer.respondent_token); const response = await fetch(`${API_BASE}/api/surveys/${state.event.publicToken}/responses`, { method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify(answer) }); if (!response.ok) return alert('Could not save your response. Please try again.'); state.responses.push(answer); $('survey-form').classList.add('hidden'); $('survey-thanks').classList.remove('hidden'); updateResponseSummary(); });
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
    $('recommendations').innerHTML = `<article class="agent-answer"><div class="card-kicker">✦ / agent response</div><div>${(data.answer || '').replaceAll('\n','<br />')}</div></article>`;
  } catch (error) {
    $('recommendations').innerHTML = `<p class="error-message">Could not reach the agent API. Start it with <code>PYTHONPATH=src uvicorn groupreservations.api:app --reload --port 8000</code>.<br /><small>${error.message}</small></p>`;
  } finally {
    button.disabled = false;
    button.innerHTML = 'Find our top 3 <span>✦</span>';
  }
});

async function loadPublicSurvey() { const token = new URLSearchParams(location.search).get('survey'); if (!token) return; const response = await fetch(`${API_BASE}/api/surveys/${token}`); const survey = await response.json(); if (!response.ok) return alert(survey.detail || 'Survey not found'); state.event = { name:survey.event_name, location:survey.location, dates:survey.dates, times:survey.times, questions:survey.questions, publicToken:survey.public_token, surveyId:survey.id, url:location.href }; prepareSurvey(); show('survey'); }
loadPublicSurvey();
