#!/usr/bin/env bash
set -euo pipefail

SERVICE_LABEL="${FLOW2API_HOST_BRIDGE_LABEL:-com.flow2api.browser-profile-host-bridge}"
LAUNCH_AGENTS_DIR="${HOME}/Library/LaunchAgents"
PLIST_PATH="${LAUNCH_AGENTS_DIR}/${SERVICE_LABEL}.plist"
USER_DOMAIN="gui/$(id -u)"

launchctl bootout "${USER_DOMAIN}" "${PLIST_PATH}" >/dev/null 2>&1 || true
rm -f "${PLIST_PATH}"

echo "已卸载 LaunchAgent: ${SERVICE_LABEL}"
echo "已删除 plist: ${PLIST_PATH}"
