#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"

SERVICE_LABEL="${FLOW2API_HOST_BRIDGE_LABEL:-com.flow2api.browser-profile-host-bridge}"
PYTHON_BIN="${FLOW2API_HOST_BRIDGE_PYTHON:-$(command -v python3)}"
HOST_BIND="${FLOW2API_BROWSER_LAUNCH_HOST_BIND:-0.0.0.0}"
HOST_PORT="${FLOW2API_BROWSER_LAUNCH_HOST_PORT:-8765}"

if [[ -z "${PYTHON_BIN}" ]]; then
  echo "未找到 python3，请先安装 Python 3。" >&2
  exit 1
fi

if [[ ! -x "${PYTHON_BIN}" ]]; then
  echo "Python 不可执行: ${PYTHON_BIN}" >&2
  exit 1
fi

BRIDGE_SCRIPT="${REPO_ROOT}/scripts/browser_profile_host_bridge.py"
if [[ ! -f "${BRIDGE_SCRIPT}" ]]; then
  echo "未找到启动桥脚本: ${BRIDGE_SCRIPT}" >&2
  exit 1
fi

LAUNCH_AGENTS_DIR="${HOME}/Library/LaunchAgents"
PLIST_PATH="${LAUNCH_AGENTS_DIR}/${SERVICE_LABEL}.plist"
LOG_DIR="${REPO_ROOT}/tmp/host-bridge"
STDOUT_LOG="${LOG_DIR}/stdout.log"
STDERR_LOG="${LOG_DIR}/stderr.log"
mkdir -p "${LAUNCH_AGENTS_DIR}" "${LOG_DIR}"

cat > "${PLIST_PATH}" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key>
  <string>${SERVICE_LABEL}</string>

  <key>ProgramArguments</key>
  <array>
    <string>${PYTHON_BIN}</string>
    <string>${BRIDGE_SCRIPT}</string>
  </array>

  <key>WorkingDirectory</key>
  <string>${REPO_ROOT}</string>

  <key>EnvironmentVariables</key>
  <dict>
    <key>PYTHONUNBUFFERED</key>
    <string>1</string>
    <key>FLOW2API_BROWSER_LAUNCH_HOST_BIND</key>
    <string>${HOST_BIND}</string>
    <key>FLOW2API_BROWSER_LAUNCH_HOST_PORT</key>
    <string>${HOST_PORT}</string>
  </dict>

  <key>RunAtLoad</key>
  <true/>
  <key>KeepAlive</key>
  <true/>

  <key>StandardOutPath</key>
  <string>${STDOUT_LOG}</string>
  <key>StandardErrorPath</key>
  <string>${STDERR_LOG}</string>
</dict>
</plist>
EOF

USER_DOMAIN="gui/$(id -u)"

launchctl bootout "${USER_DOMAIN}" "${PLIST_PATH}" >/dev/null 2>&1 || true
launchctl bootstrap "${USER_DOMAIN}" "${PLIST_PATH}"
launchctl enable "${USER_DOMAIN}/${SERVICE_LABEL}" >/dev/null 2>&1 || true
launchctl kickstart -k "${USER_DOMAIN}/${SERVICE_LABEL}"

echo "已安装并启动 LaunchAgent: ${SERVICE_LABEL}"
echo "plist 路径: ${PLIST_PATH}"
echo "stdout 日志: ${STDOUT_LOG}"
echo "stderr 日志: ${STDERR_LOG}"
echo "健康检查: curl http://127.0.0.1:${HOST_PORT}/health"
echo "状态查看: launchctl print ${USER_DOMAIN}/${SERVICE_LABEL}"
