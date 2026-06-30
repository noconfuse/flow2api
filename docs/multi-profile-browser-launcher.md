# 多账号浏览器 Profile 启动器

这套启动器的目标是把：

- `1 token`
- `1 route_key`
- `1 Chrome user-data-dir`
- `1 自动加载扩展的浏览器实例`

固定绑定起来，避免多账号共用一个浏览器上下文。

## 实现方式

启动器脚本：`scripts/browser_profile_launcher.py`

它会自动完成下面几件事：

1. 读取 `data/flow.db` 里的 token
2. 读取 `config/setting.toml` 里的 `api_key` 和服务端口
3. 为每个 token 生成独立扩展副本
4. 在扩展副本里写入 `bootstrap-settings.json`
5. 为每个 token 准备独立 `user-data-dir`
6. 用 `route_key/client_label` 自动启动 Chrome

生成文件都在：

- `browser_data/profile_launcher/extensions/`
- `browser_data/profile_launcher/user_data/`
- `browser_data/profile_launcher/metadata/`

这些目录已经被 `.gitignore` 忽略。

## 常用命令

列出 token 与 route_key：

```bash
python3 scripts/browser_profile_launcher.py list
```

只看指定 token：

```bash
python3 scripts/browser_profile_launcher.py list --token-ids 5,6,7
```

只准备 profile 目录和扩展副本，不启动浏览器：

```bash
python3 scripts/browser_profile_launcher.py prepare --token-ids 7
```

启动指定 token 的浏览器：

```bash
python3 scripts/browser_profile_launcher.py launch --token-ids 7
```

启动所有已启用 token：

```bash
python3 scripts/browser_profile_launcher.py launch --active-only
```

如果 Chrome 不在默认路径，可以手动指定：

```bash
python3 scripts/browser_profile_launcher.py launch --token-ids 7 --chrome-path "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
```

也可以追加额外 Chrome 参数：

```bash
python3 scripts/browser_profile_launcher.py launch --token-ids 7 --chrome-arg="--start-maximized"
```

## 新增一个 token 的最小流程

现在管理后台新增 Token 时，已经支持两个选项：

- `自动创建独立 Profile`
- `添加后立即启动浏览器`

默认行为是：

- 自动创建 profile：开启
- 立即启动浏览器：关闭

也就是说，最常见场景下你可以直接在后台添加 token，系统会顺手准备好这个 token 对应的扩展副本、`user-data-dir` 和 `route_key` 绑定。

如果你希望在添加完成后直接弹出独立浏览器窗口，再把“立即启动浏览器”勾上即可。

如果你仍然想用命令行手动控制，也可以继续使用下面这套 CLI 流程。

1. 后台把 token 导入系统
2. 执行：

```bash
python3 scripts/browser_profile_launcher.py prepare --token-ids <TOKEN_ID>
```

3. 再执行：

```bash
python3 scripts/browser_profile_launcher.py launch --token-ids <TOKEN_ID>
```

4. 在新打开的 Chrome 窗口里，首次登录这个 token 对应的 Google 账号
5. 登录完成后，扩展会自动带着：
   - `serverUrl`
   - `apiKey`
   - `routeKey`
   - `clientLabel`
   连回 Flow2API

以后这个 profile 可以长期复用，不需要再手动安装或手动填写扩展配置。

## route_key 规则

如果数据库里已经给 token 配了 `extension_route_key`，启动器会直接复用。

如果没有，启动器会自动写入默认值：

- `token7-worker`
- `token6-worker`
- `token5-worker`

对应浏览器标签为：

- `chrome-token7`
- `chrome-token6`
- `chrome-token5`

## 注意事项

- 每个 token 必须使用独立 `user-data-dir`
- 不要让多个 token 共用同一个 Chrome profile
- 同一个 token 的浏览器可以长期常驻
- 第一次真正需要人工操作的只有“登录 Google 账号”
- 扩展本身已经支持从 `bootstrap-settings.json` 自动读取配置，不需要再手工打开选项页填写
