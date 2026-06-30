# 新机器部署指南

这份文档面向“在一台全新的电脑上，从零部署当前这套 `gflow-proxy-server`”的场景。

它不是迁移指南，不默认假设你会从旧机器复制数据库、浏览器状态或其他运行产物。

当前项目最贴近现状的部署方式是：

- 后端运行在 Docker 容器中
- 验证码/高风控链路使用 `extension` 或 `personal`
- 需要时由宿主机 Chrome 配合扩展完成 UI 自动化
- 使用 `docker-compose.headed.yml`

如果你准备在新机器上恢复完整自动化能力，建议直接沿用这套方式。

## 1. 新机器前置要求

建议准备以下环境：

- macOS
- Git
- Docker Desktop
- Python 3.11 或至少 Python 3.10+
- Google Chrome

当前仓库里“宿主机浏览器启动桥”原生提供了 macOS 的 `launchd` 方案，同时也已经补了 Windows 一键注册常驻脚本，见：

- [browser-profile-host-bridge-service.md](file:///Users/baolei/workspace/gflow-proxy-server/docs/browser-profile-host-bridge-service.md)
- [install_host_bridge_windows.ps1](file:///Users/baolei/workspace/gflow-proxy-server/scripts/install_host_bridge_windows.ps1)

## 2. 获取代码

在新电脑选择一个工作目录，例如：

```bash
mkdir -p ~/workspace
cd ~/workspace
git clone <你的仓库地址> gflow-proxy-server
cd gflow-proxy-server
```

## 3. 准备配置文件

从模板开始：

```bash
cp config/setting_example.toml config/setting.toml
```

至少确认这些字段：

- `global.api_key`
- `global.admin_username`
- `global.admin_password`
- `captcha.captcha_method`

模板参考：

- [setting_example.toml](file:///Users/baolei/workspace/gflow-proxy-server/config/setting_example.toml)

推荐重点先看这几项：

### 3.1 `global` 段

- `api_key`
  - 给外部 API 调用和扩展 WebSocket 鉴权使用
- `admin_username`
  - 后台登录用户名
- `admin_password`
  - 后台登录密码

### 3.2 `captcha` 段

- `captcha_method = "extension"`
  - 如果你要继续使用当前主线里的扩展协作 / UI 自动化，优先选这个
- `captcha_method = "personal"`
  - 如果你更偏向 personal 浏览器方案，再按需切换

### 3.3 首次从零部署时的认识

由于这次不是迁移，首次启动后：

- `data/` 会由系统自动初始化
- 后台里不会有旧 token
- 浏览器 profile / worker 也需要重新准备

这属于正常现象。

当前数据库默认落在 `data/` 下，见：

- [database.py](file:///Users/baolei/workspace/gflow-proxy-server/src/core/database.py#L36-L38)

## 4. 推荐的启动方式

### 4.1 为什么推荐 `docker-compose.headed.yml`

当前项目的“浏览器扩展协作 / UI 自动化 / 宿主机拉起 Chrome profile”这套方案，默认是围绕 `docker-compose.headed.yml` 组织的：

- 暴露端口 `8000`
- 挂载 `config/setting.toml`
- 挂载 `data/`、`browser_data/`、`tmp/`
- 允许容器使用 headed browser 相关能力
- 默认通过 `host.docker.internal:8765` 调宿主机启动桥

配置位置：

- [docker-compose.headed.yml](file:///Users/baolei/workspace/gflow-proxy-server/docker-compose.headed.yml)

### 4.2 启动命令

首次建议直接构建：

```bash
docker compose -f docker-compose.headed.yml up -d --build
```

后续更新代码后：

```bash
docker compose -f docker-compose.headed.yml up -d --build
```

查看日志：

```bash
docker compose -f docker-compose.headed.yml logs -f
```

健康检查：

```bash
curl http://127.0.0.1:8000/health
```

## 5. 宿主机浏览器启动桥

如果你希望保留“后台点启动浏览器后，系统自动在宿主机拉起对应 Chrome Profile”的能力，就需要安装 host bridge。

### 5.1 macOS 安装

在仓库根目录执行：

```bash
chmod +x scripts/install_browser_profile_host_bridge_launchagent.sh
./scripts/install_browser_profile_host_bridge_launchagent.sh
```

### 5.2 Windows 安装

Windows 不要运行 `install_browser_profile_host_bridge_launchagent.sh`，那个脚本依赖 `launchctl`，只适用于 macOS。

当前仓库已经提供一键注册 Windows 常驻任务的脚本：

- [install_host_bridge_windows.ps1](file:///Users/baolei/workspace/gflow-proxy-server/scripts/install_host_bridge_windows.ps1)

在 PowerShell 里进入仓库根目录后执行：

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\install_host_bridge_windows.ps1
```

如果自动探测不到 Chrome，显式指定浏览器路径：

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\install_host_bridge_windows.ps1 -ChromePath "C:\Program Files\Google\Chrome\Application\chrome.exe"
```

这个脚本会自动完成：

- 探测本机 Chrome/Chromium
- 生成 `scripts\start_host_bridge_windows.cmd`
- 注册一个“登录后自动启动”的 Windows 任务计划
- 立即启动 host bridge

如果后续要删除这个常驻任务，可执行：

```powershell
Unregister-ScheduledTask -TaskName "Flow2API Host Bridge" -Confirm:$false
```

### 5.3 验证

```bash
curl http://127.0.0.1:8765/health
```

如果正常，会返回类似：

```json
{
  "success": true,
  "service": "browser_profile_host_bridge",
  "bind": "0.0.0.0",
  "port": 8765,
  "auth_enabled": true
}
```

完整说明见：

- [browser-profile-host-bridge-service.md](file:///Users/baolei/workspace/gflow-proxy-server/docs/browser-profile-host-bridge-service.md)
- [install_host_bridge_windows.ps1](file:///Users/baolei/workspace/gflow-proxy-server/scripts/install_host_bridge_windows.ps1)

如果你不需要自动拉起浏览器，只接受手工打开 Chrome，那么这一步不是必需的。

## 6. 浏览器扩展安装

当前扩展目录在：

- `extension/`

扩展 manifest：

- [manifest.json](file:///Users/baolei/workspace/gflow-proxy-server/extension/manifest.json)

### 6.1 加载方式

建议在 Chrome 中：

1. 打开 `chrome://extensions`
2. 开启“开发者模式”
3. 选择“加载已解压的扩展程序”
4. 选择仓库里的 `extension/` 目录

### 6.2 扩展需要配置什么

扩展选项页需要填写：

- `Route Key`
- `Client Label`
- `WebSocket URL`
- `Flow2API API Key`

对应页面：

- [options.html](file:///Users/baolei/workspace/gflow-proxy-server/extension/options.html)
- [options.js](file:///Users/baolei/workspace/gflow-proxy-server/extension/options.js)

默认 WebSocket 地址是：

```text
ws://127.0.0.1:8000/captcha_ws
```

如果你的服务不是跑在本机，记得改成对应地址。

`Flow2API API Key` 取自：

- `config/setting.toml` 的 `global.api_key`

### 6.3 Route Key 怎么理解

`Route Key` 用来把“某个浏览器实例”固定绑定到后台里的某个账号。

建议做法：

- 每个常驻浏览器实例给一个唯一 `Route Key`
- 扩展里填这个 `Route Key`
- 后台对应 token 也填相同的 `Route Key`

例如：

- 浏览器 A: `Route Key = token6-worker`
- 后台 token 6: `extension_route_key = token6-worker`

## 7. 首次登录后台

服务启动后访问：

- `http://127.0.0.1:8000/manage`

使用 `config/setting.toml` 中的：

- `global.admin_username`
- `global.admin_password`

首次登录后建议马上确认：
- API Key 是否正确
- 打码方式是否符合你的部署方式
- 打码方式是否符合你的部署方式
- 调度策略是否符合你的预期

## 8. 从零部署后要补哪些初始化动作

由于这次不是迁移，服务启动后后台是“空白初始状态”，通常还需要补这些动作：

### 8.1 添加或导入 token

你可以在后台：

- 手工添加账号
- 或使用导入功能批量导入 token

如果不先补 token，`/test` 和实际生成链路都无法工作。

### 8.2 为需要走浏览器协作的账号准备运行环境

通常包括：

- 为账号准备 browser profile
- 启动浏览器
- 在浏览器里完成登录
- 给对应浏览器实例配置扩展 `Route Key`
- 在后台 token 上填同样的 `Route Key`

### 8.3 视你的场景决定是否必须用扩展

如果你当前主要依赖：

- 视频 UI 自动化
- 浏览器 worker
- 高风控页面协作

那扩展和浏览器链路就是必需的。

如果你只是先把服务跑起来，不急着恢复这些功能，可以先只完成后端部署。
## 9. 如果你使用当前这套自动化链路，建议这样验收
## 9. 如果你使用当前这套自动化链路，建议这样验收
### 9.1 服务侧


先确认：

```bash
curl http://127.0.0.1:8000/health
```

然后登录后台，确认：

- 后台能正常打开
- token 列表接口正常
- 至少能添加或导入一个账号

### 9.2 host bridge

确认：

```bash
curl http://127.0.0.1:8765/health
```

如果健康，但后台点击“启动浏览器”仍失败，再看：

```bash
tail -f tmp/host-bridge/stdout.log
tail -f tmp/host-bridge/stderr.log
```

### 9.3 扩展侧

确认：

- 扩展已加载
- 扩展配置已保存
- `serverUrl` 指向正确的 `captcha_ws`
- `apiKey` 与服务配置一致
- `Route Key` 已和后台 token 对齐

### 9.4 页面级验证

建议在新电脑上至少做一轮：

- 后台登录
- 打开 `/manage`
- 打开 `/test`
- 查看 worker 是否在线
- 用一个低成本模型做一次简单生成验证

如果你要跑视频测试，建议沿用当前更省额度的模型，例如 `omni-flash-r2v_4s` 或你当前实际在用的 4s 系列。

## 10. 常见问题

### 10.1 服务能启动，但后台没有 token

这是正常的。

因为这次是新部署，不是迁移，系统不会自动带出旧账号池。

你需要：

- 在后台手工添加 token
- 或通过导入功能批量导入

### 10.2 扩展显示正常，但后台看不到 worker 在线

优先检查：

- 扩展 `serverUrl` 是否正确
- `apiKey` 是否和 `setting.toml` 里的 `global.api_key` 一致
- `Route Key` 是否和后台 token 的 route key 对齐
- 浏览器是否真打开在 `https://labs.google/*`

### 10.3 后台点“启动浏览器”没反应

优先检查：

- host bridge 是否已安装并在线
- `docker-compose.headed.yml` 里的 `FLOW2API_BROWSER_LAUNCH_HOST_URL` 是否正确
- 宿主机是否能正常拉起 Chrome
- Windows 下如果提示“未找到可用的 Chrome/Chromium”，请重新执行 `install_host_bridge_windows.ps1 -ChromePath "<chrome.exe 路径>"`

### 10.4 只是想先把服务起起来，不接浏览器自动化

可以：

- 只准备代码和 `setting.toml`
- 直接启动服务
- 使用第三方打码或其他非浏览器链路

但如果你现在的主流程依赖扩展 UI 自动化，这样做只能起到“服务起来”，不能恢复完整自动化能力。

## 11. 相关文档

- [project-introduction.md](file:///Users/baolei/workspace/gflow-proxy-server/docs/project-introduction.md)
- [browser-profile-host-bridge-service.md](file:///Users/baolei/workspace/gflow-proxy-server/docs/browser-profile-host-bridge-service.md)
- [googleflow-account-pool.md](file:///Users/baolei/workspace/gflow-proxy-server/docs/googleflow-account-pool.md)

## 12. 最短路径

如果你只想看最短可执行版本，可以直接按下面做：

```text
cd ~/workspace
git clone <你的仓库地址> gflow-proxy-server
cd gflow-proxy-server

cp config/setting_example.toml ./config/setting.toml
# 然后手工编辑 config/setting.toml，至少填好 api_key、admin_username、admin_password
```

macOS:

```bash
./scripts/install_browser_profile_host_bridge_launchagent.sh
docker compose -f docker-compose.headed.yml up -d --build
curl http://127.0.0.1:8765/health
curl http://127.0.0.1:8000/health
```

Windows:

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\install_host_bridge_windows.ps1
docker compose -f docker-compose.headed.yml up -d --build
curl http://127.0.0.1:8765/health
curl http://127.0.0.1:8000/health
```

然后：

1. 在 Chrome 加载 `extension/`
2. 配置扩展的 `serverUrl/apiKey/routeKey`
3. 登录 `http://127.0.0.1:8000/manage`
4. 在后台添加或导入 token
5. 验证 worker、浏览器自动化链路
