// Independent survey-builder implementation based on the organizer flow in app.js.
const API_BASE = window.WAE_API_BASE || 'http://127.0.0.1:8000';
const organizerId = localStorage.getItem('organizerId');
const $ = (id) => document.getElementById(id);
const QUESTION_DEFAULTS = { cuisine:['Italian','Japanese','Mexican','Thai','Indian','Surprise me'], price:['$0–20 per person','$20–40 per person','$40–60 per person','$60–80 per person','$80+ per person'], vibe:['Easygoing & casual','Make it special','Lively and social',"I'm along for the ride"], distance:['1','3','5','10','15','20','30'], dietary:['Vegetarian','Vegan','Gluten-free','Nut-free','No restrictions'] };
const QUESTION_LABELS = { cuisine:'Cuisine', price:'Budget', vibe:'Vibe', distance:'Distance', dietary:'Dietary needs' };
const questionState = Object.fromEntries(Object.entries(QUESTION_DEFAULTS).map(([key, values]) => [key, [...values]]));
const questionEnabled = Object.fromEntries(Object.entries(questionState).map(([key, values]) => [key, new Set(values)]));
let activeQuestion = null;

function fridayDates() { const today = new Date(); const friday = new Date(today); friday.setDate(today.getDate() + ((5 - today.getDay() + 7) % 7 || 7)); return [friday.toISOString().slice(0,10)]; }
function escapeHtml(value) { return String(value).replaceAll('&','&amp;').replaceAll('<','&lt;').replaceAll('>','&gt;').replaceAll('"','&quot;'); }
function formatDistance(value) { const miles = Number(value); return Number.isFinite(miles) ? `${miles}${miles === 30 ? '+' : ''} mile${miles === 1 ? '' : 's'} from meetup` : value; }
function formatApiError(detail) { if (Array.isArray(detail)) return detail.map((item) => { if (typeof item === 'string') return item; const location = Array.isArray(item?.loc) ? item.loc.filter((part) => part !== 'body').join('.') : ''; const message = item?.msg || item?.message || JSON.stringify(item); return location ? `${location}: ${message}` : message; }).join(' '); if (detail && typeof detail === 'object') return detail.msg || detail.message || JSON.stringify(detail); return String(detail || 'Could not create survey'); }
function renderScheduleEditor(values = fridayDates()) { const schedules = values.slice(0,3).map((item) => typeof item === 'string' ? { date:item, times:['18:00'] } : { date:item.date || '', times:item.times?.length ? item.times.slice(0,3) : ['18:00'] }); $('schedule-editor').innerHTML = schedules.map((schedule,index) => `<div class="schedule-editor-row"><label>Date ${index + 1}<input required type="date" class="schedule-date" value="${schedule.date || ''}" /></label><div class="schedule-times"><div class="schedule-times-heading"><span>Available times</span><small>Up to 3 for this date</small></div><div class="schedule-time-list">${schedule.times.map((time) => `<label class="time-row"><input required class="time-input" type="time" value="${time || '18:00'}" /><button class="remove-time" type="button" aria-label="Remove time">×</button></label>`).join('')}</div><button class="add-time" type="button">+ Add another time</button></div></div>`).join('') + (schedules.length < 3 ? '<button class="add-date" type="button">+ Add another date</button>' : ''); }
function selectedTopics() { const inputs = [...document.querySelectorAll('#question-topics input[type="checkbox"]')]; return inputs.length ? inputs.filter((input) => input.checked).map((input) => input.value) : ['cuisine','price','vibe','distance']; }
function renderQuestionOptions() { const enabledTopics = selectedTopics(); $('question-topics').innerHTML = Object.keys(QUESTION_DEFAULTS).map((key) => { const enabled = enabledTopics.includes(key); return `<div class="topic-row"><label class="topic-check"><input type="checkbox" value="${key}" ${enabled ? 'checked' : ''} /><span><strong>${QUESTION_LABELS[key]}</strong><small>${enabled ? `${questionEnabled[key].size} choices ready` : 'Not included'}</small></span></label><button type="button" class="topic-open" data-open-question="${key}" ${enabled ? '' : 'disabled'}>Edit <span>›</span></button></div>`; }).join(''); if (!activeQuestion || !enabledTopics.includes(activeQuestion)) { $('question-drawer').classList.add('hidden'); return; } const key = activeQuestion; $('question-drawer-title').textContent = QUESTION_LABELS[key]; $('question-options').innerHTML = `<div class="option-toggle-list">${questionState[key].map((option,index) => `<label class="option-toggle"><input type="checkbox" data-question="${key}" data-option-index="${index}" ${questionEnabled[key].has(option) ? 'checked' : ''} /><span>${key === 'distance' ? formatDistance(option) : option}</span></label>`).join('')}</div><button class="add-time" type="button" data-add-option="${key}">+ Add a choice</button>`; $('question-drawer').classList.remove('hidden'); }

