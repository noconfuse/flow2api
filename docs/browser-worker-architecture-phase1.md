# Browser Worker Architecture Phase 1

## Goal

Build the first migration step from `extension captcha token helper` to
`browser worker bound to a real account`, without reducing existing token pool
and scheduler capabilities.

Phase 1 does **not** move image/video generation into the browser yet.
It focuses on making the backend aware of which extension route is currently
bound to which real browser account and project context.

## Why This Phase Exists

The current extension path only gives Flow2API a reCAPTCHA token. The actual
upstream generation request is still sent by the backend. That preserves the
old risk surface:

- backend submits the request instead of the real browser
- route binding is only keyed by `extension_route_key`
- the backend cannot verify whether the connected browser is actually logged in
  as the token's account

For a token-pool system, the first requirement is reliable account-to-worker
binding. Without that, later browser-side execution will still be unsafe.

## What Phase 1 Adds

- Extension heartbeat/session summary reporting
- Backend route snapshot listing
- Route-key plus email consistency validation
- Admin visibility into active browser workers

## Phase 1 Data Contract

The extension now reports a browser worker summary over WebSocket:

- `route_key`
- `client_label`
- `current_email`
- `page_url`
- `page_title`
- `project_id`
- `tool_name`
- `session_state`
- `browser_user_agent`
- `capabilities`
- `worker_mode`
- `last_error`

## Expected Outcome

After Phase 1:

- scheduler still selects tokens the same way
- backend can reject a mismatched route before dispatch
- admin can inspect which browser worker is connected
- route binding is no longer "route key only"

## Next Phases

### Phase 2

Move image generation execution into the browser worker.

### Phase 3

Move video generation execution into the browser worker.

### Phase 4

Introduce explicit `profile` as a first-class asset in the database:

- `profile_id`
- `route_key`
- `expected_email`
- `proxy_binding`
- `worker_machine`
- `last_seen_at`
- `health_status`

At that point, scheduling can evolve from `pick token` to
`pick token + bound browser identity unit`.
