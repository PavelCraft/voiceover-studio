(() => {
  const textarea = document.getElementById("id_script_text");
  const preview = document.getElementById("split-phrase-preview");
  const audioInput = document.getElementById("id_audio");
  const audioDuration = document.getElementById("split-audio-duration");
  if (!textarea || !preview) return;

  const list = preview.querySelector("ol");
  let audioObjectUrl = "";

  function formatDuration(value) {
    const milliseconds = Math.max(0, Math.round(value * 1000));
    const hours = Math.floor(milliseconds / 3600000);
    const minutes = Math.floor(milliseconds % 3600000 / 60000);
    const seconds = Math.floor(milliseconds % 60000 / 1000);
    const remainder = milliseconds % 1000;
    return [hours, minutes, seconds].map((part) => String(part).padStart(2, "0")).join(":")
      + `.${String(remainder).padStart(3, "0")}`;
  }

  function showAudioDuration() {
    if (!audioInput || !audioDuration) return;
    if (audioObjectUrl) URL.revokeObjectURL(audioObjectUrl);
    audioObjectUrl = "";
    const file = audioInput.files?.[0];
    if (!file) {
      audioDuration.hidden = true;
      return;
    }
    const label = audioDuration.querySelector("span");
    const value = audioDuration.querySelector("strong");
    audioDuration.hidden = false;
    label.textContent = audioDuration.dataset.loading;
    value.textContent = "";
    const audio = new Audio();
    audio.preload = "metadata";
    audioObjectUrl = URL.createObjectURL(file);
    audio.src = audioObjectUrl;
    const finish = () => {
      if (!Number.isFinite(audio.duration) || audio.duration < 0) return false;
      label.textContent = `${audioDuration.dataset.label}:`;
      value.textContent = formatDuration(audio.duration);
      return true;
    };
    audio.addEventListener("loadedmetadata", () => {
      if (!finish() && audio.duration === Infinity) audio.currentTime = Number.MAX_SAFE_INTEGER;
    }, {once: true});
    audio.addEventListener("durationchange", finish);
    audio.addEventListener("error", () => {
      label.textContent = audioDuration.dataset.error;
      value.textContent = "";
    }, {once: true});
  }

  function phrases() {
    return textarea.value
      .split(/\r?\n/)
      .map((line) => line.trim().replace(/\s+/g, " "))
      .filter(Boolean);
  }

  function showEditor() {
    preview.hidden = true;
    textarea.hidden = false;
    textarea.focus({ preventScroll: true });
  }

  function showPreview() {
    const items = phrases();
    if (!items.length) return;
    list.replaceChildren();
    items.forEach((phrase, index) => {
      const item = document.createElement("li");
      const number = document.createElement("strong");
      const text = document.createElement("span");
      number.textContent = `${preview.dataset.phraseLabel || "Phrase"} ${index + 1}:`;
      text.textContent = `“${phrase}”`;
      item.append(number, text);
      list.append(item);
    });
    textarea.hidden = true;
    preview.hidden = false;
  }

  textarea.addEventListener("focus", () => {
    preview.hidden = true;
    textarea.hidden = false;
  });
  textarea.addEventListener("blur", showPreview);
  preview.addEventListener("click", showEditor);
  preview.addEventListener("keydown", (event) => {
    if (event.key === "Enter" || event.key === " ") {
      event.preventDefault();
      showEditor();
    }
  });
  if (audioInput) audioInput.addEventListener("change", showAudioDuration);

  showAudioDuration();
  if (textarea.value.trim()) showPreview();
})();
