(() => {
  const players = [...document.querySelectorAll(".segment-player")];
  let activeAudio = null;

  function formatTime(value) {
    if (!Number.isFinite(value) || value < 0) return "--:--";
    const seconds = Math.floor(value % 60);
    const minutes = Math.floor(value / 60);
    return `${minutes}:${String(seconds).padStart(2, "0")}`;
  }

  function setPaused(player) {
    const button = player.querySelector(".segment-player-toggle");
    button.textContent = "▶";
    button.setAttribute("aria-label", "Play");
  }

  players.forEach(player => {
    const audio = player.querySelector("audio");
    const toggle = player.querySelector(".segment-player-toggle");
    const time = player.querySelector(".segment-player-time");
    const seek = player.querySelector(".segment-player-seek");
    const volume = player.querySelector(".segment-player-volume");
    let selectedTime = 0;

    let endedNaturally = false;
    function renderTime() {
      const duration = Number.isFinite(audio.duration) ? audio.duration : 0;
      time.textContent = `${formatTime(audio.currentTime)} / ${formatTime(duration)}`;
      seek.max = String(duration);
      if (document.activeElement !== seek) {
        seek.value = String(Math.min(audio.currentTime, duration));
      }
      if (!audio.paused && document.activeElement !== seek && Number.isFinite(audio.currentTime)) selectedTime = audio.currentTime;
    }

    audio.addEventListener("loadedmetadata", renderTime);
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
    function applySeekPosition() {
      const requested = Number(seek.value);
      if (!Number.isFinite(requested)) return;
      endedNaturally = false;
      selectedTime = requested;
      audio.currentTime = requested;
      const duration = Number.isFinite(audio.duration) ? audio.duration : 0;
      time.textContent = `${formatTime(requested)} / ${formatTime(duration)}`;
    }

    seek.addEventListener("input", () => {
      applySeekPosition();
    });
    seek.addEventListener("change", () => {
      audio.currentTime = selectedTime;
      renderTime();
    });

    toggle.addEventListener("click", async () => {
      if (!audio.paused) {
        selectedTime = audio.currentTime;
        audio.pause();
        setPaused(player);
        activeAudio = null;
        return;
      }
      if (activeAudio && activeAudio !== audio) {
        activeAudio.pause();
        setPaused(activeAudio.closest(".segment-player"));
      }
      if (endedNaturally || audio.ended || (audio.duration && audio.currentTime >= audio.duration)) {
        audio.currentTime = 0;
        selectedTime = 0;
        seek.value = "0";
        endedNaturally = false;
      } else {
        const rangePosition = Number(seek.value);
        if (Number.isFinite(rangePosition)) selectedTime = rangePosition;
      }
      audio.currentTime = selectedTime;
      try {
        await audio.play();
        activeAudio = audio;
        toggle.textContent = "❚❚";
        toggle.setAttribute("aria-label", "Pause");
      } catch (error) {
        player.classList.add("segment-player-error");
      }
    });

    volume.addEventListener("click", () => {
      audio.muted = !audio.muted;
      volume.textContent = audio.muted ? "🔇" : "🔊";
    });
  });
})();
