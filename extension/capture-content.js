(function () {
  if (window.__FLOW2API_CAPTURE_CONTENT_INSTALLED__) {
    return;
  }
  window.__FLOW2API_CAPTURE_CONTENT_INSTALLED__ = true;

  const state = {
    enabled: false,
    traceId: "",
    runId: "",
    sessionId: "",
    captureTaskType: "",
    captureTaskLabel: "",
    routeKey: "",
    endpointUrl: "",
    tabId: null,
    eventSeq: 0,
    observer: null,
    mutationTimer: null,
    pointerSession: null,
  };

  function nowIso() {
    return new Date().toISOString();
  }

  function hashString(value) {
    const text = String(value || "");
    let hash = 5381;
    for (let i = 0; i < text.length; i += 1) {
      hash = ((hash << 5) + hash) + text.charCodeAt(i);
      hash &= 0xffffffff;
    }
    return `h${(hash >>> 0).toString(16)}`;
  }

  function summarizeText(value, maxLength = 120) {
    return String(value || "").replace(/\s+/g, " ").trim().slice(0, maxLength);
  }

  function extractProjectId(rawUrl) {
    try {
      const parsed = new URL(rawUrl || location.href);
      const parts = parsed.pathname.split("/").filter(Boolean);
      const index = parts.indexOf("project");
      if (index >= 0 && parts[index + 1]) {
        return parts[index + 1];
      }
    } catch (error) {
      return "";
    }
    return "";
  }

  function valueSummary(target) {
    const text = typeof target?.value === "string"
      ? target.value
      : typeof target?.innerText === "string"
        ? target.innerText
        : typeof target?.textContent === "string"
          ? target.textContent
          : "";
    const clean = summarizeText(text, 200);
    return {
      length: clean.length,
      hash: hashString(clean),
      sample: clean.slice(0, 24),
    };
  }

  function selectorCandidates(target) {
    if (!(target instanceof Element)) {
      return [];
    }
    const selectors = [];
    if (target.id) {
      selectors.push(`#${CSS.escape(target.id)}`);
    }
    const classNames = Array.from(target.classList || []).slice(0, 3);
    if (classNames.length) {
      selectors.push(
        `${target.tagName.toLowerCase()}.${classNames.map((name) => CSS.escape(name)).join(".")}`
      );
    }
    selectors.push(target.tagName.toLowerCase());
    return selectors.slice(0, 4);
  }

  function surroundingText(target) {
    if (!(target instanceof Element)) {
      return [];
    }
    const texts = [];
    const pushText = (value) => {
      const clean = summarizeText(value, 80);
      if (clean && !texts.includes(clean)) {
        texts.push(clean);
      }
    };
    pushText(target.textContent);
    pushText(target.getAttribute?.("aria-label"));
    pushText(target.parentElement?.textContent);
    for (const sibling of Array.from(target.parentElement?.children || []).slice(0, 4)) {
      if (sibling !== target) {
        pushText(sibling.textContent);
      }
    }
    return texts.slice(0, 6);
  }

  function summarizeDataset(target) {
    if (!(target instanceof Element) || !target.dataset) {
      return {};
    }
    const output = {};
    for (const [key, value] of Object.entries(target.dataset)) {
      output[key] = summarizeText(value, 80);
      if (Object.keys(output).length >= 8) {
        break;
      }
    }
    return output;
  }

  function summarizeElement(target) {
    if (!(target instanceof Element)) {
      return {
        tag: "",
        text: "",
        role: "",
        aria_label: "",
        name: "",
        placeholder: "",
        dataset: {},
        selector_candidates: [],
      };
    }
    return {
      tag: target.tagName.toLowerCase(),
      text: summarizeText(target.textContent),
      role: target.getAttribute("role") || "",
      aria_label: target.getAttribute("aria-label") || "",
      name: target.getAttribute("name") || "",
      placeholder: target.getAttribute("placeholder") || "",
      dataset: summarizeDataset(target),
      selector_candidates: selectorCandidates(target),
      surrounding_text: surroundingText(target),
    };
  }

  function summarizeRect(target) {
    if (!(target instanceof Element) || typeof target.getBoundingClientRect !== "function") {
      return null;
    }
    const rect = target.getBoundingClientRect();
    return {
      left: Math.round(rect.left),
      top: Math.round(rect.top),
      width: Math.round(rect.width),
      height: Math.round(rect.height),
      right: Math.round(rect.right),
      bottom: Math.round(rect.bottom),
    };
  }

  function collectTimeTokens(text) {
    const normalized = String(text || "");
    const matches = normalized.match(/\d{2}:\d{2}:\d{2}/g) || [];
    return matches.slice(0, 12);
  }

  function textSignalScore(text) {
    const hay = String(text || "");
    let score = 0;
    if ((hay.match(/\d{2}:\d{2}:\d{2}/g) || []).length >= 2) score += 3;
    if (hay.includes("添加到提示")) score += 2;
    if (hay.includes("裁剪") || hay.includes("修剪")) score += 2;
    if (hay.toLowerCase().includes("trim")) score += 2;
    if (hay.includes("播放") || hay.includes("静音")) score += 1;
    return score;
  }

  function findTimelineScope(target) {
    let node = target instanceof Element ? target : null;
    let bestNode = null;
    let bestScore = 0;
    let depth = 0;
    while (node && depth < 8) {
      const text = summarizeText(node.textContent, 400);
      const score = textSignalScore(text);
      const hasTrimHandle = node.querySelector?.("img[src*='trim-handle']") ? 2 : 0;
      const mediaCount = node.querySelectorAll?.("video, img").length || 0;
      const finalScore = score + (mediaCount > 0 ? 1 : 0) + hasTrimHandle;
      if (finalScore > bestScore) {
        bestNode = node;
        bestScore = finalScore;
      }
      node = node.parentElement;
      depth += 1;
    }
    return bestScore >= 3 ? bestNode : null;
  }

  function summarizeTimelineScope(target) {
    const scope = findTimelineScope(target);
    if (!scope) {
      return null;
    }
    const text = summarizeText(scope.textContent, 400);
    return {
      root: summarizeElement(scope),
      rect: summarizeRect(scope),
      time_tokens: collectTimeTokens(text),
      contains_add_to_prompt: text.includes("添加到提示"),
      contains_trim_signal: text.includes("裁剪") || text.includes("修剪") || text.toLowerCase().includes("trim"),
      media_count: scope.querySelectorAll?.("video, img").length || 0,
      trim_handle_count: scope.querySelectorAll?.("img[src*='trim-handle']").length || 0,
      text_sample: text,
    };
  }

  function buildPageSnapshot() {
    return {
      url: location.href,
      title: document.title,
      ready_state: document.readyState,
      project_id: extractProjectId(location.href),
    };
  }

  function postEvent(eventType, payload = {}) {
    if (!state.enabled || !state.endpointUrl) {
      return;
    }
    state.eventSeq += 1;
    const body = {
      trace_id: state.traceId,
      run_id: state.runId,
      session_id: state.sessionId,
      capture_task_type: state.captureTaskType,
      capture_task_label: state.captureTaskLabel,
      route_key: state.routeKey,
      tab_id: state.tabId,
      event_seq: state.eventSeq,
      event_type: eventType,
      ts: Date.now(),
      at: nowIso(),
      project_id: extractProjectId(location.href),
      page: buildPageSnapshot(),
      ...payload,
    };
    fetch(state.endpointUrl, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
      keepalive: true,
    }).catch(() => {});
  }

  function handleClick(event) {
    const target = event.target;
    postEvent("ui.click", {
      target: summarizeElement(target),
      context: {
        client_x: typeof event.clientX === "number" ? Math.round(event.clientX) : null,
        client_y: typeof event.clientY === "number" ? Math.round(event.clientY) : null,
      },
    });
  }

  function handleInput(event) {
    const target = event.target;
    postEvent(event.type === "beforeinput" ? "ui.beforeinput" : "ui.input", {
      target: summarizeElement(target),
      input: {
        value_summary: valueSummary(target),
        is_contenteditable: !!target?.isContentEditable,
        tag: target?.tagName?.toLowerCase?.() || "",
        input_type: event.inputType || "",
      },
    });
  }

  function handleChange(event) {
    const target = event.target;
    postEvent("ui.change", {
      target: summarizeElement(target),
      input: {
        value_summary: valueSummary(target),
      },
    });
  }

  function pointerContext(event) {
    return {
      client_x: typeof event.clientX === "number" ? Math.round(event.clientX) : null,
      client_y: typeof event.clientY === "number" ? Math.round(event.clientY) : null,
      page_x: typeof event.pageX === "number" ? Math.round(event.pageX) : null,
      page_y: typeof event.pageY === "number" ? Math.round(event.pageY) : null,
      pointer_id: typeof event.pointerId === "number" ? event.pointerId : null,
      pointer_type: String(event.pointerType || "mouse"),
      buttons: typeof event.buttons === "number" ? event.buttons : null,
    };
  }

  function handlePointerDown(event) {
    const timeline = summarizeTimelineScope(event.target);
    if (!timeline) {
      state.pointerSession = null;
      return;
    }
    state.pointerSession = {
      pointerId: typeof event.pointerId === "number" ? event.pointerId : null,
      startedAt: Date.now(),
      startX: typeof event.clientX === "number" ? event.clientX : null,
      startY: typeof event.clientY === "number" ? event.clientY : null,
      lastMoveAt: 0,
      lastMoveX: typeof event.clientX === "number" ? event.clientX : null,
      lastMoveY: typeof event.clientY === "number" ? event.clientY : null,
      timeline,
    };
    postEvent("ui.timeline_drag_start", {
      target: summarizeElement(event.target),
      context: pointerContext(event),
      timeline,
    });
  }

  function handlePointerMove(event) {
    const session = state.pointerSession;
    if (!session) {
      return;
    }
    if (session.pointerId !== null && typeof event.pointerId === "number" && session.pointerId !== event.pointerId) {
      return;
    }
    const now = Date.now();
    const currentX = typeof event.clientX === "number" ? event.clientX : session.lastMoveX;
    const currentY = typeof event.clientY === "number" ? event.clientY : session.lastMoveY;
    const deltaX = currentX !== null && session.lastMoveX !== null ? Math.abs(currentX - session.lastMoveX) : 0;
    const deltaY = currentY !== null && session.lastMoveY !== null ? Math.abs(currentY - session.lastMoveY) : 0;
    if ((now - session.lastMoveAt) < 120 && deltaX < 8 && deltaY < 8) {
      return;
    }
    session.lastMoveAt = now;
    session.lastMoveX = currentX;
    session.lastMoveY = currentY;
    postEvent("ui.timeline_drag_move", {
      target: summarizeElement(event.target),
      context: pointerContext(event),
      timeline: summarizeTimelineScope(event.target) || session.timeline,
    });
  }

  function handlePointerUp(event) {
    const session = state.pointerSession;
    if (!session) {
      return;
    }
    if (session.pointerId !== null && typeof event.pointerId === "number" && session.pointerId !== event.pointerId) {
      return;
    }
    const currentX = typeof event.clientX === "number" ? event.clientX : session.lastMoveX;
    const currentY = typeof event.clientY === "number" ? event.clientY : session.lastMoveY;
    postEvent("ui.timeline_drag_end", {
      target: summarizeElement(event.target),
      context: pointerContext(event),
      drag: {
        duration_ms: Math.max(0, Date.now() - session.startedAt),
        delta_x: currentX !== null && session.startX !== null ? Math.round(currentX - session.startX) : null,
        delta_y: currentY !== null && session.startY !== null ? Math.round(currentY - session.startY) : null,
      },
      timeline_before: session.timeline,
      timeline_after: summarizeTimelineScope(event.target) || session.timeline,
    });
    state.pointerSession = null;
  }

  function flushMutationSummary() {
    state.mutationTimer = null;
    const dialogs = document.querySelectorAll("[role='dialog'], dialog").length;
    const buttons = document.querySelectorAll("button, [role='button'], input[type='button'], input[type='submit']").length;
    const editors = document.querySelectorAll("textarea, [contenteditable='true'], [data-slate-editor='true']").length;
    postEvent("page.state", {
      dom_summary: {
        dialog_count: dialogs,
        button_count: buttons,
        editor_count: editors,
      },
    });
  }

  function queueMutationSummary() {
    if (!state.enabled) {
      return;
    }
    if (state.mutationTimer) {
      return;
    }
    state.mutationTimer = window.setTimeout(flushMutationSummary, 800);
  }

  function installObserver() {
    if (state.observer) {
      return;
    }
    state.observer = new MutationObserver(() => {
      queueMutationSummary();
    });
    state.observer.observe(document.documentElement || document.body, {
      subtree: true,
      childList: true,
      attributes: true,
      characterData: false,
    });
  }

  function uninstallObserver() {
    if (state.observer) {
      state.observer.disconnect();
      state.observer = null;
    }
    if (state.mutationTimer) {
      clearTimeout(state.mutationTimer);
      state.mutationTimer = null;
    }
  }

  function startCapture(config) {
    if (state.enabled) {
      stopCapture();
    }
    state.enabled = true;
    state.traceId = String(config?.trace_id || "").trim();
    state.runId = String(config?.run_id || "").trim();
    state.sessionId = String(config?.session_id || "").trim();
    state.captureTaskType = String(config?.capture_task_type || "").trim();
    state.captureTaskLabel = String(config?.capture_task_label || "").trim();
    state.routeKey = String(config?.route_key || "").trim();
    state.endpointUrl = String(config?.endpoint_url || "").trim();
    state.tabId = config?.tab_id ?? null;
    installObserver();
    document.addEventListener("click", handleClick, true);
    document.addEventListener("pointerdown", handlePointerDown, true);
    document.addEventListener("pointermove", handlePointerMove, true);
    document.addEventListener("pointerup", handlePointerUp, true);
    document.addEventListener("pointercancel", handlePointerUp, true);
    document.addEventListener("beforeinput", handleInput, true);
    document.addEventListener("input", handleInput, true);
    document.addEventListener("change", handleChange, true);
    postEvent("page.enter", {
      target: summarizeElement(document.body),
    });
  }

  function stopCapture() {
    postEvent("capture.stop", {});
    state.enabled = false;
    document.removeEventListener("click", handleClick, true);
    document.removeEventListener("pointerdown", handlePointerDown, true);
    document.removeEventListener("pointermove", handlePointerMove, true);
    document.removeEventListener("pointerup", handlePointerUp, true);
    document.removeEventListener("pointercancel", handlePointerUp, true);
    document.removeEventListener("beforeinput", handleInput, true);
    document.removeEventListener("input", handleInput, true);
    document.removeEventListener("change", handleChange, true);
    uninstallObserver();
    state.pointerSession = null;
    state.traceId = "";
    state.runId = "";
    state.sessionId = "";
    state.captureTaskType = "";
    state.captureTaskLabel = "";
    state.routeKey = "";
    state.endpointUrl = "";
    state.tabId = null;
    state.eventSeq = 0;
  }

  function captureStatus() {
    return {
      enabled: state.enabled,
      trace_id: state.traceId,
      run_id: state.runId,
      session_id: state.sessionId,
      capture_task_type: state.captureTaskType,
      capture_task_label: state.captureTaskLabel,
      route_key: state.routeKey,
      page: buildPageSnapshot(),
    };
  }

  chrome.runtime.onMessage.addListener((message, _sender, sendResponse) => {
    if (!message || typeof message !== "object") {
      return false;
    }
    if (message.type === "flow2api_capture_mode_start") {
      startCapture(message.config || {});
      sendResponse({ success: true, status: captureStatus() });
      return false;
    }
    if (message.type === "flow2api_capture_mode_stop") {
      stopCapture();
      sendResponse({ success: true, status: captureStatus() });
      return false;
    }
    if (message.type === "flow2api_capture_mode_status") {
      sendResponse({ success: true, status: captureStatus() });
      return false;
    }
    return false;
  });
})();
