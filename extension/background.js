let ws = null;
let reconnectTimeout = null;
let heartbeatInterval = null;
let statusInterval = null;
let bootstrapSettingsPromise = null;
const EXTENSION_BUILD_MARKER = "video-ui-surface-state-20260620-89";
try {
    importScripts("capture-mode.js");
} catch (error) {
    console.warn("[Flow2API] Failed to load capture-mode.js", error);
}
try {
    importScripts("video-ui-workflow.js");
} catch (error) {
    console.warn("[Flow2API] Failed to load video-ui-workflow.js", error);
}

const DEFAULT_SETTINGS = {
    serverUrl: "ws://127.0.0.1:8000/captcha_ws",
    apiKey: "",
    routeKey: "",
    clientLabel: "",
    extensionVersionTag: "",
    extensionSourceFingerprint: "",
};

function normalizeSettings(values = {}) {
    return {
        serverUrl: String(values.serverUrl || DEFAULT_SETTINGS.serverUrl).trim(),
        apiKey: String(values.apiKey || "").trim(),
        routeKey: String(values.routeKey || "").trim(),
        clientLabel: String(values.clientLabel || "").trim(),
        extensionVersionTag: String(values.extensionVersionTag || "").trim(),
        extensionSourceFingerprint: String(values.extensionSourceFingerprint || "").trim(),
    };
}

async function getBootstrapSettings() {
    if (!bootstrapSettingsPromise) {
        bootstrapSettingsPromise = fetch(chrome.runtime.getURL("bootstrap-settings.json"), {
            cache: "no-store"
        })
            .then((response) => {
                if (!response.ok) return {};
                return response.json();
            })
            .then((payload) => normalizeSettings(payload || {}))
            .catch(() => normalizeSettings({}));
    }
    return bootstrapSettingsPromise;
}

async function getSettings() {
    const bootstrap = await getBootstrapSettings();
    return new Promise((resolve) => {
        chrome.storage.local.get(DEFAULT_SETTINGS, (stored) => {
            const merged = normalizeSettings({ ...stored, ...bootstrap });
            // #endregion
            resolve(merged);
        });
    });
}

async function persistBootstrapSettings() {
    const bootstrap = await getBootstrapSettings();
    return new Promise((resolve) => {
        chrome.storage.local.set(bootstrap, () => resolve());
    });
}

function closeSocket() {
    if (heartbeatInterval) clearInterval(heartbeatInterval);
    heartbeatInterval = null;
    if (statusInterval) clearInterval(statusInterval);
    statusInterval = null;
    if (reconnectTimeout) clearTimeout(reconnectTimeout);
    reconnectTimeout = null;
    if (ws) {
        try {
            ws.close();
        } catch (e) {
            console.log("[Flow2API] Close socket error", e);
        }
        ws = null;
    }
}

function sleep(ms) {
    return new Promise(resolve => setTimeout(resolve, ms));
}

function sendSocketMessage(payload) {
    if (!ws || ws.readyState !== WebSocket.OPEN) return;
    if (["register", "status", "heartbeat", "ping"].includes(String(payload?.type || ""))) {
    }
    // #endregion
    ws.send(JSON.stringify(payload));
}

function extractProjectIdFromUrl(rawUrl) {
    try {
        const parsed = new URL(rawUrl);
        const parts = parsed.pathname.split("/").filter(Boolean);
        const projectIndex =
            parts.indexOf("projects") >= 0
                ? parts.indexOf("projects")
                : parts.indexOf("project");
        if (projectIndex >= 0 && projectIndex + 1 < parts.length) {
            return parts[projectIndex + 1];
        }
    } catch (e) {
        return "";
    }
    return "";
}

function buildProjectUrl(projectId) {
    const normalized = String(projectId || "").trim();
    return normalized
        ? `https://labs.google/fx/zh/tools/flow/project/${normalized}`
        : "https://labs.google/fx/zh/tools/flow";
}

function isLikelyApplicationErrorText(rawValue) {
    const raw = String(rawValue || "").toLowerCase();
    return (
        raw.includes("application error") ||
        raw.includes("a client-side exception has occurred") ||
        raw.includes("chrome-error://chromewebdata")
    );
}

function isApplicationErrorTabCandidate(tabLike) {
    const title = String(tabLike?.title || "");
    const url = String(tabLike?.url || "");
    return isLikelyApplicationErrorText(`${title} ${url}`);
}

function sortLabsTabsByPreference(tabs, projectId = "") {
    const normalizedProjectId = String(projectId || "").trim();
    return [...(Array.isArray(tabs) ? tabs : [])].sort((left, right) => {
        const leftProjectId = extractProjectIdFromUrl(left?.url || "");
        const rightProjectId = extractProjectIdFromUrl(right?.url || "");
        const leftExactProject = normalizedProjectId && leftProjectId === normalizedProjectId ? 1 : 0;
        const rightExactProject = normalizedProjectId && rightProjectId === normalizedProjectId ? 1 : 0;
        if (leftExactProject !== rightExactProject) {
            return rightExactProject - leftExactProject;
        }

        const leftHealthy = isApplicationErrorTabCandidate(left) ? 0 : 1;
        const rightHealthy = isApplicationErrorTabCandidate(right) ? 0 : 1;
        if (leftHealthy !== rightHealthy) {
            return rightHealthy - leftHealthy;
        }

        const leftProjectLike = String(left?.url || "").includes("/project/") || String(left?.url || "").includes("/projects/") ? 1 : 0;
        const rightProjectLike = String(right?.url || "").includes("/project/") || String(right?.url || "").includes("/projects/") ? 1 : 0;
        if (leftProjectLike !== rightProjectLike) {
            return rightProjectLike - leftProjectLike;
        }

        const leftFxLike = String(left?.url || "").includes("/fx") ? 1 : 0;
        const rightFxLike = String(right?.url || "").includes("/fx") ? 1 : 0;
        if (leftFxLike !== rightFxLike) {
            return rightFxLike - leftFxLike;
        }

        const leftActive = left?.active ? 1 : 0;
        const rightActive = right?.active ? 1 : 0;
        if (leftActive !== rightActive) {
            return rightActive - leftActive;
        }

        const leftLastAccessed = Number(left?.lastAccessed || 0);
        const rightLastAccessed = Number(right?.lastAccessed || 0);
        if (leftLastAccessed !== rightLastAccessed) {
            return rightLastAccessed - leftLastAccessed;
        }

        return Number(right?.id || 0) - Number(left?.id || 0);
    });
}

function pickBestLabsTabCandidate(tabs, projectId = "") {
    const sortedTabs = sortLabsTabsByPreference(tabs, projectId);
    return sortedTabs[0] || null;
}

async function queryWorkerStatus() {
    const tabs = await chrome.tabs.query({ url: ["https://labs.google/*"] });
    const preferredTab = await pickBestLabsTab();

    const status = {
        capabilities: ["captcha_token", "session_summary", "ui_jobs", "credential_probe", "browser_submit_request"],
        worker_mode: "captcha_worker_v1",
        session_state: preferredTab ? "labs_tab_open" : "no_labs_tab",
        current_email: "",
        page_url: preferredTab?.url || "",
        page_title: preferredTab?.title || "",
        project_id: extractProjectIdFromUrl(preferredTab?.url || ""),
        tool_name: "",
        browser_user_agent: "",
        last_error: preferredTab && isApplicationErrorTabCandidate(preferredTab) ? "application_error_tab_candidate" : "",
        labs_tab_count: tabs.length,
    };

    if (!preferredTab?.id) {
        return status;
    }

    try {
        const results = await chrome.scripting.executeScript({
            target: { tabId: preferredTab.id },
            world: "MAIN",
            func: async () => {
                const summary = {
                    current_email: "",
                    page_url: location.href,
                    page_title: document.title,
                    project_id: "",
                    tool_name: "",
                    session_state: document.readyState,
                    browser_user_agent: navigator.userAgent,
                    last_error: "",
                };

                try {
                    const url = new URL(location.href);
                    const parts = url.pathname.split("/").filter(Boolean);
                    const projectIndex =
                        parts.indexOf("projects") >= 0
                            ? parts.indexOf("projects")
                            : parts.indexOf("project");
                    if (projectIndex >= 0 && projectIndex + 1 < parts.length) {
                        summary.project_id = parts[projectIndex + 1];
                    }
                    const toolsIndex = parts.indexOf("tools");
                    if (toolsIndex >= 0 && toolsIndex + 1 < parts.length) {
                        summary.tool_name = parts[toolsIndex + 1];
                    }
                } catch (error) {
                    summary.last_error = String(error?.message || error);
                }

                try {
                    const response = await fetch("/fx/api/auth/session", {
                        credentials: "include",
                        headers: { accept: "application/json" },
                    });
                    if (response.ok) {
                        const payload = await response.json();
                        summary.current_email =
                            payload?.user?.email ||
                            payload?.user?.login ||
                            payload?.email ||
                            "";
                    } else {
                        summary.last_error = `auth_session_http_${response.status}`;
                    }
                } catch (error) {
                    summary.last_error = String(error?.message || error);
                }

                return summary;
            },
        });

        if (results && results[0] && results[0].result) {
            return { ...status, ...results[0].result };
        }
    } catch (e) {
        status.last_error = e.message || "status_script_failed";
    }

    return status;
}

async function sendRouteStatus(reason = "heartbeat") {
    if (!ws || ws.readyState !== WebSocket.OPEN) return;
    const settings = await getSettings();
    const status = await queryWorkerStatus();
    sendSocketMessage({
        type: reason === "heartbeat" ? "heartbeat" : "status",
        reason,
        route_key: settings.routeKey,
        client_label: settings.clientLabel,
        extension_version_tag: settings.extensionVersionTag,
        extension_source_fingerprint: settings.extensionSourceFingerprint,
        ...status,
    });
}

async function getPreferredLabsTab(projectId = "") {
    return await pickBestLabsTab(projectId);
}

function jobRequiresCanonicalProjectPage(jobType = "") {
    return [
        "ensure_project_page",
        "video_ui_probe",
        "video_ui_prepare",
        "video_ui_submit_probe",
        "video_ui_type_probe",
        "video_ui_submit",
        "video_submode_probe",
        "video_ui_workflow",
    ].includes(String(jobType || "").trim());
}

async function collectExecutionContext(tabId) {
    try {
        const results = await chrome.scripting.executeScript({
            target: { tabId },
            world: "MAIN",
            func: async () => {
                const context = {
                    page_url: location.href,
                    page_title: document.title,
                    ready_state: document.readyState,
                    browser_user_agent: navigator.userAgent,
                    current_email: "",
                    project_id: "",
                    tool_name: "",
                    last_error: "",
                };

                try {
                    const url = new URL(location.href);
                    const parts = url.pathname.split("/").filter(Boolean);
                    const projectIndex =
                        parts.indexOf("projects") >= 0
                            ? parts.indexOf("projects")
                            : parts.indexOf("project");
                    if (projectIndex >= 0 && projectIndex + 1 < parts.length) {
                        context.project_id = parts[projectIndex + 1];
                    }
                    const toolsIndex = parts.indexOf("tools");
                    if (toolsIndex >= 0 && toolsIndex + 1 < parts.length) {
                        context.tool_name = parts[toolsIndex + 1];
                    }
                } catch (error) {
                    context.last_error = String(error?.message || error);
                }

                try {
                    const response = await fetch("/fx/api/auth/session", {
                        credentials: "include",
                        headers: { accept: "application/json" },
                    });
                    if (response.ok) {
                        const payload = await response.json();
                        context.current_email =
                            payload?.user?.email ||
                            payload?.user?.login ||
                            payload?.email ||
                            "";
                    } else if (!context.last_error) {
                        context.last_error = `auth_session_http_${response.status}`;
                    }
                } catch (error) {
                    if (!context.last_error) {
                        context.last_error = String(error?.message || error);
                    }
                }

                return context;
            },
        });
        return results?.[0]?.result || {};
    } catch (error) {
        return {
            last_error: String(error?.message || error || "collect_execution_context_failed"),
        };
    }
}

function getLabsTabContextScore(tab, projectId = "", context = null) {
    const normalizedProjectId = String(projectId || "").trim();
    const currentContext = context || {};
    const contextProjectId = extractProjectIdFromUrl(currentContext.page_url || "") || extractProjectIdFromUrl(tab?.url || "");
    const exactProject = normalizedProjectId && contextProjectId === normalizedProjectId ? 1 : 0;
    const healthy = isApplicationErrorContext(currentContext) || isApplicationErrorTabCandidate(tab) ? 0 : 1;
    const hasEmail = String(currentContext.current_email || "").trim() ? 1 : 0;
    const projectLike =
        String(currentContext.page_url || tab?.url || "").includes("/project/") ||
        String(currentContext.page_url || tab?.url || "").includes("/projects/")
            ? 1
            : 0;
    const active = tab?.active ? 1 : 0;
    const lastAccessed = Number(tab?.lastAccessed || 0);
    const tabId = Number(tab?.id || 0);
    return [exactProject, healthy, hasEmail, projectLike, active, lastAccessed, tabId];
}

function compareLabsTabScores(leftScore, rightScore) {
    const maxLength = Math.max(leftScore.length, rightScore.length);
    for (let index = 0; index < maxLength; index += 1) {
        const leftValue = Number(leftScore[index] || 0);
        const rightValue = Number(rightScore[index] || 0);
        if (leftValue !== rightValue) {
            return rightValue - leftValue;
        }
    }
    return 0;
}

async function pickBestLabsTab(projectId = "") {
    const tabs = await chrome.tabs.query({ url: ["https://labs.google/*"] });
    const sortedTabs = sortLabsTabsByPreference(tabs, projectId);
    if (!sortedTabs.length) {
        return null;
    }

    const inspectedTabs = await Promise.all(
        sortedTabs.slice(0, Math.min(sortedTabs.length, 6)).map(async (tab) => {
            let context = null;
            try {
                context = tab?.id ? await collectExecutionContext(tab.id) : null;
            } catch (error) {
                context = {
                    page_title: String(tab?.title || ""),
                    page_url: String(tab?.url || ""),
                    last_error: String(error?.message || error || ""),
                };
            }
            return { tab, context };
        })
    );

    inspectedTabs.sort((left, right) =>
        compareLabsTabScores(
            getLabsTabContextScore(left.tab, projectId, left.context),
            getLabsTabContextScore(right.tab, projectId, right.context)
        )
    );

    return inspectedTabs[0]?.tab || pickBestLabsTabCandidate(sortedTabs, projectId);
}

function normalizeCookieItem(cookie) {
    if (!cookie || !cookie.name) {
        return null;
    }
    const normalized = {
        name: String(cookie.name || ""),
        value: String(cookie.value || ""),
        domain: String(cookie.domain || ""),
        path: String(cookie.path || "/"),
        secure: !!cookie.secure,
        httpOnly: !!cookie.httpOnly,
        session: !!cookie.session,
        storeId: String(cookie.storeId || ""),
    };
    if (cookie.sameSite) {
        normalized.sameSite = String(cookie.sameSite);
    }
    if (typeof cookie.expirationDate === "number" && Number.isFinite(cookie.expirationDate)) {
        normalized.expires = Number(cookie.expirationDate);
    }
    return normalized;
}

async function collectBrowserCredentialSnapshot(tabId) {
    const context = await collectExecutionContext(tabId);
    const domains = ["labs.google", "google.com"];
    const cookieErrors = [];
    const seen = new Set();
    const cookieItems = [];

    for (const domain of domains) {
        try {
            const cookies = await chrome.cookies.getAll({ domain });
            for (const cookie of cookies || []) {
                const normalized = normalizeCookieItem(cookie);
                if (!normalized) {
                    continue;
                }
                const stableKey = JSON.stringify(
                    [normalized.name, normalized.domain, normalized.path, normalized.storeId],
                    null,
                    0
                );
                if (seen.has(stableKey)) {
                    continue;
                }
                seen.add(stableKey);
                cookieItems.push(normalized);
            }
        } catch (error) {
            cookieErrors.push(`${domain}:${String(error?.message || error)}`);
        }
    }

    const cookieNames = Array.from(
        new Set(cookieItems.map((item) => String(item.name || "").trim()).filter(Boolean))
    ).sort();
    const sessionTokenPresent = cookieItems.some((item) => {
        const name = String(item.name || "").trim();
        return (
            name === "__Secure-next-auth.session-token" ||
            name === "next-auth.session-token"
        );
    });

    return {
        result_version: "account_credential_probe_v1",
        current_email: String(context.current_email || "").trim(),
        page_url: String(context.page_url || "").trim(),
        project_id: String(context.project_id || "").trim(),
        tool_name: String(context.tool_name || "").trim(),
        ready_state: String(context.ready_state || "").trim(),
        cookie_count: cookieItems.length,
        cookie_names: cookieNames,
        session_token_present: sessionTokenPresent,
        cookie_items: cookieItems,
        cookie_errors: cookieErrors,
    };
}

function waitForTabReady(tabId, timeoutMs = 12000) {
    return new Promise((resolve) => {
        let settled = false;
        const finish = () => {
            if (settled) return;
            settled = true;
            chrome.tabs.onUpdated.removeListener(onUpdated);
            clearTimeout(timer);
            resolve();
        };
        const onUpdated = (updatedTabId, changeInfo) => {
            if (updatedTabId === tabId && changeInfo.status === "complete") {
                finish();
            }
        };
        const timer = setTimeout(finish, timeoutMs);

        chrome.tabs.onUpdated.addListener(onUpdated);
        chrome.tabs.get(tabId, (tab) => {
            if (chrome.runtime.lastError) {
                finish();
                return;
            }
            if (tab && tab.status === "complete") {
                finish();
            }
        });
    });
}

function isApplicationErrorContext(context) {
    const title = String(context?.page_title || "");
    const lastError = String(context?.last_error || "");
    const pageUrl = String(context?.page_url || "");
    return isLikelyApplicationErrorText(`${title} ${lastError} ${pageUrl}`);
}

async function hasApplicationErrorSurface(tabId) {
    try {
        const results = await chrome.scripting.executeScript({
            target: { tabId },
            world: "MAIN",
            func: () => {
                const title = String(document.title || "");
                const bodyText = String(document.body?.innerText || "").replace(/\s+/g, " ").trim();
                const raw = `${title} ${bodyText}`.toLowerCase();
                return {
                    has_error:
                        raw.includes("application error") ||
                        raw.includes("a client-side exception has occurred while loading labs.google"),
                    title,
                    body_text_prefix: bodyText.slice(0, 240),
                };
            },
        });
        return results?.[0]?.result || { has_error: false, title: "", body_text_prefix: "" };
    } catch (error) {
        return {
            has_error: false,
            title: "",
            body_text_prefix: "",
            detect_error: String(error?.message || error || ""),
        };
    }
}

async function recoverApplicationErrorTab(tabId, projectId, activateTab) {
    try {
        await chrome.tabs.update(tabId, {
            url: buildProjectUrl(projectId),
            active: !!activateTab,
        });
        await waitForTabReady(tabId);
        await sleep(1800);
    } catch (error) {
        try {
            await chrome.tabs.reload(tabId);
            await waitForTabReady(tabId);
            await sleep(1800);
        } catch (reloadError) {
            return {
                recovered: false,
                error: String(reloadError?.message || reloadError || error || "recover_application_error_failed"),
            };
        }
    }
    const after = await collectExecutionContext(tabId);
    const surface = await hasApplicationErrorSurface(tabId);
    return {
        recovered: !isApplicationErrorContext(after) && !surface?.has_error,
        after,
        surface,
    };
}

async function connectWS() {
    if (ws && (ws.readyState === WebSocket.OPEN || ws.readyState === WebSocket.CONNECTING)) return;

    const settings = await getSettings();
    const url = new URL(settings.serverUrl || DEFAULT_SETTINGS.serverUrl);
    if (settings.apiKey) {
        url.searchParams.set("key", settings.apiKey);
    }
    if (settings.routeKey) {
        url.searchParams.set("route_key", settings.routeKey);
    }
    if (settings.clientLabel) {
        url.searchParams.set("client_label", settings.clientLabel);
    }

    // #endregion
    ws = new WebSocket(url.toString());

    ws.onopen = () => {
        console.log("[Flow2API] Background connected to WebSocket", url.toString());
        // #endregion
        sendSocketMessage({
            type: "register",
            route_key: settings.routeKey,
            client_label: settings.clientLabel,
            extension_version_tag: settings.extensionVersionTag,
            extension_source_fingerprint: settings.extensionSourceFingerprint,
        });
        if (heartbeatInterval) clearInterval(heartbeatInterval);
        heartbeatInterval = setInterval(() => {
            if (ws && ws.readyState === WebSocket.OPEN) {
                sendSocketMessage({ type: "ping" });
            }
        }, 20000);
        sendRouteStatus("register");
        if (statusInterval) clearInterval(statusInterval);
        statusInterval = setInterval(() => {
            sendRouteStatus("heartbeat");
        }, 30000);
    };

    let tokenQueue = Promise.resolve();

    ws.onmessage = async (event) => {
        let data;
        try {
            data = JSON.parse(event.data);
        } catch (e) {
            return;
        }

        // #endregion
        if (data.type === "register_ack") {
            console.log("[Flow2API] Registered route key:", data.route_key || "(empty)");
            return;
        }

        if (data.type === "get_token") {
            tokenQueue = tokenQueue.then(() => handleGetToken(data)).catch(err => {
                console.error("[Flow2API] Queue Error:", err);
            });
            return;
        }

        if (data.type === "submit_json") {
            tokenQueue = tokenQueue.then(() => handleSubmitJson(data)).catch(err => {
                console.error("[Flow2API] Queue Error:", err);
                sendSocketMessage({
                    req_id: data.req_id,
                    status: "error",
                    error: err.message || "submit_json failed",
                });
            });
            return;
        }

        if (data.type === "submit_request") {
            tokenQueue = tokenQueue.then(() => handleSubmitRequest(data)).catch(err => {
                console.error("[Flow2API] Queue Error:", err);
                sendSocketMessage({
                    req_id: data.req_id,
                    status: "error",
                    error: err.message || "submit_request failed",
                });
            });
            return;
        }

        if (data.type === "run_job") {
            tokenQueue = tokenQueue.then(() => handleRunJob(data)).catch(err => {
                console.error("[Flow2API] Queue Error:", err);
                sendSocketMessage({
                    req_id: data.req_id,
                    job_id: data.job_id || "",
                    job_type: data.job_type || "",
                    status: "error",
                    error: err.message || "run_job failed",
                });
            });
        }
    };

    ws.onclose = () => {
        console.log("[Flow2API] WebSocket Closed. Reconnecting in 2s...");
        // #endregion
        ws = null;
        if (heartbeatInterval) clearInterval(heartbeatInterval);
        if (reconnectTimeout) clearTimeout(reconnectTimeout);
        reconnectTimeout = setTimeout(connectWS, 2000);
    };

    ws.onerror = (e) => {
        console.log("[Flow2API] WebSocket Error", e);
    };
}

async function handleGetToken(data) {
    let tempTabId = null;
    try {
        const targetProjectId = String(data.project_id || "").trim();
        let targetTab = await getPreferredLabsTab(targetProjectId);
        if (!targetTab?.id) {
            console.log("[Flow2API] No reusable Labs tab found, opening a temporary tab for get_token...");
            targetTab = await chrome.tabs.create({ url: buildProjectUrl(targetProjectId), active: false });
            tempTabId = targetTab.id;
        } else if (
            targetProjectId &&
            extractProjectIdFromUrl(targetTab.url || "") !== targetProjectId
        ) {
            console.log("[Flow2API] Reusing Labs tab but navigating to target project for get_token:", targetProjectId);
            await chrome.tabs.update(targetTab.id, { url: buildProjectUrl(targetProjectId), active: false });
        } else {
            console.log("[Flow2API] Reusing existing Labs tab for get_token:", targetTab.url || "");
        }

        await waitForTabReady(targetTab.id);
        await sleep(1200);
        const beforeContext = await collectExecutionContext(targetTab.id);

        let successResponse = null;
        let lastErrorMsg = "No response from tab.";
        const scriptTimeoutMs = data.action === "VIDEO_GENERATION" ? 30000 : 20000;

        try {
            const results = await chrome.scripting.executeScript({
                target: { tabId: targetTab.id },
                world: "MAIN",
                func: async (action, timeoutMs) => {
                    return new Promise((resolve, reject) => {
                        let settled = false;
                        const finish = (fn, value) => {
                            if (settled) return;
                            settled = true;
                            fn(value);
                        };
                        try {
                            function run() {
                                grecaptcha.enterprise.ready(function() {
                                    grecaptcha.enterprise.execute("6LdsFiUsAAAAAIjVDZcuLhaHiDn5nnHVXVRQGeMV", { action: action })
                                        .then(token => finish(resolve, token))
                                        .catch(err => finish(reject, err.message || "reCAPTCHA evaluation failed internally"));
                                });
                            }

                            if (typeof grecaptcha !== "undefined" && grecaptcha.enterprise) {
                                run();
                            } else {
                                const s = document.createElement("script");
                                s.src = "https://www.google.com/recaptcha/enterprise.js?render=6LdsFiUsAAAAAIjVDZcuLhaHiDn5nnHVXVRQGeMV";
                                s.onload = run;
                                s.onerror = () => finish(reject, "Failed to load enterprise.js via network");
                                document.head.appendChild(s);
                            }

                            setTimeout(() => finish(reject, "Timeout generating reCAPTCHA locally"), timeoutMs);
                        } catch (e) {
                            finish(reject, e.message);
                        }
                    });
                },
                args: [data.action || "IMAGE_GENERATION", scriptTimeoutMs]
            });

            if (results && results[0] && results[0].result) {
                successResponse = { status: "success", token: results[0].result };
            }
        } catch (e) {
            lastErrorMsg = e.message || "Script execution failed";
        }
        const afterContext = await collectExecutionContext(targetTab.id);
        const debugContext = {
            tab_id: targetTab.id,
            reused_existing_tab: !tempTabId,
            target_project_id: targetProjectId,
            request_action: data.action || "IMAGE_GENERATION",
            before: beforeContext,
            after: afterContext,
        };

        if (successResponse) {
            sendSocketMessage({
                req_id: data.req_id,
                status: successResponse.status,
                token: successResponse.token,
                debug_context: debugContext,
            });
        } else {
            sendSocketMessage({
                req_id: data.req_id,
                status: "error",
                error: "Extension script failed: " + lastErrorMsg,
                debug_context: debugContext,
            });
        }
    } catch (err) {
        sendSocketMessage({
            req_id: data.req_id,
            status: "error",
            error: err.message,
            debug_context: {
                request_action: data.action || "IMAGE_GENERATION",
                last_error: String(err?.message || err),
            },
        });
    } finally {
        if (tempTabId) {
            try {
                await chrome.tabs.remove(tempTabId);
                console.log("[Flow2API] Closed temporary token tab.");
            } catch (e) {
                console.log("[Flow2API] Error closing tab:", e);
            }
        }
    }
}

function buildAllowedSubmitHeaders(rawHeaders) {
    const allowedHeaders = {};
    for (const [rawKey, rawValue] of Object.entries(rawHeaders || {})) {
        const key = String(rawKey || "").trim();
        if (!key) continue;
        const lowerKey = key.toLowerCase();
        if (
            [
                "authorization",
                "content-type",
                "accept",
                "origin",
                "referer",
                "x-client-data",
                "x-browser-validation",
                "x-goog-api-client",
                "x-goog-authuser",
                "accept-language",
                "sec-ch-ua",
                "sec-ch-ua-mobile",
                "sec-ch-ua-platform",
            ].includes(lowerKey) ||
            lowerKey.startsWith("x-upload-")
        ) {
            allowedHeaders[key] = String(rawValue ?? "");
        }
    }
    return allowedHeaders;
}

async function resolveBrowserSubmitTab(targetProjectId) {
    let preferredTab = await getPreferredLabsTab(targetProjectId);
    if (!preferredTab?.id) {
        throw new Error("No Google Labs tab available for browser submit");
    }
    if (
        targetProjectId &&
        extractProjectIdFromUrl(preferredTab.url || "") !== targetProjectId
    ) {
        console.log("[Flow2API] Navigating submit tab to target project:", targetProjectId);
        preferredTab = await chrome.tabs.update(preferredTab.id, {
            url: buildProjectUrl(targetProjectId),
            active: false,
        });
        await waitForTabReady(preferredTab.id);
        await sleep(1200);
    }
    return preferredTab;
}

async function executeBrowserSubmitRequest(tabId, request) {
    const scriptResults = await chrome.scripting.executeScript({
        target: { tabId },
        world: "MAIN",
        func: async (submitUrl, rawMethod, rawHeaders, rawBodyMode, rawBodyValue, responseHeaderNames) => {
            const buildAllowedHeaders = (inputHeaders) => {
                const allowedHeaders = {};
                for (const [rawKey, rawValue] of Object.entries(inputHeaders || {})) {
                    const key = String(rawKey || "").trim();
                    if (!key) continue;
                    const lowerKey = key.toLowerCase();
                    if (
                        [
                            "authorization",
                            "content-type",
                            "accept",
                            "origin",
                            "referer",
                            "x-client-data",
                            "x-browser-validation",
                            "x-goog-api-client",
                            "x-goog-authuser",
                            "accept-language",
                            "sec-ch-ua",
                            "sec-ch-ua-mobile",
                            "sec-ch-ua-platform",
                        ].includes(lowerKey) ||
                        lowerKey.startsWith("x-upload-")
                    ) {
                        allowedHeaders[key] = String(rawValue ?? "");
                    }
                }
                return allowedHeaders;
            };
            const decodeBase64ToBytes = (encoded) => {
                const normalized = String(encoded || "").replace(/\s+/g, "");
                const binary = atob(normalized);
                const bytes = new Uint8Array(binary.length);
                for (let i = 0; i < binary.length; i += 1) {
                    bytes[i] = binary.charCodeAt(i);
                }
                return bytes;
            };
            const collectResponseHeaders = (response, headerNames) => {
                const headers = {};
                const requested = Array.isArray(headerNames) ? headerNames : [];
                if (requested.length > 0) {
                    for (const rawName of requested) {
                        const name = String(rawName || "").trim();
                        if (!name) continue;
                        headers[name.toLowerCase()] = response.headers.get(name) || "";
                    }
                    return headers;
                }
                for (const [name, value] of response.headers.entries()) {
                    headers[String(name || "").toLowerCase()] = String(value || "");
                }
                return headers;
            };

            const requestOptions = {
                method: String(rawMethod || "POST").trim().toUpperCase() || "POST",
                credentials: "include",
                mode: "cors",
                headers: buildAllowedHeaders(rawHeaders),
            };
            const bodyMode = String(rawBodyMode || "json").trim().toLowerCase();
            if (bodyMode === "json") {
                requestOptions.body = JSON.stringify(rawBodyValue || {});
            } else if (bodyMode === "text") {
                requestOptions.body = String(rawBodyValue ?? "");
            } else if (bodyMode === "base64") {
                requestOptions.body = decodeBase64ToBytes(rawBodyValue);
            }

            let response = null;
            let text = "";
            try {
                response = await fetch(submitUrl, requestOptions);
                text = await response.text();
            } catch (error) {
                return {
                    ok: false,
                    http_status: 0,
                    text: "",
                    response: null,
                    response_headers: {},
                    error: String(error?.message || error || "fetch failed"),
                };
            }

            let parsed = null;
            try {
                parsed = text ? JSON.parse(text) : null;
            } catch (error) {
                parsed = null;
            }

            return {
                ok: response.ok,
                http_status: response.status,
                text,
                response: parsed,
                content_type: response.headers.get("content-type") || "",
                response_headers: collectResponseHeaders(response, responseHeaderNames),
            };
        },
        args: [
            request.url || "",
            request.method || "POST",
            request.headers || {},
            request.bodyMode || "json",
            request.bodyValue,
            Array.isArray(request.responseHeaderNames) ? request.responseHeaderNames : [],
        ],
    });

    const result = scriptResults?.[0]?.result;
    if (!result || typeof result !== "object") {
        throw new Error("Browser submit returned invalid result");
    }
    return result;
}

async function handleSubmitJson(data) {
    const targetProjectId = String(data.project_id || "").trim();
    const preferredTab = await resolveBrowserSubmitTab(targetProjectId);
    const beforeContext = await collectExecutionContext(preferredTab.id);
    const result = await executeBrowserSubmitRequest(preferredTab.id, {
        url: data.url || "",
        method: "POST",
        headers: buildAllowedSubmitHeaders(data.headers || {}),
        bodyMode: "json",
        bodyValue: data.payload || {},
        responseHeaderNames: [],
    });
    const afterContext = await collectExecutionContext(preferredTab.id);

    sendSocketMessage({
        req_id: data.req_id,
        status: result.ok ? "success" : "error",
        http_status: result.http_status || 0,
        response: result.response ?? null,
        text: result.text || "",
        error: result.error || "",
        content_type: result.content_type || "",
        response_headers: result.response_headers || {},
        debug_context: {
            tab_id: preferredTab.id,
            target_project_id: targetProjectId,
            request_url: data.url || "",
            before: beforeContext,
            after: afterContext,
        },
    });
}

async function handleSubmitRequest(data) {
    const targetProjectId = String(data.project_id || "").trim();
    const preferredTab = await resolveBrowserSubmitTab(targetProjectId);
    const beforeContext = await collectExecutionContext(preferredTab.id);
    const result = await executeBrowserSubmitRequest(preferredTab.id, {
        url: data.url || "",
        method: data.method || "POST",
        headers: buildAllowedSubmitHeaders(data.headers || {}),
        bodyMode: data.body_mode || "json",
        bodyValue: data.body,
        responseHeaderNames: Array.isArray(data.response_header_names) ? data.response_header_names : [],
    });
    const afterContext = await collectExecutionContext(preferredTab.id);

    sendSocketMessage({
        req_id: data.req_id,
        status: result.ok ? "success" : "error",
        http_status: result.http_status || 0,
        response: result.response ?? null,
        text: result.text || "",
        error: result.error || "",
        content_type: result.content_type || "",
        response_headers: result.response_headers || {},
        debug_context: {
            tab_id: preferredTab.id,
            target_project_id: targetProjectId,
            request_url: data.url || "",
            request_method: String(data.method || "POST").trim().toUpperCase() || "POST",
            request_body_mode: String(data.body_mode || "json").trim().toLowerCase() || "json",
            before: beforeContext,
            after: afterContext,
        },
    });
}

async function captureRuntimeUiState(tabId) {
    const results = await chrome.scripting.executeScript({
        target: { tabId },
        world: "MAIN",
        func: async () => {
            const flowState = window.FLOW_MAIN_PROMPT_BOX_STATE || null;
            const activeElement = document.activeElement;
            const buttons = Array.from(document.querySelectorAll("button"))
                .slice(0, 8)
                .map((button) => (button.innerText || button.getAttribute("aria-label") || "").trim())
                .filter(Boolean);
            const textareas = Array.from(document.querySelectorAll("textarea"))
                .slice(0, 4)
                .map((item) => ({
                    placeholder: item.getAttribute("placeholder") || "",
                    value_length: (item.value || "").length,
                }));
            return {
                href: location.href,
                title: document.title,
                ready_state: document.readyState,
                project_id: (() => {
                    try {
                        const parts = new URL(location.href).pathname.split("/").filter(Boolean);
                        const projectIndex =
                            parts.indexOf("projects") >= 0
                                ? parts.indexOf("projects")
                                : parts.indexOf("project");
                        return projectIndex >= 0 && projectIndex + 1 < parts.length
                            ? parts[projectIndex + 1]
                            : "";
                    } catch (e) {
                        return "";
                    }
                })(),
                flow_prompt_box_state: flowState
                    ? {
                        imageOrVideoMode: flowState.imageOrVideoMode || "",
                        selectedVideoModelFamily: flowState.selectedVideoModelFamily || "",
                        selectedModelFamily: flowState.selectedModelFamily || "",
                    }
                    : null,
                active_element: activeElement
                    ? {
                        tag: activeElement.tagName || "",
                        aria_label: activeElement.getAttribute?.("aria-label") || "",
                    }
                    : null,
                buttons,
                textareas,
            };
        },
    });
    return results?.[0]?.result || {};
}

async function runVideoUiProbe(tabId) {
    const results = await chrome.scripting.executeScript({
        target: { tabId },
        world: "MAIN",
        func: async () => {
            const visible = (el) => {
                if (!el) return false;
                const rect = el.getBoundingClientRect();
                return rect.width > 8 && rect.height > 8;
            };
            const textOf = (el) =>
                String(
                    el?.innerText ||
                    el?.textContent ||
                    el?.getAttribute?.("aria-label") ||
                    ""
                )
                    .replace(/\s+/g, " ")
                    .trim();
            const clickFirstButton = (predicates, label) => {
                for (const button of Array.from(document.querySelectorAll("button"))) {
                    if (!visible(button)) continue;
                    const text = textOf(button);
                    if (!text) continue;
                    if (predicates.some((predicate) => predicate(text))) {
                        button.click();
                        return `${label}:${text.slice(0, 80)}`;
                    }
                }
                return "";
            };
            const isClickableLike = (node) => {
                if (!node || !visible(node)) return false;
                if (node.tagName === "BUTTON") return true;
                if (node.getAttribute?.("role") === "button") return true;
                if (Number(node.tabIndex) >= 0) return true;
                const className = String(node.className || "");
                const style = window.getComputedStyle(node);
                return (
                    style.cursor === "pointer" ||
                    /\b(cursor-pointer|clickable|chip|segmented|toggle)\b/i.test(className)
                );
            };
            const collectVisibleTabs = () =>
                Array.from(document.querySelectorAll("[role='tab']"))
                    .filter((node) => visible(node))
                    .map((node) => {
                        const text = textOf(node);
                        const className = String(node.className || "");
                        const tablist = node.closest("[role='tablist']");
                        return {
                            text: text.slice(0, 120),
                            tag: String(node.tagName || "").toLowerCase(),
                            disabled: !!node.disabled || node.getAttribute("aria-disabled") === "true",
                            aria_selected: String(node.getAttribute("aria-selected") || ""),
                            data_state: String(node.getAttribute("data-state") || ""),
                            aria_controls: String(node.getAttribute("aria-controls") || ""),
                            class_name: className.slice(0, 160),
                            tablist_text: textOf(tablist).slice(0, 200),
                            selected:
                                node.getAttribute("aria-selected") === "true" ||
                                node.getAttribute("data-state") === "active" ||
                                /\b(selected|active)\b/i.test(className),
                        };
                    });
            const collectSubmodeButtons = () =>
                collectVisibleTabs()
                    .filter((item) =>
                        item.text.includes("帧") ||
                        item.text.includes("素材") ||
                        item.text.includes("文本") ||
                        item.text.includes("提示词")
                    )
                    .slice(0, 8);
            const collectVisibleClickables = () => {
                const selectors = [
                    "button",
                    "[role='button']",
                    "[role='tab']",
                    "[aria-haspopup]",
                    "[aria-selected]",
                    "[data-state]",
                    "[tabindex]",
                ].join(", ");
                return Array.from(document.querySelectorAll(selectors))
                    .filter((node) => visible(node))
                    .map((node) => {
                        const text = textOf(node);
                        const className = String(node.className || "");
                        if (!text) return null;
                        return {
                            text: text.slice(0, 160),
                            tag: String(node.tagName || "").toLowerCase(),
                            role: String(node.getAttribute?.("role") || ""),
                            aria_label: String(node.getAttribute?.("aria-label") || "").slice(0, 120),
                            aria_selected: String(node.getAttribute?.("aria-selected") || ""),
                            aria_expanded: String(node.getAttribute?.("aria-expanded") || ""),
                            aria_haspopup: String(node.getAttribute?.("aria-haspopup") || ""),
                            data_state: String(node.getAttribute?.("data-state") || ""),
                            class_name: className.slice(0, 160),
                        };
                    })
                    .filter(Boolean)
                    .slice(0, 80);
            };
            const clickVideoSubmode = (preferredText) => {
                const compactPreferred = String(preferredText || "").replace(/\s+/g, "").trim();
                const exactTabCandidates = Array.from(document.querySelectorAll("[role='tab']")).filter((node) => {
                    if (!visible(node)) return false;
                    const compactText = textOf(node).replace(/\s+/g, "").trim();
                    if (!compactText || !compactText.includes(compactPreferred)) return false;
                    if (compactPreferred === "帧" && compactText.includes("素材")) return false;
                    if (compactPreferred === "素材" && compactText.includes("帧")) return false;
                    return true;
                }).sort((a, b) => {
                    const aSelected = String(a.getAttribute("aria-selected") || "").toLowerCase() === "true" ? 1 : 0;
                    const bSelected = String(b.getAttribute("aria-selected") || "").toLowerCase() === "true" ? 1 : 0;
                    return aSelected - bSelected;
                });
                const fallbackCandidates = Array.from(document.querySelectorAll("button, [role='button'], [role='tab']")).filter((node) => {
                    if (!visible(node)) return false;
                    const compactText = textOf(node).replace(/\s+/g, "").trim();
                    return !!compactText && compactText.includes(compactPreferred);
                });
                for (const node of [...exactTabCandidates, ...fallbackCandidates]) {
                    try {
                        node.focus?.();
                    } catch (_error) {
                        // ignore focus error
                    }
                    clickReal(node);
                    try {
                        node.dispatchEvent(new KeyboardEvent("keydown", { bubbles: true, cancelable: true, key: "Enter" }));
                        node.dispatchEvent(new KeyboardEvent("keyup", { bubbles: true, cancelable: true, key: "Enter" }));
                        node.dispatchEvent(new KeyboardEvent("keydown", { bubbles: true, cancelable: true, key: " " }));
                        node.dispatchEvent(new KeyboardEvent("keyup", { bubbles: true, cancelable: true, key: " " }));
                    } catch (_error) {
                        // ignore keyboard fallback error
                    }
                    return `click:submode:${textOf(node).slice(0, 80)}`;
                }
                return "";
            };
            const parsePromptState = () => {
                const raw = String(window.localStorage.getItem("FLOW_MAIN_PROMPT_BOX_STATE") || "");
                if (!raw) {
                    return {
                        raw,
                        promptMode: "",
                        selectedVideoModelFamily: "",
                        selectedVideoDuration: "",
                        outputsPerPrompt: "",
                    };
                }
                try {
                    const parsed = JSON.parse(raw);
                    return {
                        raw,
                        promptMode: String(parsed?.imageOrVideoMode || ""),
                        selectedVideoModelFamily: String(parsed?.selectedVideoModelFamily || ""),
                        selectedVideoDuration: String(parsed?.selectedVideoDuration || ""),
                        outputsPerPrompt: String(parsed?.outputsPerPrompt || ""),
                    };
                } catch (error) {
                    return {
                        raw,
                        promptMode: "",
                        selectedVideoModelFamily: "",
                        selectedVideoDuration: "",
                        outputsPerPrompt: "",
                        parseError: String(error?.message || error),
                    };
                }
            };

            const result = {
                href: String(location.href || ""),
                title: String(document.title || ""),
                readyState: String(document.readyState || ""),
                clickedBannerText: "",
                bootstrapAction: "",
                before: parsePromptState(),
                after: null,
                submodes_before: [],
                submodes_after: [],
                buttons: [],
                textareas: [],
            };

            result.clickedBannerText =
                clickFirstButton(
                    [
                        (text) => text.includes("OK, got it"),
                        (text) => text.includes("Agree"),
                        (text) => text.includes("知道了"),
                        (text) => text.includes("同意"),
                    ],
                    "banner"
                ) || "";

            const before = result.before;
            result.submodes_before = collectSubmodeButtons();
            if (!before.promptMode || before.promptMode === "IMAGE") {
                const modelToggleClick = clickFirstButton(
                    [
                        (text) => text.includes("视频 ·"),
                        (text) => text.includes("图片 ·"),
                        (text) => text.includes("Veo 3.1"),
                        (text) =>
                            !!before.selectedVideoModelFamily &&
                            text.toLowerCase().includes(before.selectedVideoModelFamily.toLowerCase()),
                    ],
                    "click:model-toggle"
                );
                const videoTabClick =
                    modelToggleClick ||
                    clickFirstButton(
                        [
                            (text) => text.includes("观看视频"),
                            (text) => text === "视频",
                            (text) => text.startsWith("videocam"),
                        ],
                        "click:video-tab"
                    );
                const textClick = clickVideoSubmode("文本") || clickVideoSubmode("提示词");
                result.bootstrapAction = videoTabClick || textClick || "";
            } else if (before.promptMode === "VIDEO_REFERENCES") {
                result.bootstrapAction = clickVideoSubmode("文本") || clickVideoSubmode("提示词") || "";
            }

            result.after = parsePromptState();
            result.submodes_after = collectSubmodeButtons();
            result.buttons = Array.from(document.querySelectorAll("button"))
                .slice(0, 16)
                .map((button) => textOf(button))
                .filter(Boolean);
            result.textareas = Array.from(document.querySelectorAll("textarea"))
                .slice(0, 4)
                .map((item) => ({
                    placeholder: item.getAttribute("placeholder") || "",
                    value_length: (item.value || "").length,
                }));

            return result;
        },
    });
    return results?.[0]?.result || {};
}

async function runVideoSubmodeProbe(tabId, jobPayload) {
    const targetSubmode = String(jobPayload?.target_submode || "文本").trim() || "文本";
    const results = await chrome.scripting.executeScript({
        target: { tabId },
        world: "MAIN",
        args: [targetSubmode],
        func: async (preferredSubmode) => {
            try {
                const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
                const finalize = (value) => {
                    try {
                        return JSON.parse(JSON.stringify(value));
                    } catch (error) {
                        return {
                            probe_error: `serialize_failed:${String(error?.message || error)}`,
                            fallback_preview: String(value && typeof value === "object" ? Object.keys(value).join(",") : value || ""),
                            href: String(location.href || ""),
                            title: String(document.title || ""),
                            readyState: String(document.readyState || ""),
                        };
                    }
                };
                const visible = (el) => {
                    if (!el) return false;
                    const rect = el.getBoundingClientRect();
                    const style = window.getComputedStyle(el);
                    return rect.width > 8 && rect.height > 8 && style.visibility !== "hidden" && style.display !== "none";
                };
                const textOf = (el) =>
                    String(
                        el?.innerText ||
                        el?.textContent ||
                        el?.value ||
                        el?.getAttribute?.("aria-label") ||
                        el?.getAttribute?.("placeholder") ||
                        ""
                    )
                        .replace(/\s+/g, " ")
                        .trim();
                const parsePromptState = () => {
                    const raw = String(window.localStorage.getItem("FLOW_MAIN_PROMPT_BOX_STATE") || "");
                    if (!raw) {
                        return { raw, promptMode: "", selectedVideoModelFamily: "" };
                    }
                    try {
                        const parsed = JSON.parse(raw);
                        return {
                            raw,
                            promptMode: String(parsed?.imageOrVideoMode || ""),
                            selectedVideoModelFamily: String(parsed?.selectedVideoModelFamily || ""),
                            selectedVideoDuration: String(parsed?.selectedVideoDuration || ""),
                            outputsPerPrompt: String(parsed?.outputsPerPrompt || ""),
                        };
                    } catch (error) {
                        return {
                            raw,
                            promptMode: "",
                            selectedVideoModelFamily: "",
                            parseError: String(error?.message || error),
                        };
                    }
                };
                const isClickableLike = (node) => {
                    if (!node || !visible(node)) return false;
                    if (node.tagName === "BUTTON") return true;
                    if (node.getAttribute?.("role") === "button") return true;
                    if (Number(node.tabIndex) >= 0) return true;
                    const className = String(node.className || "");
                    const style = window.getComputedStyle(node);
                    return (
                        style.cursor === "pointer" ||
                        /\b(cursor-pointer|clickable|chip|segmented|toggle)\b/i.test(className)
                    );
                };
                const collectVisibleTabs = () =>
                    Array.from(document.querySelectorAll("[role='tab']"))
                        .filter((node) => visible(node))
                        .map((node) => {
                            const text = textOf(node);
                            const className = String(node.className || "");
                            const tablist = node.closest("[role='tablist']");
                            return {
                                text: text.slice(0, 120),
                                tag: String(node.tagName || "").toLowerCase(),
                                disabled: !!node.disabled || node.getAttribute("aria-disabled") === "true",
                                aria_selected: String(node.getAttribute("aria-selected") || ""),
                                data_state: String(node.getAttribute("data-state") || ""),
                                aria_controls: String(node.getAttribute("aria-controls") || ""),
                                class_name: className.slice(0, 160),
                                tablist_text: textOf(tablist).slice(0, 200),
                                selected:
                                    node.getAttribute("aria-selected") === "true" ||
                                    node.getAttribute("data-state") === "active" ||
                                    /\b(selected|active)\b/i.test(className),
                            };
                        });
                const collectSubmodeCandidates = () =>
                    Array.from(
                        document.querySelectorAll(
                            [
                                "[role='tab']",
                                "[role='button']",
                                "button",
                                "[aria-selected]",
                                "[data-state]",
                                "[data-orientation]",
                                "[aria-controls]",
                                "[tabindex]",
                            ].join(", ")
                        )
                    )
                        .filter((node) => visible(node))
                        .map((node) => {
                            const text = textOf(node);
                            if (!(text.includes("帧") || text.includes("素材"))) {
                                return null;
                            }
                            const className = String(node.className || "");
                            const parent = node.parentElement;
                            return {
                                text: text.slice(0, 120),
                                tag: String(node.tagName || "").toLowerCase(),
                                role: String(node.getAttribute("role") || ""),
                                aria_selected: String(node.getAttribute("aria-selected") || ""),
                                aria_controls: String(node.getAttribute("aria-controls") || ""),
                                aria_disabled: String(node.getAttribute("aria-disabled") || ""),
                                data_state: String(node.getAttribute("data-state") || ""),
                                data_orientation: String(node.getAttribute("data-orientation") || ""),
                                tab_index: Number(node.tabIndex),
                                class_name: className.slice(0, 160),
                                parent_text: textOf(parent).slice(0, 160),
                                parent_role: String(parent?.getAttribute?.("role") || ""),
                            };
                        })
                        .filter(Boolean)
                        .slice(0, 24);
                const collectSubmodeButtons = () =>
                    collectSubmodeCandidates()
                        .slice(0, 8);
                const collectVisibleClickables = () => {
                    const selectors = [
                        "button",
                        "[role='button']",
                        "[role='tab']",
                        "[aria-haspopup]",
                        "[aria-selected]",
                        "[data-state]",
                        "[tabindex]",
                    ].join(", ");
                    return Array.from(document.querySelectorAll(selectors))
                        .filter((node) => visible(node))
                        .map((node) => {
                            const text = textOf(node);
                            const className = String(node.className || "");
                            if (!text) return null;
                            return {
                                text: text.slice(0, 160),
                                tag: String(node.tagName || "").toLowerCase(),
                                role: String(node.getAttribute?.("role") || ""),
                                aria_label: String(node.getAttribute?.("aria-label") || "").slice(0, 120),
                                aria_selected: String(node.getAttribute?.("aria-selected") || ""),
                                aria_expanded: String(node.getAttribute?.("aria-expanded") || ""),
                                aria_haspopup: String(node.getAttribute?.("aria-haspopup") || ""),
                                data_state: String(node.getAttribute?.("data-state") || ""),
                                class_name: className.slice(0, 160),
                            };
                        })
                        .filter(Boolean)
                        .slice(0, 80);
                };
                const collectOverlayTextHints = () =>
                    Array.from(document.querySelectorAll("[role='dialog'], [aria-modal='true'], [role='listbox'], [role='menu'], [role='alert']"))
                        .filter((node) => visible(node))
                        .map((node) => textOf(node))
                        .filter((text) => text && (text.includes("帧") || text.includes("素材")))
                        .slice(0, 20);
                const collectPopupRoots = () =>
                    Array.from(
                        document.querySelectorAll(
                            [
                                "[role='dialog']",
                                "[role='listbox']",
                                "[role='menu']",
                                "[role='presentation']",
                                "[data-radix-popper-content-wrapper]",
                                "[data-state]",
                                "[aria-modal='true']",
                            ].join(", ")
                        )
                    )
                        .filter((node) => visible(node))
                        .map((node) => ({
                            tag: String(node.tagName || "").toLowerCase(),
                            role: String(node.getAttribute("role") || ""),
                            text: textOf(node).slice(0, 200),
                            aria_modal: String(node.getAttribute("aria-modal") || ""),
                            data_state: String(node.getAttribute("data-state") || ""),
                            class_name: String(node.className || "").slice(0, 160),
                        }))
                        .filter((item) => item.text || item.role || item.data_state)
                        .slice(0, 16);
                const describeNode = (node) => {
                    if (!node) return null;
                    const rect = typeof node.getBoundingClientRect === "function" ? node.getBoundingClientRect() : null;
                    return {
                        tag: String(node.tagName || "").toLowerCase(),
                        text: textOf(node).slice(0, 120),
                        role: String(node.getAttribute?.("role") || ""),
                        aria_expanded: String(node.getAttribute?.("aria-expanded") || ""),
                        aria_controls: String(node.getAttribute?.("aria-controls") || ""),
                        data_state: String(node.getAttribute?.("data-state") || ""),
                        class_name: String(node.className || "").slice(0, 160),
                        rect: rect
                            ? {
                                  x: Math.round(rect.x),
                                  y: Math.round(rect.y),
                                  width: Math.round(rect.width),
                                  height: Math.round(rect.height),
                              }
                            : null,
                    };
                };
                const dispatchRealClick = (node) => {
                    if (!node || typeof node.getBoundingClientRect !== "function") {
                        return { action: "", target: null, hit: null };
                    }
                    const rect = node.getBoundingClientRect();
                    const x = Math.round(rect.left + rect.width / 2);
                    const y = Math.round(rect.top + rect.height / 2);
                    const hit = document.elementFromPoint(x, y);
                    const target = hit && node.contains(hit) ? hit : node;
                    const common = {
                        bubbles: true,
                        cancelable: true,
                        composed: true,
                        clientX: x,
                        clientY: y,
                        button: 0,
                    };
                    for (const eventName of ["pointerdown", "mousedown", "pointerup", "mouseup", "click"]) {
                        try {
                            const EventCtor = eventName.startsWith("pointer") ? PointerEvent : MouseEvent;
                            target.dispatchEvent(new EventCtor(eventName, common));
                        } catch (error) {
                            target.dispatchEvent(new MouseEvent("click", common));
                            break;
                        }
                    }
                    if (typeof target.click === "function") {
                        target.click();
                    }
                    return {
                        action: `real-click:${x},${y}`,
                        target: describeNode(target),
                        hit: describeNode(hit),
                    };
                };
                const collectCreateButtons = () =>
                    Array.from(document.querySelectorAll("button, [role='button'], input[type='button'], input[type='submit']"))
                        .filter((node) => visible(node))
                        .map((node) => {
                            const text = textOf(node);
                            const iconText = textOf(node.querySelector?.(".google-symbols, i"));
                            const raw = `${text} ${String(node.getAttribute?.("aria-label") || "")}`.toLowerCase();
                            if (
                                !(
                                    raw.includes("创建") ||
                                    raw.includes("generate") ||
                                    raw.includes("arrow_forward") ||
                                    iconText.includes("arrow_forward")
                                )
                            ) {
                                return null;
                            }
                            return {
                                text: text.slice(0, 160),
                                disabled: !!node.disabled || node.getAttribute("aria-disabled") === "true",
                                aria_disabled: String(node.getAttribute("aria-disabled") || ""),
                                aria_label: String(node.getAttribute("aria-label") || ""),
                                icon: iconText.slice(0, 80),
                            };
                        })
                        .filter(Boolean);
                const clickVideoTab = () => {
                    const selectors = ["[role='tab']", "button", "[role='button']"].join(", ");
                    const promptTarget = findPromptTarget();
                    const promptRect = promptTarget?.getBoundingClientRect?.() || null;
                    const candidates = Array.from(document.querySelectorAll(selectors))
                        .filter((node) => visible(node))
                        .map((node) => {
                            const text = textOf(node);
                            const compact = text.replace(/\s+/g, "").toLowerCase();
                            const rect = node.getBoundingClientRect();
                            const inNav = !!node.closest?.("nav, aside, [role='navigation']");
                            const inDialog = !!node.closest?.("[role='dialog'], [aria-modal='true']");
                            const parentText = textOf(node.parentElement || null);
                            const rawAria = `${String(node.getAttribute?.("aria-label") || "")} ${String(node.getAttribute?.("aria-controls") || "")}`.toLowerCase();
                            const looksLikeVideoToggle =
                                text === "视频" ||
                                text.includes("视频 ·") ||
                                text.includes("观看视频") ||
                                text.startsWith("videocam") ||
                                text.includes("play_circle 视频") ||
                                compact.includes("play_circle视频");
                            if (!looksLikeVideoToggle) return null;
                            if (inNav) return null;
                            if (inDialog) return null;
                            if (compact.includes("查看设置")) return null;
                            if (rawAria.includes("settings")) return null;
                            const distanceScore = promptRect
                                ? Math.abs(rect.bottom - promptRect.top) + Math.abs(rect.left - promptRect.left)
                                : 999999;
                            const roleScore = node.getAttribute?.("role") === "tab" ? 0 : 1;
                            const selectedScore =
                                String(node.getAttribute?.("aria-selected") || "").toLowerCase() === "true" ? 2 : 0;
                            const parentScore =
                                parentText.includes("图片") || parentText.includes("视频") || parentText.includes("Veo")
                                    ? 0
                                    : 3;
                            return {
                                node,
                                text,
                                distanceScore,
                                roleScore,
                                selectedScore,
                                parentScore,
                            };
                        })
                        .filter(Boolean)
                        .sort((a, b) => {
                            return (
                                a.roleScore - b.roleScore ||
                                a.selectedScore - b.selectedScore ||
                                a.parentScore - b.parentScore ||
                                a.distanceScore - b.distanceScore
                            );
                        });
                    for (const item of candidates) {
                        dispatchRealClick(item.node);
                        return `click:video-tab:${item.text.slice(0, 80)}`;
                    }
                    return "";
                };
                const clickVideoSettingsChip = () => {
                    for (const node of Array.from(document.querySelectorAll("button, [role='button'], [role='tab']"))) {
                        if (!isClickableLike(node)) continue;
                        const text = textOf(node);
                        if (!text) continue;
                        const compact = text.replace(/\s+/g, "");
                        if (
                            compact.includes("视频·") ||
                            compact.includes("视频4s") ||
                            compact.includes("视频6s") ||
                            compact.includes("视频8s") ||
                            compact.includes("视频10s")
                        ) {
                            const clickInfo = dispatchRealClick(node);
                            return {
                                action: `click:settings-chip:${text.slice(0, 80)}`,
                                before: describeNode(node),
                                click: clickInfo,
                            };
                        }
                    }
                    return null;
                };
                const clickPopupCandidate = (matcher, label) => {
                    const selectors = ["button", "[role='button']", "[aria-haspopup='menu']", "[aria-haspopup='dialog']"].join(", ");
                    for (const node of Array.from(document.querySelectorAll(selectors))) {
                        if (!visible(node)) continue;
                        const text = textOf(node);
                        if (!text || !matcher(text)) continue;
                        const clickInfo = dispatchRealClick(node);
                        return {
                            action: `${label}:${text.slice(0, 80)}`,
                            before: describeNode(node),
                            click: clickInfo,
                        };
                    }
                    return null;
                };
                const clickSubmode = (targetText) => {
                    const selectors = [
                        "[role='tab']",
                        "[role='button']",
                        "button",
                        "[aria-selected]",
                        "[data-state]",
                        "[data-orientation]",
                        "[aria-controls]",
                        "[tabindex]",
                    ].join(", ");
                    for (const node of Array.from(document.querySelectorAll(selectors))) {
                        if (!visible(node)) continue;
                        const text = textOf(node);
                        if (!text.includes(targetText)) continue;
                        node.dispatchEvent(new MouseEvent("click", { bubbles: true, cancelable: true }));
                        if (typeof node.click === "function") {
                            node.click();
                        }
                        return `click:submode:${text.slice(0, 80)}`;
                    }
                    return "";
                };
                const snapshot = (label) => ({
                    label,
                    prompt_state: parsePromptState(),
                    submodes: collectSubmodeButtons(),
                    visible_tabs: collectVisibleTabs().slice(0, 24),
                    visible_clickables: collectVisibleClickables(),
                    overlay_text_hints: collectOverlayTextHints(),
                    popup_roots: collectPopupRoots(),
                    create_buttons: collectCreateButtons(),
                });

                const result = {
                    probe_version: "video_submode_probe_v3",
                    href: String(location.href || ""),
                    title: String(document.title || ""),
                    readyState: String(document.readyState || ""),
                    target_submode: preferredSubmode,
                    settings_chip_action: "",
                    settings_chip_debug: null,
                    settings_menu_action: "",
                    settings_menu_debug: null,
                    model_menu_action: "",
                    model_menu_debug: null,
                    steps: [],
                };
                result.steps.push(snapshot("initial"));
                result.video_tab_action = clickVideoTab() || "";
                if (result.video_tab_action) {
                    await sleep(250);
                    result.steps.push(snapshot("after_video_tab"));
                }
                if (!(result.steps[result.steps.length - 1]?.submodes || []).length) {
                    const settingsClick = clickVideoSettingsChip();
                    result.settings_chip_action = settingsClick?.action || "";
                    result.settings_chip_debug = settingsClick
                        ? {
                              ...settingsClick,
                              after_click_popups: collectPopupRoots(),
                          }
                        : null;
                    if (result.settings_chip_action) {
                        await sleep(350);
                        result.steps.push(snapshot("after_settings_chip"));
                    }
                }
                if (!(result.steps[result.steps.length - 1]?.submodes || []).length) {
                    const modelMenuClick = clickPopupCandidate(
                        (text) => text.includes("crop_16_9") || text.includes("Nano Banana") || text.includes("Veo"),
                        "click:model-menu"
                    );
                    result.model_menu_action = modelMenuClick?.action || "";
                    result.model_menu_debug = modelMenuClick
                        ? {
                              ...modelMenuClick,
                              after_click_popups: collectPopupRoots(),
                          }
                        : null;
                    if (result.model_menu_action) {
                        await sleep(350);
                        result.steps.push(snapshot("after_model_menu"));
                    }
                }
                result.submode_action = clickSubmode(preferredSubmode) || "";
                if (result.submode_action) {
                    await sleep(350);
                }
                result.steps.push(snapshot("after_submode_click"));
                return finalize(result);
            } catch (error) {
                return finalize({
                    probe_error: String(error?.message || error),
                    href: String(location.href || ""),
                    title: String(document.title || ""),
                    readyState: String(document.readyState || ""),
                });
            }
        },
    });
    const firstResult = results?.[0];
    if (!firstResult) {
        return {
            probe_error: "executeScript returned no frame result",
            tab_id: tabId,
        };
    }
    if (typeof firstResult.result === "undefined") {
        return {
            probe_error: "executeScript result is undefined",
            tab_id: tabId,
            frame_id: firstResult.frameId ?? null,
            document_id: firstResult.documentId ?? "",
        };
    }
    return firstResult.result || {};
}

async function runVideoUiPrepare(tabId, jobPayload) {
    const promptText = String(jobPayload?.prompt || "").trim();
    const results = await chrome.scripting.executeScript({
        target: { tabId },
        world: "MAIN",
        args: [promptText],
        func: async (promptValue) => {
            const visible = (el) => {
                if (!el) return false;
                const rect = el.getBoundingClientRect();
                const style = window.getComputedStyle(el);
                return rect.width > 8 && rect.height > 8 && style.visibility !== "hidden" && style.display !== "none";
            };
            const textOf = (el) =>
                String(
                    el?.innerText ||
                    el?.textContent ||
                    el?.value ||
                    el?.getAttribute?.("aria-label") ||
                    el?.getAttribute?.("placeholder") ||
                    ""
                )
                    .replace(/\s+/g, " ")
                    .trim();
            const parsePromptState = () => {
                const raw = String(window.localStorage.getItem("FLOW_MAIN_PROMPT_BOX_STATE") || "");
                if (!raw) {
                    return {
                        raw,
                        promptMode: "",
                        selectedVideoModelFamily: "",
                        selectedVideoDuration: "",
                        outputsPerPrompt: "",
                    };
                }
                try {
                    const parsed = JSON.parse(raw);
                    return {
                        raw,
                        promptMode: String(parsed?.imageOrVideoMode || ""),
                        selectedVideoModelFamily: String(parsed?.selectedVideoModelFamily || ""),
                        selectedVideoDuration: String(parsed?.selectedVideoDuration || ""),
                        outputsPerPrompt: String(parsed?.outputsPerPrompt || ""),
                    };
                } catch (error) {
                    return {
                        raw,
                        promptMode: "",
                        selectedVideoModelFamily: "",
                        selectedVideoDuration: "",
                        outputsPerPrompt: "",
                        parseError: String(error?.message || error),
                    };
                }
            };
            const summarizeElement = (el) => {
                if (!el) return null;
                return {
                    tag: String(el.tagName || "").toLowerCase(),
                    type: String(el.getAttribute?.("type") || ""),
                    role: String(el.getAttribute?.("role") || ""),
                    aria_label: String(el.getAttribute?.("aria-label") || ""),
                    placeholder: String(el.getAttribute?.("placeholder") || ""),
                    contenteditable: String(el.getAttribute?.("contenteditable") || ""),
                    data_slate_editor: String(el.getAttribute?.("data-slate-editor") || ""),
                    disabled: !!el.disabled,
                    text: textOf(el).slice(0, 160),
                };
            };
            const summarizeSlateState = (target) => {
                if (!target || target.getAttribute?.("data-slate-editor") !== "true") {
                    return null;
                }
                const slateStrings = Array.from(target.querySelectorAll("[data-slate-string='true']"))
                    .map((node) => String(node.textContent || "").slice(0, 160));
                const placeholders = Array.from(target.querySelectorAll("[data-slate-placeholder], [data-slate-zero-width]"))
                    .map((node) => ({
                        tag: String(node.tagName || "").toLowerCase(),
                        text: String(node.textContent || "").slice(0, 160),
                        placeholder: String(node.getAttribute?.("data-slate-placeholder") || ""),
                        zero_width: String(node.getAttribute?.("data-slate-zero-width") || ""),
                        length: Number(node.getAttribute?.("data-slate-length") || 0),
                    }));
                return {
                    inner_html: String(target.innerHTML || "").slice(0, 1200),
                    slate_strings: slateStrings,
                    placeholders,
                };
            };
            const isSlateEditor = (el) =>
                !!el &&
                el.getAttribute?.("data-slate-editor") === "true" &&
                el.getAttribute?.("role") === "textbox" &&
                el.getAttribute?.("contenteditable") === "true";
            const setNativeValue = (target, value) => {
                const prototype =
                    target instanceof HTMLTextAreaElement
                        ? HTMLTextAreaElement.prototype
                        : HTMLInputElement.prototype;
                const descriptor = Object.getOwnPropertyDescriptor(prototype, "value");
                if (descriptor?.set) {
                    descriptor.set.call(target, value);
                } else {
                    target.value = value;
                }
            };
            const fireInputEvents = (target) => {
                for (const type of ["focus", "input", "change", "blur"]) {
                    target.dispatchEvent(new Event(type, { bubbles: true }));
                }
            };
            const placeCaretAtEnd = (target) => {
                if (!target) return;
                const selection = window.getSelection();
                if (!selection) return;
                const range = document.createRange();
                range.selectNodeContents(target);
                range.collapse(false);
                selection.removeAllRanges();
                selection.addRange(range);
            };
            const placeCaretAtStart = (target) => {
                if (!target) return;
                const selection = window.getSelection();
                if (!selection) return;
                const range = document.createRange();
                range.selectNodeContents(target);
                range.collapse(true);
                selection.removeAllRanges();
                selection.addRange(range);
            };
            const clearSlateEditor = (target) => {
                if (!target) return;
                target.focus();
                if (document.execCommand) {
                    document.execCommand("selectAll", false);
                    document.execCommand("delete", false);
                } else {
                    const selection = window.getSelection();
                    const range = document.createRange();
                    range.selectNodeContents(target);
                    selection?.removeAllRanges();
                    selection?.addRange(range);
                    selection?.deleteFromDocument();
                }
                placeCaretAtEnd(target);
            };
            const insertIntoSlateEditor = (target, value) => {
                if (!target) return false;
                clearSlateEditor(target);
                target.dispatchEvent(
                    new InputEvent("beforeinput", {
                        bubbles: true,
                        cancelable: true,
                        data: value,
                        inputType: "insertText",
                    })
                );
                let inserted = false;
                if (document.execCommand) {
                    inserted = document.execCommand("insertText", false, value);
                }
                if (!inserted) {
                    const selection = window.getSelection();
                    const range = selection?.rangeCount ? selection.getRangeAt(0) : document.createRange();
                    const textNode = document.createTextNode(value);
                    range.deleteContents();
                    range.insertNode(textNode);
                    range.setStartAfter(textNode);
                    range.collapse(true);
                    selection?.removeAllRanges();
                    selection?.addRange(range);
                }
                target.dispatchEvent(
                    new InputEvent("input", {
                        bubbles: true,
                        cancelable: true,
                        data: value,
                        inputType: "insertText",
                    })
                );
                target.dispatchEvent(new Event("change", { bubbles: true, cancelable: true }));
                return true;
            };
            const isLikelyPromptField = (el) => {
                if (!visible(el)) return false;
                if (isSlateEditor(el)) return true;
                const meta = [
                    el.getAttribute?.("aria-label") || "",
                    el.getAttribute?.("placeholder") || "",
                    el.getAttribute?.("name") || "",
                    el.id || "",
                    el.className || "",
                ]
                    .join(" ")
                    .toLowerCase();
                const text = textOf(el).toLowerCase();
                if (meta.includes("search") || text.includes("搜索")) return false;
                if (meta.includes("prompt") || meta.includes("message") || meta.includes("describe")) return true;
                if (text.includes("创作") || text.includes("describe") || text.includes("prompt")) return true;
                if (el.tagName === "TEXTAREA") return true;
                if (el.getAttribute?.("contenteditable") === "true") return true;
                return false;
            };
            const isConversationLikeTarget = (el) => {
                if (!el || !visible(el)) return false;
                const rect = el.getBoundingClientRect();
                if (rect.left <= window.innerWidth * 0.52) {
                    return false;
                }
                const inspectNodes = [];
                let current = el;
                while (current && inspectNodes.length < 8) {
                    inspectNodes.push(current);
                    current = current.parentElement;
                }
                return inspectNodes.some((node) => {
                    if (!node || typeof node.getBoundingClientRect !== "function") return false;
                    const nodeRect = node.getBoundingClientRect();
                    const text = textOf(node).toLowerCase();
                    return (
                        nodeRect.left > window.innerWidth * 0.5 &&
                        nodeRect.width < window.innerWidth * 0.42 &&
                        (
                            text.includes("i've generated") ||
                            text.includes("let me know if") ||
                            text.includes("what would you like to create") ||
                            text.includes("未命名的会话") ||
                            text.includes("conversation")
                        )
                    );
                });
            };
            const collectPromptCandidates = () => {
                const slateTargets = Array.from(
                    document.querySelectorAll("[data-slate-editor='true'][role='textbox'][contenteditable='true']")
                ).filter((el) => visible(el) && isLikelyPromptField(el));
                const promptTargets = Array.from(
                    document.querySelectorAll("textarea, [contenteditable='true'], input[type='text']")
                ).filter((el) => isLikelyPromptField(el));
                return [...slateTargets, ...promptTargets]
                    .filter((el, index, items) => items.indexOf(el) === index)
                    .sort((a, b) => {
                        const aRect = a.getBoundingClientRect();
                        const bRect = b.getBoundingClientRect();
                        const aBottomComposerScore =
                            (aRect.bottom > window.innerHeight * 0.7 ? 2 : 0) +
                            (Math.abs(aRect.left + aRect.width / 2 - window.innerWidth / 2) < window.innerWidth * 0.2 ? 1 : 0);
                        const bBottomComposerScore =
                            (bRect.bottom > window.innerHeight * 0.7 ? 2 : 0) +
                            (Math.abs(bRect.left + bRect.width / 2 - window.innerWidth / 2) < window.innerWidth * 0.2 ? 1 : 0);
                        if (aBottomComposerScore !== bBottomComposerScore) {
                            return bBottomComposerScore - aBottomComposerScore;
                        }
                        return bRect.width - aRect.width;
                    });
            };
            const findPromptTarget = () =>
                collectPromptCandidates().find((el) => !isConversationLikeTarget(el)) || null;
            const findConversationDrawer = () =>
                Array.from(document.querySelectorAll("aside, section, div"))
                    .filter((node) => visible(node))
                    .map((node) => ({
                        node,
                        rect: node.getBoundingClientRect(),
                        text: textOf(node),
                    }))
                    .filter(({ rect, text, node }) =>
                        rect.left > window.innerWidth * 0.5 &&
                        rect.width > 220 &&
                        rect.width < window.innerWidth * 0.42 &&
                        rect.height > window.innerHeight * 0.45 &&
                        (
                            text.toLowerCase().includes("i've generated") ||
                            text.toLowerCase().includes("let me know if") ||
                            text.toLowerCase().includes("未命名的会话") ||
                            text.toLowerCase().includes("conversation") ||
                            !!node.querySelector("textarea, [contenteditable='true'], input[type='text']")
                        )
                    )
                    .sort((a, b) => (b.rect.width * b.rect.height) - (a.rect.width * a.rect.height))[0]?.node || null;
            const clickNode = (node) => {
                if (!node) return false;
                try {
                    node.dispatchEvent(new MouseEvent("click", { bubbles: true, cancelable: true }));
                } catch (_error) {
                    // ignore dispatch failure
                }
                if (typeof node.click === "function") {
                    node.click();
                }
                return true;
            };
            const closeConversationDrawer = async () => {
                const drawer = findConversationDrawer();
                if (!drawer) return "";
                const drawerRect = drawer.getBoundingClientRect();
                const closeButton = Array.from(drawer.querySelectorAll("button, [role='button']"))
                    .filter((node) => visible(node))
                    .find((node) => {
                        const rect = node.getBoundingClientRect();
                        const raw = `${textOf(node)} ${String(node.getAttribute?.("aria-label") || "")}`.toLowerCase();
                        return (
                            rect.top < drawerRect.top + 80 &&
                            rect.left > drawerRect.right - 96 &&
                            (raw.includes("close") || raw.includes("关闭") || raw === "" || raw === "x")
                        );
                    });
                if (!closeButton) return "drawer_detected_without_close";
                clickNode(closeButton);
                await new Promise((resolve) => setTimeout(resolve, 350));
                return "drawer_closed";
            };
            const findAgentChipNode = () =>
                Array.from(document.querySelectorAll("button, [role='button'], [aria-pressed], [aria-selected]"))
                    .filter((node) => visible(node))
                    .find((node) => {
                        const rect = node.getBoundingClientRect();
                        const raw = `${textOf(node)} ${String(node.getAttribute?.("aria-label") || "")}`.toLowerCase();
                        return rect.bottom > window.innerHeight * 0.72 && (raw.includes("智能体") || raw.includes("agent"));
                    }) || null;
            const isAgentChipActive = (node) => {
                if (!node) return false;
                const className = String(node.className || "").toLowerCase();
                return (
                    String(node.getAttribute?.("aria-pressed") || "").toLowerCase() === "true" ||
                    String(node.getAttribute?.("aria-selected") || "").toLowerCase() === "true" ||
                    /\b(active|selected|checked|on)\b/.test(className)
                );
            };
            const exitAgentModeIfNeeded = async () => {
                const agentChip = findAgentChipNode();
                const promptState = parsePromptState();
                const shouldExit = !!agentChip && (isAgentChipActive(agentChip) || !promptState.promptMode);
                if (!shouldExit) return "";
                clickNode(agentChip);
                await new Promise((resolve) => setTimeout(resolve, 350));
                return "agent_chip_toggled";
            };
            const collectSurfaceState = () => {
                const drawer = findConversationDrawer();
                const agentChip = findAgentChipNode();
                const promptState = parsePromptState();
                const promptTarget = findPromptTarget();
                const rawKind = drawer
                    ? "conversation_drawer"
                    : (agentChip && (isAgentChipActive(agentChip) || !promptState.promptMode))
                        ? "agent_dialog"
                        : promptState.promptMode
                            ? "generation"
                            : "unknown";
                return {
                    kind: rawKind,
                    prompt_state: promptState,
                    conversation_drawer: !!drawer,
                    agent_chip: summarizeElement(agentChip),
                    prompt_target: summarizeElement(promptTarget),
                };
            };
            const getButtonIconText = (node) => textOf(node?.querySelector?.(".google-symbols, i"));
            const isTrueSubmitButton = (node) => {
                if (!node || !visible(node)) return false;
                const text = textOf(node);
                const icon = getButtonIconText(node).toLowerCase();
                const ariaLabel = String(node.getAttribute?.("aria-label") || "").toLowerCase();
                const raw = `${text} ${ariaLabel} ${icon}`.toLowerCase();
                if (icon.includes("arrow_forward") || raw.includes("arrow_forward")) return true;
                if (node.getAttribute?.("aria-haspopup")) return false;
                return raw.includes("generate") || raw.includes("submit") || raw.includes("开始");
            };
            const collectSubmitCandidates = () => {
                const nodes = Array.from(
                    document.querySelectorAll("button, [role='button'], input[type='button'], input[type='submit']")
                );
                return nodes
                    .filter((node) => visible(node))
                    .map((node) => {
                        const text = textOf(node);
                        return {
                            element: node,
                            summary: {
                                text: text.slice(0, 160),
                                disabled: !!node.disabled || node.getAttribute("aria-disabled") === "true",
                                aria_label: String(node.getAttribute?.("aria-label") || ""),
                                type: String(node.getAttribute?.("type") || ""),
                                icon: getButtonIconText(node).slice(0, 80),
                                kind: isTrueSubmitButton(node) ? "submit" : "other",
                            },
                        };
                    })
                    .filter(({ summary }) => summary.kind === "submit")
                    .slice(0, 8)
                    .map(({ summary }) => summary);
            };

            const result = {
                href: String(location.href || ""),
                title: String(document.title || ""),
                readyState: String(document.readyState || ""),
                prompt_requested: promptValue,
                normalization: [],
                surface_before: collectSurfaceState(),
                surface_after: null,
                prompt_state_before: parsePromptState(),
                prompt_state_after: null,
                prompt_target_before: null,
                prompt_target_after: null,
                prompt_written: false,
                submit_candidates: [],
                all_textareas: [],
            };

            const drawerAction = await closeConversationDrawer();
            if (drawerAction) {
                result.normalization.push(drawerAction);
            }
            const agentAction = await exitAgentModeIfNeeded();
            if (agentAction) {
                result.normalization.push(agentAction);
            }

            const promptTarget = findPromptTarget();
            result.prompt_target_before = summarizeElement(promptTarget);
            result.surface_after = collectSurfaceState();
            result.all_textareas = Array.from(
                document.querySelectorAll("textarea, [contenteditable='true'], input[type='text']")
            )
                .filter((el) => isLikelyPromptField(el))
                .slice(0, 6)
                .map((el) => summarizeElement(el));

            if (result.surface_after?.kind === "generation" && promptTarget && promptValue) {
                try {
                    promptTarget.focus();
                    if (promptTarget.tagName === "TEXTAREA" || promptTarget.tagName === "INPUT") {
                        setNativeValue(promptTarget, promptValue);
                        fireInputEvents(promptTarget);
                    } else if (isSlateEditor(promptTarget)) {
                        insertIntoSlateEditor(promptTarget, promptValue);
                    } else if (promptTarget.getAttribute("contenteditable") === "true") {
                        promptTarget.textContent = promptValue;
                        fireInputEvents(promptTarget);
                    }
                    result.prompt_written = true;
                } catch (error) {
                    result.prompt_write_error = String(error?.message || error);
                }
            } else if (promptTarget && result.surface_after?.kind !== "generation") {
                result.prompt_write_error = `surface_not_ready:${String(result.surface_after?.kind || "unknown")}`;
            }

            result.prompt_target_after = summarizeElement(promptTarget);
            result.submit_candidates = collectSubmitCandidates();
            result.prompt_state_after = parsePromptState();
            return result;
        },
    });
    return results?.[0]?.result || {};
}

async function runVideoUiSubmitProbe(tabId, jobPayload) {
    const promptText = String(jobPayload?.prompt || "").trim();
    const results = await chrome.scripting.executeScript({
        target: { tabId },
        world: "MAIN",
        args: [promptText],
        func: async (promptValue) => {
            const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
            const frontendErrors = [];
            let stage = "init";
            const finalize = (value) => {
                try {
                    return JSON.parse(JSON.stringify(value));
                } catch (error) {
                    return {
                        result_version: "video_ui_submit_v2",
                        probe_error: `serialize_failed:${String(error?.message || error)}`,
                        frontend_errors: frontendErrors.slice(-12),
                        href: String(location.href || ""),
                        title: String(document.title || ""),
                        readyState: String(document.readyState || ""),
                        stage,
                    };
                }
            };
            const recordFrontendError = (kind, payload) => {
                frontendErrors.push({
                    kind,
                    detail: String(payload || "").slice(0, 500),
                });
            };
            const originalConsoleError = console.error;
            const consoleErrorProxy = (...args) => {
                try {
                    recordFrontendError(
                        "console.error",
                        args
                            .map((item) => {
                                if (typeof item === "string") return item;
                                try {
                                    return JSON.stringify(item);
                                } catch (error) {
                                    return String(item);
                                }
                            })
                            .join(" ")
                    );
                } catch (error) {
                    // ignore logging failures
                }
                return originalConsoleError.apply(console, args);
            };
            console.error = consoleErrorProxy;
            const onWindowError = (event) => {
                recordFrontendError("window.error", event?.message || event?.error || "");
            };
            const onUnhandledRejection = (event) => {
                recordFrontendError("unhandledrejection", event?.reason || "");
            };
            window.addEventListener("error", onWindowError);
            window.addEventListener("unhandledrejection", onUnhandledRejection);
            const visible = (el) => {
                if (!el) return false;
                const rect = el.getBoundingClientRect();
                const style = window.getComputedStyle(el);
                return rect.width > 8 && rect.height > 8 && style.visibility !== "hidden" && style.display !== "none";
            };
            const textOf = (el) =>
                String(
                    el?.innerText ||
                    el?.textContent ||
                    el?.value ||
                    el?.getAttribute?.("aria-label") ||
                    el?.getAttribute?.("placeholder") ||
                    ""
                )
                    .replace(/\s+/g, " ")
                    .trim();
            const summarizeElement = (el) => {
                if (!el) return null;
                return {
                    tag: String(el.tagName || "").toLowerCase(),
                    type: String(el.getAttribute?.("type") || ""),
                    role: String(el.getAttribute?.("role") || ""),
                    aria_label: String(el.getAttribute?.("aria-label") || ""),
                    placeholder: String(el.getAttribute?.("placeholder") || ""),
                    contenteditable: String(el.getAttribute?.("contenteditable") || ""),
                    data_slate_editor: String(el.getAttribute?.("data-slate-editor") || ""),
                    disabled: !!el.disabled,
                    text: textOf(el).slice(0, 160),
                };
            };
            const summarizeSlateState = (target) => {
                if (!target || target.getAttribute?.("data-slate-editor") !== "true") {
                    return null;
                }
                const slateStrings = Array.from(target.querySelectorAll("[data-slate-string='true']"))
                    .map((node) => String(node.textContent || "").slice(0, 160));
                const placeholders = Array.from(target.querySelectorAll("[data-slate-placeholder], [data-slate-zero-width]"))
                    .map((node) => ({
                        tag: String(node.tagName || "").toLowerCase(),
                        text: String(node.textContent || "").slice(0, 160),
                        placeholder: String(node.getAttribute?.("data-slate-placeholder") || ""),
                        zero_width: String(node.getAttribute?.("data-slate-zero-width") || ""),
                        length: Number(node.getAttribute?.("data-slate-length") || 0),
                    }));
                const child_nodes = Array.from(target.childNodes || []).slice(0, 8).map((node) => ({
                    node_type: Number(node.nodeType || 0),
                    tag: node.nodeType === Node.ELEMENT_NODE ? String(node.tagName || "").toLowerCase() : "",
                    text: String(node.textContent || "").slice(0, 160),
                    html: node.nodeType === Node.ELEMENT_NODE ? String(node.outerHTML || "").slice(0, 220) : "",
                }));
                const text_nodes = [];
                try {
                    const walker = document.createTreeWalker(target, NodeFilter.SHOW_TEXT);
                    let currentNode = walker.nextNode();
                    while (currentNode && text_nodes.length < 10) {
                        text_nodes.push({
                            text: String(currentNode.textContent || "").slice(0, 160),
                            parent_tag: String(currentNode.parentElement?.tagName || "").toLowerCase(),
                            parent_attrs: {
                                data_slate_string: String(currentNode.parentElement?.getAttribute?.("data-slate-string") || ""),
                                data_slate_zero_width: String(currentNode.parentElement?.getAttribute?.("data-slate-zero-width") || ""),
                                data_slate_placeholder: String(currentNode.parentElement?.getAttribute?.("data-slate-placeholder") || ""),
                            },
                        });
                        currentNode = walker.nextNode();
                    }
                } catch (_error) {}
                return {
                    inner_html: String(target.innerHTML || "").slice(0, 1200),
                    slate_strings: slateStrings,
                    placeholders,
                    inner_text: String(target.innerText || target.textContent || "").slice(0, 240),
                    child_nodes,
                    text_nodes,
                };
            };
            const summarizeFlowPromptBoxState = () => {
                const flowState = window.FLOW_MAIN_PROMPT_BOX_STATE || null;
                if (!flowState || typeof flowState !== "object") return null;
                const summary = {};
                for (const [key, value] of Object.entries(flowState).slice(0, 24)) {
                    if (typeof value === "string") {
                        summary[key] = value.slice(0, 240);
                    } else if (typeof value === "number" || typeof value === "boolean" || value == null) {
                        summary[key] = value;
                    } else if (Array.isArray(value)) {
                        summary[key] = value.slice(0, 8).map((item) =>
                            typeof item === "string" ? item.slice(0, 120) : (item && typeof item === "object" ? Object.keys(item).slice(0, 8) : item)
                        );
                    } else if (typeof value === "object") {
                        summary[key] = Object.keys(value).slice(0, 12);
                    } else {
                        summary[key] = String(value).slice(0, 120);
                    }
                }
                return summary;
            };
            const collectPromptRuntimeEvidence = (target) => ({
                prompt_target: summarizeElement(target),
                prompt_state: parsePromptState(),
                flow_prompt_box_state: summarizeFlowPromptBoxState(),
                prompt_slate: summarizeSlateState(target),
                prompt_dom: target
                    ? {
                        text: String(target.innerText || target.textContent || "").slice(0, 240),
                        html: String(target.innerHTML || "").slice(0, 1200),
                        child_count: Number(target.childNodes?.length || 0),
                        is_connected: !!target.isConnected,
                    }
                    : null,
                selection: summarizeSelection(),
                active_element: summarizeElement(document.activeElement),
            });
            const parsePromptState = () => {
                const raw = String(window.localStorage.getItem("FLOW_MAIN_PROMPT_BOX_STATE") || "");
                if (!raw) {
                    return { raw, promptMode: "", selectedVideoModelFamily: "" };
                }
                try {
                    const parsed = JSON.parse(raw);
                    return {
                        raw,
                        promptMode: String(parsed?.imageOrVideoMode || ""),
                        selectedVideoModelFamily: String(parsed?.selectedVideoModelFamily || ""),
                        selectedVideoDuration: String(parsed?.selectedVideoDuration || ""),
                        outputsPerPrompt: String(parsed?.outputsPerPrompt || ""),
                    };
                } catch (error) {
                    return {
                        raw,
                        promptMode: "",
                        selectedVideoModelFamily: "",
                        parseError: String(error?.message || error),
                    };
                }
            };
            const isLikelyPromptField = (el) => {
                if (!visible(el)) return false;
                if (
                    el.getAttribute?.("data-slate-editor") === "true" &&
                    el.getAttribute?.("role") === "textbox" &&
                    el.getAttribute?.("contenteditable") === "true"
                ) {
                    return true;
                }
                const meta = [
                    el.getAttribute?.("aria-label") || "",
                    el.getAttribute?.("placeholder") || "",
                    el.getAttribute?.("name") || "",
                    el.id || "",
                    el.className || "",
                ]
                    .join(" ")
                    .toLowerCase();
                const text = textOf(el).toLowerCase();
                if (meta.includes("search") || text.includes("搜索")) return false;
                if (meta.includes("prompt") || meta.includes("message") || meta.includes("describe")) return true;
                if (text.includes("创作") || text.includes("describe") || text.includes("prompt")) return true;
                if (el.tagName === "TEXTAREA") return true;
                if (el.getAttribute?.("contenteditable") === "true") return true;
                return false;
            };
            const isConversationLikeTarget = (el) => {
                if (!el || !visible(el)) return false;
                const rect = el.getBoundingClientRect();
                const inspectNodes = [];
                let current = el;
                while (current && inspectNodes.length < 8) {
                    inspectNodes.push(current);
                    current = current.parentElement;
                }
                const rightNarrow =
                    rect.left > window.innerWidth * 0.55 && rect.width < window.innerWidth * 0.45;
                const conversationMarker = inspectNodes.some((node) => {
                    if (!node || typeof node.getBoundingClientRect !== "function") return false;
                    const nodeRect = node.getBoundingClientRect();
                    const text = textOf(node).toLowerCase();
                    const narrowPanel =
                        nodeRect.left > window.innerWidth * 0.5 &&
                        nodeRect.width > 220 &&
                        nodeRect.width < window.innerWidth * 0.48;
                    return (
                        narrowPanel &&
                        (
                            text.includes("未命名的会话") ||
                            text.includes("conversation") ||
                            text.includes("i've generated") ||
                            text.includes("let me know if") ||
                            text.includes("what would you like to create") ||
                            text.includes("您希望创作什么内容")
                        )
                    );
                });
                return rightNarrow || conversationMarker;
            };
            const collectPromptCandidates = () => {
                const slateTargets = Array.from(
                    document.querySelectorAll("[data-slate-editor='true'][role='textbox'][contenteditable='true']")
                ).filter((el) => visible(el) && isLikelyPromptField(el));
                if (slateTargets.length) {
                    return slateTargets.sort(
                        (a, b) => b.getBoundingClientRect().width - a.getBoundingClientRect().width
                    );
                }
                return Array.from(
                    document.querySelectorAll("textarea, [contenteditable='true'], input[type='text']")
                ).filter((el) => isLikelyPromptField(el));
            };
            const findAnyPromptTarget = () => collectPromptCandidates()[0] || null;
            const findPromptTarget = () => {
                const candidates = collectPromptCandidates();
                return candidates.find((el) => !isConversationLikeTarget(el)) || null;
            };
            const getButtonIconText = (node) => textOf(node?.querySelector?.(".google-symbols, i"));
            const classifyButtonKind = (node) => {
                const text = textOf(node);
                const icon = getButtonIconText(node).toLowerCase();
                const ariaLabel = String(node.getAttribute?.("aria-label") || "").toLowerCase();
                const raw = `${text} ${ariaLabel} ${icon}`.toLowerCase();
                if (icon.includes("add_2")) return "add_dialog";
                if (icon.includes("arrow_forward") || raw.includes("arrow_forward")) return "submit";
                if (node.getAttribute?.("aria-haspopup")) return "popup";
                if (raw.includes("创建") || raw.includes("generate")) return "create_like";
                return "other";
            };
            const clickNode = (node) => {
                if (!node) return false;
                try {
                    node.dispatchEvent(new MouseEvent("click", { bubbles: true, cancelable: true }));
                } catch (_error) {
                    // ignore dispatch failure
                }
                if (typeof node.click === "function") {
                    node.click();
                    return true;
                }
                return false;
            };
            const findConversationDrawer = () =>
                Array.from(document.querySelectorAll("aside, section, div"))
                    .filter((node) => visible(node))
                    .map((node) => ({
                        node,
                        rect: node.getBoundingClientRect(),
                        text: textOf(node),
                    }))
                    .filter(({ rect, text, node }) =>
                        rect.left > window.innerWidth * 0.5 &&
                        rect.width > 220 &&
                        rect.width < window.innerWidth * 0.42 &&
                        rect.height > window.innerHeight * 0.45 &&
                        (
                            text.toLowerCase().includes("i've generated") ||
                            text.toLowerCase().includes("let me know if") ||
                            text.toLowerCase().includes("未命名的会话") ||
                            text.toLowerCase().includes("conversation") ||
                            !!node.querySelector("textarea, [contenteditable='true'], input[type='text']")
                        )
                    )
                    .sort((a, b) => (b.rect.width * b.rect.height) - (a.rect.width * a.rect.height))[0]?.node || null;
            const closeConversationDrawer = async () => {
                const drawer = findConversationDrawer();
                if (!drawer) return "";
                const drawerRect = drawer.getBoundingClientRect();
                const closeButton = Array.from(drawer.querySelectorAll("button, [role='button']"))
                    .filter((node) => visible(node))
                    .find((node) => {
                        const rect = node.getBoundingClientRect();
                        const raw = `${textOf(node)} ${String(node.getAttribute?.("aria-label") || "")}`.toLowerCase();
                        return (
                            rect.top < drawerRect.top + 80 &&
                            rect.left > drawerRect.right - 96 &&
                            (raw.includes("close") || raw.includes("关闭") || raw === "" || raw === "x")
                        );
                    });
                if (!closeButton) return "drawer_detected_without_close";
                clickNode(closeButton);
                await sleep(350);
                return "drawer_closed";
            };
            const closeConversationDrawerSafe = async () => {
                if (typeof closeConversationDrawer === "function") {
                    return closeConversationDrawer();
                }
                const drawer = Array.from(document.querySelectorAll("aside, section, div"))
                    .filter((node) => visible(node))
                    .map((node) => ({
                        node,
                        rect: node.getBoundingClientRect(),
                        text: textOf(node),
                    }))
                    .filter(({ rect, text, node }) =>
                        rect.left > window.innerWidth * 0.5 &&
                        rect.width > 220 &&
                        rect.width < window.innerWidth * 0.42 &&
                        rect.height > window.innerHeight * 0.45 &&
                        (
                            text.toLowerCase().includes("i've generated") ||
                            text.toLowerCase().includes("let me know if") ||
                            text.toLowerCase().includes("未命名的会话") ||
                            text.toLowerCase().includes("conversation") ||
                            !!node.querySelector("textarea, [contenteditable='true'], input[type='text']")
                        )
                    )
                    .sort((a, b) => (b.rect.width * b.rect.height) - (a.rect.width * a.rect.height))[0]?.node || null;
                if (!drawer) return "drawer_not_found";
                const drawerRect = drawer.getBoundingClientRect();
                const closeButton = Array.from(drawer.querySelectorAll("button, [role='button']"))
                    .filter((node) => visible(node))
                    .find((node) => {
                        const rect = node.getBoundingClientRect();
                        const raw = `${textOf(node)} ${String(node.getAttribute?.("aria-label") || "")}`.toLowerCase();
                        return (
                            rect.top < drawerRect.top + 80 &&
                            rect.left > drawerRect.right - 96 &&
                            (raw.includes("close") || raw.includes("关闭") || raw === "" || raw === "x")
                        );
                    });
                if (!closeButton) return "drawer_detected_without_close";
                clickReal(closeButton);
                await sleep(350);
                return "drawer_closed_fallback";
            };
            const findAgentChipNode = () =>
                Array.from(document.querySelectorAll("button, [role='button'], [aria-pressed], [aria-selected]"))
                    .filter((node) => visible(node))
                    .find((node) => {
                        const rect = node.getBoundingClientRect();
                        const raw = `${textOf(node)} ${String(node.getAttribute?.("aria-label") || "")}`.toLowerCase();
                        return rect.bottom > window.innerHeight * 0.72 && (raw.includes("智能体") || raw.includes("agent"));
                    }) || null;
            const isAgentChipActive = (node) => {
                if (!node) return false;
                const className = String(node.className || "").toLowerCase();
                return (
                    String(node.getAttribute?.("aria-pressed") || "").toLowerCase() === "true" ||
                    String(node.getAttribute?.("aria-selected") || "").toLowerCase() === "true" ||
                    /\b(active|selected|checked|on)\b/.test(className)
                );
            };
            const ensureCreationSurface = async () => {
                const beforeTarget = findAnyPromptTarget();
                const beforeKind = beforeTarget
                    ? (isConversationLikeTarget(beforeTarget) ? "conversation" : "creation")
                    : "missing";
                let action = "";
                if (beforeKind === "conversation") {
                    action = await closeConversationDrawer();
                }
                const promptState = parsePromptState();
                const agentChip = findAgentChipNode();
                if (!action && agentChip && (isAgentChipActive(agentChip) || !promptState.promptMode)) {
                    clickNode(agentChip);
                    action = "agent_chip_toggled";
                    await sleep(350);
                }
                const afterTarget = findAnyPromptTarget();
                return {
                    before_kind: beforeKind,
                    after_kind: afterTarget
                        ? (isConversationLikeTarget(afterTarget) ? "conversation" : "creation")
                        : "missing",
                    action,
                    prompt_before: summarizeElement(beforeTarget),
                    prompt_after: summarizeElement(afterTarget),
                };
            };
            const setPromptValue = (target, value) => {
                if (!target) return false;
                if (target.tagName === "TEXTAREA" || target.tagName === "INPUT") {
                    const prototype =
                        target instanceof HTMLTextAreaElement
                            ? HTMLTextAreaElement.prototype
                            : HTMLInputElement.prototype;
                    const descriptor = Object.getOwnPropertyDescriptor(prototype, "value");
                    if (descriptor?.set) {
                        descriptor.set.call(target, value);
                    } else {
                        target.value = value;
                    }
                } else if (target.getAttribute?.("data-slate-editor") === "true") {
                    target.focus();
                    if (document.execCommand) {
                        document.execCommand("selectAll", false);
                        document.execCommand("delete", false);
                        document.execCommand("insertText", false, value);
                    } else {
                        const selection = window.getSelection();
                        const range = document.createRange();
                        range.selectNodeContents(target);
                        selection?.removeAllRanges();
                        selection?.addRange(range);
                        selection?.deleteFromDocument();
                        const textNode = document.createTextNode(value);
                        range.insertNode(textNode);
                    }
                } else if (target.getAttribute?.("contenteditable") === "true") {
                    target.textContent = value;
                } else {
                    return false;
                }
                return true;
            };
            const fireEvents = (target, types) => {
                for (const type of types) {
                    const event =
                        type.startsWith("key")
                            ? new KeyboardEvent(type, {
                                bubbles: true,
                                cancelable: true,
                                key: "Enter",
                                code: "Enter",
                            })
                            : new Event(type, { bubbles: true, cancelable: true });
                    target.dispatchEvent(event);
                }
            };
            const surfaceBootstrap = await ensureCreationSurface();
            const promptTarget = findPromptTarget();

            const collectButtonDetails = () => {
                const nodes = Array.from(
                    document.querySelectorAll("button, [role='button'], input[type='button'], input[type='submit']")
                ).filter((node) => visible(node));
                return nodes
                    .map((node) => {
                        const text = textOf(node);
                        const parent = node.closest("[role='button'], button, div, section");
                        return {
                            text: text.slice(0, 160),
                            disabled: !!node.disabled || node.getAttribute("aria-disabled") === "true",
                            aria_disabled: String(node.getAttribute("aria-disabled") || ""),
                            aria_label: String(node.getAttribute("aria-label") || ""),
                            class_name: String(node.className || "").slice(0, 200),
                            type: String(node.getAttribute?.("type") || ""),
                            parent_text: String(textOf(parent) || "").slice(0, 200),
                            testid: String(node.getAttribute("data-testid") || ""),
                            icon: getButtonIconText(node).slice(0, 80),
                            kind: classifyButtonKind(node),
                        };
                    })
                    .filter((item) =>
                        item.kind !== "other" ||
                        item.text.includes("创建") ||
                        item.aria_label.includes("创建")
                    )
                    .slice(0, 12);
            };
            const snapshot = (label) => ({
                label,
                prompt_state: parsePromptState(),
                prompt_target: summarizeElement(promptTarget),
                surface_kind: promptTarget ? (isConversationLikeTarget(promptTarget) ? "conversation" : "creation") : "missing",
                active_element: summarizeElement(document.activeElement),
                create_buttons: collectButtonDetails(),
            });

            const result = {
                href: String(location.href || ""),
                title: String(document.title || ""),
                readyState: String(document.readyState || ""),
                prompt_requested: promptValue,
                stage,
                surface_bootstrap: surfaceBootstrap,
                steps: [],
            };

            try {
                stage = "collect_initial";
                result.stage = stage;
                result.steps.push(snapshot("initial"));

                if (promptTarget && promptValue) {
                    stage = "write_prompt";
                    result.stage = stage;
                    promptTarget.focus();
                    setPromptValue(promptTarget, promptValue);
                    fireEvents(promptTarget, ["focus", "input", "change"]);
                    result.steps.push(snapshot("after_write"));

                    stage = "post_write_wait";
                    result.stage = stage;
                    await sleep(300);
                    result.steps.push(snapshot("after_short_wait"));
                } else {
                    stage = "missing_prompt_target";
                    result.stage = stage;
                    result.steps.push({
                        label: "missing_prompt_target",
                        prompt_state: parsePromptState(),
                        prompt_target: summarizeElement(promptTarget),
                        create_buttons: collectButtonDetails(),
                    });
                }

                stage = "finalize";
                result.stage = stage;
                const submitButton = result.steps
                    .flatMap((step) => step.create_buttons || [])
                    .find((item) => item.kind === "submit");
                result.best_submit_candidate = submitButton || null;
                return finalize(result);
            } catch (error) {
                return finalize({
                    result_version: "video_ui_submit_probe_v2",
                    href: String(location.href || ""),
                    title: String(document.title || ""),
                    readyState: String(document.readyState || ""),
                    prompt_requested: promptValue,
                    stage,
                    probe_error: `runVideoUiSubmitProbe_exception:${String(error?.stack || error?.message || error)}`.slice(0, 1200),
                    frontend_errors: frontendErrors.slice(-12),
                });
            } finally {
                window.removeEventListener("error", onWindowError);
                window.removeEventListener("unhandledrejection", onUnhandledRejection);
                console.error = originalConsoleError;
            }
        },
    });
    return results?.[0]?.result || {};
}

async function runVideoUiTypeProbe(tabId, jobPayload) {
    const promptText = String(jobPayload?.prompt || "").trim();
    const results = await chrome.scripting.executeScript({
        target: { tabId },
        world: "MAIN",
        args: [promptText],
        func: async (promptValue) => {
            const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
            const frontendErrors = [];
            const finalize = (value) => {
                try {
                    return JSON.parse(JSON.stringify(value));
                } catch (error) {
                    return {
                        result_version: "video_ui_submit_v2",
                        probe_error: `serialize_failed:${String(error?.message || error)}`,
                        frontend_errors: frontendErrors.slice(-12),
                        href: String(location.href || ""),
                        title: String(document.title || ""),
                        readyState: String(document.readyState || ""),
                    };
                }
            };
            const recordFrontendError = (kind, payload) => {
                frontendErrors.push({
                    kind,
                    detail: String(payload || "").slice(0, 500),
                });
            };
            const originalConsoleError = console.error;
            const consoleErrorProxy = (...args) => {
                try {
                    recordFrontendError(
                        "console.error",
                        args
                            .map((item) => {
                                if (typeof item === "string") return item;
                                try {
                                    return JSON.stringify(item);
                                } catch (error) {
                                    return String(item);
                                }
                            })
                            .join(" ")
                    );
                } catch (error) {
                    // ignore logging failures
                }
                return originalConsoleError.apply(console, args);
            };
            console.error = consoleErrorProxy;
            const onWindowError = (event) => {
                recordFrontendError("window.error", event?.message || event?.error || "");
            };
            const onUnhandledRejection = (event) => {
                recordFrontendError("unhandledrejection", event?.reason || "");
            };
            window.addEventListener("error", onWindowError);
            window.addEventListener("unhandledrejection", onUnhandledRejection);
            const visible = (el) => {
                if (!el) return false;
                const rect = el.getBoundingClientRect();
                const style = window.getComputedStyle(el);
                return rect.width > 8 && rect.height > 8 && style.visibility !== "hidden" && style.display !== "none";
            };
            const textOf = (el) =>
                String(
                    el?.innerText ||
                    el?.textContent ||
                    el?.value ||
                    el?.getAttribute?.("aria-label") ||
                    el?.getAttribute?.("placeholder") ||
                    ""
                )
                    .replace(/\s+/g, " ")
                    .trim();
            const summarizeElement = (el) => {
                if (!el) return null;
                return {
                    tag: String(el.tagName || "").toLowerCase(),
                    type: String(el.getAttribute?.("type") || ""),
                    role: String(el.getAttribute?.("role") || ""),
                    aria_label: String(el.getAttribute?.("aria-label") || ""),
                    placeholder: String(el.getAttribute?.("placeholder") || ""),
                    contenteditable: String(el.getAttribute?.("contenteditable") || ""),
                    data_slate_editor: String(el.getAttribute?.("data-slate-editor") || ""),
                    disabled: !!el.disabled,
                    text: textOf(el).slice(0, 160),
                };
            };
            const summarizeSlateState = (target) => {
                if (!target || target.getAttribute?.("data-slate-editor") !== "true") {
                    return null;
                }
                const slateStrings = Array.from(target.querySelectorAll("[data-slate-string='true']"))
                    .map((node) => String(node.textContent || "").slice(0, 160));
                const placeholders = Array.from(target.querySelectorAll("[data-slate-placeholder], [data-slate-zero-width]"))
                    .map((node) => ({
                        tag: String(node.tagName || "").toLowerCase(),
                        text: String(node.textContent || "").slice(0, 160),
                        placeholder: String(node.getAttribute?.("data-slate-placeholder") || ""),
                        zero_width: String(node.getAttribute?.("data-slate-zero-width") || ""),
                        length: Number(node.getAttribute?.("data-slate-length") || 0),
                    }));
                const child_nodes = Array.from(target.childNodes || []).slice(0, 8).map((node) => ({
                    node_type: Number(node.nodeType || 0),
                    tag: node.nodeType === Node.ELEMENT_NODE ? String(node.tagName || "").toLowerCase() : "",
                    text: String(node.textContent || "").slice(0, 160),
                    html: node.nodeType === Node.ELEMENT_NODE ? String(node.outerHTML || "").slice(0, 220) : "",
                }));
                const text_nodes = [];
                try {
                    const walker = document.createTreeWalker(target, NodeFilter.SHOW_TEXT);
                    let currentNode = walker.nextNode();
                    while (currentNode && text_nodes.length < 10) {
                        text_nodes.push({
                            text: String(currentNode.textContent || "").slice(0, 160),
                            parent_tag: String(currentNode.parentElement?.tagName || "").toLowerCase(),
                            parent_attrs: {
                                data_slate_string: String(currentNode.parentElement?.getAttribute?.("data-slate-string") || ""),
                                data_slate_zero_width: String(currentNode.parentElement?.getAttribute?.("data-slate-zero-width") || ""),
                                data_slate_placeholder: String(currentNode.parentElement?.getAttribute?.("data-slate-placeholder") || ""),
                            },
                        });
                        currentNode = walker.nextNode();
                    }
                } catch (_error) {}
                return {
                    inner_html: String(target.innerHTML || "").slice(0, 1200),
                    slate_strings: slateStrings,
                    placeholders,
                    inner_text: String(target.innerText || target.textContent || "").slice(0, 240),
                    child_nodes,
                    text_nodes,
                };
            };
            const summarizeFlowPromptBoxState = () => {
                const flowState = window.FLOW_MAIN_PROMPT_BOX_STATE || null;
                if (!flowState || typeof flowState !== "object") return null;
                const summary = {};
                for (const [key, value] of Object.entries(flowState).slice(0, 24)) {
                    if (typeof value === "string") {
                        summary[key] = value.slice(0, 240);
                    } else if (typeof value === "number" || typeof value === "boolean" || value == null) {
                        summary[key] = value;
                    } else if (Array.isArray(value)) {
                        summary[key] = value.slice(0, 8).map((item) =>
                            typeof item === "string" ? item.slice(0, 120) : (item && typeof item === "object" ? Object.keys(item).slice(0, 8) : item)
                        );
                    } else if (typeof value === "object") {
                        summary[key] = Object.keys(value).slice(0, 12);
                    } else {
                        summary[key] = String(value).slice(0, 120);
                    }
                }
                return summary;
            };
            const collectPromptRuntimeEvidence = (target) => ({
                prompt_target: summarizeElement(target),
                prompt_state: parsePromptState(),
                flow_prompt_box_state: summarizeFlowPromptBoxState(),
                prompt_slate: summarizeSlateState(target),
                prompt_dom: target
                    ? {
                        text: String(target.innerText || target.textContent || "").slice(0, 240),
                        html: String(target.innerHTML || "").slice(0, 1200),
                        child_count: Number(target.childNodes?.length || 0),
                        is_connected: !!target.isConnected,
                    }
                    : null,
                selection: summarizeSelection(),
                active_element: summarizeElement(document.activeElement),
            });
            const parsePromptState = () => {
                const raw = String(window.localStorage.getItem("FLOW_MAIN_PROMPT_BOX_STATE") || "");
                if (!raw) {
                    return { raw, promptMode: "", selectedVideoModelFamily: "" };
                }
                try {
                    const parsed = JSON.parse(raw);
                    return {
                        raw,
                        promptMode: String(parsed?.imageOrVideoMode || ""),
                        selectedVideoModelFamily: String(parsed?.selectedVideoModelFamily || ""),
                        selectedVideoDuration: String(parsed?.selectedVideoDuration || ""),
                        outputsPerPrompt: String(parsed?.outputsPerPrompt || ""),
                    };
                } catch (error) {
                    return {
                        raw,
                        promptMode: "",
                        selectedVideoModelFamily: "",
                        parseError: String(error?.message || error),
                    };
                }
            };
            const isLikelyPromptField = (el) => {
                if (!visible(el)) return false;
                if (
                    el.getAttribute?.("data-slate-editor") === "true" &&
                    el.getAttribute?.("role") === "textbox" &&
                    el.getAttribute?.("contenteditable") === "true"
                ) {
                    return true;
                }
                const meta = [
                    el.getAttribute?.("aria-label") || "",
                    el.getAttribute?.("placeholder") || "",
                    el.getAttribute?.("name") || "",
                    el.id || "",
                    el.className || "",
                ]
                    .join(" ")
                    .toLowerCase();
                const text = textOf(el).toLowerCase();
                if (meta.includes("search") || text.includes("搜索")) return false;
                if (meta.includes("prompt") || meta.includes("message") || meta.includes("describe")) return true;
                if (text.includes("创作") || text.includes("describe") || text.includes("prompt")) return true;
                if (el.tagName === "TEXTAREA") return true;
                if (el.getAttribute?.("contenteditable") === "true") return true;
                return false;
            };
            const isConversationLikeTarget = (el) => {
                if (!el || !visible(el)) return false;
                const rect = el.getBoundingClientRect();
                if (rect.left <= window.innerWidth * 0.52) {
                    return false;
                }
                const inspectNodes = [];
                let current = el;
                while (current && inspectNodes.length < 8) {
                    inspectNodes.push(current);
                    current = current.parentElement;
                }
                return inspectNodes.some((node) => {
                    if (!node || typeof node.getBoundingClientRect !== "function") return false;
                    const nodeRect = node.getBoundingClientRect();
                    const text = textOf(node).toLowerCase();
                    return (
                        nodeRect.left > window.innerWidth * 0.5 &&
                        nodeRect.width < window.innerWidth * 0.42 &&
                        (
                            text.includes("i've generated") ||
                            text.includes("let me know if") ||
                            text.includes("what would you like to create") ||
                            text.includes("未命名的会话") ||
                            text.includes("conversation")
                        )
                    );
                });
            };
            const collectPromptCandidates = () =>
                Array.from(
                    document.querySelectorAll("[data-slate-editor='true'][role='textbox'][contenteditable='true'], textarea, [contenteditable='true'], input[type='text']")
                )
                    .filter((el) => visible(el) && isLikelyPromptField(el))
                    .filter((el, index, items) => items.indexOf(el) === index)
                    .sort((a, b) => {
                        const aRect = a.getBoundingClientRect();
                        const bRect = b.getBoundingClientRect();
                        const aBottomComposerScore =
                            (aRect.bottom > window.innerHeight * 0.7 ? 2 : 0) +
                            (Math.abs(aRect.left + aRect.width / 2 - window.innerWidth / 2) < window.innerWidth * 0.2 ? 1 : 0);
                        const bBottomComposerScore =
                            (bRect.bottom > window.innerHeight * 0.7 ? 2 : 0) +
                            (Math.abs(bRect.left + bRect.width / 2 - window.innerWidth / 2) < window.innerWidth * 0.2 ? 1 : 0);
                        if (aBottomComposerScore !== bBottomComposerScore) {
                            return bBottomComposerScore - aBottomComposerScore;
                        }
                        return bRect.width - aRect.width;
                    });
            const findPromptTarget = () =>
                collectPromptCandidates().find((el) => !isConversationLikeTarget(el)) || null;
            const findConversationDrawer = () =>
                Array.from(document.querySelectorAll("aside, section, div"))
                    .filter((node) => visible(node))
                    .map((node) => ({
                        node,
                        rect: node.getBoundingClientRect(),
                        text: textOf(node),
                    }))
                    .filter(({ rect, text, node }) =>
                        rect.left > window.innerWidth * 0.5 &&
                        rect.width > 220 &&
                        rect.width < window.innerWidth * 0.42 &&
                        rect.height > window.innerHeight * 0.45 &&
                        (
                            text.toLowerCase().includes("i've generated") ||
                            text.toLowerCase().includes("let me know if") ||
                            text.toLowerCase().includes("未命名的会话") ||
                            text.toLowerCase().includes("conversation") ||
                            !!node.querySelector("textarea, [contenteditable='true'], input[type='text']")
                        )
                    )
                    .sort((a, b) => (b.rect.width * b.rect.height) - (a.rect.width * a.rect.height))[0]?.node || null;
            const closeConversationDrawer = async () => {
                const drawer = findConversationDrawer();
                if (!drawer) return "";
                const drawerRect = drawer.getBoundingClientRect();
                const closeButton = Array.from(drawer.querySelectorAll("button, [role='button']"))
                    .filter((node) => visible(node))
                    .find((node) => {
                        const rect = node.getBoundingClientRect();
                        const raw = `${textOf(node)} ${String(node.getAttribute?.("aria-label") || "")}`.toLowerCase();
                        return (
                            rect.top < drawerRect.top + 80 &&
                            rect.left > drawerRect.right - 96 &&
                            (raw.includes("close") || raw.includes("关闭") || raw === "" || raw === "x")
                        );
                    });
                if (!closeButton) return "drawer_detected_without_close";
                clickReal(closeButton);
                await sleep(350);
                return "drawer_closed";
            };
            const getButtonIconText = (node) => textOf(node?.querySelector?.(".google-symbols, i"));
            const classifyButtonKind = (node) => {
                const text = textOf(node);
                const icon = getButtonIconText(node).toLowerCase();
                const ariaLabel = String(node.getAttribute?.("aria-label") || "").toLowerCase();
                const raw = `${text} ${ariaLabel} ${icon}`.toLowerCase();
                if (icon.includes("add_2")) return "add_dialog";
                if (icon.includes("arrow_forward") || raw.includes("arrow_forward")) return "submit";
                if (node.getAttribute?.("aria-haspopup")) return "popup";
                if (raw.includes("创建") || raw.includes("generate")) return "create_like";
                return "other";
            };
            const clickFirstButton = (predicates, label) => {
                for (const button of Array.from(document.querySelectorAll("button, [role='button']"))) {
                    if (!visible(button)) continue;
                    const text = textOf(button);
                    if (!text) continue;
                    if (predicates.some((predicate) => predicate(text))) {
                        try {
                            button.dispatchEvent(new MouseEvent("click", { bubbles: true, cancelable: true }));
                        } catch (_error) {
                            // ignore synthetic click dispatch failure
                        }
                        if (typeof button.click === "function") {
                            button.click();
                        }
                        return `${label}:${text.slice(0, 80)}`;
                    }
                }
                return "";
            };
            const isClickableLike = (node) => {
                if (!node || !visible(node)) return false;
                if (node.tagName === "BUTTON") return true;
                if (node.getAttribute?.("role") === "button") return true;
                if (node.getAttribute?.("role") === "tab") return true;
                if (Number(node.tabIndex) >= 0) return true;
                const className = String(node.className || "");
                const style = window.getComputedStyle(node);
                return (
                    style.cursor === "pointer" ||
                    /\b(cursor-pointer|clickable|chip|segmented|toggle)\b/i.test(className)
                );
            };
            const targetGenerationModeOnPage = String(requestedGenerationMode || "video").trim().toLowerCase() === "image"
                ? "image"
                : "video";
            const collectVideoTabCandidates = () => {
                const selectors = ["[role='tab']", "button", "[role='button']"].join(", ");
                const promptTarget = findPromptTarget();
                const promptRect = promptTarget?.getBoundingClientRect?.() || null;
                return Array.from(document.querySelectorAll(selectors))
                    .filter((node) => visible(node))
                    .map((node) => {
                        const text = textOf(node);
                        const compact = text.replace(/\s+/g, "").toLowerCase();
                        const rect = node.getBoundingClientRect();
                        const inNav = !!node.closest?.("nav, aside, [role='navigation']");
                        const inDialog = !!node.closest?.("[role='dialog'], [aria-modal='true']");
                        const parentText = textOf(node.parentElement || null);
                        const rawAria = `${String(node.getAttribute?.("aria-label") || "")} ${String(node.getAttribute?.("aria-controls") || "")}`.toLowerCase();
                        const looksLikeVideoToggle =
                            text === "视频" ||
                            text.includes("视频 ·") ||
                            text.includes("观看视频") ||
                            text.startsWith("videocam") ||
                            text.includes("play_circle 视频") ||
                            compact.includes("play_circle视频");
                        if (!looksLikeVideoToggle) return null;
                        if (inNav) return null;
                        if (inDialog) return null;
                        if (compact.includes("查看设置")) return null;
                        if (rawAria.includes("settings")) return null;
                        const distanceScore = promptRect
                            ? Math.abs(rect.bottom - promptRect.top) + Math.abs(rect.left - promptRect.left)
                            : 999999;
                        const roleScore = node.getAttribute?.("role") === "tab" ? 0 : 1;
                        const selectedScore =
                            String(node.getAttribute?.("aria-selected") || "").toLowerCase() === "true" ? 2 : 0;
                        const parentScore =
                            parentText.includes("图片") || parentText.includes("视频") || parentText.includes("Veo")
                                ? 0
                                : 3;
                        return {
                            node,
                            text,
                            distanceScore,
                            roleScore,
                            selectedScore,
                            parentScore,
                        };
                    })
                    .filter(Boolean)
                    .sort((a, b) => {
                        return (
                            a.roleScore - b.roleScore ||
                            a.selectedScore - b.selectedScore ||
                            a.parentScore - b.parentScore ||
                            a.distanceScore - b.distanceScore
                        );
                    });
                for (const item of candidates) {
                    const node = item.node;
                    const text = item.text;
                    clickReal(node);
                    return `click:video-tab:${text.slice(0, 80)}`;
                }
                return "";
            };
            const clickVideoSettingsChip = () => {
                for (const node of Array.from(document.querySelectorAll("button, [role='button'], [role='tab']"))) {
                    if (!isClickableLike(node)) continue;
                    const text = textOf(node);
                    if (!text) continue;
                    const compact = text.replace(/\s+/g, "");
                    if (
                        compact.includes("视频·") ||
                        compact.includes("视频4s") ||
                        compact.includes("视频6s") ||
                        compact.includes("视频8s") ||
                        compact.includes("视频10s") ||
                        compact.includes("veo3.1")
                    ) {
                        clickReal(node);
                        return `click:settings-chip:${text.slice(0, 80)}`;
                    }
                }
                return "";
            };
            const clickVideoSubmode = (preferredText) => {
                const compactPreferred = String(preferredText || "").replace(/\s+/g, "").trim();
                const exactTabCandidates = Array.from(document.querySelectorAll("[role='tab']")).filter((node) => {
                    if (!visible(node)) return false;
                    const compactText = textOf(node).replace(/\s+/g, "").trim();
                    if (!compactText || !compactText.includes(compactPreferred)) return false;
                    if (compactPreferred === "帧" && compactText.includes("素材")) return false;
                    if (compactPreferred === "素材" && compactText.includes("帧")) return false;
                    return true;
                }).sort((a, b) => {
                    const aSelected = String(a.getAttribute("aria-selected") || "").toLowerCase() === "true" ? 1 : 0;
                    const bSelected = String(b.getAttribute("aria-selected") || "").toLowerCase() === "true" ? 1 : 0;
                    return aSelected - bSelected;
                });
                const fallbackCandidates = Array.from(
                    document.querySelectorAll(
                        "[role='button'], button, [aria-selected], [data-state], [data-orientation], [aria-controls], [tabindex]"
                    )
                ).filter((node) => {
                    if (!visible(node)) return false;
                    const compactText = textOf(node).replace(/\s+/g, "").trim();
                    return !!compactText && compactText.includes(compactPreferred);
                });
                for (const node of [...exactTabCandidates, ...fallbackCandidates]) {
                    try {
                        node.focus?.();
                    } catch (_error) {
                        // ignore focus failure
                    }
                    clickReal(node);
                    try {
                        node.dispatchEvent(new KeyboardEvent("keydown", { bubbles: true, cancelable: true, key: "Enter" }));
                        node.dispatchEvent(new KeyboardEvent("keyup", { bubbles: true, cancelable: true, key: "Enter" }));
                        node.dispatchEvent(new KeyboardEvent("keydown", { bubbles: true, cancelable: true, key: " " }));
                        node.dispatchEvent(new KeyboardEvent("keyup", { bubbles: true, cancelable: true, key: " " }));
                    } catch (_error) {
                        // ignore keyboard fallback failure
                    }
                    return `click:submode:${textOf(node).slice(0, 80)}`;
                }
                return "";
            };
            const openCreateDialogAndSelectMode = async (requestedMode) => {
                const addDialogButton = Array.from(
                    document.querySelectorAll("button, [role='button'], input[type='button'], input[type='submit']")
                )
                    .filter((node) => visible(node))
                    .find((node) => classifyButtonKind(node) === "add_dialog");
                // #endregion
                if (!addDialogButton) {
                    return "";
                }
                const addClick = clickReal(addDialogButton);
                await sleep(300);
                const dialogs = Array.from(document.querySelectorAll("[role='dialog'], [aria-modal='true']")).filter((node) => visible(node));
                // #endregion
                if (!dialogs.length) {
                    return addClick.clicked ? "click:add-dialog" : "";
                }
                const combinedDialogText = dialogs
                    .map((node) => textOf(node))
                    .join(" | ");
                const looksLikeMediaLibrary =
                    combinedDialogText.includes("上传媒体") ||
                    combinedDialogText.includes("最近") ||
                    (combinedDialogText.includes("全部") &&
                        combinedDialogText.includes("图片") &&
                        combinedDialogText.includes("视频") &&
                        combinedDialogText.includes("语音"));
                if (looksLikeMediaLibrary) {
                    try {
                        document.dispatchEvent(
                            new KeyboardEvent("keydown", { key: "Escape", bubbles: true, cancelable: true })
                        );
                    } catch (_error) {
                        // ignore dialog close failure
                    }
                    await sleep(150);
                    if (document.body) {
                        document.body.click();
                    }
                    return "";
                }
                for (const dialog of dialogs) {
                    const targetMode = String(requestedMode || "video").trim().toLowerCase() === "image" ? "image" : "video";
                    const desiredOption = Array.from(dialog.querySelectorAll("button, [role='button'], *"))
                        .filter((node) => visible(node))
                        .find((node) => {
                            const text = textOf(node);
                            if (targetMode === "image") {
                                return (
                                    text === "图片" ||
                                    text.includes(" image 图片") ||
                                    text.startsWith("image 图片") ||
                                    text.includes("图片 ")
                                );
                            }
                            return (
                                text === "视频" ||
                                text.includes(" videocam 视频") ||
                                text.startsWith("videocam 视频") ||
                                text.includes("视频 ")
                            );
                        });
                    // #endregion
                    if (!videoOption) continue;
                    clickReal(videoOption);
                    await sleep(500);
                    return "click:add-dialog-video";
                }
                return addClick.clicked ? "click:add-dialog" : "";
            };
            const createInputEvent = (type, data) => {
                try {
                    return new InputEvent(type, {
                        bubbles: true,
                        cancelable: true,
                        data,
                        inputType: type === "beforeinput" ? "insertText" : "insertText",
                    });
                } catch (error) {
                    return new Event(type, { bubbles: true, cancelable: true });
                }
            };
            const dispatchPasteText = (target, value) => {
                if (!target || !value) return false;
                try {
                    const dataTransfer = new DataTransfer();
                    dataTransfer.setData("text/plain", value);
                    dataTransfer.setData(
                        "text/html",
                        String(value)
                            .replace(/&/g, "&amp;")
                            .replace(/</g, "&lt;")
                            .replace(/>/g, "&gt;")
                    );
                    let event;
                    try {
                        event = new ClipboardEvent("paste", {
                            bubbles: true,
                            cancelable: true,
                            clipboardData: dataTransfer,
                        });
                    } catch (_error) {
                        event = new Event("paste", { bubbles: true, cancelable: true });
                        Object.defineProperty(event, "clipboardData", {
                            configurable: true,
                            value: dataTransfer,
                        });
                    }
                    return !!target.dispatchEvent(event);
                } catch (_error) {
                    return false;
                }
            };
            const placeCaretAtEnd = (target) => {
                if (!target) return;
                const selection = window.getSelection();
                if (!selection) return;
                const range = document.createRange();
                range.selectNodeContents(target);
                range.collapse(false);
                selection.removeAllRanges();
                selection.addRange(range);
            };
            const clearPromptTarget = (target) => {
                if (!target) return;
                if (target.tagName === "TEXTAREA" || target.tagName === "INPUT") {
                    target.value = "";
                } else if (target.getAttribute?.("data-slate-editor") === "true") {
                    if (document.execCommand) {
                        target.focus();
                        document.execCommand("selectAll", false);
                        document.execCommand("delete", false);
                    } else {
                        const selection = window.getSelection();
                        const range = document.createRange();
                        range.selectNodeContents(target);
                        selection?.removeAllRanges();
                        selection?.addRange(range);
                        selection?.deleteFromDocument();
                    }
                    placeCaretAtEnd(target);
                } else if (target.getAttribute?.("contenteditable") === "true") {
                    target.textContent = "";
                    placeCaretAtEnd(target);
                }
            };
            const typeText = async (target, value) => {
                if (!target) return false;
                // #endregion
                clearPromptTarget(target);
                target.focus();
                if (target.getAttribute?.("contenteditable") === "true") {
                    placeCaretAtEnd(target);
                }
                if (target.getAttribute?.("data-slate-editor") === "true") {
                    target.dispatchEvent(createInputEvent("beforeinput", value));
                    let inserted = false;
                    if (document.execCommand) {
                        document.execCommand("selectAll", false);
                        inserted = document.execCommand("insertText", false, value);
                    }
                    if (!inserted) {
                        const selection = window.getSelection();
                        const range = selection?.rangeCount ? selection.getRangeAt(0) : document.createRange();
                        const textNode = document.createTextNode(value);
                        range.deleteContents();
                        range.insertNode(textNode);
                        range.setStartAfter(textNode);
                        range.collapse(true);
                        selection?.removeAllRanges();
                        selection?.addRange(range);
                    }
                    target.dispatchEvent(createInputEvent("input", value));
                    target.dispatchEvent(new Event("change", { bubbles: true, cancelable: true }));
                    await sleep(80);
                } else {
                    for (const ch of Array.from(value)) {
                        target.dispatchEvent(createInputEvent("beforeinput", ch));
                        if (target.tagName === "TEXTAREA" || target.tagName === "INPUT") {
                            target.value += ch;
                        } else if (target.getAttribute?.("contenteditable") === "true") {
                            if (document.execCommand) {
                                const inserted = document.execCommand("insertText", false, ch);
                                if (!inserted) {
                                    target.textContent = `${target.textContent || ""}${ch}`;
                                    placeCaretAtEnd(target);
                                }
                            } else {
                                target.textContent = `${target.textContent || ""}${ch}`;
                                placeCaretAtEnd(target);
                            }
                        }
                        target.dispatchEvent(createInputEvent("input", ch));
                        target.dispatchEvent(
                            new KeyboardEvent("keydown", {
                                bubbles: true,
                                cancelable: true,
                                key: ch,
                            })
                        );
                        target.dispatchEvent(
                            new KeyboardEvent("keyup", {
                                bubbles: true,
                                cancelable: true,
                                key: ch,
                            })
                        );
                        await sleep(20);
                    }
                }
                // #endregion
                return true;
            };
            const collectStatus = (label, promptTarget) => {
                const bodyText = String(document.body?.innerText || "").replace(/\s+/g, " ").trim();
                const buttons = Array.from(
                    document.querySelectorAll("button, [role='button'], input[type='button'], input[type='submit']")
                )
                    .filter((node) => visible(node))
                    .map((node) => {
                        const text = textOf(node);
                        return {
                            text: text.slice(0, 160),
                            disabled: !!node.disabled || node.getAttribute("aria-disabled") === "true",
                            aria_disabled: String(node.getAttribute("aria-disabled") || ""),
                            icon: getButtonIconText(node).slice(0, 80),
                            kind: classifyButtonKind(node),
                        };
                    })
                    .filter((item) => item.kind !== "other");
                return {
                    label,
                    prompt_state: parsePromptState(),
                    prompt_target: summarizeElement(promptTarget),
                    active_element: summarizeElement(document.activeElement),
                    create_buttons: buttons,
                    body_error_hint: bodyText.includes("必须提供提示") ? "必须提供提示" : "",
                    body_text_prefix: bodyText.slice(0, 220),
                };
            };

            const promptTarget = findPromptTarget();
            const result = {
                href: String(location.href || ""),
                title: String(document.title || ""),
                readyState: String(document.readyState || ""),
                prompt_requested: promptValue,
                steps: [],
            };

            result.steps.push(collectStatus("initial", promptTarget));

            if (promptTarget && promptValue) {
                const typed = await typeText(promptTarget, promptValue);
                result.typed = typed;
                result.steps.push(collectStatus("after_type", promptTarget));

                await sleep(300);
                result.steps.push(collectStatus("after_short_wait", promptTarget));

                promptTarget.dispatchEvent(new Event("blur", { bubbles: true, cancelable: true }));
                if (promptTarget.blur) {
                    promptTarget.blur();
                }
                await sleep(250);
                result.steps.push(collectStatus("after_blur", promptTarget));

                if (document.body) {
                    document.body.click();
                }
                await sleep(500);
                result.steps.push(collectStatus("after_body_click", promptTarget));
            } else {
                result.typed = false;
                result.steps.push(collectStatus("missing_prompt_target", promptTarget));
            }

            result.best_submit_candidate = result.steps
                .flatMap((step) => step.create_buttons || [])
                .find((item) => item.kind === "submit") || null;
            return result;
        },
    });
    return results?.[0]?.result || {};
}

async function runVideoUiSubmit(tabId, jobPayload) {
    const VIDEO_EDIT_REQUEST_FRAME_RATE = 60;
    const promptText = String(jobPayload?.prompt || "").trim();
    const submitMode = String(jobPayload?.submit_mode || "full").trim() || "full";
    const preferredSubmode = String(jobPayload?.preferred_submode || "文本").trim() || "文本";
    const desiredModelDisplayName = String(jobPayload?.desired_model_display_name || "").trim();
    const desiredDurationSeconds = String(jobPayload?.desired_duration_seconds || "").trim();
    const desiredAspectRatioLabel = String(jobPayload?.desired_aspect_ratio_label || "").trim();
    const desiredOutputsPerPrompt = (() => {
        const parsed = Number.parseInt(String(jobPayload?.desired_outputs_per_prompt || "").trim(), 10);
        return Number.isFinite(parsed) && parsed > 0 ? Math.max(1, Math.min(4, parsed)) : 0;
    })();
    const desiredStartFrameIndex = (() => {
        const parsed = Number.parseInt(String(jobPayload?.desired_start_frame_index || "").trim(), 10);
        return Number.isFinite(parsed) && parsed >= 0 ? parsed : null;
    })();
    const desiredEndFrameIndex = (() => {
        const parsed = Number.parseInt(String(jobPayload?.desired_end_frame_index || "").trim(), 10);
        return Number.isFinite(parsed) && parsed >= 0 ? parsed : null;
    })();
    const desiredStartSeconds = (() => {
        const parsed = Number.parseFloat(String(jobPayload?.desired_start_seconds || "").trim());
        return Number.isFinite(parsed) && parsed >= 0 ? parsed : null;
    })();
    const desiredEndSeconds = (() => {
        const parsed = Number.parseFloat(String(jobPayload?.desired_end_seconds || "").trim());
        return Number.isFinite(parsed) && parsed >= 0 ? parsed : null;
    })();
    const desiredSourceDurationSeconds = (() => {
        const parsed = Number.parseFloat(String(jobPayload?.desired_source_duration_seconds || "").trim());
        return Number.isFinite(parsed) && parsed > 0 ? parsed : null;
    })();
    const targetGenerationMode = String(jobPayload?.target_generation_mode || "video").trim().toLowerCase() === "image"
        ? "image"
        : "video";
    const referenceTexts = Array.isArray(jobPayload?.reference_texts)
        ? jobPayload.reference_texts.map((item) => String(item || "").trim()).filter(Boolean).slice(0, 8)
        : [];
    const referenceMediaId = String(jobPayload?.reference_media_id || "").trim();
    const debugVideoEditSubmitHookMode = String(jobPayload?.debug_video_edit_submit_hook_mode || "").trim().toLowerCase();
    const effectiveVideoEditSubmitHookMode =
        debugVideoEditSubmitHookMode === "capture" || debugVideoEditSubmitHookMode === "rewrite"
            ? debugVideoEditSubmitHookMode
            : "";
    const debugVideoEditSubmitHookConfig =
        effectiveVideoEditSubmitHookMode === "capture" || effectiveVideoEditSubmitHookMode === "rewrite"
            ? {
                  enabled: true,
                  mode: effectiveVideoEditSubmitHookMode,
                  overrideMediaId: effectiveVideoEditSubmitHookMode === "rewrite" ? referenceMediaId : "",
                  overrideStartFrameIndex:
                      effectiveVideoEditSubmitHookMode === "rewrite" &&
                      desiredStartFrameIndex !== null &&
                      desiredStartFrameIndex !== undefined &&
                      Number.isFinite(Number(desiredStartFrameIndex))
                          ? Number(desiredStartFrameIndex)
                          : null,
                  overrideEndFrameIndex:
                      effectiveVideoEditSubmitHookMode === "rewrite" &&
                      desiredEndFrameIndex !== null &&
                      desiredEndFrameIndex !== undefined &&
                      Number.isFinite(Number(desiredEndFrameIndex))
                          ? Number(desiredEndFrameIndex)
                          : null,
                  overrideStartSeconds:
                      effectiveVideoEditSubmitHookMode === "rewrite" && Number.isFinite(Number(desiredStartSeconds))
                          ? Number(desiredStartSeconds)
                          : null,
                  overrideEndSeconds:
                      effectiveVideoEditSubmitHookMode === "rewrite" && Number.isFinite(Number(desiredEndSeconds))
                          ? Number(desiredEndSeconds)
                          : null,
                  overrideSourceDurationSeconds:
                      effectiveVideoEditSubmitHookMode === "rewrite" && Number.isFinite(Number(desiredSourceDurationSeconds))
                          ? Number(desiredSourceDurationSeconds)
                          : null,
                  overrideFrameRate: VIDEO_EDIT_REQUEST_FRAME_RATE,
              }
            : null;
    const debugHoldReferencePickerMs = (() => {
        const parsed = Number.parseInt(String(jobPayload?.debug_hold_reference_picker_ms || "").trim(), 10);
        return Number.isFinite(parsed) && parsed > 0 ? Math.min(parsed, 120000) : 0;
    })();
    const debugSkipAddToPrompt = !!jobPayload?.debug_skip_add_to_prompt || debugHoldReferencePickerMs > 0;
    let results = null;
    try {
        if (debugVideoEditSubmitHookConfig) {
            await chrome.scripting.executeScript({
                target: { tabId },
                world: "MAIN",
                files: ["video-edit-submit-hook.js"],
            });
        }
        results = await Promise.race([
            chrome.scripting.executeScript({
                target: { tabId },
                world: "MAIN",
                args: [
                    promptText,
                    submitMode,
                    EXTENSION_BUILD_MARKER,
                    preferredSubmode,
                    referenceTexts,
                    referenceMediaId,
                    targetGenerationMode,
                    desiredModelDisplayName,
                    desiredDurationSeconds,
                    desiredAspectRatioLabel,
                    desiredOutputsPerPrompt,
                    desiredStartFrameIndex,
                    desiredEndFrameIndex,
                    desiredStartSeconds,
                    desiredEndSeconds,
                    desiredSourceDurationSeconds,
                    debugVideoEditSubmitHookConfig,
                    debugHoldReferencePickerMs,
                    debugSkipAddToPrompt,
                ],
                func: async (
                    promptValue,
                    mode,
                    buildMarker,
                    targetSubmode,
                    sourceReferenceTexts,
                    sourceReferenceMediaId,
                    requestedGenerationMode,
                    requestedModelDisplayName,
                    requestedDurationSeconds,
                    requestedAspectRatioLabel,
                    requestedOutputsPerPrompt,
                    requestedStartFrameIndex,
                    requestedEndFrameIndex,
                    requestedStartSeconds,
                    requestedEndSeconds,
                    requestedSourceDurationSeconds,
                    requestedDebugVideoEditSubmitHookConfig,
                    requestedDebugHoldReferencePickerMs,
                    requestedDebugSkipAddToPrompt
                ) => {
            const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
            const frontendErrors = [];
            let stage = "init";
            let submitRequestHook = null;
            const finalize = (value) => {
                try {
                    return JSON.parse(JSON.stringify(value));
                } catch (error) {
                    return {
                        result_version: "video_ui_submit_v3",
                        build_marker: String(buildMarker || ""),
                        submit_mode: mode,
                        probe_error: `serialize_failed:${String(error?.message || error)}`,
                        frontend_errors: frontendErrors.slice(-12),
                        page_title: String(document.title || ""),
                        href: String(location.href || ""),
                        stage,
                    };
                }
            };
            const withTimeout = async (label, work, timeoutMs = 5000) => {
                let timer = null;
                const promise = typeof work === "function" ? Promise.resolve().then(work) : Promise.resolve(work);
                try {
                    return await Promise.race([
                        promise,
                        new Promise((_, reject) => {
                            timer = setTimeout(() => {
                                reject(new Error(`stage_timeout:${label}:${timeoutMs}`));
                            }, timeoutMs);
                        }),
                    ]);
                } finally {
                    if (timer) clearTimeout(timer);
                }
            };
            const recordFrontendError = (kind, payload) => {
                frontendErrors.push({
                    kind,
                    detail: String(payload || "").slice(0, 500),
                });
            };
            const originalConsoleError = console.error;
            const consoleErrorProxy = (...args) => {
                try {
                    recordFrontendError(
                        "console.error",
                        args
                            .map((item) => {
                                if (typeof item === "string") return item;
                                try {
                                    return JSON.stringify(item);
                                } catch (error) {
                                    return String(item);
                                }
                            })
                            .join(" ")
                    );
                } catch (error) {
                    // ignore logging failures
                }
                return originalConsoleError.apply(console, args);
            };
            console.error = consoleErrorProxy;
            const onWindowError = (event) => {
                recordFrontendError("window.error", event?.message || event?.error || "");
            };
            const onUnhandledRejection = (event) => {
                recordFrontendError("unhandledrejection", event?.reason || "");
            };
            window.addEventListener("error", onWindowError);
            window.addEventListener("unhandledrejection", onUnhandledRejection);
            const visible = (el) => {
                if (!el) return false;
                const rect = el.getBoundingClientRect();
                const style = window.getComputedStyle(el);
                return rect.width > 8 && rect.height > 8 && style.visibility !== "hidden" && style.display !== "none";
            };
            const textOf = (el) =>
                String(
                    el?.innerText ||
                    el?.textContent ||
                    el?.value ||
                    el?.getAttribute?.("aria-label") ||
                    el?.getAttribute?.("placeholder") ||
                    ""
                )
                    .replace(/\s+/g, " ")
                    .trim();
            const normalizeUiText = (value) =>
                String(value || "")
                    .replace(/\s+/g, "")
                    .replace(/[()（）]/g, "")
                    .trim()
                    .toLowerCase();
            const summarizeElement = (el) => {
                if (!el) return null;
                return {
                    tag: String(el.tagName || "").toLowerCase(),
                    type: String(el.getAttribute?.("type") || ""),
                    role: String(el.getAttribute?.("role") || ""),
                    aria_label: String(el.getAttribute?.("aria-label") || ""),
                    placeholder: String(el.getAttribute?.("placeholder") || ""),
                    contenteditable: String(el.getAttribute?.("contenteditable") || ""),
                    data_slate_editor: String(el.getAttribute?.("data-slate-editor") || ""),
                    aria_disabled: String(el.getAttribute?.("aria-disabled") || ""),
                    disabled: !!el.disabled,
                    text: textOf(el).slice(0, 160),
                };
            };
            const collectSettingsOverlaySnapshot = () =>
                collectSettingsOverlayRoots().slice(0, 4).map((root) => {
                    const rect = root.getBoundingClientRect?.() || null;
                    return {
                        node: summarizeElement(root),
                        role: String(root.getAttribute?.("role") || ""),
                        data_state: String(root.getAttribute?.("data-state") || ""),
                        text: textOf(root).slice(0, 500),
                        rect: rect
                            ? {
                                  left: Math.round(rect.left),
                                  top: Math.round(rect.top),
                                  width: Math.round(rect.width),
                                  height: Math.round(rect.height),
                              }
                            : null,
                    };
                });
            const summarizeSlateState = (target) => {
                if (!target || target.getAttribute?.("data-slate-editor") !== "true") {
                    return null;
                }
                const slateStrings = Array.from(target.querySelectorAll("[data-slate-string='true']"))
                    .map((node) => String(node.textContent || "").slice(0, 160));
                const placeholders = Array.from(target.querySelectorAll("[data-slate-placeholder], [data-slate-zero-width]"))
                    .map((node) => ({
                        tag: String(node.tagName || "").toLowerCase(),
                        text: String(node.textContent || "").slice(0, 160),
                        placeholder: String(node.getAttribute?.("data-slate-placeholder") || ""),
                        zero_width: String(node.getAttribute?.("data-slate-zero-width") || ""),
                        length: Number(node.getAttribute?.("data-slate-length") || 0),
                    }));
                const child_nodes = Array.from(target.childNodes || []).slice(0, 8).map((node) => ({
                    node_type: Number(node.nodeType || 0),
                    tag: node.nodeType === Node.ELEMENT_NODE ? String(node.tagName || "").toLowerCase() : "",
                    text: String(node.textContent || "").slice(0, 160),
                    html: node.nodeType === Node.ELEMENT_NODE ? String(node.outerHTML || "").slice(0, 220) : "",
                }));
                const text_nodes = [];
                try {
                    const walker = document.createTreeWalker(target, NodeFilter.SHOW_TEXT);
                    let currentNode = walker.nextNode();
                    while (currentNode && text_nodes.length < 10) {
                        text_nodes.push({
                            text: String(currentNode.textContent || "").slice(0, 160),
                            parent_tag: String(currentNode.parentElement?.tagName || "").toLowerCase(),
                            parent_attrs: {
                                data_slate_string: String(currentNode.parentElement?.getAttribute?.("data-slate-string") || ""),
                                data_slate_zero_width: String(currentNode.parentElement?.getAttribute?.("data-slate-zero-width") || ""),
                                data_slate_placeholder: String(currentNode.parentElement?.getAttribute?.("data-slate-placeholder") || ""),
                            },
                        });
                        currentNode = walker.nextNode();
                    }
                } catch (_error) {}
                return {
                    inner_html: String(target.innerHTML || "").slice(0, 1200),
                    slate_strings: slateStrings,
                    placeholders,
                    inner_text: String(target.innerText || target.textContent || "").slice(0, 240),
                    child_nodes,
                    text_nodes,
                };
            };
            const summarizeFlowPromptBoxState = () => {
                const flowState = window.FLOW_MAIN_PROMPT_BOX_STATE || null;
                if (!flowState || typeof flowState !== "object") return null;
                const summary = {};
                for (const [key, value] of Object.entries(flowState).slice(0, 24)) {
                    if (typeof value === "string") {
                        summary[key] = value.slice(0, 240);
                    } else if (typeof value === "number" || typeof value === "boolean" || value == null) {
                        summary[key] = value;
                    } else if (Array.isArray(value)) {
                        summary[key] = value.slice(0, 8).map((item) =>
                            typeof item === "string" ? item.slice(0, 120) : (item && typeof item === "object" ? Object.keys(item).slice(0, 8) : item)
                        );
                    } else if (typeof value === "object") {
                        summary[key] = Object.keys(value).slice(0, 12);
                    } else {
                        summary[key] = String(value).slice(0, 120);
                    }
                }
                return summary;
            };
            const collectPromptRuntimeEvidence = (target) => ({
                prompt_target: summarizeElement(target),
                prompt_state: parsePromptState(),
                flow_prompt_box_state: summarizeFlowPromptBoxState(),
                prompt_slate: summarizeSlateState(target),
                prompt_dom: target
                    ? {
                        text: String(target.innerText || target.textContent || "").slice(0, 240),
                        html: String(target.innerHTML || "").slice(0, 1200),
                        child_count: Number(target.childNodes?.length || 0),
                        is_connected: !!target.isConnected,
                    }
                    : null,
                selection: summarizeSelection(),
                active_element: summarizeElement(document.activeElement),
            });
            const parsePromptState = () => {
                const raw = String(window.localStorage.getItem("FLOW_MAIN_PROMPT_BOX_STATE") || "");
                if (!raw) {
                    return { raw, promptMode: "", selectedVideoModelFamily: "" };
                }
                try {
                    const parsed = JSON.parse(raw);
                    return {
                        raw,
                        promptMode: String(parsed?.imageOrVideoMode || ""),
                        selectedVideoModelFamily: String(parsed?.selectedVideoModelFamily || ""),
                        selectedVideoDuration: String(parsed?.selectedVideoDuration || ""),
                        outputsPerPrompt: String(parsed?.outputsPerPrompt || ""),
                        aspectRatio: String(parsed?.aspectRatio || ""),
                    };
                } catch (error) {
                    return {
                        raw,
                        promptMode: "",
                        selectedVideoModelFamily: "",
                        parseError: String(error?.message || error),
                    };
                }
            };
            const isLikelyPromptField = (el) => {
                if (!visible(el)) return false;
                if (
                    el.getAttribute?.("data-slate-editor") === "true" &&
                    el.getAttribute?.("role") === "textbox" &&
                    el.getAttribute?.("contenteditable") === "true"
                ) {
                    return true;
                }
                const meta = [
                    el.getAttribute?.("aria-label") || "",
                    el.getAttribute?.("placeholder") || "",
                    el.getAttribute?.("name") || "",
                    el.id || "",
                    el.className || "",
                ]
                    .join(" ")
                    .toLowerCase();
                const text = textOf(el).toLowerCase();
                if (meta.includes("search") || text.includes("搜索")) return false;
                if (meta.includes("prompt") || meta.includes("message") || meta.includes("describe")) return true;
                if (text.includes("创作") || text.includes("describe") || text.includes("prompt")) return true;
                if (el.tagName === "TEXTAREA") return true;
                if (el.getAttribute?.("contenteditable") === "true") return true;
                return false;
            };
            const isConversationLikeTarget = (el) => {
                if (!el || !visible(el)) return false;
                const rect = el.getBoundingClientRect();
                const inspectNodes = [];
                let current = el;
                while (current && inspectNodes.length < 8) {
                    inspectNodes.push(current);
                    current = current.parentElement;
                }
                const rightNarrow =
                    rect.left > window.innerWidth * 0.55 && rect.width < window.innerWidth * 0.45;
                const conversationMarker = inspectNodes.some((node) => {
                    if (!node || typeof node.getBoundingClientRect !== "function") return false;
                    const nodeRect = node.getBoundingClientRect();
                    const text = textOf(node).toLowerCase();
                    const narrowPanel =
                        nodeRect.left > window.innerWidth * 0.5 &&
                        nodeRect.width > 220 &&
                        nodeRect.width < window.innerWidth * 0.48;
                    return (
                        narrowPanel &&
                        (
                            text.includes("未命名的会话") ||
                            text.includes("conversation") ||
                            text.includes("i've generated") ||
                            text.includes("let me know if") ||
                            text.includes("what would you like to create") ||
                            text.includes("您希望创作什么内容")
                        )
                    );
                });
                return rightNarrow || conversationMarker;
            };
            const collectPromptCandidates = () => {
                const slateTargets = Array.from(
                    document.querySelectorAll("[data-slate-editor='true'][role='textbox'][contenteditable='true']")
                ).filter((el) => visible(el) && isLikelyPromptField(el));
                const promptTargets = Array.from(
                    document.querySelectorAll("textarea, [contenteditable='true'], input[type='text']")
                ).filter((el) => isLikelyPromptField(el));
                return [...slateTargets, ...promptTargets]
                    .filter((el, index, items) => items.indexOf(el) === index)
                    .sort((a, b) => {
                        const aRect = a.getBoundingClientRect();
                        const bRect = b.getBoundingClientRect();
                        const aBottomComposerScore =
                            (aRect.bottom > window.innerHeight * 0.7 ? 2 : 0) +
                            (Math.abs(aRect.left + aRect.width / 2 - window.innerWidth / 2) < window.innerWidth * 0.2 ? 1 : 0);
                        const bBottomComposerScore =
                            (bRect.bottom > window.innerHeight * 0.7 ? 2 : 0) +
                            (Math.abs(bRect.left + bRect.width / 2 - window.innerWidth / 2) < window.innerWidth * 0.2 ? 1 : 0);
                        if (aBottomComposerScore !== bBottomComposerScore) {
                            return bBottomComposerScore - aBottomComposerScore;
                        }
                        return bRect.width - aRect.width;
                    });
            };
            const findAnyPromptTarget = () => collectPromptCandidates()[0] || null;
            const findPromptTarget = () =>
                collectPromptCandidates().find((el) => !isConversationLikeTarget(el)) || null;
            const getButtonIconText = (node) => textOf(node?.querySelector?.(".google-symbols, i"));
            const classifyButtonKind = (node) => {
                const text = textOf(node);
                const icon = getButtonIconText(node).toLowerCase();
                const ariaLabel = String(node.getAttribute?.("aria-label") || "").toLowerCase();
                const raw = `${text} ${ariaLabel} ${icon}`.toLowerCase();
                if (icon.includes("add_2")) return "add_dialog";
                if (icon.includes("arrow_forward") || raw.includes("arrow_forward")) return "submit";
                if (node.getAttribute?.("aria-haspopup")) return "popup";
                if (raw.includes("创建") || raw.includes("generate")) return "create_like";
                return "other";
            };
            const createInputEvent = (type, data) => {
                try {
                    return new InputEvent(type, {
                        bubbles: true,
                        cancelable: true,
                        data,
                        inputType: "insertText",
                    });
                } catch (error) {
                    return new Event(type, { bubbles: true, cancelable: true });
                }
            };
            const dispatchPasteText = (target, value) => {
                if (!target || !value) return false;
                try {
                    const dataTransfer = new DataTransfer();
                    dataTransfer.setData("text/plain", value);
                    dataTransfer.setData(
                        "text/html",
                        String(value)
                            .replace(/&/g, "&amp;")
                            .replace(/</g, "&lt;")
                            .replace(/>/g, "&gt;")
                    );
                    let event;
                    try {
                        event = new ClipboardEvent("paste", {
                            bubbles: true,
                            cancelable: true,
                            clipboardData: dataTransfer,
                        });
                    } catch (_error) {
                        event = new Event("paste", { bubbles: true, cancelable: true });
                        Object.defineProperty(event, "clipboardData", {
                            configurable: true,
                            value: dataTransfer,
                        });
                    }
                    return !!target.dispatchEvent(event);
                } catch (_error) {
                    return false;
                }
            };
            const placeCaretAtStart = (target) => {
                if (!target) return;
                const selection = window.getSelection();
                if (!selection) return;
                const range = document.createRange();
                range.selectNodeContents(target);
                range.collapse(true);
                selection.removeAllRanges();
                selection.addRange(range);
            };
            const placeCaretAtEnd = (target) => {
                if (!target) return;
                const selection = window.getSelection();
                if (!selection) return;
                const range = document.createRange();
                range.selectNodeContents(target);
                range.collapse(false);
                selection.removeAllRanges();
                selection.addRange(range);
            };
            const clearPromptTarget = (target) => {
                if (!target) return;
                if (target.tagName === "TEXTAREA" || target.tagName === "INPUT") {
                    target.value = "";
                } else if (target.getAttribute?.("data-slate-editor") === "true") {
                    const existingSlateText = Array.from(target.querySelectorAll("[data-slate-string='true']"))
                        .map((node) => String(node.textContent || ""))
                        .join("")
                        .trim();
                    const zeroWidthText = Array.from(target.querySelectorAll("[data-slate-zero-width]"))
                        .map((node) => String(node.textContent || ""))
                        .join("")
                        .replace(/\u200b/g, "")
                        .trim();
                    target.focus();
                    if (!existingSlateText && !zeroWidthText) {
                        placeCaretAtEnd(target);
                        return;
                    }
                    if (document.execCommand) {
                        document.execCommand("selectAll", false);
                        document.execCommand("delete", false);
                    } else {
                        const selection = window.getSelection();
                        const range = document.createRange();
                        range.selectNodeContents(target);
                        selection?.removeAllRanges();
                        selection?.addRange(range);
                        selection?.deleteFromDocument();
                    }
                    placeCaretAtEnd(target);
                } else if (target.getAttribute?.("contenteditable") === "true") {
                    target.textContent = "";
                    placeCaretAtEnd(target);
                }
            };
            const placeCaretForSlateInsert = (target) => {
                const selection = window.getSelection();
                if (!selection) return false;
                const range = document.createRange();
                try {
                    range.selectNodeContents(target);
                    range.collapse(false);
                    selection.removeAllRanges();
                    selection.addRange(range);
                    return true;
                } catch (error) {
                    try {
                        range.selectNodeContents(target);
                        range.collapse(false);
                        selection.removeAllRanges();
                        selection.addRange(range);
                        return true;
                    } catch (_error) {
                        return false;
                    }
                }
            };
            const safeInsertIntoSlate = (target, value) => {
                const normalizeSlateInsertedValue = () => {
                    const slateStrings = Array.from(target.querySelectorAll("[data-slate-string='true']"))
                        .map((node) => String(node.textContent || ""))
                        .join("")
                        .trim();
                    const zeroWidthText = Array.from(target.querySelectorAll("[data-slate-zero-width]"))
                        .map((node) => String(node.textContent || ""))
                        .join("")
                        .replace(/\u200b/g, "")
                        .trim();
                    return {
                        slate_text: slateStrings,
                        zero_width_text: zeroWidthText,
                    };
                };
                target.focus();
                const existingSlateText = Array.from(target.querySelectorAll("[data-slate-string='true']"))
                    .map((node) => String(node.textContent || ""))
                    .join("")
                    .trim();
                if (existingSlateText) {
                    clearPromptTarget(target);
                }
                placeCaretForSlateInsert(target);
                target.dispatchEvent(createInputEvent("beforeinput", value));
                let inserted = false;
                if (document.execCommand) {
                    inserted = document.execCommand("insertText", false, value);
                }
                if (!inserted) {
                    return false;
                }
                const normalized = normalizeSlateInsertedValue();
                target.dispatchEvent(createInputEvent("input", value));
                target.dispatchEvent(new Event("change", { bubbles: true, cancelable: true }));
                return !!normalized?.slate_text;
            };
            const typeText = async (target, value) => {
                if (!target) return false;
                // #endregion
                target.focus();
                if (target.getAttribute?.("contenteditable") === "true") {
                    placeCaretAtEnd(target);
                }
                if (target.getAttribute?.("data-slate-editor") === "true") {
                    const startedAt = Date.now();
                    // #endregion
                    clearPromptTarget(target);
                    // #endregion
                    let inserted = false;
                    const pasteDispatched = dispatchPasteText(target, value);
                    await sleep(80);
                    const pastedSlateState = summarizeSlateState(target);
                    const pastedSlateText = Array.isArray(pastedSlateState?.slate_strings)
                        ? pastedSlateState.slate_strings.join("").trim()
                        : "";
                    // #endregion
                    if (!pastedSlateText) {
                        target.dispatchEvent(createInputEvent("beforeinput", value));
                        if (document.execCommand) {
                            document.execCommand("selectAll", false);
                            inserted = document.execCommand("insertText", false, value);
                        }
                        if (!inserted) {
                            const selection = window.getSelection();
                            const range = selection?.rangeCount ? selection.getRangeAt(0) : document.createRange();
                            const textNode = document.createTextNode(value);
                            range.deleteContents();
                            range.insertNode(textNode);
                            range.setStartAfter(textNode);
                            range.collapse(true);
                            selection?.removeAllRanges();
                            selection?.addRange(range);
                        }
                        target.dispatchEvent(createInputEvent("input", value));
                        // #endregion
                    }
                    target.dispatchEvent(new Event("change", { bubbles: true, cancelable: true }));
                    await sleep(80);
                    // #endregion
                    return true;
                } else {
                    clearPromptTarget(target);
                    for (const ch of Array.from(value)) {
                        target.dispatchEvent(createInputEvent("beforeinput", ch));
                        if (target.tagName === "TEXTAREA" || target.tagName === "INPUT") {
                            target.value += ch;
                        } else if (target.getAttribute?.("contenteditable") === "true") {
                            if (document.execCommand) {
                                const inserted = document.execCommand("insertText", false, ch);
                                if (!inserted) {
                                    target.textContent = `${target.textContent || ""}${ch}`;
                                    placeCaretAtEnd(target);
                                }
                            } else {
                                target.textContent = `${target.textContent || ""}${ch}`;
                                placeCaretAtEnd(target);
                            }
                        }
                        target.dispatchEvent(createInputEvent("input", ch));
                        await sleep(20);
                    }
                }
                // #endregion
                return true;
            };
            const findAgentButtonNode = () => {
                const candidates = Array.from(
                    document.querySelectorAll("button, [role='button'], [aria-pressed], [aria-selected]")
                )
                    .filter((node) => visible(node))
                    .map((node) => {
                        const rect = node.getBoundingClientRect();
                        const text = textOf(node);
                        const ariaLabel = String(node.getAttribute?.("aria-label") || "");
                        const raw = `${text} ${ariaLabel}`.toLowerCase();
                        if (!raw.includes("智能体") && !raw.includes("agent")) return null;
                        if (rect.bottom < window.innerHeight * 0.72) return null;
                        return {
                            node,
                            text,
                            ariaLabel,
                            className: String(node.className || ""),
                            active:
                                String(node.getAttribute?.("aria-pressed") || "").toLowerCase() === "true" ||
                                String(node.getAttribute?.("aria-selected") || "").toLowerCase() === "true" ||
                                String(node.getAttribute?.("aria-expanded") || "").toLowerCase() === "true" ||
                                /\b(active|selected|checked|on)\b/i.test(String(node.className || "")),
                        };
                    })
                    .filter(Boolean);
                return candidates.find((item) => item.active)?.node || null;
            };
            const clickFirstButton = (predicates, label) => {
                for (const button of Array.from(document.querySelectorAll("button, [role='button']"))) {
                    if (!visible(button)) continue;
                    const text = textOf(button);
                    if (!text) continue;
                    if (predicates.some((predicate) => predicate(text))) {
                        try {
                            button.dispatchEvent(new MouseEvent("click", { bubbles: true, cancelable: true }));
                        } catch (_error) {
                            // ignore synthetic click dispatch failure
                        }
                        if (typeof button.click === "function") {
                            button.click();
                        }
                        return `${label}:${text.slice(0, 80)}`;
                    }
                }
                return "";
            };
            const isClickableLike = (node) => {
                if (!node || !visible(node)) return false;
                if (node.tagName === "BUTTON") return true;
                if (node.getAttribute?.("role") === "button") return true;
                if (node.getAttribute?.("role") === "tab") return true;
                if (Number(node.tabIndex) >= 0) return true;
                const className = String(node.className || "");
                const style = window.getComputedStyle(node);
                return (
                    style.cursor === "pointer" ||
                    /\b(cursor-pointer|clickable|chip|segmented|toggle)\b/i.test(className)
                );
            };
            const targetGenerationModeOnPage = String(requestedGenerationMode || "video").trim().toLowerCase() === "image"
                ? "image"
                : "video";
            const desiredOutputsPerPromptLabel = (() => {
                const parsed = Number.parseInt(String(requestedOutputsPerPrompt || "").trim(), 10);
                return Number.isFinite(parsed) && parsed > 0 ? `x${Math.max(1, Math.min(4, parsed))}` : "";
            })();
            const desiredVideoSettings = {
                modelDisplayName: String(requestedModelDisplayName || "").trim(),
                durationSeconds: String(requestedDurationSeconds || "").trim(),
                aspectRatioLabel: String(requestedAspectRatioLabel || "").trim(),
                outputsPerPromptLabel: desiredOutputsPerPromptLabel,
            };
            const hasDesiredVideoSettings = () =>
                !!(
                    desiredVideoSettings.modelDisplayName ||
                    desiredVideoSettings.durationSeconds ||
                    desiredVideoSettings.aspectRatioLabel ||
                    desiredVideoSettings.outputsPerPromptLabel
                );
            const collectVideoTabCandidates = () => {
                const selectors = ["[role='tab']", "button", "[role='button']"].join(", ");
                const promptTarget = findPromptTarget();
                const promptRect = promptTarget?.getBoundingClientRect?.() || null;
                return Array.from(document.querySelectorAll(selectors))
                    .filter((node) => visible(node))
                    .map((node) => {
                        const text = textOf(node);
                        const compact = text.replace(/\s+/g, "").toLowerCase();
                        const rect = node.getBoundingClientRect();
                        const inNav = !!node.closest?.("nav, aside, [role='navigation']");
                        const inDialog = !!node.closest?.("[role='dialog'], [aria-modal='true']");
                        const parentText = textOf(node.parentElement || null);
                        const rawAria = `${String(node.getAttribute?.("aria-label") || "")} ${String(node.getAttribute?.("aria-controls") || "")}`.toLowerCase();
                        const looksLikeVideoToggle =
                            text === "视频" ||
                            text.includes("视频 ·") ||
                            text.includes("观看视频") ||
                            text.startsWith("videocam") ||
                            text.includes("play_circle 视频") ||
                            compact.includes("play_circle视频");
                        if (!looksLikeVideoToggle) return null;
                        if (inNav) return null;
                        if (inDialog) return null;
                        if (compact.includes("查看设置")) return null;
                        if (rawAria.includes("settings")) return null;
                        const distanceScore = promptRect
                            ? Math.abs(rect.bottom - promptRect.top) + Math.abs(rect.left - promptRect.left)
                            : 999999;
                        const roleScore = node.getAttribute?.("role") === "tab" ? 0 : 1;
                        const selectedScore =
                            String(node.getAttribute?.("aria-selected") || "").toLowerCase() === "true" ? 2 : 0;
                        const parentScore =
                            parentText.includes("图片") || parentText.includes("视频") || parentText.includes("Veo")
                                ? 0
                                : 3;
                        return {
                            node,
                            text,
                            distanceScore,
                            roleScore,
                            selectedScore,
                            parentScore,
                        };
                    })
                    .filter(Boolean)
                    .sort((a, b) => {
                        return (
                            a.roleScore - b.roleScore ||
                            a.selectedScore - b.selectedScore ||
                            a.parentScore - b.parentScore ||
                            a.distanceScore - b.distanceScore
                        );
                    });
            };
            const collectImageTabCandidates = () => {
                const selectors = ["[role='tab']", "button", "[role='button']"].join(", ");
                const promptTarget = findPromptTarget();
                const promptRect = promptTarget?.getBoundingClientRect?.() || null;
                return Array.from(document.querySelectorAll(selectors))
                    .filter((node) => visible(node))
                    .map((node) => {
                        const text = textOf(node);
                        const compact = text.replace(/\s+/g, "").toLowerCase();
                        const rect = node.getBoundingClientRect();
                        const inNav = !!node.closest?.("nav, aside, [role='navigation']");
                        const inDialog = !!node.closest?.("[role='dialog'], [aria-modal='true']");
                        const parentText = textOf(node.parentElement || null);
                        const rawAria = `${String(node.getAttribute?.("aria-label") || "")} ${String(node.getAttribute?.("aria-controls") || "")}`.toLowerCase();
                        const looksLikeImageToggle =
                            text === "图片" ||
                            text.includes("图片 ·") ||
                            compact === "图片" ||
                            compact.includes("image图片");
                        if (!looksLikeImageToggle) return null;
                        if (inNav) return null;
                        if (inDialog) return null;
                        if (compact.includes("查看设置")) return null;
                        if (rawAria.includes("settings")) return null;
                        const distanceScore = promptRect
                            ? Math.abs(rect.bottom - promptRect.top) + Math.abs(rect.left - promptRect.left)
                            : 999999;
                        const roleScore = node.getAttribute?.("role") === "tab" ? 0 : 1;
                        const selectedScore =
                            String(node.getAttribute?.("aria-selected") || "").toLowerCase() === "true" ? 2 : 0;
                        const parentScore =
                            parentText.includes("图片") || parentText.includes("视频") || parentText.includes("Veo")
                                ? 0
                                : 3;
                        return {
                            node,
                            text,
                            distanceScore,
                            roleScore,
                            selectedScore,
                            parentScore,
                        };
                    })
                    .filter(Boolean)
                    .sort((a, b) => {
                        return (
                            a.roleScore - b.roleScore ||
                            a.selectedScore - b.selectedScore ||
                            a.parentScore - b.parentScore ||
                            a.distanceScore - b.distanceScore
                        );
                    });
            };
            const clickVideoTab = () => {
                const candidates = collectVideoTabCandidates();
                for (const item of candidates) {
                    clickReal(item.node);
                    return `click:video-tab:${item.text.slice(0, 80)}`;
                }
                return "";
            };
            const clickImageTab = () => {
                const candidates = collectImageTabCandidates();
                for (const item of candidates) {
                    clickReal(item.node);
                    return `click:image-tab:${item.text.slice(0, 80)}`;
                }
                return "";
            };
            const findSubmitAdjacentPopupButton = () => {
                const submitButton = getSubmitButton();
                const submitRect = submitButton?.getBoundingClientRect?.() || null;
                const candidates = Array.from(
                    document.querySelectorAll("button, [role='button'], [role='tab'], input[type='button'], input[type='submit']")
                )
                    .filter((node) => visible(node))
                    .map((node) => {
                        const kind = classifyButtonKind(node);
                        if (kind !== "popup") return null;
                        const text = textOf(node);
                        const icon = getButtonIconText(node);
                        const raw = `${text} ${icon} ${String(node.getAttribute?.("aria-label") || "")}`.toLowerCase();
                        if (raw.includes("查看设置") || raw.includes("settings")) return null;
                        if (raw.includes("更多选项") || raw.includes("排序和过滤") || raw.includes("添加媒体")) return null;
                        const rect = node.getBoundingClientRect();
                        const distanceScore = submitRect
                            ? Math.abs(rect.right - submitRect.left) + Math.abs(rect.top - submitRect.top)
                            : 999999;
                        const bottomLaneScore = rect.bottom > window.innerHeight * 0.7 ? 0 : 5;
                        const modeHintScore =
                            raw.includes("crop_16_9") ||
                            raw.includes("nanobanana") ||
                            raw.includes("veo") ||
                            raw.includes("图片") ||
                            raw.includes("视频")
                                ? 0
                                : 3;
                        return { node, text, distanceScore, bottomLaneScore, modeHintScore };
                    })
                    .filter(Boolean)
                    .sort((a, b) => {
                        return (
                            a.bottomLaneScore - b.bottomLaneScore ||
                            a.modeHintScore - b.modeHintScore ||
                            a.distanceScore - b.distanceScore
                        );
                    });
                // #endregion
                return candidates[0] || null;
            };
            const clickVideoSettingsChip = () => {
                const adjacentPopup = findSubmitAdjacentPopupButton();
                if (adjacentPopup) {
                    clickReal(adjacentPopup.node);
                    return `click:settings-chip:${adjacentPopup.text.slice(0, 80)}`;
                }
                for (const node of Array.from(document.querySelectorAll("button, [role='button'], [role='tab']"))) {
                    if (!isClickableLike(node)) continue;
                    const text = textOf(node);
                    if (!text) continue;
                    const compact = text.replace(/\s+/g, "").toLowerCase();
                    if (
                        compact.includes("veo3.1") ||
                        compact.includes("crop_16_9") ||
                        compact.includes("nanobanana")
                    ) {
                        clickReal(node);
                        return `click:settings-chip:${text.slice(0, 80)}`;
                    }
                }
                return "";
            };
            const collectSettingsOverlayRoots = () =>
                Array.from(
                    document.querySelectorAll(
                        [
                            "[role='dialog']",
                            "[aria-modal='true']",
                            "[role='listbox']",
                            "[role='menu']",
                            "[data-radix-popper-content-wrapper]",
                            "[data-state='open']",
                            "[data-headlessui-state='open']",
                            "[data-open='true']",
                        ].join(", ")
                    )
                )
                    .filter((node) => visible(node))
                    .sort((left, right) => {
                        const leftRect = left.getBoundingClientRect();
                        const rightRect = right.getBoundingClientRect();
                        return (rightRect.width * rightRect.height) - (leftRect.width * leftRect.height);
                    });
            const resolveSettingCandidateNode = (node) => {
                const directMatch = node?.closest?.(
                    [
                        "button",
                        "[role='button']",
                        "[role='tab']",
                        "[role='option']",
                        "[role='menuitemradio']",
                        "[role='menuitemcheckbox']",
                        "[aria-selected]",
                        "[tabindex]",
                        "label",
                    ].join(", ")
                );
                if (visible(directMatch)) {
                    return directMatch;
                }
                return visible(node) ? node : null;
            };
            const collectSettingOptionCandidates = (overlayOnly = true) => {
                const selectors = [
                    "button",
                    "[role='button']",
                    "[role='tab']",
                    "[role='option']",
                    "[role='menuitemradio']",
                    "[role='menuitemcheckbox']",
                    "[aria-selected]",
                    "[tabindex]",
                    "label",
                    "div",
                    "span",
                ].join(", ");
                const roots = overlayOnly ? collectSettingsOverlayRoots() : [];
                const scopes = roots.length ? roots : [document.body];
                const seen = new Set();
                return scopes
                    .flatMap((scope, scopeIndex) =>
                        Array.from(scope.querySelectorAll(selectors))
                            .filter((node) => visible(node))
                            .map((node) => {
                                const candidateNode = resolveSettingCandidateNode(node);
                                if (!candidateNode || seen.has(candidateNode)) return null;
                                seen.add(candidateNode);
                                const text = textOf(candidateNode) || textOf(node);
                                const normalized = normalizeUiText(text);
                                if (!normalized) return null;
                                if (
                                    normalized.includes("查看设置") ||
                                    normalized.includes("settings") ||
                                    normalized.includes("更多选项") ||
                                    normalized.includes("排序和过滤") ||
                                    normalized.includes("添加媒体") ||
                                    normalized.includes("arrow_forward") ||
                                    normalized === "创建"
                                ) {
                                    return null;
                                }
                                const clickable =
                                    roots.length ||
                                    isClickableLike(candidateNode) ||
                                    ["option", "menuitemradio", "menuitemcheckbox"].includes(String(candidateNode.getAttribute?.("role") || ""));
                                if (!clickable) return null;
                                return {
                                    node: candidateNode,
                                    text,
                                    normalized,
                                    scopeIndex,
                                    inOverlay: roots.length ? !!candidateNode.closest("[role='dialog'], [aria-modal='true'], [role='listbox'], [role='menu'], [data-radix-popper-content-wrapper], [data-state='open'], [data-headlessui-state='open'], [data-open='true']") : false,
                                };
                            })
                            .filter(Boolean)
                    )
                    .sort((left, right) => {
                        if (left.scopeIndex !== right.scopeIndex) {
                            return left.scopeIndex - right.scopeIndex;
                        }
                        if (left.inOverlay !== right.inOverlay) {
                            return left.inOverlay ? -1 : 1;
                        }
                        return left.text.length - right.text.length;
                    });
            };
            const computeAliasScore = (normalizedText, aliases) => {
                let score = Number.POSITIVE_INFINITY;
                for (const alias of aliases) {
                    const normalizedAlias = normalizeUiText(alias);
                    if (!normalizedAlias) continue;
                    if (normalizedText === normalizedAlias) return 0;
                    if (normalizedText.startsWith(normalizedAlias) || normalizedText.endsWith(normalizedAlias)) {
                        score = Math.min(score, 1);
                    } else if (normalizedText.includes(normalizedAlias)) {
                        score = Math.min(score, 2);
                    }
                }
                return score;
            };
            const findSettingOptionCandidate = (aliases) => {
                const overlayCandidates = collectSettingOptionCandidates(true)
                    .map((item) => ({ ...item, matchScore: computeAliasScore(item.normalized, aliases) }))
                    .filter((item) => Number.isFinite(item.matchScore))
                    .sort((left, right) => left.matchScore - right.matchScore || left.text.length - right.text.length);
                if (overlayCandidates.length) {
                    return {
                        candidate: overlayCandidates[0],
                        scanned: overlayCandidates.slice(0, 8).map((item) => item.text),
                        source: "overlay",
                    };
                }
                const overlayRoots = collectSettingsOverlayRoots();
                if (overlayRoots.length) {
                    const textNodeMatches = [];
                    for (const root of overlayRoots) {
                        const walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT);
                        let currentNode = walker.nextNode();
                        while (currentNode) {
                            const rawText = String(currentNode.textContent || "").trim();
                            const normalizedText = normalizeUiText(rawText);
                            const matchScore = computeAliasScore(normalizedText, aliases);
                            if (rawText && Number.isFinite(matchScore)) {
                                const parentNode = resolveSettingCandidateNode(currentNode.parentElement || root);
                                if (parentNode) {
                                    textNodeMatches.push({
                                        node: parentNode,
                                        text: textOf(parentNode) || rawText,
                                        normalized: normalizeUiText(textOf(parentNode) || rawText),
                                        matchScore,
                                    });
                                }
                            }
                            currentNode = walker.nextNode();
                        }
                    }
                    textNodeMatches.sort((left, right) => left.matchScore - right.matchScore || left.text.length - right.text.length);
                    if (textNodeMatches.length) {
                        return {
                            candidate: textNodeMatches[0],
                            scanned: textNodeMatches.slice(0, 8).map((item) => item.text),
                            source: "overlay-text-node",
                        };
                    }
                }
                const documentCandidates = collectSettingOptionCandidates(false)
                    .map((item) => ({ ...item, matchScore: computeAliasScore(item.normalized, aliases) }))
                    .filter((item) => Number.isFinite(item.matchScore))
                    .sort((left, right) => left.matchScore - right.matchScore || left.text.length - right.text.length);
                return {
                    candidate: documentCandidates[0] || null,
                    scanned: documentCandidates.slice(0, 8).map((item) => item.text),
                    source: documentCandidates.length ? "document" : "",
                };
            };
            const getCurrentModelDisplayAliases = (promptState) => {
                const family = normalizeUiText(promptState?.selectedVideoModelFamily || "");
                if (family === "abra" || family.includes("abra") || family.includes("omni")) {
                    return ["Omni Flash", "OmniFlash", "arrow_drop_down"];
                }
                if (family.includes("veo")) {
                    return ["Veo", "arrow_drop_down"];
                }
                return ["arrow_drop_down"];
            };
            const openModelDropdownInsideOverlay = (promptState) => {
                const aliases = getCurrentModelDisplayAliases(promptState);
                const triggerMatch = findSettingOptionCandidate(aliases);
                const triggerCandidate = triggerMatch?.candidate || null;
                if (!triggerCandidate) {
                    return {
                        opened: false,
                        trigger_source: triggerMatch?.source || "",
                        scanned: triggerMatch?.scanned || [],
                    };
                }
                const click = clickReal(triggerCandidate.node);
                return {
                    opened: !!click?.clicked,
                    trigger_source: triggerMatch?.source || "",
                    trigger_text: triggerCandidate.text,
                    click,
                };
            };
            const clickOverlayTextNodeByAlias = (aliases) => {
                const overlayRoots = collectSettingsOverlayRoots();
                const matches = [];
                for (const root of overlayRoots) {
                    const walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT);
                    let currentNode = walker.nextNode();
                    while (currentNode) {
                        const rawText = String(currentNode.textContent || "").trim();
                        const normalizedText = normalizeUiText(rawText);
                        const matchScore = computeAliasScore(normalizedText, aliases);
                        if (rawText && Number.isFinite(matchScore)) {
                            const range = document.createRange();
                            range.selectNodeContents(currentNode);
                            const rect = range.getBoundingClientRect();
                            if (rect.width > 4 && rect.height > 4) {
                                matches.push({
                                    text: rawText,
                                    matchScore,
                                    rect,
                                });
                            }
                        }
                        currentNode = walker.nextNode();
                    }
                }
                matches.sort((left, right) => left.matchScore - right.matchScore || left.text.length - right.text.length);
                const match = matches[0];
                if (!match) return null;
                const click = clickAtPoint(match.rect.left + match.rect.width / 2, match.rect.top + match.rect.height / 2);
                return {
                    matched_text: match.text,
                    match_source: "overlay-text-range",
                    click,
                };
            };
            const isDesiredModelAlreadySelected = (promptState, desiredLabel) => {
                const family = normalizeUiText(promptState?.selectedVideoModelFamily || "");
                const desired = normalizeUiText(desiredLabel);
                if (!desired) return true;
                if (desired.includes("omniflash")) {
                    return family === "abra" || family.includes("abra") || family.includes("omni");
                }
                if (desired === "veo") {
                    return family.includes("veo");
                }
                return family.includes(desired);
            };
            const isDesiredDurationAlreadySelected = (promptState, desiredSeconds) =>
                !!desiredSeconds && String(promptState?.selectedVideoDuration || "").trim() === String(desiredSeconds || "").trim();
            const isDesiredAspectRatioAlreadySelected = (promptState, desiredLabel) => {
                const current = normalizeUiText(promptState?.aspectRatio || "");
                const desired = normalizeUiText(desiredLabel);
                if (!desired) return true;
                if (desired === "9:16") return current.includes("portrait");
                if (desired === "16:9") return current.includes("landscape");
                return current.includes(desired);
            };
            const isDesiredOutputsAlreadySelected = (promptState, desiredLabel) => {
                if (!desiredLabel) return true;
                const current = Number.parseInt(String(promptState?.outputsPerPrompt || "").trim(), 10);
                const desired = Number.parseInt(String(desiredLabel || "").replace(/^x/i, "").trim(), 10);
                return Number.isFinite(current) && Number.isFinite(desired) && current === desired;
            };
            const selectVideoSettingOption = async (settingKind, aliases, targetLabel, verifySelected) => {
                const beforeState = parsePromptState();
                if (!targetLabel) {
                    return {
                        kind: settingKind,
                        target: "",
                        skipped: "empty_target",
                        before_state: beforeState,
                        after_state: beforeState,
                    };
                }
                if (typeof verifySelected === "function" && verifySelected(beforeState)) {
                    return {
                        kind: settingKind,
                        target: targetLabel,
                        skipped: "already_selected",
                        before_state: beforeState,
                        after_state: beforeState,
                    };
                }
                // #endregion
                const overlaysBeforeOpen = collectSettingsOverlayRoots();
                const openAction = overlaysBeforeOpen.length ? "reuse:already-open" : clickVideoSettingsChip();
                if (!openAction) {
                    return {
                        kind: settingKind,
                        target: targetLabel,
                        error: "settings_chip_not_found",
                        before_state: beforeState,
                        after_state: parsePromptState(),
                    };
                }
                let overlayRoots = [];
                let optionMatch = null;
                for (let attempt = 0; attempt < 6; attempt += 1) {
                    await sleep(attempt === 0 ? (overlaysBeforeOpen.length ? 80 : 250) : 180);
                    overlayRoots = collectSettingsOverlayRoots();
                    optionMatch = findSettingOptionCandidate(aliases);
                    if (optionMatch?.candidate) {
                        break;
                    }
                }
                const resolvedCandidate = optionMatch?.candidate || null;
                // #endregion
                if (!resolvedCandidate && settingKind === "model") {
                    const nestedModelOpen = openModelDropdownInsideOverlay(beforeState);
                    if (nestedModelOpen?.opened) {
                        for (let attempt = 0; attempt < 6; attempt += 1) {
                            await sleep(attempt === 0 ? 220 : 180);
                            overlayRoots = collectSettingsOverlayRoots();
                            optionMatch = findSettingOptionCandidate(aliases);
                            if (optionMatch?.candidate) {
                                break;
                            }
                        }
                    }
                }
                const finalResolvedCandidate = optionMatch?.candidate || null;
                if (!finalResolvedCandidate) {
                    const textNodeClick = clickOverlayTextNodeByAlias(aliases);
                    if (textNodeClick?.click?.clicked) {
                        await sleep(300);
                        return {
                            kind: settingKind,
                            target: targetLabel,
                            open_action: openAction,
                            matched_text: textNodeClick.matched_text,
                            match_source: textNodeClick.match_source,
                            click: textNodeClick.click,
                            before_state: beforeState,
                            after_state: parsePromptState(),
                        };
                    }
                    return {
                        kind: settingKind,
                        target: targetLabel,
                        open_action: openAction,
                        error: "option_not_found",
                        scanned: optionMatch?.scanned || [],
                        before_state: beforeState,
                        after_state: parsePromptState(),
                    };
                }
                const clickInfo = clickReal(finalResolvedCandidate.node);
                await sleep(300);
                return {
                    kind: settingKind,
                    target: targetLabel,
                    open_action: openAction,
                    matched_text: finalResolvedCandidate.text,
                    match_source: optionMatch?.source || "",
                    click: clickInfo,
                    before_state: beforeState,
                    after_state: parsePromptState(),
                };
            };
            const applyDesiredVideoSettings = async () => {
                const beforeState = parsePromptState();
                const actions = [];
                if (!hasDesiredVideoSettings()) {
                    return {
                        requested: desiredVideoSettings,
                        before_state: beforeState,
                        after_state: beforeState,
                        actions,
                    };
                }
                actions.push(
                    await selectVideoSettingOption(
                        "model",
                        desiredVideoSettings.modelDisplayName
                            ? [
                                  desiredVideoSettings.modelDisplayName,
                                  desiredVideoSettings.modelDisplayName === "Omni Flash" ? "abra" : "",
                                  desiredVideoSettings.modelDisplayName === "Veo" ? "veo" : "",
                              ].filter(Boolean)
                            : [],
                        desiredVideoSettings.modelDisplayName,
                        (state) => isDesiredModelAlreadySelected(state, desiredVideoSettings.modelDisplayName)
                    )
                );
                actions.push(
                    await selectVideoSettingOption(
                        "duration",
                        desiredVideoSettings.durationSeconds
                            ? [
                                  `${desiredVideoSettings.durationSeconds}s`,
                                  `视频${desiredVideoSettings.durationSeconds}s`,
                                  desiredVideoSettings.durationSeconds,
                              ]
                            : [],
                        desiredVideoSettings.durationSeconds ? `${desiredVideoSettings.durationSeconds}s` : "",
                        (state) => isDesiredDurationAlreadySelected(state, desiredVideoSettings.durationSeconds)
                    )
                );
                actions.push(
                    await selectVideoSettingOption(
                        "aspect_ratio",
                        desiredVideoSettings.aspectRatioLabel
                            ? [
                                  desiredVideoSettings.aspectRatioLabel,
                                  desiredVideoSettings.aspectRatioLabel === "9:16" ? "portrait" : "",
                                  desiredVideoSettings.aspectRatioLabel === "16:9" ? "landscape" : "",
                              ].filter(Boolean)
                            : [],
                        desiredVideoSettings.aspectRatioLabel,
                        (state) => isDesiredAspectRatioAlreadySelected(state, desiredVideoSettings.aspectRatioLabel)
                    )
                );
                actions.push(
                    await selectVideoSettingOption(
                        "outputs_per_prompt",
                        desiredVideoSettings.outputsPerPromptLabel
                            ? [
                                  desiredVideoSettings.outputsPerPromptLabel,
                                  desiredVideoSettings.outputsPerPromptLabel.replace(/^x/i, ""),
                                  `${desiredVideoSettings.outputsPerPromptLabel.replace(/^x/i, "")}个`,
                              ]
                            : [],
                        desiredVideoSettings.outputsPerPromptLabel,
                        (state) => isDesiredOutputsAlreadySelected(state, desiredVideoSettings.outputsPerPromptLabel)
                    )
                );
                const afterState = parsePromptState();
                return {
                    requested: desiredVideoSettings,
                    before_state: beforeState,
                    after_state: afterState,
                    actions,
                };
            };
            const clickCreateEntryButton = () => {
                const promptTarget = findPromptTarget();
                const promptRect = promptTarget?.getBoundingClientRect?.() || null;
                const candidates = Array.from(
                    document.querySelectorAll("button, [role='button'], [role='tab'], input[type='button'], input[type='submit']")
                )
                    .filter((node) => visible(node))
                    .map((node) => {
                        const text = textOf(node);
                        const ariaLabel = String(node.getAttribute?.("aria-label") || "");
                        const icon = getButtonIconText(node);
                        const raw = `${text} ${ariaLabel} ${icon}`.toLowerCase();
                        if (!raw) return null;
                        if (classifyButtonKind(node) === "submit") return null;
                        if (raw.includes("查看设置") || raw.includes("settings")) return null;
                        if (raw.includes("智能体") || raw.includes("agent")) return null;
                        const rect = node.getBoundingClientRect();
                        const inNav = !!node.closest?.("nav, aside, [role='navigation']");
                        const inDialog = !!node.closest?.("[role='dialog'], [aria-modal='true']");
                        if (inNav || inDialog) return null;
                        const looksLikeCreateEntry =
                            text.includes("开始创建") ||
                            text.includes("创建") ||
                            raw.includes("start create") ||
                            raw.includes("start creating") ||
                            raw.includes("drag") ||
                            raw.includes("拖放媒体");
                        if (!looksLikeCreateEntry) return null;
                        const primaryScore = text.includes("开始创建") ? 0 : text === "创建" ? 1 : 2;
                        const centerScore = Math.abs(rect.left + rect.width / 2 - window.innerWidth / 2);
                        const distanceScore = promptRect
                            ? Math.abs(rect.top - promptRect.top) + Math.abs(rect.left - promptRect.left)
                            : centerScore + Math.abs(rect.top - window.innerHeight * 0.62);
                        return {
                            node,
                            text,
                            primaryScore,
                            centerScore,
                            distanceScore,
                        };
                    })
                    .filter(Boolean)
                    .sort((a, b) => {
                        return (
                            a.primaryScore - b.primaryScore ||
                            a.centerScore - b.centerScore ||
                            a.distanceScore - b.distanceScore
                        );
                    });
                for (const item of candidates) {
                    clickReal(item.node);
                    return `click:create-entry:${item.text.slice(0, 80)}`;
                }
                return "";
            };
            const openModeMenuAndSelectTarget = async (requestedMode) => {
                const menuAction = clickVideoSettingsChip();
                if (!menuAction) return "";
                await sleep(350);
                const targetMode = String(requestedMode || "video").trim().toLowerCase() === "image" ? "image" : "video";
                const selectors = ["[role='tab']", "button", "[role='button']"].join(", ");
                const dialogs = Array.from(document.querySelectorAll("[role='dialog'], [aria-modal='true']")).filter((node) => visible(node));
                // #endregion
                for (const node of Array.from(document.querySelectorAll(selectors))) {
                    if (!visible(node)) continue;
                    const text = textOf(node);
                    const ariaControls = String(node.getAttribute?.("aria-controls") || "").toUpperCase();
                    const compact = text.replace(/\s+/g, "").toLowerCase();
                    const matchesTarget =
                        targetMode === "image"
                            ? (
                                  ariaControls.includes("IMAGE") ||
                                  text === "图片" ||
                                  text.includes("image 图片") ||
                                  compact.includes("image图片")
                              )
                            : (
                                  ariaControls.includes("VIDEO") ||
                                  text === "视频" ||
                                  text.includes("play_circle 视频") ||
                                  compact.includes("play_circle视频")
                              );
                    if (matchesTarget) {
                        clickReal(node);
                        await sleep(300);
                        return `click:model-menu-${targetMode}-tab:${text.slice(0, 80)}`;
                    }
                }
                return menuAction;
            };
            const ensureVideoComposerSurface = async () => {
                const initialPromptTarget = findPromptTarget();
                const initialState = parsePromptState();
                const bodyText = String(document.body?.innerText || "").replace(/\s+/g, " ").trim();
                const videoCandidatesBefore = collectVideoTabCandidates().map((item) => item.text).slice(0, 8);
                const imageCandidatesBefore = collectImageTabCandidates().map((item) => item.text).slice(0, 8);
                let action = "";
                let secondaryAction = "";
                const looksLikeMediaLibrary =
                    bodyText.includes("所有媒体内容") ||
                    bodyText.includes("开始创建或拖放媒体") ||
                    bodyText.includes("拖放媒体");
                const hasComposerSurface = !!initialPromptTarget;
                if (!hasComposerSurface && (looksLikeMediaLibrary || (!videoCandidatesBefore.length && (!initialState.promptMode || initialState.promptMode === "IMAGE")))) {
                    action = clickCreateEntryButton();
                    if (action) {
                        await sleep(400);
                    }
                }
                const afterCreateState = parsePromptState();
                const missingTargetEntry =
                    targetGenerationModeOnPage === "video"
                        ? (!collectVideoTabCandidates().length && (!afterCreateState.promptMode || afterCreateState.promptMode === "IMAGE"))
                        : (!collectImageTabCandidates().length && !afterCreateState.promptMode);
                if (missingTargetEntry) {
                    const popupAction = await openModeMenuAndSelectTarget(targetGenerationModeOnPage);
                    if (popupAction) {
                        secondaryAction = popupAction;
                        await sleep(300);
                    }
                }
                if (missingTargetEntry && !secondaryAction && !hasComposerSurface) {
                    const dialogAction = await openCreateDialogAndSelectMode(targetGenerationModeOnPage);
                    if (dialogAction) {
                        secondaryAction = dialogAction;
                        await sleep(400);
                    }
                }
                return {
                    before: {
                        prompt_state: initialState,
                        prompt_target: summarizeElement(initialPromptTarget),
                        target_generation_mode: targetGenerationModeOnPage,
                        video_tab_candidates: videoCandidatesBefore,
                        image_tab_candidates: imageCandidatesBefore,
                    },
                    after: {
                        prompt_state: parsePromptState(),
                        prompt_target: summarizeElement(findPromptTarget()),
                        target_generation_mode: targetGenerationModeOnPage,
                        video_tab_candidates: collectVideoTabCandidates().map((item) => item.text).slice(0, 8),
                        image_tab_candidates: collectImageTabCandidates().map((item) => item.text).slice(0, 8),
                    },
                    action,
                    secondary_action: secondaryAction,
                    media_library_hint: looksLikeMediaLibrary,
                    has_composer_surface: hasComposerSurface,
                };
            };
            const clickVideoSubmode = (preferredText) => {
                const compactPreferred = String(preferredText || "").replace(/\s+/g, "").trim();
                const exactTabCandidates = Array.from(document.querySelectorAll("[role='tab']")).filter((node) => {
                    if (!visible(node)) return false;
                    const compactText = textOf(node).replace(/\s+/g, "").trim();
                    if (!compactText || !compactText.includes(compactPreferred)) return false;
                    if (compactPreferred === "帧" && compactText.includes("素材")) return false;
                    if (compactPreferred === "素材" && compactText.includes("帧")) return false;
                    return true;
                });
                const fallbackCandidates = Array.from(
                    document.querySelectorAll(
                        "[role='button'], button, [aria-selected], [data-state], [data-orientation], [aria-controls], [tabindex]"
                    )
                ).filter((node) => {
                    if (!visible(node)) return false;
                    const compactText = textOf(node).replace(/\s+/g, "").trim();
                    return !!compactText && compactText.includes(compactPreferred);
                });
                for (const node of [...exactTabCandidates, ...fallbackCandidates]) {
                    try {
                        node.dispatchEvent(new MouseEvent("click", { bubbles: true, cancelable: true }));
                    } catch (_error) {
                        // ignore dispatch failure
                    }
                    if (typeof node.click === "function") {
                        node.click();
                    }
                    return `click:submode:${textOf(node).slice(0, 80)}`;
                }
                return "";
            };
            const openCreateDialogAndSelectVideo = async (requestedMode) => {
                const addDialogButton = Array.from(
                    document.querySelectorAll("button, [role='button'], input[type='button'], input[type='submit']")
                )
                    .filter((node) => visible(node))
                    .find((node) => classifyButtonKind(node) === "add_dialog");
                if (!addDialogButton) {
                    return "";
                }
                const addClick = clickReal(addDialogButton);
                await sleep(300);
                const dialogs = Array.from(document.querySelectorAll("[role='dialog'], [aria-modal='true']")).filter((node) =>
                    visible(node)
                );
                if (!dialogs.length) {
                    return addClick.clicked ? "click:add-dialog" : "";
                }
                const combinedDialogText = dialogs
                    .map((node) => textOf(node))
                    .join(" | ");
                const looksLikeMediaLibrary =
                    combinedDialogText.includes("上传媒体") ||
                    combinedDialogText.includes("最近") ||
                    (combinedDialogText.includes("全部") &&
                        combinedDialogText.includes("图片") &&
                        combinedDialogText.includes("视频") &&
                        combinedDialogText.includes("语音"));
                if (looksLikeMediaLibrary) {
                    try {
                        document.dispatchEvent(
                            new KeyboardEvent("keydown", { key: "Escape", bubbles: true, cancelable: true })
                        );
                    } catch (_error) {
                        // ignore dialog close failure
                    }
                    await sleep(150);
                    if (document.body) {
                        document.body.click();
                    }
                    return "";
                }
                for (const dialog of dialogs) {
                    const targetMode = String(requestedMode || "video").trim().toLowerCase() === "image" ? "image" : "video";
                    const desiredOption = Array.from(dialog.querySelectorAll("button, [role='button'], *"))
                        .filter((node) => visible(node))
                        .find((node) => {
                            const text = textOf(node);
                            if (targetMode === "image") {
                                return (
                                    text === "图片" ||
                                    text.includes(" image 图片") ||
                                    text.startsWith("image 图片") ||
                                    text.includes("图片 ")
                                );
                            }
                            return (
                                text === "视频" ||
                                text.includes(" videocam 视频") ||
                                text.startsWith("videocam 视频") ||
                                text.includes("视频 ")
                            );
                        });
                    if (!desiredOption) continue;
                    clickReal(desiredOption);
                    await sleep(500);
                    return targetMode === "image" ? "click:add-dialog-image" : "click:add-dialog-video";
                }
                return addClick.clicked ? "click:add-dialog" : "";
            };
            const openCreateDialogAndSelectMode = async (requestedMode) =>
                openCreateDialogAndSelectVideo(requestedMode);
            const resetAgentMode = async () => {
                const readAgentButtons = () =>
                    Array.from(document.querySelectorAll("button, [role='button'], [aria-pressed], [aria-selected]"))
                        .filter((node) => visible(node))
                        .map((node) => {
                            const text = textOf(node);
                            const raw = `${text} ${String(node.getAttribute?.("aria-label") || "")}`.toLowerCase();
                            if (!raw.includes("智能体") && !raw.includes("agent")) return null;
                            return {
                                text: text.slice(0, 120),
                                aria_label: String(node.getAttribute?.("aria-label") || "").slice(0, 120),
                                aria_pressed: String(node.getAttribute?.("aria-pressed") || ""),
                                aria_selected: String(node.getAttribute?.("aria-selected") || ""),
                                aria_expanded: String(node.getAttribute?.("aria-expanded") || ""),
                                class_name: String(node.className || "").slice(0, 200),
                            };
                        })
                        .filter(Boolean)
                        .slice(0, 8);
                const isAgentButtonActive = (item) => {
                    if (!item || typeof item !== "object") return false;
                    const className = String(item.class_name || "").toLowerCase();
                    return (
                        String(item.aria_pressed || "").toLowerCase() === "true" ||
                        String(item.aria_selected || "").toLowerCase() === "true" ||
                        String(item.aria_expanded || "").toLowerCase() === "true" ||
                        /\b(active|selected|checked|on)\b/.test(className)
                    );
                };
                const resolveSurfaceKind = () => {
                    const anyPromptTarget = findAnyPromptTarget();
                    if (anyPromptTarget && isConversationLikeTarget(anyPromptTarget)) {
                        return "conversation_drawer";
                    }
                    const promptState = parsePromptState();
                    const agentButtons = readAgentButtons();
                    if (agentButtons.some((item) => isAgentButtonActive(item)) || !promptState.promptMode) {
                        return "agent_dialog";
                    }
                    return promptState.promptMode ? "generation" : "unknown";
                };
                const toggleAgentChip = (node) => {
                    if (!node) return false;
                    try {
                        node.dispatchEvent(new MouseEvent("click", { bubbles: true, cancelable: true }));
                    } catch (_error) {
                        // ignore synthetic click dispatch failure
                    }
                    if (typeof node.click === "function") {
                        node.click();
                    }
                    return true;
                };
                const findAgentInstructionChipNode = () =>
                    Array.from(document.querySelectorAll("button, [role='button'], [aria-pressed], [aria-selected]"))
                        .filter((node) => visible(node))
                        .find((node) => {
                            const rect = node.getBoundingClientRect();
                            const raw = `${textOf(node)} ${String(node.getAttribute?.("aria-label") || "")}`.toLowerCase();
                            if (rect.bottom < window.innerHeight * 0.6) return false;
                            return raw.includes("智能体指令") || raw.includes("agent instruction") || raw.includes("article_spark");
                        }) || null;
                const dismissAgentOverlay = () => {
                    try {
                        document.dispatchEvent(
                            new KeyboardEvent("keydown", { key: "Escape", bubbles: true, cancelable: true })
                        );
                    } catch (_error) {
                        // ignore escape dispatch failure
                    }
                    if (document.body) {
                        document.body.click();
                    }
                };
                const beforeButtons = readAgentButtons();
                const beforeSurface = resolveSurfaceKind();
                const transitions = [];
                let action = "";
                if (beforeSurface === "conversation_drawer") {
                    action = await closeConversationDrawer();
                    transitions.push({
                        attempt: 0,
                        action,
                        surface_after: resolveSurfaceKind(),
                    });
                }
                if (!action && resolveSurfaceKind() === "agent_dialog") {
                    const candidate = findAgentButtonNode();
                    if (candidate) {
                        const before = summarizeElement(candidate);
                        toggleAgentChip(candidate);
                        action = "agent_chip_toggled";
                        await sleep(150);
                        dismissAgentOverlay();
                        await sleep(250);
                        transitions.push({
                            attempt: 1,
                            action,
                            clicked: before,
                            buttons_after: readAgentButtons(),
                            surface_after: resolveSurfaceKind(),
                        });
                        if (resolveSurfaceKind() === "agent_dialog") {
                            clickReal(candidate);
                            await sleep(150);
                            dismissAgentOverlay();
                            await sleep(250);
                            transitions.push({
                                attempt: 2,
                                action: "agent_chip_real_click",
                                clicked: before,
                                buttons_after: readAgentButtons(),
                                surface_after: resolveSurfaceKind(),
                            });
                        }
                        if (resolveSurfaceKind() === "agent_dialog") {
                            const instructionChip = findAgentInstructionChipNode();
                            if (instructionChip) {
                                const instructionBefore = summarizeElement(instructionChip);
                                clickReal(instructionChip);
                                await sleep(150);
                                dismissAgentOverlay();
                                await sleep(250);
                                transitions.push({
                                    attempt: 3,
                                    action: "agent_instruction_chip_click",
                                    clicked: instructionBefore,
                                    buttons_after: readAgentButtons(),
                                    surface_after: resolveSurfaceKind(),
                                });
                            }
                        }
                    }
                }
                if (!action && resolveSurfaceKind() === "agent_dialog") {
                    await sleep(150);
                    dismissAgentOverlay();
                    transitions.push({
                        attempt: 4,
                        action: "agent_overlay_dismiss_only",
                        buttons_after: readAgentButtons(),
                        surface_after: resolveSurfaceKind(),
                    });
                }
                return {
                    before: beforeButtons,
                    after: readAgentButtons(),
                    before_surface: beforeSurface,
                    after_surface: resolveSurfaceKind(),
                    action,
                    transitions,
                };
            };
            const matchesReferenceText = (textValue, references) => {
                const compactText = String(textValue || "").replace(/\s+/g, "").trim().toLowerCase();
                if (!compactText) return false;
                for (const item of Array.isArray(references) ? references : []) {
                    const compactRef = String(item || "").replace(/\s+/g, "").trim().toLowerCase();
                    if (!compactRef || compactRef.length < 3) continue;
                    if (compactText.includes(compactRef) || compactRef.includes(compactText)) {
                        return true;
                    }
                }
                return false;
            };
            const collectReferencePickerRoots = () =>
                Array.from(
                    document.querySelectorAll(
                        [
                            "[role='dialog']",
                            "[aria-modal='true']",
                            "[role='menu']",
                            "[role='listbox']",
                            "[data-radix-popper-content-wrapper]",
                            "[data-state='open']",
                            "[data-headlessui-state='open']",
                            "[data-open='true']",
                        ].join(", ")
                    )
                )
                    .filter((node) => visible(node))
                    .filter((node, _index, all) => !all.some((other) => other !== node && other.contains(node)))
                    .sort((left, right) => {
                        const leftDialog = left.matches?.("[role='dialog'], [aria-modal='true']") ? 1 : 0;
                        const rightDialog = right.matches?.("[role='dialog'], [aria-modal='true']") ? 1 : 0;
                        if (leftDialog !== rightDialog) {
                            return rightDialog - leftDialog;
                        }
                        const leftRect = left.getBoundingClientRect?.();
                        const rightRect = right.getBoundingClientRect?.();
                        const leftArea = leftRect ? leftRect.width * leftRect.height : 0;
                        const rightArea = rightRect ? rightRect.width * rightRect.height : 0;
                        return rightArea - leftArea;
                    });
            const isReferencePickerRoot = (node, references) => {
                const text = textOf(node);
                if (!text) return false;
                const lower = text.toLowerCase();
                return (
                    text.includes("上传") ||
                    text.includes("添加媒体") ||
                    text.includes("选择媒体") ||
                    text.includes("我的媒体") ||
                    text.includes("项目") ||
                    lower.includes("upload") ||
                    lower.includes("media library") ||
                    matchesReferenceText(text, references)
                );
            };
            const collectReferencePickerSnapshot = (references) => {
                const overlays = collectReferencePickerRoots().slice(0, 8).map((node) => ({
                    text: textOf(node).slice(0, 260),
                    is_reference_picker: isReferencePickerRoot(node, references),
                    node: summarizeElement(node),
                }));
                const buttons = Array.from(document.querySelectorAll("button, [role='button'], [role='tab']"))
                    .filter((node) => visible(node))
                    .slice(0, 30)
                    .map((node) => ({
                        text: textOf(node).slice(0, 120),
                        role: String(node.getAttribute?.("role") || ""),
                        aria_controls: String(node.getAttribute?.("aria-controls") || ""),
                        aria_expanded: String(node.getAttribute?.("aria-expanded") || ""),
                        aria_haspopup: String(node.getAttribute?.("aria-haspopup") || ""),
                        node: summarizeElement(node),
                    }));
                const textNodes = [];
                for (const root of collectReferencePickerRoots().slice(0, 6)) {
                    const walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT);
                    let currentNode = walker.nextNode();
                    while (currentNode && textNodes.length < 40) {
                        const raw = String(currentNode.textContent || "").trim();
                        if (raw) {
                            textNodes.push(raw.slice(0, 140));
                        }
                        currentNode = walker.nextNode();
                    }
                }
                return { overlays, buttons, text_nodes: textNodes };
            };
            const collectReferenceSelectionState = (dialog) => {
                const selectedNodes = Array.from(dialog.querySelectorAll("[aria-selected='true'], [data-selected='true'], [data-state='selected'], [aria-checked='true']"))
                    .filter((node) => visible(node))
                    .slice(0, 10)
                    .map((node) => ({
                        text: textOf(node).slice(0, 180),
                        node: summarizeElement(node),
                    }));
                const addButtons = Array.from(dialog.querySelectorAll("button, [role='button'], [tabindex]"))
                    .filter((node) => visible(node))
                    .filter((node) => textOf(node).includes("添加到提示"))
                    .slice(0, 4)
                    .map((node) => ({
                        text: textOf(node).slice(0, 80),
                        disabled: !!node.disabled || String(node.getAttribute?.("aria-disabled") || "") === "true",
                        node: summarizeElement(node),
                    }));
                return {
                    selected_nodes: selectedNodes,
                    add_buttons: addButtons,
                };
            };
                const dialogAlreadyShowsReferenceDetail = (dialog, previewState, selectionState, activeReferences = []) => {
                    const dialogText = textOf(dialog);
                    const hasMatchedReferenceText = matchesReferenceText(dialogText, activeReferences);
                    const hasDetailSignals =
                        selectionState.add_buttons.length > 0 ||
                        previewState.time_nodes.length >= 2 ||
                        previewHasFrameStrip(previewState) ||
                        previewHasRenderableGeometry(previewState);
                    return hasMatchedReferenceText && hasDetailSignals;
                };
            const isReferenceCandidateAlreadySelected = (node, stopNode = null) => {
                let current = node;
                let guard = 0;
                while (current && guard < 6) {
                    if (stopNode && current === stopNode) {
                        break;
                    }
                    const className = String(current.className || "");
                    const dataState = String(current.getAttribute?.("data-state") || "").toLowerCase();
                    const dataSelected = String(current.getAttribute?.("data-selected") || "").toLowerCase();
                    const ariaSelected = String(current.getAttribute?.("aria-selected") || "").toLowerCase();
                    const ariaChecked = String(current.getAttribute?.("aria-checked") || "").toLowerCase();
                    const ariaCurrent = String(current.getAttribute?.("aria-current") || "").toLowerCase();
                    if (
                        ariaSelected === "true" ||
                        ariaChecked === "true" ||
                        ariaCurrent === "true" ||
                        dataSelected === "true" ||
                        dataState === "selected" ||
                        dataState === "active" ||
                        /\b(selected|active|current|checked)\b/i.test(className)
                    ) {
                        return true;
                    }
                    current = current.parentElement;
                    guard += 1;
                }
                return false;
            };
            const collectReferencePreviewState = (dialog) => {
                const summarizeRect = (rect) =>
                    rect
                        ? {
                            left: Math.round(rect.left),
                            top: Math.round(rect.top),
                            width: Math.round(rect.width),
                            height: Math.round(rect.height),
                        }
                        : null;
                const hasPositiveRect = (rect) => !!rect && rect.width > 0 && rect.height > 0;
                const getAncestorEntries = (node, stopNode = null, maxDepth = 8) => {
                    const entries = [];
                    let currentAncestor = node?.parentElement || null;
                    let guard = 0;
                    while (currentAncestor && guard < maxDepth) {
                        const rect = currentAncestor.getBoundingClientRect?.() || null;
                        const style = window.getComputedStyle?.(currentAncestor) || null;
                        entries.push({
                            element: currentAncestor,
                            tag: String(currentAncestor.tagName || "").toLowerCase(),
                            class_name: String(currentAncestor.className || "").slice(0, 120),
                            rect: summarizeRect(rect),
                            cursor: String(style?.cursor || ""),
                            inline_style: String(currentAncestor.getAttribute?.("style") || "").slice(0, 240),
                            node: summarizeElement(currentAncestor),
                        });
                        if (stopNode && currentAncestor === stopNode) {
                            break;
                        }
                        currentAncestor = currentAncestor.parentElement;
                        guard += 1;
                    }
                    return entries;
                };
                const resolveHandleRectFromSnapshot = (snapshot) => {
                    if (!snapshot) return null;
                    const parentRect = snapshot.parent_rect;
                    const rect = snapshot.rect;
                    const ancestorRects = Array.isArray(snapshot.ancestor_rects)
                        ? snapshot.ancestor_rects.map((entry) => entry.rect).filter(Boolean)
                        : [];
                    const explicitResolved = snapshot.resolved_handle_rect;
                    if (explicitResolved && explicitResolved.width >= 8 && explicitResolved.height >= 16) {
                        return explicitResolved;
                    }
                    if (parentRect && parentRect.width >= 8 && parentRect.width <= 36 && parentRect.height >= 16 && parentRect.height <= 72) {
                        return parentRect;
                    }
                    const ancestorRect = ancestorRects.find((candidate) =>
                        candidate &&
                        candidate.width >= 8 &&
                        candidate.width <= 48 &&
                        candidate.height >= 16 &&
                        candidate.height <= 96
                    );
                    if (ancestorRect) {
                        return ancestorRect;
                    }
                    if (rect && rect.width >= 8 && rect.height >= 8) {
                        return rect;
                    }
                    return null;
                };
                const timeNodes = [];
                const timeNodeEntries = [];
                try {
                    const walker = document.createTreeWalker(dialog, NodeFilter.SHOW_TEXT);
                    let currentNode = walker.nextNode();
                    while (currentNode && timeNodes.length < 12) {
                        const raw = String(currentNode.textContent || "").trim();
                        if (/^\d{2}:\d{2}:\d{2}$/.test(raw)) {
                            timeNodes.push(raw);
                            let rangeRect = null;
                            try {
                                const range = document.createRange();
                                range.selectNodeContents(currentNode);
                                rangeRect = range.getBoundingClientRect?.() || null;
                            } catch (error) {}
                            const hostNode = currentNode.parentElement || currentNode.parentNode;
                            const hostRect = hostNode?.getBoundingClientRect?.() || null;
                            timeNodeEntries.push({
                                text: raw,
                                rect: summarizeRect(rangeRect && rangeRect.width > 0 && rangeRect.height > 0 ? rangeRect : hostRect),
                                parent_rect: summarizeRect(hostRect),
                                node: summarizeElement(hostNode),
                            });
                        }
                        currentNode = walker.nextNode();
                    }
                } catch (error) {}
                const dialogRect = dialog.getBoundingClientRect?.() || null;
                const controls = Array.from(dialog.querySelectorAll("button, [role='button'], [role='slider'], input[type='range'], video, img"))
                    .filter((node) => visible(node))
                    .map((node) => {
                        const rect = node.getBoundingClientRect?.();
                        const text = textOf(node).slice(0, 100);
                        const ariaLabel = String(node.getAttribute?.("aria-label") || "").slice(0, 100);
                        const role = String(node.getAttribute?.("role") || "");
                        const tag = String(node.tagName || "").toLowerCase();
                        const ariaValueNow = String(node.getAttribute?.("aria-valuenow") || "");
                        const ariaValueMin = String(node.getAttribute?.("aria-valuemin") || "");
                        const ariaValueMax = String(node.getAttribute?.("aria-valuemax") || "");
                        return {
                            text,
                            aria_label: ariaLabel,
                            role,
                            tag,
                            aria_value_now: ariaValueNow,
                            aria_value_min: ariaValueMin,
                            aria_value_max: ariaValueMax,
                            rect: summarizeRect(rect),
                            node: summarizeElement(node),
                        };
                    })
                    .filter((item) => {
                        const hay = `${item.text} ${item.aria_label}`.toLowerCase();
                        return (
                            item.role === "slider" ||
                            item.tag === "video" ||
                            item.tag === "input" ||
                            item.tag === "img" ||
                            !!item.aria_value_now ||
                            hay.includes("播放") ||
                            hay.includes("静音") ||
                            hay.includes("添加到提示") ||
                            hay.includes("裁剪") ||
                            hay.includes("修剪") ||
                            hay.includes("crop") ||
                            hay.includes("trim")
                        );
                    })
                    .slice(0, 12);
                const handleCandidates = Array.from(dialog.querySelectorAll("div, span, button, [role='slider'], input, img"))
                    .filter((node) => visible(node))
                    .map((node) => {
                        const rect = node.getBoundingClientRect?.();
                        const style = window.getComputedStyle?.(node);
                        const text = textOf(node).slice(0, 80);
                        const ariaLabel = String(node.getAttribute?.("aria-label") || "").slice(0, 80);
                        const role = String(node.getAttribute?.("role") || "");
                        const tag = String(node.tagName || "").toLowerCase();
                        const className = String(node.className || "").slice(0, 120);
                        const hay = `${text} ${ariaLabel} ${className}`.toLowerCase();
                        return {
                            text,
                            aria_label: ariaLabel,
                            role,
                            tag,
                            class_name: className,
                            cursor: String(style?.cursor || ""),
                            background: String(style?.backgroundColor || ""),
                            rect: summarizeRect(rect),
                            node: summarizeElement(node),
                            hay,
                        };
                    })
                    .filter((item) => {
                        const rect = item.rect;
                        if (!rect) return false;
                        const cursor = item.cursor.toLowerCase();
                        return (
                            item.role === "slider" ||
                            item.tag === "input" ||
                            item.hay.includes("裁剪") ||
                            item.hay.includes("修剪") ||
                            item.hay.includes("crop") ||
                            item.hay.includes("trim") ||
                            cursor.includes("resize") ||
                            cursor.includes("grab") ||
                            cursor.includes("col-resize") ||
                            cursor.includes("ew-resize") ||
                            (rect.width >= 6 && rect.width <= 40 && rect.height >= 24 && rect.height <= 160) ||
                            (rect.height >= 6 && rect.height <= 48 && rect.width >= 120 && rect.width <= 420)
                        );
                    })
                    .slice(0, 20)
                    .map(({ hay, ...rest }) => rest);
                const timelineCandidates = Array.from(dialog.querySelectorAll("div, section, span"))
                    .filter((node) => visible(node))
                    .map((node) => {
                        const rect = node.getBoundingClientRect?.();
                        const style = window.getComputedStyle?.(node);
                        const text = textOf(node).slice(0, 80);
                        return {
                            text,
                            tag: String(node.tagName || "").toLowerCase(),
                            class_name: String(node.className || "").slice(0, 120),
                            child_count: Number(node.childElementCount || 0),
                            rect: summarizeRect(rect),
                            border_color: String(style?.borderColor || ""),
                            background: String(style?.backgroundColor || ""),
                            node: summarizeElement(node),
                        };
                    })
                    .filter((item) => item.rect && item.rect.width >= 140 && item.rect.height >= 20 && item.rect.height <= 120)
                    .slice(0, 16);
                const mediaNodeEntries = Array.from(dialog.querySelectorAll("video, img"))
                    .filter((node) => visible(node))
                    .map((node) => {
                        const rect = node.getBoundingClientRect?.();
                        return {
                            _node: node,
                            tag: String(node.tagName || "").toLowerCase(),
                            class_name: String(node.className || "").slice(0, 120),
                            area: rect ? Math.round(rect.width * rect.height) : 0,
                            rect: summarizeRect(rect),
                            node: summarizeElement(node),
                        };
                    })
                    .filter((item) => item.rect && item.area > 0)
                    .sort((left, right) => right.area - left.area);
                const primaryMediaEntry = mediaNodeEntries[0] || null;
                const primaryMediaNode = primaryMediaEntry?._node || null;
                const previewAncestorChain = [];
                if (primaryMediaNode) {
                    let currentNode = primaryMediaNode;
                    let guard = 0;
                    while (currentNode && dialog.contains(currentNode) && guard < 8) {
                        const rect = currentNode.getBoundingClientRect?.();
                        previewAncestorChain.push({
                            tag: String(currentNode.tagName || "").toLowerCase(),
                            class_name: String(currentNode.className || "").slice(0, 120),
                            child_count: Number(currentNode.childElementCount || 0),
                            rect: summarizeRect(rect),
                            node: summarizeElement(currentNode),
                        });
                        currentNode = currentNode.parentElement;
                        guard += 1;
                    }
                }
                const resolvedTimeRects = timeNodeEntries
                    .map((entry) => entry.rect || entry.parent_rect)
                    .filter((rect) => rect && rect.width > 0 && rect.height > 0);
                const stripSearchBounds = (() => {
                    if (primaryMediaEntry?.rect) {
                        return {
                            left: primaryMediaEntry.rect.left - 48,
                            right: primaryMediaEntry.rect.left + primaryMediaEntry.rect.width + 120,
                            top: primaryMediaEntry.rect.top + primaryMediaEntry.rect.height - 40,
                            bottom: primaryMediaEntry.rect.top + primaryMediaEntry.rect.height + 220,
                        };
                    }
                    if (resolvedTimeRects.length) {
                        const left = Math.min(...resolvedTimeRects.map((rect) => rect.left)) - 120;
                        const right = Math.max(...resolvedTimeRects.map((rect) => rect.left + rect.width)) + 120;
                        const top = Math.min(...resolvedTimeRects.map((rect) => rect.top)) - 72;
                        const bottom = Math.max(...resolvedTimeRects.map((rect) => rect.top + rect.height)) + 120;
                        return { left, right, top, bottom };
                    }
                    if (dialogRect) {
                        return {
                            left: dialogRect.left + 8,
                            right: dialogRect.left + dialogRect.width - 8,
                            top: dialogRect.top + Math.max(32, Math.round(dialogRect.height * 0.45)),
                            bottom: dialogRect.top + dialogRect.height - 8,
                        };
                    }
                    return null;
                })();
                const lowerStripCandidates = stripSearchBounds
                    ? Array.from(dialog.querySelectorAll("div, span, button, [role='button'], [role='slider'], input, video, img"))
                        .filter((node) => visible(node))
                        .map((node) => {
                            const rect = node.getBoundingClientRect?.();
                            const style = window.getComputedStyle?.(node);
                            const parentRect = node.parentElement?.getBoundingClientRect?.();
                            return {
                                text: textOf(node).slice(0, 80),
                                tag: String(node.tagName || "").toLowerCase(),
                                class_name: String(node.className || "").slice(0, 120),
                                cursor: String(style?.cursor || ""),
                                background: String(style?.backgroundColor || ""),
                                src: String(node.getAttribute?.("src") || "").slice(0, 240),
                                parent_tag: String(node.parentElement?.tagName || "").toLowerCase(),
                                parent_class_name: String(node.parentElement?.className || "").slice(0, 120),
                                parent_rect: summarizeRect(parentRect),
                                rect: summarizeRect(rect),
                                node: summarizeElement(node),
                            };
                        })
                        .filter((item) => {
                            const rect = item.rect;
                            if (!rect) return false;
                            return (
                                rect.left >= stripSearchBounds.left &&
                                rect.left <= stripSearchBounds.right &&
                                rect.top >= stripSearchBounds.top &&
                                rect.top <= stripSearchBounds.bottom &&
                                rect.width >= 8 &&
                                rect.height >= 8
                            );
                        })
                        .slice(0, 32)
                    : [];
                const trimHandleCandidates = Array.from(dialog.querySelectorAll("img[src*='trim-handle']"))
                    .map((node) => {
                        const rect = node.getBoundingClientRect?.();
                        const parentRect = node.parentElement?.getBoundingClientRect?.();
                        const ancestorRects = getAncestorEntries(node, dialog, 6).map(({ element, ...rest }) => rest);
                        const resolvedHandleRect = (() => {
                            const ancestorMatch = ancestorRects.find((entry) =>
                                entry.rect &&
                                entry.rect.width >= 8 &&
                                entry.rect.width <= 48 &&
                                entry.rect.height >= 16 &&
                                entry.rect.height <= 96
                            );
                            return ancestorMatch?.rect || null;
                        })();
                        return {
                            src: String(node.getAttribute?.("src") || "").slice(0, 240),
                            rect: summarizeRect(rect),
                            parent_rect: summarizeRect(parentRect),
                            resolved_handle_rect: resolvedHandleRect,
                            class_name: String(node.className || "").slice(0, 120),
                            parent_class_name: String(node.parentElement?.className || "").slice(0, 120),
                            ancestor_rects: ancestorRects,
                            node: summarizeElement(node),
                            parent_node: summarizeElement(node.parentElement),
                        };
                    })
                    .filter((item) => item.rect || item.parent_rect || (Array.isArray(item.ancestor_rects) && item.ancestor_rects.length))
                    .slice(0, 8);
                const localHandlesUsable = trimHandleCandidates.some((item) => hasPositiveRect(resolveHandleRectFromSnapshot(item)));
                const globalTrimHandleSnapshots = !localHandlesUsable
                    ? Array.from(document.querySelectorAll("img[src*='trim-handle']"))
                        .slice(0, 12)
                        .map((node) => {
                            const rect = node.getBoundingClientRect?.() || null;
                            const parentRect = node.parentElement?.getBoundingClientRect?.() || null;
                            const ancestorEntries = getAncestorEntries(node, null, 8);
                            const resolvedHandleRect = (() => {
                                const ancestorMatch = ancestorEntries.find((entry) =>
                                    entry.rect &&
                                    entry.rect.width >= 8 &&
                                    entry.rect.width <= 48 &&
                                    entry.rect.height >= 16 &&
                                    entry.rect.height <= 96
                                );
                                return ancestorMatch?.rect || null;
                            })();
                            return {
                                element: node,
                                src: String(node.getAttribute?.("src") || "").slice(0, 240),
                                rect: summarizeRect(rect),
                                parent_rect: summarizeRect(parentRect),
                                resolved_handle_rect: resolvedHandleRect,
                                ancestor_rects: ancestorEntries,
                                visible: visible(node),
                                inside_dialog: !!(dialog && dialog.contains?.(node)),
                                node: summarizeElement(node),
                                parent_node: summarizeElement(node.parentElement),
                            };
                        })
                    : [];
                const globalTrimHandleDebug = globalTrimHandleSnapshots.map(({ element, ancestor_rects, ...rest }) => ({
                    ...rest,
                    ancestor_rects: Array.isArray(ancestor_rects)
                        ? ancestor_rects.map(({ element: _element, ...entry }) => entry)
                        : [],
                }));
                const globalDialogDebug = Array.from(document.querySelectorAll("[role='dialog'], [aria-modal='true']"))
                    .slice(0, 8)
                    .map((node) => ({
                        rect: summarizeRect(node.getBoundingClientRect?.() || null),
                        visible: visible(node),
                        text: textOf(node).slice(0, 240),
                        node: summarizeElement(node),
                    }));
                const globalTrackRect = (() => {
                    if (!globalTrimHandleSnapshots.length) return null;
                    const leftItems = globalTrimHandleSnapshots.filter((item) => /trim-handle-left\.svg/i.test(String(item.src || "")));
                    const rightItems = globalTrimHandleSnapshots.filter((item) => /trim-handle-right\.svg/i.test(String(item.src || "")));
                    let best = null;
                    for (const leftItem of leftItems) {
                        for (const rightItem of rightItems) {
                            const sharedAncestors = [];
                            for (const leftAncestor of leftItem.ancestor_rects || []) {
                                const matched = (rightItem.ancestor_rects || []).find((rightAncestor) => rightAncestor.element === leftAncestor.element);
                                if (matched) {
                                    sharedAncestors.push(leftAncestor);
                                }
                            }
                            const trackCandidate = sharedAncestors
                                .filter((entry) => entry.rect && entry.rect.width >= 120 && entry.rect.height >= 12 && entry.rect.height <= 120)
                                .sort((a, b) => (b.rect.width * b.rect.height) - (a.rect.width * a.rect.height))[0];
                            if (!trackCandidate) continue;
                            const score = trackCandidate.rect.width * 1000 + trackCandidate.rect.height;
                            if (!best || score > best.score) {
                                best = { score, rect: trackCandidate.rect };
                            }
                        }
                    }
                    return best?.rect || null;
                })();
                const effectiveTrimHandleCandidates = localHandlesUsable
                    ? trimHandleCandidates
                    : globalTrimHandleSnapshots.map(({ element, ...rest }) => rest).slice(0, 8);
                return {
                    time_nodes: timeNodes,
                    time_node_entries: timeNodeEntries,
                    controls,
                    handle_candidates: handleCandidates,
                    timeline_candidates: timelineCandidates,
                    media_candidates: mediaNodeEntries.slice(0, 6).map(({ _node, ...rest }) => rest),
                    primary_media: primaryMediaEntry ? (({ _node, ...rest }) => rest)(primaryMediaEntry) : null,
                    preview_ancestor_chain: previewAncestorChain,
                    track_rect: globalTrackRect,
                    global_trim_handle_debug: globalTrimHandleDebug,
                    global_dialog_debug: globalDialogDebug,
                    strip_search_bounds: stripSearchBounds,
                    lower_strip_candidates: lowerStripCandidates,
                    trim_handle_candidates: effectiveTrimHandleCandidates,
                };
            };
            const collectPromptRegionState = () => {
                const promptTarget = findPromptTarget();
                const promptRect = promptTarget?.getBoundingClientRect?.() || null;
                const distanceToPrompt = (rect) => {
                    if (!promptRect || !rect) return Number.POSITIVE_INFINITY;
                    const promptCx = promptRect.left + (promptRect.width / 2);
                    const promptCy = promptRect.top + (promptRect.height / 2);
                    const cx = rect.left + (rect.width / 2);
                    const cy = rect.top + (rect.height / 2);
                    return Math.round(Math.hypot(cx - promptCx, cy - promptCy));
                };
                const nearby = Array.from(document.querySelectorAll("button, [role='button'], [role='option'], img, video, [data-testid], [tabindex], span, div"))
                    .filter((node) => visible(node))
                    .map((node) => {
                        const rect = node.getBoundingClientRect?.();
                        return {
                            text: textOf(node).slice(0, 180),
                            aria_label: String(node.getAttribute?.("aria-label") || "").slice(0, 120),
                            role: String(node.getAttribute?.("role") || ""),
                            distance: Number.isFinite(distanceToPrompt(rect)) ? distanceToPrompt(rect) : -1,
                            rect: rect
                                ? {
                                    left: Math.round(rect.left),
                                    top: Math.round(rect.top),
                                    width: Math.round(rect.width),
                                    height: Math.round(rect.height),
                                }
                                : null,
                            node: summarizeElement(node),
                        };
                    })
                    .filter((item) => item.distance >= 0 && item.distance <= 520)
                    .filter((item) => {
                        const hay = `${item.text} ${item.aria_label}`.toLowerCase();
                        return (
                            hay.includes("视频") ||
                            hay.includes("图片") ||
                            hay.includes("播放") ||
                            hay.includes("删除") ||
                            hay.includes("remove") ||
                            hay.includes("media") ||
                            hay.includes("prompt") ||
                            hay.includes("上传") ||
                            hay.includes("添加到提示")
                        );
                    })
                    .slice(0, 30);
                return {
                    prompt_target: summarizeElement(promptTarget),
                    prompt_rect: promptRect
                        ? {
                            left: Math.round(promptRect.left),
                            top: Math.round(promptRect.top),
                            width: Math.round(promptRect.width),
                            height: Math.round(promptRect.height),
                        }
                        : null,
                    nearby,
                };
            };
            const collectPromptAttachmentState = () => {
                const promptTarget = findPromptTarget();
                const promptRoot = promptTarget?.closest?.("[data-testid], form, section, main, div") || promptTarget?.parentElement || null;
                const container = promptRoot?.parentElement || promptRoot || document.body;
                const nearby = Array.from(container.querySelectorAll("button, [role='button'], img, video, [data-testid], [tabindex], span, div"))
                    .filter((node) => visible(node))
                    .map((node) => ({
                        text: textOf(node).slice(0, 160),
                        aria_label: String(node.getAttribute?.("aria-label") || "").slice(0, 120),
                        role: String(node.getAttribute?.("role") || ""),
                        node: summarizeElement(node),
                    }))
                    .filter((item) => {
                        const hay = `${item.text} ${item.aria_label}`.toLowerCase();
                        return (
                            hay.includes("添加到提示") ||
                            hay.includes("上传媒体") ||
                            hay.includes("视频") ||
                            hay.includes("图片") ||
                            hay.includes("播放") ||
                            hay.includes("删除") ||
                            hay.includes("remove") ||
                            hay.includes("media")
                        );
                    })
                    .slice(0, 20);
                return {
                    prompt_target: summarizeElement(promptTarget),
                    nearby,
                };
            };
            const parseTimecodeFrameIndex = (text) => {
                const match = String(text || "").trim().match(/^(\d{2}):(\d{2}):(\d{2})$/);
                if (!match) return null;
                const minutes = Number.parseInt(match[1], 10);
                const seconds = Number.parseInt(match[2], 10);
                const frames = Number.parseInt(match[3], 10);
                if (![minutes, seconds, frames].every((value) => Number.isFinite(value) && value >= 0)) {
                    return null;
                }
                return (minutes * 60 * 24) + (seconds * 24) + frames;
            };
            const clampNumber = (value, minValue, maxValue) => {
                if (!Number.isFinite(Number(value))) return minValue;
                return Math.min(maxValue, Math.max(minValue, Number(value)));
            };
            const parseTimecodeParts = (text) => {
                const match = String(text || "").trim().match(/^(\d{2}):(\d{2}):(\d{2})$/);
                if (!match) return null;
                const minutes = Number.parseInt(match[1], 10);
                const seconds = Number.parseInt(match[2], 10);
                const fraction = Number.parseInt(match[3], 10);
                if (![minutes, seconds, fraction].every((value) => Number.isFinite(value) && value >= 0)) {
                    return null;
                }
                return { minutes, seconds, fraction };
            };
            const inferTimeFractionBase = (timeNodes, fixedTotalSeconds = null) => {
                const lastNode = Array.isArray(timeNodes) && timeNodes.length ? timeNodes[timeNodes.length - 1] : "";
                const parts = parseTimecodeParts(lastNode);
                if (!parts) return 100;
                if (parts.fraction > 23) return 100;
                if (Number.isFinite(Number(fixedTotalSeconds)) && Number(fixedTotalSeconds) > 0) {
                    const target = Number(fixedTotalSeconds);
                    const hundredths = parts.minutes * 60 + parts.seconds + (parts.fraction / 100);
                    const frame24 = parts.minutes * 60 + parts.seconds + (parts.fraction / 24);
                    return Math.abs(hundredths - target) <= Math.abs(frame24 - target) ? 100 : 24;
                }
                return 24;
            };
            const parseTimecodeSeconds = (text, fixedTotalSeconds = null, explicitFractionBase = null) => {
                const parts = parseTimecodeParts(text);
                if (!parts) return null;
                const fractionBase = Number.isFinite(Number(explicitFractionBase)) && Number(explicitFractionBase) > 0
                    ? Number(explicitFractionBase)
                    : inferTimeFractionBase([text], fixedTotalSeconds);
                return parts.minutes * 60 + parts.seconds + (parts.fraction / fractionBase);
            };
            const extractReferenceTimeRange = (previewState, fixedTotalSeconds = null) => {
                const fractionBase = inferTimeFractionBase(previewState?.time_nodes || [], fixedTotalSeconds);
                const parsedTimeNodes = Array.isArray(previewState?.time_nodes)
                    ? previewState.time_nodes
                        .map((item) => parseTimecodeSeconds(item, fixedTotalSeconds, fractionBase))
                        .filter((value) => Number.isFinite(value) && value >= 0)
                    : [];
                const inferredTotalSeconds = Number.isFinite(Number(fixedTotalSeconds)) && Number(fixedTotalSeconds) > 0
                    ? Number(fixedTotalSeconds)
                    : (parsedTimeNodes.length ? Math.max(...parsedTimeNodes) : null);
                const startSeconds = parsedTimeNodes.length >= 2 ? Math.min(...parsedTimeNodes) : 0;
                const endSeconds = parsedTimeNodes.length >= 2 ? Math.max(...parsedTimeNodes) : inferredTotalSeconds;
                return {
                    parsed_time_seconds: parsedTimeNodes,
                    fraction_base: fractionBase,
                    total_seconds: Number.isFinite(inferredTotalSeconds) && inferredTotalSeconds > 0 ? inferredTotalSeconds : null,
                    start_seconds: Number.isFinite(startSeconds) && startSeconds >= 0 ? startSeconds : null,
                    end_seconds: Number.isFinite(endSeconds) && endSeconds >= 0 ? endSeconds : null,
                };
            };
            const extractReferenceFrameRange = (previewState, fixedTotalFrames = null) => {
                const parsedTimeNodes = Array.isArray(previewState?.time_nodes)
                    ? previewState.time_nodes
                        .map((item) => parseTimecodeFrameIndex(item))
                        .filter((value) => Number.isFinite(value) && value >= 0)
                    : [];
                const inferredTotalFrames = Number.isFinite(Number(fixedTotalFrames)) && Number(fixedTotalFrames) > 0
                    ? Number(fixedTotalFrames)
                    : (parsedTimeNodes.length ? Math.max(...parsedTimeNodes) : null);
                const startFrameIndex = parsedTimeNodes.length >= 2
                    ? Math.min(...parsedTimeNodes)
                    : 0;
                const endFrameIndex = parsedTimeNodes.length >= 2
                    ? Math.max(...parsedTimeNodes)
                    : inferredTotalFrames;
                return {
                    parsed_time_nodes: parsedTimeNodes,
                    total_frames: Number.isFinite(inferredTotalFrames) && inferredTotalFrames > 0 ? inferredTotalFrames : null,
                    start_frame_index: Number.isFinite(startFrameIndex) && startFrameIndex >= 0 ? startFrameIndex : null,
                    end_frame_index: Number.isFinite(endFrameIndex) && endFrameIndex >= 0 ? endFrameIndex : null,
                };
            };
            const dragAtPoint = (fromX, fromY, toX, toY) => {
                const startX = Math.round(fromX);
                const startY = Math.round(fromY);
                const endX = Math.round(toX);
                const endY = Math.round(toY);
                const startTarget = document.elementFromPoint(startX, startY) || document.body;
                if (!startTarget?.dispatchEvent) {
                    return { dragged: false, reason: "missing-start-target", start_x: startX, start_y: startY, end_x: endX, end_y: endY };
                }
                const dispatch = (node, type, x, y) => {
                    const common = {
                        bubbles: true,
                        cancelable: true,
                        composed: true,
                        clientX: Math.round(x),
                        clientY: Math.round(y),
                        button: 0,
                        buttons: type === "pointerup" || type === "mouseup" ? 0 : 1,
                    };
                    try {
                        const EventCtor = type.startsWith("pointer") ? PointerEvent : MouseEvent;
                        node.dispatchEvent(new EventCtor(type, common));
                    } catch (_error) {
                        node.dispatchEvent(new MouseEvent(type.startsWith("pointer") ? "mousemove" : type, common));
                    }
                };
                dispatch(startTarget, "pointerdown", startX, startY);
                dispatch(startTarget, "mousedown", startX, startY);
                for (let step = 1; step <= 8; step += 1) {
                    const progress = step / 8;
                    const x = startX + ((endX - startX) * progress);
                    const y = startY + ((endY - startY) * progress);
                    const moveTarget = document.elementFromPoint(Math.round(x), Math.round(y)) || document.body;
                    dispatch(moveTarget, "pointermove", x, y);
                    dispatch(moveTarget, "mousemove", x, y);
                }
                const endTarget = document.elementFromPoint(endX, endY) || document.body;
                dispatch(endTarget, "pointerup", endX, endY);
                dispatch(endTarget, "mouseup", endX, endY);
                return {
                    dragged: true,
                    start_x: startX,
                    start_y: startY,
                    end_x: endX,
                    end_y: endY,
                    start_target: summarizeElement(startTarget),
                    end_target: summarizeElement(endTarget),
                };
            };
            const isReferenceResizeHandleCursor = (cursorValue) => {
                const cursor = String(cursorValue || "").toLowerCase();
                return cursor.includes("grab") || cursor.includes("ew-resize") || cursor.includes("col-resize");
            };
            const collectReferenceResizeHandleSegments = (lowerStripCandidates) =>
                (Array.isArray(lowerStripCandidates) ? lowerStripCandidates : [])
                    .filter((item) => item?.tag === "div")
                    .filter((item) => isReferenceResizeHandleCursor(item?.cursor))
                    .filter((item) => item?.rect && item.rect.width >= 10 && item.rect.width <= 24 && item.rect.height >= 40 && item.rect.height <= 80)
                    .sort((left, right) => left.rect.left - right.rect.left);
            const buildReferenceTimeRangePlan = (previewState, requestedStartSeconds, requestedEndSeconds, fixedTotalSeconds = null) => {
                const parsedTimeRange = extractReferenceTimeRange(previewState, fixedTotalSeconds);
                const totalSeconds = Number.isFinite(Number(fixedTotalSeconds)) && Number(fixedTotalSeconds) > 0
                    ? Number(fixedTotalSeconds)
                    : parsedTimeRange.total_seconds;
                const lowerStripCandidates = Array.isArray(previewState?.lower_strip_candidates) ? previewState.lower_strip_candidates : [];
                const directTrimHandleCandidates = Array.isArray(previewState?.trim_handle_candidates) ? previewState.trim_handle_candidates : [];
                const trimHandleImages = [
                    ...directTrimHandleCandidates,
                    ...lowerStripCandidates.filter((item) => /trim-handle-(left|right)\.svg/i.test(String(item.src || ""))),
                ];
                const resolveHandleRect = (item) => {
                    if (!item) return null;
                    const resolvedHandleRect = item.resolved_handle_rect;
                    const parentRect = item.parent_rect;
                    const rect = item.rect;
                    const ancestorRects = Array.isArray(item.ancestor_rects) ? item.ancestor_rects.map((entry) => entry.rect).filter(Boolean) : [];
                    if (resolvedHandleRect && resolvedHandleRect.width >= 8 && resolvedHandleRect.height >= 16) {
                        return resolvedHandleRect;
                    }
                    if (parentRect && parentRect.width >= 8 && parentRect.width <= 28 && parentRect.height >= 20 && parentRect.height <= 48) {
                        return parentRect;
                    }
                    const ancestorRect = ancestorRects.find((candidate) =>
                        candidate &&
                        candidate.width >= 8 &&
                        candidate.width <= 36 &&
                        candidate.height >= 20 &&
                        candidate.height <= 64
                    );
                    if (ancestorRect) {
                        return ancestorRect;
                    }
                    if (rect && rect.width >= 8 && rect.height >= 8) {
                        return rect;
                    }
                    return null;
                };
                const leftHandleItem = trimHandleImages.find((item) => /trim-handle-left\.svg/i.test(String(item.src || ""))) || null;
                const rightHandleItem = trimHandleImages.find((item) => /trim-handle-right\.svg/i.test(String(item.src || ""))) || null;
                const resizeHandleSegments = collectReferenceResizeHandleSegments(lowerStripCandidates);
                const inferredLeftHandleRect = resizeHandleSegments.length >= 2 ? resizeHandleSegments[0].rect : null;
                const inferredRightHandleRect = resizeHandleSegments.length >= 2 ? resizeHandleSegments[resizeHandleSegments.length - 1].rect : null;
                const leftHandleRect = resolveHandleRect(leftHandleItem) || inferredLeftHandleRect;
                const rightHandleRect = resolveHandleRect(rightHandleItem) || inferredRightHandleRect;
                const grabSegments = lowerStripCandidates
                    .filter((item) => isReferenceResizeHandleCursor(item?.cursor))
                    .filter((item) => item.tag === "div")
                    .filter((item) => item.rect && item.rect.width >= 10 && item.rect.width <= 60 && item.rect.height >= 20 && item.rect.height <= 80);
                const slotNodes = lowerStripCandidates
                    .filter((item) => item.rect && item.rect.width >= 60 && item.rect.width <= 90 && item.rect.height >= 50 && item.rect.height <= 90);
                if (!Number.isFinite(totalSeconds) || totalSeconds <= 0) {
                    return { ok: false, reason: "missing-total-seconds", total_seconds: totalSeconds || 0 };
                }
                if (!grabSegments.length && !(leftHandleRect && rightHandleRect)) {
                    return { ok: false, reason: "missing-grab-segments", total_seconds: totalSeconds || 0 };
                }
                const currentLeft = leftHandleRect ? leftHandleRect.left : Math.min(...grabSegments.map((item) => item.rect.left));
                const currentRight = rightHandleRect ? (rightHandleRect.left + rightHandleRect.width) : Math.max(...grabSegments.map((item) => item.rect.left + item.rect.width));
                const explicitTrackRect = previewState?.track_rect;
                let trackLeft = explicitTrackRect?.left ?? (slotNodes.length ? Math.min(...slotNodes.map((item) => item.rect.left)) : currentLeft);
                let trackRight = explicitTrackRect ? (explicitTrackRect.left + explicitTrackRect.width) : (slotNodes.length ? Math.max(...slotNodes.map((item) => item.rect.left + item.rect.width)) : currentRight);
                if (leftHandleRect && currentLeft < trackLeft) trackLeft = currentLeft;
                if (rightHandleRect && currentRight > trackRight) trackRight = currentRight;
                const trackWidth = Math.max(1, trackRight - trackLeft);
                const centerY = leftHandleRect && rightHandleRect
                    ? Math.round(((leftHandleRect.top + (leftHandleRect.height / 2)) + (rightHandleRect.top + (rightHandleRect.height / 2))) / 2)
                    : Math.round(grabSegments.reduce((sum, item) => sum + item.rect.top + (item.rect.height / 2), 0) / grabSegments.length);
                if (trackWidth < 24 || !Number.isFinite(centerY) || centerY <= 0) {
                    return {
                        ok: false,
                        reason: "invalid-track-geometry",
                        total_seconds: totalSeconds || 0,
                        track_left: Math.round(trackLeft || 0),
                        track_right: Math.round(trackRight || 0),
                        track_width: Math.round(trackWidth || 0),
                        center_y: Math.round(centerY || 0),
                    };
                }
                const normalizedStart = Number.isFinite(requestedStartSeconds)
                    ? Math.max(0, Math.min(totalSeconds - 0.01, Number(requestedStartSeconds)))
                    : 0;
                const normalizedEnd = Number.isFinite(requestedEndSeconds)
                    ? Math.max(normalizedStart + 0.01, Math.min(totalSeconds, Number(requestedEndSeconds)))
                    : totalSeconds;
                return {
                    ok: true,
                    total_seconds: totalSeconds,
                    track_left: Math.round(trackLeft),
                    track_right: Math.round(trackRight),
                    track_width: Math.round(trackWidth),
                    current_left: Math.round(currentLeft),
                    current_right: Math.round(currentRight),
                    center_y: centerY,
                    pixels_per_second: trackWidth / totalSeconds,
                    start_handle_x: Math.round(leftHandleRect ? (leftHandleRect.left + leftHandleRect.width - 1) : Math.max(trackLeft + 2, currentLeft - 8)),
                    end_handle_x: Math.round(rightHandleRect ? (rightHandleRect.left + 1) : Math.min(trackRight - 2, currentRight - 2)),
                    target_left: Math.round(trackLeft + ((trackWidth * normalizedStart) / totalSeconds)),
                    target_right: Math.round(trackLeft + ((trackWidth * normalizedEnd) / totalSeconds)),
                    current_start_seconds: parsedTimeRange.start_seconds,
                    current_end_seconds: parsedTimeRange.end_seconds,
                    parsed_time_seconds: parsedTimeRange.parsed_time_seconds,
                    parsed_fraction_base: parsedTimeRange.fraction_base,
                    requested_start_seconds: normalizedStart,
                    requested_end_seconds: normalizedEnd,
                };
            };
            const buildReferenceFrameRangePlan = (previewState, requestedStartFrameIndex, requestedEndFrameIndex, fixedTotalFrames = null) => {
                const parsedTotalFrames = parseTimecodeFrameIndex(previewState?.time_nodes?.[previewState.time_nodes.length - 1]);
                const parsedFrameRange = extractReferenceFrameRange(previewState, fixedTotalFrames);
                const totalFrames = Number.isFinite(Number(fixedTotalFrames)) && Number(fixedTotalFrames) > 0
                    ? Number(fixedTotalFrames)
                    : (parsedFrameRange.total_frames || parsedTotalFrames);
                const lowerStripCandidates = Array.isArray(previewState?.lower_strip_candidates) ? previewState.lower_strip_candidates : [];
                const directTrimHandleCandidates = Array.isArray(previewState?.trim_handle_candidates) ? previewState.trim_handle_candidates : [];
                const trimHandleImages = [
                    ...directTrimHandleCandidates,
                    ...lowerStripCandidates.filter((item) => /trim-handle-(left|right)\.svg/i.test(String(item.src || ""))),
                ];
                const resolveHandleRect = (item) => {
                    if (!item) return null;
                    const resolvedHandleRect = item.resolved_handle_rect;
                    const parentRect = item.parent_rect;
                    const rect = item.rect;
                    const ancestorRects = Array.isArray(item.ancestor_rects) ? item.ancestor_rects.map((entry) => entry.rect).filter(Boolean) : [];
                    if (resolvedHandleRect && resolvedHandleRect.width >= 8 && resolvedHandleRect.height >= 16) {
                        return resolvedHandleRect;
                    }
                    if (parentRect && parentRect.width >= 8 && parentRect.width <= 28 && parentRect.height >= 20 && parentRect.height <= 48) {
                        return parentRect;
                    }
                    const ancestorRect = ancestorRects.find((candidate) =>
                        candidate &&
                        candidate.width >= 8 &&
                        candidate.width <= 36 &&
                        candidate.height >= 20 &&
                        candidate.height <= 64
                    );
                    if (ancestorRect) {
                        return ancestorRect;
                    }
                    if (rect && rect.width >= 8 && rect.height >= 8) {
                        return rect;
                    }
                    return null;
                };
                const leftHandleItem = trimHandleImages.find((item) => /trim-handle-left\.svg/i.test(String(item.src || ""))) || null;
                const rightHandleItem = trimHandleImages.find((item) => /trim-handle-right\.svg/i.test(String(item.src || ""))) || null;
                const resizeHandleSegments = collectReferenceResizeHandleSegments(lowerStripCandidates);
                const inferredLeftHandleRect = resizeHandleSegments.length >= 2 ? resizeHandleSegments[0].rect : null;
                const inferredRightHandleRect = resizeHandleSegments.length >= 2 ? resizeHandleSegments[resizeHandleSegments.length - 1].rect : null;
                const leftHandleRect = resolveHandleRect(leftHandleItem) || inferredLeftHandleRect;
                const rightHandleRect = resolveHandleRect(rightHandleItem) || inferredRightHandleRect;
                const grabSegments = lowerStripCandidates
                    .filter((item) => isReferenceResizeHandleCursor(item?.cursor))
                    .filter((item) => item.tag === "div")
                    .filter((item) => item.rect && item.rect.width >= 10 && item.rect.width <= 60 && item.rect.height >= 20 && item.rect.height <= 80);
                const slotNodes = lowerStripCandidates
                    .filter((item) => item.rect && item.rect.width >= 60 && item.rect.width <= 90 && item.rect.height >= 50 && item.rect.height <= 90);
                if (!Number.isFinite(totalFrames) || totalFrames <= 0) {
                    return { ok: false, reason: "missing-total-frames", total_frames: totalFrames || 0 };
                }
                if (!grabSegments.length && !(leftHandleRect && rightHandleRect)) {
                    return { ok: false, reason: "missing-grab-segments", total_frames: totalFrames || 0 };
                }
                const currentLeft = leftHandleRect
                    ? leftHandleRect.left
                    : Math.min(...grabSegments.map((item) => item.rect.left));
                const currentRight = rightHandleRect
                    ? (rightHandleRect.left + rightHandleRect.width)
                    : Math.max(...grabSegments.map((item) => item.rect.left + item.rect.width));
                let trackLeft = slotNodes.length
                    ? Math.min(...slotNodes.map((item) => item.rect.left))
                    : currentLeft;
                let trackRight = slotNodes.length
                    ? Math.max(...slotNodes.map((item) => item.rect.left + item.rect.width))
                    : currentRight;
                if (leftHandleRect && currentLeft < trackLeft) {
                    trackLeft = currentLeft;
                }
                if (rightHandleRect && currentRight > trackRight) {
                    trackRight = currentRight;
                }
                const trackWidth = Math.max(1, trackRight - trackLeft);
                const centerY = leftHandleRect && rightHandleRect
                    ? Math.round(((leftHandleRect.top + (leftHandleRect.height / 2)) + (rightHandleRect.top + (rightHandleRect.height / 2))) / 2)
                    : Math.round(grabSegments.reduce((sum, item) => sum + item.rect.top + (item.rect.height / 2), 0) / grabSegments.length);
                if (trackWidth < 24 || !Number.isFinite(centerY) || centerY <= 0) {
                    return {
                        ok: false,
                        reason: "invalid-track-geometry",
                        total_frames: totalFrames || 0,
                        track_left: Math.round(trackLeft || 0),
                        track_right: Math.round(trackRight || 0),
                        track_width: Math.round(trackWidth || 0),
                        center_y: Math.round(centerY || 0),
                    };
                }
                const normalizedStart = Number.isFinite(requestedStartFrameIndex)
                    ? Math.max(0, Math.min(totalFrames - 1, Number(requestedStartFrameIndex)))
                    : 0;
                const normalizedEnd = Number.isFinite(requestedEndFrameIndex)
                    ? Math.max(normalizedStart + 1, Math.min(totalFrames, Number(requestedEndFrameIndex)))
                    : totalFrames;
                return {
                    ok: true,
                    total_frames: totalFrames,
                    track_left: Math.round(trackLeft),
                    track_right: Math.round(trackRight),
                    track_width: Math.round(trackWidth),
                    current_left: Math.round(currentLeft),
                    current_right: Math.round(currentRight),
                    center_y: centerY,
                    pixels_per_frame: trackWidth / totalFrames,
                    start_handle_x: Math.round(leftHandleRect ? (leftHandleRect.left + leftHandleRect.width - 1) : Math.max(trackLeft + 2, currentLeft - 8)),
                    end_handle_x: Math.round(rightHandleRect ? (rightHandleRect.left + 1) : Math.min(trackRight - 2, currentRight - 2)),
                    target_left: Math.round(trackLeft + ((trackWidth * normalizedStart) / totalFrames)),
                    target_right: Math.round(trackLeft + ((trackWidth * normalizedEnd) / totalFrames)),
                    left_handle_rect: leftHandleRect,
                    right_handle_rect: rightHandleRect,
                    current_start_frame_index: parsedFrameRange.start_frame_index,
                    current_end_frame_index: parsedFrameRange.end_frame_index,
                    parsed_time_nodes: parsedFrameRange.parsed_time_nodes,
                    requested_start_frame_index: normalizedStart,
                    requested_end_frame_index: normalizedEnd,
                };
            };
            const previewHasFrameStrip = (previewState) => {
                const lowerStripCandidates = Array.isArray(previewState?.lower_strip_candidates) ? previewState.lower_strip_candidates : [];
                const trimHandleImages = lowerStripCandidates.filter((item) => /trim-handle-(left|right)\.svg/i.test(String(item.src || "")));
                const grabSegments = lowerStripCandidates
                    .filter((item) => isReferenceResizeHandleCursor(item?.cursor))
                    .filter((item) => item.rect && item.rect.width >= 10 && item.rect.width <= 60 && item.rect.height >= 20 && item.rect.height <= 80);
                const stripMarkers = lowerStripCandidates
                    .filter((item) => item.rect && item.rect.top >= 860)
                    .filter((item) => item.rect.width >= 10 && item.rect.height >= 20);
                return trimHandleImages.length >= 1 || grabSegments.length > 0 || stripMarkers.length >= 6;
            };
            const previewHasInteractiveTrimControls = (previewState) => {
                const lowerStripCandidates = Array.isArray(previewState?.lower_strip_candidates) ? previewState.lower_strip_candidates : [];
                const trimHandleImages = lowerStripCandidates.filter((item) => /trim-handle-(left|right)\.svg/i.test(String(item.src || "")));
                const grabSegments = lowerStripCandidates
                    .filter((item) => isReferenceResizeHandleCursor(item?.cursor))
                    .filter((item) => item.rect && item.rect.width >= 10 && item.rect.width <= 60 && item.rect.height >= 20 && item.rect.height <= 80);
                return trimHandleImages.length >= 2 || grabSegments.length >= 2;
            };
            const settleReferenceFramePreview = async (dialog, initialPreviewState, dialogResolver = null) => {
                const getActiveDialog = () => {
                    if (typeof dialogResolver === "function") {
                        try {
                            return dialogResolver() || dialog;
                        } catch (_error) {
                            return dialog;
                        }
                    }
                    return dialog;
                };
                let activeDialog = getActiveDialog();
                let previewState = initialPreviewState;
                const snapshots = [
                    {
                        attempt: 0,
                        href: String(location.href || ""),
                        dialog_node: summarizeElement(activeDialog),
                        time_nodes: previewState?.time_nodes || [],
                        lower_strip_count: Array.isArray(previewState?.lower_strip_candidates) ? previewState.lower_strip_candidates.length : 0,
                        has_frame_strip: previewHasFrameStrip(previewState),
                        has_interactive_trim: previewHasInteractiveTrimControls(previewState),
                    },
                ];
                let nudged = false;
                for (let attempt = 1; attempt <= 12; attempt += 1) {
                    if (previewHasInteractiveTrimControls(previewState)) break;
                    if (!nudged && attempt === 5 && previewState?.primary_media?.rect) {
                        const rect = previewState.primary_media.rect;
                        const nudgeResult = clickAtPoint(rect.left + (rect.width / 2), rect.top + (rect.height / 2), { nativeClick: false });
                        nudged = true;
                    }
                    await sleep(attempt <= 3 ? 220 : (attempt <= 8 ? 320 : 420));
                    activeDialog = getActiveDialog();
                    previewState = collectReferencePreviewState(activeDialog);
                    snapshots.push({
                        attempt,
                        href: String(location.href || ""),
                        dialog_node: summarizeElement(activeDialog),
                        time_nodes: previewState?.time_nodes || [],
                        lower_strip_count: Array.isArray(previewState?.lower_strip_candidates) ? previewState.lower_strip_candidates.length : 0,
                        has_frame_strip: previewHasFrameStrip(previewState),
                        has_interactive_trim: previewHasInteractiveTrimControls(previewState),
                    });
                }
                // #endregion
                return previewState;
            };
            const applyReferenceFrameRange = async (dialog, requestedStartFrameIndex, requestedEndFrameIndex, previewStateBefore, dialogResolver = null) => {
                const getActiveDialog = () => {
                    if (typeof dialogResolver === "function") {
                        try {
                            return dialogResolver() || dialog;
                        } catch (_error) {
                            return dialog;
                        }
                    }
                    return dialog;
                };
                if (!Number.isFinite(requestedStartFrameIndex) && !Number.isFinite(requestedEndFrameIndex)) {
                    return { applied: false, reason: "range-not-requested" };
                }
                let activeDialog = getActiveDialog();
                const settledPreviewBefore = await settleReferenceFramePreview(activeDialog, previewStateBefore, dialogResolver);
                const initialPlan = buildReferenceFrameRangePlan(settledPreviewBefore, requestedStartFrameIndex, requestedEndFrameIndex);
                // #endregion
                if (!initialPlan.ok) {
                    return {
                        applied: false,
                        reason: initialPlan.reason || "plan-unavailable",
                        plan: initialPlan,
                        preview_before: settledPreviewBefore,
                    };
                }
                const actions = [];
                const baselineTotalFrames = initialPlan.total_frames;
                let workingPreview = settledPreviewBefore;
                let postStartPlan = initialPlan;
                const refineHandle = async (kind, requestedFrameIndex) => {
                    if (!Number.isFinite(requestedFrameIndex)) return;
                    for (let iteration = 0; iteration < 4; iteration += 1) {
                        const plan = buildReferenceFrameRangePlan(
                            workingPreview,
                            requestedStartFrameIndex,
                            requestedEndFrameIndex,
                            baselineTotalFrames
                        );
                        if (!plan.ok) return;
                        const isStart = kind === "start";
                        const currentFrameIndex = isStart ? plan.current_start_frame_index : plan.current_end_frame_index;
                        const fallbackTargetX = isStart ? (plan.target_left + 2) : (plan.target_right - 2);
                        const handleX = isStart ? plan.start_handle_x : plan.end_handle_x;
                        const minX = plan.track_left + 1;
                        const maxX = plan.track_right - 1;
                        const frameDelta = Number.isFinite(currentFrameIndex)
                            ? (Number(requestedFrameIndex) - Number(currentFrameIndex))
                            : null;
                        if (frameDelta === 0) {
                            if (isStart) postStartPlan = plan;
                            return;
                        }
                        let targetX = fallbackTargetX;
                        let moveReason = "absolute-target";
                        if (Number.isFinite(frameDelta) && Number.isFinite(plan.pixels_per_frame) && plan.pixels_per_frame > 0) {
                            let deltaPixels = Math.round(frameDelta * plan.pixels_per_frame);
                            if (deltaPixels === 0) {
                                deltaPixels = frameDelta > 0 ? 1 : -1;
                            }
                            if (Math.abs(frameDelta) <= 2) {
                                deltaPixels += deltaPixels > 0 ? 1 : -1;
                            }
                            targetX = handleX + deltaPixels;
                            moveReason = "frame-delta";
                        }
                        targetX = clampNumber(targetX, minX, maxX);
                        if (Math.abs(targetX - handleX) < 1) {
                            if (isStart) postStartPlan = plan;
                            return;
                        }
                        const drag = dragAtPoint(handleX, plan.center_y, targetX, plan.center_y);
                        actions.push({
                            kind,
                            iteration,
                            requested_frame_index: Number(requestedFrameIndex),
                            current_frame_index: Number.isFinite(currentFrameIndex) ? Number(currentFrameIndex) : null,
                            frame_delta: Number.isFinite(frameDelta) ? Number(frameDelta) : null,
                            move_reason: moveReason,
                            drag,
                        });
                        await sleep(iteration === 0 ? 220 : 260);
                        activeDialog = getActiveDialog();
                        workingPreview = await settleReferenceFramePreview(activeDialog, collectReferencePreviewState(activeDialog), dialogResolver);
                        if (isStart) {
                            postStartPlan = buildReferenceFrameRangePlan(
                                workingPreview,
                                requestedStartFrameIndex,
                                requestedEndFrameIndex,
                                baselineTotalFrames
                            );
                        }
                    }
                };
                await refineHandle("start", requestedStartFrameIndex);
                if (!postStartPlan.ok) {
                    postStartPlan = initialPlan;
                }
                await refineHandle("end", requestedEndFrameIndex);
                activeDialog = getActiveDialog();
                const previewAfter = workingPreview || collectReferencePreviewState(activeDialog);
                const finalPlan = buildReferenceFrameRangePlan(previewAfter, requestedStartFrameIndex, requestedEndFrameIndex, baselineTotalFrames);
                // #endregion
                return {
                    applied: withinTolerance,
                    drag_attempted: actions.length > 0,
                    actions,
                    initial_plan: initialPlan,
                    post_start_plan: postStartPlan,
                    final_plan: finalPlan,
                    preview_before: settledPreviewBefore,
                    preview_after: previewAfter,
                };
            };
            const applyReferenceTimeRange = async (
                dialog,
                requestedStartSeconds,
                requestedEndSeconds,
                requestedSourceDurationSeconds,
                previewStateBefore,
                dialogResolver = null
            ) => {
                const getActiveDialog = () => {
                    if (typeof dialogResolver === "function") {
                        try {
                            return dialogResolver() || dialog;
                        } catch (_error) {
                            return dialog;
                        }
                    }
                    return dialog;
                };
                if (!Number.isFinite(requestedStartSeconds) && !Number.isFinite(requestedEndSeconds)) {
                    return { applied: false, reason: "range-not-requested" };
                }
                let activeDialog = getActiveDialog();
                // #endregion
                const settledPreviewBefore = await settleReferenceFramePreview(activeDialog, previewStateBefore, dialogResolver);
                const initialPlan = buildReferenceTimeRangePlan(
                    settledPreviewBefore,
                    requestedStartSeconds,
                    requestedEndSeconds,
                    requestedSourceDurationSeconds
                );
                if (!initialPlan.ok) {
                    // #endregion
                    return {
                        applied: false,
                        reason: initialPlan.reason || "plan-unavailable",
                        plan: initialPlan,
                        preview_before: settledPreviewBefore,
                    };
                }
                const actions = [];
                const baselineTotalSeconds = initialPlan.total_seconds;
                let workingPreview = settledPreviewBefore;
                let postStartPlan = initialPlan;
                const refineHandle = async (kind, requestedSeconds) => {
                    if (!Number.isFinite(requestedSeconds)) return;
                    for (let iteration = 0; iteration < 4; iteration += 1) {
                        const plan = buildReferenceTimeRangePlan(
                            workingPreview,
                            requestedStartSeconds,
                            requestedEndSeconds,
                            baselineTotalSeconds
                        );
                        // #endregion
                        if (!plan.ok) {
                            // #endregion
                            return;
                        }
                        const isStart = kind === "start";
                        const currentSeconds = isStart ? plan.current_start_seconds : plan.current_end_seconds;
                        const fallbackTargetX = isStart ? (plan.target_left + 2) : (plan.target_right - 2);
                        const handleX = isStart ? plan.start_handle_x : plan.end_handle_x;
                        const minX = plan.track_left + 1;
                        const maxX = plan.track_right - 1;
                        const secondsDelta = Number.isFinite(currentSeconds)
                            ? (Number(requestedSeconds) - Number(currentSeconds))
                            : null;
                        if (Number.isFinite(secondsDelta) && Math.abs(secondsDelta) <= 0.03) {
                            if (isStart) postStartPlan = plan;
                            return;
                        }
                        let targetX = fallbackTargetX;
                        let moveReason = "absolute-target";
                        if (Number.isFinite(secondsDelta) && Number.isFinite(plan.pixels_per_second) && plan.pixels_per_second > 0) {
                            let deltaPixels = Math.round(secondsDelta * plan.pixels_per_second);
                            if (deltaPixels === 0) {
                                deltaPixels = secondsDelta > 0 ? 1 : -1;
                            }
                            targetX = handleX + deltaPixels;
                            moveReason = "time-delta";
                        }
                        targetX = clampNumber(targetX, minX, maxX);
                        if (Math.abs(targetX - handleX) < 1) {
                            if (isStart) postStartPlan = plan;
                            return;
                        }
                        // #endregion
                        const drag = dragAtPoint(handleX, plan.center_y, targetX, plan.center_y);
                        actions.push({
                            kind,
                            iteration,
                            requested_seconds: Number(requestedSeconds),
                            current_seconds: Number.isFinite(currentSeconds) ? Number(currentSeconds) : null,
                            seconds_delta: Number.isFinite(secondsDelta) ? Number(secondsDelta) : null,
                            move_reason: moveReason,
                            drag,
                        });
                        await sleep(iteration === 0 ? 220 : 260);
                        activeDialog = getActiveDialog();
                        workingPreview = await settleReferenceFramePreview(activeDialog, collectReferencePreviewState(activeDialog), dialogResolver);
                        // #endregion
                        if (isStart) {
                            postStartPlan = buildReferenceTimeRangePlan(
                                workingPreview,
                                requestedStartSeconds,
                                requestedEndSeconds,
                                baselineTotalSeconds
                            );
                        }
                    }
                };
                await refineHandle("start", requestedStartSeconds);
                if (!postStartPlan.ok) {
                    postStartPlan = initialPlan;
                }
                await refineHandle("end", requestedEndSeconds);
                activeDialog = getActiveDialog();
                const previewAfter = workingPreview || collectReferencePreviewState(activeDialog);
                const finalPlan = buildReferenceTimeRangePlan(
                    previewAfter,
                    requestedStartSeconds,
                    requestedEndSeconds,
                    baselineTotalSeconds
                );
                const finalStartSeconds = Number.isFinite(finalPlan?.current_start_seconds) ? Number(finalPlan.current_start_seconds) : null;
                const finalEndSeconds = Number.isFinite(finalPlan?.current_end_seconds) ? Number(finalPlan.current_end_seconds) : null;
                const requestedStart = Number.isFinite(requestedStartSeconds) ? Number(requestedStartSeconds) : null;
                const requestedEnd = Number.isFinite(requestedEndSeconds) ? Number(requestedEndSeconds) : null;
                const startErrorSeconds =
                    Number.isFinite(finalStartSeconds) && Number.isFinite(requestedStart)
                        ? Number((finalStartSeconds - requestedStart).toFixed(4))
                        : null;
                const endErrorSeconds =
                    Number.isFinite(finalEndSeconds) && Number.isFinite(requestedEnd)
                        ? Number((finalEndSeconds - requestedEnd).toFixed(4))
                        : null;
                const withinTolerance =
                    (!Number.isFinite(startErrorSeconds) || Math.abs(startErrorSeconds) <= 0.05) &&
                    (!Number.isFinite(endErrorSeconds) || Math.abs(endErrorSeconds) <= 0.05);
                return {
                    applied: withinTolerance,
                    drag_attempted: actions.length > 0,
                    actions,
                    initial_plan: initialPlan,
                    post_start_plan: postStartPlan,
                    final_plan: finalPlan,
                    requested_start_seconds: requestedStart,
                    requested_end_seconds: requestedEnd,
                    final_start_seconds: finalStartSeconds,
                    final_end_seconds: finalEndSeconds,
                    start_error_seconds: startErrorSeconds,
                    end_error_seconds: endErrorSeconds,
                    within_tolerance: withinTolerance,
                    preview_before: settledPreviewBefore,
                    preview_after: previewAfter,
                };
            };
            const findPromptAdjacentAddMediaButton = () => {
                const promptTarget = findPromptTarget();
                const promptRect = promptTarget?.getBoundingClientRect?.() || null;
                const candidates = Array.from(document.querySelectorAll("button, [role='button']"))
                    .filter((node) => visible(node))
                    .map((node) => {
                        const text = textOf(node);
                        const icon = getButtonIconText(node).toLowerCase();
                        const ariaLabel = String(node.getAttribute?.("aria-label") || "").trim();
                        const title = String(node.getAttribute?.("title") || "").trim();
                        const rect = node.getBoundingClientRect?.() || null;
                        const raw = `${text} ${ariaLabel} ${title}`;
                        const compact = raw.replace(/\s+/g, "").toLowerCase();
                        const looksAdd =
                            text.includes("添加媒体") ||
                            text.includes("上传媒体") ||
                            text.includes("选择媒体") ||
                            ariaLabel.includes("添加媒体") ||
                            ariaLabel.includes("上传媒体") ||
                            ariaLabel.includes("选择媒体") ||
                            title.includes("添加媒体") ||
                            title.includes("上传媒体") ||
                            title.includes("选择媒体") ||
                            compact.startsWith("add添加媒体") ||
                            compact.startsWith("add上传媒体") ||
                            compact.includes("upload上传媒体") ||
                            (icon === "add" && compact.includes("媒体"));
                        if (!looksAdd || !rect) return null;
                        const nearPrompt = promptRect
                            ? rect.top >= promptRect.top - 80 &&
                              rect.top <= promptRect.bottom + 140 &&
                              rect.left <= promptRect.left + 140
                            : false;
                        const distanceScore = promptRect
                            ? Math.abs(rect.left - promptRect.left) + Math.abs(rect.top - promptRect.bottom)
                            : 999999;
                        return {
                            node,
                            text,
                            icon,
                            rect,
                            nearPrompt,
                            distanceScore,
                        };
                    })
                    .filter(Boolean)
                    .sort((left, right) => {
                        if (left.nearPrompt !== right.nearPrompt) return left.nearPrompt ? -1 : 1;
                        return left.distanceScore - right.distanceScore;
                    });
                return {
                    button: candidates[0]?.node || null,
                    candidates: candidates.slice(0, 8).map((item) => ({
                        text: item.text,
                        icon: item.icon,
                        near_prompt: item.nearPrompt,
                        distance_score: item.distanceScore,
                        rect: {
                            left: Math.round(item.rect.left),
                            top: Math.round(item.rect.top),
                            width: Math.round(item.rect.width),
                            height: Math.round(item.rect.height),
                        },
                        node: summarizeElement(item.node),
                    })),
                };
            };
            const openReferencePicker = async (references) => {
                const existingRoots = collectReferencePickerRoots().filter((node) => isReferencePickerRoot(node, references));
                if (existingRoots.length) {
                    return {
                        action: "reuse:reference-picker-open",
                        roots: existingRoots.slice(0, 3).map((node) => ({
                            text: textOf(node).slice(0, 240),
                            node: summarizeElement(node),
                        })),
                    };
                }
                const addButtonChoice = findPromptAdjacentAddMediaButton();
                const addButton = addButtonChoice.button;
                const addClick = addButton ? clickReal(addButton) : null;
                const addAction = addClick?.clicked
                    ? `click:open-reference-picker:${textOf(addButton).slice(0, 80)}`
                    : clickFirstButton(
                          [
                              (text) => text.includes("添加媒体"),
                              (text) => text.includes("上传媒体"),
                              (text) => text.includes("选择媒体"),
                              (text) => text.replace(/\s+/g, "").startsWith("add添加媒体"),
                              (text) => text.toLowerCase().includes("upload"),
                          ],
                          "click:open-reference-picker"
                      );
                if (!addAction) {
                    return {
                        action: "",
                        roots: [],
                    };
                }
                await sleep(300);
                await sleep(500);
                for (let attempt = 0; attempt < 6; attempt += 1) {
                    await sleep(attempt === 0 ? 260 : 180);
                    const roots = collectReferencePickerRoots().filter((node) => isReferencePickerRoot(node, references));
                    if (roots.length) {
                        return {
                            action: addAction,
                            roots: roots.slice(0, 3).map((node) => ({
                                text: textOf(node).slice(0, 240),
                                node: summarizeElement(node),
                            })),
                        };
                    }
                }
                return {
                    action: addAction,
                    roots: [],
                    snapshot: collectReferencePickerSnapshot(references),
                };
            };
            const resolveActiveReferenceDialog = (references, fallbackDialog = null) => {
                const candidates = collectReferencePickerRoots().filter((node) => isReferencePickerRoot(node, references));
                const scored = candidates
                    .map((node) => {
                        const rect = node.getBoundingClientRect?.() || null;
                        const area = rect ? Math.round(rect.width * rect.height) : 0;
                        const text = textOf(node);
                        const timeTokenCount = (text.match(/\d{2}:\d{2}:\d{2}/g) || []).length;
                        const trimHandleCount = node.querySelectorAll?.("img[src*='trim-handle']").length || 0;
                        const mediaCount = node.querySelectorAll?.("video, img").length || 0;
                        return {
                            node,
                            area,
                            timeTokenCount,
                            trimHandleCount,
                            mediaCount,
                        };
                    })
                    .sort((left, right) => {
                        if (left.trimHandleCount !== right.trimHandleCount) {
                            return right.trimHandleCount - left.trimHandleCount;
                        }
                        if (left.timeTokenCount !== right.timeTokenCount) {
                            return right.timeTokenCount - left.timeTokenCount;
                        }
                        if (left.mediaCount !== right.mediaCount) {
                            return right.mediaCount - left.mediaCount;
                        }
                        return right.area - left.area;
                    });
                return scored[0]?.node || fallbackDialog || null;
            };
            const selectVideoReferenceFromLibrary = async (
                references,
                mediaIdHint,
                requestedStartFrameIndex,
                requestedEndFrameIndex,
                requestedDebugHoldReferencePickerMs,
                requestedDebugSkipAddToPrompt
            ) => {
                // #endregion
                const pickerOpen = await openReferencePicker(references);
                const dialogs = collectReferencePickerRoots().filter((node) => isReferencePickerRoot(node, references));
                // #endregion
                if (!dialogs.length) {
                    return { action: "", matched_text: "", scanned: [] };
                }
                const scanned = [];
                const clickableCandidateSelector = "button, [role='button'], [role='option'], [aria-selected], [data-selected], [data-state]";
                const referenceListItemSelector = "[role='option'], [aria-selected], [data-selected], [data-state], li, article";
                const isReferenceListContainerNode = (node, root = null) => {
                    if (!node || node === root) return true;
                    const dataTestId = String(node.getAttribute?.("data-testid") || "").toLowerCase();
                    const dataVirtuosoScroller = String(node.getAttribute?.("data-virtuoso-scroller") || "").toLowerCase();
                    const role = String(node.getAttribute?.("role") || "").toLowerCase();
                    const tabIndex = String(node.getAttribute?.("tabindex") || "");
                    const className = String(node.className || "");
                    const style = typeof window !== "undefined" && window.getComputedStyle ? window.getComputedStyle(node) : null;
                    const overflowY = String(style?.overflowY || "").toLowerCase();
                    const childCount = node.children?.length || 0;
                    if (dataVirtuosoScroller === "true") return true;
                    if (dataTestId.includes("virtuoso-scroller")) return true;
                    if (role === "listbox" || role === "list") return true;
                    if (tabIndex === "0" && overflowY === "auto" && childCount >= 2) return true;
                    if (/virtuoso|scroller/i.test(className) && childCount >= 2) return true;
                    return false;
                };
                const resolveReferenceClickableCandidate = (node, root) => {
                    const preferred =
                        node.closest?.(referenceListItemSelector) ||
                        node.closest?.(clickableCandidateSelector) ||
                        node;
                    if (!preferred || preferred === root || !root.contains(preferred) || !visible(preferred)) {
                        return null;
                    }
                    if (isReferenceListContainerNode(preferred, root)) {
                        return null;
                    }
                    return preferred;
                };
                const resolveReferenceLibraryListScope = (dialog) => {
                    const candidates = Array.from(dialog.querySelectorAll("div, section, article, aside"))
                        .filter((node) => visible(node))
                        .map((node) => {
                            const rect = node.getBoundingClientRect?.() || null;
                            const text = textOf(node);
                            const itemCount = node.querySelectorAll?.("[role='option'], [aria-selected], [data-selected], [data-state], li, article").length || 0;
                            const dataTestId = String(node.getAttribute?.("data-testid") || "").toLowerCase();
                            const dataVirtuosoScroller = String(node.getAttribute?.("data-virtuoso-scroller") || "").toLowerCase();
                            return {
                                node,
                                rect,
                                itemCount,
                                textLength: text.length,
                                dataTestId,
                                dataVirtuosoScroller,
                            };
                        })
                        .filter((entry) => {
                            const rect = entry.rect;
                            if (!rect) return false;
                            const looksLikeListColumn =
                                rect.width >= 160 &&
                                rect.width <= 420 &&
                                rect.height >= 180 &&
                                rect.left < (window.innerWidth * 0.45);
                            const hasListSignals =
                                entry.dataVirtuosoScroller === "true" ||
                                entry.dataTestId.includes("virtuoso-scroller") ||
                                entry.itemCount >= 2;
                            return looksLikeListColumn && hasListSignals;
                        })
                        .sort((left, right) => {
                            const leftVirtuosoScore = Number(left.dataVirtuosoScroller === "true" || left.dataTestId.includes("virtuoso-scroller"));
                            const rightVirtuosoScore = Number(right.dataVirtuosoScroller === "true" || right.dataTestId.includes("virtuoso-scroller"));
                            if (leftVirtuosoScore !== rightVirtuosoScore) {
                                return rightVirtuosoScore - leftVirtuosoScore;
                            }
                            if (left.itemCount !== right.itemCount) {
                                return right.itemCount - left.itemCount;
                            }
                            return (left.rect?.left || 0) - (right.rect?.left || 0);
                        });
                    return candidates[0]?.node || dialog;
                };
                const hasMatchingSelectedReferenceOption = (listScope, activeReferences = [], mediaIdHint = "") => {
                    if (!listScope) return false;
                    const mediaIdCompact = String(mediaIdHint || "").replace(/\s+/g, "").toLowerCase();
                    return Array.from(listScope.querySelectorAll("[role='option'][aria-selected='true'], [role='option'][data-selected='true'], [role='option'][data-state='selected']"))
                        .filter((node) => visible(node))
                        .some((node) => {
                            const text = textOf(node);
                            const raw = `${text} ${String(node.getAttribute?.("aria-label") || "")} ${String(node.className || "")}`;
                            const compact = raw.replace(/\s+/g, "").toLowerCase();
                            const matchesByText = matchesReferenceText(raw, activeReferences);
                            const matchesById = !!mediaIdCompact && compact.includes(mediaIdCompact);
                            return matchesByText || matchesById;
                        });
                };
                const countReferenceOccurrences = (compactText) => {
                    let maxCount = 0;
                    for (const ref of Array.isArray(references) ? references : []) {
                        const compactRef = String(ref || "").replace(/\s+/g, "").trim().toLowerCase();
                        if (!compactRef || compactRef.length < 3) continue;
                        let count = 0;
                        let searchIndex = 0;
                        while (searchIndex >= 0) {
                            const nextIndex = compactText.indexOf(compactRef, searchIndex);
                            if (nextIndex < 0) break;
                            count += 1;
                            searchIndex = nextIndex + compactRef.length;
                        }
                        if (count > maxCount) {
                            maxCount = count;
                        }
                    }
                    return maxCount;
                };
                const looksLikeDirectReferenceLabel = (compactText) => {
                    for (const ref of Array.isArray(references) ? references : []) {
                        const compactRef = String(ref || "").replace(/\s+/g, "").trim().toLowerCase();
                        if (!compactRef || compactRef.length < 3) continue;
                        if (
                            compactText === compactRef ||
                            compactText === `${compactRef}视频` ||
                            compactText === `${compactRef}image` ||
                            compactText === `${compactRef}图片` ||
                            compactText.startsWith(`${compactRef}视频`) ||
                            compactText.startsWith(`${compactRef}image`) ||
                            compactText.startsWith(`${compactRef}图片`)
                        ) {
                            return true;
                        }
                    }
                    return false;
                };
                const buildCandidates = (root, selectors) => {
                    const candidateMap = new Map();
                    for (const node of Array.from(root.querySelectorAll(selectors))) {
                        if (!visible(node)) continue;
                        const clickableNode = resolveReferenceClickableCandidate(node, root);
                        if (!clickableNode) {
                            continue;
                        }
                        if (candidateMap.has(clickableNode)) continue;
                        const text = textOf(clickableNode);
                        const raw = `${text} ${String(clickableNode.getAttribute?.("aria-label") || "")} ${String(clickableNode.className || "")}`;
                        const compactText = text.replace(/\s+/g, "").toLowerCase();
                        const compact = raw.replace(/\s+/g, "").toLowerCase();
                        if (!text && !raw) continue;
                        candidateMap.set(clickableNode, {
                            node: clickableNode,
                            text,
                            raw,
                            compact_text: compactText,
                            compact,
                            repeated_reference_count: countReferenceOccurrences(compactText || compact),
                        });
                    }
                    return Array.from(candidateMap.values())
                        .filter((item) => item.text || item.raw)
                        .sort((left, right) => left.text.length - right.text.length);
                };
                const resolveReferenceCandidateClickTarget = (candidateNode) => {
                    if (!candidateNode || typeof candidateNode.querySelectorAll !== "function") {
                        return candidateNode || null;
                    }
                    const role = String(candidateNode.getAttribute?.("role") || "").toLowerCase();
                    if (role === "option") {
                        return candidateNode;
                    }
                    const textLikeDescendants = Array.from(candidateNode.querySelectorAll("span, p, h3, h4, div"))
                        .filter((node) => node !== candidateNode)
                        .filter((node) => visible(node))
                        .filter((node) => !node.querySelector?.("img, video, canvas, svg"))
                        .map((node) => ({
                            node,
                            text: textOf(node).trim(),
                            rect: node.getBoundingClientRect?.() || null,
                        }))
                        .filter((entry) => entry.text && entry.rect && entry.rect.width >= 16 && entry.rect.height >= 10)
                        .sort((left, right) => {
                            const leftArea = Math.round((left.rect?.width || 0) * (left.rect?.height || 0));
                            const rightArea = Math.round((right.rect?.width || 0) * (right.rect?.height || 0));
                            return leftArea - rightArea || left.text.length - right.text.length;
                        });
                    return textLikeDescendants[0]?.node || candidateNode;
                };
                const collectGlobalReferenceDetailState = (activeDialog = null) => {
                    const summarizeRect = (rect) =>
                        rect
                            ? {
                                left: Math.round(rect.left),
                                top: Math.round(rect.top),
                                width: Math.round(rect.width),
                                height: Math.round(rect.height),
                            }
                            : null;
                    const collectGlobalTimeEntries = () => {
                        const entries = [];
                        try {
                            const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
                            let currentNode = walker.nextNode();
                            while (currentNode && entries.length < 16) {
                                const raw = String(currentNode.textContent || "").trim();
                                if (/^\d{2}:\d{2}:\d{2}$/.test(raw)) {
                                    let rangeRect = null;
                                    try {
                                        const range = document.createRange();
                                        range.selectNodeContents(currentNode);
                                        rangeRect = range.getBoundingClientRect?.() || null;
                                    } catch (error) {}
                                    const hostNode = currentNode.parentElement || currentNode.parentNode;
                                    const hostRect = hostNode?.getBoundingClientRect?.() || null;
                                    const containerNode = hostNode?.closest?.("div, section, article, aside, [role='dialog'], [aria-modal='true']") || null;
                                    entries.push({
                                        text: raw,
                                        rect: summarizeRect(rangeRect && rangeRect.width > 0 && rangeRect.height > 0 ? rangeRect : hostRect),
                                        host_node: summarizeElement(hostNode),
                                        host_rect: summarizeRect(hostRect),
                                        container_node: summarizeElement(containerNode),
                                        container_rect: summarizeRect(containerNode?.getBoundingClientRect?.() || null),
                                        inside_active_dialog: !!(activeDialog && hostNode && activeDialog.contains(hostNode)),
                                    });
                                }
                                currentNode = walker.nextNode();
                            }
                        } catch (error) {}
                        return entries;
                    };
                    const panelCandidates = Array.from(
                        document.querySelectorAll("div, section, article, aside, [role='dialog'], [aria-modal='true'], [data-state='open']")
                    )
                        .filter((node) => visible(node))
                        .map((node) => {
                            const rect = node.getBoundingClientRect?.() || null;
                            const text = textOf(node);
                            return {
                                node: summarizeElement(node),
                                rect: summarizeRect(rect),
                                area: rect ? Math.round(rect.width * rect.height) : 0,
                                text: text.slice(0, 220),
                                time_token_count: (text.match(/\d{2}:\d{2}:\d{2}/g) || []).length,
                                trim_handle_count: node.querySelectorAll?.("img[src*='trim-handle']").length || 0,
                                media_count: node.querySelectorAll?.("video, img").length || 0,
                                add_to_prompt_count: text.includes("添加到提示") ? 1 : 0,
                                is_reference_picker_root: isReferencePickerRoot(node, references),
                                contains_active_dialog: !!(activeDialog && node.contains(activeDialog)),
                                inside_active_dialog: !!(activeDialog && activeDialog.contains(node)),
                            };
                        })
                        .filter((item) =>
                            item.time_token_count > 0 ||
                            item.trim_handle_count > 0 ||
                            item.media_count > 0 ||
                            item.add_to_prompt_count > 0 ||
                            item.is_reference_picker_root
                        )
                        .sort((left, right) => {
                            if (left.trim_handle_count !== right.trim_handle_count) {
                                return right.trim_handle_count - left.trim_handle_count;
                            }
                            if (left.time_token_count !== right.time_token_count) {
                                return right.time_token_count - left.time_token_count;
                            }
                            if (left.media_count !== right.media_count) {
                                return right.media_count - left.media_count;
                            }
                            return right.area - left.area;
                        })
                        .slice(0, 12);
                    const globalTrimHandles = Array.from(document.querySelectorAll("img[src*='trim-handle']"))
                        .slice(0, 16)
                        .map((node) => ({
                            src: String(node.getAttribute?.("src") || "").slice(0, 240),
                            rect: summarizeRect(node.getBoundingClientRect?.() || null),
                            parent_rect: summarizeRect(node.parentElement?.getBoundingClientRect?.() || null),
                            visible: visible(node),
                            inside_active_dialog: !!(activeDialog && activeDialog.contains(node)),
                            node: summarizeElement(node),
                            parent_node: summarizeElement(node.parentElement),
                        }));
                    return {
                        active_dialog: summarizeElement(activeDialog),
                        active_dialog_rect: summarizeRect(activeDialog?.getBoundingClientRect?.() || null),
                        panel_candidates: panelCandidates,
                        global_time_entries: collectGlobalTimeEntries(),
                        global_trim_handles: globalTrimHandles,
                    };
                };
                function collectVideoEditSurfaceState(activeDialog = null) {
                    const summarizeRect = (rect) =>
                        rect
                            ? {
                                left: Math.round(rect.left),
                                top: Math.round(rect.top),
                                width: Math.round(rect.width),
                                height: Math.round(rect.height),
                            }
                            : null;
                    const pathname = (() => {
                        try {
                            return String(new URL(String(location.href || "")).pathname || "");
                        } catch (_error) {
                            return String(location.pathname || "");
                        }
                    })();
                    const editButtons = Array.from(document.querySelectorAll("button, [role='button'], [tabindex]"))
                        .filter((node) => visible(node))
                        .map((node) => ({
                            text: textOf(node).trim(),
                            rect: summarizeRect(node.getBoundingClientRect?.() || null),
                            node: summarizeElement(node),
                        }))
                        .filter((entry) => {
                            const text = String(entry.text || "");
                            return (
                                text.includes("完成") ||
                                text.includes("返回项目") ||
                                text.includes("收藏") ||
                                text.includes("分享") ||
                                text.includes("下载")
                            );
                        })
                        .slice(0, 8);
                    const visibleMedia = Array.from(document.querySelectorAll("video, img"))
                        .filter((node) => visible(node))
                        .map((node) => {
                            const rect = node.getBoundingClientRect?.() || null;
                            return {
                                tag: String(node.tagName || "").toLowerCase(),
                                src: String(node.getAttribute?.("src") || "").slice(0, 240),
                                rect: summarizeRect(rect),
                                area: rect ? Math.round(rect.width * rect.height) : 0,
                                inside_active_dialog: !!(activeDialog && activeDialog.contains(node)),
                                node: summarizeElement(node),
                            };
                        })
                        .sort((left, right) => right.area - left.area)
                        .slice(0, 8);
                    const timeEntries = [];
                    try {
                        const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
                        let currentNode = walker.nextNode();
                        while (currentNode && timeEntries.length < 12) {
                            const raw = String(currentNode.textContent || "").trim();
                            if (/^\d{2}:\d{2}:\d{2}$/.test(raw)) {
                                const hostNode = currentNode.parentElement || currentNode.parentNode;
                                const hostRect = hostNode?.getBoundingClientRect?.() || null;
                                const containerNode =
                                    hostNode?.closest?.("div, section, article, aside, main, [role='dialog'], [aria-modal='true']") || null;
                                timeEntries.push({
                                    text: raw,
                                    host_rect: summarizeRect(hostRect),
                                    container_rect: summarizeRect(containerNode?.getBoundingClientRect?.() || null),
                                    inside_active_dialog: !!(activeDialog && hostNode && activeDialog.contains(hostNode)),
                                    host_node: summarizeElement(hostNode),
                                    container_node: summarizeElement(containerNode),
                                });
                            }
                            currentNode = walker.nextNode();
                        }
                    } catch (_error) {}
                    const trimHandles = Array.from(document.querySelectorAll("img[src*='trim-handle']"))
                        .map((node) => ({
                            src: String(node.getAttribute?.("src") || "").slice(0, 240),
                            rect: summarizeRect(node.getBoundingClientRect?.() || null),
                            parent_rect: summarizeRect(node.parentElement?.getBoundingClientRect?.() || null),
                            visible: visible(node),
                            inside_active_dialog: !!(activeDialog && activeDialog.contains(node)),
                            node: summarizeElement(node),
                        }))
                        .slice(0, 8);
                    const trackCandidates = Array.from(document.querySelectorAll("div, section, article, aside, main"))
                        .filter((node) => visible(node))
                        .map((node) => {
                            const rect = node.getBoundingClientRect?.() || null;
                            const text = textOf(node).trim();
                            return {
                                rect: summarizeRect(rect),
                                text: text.slice(0, 160),
                                child_count: Number(node.childElementCount || 0),
                                media_count: node.querySelectorAll?.("video, img").length || 0,
                                trim_handle_count: node.querySelectorAll?.("img[src*='trim-handle']").length || 0,
                                time_token_count: (text.match(/\d{2}:\d{2}:\d{2}/g) || []).length,
                                inside_active_dialog: !!(activeDialog && activeDialog.contains(node)),
                                node: summarizeElement(node),
                            };
                        })
                        .filter((entry) => {
                            const rect = entry.rect;
                            if (!rect) return false;
                            return (
                                rect.width >= 180 &&
                                rect.height >= 24 &&
                                rect.height <= 500 &&
                                (entry.time_token_count > 0 || entry.trim_handle_count > 0 || entry.media_count > 0)
                            );
                        })
                        .sort((left, right) => {
                            if (left.time_token_count !== right.time_token_count) {
                                return right.time_token_count - left.time_token_count;
                            }
                            if (left.trim_handle_count !== right.trim_handle_count) {
                                return right.trim_handle_count - left.trim_handle_count;
                            }
                            if (left.media_count !== right.media_count) {
                                return right.media_count - left.media_count;
                            }
                            return ((right.rect?.width || 0) * (right.rect?.height || 0)) - ((left.rect?.width || 0) * (left.rect?.height || 0));
                        })
                        .slice(0, 10);
                    return {
                        href: String(location.href || ""),
                        pathname,
                        is_edit_route: pathname.includes("/edit/"),
                        active_dialog: summarizeElement(activeDialog),
                        active_dialog_rect: summarizeRect(activeDialog?.getBoundingClientRect?.() || null),
                        edit_buttons: editButtons,
                        visible_media: visibleMedia,
                        time_entries: timeEntries,
                        trim_handles: trimHandles,
                        track_candidates: trackCandidates,
                    };
                }
                const resolveReferenceDetailScopeAfterSelection = (fallbackDialog = null) => {
                    const hasUsableRect = (node) => {
                        if (!node || !node.isConnected || !visible(node)) return false;
                        const rect = node.getBoundingClientRect?.() || null;
                        return !!rect && rect.width >= 80 && rect.height >= 80;
                    };
                    const activePickerDialog = resolveActiveReferenceDialog(references, null);
                    const panelCandidates = Array.from(
                        document.querySelectorAll("main, div, section, article, aside, [role='dialog'], [aria-modal='true'], [data-state='open']")
                    )
                        .filter((node) => hasUsableRect(node))
                        .map((node) => {
                            const rect = node.getBoundingClientRect?.() || null;
                            const text = textOf(node);
                            const timeTokenCount = (text.match(/\d{2}:\d{2}:\d{2}/g) || []).length;
                            const trimHandleCount = node.querySelectorAll?.("img[src*='trim-handle']").length || 0;
                            const addToPromptCount = text.includes("添加到提示") ? 1 : 0;
                            const mediaCount = node.querySelectorAll?.("video, img").length || 0;
                            const playIndicatorCount = (text.match(/play_circle/g) || []).length;
                            const detailSignalCount =
                                Number(timeTokenCount > 0) +
                                Number(trimHandleCount > 0) +
                                Number(addToPromptCount > 0) +
                                Number(mediaCount > 0) +
                                Number(playIndicatorCount > 0);
                            const isLikelyDetailPanel =
                                timeTokenCount >= 2 &&
                                mediaCount > 0 &&
                                (addToPromptCount > 0 || playIndicatorCount > 0);
                            return {
                                node,
                                rect,
                                area: rect ? Math.round(rect.width * rect.height) : 0,
                                timeTokenCount,
                                trimHandleCount,
                                addToPromptCount,
                                mediaCount,
                                playIndicatorCount,
                                detailSignalCount,
                                isLikelyDetailPanel,
                                isReferencePicker: isReferencePickerRoot(node, references),
                            };
                        })
                        .filter((item) =>
                            item.timeTokenCount > 0 ||
                            item.trimHandleCount > 0 ||
                            item.addToPromptCount > 0 ||
                            item.mediaCount > 0 ||
                            item.playIndicatorCount > 0 ||
                            item.isReferencePicker
                        )
                        .sort((left, right) => {
                            if (left.isLikelyDetailPanel !== right.isLikelyDetailPanel) {
                                return Number(right.isLikelyDetailPanel) - Number(left.isLikelyDetailPanel);
                            }
                            if (left.detailSignalCount !== right.detailSignalCount) {
                                return right.detailSignalCount - left.detailSignalCount;
                            }
                            if (left.timeTokenCount !== right.timeTokenCount) {
                                return right.timeTokenCount - left.timeTokenCount;
                            }
                            if (left.trimHandleCount !== right.trimHandleCount) {
                                return right.trimHandleCount - left.trimHandleCount;
                            }
                            if (left.addToPromptCount !== right.addToPromptCount) {
                                return right.addToPromptCount - left.addToPromptCount;
                            }
                            if (left.mediaCount !== right.mediaCount) {
                                return right.mediaCount - left.mediaCount;
                            }
                            if (left.playIndicatorCount !== right.playIndicatorCount) {
                                return right.playIndicatorCount - left.playIndicatorCount;
                            }
                            if (left.isReferencePicker !== right.isReferencePicker) {
                                return Number(left.isReferencePicker) - Number(right.isReferencePicker);
                            }
                            return left.area - right.area;
                        });
                    return panelCandidates[0]?.node || (hasUsableRect(activePickerDialog) ? activePickerDialog : (hasUsableRect(fallbackDialog) ? fallbackDialog : null));
                };
                const previewHasRenderableGeometry = (previewState) => {
                    const hasPrimaryMedia =
                        !!(previewState?.primary_media?.rect && previewState.primary_media.rect.width > 40 && previewState.primary_media.rect.height > 40);
                    const hasRenderableControl = Array.isArray(previewState?.controls) &&
                        previewState.controls.some((entry) => entry?.rect && entry.rect.width > 8 && entry.rect.height > 8);
                    const hasRenderableTimeline = Array.isArray(previewState?.timeline_candidates) &&
                        previewState.timeline_candidates.some((entry) => entry?.rect && entry.rect.width > 40 && entry.rect.height > 8);
                    const hasRenderableHandles = Array.isArray(previewState?.trim_handle_candidates) &&
                        previewState.trim_handle_candidates.some((entry) => {
                            const rect = entry?.resolved_handle_rect || entry?.parent_rect || entry?.rect;
                            return rect && rect.width > 6 && rect.height > 12;
                        });
                    const hasRenderableTimeNode = Array.isArray(previewState?.time_node_entries) &&
                        previewState.time_node_entries.some((entry) => entry?.rect && entry.rect.width > 20 && entry.rect.height > 10);
                    return hasPrimaryMedia || hasRenderableControl || hasRenderableTimeline || hasRenderableHandles || hasRenderableTimeNode;
                };
                const settleReferencePreviewAfterItemClick = async (currentDialog, candidateText) => {
                    let activeDialog = resolveReferenceDetailScopeAfterSelection(currentDialog) || currentDialog;
                    let previewState = collectReferencePreviewState(activeDialog);
                    const snapshots = [
                        {
                            attempt: 0,
                            href: String(location.href || ""),
                            dialog_node: summarizeElement(activeDialog),
                            renderable_geometry: previewHasRenderableGeometry(previewState),
                            has_frame_strip: previewHasFrameStrip(previewState),
                            preview_state: previewState,
                        },
                    ];
                    for (let attempt = 1; attempt <= 8; attempt += 1) {
                        if (previewHasRenderableGeometry(previewState) || previewHasFrameStrip(previewState)) {
                            break;
                        }
                        await sleep(attempt <= 3 ? 180 : 260);
                        activeDialog = resolveReferenceDetailScopeAfterSelection(currentDialog) || currentDialog;
                        previewState = collectReferencePreviewState(activeDialog);
                        snapshots.push({
                            attempt,
                            href: String(location.href || ""),
                            dialog_node: summarizeElement(activeDialog),
                            renderable_geometry: previewHasRenderableGeometry(previewState),
                            has_frame_strip: previewHasFrameStrip(previewState),
                            preview_state: previewState,
                        });
                    }
                    return {
                        dialog: activeDialog,
                        preview: previewState,
                    };
                };
                const reopenReferenceLibraryFromDetailView = async (dialog) => {
                    const activeDialog = resolveActiveReferenceDialog(references, dialog) || dialog;
                    if (!activeDialog) {
                        return dialog;
                    }
                    const dialogText = textOf(activeDialog);
                    const dialogCompact = dialogText.replace(/\s+/g, "").toLowerCase();
                    const selectionState = collectReferenceSelectionState(activeDialog);
                    const previewState = collectReferencePreviewState(activeDialog);
                    const hasReferenceMarker =
                        matchesReferenceText(dialogText, references) ||
                        (!!mediaIdHint && dialogCompact.includes(String(mediaIdHint).replace(/\s+/g, "").toLowerCase()));
                    const navCandidates = Array.from(activeDialog.querySelectorAll("button, [role='button']"))
                        .filter((node) => visible(node))
                        .map((node) => {
                            const text = textOf(node).trim();
                            const compact = text.replace(/\s+/g, "").toLowerCase();
                            return {
                                node,
                                text,
                                compact,
                            };
                        });
                    const backCandidate =
                        navCandidates.find((item) => item.compact.includes("arrow_back") || item.compact.includes("返回")) || null;
                    const dashboardCandidate =
                        navCandidates.find((item) => item.compact.includes("所有媒体内容") || item.compact.includes("dashboard")) || null;
                    const shouldReopen =
                        !hasReferenceMarker &&
                        selectionState.add_buttons.length > 0 &&
                        (previewState.time_nodes.length >= 2 || previewHasFrameStrip(previewState)) &&
                        !!(backCandidate || dashboardCandidate);
                    if (!shouldReopen) {
                        return activeDialog;
                    }
                    const navTarget = backCandidate || dashboardCandidate;
                    const navClick = clickReal(navTarget.node);
                    if (navClick?.clicked) {
                        await sleep(350);
                    }
                    return resolveActiveReferenceDialog(references, activeDialog) || activeDialog;
                };
                for (const initialDialog of dialogs) {
                    referenceDialogLoop:
                    for (let settleAttempt = 0; settleAttempt < 18; settleAttempt += 1) {
                        let dialog = resolveActiveReferenceDialog(references, initialDialog) || initialDialog;
                        if (settleAttempt >= 12) {
                            dialog = await reopenReferenceLibraryFromDetailView(dialog);
                        }
                        const libraryListScope = resolveReferenceLibraryListScope(dialog);
                        const candidates = buildCandidates(libraryListScope, "[role='option']");
                        for (const item of candidates) {
                            if (String(item.node?.getAttribute?.("role") || "").toLowerCase() !== "option") {
                                continue;
                            }
                            if (item.text.length > 220) continue;
                            scanned.push(item.text.slice(0, 120));
                            const matchesByText = matchesReferenceText(item.raw, references);
                            const matchesById = !!mediaIdHint && item.compact.includes(String(mediaIdHint).replace(/\s+/g, "").toLowerCase());
                            if (!matchesByText && !matchesById) continue;
                            if (!matchesById && item.repeated_reference_count > 1 && !looksLikeDirectReferenceLabel(item.compact_text || "")) continue;
                            const detailScopeBeforeClick = resolveReferenceDetailScopeAfterSelection(dialog) || dialog;
                            const previewBeforeClick = collectReferencePreviewState(detailScopeBeforeClick);
                            const selectionBeforeClick = collectReferenceSelectionState(detailScopeBeforeClick);
                            const candidateAlreadySelected = isReferenceCandidateAlreadySelected(item.node, dialog);
                            const selectedReferenceOptionMatched = hasMatchingSelectedReferenceOption(libraryListScope, references, mediaIdHint);
                            const dialogTextBeforeClick = textOf(dialog);
                            const dialogLevelDetailReady =
                                matchesReferenceText(dialogTextBeforeClick, references) &&
                                dialogTextBeforeClick.includes("添加到提示") &&
                                ((dialogTextBeforeClick.match(/\d{2}:\d{2}:\d{2}/g) || []).length >= 2);
                            const existingDetailReady = dialogAlreadyShowsReferenceDetail(detailScopeBeforeClick, previewBeforeClick, selectionBeforeClick, references);
                            const canReuseExistingDetail =
                                (
                                    selectedReferenceOptionMatched ||
                                    candidateAlreadySelected ||
                                    existingDetailReady ||
                                    dialogLevelDetailReady
                                ) &&
                                (
                                    selectedReferenceOptionMatched ||
                                    selectionBeforeClick.add_buttons.length > 0 ||
                                    dialogLevelDetailReady ||
                                    previewBeforeClick.time_nodes.length >= 2 ||
                                    previewHasFrameStrip(previewBeforeClick) ||
                                    previewHasRenderableGeometry(previewBeforeClick)
                                );
                            // #endregion
                            if (!canReuseExistingDetail) {
                                const clickTarget = resolveReferenceCandidateClickTarget(item.node) || item.node;
                                const candidateClick = clickReal(clickTarget, { lockTarget: true });
                                await sleep(300);
                            } else {
                                dialog = detailScopeBeforeClick;
                            }
                            const settledPreviewAfterClick = await settleReferencePreviewAfterItemClick(dialog, item.text);
                            if (String(location.pathname || "").includes("/edit/")) {
                                const backToProjectButton = Array.from(document.querySelectorAll("button, [role='button']"))
                                    .filter((node) => visible(node))
                                    .find((node) => {
                                        const compact = textOf(node).replace(/\s+/g, "").toLowerCase();
                                        return compact.includes("返回项目") || compact.includes("arrow_back");
                                    }) || null;
                                const backToProjectClick = backToProjectButton ? clickReal(backToProjectButton) : null;
                                if (backToProjectClick?.clicked) {
                                    await sleep(500);
                                }
                                continue referenceDialogLoop;
                            }
                            dialog = settledPreviewAfterClick.dialog || resolveReferenceDetailScopeAfterSelection(dialog) || dialog;
                            const selectionAfterItemClick = collectReferenceSelectionState(dialog);
                            const previewAfterItemClick = settledPreviewAfterClick.preview || collectReferencePreviewState(dialog);
                            // #endregion
                            const dialogResolver = () =>
                                resolveReferenceDetailScopeAfterSelection(dialog) ||
                                resolveActiveReferenceDialog(references, dialog) ||
                                dialog;
                            const timeRangeRequested =
                                Number.isFinite(Number(requestedStartSeconds)) || Number.isFinite(Number(requestedEndSeconds));
                            const rangeApply = timeRangeRequested
                                ? await applyReferenceTimeRange(
                                      dialog,
                                      Number.isFinite(Number(requestedStartSeconds)) ? Number(requestedStartSeconds) : null,
                                      Number.isFinite(Number(requestedEndSeconds)) ? Number(requestedEndSeconds) : null,
                                      Number.isFinite(Number(requestedSourceDurationSeconds)) ? Number(requestedSourceDurationSeconds) : null,
                                      previewAfterItemClick,
                                      dialogResolver
                                  )
                                : await applyReferenceFrameRange(
                                      dialog,
                                      Number.isFinite(Number(requestedStartFrameIndex)) ? Number(requestedStartFrameIndex) : null,
                                      Number.isFinite(Number(requestedEndFrameIndex)) ? Number(requestedEndFrameIndex) : null,
                                      previewAfterItemClick,
                                      dialogResolver
                                  );
                            // #endregion
                            const previewAfterRange = rangeApply?.preview_after || previewAfterItemClick;
                            const frameRangeRequested =
                                Number.isFinite(Number(requestedStartFrameIndex)) || Number.isFinite(Number(requestedEndFrameIndex));
                            const rangeRequested = timeRangeRequested || frameRangeRequested;
                            const rangeConfirmed =
                                !rangeRequested ||
                                !!rangeApply?.within_tolerance;
                            if (!rangeConfirmed) {
                                continue;
                            }
                            const targetSelectedAfterItemClick = selectionAfterItemClick.selected_nodes.some((entry) => {
                                const selectedText = `${entry.text || ""} ${entry.node?.text || ""}`;
                                const selectedCompact = String(selectedText || "").replace(/\s+/g, "").toLowerCase();
                                return (
                                    matchesReferenceText(selectedText, references) ||
                                    (!!mediaIdHint && selectedCompact.includes(String(mediaIdHint).replace(/\s+/g, "").toLowerCase()))
                                );
                            });
                            const addToPromptLabels = [];
                            const addToPromptWalker = document.createTreeWalker(dialog, NodeFilter.SHOW_TEXT);
                            let addToPromptTextNode = addToPromptWalker.nextNode();
                            while (addToPromptTextNode) {
                                const exactText = String(addToPromptTextNode.textContent || "").trim();
                                if (exactText === "添加到提示") {
                                    const parentNode = addToPromptTextNode.parentElement;
                                    if (parentNode && visible(parentNode)) {
                                        addToPromptLabels.push({
                                            node: parentNode,
                                            text: exactText,
                                        });
                                    }
                                }
                                addToPromptTextNode = addToPromptWalker.nextNode();
                            }
                            const addToPromptButtons = addToPromptLabels
                                .map((entry) => entry.node.closest?.("button, [role='button']"))
                                .filter((node, index, arr) => !!node && arr.indexOf(node) === index)
                                .map((node) => ({
                                    node,
                                    text: textOf(node).trim(),
                                    disabled: !!node.disabled || String(node.getAttribute?.("aria-disabled") || "") === "true",
                                }));
                            const addToPromptButton = addToPromptButtons.find((entry) => !entry.disabled)?.node || addToPromptButtons[0]?.node || null;
                            const enteredReferenceEditRoute = String(location.pathname || "").includes("/edit/");
                            const addToPromptClick =
                                !enteredReferenceEditRoute && !requestedDebugSkipAddToPrompt && addToPromptButton
                                    ? clickReal(addToPromptButton)
                                    : null;
                            if (addToPromptClick?.clicked) {
                                await sleep(350);
                            }
                            const selectionAfterAddClick = collectReferenceSelectionState(dialog);
                            const promptAttachmentsAfterAdd = collectPromptAttachmentState();
                            const promptRegionAfterAdd = collectPromptRegionState();
                            const primaryAction = targetSelectedAfterItemClick
                                ? (matchesById ? "select:reference-media-id" : "select:reference-video")
                                : (matchesById ? "click:reference-media-id" : "click:reference-text");
                            const addAction = enteredReferenceEditRoute
                                ? "route:auto-enter-edit"
                                : addToPromptClick?.clicked
                                    ? "click:add-to-prompt"
                                    : (requestedDebugSkipAddToPrompt ? "hold:skip-add-to-prompt" : "");
                            if (Number.isFinite(Number(requestedDebugHoldReferencePickerMs)) && Number(requestedDebugHoldReferencePickerMs) > 0) {
                                await sleep(Number(requestedDebugHoldReferencePickerMs));
                            }
                            // #endregion
                            return {
                                action: primaryAction,
                                add_action: addAction,
                                debug_hold_open: !!requestedDebugSkipAddToPrompt || Number(requestedDebugHoldReferencePickerMs) > 0,
                                matched_text: item.text.slice(0, 200),
                                scanned: scanned.slice(0, 20),
                                range_apply: rangeApply,
                                preview_after_range: previewAfterRange,
                            };
                        }
                        if (settleAttempt < 17) {
                            await sleep(settleAttempt < 4 ? 250 : (settleAttempt < 10 ? 400 : 600));
                        }
                    }
                }
                // #endregion
                if (Number.isFinite(Number(requestedDebugHoldReferencePickerMs)) && Number(requestedDebugHoldReferencePickerMs) > 0) {
                    await sleep(Number(requestedDebugHoldReferencePickerMs));
                }
                return {
                    action: "",
                    matched_text: "",
                    scanned: scanned.slice(0, 20),
                };
            };
            const normalizeVideoCreationMode = async () => {
                const surfaceReset = await ensureVideoComposerSurface();
                const before = parsePromptState();
                let bootstrapAction = "";
                let secondaryAction = "";
                let referencePick = null;
                let desiredSettingsApply = null;
                // #endregion
                // #endregion
                if (targetGenerationModeOnPage === "image") {
                    if (!before.promptMode || before.promptMode !== "IMAGE") {
                        const imageTabClick = clickImageTab();
                        if (imageTabClick) {
                            await sleep(250);
                        }
                        const modelToggleClick = imageTabClick ? "" : await openModeMenuAndSelectTarget("image");
                        bootstrapAction = imageTabClick || modelToggleClick || "";
                    }
                } else if (!before.promptMode || before.promptMode === "IMAGE") {
                    const videoTabClick = clickVideoTab();
                    if (videoTabClick) {
                        await sleep(250);
                    }
                    const modelToggleClick = videoTabClick ? "" : await openModeMenuAndSelectTarget("video");
                    const afterVideoSwitch = parsePromptState();
                    let desiredSubmodeClick = "";
                    if (afterVideoSwitch.promptMode && afterVideoSwitch.promptMode !== "IMAGE") {
                        desiredSubmodeClick =
                            clickVideoSubmode(targetSubmode || "文本") ||
                            clickVideoSubmode("文本") ||
                            clickVideoSubmode("提示词");
                    }
                    bootstrapAction = videoTabClick || modelToggleClick || desiredSubmodeClick || "";
                } else if (before.promptMode === "VIDEO_REFERENCES") {
                    bootstrapAction = String(targetSubmode || "") === "素材"
                        ? ""
                        : (
                            clickVideoSubmode(targetSubmode || "文本") ||
                            clickVideoSubmode("文本") ||
                            clickVideoSubmode("提示词") ||
                            ""
                        );
                    if (String(targetSubmode || "") !== "素材" && parsePromptState().promptMode === "VIDEO_REFERENCES") {
                        const modelMenuAction = await openModeMenuAndSelectTarget("video");
                        if (modelMenuAction) {
                            secondaryAction = modelMenuAction;
                            await sleep(250);
                        }
                    }
                    if (String(targetSubmode || "") !== "素材" && parsePromptState().promptMode === "VIDEO_REFERENCES") {
                        const textModeClick = clickFirstButton(
                            [
                                (text) => text.includes("帧"),
                                (text) => text.includes("文本"),
                                (text) => text.includes("提示词"),
                                (text) => text.toLowerCase().includes("text"),
                            ],
                            "click:video-text-mode"
                        );
                        if (textModeClick) {
                            secondaryAction = secondaryAction || textModeClick;
                            await sleep(250);
                        }
                    }
                    if (parsePromptState().promptMode === "VIDEO_REFERENCES") {
                        const targetModeClick =
                            clickVideoSubmode(targetSubmode || "文本") ||
                            clickVideoSubmode("文本") ||
                            clickVideoSubmode("提示词");
                        secondaryAction = secondaryAction || targetModeClick || "";
                    }
                } else if (before.promptMode === "VIDEO_FRAMES" && String(targetSubmode || "") === "素材") {
                    bootstrapAction = clickVideoSubmode("素材") || "";
                }
                if (targetGenerationModeOnPage === "video") {
                    // #endregion
                    desiredSettingsApply = await applyDesiredVideoSettings();
                    // #endregion
                    await sleep(120);
                    // #endregion
                }
                if (String(targetSubmode || "") === "素材") {
                    // #endregion
                    await sleep(250);
                    // #endregion
                    referencePick = await selectVideoReferenceFromLibrary(
                        sourceReferenceTexts,
                        sourceReferenceMediaId,
                        Number.isFinite(Number(requestedStartFrameIndex)) ? Number(requestedStartFrameIndex) : null,
                        Number.isFinite(Number(requestedEndFrameIndex)) ? Number(requestedEndFrameIndex) : null,
                        Number.isFinite(Number(requestedDebugHoldReferencePickerMs)) ? Number(requestedDebugHoldReferencePickerMs) : 0,
                        !!requestedDebugSkipAddToPrompt
                    );
                    if (referencePick?.add_action || referencePick?.action) {
                        secondaryAction = secondaryAction || referencePick.add_action || referencePick.action;
                    }
                }
                await sleep(300);
                // #endregion
                return {
                    before,
                    after: parsePromptState(),
                    action: surfaceReset.action || bootstrapAction,
                    secondary_action: secondaryAction || surfaceReset.secondary_action,
                    desired_settings_apply: desiredSettingsApply,
                    reference_pick: referencePick,
                    surface_reset: surfaceReset,
                };
            };
            const getSubmitButton = () => {
                const buttons = Array.from(
                    document.querySelectorAll("button, [role='button'], input[type='button'], input[type='submit']")
                ).filter((node) => visible(node));
                const strictSubmit = buttons.find((node) => classifyButtonKind(node) === "submit");
                if (strictSubmit) return strictSubmit;
                const fallbackSubmitCandidates = buttons
                    .map((node) => {
                        const text = textOf(node);
                        const icon = getButtonIconText(node);
                        const raw = `${text} ${icon} ${String(node.getAttribute?.("aria-label") || "")}`.toLowerCase();
                        if (!raw.includes("创建") && !raw.includes("generate")) return null;
                        if (raw.includes("添加媒体")) return null;
                        if (raw.includes("查看设置") || raw.includes("settings")) return null;
                        if (raw.includes("排序和过滤") || raw.includes("filter")) return null;
                        if (raw.includes("更多")) return null;
                        if (!!node.closest?.("[role='dialog'], [aria-modal='true'], nav, aside, [role='navigation']")) return null;
                        const rect = node.getBoundingClientRect?.();
                        if (!rect || rect.bottom < window.innerHeight * 0.7) return null;
                        return {
                            node,
                            rect,
                            rightScore: window.innerWidth - rect.right,
                            bottomScore: window.innerHeight - rect.bottom,
                        };
                    })
                    .filter(Boolean)
                    .sort((a, b) => a.bottomScore - b.bottomScore || a.rightScore - b.rightScore);
                return fallbackSubmitCandidates[0]?.node || null;
            };
            const isRecoverableWrongSurface = (promptTarget) => {
                const promptState = parsePromptState();
                if (!promptTarget || isConversationLikeTarget(promptTarget)) {
                    return true;
                }
                if (!promptState.promptMode || promptState.promptMode === "IMAGE") {
                    return true;
                }
                const dialogsText = Array.from(document.querySelectorAll("[role='dialog'], [aria-modal='true']")).filter((node) => visible(node))
                    .map((node) => textOf(node))
                    .join(" | ");
                if (dialogsText.includes("图片") && dialogsText.includes("视频") && dialogsText.includes("上传媒体")) {
                    return true;
                }
                const slateState = summarizeSlateState(promptTarget);
                const hasPlaceholders = Array.isArray(slateState?.placeholders) && slateState.placeholders.length > 0;
                const hasSlateStrings = Array.isArray(slateState?.slate_strings) && slateState.slate_strings.length > 0;
                if (hasPlaceholders && !hasSlateStrings) {
                    return true;
                }
                return false;
            };
            const clickReal = (node, options = {}) => {
                if (!node || typeof node.getBoundingClientRect !== "function") {
                    return { clicked: false };
                }
                const nativeClick = options.nativeClick !== false;
                const lockTarget = options.lockTarget === true;
                const rect = node.getBoundingClientRect();
                const x = Math.round(rect.left + rect.width / 2);
                const y = Math.round(rect.top + rect.height / 2);
                const hit = document.elementFromPoint(x, y);
                const target = lockTarget ? node : (hit && node.contains(hit) ? hit : node);
                const common = {
                    bubbles: true,
                    cancelable: true,
                    composed: true,
                    clientX: x,
                    clientY: y,
                    button: 0,
                };
                for (const type of ["pointerdown", "mousedown", "pointerup", "mouseup", "click"]) {
                    try {
                        const EventCtor = type.startsWith("pointer") ? PointerEvent : MouseEvent;
                        target.dispatchEvent(new EventCtor(type, common));
                    } catch (error) {
                        target.dispatchEvent(new MouseEvent("click", common));
                        break;
                    }
                }
                if (nativeClick && typeof target.click === "function") {
                    target.click();
                }
                return {
                    clicked: true,
                    native_click: nativeClick,
                    lock_target: lockTarget,
                    x,
                    y,
                    target: summarizeElement(target),
                    hit: summarizeElement(hit),
                };
            };
            const clickAtPoint = (x, y, options = {}) => {
                const roundedX = Math.round(x);
                const roundedY = Math.round(y);
                const hit = document.elementFromPoint(roundedX, roundedY);
                if (!hit) {
                    return { clicked: false, x: roundedX, y: roundedY, target: null, hit: null };
                }
                const nativeClick = options.nativeClick !== false;
                const common = {
                    bubbles: true,
                    cancelable: true,
                    composed: true,
                    clientX: roundedX,
                    clientY: roundedY,
                    button: 0,
                };
                for (const type of ["pointerdown", "mousedown", "pointerup", "mouseup", "click"]) {
                    try {
                        const EventCtor = type.startsWith("pointer") ? PointerEvent : MouseEvent;
                        hit.dispatchEvent(new EventCtor(type, common));
                    } catch (error) {
                        hit.dispatchEvent(new MouseEvent("click", common));
                        break;
                    }
                }
                if (nativeClick && typeof hit.click === "function") {
                    hit.click();
                }
                return {
                    clicked: true,
                    native_click: nativeClick,
                    x: roundedX,
                    y: roundedY,
                    target: summarizeElement(hit),
                    hit: summarizeElement(hit),
                };
            };
            const collectButtons = () =>
                Array.from(document.querySelectorAll("button, [role='button'], input[type='button'], input[type='submit']"))
                    .filter((node) => visible(node))
                    .map((node) => ({
                        text: textOf(node).slice(0, 160),
                        icon: getButtonIconText(node).slice(0, 80),
                        disabled: !!node.disabled || node.getAttribute("aria-disabled") === "true",
                        aria_disabled: String(node.getAttribute("aria-disabled") || ""),
                        kind: classifyButtonKind(node),
                    }))
                    .filter((item) => item.kind !== "other");
            const summarizeSelection = () => {
                const selection = window.getSelection?.();
                if (!selection) return null;
                const anchorElement =
                    selection.anchorNode?.nodeType === Node.ELEMENT_NODE
                        ? selection.anchorNode
                        : selection.anchorNode?.parentElement || null;
                const focusElement =
                    selection.focusNode?.nodeType === Node.ELEMENT_NODE
                        ? selection.focusNode
                        : selection.focusNode?.parentElement || null;
                return {
                    text: String(selection.toString() || "").slice(0, 160),
                    range_count: selection.rangeCount,
                    is_collapsed: selection.isCollapsed,
                    anchor_offset: selection.anchorOffset,
                    focus_offset: selection.focusOffset,
                    anchor_element: summarizeElement(anchorElement),
                    focus_element: summarizeElement(focusElement),
                };
            };
            const collectStatus = (label, promptTarget) => {
                const bodyText = String(document.body?.innerText || "").replace(/\s+/g, " ").trim();
                const submitButton = getSubmitButton();
                const dialogsText = Array.from(document.querySelectorAll("[role='dialog'], [aria-modal='true']")).filter((node) => visible(node))
                    .map((node) => textOf(node))
                    .filter(Boolean)
                    .join(" | ")
                    .slice(0, 800);
                return {
                    label,
                    surface_kind: promptTarget
                        ? (isConversationLikeTarget(promptTarget) ? "conversation_drawer" : "generation")
                        : "missing",
                    prompt_state: parsePromptState(),
                    prompt_target: summarizeElement(promptTarget),
                    prompt_slate: summarizeSlateState(promptTarget),
                    flow_prompt_box_state: summarizeFlowPromptBoxState(),
                    prompt_dom: promptTarget
                        ? {
                            text: String(promptTarget.innerText || promptTarget.textContent || "").slice(0, 240),
                            html: String(promptTarget.innerHTML || "").slice(0, 1200),
                            child_count: Number(promptTarget.childNodes?.length || 0),
                        }
                        : null,
                    active_element: summarizeElement(document.activeElement),
                    selection: summarizeSelection(),
                    submit_button: summarizeElement(submitButton),
                    create_buttons: collectButtons(),
                    body_error_hint:
                        bodyText.includes("必须提供提示")
                            ? "必须提供提示"
                            : bodyText.includes("异常活动")
                                ? "异常活动"
                                : bodyText.includes("Application error")
                                    ? "Application error"
                                : "",
                    dialogs_text: dialogsText,
                    body_text_prefix: bodyText.slice(0, 260),
                    frontend_errors: frontendErrors.slice(-8),
                };
            };
            const installSubmitEventProbe = (submitButton, promptTarget) => {
                const startedAt = Date.now();
                const events = [];
                const removers = [];
                const addProbe = (node, source, type) => {
                    if (!node?.addEventListener) return;
                    const handler = (event) => {
                        if (events.length >= 40) return;
                        events.push({
                            source,
                            type,
                            elapsed_ms: Date.now() - startedAt,
                            target: summarizeElement(event?.target),
                            active_element: summarizeElement(document.activeElement),
                        });
                    };
                    node.addEventListener(type, handler, true);
                    removers.push(() => {
                        try {
                            node.removeEventListener(type, handler, true);
                        } catch (_error) {}
                    });
                };
                ["pointerdown", "mousedown", "pointerup", "mouseup", "click"].forEach((type) => addProbe(submitButton, "submit_button", type));
                ["click"].forEach((type) => addProbe(document, "document", type));
                ["blur", "focus", "input", "change"].forEach((type) => addProbe(promptTarget, "prompt_target", type));
                return {
                    snapshot: () => ({
                        elapsed_ms: Date.now() - startedAt,
                        events: events.slice(0, 40),
                    }),
                    dispose: () => removers.forEach((fn) => fn()),
                };
            };
            const findConversationDrawer = () =>
                Array.from(document.querySelectorAll("aside, section, div"))
                    .filter((node) => visible(node))
                    .map((node) => ({
                        node,
                        rect: node.getBoundingClientRect(),
                        text: textOf(node),
                    }))
                    .filter(({ rect, text, node }) =>
                        rect.left > window.innerWidth * 0.5 &&
                        rect.width > 220 &&
                        rect.width < window.innerWidth * 0.42 &&
                        rect.height > window.innerHeight * 0.45 &&
                        (
                            text.toLowerCase().includes("i've generated") ||
                            text.toLowerCase().includes("let me know if") ||
                            text.toLowerCase().includes("未命名的会话") ||
                            text.toLowerCase().includes("conversation") ||
                            !!node.querySelector("textarea, [contenteditable='true'], input[type='text']")
                        )
                    )
                    .sort((a, b) => (b.rect.width * b.rect.height) - (a.rect.width * a.rect.height))[0]?.node || null;
            const closeConversationDrawer = async () => {
                const drawer = findConversationDrawer();
                if (!drawer) return "";
                const drawerRect = drawer.getBoundingClientRect();
                const closeButton = Array.from(drawer.querySelectorAll("button, [role='button']"))
                    .filter((node) => visible(node))
                    .find((node) => {
                        const rect = node.getBoundingClientRect();
                        const raw = `${textOf(node)} ${String(node.getAttribute?.("aria-label") || "")}`.toLowerCase();
                        return (
                            rect.top < drawerRect.top + 80 &&
                            rect.left > drawerRect.right - 96 &&
                            (raw.includes("close") || raw.includes("关闭") || raw === "" || raw === "x")
                        );
                    });
                if (!closeButton) return "drawer_detected_without_close";
                clickReal(closeButton);
                await sleep(350);
                return "drawer_closed";
            };
            const dismissBannerIfPresent = async () => {
                const bannerMatchers = [
                    (text) => text.includes("OK, got it"),
                    (text) => text.includes("Agree"),
                    (text) => text.includes("知道了"),
                    (text) => text.includes("同意"),
                ];
                const findBannerButton = () =>
                    Array.from(document.querySelectorAll("button, [role='button']"))
                        .filter((node) => visible(node))
                        .map((node) => ({
                            node,
                            text: textOf(node),
                        }))
                        .filter((item) => item.text && bannerMatchers.some((matcher) => matcher(item.text)))
                        .sort((a, b) => {
                            const aExact = a.text === "OK, got it" ? 0 : 1;
                            const bExact = b.text === "OK, got it" ? 0 : 1;
                            return aExact - bExact || a.text.length - b.text.length;
                        })[0] || null;
                let action = "";
                for (let attempt = 0; attempt < 3; attempt += 1) {
                    const candidate = findBannerButton();
                    if (!candidate) break;
                    const click = clickReal(candidate.node, { nativeClick: true });
                    action = `banner:${candidate.text.slice(0, 80)}`;
                    await sleep(250);
                    if (!findBannerButton()) {
                        return action;
                    }
                    if (!click?.clicked) {
                        break;
                    }
                    try {
                        document.dispatchEvent(
                            new KeyboardEvent("keydown", { key: "Escape", bubbles: true, cancelable: true })
                        );
                    } catch (_error) {}
                    await sleep(150);
                }
                if (!action) {
                    action =
                        clickFirstButton(
                            bannerMatchers,
                            "banner"
                        ) || "";
                    if (action) {
                        await sleep(250);
                    }
                }
                return action;
            };

            try {
                let promptTarget = findPromptTarget();
                const result = {
                    result_version: "video_ui_submit_v3",
                    build_marker: String(buildMarker || ""),
                    submit_mode: mode,
                    preferred_submode: String(targetSubmode || ""),
                    reference_texts: Array.isArray(sourceReferenceTexts) ? sourceReferenceTexts.slice(0, 8) : [],
                    reference_media_id: String(sourceReferenceMediaId || ""),
                    desired_video_settings: desiredVideoSettings,
                    href: String(location.href || ""),
                    title: String(document.title || ""),
                    readyState: String(document.readyState || ""),
                    prompt_requested: promptValue,
                    typed: false,
                    clicked: false,
                    click_debug: null,
                    banner_reset: "",
                    agent_reset: null,
                    mode_normalize: null,
                    stage,
                    steps: [],
                };

                stage = "collect_initial";
                result.stage = stage;
                result.banner_reset = await withTimeout("banner_reset", () => dismissBannerIfPresent(), 1500);
                result.drawer_reset = await withTimeout("drawer_reset", () => closeConversationDrawer(), 2000);
                promptTarget = findPromptTarget();
                result.steps.push(collectStatus("initial", promptTarget));

                if (mode !== "click_only") {
                    stage = "agent_reset";
                    result.stage = stage;
                    try {
                        result.agent_reset = await withTimeout("agent_reset", () => resetAgentMode(), 3500);
                    } catch (error) {
                        result.agent_reset = {
                            error: String(error?.message || error),
                        };
                    }
                    promptTarget = findPromptTarget();
                    result.steps.push(collectStatus("after_agent_reset", promptTarget));
                    stage = "mode_normalize";
                    result.stage = stage;
                    const modeNormalizeTimeoutMs = Math.max(
                        8000,
                        (Number.isFinite(Number(requestedDebugHoldReferencePickerMs)) ? Number(requestedDebugHoldReferencePickerMs) : 0) + 20000
                    );
                    result.mode_normalize = await withTimeout("mode_normalize", () => normalizeVideoCreationMode(), modeNormalizeTimeoutMs);
                    promptTarget = findPromptTarget();
                    result.steps.push(collectStatus("after_mode_normalize", promptTarget));
                    if (result.mode_normalize?.reference_pick?.debug_hold_open) {
                        stage = "reference_picker_hold";
                        result.stage = stage;
                        result.steps.push(collectStatus("reference_picker_hold", promptTarget));
                        result.best_submit_candidate = summarizeElement(getSubmitButton());
                        result.frontend_errors = frontendErrors.slice(-12);
                        return finalize(result);
                    }
                } else if (isRecoverableWrongSurface(promptTarget)) {
                    stage = "click_only_agent_reset";
                    result.stage = stage;
                    try {
                        result.agent_reset = await withTimeout("click_only_agent_reset", () => resetAgentMode(), 3500);
                    } catch (error) {
                        result.agent_reset = {
                            error: String(error?.message || error),
                        };
                    }
                    promptTarget = findPromptTarget();
                    result.steps.push(collectStatus("after_click_only_agent_reset", promptTarget));
                    stage = "click_only_mode_normalize";
                    result.stage = stage;
                    result.mode_normalize = await withTimeout("click_only_mode_normalize", () => normalizeVideoCreationMode(), 5000);
                    promptTarget = findPromptTarget();
                    result.steps.push(collectStatus("after_click_only_normalize", promptTarget));
                }

                if (mode !== "click_only" && promptTarget && promptValue) {
                    stage = "type_text";
                    result.stage = stage;
                    // #endregion
                    result.typed = await withTimeout("type_text", () => typeText(promptTarget, promptValue), 5000);
                    stage = "post_type_wait";
                    result.stage = stage;
                    await withTimeout("post_type_wait", () => sleep(250), 1000);
                    // #endregion
                    result.steps.push(collectStatus("after_type", promptTarget));
                    stage = "post_type_snapshot_wait";
                    result.stage = stage;
                    await withTimeout("post_type_snapshot_wait", () => sleep(400), 1200);
                    // Avoid blurring Slate during the staged submit path: on the
                    // live page this can duplicate text and crash the Next.js app
                    // before we get a stable submit-button snapshot.
                    result.steps.push(collectStatus("after_short_wait", promptTarget));
                } else if (mode !== "click_only") {
                    result.steps.push(collectStatus("missing_prompt_target", promptTarget));
                }
                if (mode === "click_only" && promptTarget && promptValue) {
                    const currentSlate = summarizeSlateState(promptTarget);
                    const currentSubmit = getSubmitButton();
                    const hasSlateStrings =
                        Array.isArray(currentSlate?.slate_strings) && currentSlate.slate_strings.some((item) => String(item || "").trim());
                    const promptText = textOf(promptTarget);
                    const normalizedPromptText = promptText.replace("您希望创作什么内容？", "").replace(/\s+/g, " ").trim();
                    const normalizedRequested = String(promptValue || "").replace(/\s+/g, " ").trim();
                    const promptLooksPresent = !!normalizedPromptText && normalizedPromptText === normalizedRequested;
                    const needsRetype =
                        (!hasSlateStrings && !promptLooksPresent) ||
                        !promptText ||
                        (promptText.includes("您希望创作什么内容？") && !promptLooksPresent) ||
                        ((!!currentSubmit?.disabled || currentSubmit?.getAttribute?.("aria-disabled") === "true") && !promptLooksPresent) ||
                        currentSubmit?.getAttribute?.("aria-disabled") === "true";
                    if (needsRetype) {
                        stage = "click_only_retype";
                        result.stage = stage;
                        result.typed = (await withTimeout("click_only_retype", () => typeText(promptTarget, promptValue), 5000)) || result.typed;
                        stage = "click_only_retype_wait";
                        result.stage = stage;
                        await withTimeout("click_only_retype_wait", () => sleep(250), 1000);
                        promptTarget = findPromptTarget();
                        result.steps.push(collectStatus("after_click_only_retype", promptTarget));
                    }
                }

                const submitButton = getSubmitButton();
                // #endregion
                const allowClick = mode !== "type_only";
                const clickBlockedBySurface = allowClick && isRecoverableWrongSurface(promptTarget);
                if (allowClick && !clickBlockedBySurface && submitButton && !(submitButton.disabled || submitButton.getAttribute("aria-disabled") === "true")) {
                    stage = "submit_click";
                    result.stage = stage;
                    if (requestedDebugVideoEditSubmitHookConfig?.enabled && typeof createFlowVideoEditSubmitHook === "function") {
                        try {
                            submitRequestHook = createFlowVideoEditSubmitHook(requestedDebugVideoEditSubmitHookConfig);
                        } catch (error) {
                            submitRequestHook = {
                                snapshot: () => ({
                                    install_error: String(error?.stack || error?.message || error).slice(0, 600),
                                }),
                                dispose: () => {},
                            };
                        }
                    }
                    const submitEventProbe = installSubmitEventProbe(submitButton, promptTarget);
                    // #endregion
                    result.click_debug = clickReal(submitButton, { nativeClick: false });
                    result.clicked = !!result.click_debug?.clicked;
                    // #endregion
                    await withTimeout("after_submit_probe_short", () => sleep(80), 300);
                    // #endregion
                    await withTimeout("after_submit_probe_medium", () => sleep(220), 600);
                    // #endregion
                    stage = "after_submit_click_wait";
                    result.stage = stage;
                    await withTimeout("after_submit_click_wait", () => sleep(400), 1200);
                    result.steps.push(collectStatus("after_submit_click", promptTarget));
                    stage = "after_submit_settle_wait";
                    result.stage = stage;
                    await withTimeout("after_submit_settle_wait", () => sleep(1200), 2500);
                    result.steps.push(collectStatus("after_submit_wait", promptTarget));
                    result.submit_request_hook = submitRequestHook?.snapshot?.() || null;
                    submitEventProbe.dispose();
                    submitRequestHook?.dispose?.();
                    submitRequestHook = null;
                } else if (clickBlockedBySurface) {
                    stage = "skip_click_wrong_surface";
                    result.stage = stage;
                    result.steps.push(collectStatus("skip_click_wrong_surface", promptTarget));
                } else if (!allowClick) {
                    stage = "skip_click_type_only";
                    result.stage = stage;
                    // #endregion
                    result.steps.push(collectStatus("skip_click_type_only", promptTarget));
                } else {
                    stage = "submit_not_clickable";
                    result.stage = stage;
                    result.steps.push(collectStatus("submit_not_clickable", promptTarget));
                }

                stage = "finalize";
                result.stage = stage;
                if (!result.submit_request_hook && submitRequestHook?.snapshot) {
                    result.submit_request_hook = submitRequestHook.snapshot();
                }
                result.best_submit_candidate = summarizeElement(getSubmitButton());
                result.frontend_errors = frontendErrors.slice(-12);
                return finalize(result);
            } catch (error) {
                return finalize({
                    result_version: "video_ui_submit_v3",
                    build_marker: String(buildMarker || ""),
                    submit_mode: mode,
                    preferred_submode: String(targetSubmode || ""),
                    href: String(location.href || ""),
                    title: String(document.title || ""),
                    readyState: String(document.readyState || ""),
                    prompt_requested: promptValue,
                    stage,
                    probe_error: `runVideoUiSubmit_exception:${String(error?.stack || error?.message || error)}`.slice(0, 1200),
                    frontend_errors: frontendErrors.slice(-12),
                });
            } finally {
                submitRequestHook?.dispose?.();
                window.removeEventListener("error", onWindowError);
                window.removeEventListener("unhandledrejection", onUnhandledRejection);
                console.error = originalConsoleError;
            }
                },
            }),
            sleep(String(preferredSubmode || "") === "素材" ? 75000 : 45000).then(() => {
                throw new Error("executeScript_timeout");
            }),
        ]);
    } catch (error) {
        return {
            result_version: "video_ui_submit_v3",
            build_marker: String(EXTENSION_BUILD_MARKER || ""),
            submit_mode: submitMode,
            tab_id: tabId,
            probe_error: `worker_executeScript_failed:${String(error?.message || error)}`.slice(0, 240),
        };
    }
    const firstResult = results?.[0];
    if (!firstResult) {
        return {
            probe_error: "executeScript returned no frame result",
            tab_id: tabId,
        };
    }
    if (typeof firstResult.result === "undefined") {
        return {
            probe_error: "executeScript result is undefined",
            tab_id: tabId,
            frame_id: firstResult.frameId ?? null,
            document_id: firstResult.documentId ?? "",
        };
    }
    const workerResult = firstResult.result || {};
    // #endregion
    // #endregion
    const latestBodyPrefix = Array.isArray(workerResult?.steps)
        ? String(workerResult.steps[workerResult.steps.length - 1]?.body_text_prefix || "")
        : "";
    const hasApplicationErrorResult =
        String(workerResult?.title || "").toLowerCase().includes("application error") ||
        latestBodyPrefix.toLowerCase().includes("application error");
    const alreadyRetried = Number(jobPayload?._app_error_retry || 0) > 0;
    const retryProjectId = String(jobPayload?.project_id || "").trim();
    if (hasApplicationErrorResult && !alreadyRetried && retryProjectId) {
        const recovery = await recoverApplicationErrorTab(tabId, retryProjectId, false);
        if (recovery?.recovered) {
            await sleep(1200);
            return runVideoUiSubmit(tabId, {
                ...jobPayload,
                _app_error_retry: 1,
            });
        }
    }
    return firstResult.result || {};
}

async function handleRunJob(data) {
    const jobId = String(data.job_id || "").trim();
    const jobType = String(data.job_type || "").trim();
    if (
        jobType === "capture_mode_start" ||
        jobType === "capture_mode_stop" ||
        jobType === "capture_mode_status"
    ) {
        const resultPayload = await globalThis.Flow2ApiCaptureMode.handleJob({
            data,
            helpers: {
                buildProjectUrl,
                extractProjectIdFromUrl,
                getPreferredLabsTab,
                waitForTabReady,
                sleep,
                getSettings,
            },
        });
        sendSocketMessage({
            req_id: data.req_id,
            job_id: jobId,
            job_type: jobType,
            status: "success",
            result: resultPayload,
        });
        return;
    }
    const targetProjectId = String(data.project_id || "").trim();
    const canonicalProjectUrl = buildProjectUrl(targetProjectId);
    const jobPayload = {
        ...(data.job_payload || {}),
        project_id: String((data.job_payload || {}).project_id || targetProjectId || "").trim(),
    };
    const activateTab = !!jobPayload.activate_tab;
    const forceCanonicalProjectPage = !!targetProjectId && jobRequiresCanonicalProjectPage(jobType);

    if (!jobType) {
        throw new Error("Missing job_type");
    }

    let targetTab = await getPreferredLabsTab(targetProjectId);
    if (!targetTab?.id) {
        targetTab = await chrome.tabs.create({
            url: canonicalProjectUrl,
            active: activateTab,
        });
    } else if (
        forceCanonicalProjectPage &&
        String(targetTab.url || "") !== canonicalProjectUrl
    ) {
        targetTab = await chrome.tabs.update(targetTab.id, {
            url: canonicalProjectUrl,
            active: activateTab,
        });
    } else if (activateTab) {
        targetTab = await chrome.tabs.update(targetTab.id, {
            active: true,
        });
    }

    await waitForTabReady(targetTab.id);
    await sleep(1200);

    let beforeContext = await collectExecutionContext(targetTab.id);
    let recoveryContext = null;
    const beforeSurface =
        jobType === "video_ui_submit" || jobType === "video_ui_workflow"
            ? await hasApplicationErrorSurface(targetTab.id)
            : null;
    if (
        (jobType === "video_ui_submit" || jobType === "video_ui_workflow") &&
        (isApplicationErrorContext(beforeContext) || !!beforeSurface?.has_error)
    ) {
        recoveryContext = await recoverApplicationErrorTab(targetTab.id, targetProjectId, activateTab);
        beforeContext = await collectExecutionContext(targetTab.id);
    }
    let resultPayload = {};

    if (jobType === "ensure_project_page") {
        resultPayload = {
            ensured_project_id: targetProjectId,
            tab_id: targetTab.id,
            page_url: targetTab.url || "",
        };
    } else if (jobType === "capture_ui_state") {
        resultPayload = {
            requested_scope: String(jobPayload.scope || "default"),
            tab_id: targetTab.id,
            ui_state: await captureRuntimeUiState(targetTab.id),
        };
    } else if (jobType === "account_credential_probe") {
        resultPayload = {
            requested_scope: String(jobPayload.scope || "account_credential_probe"),
            tab_id: targetTab.id,
            ui_state: await collectBrowserCredentialSnapshot(targetTab.id),
        };
    } else if (jobType === "video_ui_probe") {
        resultPayload = {
            requested_scope: String(jobPayload.scope || "video_probe"),
            tab_id: targetTab.id,
            ui_state: await runVideoUiProbe(targetTab.id),
        };
    } else if (jobType === "video_ui_prepare") {
        resultPayload = {
            requested_scope: String(jobPayload.scope || "video_prepare"),
            tab_id: targetTab.id,
            ui_state: await runVideoUiPrepare(targetTab.id, jobPayload),
        };
    } else if (jobType === "video_ui_submit_probe") {
        resultPayload = {
            requested_scope: String(jobPayload.scope || "video_submit_probe"),
            tab_id: targetTab.id,
            ui_state: await runVideoUiSubmitProbe(targetTab.id, jobPayload),
        };
    } else if (jobType === "video_ui_type_probe") {
        resultPayload = {
            requested_scope: String(jobPayload.scope || "video_ui_type_probe"),
            tab_id: targetTab.id,
            ui_state: await runVideoUiTypeProbe(targetTab.id, jobPayload),
        };
    } else if (jobType === "video_ui_submit") {
        resultPayload = {
            requested_scope: String(jobPayload.scope || "video_ui_submit"),
            tab_id: targetTab.id,
            ui_state: await runVideoUiSubmit(targetTab.id, jobPayload),
        };
    } else if (jobType === "video_submode_probe") {
        resultPayload = {
            requested_scope: String(jobPayload.scope || "video_submode_probe"),
            tab_id: targetTab.id,
            ui_state: await runVideoSubmodeProbe(targetTab.id, jobPayload),
        };
    } else if (jobType === "video_ui_workflow") {
        resultPayload = {
            requested_scope: String(jobPayload.scope || "video_ui_workflow"),
            tab_id: targetTab.id,
            ui_state: await globalThis.Flow2ApiVideoWorkflow.handleJob({
                tabId: targetTab.id,
                jobPayload,
            }),
        };
    } else {
        throw new Error(`Unsupported job_type: ${jobType}`);
    }

    const afterContext = await collectExecutionContext(targetTab.id);
    sendSocketMessage({
        req_id: data.req_id,
        job_id: jobId,
        job_type: jobType,
        status: "success",
        result: resultPayload,
        debug_context: {
            tab_id: targetTab.id,
            target_project_id: targetProjectId,
            job_type: jobType,
            before: beforeContext,
            recovery: recoveryContext,
            after: afterContext,
        },
    });
}

chrome.storage.onChanged.addListener((changes, areaName) => {
    if (areaName !== "local") return;
    if (changes.routeKey || changes.serverUrl || changes.apiKey || changes.clientLabel) {
        console.log("[Flow2API] Extension settings changed, reconnecting WebSocket...");
        closeSocket();
        connectWS();
    }
});

chrome.runtime.onInstalled.addListener(() => {
    persistBootstrapSettings().then(() => connectWS()).catch(() => connectWS());
});

chrome.runtime.onStartup.addListener(() => {
    persistBootstrapSettings().then(() => connectWS()).catch(() => connectWS());
});

chrome.tabs.onUpdated.addListener((_tabId, changeInfo, tab) => {
    const tabUrl = changeInfo.url || tab?.url || "";
    if (tabUrl.startsWith("https://labs.google/") || (tab?.url || "").startsWith("https://labs.google/")) {
        sendRouteStatus("tab_updated");
    }
});

chrome.tabs.onRemoved.addListener(() => {
    sendRouteStatus("tab_removed");
});

chrome.tabs.onActivated.addListener(async ({ tabId }) => {
    try {
        const tab = await chrome.tabs.get(tabId);
        if ((tab?.url || "").startsWith("https://labs.google/")) {
            sendRouteStatus("tab_activated");
        }
    } catch (e) {
        // Ignore transient tab lookup failures.
    }
});

connectWS();
