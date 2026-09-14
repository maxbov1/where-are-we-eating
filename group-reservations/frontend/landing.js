const API_BASE = window.WAE_API_BASE || 'http://127.0.0.1:8000';
const button = document.getElementById('demo-entry');
const message = document.getElementById('demo-message');

button?.addEventListener('click', async () => {
  button.disabled = true;
  message.textContent = 'Preparing the demo table…';
  try {
    const response = await fetch(`${API_BASE}/api/demo`, { method: 'POST' });
    const data = await response.json();
    if (!response.ok) throw new Error(data.detail || 'Could not prepare the demo.');
    localStorage.setItem('organizerId', data.organizer_id);
    localStorage.setItem('organizerEmail', 'Judge demo');
    window.location.assign(`event-overview.html?survey=${encodeURIComponent(data.survey_id)}&demo=1`);
  } catch (error) {
    message.textContent = error instanceof Error ? error.message : 'Could not prepare the demo.';
    button.disabled = false;
  }
});
