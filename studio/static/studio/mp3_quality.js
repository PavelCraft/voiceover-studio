(() => {
  document.querySelectorAll("[data-mp3-quality]").forEach((qualityField) => {
    const form = qualityField.closest("form");
    const format = form?.querySelector('[name="output_format"]');
    const bitrate = qualityField.querySelector('[name="mp3_bitrate"]');
    if (!format || !bitrate) return;

    const update = () => {
      const enabled = format.value === "mp3";
      qualityField.hidden = !enabled;
      bitrate.disabled = !enabled;
    };

    format.addEventListener("change", update);
    update();
  });
})();
