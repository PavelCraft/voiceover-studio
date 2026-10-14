(() => {
  const editor = document.getElementById("segment-editor");
  if (!editor) return;

  const canvas = document.getElementById("waveform-canvas");
  const shell = document.getElementById("waveform-shell");
  const context = canvas.getContext("2d");
  const audio = document.getElementById("editor-audio");
  const timeInput = document.getElementById("editor-time");
  const playButton = document.getElementById("editor-play");
  const message = document.getElementById("editor-message");
  const loading = document.getElementById("waveform-loading");
  const durationLabel = document.getElementById("editor-selection-duration");
  let data = null;
  let peaks = [];
  let peaksPerSecond = 100;
  let cursorTime = 0;
  let visibleSeconds = 20;
  let state = null;
  let history = [];
  let historyIndex = -1;
  let dragging = false;
  let dragX = 0;
  let dragTime = 0;
  let animationFrame = null;
  let audioContext = null;
  let audioGain = null;
  let playbackEndTimer = null;

  const cloneState = value => JSON.parse(JSON.stringify(value));
  const clamp = (value, minimum, maximum) => Math.max(minimum, Math.min(maximum, value));

  function csrfToken() {
    return document.cookie.split(";").map(item => item.trim()).find(item => item.startsWith("csrftoken="))?.split("=")[1] || "";
  }

  function formatTime(value) {
    const totalMilliseconds = Math.round(Math.max(0, Number(value) || 0) * 1000);
    const hours = Math.floor(totalMilliseconds / 3600000);
    const minutes = Math.floor((totalMilliseconds % 3600000) / 60000);
    const seconds = Math.floor((totalMilliseconds % 60000) / 1000);
    const milliseconds = totalMilliseconds % 1000;
    const prefix = hours ? `${String(hours).padStart(2, "0")}:` : "";
    return `${prefix}${String(minutes).padStart(2, "0")}:${String(seconds).padStart(2, "0")}.${String(milliseconds).padStart(3, "0")}`;
  }

  function parseTime(value) {
    const clean = value.trim().replace(",", ".");
    const parts = clean.split(":");
    if (!parts.length || parts.some(part => !/^\d+(?:\.\d+)?$/.test(part))) return null;
    if (parts.length === 1) return Number(parts[0]);
    if (parts.length === 2) return Number(parts[0]) * 60 + Number(parts[1]);
    if (parts.length === 3) return Number(parts[0]) * 3600 + Number(parts[1]) * 60 + Number(parts[2]);
    return null;
  }

  function hasAppliedBounds() {
    return state?.appliedStart != null || state?.appliedEnd != null;
  }

  function lowerBound() {
    return hasAppliedBounds() ? (state.appliedStart ?? state.baseStart ?? 0) : 0;
  }

  function upperBound() {
    return hasAppliedBounds() ? (state.appliedEnd ?? state.baseEnd ?? data?.duration ?? 0) : (data?.duration ?? 0);
  }
  function committedStart() { return state.appliedStart ?? state.baseStart; }
  function committedEnd() { return state.appliedEnd ?? state.baseEnd; }

  function seek(value) {
    cursorTime = clamp(Number(value) || 0, lowerBound(), upperBound());
    timeInput.value = formatTime(cursorTime);
    draw();
  }

  function snapshot() {
    return {...cloneState(state), cursorTime};
  }

  function restore(value) {
    const restored = cloneState(value);
    cursorTime = restored.cursorTime;
    delete restored.cursorTime;
    state = restored;
    cursorTime = clamp(cursorTime, lowerBound(), upperBound());
    renderState();
  }

  function pushHistory() {
    history = history.slice(0, historyIndex + 1);
    history.push(snapshot());
    historyIndex = history.length - 1;
    renderState();
  }

  function undo() {
    if (historyIndex <= 0) return;
    historyIndex -= 1;
    restore(history[historyIndex]);
  }

  function redo() {
    if (historyIndex >= history.length - 1) return;
    historyIndex += 1;
    restore(history[historyIndex]);
  }

  function markerTime(kind) {
    return kind === "start" ? state.draftStart : state.draftEnd;
  }

  function drawMarker(time, color) {
    if (time == null) return;
    const view = viewRange();
    if (time < view.start || time > view.end) return;
    const x = (time - view.start) / (view.end - view.start) * canvas.clientWidth;
    context.save();
    context.strokeStyle = color;
    context.lineWidth = 3;
    context.beginPath();
    context.moveTo(x, 0);
    context.lineTo(x, canvas.clientHeight);
    context.stroke();
    context.restore();
  }

  function viewRange() {
    const width = Math.min(visibleSeconds, Math.max(0.01, data.duration));
    return {start: cursorTime - width / 2, end: cursorTime + width / 2};
  }

  function resizeCanvas() {
    const ratio = window.devicePixelRatio || 1;
    const width = Math.max(1, shell.clientWidth);
    const height = Math.max(1, shell.clientHeight);
    canvas.width = Math.round(width * ratio);
    canvas.height = Math.round(height * ratio);
    canvas.style.width = `${width}px`;
    canvas.style.height = `${height}px`;
    context.setTransform(ratio, 0, 0, ratio, 0, 0);
    draw();
  }

  function draw() {
    if (!data) return;
    const width = canvas.clientWidth;
    const height = canvas.clientHeight;
    context.clearRect(0, 0, width, height);
    context.fillStyle = "#f8fafc";
    context.fillRect(0, 0, width, height);
    const range = viewRange();
    const from = Math.max(range.start, lowerBound(), 0);
    const to = Math.min(range.end, upperBound(), data.duration);
    if (to > from && peaks.length) {
      context.fillStyle = "#3970d8";
      const center = height / 2;
      const secondsPerPixel = (range.end - range.start) / width;
      for (let x = 0; x < width; x += 1) {
        const startTime = range.start + x * secondsPerPixel;
        const endTime = startTime + secondsPerPixel;
        if (endTime < from || startTime > to) continue;
        const first = clamp(Math.floor(Math.max(startTime, from) * peaksPerSecond), 0, peaks.length - 1);
        const last = clamp(Math.ceil(Math.min(endTime, to) * peaksPerSecond), first + 1, peaks.length);
        let peak = 0;
        for (let index = first; index < last; index += 1) peak = Math.max(peak, peaks[index] || 0);
        const amplitude = Math.max(1, peak * (height * 0.44));
        context.fillRect(x, center - amplitude, 1, amplitude * 2);
      }
    }
    if (state) {
      if (state.appliedStart == null && state.startMarkerVisible) drawMarker(markerTime("start"), state.dirtyStart ? "#e11d48" : "#f59e0b");
      if (state.appliedEnd == null && state.endMarkerVisible) drawMarker(markerTime("end"), state.dirtyEnd ? "#e11d48" : "#f59e0b");
    }
    timeInput.value = formatTime(cursorTime);
  }

  function renderState() {
    document.getElementById("editor-undo").disabled = historyIndex <= 0;
    document.getElementById("editor-redo").disabled = historyIndex >= history.length - 1;
    const startButton = document.getElementById("editor-set-start");
    const endButton = document.getElementById("editor-set-end");
    startButton.classList.toggle("primary", state?.startSelected === true);
    startButton.classList.toggle("secondary", state?.startSelected !== true);
    endButton.classList.toggle("primary", state?.endSelected === true);
    endButton.classList.toggle("secondary", state?.endSelected !== true);
    const start = committedStart();
    const end = committedEnd();
    durationLabel.textContent = start != null && end != null ? `${formatTime(start)} — ${formatTime(end)} (${(end - start).toFixed(3)} s)` : "";
    message.classList.remove("processing-message");
    message.textContent = "";
    seek(cursorTime);
  }

  async function prepareAudioOutput() {
    if (!audioContext) {
      const AudioContextClass = window.AudioContext || window.webkitAudioContext;
      if (!AudioContextClass) return;
      audioContext = new AudioContextClass();
      const source = audioContext.createMediaElementSource(audio);
      audioGain = audioContext.createGain();
      source.connect(audioGain).connect(audioContext.destination);
    }
    if (audioContext.state === "suspended") await audioContext.resume();
    audioGain.gain.cancelScheduledValues(audioContext.currentTime);
    audioGain.gain.setValueAtTime(1, audioContext.currentTime);
  }

  function clearPlaybackEndTimer() {
    if (playbackEndTimer) window.clearTimeout(playbackEndTimer);
    playbackEndTimer = null;
  }

  function updatePausedControls() {
    playButton.textContent = "▶";
    playButton.setAttribute("aria-label", "Play");
    if (animationFrame) cancelAnimationFrame(animationFrame);
    animationFrame = null;
  }

  function pause(keepMuted = false) {
    audio.pause();
    clearPlaybackEndTimer();
    if (audioGain && !keepMuted) {
      audioGain.gain.cancelScheduledValues(audioContext.currentTime);
      audioGain.gain.setValueAtTime(1, audioContext.currentTime);
    }
    updatePausedControls();
  }

  function finishPlaybackAtBoundary() {
    audio.pause();
    clearPlaybackEndTimer();
    cursorTime = upperBound();
    timeInput.value = formatTime(cursorTime);
    draw();
    updatePausedControls();
  }

  function followPlayback() {
    cursorTime = Math.min(audio.currentTime, upperBound());
    if (audio.ended) {
      seek(upperBound());
      pause();
      return;
    }
    timeInput.value = formatTime(cursorTime);
    draw();
    animationFrame = requestAnimationFrame(followPlayback);
  }

  function waitForAudioEvent(eventName) {
    return new Promise((resolve, reject) => {
      const cleanup = () => {
        audio.removeEventListener(eventName, handleEvent);
        audio.removeEventListener("error", handleError);
      };
      const handleEvent = () => {
        cleanup();
        resolve();
      };
      const handleError = () => {
        cleanup();
        reject(new Error("Audio could not be loaded"));
      };
      audio.addEventListener(eventName, handleEvent, {once: true});
      audio.addEventListener("error", handleError, {once: true});
    });
  }

  async function togglePlayback() {
    if (!audio.paused) {
      pause();
      return;
    }
    const requestedTime = clamp(cursorTime, lowerBound(), upperBound());
    try {
      await prepareAudioOutput();
      if (audio.readyState < HTMLMediaElement.HAVE_METADATA) {
        await waitForAudioEvent("loadedmetadata");
      }
      if (Math.abs(audio.currentTime - requestedTime) > 0.005) {
        const seeked = waitForAudioEvent("seeked");
        audio.currentTime = requestedTime;
        await seeked;
      } else {
        audio.currentTime = requestedTime;
      }
      cursorTime = requestedTime;
      await audio.play();
      const remaining = Math.max(0, upperBound() - audio.currentTime);
      if (audioGain) {
        const stopAt = audioContext.currentTime + remaining;
        audioGain.gain.cancelScheduledValues(audioContext.currentTime);
        audioGain.gain.setValueAtTime(1, audioContext.currentTime);
        audioGain.gain.setValueAtTime(0, stopAt);
      }
      playbackEndTimer = window.setTimeout(
        finishPlaybackAtBoundary,
        Math.max(0, remaining * 1000),
      );
      playButton.textContent = "❚❚";
      playButton.setAttribute("aria-label", "Pause");
      animationFrame = requestAnimationFrame(followPlayback);
    } catch (error) {
      message.textContent = error.message;
    }
  }

  function setBoundary(kind) {
    const isStart = kind === "start";
    const selectedKey = isStart ? "startSelected" : "endSelected";
    const markerKey = isStart ? "startMarkerVisible" : "endMarkerVisible";
    const draftKey = isStart ? "draftStart" : "draftEnd";
    const dirtyKey = isStart ? "dirtyStart" : "dirtyEnd";
    const selectedAtCursor = state[selectedKey]
      && state[draftKey] != null
      && Math.abs(state[draftKey] - cursorTime) <= 0.005;
    if (selectedAtCursor) {
      state[selectedKey] = false;
      state[markerKey] = false;
      state[draftKey] = isStart ? committedStart() : committedEnd();
      state[dirtyKey] = false;
      pushHistory();
      return;
    }
    if (isStart) {
      state.draftStart = cursorTime;
      state.dirtyStart = true;
      state.startSelected = true;
      state.startMarkerVisible = true;
    } else {
      state.draftEnd = cursorTime;
      state.dirtyEnd = true;
      state.endSelected = true;
      state.endMarkerVisible = true;
    }
    pushHistory();
  }

  function applyBoundaries() {
    if (!state.dirtyStart && !state.dirtyEnd) return;
    const start = state.dirtyStart ? state.draftStart : committedStart();
    const end = state.dirtyEnd ? state.draftEnd : committedEnd();
    if (start != null && end != null && start >= end) {
      message.textContent = editor.dataset.invalidRange;
      return;
    }
    if (state.dirtyStart) state.appliedStart = state.draftStart;
    if (state.dirtyEnd) state.appliedEnd = state.draftEnd;
    state.dirtyStart = false;
    state.dirtyEnd = false;
    cursorTime = clamp(cursorTime, lowerBound(), upperBound());
    pushHistory();
  }

  function showProcessingMessage() {
    message.classList.add("processing-message");
    message.replaceChildren(
      document.createTextNode(editor.dataset.processingText),
      Object.assign(document.createElement("span"), {className: "processing-dots", ariaHidden: "true"}),
      document.createTextNode(` ${editor.dataset.processingWait}`),
    );
  }

  async function postBoundary(payload) {
    message.textContent = editor.dataset.loadingText;
    const response = await fetch(data.save_url, {
      method: "POST",
      headers: {"Content-Type": "application/json", "X-CSRFToken": csrfToken()},
      body: JSON.stringify(payload),
    });
    const result = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(result.error || editor.dataset.saveError);
    if (!result.processing) {
      redirectToSegment(result.redirect_url);
      return;
    }
    showProcessingMessage();
    await waitForProcessing(result.status_url);
    redirectToSegment(result.redirect_url);
  }

  function redirectToSegment(url) {
    const target = new URL(url, window.location.href);
    const position = data.position;
    target.hash = `segment-${position}`;
    closeEditor();
    window.location.replace(target.href);
  }

  async function waitForProcessing(statusUrl) {
    if (!statusUrl) throw new Error(editor.dataset.saveError);
    while (true) {
      const response = await fetch(statusUrl, {
        headers: {"X-Requested-With": "XMLHttpRequest"},
        cache: "no-store",
      });
      if (!response.ok) throw new Error(editor.dataset.saveError);
      const status = await response.json();
      if (status.failed) throw new Error(status.error || editor.dataset.saveError);
      if (status.complete) return;
      await new Promise(resolve => window.setTimeout(resolve, 350));
    }
  }

  async function save() {
    if (state.dirtyStart || state.dirtyEnd) {
      message.textContent = editor.dataset.applyFirst;
      return;
    }
    const start = committedStart();
    const end = committedEnd();
    if (start == null || end == null || start >= end) {
      message.textContent = editor.dataset.invalidRange;
      return;
    }
    disableActions(true);
    try {
      await postBoundary({audio_start: start, audio_end: end, skipped: false});
    } catch (error) {
      disableActions(false);
      message.textContent = error.message;
    }
  }

  async function skipPhrase() {
    if (!window.confirm(editor.dataset.skipConfirm)) return;
    disableActions(true);
    try {
      await postBoundary({skipped: true});
    } catch (error) {
      disableActions(false);
      message.textContent = error.message;
    }
  }

  function disableActions(disabled) {
    editor.querySelectorAll("button").forEach(button => { button.disabled = disabled; });
  }

  function closeEditor() {
    pause();
    audio.removeAttribute("src");
    audio.load();
    editor.hidden = true;
    document.body.classList.remove("editor-open");
    data = null;
  }

  async function openEditor(url) {
    editor.hidden = false;
    document.body.classList.add("editor-open");
    loading.hidden = false;
    message.textContent = "";
    disableActions(true);
    try {
      const response = await fetch(url, {headers: {"X-Requested-With": "XMLHttpRequest"}, cache: "no-store"});
      if (!response.ok) throw new Error(editor.dataset.saveError);
      data = await response.json();
      const peaksResponse = await fetch(data.peaks_url, {cache: "no-store"});
      if (!peaksResponse.ok) throw new Error(editor.dataset.saveError);
      const waveform = await peaksResponse.json();
      peaks = waveform.peaks;
      peaksPerSecond = waveform.peaks_per_second;
      data.duration = waveform.duration;
      state = {
        baseStart: data.audio_start,
        baseEnd: data.audio_end,
        draftStart: data.audio_start,
        draftEnd: data.audio_end,
        appliedStart: null,
        appliedEnd: null,
        dirtyStart: false,
        dirtyEnd: false,
        startSelected: Boolean(data.manual),
        endSelected: Boolean(data.manual),
        startMarkerVisible: true,
        endMarkerVisible: true,
      };
      cursorTime = clamp(data.focus || 0, lowerBound(), upperBound());
      visibleSeconds = Math.min(20, Math.max(1, data.duration));
      history = [snapshot()];
      historyIndex = 0;
      document.getElementById("editor-title").textContent = `#${data.position}`;
      document.getElementById("editor-phrase").textContent = data.text;
      document.getElementById("editor-previous").textContent = data.previous_text || "—";
      document.getElementById("editor-next").textContent = data.next_text || "—";
      audio.src = data.audio_url;
      audio.volume = Number(document.getElementById("editor-volume").value);
      loading.hidden = true;
      disableActions(false);
      renderState();
      resizeCanvas();
    } catch (error) {
      loading.textContent = error.message;
      message.textContent = error.message;
      document.getElementById("editor-close").disabled = false;
      document.getElementById("editor-cancel").disabled = false;
    }
  }

  document.querySelectorAll(".segment-edit-button").forEach(button => button.addEventListener("click", () => openEditor(button.dataset.editorUrl)));
  document.getElementById("editor-close").addEventListener("click", closeEditor);
  document.getElementById("editor-cancel").addEventListener("click", closeEditor);
  playButton.addEventListener("click", togglePlayback);
  document.getElementById("editor-set-start").addEventListener("click", () => setBoundary("start"));
  document.getElementById("editor-set-end").addEventListener("click", () => setBoundary("end"));
  document.getElementById("editor-apply").addEventListener("click", applyBoundaries);
  document.getElementById("editor-undo").addEventListener("click", undo);
  document.getElementById("editor-redo").addEventListener("click", redo);
  document.getElementById("editor-save").addEventListener("click", save);
  document.getElementById("editor-skip").addEventListener("click", skipPhrase);
  document.querySelectorAll("[data-nudge]").forEach(button => button.addEventListener("click", () => seek(cursorTime + Number(button.dataset.nudge))));
  document.getElementById("editor-zoom-in").addEventListener("click", () => { visibleSeconds = Math.max(0.25, visibleSeconds / 2); draw(); });
  document.getElementById("editor-zoom-out").addEventListener("click", () => { visibleSeconds = Math.min(data.duration, visibleSeconds * 2); draw(); });
  document.getElementById("editor-volume").addEventListener("input", event => { audio.volume = Number(event.target.value); audio.muted = false; document.getElementById("editor-mute").textContent = "🔊"; });
  document.getElementById("editor-mute").addEventListener("click", event => { audio.muted = !audio.muted; event.currentTarget.textContent = audio.muted ? "🔇" : "🔊"; });
  timeInput.addEventListener("change", () => { const parsed = parseTime(timeInput.value); if (parsed == null) timeInput.value = formatTime(cursorTime); else seek(parsed); });
  timeInput.addEventListener("blur", () => timeInput.dispatchEvent(new Event("change")));

  shell.addEventListener("pointerdown", event => {
    if (!data) return;
    dragging = true;
    dragX = event.clientX;
    dragTime = cursorTime;
    shell.setPointerCapture(event.pointerId);
    pause();
  });
  shell.addEventListener("pointermove", event => {
    if (!dragging) return;
    const pixelsPerSecond = shell.clientWidth / visibleSeconds;
    seek(dragTime - (event.clientX - dragX) / pixelsPerSecond);
  });
  shell.addEventListener("pointerup", event => { dragging = false; shell.releasePointerCapture(event.pointerId); });
  shell.addEventListener("pointercancel", () => { dragging = false; });

  document.addEventListener("keydown", event => {
    if (editor.hidden) return;
    if (event.key === "Escape") { closeEditor(); return; }
    if (["INPUT", "TEXTAREA"].includes(document.activeElement?.tagName)) return;
    if (event.key === "ArrowLeft") { event.preventDefault(); seek(cursorTime - 1); }
    if (event.key === "ArrowRight") { event.preventDefault(); seek(cursorTime + 1); }
    if (event.key === " ") { event.preventDefault(); togglePlayback(); }
  });
  new ResizeObserver(resizeCanvas).observe(shell);
})();
