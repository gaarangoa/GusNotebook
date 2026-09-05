const unlockForm = document.getElementById('unlock-form');
const launchToken = document.getElementById('launch-token');
const suppliedToken = new URLSearchParams(location.hash.slice(1)).get('token');
history.replaceState(null, '', location.pathname + location.search);
async function unlock(event) {
  if (event) event.preventDefault();
  try {
    const response = await fetch(unlockForm.dataset.base + '/auth', {
      method: 'POST', headers: {Authorization: 'Bearer ' + launchToken.value},
    });
    if (!response.ok) throw new Error('The launch token is invalid. Use the current launch link.');
    location.reload();
  } catch (error) {
    document.getElementById('unlock-error').textContent = error.message;
  }
}
unlockForm.addEventListener('submit', unlock);
if (suppliedToken) { launchToken.value = suppliedToken; unlock(); }
