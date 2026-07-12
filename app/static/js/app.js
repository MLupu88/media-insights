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
