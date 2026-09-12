// Standalone organizer entry flow. The original app.js remains unchanged.
const API_BASE = window.WAE_API_BASE || 'http://127.0.0.1:8000';
const form = document.getElementById('signup-form');
const email = document.getElementById('organizer-email');
const message = document.getElementById('form-message');
const submit = form.querySelector('button[type="submit"]');
const choices = document.getElementById('organizer-choices');
const EMAIL_PATTERN = /^[^\s@]+@[^\s@]+\.[^\s@]{2,}$/;

function setMessage(text, isError = false) {
  message.textContent = text;
  message.classList.toggle('error', isError);
}

form.addEventListener('submit', async (event) => {
  event.preventDefault();
  if (!email.reportValidity() || !EMAIL_PATTERN.test(email.value.trim())) {
    email.setCustomValidity('Enter a valid email address.');
    email.reportValidity();
    email.setCustomValidity('');
    return;
  }

  submit.disabled = true;
  setMessage('Setting your place at the table…');
  try {
    const response = await fetch(`${API_BASE}/api/users`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ email: email.value.trim() }),
    });
    const user = await response.json();
    if (!response.ok) throw new Error(user.detail || 'Could not create organizer');

    // Matches the existing app.js organizer persistence contract.
    localStorage.setItem('organizerEmail', email.value.trim());
    localStorage.setItem('organizerId', user.id);
    setMessage('Your place is saved.');
    form.hidden = true;
    form.style.display = 'none';
    choices.hidden = false;
    choices.style.display = 'grid';
  } catch (error) {
    setMessage(error instanceof Error ? error.message : 'Could not create organizer. Please try again.', true);
    submit.disabled = false;
  }
});
