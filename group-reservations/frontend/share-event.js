const survey = JSON.parse(localStorage.getItem('lastCreatedSurvey') || 'null');
const $ = (id) => document.getElementById(id);

function setStatus(text) { $('share-status').textContent = text; window.clearTimeout(setStatus.timer); setStatus.timer = window.setTimeout(() => { $('share-status').textContent = ''; }, 2400); }
async function copyValue(value, label) {
  try { await navigator.clipboard.writeText(value); } catch { const input = document.createElement('textarea'); input.value = value; document.body.append(input); input.select(); document.execCommand('copy'); input.remove(); }
  setStatus(`${label} copied — ready to share.`);
}

if (!survey?.shareUrl) {
  $('share-title').textContent = 'Invitation not found.';
  $('event-summary').textContent = 'Start a new plan to create a shareable guest invitation.';
  document.querySelector('.share-grid').classList.add('hidden');
} else {
  $('event-summary').textContent = `${survey.name} · ${survey.location}`;
  $('survey-link').value = survey.shareUrl;
  $('share-message').value = `🍽️ Help us pick ${survey.name} in ${survey.location}!\n\nVote here (30 seconds): ${survey.shareUrl}\n\nPick the dates and vibe that work for you — we’ll find the best table for everyone.`;
  if (window.QRCode) window.QRCode.toDataURL(survey.shareUrl, { width:180, margin:1, errorCorrectionLevel:'M' }).then((url) => { $('qr-code').src = url; });
  else $('qr-code').alt = 'QR code unavailable; use the guest link below.';
  $('copy-link').addEventListener('click', () => copyValue($('survey-link').value, 'Guest link'));
  $('copy-message').addEventListener('click', () => copyValue($('share-message').value, 'Group message'));
}
