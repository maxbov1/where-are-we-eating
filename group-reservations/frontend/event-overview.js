const API_BASE = window.WAE_API_BASE || 'http://127.0.0.1:8000';
const surveyId = new URLSearchParams(location.search).get('survey');
let isDemo = new URLSearchParams(location.search).get('demo') === '1';
const organizerId = localStorage.getItem('organizerId');
const $ = (id) => document.getElementById(id);
const escapeHtml = (value) => String(value).replaceAll('&','&amp;').replaceAll('<','&lt;').replaceAll('>','&gt;').replaceAll('"','&quot;');
const prettyDate = (value) => new Intl.DateTimeFormat('en-US',{weekday:'short',month:'short',day:'numeric'}).format(new Date(`${value}T12:00:00`));
const prettyTime = (value) => { const [hours,minutes] = value.split(':'); return new Intl.DateTimeFormat('en-US',{hour:'numeric',minute:'2-digit'}).format(new Date(2000,0,1,Number(hours),Number(minutes))); };
const labels = { cuisine:'Cuisine', price:'Budget', vibe:'Vibe', distance:'Distance', dietary:'Dietary needs' };
const dietaryLabels = { vegetarian:'vegetarian options', vegan:'vegan options', 'gluten-free':'gluten-free options', 'nut-free':'nut-free options', 'nut allergy':'nut allergy' };
const colors = ['#405238','#a84730','#e6a93d','#543d51','#78906b','#c9794f','#b08c3d','#76586e'];

function pieChart(key, item) {
  const votes = Object.entries(item.votes || {}).filter(([,count]) => Number(count) > 0).sort((a,b) => Number(b[1]) - Number(a[1]) || a[0].localeCompare(b[0]));
  const total = votes.reduce((sum,[,count]) => sum + Number(count), 0);
  const circumference = 263.89;
  let offset = 0;
  const segments = votes.map(([value,count], index) => { const length = circumference * (Number(count) / total); const segment = `<circle class="preference-donut-segment" cx="50" cy="50" r="42" stroke="${colors[index % colors.length]}" stroke-dasharray="${length} ${circumference - length}" stroke-dashoffset="${-offset}" />`; offset += length; return segment; }).join('');
  const legend = votes.map(([value,count], index) => `<li><i style="background:${colors[index % colors.length]}"></i><span>${escapeHtml(value)}</span><b>${count}</b><small>${Math.round((Number(count) / total) * 100)}%</small></li>`).join('');
  return `<section class="overview-block overview-category"><div class="visual-heading"><h3>${escapeHtml(labels[key] || key)}</h3></div><div class="category-visual"><div class="donut-wrap"><svg viewBox="0 0 100 100" role="img" aria-label="${escapeHtml(labels[key] || key)} response breakdown"><circle class="preference-donut-base" cx="50" cy="50" r="42" />${segments}</svg><strong>${total}</strong><span>responses</span></div><ul class="category-legend">${legend}</ul></div></section>`;
}

function distanceChart(item) {
  const votes = Object.entries(item.votes || {}).filter(([,count]) => Number(count) > 0).sort((a,b) => Number(a[0]) - Number(b[0]));
  const max = Math.max(...votes.map(([,count]) => Number(count)), 1);
  return `<section class="overview-block overview-distance"><div class="visual-heading"><h3>Travel distance</h3></div><div class="distance-visual">${votes.map(([value,count]) => `<div class="distance-row"><div><span>Up to ${escapeHtml(value)} mi</span><b>${count}</b></div><div class="visual-track"><i style="width:${Math.round((Number(count) / max) * 100)}%"></i></div></div>`).join('')}</div></section>`;
}