function setupLocationPicker() {
  const input = $('event-location');
  const menu = $('organizer-location-menu');
  const status = $('location-status');
  let timer;
  let sessionToken = crypto.randomUUID();
  input.addEventListener('input', () => {
    delete input.dataset.placeId;
    delete input.dataset.lat;
    delete input.dataset.lng;
    status.textContent = '';
    clearTimeout(timer);
    menu.innerHTML = '';
    menu.classList.add('hidden');
    if (input.value.trim().length < 2) return;
    timer = setTimeout(async () => {
      try {
        const query = new URLSearchParams({ input:input.value.trim(), cities_only:'true', session_token:sessionToken });
        const response = await fetch(`${API_BASE}/api/locations/autocomplete?${query}`);
        if (!response.ok) throw new Error('Location lookup unavailable');
        const data = await response.json();
        menu.innerHTML = (data.predictions || []).map((item) => `<button type="button" class="location-option" role="option" data-place-id="${escapeHtml(item.place_id)}" data-label="${escapeHtml(item.text)}"><strong>${escapeHtml(item.main_text || item.text)}</strong><span>${escapeHtml(item.secondary_text || '')}</span></button>`).join('');
        menu.classList.toggle('hidden', !menu.children.length);
        if (!menu.children.length) status.textContent = 'No matching cities found.';
      } catch { menu.classList.add('hidden'); status.textContent = 'City suggestions are temporarily unavailable.'; }
    }, 250);
  });
  menu.addEventListener('click', async (event) => {
    const option = event.target.closest('.location-option');
    if (!option) return;
    try {
      const response = await fetch(`${API_BASE}/api/locations/details`, { method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({ place_id:option.dataset.placeId, session_token:sessionToken }) });
      if (!response.ok) throw new Error('Could not verify city');
      const details = await response.json();
      input.value = details.label || option.dataset.label;
      input.dataset.placeId = details.place_id || option.dataset.placeId;
      input.dataset.lat = details.latitude ?? '';
      input.dataset.lng = details.longitude ?? '';
      status.textContent = 'Verified city';
      menu.classList.add('hidden');
      sessionToken = crypto.randomUUID();
    } catch { status.textContent = 'Could not verify that city. Please choose another suggestion.'; }
  });
  document.addEventListener('click', (event) => { if (!event.target.closest('#event-location') && !event.target.closest('#organizer-location-menu')) menu.classList.add('hidden'); });
}

