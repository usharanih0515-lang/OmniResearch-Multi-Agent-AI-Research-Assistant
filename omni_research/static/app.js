document.querySelector("[data-research-form]")?.addEventListener("submit", () => {
  const button = document.querySelector("[data-submit-button]");
  if (button) {
    button.disabled = true;
    button.textContent = "Researching…";
  }
});