function scheduleChart(schedule) {
  const dates = Object.keys(schedule.times_by_date || {});
  const pairs = schedule.pair_votes || [];
  const times = [...new Set(pairs.map((pair) => pair.time))].sort();
  const counts = new Map(pairs.map((pair) => [`${pair.date}|${pair.time}`, Number(pair.votes) || 0]));
  const max = Math.max(...pairs.map((pair) => Number(pair.votes) || 0), 1);
  if (!dates.length || !times.length) return '';
  const header = `<div class="heatmap-corner"></div>${times.map((time) => `<div class="heatmap-time">${prettyTime(time)}</div>`).join('')}`;
  const rows = dates.map((date) => `<div class="heatmap-date">${prettyDate(date)}</div>${times.map((time) => { const allowed = (schedule.times_by_date[date] || []).includes(time); const votes = counts.get(`${date}|${time}`) || 0; if (!allowed) return '<div class="heatmap-cell heatmap-cell-empty" aria-hidden="true"></div>'; const intensity = 0.12 + (votes / max) * 0.78; return `<div class="heatmap-cell" data-heatmap-cell style="--heat-opacity:${intensity};" aria-label="${escapeHtml(prettyDate(date))} at ${escapeHtml(prettyTime(time))}: ${votes} vote${votes === 1 ? '' : 's'}"><strong>${votes || '—'}</strong></div>`; }).join('')}`).join('');
  const html = `<section class="overview-block overview-schedule"><div class="visual-heading"><h3>When</h3></div><div class="schedule-heatmap" style="--heatmap-columns:${times.length}">${header}${rows}</div></section>`;
  requestAnimationFrame(() => document.querySelectorAll('[data-heatmap-cell]').forEach((cell, index) => { if (window.motionAnimate) window.motionAnimate(cell, { opacity:[0,1], transform:['scale(.92)','scale(1)'] }, { duration:.35, delay:index * .04 }); else cell.style.opacity = 1; }));
  return html;
}

function renderOverview(aggregate) {
  const schedule = aggregate.report?.schedule || {}, preferences = aggregate.report?.preferences || {}, responseCount = aggregate.response_count || 0;
  const categories = Object.entries(preferences).filter(([key,item]) => key !== 'distance' && item?.votes && Object.keys(item.votes).length).map(([key,item]) => pieChart(key,item)).join('');
  const distance = preferences.distance?.votes ? distanceChart(preferences.distance) : '';
  const dietaryVotes = Object.entries(preferences.dietary?.votes || {}).filter(([value,count]) => value.toLowerCase() !== 'no restrictions' && Number(count) > 0);
  const dietary = dietaryVotes.length ? `<section class="overview-constraint"><h3>Dietary needs</h3><div class="constraint-list">${dietaryVotes.map(([value,count]) => `<span>${Number(count) === 1 ? '1 person' : `${count} people`} requested ${escapeHtml(dietaryLabels[value.toLowerCase()] || value.toLowerCase())}</span>`).join('')}</div></section>` : '';
  $('overview-grid').innerHTML = `<p class="overview-intro">${responseCount ? `${responseCount} person${responseCount === 1 ? '' : 's'} weighed in. Here’s where the group’s choices are converging.` : 'Waiting for the first response.'}</p>${scheduleChart(schedule)}${distance}${categories}${dietary}`;
}

async function load() {
  if (!surveyId || !organizerId) { $('overview-grid').innerHTML = '<p class="response-summary">This event link is missing or your organizer session has expired.</p>'; return; }
  if (isDemo) $('demo-notice').classList.remove('hidden');
  try {
    const [eventResponse, aggregateResponse] = await Promise.all([fetch(`${API_BASE}/api/surveys/${encodeURIComponent(surveyId)}`),fetch(`${API_BASE}/api/surveys/${encodeURIComponent(surveyId)}/aggregate`)]);
    const event = await eventResponse.json(), aggregate = await aggregateResponse.json();
    if (!eventResponse.ok || !aggregateResponse.ok) throw new Error(event.detail || aggregate.detail || 'Could not load this event');
    isDemo = event.is_demo === true;
    $('demo-notice').classList.toggle('hidden', !isDemo); $('overview-title').textContent = event.event_name; $('overview-location').textContent = event.location; $('find-recommendation').href = `recommendations.html?survey=${encodeURIComponent(surveyId)}`; renderOverview(aggregate);
  } catch (error) { $('overview-grid').innerHTML = `<p class="response-summary">${escapeHtml(error.message || 'Could not load this event.')}</p>`; }
}
load();
