(() => {
  const dialog = document.getElementById("segment-text-dialog");
  if (!dialog) return;

  const form = document.getElementById("segment-text-form");
  const input = document.getElementById("segment-text-input");
  const error = document.getElementById("segment-text-error");
  let activeButton = null;

  function csrfToken() {
    return document.cookie.split(";").map(value => value.trim())
      .find(value => value.startsWith("csrftoken="))?.slice("csrftoken=".length) || "";
  }

  document.querySelectorAll(".segment-text-edit").forEach(button => {
    button.addEventListener("click", () => {
      activeButton = button;
      input.value = button.closest(".segment").querySelector(".segment-text").textContent.trim();
      error.hidden = true;
      error.textContent = "";
      dialog.showModal();
      window.setTimeout(() => input.focus(), 0);
    });
  });

  document.querySelectorAll("[data-segment-text-cancel]").forEach(button => {
    button.addEventListener("click", () => dialog.close());
  });

  form.addEventListener("submit", async event => {
    event.preventDefault();
    if (!activeButton) return;
    const text = input.value.trim();
    if (!text) {
      input.focus();
      return;
    }
    const submit = form.querySelector('[type="submit"]');
    submit.disabled = true;
    try {
      const response = await fetch(activeButton.dataset.updateUrl, {
        method: "POST",
        headers: {"Content-Type": "application/json", "X-CSRFToken": csrfToken()},
        body: JSON.stringify({text}),
      });
      const data = await response.json();
      if (!response.ok) throw new Error(data.error || "Could not save phrase text");
      activeButton.closest(".segment").querySelector(".segment-text").textContent = data.text;
      dialog.close();
    } catch (requestError) {
      error.textContent = requestError.message;
      error.hidden = false;
    } finally {
      submit.disabled = false;
    }
  });
})();
