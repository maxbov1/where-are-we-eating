const API_BASE = window.WAE_API_BASE || 'http://127.0.0.1:8000';
const organizerId = localStorage.getItem('organizerId');
const shelf = document.getElementById('event-shelf');
const escapeHtml = (value) => String(value).replaceAll('&','&amp;').replaceAll('<','&lt;').replaceAll('>','&gt;').replaceAll('"','&quot;');

if (!organizerId) {
  shelf.innerHTML = '<p class="response-summary">Start with your organizer email to view your events.</p><a class="button primary" href="event-creation.html">Start planning <span>→</span></a>';
} else {
  fetch(`${API_BASE}/api/organizers/${encodeURIComponent(organizerId)}/surveys`, { headers:{'X-Organizer-Id':organizerId} })
    .then(async (response) => { const data = await response.json(); if (!response.ok) throw new Error(data.detail || 'Could not load your events'); return data.events || []; })
    .then((events) => { shelf.innerHTML = events.length ? events.map((event) => `<a class="event-shelf-card" href="event-overview.html?survey=${encodeURIComponent(event.id)}"><span><strong>${escapeHtml(event.event_name)}</strong><small>${escapeHtml(event.location)} · ${event.response_count} response${event.response_count === 1 ? '' : 's'}</small></span><b>${event.is_open ? 'Open' : 'Closed'} <span aria-hidden="true">→</span></b></a>`).join('') : '<p class="response-summary">No events yet. Start with a quick dinner plan.</p>'; })
    .catch((error) => { shelf.innerHTML = `<p class="response-summary">${escapeHtml(error.message || 'Your event shelf is unavailable right now.')}</p>`; });
}
