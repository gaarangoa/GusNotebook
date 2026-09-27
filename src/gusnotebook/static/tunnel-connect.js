/* A click opens this local page immediately, before any asynchronous work. */
(() => {
  const base = document.body.dataset.base;
  const params = new URLSearchParams(location.search);
  let identifier = params.get('id'), generation = 0, setup = Promise.resolve();
  const status = document.getElementById('connection-status');
  const retry = document.getElementById('connection-retry');
  const cancel = document.getElementById('connection-cancel');
  document.getElementById('connection-name').textContent = params.get('name') || 'Remote workspace';
  async function api(path, body) {
    const response = await fetch(base + '/api/tunnels' + path, body === undefined ? {} : {
      method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify(body),
    });
    const data = await response.json();
    if (!response.ok) throw new Error(data.error || 'Connection request failed');
    return data;
  }
  function error(message) {
    status.textContent = message; retry.hidden = false; cancel.hidden = true;
  }
  async function start() {
    const attempt = ++generation;
    retry.hidden = true; cancel.hidden = false; status.textContent = 'Connecting…';
    setup = (async () => {
      if (!identifier) {
        const entry = await api('', {name:params.get('name') || params.get('tunnel'), tunnel:params.get('tunnel')});
        identifier = entry.id;
        const url = new URL(location.href); url.searchParams.set('id', identifier); history.replaceState(null, '', url);
      }
      if (attempt !== generation) return;
      await api('/' + encodeURIComponent(identifier) + '/connect', {});
    })();
    try {
      await setup;
      while (attempt === generation) {
        const data = await api('');
        if (attempt !== generation) return;
        const entry = data.saved.find(item => item.id === identifier);
        if (!entry) throw new Error('This saved tunnel was removed. Add it again in the Tunnels sidebar.');
        document.getElementById('connection-name').textContent = entry.name;
        if (entry.state === 'connected' && entry.url) {
          const url = new URL(entry.url);
          if (url.protocol !== 'http:' || url.hostname !== 'gusnotebook.localhost' || !url.port) throw new Error('Invalid connection address');
          location.replace(url.href); return;
        }
        if (entry.state === 'error') throw new Error(entry.error || 'Could not connect');
        if (entry.state !== 'connecting') throw new Error('Connection canceled. Retry when you are ready.');
        await new Promise(resolve => setTimeout(resolve, 500));
      }
    } catch (failure) { if (attempt === generation) error(failure.message); }
  }
  retry.addEventListener('click', start);
  cancel.addEventListener('click', async () => {
    generation++; cancel.disabled = true;
    // Finish the in-flight start before disconnecting, so it cannot reconnect
    // after cancellation or interfere with a later Retry.
    try {
      await setup.catch(() => {});
      if (identifier) await api('/' + encodeURIComponent(identifier) + '/disconnect', {});
    }
    catch (failure) { error(failure.message); return; }
    finally { cancel.disabled = false; }
    error('Connection canceled. The remote workspace is still running.');
  });
  start();
})();
