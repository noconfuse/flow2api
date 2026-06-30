function createFlowVideoEditSubmitHook(rawConfig) {
    const targetPattern = /\/v1\/video:batchAsyncGenerateVideoEditVideo(?:[/?#]|$)/i;
    const activeGlobalKey = "__FLOW2API_VIDEO_EDIT_SUBMIT_HOOK__";
    const safeClone = (value) => {
        try {
            return JSON.parse(JSON.stringify(value));
        } catch (_error) {
            return null;
        }
    };
    const normalizeFrameIndex = (value) => {
        const parsed = Number.parseInt(String(value ?? "").trim(), 10);
        return Number.isFinite(parsed) && parsed >= 0 ? parsed : null;
    };
    const normalizeSeconds = (value) => {
        const parsed = Number.parseFloat(String(value ?? "").trim());
        return Number.isFinite(parsed) && parsed >= 0 ? parsed : null;
    };
    const normalizeFrameRate = (value) => {
        const parsed = Number.parseFloat(String(value ?? "").trim());
        return Number.isFinite(parsed) && parsed > 0 ? parsed : 60;
    };
    const secondsToFrameIndex = (seconds, frameRate) => {
        if (!Number.isFinite(seconds) || seconds < 0 || !Number.isFinite(frameRate) || frameRate <= 0) {
            return null;
        }
        return Math.max(0, Math.round(seconds * frameRate));
    };
    const normalizeConfig = (value) => ({
        enabled: !!value?.enabled,
        mode: String(value?.mode || "capture").trim().toLowerCase() === "rewrite" ? "rewrite" : "capture",
        overrideMediaId: String(value?.overrideMediaId || "").trim(),
        overrideStartFrameIndex: normalizeFrameIndex(value?.overrideStartFrameIndex),
        overrideEndFrameIndex: normalizeFrameIndex(value?.overrideEndFrameIndex),
        overrideStartSeconds: normalizeSeconds(value?.overrideStartSeconds),
        overrideEndSeconds: normalizeSeconds(value?.overrideEndSeconds),
        overrideSourceDurationSeconds: normalizeSeconds(value?.overrideSourceDurationSeconds),
        overrideFrameRate: normalizeFrameRate(value?.overrideFrameRate),
        maxEntries: Math.max(1, Math.min(20, Number.parseInt(String(value?.maxEntries || "8"), 10) || 8)),
    });
    const resolveOverrideFrameRange = (configValue) => {
        const frameRate = normalizeFrameRate(configValue?.overrideFrameRate);
        const maxFrameIndex = Number.isFinite(configValue?.overrideSourceDurationSeconds)
            ? secondsToFrameIndex(configValue.overrideSourceDurationSeconds, frameRate)
            : null;
        let startFrameIndex = Number.isFinite(configValue?.overrideStartFrameIndex)
            ? Math.max(0, Number(configValue.overrideStartFrameIndex))
            : null;
        let endFrameIndex = Number.isFinite(configValue?.overrideEndFrameIndex)
            ? Math.max(0, Number(configValue.overrideEndFrameIndex))
            : null;
        if (startFrameIndex == null && Number.isFinite(configValue?.overrideStartSeconds)) {
            startFrameIndex = secondsToFrameIndex(configValue.overrideStartSeconds, frameRate);
        }
        if (endFrameIndex == null && Number.isFinite(configValue?.overrideEndSeconds)) {
            endFrameIndex = secondsToFrameIndex(configValue.overrideEndSeconds, frameRate);
        }
        if (Number.isFinite(maxFrameIndex)) {
            if (Number.isFinite(startFrameIndex)) {
                startFrameIndex = Math.min(startFrameIndex, maxFrameIndex);
            }
            if (Number.isFinite(endFrameIndex)) {
                endFrameIndex = Math.min(endFrameIndex, maxFrameIndex);
            }
        }
        if (Number.isFinite(startFrameIndex) && Number.isFinite(endFrameIndex) && endFrameIndex < startFrameIndex) {
            [startFrameIndex, endFrameIndex] = [endFrameIndex, startFrameIndex];
        }
        return {
            frameRate,
            startFrameIndex: Number.isFinite(startFrameIndex) ? startFrameIndex : null,
            endFrameIndex: Number.isFinite(endFrameIndex) ? endFrameIndex : null,
        };
    };
    const summarizePayload = (payload) => {
        const requests = Array.isArray(payload?.requests) ? payload.requests : [];
        return {
            project_id: String(payload?.clientContext?.projectId || ""),
            request_count: requests.length,
            video_inputs: requests.slice(0, 4).map((request, index) => ({
                index,
                mediaId: String(request?.videoInput?.mediaId || ""),
                startFrameIndex: normalizeFrameIndex(request?.videoInput?.startFrameIndex),
                endFrameIndex: normalizeFrameIndex(request?.videoInput?.endFrameIndex),
                videoModelKey: String(request?.videoModelKey || ""),
            })),
        };
    };
    const extractBodyText = (body) => {
        if (typeof body === "string") return body;
        if (body == null) return "";
        if (typeof URLSearchParams !== "undefined" && body instanceof URLSearchParams) {
            return body.toString();
        }
        return null;
    };
    const summarizeError = (error) => String(error?.stack || error?.message || error || "").slice(0, 600);
    const currentGlobal = window[activeGlobalKey];
    if (currentGlobal?.dispose) {
        try {
            currentGlobal.dispose();
        } catch (_error) {}
    }

    const config = normalizeConfig(rawConfig);
    const originalFetch = typeof window.fetch === "function" ? window.fetch.bind(window) : null;
    const originalXhrOpen = window.XMLHttpRequest?.prototype?.open;
    const originalXhrSend = window.XMLHttpRequest?.prototype?.send;
    const entries = [];
    const errors = [];
    const installedAt = Date.now();

    const pushEntry = (entry) => {
        const stored = {
            ts: Date.now(),
            ...safeClone(entry),
        };
        entries.push(stored);
        while (entries.length > config.maxEntries) {
            entries.shift();
        }
        return stored;
    };
    const pushError = (kind, error) => {
        errors.push({
            kind: String(kind || ""),
            detail: summarizeError(error),
            ts: Date.now(),
        });
        while (errors.length > config.maxEntries) {
            errors.shift();
        }
    };
    const maybeRewritePayload = (payload) => {
        const requests = Array.isArray(payload?.requests) ? payload.requests : [];
        const touched = [];
        let mutated = false;
        const overrideFrameRange = resolveOverrideFrameRange(config);
        requests.forEach((request, index) => {
            const videoInput = request?.videoInput;
            if (!videoInput || typeof videoInput !== "object") return;
            const before = {
                mediaId: String(videoInput.mediaId || ""),
                startFrameIndex: normalizeFrameIndex(videoInput.startFrameIndex),
                endFrameIndex: normalizeFrameIndex(videoInput.endFrameIndex),
            };
            if (config.mode === "rewrite") {
                if (config.overrideMediaId) {
                    videoInput.mediaId = config.overrideMediaId;
                }
                if (Number.isFinite(overrideFrameRange.startFrameIndex)) {
                    videoInput.startFrameIndex = overrideFrameRange.startFrameIndex;
                }
                if (Number.isFinite(overrideFrameRange.endFrameIndex)) {
                    videoInput.endFrameIndex = overrideFrameRange.endFrameIndex;
                }
            }
            const after = {
                mediaId: String(videoInput.mediaId || ""),
                startFrameIndex: normalizeFrameIndex(videoInput.startFrameIndex),
                endFrameIndex: normalizeFrameIndex(videoInput.endFrameIndex),
            };
            if (
                before.mediaId !== after.mediaId ||
                before.startFrameIndex !== after.startFrameIndex ||
                before.endFrameIndex !== after.endFrameIndex
            ) {
                mutated = true;
            }
            touched.push({ index, before, after });
        });
        return {
            payload,
            mutated,
            touched,
            overrideFrameRange,
        };
    };
    const prepareIntercept = (transport, url, body) => {
        if (!config.enabled || !targetPattern.test(String(url || ""))) {
            return { matched: false, url: String(url || ""), body };
        }
        const bodyText = extractBodyText(body);
        if (bodyText == null) {
            pushEntry({
                transport,
                url: String(url || ""),
                matched: true,
                body_supported: false,
            });
            return { matched: true, url: String(url || ""), body };
        }
        try {
            const payload = bodyText ? JSON.parse(bodyText) : {};
            const beforeSummary = summarizePayload(payload);
            const rewriteResult = maybeRewritePayload(payload);
            const afterSummary = summarizePayload(rewriteResult.payload);
            const entry = pushEntry({
                transport,
                url: String(url || ""),
                matched: true,
                mode: config.mode,
                override_frame_range: rewriteResult.overrideFrameRange,
                before: beforeSummary,
                after: afterSummary,
                mutated: !!rewriteResult.mutated,
                touched: rewriteResult.touched,
            });
            return {
                matched: true,
                url: String(url || ""),
                body: rewriteResult.mutated ? JSON.stringify(rewriteResult.payload) : bodyText,
                entry,
            };
        } catch (error) {
            pushError(`${transport}:parse`, error);
            const entry = pushEntry({
                transport,
                url: String(url || ""),
                matched: true,
                parse_failed: true,
            });
            return { matched: true, url: String(url || ""), body, entry };
        }
    };

    if (originalFetch) {
        window.fetch = async function patchedFlowVideoEditFetch(resource, init) {
            const requestUrl = typeof resource === "string" ? resource : String(resource?.url || "");
            const requestBody =
                init && Object.prototype.hasOwnProperty.call(init, "body")
                    ? init.body
                    : undefined;
            const intercept = prepareIntercept("fetch", requestUrl, requestBody);
            let response;
            if (intercept.matched && init && Object.prototype.hasOwnProperty.call(init, "body")) {
                response = await originalFetch(resource, {
                    ...init,
                    body: intercept.body,
                });
            } else {
                response = await originalFetch(resource, init);
            }
            if (intercept.entry && response) {
                intercept.entry.response_status = Number(response.status || 0) || 0;
                intercept.entry.response_ok = !!response.ok;
                try {
                    const responseText = await response.clone().text();
                    intercept.entry.response_preview = String(responseText || "").slice(0, 1200);
                } catch (error) {
                    intercept.entry.response_preview_error = summarizeError(error);
                }
            }
            return response;
        };
    }

    if (originalXhrOpen && originalXhrSend) {
        window.XMLHttpRequest.prototype.open = function patchedFlowVideoEditOpen(method, url, ...rest) {
            this.__flow2api_video_edit_submit_url = String(url || "");
            this.__flow2api_video_edit_submit_method = String(method || "");
            return originalXhrOpen.call(this, method, url, ...rest);
        };
        window.XMLHttpRequest.prototype.send = function patchedFlowVideoEditSend(body) {
            const requestUrl = String(this.__flow2api_video_edit_submit_url || "");
            const intercept = prepareIntercept("xhr", requestUrl, body);
            if (intercept.entry) {
                this.addEventListener("loadend", () => {
                    intercept.entry.response_status = Number(this.status || 0) || 0;
                    intercept.entry.response_ok = Number(this.status || 0) >= 200 && Number(this.status || 0) < 300;
                    try {
                        intercept.entry.response_preview = String(this.responseText || "").slice(0, 1200);
                    } catch (error) {
                        intercept.entry.response_preview_error = summarizeError(error);
                    }
                }, { once: true });
            }
            return originalXhrSend.call(this, intercept.body);
        };
    }

    const api = {
        snapshot() {
            return safeClone({
                enabled: config.enabled,
                mode: config.mode,
                overrideMediaId: config.overrideMediaId,
                overrideStartFrameIndex: config.overrideStartFrameIndex,
                overrideEndFrameIndex: config.overrideEndFrameIndex,
                overrideStartSeconds: config.overrideStartSeconds,
                overrideEndSeconds: config.overrideEndSeconds,
                overrideSourceDurationSeconds: config.overrideSourceDurationSeconds,
                overrideFrameRate: config.overrideFrameRate,
                installedAt,
                entries,
                errors,
            });
        },
        dispose() {
            if (originalFetch) {
                window.fetch = originalFetch;
            }
            if (originalXhrOpen && originalXhrSend) {
                window.XMLHttpRequest.prototype.open = originalXhrOpen;
                window.XMLHttpRequest.prototype.send = originalXhrSend;
            }
            if (window[activeGlobalKey] === api) {
                delete window[activeGlobalKey];
            }
        },
    };
    window[activeGlobalKey] = api;
    return api;
}