renderScheduleEditor(); renderQuestionOptions(); setupLocationPicker();
$('question-topics').addEventListener('change', renderQuestionOptions);
$('question-topics').addEventListener('click', (event) => { const button = event.target.closest('[data-open-question]'); if (button) { activeQuestion = button.dataset.openQuestion; renderQuestionOptions(); } });
$('question-options').addEventListener('change', (event) => { if (!event.target.matches('[data-question]')) return; const key = event.target.dataset.question; const option = questionState[key][Number(event.target.dataset.optionIndex)]; if (event.target.checked) questionEnabled[key].add(option); else questionEnabled[key].delete(option); if (!questionEnabled[key].size) { event.target.checked = true; questionEnabled[key].add(option); } renderQuestionOptions(); });
$('question-options').addEventListener('click', (event) => { const button = event.target.closest('[data-add-option]'); if (!button) return; const key = button.dataset.addOption; const value = window.prompt(`Add a ${QUESTION_LABELS[key].toLowerCase()} choice`); if (!value?.trim() || questionState[key].includes(value.trim()) || questionState[key].length >= 10) return; questionState[key].push(value.trim()); questionEnabled[key].add(value.trim()); renderQuestionOptions(); });
$('question-drawer').addEventListener('click', (event) => { if (event.target.matches('[data-close-question]') || event.target === $('question-drawer')) { activeQuestion = null; $('question-drawer').classList.add('hidden'); } });
$('schedule-editor').addEventListener('click', (event) => { if (event.target.matches('.add-date')) { const rows = [...$('schedule-editor').querySelectorAll('.schedule-editor-row')]; if (rows.length >= 3) return; const schedules = rows.map((item) => ({ date:item.querySelector('.schedule-date').value, times:[...item.querySelectorAll('.time-input')].map((input) => input.value) })); const lastDate = schedules.at(-1)?.date; const nextDate = lastDate ? new Date(`${lastDate}T12:00:00`) : new Date(); nextDate.setDate(nextDate.getDate() + 7); schedules.push({ date:nextDate.toISOString().slice(0,10), times:['18:00'] }); renderScheduleEditor(schedules); return; } const row = event.target.closest('.schedule-editor-row'); if (!row) return; if (event.target.matches('.add-time')) { const list = row.querySelector('.schedule-time-list'); if (list.children.length < 3) list.insertAdjacentHTML('beforeend','<label class="time-row"><input required class="time-input" type="time" value="21:00" /><button class="remove-time" type="button" aria-label="Remove time">×</button></label>'); } if (event.target.matches('.remove-time') && row.querySelectorAll('.time-row').length > 1) event.target.closest('.time-row').remove(); });

$('event-form').addEventListener('submit', async (event) => { event.preventDefault(); const rows = [...document.querySelectorAll('.schedule-editor-row')]; const availability = Object.fromEntries(rows.map((row) => [row.querySelector('.schedule-date').value, [...row.querySelectorAll('.time-input')].map((input) => input.value).filter(Boolean)]).filter(([date, times]) => date && times.length)); const dates = Object.keys(availability); const times = [...new Set(Object.values(availability).flat())]; const questions = Object.fromEntries(selectedTopics().map((key) => [key, questionState[key].filter((option) => questionEnabled[key].has(option))])); const message = $('builder-message'); const location = $('event-location'); if (!organizerId) { message.textContent = 'Please start with your organizer email.'; return; } if (!location.dataset.placeId) { message.textContent = 'Choose a verified city from the suggestions.'; location.focus(); return; } if (dates.length < 1 || dates.length > 3) { message.textContent = 'Choose between one and three dates.'; return; } if (new Set(dates).size !== dates.length || Object.values(availability).some((slots) => new Set(slots).size !== slots.length)) { message.textContent = 'Choose unique dates and times.'; return; } const expiryDays = Number($('event-expiry').value) || 2; const payload = { organizer_id:organizerId, event_name:$('event-name').value.trim(), location:location.value.trim(), location_place_id:location.dataset.placeId, location_lat:location.dataset.lat ? Number(location.dataset.lat) : null, location_lng:location.dataset.lng ? Number(location.dataset.lng) : null, dates, times, availability, questions, expires_at:new Date(Date.now() + expiryDays * 86400000).toISOString() }; const button = event.target.querySelector('.submit-button'); button.disabled = true; message.textContent = 'Preparing your invitation…'; try { const response = await fetch(`${API_BASE}/api/surveys`, { method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify(payload) }); const data = await response.json(); if (!response.ok) throw new Error(formatApiError(data.detail)); localStorage.setItem('lastCreatedSurvey', JSON.stringify({ name:payload.event_name, location:payload.location, shareUrl:data.share_url, expiresAt:data.expires_at || payload.expires_at })); window.location.assign('share-event.html'); } catch (error) { message.textContent = error instanceof Error ? error.message : 'Could not create survey. Please try again.'; button.disabled = false; } });
