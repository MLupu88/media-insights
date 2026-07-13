document.addEventListener("click", (event) => {
  const opener = event.target.closest("[data-modal-open]");
  if (opener) {
    const modal = document.getElementById(opener.getAttribute("data-modal-open"));
    if (modal) modal.classList.remove("hidden");
    return;
  }

  const closer = event.target.closest("[data-modal-close]");
  if (closer) {
    const modal = closer.closest("[data-modal]");
    if (modal) modal.classList.add("hidden");
    return;
  }

  const modal = event.target.closest("[data-modal]");
  if (modal && event.target === modal) {
    modal.classList.add("hidden");
    return;
  }

  const tabButton = event.target.closest("[data-tab-button]");
  if (tabButton) {
    const target = tabButton.getAttribute("data-tab-button");

    document.querySelectorAll("[data-tab-panel]").forEach((panel) => {
      panel.classList.toggle("hidden", panel.getAttribute("data-tab-panel") !== target);
    });

    document.querySelectorAll("[data-tab-button], [data-tab-indicator]").forEach((el) => {
      const key = el.getAttribute("data-tab-button") || el.getAttribute("data-tab-indicator");
      const active = key === target;
      el.classList.toggle("border-ink", active);
      el.classList.toggle("text-ink", active);
      el.classList.toggle("border-transparent", !active);
      el.classList.toggle("text-ink/50", !active);
    });
  }
});

document.addEventListener("keydown", (event) => {
  if (event.key === "Escape") {
    document.querySelectorAll("[data-modal]:not(.hidden)").forEach((modal) => {
      modal.classList.add("hidden");
    });
  }
});

// Multi-file upload dropzone: progressive enhancement only. The real
// `<input type="file" multiple>` inside the dropzone stays fully
// functional without any of this — drag-and-drop just populates that
// same input, and the form still submits natively (full page reload),
// never XHR. No fake progress percentage is ever shown, since the import
// itself is synchronous and there is nothing real to measure — only an
// honest "Processing files…" state once the request is underway.
document.querySelectorAll("[data-upload-dropzone]").forEach((dropzone) => {
  const input = dropzone.querySelector('input[type="file"]');
  const fileList = dropzone.querySelector("[data-selected-files]");
  const submitButton = dropzone.querySelector("[data-upload-submit]");
  if (!input) return;

  function renderSelectedFiles() {
    if (!fileList) return;
    const files = Array.from(input.files || []);
    fileList.innerHTML = "";
    if (!files.length) {
      fileList.classList.add("hidden");
      return;
    }
    files.forEach((file) => {
      const item = document.createElement("li");
      item.textContent = file.name;
      fileList.appendChild(item);
    });
    fileList.classList.remove("hidden");
  }

  input.addEventListener("change", renderSelectedFiles);

  ["dragenter", "dragover"].forEach((eventName) => {
    dropzone.addEventListener(eventName, (event) => {
      event.preventDefault();
      dropzone.classList.add("border-accent");
    });
  });

  ["dragleave", "drop"].forEach((eventName) => {
    dropzone.addEventListener(eventName, (event) => {
      event.preventDefault();
      dropzone.classList.remove("border-accent");
    });
  });

  dropzone.addEventListener("drop", (event) => {
    const dropped = event.dataTransfer && event.dataTransfer.files;
    if (dropped && dropped.length) {
      input.files = dropped;
      renderSelectedFiles();
    }
  });

  dropzone.addEventListener("submit", () => {
    if (submitButton) {
      submitButton.disabled = true;
      submitButton.textContent = "Processing files…";
    }
  });
});
