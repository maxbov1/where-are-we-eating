// Standalone survey builder using the same survey API contract as app.js.
const API_BASE = window.WAE_API_BASE || 'http://127.0.0.1:8000';
const organizerId = localStorage.getItem('organizerId');
const form = document.getElementById('event-form');
const message = document.getElementById('builder-message');
const button = form.querySelector('button[type="submit"]');

function defaultDate() {
  const today = new Date();
  const friday = new Date(today);
  friday.setDate(today.getDate() + ((5 - today.getDay() + 7) % 7 || 7));
  return friday.toISOString().slice(0, 10);
}

document.getElementById('event-date').value = defaultDate();

if (!organizerId) {
  message.textContent = 'Please start by adding your email address.';
  button.disabled = true;
}

form.addEventListener('submit', async (event) => {
  event.preventDefault();
  if (!form.reportValidity() || !organizerId) return;

  const date = document.getElementById('event-date').value;
  const time = document.getElementById('event-time').value;
  const expiryDays = Number(document.getElementById('event-expiry').value) || 2;
  const payload = {
    organizer_id: organizerId,
    event_name: document.getElementById('event-name').value.trim(),
    location: document.getElementById('event-location').value.trim(),
    location_place_id: null,
    location_lat: null,
    location_lng: null,
    dates: [date],
    times: [time],
    availability: { [date]: [time] },
    questions: {
      cuisine: ['Italian', 'Japanese', 'Mexican', 'Thai', 'Indian', 'Surprise me'],
      price: ['$0–20 per person', '$20–40 per person', '$40–60 per person', '$60–80 per person', '$80+ per person'],
      vibe: ['Easygoing & casual', 'Make it special', 'Lively and social', "I'm along for the ride"],
      distance: ['1', '3', '5', '10', '15', '20', '30'],
    },
    expires_at: new Date(Date.now() + expiryDays * 86400000).toISOString(),
  };

  button.disabled = true;
  message.textContent = 'Preparing your invitation…';
  try {
    const response = await fetch(`${API_BASE}/api/surveys`, {
      method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(payload),
    });
    const survey = await response.json();
    if (!response.ok) throw new Error(survey.detail || 'Could not create survey');
    message.innerHTML = `Your survey is ready: <a href="${survey.share_url}">open the guest link</a>.`;
  } catch (error) {
    message.textContent = error instanceof Error ? error.message : 'Could not create survey. Please try again.';
    button.disabled = false;
  }
});
