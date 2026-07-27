# 宿主机浏览器启动桥常驻服务

## 作用

`scripts/browser_profile_host_bridge.py` 是运行在宿主机上的轻量 HTTP 服务。

它负责接收容器内后端发来的 `/launch` 请求，再由宿主机实际启动对应账号的浏览器 profile。

当前 headed 部署默认就是这条链路，见 [docker-compose.headed.yml](file:///Users/baolei/workspace/gflow-proxy-server/docker-compose.headed.yml#L21-L29)：

- `FLOW2API_BROWSER_LAUNCH_MODE=host_bridge`
- `FLOW2API_BROWSER_LAUNCH_HOST_URL=http://host.docker.internal:8765/launch`

只要你希望保留“后台点启动浏览器，宿主机自动拉起对应 profile”的能力，这个服务就应该常驻。

## 当前约束

- 宿主机浏览器只支持 `Chrome for Testing`
- 不再把普通 `Google Chrome` 或 `Chromium` 当作自动回退目标
- 可通过 `FLOW2API_CHROME_PATH` 显式指定 `Chrome for Testing` 的 `chrome.exe` / 可执行文件路径

当前浏览器路径探测和校验逻辑见 [browser_profile_launcher.py](file:///Users/baolei/workspace/gflow-proxy-server/scripts/browser_profile_launcher.py#L29-L58) 和 [browser_profile_launcher.py](file:///Users/baolei/workspace/gflow-proxy-server/scripts/browser_profile_launcher.py#L380-L410)。

## 适用场景

- 后端运行在 Docker 容器中
- 浏览器运行在宿主机上
- 需要按 token 自动打开独立浏览器 profile
- 需要为视频 UI 自动化、扩展协作、浏览器登录态复用提供宿主机浏览器环境

如果你完全接受“浏览器只手工打开，不做自动拉起”，那么 host bridge 不是必须。

## 前置条件

- 宿主机是 `macOS` 或 `Windows`
- 已安装 Python 3
- 已安装 `Chrome for Testing`
- 仓库已拉到本机
- `config/setting.toml` 已存在，并正确配置 `global.api_key`
- 容器侧保留 headed 配置

## macOS 安装方式

macOS 推荐注册成 `LaunchAgent` 常驻服务。

在仓库根目录执行：

```bash
chmod +x scripts/install_browser_profile_host_bridge_launchagent.sh
./scripts/install_browser_profile_host_bridge_launchagent.sh
```

默认行为：

- 服务名：`com.flow2api.browser-profile-host-bridge`
- Python：当前 `python3`
- 监听：`0.0.0.0:8765`
- 开机自启
- 异常退出自动拉起
- 日志写入 `tmp/host-bridge/stdout.log` 与 `tmp/host-bridge/stderr.log`

安装完成后可检查：

```bash
curl http://127.0.0.1:8765/health
launchctl print gui/$(id -u)/com.flow2api.browser-profile-host-bridge
```

卸载：

```bash
chmod +x scripts/uninstall_browser_profile_host_bridge_launchagent.sh
./scripts/uninstall_browser_profile_host_bridge_launchagent.sh
```

## Windows 启动方式

Windows 当前只保留一个入口：

- [start_host_bridge_windows.cmd](file:///Users/baolei/workspace/gflow-proxy-server/scripts/start_host_bridge_windows.cmd)

在仓库根目录执行：

```cmd
scripts\start_host_bridge_windows.cmd
```

这个脚本会：

- 自动切到仓库根目录
- 启动 `browser_profile_host_bridge.py`
- 把日志写到 `tmp\host-bridge\`
- 保留一个 `cmd` 窗口作为常驻进程

那个黑色 `cmd` 窗口就是 host bridge 进程本身，关闭后服务也会停止。

如果自动探测不到 `Chrome for Testing`，可先显式指定路径，再启动：

```cmd
set "FLOW2API_CHROME_PATH=C:\Users\%USERNAME%\AppData\Local\Google\Chrome for Testing\chrome.exe"
scripts\start_host_bridge_windows.cmd
```

如果你的安装形态是 `Application` 子目录，则改成：

```cmd
set "FLOW2API_CHROME_PATH=C:\Users\%USERNAME%\AppData\Local\Google\Chrome for Testing\Application\chrome.exe"
scripts\start_host_bridge_windows.cmd
```

## 健康检查

```bash
curl http://127.0.0.1:8765/health
```

成功响应示例：

```json
{
  "success": true,
  "service": "browser_profile_host_bridge",
  "bind": "0.0.0.0",
  "port": 8765,
  "auth_enabled": true
}
```

## 常用运维命令

macOS 查看服务状态：

```bash
launchctl print gui/$(id -u)/com.flow2api.browser-profile-host-bridge
```

macOS 手动重启：

```bash
launchctl kickstart -k gui/$(id -u)/com.flow2api.browser-profile-host-bridge
```

查看实时日志：

```bash
tail -f tmp/host-bridge/stdout.log
tail -f tmp/host-bridge/stderr.log
```

## 自定义参数

macOS 安装脚本支持：

```bash
FLOW2API_HOST_BRIDGE_LABEL=com.example.flow2api.host-bridge
FLOW2API_HOST_BRIDGE_PYTHON=/opt/homebrew/bin/python3
FLOW2API_BROWSER_LAUNCH_HOST_BIND=0.0.0.0
FLOW2API_BROWSER_LAUNCH_HOST_PORT=8765
```

Windows 启动脚本同样支持：

```cmd
set "FLOW2API_BROWSER_LAUNCH_HOST_BIND=0.0.0.0"
set "FLOW2API_BROWSER_LAUNCH_HOST_PORT=8765"
scripts\start_host_bridge_windows.cmd
```

如果你修改了端口，别忘了同步修改容器侧：

```yaml
FLOW2API_BROWSER_LAUNCH_HOST_URL=http://host.docker.internal:<port>/launch
```

## 故障排查

### 1. 提示 `宿主机浏览器启动桥不可用`

先检查：

```bash
curl http://127.0.0.1:8765/health
```

如果连接失败，说明 host bridge 没启动或已退出。

### 2. 宿主机健康检查正常，但容器仍访问失败

进入容器验证：

```bash
docker exec flow2api-headed python - <<'PY'
import urllib.request
print(urllib.request.urlopen("http://host.docker.internal:8765/health", timeout=5).read().decode())
PY
```

如果这里失败，说明是 Docker 到宿主机的连通性问题。

### 3. 报错 `403 forbidden`

说明 bridge 启用了鉴权，但容器传来的 `X-Flow2API-Key` 不匹配。

鉴权来源优先级：

1. `config/setting.toml` 的 `global.api_key`
2. 环境变量 `FLOW2API_BROWSER_LAUNCH_HOST_TOKEN`

建议优先统一使用 `setting.toml` 中的 `api_key`。

### 4. 报错“未找到可用的 Chrome for Testing”

优先检查：

- 本机是否真的安装了 `Chrome for Testing`
- 路径是否是 `chrome.exe` 本身，而不是上层目录
- 当前运行的 host bridge 是否已经重启到最新代码
- 是否需要临时显式设置 `FLOW2API_CHROME_PATH`

### 5. 浏览器没有成功拉起

检查：

- `tmp/host-bridge/stderr.log`
- `tmp/host-bridge/stdout.log`
- `FLOW2API_CHROME_PATH` 是否指向 `Chrome for Testing`
- 单独执行启动器是否成功：

```bash
python3 scripts/browser_profile_launcher.py launch --token-ids <id>
```

### 6. Windows 重新拉起后出现“Chromium 未正确关闭”提示

这是 Windows 下 relaunch 时可能出现的恢复提示，不一定代表功能异常。

当前代码已经在 relaunch 前尽量清理旧进程、清 session 文件，并写回正常退出标记；如果仍偶发出现，优先确认：

- 旧窗口是否真的已退出
- host bridge 是否已重启到最新代码
- 是否仍在使用 `Chrome for Testing`

## 推荐做法

- macOS 上把 host bridge 注册成 `launchd` 常驻服务
- Windows 上直接常驻运行 `start_host_bridge_windows.cmd`
- 宿主机与容器统一使用同一份 `setting.toml` 管理 `api_key`
- 明确使用 `Chrome for Testing`，不要依赖真实 Chrome 回退
- 每次更新了宿主桥相关脚本后，记得重启 host bridge 进程
