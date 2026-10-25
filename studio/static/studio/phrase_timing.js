(() => {
  const root = document.getElementById("phrase-timing-editor");
  if (!root) return;

  const video = document.getElementById("timing-video");
  const videoSeek = document.getElementById("timing-video-seek");
  const list = document.getElementById("timing-phrase-list");
  const editBox = document.getElementById("timing-text-edit");
  const textarea = document.getElementById("timing-textarea");
  const csrf = root.querySelector("input[name=csrfmiddlewaretoken]").value;
  const labels = root.dataset;
  const automaticMode = root.dataset.projectMode === "automatic";
  let phrases = JSON.parse(document.getElementById("timing-initial-phrases").textContent);
  let reviewItems = JSON.parse(document.getElementById("timing-initial-review-items")?.textContent || "[]");
  let selectedIndex = null;
  let phraseEditIndex = null;
  let pendingReviewItem = null;
  let pendingInsertIndex = null;
  let reviewPlayback = null;
  let reviewSeekGeneration = 0;
  let quickReviewState = null;
  let mode = "video";
  let modeBeforeEdit = "video";
  let structure = { index: Math.min(firstUntimed(), Math.max(0, phrases.length - 1)), boundary: 1 };
  let structureBetween = null;
  let undoStack = [];
  let redoStack = [];
  let saveTimer = null;
  let saveQueue = Promise.resolve();
  let pendingSeekTarget = null;
  let seekRetryTimer = null;
  let overlapWarningTimer = null;
  let overlapWarningHideTimer = null;
  let draggingVideoSeek = false;
  let markingTarget = null;
  let pendingExportFormat = null;

  const interfaceTooltip = document.createElement("div");
  interfaceTooltip.className = "timing-interface-tooltip";
  interfaceTooltip.setAttribute("role", "tooltip");
  interfaceTooltip.hidden = true;
  document.body.append(interfaceTooltip);
  let interfaceTooltipTarget = null;
  const showInterfaceTooltip = target => {
    if (!target?.dataset.uiTooltip) return;
    interfaceTooltipTarget = target;
    interfaceTooltip.textContent = target.dataset.uiTooltip;
    interfaceTooltip.hidden = false;
    const targetRect = target.getBoundingClientRect();
    const tooltipRect = interfaceTooltip.getBoundingClientRect();
    let left = targetRect.left + targetRect.width / 2 - tooltipRect.width / 2;
    left = Math.max(12, Math.min(window.innerWidth - tooltipRect.width - 12, left));
    let top = targetRect.bottom + 9;
    if (top + tooltipRect.height > window.innerHeight - 12) top = targetRect.top - tooltipRect.height - 9;
    interfaceTooltip.style.left = `${left}px`;
    interfaceTooltip.style.top = `${Math.max(12, top)}px`;
  };
  const hideInterfaceTooltip = target => {
    if (target && target !== interfaceTooltipTarget) return;
    interfaceTooltipTarget = null;
    interfaceTooltip.hidden = true;
  };
  document.addEventListener("pointerover", event => showInterfaceTooltip(event.target.closest?.("[data-ui-tooltip]")));
  document.addEventListener("pointerout", event => {
    const target = event.target.closest?.("[data-ui-tooltip]");
    if (target && !target.contains(event.relatedTarget)) hideInterfaceTooltip(target);
  });
  document.addEventListener("focusin", event => showInterfaceTooltip(event.target.closest?.("[data-ui-tooltip]")));
  document.addEventListener("focusout", event => hideInterfaceTooltip(event.target.closest?.("[data-ui-tooltip]")));
  window.addEventListener("scroll", () => hideInterfaceTooltip(), true);
  window.addEventListener("resize", () => hideInterfaceTooltip());

  const clone = value => JSON.parse(JSON.stringify(value));
  const words = text => text.trim().split(/\s+/).filter(Boolean);
  const estimatedDuration = text => {
    const count = (text.match(/[\p{L}\p{N}'-]+(?:\.[\p{L}\p{N}'-]+)*/gu) || []).length;
    const punctuation = (text.match(/[,;:](?=\s|$)/g) || []).length * .2
      + (text.match(/[!?…]+|\.(?=\s|$)/g) || []).length * .35;
    return Math.max(.8, count * 60 / 160 + punctuation);
  };
  const clock = value => {
    const milliseconds = Math.max(0, Math.round((Number(value) || 0) * 1000));
    const minutes = Math.floor(milliseconds / 60000);
    const seconds = Math.floor(milliseconds % 60000 / 1000);
    return `${String(minutes).padStart(2, "0")}:${String(seconds).padStart(2, "0")}.${String(milliseconds % 1000).padStart(3, "0")}`;
  };
  const parseClock = value => {
    const parts = String(value).trim().replace(",", ".").split(":");
    if (parts.length !== 2 && parts.length !== 3) return null;
    if (!parts.every(part => part !== "" && /^\d+(?:\.\d+)?$/.test(part))) return null;
    const seconds = Number(parts.at(-1));
    const minutes = Number(parts.at(-2));
    const hours = parts.length === 3 ? Number(parts[0]) : 0;
    if (!Number.isFinite(seconds) || !Number.isInteger(minutes) || !Number.isInteger(hours)
      || seconds >= 60 || minutes < 0 || (parts.length === 3 && minutes >= 60)) return null;
    return Math.round((hours * 3600 + minutes * 60 + seconds) * 1000) / 1000;
  };
  const editableLine = item => item.start === null || item.start === undefined
    ? item.text
    : `[${clock(item.start)}${item.end_explicit ? ` --> ${clock(item.end)}` : ""}] ${item.text}`;
  function firstUntimed() {
    const index = phrases.findIndex(item => item.start === null || item.start === undefined);
    return index < 0 ? phrases.length : index;
  }
  function startedPhraseIndex() {
    let active = -1;
    phrases.forEach((item, index) => {
      if (item.start !== null && item.start !== undefined && Number(item.start) <= video.currentTime + .001) active = index;
    });
    return active;
  }
  function playbackPhraseIndexes(time = video.currentTime) {
    const current = Number(time);
    return phrases.reduce((active, item, index) => {
      if (item.start === null || item.start === undefined || Number(item.start) > current + .001) return active;
      const boundary = item.end_explicit ? Number(item.end) : nearestNextStart(index);
      if (boundary === null || current < boundary - .001) active.push(index);
      return active;
    }, []);
  }
  function targetPhraseIndex() {
    if (markingTarget !== null && phrases[markingTarget]
      && (phrases[markingTarget].start === null || phrases[markingTarget].start === undefined)) {
      return markingTarget;
    }
    return Math.min(startedPhraseIndex() + 1, phrases.length);
  }
  function snapshot() {
    undoStack.push({ phrases: clone(phrases), reviewItems: clone(reviewItems), markingTarget, selectedIndex });
    if (undoStack.length > 100) undoStack.shift();
    redoStack = [];
  }
  function renumber() {
    phrases.forEach((item, index) => { item.id = index + 1; });
  }
  function nearestNextStart(index) {
    for (let next = index + 1; next < phrases.length; next += 1) {
      if (phrases[next].start !== null && phrases[next].start !== undefined) return Number(phrases[next].start);
    }
    return null;
  }
  function phraseStatus(item, index) {
    if (item.start === null || item.start === undefined) return null;
    const estimate = estimatedDuration(item.text);
    const boundary = item.end_explicit ? Number(item.end) : nearestNextStart(index);
    if (boundary === null) return { kind: "waiting", text: `${labels.labelEstimate} ${estimate.toFixed(1)} ${labels.labelSeconds}` };
    const available = boundary - Number(item.start);
    const difference = available - estimate;
    if (difference >= .45) return { kind: "fits", text: `+${difference.toFixed(1)} ${labels.labelSeconds}` };
    if (difference >= 0) return { kind: "tight", text: labels.labelTight };
    return { kind: "overflow", text: `${labels.labelShortage} ${Math.abs(difference).toFixed(1)} ${labels.labelSeconds}` };
  }

  function replayStartBeforePhrase(index) {
    let anchorIndex = index - 1;
    while (anchorIndex >= 0 && (phrases[anchorIndex].start === null || phrases[anchorIndex].start === undefined)) {
      anchorIndex -= 1;
    }
    if (anchorIndex < 0) return 0;
    let estimatedBoundary = Number(phrases[anchorIndex].start);
    for (let phraseIndex = anchorIndex; phraseIndex < index; phraseIndex += 1) {
      estimatedBoundary += estimatedDuration(phrases[phraseIndex].text);
    }
    return Math.max(0, estimatedBoundary - 1.5);
  }

  function scrollPhraseInsideList(index, block = "center", behavior = "smooth") {
    const row = list.querySelector(`.timing-phrase-row[data-index="${index}"]`);
    if (!row) return;
    const listRect = list.getBoundingClientRect();
    const rowRect = row.getBoundingClientRect();
    if (block === "nearest" && rowRect.top >= listRect.top && rowRect.bottom <= listRect.bottom) return;
    let delta;
    if (block === "nearest") {
      delta = rowRect.top < listRect.top ? rowRect.top - listRect.top : rowRect.bottom - listRect.bottom;
    } else {
      delta = (rowRect.top + rowRect.height / 2) - (listRect.top + listRect.height / 2);
    }
    list.scrollTo({ top: list.scrollTop + delta, behavior });
  }

  function render() {
    renumber();
    const playback = new Set(playbackPhraseIndexes());
    const target = targetPhraseIndex();
    list.replaceChildren();
    phrases.forEach((item, index) => {
      const row = document.createElement("article");
      row.className = "timing-phrase-row";
      row.dataset.index = index;
      if (playback.has(index)) row.classList.add("playback-current");
      if (!automaticMode && index === target) row.classList.add("marking-current");
      if (index === selectedIndex) row.classList.add("selected-phrase");
      if (item.start !== null && item.start !== undefined) row.classList.add("timed");
      if (mode === "structure" && index === structure.index) row.classList.add("structure-active");

      const number = document.createElement("span");
      number.className = "timing-phrase-number";
      number.textContent = String(index + 1).padStart(2, "0");
      const body = document.createElement("div");
      body.className = "timing-phrase-body";
      const line = document.createElement("div");
      line.className = "timing-phrase-line";
      const copy = document.createElement("span");
      copy.className = "timing-phrase-copy";
      const itemWords = words(item.text);
      let totalWordWeight = 0;
      itemWords.forEach((word, wordIndex) => {
        const wordNode = document.createElement("span");
        wordNode.className = "timing-phrase-word";
        wordNode.textContent = word;
        wordNode.dataset.word = wordIndex;
        const wordWeight = Math.max(1, (word.match(/[\p{L}\p{N}]/gu) || []).length);
        wordNode.dataset.weight = wordWeight;
        totalWordWeight += wordWeight;
        copy.append(wordNode);
        if (wordIndex < itemWords.length - 1) {
          copy.append(document.createTextNode(" "));
          const boundary = document.createElement("button");
          boundary.type = "button";
          boundary.className = "timing-structure-marker split-marker";
          boundary.dataset.boundary = wordIndex + 1;
          boundary.setAttribute("aria-label", labels.labelSplitHere);
          const splitHint = document.createElement("span");
          splitHint.className = "timing-marker-tooltip";
          splitHint.textContent = labels.labelSplitHere;
          boundary.append(splitHint);
          if (mode === "structure" && structure.index === index && structure.boundary === wordIndex + 1) boundary.classList.add("keyboard-active");
          boundary.addEventListener("click", event => {
            event.stopPropagation();
            splitPhrase(index, wordIndex + 1);
          });
          copy.append(boundary);
        }
      });
      copy.dataset.totalWeight = totalWordWeight;
      line.append(copy);

      const meta = document.createElement("div");
      meta.className = "timing-phrase-meta";
      if (item.start !== null && item.start !== undefined) {
        const timeGroup = document.createElement("span");
        timeGroup.className = "timing-phrase-time-group";
        const startGroup = document.createElement("span");
        startGroup.className = "timing-boundary-group";
        const time = document.createElement("button");
        time.type = "button";
        time.className = "timing-phrase-time";
        time.textContent = clock(item.start);
        time.dataset.uiTooltip = labels.labelTimecodeHint || "";
        time.setAttribute("aria-label", `${labels.labelGoToTimecode} ${clock(item.start)}`);
        time.addEventListener("click", event => {
          event.stopPropagation();
          handleTimecodeClick(index, Number(item.start));
        });
        const remove = document.createElement("button");
        remove.type = "button";
        remove.className = "timing-remove-timecode";
        remove.textContent = "×";
        remove.dataset.tooltip = labels.labelRemoveTimecode;
        remove.setAttribute("aria-label", labels.labelRemoveTimecode);
        remove.addEventListener("click", event => {
          event.stopPropagation();
          removeTimecode(index);
        });
        startGroup.append(time, remove);
        timeGroup.append(startGroup);
        if (item.end_explicit) {
          const arrow = document.createElement("span");
          arrow.className = "timing-boundary-arrow";
          arrow.setAttribute("aria-hidden", "true");
          arrow.innerHTML = '<svg viewBox="0 0 24 24"><path d="M5 12h14M14 7l5 5-5 5"/></svg>';
          const endGroup = document.createElement("span");
          endGroup.className = "timing-boundary-group";
          const endTime = document.createElement("button");
          endTime.type = "button";
          endTime.className = "timing-phrase-time timing-phrase-end";
          endTime.textContent = clock(item.end);
          endTime.dataset.uiTooltip = labels.labelEndTimecodeHint || labels.labelTimecodeHint || "";
          endTime.setAttribute("aria-label", `${labels.labelGoToTimecode} ${clock(item.end)}`);
          endTime.addEventListener("click", event => {
            event.stopPropagation();
            handleTimecodeClick(index, Number(item.end));
          });
          const removeEnd = document.createElement("button");
          removeEnd.type = "button";
          removeEnd.className = "timing-remove-timecode";
          removeEnd.textContent = "×";
          removeEnd.dataset.tooltip = labels.labelRemoveEndTimecode;
          removeEnd.setAttribute("aria-label", labels.labelRemoveEndTimecode);
          removeEnd.addEventListener("click", event => {
            event.stopPropagation();
            removeEndTimecode(index);
          });
          endGroup.append(endTime, removeEnd);
          timeGroup.append(arrow, endGroup);
        }
        meta.append(timeGroup);
        const durationBoundary = item.end_explicit ? Number(item.end) : nearestNextStart(index);
        if (Number.isFinite(durationBoundary) && durationBoundary > Number(item.start)) {
          const duration = document.createElement("span");
          duration.className = "timing-phrase-duration";
          duration.textContent = `${labels.labelDuration} ${(durationBoundary - Number(item.start)).toFixed(1)} ${labels.labelSeconds}`;
          meta.append(duration);
        }
      }
      const status = phraseStatus(item, index);
      if (status) {
        const badge = document.createElement("em");
        badge.className = status.kind;
        badge.textContent = status.text;
        badge.dataset.uiTooltip = labels.labelDurationHint || "";
        meta.append(badge);
      }
      if ((item.issues || []).includes("low_confidence")) {
        const warning = document.createElement("em");
        warning.className = "recognition-warning";
        warning.textContent = labels.labelLowConfidence;
        meta.append(warning);
      }
      body.append(line);
      if (meta.childElementCount) body.append(meta);
      const timingActions = document.createElement("div");
      timingActions.className = "timing-card-actions";
      const startAction = document.createElement("button");
      startAction.type = "button";
      startAction.className = "timing-card-boundary timing-card-start";
      startAction.innerHTML = `<span aria-hidden="true">I</span><b>${labels.labelStartShort || "Start"}</b>`;
      startAction.title = labels.labelSetStart || "Set start to current time";
      startAction.addEventListener("click", event => {
        event.stopPropagation();
        selectedIndex = index;
        markCurrent();
      });
      const endAction = document.createElement("button");
      endAction.type = "button";
      endAction.className = "timing-card-boundary timing-card-end";
      endAction.innerHTML = `<span aria-hidden="true">O</span><b>${labels.labelEndShort || "End"}</b>`;
      endAction.title = labels.labelSetEnd || "Set end to current time";
      endAction.dataset.uiTooltip = labels.labelSetEndHint || "";
      endAction.disabled = item.start === null || item.start === undefined;
      endAction.addEventListener("click", event => {
        event.stopPropagation();
        selectedIndex = index;
        markEndCurrent();
      });
      timingActions.append(startAction, endAction);
      body.append(timingActions);
      row.append(number, body);
      row.classList.add("has-phrase-editor");
      const editPhrase = document.createElement("button");
      editPhrase.type = "button";
      editPhrase.className = "timing-edit-phrase";
      editPhrase.innerHTML = '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M4 20l1.5-5.2L15.9 4.4a2.1 2.1 0 0 1 3 0l.7.7a2.1 2.1 0 0 1 0 3L9.2 18.5 4 20zM14 6.3l3.7 3.7M5.5 14.8l3.7 3.7"/></svg>';
      editPhrase.setAttribute("aria-label", labels.labelEditPhrase || "Edit phrase and timecodes");
      editPhrase.dataset.tooltip = labels.labelEditPhrase || "Edit phrase and timecodes";
      editPhrase.addEventListener("click", event => {
        event.stopPropagation();
        openPhraseEditor(index);
      });
      row.append(editPhrase);
      row.addEventListener("dblclick", event => {
        if (event.target.closest(".timing-structure-marker")) return;
        const wordNode = event.target.closest(".timing-phrase-word");
        let offset = item.text.length;
        if (wordNode) {
          const wordIndex = Number(wordNode.dataset.word);
          const clickedWord = wordNode.textContent || "";
          const wordStart = itemWords.slice(0, wordIndex).join(" ").length + (wordIndex ? 1 : 0);
          const caret = document.caretPositionFromPoint?.(event.clientX, event.clientY);
          const caretOffset = caret && wordNode.contains(caret.offsetNode)
            ? Math.max(0, Math.min(clickedWord.length, caret.offset))
            : Math.round(clickedWord.length * Math.max(0, Math.min(1, (event.clientX - wordNode.getBoundingClientRect().left) / wordNode.getBoundingClientRect().width)));
          offset = wordStart + caretOffset;
        } else if (event.clientX <= copy.getBoundingClientRect().left) {
          offset = 0;
        }
        const desiredViewportY = event.clientY - list.getBoundingClientRect().top;
        startEditing(index, offset, desiredViewportY);
      });
      row.addEventListener("click", event => {
        if (event.target.closest("button, .timing-structure-marker")) return;
        if (quickReviewState) {
          pauseQuickReview(false);
          quickReviewState.resumeAfterIndex = null;
        }
        selectedIndex = selectedIndex === index ? null : index;
        render();
      });
      list.append(row);
      if (index < phrases.length - 1) {
        const insertion = document.createElement("div");
        insertion.className = "timing-insert-phrase";

        const mergeButton = document.createElement("button");
        mergeButton.type = "button";
        mergeButton.className = "timing-between-merge";
        mergeButton.setAttribute("aria-label", labels.labelMergePhrases || labels.labelMergePhrase);
        mergeButton.insertAdjacentHTML("beforeend", '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M8 5l4 4 4-4M12 9V3M8 19l4-4 4 4M12 15v6"/></svg>');
        const mergeCopy = document.createElement("span");
        mergeCopy.textContent = labels.labelMergePhrases || labels.labelMergePhrase || "Merge phrases";
        mergeButton.append(mergeCopy);
        if (mode === "structure" && structureBetween?.index === index && structureBetween.action === "merge") {
          mergeButton.classList.add("keyboard-active");
        }
        mergeButton.addEventListener("click", () => mergePhrase(index));

        const insertionButton = document.createElement("button");
        insertionButton.type = "button";
        insertionButton.className = "timing-between-add";
        insertionButton.insertAdjacentHTML("beforeend", '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M12 5v14M5 12h14"/></svg>');
        const insertionCopy = document.createElement("span");
        insertionCopy.textContent = labels.labelAddPhrase || "Add phrase";
        insertionButton.append(insertionCopy);
        if (mode === "structure" && structureBetween?.index === index && structureBetween.action === "add") {
          insertionButton.classList.add("keyboard-active");
        }
        insertionButton.addEventListener("click", () => openAddPhraseDialog(index + 1));
        insertion.append(mergeButton, insertionButton);
        list.append(insertion);
      }
    });
    renderReviewItems();
    updateInterface();
    updateKaraoke();
  }

  function splitPhrase(index, boundary) {
    const itemWords = words(phrases[index].text);
    if (boundary <= 0 || boundary >= itemWords.length) return;
    snapshot();
    const anchors = Array.isArray(phrases[index].words) && phrases[index].words.length === itemWords.length ? phrases[index].words : null;
    const left = { ...phrases[index], text: itemWords.slice(0, boundary).join(" ") };
    const right = { id: 0, text: itemWords.slice(boundary).join(" "), start: null, end: null, end_explicit: false };
    if (anchors) {
      left.words = anchors.slice(0, boundary);
      left.end = left.words.at(-1).end;
      left.end_explicit = true;
      right.words = anchors.slice(boundary);
      right.start = right.words[0].start;
      right.end = phrases[index].end;
      right.end_explicit = true;
      right.issues = phrases[index].issues || [];
      right.source = phrases[index].source;
    } else {
      left.end = null;
      left.end_explicit = false;
    }
    phrases.splice(index, 1, left, right);
    markingTarget = null;
    structure = { index, boundary: words(left.text).length };
    structureBetween = null;
    render();
    queueSave();
  }

  function mergePhrase(index) {
    if (index < 0 || index >= phrases.length - 1) return;
    snapshot();
    const boundary = words(phrases[index].text).length;
    const next = phrases[index + 1];
    phrases[index] = {
      ...phrases[index],
      text: `${phrases[index].text.trim()} ${next.text.trim()}`.trim(),
      end: next.end_explicit ? next.end : null,
      end_explicit: Boolean(next.end_explicit),
      words: [...(phrases[index].words || []), ...(next.words || [])],
      issues: [...new Set([...(phrases[index].issues || []), ...(next.issues || [])])],
    };
    phrases.splice(index + 1, 1);
    if (selectedIndex === index + 1) selectedIndex = index;
    else if (selectedIndex !== null && selectedIndex > index + 1) selectedIndex -= 1;
    markingTarget = null;
    structure = { index, boundary };
    structureBetween = null;
    render();
    queueSave();
  }

  function setMode(next) {
    if (next === "structure" || next === "edit") video.pause();
    mode = next;
    if (mode === "structure") {
      structure.index = Math.min(firstUntimed(), phrases.length - 1);
      structure.boundary = Math.min(1, Math.max(1, words(phrases[structure.index]?.text || "").length));
      structureBetween = null;
    }
    render();
    if (mode === "structure") requestAnimationFrame(() => root.focus({ preventScroll: true }));
  }

  function scrollEditorToCaret(globalOffset, desiredViewportY) {
    const style = getComputedStyle(textarea);
    const mirror = document.createElement("div");
    Object.assign(mirror.style, {
      position: "fixed",
      left: "-100000px",
      top: "0",
      visibility: "hidden",
      width: `${textarea.clientWidth}px`,
      boxSizing: style.boxSizing,
      padding: style.padding,
      border: style.border,
      font: style.font,
      lineHeight: style.lineHeight,
      letterSpacing: style.letterSpacing,
      whiteSpace: "pre-wrap",
      overflowWrap: style.overflowWrap,
      wordBreak: style.wordBreak,
    });
    mirror.append(document.createTextNode(textarea.value.slice(0, globalOffset)));
    const marker = document.createElement("span");
    marker.textContent = "\u200b";
    mirror.append(marker, document.createTextNode(textarea.value.slice(globalOffset)));
    document.body.append(mirror);
    const targetY = Number.isFinite(desiredViewportY)
      ? Math.max(24, Math.min(textarea.clientHeight - 24, desiredViewportY))
      : textarea.clientHeight / 2;
    textarea.scrollTop = Math.max(0, marker.offsetTop - targetY + parseFloat(style.paddingTop || 0));
    mirror.remove();
  }

  function startEditing(index = Math.min(firstUntimed(), phrases.length - 1), localOffset = 0, desiredViewportY = null) {
    video.pause();
    modeBeforeEdit = mode === "edit" ? "structure" : mode;
    mode = "edit";
    const lines = phrases.map(editableLine);
    textarea.value = lines.join("\n");
    list.hidden = true;
    editBox.hidden = false;
    const currentPrefixLength = phrases[index]?.start === null || phrases[index]?.start === undefined
      ? 0
      : clock(phrases[index].start).length + 3;
    const globalOffset = lines.slice(0, index).reduce((sum, line) => sum + line.length + 1, 0) + currentPrefixLength + localOffset;
    textarea.focus();
    textarea.setSelectionRange(globalOffset, globalOffset);
    requestAnimationFrame(() => scrollEditorToCaret(globalOffset, desiredViewportY));
    updateInterface();
  }

  async function finishEditing() {
    const value = textarea.value.trim();
    if (!value) return;
    setSaveState("saving");
    await saveNow();
    try {
      const response = await fetch(root.dataset.resegmentUrl, {
        method: "POST",
        headers: { "Content-Type": "application/json", "X-CSRFToken": csrf },
        body: JSON.stringify({
          text: value,
          duration: Number.isFinite(video.duration) ? video.duration : null,
        }),
      });
      const data = await response.json();
      if (!response.ok || !data.ok) throw new Error(data.error || "Could not update text");
      snapshot();
      phrases = data.phrases;
      editBox.hidden = true;
      list.hidden = false;
      mode = modeBeforeEdit === "structure" ? "structure" : "video";
      structure = { index: firstUntimed(), boundary: 1 };
      render();
      setSaveState("saved");
    } catch (error) {
      setSaveState("error");
    }
  }

  function cancelEditing() {
    editBox.hidden = true;
    list.hidden = false;
    mode = modeBeforeEdit === "structure" ? "structure" : "video";
    render();
  }

  function markCurrent() {
    if (!allowTimingShortcut()) return;
    const keepSelection = selectedIndex !== null;
    const index = selectedIndex !== null ? selectedIndex : targetPhraseIndex();
    if (index < 0 || index >= phrases.length || !phrases[index]) return;
    const previous = [...phrases.slice(0, index)].reverse().find(item => item.start !== null && item.start !== undefined);
    const next = phrases.slice(index + 1).find(item => item.start !== null && item.start !== undefined);
    if ((previous && video.currentTime < Number(previous.start)) || (next && video.currentTime > Number(next.start))
      || (phrases[index].end_explicit && video.currentTime >= Number(phrases[index].end))) {
      document.getElementById("timing-fit-summary").textContent = labels.labelOrder;
      return;
    }
    snapshot();
    phrases[index].start = Math.round(video.currentTime * 1000) / 1000;
    if (!phrases[index].end_explicit) phrases[index].end = null;
    markingTarget = null;
    structure = { index: Math.min(index + 1, phrases.length - 1), boundary: 1 };
    render();
    queueSave();
    scrollPhraseInsideList(keepSelection ? index : Math.min(index + 1, phrases.length - 1));
  }

  function markEndCurrent() {
    if (!allowTimingShortcut()) return;
    const active = playbackPhraseIndexes();
    const index = selectedIndex !== null ? selectedIndex : (active[0] ?? startedPhraseIndex());
    if (index < 0 || !phrases[index]) return;
    const end = Math.round(video.currentTime * 1000) / 1000;
    if (end <= Number(phrases[index].start)
      || (Number.isFinite(video.duration) && end > video.duration)) {
      document.getElementById("timing-fit-summary").textContent = labels.labelEndOrder;
      return;
    }
    snapshot();
    phrases[index].end = end;
    phrases[index].end_explicit = true;
    render();
    queueSave();
  }

  function allowTimingShortcut() {
    if (selectedIndex !== null || playbackPhraseIndexes().length < 2) return true;
    const warning = document.getElementById("timing-overlap-warning");
    warning.hidden = false;
    warning.classList.remove("visible");
    requestAnimationFrame(() => warning.classList.add("visible"));
    clearTimeout(overlapWarningTimer);
    clearTimeout(overlapWarningHideTimer);
    overlapWarningTimer = setTimeout(() => {
      warning.classList.remove("visible");
      overlapWarningHideTimer = setTimeout(() => { warning.hidden = true; }, 180);
    }, 5200);
    return false;
  }

  function removeEndTimecode(index) {
    const item = phrases[index];
    if (!item?.end_explicit) return;
    snapshot();
    item.end = null;
    item.end_explicit = false;
    render();
    queueSave();
  }

  function removeTimecode(index) {
    const item = phrases[index];
    if (!item || item.start === null || item.start === undefined) return;
    const replayStart = replayStartBeforePhrase(index);
    snapshot();
    item.start = null;
    item.end = null;
    item.end_explicit = false;
    markingTarget = index;
    structure = { index, boundary: 1 };
    video.pause();
    applySeekTarget(replayStart);
    render();
    queueSave();
  }

  function removeLastMark() {
    let index = -1;
    phrases.forEach((item, itemIndex) => {
      if (item.start !== null && item.start !== undefined) index = itemIndex;
    });
    if (index < 0) return;
    snapshot();
    phrases[index].start = null;
    phrases[index].end = null;
    phrases[index].end_explicit = false;
    markingTarget = index;
    video.currentTime = Math.max(0, video.currentTime - 1);
    render();
    queueSave();
  }

  function undo() {
    if (!undoStack.length) return;
    redoStack.push({ phrases: clone(phrases), reviewItems: clone(reviewItems), markingTarget, selectedIndex });
    const state = undoStack.pop();
    phrases = state.phrases;
    markingTarget = state.markingTarget;
    reviewItems = state.reviewItems || reviewItems;
    selectedIndex = state.selectedIndex ?? null;
    structure.index = Math.min(structure.index, phrases.length - 1);
    render();
    queueSave();
  }
  function redo() {
    if (!redoStack.length) return;
    undoStack.push({ phrases: clone(phrases), reviewItems: clone(reviewItems), markingTarget, selectedIndex });
    const state = redoStack.pop();
    phrases = state.phrases;
    markingTarget = state.markingTarget;
    reviewItems = state.reviewItems || reviewItems;
    selectedIndex = state.selectedIndex ?? null;
    structure.index = Math.min(structure.index, phrases.length - 1);
    render();
    queueSave();
  }

  function renderReviewItems() {
    const section = document.getElementById("timing-review-items");
    const container = document.getElementById("timing-review-list");
    if (!section || !container) return;
    const unresolved = reviewItems.filter(item => !item.resolved);
    section.hidden = !unresolved.length;
    container.replaceChildren();
    unresolved.forEach(item => {
      const row = document.createElement("article");
      row.className = "timing-review-item";
      const copy = document.createElement("div");
      copy.innerHTML = `<strong>${labels.labelUnrecognizedSpeech || "Possible unrecognized speech"}</strong><span>${clock(item.start)} → ${clock(item.end)}</span>`;
      const listen = document.createElement("button");
      listen.type = "button";
      listen.textContent = labels.labelListen || "Listen";
      listen.addEventListener("click", () => playExactRange(Number(item.start), Number(item.end)));
      const add = document.createElement("button");
      add.type = "button";
      add.textContent = labels.labelAddPhrase || "Add phrase";
      add.addEventListener("click", () => {
        pendingReviewItem = item;
        pendingInsertIndex = null;
        document.getElementById("timing-missing-text").value = "";
        document.getElementById("timing-add-phrase").showModal();
        setTimeout(() => document.getElementById("timing-missing-text").focus(), 0);
      });
      row.append(copy, listen, add);
      container.append(row);
    });
  }

  function openAddPhraseDialog(insertIndex) {
    if (quickReviewState) pauseQuickReview(false);
    else video.pause();
    pendingReviewItem = null;
    pendingInsertIndex = insertIndex;
    structureBetween = null;
    document.getElementById("timing-missing-text").value = "";
    document.getElementById("timing-add-phrase").showModal();
    setTimeout(() => document.getElementById("timing-missing-text").focus(), 0);
  }

  function openPhraseEditor(index) {
    const item = phrases[index];
    if (!item) return;
    if (quickReviewState) pauseQuickReview(false);
    else video.pause();
    phraseEditIndex = index;
    document.getElementById("timing-phrase-edit-text").value = item.text;
    document.getElementById("timing-phrase-edit-start").value = item.start === null || item.start === undefined ? "" : clock(item.start);
    document.getElementById("timing-phrase-edit-end").value = item.end_explicit && item.end !== null && item.end !== undefined ? clock(item.end) : "";
    const error = document.getElementById("timing-phrase-edit-error");
    error.hidden = true;
    error.textContent = "";
    document.getElementById("timing-phrase-edit-dialog").showModal();
    setTimeout(() => document.getElementById("timing-phrase-edit-text").focus(), 0);
  }

  function phraseEditError(message) {
    const error = document.getElementById("timing-phrase-edit-error");
    error.textContent = message;
    error.hidden = false;
  }

  function playExactRange(start, end, onFinish = null) {
    video.pause();
    const generation = ++reviewSeekGeneration;
    reviewPlayback = { end, onFinish, generation };
    const startWhenReady = () => {
      if (!reviewPlayback || reviewPlayback.generation !== generation) return;
      pendingSeekTarget = null;
      clearTimeout(seekRetryTimer);
      video.play().catch(() => {});
    };
    if (!video.seeking && Math.abs(video.currentTime - start) <= .02) {
      pendingSeekTarget = null;
      clearTimeout(seekRetryTimer);
      requestAnimationFrame(startWhenReady);
    } else {
      video.addEventListener("seeked", startWhenReady, { once: true });
      applySeekTarget(start);
    }
  }

  function stopExactPlayback() {
    video.pause();
    reviewSeekGeneration += 1;
    clearTimeout(seekRetryTimer);
    pendingSeekTarget = null;
    reviewPlayback = null;
  }

  function buildQuickReviewStages() {
    const stages = [];
    phrases.forEach((item, index) => {
      if (item.start === null || item.start === undefined || item.end === null || item.end === undefined) return;
      const start = Number(item.start), end = Number(item.end), duration = end - start;
      if (duration <= 2) stages.push({ index, start, end });
      else stages.push(
        { index, start, end: Math.min(end, start + 1) },
        { index, start: Math.max(start, end - 1), end },
      );
    });
    reviewItems.filter(item => !item.resolved).forEach(item => stages.push({ index: null, start: Number(item.start), end: Number(item.end) }));
    stages.sort((left, right) => left.start - right.start || (left.index === null ? 1 : -1));
    return stages;
  }

  function updateQuickReviewControls() {
    const controls = document.getElementById("timing-review-controls");
    const start = document.querySelector("[data-timing-review]");
    if (!controls || !start) return;
    const active = Boolean(quickReviewState);
    controls.hidden = !active;
    start.hidden = active;
    document.getElementById("timing-review-pause").disabled = !active || quickReviewState.paused;
    document.getElementById("timing-review-continue").disabled = !active || !quickReviewState.paused;
  }

  function playNextQuickReviewStage() {
    const state = quickReviewState;
    if (!state || state.paused) return;
    if (state.position >= state.stages.length) {
      exitQuickReview(false);
      return;
    }
    const stage = state.stages[state.position++];
    const playToken = ++state.playToken;
    state.currentIndex = stage.index;
    selectedIndex = stage.index;
    render();
    if (stage.index !== null) {
      scrollPhraseInsideList(stage.index, "center", "auto");
    }
    playExactRange(stage.start, stage.end, () => {
      if (quickReviewState === state && !state.paused && state.playToken === playToken) playNextQuickReviewStage();
    });
  }

  function startQuickReview() {
    const stages = buildQuickReviewStages();
    if (!stages.length) return;
    const selectedStart = selectedIndex === null ? -1 : stages.findIndex(stage => stage.index === selectedIndex);
    quickReviewState = {
      stages,
      position: selectedStart >= 0 ? selectedStart : 0,
      currentIndex: selectedIndex,
      paused: false,
      resumeAfterIndex: null,
      playToken: 0,
    };
    updateQuickReviewControls();
    playNextQuickReviewStage();
  }

  function pauseQuickReview(shouldRender = true) {
    if (!quickReviewState) return;
    quickReviewState.paused = true;
    quickReviewState.playToken += 1;
    stopExactPlayback();
    updateQuickReviewControls();
    if (shouldRender) render();
  }

  function continueQuickReview() {
    const state = quickReviewState;
    if (!state) return;
    stopExactPlayback();
    let position = -1;
    if (state.resumeAfterIndex !== null) {
      position = state.stages.findIndex(stage => stage.index !== null && stage.index > state.resumeAfterIndex);
    } else if (selectedIndex !== null) {
      position = state.stages.findIndex(stage => stage.index === selectedIndex);
    }
    if (position >= 0) state.position = position;
    else if (state.resumeAfterIndex !== null) {
      exitQuickReview(false);
      return;
    }
    state.resumeAfterIndex = null;
    state.paused = false;
    updateQuickReviewControls();
    playNextQuickReviewStage();
  }

  function exitQuickReview(clearSelection = true) {
    stopExactPlayback();
    quickReviewState = null;
    if (clearSelection) selectedIndex = null;
    updateQuickReviewControls();
    render();
  }

  function handleTimecodeClick(index, fallbackTime) {
    if (!quickReviewState) {
      applySeekTarget(fallbackTime);
      return;
    }
    pauseQuickReview(false);
    selectedIndex = index;
    quickReviewState.currentIndex = index;
    quickReviewState.resumeAfterIndex = index;
    render();
    const item = phrases[index];
    const start = Number(item.start);
    const inferredEnd = nearestNextStart(index) ?? (start + estimatedDuration(item.text));
    const end = item.end !== null && item.end !== undefined ? Number(item.end) : inferredEnd;
    playExactRange(start, Math.max(start + .05, end), () => {
      video.pause();
      updateQuickReviewControls();
    });
  }

  function setSaveState(state) {
    const box = document.getElementById("timing-save-state");
    box.className = `timing-save-state ${state}`;
    box.querySelector("span").textContent = state === "saving" ? labels.labelSaving : state === "error" ? labels.labelError : labels.labelSaved;
  }
  function queueSave() {
    clearTimeout(saveTimer);
    setSaveState("saving");
    saveTimer = setTimeout(saveNow, 350);
  }
  function saveNow() {
    clearTimeout(saveTimer);
    const payload = clone(phrases);
    const duration = Number.isFinite(video.duration) ? video.duration : null;
    saveQueue = saveQueue.then(async () => {
      const response = await fetch(root.dataset.saveUrl, {
        method: "POST",
        headers: { "Content-Type": "application/json", "X-CSRFToken": csrf },
        body: JSON.stringify({ phrases: payload, review_items: clone(reviewItems), duration }),
      });
      const data = await response.json();
      if (!response.ok || !data.ok) throw new Error(data.error || "Could not save");
      setSaveState("saved");
      return data;
    }).catch(() => { setSaveState("error"); });
    return saveQueue;
  }

  function updateInterface() {
    const marked = phrases.filter(item => item.start !== null && item.start !== undefined).length;
    document.getElementById("timing-progress-copy").textContent = `${marked} / ${phrases.length}`;
    document.getElementById("timing-progress-bar").style.width = `${phrases.length ? marked / phrases.length * 100 : 0}%`;
    document.getElementById("timing-undo").disabled = !undoStack.length;
    document.getElementById("timing-redo").disabled = !redoStack.length;
    const badge = document.getElementById("timing-mode-badge");
    const shortcuts = document.getElementById("timing-shortcut-copy");
    const textModeNotice = document.getElementById("timing-text-mode-notice");
    const textColumn = document.querySelector(".timing-text-column");
    textModeNotice.hidden = mode !== "structure";
    textColumn.classList.toggle("text-mode-active", mode === "structure");
    if (mode === "video") {
      badge.textContent = labels.labelVideo;
      shortcuts.textContent = labels.shortcutsVideo;
    } else if (mode === "structure") {
      badge.textContent = labels.labelText;
      const itemWords = words(phrases[structure.index]?.text || "");
      const action = structureBetween
        ? (structureBetween.action === "add" ? labels.labelAddPhrase : labels.labelMergePhrases)
        : (structure.boundary >= itemWords.length ? labels.labelMerge : labels.labelSplit);
      shortcuts.textContent = `${labels.shortcutsText} · ${action} · E — ${labels.labelEdit.toLowerCase()}`;
    } else {
      badge.textContent = labels.labelEdit;
      shortcuts.textContent = labels.shortcutsEdit;
    }
  }

  function updateKaraoke() {
    document.getElementById("timing-current-time").textContent = clock(video.currentTime);
    document.getElementById("timing-duration").textContent = clock(video.duration || 0);
    const playback = new Set(playbackPhraseIndexes());
    const target = targetPhraseIndex();
    document.querySelectorAll(".timing-phrase-row").forEach(row => {
      const index = Number(row.dataset.index);
      row.classList.toggle("playback-current", playback.has(index));
      row.classList.toggle("marking-current", !automaticMode && index === target);
      const item = phrases[index];
      if (item?.start === null || item?.start === undefined) return;
      const karaokeDuration = item.end_explicit
        ? Math.max(.05, Number(item.end) - Number(item.start))
        : estimatedDuration(item.text);
      const fill = playback.has(index)
        ? Math.max(0, Math.min(1, (video.currentTime - Number(item.start)) / karaokeDuration))
        : 0;
      row.style.setProperty("--karaoke-fill", `${fill * 100}%`);
      const copy = row.querySelector(".timing-phrase-copy");
      let remainingWeight = fill * Number(copy?.dataset.totalWeight || 0);
      copy?.querySelectorAll(".timing-phrase-word").forEach(wordNode => {
        const weight = Number(wordNode.dataset.weight || 1);
        const wordFill = Math.max(0, Math.min(1, remainingWeight / weight));
        wordNode.style.setProperty("--word-fill", `${wordFill * 100}%`);
        remainingWeight -= weight;
      });
    });
    document.getElementById("timing-video-toggle").classList.toggle("playing", !video.paused);
    document.getElementById("timing-video-toggle").querySelector("span").textContent = video.paused ? "▶" : "Ⅱ";
    if (Number.isFinite(video.duration) && video.duration > 0) videoSeek.max = String(video.duration);
    if (!draggingVideoSeek) {
      const displayedPosition = pendingSeekTarget ?? (Number.isFinite(video.currentTime) ? video.currentTime : 0);
      videoSeek.value = String(displayedPosition);
    }
  }

  function animationTick() {
    updateKaraoke();
    if (reviewPlayback && !video.paused && video.currentTime >= reviewPlayback.end - .015) {
      const finish = reviewPlayback.onFinish;
      video.pause();
      reviewPlayback = null;
      if (finish) setTimeout(finish, 120);
    }
    requestAnimationFrame(animationTick);
  }

  function toggleVideo() {
    if (video.paused) video.play().catch(() => {}); else video.pause();
  }
  function clampSeekTarget(value) {
    const current = Number.isFinite(value) ? value : 0;
    const duration = Number.isFinite(video.duration) && video.duration > 0 ? video.duration : null;
    return Math.max(0, duration === null ? current : Math.min(duration, current));
  }
  function applySeekTarget(value, attempt = 0) {
    const target = clampSeekTarget(value);
    pendingSeekTarget = target;
    videoSeek.value = String(target);
    if (video.readyState === HTMLMediaElement.HAVE_NOTHING) {
      video.load();
    } else {
      try { video.currentTime = target; } catch (_error) { /* Retry when more media data is available. */ }
    }
    clearTimeout(seekRetryTimer);
    seekRetryTimer = setTimeout(() => {
      if (Math.abs(video.currentTime - target) <= .02 || attempt >= 12) {
        pendingSeekTarget = null;
        return;
      }
      applySeekTarget(target, attempt + 1);
    }, 100);
  }
  function applySeek(delta) {
    const current = Number.isFinite(video.currentTime) ? video.currentTime : 0;
    applySeekTarget((pendingSeekTarget ?? current) + delta);
  }
  function seek(delta) {
    applySeek(delta);
  }

  const tour = document.getElementById("timing-tour");
  const tourStorageKey = `voiceover-phrase-timing-tour-${automaticMode ? "automatic" : "manual"}-v1`;
  const tourSessionKey = `${tourStorageKey}-session`;
  let tourIndex = 0;
  let tourTarget = null;
  let tourHighlights = [];
  const tourStepDefinitions = tour ? [
    { key: "play", target: ".timing-video-column" },
    { key: "edit", target: "#timing-edit", highlights: "#timing-edit, .timing-phrase-row:first-child .timing-edit-phrase" },
    { key: "timing", target: ".timing-phrase-row:first-child" },
    { key: "fit", target: ".timing-phrase-meta .fits, .timing-phrase-meta .tight, .timing-phrase-meta .overflow" },
    { key: "listen", target: ".timing-phrase-time, .timing-phrase-list" },
    { key: "structure", target: ".timing-insert-phrase, .timing-phrase-list" },
    { key: "history", target: ".timing-toolbar-actions" },
    { key: "help", target: "[data-timing-help]" },
  ] : [];
  let tourSteps = [];

  function tourPreference(storage, key) {
    try { return storage.getItem(key); } catch (_error) { return null; }
  }

  function positionTourCard(target) {
    const card = tour.querySelector(".timing-tour-card");
    const rect = target.getBoundingClientRect();
    const margin = tourSteps[tourIndex]?.key === "fit" ? 32 : 18;
    const width = Math.min(420, window.innerWidth - 32);
    card.style.width = `${width}px`;
    const cardHeight = card.offsetHeight || 330;
    let left = rect.right + margin;
    let top = rect.top + Math.min(24, rect.height / 4);
    if (left + width > window.innerWidth - 16) left = rect.left - width - margin;
    if (left < 16) left = Math.max(16, Math.min(window.innerWidth - width - 16, rect.left));
    if (top + cardHeight > window.innerHeight - 16) top = Math.max(16, window.innerHeight - cardHeight - 16);
    if (tourSteps[tourIndex]?.key === "fit" && tourHighlights.length > 1) {
      const rects = tourHighlights.map(item => item.getBoundingClientRect());
      const bounds = rects.reduce((box, item) => ({
        left: Math.min(box.left, item.left),
        top: Math.min(box.top, item.top),
        right: Math.max(box.right, item.right),
        bottom: Math.max(box.bottom, item.bottom),
      }), { left: Infinity, top: Infinity, right: -Infinity, bottom: -Infinity });
      const clampLeft = value => Math.max(16, Math.min(window.innerWidth - width - 16, value));
      const clampTop = value => Math.max(16, Math.min(window.innerHeight - cardHeight - 16, value));
      const candidates = [
        { left: bounds.right + margin, top: bounds.top },
        { left: bounds.left - width - margin, top: bounds.top },
        { left: bounds.left + (bounds.right - bounds.left - width) / 2, top: bounds.bottom + margin },
        { left: bounds.left + (bounds.right - bounds.left - width) / 2, top: bounds.top - cardHeight - margin },
      ].map(candidate => ({ left: clampLeft(candidate.left), top: clampTop(candidate.top) }));
      const overlap = candidate => rects.reduce((total, item) => {
        const overlapWidth = Math.max(0, Math.min(candidate.left + width, item.right) - Math.max(candidate.left, item.left));
        const overlapHeight = Math.max(0, Math.min(candidate.top + cardHeight, item.bottom) - Math.max(candidate.top, item.top));
        return total + overlapWidth * overlapHeight;
      }, 0);
      ({ left, top } = candidates.sort((a, b) => overlap(a) - overlap(b))[0]);
    }
    card.style.left = `${left}px`;
    card.style.top = `${top}px`;
  }

  function showTourStep() {
    const step = tourSteps[tourIndex];
    if (!step) return;
    tourHighlights.forEach(target => target.classList.remove("timing-tour-highlight"));
    tourTarget = document.querySelector(step.target);
    if (!tourTarget) return;
    if (step.key === "fit") {
      tourHighlights = [".timing-phrase-meta .fits", ".timing-phrase-meta .tight", ".timing-phrase-meta .overflow"]
        .map(selector => document.querySelector(selector))
        .filter(Boolean);
    } else {
      tourHighlights = step.highlights
        ? [...document.querySelectorAll(step.highlights)]
        : [tourTarget];
    }
    tourHighlights.forEach(target => target.classList.add("timing-tour-highlight"));
    tourTarget.scrollIntoView({ block: "nearest", behavior: "smooth" });
    tour.querySelector("#timing-tour-count").textContent = `${tourIndex + 1} / ${tourSteps.length}`;
    const title = tour.dataset[`${step.key}Title`];
    tour.querySelector("#timing-tour-title").textContent = title;
    tour.querySelector("#timing-tour-copy").textContent = tour.dataset[`${step.key}Copy`];
    tour.querySelector("#timing-tour-keys").textContent = tour.dataset[`${step.key}Keys`] || "";
    tour.querySelector("[data-tour-prev]").disabled = tourIndex === 0;
    tour.querySelector("[data-tour-next]").textContent = tourIndex === tourSteps.length - 1
      ? (document.documentElement.lang === "ru" ? "Готово" : "Done")
      : (document.documentElement.lang === "ru" ? "Далее" : "Next");
    setTimeout(() => positionTourCard(tourTarget), 180);
  }

  function openTour(force = false) {
    if (!tour || (!force && (tourPreference(localStorage, tourStorageKey) || tourPreference(sessionStorage, tourSessionKey)))) return;
    tourSteps = tourStepDefinitions.filter(step => document.querySelector(step.target));
    if (!tourSteps.length) return;
    document.getElementById("timing-help")?.close();
    tourIndex = 0;
    tour.hidden = false;
    document.body.classList.add("timing-tour-open");
    showTourStep();
  }

  function closeTour(completed = false) {
    if (!tour) return;
    tourHighlights.forEach(target => target.classList.remove("timing-tour-highlight"));
    tourHighlights = [];
    tourTarget = null;
    tour.hidden = true;
    document.body.classList.remove("timing-tour-open");
    try {
      if (completed || document.getElementById("timing-tour-never")?.checked) localStorage.setItem(tourStorageKey, "done");
      else sessionStorage.setItem(tourSessionKey, "closed");
    } catch (_error) { /* Storage can be disabled without breaking the editor. */ }
  }

  tour?.querySelector("[data-tour-next]")?.addEventListener("click", () => {
    if (tourIndex >= tourSteps.length - 1) closeTour(true);
    else { tourIndex += 1; showTourStep(); }
  });
  tour?.querySelector("[data-tour-prev]")?.addEventListener("click", () => {
    if (tourIndex > 0) { tourIndex -= 1; showTourStep(); }
  });
  tour?.querySelector("[data-tour-skip]")?.addEventListener("click", () => closeTour(false));
  tour?.querySelector("[data-tour-close]")?.addEventListener("click", () => closeTour(false));
  document.querySelector("[data-timing-tour-replay]")?.addEventListener("click", () => openTour(true));
  window.addEventListener("resize", () => { if (!tour?.hidden && tourTarget) positionTourCard(tourTarget); });

  root.tabIndex = -1;
  window.addEventListener("keydown", event => {
    if (document.getElementById("timing-phrase-edit-dialog")?.open) return;
    if (event.target === textarea || event.target.closest?.("textarea, select, input:not(#timing-video-seek)")) return;
    const key = event.key.toLowerCase();
    const physicalFallback = { KeyE: "e", KeyI: "i", KeyJ: "j", KeyK: "k", KeyL: "l", KeyO: "o", KeyT: "t", KeyZ: "z" };
    const shortcut = /^[a-z]$/.test(key) ? key : (physicalFallback[event.code] || key);
    if ((event.ctrlKey || event.metaKey) && shortcut === "z") {
      event.preventDefault();
      if (event.shiftKey) redo(); else undo();
      return;
    }
    if (event.key === "?") {
      event.preventDefault();
      document.getElementById("timing-help").showModal();
      return;
    }
    if (mode === "video") {
      if (event.key === "Enter") { event.preventDefault(); markCurrent(); }
      else if (shortcut === "i") { event.preventDefault(); markCurrent(); }
      else if (shortcut === "o") { event.preventDefault(); markEndCurrent(); }
      else if (event.key === " " || shortcut === "k") { event.preventDefault(); toggleVideo(); }
      else if (shortcut === "j") { event.preventDefault(); seek(-10); }
      else if (shortcut === "l") { event.preventDefault(); seek(10); }
      else if (event.key === "ArrowLeft") { event.preventDefault(); seek(event.shiftKey ? -1 : -1 / 25); }
      else if (event.key === "ArrowRight") { event.preventDefault(); seek(event.shiftKey ? 1 : 1 / 25); }
      else if (shortcut === "t") { event.preventDefault(); setMode("structure"); }
      else if (shortcut === "e") { event.preventDefault(); startEditing(); }
      else if (event.key === "Escape" && selectedIndex !== null) { event.preventDefault(); selectedIndex = null; render(); }
      else if (event.key === "Backspace") { event.preventDefault(); removeLastMark(); }
      return;
    }
    if (mode === "structure") {
      const itemWords = words(phrases[structure.index]?.text || "");
      let movedPhrase = false;
      if (event.key === "ArrowLeft") {
        if (structureBetween) structureBetween.action = "merge";
        else structure.boundary = Math.max(1, structure.boundary - 1);
      }
      else if (event.key === "ArrowRight") {
        if (structureBetween) structureBetween.action = "add";
        else structure.boundary = Math.min(itemWords.length, structure.boundary + 1);
      }
      else if (event.key === "ArrowUp") {
        if (structureBetween) {
          structure.index = structureBetween.index;
          structure.boundary = Math.max(1, words(phrases[structure.index]?.text || "").length);
          structureBetween = null;
          movedPhrase = true;
        } else if (structure.index > 0) {
          structureBetween = { index: structure.index - 1, action: "merge" };
          movedPhrase = true;
        }
      }
      else if (event.key === "ArrowDown") {
        if (structureBetween) {
          structure.index = Math.min(phrases.length - 1, structureBetween.index + 1);
          structure.boundary = 1;
          structureBetween = null;
          movedPhrase = true;
        } else if (structure.index < phrases.length - 1) {
          structureBetween = { index: structure.index, action: "merge" };
          movedPhrase = true;
        }
      }
      else if (event.key === "Enter") {
        if (structureBetween?.action === "add") openAddPhraseDialog(structureBetween.index + 1);
        else if (structureBetween?.action === "merge") mergePhrase(structureBetween.index);
        else if (structure.boundary >= itemWords.length) mergePhrase(structure.index);
        else splitPhrase(structure.index, structure.boundary);
      } else if (shortcut === "e") startEditing(structure.index, words(phrases[structure.index].text).slice(0, structure.boundary).join(" ").length + 1);
      else if (event.key === "Escape" || shortcut === "t") setMode("video");
      else return;
      event.preventDefault();
      if (mode === "structure") {
        render();
        if (movedPhrase) scrollPhraseInsideList(structure.index, "nearest", "auto");
      }
    }
  }, true);

  textarea.addEventListener("keydown", event => {
    if (event.key === "Enter" && !event.shiftKey) { event.preventDefault(); finishEditing(); }
    else if (event.key === "Escape") { event.preventDefault(); cancelEditing(); }
  });
  document.getElementById("timing-edit").addEventListener("click", () => startEditing());
  document.getElementById("timing-edit-save").addEventListener("click", finishEditing);
  document.getElementById("timing-edit-cancel").addEventListener("click", cancelEditing);
  document.getElementById("timing-exit-text-mode").addEventListener("click", () => setMode("video"));
  document.querySelector("[data-timing-review]")?.addEventListener("click", startQuickReview);
  document.getElementById("timing-review-pause")?.addEventListener("click", () => pauseQuickReview());
  document.getElementById("timing-review-continue")?.addEventListener("click", continueQuickReview);
  document.getElementById("timing-review-exit")?.addEventListener("click", () => exitQuickReview());
  document.getElementById("timing-add-phrase-save")?.addEventListener("click", event => {
    const text = document.getElementById("timing-missing-text").value.trim();
    if (!text) { event.preventDefault(); return; }
    snapshot();
    const reviewItem = pendingReviewItem;
    const created = {
      id: 0,
      text,
      start: reviewItem ? Number(reviewItem.start) : null,
      end: reviewItem ? Number(reviewItem.end) : null,
      end_explicit: Boolean(reviewItem),
      words: [],
      issues: [],
      source: "manual",
    };
    if (reviewItem) {
      phrases.push(created);
      phrases.sort((left, right) => Number(left.start ?? Infinity) - Number(right.start ?? Infinity));
    } else {
      phrases.splice(Math.max(0, Math.min(phrases.length, pendingInsertIndex ?? phrases.length)), 0, created);
    }
    if (reviewItem) reviewItem.resolved = true;
    pendingReviewItem = null;
    pendingInsertIndex = null;
    selectedIndex = phrases.indexOf(created);
    render();
    scrollPhraseInsideList(selectedIndex);
    queueSave();
  });
  document.getElementById("timing-phrase-edit-form")?.addEventListener("submit", event => {
    event.preventDefault();
    const index = phraseEditIndex;
    const item = phrases[index];
    if (!item) return;
    const text = document.getElementById("timing-phrase-edit-text").value.trim();
    const startValue = document.getElementById("timing-phrase-edit-start").value.trim();
    const endValue = document.getElementById("timing-phrase-edit-end").value.trim();
    const start = startValue ? parseClock(startValue) : null;
    const end = endValue ? parseClock(endValue) : null;
    if (!text) {
      phraseEditError(labels.labelPhraseTextEmpty);
      return;
    }
    if ((startValue && start === null) || (endValue && end === null)) {
      phraseEditError(labels.labelTimecodeFormatError);
      return;
    }
    if (start === null && end !== null) {
      phraseEditError(labels.labelEndRequiresStart);
      return;
    }
    const previous = [...phrases.slice(0, index)].reverse().find(candidate => candidate.start !== null && candidate.start !== undefined);
    const next = phrases.slice(index + 1).find(candidate => candidate.start !== null && candidate.start !== undefined);
    if (start !== null && ((previous && start < Number(previous.start)) || (next && start > Number(next.start)))) {
      phraseEditError(labels.labelOrder);
      return;
    }
    if (end !== null && (end <= start
      || (Number.isFinite(video.duration) && end > video.duration))) {
      phraseEditError(labels.labelEndOrder);
      return;
    }
    selectedIndex = null;
    snapshot();
    const textChanged = text !== item.text;
    item.text = text;
    item.start = start;
    item.end = end;
    item.end_explicit = end !== null;
    item.source = "manual-edit";
    if (textChanged) {
      item.words = [];
      item.issues = (item.issues || []).filter(issue => issue !== "low_confidence");
    }
    phraseEditIndex = null;
    document.getElementById("timing-phrase-edit-dialog").close();
    render();
    scrollPhraseInsideList(index);
    queueSave();
  });
  document.querySelectorAll("[data-phrase-edit-cancel]").forEach(button => {
    button.addEventListener("click", () => {
      phraseEditIndex = null;
      document.getElementById("timing-phrase-edit-dialog").close();
    });
  });
  document.getElementById("timing-phrase-edit-dialog")?.addEventListener("close", () => {
    phraseEditIndex = null;
    selectedIndex = null;
    render();
  });
  document.getElementById("timing-undo").addEventListener("click", undo);
  document.getElementById("timing-redo").addEventListener("click", redo);
  document.getElementById("timing-video-toggle").addEventListener("click", toggleVideo);
  document.querySelector(".timing-video-shell").addEventListener("click", event => {
    if (event.target.closest("#timing-video-toggle")) return;
    toggleVideo();
  });
  videoSeek.addEventListener("pointerdown", () => { draggingVideoSeek = true; });
  videoSeek.addEventListener("input", () => {
    const target = Number(videoSeek.value);
    if (Number.isFinite(target)) applySeekTarget(target);
  });
  const finishVideoSeek = () => { draggingVideoSeek = false; };
  videoSeek.addEventListener("pointerup", finishVideoSeek);
  videoSeek.addEventListener("pointercancel", finishVideoSeek);
  videoSeek.addEventListener("change", finishVideoSeek);
  document.querySelectorAll("[data-timing-help]").forEach(button => button.addEventListener("click", () => document.getElementById("timing-help").showModal()));
  document.querySelectorAll("[data-command]").forEach(button => button.addEventListener("click", () => {
    const command = button.dataset.command;
    if (command === "play") toggleVideo();
    if (command === "back") seek(-10);
    if (command === "forward") seek(10);
    if (command === "frame-back") seek(-1 / 25);
    if (command === "frame-forward") seek(1 / 25);
  }));
  document.querySelectorAll("[data-export]").forEach(link => link.addEventListener("click", async event => {
    event.preventDefault();
    const unmarked = phrases.filter(item => item.start === null || item.start === undefined);
    if (unmarked.length) {
      pendingExportFormat = link.dataset.export;
      document.getElementById("timing-unmarked-count").textContent = String(unmarked.length);
      const firstIsUnmarked = phrases[0]?.start === null || phrases[0]?.start === undefined;
      document.getElementById("timing-unmarked-general").hidden = firstIsUnmarked;
      document.getElementById("timing-first-unmarked").hidden = !firstIsUnmarked;
      document.getElementById("timing-export-anyway").hidden = firstIsUnmarked;
      document.getElementById("timing-export-warning").showModal();
      return;
    }
    await startExport(link.dataset.export, false);
  }));
  async function startExport(format, includeUnmarked) {
    await saveNow();
    const key = `export${format.charAt(0).toUpperCase()}${format.slice(1)}`;
    window.location.href = `${root.dataset[key]}${includeUnmarked ? "?include_unmarked=1" : ""}`;
  }
  document.getElementById("timing-show-unmarked").addEventListener("click", () => {
    const index = phrases.findIndex(item => item.start === null || item.start === undefined);
    document.getElementById("timing-export-warning").close();
    if (index < 0) return;
    markingTarget = index;
    render();
    const row = document.querySelector(`.timing-phrase-row[data-index="${index}"]`);
    row?.classList.add("unmarked-attention");
    scrollPhraseInsideList(index);
    setTimeout(() => row?.classList.remove("unmarked-attention"), 1800);
  });
  document.getElementById("timing-export-anyway").addEventListener("click", async () => {
    const format = pendingExportFormat;
    pendingExportFormat = null;
    document.getElementById("timing-export-warning").close();
    if (format) await startExport(format, true);
  });
  video.addEventListener("loadedmetadata", () => {
    videoSeek.max = String(Number.isFinite(video.duration) ? video.duration : 0);
    if (pendingSeekTarget !== null) applySeekTarget(pendingSeekTarget);
    queueSave();
  });
  video.addEventListener("seeked", () => {
    if (pendingSeekTarget !== null && Math.abs(video.currentTime - pendingSeekTarget) <= .02) {
      pendingSeekTarget = null;
      clearTimeout(seekRetryTimer);
    }
  });
  window.addEventListener("beforeunload", () => { if (saveTimer) saveNow(); });
  render();
  animationTick();
  setTimeout(() => openTour(false), 700);
})();
