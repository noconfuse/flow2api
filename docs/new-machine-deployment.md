# 新电脑部署指南

这份文档面向“把当前这套 `gflow-proxy-server` 部署到一台新电脑，并尽量复用旧机器上的现有配置和运行方式”的场景。

当前项目最常见、也最贴近现状的部署方式是：

- 后端运行在 Docker 容器中
- 验证码/高风控链路使用 `extension` 或 `personal`
- 需要时由宿主机 Chrome 配合扩展完成 UI 自动化
- 使用 `docker-compose.headed.yml`

如果你当前机器就是这样跑的，建议新电脑也沿用这一套。

## 1. 先决定迁移方式

### 1.1 只迁代码，不迁运行状态

适合：

- 你愿意在新电脑重新添加账号
- 不需要保留旧机器上的 token 库、后台配置和浏览器状态

需要准备：

- 仓库代码
- 一份新的 `config/setting.toml`

### 1.2 连同当前状态一起迁移

适合：

- 你希望保留旧机器上的账号池、后台配置、数据库状态
- 你已经在旧机器上维护好了 token、项目、调度参数

建议至少迁移：

- `config/setting.toml`
- `data/`

按需迁移：

- `browser_data/`
- `tmp/`

通常不建议迁移：

- `.dbg/`
- 各类临时日志
- 本地调试产物

原因：

- `config/setting.toml` 保存 API Key、后台账号和系统配置
- `data/` 下是 SQLite 数据库和后台持久化状态
- `browser_data/` 是否要迁，取决于你是否依赖本机保留的浏览器 profile 相关产物
- `tmp/` 更偏缓存，通常可不迁
- `.dbg/` 是调试痕迹，不属于正式运行依赖

## 2. 新电脑前置要求

建议准备以下环境：

- macOS
- Git
- Docker Desktop
- Python 3.11 或至少 Python 3.10+
- Google Chrome

当前仓库里“宿主机浏览器启动桥”是按 macOS 的 `launchd` 方案提供的，见：

