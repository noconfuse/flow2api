(function () {
  if (globalThis.Flow2ApiVideoWorkflow) {
    return;
  }

  async function executeWorkflowScript(tabId, payload) {
    const workflowMode = String(payload?.workflow_mode || "").trim();
    let result = null;
    try {
      const executionResults = await chrome.scripting.executeScript({
      target: { tabId },
      world: "MAIN",
      func: async (jobPayload) => {
        const LAST_RESULT_STORAGE_KEY = "__FLOW2API_VIDEO_WORKFLOW_LAST_RESULT__";
        const finalize = (value) => {
          let serializable = value;
          try {
            serializable = JSON.parse(JSON.stringify(value));
          } catch (error) {
            serializable = {
              result_version: "video_ui_workflow_v1",
              success: false,
              error: `serialize_failed:${String(error?.message || error)}`,
              steps: [],
            };
          }
          try {
            window[LAST_RESULT_STORAGE_KEY] = serializable;
          } catch (_error) {
            // ignore page storage failure
          }
          try {
            window.localStorage?.setItem(LAST_RESULT_STORAGE_KEY, JSON.stringify(serializable));
          } catch (_error) {
            // ignore local storage failure
          }
          try {
            window.sessionStorage?.setItem(LAST_RESULT_STORAGE_KEY, JSON.stringify(serializable));
          } catch (_error) {
            // ignore session storage failure
          }
          return serializable;
        };
        const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
        const humanPause = async (minMs, maxMs = null) => {
          const start = Number(minMs || 0);
          const end = Number(maxMs == null ? minMs : maxMs);
          const low = Math.max(0, Math.min(start, end));
          const high = Math.max(low, Math.max(start, end));
          const jitter = low === high ? low : Math.round(low + Math.random() * (high - low));
          await sleep(jitter);
          return jitter;
        };
        const steps = [];

        const visible = (node) => {
          if (!(node instanceof Element)) return false;
          const rect = node.getBoundingClientRect();
          const style = window.getComputedStyle(node);
          return (
            rect.width > 0 &&
            rect.height > 0 &&
            style.visibility !== "hidden" &&
            style.display !== "none" &&
            style.opacity !== "0"
          );
        };

        const textOf = (node) => String(node?.innerText || node?.textContent || "").replace(/\s+/g, " ").trim();

        const summarizeElement = (node) => {
          if (!(node instanceof Element)) {
            return { tag: "", text: "", aria_label: "", role: "" };
          }
          return {
            tag: node.tagName.toLowerCase(),
            text: textOf(node).slice(0, 200),
            aria_label: String(node.getAttribute("aria-label") || ""),
            role: String(node.getAttribute("role") || ""),
          };
        };

        const recordStep = (label, data = {}) => {
          steps.push({ label, ...data });
        };

        const waitFor = async (factory, timeoutMs = 8000, intervalMs = 120) => {
          const startedAt = Date.now();
          while ((Date.now() - startedAt) < timeoutMs) {
            const value = factory();
            if (value) return value;
            await sleep(intervalMs);
          }
          return null;
        };

        const clickNode = (node, options = {}) => {
          if (!(node instanceof Element)) return false;
          const nativeOnly = !!options.nativeOnly;
          try {
            node.scrollIntoView({ block: "center", inline: "center", behavior: "instant" });
          } catch (_error) {
            // ignore scroll failures
          }
          const rect = node.getBoundingClientRect();
          const clientX = Math.round(rect.left + rect.width / 2);
          const clientY = Math.round(rect.top + rect.height / 2);
          const target = document.elementFromPoint(clientX, clientY) || node;
          const dispatchMouse = (targetNode, type, buttons) => {
            targetNode.dispatchEvent(new MouseEvent(type, {
              bubbles: true,
              cancelable: true,
              composed: true,
              clientX,
              clientY,
              button: 0,
              buttons,
              view: window,
            }));
          };
          try {
            if (!nativeOnly) {
              dispatchMouse(target, "mousedown", 1);
              dispatchMouse(target, "mouseup", 0);
              dispatchMouse(target, "click", 0);
            }
            if (typeof node.click === "function") {
              node.click();
            }
            return true;
          } catch (_error) {
            return false;
          }
        };

        const dispatchKeyboard = (target, key) => {
          if (!(target instanceof Element)) return false;
          const eventInit = {
            bubbles: true,
            cancelable: true,
            composed: true,
            key,
          };
          try {
            target.dispatchEvent(new KeyboardEvent("keydown", eventInit));
            target.dispatchEvent(new KeyboardEvent("keyup", eventInit));
            return true;
          } catch (_error) {
            return false;
          }
        };

        const dispatchPointer = (type, x, y, buttons = 1) => {
          const target = document.elementFromPoint(x, y) || document.body;
          const base = {
            bubbles: true,
            cancelable: true,
            composed: true,
            clientX: Math.round(x),
            clientY: Math.round(y),
            button: 0,
            buttons,
            pointerId: 1,
            pointerType: "mouse",
            isPrimary: true,
            view: window,
          };
          if (typeof PointerEvent === "function") {
            target.dispatchEvent(new PointerEvent(type, base));
          }
          const mouseType = type.replace("pointer", "mouse");
          target.dispatchEvent(new MouseEvent(mouseType, base));
        };

        const dragHandleTo = async (fromX, toX, y) => {
          const stepsCount = Math.max(8, Math.min(20, Math.round(Math.abs(toX - fromX) / 12)));
          dispatchPointer("pointerdown", fromX, y, 1);
          await sleep(60);
          for (let index = 1; index <= stepsCount; index += 1) {
            const nextX = fromX + ((toX - fromX) * index) / stepsCount;
            dispatchPointer("pointermove", nextX, y, 1);
            await sleep(24);
          }
          dispatchPointer("pointerup", toX, y, 0);
          await sleep(220);
        };

        const collectVisibleButtons = () =>
          Array.from(document.querySelectorAll("button, [role='button'], [tabindex]")).filter((node) => visible(node));

        const collectDialogs = () =>
          Array.from(document.querySelectorAll("[role='dialog'], dialog, [aria-modal='true']")).filter((node) => visible(node));

        const findCreateLauncher = () => {
          const promptTarget = findPromptTarget();
          const promptRect = promptTarget?.getBoundingClientRect?.() || null;
          const buttons = collectVisibleButtons();
          return buttons
            .map((node) => {
              const text = textOf(node).replace(/\s+/g, " ").trim();
              const compact = text.replace(/\s+/g, "");
              const rect = node.getBoundingClientRect();
              if (!compact) return null;
              if (compact.includes("为图片创建多个版本")) return null;
              if (compact.includes("arrow_forward创建")) return null;
              if (compact.includes("添加到提示")) return null;
              const looksLikeCreate =
                compact === "add_2创建" ||
                compact === "创建" ||
                compact.startsWith("add_2创建");
              if (!looksLikeCreate) return null;
              const bottomComposerScore =
                (rect.bottom > window.innerHeight * 0.72 ? 40 : 0) +
                (rect.left < window.innerWidth * 0.42 ? 20 : 0) +
                (promptRect && rect.top >= promptRect.top - 80 && rect.bottom <= promptRect.bottom + 80 ? 25 : 0) +
                (compact.includes("add_2") ? 20 : 0);
              if (bottomComposerScore < 40) return null;
              return { node, bottomComposerScore, rect };
            })
            .filter(Boolean)
            .sort((a, b) =>
              b.bottomComposerScore - a.bottomComposerScore ||
              b.rect.bottom - a.rect.bottom ||
              a.rect.left - b.rect.left
            )[0]?.node || null;
        };

        const scoreDialog = (dialog) => {
          if (!(dialog instanceof Element)) return -1;
          const text = textOf(dialog);
          const rect = dialog.getBoundingClientRect();
          let score = rect.width * rect.height * 0.0001;
          if (text.includes("上传媒体")) score += 30;
          if (text.includes("最近")) score += 16;
          if (text.includes("图片")) score += 10;
          if (text.includes("视频")) score += 10;
          if (text.includes("添加到提示")) score += 40;
          if ((text.match(/\d{2}:\d{2}:\d{2}/g) || []).length >= 2) score += 35;
          score += dialog.querySelectorAll("video, img").length * 2;
          score += dialog.querySelectorAll("img[src*='trim-handle']").length * 12;
          if (text.includes("查看所有更新日志")) score -= 80;
          if (text.includes("开始使用") && text.includes("最新更新")) score -= 80;
          return score;
        };

        const isMediaPickerDialog = (dialog) => {
          if (!(dialog instanceof Element)) return false;
          const text = textOf(dialog);
          const timeTokenCount = (text.match(/\d{2}:\d{2}:\d{2}/g) || []).length;
          const visualCount = dialog.querySelectorAll("video, img").length;
          const hasPickerChrome =
            (text.includes("上传媒体") || text.includes("上传的内容")) &&
            (text.includes("最近") || text.includes("全部") || text.includes("图片") || text.includes("视频"));
          const hasEditorHints = text.includes("添加到提示") || timeTokenCount >= 2;
          return (hasPickerChrome && visualCount >= 1) || (hasPickerChrome && hasEditorHints) || (hasEditorHints && visualCount >= 2);
        };

        const getActiveDialog = () => {
          const dialogs = collectDialogs().sort((left, right) => scoreDialog(right) - scoreDialog(left));
          return dialogs[0] || null;
        };

        const getMediaPickerDialog = () => {
          const dialogs = collectDialogs()
            .filter((dialog) => isMediaPickerDialog(dialog))
            .sort((left, right) => scoreDialog(right) - scoreDialog(left));
          return dialogs[0] || null;
        };

        const getReferenceMediaKind = () => {
          const raw = String(jobPayload.reference_media_kind || "").trim().toLowerCase();
          return raw === "image" ? "image" : "video";
        };

        const getReferenceMediaId = () => String(jobPayload.reference_media_id || "").trim();

        const collectReferenceMediaIdStrings = (node) => {
          if (!(node instanceof Element)) return [];
          const values = [];
          const seen = new Set();
          const pushValue = (value) => {
            const normalized = normalizeUiToken(value);
            if (!normalized || seen.has(normalized)) return;
            seen.add(normalized);
            values.push(normalized);
          };

          pushValue(node.getAttribute("src"));

          Array.from(node.querySelectorAll("img, video, source"))
            .slice(0, 12)
            .forEach((child) => {
              if (!(child instanceof Element)) return;
              pushValue(child.getAttribute("src"));
            });

          return values;
        };

        const findReferencePickerItemByMediaId = (pickerItems) => {
          const referenceMediaId = normalizeUiToken(getReferenceMediaId());
          if (!referenceMediaId) {
            return null;
          }

          const scored = pickerItems
            .map((node, index) => {
              const strings = collectReferenceMediaIdStrings(node);
              const mediaIdMatched = !!referenceMediaId && strings.some((value) => value.includes(referenceMediaId));
              return {
                index,
                node,
                strings: strings.slice(0, 8),
                media_id_matched: mediaIdMatched,
                score: mediaIdMatched ? 100 : 0,
              };
            })
            .filter((item) => item.score > 0)
            .sort((left, right) => right.score - left.score || left.index - right.index);

          return scored[0] || null;
        };

        const collectMediaPickerItems = (dialog) => {
          if (!(dialog instanceof Element)) return [];
          const dialogRect = dialog.getBoundingClientRect();
          const rightPaneBoundary = dialogRect.left + dialogRect.width * 0.96;
          const minTop = dialogRect.top + 92;
          const seen = new Set();
          const candidates = [];

          const addCandidate = (node) => {
            if (!(node instanceof Element) || !visible(node)) return;
            const rect = node.getBoundingClientRect();
            const key = `${Math.round(rect.left)}:${Math.round(rect.top)}:${Math.round(rect.width)}:${Math.round(rect.height)}`;
            if (seen.has(key)) return;
            seen.add(key);
            candidates.push(node);
          };

          const scoreCandidate = (node) => {
            if (!(node instanceof Element) || !visible(node)) return -1;
            const rect = node.getBoundingClientRect();
            if (rect.width < 72 || rect.height < 72) return -1;
            if (rect.left > rightPaneBoundary) return -1;
            if (rect.top < minTop) return -1;
            if (rect.width > dialogRect.width * 0.92) return -1;
            if (rect.height > dialogRect.height * 0.9) return -1;
            const mediaCount = node.querySelectorAll("video, img").length;
            const text = textOf(node);
            const role = String(node.getAttribute("role") || "");
            let score = mediaCount * 20;
            if (role === "option") score += 30;
            if (node.matches("button, [role='button']")) score += 12;
            if (text && text.length <= 120) score += 6;
            if (rect.height >= 140) score += 12;
            if (rect.width >= 110 && rect.width <= 280) score += 8;
            if (rect.left <= dialogRect.left + dialogRect.width * 0.22 && mediaCount === 0) score -= 50;
            return score;
          };

          const tryAdd = (node) => {
            if (!(node instanceof Element)) return;
            const chain = [node, node.parentElement, node.parentElement?.parentElement, node.parentElement?.parentElement?.parentElement]
              .filter((item) => item instanceof Element);
            const best = chain
              .map((item) => ({ node: item, score: scoreCandidate(item) }))
              .filter((item) => item.score >= 20)
              .sort((left, right) => right.score - left.score || right.node.getBoundingClientRect().width - left.node.getBoundingClientRect().width)[0];
            if (!best) return;
            addCandidate(best.node);
          };

          Array.from(dialog.querySelectorAll("[data-virtuoso-scroller='true'] [data-item-index], [data-item-index]"))
            .filter((node) => visible(node))
            .forEach((row) => {
              const rect = row.getBoundingClientRect();
              if (rect.left > rightPaneBoundary || rect.top < minTop) return;
              const option = row.querySelector("[role='option']");
              addCandidate(option instanceof Element ? option : row);
            });

          if (candidates.length) {
            return candidates.sort((left, right) => {
              const leftIndex = Number.parseInt(String(left.closest("[data-item-index]")?.getAttribute("data-item-index") || ""), 10);
              const rightIndex = Number.parseInt(String(right.closest("[data-item-index]")?.getAttribute("data-item-index") || ""), 10);
              if (Number.isInteger(leftIndex) && Number.isInteger(rightIndex) && leftIndex !== rightIndex) {
                return leftIndex - rightIndex;
              }
              const leftRect = left.getBoundingClientRect();
              const rightRect = right.getBoundingClientRect();
              if (Math.abs(leftRect.top - rightRect.top) > 10) {
                return leftRect.top - rightRect.top;
              }
              return leftRect.left - rightRect.left;
            });
          }

          Array.from(dialog.querySelectorAll("[role='option'], button, [role='button'], video, img"))
            .filter((node) => visible(node))
            .forEach((node) => tryAdd(node));

          if (!candidates.length) {
            Array.from(dialog.querySelectorAll("div, section, article"))
              .filter((node) => visible(node))
              .forEach((node) => tryAdd(node));
          }

          if (!candidates.length) {
            const sampleXs = [0.12, 0.28, 0.44];
            const sampleYs = [0.34, 0.52, 0.7];
            sampleYs.forEach((yRatio) => {
              sampleXs.forEach((xRatio) => {
                const x = dialogRect.left + dialogRect.width * xRatio;
                const y = dialogRect.top + dialogRect.height * yRatio;
                Array.from(document.elementsFromPoint(x, y)).forEach((node) => tryAdd(node));
              });
            });
          }

          return candidates.sort((left, right) => {
            const leftRect = left.getBoundingClientRect();
            const rightRect = right.getBoundingClientRect();
            if (Math.abs(leftRect.top - rightRect.top) > 10) {
              return leftRect.top - rightRect.top;
            }
            return leftRect.left - rightRect.left;
          });
        };

        const ensureReferenceSelected = async (dialog) => {
          const pickerItems = collectMediaPickerItems(dialog);
          const referenceMediaId = getReferenceMediaId();
          if (!referenceMediaId) {
            return {
              ok: false,
              reason: "missing_reference_media_id",
              picker_count: pickerItems.length,
            };
          }
          const matchedItem = findReferencePickerItemByMediaId(pickerItems);
          if (!matchedItem) {
            return {
              ok: false,
              reason: "reference_media_id_not_found",
              reference_media_id: referenceMediaId,
              picker_count: pickerItems.length,
              picker_samples: pickerItems.slice(0, 8).map((node) => ({
                tile: summarizeElement(node),
                src_strings: collectReferenceMediaIdStrings(node).slice(0, 6),
              })),
            };
          }
          const targetItem = matchedItem.node;
          clickNode(targetItem);
          const selected = await waitFor(
            () => {
              const timelineReady = getTimelineState(dialog);
              if (timelineReady && timelineReady.handles.length >= 2) {
                return true;
              }
              const addToPromptButton = findAddToPromptButton(dialog);
              return addToPromptButton ? true : null;
            },
            12000,
            120
          );
          return {
            ok: !!selected,
            action: "clicked_reference_tile",
            tile: summarizeElement(targetItem),
            match_strategy: "reference_media_id",
            resolved_target_index: matchedItem.index,
            reference_media_id: referenceMediaId,
            picker_count: pickerItems.length,
          };
        };

        const getTimelineContainer = (dialog) => {
          if (!(dialog instanceof Element)) return null;
          const candidates = Array.from(dialog.querySelectorAll("div, section, article"))
            .filter((node) => visible(node))
            .map((node) => {
              const text = textOf(node);
              const score =
                ((text.match(/\d{2}:\d{2}:\d{2}/g) || []).length >= 2 ? 3 : 0) +
                (text.includes("添加到提示") ? 2 : 0) +
                ((node.querySelectorAll("img[src*='trim-handle']").length || 0) >= 2 ? 3 : 0) +
                ((node.querySelectorAll("video, img").length || 0) > 0 ? 1 : 0);
              return { node, text, score };
            })
            .filter((item) => item.score >= 5)
            .sort((left, right) => right.score - left.score);
          return candidates[0]?.node || null;
        };

        const getTimelineState = (dialog) => {
          const root = getTimelineContainer(dialog);
          if (!(root instanceof Element)) {
            return null;
          }
          const handleImages = Array.from(root.querySelectorAll("img[src*='trim-handle']"))
            .filter((node) => visible(node))
            .sort((left, right) => left.getBoundingClientRect().left - right.getBoundingClientRect().left);
          const timeTokens = textOf(root).match(/\d{2}:\d{2}:\d{2}/g) || [];
          return {
            root: summarizeElement(root),
            time_tokens: timeTokens.slice(0, 6),
            handle_count: handleImages.length,
            handles: handleImages.map((node) => {
              const rect = node.getBoundingClientRect();
              return {
                x: rect.left + rect.width / 2,
                y: rect.top + rect.height / 2,
              };
            }),
          };
        };

        const maybeAdjustTimeRange = async (dialog) => {
          if (getReferenceMediaKind() === "image") {
            return { ok: true, skipped: true, reason: "reference_media_kind_image" };
          }
          const totalSeconds = Number.parseFloat(String(jobPayload.desired_source_duration_seconds || ""));
          const desiredStart = Number.parseFloat(String(jobPayload.desired_start_seconds || ""));
          const desiredEnd = Number.parseFloat(String(jobPayload.desired_end_seconds || ""));
          if (!Number.isFinite(totalSeconds) || totalSeconds <= 0) {
            return { ok: true, skipped: true, reason: "missing_total_seconds" };
          }
          if (!Number.isFinite(desiredStart) && !Number.isFinite(desiredEnd)) {
            return { ok: true, skipped: true, reason: "missing_requested_range" };
          }
          const timeline = await waitFor(() => {
            const current = getTimelineState(dialog);
            return current && current.handles.length >= 2 ? current : null;
          }, 12000, 120);
          if (!timeline || timeline.handles.length < 2) {
            return { ok: false, reason: "timeline_handles_not_found", timeline };
          }
          const leftHandle = timeline.handles[0];
          const rightHandle = timeline.handles[timeline.handles.length - 1];
          const baseLeft = leftHandle.x;
          const baseRight = rightHandle.x;
          const width = Math.max(20, baseRight - baseLeft);
          const normalizedStart = Number.isFinite(desiredStart) ? Math.max(0, Math.min(totalSeconds, desiredStart)) : 0;
          const normalizedEnd = Number.isFinite(desiredEnd) ? Math.max(normalizedStart, Math.min(totalSeconds, desiredEnd)) : totalSeconds;
          const targetLeft = baseLeft + (normalizedStart / totalSeconds) * width;
          const targetRight = baseLeft + (normalizedEnd / totalSeconds) * width;
          if (Number.isFinite(desiredStart) && Math.abs(targetLeft - baseLeft) > 3) {
            await dragHandleTo(baseLeft, targetLeft, leftHandle.y);
          }
          if (Number.isFinite(desiredEnd) && Math.abs(targetRight - baseRight) > 3) {
            await dragHandleTo(baseRight, targetRight, rightHandle.y);
          }
          await sleep(260);
          return {
            ok: true,
            skipped: false,
            before: timeline,
            after: getTimelineState(dialog),
            requested_start_seconds: Number.isFinite(desiredStart) ? desiredStart : null,
            requested_end_seconds: Number.isFinite(desiredEnd) ? desiredEnd : null,
          };
        };

        const findAddToPromptButton = (dialog) => {
          if (!(dialog instanceof Element)) return null;
          return Array.from(dialog.querySelectorAll("button, [role='button']"))
            .filter((node) => visible(node))
            .find((node) => textOf(node).includes("添加到提示")) || null;
        };

        const isConversationLikeTarget = (node) => {
          if (!(node instanceof Element) || !visible(node)) return false;
          const rect = node.getBoundingClientRect();
          const inspectNodes = [];
          let current = node;
          while (current && inspectNodes.length < 8) {
            inspectNodes.push(current);
            current = current.parentElement;
          }
          const rightNarrow = rect.left > window.innerWidth * 0.55 && rect.width < window.innerWidth * 0.45;
          const conversationMarker = inspectNodes.some((item) => {
            if (!(item instanceof Element)) return false;
            const itemRect = item.getBoundingClientRect();
            const text = textOf(item).toLowerCase();
            const narrowPanel =
              itemRect.left > window.innerWidth * 0.5 &&
              itemRect.width > 220 &&
              itemRect.width < window.innerWidth * 0.48;
            return narrowPanel && (
              text.includes("未命名的会话") ||
              text.includes("conversation") ||
              text.includes("i've generated") ||
              text.includes("let me know if") ||
              text.includes("what would you like to create") ||
              text.includes("您希望创作什么内容")
            );
          });
          return rightNarrow || conversationMarker;
        };

        const isLikelyPromptField = (node) => {
          if (!(node instanceof Element) || !visible(node)) return false;
          const meta = [
            node.getAttribute("aria-label") || "",
            node.getAttribute("placeholder") || "",
            node.getAttribute("name") || "",
            node.id || "",
            node.className || "",
          ].join(" ").toLowerCase();
          const text = textOf(node).toLowerCase();
          if (meta.includes("search") || text.includes("搜索")) return false;
          if (node.getAttribute("data-slate-editor") === "true") return true;
          return (
            meta.includes("prompt") ||
            meta.includes("message") ||
            meta.includes("describe") ||
            meta.includes("创作") ||
            text.includes("创作") ||
            text.includes("您希望创作什么内容")
          );
        };

        const collectPromptCandidates = () => {
          const slateTargets = Array.from(
            document.querySelectorAll("[data-slate-editor='true'][role='textbox'][contenteditable='true']")
          ).filter((node) => visible(node) && isLikelyPromptField(node));
          const promptTargets = Array.from(
            document.querySelectorAll("textarea, [contenteditable='true'], input[type='text']")
          ).filter((node) => visible(node) && isLikelyPromptField(node));
          return [...slateTargets, ...promptTargets]
            .filter((node) => !getActiveDialog() || !getActiveDialog().contains(node))
            .filter((node, index, items) => items.indexOf(node) === index)
            .sort((left, right) => {
              const leftRect = left.getBoundingClientRect();
              const rightRect = right.getBoundingClientRect();
              const leftBottomComposerScore =
                (leftRect.bottom > window.innerHeight * 0.7 ? 2 : 0) +
                (Math.abs(leftRect.left + leftRect.width / 2 - window.innerWidth / 2) < window.innerWidth * 0.2 ? 1 : 0);
              const rightBottomComposerScore =
                (rightRect.bottom > window.innerHeight * 0.7 ? 2 : 0) +
                (Math.abs(rightRect.left + rightRect.width / 2 - window.innerWidth / 2) < window.innerWidth * 0.2 ? 1 : 0);
              if (leftBottomComposerScore !== rightBottomComposerScore) {
                return rightBottomComposerScore - leftBottomComposerScore;
              }
              return rightRect.width - leftRect.width;
            });
        };

        const findPromptTarget = () => {
          const candidates = collectPromptCandidates();
          return candidates.find((node) => !isConversationLikeTarget(node)) || candidates[0] || null;
        };

        const createInputEvent = (type, data) => {
          try {
            return new InputEvent(type, {
              bubbles: true,
              cancelable: true,
              data,
              inputType: "insertText",
            });
          } catch (_error) {
            return new Event(type, { bubbles: true, cancelable: true });
          }
        };

        const dispatchPasteText = (target, value) => {
          if (!(target instanceof Element) || !value) return false;
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
          if (!(target instanceof Element)) return;
          const selection = window.getSelection();
          if (!selection) return;
          const range = document.createRange();
          range.selectNodeContents(target);
          range.collapse(false);
          selection.removeAllRanges();
          selection.addRange(range);
        };

        const getEditableRangeWithinTarget = (target) => {
          if (!(target instanceof Element)) return null;
          const selection = window.getSelection();
          if (selection?.rangeCount) {
            const candidate = selection.getRangeAt(0);
            const container = candidate.commonAncestorContainer;
            if (container && target.contains(container.nodeType === Node.TEXT_NODE ? container.parentNode : container)) {
              return candidate.cloneRange();
            }
          }
          const range = document.createRange();
          range.selectNodeContents(target);
          range.collapse(false);
          return range;
        };

        const waitForPromptEditorReadyAfterSettings = async (timeoutMs = 2600) => {
          return await waitFor(() => {
            const overlayRoots = collectSettingsOverlayRoots();
            if (overlayRoots.length) return null;
            const target = findPromptTarget();
            if (!(target instanceof Element) || !visible(target)) return null;
            clickNode(target, { nativeOnly: true });
            target.focus();
            placeCaretAtEnd(target);
            const range = getEditableRangeWithinTarget(target);
            if (!range) return null;
            const selection = window.getSelection();
            if (!selection) return null;
            selection.removeAllRanges();
            selection.addRange(range);
            return {
              ok: true,
              prompt_target: summarizeElement(target),
              active_element: summarizeElement(document.activeElement),
              submit_state: summarizeSubmitButtonState(),
              overlay_count: overlayRoots.length,
            };
          }, timeoutMs, 120);
        };

        const closeVideoSettingsOverlays = async () => {
          const confirmClosed = async () =>
            await waitFor(() => {
              const overlayRoots = collectSettingsOverlayRoots();
              return overlayRoots.length ? null : { ok: true };
            }, 1600, 100);
          const attemptResults = [];
          const overlayRootsBefore = collectSettingsOverlayRoots().map((node) => summarizeElement(node));
          if (!overlayRootsBefore.length) {
            return { ok: true, skipped: "already_closed", attempt_results: [], overlay_count_before: 0, overlay_count_after: 0 };
          }

          document.body?.focus?.();
          dispatchKeyboard(document.body || document.documentElement, "Escape");
          let confirmed = await confirmClosed();
          attemptResults.push({ attempt: "escape_key", confirmed: !!confirmed });

          if (!confirmed) {
            const chip = collectVideoSettingsChipCandidates()[0]?.node || null;
            if (chip instanceof Element) {
              const rect = chip.getBoundingClientRect();
              if (rect.width > 0 && rect.height > 0) {
                clickAtPoint(rect.left + rect.width / 2, rect.top + rect.height / 2);
              } else {
                clickNode(chip, { nativeOnly: true });
              }
              await sleep(220);
              confirmed = await confirmClosed();
              attemptResults.push({ attempt: "chip_toggle", confirmed: !!confirmed, chip: summarizeElement(chip) });
            }
          }

          if (!confirmed) {
            clickAtPoint(Math.max(8, Math.round(window.innerWidth * 0.08)), Math.max(8, Math.round(window.innerHeight * 0.12)));
            await sleep(220);
            confirmed = await confirmClosed();
            attemptResults.push({ attempt: "outside_click", confirmed: !!confirmed });
          }

          return {
            ok: !!confirmed,
            reason: confirmed ? "" : "settings_overlay_close_not_confirmed",
            attempt_results: attemptResults,
            overlay_count_before: overlayRootsBefore.length,
            overlay_count_after: collectSettingsOverlayRoots().length,
            overlay_snapshot_before: overlayRootsBefore.slice(0, 4),
            overlay_snapshot_after: collectSettingsOverlayRoots().slice(0, 4).map((node) => summarizeElement(node)),
          };
        };

        const summarizeSubmitButtonState = () => {
          const button = findSubmitButton();
          if (!(button instanceof Element)) {
            return { found: false };
          }
          return {
            found: true,
            button: summarizeElement(button),
            disabled: !!button.disabled,
            aria_disabled: String(button.getAttribute("aria-disabled") || ""),
          };
        };

        const getSubmitFailureState = () => {
          const bodyText = String(document.body?.innerText || "").replace(/\s+/g, " ").trim();
          const visibleBlocks = Array.from(document.querySelectorAll("article, section, div, li"))
            .filter((node) => visible(node))
            .map((node) => ({ node, text: textOf(node) }))
            .filter((item) => item.text && item.text.length >= 2);
          const findBlock = (predicate) =>
            visibleBlocks
              .filter((item) => predicate(item.text))
              .sort((left, right) => left.text.length - right.text.length)[0];
          const abnormalActivity = findBlock((text) =>
            text.includes("异常活动") ||
            text.includes("我们发现了一些异常活动") ||
            (text.includes("失败") && text.includes("帮助中心"))
          );
          if (abnormalActivity) {
            return {
              kind: "abnormal_activity",
              message: abnormalActivity.text.slice(0, 240),
              card: summarizeElement(abnormalActivity.node),
            };
          }
          const genericFailure = findBlock((text) =>
            text.includes("生成失败") ||
            text.includes("提交失败") ||
            text.includes("出错了") ||
            text.includes("something went wrong")
          );
          if (genericFailure) {
            return {
              kind: "generation_failed",
              message: genericFailure.text.slice(0, 240),
              card: summarizeElement(genericFailure.node),
            };
          }
          if (bodyText.includes("异常活动") || (bodyText.includes("失败") && bodyText.includes("帮助中心"))) {
            return {
              kind: "abnormal_activity",
              message: bodyText.slice(0, 260),
              card: summarizeElement(document.body),
            };
          }
          return null;
        };

        const getSubmitHookSnapshot = () => {
          const hook = window.__FLOW2API_VIDEO_WORKFLOW_SUBMIT_HOOK__;
          if (!hook?.snapshot) {
            return { entries: [], errors: [] };
          }
          return hook.snapshot() || { entries: [], errors: [] };
        };

        const summarizeSlateState = (target) => {
          if (!(target instanceof Element) || target.getAttribute("data-slate-editor") !== "true") {
            return null;
          }
          return {
            slate_strings: Array.from(target.querySelectorAll("[data-slate-string='true']")).map((node) => String(node.textContent || "").slice(0, 120)),
            zero_width: Array.from(target.querySelectorAll("[data-slate-zero-width]")).map((node) => String(node.textContent || "").replace(/\u200b/g, "").slice(0, 80)),
            html: String(target.innerHTML || "").slice(0, 1200),
          };
        };

        const parsePromptState = () => {
          const raw = String(window.localStorage.getItem("FLOW_MAIN_PROMPT_BOX_STATE") || "");
          if (!raw) {
            return { raw, promptMode: "", selectedVideoModelFamily: "", selectedVideoDuration: "", outputsPerPrompt: "" };
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

        const collectModeToggleEvidence = () => {
          const candidates = Array.from(document.querySelectorAll("button, [role='button'], [role='tab'], [aria-pressed], [aria-selected]"))
            .filter((node) => visible(node))
            .map((node) => {
              const text = textOf(node).replace(/\s+/g, " ").trim();
              if (!text) return null;
              const normalized = text.toLowerCase();
              if (
                !normalized.includes("image")
                && !normalized.includes("video")
                && !text.includes("图片")
                && !text.includes("视频")
                && !normalized.includes("nano banana")
                && !normalized.includes("omni")
              ) {
                return null;
              }
              return {
                text: text.slice(0, 120),
                aria_pressed: node.getAttribute("aria-pressed"),
                aria_selected: node.getAttribute("aria-selected"),
                data_state: node.getAttribute("data-state"),
                class: String(node.className || "").slice(0, 240),
                element: summarizeElement(node),
              };
            })
            .filter(Boolean);
          return candidates.slice(0, 24);
        };

        const collectModeRuntimeEvidence = () => {
          const promptState = parsePromptState();
          const activeDialog = getActiveDialog();
          const mediaDialog = getMediaPickerDialog();
          return {
            href: String(location.href || ""),
            prompt_state: promptState,
            active_dialog: summarizeElement(activeDialog),
            media_picker_dialog: summarizeElement(mediaDialog),
            submit_state: summarizeSubmitButtonState(),
            mode_toggle_candidates: collectModeToggleEvidence(),
            visible_text_samples: Array.from(document.querySelectorAll("button, [role='button'], [role='tab']"))
              .filter((node) => visible(node))
              .map((node) => textOf(node).replace(/\s+/g, " ").trim())
              .filter(Boolean)
              .filter((text) => /视频|图片|image|video|nano banana|omni/i.test(text))
              .slice(0, 24),
          };
        };

        const isPromptMode = (promptState, mode) => {
          const normalizedMode = String(mode || "").trim().toLowerCase();
          const raw = String(promptState?.promptMode || "").trim().toLowerCase();
          if (!raw || !normalizedMode) return false;
          return raw.includes(normalizedMode) || (normalizedMode === "video" && raw.includes("vide")) || (normalizedMode === "image" && raw.includes("imag"));
        };

        const getConversationDrawerSnapshot = () => {
          const candidates = Array.from(document.querySelectorAll("aside, section, div"))
            .filter((node) => visible(node))
            .map((node) => {
              const rect = node.getBoundingClientRect();
              const text = textOf(node);
              const compact = text.replace(/\s+/g, "");
              const closeButtonEntries = Array.from(node.querySelectorAll("button, [role='button']"))
                .filter((child) => visible(child))
                .map((child) => {
                  const childRect = child.getBoundingClientRect();
                  const raw = `${textOf(child)} ${String(child.getAttribute?.("aria-label") || "")}`.replace(/\s+/g, "").toLowerCase();
                  const inDrawerHeader =
                    childRect.top >= rect.top - 20 &&
                    childRect.top <= rect.top + 112 &&
                    childRect.left >= rect.left - 24 &&
                    childRect.right <= rect.right + 24;
                  const nearDrawerTopRight =
                    childRect.top >= rect.top - 12 &&
                    childRect.top <= rect.top + 120 &&
                    childRect.left >= rect.right - Math.max(120, rect.width * 0.35);
                  const closeScore =
                    (raw.includes("close") || raw.includes("关闭") ? 100 : 0) +
                    (raw === "x" ? 30 : 0) +
                    (inDrawerHeader ? 45 : 0) +
                    (nearDrawerTopRight ? 35 : 0);
                  if (closeScore < 100) return null;
                  return {
                    node: child,
                    rect: childRect,
                    raw,
                    closeScore,
                  };
                })
                .filter(Boolean)
                .sort((a, b) => b.closeScore - a.closeScore || a.rect.top - b.rect.top || b.rect.left - a.rect.left);
              const closeButtonEntry = closeButtonEntries[0] || null;
              const hasHeader =
                compact.includes("未命名的会话") ||
                compact.includes("新建会话") ||
                compact.includes("conversation");
              const hasConversationComposer = !!Array.from(
                node.querySelectorAll("textarea, [contenteditable='true'], input[type='text']")
              ).find((child) => {
                if (!visible(child)) return false;
                const childRect = child.getBoundingClientRect();
                return childRect.bottom > window.innerHeight * 0.8;
              });
              const hasCloseButton = !!closeButtonEntry;
              const hasAgentConversationSignals =
                compact.includes("我将开始生成") ||
                compact.includes("批准") ||
                compact.includes("拒绝") ||
                compact.includes("letmeknowif") ||
                compact.includes("identifyingtheartifacts") ||
                compact.includes("您希望创作什么内容") ||
                compact.includes("你希望创作什么内容");
              const rightDocked =
                rect.left > window.innerWidth * 0.72 &&
                rect.right > window.innerWidth - 24;
              const drawerSized =
                rect.width >= 260 &&
                rect.width <= Math.min(420, window.innerWidth * 0.42) &&
                rect.height > window.innerHeight * 0.45;
              const score =
                (rightDocked ? 70 : 0) +
                (drawerSized ? 40 : 0) +
                (hasHeader ? 40 : 0) +
                (hasConversationComposer ? 40 : 0) +
                (hasCloseButton ? 40 : 0) +
                (hasAgentConversationSignals ? 28 : 0);
              return {
                node,
                rect,
                text,
                score,
                hasHeader,
                hasConversationComposer,
                hasCloseButton,
                hasAgentConversationSignals,
                rightDocked,
                drawerSized,
                closeButtonNode: closeButtonEntry?.node || null,
              };
            })
            .filter((item) => item.score >= 150)
            .filter((item) => item.rightDocked && item.drawerSized && item.hasCloseButton)
            .filter((item) => item.hasConversationComposer || item.hasHeader || item.hasAgentConversationSignals)
            .sort((a, b) => b.score - a.score || (b.rect.width * b.rect.height) - (a.rect.width * a.rect.height));
          return candidates[0] || null;
        };

        const closeConversationDrawer = async () => {
          const drawerSnapshot = getConversationDrawerSnapshot();
          const drawer = drawerSnapshot?.node || null;
          if (!drawer) return { acted: false, reason: "drawer_not_found" };
          const drawerRect = drawer.getBoundingClientRect();
          const closeButton = drawerSnapshot?.closeButtonNode || null;
          if (!closeButton) {
            return { acted: false, reason: "drawer_detected_without_close" };
          }
          const confirmClosed = async () => {
            const closed = await waitFor(() => {
              const nextSnapshot = getConversationDrawerSnapshot();
              if (!nextSnapshot) return { ok: true, mode: "snapshot_missing" };
              const nextNode = nextSnapshot.node;
              if (!(nextNode instanceof Element)) return { ok: true, mode: "snapshot_invalid" };
              if (nextNode !== drawer) return { ok: true, mode: "drawer_replaced" };
              const nextRect = nextNode.getBoundingClientRect();
              const nextStyle = window.getComputedStyle(nextNode);
              const effectivelyHidden =
                nextRect.width < 32 ||
                nextRect.right <= window.innerWidth - 8 ||
                nextRect.left >= window.innerWidth - 24 ||
                nextStyle.display === "none" ||
                nextStyle.visibility === "hidden" ||
                Number.parseFloat(String(nextStyle.opacity || "1")) < 0.08;
              return effectivelyHidden ? {
                ok: true,
                mode: "drawer_hidden",
                next_rect: {
                  left: Math.round(nextRect.left),
                  right: Math.round(nextRect.right),
                  width: Math.round(nextRect.width),
                  height: Math.round(nextRect.height),
                },
              } : null;
            }, 1800, 120);
            return closed;
          };
          const tryCloseByPoint = () => {
            const rect = closeButton.getBoundingClientRect();
            return clickAtPoint(
              Math.round(rect.left + rect.width / 2),
              Math.round(rect.top + rect.height / 2)
            );
          };
          const tryCloseByHotspot = () =>
            clickAtPoint(
              Math.max(4, Math.round(drawerRect.right - 18)),
              Math.max(4, Math.round(drawerRect.top + 18))
            );
          const tryCloseByEscape = () => {
            document.body?.focus?.();
            dispatchKeyboard(document.body || document.documentElement, "Escape");
            return dispatchKeyboard(closeButton, "Escape");
          };
          const attemptResults = [];
          clickNode(closeButton, { nativeOnly: true });
          let confirmed = await confirmClosed();
          attemptResults.push({ attempt: "native_click", confirmed: !!confirmed });
          if (!confirmed) {
            tryCloseByPoint();
            await sleep(220);
            confirmed = await confirmClosed();
            attemptResults.push({ attempt: "point_click", confirmed: !!confirmed });
          }
          if (!confirmed) {
            tryCloseByHotspot();
            await sleep(220);
            confirmed = await confirmClosed();
            attemptResults.push({ attempt: "hotspot_click", confirmed: !!confirmed });
          }
          if (!confirmed) {
            closeButton.focus?.();
            dispatchKeyboard(closeButton, "Enter");
            await sleep(220);
            confirmed = await confirmClosed();
            attemptResults.push({ attempt: "enter_key", confirmed: !!confirmed });
          }
          if (!confirmed) {
            tryCloseByEscape();
            await sleep(240);
            confirmed = await confirmClosed();
            attemptResults.push({ attempt: "escape_key", confirmed: !!confirmed });
          }
          const latestSnapshot = getConversationDrawerSnapshot();
          if (!confirmed) {
            return {
              acted: false,
              reason: "drawer_close_not_confirmed",
              button: summarizeElement(closeButton),
              attempt_results: attemptResults,
              drawer_snapshot: {
                has_header: drawerSnapshot?.hasHeader || false,
                has_conversation_composer: drawerSnapshot?.hasConversationComposer || false,
                has_close_button: drawerSnapshot?.hasCloseButton || false,
                has_agent_conversation_signals: drawerSnapshot?.hasAgentConversationSignals || false,
              },
              latest_drawer: latestSnapshot ? {
                rect: {
                  left: Math.round(latestSnapshot.rect.left),
                  right: Math.round(latestSnapshot.rect.right),
                  width: Math.round(latestSnapshot.rect.width),
                  height: Math.round(latestSnapshot.rect.height),
                },
                has_header: latestSnapshot.hasHeader,
                has_conversation_composer: latestSnapshot.hasConversationComposer,
                has_close_button: latestSnapshot.hasCloseButton,
                has_agent_conversation_signals: latestSnapshot.hasAgentConversationSignals,
              } : null,
            };
          }
          return {
            acted: true,
            action: "drawer_closed",
            confirmation: confirmed,
            button: summarizeElement(closeButton),
            drawer_snapshot: {
              has_header: drawerSnapshot?.hasHeader || false,
              has_conversation_composer: drawerSnapshot?.hasConversationComposer || false,
              has_close_button: drawerSnapshot?.hasCloseButton || false,
              has_agent_conversation_signals: drawerSnapshot?.hasAgentConversationSignals || false,
            },
          };
        };

        const waitForComposerUiSettle = async (timeoutMs = 2200) => {
          await sleep(220);
          return await waitFor(() => {
            const drawer = getConversationDrawerSnapshot();
            const activeDialog = summarizeElement(getActiveDialog());
            if (drawer) return null;
            if (activeDialog?.tag) return null;
            return {
              ok: true,
              drawer_visible: false,
              active_dialog: activeDialog,
            };
          }, timeoutMs, 120);
        };

        const collectAgentChipCandidates = () => {
          const promptTarget = findPromptTarget();
          const promptRect = promptTarget?.getBoundingClientRect?.() || null;
          return Array.from(document.querySelectorAll("button[aria-pressed], [role='button'][aria-pressed]"))
            .filter((node) => visible(node))
            .filter((node) => !node.closest?.("[role='dialog'], [aria-modal='true']"))
            .map((node) => {
              const rect = node.getBoundingClientRect();
              const raw = `${textOf(node)} ${textOf(node.parentElement)} ${String(node.getAttribute?.("aria-label") || "")}`.replace(/\s+/g, "").toLowerCase();
              const ariaPressed = String(node.getAttribute("aria-pressed") || "").toLowerCase();
              const hasPressedAttr = node.hasAttribute("aria-pressed");
              const nearPrompt =
                !!promptRect &&
                rect.bottom >= promptRect.top - 24 &&
                rect.top <= promptRect.bottom + 24;
              const bottomLeftComposer =
                rect.bottom > window.innerHeight * 0.72 &&
                rect.left < window.innerWidth * 0.45;
              const looksLikeAgent =
                raw.includes("智能体") ||
                raw.includes("agent") ||
                raw.includes("articlespark");
              const score =
                (looksLikeAgent ? 80 : 0) +
                (hasPressedAttr ? 36 : 0) +
                (ariaPressed === "true" ? 70 : 0) +
                (nearPrompt ? 35 : 0) +
                (bottomLeftComposer ? 28 : 0);
              if (score < 64) return null;
              return { node, score, rect, raw, ariaPressed, hasPressedAttr, nearPrompt, bottomLeftComposer };
            })
            .filter(Boolean)
            .sort((a, b) =>
              b.score - a.score ||
              b.hasPressedAttr - a.hasPressedAttr ||
              a.rect.left - b.rect.left ||
              b.rect.bottom - a.rect.bottom
            );
        };

        const findAgentChipNode = () => collectAgentChipCandidates()[0]?.node || null;

        const summarizeAgentChipState = (node) => {
          if (!(node instanceof Element)) return null;
          return {
            element: summarizeElement(node),
            aria_pressed: String(node.getAttribute("aria-pressed") || ""),
            aria_selected: String(node.getAttribute("aria-selected") || ""),
            aria_expanded: String(node.getAttribute("aria-expanded") || ""),
            aria_current: String(node.getAttribute("aria-current") || ""),
            data_state: String(node.getAttribute("data-state") || ""),
            data_active: String(node.getAttribute("data-active") || ""),
            data_selected: String(node.getAttribute("data-selected") || ""),
            class: String(node.className || "").slice(0, 240),
          };
        };

        const isAgentChipActive = (node) => {
          if (!(node instanceof Element)) return false;
          return String(node.getAttribute("aria-pressed") || "").toLowerCase() === "true";
        };

        const exitAgentModeIfNeeded = async () => {
          const agentChip = findAgentChipNode();
          const shouldExit = !!agentChip && isAgentChipActive(agentChip);
          if (!shouldExit) {
            return {
              acted: false,
              reason: agentChip ? "agent_exit_not_needed" : "agent_chip_not_found",
              agent_chip: summarizeAgentChipState(agentChip),
              agent_chip_candidates: collectAgentChipCandidates().slice(0, 4).map((item) => ({
                score: item.score,
                aria_pressed: item.ariaPressed,
                has_pressed_attr: item.hasPressedAttr,
                near_prompt: item.nearPrompt,
                bottom_left_composer: item.bottomLeftComposer,
                raw: item.raw.slice(0, 120),
                element: summarizeElement(item.node),
              })),
            };
          }
          const confirmInactive = async () =>
            await waitFor(() => {
              const nextChip = findAgentChipNode();
              if (!(nextChip instanceof Element)) return { ok: true, mode: "chip_missing_after_toggle" };
              return !isAgentChipActive(nextChip) ? {
                ok: true,
                mode: "aria_pressed_false",
                agent_chip: summarizeAgentChipState(nextChip),
              } : null;
            }, 2600, 120);
          const attemptResults = [];
          await sleep(120);
          clickNode(agentChip, { nativeOnly: true });
          let toggled = await confirmInactive();
          attemptResults.push({ attempt: "native_click", confirmed: !!toggled });
          if (!toggled) {
            const rect = agentChip.getBoundingClientRect();
            clickAtPoint(
              Math.round(rect.left + rect.width / 2),
              Math.round(rect.top + rect.height / 2)
            );
            await sleep(180);
            toggled = await confirmInactive();
            attemptResults.push({ attempt: "point_click", confirmed: !!toggled });
          }
          if (!toggled) {
            agentChip.focus?.();
            dispatchKeyboard(agentChip, "Enter");
            await sleep(180);
            toggled = await confirmInactive();
            attemptResults.push({ attempt: "enter_key", confirmed: !!toggled });
          }
          if (!toggled) {
            agentChip.focus?.();
            dispatchKeyboard(agentChip, " ");
            await sleep(180);
            toggled = await confirmInactive();
            attemptResults.push({ attempt: "space_key", confirmed: !!toggled });
          }
          return {
            acted: !!toggled,
            action: toggled ? "agent_chip_toggled" : "agent_chip_toggle_not_confirmed",
            agent_chip: summarizeAgentChipState(agentChip),
            confirmation: toggled || null,
            attempt_results: attemptResults,
          };
        };

        const findGenerationModeMenuTrigger = () =>
          Array.from(document.querySelectorAll("button, [role='button']"))
            .filter((node) => visible(node))
            .filter((node) => !node.closest?.("[role='dialog'], [aria-modal='true']"))
            .map((node) => {
              const rect = node.getBoundingClientRect();
              const text = textOf(node);
              const raw = `${text} ${String(node.getAttribute("aria-label") || "")}`.toLowerCase();
              const score =
                (rect.bottom > window.innerHeight * 0.72 ? 40 : 0) +
                (rect.left > window.innerWidth * 0.52 ? 18 : 0) +
                ((node.getAttribute("aria-haspopup") || node.getAttribute("data-state")) ? 12 : 0) +
                (/nano banana|omni|veo|imagen|x2|x3|x4|crop_16_9|crop_9_16/.test(raw) ? 20 : 0);
              if (score < 52) return null;
              return { node, score, text, rect };
            })
            .filter(Boolean)
            .sort((a, b) => b.score - a.score || b.rect.bottom - a.rect.bottom || b.rect.left - a.rect.left)[0]?.node || null;

        const clickAtPoint = (x, y) => {
          const target = document.elementFromPoint(x, y);
          if (!(target instanceof Element)) return { clicked: false };
          dispatchPointer("pointerdown", x, y, 1);
          dispatchPointer("pointerup", x, y, 0);
          try {
            target.dispatchEvent(new MouseEvent("click", {
              bubbles: true,
              cancelable: true,
              composed: true,
              clientX: Math.round(x),
              clientY: Math.round(y),
              button: 0,
              buttons: 0,
              view: window,
            }));
          } catch (_error) {
            // ignore synthetic click failure
          }
          if (typeof target.click === "function") {
            try {
              target.click();
            } catch (_error) {
              // ignore native click failure
            }
          }
          return { clicked: true, target: summarizeElement(target) };
        };

        const clickOverlayAlias = (aliases) => {
          const normalizedAliases = aliases
            .map((item) => String(item || "").trim())
            .filter(Boolean)
            .map((item) => item.replace(/\s+/g, "").toLowerCase());
          const overlayRoots = Array.from(document.querySelectorAll("[role='menu'], [data-radix-menu-content], [data-state='open']"))
            .filter((node) => visible(node));
          for (const root of overlayRoots) {
            const walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT);
            let current = walker.nextNode();
            while (current) {
              const rawText = String(current.textContent || "").trim();
              const compact = rawText.replace(/\s+/g, "").toLowerCase();
              const matched = normalizedAliases.some((alias) => compact === alias || compact.includes(alias) || alias.includes(compact));
              if (rawText && matched) {
                const range = document.createRange();
                range.selectNodeContents(current);
                const rect = range.getBoundingClientRect();
                if (rect.width > 4 && rect.height > 4) {
                  const click = clickAtPoint(rect.left + rect.width / 2, rect.top + rect.height / 2);
                  return { matched_text: rawText, click, root: summarizeElement(root) };
                }
              }
              current = walker.nextNode();
            }
          }
          return null;
        };

        const collectOpenModeOverlayRoots = () =>
          Array.from(document.querySelectorAll("[role='menu'], [data-radix-menu-content], [data-state='open']"))
            .filter((node) => visible(node));

        const waitForGenerationModeMenuReady = async (targetMode, timeoutMs = 900, intervalMs = 90) => {
          const normalizedMode = String(targetMode || "video").trim().toLowerCase();
          return await waitFor(() => {
            const candidate = collectGenerationModeTabCandidates(normalizedMode)[0]?.node || null;
            const overlayRoots = collectOpenModeOverlayRoots();
            if (candidate || overlayRoots.length > 0) {
              return {
                candidate,
                overlay_roots: overlayRoots.map((node) => summarizeElement(node)),
              };
            }
            return null;
          }, timeoutMs, intervalMs);
        };

        const openGenerationModeMenu = async (trigger, targetMode) => {
          if (!(trigger instanceof Element)) {
            return { opened: false, attempts: [] };
          }
          const attempts = [];

          clickNode(trigger);
          const firstReady = await waitForGenerationModeMenuReady(targetMode, 700, 80);
          attempts.push({
            attempt: "clickNode",
            trigger_state: String(trigger.getAttribute("data-state") || ""),
            menu_ready: !!firstReady,
            overlay_count: Array.isArray(firstReady?.overlay_roots) ? firstReady.overlay_roots.length : 0,
          });
          if (firstReady) {
            return { opened: true, attempts, ...firstReady };
          }

          const rect = trigger.getBoundingClientRect();
          const fallbackClick = clickAtPoint(rect.left + rect.width / 2, rect.top + rect.height / 2);
          await sleep(220);
          const secondReady = await waitForGenerationModeMenuReady(targetMode, 900, 80);
          attempts.push({
            attempt: "clickAtPoint",
            trigger_state: String(trigger.getAttribute("data-state") || ""),
            menu_ready: !!secondReady,
            overlay_count: Array.isArray(secondReady?.overlay_roots) ? secondReady.overlay_roots.length : 0,
            click: fallbackClick,
          });
          if (secondReady) {
            return { opened: true, attempts, click: fallbackClick, ...secondReady };
          }

          return { opened: false, attempts, click: fallbackClick };
        };

        const collectGenerationModeTabCandidates = (targetMode) => {
          const normalizedMode = String(targetMode || "video").trim().toLowerCase();
          const selectors = ["[role='tab']", "button", "[role='button']", "[role='menuitem']"].join(", ");
          const promptTarget = findPromptTarget();
          const promptRect = promptTarget?.getBoundingClientRect?.() || null;
          return Array.from(document.querySelectorAll(selectors))
            .filter((node) => visible(node))
            .map((node) => {
              const text = textOf(node);
              const compact = text.replace(/\s+/g, "").toLowerCase();
              const rect = node.getBoundingClientRect();
              const inNav = !!node.closest?.("nav, aside, [role='navigation']");
              const parentText = textOf(node.parentElement || null);
              const inOverlay = !!node.closest?.("[role='menu'], [data-radix-menu-content], [data-state='open']");
              const rawAria = `${String(node.getAttribute?.("aria-label") || "")} ${String(node.getAttribute?.("aria-controls") || "")}`.toLowerCase();
              const looksLikeVideoToggle =
                text === "视频" ||
                text.includes("视频 ·") ||
                text.includes("play_circle 视频") ||
                compact.includes("play_circle视频");
              const looksLikeImageToggle =
                text === "图片" ||
                text.includes("图片 ·") ||
                compact === "图片" ||
                compact.includes("image图片");
              const looksLikeTarget = normalizedMode === "video" ? looksLikeVideoToggle : looksLikeImageToggle;
              if (!looksLikeTarget) return null;
              if (inNav) return null;
              if (compact.includes("观看视频") || compact.includes("videocam观看视频")) return null;
              if (compact.includes("查看设置")) return null;
              if (rawAria.includes("settings")) return null;
              const distanceScore = promptRect
                ? Math.abs(rect.bottom - promptRect.top) + Math.abs(rect.left - promptRect.left)
                : 999999;
              return {
                node,
                text,
                inOverlayScore: inOverlay ? 0 : 5,
                roleScore: node.getAttribute?.("role") === "tab" ? 0 : 1,
                selectedScore: String(node.getAttribute?.("aria-selected") || "").toLowerCase() === "true" ? 2 : 0,
                parentScore: parentText.includes("图片") || parentText.includes("视频") || parentText.includes("Veo") ? 0 : 3,
                distanceScore,
              };
            })
            .filter(Boolean)
            .sort((a, b) =>
              a.inOverlayScore - b.inOverlayScore ||
              a.roleScore - b.roleScore ||
              a.selectedScore - b.selectedScore ||
              a.parentScore - b.parentScore ||
              a.distanceScore - b.distanceScore
            );
        };

        const ensureGenerationMode = async (targetMode) => {
          const normalizedMode = String(targetMode || "video").trim().toLowerCase();
          const before = parsePromptState();
          const promptTargetBefore = findPromptTarget();
          if (isPromptMode(before, normalizedMode) && promptTargetBefore) {
            return { ok: true, acted: false, action: "mode_already_selected", target_mode: normalizedMode, prompt_state_before: before };
          }
          const directCandidate = collectGenerationModeTabCandidates(normalizedMode)[0]?.node || null;
          const trigger = findGenerationModeMenuTrigger();
          if (directCandidate) {
            clickNode(directCandidate);
          } else {
            if (!(trigger instanceof Element)) {
              return { ok: false, acted: false, reason: "mode_menu_trigger_not_found", target_mode: normalizedMode, prompt_state_before: before };
            }
            const menuOpenResult = await openGenerationModeMenu(trigger, normalizedMode);
            const openedCandidate = menuOpenResult?.candidate || collectGenerationModeTabCandidates(normalizedMode)[0]?.node || null;
            if (openedCandidate) {
              clickNode(openedCandidate);
            } else {
              const aliasClick = clickOverlayAlias(normalizedMode === "video" ? ["视频", "play_circle视频"] : ["图片", "image图片"]);
              if (!aliasClick?.click?.clicked) {
                return {
                  ok: false,
                  acted: true,
                  reason: menuOpenResult?.opened ? "target_mode_option_not_found" : "mode_menu_open_failed",
                  target_mode: normalizedMode,
                  trigger: summarizeElement(trigger),
                  menu_open_result: menuOpenResult,
                };
              }
            }
          }
          const after = await waitFor(() => {
            const current = parsePromptState();
            return isPromptMode(current, normalizedMode) ? current : null;
          }, 3500, 120);
          return {
            ok: !!after,
            acted: true,
            action: `mode_switched_${normalizedMode}`,
            target_mode: normalizedMode,
            trigger: summarizeElement(trigger),
            prompt_state_before: before,
            prompt_state_after: after || parsePromptState(),
          };
        };

        const normalizeUiToken = (value) => String(value || "").replace(/\s+/g, "").trim().toLowerCase();

        const desiredVideoSettings = {
          model_key: String(jobPayload.desired_model_key || "").trim(),
          model_display_name: String(jobPayload.desired_model_display_name || "").trim(),
          duration_seconds: String(jobPayload.desired_duration_seconds || "").trim(),
          aspect_ratio_label: String(jobPayload.desired_aspect_ratio_label || "").trim(),
          outputs_per_prompt: Number.parseInt(String(jobPayload.desired_outputs_per_prompt || "").trim(), 10) || 0,
        };

        const hasDesiredVideoSettings = () =>
          !!desiredVideoSettings.model_key ||
          !!desiredVideoSettings.model_display_name ||
          !!desiredVideoSettings.duration_seconds ||
          !!desiredVideoSettings.aspect_ratio_label ||
          desiredVideoSettings.outputs_per_prompt > 0;

        const getDesiredModelFamilyKey = () => {
          const desired = normalizeUiToken(desiredVideoSettings.model_display_name);
          const modelKey = normalizeUiToken(desiredVideoSettings.model_key);
          if (desired.includes("omni") || modelKey.includes("omni") || modelKey.includes("abra")) {
            return "abra";
          }
          if (desired.includes("lite") || modelKey.includes("lite")) {
            return "veo_3_1_lite";
          }
          if (
            desired.includes("fast") ||
            modelKey.includes("fast") ||
            modelKey.includes("ultra") ||
            modelKey.includes("relaxed")
          ) {
            return "veo_3_1_fast";
          }
          if (desired.includes("quality") || modelKey.includes("veo_3_1") || modelKey.startsWith("veo")) {
            return "veo_3_1_quality";
          }
          return "";
        };

        const buildDesiredModelAliases = () => {
          const desired = String(desiredVideoSettings.model_display_name || "").trim();
          const desiredNormalized = normalizeUiToken(desired);
          const modelKey = normalizeUiToken(desiredVideoSettings.model_key);
          if (desiredNormalized.includes("omni") || modelKey.includes("omni") || modelKey.includes("abra")) {
            return ["Omni Flash", "OmniFlash", "omni flash", "omni", "abra"];
          }
          const aliases = [];
          if (desired) {
            aliases.push(desired, desired.replace(/\s*-\s*/g, " "));
          }
          if (desiredNormalized.includes("lite") || modelKey.includes("lite")) {
            aliases.push("Veo 3.1 - Lite", "Veo 3.1 Lite", "veo 3.1 lite", "veo_3_1_lite", "lite");
          } else if (
            desiredNormalized.includes("fast") ||
            modelKey.includes("fast") ||
            modelKey.includes("ultra") ||
            modelKey.includes("relaxed")
          ) {
            aliases.push("Veo 3.1 - Fast", "Veo 3.1 Fast", "veo 3.1 fast", "veo_3_1_fast", "fast");
            if (modelKey.includes("ultra")) aliases.push("ultra");
            if (modelKey.includes("relaxed")) aliases.push("relaxed");
          } else if (desiredNormalized.includes("quality") || modelKey.includes("veo_3_1") || modelKey.startsWith("veo")) {
            aliases.push("Veo 3.1 - Quality", "Veo 3.1 Quality", "veo 3.1 quality", "veo_3_1_quality", "quality");
          }
          return Array.from(new Set(aliases.filter(Boolean)));
        };

        const isDesiredModelSelected = (promptState) => {
          const desiredFamily = getDesiredModelFamilyKey();
          const desired = normalizeUiToken(desiredVideoSettings.model_display_name);
          if (!desired && !desiredFamily) return true;
          const current = normalizeUiToken(promptState?.selectedVideoModelFamily || "");
          if (desiredFamily === "abra") return current.includes("abra");
          if (desiredFamily) return current.includes(desiredFamily);
          return current.includes(desired);
        };

        const isDesiredDurationSelected = (promptState) => {
          const desired = String(desiredVideoSettings.duration_seconds || "").trim();
          if (!desired) return true;
          const current = String(promptState?.selectedVideoDuration || "").trim();
          return current === desired;
        };

        const isDesiredAspectRatioSelected = (promptState) => {
          const desired = normalizeUiToken(desiredVideoSettings.aspect_ratio_label);
          if (!desired) return true;
          const current = normalizeUiToken(promptState?.aspectRatio || "");
          if (desired === "9:16") return current.includes("portrait");
          if (desired === "16:9") return current.includes("landscape");
          return current.includes(desired);
        };

        const isDesiredAspectRatioVisibleOnChip = () => {
          const desired = normalizeUiToken(desiredVideoSettings.aspect_ratio_label);
          if (!desired) return true;
          const chip = collectVideoSettingsChipCandidates()[0]?.node || null;
          if (!(chip instanceof Element)) return false;
          const compact = normalizeUiToken(textOf(chip));
          if (desired === "9:16") {
            return compact.includes("9:16") || compact.includes("crop_9_16");
          }
          if (desired === "16:9") {
            return compact.includes("16:9") || compact.includes("crop_16_9");
          }
          return compact.includes(desired);
        };

        const isDesiredOutputsSelected = (promptState) => {
          if (!(desiredVideoSettings.outputs_per_prompt > 0)) return true;
          const current = Number.parseInt(String(promptState?.outputsPerPrompt || "").trim(), 10);
          return Number.isFinite(current) && current === desiredVideoSettings.outputs_per_prompt;
        };

        const collectVideoSettingsChipCandidates = () => {
          const promptTarget = findPromptTarget();
          const promptRect = promptTarget?.getBoundingClientRect?.() || null;
          const submitButton = findSubmitButton();
          const submitRect = submitButton?.getBoundingClientRect?.() || null;
          return Array.from(document.querySelectorAll("button, [role='button'], div"))
            .filter((node) => visible(node))
            .filter((node) => !node.closest?.("[role='dialog'], [aria-modal='true']"))
            .map((node) => {
              const text = textOf(node);
              const compact = normalizeUiToken(text);
              const rect = node.getBoundingClientRect();
              const ariaHasPopup = String(node.getAttribute("aria-haspopup") || "").toLowerCase();
              const dataState = String(node.getAttribute("data-state") || "").toLowerCase();
              const nearSubmitLeft =
                !!submitRect &&
                rect.right <= submitRect.left + 12 &&
                rect.left >= submitRect.left - 320 &&
                Math.abs(rect.bottom - submitRect.bottom) <= 40;
              const sameComposerRow =
                !!promptRect &&
                rect.bottom >= promptRect.top - 36 &&
                rect.top <= promptRect.bottom + 48;
              const dynamicSettingsLike =
                /crop_16_9|crop_9_16|16:9|9:16|1x|x2|x3|x4|4s|6s|8s|10s/.test(compact);
              const score =
                ((compact.includes("视频·") || compact.includes("视频.") || compact.startsWith("视频")) ? 50 : 0) +
                (dynamicSettingsLike ? 65 : 0) +
                ((/\bx[1-4]\b/.test(text) || compact.includes("x2") || compact.includes("x3") || compact.includes("x4") || compact.includes("1x")) ? 35 : 0) +
                (sameComposerRow ? 40 : 0) +
                (nearSubmitLeft ? 70 : 0) +
                ((rect.bottom > window.innerHeight * 0.72) ? 30 : 0) +
                ((ariaHasPopup === "dialog" || ariaHasPopup === "menu" || dataState) ? 15 : 0);
              if (score < 120) return null;
              return {
                node,
                text,
                rect,
                score,
                nearSubmitLeft,
                sameComposerRow,
                dynamicSettingsLike,
                summary: summarizeElement(node),
              };
            })
            .filter(Boolean)
            .sort((a, b) => b.score - a.score || b.rect.bottom - a.rect.bottom || b.rect.left - a.rect.left);
        };

        const collectOpenVideoSettingsMenuRoots = () =>
          Array.from(document.querySelectorAll("[role='menu'], [data-radix-menu-content], [data-state='open']"))
            .filter((node) => visible(node))
            .map((node) => {
              const text = textOf(node);
              const compact = normalizeUiToken(text);
              const score =
                (compact.includes("omni") || compact.includes("veo") ? 60 : 0) +
                (compact.includes("4s") || compact.includes("6s") || compact.includes("8s") || compact.includes("10s") ? 40 : 0) +
                (compact.includes("1x") || compact.includes("x2") || compact.includes("x3") || compact.includes("x4") ? 25 : 0) +
                (compact.includes("9:16") || compact.includes("16:9") || compact.includes("crop_9_16") || compact.includes("crop_16_9") ? 25 : 0) +
                (node.getAttribute("role") === "menu" ? 18 : 0);
              if (score < 40) return null;
              return { node, score, text };
            })
            .filter(Boolean)
            .sort((a, b) => b.score - a.score);

        const getOpenVideoSettingsMenuRoot = () => collectOpenVideoSettingsMenuRoots()[0]?.node || null;

        const collectSettingsOverlayRoots = () =>
          Array.from(document.querySelectorAll(
            [
              "[role='menu']",
              "[data-radix-menu-content]",
              "[data-radix-popper-content-wrapper]",
              "[data-state='open']",
              "[role='listbox']",
            ].join(", ")
          )).filter((node) => visible(node));

        const resolveSettingCandidateNode = (node) => {
          let current = node instanceof Element ? node : null;
          while (current instanceof Element) {
            const role = String(current.getAttribute("role") || "").toLowerCase();
            if (
              current.matches("button, [role='button'], [role='tab'], [role='option'], [role='menuitem'], [role='menuitemradio'], [role='menuitemcheckbox']") ||
              ["option", "menuitem", "menuitemradio", "menuitemcheckbox", "tab", "button"].includes(role)
            ) {
              return current;
            }
            current = current.parentElement;
          }
          return node instanceof Element && visible(node) ? node : null;
        };

        const computeAliasScore = (normalizedText, aliases) => {
          let score = Number.POSITIVE_INFINITY;
          for (const alias of aliases || []) {
            const normalizedAlias = normalizeUiToken(alias);
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

        const collectSettingOptionCandidates = (overlayOnly = true) => {
          const selectors = [
            "button",
            "[role='button']",
            "[role='tab']",
            "[role='option']",
            "[role='menuitem']",
            "[role='menuitemradio']",
            "[role='menuitemcheckbox']",
            "div",
            "span",
          ].join(", ");
          const roots = overlayOnly ? collectSettingsOverlayRoots() : [];
          const scopes = roots.length ? roots : [document.body];
          const seen = new Set();
          return scopes
            .flatMap((root, scopeIndex) =>
              Array.from(root.querySelectorAll(selectors))
                .map((node) => {
                  const candidateNode = resolveSettingCandidateNode(node);
                  if (!(candidateNode instanceof Element) || !visible(candidateNode)) return null;
                  if (seen.has(candidateNode)) return null;
                  seen.add(candidateNode);
                  const text = textOf(candidateNode);
                  const normalized = normalizeUiToken(text);
                  if (!text) return null;
                  return {
                    node: candidateNode,
                    text,
                    normalized,
                    scopeIndex,
                    inOverlay: roots.length ? !!candidateNode.closest("[role='menu'], [role='listbox'], [data-radix-popper-content-wrapper], [data-state='open']") : false,
                  };
                })
                .filter(Boolean)
            )
            .sort((left, right) => {
              if (left.scopeIndex !== right.scopeIndex) return left.scopeIndex - right.scopeIndex;
              if (left.inOverlay !== right.inOverlay) return left.inOverlay ? -1 : 1;
              return left.text.length - right.text.length;
            });
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
                const normalizedText = normalizeUiToken(rawText);
                const matchScore = computeAliasScore(normalizedText, aliases);
                if (rawText && Number.isFinite(matchScore)) {
                  const parentNode = resolveSettingCandidateNode(currentNode.parentElement || root);
                  if (parentNode) {
                    textNodeMatches.push({
                      node: parentNode,
                      text: textOf(parentNode) || rawText,
                      normalized: normalizeUiToken(textOf(parentNode) || rawText),
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
          const family = normalizeUiToken(promptState?.selectedVideoModelFamily || "");
          if (family === "abra" || family.includes("abra") || family.includes("omni")) {
            return ["Omni Flash", "OmniFlash", "arrow_drop_down"];
          }
          if (family.includes("veo_3_1_lite")) {
            return ["Veo 3.1 - Lite", "Veo 3.1 Lite", "veo_3_1_lite", "arrow_drop_down"];
          }
          if (family.includes("veo_3_1_fast")) {
            return ["Veo 3.1 - Fast", "Veo 3.1 Fast", "veo_3_1_fast", "arrow_drop_down"];
          }
          if (family.includes("veo_3_1_quality")) {
            return ["Veo 3.1 - Quality", "Veo 3.1 Quality", "veo_3_1_quality", "arrow_drop_down"];
          }
          if (family.includes("veo")) {
            return ["Veo 3.1", "Veo", "arrow_drop_down"];
          }
          return ["arrow_drop_down"];
        };

        const openModelDropdownInsideOverlay = async (promptState) => {
          const triggerMatch = findSettingOptionCandidate(getCurrentModelDisplayAliases(promptState));
          const triggerCandidate = triggerMatch?.candidate || null;
          if (!triggerCandidate) {
            return {
              opened: false,
              trigger_source: triggerMatch?.source || "",
              scanned: triggerMatch?.scanned || [],
            };
          }
          const beforeOverlays = collectSettingsOverlayRoots().map((root) => summarizeElement(root));
          const overlayTarget =
            Array.from(triggerCandidate.node.querySelectorAll("*"))
              .find((node) => String(node.getAttribute?.("data-type") || "").toLowerCase() === "button-overlay") || null;
          const iconTarget =
            Array.from(triggerCandidate.node.querySelectorAll("*"))
              .find((node) => normalizeUiToken(textOf(node)).includes("arrow_drop_down")) || null;
          const preferredTarget =
            overlayTarget instanceof Element
              ? overlayTarget
              : iconTarget instanceof Element
                ? iconTarget
                : triggerCandidate.node;
          const beforeExpanded = String(triggerCandidate.node.getAttribute("aria-expanded") || "");
          const beforeState = String(triggerCandidate.node.getAttribute("data-state") || "");
          const openSignals = () => {
            const afterExpandedValue = String(triggerCandidate.node.getAttribute("aria-expanded") || "");
            const afterStateValue = String(triggerCandidate.node.getAttribute("data-state") || "");
            const overlayRoots = collectSettingsOverlayRoots();
            const hasModelListHint = overlayRoots.some((root) => {
              const compact = normalizeUiToken(textOf(root));
              return compact.includes("omniflash") || compact.includes("veo3.1quality") || compact.includes("veo3.1fast");
            });
            return (
              afterExpandedValue === "true" ||
              afterStateValue === "open" ||
              hasModelListHint ||
              overlayRoots.length > beforeOverlays.length
            );
          };
          const attemptResults = [];
          const preferredRect = preferredTarget.getBoundingClientRect?.() || null;
          if (preferredRect && preferredRect.width > 0 && preferredRect.height > 0) {
            const hoverX = preferredRect.left + preferredRect.width / 2;
            const hoverY = preferredRect.top + preferredRect.height / 2;
            dispatchPointer("pointermove", hoverX, hoverY, 0);
            attemptResults.push({ attempt: "pointer_move", target: summarizeElement(preferredTarget) });
            await sleep(180);
          }
          if (preferredRect && preferredRect.width > 0 && preferredRect.height > 0) {
            const pointClick = clickAtPoint(preferredRect.left + preferredRect.width / 2, preferredRect.top + preferredRect.height / 2);
            attemptResults.push({ attempt: "point_click", target: summarizeElement(preferredTarget), click: pointClick });
            await sleep(220);
          }
          const afterOverlays = collectSettingsOverlayRoots().map((root) => summarizeElement(root));
          return {
            opened: openSignals(),
            trigger_source: triggerMatch?.source || "",
            trigger_text: triggerCandidate.text,
            trigger: summarizeElement(triggerCandidate.node),
            click_target: summarizeElement(preferredTarget),
            trigger_state_before: { aria_expanded: beforeExpanded, data_state: beforeState },
            trigger_state_after: {
              aria_expanded: String(triggerCandidate.node.getAttribute("aria-expanded") || ""),
              data_state: String(triggerCandidate.node.getAttribute("data-state") || ""),
            },
            attempt_names: attemptResults.map((item) => item.attempt),
            overlay_count_before: beforeOverlays.length,
            overlay_count_after: afterOverlays.length,
          };
        };

        const selectVideoSettingOption = async ({
          kind,
          aliases,
          targetLabel,
          verifySelected,
          extraSelectedCheck = null,
          optionNotFoundReason,
          notConfirmedReason,
        }) => {
          const before = parsePromptState();
          const verify = (state) =>
            (typeof verifySelected === "function" ? !!verifySelected(state) : false) ||
            (typeof extraSelectedCheck === "function" ? !!extraSelectedCheck() : false);
          if (!targetLabel) {
            return { ok: true, skipped: "empty_target", before_state: before, after_state: before };
          }
          if (verify(before)) {
            return { ok: true, skipped: "already_selected", before_state: before, after_state: before };
          }
          const overlaysBeforeOpen = collectSettingsOverlayRoots();
          const openResult = overlaysBeforeOpen.length
            ? { ok: true, skipped: "reuse_already_open", menu_count: overlaysBeforeOpen.length }
            : await openVideoSettingsChip();
          if (!openResult?.ok) {
            return { ok: false, reason: openResult?.reason || "settings_menu_not_found", before_state: before, after_state: parsePromptState() };
          }
          let overlayRoots = [];
          let optionMatch = null;
          for (let attempt = 0; attempt < 6; attempt += 1) {
            await sleep(attempt === 0 ? (overlaysBeforeOpen.length ? 80 : 250) : 180);
            overlayRoots = collectSettingsOverlayRoots();
            optionMatch = findSettingOptionCandidate(aliases);
            if (optionMatch?.candidate && overlayRoots.length && !optionMatch.candidate.inOverlay) {
              optionMatch = null;
            }
            if (optionMatch?.candidate) break;
          }
          let nestedModelOpen = null;
          if (!optionMatch?.candidate && kind === "model") {
            nestedModelOpen = await openModelDropdownInsideOverlay(before);
            if (nestedModelOpen?.opened) {
              for (let attempt = 0; attempt < 6; attempt += 1) {
                await sleep(attempt === 0 ? 220 : 180);
                overlayRoots = collectSettingsOverlayRoots();
                optionMatch = findSettingOptionCandidate(aliases);
                if (optionMatch?.candidate && overlayRoots.length && !optionMatch.candidate.inOverlay) {
                  optionMatch = null;
                }
                if (optionMatch?.candidate) break;
              }
            }
          }
          const target = optionMatch?.candidate?.node || null;
          const clickAttempts = [];
          if (!(target instanceof Element)) {
            return {
              ok: false,
              reason: optionNotFoundReason,
              desired: targetLabel,
              before_state: before,
              after_state: parsePromptState(),
              open_result: openResult,
              option_match: {
                source: String(optionMatch?.source || ""),
                scanned: Array.isArray(optionMatch?.scanned) ? optionMatch.scanned.slice(0, 8) : [],
              },
              nested_model_open: nestedModelOpen,
              overlay_snapshot: overlayRoots.slice(0, 4).map((root) => summarizeElement(root)),
            };
          }
          const rect = target.getBoundingClientRect?.() || null;
          if (rect && rect.width > 0 && rect.height > 0) {
            clickAttempts.push({
              attempt: "point_click",
              target: summarizeElement(target),
              click: clickAtPoint(rect.left + rect.width / 2, rect.top + rect.height / 2),
            });
          } else {
            clickAttempts.push({
              attempt: "native_click",
              target: summarizeElement(target),
              clicked: clickNode(target, { nativeOnly: true }),
            });
          }
          await sleep(320);
          const after = await waitFor(() => {
            const current = parsePromptState();
            return verify(current) ? current : null;
          }, 2400, 120);
          return {
            ok: !!after,
            desired: targetLabel,
            trigger: target instanceof Element ? summarizeElement(target) : summarizeElement(getOpenVideoSettingsMenuRoot()),
            before_state: before,
            after_state: after || parsePromptState(),
            open_result: openResult,
            option_match: {
              source: String(optionMatch?.source || ""),
              scanned: Array.isArray(optionMatch?.scanned) ? optionMatch.scanned.slice(0, 8) : [],
              candidate: optionMatch?.candidate ? {
                text: String(optionMatch.candidate.text || ""),
                summary: summarizeElement(optionMatch.candidate.node),
              } : null,
            },
            nested_model_open: nestedModelOpen,
            click_attempts: clickAttempts,
            overlay_snapshot: overlayRoots.slice(0, 4).map((root) => summarizeElement(root)),
            reason: after ? "" : notConfirmedReason,
          };
        };

        const openVideoSettingsChip = async () => {
          const candidates = collectVideoSettingsChipCandidates();
          const chip = candidates[0]?.node || null;
          if (!(chip instanceof Element)) {
            return {
              ok: false,
              reason: "settings_chip_not_found",
              candidates: [],
            };
          }
          const attemptResults = [];
          let menu = null;
          const rect = chip.getBoundingClientRect();
          if (rect.width > 0 && rect.height > 0) {
            const pointClick = clickAtPoint(rect.left + rect.width / 2, rect.top + rect.height / 2);
            attemptResults.push({ attempt: "point_click", click: pointClick });
            menu = await waitFor(() => getOpenVideoSettingsMenuRoot(), 1800, 100);
          } else {
            clickNode(chip, { nativeOnly: true });
            attemptResults.push({ attempt: "native_click", chip: summarizeElement(chip) });
            menu = await waitFor(() => getOpenVideoSettingsMenuRoot(), 1800, 100);
          }
          return {
            ok: !!menu,
            reason: menu ? "" : "settings_menu_not_found",
            chip: summarizeElement(chip),
            menu: summarizeElement(menu),
            attempt_results: attemptResults,
            candidates: candidates.slice(0, 5).map((item) => ({
              score: item.score,
              near_submit_left: item.nearSubmitLeft,
              same_composer_row: item.sameComposerRow,
              dynamic_settings_like: item.dynamicSettingsLike,
              element: item.summary,
            })),
          };
        };

        const selectAspectRatioIfNeeded = async () => {
          const desired = String(desiredVideoSettings.aspect_ratio_label || "").trim();
          const aliases =
            desired === "9:16"
              ? ["9:16", "portrait", "crop_9_16", "trigger-portrait"]
              : desired === "16:9"
                ? ["16:9", "landscape", "crop_16_9", "trigger-landscape"]
                : [desired];
          return await selectVideoSettingOption({
            kind: "aspect_ratio",
            aliases,
            targetLabel: desired,
            verifySelected: (state) => isDesiredAspectRatioSelected(state),
            extraSelectedCheck: () => isDesiredAspectRatioVisibleOnChip(),
            optionNotFoundReason: "aspect_ratio_option_not_found",
            notConfirmedReason: "aspect_ratio_not_confirmed",
          });
        };

        const selectModelIfNeeded = async () => {
          const desired = String(desiredVideoSettings.model_display_name || "").trim();
          const aliases = buildDesiredModelAliases();
          return await selectVideoSettingOption({
            kind: "model",
            aliases,
            targetLabel: desired,
            verifySelected: (state) => isDesiredModelSelected(state),
            optionNotFoundReason: "model_option_not_found",
            notConfirmedReason: "model_not_confirmed",
          });
        };

        const selectDurationIfNeeded = async () => {
          const desired = String(desiredVideoSettings.duration_seconds || "").trim();
          return await selectVideoSettingOption({
            kind: "duration",
            aliases: [`${desired}秒`, `${desired} s`, `${desired}s`, desired],
            targetLabel: desired,
            verifySelected: (state) => isDesiredDurationSelected(state),
            optionNotFoundReason: "duration_option_not_found",
            notConfirmedReason: "duration_not_confirmed",
          });
        };

        const selectOutputsIfNeeded = async () => {
          const desired = `${desiredVideoSettings.outputs_per_prompt}x`;
          return await selectVideoSettingOption({
            kind: "outputs_per_prompt",
            aliases: [desired, `x${desiredVideoSettings.outputs_per_prompt}`, String(desiredVideoSettings.outputs_per_prompt)],
            targetLabel: desired,
            verifySelected: (state) => isDesiredOutputsSelected(state),
            optionNotFoundReason: "outputs_option_not_found",
            notConfirmedReason: "outputs_not_confirmed",
          });
        };

        const applyDesiredVideoSettings = async () => {
          const before = parsePromptState();
          if (!hasDesiredVideoSettings()) {
            return { ok: true, skipped: "no_desired_settings", before_state: before, after_state: before, actions: [] };
          }
          const actions = [];
          const modelResult = await selectModelIfNeeded();
          actions.push({ kind: "model", ...modelResult });
          if (!modelResult.ok) {
            return { ok: false, reason: modelResult.reason || "model_failed", before_state: before, after_state: parsePromptState(), actions };
          }
          await humanPause(220, 480);
          const durationResult = await selectDurationIfNeeded();
          actions.push({ kind: "duration", ...durationResult });
          if (!durationResult.ok) {
            return { ok: false, reason: durationResult.reason || "duration_failed", before_state: before, after_state: parsePromptState(), actions };
          }
          await humanPause(220, 480);
          const aspectResult = await selectAspectRatioIfNeeded();
          actions.push({ kind: "aspect_ratio", ...aspectResult });
          if (!aspectResult.ok) {
            return { ok: false, reason: aspectResult.reason || "aspect_ratio_failed", before_state: before, after_state: parsePromptState(), actions };
          }
          await humanPause(220, 480);
          const outputsResult = await selectOutputsIfNeeded();
          actions.push({ kind: "outputs_per_prompt", ...outputsResult });
          if (!outputsResult.ok) {
            return { ok: false, reason: outputsResult.reason || "outputs_per_prompt_failed", before_state: before, after_state: parsePromptState(), actions };
          }
          const after = parsePromptState();
          return {
            ok:
              isDesiredModelSelected(after) &&
              isDesiredDurationSelected(after) &&
              isDesiredAspectRatioSelected(after) &&
              isDesiredOutputsSelected(after),
            reason:
              isDesiredModelSelected(after) &&
              isDesiredDurationSelected(after) &&
              isDesiredAspectRatioSelected(after) &&
              isDesiredOutputsSelected(after)
                ? ""
                : "desired_settings_not_confirmed",
            requested: desiredVideoSettings,
            before_state: before,
            after_state: after,
            actions,
          };
        };

        const normalizeComposerEntryState = async (targetMode) => {
          const before = collectModeRuntimeEvidence();
          const drawerAction = await closeConversationDrawer();
          const drawerSettle = await waitForComposerUiSettle(drawerAction?.acted ? 2600 : 1200);
          const drawerStillVisible = !!getConversationDrawerSnapshot();
          const drawerBlocked =
            drawerAction?.reason === "drawer_detected_without_close" ||
            drawerAction?.reason === "drawer_close_not_confirmed" ||
            (drawerAction?.action === "drawer_closed" && !drawerSettle && drawerStillVisible);
          if (drawerBlocked) {
            const afterBlocked = collectModeRuntimeEvidence();
            return {
              ok: false,
              reason: "conversation_drawer_blocking",
              target_mode: targetMode,
              before,
              after: afterBlocked,
              drawer_action: drawerAction,
              drawer_settle: drawerSettle || null,
              drawer_still_visible: drawerStillVisible,
            };
          }
          await sleep(220);
          const agentAction = await exitAgentModeIfNeeded();
          const postAgentChip = findAgentChipNode();
          const agentStillActive = !!postAgentChip && isAgentChipActive(postAgentChip);
          if (agentStillActive) {
            const afterBlocked = collectModeRuntimeEvidence();
            return {
              ok: false,
              reason: "agent_mode_still_active",
              target_mode: targetMode,
              before,
              after: afterBlocked,
              drawer_action: drawerAction,
              agent_action: agentAction,
              agent_chip: summarizeAgentChipState(postAgentChip),
            };
          }
          const modeAction = await ensureGenerationMode(targetMode);
          const after = collectModeRuntimeEvidence();
          return {
            ok: modeAction.ok || isPromptMode(after.prompt_state, targetMode),
            target_mode: targetMode,
            before,
            after,
            drawer_action: drawerAction,
            agent_action: agentAction,
            mode_action: modeAction,
          };
        };

        const collectPromptRuntimeEvidence = (target) => ({
          prompt_target: summarizeElement(target),
          prompt_state: parsePromptState(),
          prompt_slate: summarizeSlateState(target),
          prompt_dom: target
            ? {
                text: String(target.innerText || target.textContent || "").slice(0, 240),
                html: String(target.innerHTML || "").slice(0, 1200),
                child_count: Number(target.childNodes?.length || 0),
                is_connected: !!target.isConnected,
              }
            : null,
          active_element: summarizeElement(document.activeElement),
          submit_state: summarizeSubmitButtonState(),
        });

        const installVideoEditSubmitHook = () => {
          const globalKey = "__FLOW2API_VIDEO_WORKFLOW_SUBMIT_HOOK__";
          const existing = window[globalKey];
          if (existing?.snapshot) return existing;
          const targetPattern = /\/v1\/video:batchAsyncGenerateVideo(?:[A-Z][A-Za-z]+)?(?:[/?#]|$)/i;
          const entries = [];
          const errors = [];
          const maxEntries = 6;
          const originalFetch = typeof window.fetch === "function" ? window.fetch.bind(window) : null;
          const originalXhrOpen = window.XMLHttpRequest?.prototype?.open;
          const originalXhrSend = window.XMLHttpRequest?.prototype?.send;
          const safeClone = (value) => {
            try {
              return JSON.parse(JSON.stringify(value));
            } catch (_error) {
              return null;
            }
          };
          const pushEntry = (entry) => {
            entries.push({ ts: Date.now(), ...safeClone(entry) });
            while (entries.length > maxEntries) entries.shift();
          };
          const summarizePayload = (payload) => {
            const requests = Array.isArray(payload?.requests) ? payload.requests : [];
            return {
              project_id: String(payload?.clientContext?.projectId || ""),
              request_count: requests.length,
              requests: requests.slice(0, 4).map((request, index) => ({
                index,
                videoModelKey: String(request?.videoModelKey || ""),
                mediaId: String(request?.videoInput?.mediaId || ""),
                startFrameIndex: request?.videoInput?.startFrameIndex ?? null,
                endFrameIndex: request?.videoInput?.endFrameIndex ?? null,
                text_parts: Array.isArray(request?.textInput?.structuredPrompt?.parts)
                  ? request.textInput.structuredPrompt.parts.map((part) => String(part?.text || "")).slice(0, 6)
                  : [],
              })),
            };
          };
          const interceptBody = (transport, url, body) => {
            if (!targetPattern.test(String(url || ""))) return { matched: false, body };
            try {
              const payload = typeof body === "string" ? JSON.parse(body || "{}") : body;
              pushEntry({
                transport,
                url: String(url || ""),
                payload_summary: summarizePayload(payload),
              });
            } catch (error) {
              errors.push({ transport, detail: String(error?.message || error), ts: Date.now() });
            }
            return { matched: true, body };
          };
          if (originalFetch) {
            window.fetch = async function patchedFlow2ApiVideoWorkflowFetch(resource, init) {
              const requestUrl = typeof resource === "string" ? resource : String(resource?.url || "");
              if (init && Object.prototype.hasOwnProperty.call(init, "body")) {
                interceptBody("fetch", requestUrl, init.body);
              }
              return await originalFetch(resource, init);
            };
          }
          if (originalXhrOpen && originalXhrSend) {
            window.XMLHttpRequest.prototype.open = function patchedFlow2ApiVideoWorkflowOpen(method, url, ...rest) {
              this.__flow2api_video_workflow_submit_url = String(url || "");
              return originalXhrOpen.call(this, method, url, ...rest);
            };
            window.XMLHttpRequest.prototype.send = function patchedFlow2ApiVideoWorkflowSend(body) {
              interceptBody("xhr", String(this.__flow2api_video_workflow_submit_url || ""), body);
              return originalXhrSend.call(this, body);
            };
          }
          const hook = {
            snapshot() {
              return safeClone({ entries, errors });
            },
            dispose() {
              if (originalFetch) window.fetch = originalFetch;
              if (originalXhrOpen && originalXhrSend) {
                window.XMLHttpRequest.prototype.open = originalXhrOpen;
                window.XMLHttpRequest.prototype.send = originalXhrSend;
              }
              if (window[globalKey] === hook) delete window[globalKey];
            },
          };
          window[globalKey] = hook;
          return hook;
        };

        const typePrompt = async (promptText) => {
          const target = await waitFor(() => findPromptTarget(), 6000, 100);
          if (!(target instanceof Element)) {
            return { ok: false, reason: "prompt_target_not_found" };
          }
          const promptValue = String(promptText || "").trim();
          if (!promptValue) {
            return { ok: true, skipped: true, reason: "empty_prompt", target: summarizeElement(target) };
          }
          const beforeText = textOf(target);
          clickNode(target, { nativeOnly: true });
          await sleep(80);
          target.focus();
          if (target.getAttribute("data-slate-editor") === "true") {
            const currentText = Array.from(target.querySelectorAll("[data-slate-string='true']"))
              .map((node) => String(node.textContent || ""))
              .join("")
              .trim();
            const normalizedCurrent = currentText.replace(/\s+/g, " ").trim();
            const needsLeadingSpace = normalizedCurrent && !normalizedCurrent.endsWith(" ");
            const valueToInsert = `${needsLeadingSpace ? " " : ""}${promptValue}`;
            placeCaretAtEnd(target);
            const pasteDispatched = dispatchPasteText(target, valueToInsert);
            await sleep(80);
            let insertedText = textOf(target);
            let inserted = insertedText.includes(promptValue);
            if (!inserted) {
              target.dispatchEvent(createInputEvent("beforeinput", valueToInsert));
              try {
                inserted = document.execCommand("insertText", false, valueToInsert);
              } catch (_error) {
                inserted = false;
              }
              if (!inserted) {
                const selection = window.getSelection();
                const range = getEditableRangeWithinTarget(target);
                if (!range) {
                  throw new Error("prompt_edit_range_not_available");
                }
                const textNode = document.createTextNode(valueToInsert);
                range.deleteContents();
                range.insertNode(textNode);
                range.setStartAfter(textNode);
                range.collapse(true);
                selection?.removeAllRanges();
                selection?.addRange(range);
              }
              target.dispatchEvent(createInputEvent("input", valueToInsert));
              target.dispatchEvent(new Event("change", { bubbles: true, cancelable: true }));
              await sleep(120);
              insertedText = textOf(target);
            }
            return {
              ok: insertedText.includes(promptValue),
              target: summarizeElement(target),
              before_text: beforeText.slice(0, 200),
              after_text: insertedText.slice(0, 240),
              paste_dispatched: pasteDispatched,
              runtime: collectPromptRuntimeEvidence(target),
              submit_state: summarizeSubmitButtonState(),
            };
          }
          if ("value" in target) {
            target.value = promptValue;
            target.dispatchEvent(createInputEvent("beforeinput", promptValue));
            target.dispatchEvent(createInputEvent("input", promptValue));
            target.dispatchEvent(new Event("change", { bubbles: true, cancelable: true }));
          } else {
            try {
              placeCaretAtEnd(target);
              target.dispatchEvent(createInputEvent("beforeinput", promptValue));
              const inserted = document.execCommand("insertText", false, promptValue);
              if (!inserted) {
                target.textContent = promptValue;
              }
            } catch (_error) {
              target.textContent = promptValue;
            }
            target.dispatchEvent(createInputEvent("input", promptValue));
            target.dispatchEvent(new Event("change", { bubbles: true, cancelable: true }));
          }
          await sleep(200);
          return {
            ok: true,
            target: summarizeElement(target),
            before_text: beforeText.slice(0, 200),
            after_text: textOf(target).slice(0, 240),
            value_length: textOf(target).length,
            runtime: collectPromptRuntimeEvidence(target),
            submit_state: summarizeSubmitButtonState(),
          };
        };

        const findSubmitButton = () => {
          const buttons = collectVisibleButtons().filter((node) => !getActiveDialog() || !getActiveDialog().contains(node));
          const submitCandidates = buttons.filter((node) => {
            const text = textOf(node);
            return text.includes("创建") && !text.includes("添加到提示") && !text.includes("add_2");
          });
          submitCandidates.sort((left, right) => {
            const leftDisabled = !!left.disabled || left.getAttribute("aria-disabled") === "true";
            const rightDisabled = !!right.disabled || right.getAttribute("aria-disabled") === "true";
            if (leftDisabled !== rightDisabled) {
              return leftDisabled ? 1 : -1;
            }
            const leftRect = left.getBoundingClientRect();
            const rightRect = right.getBoundingClientRect();
            return rightRect.top - leftRect.top;
          });
          return submitCandidates[0] || null;
        };

        const waitForVideoComposerReady = async (timeoutMs = 9000) => {
          await sleep(260);
          return await waitFor(() => {
            const promptTarget = findPromptTarget();
            const settingsChip = collectVideoSettingsChipCandidates()[0]?.node || null;
            const submitButton = findSubmitButton();
            const modeRuntime = collectModeRuntimeEvidence();
            const hasModeCandidates = Array.isArray(modeRuntime?.mode_toggle_candidates) && modeRuntime.mode_toggle_candidates.length > 0;
            if (!(promptTarget || settingsChip || submitButton || hasModeCandidates)) return null;
            return {
              ok: true,
              prompt_target: summarizeElement(promptTarget),
              settings_chip: summarizeElement(settingsChip),
              submit_button: summarizeElement(submitButton),
              mode_runtime: modeRuntime,
            };
          }, timeoutMs, 140);
        };

        const workflowMode = String(jobPayload.workflow_mode || "").trim();
        if (!["edit_existing_video", "text_to_video"].includes(workflowMode)) {
          return finalize({
            result_version: "video_ui_workflow_v1",
            success: false,
            workflow_mode: workflowMode,
            error: `unsupported workflow_mode: ${workflowMode}`,
            steps,
          });
        }

        try {
          const normalizationResult = await normalizeComposerEntryState("video");
          recordStep("composer_normalized", normalizationResult);
          if (!normalizationResult.ok) {
            throw new Error(
              `composer normalization failed: ${
                normalizationResult.reason ||
                normalizationResult.mode_action?.reason ||
                "target_video_mode_not_ready"
              }`
            );
          }
          await humanPause(320, 760);

          if (workflowMode === "edit_existing_video") {
            const createLauncher = await waitFor(() => findCreateLauncher(), 6000, 100);
            if (!createLauncher) {
              throw new Error("create launcher not found");
            }
            clickNode(createLauncher);
            recordStep("open_create_dialog", { button: summarizeElement(createLauncher) });
            await humanPause(320, 760);

            const dialog = await waitFor(() => getMediaPickerDialog(), 6000, 100);
            if (!dialog) {
              throw new Error("create dialog not found");
            }
            recordStep("dialog_ready", { dialog: summarizeElement(dialog) });

            const referenceSelection = await ensureReferenceSelected(dialog);
            if (!referenceSelection.ok) {
              throw new Error(
                `reference selection failed: ${referenceSelection.reason || "unknown"}; detail=${JSON.stringify(referenceSelection)}`
              );
            }
            recordStep("reference_selected", referenceSelection);
            await humanPause(700, 1450);

            const rangeResult = await maybeAdjustTimeRange(dialog);
            if (!rangeResult.ok) {
              throw new Error(
                `time range failed: ${rangeResult.reason || "unknown"}; detail=${JSON.stringify(rangeResult)}`
              );
            }
            recordStep("time_range_ready", rangeResult);
            await humanPause(650, 1300);

            const addToPromptButton = await waitFor(() => findAddToPromptButton(dialog), 4000, 100);
            if (!addToPromptButton) {
              throw new Error("add-to-prompt button not found");
            }
            clickNode(addToPromptButton);
            recordStep("add_to_prompt", { button: summarizeElement(addToPromptButton) });

            await humanPause(800, 1650);
          } else {
            const composerReady = await waitForVideoComposerReady();
            recordStep("video_composer_ready", composerReady || { ok: false, reason: "video_composer_controls_not_ready" });
            if (!composerReady?.ok) {
              throw new Error("video composer controls not ready");
            }
            await humanPause(420, 900);
          }

          const desiredSettingsResult = await applyDesiredVideoSettings();
          recordStep("desired_video_settings", desiredSettingsResult);
          if (!desiredSettingsResult.ok) {
            throw new Error(
              `desired video settings failed: ${
                desiredSettingsResult.reason || "unknown"
              }; detail=${JSON.stringify(desiredSettingsResult)}`
            );
          }
          const settingsOverlayClosed = await closeVideoSettingsOverlays();
          recordStep("settings_overlay_closed", settingsOverlayClosed);
          if (!settingsOverlayClosed.ok) {
            throw new Error(`settings_overlay_close_failed:${settingsOverlayClosed.reason || "unknown"}`);
          }
          const promptReadyAfterSettings = await waitForPromptEditorReadyAfterSettings();
          recordStep("prompt_ready_after_settings", promptReadyAfterSettings || { ok: false, reason: "prompt_not_ready_after_settings" });
          if (!promptReadyAfterSettings?.ok) {
            throw new Error("prompt_not_ready_after_settings");
          }
          await humanPause(260, 520);

          const promptResult = await typePrompt(String(jobPayload.prompt || ""));
          if (!promptResult.ok) {
            throw new Error(`prompt typing failed: ${promptResult.reason || "unknown"}`);
          }
          recordStep("prompt_typed", promptResult);

          installVideoEditSubmitHook();
          const submitHookBefore = getSubmitHookSnapshot();
          const submitEntriesBefore = Array.isArray(submitHookBefore?.entries) ? submitHookBefore.entries.length : 0;
          await humanPause(1000, 2200);
          const submitButton = await waitFor(() => findSubmitButton(), 6000, 120);
          if (!submitButton) {
            throw new Error("submit button not found");
          }
          clickNode(submitButton, { nativeOnly: true });
          recordStep("submit_clicked", { button: summarizeElement(submitButton) });
          await sleep(800);
          const submitObserved = await waitFor(() => {
            const snapshot = getSubmitHookSnapshot();
            const entries = Array.isArray(snapshot?.entries) ? snapshot.entries : [];
            if (entries.length <= submitEntriesBefore) return null;
            return {
              entry_count_before: submitEntriesBefore,
              entry_count_after: entries.length,
              latest_entry: entries[entries.length - 1] || null,
              errors: Array.isArray(snapshot?.errors) ? snapshot.errors.slice(-4) : [],
            };
          }, 6000, 120);
          recordStep("submit_request_observed", submitObserved || {
            entry_count_before: submitEntriesBefore,
            entry_count_after: getSubmitHookSnapshot()?.entries?.length || submitEntriesBefore,
          });
          if (!submitObserved) {
            const submitFailure = await waitFor(() => getSubmitFailureState(), 4500, 150);
            if (submitFailure) {
              recordStep("submit_failed", submitFailure);
              throw new Error(`submit_failed:${submitFailure.kind}:${submitFailure.message}`);
            }
            throw new Error("submit_request_not_observed");
          }

          const submitFailure = getSubmitFailureState();
          if (submitFailure?.kind === "abnormal_activity") {
            recordStep("submit_failed", submitFailure);
            throw new Error(`submit_failed:${submitFailure.kind}:${submitFailure.message}`);
          }

          return finalize({
            result_version: "video_ui_workflow_v1",
            success: true,
            workflow_mode: workflowMode,
            submitted: true,
            steps,
          });
        } catch (error) {
          return finalize({
            result_version: "video_ui_workflow_v1",
            success: false,
            workflow_mode: workflowMode,
            error: String(error?.message || error),
            steps,
          });
        }
      },
      args: [payload],
      });
      result = executionResults?.[0]?.result || null;
    } catch (error) {
      return {
        result_version: "video_ui_workflow_v1",
        success: false,
        workflow_mode: workflowMode,
        error: `execute_script_failed:${String(error?.message || error)}`,
      };
    }
    if (result && Object.keys(result).length > 0) {
      return result;
    }
    try {
      const [fallbackRead] = await chrome.scripting.executeScript({
        target: { tabId },
        world: "MAIN",
        func: () => {
          const storageKey = "__FLOW2API_VIDEO_WORKFLOW_LAST_RESULT__";
          const parseStored = (value) => {
            if (!value) return null;
            try {
              return JSON.parse(value);
            } catch (_error) {
              return null;
            }
          };
          return (
            window[storageKey] ||
            parseStored(window.sessionStorage?.getItem(storageKey)) ||
            parseStored(window.localStorage?.getItem(storageKey)) ||
            {}
          );
        },
      });
      const fallbackResult = fallbackRead?.result || null;
      if (fallbackResult && Object.keys(fallbackResult).length > 0) {
        return fallbackResult;
      }
    } catch (error) {
      return {
        result_version: "video_ui_workflow_v1",
        success: false,
        workflow_mode: workflowMode,
        error: `fallback_read_failed:${String(error?.message || error)}`,
      };
    }
    return {
      result_version: "video_ui_workflow_v1",
      success: false,
      workflow_mode: workflowMode,
      error: "empty_workflow_result",
    };
  }

  async function handleJob({ tabId, jobPayload }) {
    return await executeWorkflowScript(tabId, jobPayload || {});
  }

  globalThis.Flow2ApiVideoWorkflow = {
    handleJob,
  };
})();
