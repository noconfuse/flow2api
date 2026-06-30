(function () {
  if (globalThis.Flow2ApiCaptureMode) {
    return;
  }

  const DEFAULT_CAPTURE_ENDPOINT = "http://127.0.0.1:8000/debug/capture-trace/event";

  function randomId(prefix) {
    return `${prefix}-${Date.now()}-${Math.random().toString(36).slice(2, 8)}`;
  }

  async function sendTabMessage(tabId, message) {
    return await chrome.tabs.sendMessage(tabId, message);
  }

  async function resolveTargetTab({ data, helpers }) {
    const targetProjectId = String(data.project_id || "").trim();
    const activateTab = !!data.job_payload?.activate_tab;
    const canonicalProjectUrl = helpers.buildProjectUrl(targetProjectId);
    let targetTab = await helpers.getPreferredLabsTab(targetProjectId);

    if (!targetTab?.id) {
      targetTab = await chrome.tabs.create({
        url: canonicalProjectUrl,
        active: activateTab,
      });
    } else if (
      targetProjectId &&
      helpers.extractProjectIdFromUrl(targetTab.url || "") !== targetProjectId
    ) {
      targetTab = await chrome.tabs.update(targetTab.id, {
        url: canonicalProjectUrl,
        active: activateTab,
      });
    } else if (activateTab) {
      targetTab = await chrome.tabs.update(targetTab.id, { active: true });
    }

    await helpers.waitForTabReady(targetTab.id);
    await helpers.sleep(800);
    return targetTab;
  }

  async function handleJob({ data, helpers }) {
    const jobType = String(data.job_type || "").trim();
    if (!["capture_mode_start", "capture_mode_stop", "capture_mode_status"].includes(jobType)) {
      throw new Error(`Unsupported capture job_type: ${jobType}`);
    }

    const targetTab = await resolveTargetTab({ data, helpers });
    const routeStatus = await helpers.getSettings();
    const payload = data.job_payload || {};

    if (jobType === "capture_mode_start") {
      const traceId = String(payload.trace_id || randomId("trace")).trim();
      const runId = String(payload.run_id || randomId("run")).trim();
      const sessionId = String(payload.session_id || "video-ui-capture").trim();
      const endpointUrl = String(payload.endpoint_url || DEFAULT_CAPTURE_ENDPOINT).trim();
      const captureTaskType = String(payload.capture_task_type || "text_to_video").trim();
      const captureTaskLabel = String(payload.capture_task_label || "").trim();
      const response = await sendTabMessage(targetTab.id, {
        type: "flow2api_capture_mode_start",
        config: {
          trace_id: traceId,
          run_id: runId,
          session_id: sessionId,
          capture_task_type: captureTaskType,
          capture_task_label: captureTaskLabel,
          route_key: routeStatus.routeKey || "",
          endpoint_url: endpointUrl,
          tab_id: targetTab.id,
        },
      });
      return {
        trace_id: traceId,
        run_id: runId,
        session_id: sessionId,
        endpoint_url: endpointUrl,
        capture_task_type: captureTaskType,
        capture_task_label: captureTaskLabel,
        tab_id: targetTab.id,
        started: true,
        capture_status: response?.status || null,
      };
    }

    if (jobType === "capture_mode_stop") {
      const response = await sendTabMessage(targetTab.id, {
        type: "flow2api_capture_mode_stop",
      });
      return {
        tab_id: targetTab.id,
        stopped: true,
        capture_status: response?.status || null,
      };
    }

    const response = await sendTabMessage(targetTab.id, {
      type: "flow2api_capture_mode_status",
    });
    return {
      tab_id: targetTab.id,
      capture_status: response?.status || null,
    };
  }

  globalThis.Flow2ApiCaptureMode = {
    handleJob,
  };
})();