- [browser-profile-host-bridge-service.md](file:///Users/baolei/workspace/gflow-proxy-server/docs/browser-profile-host-bridge-service.md)

如果你新电脑不是 macOS，这份指南里的 host bridge 安装步骤需要自行换成 systemd 或手工常驻方式。

## 3. 获取代码

在新电脑选择一个工作目录，例如：

```bash
mkdir -p ~/workspace
cd ~/workspace
git clone <你的仓库地址> gflow-proxy-server
cd gflow-proxy-server
```

如果你不是通过 Git 拉代码，而是直接从旧电脑拷贝仓库目录，也可以，但建议仍然保留 Git 仓库，方便后续更新。

## 4. 迁移必要文件

### 4.1 配置文件

如果旧机器已经有可用配置，直接复制：

```bash
mkdir -p config
cp /旧机器导出的路径/setting.toml config/setting.toml
```

如果没有现成配置，可以从模板开始：

```bash
cp config/setting_example.toml config/setting.toml
```

然后至少确认这些字段：

- `global.api_key`
- `global.admin_username`
- `global.admin_password`
- `captcha.captcha_method`

模板参考：

- [setting_example.toml](file:///Users/baolei/workspace/gflow-proxy-server/config/setting_example.toml)

### 4.2 数据目录

如果你希望保留旧机器上的账号池和后台状态，复制：

```bash
mkdir -p data
cp -R /旧机器导出的路径/data/. ./data/
```

当前数据库默认落在 `data/` 下，见：

- [database.py](file:///Users/baolei/workspace/gflow-proxy-server/src/core/database.py#L36-L38)

### 4.3 浏览器相关目录

如果你旧机器上长期使用浏览器 profile / host bridge，并且确认本地状态对你有价值，可以按需复制：

```bash
mkdir -p browser_data
cp -R /旧机器导出的路径/browser_data/. ./browser_data/
```

不确定是否需要时，可以先不复制，后续重新准备 profile。

## 5. 推荐的启动方式

### 5.1 为什么推荐 `docker-compose.headed.yml`

当前项目的“浏览器扩展协作 / UI 自动化 / 宿主机拉起 Chrome profile”这套方案，默认是围绕 `docker-compose.headed.yml` 组织的：

- 暴露端口 `8000`
- 挂载 `config/setting.toml`
- 挂载 `data/`、`browser_data/`、`tmp/`
- 允许容器使用 headed browser 相关能力
- 默认通过 `host.docker.internal:8765` 调宿主机启动桥

配置位置：

- [docker-compose.headed.yml](file:///Users/baolei/workspace/gflow-proxy-server/docker-compose.headed.yml)

### 5.2 启动命令

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

## 6. 宿主机浏览器启动桥

如果你希望保留“后台点启动浏览器后，系统自动在宿主机拉起对应 Chrome Profile”的能力，就需要安装 host bridge。

### 6.1 安装

在仓库根目录执行：

```bash
chmod +x scripts/install_browser_profile_host_bridge_launchagent.sh
./scripts/install_browser_profile_host_bridge_launchagent.sh
```

### 6.2 验证

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

如果你不需要自动拉起浏览器，只接受手工打开 Chrome，那么这一步不是必需的。

## 7. 浏览器扩展安装

当前扩展目录在：

- `extension/`

扩展 manifest：

- [manifest.json](file:///Users/baolei/workspace/gflow-proxy-server/extension/manifest.json)

### 7.1 加载方式

建议在 Chrome 中：

1. 打开 `chrome://extensions`
2. 开启“开发者模式”
3. 选择“加载已解压的扩展程序”
4. 选择仓库里的 `extension/` 目录

### 7.2 扩展需要配置什么

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

### 7.3 Route Key 怎么理解

`Route Key` 用来把“某个浏览器实例”固定绑定到后台里的某个账号。

建议做法：

- 每个常驻浏览器实例给一个唯一 `Route Key`
- 扩展里填这个 `Route Key`
- 后台对应 token 也填相同的 `Route Key`

例如：

- 浏览器 A: `Route Key = token6-worker`
- 后台 token 6: `extension_route_key = token6-worker`

## 8. 首次登录后台

服务启动后访问：

- `http://127.0.0.1:8000/manage`

如果你沿用了旧机器的 `config/setting.toml`，就使用其中的：

- `global.admin_username`
- `global.admin_password`

如果是首次新建配置，建议登录后马上确认：

- API Key 是否正确
- 打码方式是否符合你的部署方式
- 调度策略是否符合旧机器设置

## 9. 如果你使用当前这套自动化链路，建议这样验收

### 9.1 服务侧

先确认：

```bash
curl http://127.0.0.1:8000/health
```

然后登录后台，确认：

- 后台能正常打开
- token 列表能显示
- 已迁移的数据确实存在

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

## 10. 从旧电脑迁移时的推荐顺序

建议按下面顺序做，最稳：

1. 新电脑安装 Docker、Python、Chrome
2. 克隆仓库
3. 复制 `config/setting.toml`
4. 复制 `data/`
5. 按需复制 `browser_data/`
6. 安装 host bridge
7. 启动 `docker-compose.headed.yml`
8. 加载扩展并填写配置
9. 登录后台做一次验证

这样做的好处是：

- 先把配置和状态迁过来
- 再恢复浏览器自动化链路
- 出问题时更容易定位是“服务侧”还是“浏览器侧”

## 11. 哪些东西通常不用迁

一般不需要从旧机器复制这些内容：

- `.dbg/`
- 本地调试脚本
- 临时测试文件
- 宿主机日志输出
- 容器构建缓存

这些要么不是运行依赖，要么在新电脑上重新生成更干净。

## 12. 常见问题

### 12.1 服务能启动，但后台没有旧 token

优先检查：

- `data/` 是否复制成功
- 是否真的挂载到了容器里
- 容器是否读到了正确的 `setting.toml`

### 12.2 扩展显示正常，但后台看不到 worker 在线

优先检查：

- 扩展 `serverUrl` 是否正确
- `apiKey` 是否和 `setting.toml` 里的 `global.api_key` 一致
- `Route Key` 是否和后台 token 的 route key 对齐
- 浏览器是否真打开在 `https://labs.google/*`

### 12.3 后台点“启动浏览器”没反应

优先检查：

- host bridge 是否已安装并在线
- `docker-compose.headed.yml` 里的 `FLOW2API_BROWSER_LAUNCH_HOST_URL` 是否正确
- 宿主机是否能正常拉起 Chrome

### 12.4 只是想在新电脑快速起服务，不想接浏览器自动化

可以：

- 只复制代码和 `setting.toml`
- 直接启动服务
- 使用第三方打码或其他非浏览器链路

但如果你现在的主流程依赖扩展 UI 自动化，这样做只能起到“服务起来”，不能恢复完整自动化能力。

## 13. 相关文档

- [project-introduction.md](file:///Users/baolei/workspace/gflow-proxy-server/docs/project-introduction.md)
- [browser-profile-host-bridge-service.md](file:///Users/baolei/workspace/gflow-proxy-server/docs/browser-profile-host-bridge-service.md)
- [googleflow-account-pool.md](file:///Users/baolei/workspace/gflow-proxy-server/docs/googleflow-account-pool.md)

## 14. 最短路径

如果你只想看最短可执行版本，可以直接按下面做：

```bash
cd ~/workspace
git clone <你的仓库地址> gflow-proxy-server
cd gflow-proxy-server

cp /旧机器导出的 setting.toml ./config/setting.toml
cp -R /旧机器导出的 data ./data

./scripts/install_browser_profile_host_bridge_launchagent.sh
docker compose -f docker-compose.headed.yml up -d --build

curl http://127.0.0.1:8765/health
curl http://127.0.0.1:8000/health
```

然后：

1. 在 Chrome 加载 `extension/`
2. 配置扩展的 `serverUrl/apiKey/routeKey`
3. 登录 `http://127.0.0.1:8000/manage`
4. 验证 token、worker、浏览器自动化链路
