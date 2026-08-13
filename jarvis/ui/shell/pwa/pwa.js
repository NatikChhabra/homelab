/**
 * JARVIS PWA — phone-first voice client.
 *
 * Two modes:
 *  - PTT: push-to-talk, same protocol as desktop app.js.
 *  - Satellite: continuous mic feeding wake-word, room-tagged, broadcast
 *    detections to all connected HUDs via the server.
 *
 * Audio: 16 kHz mono int16 PCM, sent as binary WebSocket frames.
 * Wake Lock API used in satellite mode to keep screen alive.
 */
'use strict';

/* -- state ---------------------------------------------------------------- */
let ws = null;
let audioCtx = null;
let micStream = null;
let micNode = null;
let state = 'offline';   // offline | idle | listening | thinking | speaking
let reconnectTimer = null;
let wakeLock = null;
let satelliteRunning = false;
let satelliteRoom = '';
let pttActive = false;

/* -- DOM refs ------------------------------------------------------------- */
const $ = (s) => document.querySelector(s);
const els = {
  statusDot:   $('#statusDot'),
  statusText:  $('#statusText'),
  orb:         $('#orb'),
  orbLabel:    $('#orbLabel'),
  pttBtn:      $('#pttBtn'),
  transcript:  $('#transcript'),
  textInput:   $('#textInput'),
  glance:      $('#glance'),
  tileLights:  $('#tile-lights'),
  tileFan:     $('#tile-fan'),
  tileAc:      $('#tile-ac'),
  tilePc:      $('#tile-pc'),
  satBtn:      $('#satBtn'),
  satStatus:   $('#satStatus'),
  roomInput:   $('#roomInput'),
  wakeToast:   $('#wakeToast'),
  toast:       $('#toast'),
  installBanner: $('#installBanner'),
};

/* -- WebSocket ------------------------------------------------------------ */
function connect() {
  const proto = location.protocol === 'https:' ? 'wss' : 'ws';
  ws = new WebSocket(`${proto}://${location.host}/ws`);
  ws.binaryType = 'arraybuffer';

  ws.onopen = () => {
    clearTimeout(reconnectTimer);
    els.statusDot.className = 'status-dot connected';
    els.statusText.textContent = 'connected';
    setOrb('idle');
    logSys('connected');
  };

  ws.onmessage = (event) => {
    if (event.data instanceof ArrayBuffer) return;  // TTS PCM — not used in PWA
    let msg;
    try { msg = JSON.parse(event.data); } catch { return; }
    handleMsg(msg);
  };

  ws.onclose = () => {
    els.statusDot.className = 'status-dot';
    els.statusText.textContent = 'disconnected';
    setOrb('offline');
    logSys('disconnected — reconnecting…');
    if (!satelliteRunning) setOrb('offline');
    reconnectTimer = setTimeout(connect, 2000);
  };

  ws.onerror = () => { /* onclose fires after; swallow here */ };
}

function handleMsg(msg) {
  switch (msg.type) {
    case 'ready':
      logSys(`ready — wake word: ${msg.wake_word}`);
      break;
    case 'wake':
      setOrb('listening');
      break;
    case 'thinking':
      setOrb('thinking');
      break;
    case 'transcript':
      logUser(msg.text);
      break;
    case 'reply':
      logJarvis(msg.text);
      if (msg.route) {
        logSys(`route: ${msg.route}${msg.local_ms ? ` · ${Math.round(msg.local_ms)}ms` : ''}`);
      }
      break;
    case 'speak-start':
      setOrb('speaking');
      break;
    case 'speak-end':
      setOrb('idle');
      break;
    case 'idle':
      setOrb('idle');
      break;
    case 'error':
      showToast(msg.message, 'error');
      setOrb('idle');
      break;
    case 'satellite_started':
      els.satStatus.className = 'sat-status live';
      els.satStatus.textContent = `Satellite live in "${msg.room}". Mic active.`;
      els.statusDot.className = 'status-dot connected satellite-live';
      logSys(`satellite started: ${msg.room}`);
      requestWakeLock();
      break;
    case 'satellite_stopped':
      els.satStatus.className = 'sat-status';
      els.satStatus.textContent = 'Satellite stopped.';
      releaseWakeLock();
      satelliteRunning = false;
      updateSatBtn();
      break;
    case 'satellite_wake':
      // Show toast + vibrate; do NOT auto-act.
      showWakeToast(msg);
      if (navigator.vibrate) navigator.vibrate(200);
      break;
  }
}

