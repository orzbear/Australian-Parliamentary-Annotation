(() => {
  const form = document.querySelector("[data-dirty-form]");
  if (!form) return;

  let dirty = false;
  form.addEventListener("input", () => { dirty = true; updateConditionalFields(); });
  form.addEventListener("change", () => { dirty = true; updateCounts(); updateConditionalFields(); });
  form.addEventListener("submit", () => { dirty = false; });
  document.body.addEventListener("htmx:afterRequest", (event) => {
    if (event.detail.successful) dirty = false;
  });
  window.addEventListener("beforeunload", (event) => {
    if (dirty) { event.preventDefault(); event.returnValue = ""; }
  });
  document.addEventListener("keydown", (event) => {
    if (!(event.ctrlKey || event.metaKey)) return;
    if (event.key.toLowerCase() === "s") {
      event.preventDefault();
      form.querySelector("[data-save-draft]")?.click();
    }
    if (event.key === "Enter") {
      event.preventDefault();
      form.querySelector("[data-submit-next]")?.click();
    }
  });

  function selected(name) {
    return form.querySelector(`input[name="${name}"]:checked`)?.value || "";
  }
  function setVisible(key, visible) {
    const wrapper = form.querySelector(`[data-field="${key}"]`);
    if (!wrapper) return;
    wrapper.hidden = !visible;
    wrapper.querySelectorAll("input,textarea,select").forEach((control) => {
      control.disabled = !visible;
      if (!visible && (control.type === "checkbox" || control.type === "radio")) control.checked = false;
      if (!visible && !["checkbox", "radio"].includes(control.type)) control.value = "";
    });
  }
  function updateConditionalFields() {
    if (form.querySelector('input[name="is_non_policy"]')) {
      const nonPolicy = selected("is_non_policy") === "true";
      const submit = form.querySelector("[data-submit-next]");
      if (submit) submit.textContent = nonPolicy ? "Mark non-policy & next" : "Submit & next";
      setVisible("primary_australian_domain", !nonPolicy);
      setVisible("secondary_australian_domains", !nonPolicy);
      setVisible("fallback_explanation", !nonPolicy && selected("primary_australian_domain") === "AU_OTHER_REVIEW");
      updateRevealControls();
      return;
    }
    const status = selected("content_status");
    if (!form.querySelector('input[name="content_status"]')) return;
    const substantive = status === "substantive_policy";
    ["primary_australian_domain", "secondary_australian_domains", "specific_australian_issue"].forEach((key) => setVisible(key, substantive));
    setVisible("fallback_explanation", substantive && selected("primary_australian_domain") === "AU_OTHER_REVIEW");
    setVisible("unclassifiable_reason", status === "unclassifiable");
  }
  function updateRevealControls() {
    form.querySelectorAll("[data-reveal-toggle]").forEach((toggle) => {
      const key = toggle.dataset.revealToggle;
      const content = form.querySelector(`[data-reveal-content="${key}"]`);
      if (!content) return;
      const wrapperHidden = toggle.closest("[data-field]")?.hidden;
      const visible = toggle.checked && !wrapperHidden;
      content.hidden = !visible;
      content.querySelectorAll("input,textarea,select").forEach((control) => {
        control.disabled = !visible;
        if (!visible && (control.type === "checkbox" || control.type === "radio")) control.checked = false;
        if (!visible && !["checkbox", "radio"].includes(control.type)) control.value = "";
      });
    });
  }
  function updateCounts() {
    document.querySelectorAll("[data-max]").forEach((group) => {
      const checked = group.querySelectorAll('input[type="checkbox"]:checked');
      const max = Number(group.dataset.max || 0);
      group.closest("fieldset")?.querySelector("[data-choice-count]").replaceChildren(`${checked.length} of ${max}`);
      group.querySelectorAll('input[type="checkbox"]:not(:checked)').forEach((input) => { input.disabled = checked.length >= max; });
    });
  }
  updateCounts();
  form.querySelectorAll("[data-reveal-toggle]").forEach((toggle) => {
    toggle.addEventListener("change", () => { updateRevealControls(); updateCounts(); });
  });
  updateRevealControls();
  updateConditionalFields();
})();
