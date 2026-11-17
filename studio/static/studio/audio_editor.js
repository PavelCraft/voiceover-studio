(() => {
  const editor = document.getElementById("audio-editor");
  if (!editor) return;
  const canvas = document.getElementById("audio-waveform-canvas");
  const shell = document.getElementById("audio-waveform-shell");
  const context = canvas.getContext("2d");
  const trackData = JSON.parse(document.getElementById("audio-editor-tracks")?.textContent || "[]");
  const savedState = JSON.parse(document.getElementById("audio-editor-state")?.textContent || "{}");
  const savedNumber = value => value == null || value === ""
    ? null
    : (Number.isFinite(Number(value)) ? Number(value) : null);
  let tracks = trackData.map(item => ({
    ...item,
    input_id: Number(item.input_id),
    duration: Number(item.duration) || 0,
    audio: document.querySelector(`#audio-editor-players audio[data-track-id="${Number(item.input_id)}"]`),
    peaks: [],
    peaksPerSecond: 100,
    offset: 0,
  }));
  const trackIds = tracks.map(item => item.input_id);
  const savedOrder = Array.isArray(savedState.trackOrder) ? savedState.trackOrder.map(Number) : [];
  let trackOrder = savedOrder.length === trackIds.length && new Set(savedOrder).size === trackIds.length
    && savedOrder.every(id => trackIds.includes(id)) ? savedOrder : trackIds;
  const savedGroups = Array.isArray(savedState.trackGroups)
    ? savedState.trackGroups.map(group => Array.isArray(group) ? group.map(Number) : []) : [];
  const savedGroupOrder = savedGroups.flat();
  let trackGroups = savedGroupOrder.length === trackOrder.length && new Set(savedGroupOrder).size === trackOrder.length
    && savedGroupOrder.every((id, index) => id === trackOrder[index])
    ? savedGroups : (savedState.mergeApplied && trackOrder.length > 1 ? [[...trackOrder]] : trackOrder.map(id => [id]));
  let audio = tracks[0]?.audio || null, currentTrackIndex = 0, playbackActive = false, playbackStarting = false, playbackToken = 0;
  let mergeApplied = typeof savedState.mergeApplied === "boolean" ? savedState.mergeApplied : tracks.length <= 1;
  let trailingSilence = savedNumber(savedState.trailingSilence) ?? 0;
  let canUndoTrackAddition = editor.dataset.canUndoAddition === "true";
  let draggedTrackId = null, addingTrack = false, removingTrack = false;
  const loading = document.getElementById("audio-waveform-loading");
  const playhead = document.getElementById("audio-playhead");
  const selectionBox = document.getElementById("audio-selection");
  const startMarker = document.getElementById("audio-selection-start");
  const endMarker = document.getElementById("audio-selection-end");
  const message = document.getElementById("audio-editor-message");
  const operationList = document.getElementById("audio-operation-list");
  const emptyOperations = document.getElementById("audio-operation-empty");
  let peaks = [], peaksPerSecond = 100, duration = 0, cursor = savedNumber(savedState.cursor) ?? 0;
  let selectionStart = savedNumber(savedState.selectionStart), selectionEnd = savedNumber(savedState.selectionEnd);
  let targetDuration = savedNumber(savedState.targetDuration);
  let operations = Array.isArray(savedState.operations) ? savedState.operations : [];
  let animationFrame = null, playbackStopTimer = null, playbackBoundaryTimer = null;
  let history = [], historyIndex = -1, draggingHandle = null, waveformDrag = null;
  let viewStart = 0, viewDuration = 0, resumePlayback = false;
  let playbackEndCursor = null;
  let cutSkipTarget = null;
  let cutSkipInProgress = false;
  let virtualSilence = null;
  let draftOperations = null, selectionRate = savedNumber(savedState.selectionRate) ?? 1;
  let debugSequence = 0;
  let draftSaveTimer = null;
  const MIN_MEDIA_PLAYBACK_RATE = .0625;
  const MAX_MEDIA_PLAYBACK_RATE = 16;

  const clamp = (value, min, max) => Math.min(max, Math.max(min, value));
  const csrfToken = () => document.cookie.split("; ").find(row => row.startsWith("csrftoken="))?.split("=")[1] || "";
  const formatTime = value => {
    const total = Math.max(0, Number(value) || 0);
    const hours = Math.floor(total / 3600);
    const minutes = Math.floor(total % 3600 / 60);
    const seconds = Math.floor(total % 60);
    const milliseconds = Math.floor((total - Math.floor(total)) * 1000);
    return `${hours ? `${String(hours).padStart(2, "0")}:` : ""}${String(minutes).padStart(2, "0")}:${String(seconds).padStart(2, "0")}.${String(milliseconds).padStart(3, "0")}`;
  };
  const timelineOperations = (includeDraft = true) => [
    ...(includeDraft && draftOperations ? draftOperations : operations),
  ].sort((a, b) => a.start - b.start);
  const sourceEditedDuration = (includeDraft = true) => Math.max(0, timelineOperations(includeDraft).reduce((total, operation) => {
    const sourceLength = operation.end - operation.start;
    if (operation.type === "cut") return total - sourceLength;
    if (operation.type === "silence") return total - sourceLength + operation.duration;
    return total - sourceLength + sourceLength / operation.speed;
  }, duration));
  const editedDuration = (includeDraft = true) => sourceEditedDuration(includeDraft);
  const sourceToEdited = (value, includeDraft = true, insertionBoundary = "left") => {
    const source = clamp(Number(value) || 0, 0, duration);
    let sourceCursor = 0, editedCursor = 0;
    for (const operation of timelineOperations(includeDraft)) {
      const insertedSilence = operation.type === "silence" && operation.end - operation.start < .001;
      if (insertedSilence) {
        if (source < operation.start - .0005) return editedCursor + source - sourceCursor;
        editedCursor += operation.start - sourceCursor;
        if (Math.abs(source - operation.start) <= .0005) {
          return editedCursor + (insertionBoundary === "right" ? operation.duration : 0);
        }
        editedCursor += operation.duration;
        sourceCursor = operation.start;
        continue;
      }
      if (source < operation.start) return editedCursor + source - sourceCursor;
      editedCursor += operation.start - sourceCursor;
      if (source <= operation.end) {
        if (operation.type === "cut") return editedCursor;
        if (operation.type === "silence") {
          return editedCursor + (source - operation.start) / (operation.end - operation.start) * operation.duration;
        }
        return editedCursor + (source - operation.start) / operation.speed;
      }
      if (operation.type === "speed") editedCursor += (operation.end - operation.start) / operation.speed;
      if (operation.type === "silence") editedCursor += operation.duration;
      sourceCursor = operation.end;
    }
    return editedCursor + source - sourceCursor;
  };
  const editedToSource = (value, boundary = "right", includeDraft = true) => {
    const edited = clamp(Number(value) || 0, 0, editedDuration(includeDraft));
    let sourceCursor = 0, editedCursor = 0;
    for (const operation of timelineOperations(includeDraft)) {
      const keptDuration = operation.start - sourceCursor;
      const keptEnd = editedCursor + keptDuration;
      if (edited < keptEnd - .0005) return sourceCursor + edited - editedCursor;
      if (Math.abs(edited - keptEnd) <= .0005 && operation.type === "cut") return boundary === "left" ? operation.start : operation.end;
      editedCursor = keptEnd;
      if (operation.type === "speed" || operation.type === "silence") {
        const transformedDuration = operation.type === "silence"
          ? operation.duration
          : (operation.end - operation.start) / operation.speed;
        if (edited <= editedCursor + transformedDuration + .0005) {
          const sourcePerEditedSecond = (operation.end - operation.start) / transformedDuration;
          return clamp(operation.start + (edited - editedCursor) * sourcePerEditedSecond, operation.start, operation.end);
        }
        editedCursor += transformedDuration;
      }
      sourceCursor = operation.end;
    }
    return clamp(sourceCursor + edited - editedCursor, 0, duration);
  };
  const speedOperationAtSource = source => timelineOperations(false).find(operation => (
    operation.type === "speed" && source >= operation.start - .0005 && source < operation.end - .0005
  ));
  const silenceOperationAtSource = source => timelineOperations(false).find(operation => (
    operation.type === "silence" && source >= operation.start - .0005 && source < operation.end - .0005
  ));
  const silenceEditedBounds = operation => {
    const start = sourceToEdited(operation.start, false, "left");
    return {start, end: start + operation.duration};
  };
  const silenceOperationAtEdited = edited => timelineOperations(false).find(operation => {
    if (operation.type !== "silence") return false;
    const bounds = silenceEditedBounds(operation);
    return edited >= bounds.start - .0005 && edited < bounds.end - .0005;
  });
  const playbackRateAtSource = source => speedOperationAtSource(source)?.speed || 1;
  const mediaPlaybackRate = speed => clamp(speed, MIN_MEDIA_PLAYBACK_RATE, MAX_MEDIA_PLAYBACK_RATE);
  const nextPlaybackBoundary = source => {
    for (const operation of timelineOperations(false)) {
      if (!['speed', 'silence'].includes(operation.type)) continue;
      if (operation.start > source + .0005) return operation.start;
      // The media element itself finishes the final source interval. Seeking to
      // its end from a timer can fire a few milliseconds early and clip speech.
      if (operation.end > source + .0005 && operation.end < duration - .0005) return operation.end;
    }
    return null;
  };
  const sourceSelectionDuration = () => selectionStart == null || selectionEnd == null ? null : selectionEnd - selectionStart;
  const selectionSpeed = () => selectionRate;
  const editorState = () => ({cursor, selectionStart, selectionEnd, targetDuration, selectionRate, operations, trackOrder, trackGroups, mergeApplied, trailingSilence: 0});
  const snapshot = () => JSON.parse(JSON.stringify(editorState()));
  const persistEditorState = () => fetch(editor.dataset.draftUrl, {
    method: "POST",
    headers: {"Content-Type": "application/json", "X-CSRFToken": csrfToken()},
    body: JSON.stringify(editorState()),
    keepalive: true,
  }).then(async response => {
    if (response.ok) return;
    const result = await response.json().catch(() => ({}));
    throw new Error(result.error || editor.dataset.errorSave);
  });
  const scheduleDraftSave = () => {
    if (draftSaveTimer) clearTimeout(draftSaveTimer);
    draftSaveTimer = setTimeout(() => {
      draftSaveTimer = null;
      persistEditorState().catch(error => setMessage(error.message));
    }, 150);
  };
  const restore = state => {
    const restored = JSON.parse(JSON.stringify(state));
    ({cursor, selectionStart, selectionEnd, targetDuration, selectionRate, operations, trackOrder, trackGroups, mergeApplied} = restored);
    trailingSilence = savedNumber(restored.trailingSilence) ?? 0;
    draftOperations = null;
    pause(); rebuildTrackTimeline(); render(); scheduleDraftSave();
  };
  const pushHistory = () => {
    history = history.slice(0, historyIndex + 1);
    history.push(snapshot());
    if (history.length > 200) history.shift();
    historyIndex = history.length - 1;
    render(); scheduleDraftSave();
  };
  const setMessage = text => { message.textContent = text || ""; };
  const virtualSilenceCursor = () => {
    if (!virtualSilence) return null;
    const progress = clamp((performance.now() - virtualSilence.startedAt) / virtualSilence.durationMs, 0, 1);
    return virtualSilence.editedStart + (virtualSilence.editedEnd - virtualSilence.editedStart) * progress;
  };
  const debugState = () => ({
    sequence: ++debugSequence,
    client_time: new Date().toISOString(),
    cursor,
    duration,
    edited_duration: editedDuration(),
    selection_start: selectionStart,
    selection_end: selectionEnd,
    playback_active: playbackActive,
    playback_starting: playbackStarting,
    playback_token: playbackToken,
    current_track_index: currentTrackIndex,
    source_time: audio ? currentSourceTime() : null,
    audio_time: audio?.currentTime ?? null,
    audio_duration: Number.isFinite(audio?.duration) ? audio.duration : null,
    paused: audio?.paused ?? null,
    ended: audio?.ended ?? null,
    seeking: audio?.seeking ?? null,
    ready_state: audio?.readyState ?? null,
    network_state: audio?.networkState ?? null,
    playback_rate: audio?.playbackRate ?? null,
    operations: operations.map(item => ({...item})),
  });
  const debugLog = (event, details = {}, {clear = false} = {}) => {
    const payload = JSON.stringify({event, details, state: debugState(), clear});
    fetch(editor.dataset.debugUrl, {
      method: "POST",
      headers: {"Content-Type": "application/json", "X-CSRFToken": csrfToken()},
      body: payload,
      keepalive: true,
    }).catch(() => {});
  };

  function normalizeOperations(items) {
    const normalized = [];
    [...items]
      .filter(item => (item.end - item.start >= .001 || item.type === "silence" && Math.abs(item.end - item.start) < .001) && (
        item.type === "cut" || item.type === "silence" && item.duration >= .001 || Math.abs(item.speed - 1) >= .001
      ))
      .sort((left, right) => left.start - right.start)
      .forEach(item => {
        const next = {...item};
        const previous = normalized[normalized.length - 1];
        const sameSpeed = next.type === "speed" && previous?.type === "speed" && Math.abs(next.speed - previous.speed) < .001;
        const adjacentSilence = next.type === "silence" && previous?.type === "silence";
        if (previous && previous.type === next.type && (next.type === "cut" || sameSpeed || adjacentSilence) && next.start <= previous.end + .001) {
          previous.end = Math.max(previous.end, next.end);
          if (adjacentSilence) previous.duration += next.duration;
        } else {
          normalized.push(next);
        }
      });
    return normalized;
  }

  function transformSourceRange(baseOperations, sourceStart, sourceEnd, transform) {
    const insertedSilences = baseOperations.filter(operation => (
      operation.type === "silence" && operation.end - operation.start < .001
    ));
    const rangedOperations = baseOperations.filter(operation => !insertedSilences.includes(operation));
    const boundaries = new Set([0, duration, sourceStart, sourceEnd]);
    rangedOperations.forEach(operation => { boundaries.add(operation.start); boundaries.add(operation.end); });
    const points = [...boundaries].filter(value => value >= 0 && value <= duration).sort((a, b) => a - b);
    const result = [];
    for (let index = 0; index < points.length - 1; index += 1) {
      const start = points[index], end = points[index + 1];
      if (end - start < .001) continue;
      const middle = (start + end) / 2;
      const original = rangedOperations.find(operation => middle > operation.start - .0005 && middle < operation.end + .0005);
      const existing = original?.type === "silence"
        ? {...original, duration: original.duration * (end - start) / (original.end - original.start)}
        : original;
      const inside = start >= sourceStart - .0005 && end <= sourceEnd + .0005;
      const changed = transform(existing, inside, start, end);
      if (changed) result.push({...changed, start, end});
    }
    return normalizeOperations([...result, ...insertedSilences]);
  }

  const speedForOperation = operation => operation?.type === "speed" ? operation.speed : 1;
  const speedOperationsForRange = (baseOperations, sourceStart, sourceEnd, relativeRate) => transformSourceRange(
    baseOperations, sourceStart, sourceEnd,
    (existing, inside) => {
      if (existing?.type === "cut") return {type: "cut"};
      const speed = speedForOperation(existing) * (inside ? relativeRate : 1);
      return Math.abs(speed - 1) < .001 ? null : {type: "speed", speed};
    },
  );
  const silenceOperationsForRange = (baseOperations, sourceStart, sourceEnd, outputDuration) => transformSourceRange(
    baseOperations, sourceStart, sourceEnd,
    (existing, inside, start, end) => {
      if (!inside) return existing;
      const portion = (end - start) / Math.max(.001, sourceEnd - sourceStart);
      return {type: "silence", duration: outputDuration * portion};
    },
  );
  const cutOperationsForRange = (baseOperations, sourceStart, sourceEnd) => transformSourceRange(
    baseOperations, sourceStart, sourceEnd,
    (existing, inside) => inside ? {type: "cut"} : existing,
  );
  const removeInsertedSilenceFromEditedRange = (baseOperations, editedStart, editedEnd) => normalizeOperations(
    baseOperations.flatMap(operation => {
      if (operation.type !== "silence" || operation.end - operation.start >= .001) return [operation];
      const bounds = silenceEditedBounds(operation);
      const overlap = Math.max(0, Math.min(editedEnd, bounds.end) - Math.max(editedStart, bounds.start));
      const remaining = operation.duration - overlap;
      return remaining >= .001 ? [{...operation, duration: remaining}] : [];
    }),
  );
  const sourceRangeIsSilent = (sourceStart, sourceEnd) => {
    const first = clamp(Math.floor(sourceStart * peaksPerSecond), 0, peaks.length);
    const last = clamp(Math.ceil(sourceEnd * peaksPerSecond), first + 1, peaks.length);
    let maximum = 0;
    for (let index = first; index < last; index += 1) maximum = Math.max(maximum, peaks[index] || 0);
    return maximum <= .003;
  };
  const orderedTracks = () => trackOrder.map(id => tracks.find(track => track.input_id === id)).filter(Boolean);
  const editingLocked = () => !mergeApplied;
  const requireEditable = () => {
    if (!editingLocked()) return true;
    setMessage(editor.dataset.errorMergeFirst);
    return false;
  };
  function trackLayout(order) {
    let offset = 0;
    const layout = new Map();
    order.forEach(id => {
      const track = tracks.find(item => item.input_id === id);
      if (!track) return;
      layout.set(id, {track, offset, end: offset + track.duration});
      offset += track.duration;
    });
    return layout;
  }
  function sourcePointInOrder(value, oldOrder, newOrder, boundary = "right") {
    const source = clamp(Number(value) || 0, 0, duration);
    const oldLayout = trackLayout(oldOrder);
    const newLayout = trackLayout(newOrder);
    let selected = null;
    oldOrder.some((id, index) => {
      const item = oldLayout.get(id);
      if (!item) return false;
      const atEnd = Math.abs(source - item.end) <= .0005;
      if (source < item.end - .0005 || atEnd && (boundary === "left" || index === oldOrder.length - 1)) {
        selected = {id, local: clamp(source - item.offset, 0, item.track.duration)};
        return true;
      }
      return false;
    });
    if (!selected) {
      const lastId = oldOrder[oldOrder.length - 1];
      const item = oldLayout.get(lastId);
      selected = {id: lastId, local: item?.track.duration || 0};
    }
    const destination = newLayout.get(selected.id);
    return destination ? destination.offset + selected.local : 0;
  }
  function operationsInOrder(baseOperations, oldOrder, newOrder) {
    const oldLayout = trackLayout(oldOrder);
    const newLayout = trackLayout(newOrder);
    const moved = [];
    baseOperations.forEach(operation => {
      if (operation.type === "silence" && operation.end - operation.start < .001) {
        if (operation.start <= .0005) {
          moved.push({...operation, start: 0, end: 0});
          return;
        }
        const sourceTrack = [...oldOrder].reverse().find(id => {
          const item = oldLayout.get(id);
          return item && operation.start >= item.offset - .0005 && operation.start >= item.end - .0005;
        });
        const destination = newLayout.get(sourceTrack);
        const original = oldLayout.get(sourceTrack);
        if (destination && original) {
          const position = destination.offset + clamp(operation.start - original.offset, 0, original.track.duration);
          moved.push({...operation, start: position, end: position});
        }
        return;
      }
      oldOrder.forEach(id => {
        const oldItem = oldLayout.get(id), newItem = newLayout.get(id);
        if (!oldItem || !newItem) return;
        const start = Math.max(operation.start, oldItem.offset);
        const end = Math.min(operation.end, oldItem.end);
        if (end - start < .001) return;
        moved.push({
          ...operation,
          start: newItem.offset + start - oldItem.offset,
          end: newItem.offset + end - oldItem.offset,
        });
      });
    });
    return normalizeOperations(moved);
  }
  function reorderTracks(nextOrder, nextGroups = trackGroups) {
    const oldOrder = [...trackOrder];
    const cursorSource = editedToSource(cursor, "right", false);
    const startSource = selectionStart == null ? null : editedToSource(selectionStart, "right", false);
    const endSource = selectionEnd == null ? null : editedToSource(selectionEnd, "left", false);
    pause();
    operations = operationsInOrder(operations, oldOrder, nextOrder);
    trackOrder = nextOrder;
    trackGroups = nextGroups;
    rebuildTrackTimeline();
    cursor = sourceToEdited(sourcePointInOrder(cursorSource, oldOrder, nextOrder, "right"), false);
    selectionStart = startSource == null ? null : sourceToEdited(sourcePointInOrder(startSource, oldOrder, nextOrder, "right"), false);
    selectionEnd = endSource == null ? null : sourceToEdited(sourcePointInOrder(endSource, oldOrder, nextOrder, "left"), false);
    targetDuration = selectionStart != null && selectionEnd != null ? Math.max(0, selectionEnd - selectionStart) : null;
    selectionRate = 1;
    pushHistory();
  }
  const currentSourceTime = () => (orderedTracks()[currentTrackIndex]?.offset || 0) + (audio?.currentTime || 0);
  const trackAtSource = value => {
    const ordered = orderedTracks();
    const source = clamp(Number(value) || 0, 0, duration);
    let chosen = ordered[ordered.length - 1] || null;
    for (const track of ordered) {
      if (source < track.offset + track.duration - .0005) { chosen = track; break; }
    }
    return chosen;
  };
  const setSourcePosition = value => {
    const track = trackAtSource(value);
    if (!track) return null;
    const ordered = orderedTracks();
    const nextIndex = ordered.indexOf(track);
    if (audio && audio !== track.audio) audio.pause();
    currentTrackIndex = nextIndex;
    audio = track.audio;
    const localTime = clamp((Number(value) || 0) - track.offset, 0, track.duration);
    audio.currentTime = localTime;
    return {audio, localTime};
  };

  function seekAudio(element, time) {
    if (!element) return Promise.resolve();
    const target = Math.max(0, Number(time) || 0);
    element.pause();
    return new Promise(resolve => {
      let settled = false;
      let check = null, timeout = null;
      const finish = (force = false) => {
        if (settled) return;
        if (!force && (element.seeking || Math.abs(element.currentTime - target) > .02)) return;
        settled = true;
        element.removeEventListener("seeked", onSeeked);
        if (check) clearInterval(check);
        if (timeout) clearTimeout(timeout);
        resolve();
      };
      const onSeeked = () => finish();
      element.addEventListener("seeked", onSeeked);
      element.currentTime = target;
      check = setInterval(() => finish(), 20);
      timeout = setTimeout(() => finish(true), 1500);
      setTimeout(() => finish(), 0);
    });
  }

  function rebuildTrackTimeline() {
    let offset = 0;
    peaks = [];
    const ordered = orderedTracks();
    ordered.forEach(track => {
      track.offset = offset;
      offset += track.duration;
      peaks.push(...track.peaks);
    });
    duration = offset;
    peaksPerSecond = ordered[0]?.peaksPerSecond || 100;
    currentTrackIndex = clamp(currentTrackIndex, 0, Math.max(0, ordered.length - 1));
    audio = ordered[currentTrackIndex]?.audio || null;
    cursor = clamp(cursor, 0, editedDuration());
  }
  const displayDuration = () => Math.max(editedDuration(), selectionStart != null && targetDuration ? selectionStart + targetDuration : 0);
  const timelineDuration = () => Math.max(.001, displayDuration());
  const normalizeView = () => {
    const total = timelineDuration();
    viewDuration = clamp(viewDuration || total, Math.min(.25, total), total);
    viewStart = clamp(viewStart, -viewDuration / 2, Math.max(0, total - viewDuration / 2));
  };
  const xForTime = value => (value - viewStart) / Math.max(.001, viewDuration) * shell.clientWidth;
  const timeForX = value => clamp(viewStart + value / Math.max(1, shell.clientWidth) * viewDuration, 0, editedDuration());

  function niceGridInterval() {
    const secondsPerLabel = viewDuration / Math.max(2, shell.clientWidth / 100);
    return [0.1, .25, .5, 1, 2, 5, 10, 15, 30, 60, 120, 300, 600, 1800].find(value => value >= secondsPerLabel) || 3600;
  }

  function formatRulerTime(value, interval) {
    const label = formatTime(value);
    return interval >= 1
      ? label.replace(/\.\d{3}$/, "")
      : label.replace(/0+$/, "").replace(/\.$/, "");
  }

  function draw() {
    if (!duration) return;
    normalizeView();
    const width = canvas.clientWidth, height = canvas.clientHeight;
    context.clearRect(0, 0, width, height);
    context.fillStyle = "#f7f9fb"; context.fillRect(0, 0, width, height);
    const rulerHeight = 30, center = rulerHeight + (height - rulerHeight) / 2;
    const interval = niceGridInterval();
    context.font = "12px ui-monospace, monospace";
    context.textAlign = "left";
    const viewEnd = viewStart + viewDuration;
    for (let time = Math.floor(viewStart / interval) * interval; time <= viewEnd + .001; time += interval) {
      if (time < 0 || time > timelineDuration()) continue;
      const x = xForTime(time);
      context.strokeStyle = "rgba(35,52,67,.14)"; context.lineWidth = 1;
      context.beginPath(); context.moveTo(x, rulerHeight); context.lineTo(x, height); context.stroke();
      context.fillStyle = "#627080"; context.fillText(formatRulerTime(time, interval), x + 4, 19);
    }
    const secondsPerPixel = viewDuration / Math.max(1, width);
    context.fillStyle = "#3970d8";
    for (let x = 0; x < width; x += 1) {
      const from = viewStart + x * secondsPerPixel, to = Math.min(editedDuration(), from + secondsPerPixel);
      if (from < 0 || from >= editedDuration()) continue;
      let peak = 0;
      if (!silenceOperationAtEdited((from + to) / 2)) {
        const sourceFrom = editedToSource(from, "right"), sourceTo = editedToSource(to, "left");
        const first = clamp(Math.floor(sourceFrom * peaksPerSecond), 0, peaks.length - 1);
        const last = clamp(Math.ceil(Math.max(sourceFrom, sourceTo) * peaksPerSecond), first + 1, peaks.length);
        for (let index = first; index < last; index += 1) peak = Math.max(peak, peaks[index] || 0);
      }
      const amplitude = Math.max(1, peak * ((height - rulerHeight) * .44));
      context.fillRect(x, center - amplitude, 1, amplitude * 2);
    }
    if (!mergeApplied && tracks.length > 1) {
      trackGroups.forEach((group, index) => {
        const groupTracks = group.map(id => tracks.find(track => track.input_id === id)).filter(Boolean);
        if (!groupTracks.length) return;
        const firstTrack = groupTracks[0], lastTrack = groupTracks[groupTracks.length - 1];
        const start = sourceToEdited(firstTrack.offset);
        const end = sourceToEdited(lastTrack.offset + lastTrack.duration);
        const left = xForTime(start), right = xForTime(end);
        if (index % 2) {
          context.fillStyle = "rgba(145,130,247,.055)";
          context.fillRect(left, rulerHeight, right - left, height - rulerHeight);
        }
        if (index > 0) {
          context.strokeStyle = "rgba(230,118,135,.95)"; context.lineWidth = 2;
          context.setLineDash([7, 5]);
          context.beginPath(); context.moveTo(left, rulerHeight); context.lineTo(left, height); context.stroke();
          context.setLineDash([]);
        }
        if (right > 0 && left < width) {
          context.fillStyle = "#695e83";
          context.font = "700 11px Manrope, sans-serif";
          context.fillText(`${index + 1}. ${groupTracks.map(track => track.name).join(" + ")}`, Math.max(7, left + 8), 47);
        }
      });
    }
    document.getElementById("audio-zoom-value").textContent = `${(timelineDuration() / viewDuration).toFixed(1)}×`;
    renderOverlay();
  }

  function renderOverlay() {
    const hasStart = selectionStart != null;
    const hasEnd = selectionEnd != null;
    const selected = selectionStart != null && selectionEnd != null && selectionEnd > selectionStart;
    selectionBox.hidden = !selected;
    startMarker.hidden = !hasStart;
    endMarker.hidden = !hasEnd;
    endMarker.classList.toggle("draggable", selected);
    if (hasStart) startMarker.style.left = `${xForTime(selectionStart)}px`;
    if (hasEnd) endMarker.style.left = `${xForTime(selectionEnd)}px`;
    if (selected) {
      const displayedDuration = targetDuration ?? selectionEnd - selectionStart;
      const startX = xForTime(selectionStart), endX = xForTime(selectionStart + displayedDuration);
      selectionBox.style.left = `${startX}px`; selectionBox.style.width = `${Math.max(2, endX - startX)}px`;
      endMarker.style.left = `${endX}px`;
    }
    playhead.style.left = "50%";
  }

  function renderOperations() {
    operationList.replaceChildren();
    operations.forEach((operation, index) => {
      const row = document.createElement("li");
      const title = operation.type === "cut"
        ? editor.dataset.labelCut
        : operation.type === "silence"
          ? `${editor.dataset.labelSilence} · ${operation.duration.toFixed(3)} s`
          : `${editor.dataset.labelSpeed} ${Number(operation.speed).toFixed(2)}×`;
      const visibleStart = sourceToEdited(operation.start, false, "left");
      const visibleEnd = operation.type === "cut"
        ? visibleStart
        : operation.type === "silence"
          ? silenceEditedBounds(operation).end
          : sourceToEdited(operation.end, false, "right");
      const interval = operation.type === "cut"
        ? `${formatTime(visibleStart)} · −${(operation.end - operation.start).toFixed(3)} s`
        : `${formatTime(visibleStart)} — ${formatTime(visibleEnd)}`;
      row.innerHTML = `<span><strong>${title}</strong><small>${interval}</small></span>`;
      const remove = document.createElement("button");
      remove.type = "button"; remove.textContent = "×"; remove.title = editor.dataset.labelRemoveOperation;
      remove.addEventListener("click", () => {
        if (!requireEditable()) return;
        pause();
        selectionStart = selectionEnd = targetDuration = null; selectionRate = 1;
        operations.splice(index, 1); pushHistory();
      });
      row.append(remove); operationList.append(row);
    });
    emptyOperations.hidden = operations.length > 0;
  }

  function renderTrackPanel() {
    const list = document.getElementById("audio-track-list");
    const locked = tracks.length > 1 && mergeApplied;
    const signature = `${trackGroups.map(group => group.join(",")).join("|")}:${locked}`;
    if (list.dataset.signature !== signature) {
      list.dataset.signature = signature;
      list.replaceChildren();
      trackGroups.forEach((group, index) => {
        const groupTracks = group.map(id => tracks.find(track => track.input_id === id)).filter(Boolean);
        if (!groupTracks.length) return;
        const groupKey = group.join(",");
        const groupName = groupTracks.map(track => track.name).join(" + ");
        const groupDuration = groupTracks.reduce((total, track) => total + track.duration, 0);
        const row = document.createElement("li");
        row.draggable = !locked;
        row.dataset.trackIds = groupKey;
        const grip = Object.assign(document.createElement("span"), {className: "audio-track-grip", textContent: "⠿"});
        grip.setAttribute("aria-hidden", "true");
        const copy = Object.assign(document.createElement("span"), {className: "audio-track-copy"});
        groupTracks.forEach((track, trackIndex) => {
          const entry = Object.assign(document.createElement("span"), {className: "audio-track-entry"});
          const name = Object.assign(document.createElement("strong"), {
            textContent: `${index + trackIndex + 1}. ${track.name}`,
          });
          const length = Object.assign(document.createElement("small"), {textContent: formatTime(track.duration)});
          const remove = Object.assign(document.createElement("button"), {
            type: "button", className: "audio-track-remove", textContent: "×", title: editor.dataset.labelRemoveTrack,
          });
          const canRemoveTrack = !mergeApplied && group.length === 1 && tracks.length > 1;
          remove.disabled = !canRemoveTrack || removingTrack;
          remove.addEventListener("pointerdown", event => event.stopPropagation());
          remove.addEventListener("click", event => {
            event.preventDefault(); event.stopPropagation(); removeAudioTrack(track.input_id);
          });
          entry.append(name, length, remove); copy.append(entry);
        });
        row.append(grip, copy);
        row.addEventListener("dragstart", event => {
          if (locked) { event.preventDefault(); return; }
          draggedTrackId = groupKey;
          row.classList.add("dragging");
          event.dataTransfer.effectAllowed = "move";
          event.dataTransfer.setData("text/plain", groupKey);
        });
        row.addEventListener("dragend", () => { draggedTrackId = null; row.classList.remove("dragging"); });
        row.addEventListener("dragover", event => { if (!locked) { event.preventDefault(); event.dataTransfer.dropEffect = "move"; } });
        row.addEventListener("drop", event => {
          event.preventDefault();
          const sourceKey = event.dataTransfer.getData("text/plain") || draggedTrackId;
          if (!sourceKey || sourceKey === groupKey) return;
          const sourceIndex = trackGroups.findIndex(item => item.join(",") === sourceKey);
          const targetIndex = trackGroups.findIndex(item => item.join(",") === groupKey);
          if (sourceIndex < 0 || targetIndex < 0) return;
          const nextGroups = trackGroups.map(item => [...item]);
          const [moved] = nextGroups.splice(sourceIndex, 1);
          nextGroups.splice(targetIndex, 0, moved);
          reorderTracks(nextGroups.flat(), nextGroups);
        });
        list.append(row);
      });
    }
    const isEditingLocked = editingLocked();
    document.getElementById("audio-apply-merge").disabled = mergeApplied;
    document.getElementById("audio-add-track").disabled = addingTrack;
    document.getElementById("audio-add-silence").disabled = addingTrack;
    document.getElementById("audio-export").disabled = isEditingLocked;
    list.classList.toggle("locked", locked);
  }

  function render() {
    cursor = clamp(cursor, 0, editedDuration());
    viewStart = cursor - viewDuration / 2;
    document.getElementById("audio-cursor-time").textContent = formatTime(cursor);
    const source = sourceSelectionDuration();
    document.getElementById("audio-selection-time").textContent = source ? `${formatTime(selectionStart)} — ${formatTime(selectionEnd)} · ${source.toFixed(3)} s` : "—";
    document.getElementById("audio-speed-value").textContent = `${selectionSpeed().toFixed(2)}×`;
    document.getElementById("audio-target-duration").textContent = source
      ? `${(targetDuration ?? source).toFixed(3)} s`
      : "—";
    document.getElementById("audio-editor-undo").disabled = historyIndex <= 0 && !canUndoTrackAddition;
    document.getElementById("audio-editor-redo").disabled = historyIndex >= history.length - 1;
    const startButton = document.getElementById("audio-set-start");
    const endButton = document.getElementById("audio-set-end");
    const isEditingLocked = editingLocked();
    startButton.classList.toggle("primary", selectionStart != null);
    startButton.classList.toggle("secondary", selectionStart == null);
    endButton.classList.toggle("primary", selectionEnd != null);
    endButton.classList.toggle("secondary", selectionEnd == null);
    const editingButtons = [startButton, endButton, document.getElementById("audio-reset-selection"), document.getElementById("audio-delete-selection")];
    editingButtons.forEach(button => {
      button.classList.toggle("editing-locked", isEditingLocked);
      button.setAttribute("aria-disabled", String(isEditingLocked));
      button.title = isEditingLocked ? editor.dataset.errorMergeFirst : "";
    });
    document.getElementById("audio-delete-selection").disabled = !isEditingLocked && !source;
    renderTrackPanel(); renderOperations(); draw();
  }

  function resizeCanvas() {
    const ratio = window.devicePixelRatio || 1;
    canvas.width = Math.max(1, Math.round(shell.clientWidth * ratio));
    canvas.height = Math.max(1, Math.round(shell.clientHeight * ratio));
    canvas.style.width = `${shell.clientWidth}px`; canvas.style.height = `${shell.clientHeight}px`;
    context.setTransform(ratio, 0, 0, ratio, 0, 0); draw();
  }

  function pause({keepResume = false} = {}) {
    debugLog("pause_requested", {keepResume});
    playbackToken += 1;
    const wasPlaying = playbackActive;
    if (wasPlaying) {
      cursor = virtualSilenceCursor() ?? sourceToEdited(Math.min(duration, currentSourceTime()));
      viewStart = cursor - viewDuration / 2;
    }
    playbackActive = false;
    playbackStarting = false;
    if (audio) { audio.pause(); audio.playbackRate = 1; }
    if (playbackStopTimer) clearTimeout(playbackStopTimer);
    playbackStopTimer = null;
    if (playbackBoundaryTimer) clearTimeout(playbackBoundaryTimer);
    playbackBoundaryTimer = null;
    playbackEndCursor = null;
    cutSkipTarget = null;
    cutSkipInProgress = false;
    virtualSilence = null;
    if (animationFrame) cancelAnimationFrame(animationFrame);
    animationFrame = null;
    resumePlayback = keepResume && wasPlaying;
    document.getElementById("audio-editor-play").textContent = "▶";
    if (wasPlaying) {
      document.getElementById("audio-cursor-time").textContent = formatTime(cursor);
      draw();
    }
    debugLog("pause_finished", {wasPlaying});
  }

  function schedulePlaybackBoundary() {
    if (playbackBoundaryTimer) clearTimeout(playbackBoundaryTimer);
    playbackBoundaryTimer = null;
    if (!playbackActive || !audio) return;
    const source = currentSourceTime();
    const boundary = nextPlaybackBoundary(source);
    const track = orderedTracks()[currentTrackIndex];
    if (boundary == null || !track || boundary > track.offset + track.duration + .0005) return;
    const delay = Math.max(0, (boundary - source) / Math.max(.000001, audio.playbackRate) * 1000);
    const token = playbackToken;
    playbackBoundaryTimer = setTimeout(async () => {
      playbackBoundaryTimer = null;
      if (!playbackActive || token !== playbackToken) return;
      const liveSource = currentSourceTime();
      if (liveSource < boundary - .001) {
        schedulePlaybackBoundary();
        return;
      }
      const silence = silenceOperationAtEdited(sourceToEdited(boundary, false, "left"));
      if (silence) {
        beginVirtualSilence(silence, sourceToEdited(boundary, false, "left"), token);
        return;
      }
      const operation = speedOperationAtSource(liveSource);
      audio.playbackRate = mediaPlaybackRate(operation?.speed || 1);
      debugLog("speed_boundary_applied", {boundary, liveSource, playbackRate: audio.playbackRate});
      schedulePlaybackBoundary();
    }, delay);
  }

  async function beginVirtualSilence(operation, editedStart, token) {
    if (audio) audio.pause();
    const position = setSourcePosition(operation.end);
    if (!position) { pause(); return; }
    await seekAudio(position.audio, position.localTime);
    if (!playbackActive || token !== playbackToken) return;
    const editedEnd = silenceEditedBounds(operation).end;
    virtualSilence = {
      editedStart,
      editedEnd,
      startedAt: performance.now(),
      durationMs: Math.max(1, (editedEnd - editedStart) * 1000),
    };
    playbackBoundaryTimer = setTimeout(() => {
      playbackBoundaryTimer = null;
      if (!playbackActive || token !== playbackToken) return;
      virtualSilence = null;
      if (operation.end >= duration - .0005) {
        pause(); cursor = editedEnd; render();
      } else {
        resumePlaybackAtBoundary(operation.end, token);
      }
    }, virtualSilence.durationMs);
    debugLog("virtual_silence_started", {sourceEnd: operation.end, editedStart, editedEnd});
    if (animationFrame) cancelAnimationFrame(animationFrame);
    animationFrame = requestAnimationFrame(followPlayback);
  }

  async function resumePlaybackAtBoundary(boundary, token) {
    const position = setSourcePosition(boundary);
    if (!position) { pause(); return; }
    await seekAudio(position.audio, position.localTime);
    if (!playbackActive || token !== playbackToken) return;
    const operation = speedOperationAtSource(boundary);
    audio.playbackRate = mediaPlaybackRate(operation?.speed || 1);
    try {
      await audio.play();
    } catch (error) {
      debugLog("speed_boundary_play_rejected", {boundary, name: error?.name, message: error?.message});
      if (token === playbackToken) pause();
      return;
    }
    if (!playbackActive || token !== playbackToken) { audio.pause(); return; }
    debugLog("speed_boundary_resumed", {boundary, playbackRate: audio.playbackRate});
    schedulePlaybackBoundary();
    if (animationFrame) cancelAnimationFrame(animationFrame);
    animationFrame = requestAnimationFrame(followPlayback);
  }

  function followPlayback() {
    if (!audio) return;
    const silenceCursor = virtualSilenceCursor();
    if (silenceCursor != null) {
      cursor = silenceCursor;
      viewStart = cursor - viewDuration / 2;
      draw();
      document.getElementById("audio-cursor-time").textContent = formatTime(cursor);
      if (playbackActive) animationFrame = requestAnimationFrame(followPlayback);
      return;
    }
    const currentSource = Math.min(duration, currentSourceTime());
    const activeCut = timelineOperations(false).find(operation => (
      operation.type === "cut" && currentSource >= operation.start - .003 && currentSource < operation.end - .003
    ));
    if (activeCut) {
      const cutBoundary = sourceToEdited(activeCut.end);
      cursor = cutBoundary;
      viewStart = cursor - viewDuration / 2;
      draw();
      document.getElementById("audio-cursor-time").textContent = formatTime(cursor);
      if (playbackEndCursor != null && cutBoundary >= playbackEndCursor - .005) {
        const end = playbackEndCursor;
        pause(); cursor = end; render();
        return;
      }
      if (cutSkipTarget == null || Math.abs(cutSkipTarget - activeCut.end) > .001) {
        cutSkipTarget = activeCut.end;
        resumePlaybackAfterCut(activeCut.end);
      }
      return;
    } else {
      cutSkipTarget = null;
      cursor = sourceToEdited(currentSource);
    }
    const desiredRate = mediaPlaybackRate(playbackRateAtSource(currentSourceTime()));
    if (Math.abs(audio.playbackRate - desiredRate) > .001) audio.playbackRate = desiredRate;
    viewStart = cursor - viewDuration / 2;
    draw();
    document.getElementById("audio-cursor-time").textContent = formatTime(cursor);
    if (playbackEndCursor != null && cursor >= playbackEndCursor - .005) {
      const end = playbackEndCursor;
      pause(); cursor = end; render();
    } else if (playbackActive && !audio.paused) animationFrame = requestAnimationFrame(followPlayback);
  }

  async function resumePlaybackAfterCut(sourceTarget) {
    if (cutSkipInProgress || !playbackActive) return;
    cutSkipInProgress = true;
    const token = playbackToken;
    const position = setSourcePosition(sourceTarget);
    debugLog("cut_skip_started", {sourceTarget, localTime: position?.localTime ?? null});
    if (!position) { cutSkipInProgress = false; pause(); return; }
    await seekAudio(position.audio, position.localTime);
    if (!playbackActive || token !== playbackToken) { cutSkipInProgress = false; return; }
    const silence = silenceOperationAtSource(sourceTarget);
    if (silence) {
      cutSkipInProgress = false;
      beginVirtualSilence(silence, sourceToEdited(sourceTarget, false), token);
      return;
    }
    audio.playbackRate = mediaPlaybackRate(playbackRateAtSource(sourceTarget));
    try {
      await audio.play();
    } catch (error) {
      debugLog("cut_skip_play_rejected", {name: error?.name, message: error?.message});
      cutSkipInProgress = false;
      pause();
      return;
    }
    if (!playbackActive || token !== playbackToken) { audio.pause(); cutSkipInProgress = false; return; }
    cutSkipInProgress = false;
    debugLog("cut_skip_finished", {sourceTarget, localTime: position.localTime});
    schedulePlaybackBoundary();
    if (animationFrame) cancelAnimationFrame(animationFrame);
    animationFrame = requestAnimationFrame(followPlayback);
  }

  async function continueWithNextTrack() {
    if (!playbackActive) return;
    const ordered = orderedTracks();
    const next = ordered[currentTrackIndex + 1];
    if (!next) {
      const end = playbackEndCursor ?? editedDuration();
      const insertedSilence = silenceOperationAtEdited(sourceToEdited(duration, false, "left"));
      if (insertedSilence && cursor < silenceEditedBounds(insertedSilence).end - .005) {
        beginVirtualSilence(
          insertedSilence,
          Math.max(cursor, silenceEditedBounds(insertedSilence).start),
          playbackToken,
        );
        return;
      }
      pause(); cursor = end; render(); return;
    }
    if (sourceToEdited(next.offset) >= (playbackEndCursor ?? editedDuration()) - .005) {
      const end = playbackEndCursor ?? editedDuration();
      pause(); cursor = end; render(); return;
    }
    const silence = silenceOperationAtEdited(sourceToEdited(next.offset, false, "left"));
    if (silence) {
      beginVirtualSilence(silence, sourceToEdited(next.offset, false, "left"), playbackToken);
      return;
    }
    currentTrackIndex += 1;
    audio = next.audio;
    audio.currentTime = 0;
    const operation = speedOperationAtSource(next.offset);
    audio.playbackRate = mediaPlaybackRate(operation?.speed || 1);
    await audio.play();
    schedulePlaybackBoundary();
    if (animationFrame) cancelAnimationFrame(animationFrame);
    animationFrame = requestAnimationFrame(followPlayback);
  }

  async function togglePlayback() {
    debugLog("play_button", {});
    if (playbackActive || playbackStarting) { pause({keepResume: true}); return; }
    const start = cursor;
    const end = editedDuration();
    const sourceStart = editedToSource(start, "right");
    const silence = silenceOperationAtEdited(start);
    const operation = speedOperationAtSource(sourceStart);
    const speed = mediaPlaybackRate(operation?.speed || 1);
    resumePlayback = false;
    playbackStarting = true;
    const token = ++playbackToken;
    const position = setSourcePosition(sourceStart);
    debugLog("play_position_selected", {start, sourceStart, localTime: position?.localTime ?? null});
    if (!position) { playbackStarting = false; debugLog("play_aborted_no_position"); return; }
    await seekAudio(position.audio, position.localTime);
    debugLog("first_seek_finished", {target: position.localTime});
    if (token !== playbackToken) { debugLog("play_aborted_token_after_seek", {token}); return; }
    if (Math.abs(position.audio.currentTime - position.localTime) > .05) {
      await seekAudio(position.audio, position.localTime);
      debugLog("second_seek_finished", {target: position.localTime});
      if (token !== playbackToken) { debugLog("play_aborted_token_after_second_seek", {token}); return; }
    }
    playbackEndCursor = end;
    playbackActive = true;
    playbackStarting = false;
    document.getElementById("audio-editor-play").textContent = "❚❚";
    if (silence) {
      debugLog("playback_started_in_silence", {target: position.localTime});
      beginVirtualSilence(silence, start, token);
      return;
    }
    audio.playbackRate = speed;
    try {
      await audio.play();
      debugLog("audio_play_resolved", {target: position.localTime});
    } catch (error) {
      debugLog("audio_play_rejected", {name: error?.name, message: error?.message});
      if (token === playbackToken) pause();
      return;
    }
    if (token !== playbackToken) { debugLog("play_aborted_token_after_play", {token}); audio.pause(); return; }
    debugLog("playback_started", {target: position.localTime});
    schedulePlaybackBoundary();
    animationFrame = requestAnimationFrame(followPlayback);
  }

  function setBoundary(kind) {
    if (!requireEditable()) return;
    pause(); draftOperations = null; selectionRate = 1; setMessage("");
    const current = kind === "start" ? selectionStart : selectionEnd;
    if (current != null && Math.abs(current - cursor) <= .005) {
      if (kind === "start") selectionStart = null;
      else selectionEnd = null;
    } else if (kind === "start") selectionStart = cursor;
    else selectionEnd = cursor;
    if (selectionStart != null && selectionEnd != null && selectionEnd > selectionStart) targetDuration = selectionEnd - selectionStart;
    else targetDuration = null;
    debugLog("boundary_changed", {kind});
    pushHistory();
  }

  function changeZoom(multiplier) {
    pause();
    const oldDuration = viewDuration || timelineDuration();
    viewDuration = clamp(oldDuration * multiplier, Math.min(.25, timelineDuration()), timelineDuration());
    viewStart = cursor - viewDuration / 2;
    normalizeView();
    draw();
  }

  function resetSelection() {
    if (!requireEditable()) return;
    pause(); selectionStart = selectionEnd = targetDuration = null; selectionRate = 1; setMessage(""); pushHistory();
  }

  function addOperation(type) {
    if (!requireEditable()) return;
    const source = sourceSelectionDuration();
    if (!source) { setMessage(editor.dataset.errorSelection); return; }
    if (type === "cut" && selectionStart <= .001 && selectionEnd >= editedDuration() - .001) { setMessage(editor.dataset.errorWholeTrack); return; }
    const sourceStart = editedToSource(selectionStart, "right", false);
    const sourceEnd = editedToSource(selectionEnd, "left", false);
    const withoutSelectedSilence = removeInsertedSilenceFromEditedRange(operations, selectionStart, selectionEnd);
    operations = sourceEnd - sourceStart >= .001
      ? cutOperationsForRange(withoutSelectedSilence, sourceStart, sourceEnd)
      : withoutSelectedSilence;
    debugLog("cut_applied", {sourceStart, sourceEnd});
    cursor = selectionStart;
    selectionStart = selectionEnd = targetDuration = null; selectionRate = 1; setMessage(""); pushHistory();
  }

  async function exportAudio() {
    pause(); setMessage("");
    const button = document.getElementById("audio-export"); button.disabled = true;
    try {
      const response = await fetch(editor.dataset.saveUrl, {
        method: "POST", headers: {"Content-Type": "application/json", "X-CSRFToken": csrfToken()},
        body: JSON.stringify({
          operations,
          trailing_silence: 0,
          output_format: editor.dataset.outputFormat,
          mp3_bitrate: editor.dataset.mp3Bitrate,
          track_order: trackOrder,
        }),
      });
      const result = await response.json().catch(() => ({}));
      if (!response.ok) throw new Error(result.error || editor.dataset.errorSave);
      location.replace(result.result_url);
    } catch (error) { button.disabled = false; setMessage(error.message); }
  }

  async function applyMerge() {
    if (mergeApplied) return;
    closeSilencePlacement();
    pause(); mergeApplied = true; trackGroups = [[...trackOrder]];
    setMessage(""); pushHistory();
    if (draftSaveTimer) { clearTimeout(draftSaveTimer); draftSaveTimer = null; }
    try {
      await persistEditorState();
    } catch (error) {
      setMessage(error.message);
    }
  }

  async function pollAddedTrack(statusUrl, resultUrl) {
    const status = document.getElementById("audio-track-status");
    while (true) {
      await new Promise(resolve => setTimeout(resolve, 700));
      const response = await fetch(statusUrl, {cache: "no-store"});
      const data = await response.json();
      status.textContent = data.step || "";
      if (data.failed) throw new Error(data.error || editor.dataset.errorAdd);
      if (data.complete) { location.replace(resultUrl); return; }
    }
  }

  async function addAudioTrack(file) {
    const button = document.getElementById("audio-add-track");
    const status = document.getElementById("audio-track-status");
    addingTrack = true; button.disabled = true; status.textContent = file.name;
    const body = new FormData();
    body.append("audio", file);
    body.append("editor_state", JSON.stringify(editorState()));
    try {
      const response = await fetch(editor.dataset.addUrl, {method: "POST", headers: {"X-CSRFToken": csrfToken()}, body});
      const result = await response.json().catch(() => ({}));
      if (!response.ok) throw new Error(result.error || editor.dataset.errorAdd);
      await pollAddedTrack(result.status_url, result.result_url);
    } catch (error) {
      addingTrack = false; status.textContent = error.message; button.disabled = false;
    }
  }

  function stateWithoutTrack(trackId) {
    const nextOrder = trackOrder.filter(id => id !== trackId);
    const nextGroups = trackGroups
      .map(group => group.filter(id => id !== trackId))
      .filter(group => group.length);
    return {
      ...editorState(),
      operations: operationsInOrder(operations, trackOrder, nextOrder),
      trackOrder: nextOrder,
      trackGroups: nextGroups,
      mergeApplied: nextOrder.length <= 1 ? true : mergeApplied,
      cursor: 0,
      selectionStart: null,
      selectionEnd: null,
      targetDuration: null,
      selectionRate: 1,
    };
  }

  async function removeAudioTrack(trackId = null, undoAddition = false) {
    if (removingTrack || tracks.length <= 1) return;
    pause(); removingTrack = true; renderTrackPanel();
    const status = document.getElementById("audio-track-status");
    status.textContent = undoAddition ? editor.dataset.labelUndoingAddition : editor.dataset.labelRemovingTrack;
    const body = undoAddition
      ? {undo_addition: true}
      : {track_id: trackId, editor_state: stateWithoutTrack(trackId)};
    try {
      const response = await fetch(editor.dataset.removeUrl, {
        method: "POST",
        headers: {"Content-Type": "application/json", "X-CSRFToken": csrfToken()},
        body: JSON.stringify(body),
      });
      const result = await response.json().catch(() => ({}));
      if (!response.ok) throw new Error(result.error || editor.dataset.errorRemove);
      await pollAddedTrack(result.status_url, result.result_url);
    } catch (error) {
      removingTrack = false; status.textContent = error.message; renderTrackPanel();
    }
  }

  function closeSilencePlacement() {
    const placement = document.getElementById("audio-silence-placement");
    placement.hidden = true;
    document.getElementById("audio-add-silence").setAttribute("aria-expanded", "false");
  }

  function addSilence(position) {
    pause();
    const sourcePosition = position === "start" ? 0 : duration;
    operations = normalizeOperations([
      ...operations,
      {type: "silence", start: sourcePosition, end: sourcePosition, duration: 1},
    ]);
    trailingSilence = 0;
    mergeApplied = false;
    cursor = sourceToEdited(sourcePosition, false, "left");
    closeSilencePlacement();
    setMessage(""); pushHistory();
  }

  const beginWaveformDrag = event => {
    if (event.target === endMarker || endMarker.contains(event.target)) return;
    if (event.button != null && event.button !== 0) return;
    event.preventDefault();
    pause();
    waveformDrag = {x: event.clientX, cursor, moved: false, pointerId: event.pointerId};
    shell.setPointerCapture(event.pointerId);
  };
  const moveWaveformDrag = event => {
    if (!waveformDrag || event.pointerId !== waveformDrag.pointerId) return;
    event.preventDefault();
    const delta = event.clientX - waveformDrag.x;
    if (Math.abs(delta) > 3) waveformDrag.moved = true;
    if (!waveformDrag.moved) return;
    cursor = clamp(waveformDrag.cursor - delta * viewDuration / Math.max(1, shell.clientWidth), 0, editedDuration());
    viewStart = cursor - viewDuration / 2;
    normalizeView(); shell.classList.add("panning"); render();
  };
  const finishWaveformDrag = event => {
    if (!waveformDrag || event.pointerId !== waveformDrag.pointerId) return;
    const moved = waveformDrag.moved;
    waveformDrag = null; shell.classList.remove("panning");
    if (shell.hasPointerCapture(event.pointerId)) shell.releasePointerCapture(event.pointerId);
    if (!moved) {
      cursor = timeForX(event.clientX - shell.getBoundingClientRect().left);
      viewStart = cursor - viewDuration / 2;
      render();
    }
  };
  const cancelWaveformDrag = event => {
    if (!waveformDrag || event.pointerId !== waveformDrag.pointerId) return;
    waveformDrag = null; shell.classList.remove("panning");
  };
  // Capture the gesture before boundary markers or their children can consume it.
  // This matters when the fixed playhead and the start marker occupy the same point.
  shell.addEventListener("pointerdown", beginWaveformDrag, {capture: true});
  shell.addEventListener("pointermove", moveWaveformDrag, {capture: true});
  shell.addEventListener("pointerup", finishWaveformDrag, {capture: true});
  shell.addEventListener("pointercancel", cancelWaveformDrag, {capture: true});
  endMarker.addEventListener("pointerdown", event => {
    if (!requireEditable()) return;
    if (selectionStart == null || selectionEnd == null || selectionEnd <= selectionStart) return;
    event.preventDefault(); event.stopPropagation(); pause(); setMessage("");
    const sourceStart = editedToSource(selectionStart, "right", false);
    const sourceEnd = editedToSource(selectionEnd, "left", false);
    const displayedDuration = selectionEnd - selectionStart;
    draggingHandle = {
      x: event.clientX, duration: targetDuration ?? displayedDuration, secondsPerPixel: viewDuration / shell.clientWidth,
      sourceStart, sourceEnd, displayedDuration, silent: sourceRangeIsSilent(sourceStart, sourceEnd),
      hasGeneratedSilence: operations.some(operation => (
        operation.type === "silence" && operation.start < sourceEnd - .0005 && operation.end > sourceStart + .0005
      )),
      baseOperations: JSON.parse(JSON.stringify(operations)), moved: false, pointerId: event.pointerId,
    };
    endMarker.setPointerCapture(event.pointerId);
  });
  endMarker.addEventListener("pointermove", event => {
    if (!draggingHandle) return;
    const delta = event.clientX - draggingHandle.x;
    if (Math.abs(delta) > 2) draggingHandle.moved = true;
    if (!draggingHandle.moved) return;
    const requestedDuration = draggingHandle.duration + delta * draggingHandle.secondsPerPixel;
    const minimumDuration = draggingHandle.displayedDuration / MAX_MEDIA_PLAYBACK_RATE;
    const maximumDuration = draggingHandle.silent
      ? Number.POSITIVE_INFINITY
      : draggingHandle.displayedDuration / MIN_MEDIA_PLAYBACK_RATE;
    targetDuration = clamp(requestedDuration, minimumDuration, maximumDuration);
    selectionRate = draggingHandle.displayedDuration / targetDuration;
    const adjustsGeneratedSilence = draggingHandle.hasGeneratedSilence
      || draggingHandle.silent && targetDuration > draggingHandle.displayedDuration + .001;
    draftOperations = adjustsGeneratedSilence
      ? silenceOperationsForRange(
        draggingHandle.baseOperations, draggingHandle.sourceStart, draggingHandle.sourceEnd, targetDuration,
      )
      : speedOperationsForRange(
        draggingHandle.baseOperations, draggingHandle.sourceStart, draggingHandle.sourceEnd, selectionRate,
      );
    render();
  });
  endMarker.addEventListener("pointerup", event => {
    if (!draggingHandle) return;
    const moved = draggingHandle.moved;
    if (endMarker.hasPointerCapture(event.pointerId)) endMarker.releasePointerCapture(event.pointerId);
    const drag = draggingHandle;
    draggingHandle = null;
    if (moved && draftOperations) {
      operations = draftOperations;
      draftOperations = null;
      selectionStart = sourceToEdited(drag.sourceStart, false, "left");
      selectionEnd = sourceToEdited(drag.sourceEnd, false, "right");
      targetDuration = Math.max(0, selectionEnd - selectionStart);
      cursor = selectionStart;
      pushHistory();
      return;
    }
    draftOperations = null; selectionRate = 1; render();
  });
  endMarker.addEventListener("pointercancel", () => {
    draggingHandle = null; draftOperations = null; selectionRate = 1; render();
  });
  document.getElementById("audio-editor-play").addEventListener("click", togglePlayback);
  document.getElementById("audio-zoom-out").addEventListener("click", () => changeZoom(2));
  document.getElementById("audio-zoom-in").addEventListener("click", () => changeZoom(.5));
  document.getElementById("audio-set-start").addEventListener("click", () => setBoundary("start"));
  document.getElementById("audio-set-end").addEventListener("click", () => setBoundary("end"));
  document.getElementById("audio-reset-selection").addEventListener("click", resetSelection);
  document.getElementById("audio-delete-selection").addEventListener("click", () => addOperation("cut"));
  document.getElementById("audio-apply-merge").addEventListener("click", applyMerge);
  document.getElementById("audio-add-silence").addEventListener("click", event => {
    const placement = document.getElementById("audio-silence-placement");
    placement.hidden = !placement.hidden;
    event.currentTarget.setAttribute("aria-expanded", String(!placement.hidden));
  });
  document.querySelectorAll("[data-silence-position]").forEach(button => {
    button.addEventListener("click", () => addSilence(button.dataset.silencePosition));
  });
  const addTrackInput = document.getElementById("audio-add-track-input");
  document.getElementById("audio-add-track").addEventListener("click", () => { addTrackInput.value = ""; addTrackInput.click(); });
  addTrackInput.addEventListener("change", () => { if (addTrackInput.files?.[0]) addAudioTrack(addTrackInput.files[0]); });
  document.getElementById("audio-editor-undo").addEventListener("click", () => {
    if (historyIndex > 0) restore(history[--historyIndex]);
    else if (canUndoTrackAddition) removeAudioTrack(null, true);
  });
  document.getElementById("audio-editor-redo").addEventListener("click", () => { if (historyIndex < history.length - 1) restore(history[++historyIndex]); });
  document.getElementById("audio-export").addEventListener("click", exportAudio);
  document.addEventListener("keydown", event => {
    if (["INPUT", "TEXTAREA", "SELECT"].includes(document.activeElement?.tagName)) return;
    if (event.code === "Space") { event.preventDefault(); togglePlayback(); }
    else if (event.key.toLowerCase() === "i") setBoundary("start");
    else if (event.key.toLowerCase() === "o") setBoundary("end");
    else if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === "z") {
      event.preventDefault();
      if (event.shiftKey && historyIndex < history.length - 1) restore(history[++historyIndex]);
      else if (!event.shiftKey && historyIndex > 0) restore(history[--historyIndex]);
    }
  });
  window.addEventListener("resize", resizeCanvas);
  tracks.forEach(track => track.audio?.addEventListener("ended", continueWithNextTrack));
  tracks.forEach(track => ["play", "playing", "pause", "waiting", "stalled", "seeking", "seeked", "ended", "error", "emptied"].forEach(eventName => {
    track.audio?.addEventListener(eventName, () => debugLog(`media_${eventName}`, {
      track_id: track.input_id,
      media_error: track.audio?.error ? {code: track.audio.error.code, message: track.audio.error.message} : null,
    }));
  }));

  Promise.all(tracks.map(track => Promise.all([
    fetch(track.peaks_url, {cache: "no-store"}).then(response => {
      if (!response.ok) throw new Error(editor.dataset.errorSave);
      return response.json();
    }),
    new Promise((resolve, reject) => {
      if (track.audio.readyState >= HTMLMediaElement.HAVE_METADATA) resolve();
      else { track.audio.addEventListener("loadedmetadata", resolve, {once: true}); track.audio.addEventListener("error", reject, {once: true}); }
    }),
  ]).then(([waveform]) => {
    track.peaks = waveform.peaks || [];
    track.peaksPerSecond = waveform.peaks_per_second || 100;
    track.duration = Number(waveform.duration) || track.audio.duration || track.duration;
  }))).then(() => {
    rebuildTrackTimeline();
    if (trailingSilence > 0) {
      operations = normalizeOperations([
        ...operations,
        {type: "silence", start: duration, end: duration, duration: trailingSilence},
      ]);
      trailingSilence = 0;
    }
    cursor = clamp(cursor, 0, editedDuration());
    selectionStart = selectionStart == null ? null : clamp(selectionStart, 0, editedDuration());
    selectionEnd = selectionEnd == null ? null : clamp(selectionEnd, 0, editedDuration());
    if (selectionStart != null && selectionEnd != null && selectionEnd < selectionStart) [selectionStart, selectionEnd] = [selectionEnd, selectionStart];
    targetDuration = selectionStart != null && selectionEnd != null
      ? (targetDuration ?? selectionEnd - selectionStart) : null;
    viewDuration = duration; viewStart = cursor - viewDuration / 2;
    history = [snapshot()]; historyIndex = 0; loading.hidden = true; resizeCanvas(); render();
    debugLog("editor_ready", {user_agent: navigator.userAgent}, {clear: true});
  }).catch(error => { loading.textContent = error.message || editor.dataset.errorSave; });
})();