/* -- mic ------------------------------------------------------------------ */
async function startMic() {
  if (micStream) return;
  try {
    micStream = await navigator.mediaDevices.getUserMedia({
      audio: { channelCount: 1, echoCancellation: true, noiseSuppression: true, autoGainControl: true },
    });
    audioCtx = new (window.AudioContext || window.webkitAudioContext)({ sampleRate: 16000 });
    if (audioCtx.state === 'suspended') await audioCtx.resume();
    await audioCtx.audioWorklet.addModule('/shell/pwa/mic-worklet.js');
    const source = audioCtx.createMediaStreamSource(micStream);
    micNode = new AudioWorkletNode(audioCtx, 'mic-processor');
    micNode.port.onmessage = ({ data }) => {
      if (data.type === 'audio' && ws && ws.readyState === WebSocket.OPEN) {
        ws.send(data.pcm.buffer);
      }
    };
    source.connect(micNode);
    logSys('mic live');
  } catch (err) {
    showToast(`Mic denied: ${err.message}`, 'error');
    logSys(`mic error: ${err.message}`);
  }
}

function stopMic() {
  if (micNode) { micNode.disconnect(); micNode = null; }
  if (micStream) { micStream.getTracks().forEach(t => t.stop()); micStream = null; }
  if (audioCtx) { audioCtx.close(); audioCtx = null; }
}

/* -- PTT ------------------------------------------------------------------ */
async function togglePtt() {
  if (pttActive) {
    // Release: send an empty binary to signal end, then trigger the session.
    pttActive = false;
    els.pttBtn.textContent = 'HOLD TO TALK';
    els.pttBtn.style.background = '';
    if (ws && ws.readyState === WebSocket.OPEN) {
      ws.send(JSON.stringify({ type: 'push_to_talk' }));
    }
    return;
  }
  // Press: start mic if needed, enter listening state.
  pttActive = true;
  els.pttBtn.textContent = 'RELEASE TO SEND';
  els.pttBtn.style.background = 'rgba(245,158,11,.15)';
  setOrb('listening');
  if (!audioCtx) await startMic();
  if (ws && ws.readyState === WebSocket.OPEN) {
    ws.send(JSON.stringify({ type: 'push_to_talk' }));
  }
}

/* Touch/mouse hold detection for PTT */
(function initPttHold() {
  const btn = els.pttBtn;
  let holdTimer = null;

  function startHold(e) {
    e.preventDefault();
    if (pttActive) return;
    holdTimer = setTimeout(() => togglePtt(), 50);
  }
  function endHold(e) {
    e.preventDefault();
    clearTimeout(holdTimer);
    if (pttActive) togglePtt();
  }
  btn.addEventListener('mousedown', startHold);
  btn.addEventListener('mouseup', endHold);
  btn.addEventListener('mouseleave', endHold);
  btn.addEventListener('touchstart', startHold, { passive: false });
  btn.addEventListener('touchend', endHold, { passive: false });
  btn.addEventListener('touchcancel', endHold, { passive: false });
})();

/* -- text input ----------------------------------------------------------- */
function sendText() {
  const text = els.textInput.value.trim();
  if (!text || !ws || ws.readyState !== WebSocket.OPEN) return;
  logUser(text);
  ws.send(JSON.stringify({ type: 'text', text }));
  els.textInput.value = '';
}
els.textInput.addEventListener('keydown', (e) => {
  if (e.key === 'Enter') sendText();
});

