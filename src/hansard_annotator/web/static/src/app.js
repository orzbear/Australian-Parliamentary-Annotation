(() => {
  function resetProgressForms() {
    document.querySelectorAll("form[data-progress-form]").forEach((progressForm) => {
      delete progressForm.dataset.submitting;
      progressForm.removeAttribute("aria-busy");
      progressForm.querySelectorAll('button[type="submit"], button:not([type])').forEach((button) => {
        button.disabled = false;
        if (button.dataset.originalText) button.textContent = button.dataset.originalText;
        button.classList.remove("is-loading");
      });
    });
  }
  window.addEventListener("pageshow", resetProgressForms);

  document.addEventListener("submit", (event) => {
    const progressForm = event.target.closest?.("form[data-progress-form]");
    if (!progressForm) return;
    if (progressForm.dataset.submitting === "true") {
      event.preventDefault();
      return;
    }
    progressForm.dataset.submitting = "true";
    progressForm.setAttribute("aria-busy", "true");
    progressForm.querySelectorAll('button[type="submit"], button:not([type])').forEach((button) => {
      button.disabled = true;
    });
    const submitter = event.submitter;
    if (submitter) {
      submitter.dataset.originalText ||= submitter.textContent;
      submitter.textContent = submitter.dataset.progressText || "Working...";
      submitter.classList.add("is-loading");
    }
  });

  const speech = document.querySelector("[data-speech-text]");
  const canonicalSpeech = speech?.textContent || "";
  const copyButton = document.querySelector("[data-copy-speech]");
  const copyFeedback = document.querySelector("[data-copy-feedback]");
  function legacyCopy(text) {
    const fallback = document.createElement("textarea");
    fallback.value = text;
    fallback.setAttribute("readonly", "");
    fallback.style.position = "fixed";
    fallback.style.left = "-9999px";
    fallback.style.top = "0";
    document.body.append(fallback);
    fallback.focus();
    fallback.select();
    fallback.setSelectionRange(0, fallback.value.length);
    const copied = document.execCommand("copy");
    fallback.remove();
    return copied;
  }
  async function copySpeech() {
    let copied = false;
    try {
      if (navigator.clipboard?.writeText) {
        await navigator.clipboard.writeText(canonicalSpeech);
        copied = true;
      }
    } catch {
      copied = false;
    }
    if (!copied) copied = legacyCopy(canonicalSpeech);
    if (copyFeedback) copyFeedback.textContent = copied ? "Copied" : "Copy failed — select the speech and copy manually";
    window.setTimeout(() => { if (copyFeedback) copyFeedback.textContent = ""; }, 1500);
  }
  copyButton?.addEventListener("click", copySpeech);

  const form = document.querySelector("[data-dirty-form]");
  if (!form) return;

  const correctionToggle = document.querySelector("[data-correction-toggle]");
  const correctionForm = document.querySelector("[data-correction-form]");
  if (correctionToggle && correctionForm) {
    correctionForm.hidden = !correctionToggle.checked;
    correctionToggle.addEventListener("change", () => {
      correctionForm.hidden = !correctionToggle.checked;
      if (correctionToggle.checked) correctionForm.querySelector("input,textarea,select")?.focus();
    });
  }

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
    wrapper.disabled = !visible;
    wrapper.querySelectorAll("input,textarea,select").forEach((control) => {
      control.disabled = !visible;
      if (!visible && (control.type === "checkbox" || control.type === "radio")) control.checked = false;
      if (!visible && !["checkbox", "radio"].includes(control.type)) control.value = "";
    });
    if (!visible && key === "evidence_span" && typeof evidenceSpans !== "undefined") {
      evidenceSpans = [];
      renderEvidence();
    }
  }
  const evidenceInput = form.querySelector("[data-evidence-input]");
  const evidenceList = form.querySelector("[data-evidence-list]");
  const evidenceFeedback = form.querySelector("[data-evidence-feedback]");
  let evidenceSpans = [];
  if (evidenceInput) {
    try { evidenceSpans = JSON.parse(evidenceInput.value || "[]"); } catch { evidenceSpans = []; }
  }
  function renderEvidence() {
    if (!speech || !evidenceInput || !evidenceList) return;
    evidenceSpans.sort((left, right) => left.start_offset - right.start_offset || left.end_offset - right.end_offset);
    evidenceInput.value = JSON.stringify(evidenceSpans);
    speech.replaceChildren();
    evidenceList.replaceChildren();
    const codePoints = Array.from(canonicalSpeech);
    let cursor = 0;
    evidenceSpans.forEach((span, index) => {
      speech.append(document.createTextNode(codePoints.slice(cursor, span.start_offset).join("")));
      const mark = document.createElement("mark");
      mark.append(document.createTextNode(codePoints.slice(span.start_offset, span.end_offset).join("")));
      speech.append(mark);
      cursor = span.end_offset;

      const item = document.createElement("li");
      item.className = "rounded-xl border p-3";
      const quote = document.createElement("span");
      quote.textContent = span.text;
      const remove = document.createElement("button");
      remove.type = "button";
      remove.className = "btn btn-quiet ml-2";
      remove.textContent = "Remove";
      remove.addEventListener("click", () => {
        evidenceSpans.splice(index, 1);
        renderEvidence();
        form.dispatchEvent(new Event("input", {bubbles: true}));
      });
      item.append(quote, remove);
      evidenceList.append(item);
    });
    speech.append(document.createTextNode(codePoints.slice(cursor).join("")));
  }
  function selectionOffsets() {
    if (!speech) return null;
    const selection = window.getSelection();
    if (!selection || selection.rangeCount !== 1 || selection.isCollapsed) return null;
    const range = selection.getRangeAt(0);
    if (!speech.contains(range.startContainer) || !speech.contains(range.endContainer)) return null;
    const prefix = document.createRange();
    prefix.selectNodeContents(speech);
    prefix.setEnd(range.startContainer, range.startOffset);
    const start = Array.from(prefix.toString()).length;
    const end = start + Array.from(range.toString()).length;
    return {start_offset: start, end_offset: end, text: Array.from(canonicalSpeech).slice(start, end).join("")};
  }
  form.querySelector("[data-add-evidence]")?.addEventListener("click", () => {
    const span = selectionOffsets();
    if (!span || !span.text) {
      if (evidenceFeedback) evidenceFeedback.textContent = "Select text within the speech first.";
      return;
    }
    const overlaps = evidenceSpans.some((item) => span.start_offset < item.end_offset && span.end_offset > item.start_offset);
    if (overlaps) {
      if (evidenceFeedback) evidenceFeedback.textContent = "That selection overlaps existing evidence.";
      return;
    }
    evidenceSpans.push(span);
    window.getSelection()?.removeAllRanges();
    if (evidenceFeedback) evidenceFeedback.textContent = "Evidence added.";
    renderEvidence();
    form.dispatchEvent(new Event("input", {bubbles: true}));
  });
  function currentValue(key) {
    const controls = [...form.querySelectorAll(`[name="${key}"]`)];
    if (!controls.length) return null;
    if (controls[0].type === "checkbox" || controls[0].type === "radio") {
      const checked = controls.find((control) => control.checked);
      if (!checked) return null;
      if (checked.value === "true") return true;
      if (checked.value === "false") return false;
      return checked.value;
    }
    return controls[0].value || null;
  }
  function conditionMatches(condition) {
    if (!condition || typeof condition !== "object") return false;
    if (Array.isArray(condition.all)) return condition.all.every(conditionMatches);
    if (Array.isArray(condition.any)) return condition.any.some(conditionMatches);
    for (const operator of ["field_equals", "field_not_equals", "taxonomy_code_equals"]) {
      const operand = condition[operator];
      if (!operand) continue;
      const equal = currentValue(operand.field) === operand.value;
      return operator === "field_not_equals" ? !equal : equal;
    }
    return false;
  }
  function updateDeclarativeVisibility() {
    form.querySelectorAll("[data-visible-when]").forEach((wrapper) => {
      setVisible(wrapper.dataset.field, conditionMatches(JSON.parse(wrapper.dataset.visibleWhen)));
    });
  }
  function updateConditionalFields() {
    updateDeclarativeVisibility();
    const hostilityIsNo = selected("hostility_present") === "NO";
    form.querySelectorAll("[data-hostility-not-applicable]").forEach((message) => {
      message.hidden = !hostilityIsNo;
    });
    if (form.querySelector('input[name="is_non_policy"]')) {
      const nonPolicy = selected("is_non_policy") === "true";
      const submit = form.querySelector("[data-submit-next]");
      if (submit && !submit.hasAttribute("data-ai-edit-submit")) submit.textContent = nonPolicy ? "Mark non-policy & next" : "Submit & next";
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
  renderEvidence();
})();
