# Video UI State Machine

## Goal

Make browser-side text-to-video submission stable under recoverable UI states by:

- identifying the current Flow page state
- normalizing into the standard video-creation state
- submitting only when the page is in a valid state
- validating that a real video operation was created

## Recoverable States

### `STATE_PROJECT_IMAGE_MODE`

Signals:

- `FLOW_MAIN_PROMPT_BOX_STATE.imageOrVideoMode == "IMAGE"`
- page body contains image-family markers such as `Nano Banana`
- left-side `add_2 创建` menu exists
- right-side `arrow_forward 创建` is disabled

Recovery:

1. Open `add_2 创建`
2. Select `视频`
3. Re-read prompt state and visible buttons
4. Continue only after the page re-renders into a video-capable state

### `STATE_PROJECT_VIDEO_MENU_OPEN`

Signals:

- visible dialog/menu with options like `全部 / 图片 / 视频 / 语音 / 角色`
- `add_2 创建` has `aria-expanded=true`

Recovery:

1. Prefer selecting `视频` from the open dialog
2. If no video option is visible, close the dialog and retry normalization once

### `STATE_AGENT_INACTIVE`

Signals:

- visible `智能体` chip exists
- `aria-pressed != true`

Recovery:

- do nothing

### `STATE_AGENT_ACTIVE`

Signals:

- visible `智能体` / `article_spark` button with `aria-pressed=true`

Recovery:

1. Toggle it off
2. Click body once to collapse focus state
3. Re-snapshot the prompt and submit button

### `STATE_PROMPT_PLACEHOLDER`

Signals:

- prompt editor contains `data-slate-placeholder=true`
- no `data-slate-string=true`
- prompt text is empty or only zero-width nodes

Recovery:

1. Clear prompt editor
2. Re-focus prompt editor
3. Type prompt again using the strongest safe input path
4. Re-check Slate state before attempting submit

### `STATE_PROMPT_DIRTY_BUT_VALID`

Signals:

- prompt text exists
- placeholder removed
- Slate contains text or content has been committed into the editor tree

Recovery:

- keep current prompt and continue

## Submit Criteria

The extension should treat `click_only` as valid only when:

1. the clicked control is the real `arrow_forward 创建` submit button
2. the page is no longer stuck in the create-menu state
3. the prompt editor is not in placeholder-only state
4. a later backend poll observes a new video operation

## Failure Classes

### `FAIL_WRONG_ENTRYPOINT`

Meaning:

- the automation interacted with `add_2 创建` or another menu entry instead of the final submit button

Action:

- normalize state again and retry

### `FAIL_SUBMIT_ACCEPTED_NO_OPERATION`

Meaning:

- the UI path accepted a click, but no new project video operation appeared

Action:

1. inspect post-click UI state
2. if the page still looks like image mode or create-menu mode, classify as normalization failure
3. otherwise classify as upstream/business failure

### `FAIL_UNRECOVERABLE_PAGE_STATE`

Meaning:

- required controls are missing or page never stabilizes

Action:

- fail fast with a high-signal snapshot for manual diagnosis

## Retry Strategy

### Type Phase

- retry once when:
  - prompt remains in placeholder/zero-width state
  - page still shows recoverable markers such as image mode or wrong menu state

### Click Phase

- do not count UI click as success by itself
- success requires later discovery of a new project video operation
- if no operation appears, classify the last UI state before deciding whether to retry

## Current Implementation Direction

- Extension side:
  - normalize `创建 -> 视频`
  - avoid false agent toggling
  - prefer the real submit button over generic create controls

- Backend side:
  - keep `type_only` and `click_only` separated
  - retry `type_only` once for recoverable UI states
  - treat “no new operation” as a failed end-to-end submit, not as success