/* -- satellite ------------------------------------------------------------ */
async function toggleSatellite() {
  if (satelliteRunning) {
    // Stop.
    if (ws && ws.readyState === WebSocket.OPEN) {
      ws.send(JSON.stringify({ type: 'satellite_stop' }));
    }
    satelliteRunning = false;
    updateSatBtn();
    return;
  }
  // Start.
  const room = els.roomInput.value.trim().toLowerCase();
  if (!room) {
    showToast('Enter a room name first.', 'error');
    return;
  }
  if (!ws || ws.readyState !== WebSocket.OPEN) {
    showToast('Not connected to server.', 'error');
    return;
  }
  satelliteRoom = room;
  ws.send(JSON.stringify({ type: 'satellite_start', room }));
  satelliteRunning = true;
  updateSatBtn();
  await startMic();
}

function updateSatBtn() {
  if (satelliteRunning) {
    els.satBtn.textContent = 'STOP SATELLITE';
    els.satBtn.className = 'sat-btn stop';
  } else {
    els.satBtn.textContent = 'START SATELLITE';
    els.satBtn.className = 'sat-btn';
  }
}

/* -- wake lock ------------------------------------------------------------ */
async function requestWakeLock() {
  try {
    if ('wakeLock' in navigator) {
      wakeLock = await navigator.wakeLock.request('screen');
    }
  } catch (_) { /* ignore — non-critical */ }
}
function releaseWakeLock() {
  if (wakeLock) { wakeLock.release(); wakeLock = null; }
}
document.addEventListener('visibilitychange', () => {
  if (document.visibilityState === 'visible' && satelliteRunning) requestWakeLock();
});

/* -- orb ------------------------------------------------------------------ */
function setOrb(s) {
  state = s;
  els.orb.className = 'orb' + (s !== 'offline' && s !== 'idle' ? ` ${s}` : '');
  const labels = {
    offline: 'OFFLINE', idle: 'SAY "HEY JARVIS"',
    listening: 'LISTENING', thinking: 'THINKING', speaking: 'SPEAKING',
  };
  els.orbLabel.textContent = labels[s] || s;
}

/* -- transcript ----------------------------------------------------------- */
function logUser(text) {
  const div = document.createElement('div');
  div.className = 'user';
  div.textContent = `YOU: ${text}`;
  els.transcript.appendChild(div);
  els.transcript.scrollTop = els.transcript.scrollHeight;
}
function logJarvis(text) {
  const div = document.createElement('div');
  div.className = 'jarvis';
  div.textContent = `JARVIS: ${text}`;
  els.transcript.appendChild(div);
  els.transcript.scrollTop = els.transcript.scrollHeight;
}
function logSys(text) {
  const div = document.createElement('div');
  div.style.cssText = 'font-size:11px;color:#555;font-family:monospace;margin-top:2px;';
  div.textContent = text;
  els.transcript.appendChild(div);
  els.transcript.scrollTop = els.transcript.scrollHeight;
}

