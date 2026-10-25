(() => {
  const sidebarShell = document.querySelector("[data-sidebar-shell]");
  const sidebar = document.getElementById("app-sidebar");
  const sidebarReveal = document.querySelector("[data-sidebar-reveal]");
  const sidebarPin = document.querySelector("[data-sidebar-pin]");
  if (sidebarShell && sidebar && sidebarReveal && sidebarPin) {
    const storageKey = "voiceover-studio-sidebar-pinned";
    let pinned = localStorage.getItem(storageKey) !== "false";
    let previewOpen = false;
    const renderSidebar = () => {
      sidebarShell.classList.toggle("sidebar-unpinned", !pinned);
      sidebarShell.classList.toggle("sidebar-preview-open", !pinned && previewOpen);
      sidebarPin.setAttribute("aria-pressed", String(pinned));
      sidebarPin.title = pinned ? sidebarPin.dataset.titleUnpin : sidebarPin.dataset.titlePin;
      sidebarReveal.setAttribute("aria-expanded", String(previewOpen));
    };
    sidebarReveal.addEventListener("click", () => {
      if (pinned) return;
      previewOpen = true;
      renderSidebar();
      sidebar.querySelector("a, button:not([data-sidebar-reveal])")?.focus();
    });
    sidebarPin.addEventListener("click", () => {
      pinned = !pinned;
      previewOpen = !pinned;
      localStorage.setItem(storageKey, String(pinned));
      renderSidebar();
    });
    const closeUnpinnedSidebar = event => {
      if (!pinned && previewOpen && !sidebar.contains(event.relatedTarget) && !sidebar.contains(document.activeElement)) {
        previewOpen = false;
        renderSidebar();
      }
    };
    sidebar.addEventListener("pointerleave", closeUnpinnedSidebar);
    sidebar.addEventListener("focusout", closeUnpinnedSidebar);
    document.addEventListener("keydown", event => {
      if (event.key === "Escape" && !pinned && previewOpen) {
        previewOpen = false;
        renderSidebar();
        sidebarReveal.focus();
      }
    });
    renderSidebar();
  }

  document.querySelectorAll(".file-drop").forEach(drop => {
    const input = drop.querySelector('input[type="file"]');
    const label = drop.querySelector("strong");
    if (!input || !label) return;
    const original = label.textContent;
    const render = () => {
      const files = [...input.files];
      label.textContent = files.length ? files.map(file => file.name).join(", ") : original;
      drop.classList.toggle("has-file", files.length > 0);
    };
    input.addEventListener("change", render);
    ["dragenter", "dragover"].forEach(name => drop.addEventListener(name, () => drop.classList.add("drag-active")));
    ["dragleave", "drop"].forEach(name => drop.addEventListener(name, () => window.setTimeout(() => drop.classList.remove("drag-active"), 0)));
  });
  const formatField = document.getElementById("audio-format-field");
  const outputModes = [...document.querySelectorAll('input[name="output_mode"]')];
  if (formatField && outputModes.length) {
    const syncFormat = () => {
      const mode = outputModes.find(input => input.checked)?.value;
      formatField.hidden = mode === "video";
    };
    outputModes.forEach(input => input.addEventListener("change", syncFormat));
    syncFormat();
  }
})();
