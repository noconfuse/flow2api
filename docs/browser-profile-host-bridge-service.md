# 宿主机浏览器启动桥常驻服务

## 作用

`browser_profile_host_bridge.py` 是运行在宿主机上的轻量 HTTP 服务，用来接收容器内 `flow2api-headed` 发起的浏览器启动请求，再由宿主机实际拉起 Chrome Profile。

在当前 `docker-compose.headed.yml` 配置下：

- `FLOW2API_BROWSER_LAUNCH_MODE=host_bridge`
- `FLOW2API_BROWSER_LAUNCH_HOST_URL=http://host.docker.internal:8765/launch`

因此，只要你希望保留“后端自动拉起浏览器 profile”能力，这个服务就应该常驻。

## 适用场景

- 后端运行在 Docker 容器中
- Chrome / Chromium 运行在宿主机上
- 需要在调度命中某个 token 后，由系统自动打开对应浏览器

如果你完全接受“浏览器只手工打开，不做自动拉起”，那么这个服务不是必须。

## 前置条件

- macOS
- 已安装 Python 3
- 仓库已拉到本机，例如 `/Users/you/workspace/gflow-proxy-server`
- `config/setting.toml` 已存在，并正确配置了 `global.api_key`
- Docker Compose 中保留以下配置：

```yaml
- FLOW2API_BROWSER_LAUNCH_MODE=host_bridge
- FLOW2API_BROWSER_LAUNCH_HOST_URL=http://host.docker.internal:8765/launch
```

## 一键安装为 LaunchAgent

在仓库根目录执行：

```bash
chmod +x scripts/install_browser_profile_host_bridge_launchagent.sh
./scripts/install_browser_profile_host_bridge_launchagent.sh
```

默认会：

- 注册服务名 `com.flow2api.browser-profile-host-bridge`
- 使用当前 `python3`
- 监听 `0.0.0.0:8765`
- 开机自启
- 异常退出自动拉起
- 将日志写到：
  - `tmp/host-bridge/stdout.log`
  - `tmp/host-bridge/stderr.log`

安装完成后，可检查：

```bash
curl http://127.0.0.1:8765/health
launchctl print gui/$(id -u)/com.flow2api.browser-profile-host-bridge
```

## 卸载

```bash
chmod +x scripts/uninstall_browser_profile_host_bridge_launchagent.sh
./scripts/uninstall_browser_profile_host_bridge_launchagent.sh
```

## 常用运维命令

### 查看服务状态

```bash
launchctl print gui/$(id -u)/com.flow2api.browser-profile-host-bridge
```

### 手动重启服务

```bash
launchctl kickstart -k gui/$(id -u)/com.flow2api.browser-profile-host-bridge
```

### 查看实时日志

```bash
tail -f tmp/host-bridge/stdout.log
tail -f tmp/host-bridge/stderr.log
```

### 健康检查

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

## 自定义参数

安装脚本支持以下环境变量：

```bash
FLOW2API_HOST_BRIDGE_LABEL=com.example.flow2api.host-bridge
FLOW2API_HOST_BRIDGE_PYTHON=/opt/homebrew/bin/python3
FLOW2API_BROWSER_LAUNCH_HOST_BIND=0.0.0.0
FLOW2API_BROWSER_LAUNCH_HOST_PORT=8765
```

示例：

```bash
FLOW2API_HOST_BRIDGE_PYTHON=/opt/homebrew/bin/python3 \
FLOW2API_BROWSER_LAUNCH_HOST_PORT=8876 \
./scripts/install_browser_profile_host_bridge_launchagent.sh
```

如果你改了端口，别忘了同步更新 `docker-compose.headed.yml` 里的：

```yaml
FLOW2API_BROWSER_LAUNCH_HOST_URL=http://host.docker.internal:<port>/launch
```

## 在另一台机器部署

建议按下面顺序做：

1. 克隆仓库并准备 `config/setting.toml`
2. 确认宿主机安装了 Chrome / Chromium
3. 执行安装脚本，把 host bridge 注册成 LaunchAgent
4. 用 `curl http://127.0.0.1:8765/health` 确认服务在线
5. 再启动 Docker：

```bash
docker compose -f docker-compose.headed.yml up -d --build
```

6. 进入管理台后测试“启动浏览器”

## 故障排查

### 1. 报错 `宿主机浏览器启动桥不可用`

先检查：

```bash
curl http://127.0.0.1:8765/health
```

如果连接失败，说明宿主机 bridge 没启动或已退出。

### 2. 宿主机健康检查正常，但容器仍然报网络错误

进入容器验证：

```bash
docker exec flow2api-headed python - <<'PY'
import urllib.request
print(urllib.request.urlopen("http://host.docker.internal:8765/health", timeout=5).read().decode())
PY
```

如果这里失败，说明是 Docker 到宿主机的连通性问题。

### 3. 报错 `403 forbidden`

说明 bridge 启用了鉴权，但容器侧传来的 `X-Flow2API-Key` 不匹配。

宿主机 bridge 的鉴权来源优先级为：

1. `config/setting.toml` 的 `global.api_key`
2. 环境变量 `FLOW2API_BROWSER_LAUNCH_HOST_TOKEN`

建议优先统一使用 `setting.toml` 中的 `api_key`。

### 4. 浏览器没有成功拉起

检查：

- `tmp/host-bridge/stderr.log`
- `tmp/host-bridge/stdout.log`
- Chrome 路径是否存在
- `scripts/browser_profile_launcher.py launch --token-ids <id>` 单独执行是否成功

## 推荐做法

在生产或长期运行机器上，建议：

- 始终把启动桥注册成 `launchd` 常驻服务
- 不要手工在终端里裸跑 `python3 scripts/browser_profile_host_bridge.py`
- 把宿主机和容器都统一用同一份 `setting.toml` 管理 `api_key`