/* -- device glance -------------------------------------------------------- */
async function refreshDevices() {
  try {
    const resp = await fetch('/api/devices');
    if (!resp.ok) return;
    const data = await resp.json();
    const entities = data.entities || [];

    // Lights: sum of "on" lights.
    const lights = entities.filter(e => e.domain === 'light');
    const lightsOn = lights.filter(e => e.state === 'on').length;
    const tileLights = els.tileLights;
    tileLights.className = 'tile' + (lights.length ? (lightsOn > 0 ? ' on' : ' off') : ' dead');
    tileLights.querySelector('.tile-val').textContent = lights.length ? (lightsOn > 0 ? `${lightsOn} ON` : 'ALL OFF') : '—';

    // Fan: first fan entity.
    const fans = entities.filter(e => e.domain === 'fan');
    const fan = fans[0];
    const tileFan = els.tileFan;
    tileFan.className = 'tile' + (fan ? (fan.state === 'on' ? ' on' : ' off') : ' dead');
    tileFan.querySelector('.tile-val').textContent = fan ? (fan.state === 'on' ? `SPEED ${fan.percentage || '?'}` : 'OFF') : '—';

    // AC: first climate entity.
    const acs = entities.filter(e => e.domain === 'climate');
    const ac = acs[0];
    const tileAc = els.tileAc;
    tileAc.className = 'tile' + (ac ? (ac.state === 'cool' || ac.state === 'heat' ? ' on' : ' off') : ' dead');
    tileAc.querySelector('.tile-val').textContent = ac ? `${ac.current_temperature ?? ac.state ?? '—'}` : '—';

    // PC: check via /api/status proxy or assume available if hass is ok.
    // The PC agent status is not in /api/devices; use /api/status as proxy.
    const tilePc = els.tilePc;
    // We don't have a direct PC endpoint from the PWA perspective,
    // so mark it as available if the server is up.
    tilePc.className = 'tile on';
    tilePc.querySelector('.tile-val').textContent = 'ONLINE';

  } catch (err) {
    logSys(`devices error: ${err.message}`);
  }
}

/* -- toast ---------------------------------------------------------------- */
function showWakeToast(msg) {
  els.wakeToast.textContent = `🎙 Wake word in "${msg.room}" — ${msg.name || 'hey_jarvis'}`;
  els.wakeToast.classList.add('show');
  setTimeout(() => els.wakeToast.classList.remove('show'), 3500);
}
function showToast(msg, type) {
  els.toast.textContent = msg;
  els.toast.className = 'toast' + (type === 'error' ? '' : ' info') + ' show';
  setTimeout(() => els.toast.classList.remove('show'), 3500);
}

/* -- tabs ----------------------------------------------------------------- */
function switchTab(tab) {
  const pttVisible = tab === 'ptt';
  const satVisible = tab === 'sat';
  $('#tab-ptt').className = pttVisible ? 'visible' : '';
  $('#tab-sat').className = satVisible ? 'visible' : '';
  $('#tabBtnPtt').className = pttVisible ? 'tab active' : 'tab';
  $('#tabBtnSat').className = satVisible ? 'tab active' : 'tab';
  $('#navPtt').className = pttVisible ? 'nav-btn active' : 'nav-btn';
  $('#navSat').className = satVisible ? 'nav-btn active' : 'nav-btn';
}

/* -- install prompt ------------------------------------------------------- */
let deferredPrompt = null;
window.addEventListener('beforeinstallprompt', (e) => {
  e.preventDefault();
  deferredPrompt = e;
  els.installBanner.style.display = 'block';
});
async function installPwa() {
  if (!deferredPrompt) return;
  deferredPrompt.prompt();
  const { outcome } = await deferredPrompt.userChoice;
  if (outcome === 'accepted') els.installBanner.style.display = 'none';
  deferredPrompt = null;
}

/* -- boot ----------------------------------------------------------------- */
(async function boot() {
  // Register service worker.
  if ('serviceWorker' in navigator) {
    try {
      await navigator.serviceWorker.register('/shell/pwa/sw.js', { scope: '/shell/pwa/' });
    } catch (_) { /* SW registration failure is non-fatal */ }
  }

  connect();
  refreshDevices();
  setInterval(refreshDevices, 30000);  // poll devices every 30s

  // Secure-context check.
  const ok = window.isSecureContext && navigator.mediaDevices && navigator.mediaDevices.getUserMedia;
  if (!ok) {
    showToast('Mic unavailable over HTTP. Use Tailscale Serve (https) or localhost.', 'error');
    els.pttBtn.disabled = true;
    els.pttBtn.textContent = 'NO MIC (HTTP)';
  }
})();
