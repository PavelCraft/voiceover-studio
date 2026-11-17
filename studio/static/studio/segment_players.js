(() => {
  const players = [...document.querySelectorAll(".segment-player")];
  let activeAudio = null;

  function formatTime(value, rounding = "floor") {
    if (!Number.isFinite(value) || value < 0) return "--:--";
    const totalSeconds = rounding === "nearest" ? Math.round(value) : Math.floor(value);
    return `${Math.floor(totalSeconds / 60)}:${String(totalSeconds % 60).padStart(2, "0")}`;
  }

  function setPaused(player) {
    const button = player.querySelector(".segment-player-toggle");
    button.textContent = "▶";
    button.setAttribute("aria-label", "Play");
  }

  players.forEach((player) => {
    const audio = player.querySelector("audio");
    const toggle = player.querySelector(".segment-player-toggle");
    const time = player.querySelector(".segment-player-time");
    const seek = player.querySelector(".segment-player-seek");
    const volume = player.querySelector(".segment-player-volume");
    let selectedTime = 0;
    let scrubbing = false;
    let endedNaturally = false;

    function duration() {
      const supplied = Number(player.dataset.duration);
      if (Number.isFinite(supplied) && supplied > 0) return supplied;
      return Number.isFinite(audio.duration) && audio.duration > 0 ? audio.duration : 0;
    }

    function clampTime(value) {
      const maximum = duration();
      return Math.max(0, maximum ? Math.min(value, maximum) : value);
    }

    function renderTime() {
      const maximum = duration();
      if (!scrubbing && !audio.paused && Number.isFinite(audio.currentTime)) {
        selectedTime = audio.currentTime;
      }
      seek.max = String(maximum);
      if (!scrubbing) seek.value = String(Math.min(selectedTime, maximum || selectedTime));
      time.textContent = `${formatTime(selectedTime)} / ${formatTime(maximum, "nearest")}`;
    }

    function setAudioPosition(value) {
      selectedTime = clampTime(value);
      if (audio.readyState > 0 && Number.isFinite(audio.duration)) {
        try {
          audio.currentTime = Math.min(selectedTime, audio.duration);
        } catch (_) {
          // Retain the selected position and apply it after metadata arrives.
        }
      }
    }

    function metadataReady() {
      if (audio.readyState > 0 && Number.isFinite(audio.duration)) return Promise.resolve();
      return new Promise((resolve, reject) => {
        const loaded = () => { cleanup(); resolve(); };
        const failed = () => { cleanup(); reject(new Error("Audio metadata could not be loaded")); };
        const cleanup = () => {
          audio.removeEventListener("loadedmetadata", loaded);
          audio.removeEventListener("error", failed);
        };
        audio.addEventListener("loadedmetadata", loaded, {once: true});
        audio.addEventListener("error", failed, {once: true});
        audio.load();
      });
    }

    audio.addEventListener("loadedmetadata", () => {
      setAudioPosition(selectedTime);
      renderTime();
    });
    audio.addEventListener("durationchange", renderTime);
    audio.addEventListener("timeupdate", renderTime);
    audio.addEventListener("ended", () => {
      setPaused(player);
      activeAudio = null;
      endedNaturally = true;
      selectedTime = 0;
      seek.value = "0";
      renderTime();
    });
    audio.addEventListener("error", () => {
      player.classList.add("segment-player-error");
      toggle.disabled = true;
    });

    player.addEventListener("segment-audio-updated", (event) => {
      if (activeAudio === audio) activeAudio = null;
      audio.pause();
      selectedTime = 0;
      scrubbing = false;
      endedNaturally = false;
      delete player.dataset.duration;
      player.classList.remove("segment-player-error");
      toggle.disabled = false;
      setPaused(player);
      seek.value = "0";
      seek.max = "0";
      time.textContent = "0:00 / --:--";
      audio.src = event.detail.url;
      const download = player.querySelector(".segment-player-download");
      if (download) download.href = event.detail.url;
      audio.load();
    });

    seek.addEventListener("pointerdown", () => { scrubbing = true; });
    seek.addEventListener("input", () => {
      scrubbing = true;
      endedNaturally = false;
      const requested = Number(seek.value);
      if (Number.isFinite(requested)) setAudioPosition(requested);
      renderTime();
    });
    seek.addEventListener("change", () => {
      const requested = Number(seek.value);
      if (Number.isFinite(requested)) setAudioPosition(requested);
      scrubbing = false;
      renderTime();
    });

    toggle.addEventListener("click", async () => {
      if (!audio.paused) {
        setAudioPosition(audio.currentTime);
        audio.pause();
        setPaused(player);
        activeAudio = null;
        renderTime();
        return;
      }
      if (activeAudio && activeAudio !== audio) {
        activeAudio.pause();
        setPaused(activeAudio.closest(".segment-player"));
      }
      try {
        await metadataReady();
        const maximum = duration();
        if (endedNaturally || audio.ended || (maximum && selectedTime >= maximum)) {
          selectedTime = 0;
          seek.value = "0";
          endedNaturally = false;
        } else {
          const requested = Number(seek.value);
          if (Number.isFinite(requested)) selectedTime = clampTime(requested);
        }
        setAudioPosition(selectedTime);
        await audio.play();
        activeAudio = audio;
        toggle.textContent = "❚❚";
        toggle.setAttribute("aria-label", "Pause");
      } catch (_) {
        player.classList.add("segment-player-error");
      }
    });

    volume.addEventListener("click", () => {
      audio.muted = !audio.muted;
      volume.textContent = audio.muted ? "🔇" : "🔊";
    });

    renderTime();
  });
})();
