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

// Explicit browser confirmation for destructive form submissions (e.g.
// deleting a conversation or narrative generation): a form with
// data-confirm="..." is blocked unless the user accepts the native
// confirm() dialog. Progressive enhancement only -- the server is the
// real authority on what gets deleted; this only prevents an accidental
// click. The submit button is disabled right after a confirmed submit to
// prevent a double-click from firing the request twice.
document.addEventListener("submit", (event) => {
  const form = event.target;
  const message = form.getAttribute && form.getAttribute("data-confirm");
  if (!message) return;

  if (!window.confirm(message)) {
    event.preventDefault();
    return;
  }

  const button = form.querySelector('button[type="submit"]');
  if (button) button.disabled = true;
});

// "Select all visible" for a bulk-action checkbox group: purely a
// convenience toggle over the real per-row checkboxes already submitted
// with the form (via the `form="..."` attribute) -- nothing here changes
// what gets submitted, it only pre-checks/unchecks the boxes.
document.addEventListener("change", (event) => {
  const selectAll = event.target.closest("[data-select-all]");
  if (!selectAll) return;
  const name = selectAll.getAttribute("data-select-all");
  const container = selectAll.closest(".msl-panel") || document;
  container.querySelectorAll(`input[type="checkbox"][name="${name}"]`).forEach((checkbox) => {
    checkbox.checked = selectAll.checked;
  });
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

// Delete-project confirmation: the submit button stays disabled until the
// typed value exactly matches the project's own name -- server-side
// validation (app/api/pages.py::delete_project_action) is the real
// safety check; this is only to prevent an accidental click.
document.querySelectorAll("[data-delete-confirm-input]").forEach((input) => {
  const expected = input.getAttribute("data-expected-name") || "";
  const form = input.closest("form");
  const submitButton = form ? form.querySelector("[data-delete-confirm-submit]") : null;
  if (!submitButton) return;

  input.addEventListener("input", () => {
    submitButton.disabled = input.value !== expected;
  });
});


// Progressive enhancement for asynchronous Chat and Insights jobs. The
// server-rendered pages remain fully usable without JavaScript: a manual
// refresh still reveals the final state. With JavaScript enabled, only a
// minimal authenticated status endpoint is polled; the page reloads once
// when the job reaches a terminal state.
document.querySelectorAll("[data-async-status-poll]").forEach((poller) => {
  const statusUrl = poller.getAttribute("data-status-url");
  const reloadUrl = poller.getAttribute("data-reload-url") || window.location.href;
  const intervalMs = Number(poller.getAttribute("data-poll-interval-ms") || 1500);
  const timeoutMs = Number(poller.getAttribute("data-timeout-ms") || 90000);
  const timeoutMessage =
    poller.getAttribute("data-timeout-message") ||
    "This operation is taking longer than expected. You can safely refresh later.";

  if (!statusUrl) return;

  const deadline = Date.now() + timeoutMs;
  let stopped = false;

  function stopWithMessage(message) {
    stopped = true;
    poller.textContent = message;
  }

  async function checkStatus() {
    if (stopped) return;
    if (Date.now() >= deadline) {
      stopWithMessage(timeoutMessage);
      return;
    }

    try {
      const response = await fetch(statusUrl, {
        method: "GET",
        credentials: "same-origin",
        cache: "no-store",
        headers: { Accept: "application/json" },
      });

      if (response.redirected) {
        window.location.assign(response.url);
        return;
      }
      if (!response.ok) throw new Error(`Status request failed: ${response.status}`);

      const payload = await response.json();
      if (payload.terminal === true) {
        stopped = true;
        window.location.replace(reloadUrl);
        return;
      }
    } catch (_error) {
      // A transient network failure should not turn a successfully-running
      // background job into a visible error. Keep polling until the bounded
      // timeout; a manual refresh remains available throughout.
    }

    window.setTimeout(checkStatus, intervalMs);
  }

  window.setTimeout(checkStatus, intervalMs);
});
