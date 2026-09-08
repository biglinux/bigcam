"use strict";
const config = JSON.parse(document.getElementById("config").textContent);
const text = key => config.strings[key] || key;
for (const node of document.querySelectorAll("[data-i18n]")) node.textContent = text(node.dataset.i18n);
for (const node of document.querySelectorAll("[data-label]")) node.setAttribute("aria-label", text(node.dataset.label));
const $ = id => document.getElementById(id);
const query = new URLSearchParams(location.search);
query.set("client", crypto.randomUUID());
const suffix = "?" + query;
let stream, ws, wt, timer, audioContext, worklet, audioSource;
let generation = 0, busy = false, sending = false, audioSending = false, http = false, audioSequence = 0;
let quality = 0.75, sent = 0, since = performance.now();
const status = key => { $("status").textContent = text(key); };
function buttons(active) {
  $("start").hidden = active;
  $("stop").hidden = !active;
  $("switch").hidden = !active;
  $("start").disabled = busy;
}
function withTimeout(promise, milliseconds) {
  let timer;
  return Promise.race([promise, new Promise((_, reject) => { timer = setTimeout(() => reject(new Error("timeout")), milliseconds); })])
    .finally(() => clearTimeout(timer));
}
async function websocket() {
  const socket = new WebSocket("wss://" + location.host + "/ws" + suffix);
  socket.binaryType = "arraybuffer";
  try {
    await withTimeout(new Promise((resolve, reject) => {
      socket.onopen = resolve;
      socket.onerror = reject;
      socket.onclose = reject;
    }), 5000);
    socket.onclose = () => { if (socket === ws && stream) { stop(); status("error"); } };
    return socket;
  } catch (error) { socket.close(); throw error; }
}
async function send(packet, current) {
  if (current !== generation || !stream) return;
  if (wt) {
    const channel = await wt.createUnidirectionalStream();
    const writer = channel.getWriter();
    try { await writer.write(packet); await writer.close(); }
    catch (error) { await writer.abort().catch(() => {}); throw error; }
    finally { writer.releaseLock(); }
  } else if (ws && ws.readyState === WebSocket.OPEN) {
    if (ws.bufferedAmount < 131072) ws.send(packet);
  } else if (http) {
    const response = await fetch("/frame" + suffix, {method: "POST", body: packet, signal: AbortSignal.timeout(4000)});
    if (!response.ok) throw new Error("HTTP " + response.status);
  } else throw new Error("transport closed");
}
async function start() {
  if (busy || stream) return;
  busy = true;
  const current = ++generation;
  buttons(false); status("connecting");
  try {
    const auth = await fetch("/status" + suffix, {signal: AbortSignal.timeout(4000)});
    if (auth.status === 401 || auth.status === 403) { status("authentication"); throw new Error("authentication"); }
    if (!auth.ok || (await auth.json()).busy) throw new Error("busy");
    const height = Number($("resolution").value);
    const video = {facingMode: {ideal: $("facing").value}};
    if (height) { video.height = {ideal: height}; video.width = {ideal: Math.round(height * 16 / 9)}; }
    const acquired = await navigator.mediaDevices.getUserMedia({video, audio: $("microphone").checked});
    if (current !== generation) { acquired.getTracks().forEach(track => track.stop()); return; }
    stream = acquired; $("video").srcObject = stream;
    await $("video").play();
    http = false;
    if (config.quic && typeof WebTransport !== "undefined") {
      const hash = Uint8Array.from(atob(config.certHash), character => character.charCodeAt(0));
      const transport = new WebTransport("https://" + location.host + "/camera" + suffix,
        {serverCertificateHashes: [{algorithm: "sha-256", value: hash.buffer}]});
      try { await withTimeout(transport.ready, 3500); wt = transport; }
      catch (_) { transport.close(); }
    }
    if (!wt) {
      try { ws = await websocket(); }
      catch (_) {
        // Recheck authentication. A rejected token must never trigger fallback media.
        const check = await fetch("/status" + suffix, {signal: AbortSignal.timeout(4000)});
        if (!check.ok) throw new Error("authentication");
        http = true;
      }
    }
    if (current !== generation) return;
    quality = Number($("quality").value);
    sending = audioSending = false; audioSequence = 0;
    if ($("microphone").checked) await audio(current);
    status("connected"); buttons(true);
    timer = setInterval(() => capture(current), 1000 / Number($("fps").value));
  } catch (error) {
    await stop(); status(error.message === "authentication" ? "authentication" : "error");
  } finally { busy = false; $("start").disabled = false; }
}
async function capture(current) {
  if (current !== generation || sending || !stream || !$("video").videoWidth) return;
  if (ws && ws.bufferedAmount > 131072) return;
  sending = true;
  try {
    const canvas = $("canvas"), video = $("video");
    canvas.width = video.videoWidth; canvas.height = video.videoHeight;
    canvas.getContext("2d").drawImage(video, 0, 0);
    const base = Number($("quality").value);
    quality = ws && ws.bufferedAmount > 65536 ? Math.max(.3, quality - .05) : Math.min(base, quality + .02);
    const blob = await new Promise(resolve => canvas.toBlob(resolve, "image/jpeg", quality));
    if (blob && current === generation) {
      await withTimeout(send(new Uint8Array(await blob.arrayBuffer()), current), 5000);
      ++sent;
      if (performance.now() - since >= 1000) {
        $("stats").textContent = `${canvas.width} × ${canvas.height} · ${Math.round(sent * 1000 / (performance.now() - since))} fps · ${wt ? "QUIC" : http ? "HTTPS" : "WS"}`;
        sent = 0; since = performance.now();
      }
    }
  } catch (_) { if (current === generation) { await stop(); status("error"); } }
  finally { sending = false; }
}
async function audio(current) {
  audioContext = new AudioContext();
  await audioContext.audioWorklet.addModule("/audio-worklet.js" + suffix);
  if (current !== generation) return;
  audioSource = audioContext.createMediaStreamSource(stream);
  worklet = new AudioWorkletNode(audioContext, "bigcam-pcm");
  const silence = audioContext.createGain(); silence.gain.value = 0;
  audioSource.connect(worklet).connect(silence).connect(audioContext.destination);
  worklet.port.onmessage = async event => {
    if (current !== generation || audioSending) return;
    audioSending = true;
    const packet = new Uint8Array(645);
    packet[0] = 1; new DataView(packet.buffer).setUint32(1, audioSequence++);
    packet.set(new Uint8Array(event.data), 5);
    try { await withTimeout(send(packet, current), 2000); }
    catch (_) { if (current === generation) status("audioError"); }
    finally { audioSending = false; }
  };
  await audioContext.resume();
}
async function stop() {
  ++generation;
  if (timer) clearInterval(timer);
  timer = null;
  if (worklet) { worklet.port.onmessage = null; worklet.disconnect(); worklet = null; }
  if (audioSource) { audioSource.disconnect(); audioSource = null; }
  if (audioContext) { const context = audioContext; audioContext = null; await context.close().catch(() => {}); }
  if (wt) { wt.close(); wt = null; }
  if (ws) { const socket = ws; ws = null; socket.close(); }
  if (http) { fetch("/disconnect" + suffix, {method:"POST", keepalive:true}).catch(() => {}); http = false; }
  if (stream) { stream.getTracks().forEach(track => track.stop()); stream = null; }
  $("video").srcObject = null; $("stats").textContent = "";
  buttons(false); status("disconnected");
}
$("start").addEventListener("click", start);
$("stop").addEventListener("click", stop);
$("switch").addEventListener("click", async () => {
  $("facing").value = $("facing").value === "user" ? "environment" : "user";
  await stop(); await start();
});
for (const id of ["resolution", "facing", "fps", "microphone"]) {
  $(id).addEventListener("change", async () => { if (stream) { await stop(); await start(); } });
}
window.addEventListener("pagehide", () => { stop(); });
