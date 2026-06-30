# Browser-Backed Account 主模型设计

## 背景

当前系统的主对象仍然是 `Token`，核心接入方式是：

1. 用户提供 `ST`
2. 系统换取 `AT`
3. 后端围绕 token 池进行调度

但视频生成、编辑、真实 UI submit 已经证明：

- 仅有 `ST/AT` 并不足以稳定支撑全部能力
- 真实浏览器登录态、页面态、cookie、storage、service worker 才是更完整的账号运行时
- 多账号场景下，`1 token = 1 独立浏览器 profile` 比“一个浏览器里来回切号”更稳定

因此，系统需要从 `token-centered` 演进到 `browser-backed account-centered`。

## 当前结构

现有数据模型已经有三层雏形：

- `Token`
  - 凭证与业务控制面
  - 包含 `st / cookie / at / credits / current_project_id / extension_route_key`
- `BrowserProfile`
  - 持久浏览器身份资产
  - 包含 `profile_id / token_id / expected_email / storage_path / health_status`
- `WorkerSlot`
  - 在线浏览器执行槽
  - 包含 `route_key / client_label / profile_id / current_email / page_url / session_state`

这意味着系统并不是缺少浏览器账号模型，而是：

- 当前主对象仍对外表现为 `Token`
- `BrowserProfile / WorkerSlot` 还只是 token 的附属能力

## 目标模型

目标统一为：

- `1 account = 1 browser profile = 1 runtime identity`

账号对象由两部分组成：

### 1. Browser Runtime

这是账号的主状态：

- profile 目录
- 登录状态
- route_key
- 在线/离线
- 当前邮箱
- 当前项目页
- storage / cookie / service worker 等浏览器上下文

### 2. Derived Credentials

这是从浏览器运行时派生出来的附属状态：

- `ST`
- `cookie`
- `AT`
- `AT` 过期时间
- 最近同步时间

也就是说：

- `ST/cookie/AT` 不再是账号的主要接入入口
- 它们是浏览器账号运行时的衍生物

## 产品语义调整

### 现状

后台当前心智接近：

- 新增 Token
- 导入 ST
- 可选补建浏览器 profile

### 目标

后台应调整为：

- 新增账号
- 创建独立浏览器 Profile
- 启动浏览器
- 用户登录 Google
- 系统自动同步凭证与状态

### 兼容术语

第一阶段不强制改数据库表名，但产品语义上建议：

- 页面中的 “Token” -> “账号”
- 页面中的 `ST/AT/cookie` -> “凭证状态”
- 页面中的 `route/profile/browser online` -> “运行时状态”

## 设计原则

### 原则 1：账号以浏览器运行时为主

对于需要真实 UI、风控连续性、页面态一致性的能力：

- 真实浏览器上下文优先
- 凭证同步从属于浏览器

### 原则 2：凭证是派生状态，不是主接入入口

系统仍保留手动导入 `ST/cookie` 的能力，但只作为：

- 调试
- 紧急恢复
- 兼容旧账号

而不是默认主流程。

### 原则 3：调度面以账号运行时为准

调度时优先关心：

- 账号是否有 profile
- profile 是否在线
- 当前在线浏览器登录的邮箱是否匹配
- 当前项目页是否就绪

而不是只关心 token 是否有 `AT`。

## 数据模型映射

在不立刻重命名表的前提下，建议先采用下面的语义映射：

### `tokens` 表

短期保留，但语义调整为 “账号的凭证与业务配置快照”。

保留字段：

- `st`
- `cookie`
- `at`
- `at_expires`
- `credits`
- `user_paygate_tier`
- `current_project_id`
- `image_enabled`
- `video_enabled`
- `image_concurrency`
- `video_concurrency`
- `captcha_proxy_url`

不再把它视为账号本体，而是账号的 credential/config 子视图。

### `browser_profiles` 表

升级为账号主身份表。

建议逐步补齐或重解释：

- `profile_id`: 账号主键的稳定浏览器身份
- `token_id`: 短期兼容引用，长期可以替换为 `account_id`
- `expected_email`: 目标账号邮箱
- `storage_path`: profile 目录
- `profile_type`: 本地/远程/容器化 profile 类型
- `health_status`: provisioned / login_required / online / stale / broken
- `last_known_project_id`: 最近项目

### `worker_slots` 表

保持为在线执行态，不作为持久身份，只作为 runtime session 视图。

### 后续可选新增 `accounts` 表

长期建议新增一个明确的 `accounts` 表作为统一主实体：

- `account_id`
- `display_name`
- `expected_email`
- `status`
- `primary_profile_id`
- `last_credential_sync_at`

然后逐步让：

- `tokens` 成为 `account_credentials`
- `browser_profiles` 成为 `account_profiles`

但这不是第一阶段必须做的。

## 接入流程

### 目标主流程

1. 后台创建账号
2. 系统创建独立 profile
3. 系统启动浏览器
4. 用户在该浏览器中登录 Google
5. 系统自动识别当前邮箱
6. 系统自动同步：
   - `ST`
   - `cookie`
   - `AT`
   - `project_id`
   - 可用能力
7. 账号进入可调度状态

### 兼容流程

1. 手动导入 `ST`
2. 账号暂时可用于后端凭证能力
3. 若要启用 UI submit / 视频强依赖能力
4. 再补建 profile 并登录

这个流程保留，但不推荐作为长期主流程。

## 功能分层

### 仅凭证可支持

理论上只依赖 `ST/AT` 的能力：

- 某些纯 HTTP 接口
- 某些只要 token 不要真实浏览器的查询能力
- 后台辅助读取

### 必须 browser-backed

以下能力应明确要求账号具备浏览器运行时：

- 视频 UI submit
- 视频编辑 UI submit
- 图片或视频的真实前端提交路径
- 需要读取/复用当前页面态的能力
- 依赖风控连续性的关键能力

## 后台界面调整建议

### 一阶段

保留当前 `Token` 页面，但按“账号视角”重排：

- 主信息：邮箱、在线状态、profile 状态
- 次信息：ST/AT/cookie 状态
- 操作：
  - 准备 Profile
  - 启动浏览器
  - 刷新凭证
  - 详情

### 二阶段

页面文案统一替换：

- `Token管理` -> `账号管理`
- `添加Token` -> `添加账号`
- `刷新AT` -> `同步凭证`

### 三阶段

拆成两个面板：

- 账号运行时
- 凭证状态

## 调度调整建议

### 当前

主要还是：

- 选 token
- 再找 `extension_route_key`

### 目标

调度顺序改为：

1. 选账号
2. 看账号是否有健康 profile
3. 看是否有在线 slot
4. 看在线浏览器邮箱是否匹配
5. 看当前项目是否就绪
6. 再决定是否可执行 UI job

而凭证只作为：

- 后端请求辅助
- 同步信息
- 降级兜底

## 凭证同步策略

系统应支持从浏览器运行时自动反哺：

- `cookie -> ST`
- `ST -> AT`
- 当前邮箱校验
- 当前项目同步

建议增加一类后台动作：

- `同步账号凭证`

触发后：

1. 从 profile 当前上下文抓 cookie/session
2. 尝试提取或刷新 `ST`
3. 刷新 `AT`
4. 回写 `tokens` 表

## 迁移方案

### Phase 1：语义迁移，不动大表名

目标：

- 产品上把主对象理解为账号
- 技术上继续复用 `tokens + browser_profiles + worker_slots`

动作：

- 后台新增账号时默认创建 profile
- token 列表增强 profile/route/在线状态展示
- 文档与交互改成“浏览器运行时优先”
- 保留手动导入 ST 兼容能力

### Phase 2：自动凭证同步

目标：

- 登录 profile 后系统自动维护 `ST/cookie/AT`

动作：

- 增加 profile -> credential sync 能力
- 后台增加“同步凭证”动作
- 把 `刷新AT` 改为更广义的“同步账号凭证”

### Phase 3：调度语义切换

目标：

- 生成链路按账号运行时调度，而不是按 token 调度

动作：

- 让 UI submit / 视频编辑优先按 profile/slot 调度
- token 只作为 credential view

### Phase 4：数据模型正名

目标：

- 引入 `accounts` 主表，逐步弱化 `tokens` 作为主实体的角色

动作：

- 新增 `accounts`
- `tokens` 改为 `account_credentials`
- `browser_profiles` 改为 `account_profiles`

## 第一阶段建议实施范围

第一阶段只做最小但方向正确的改造：

1. 保持现有表结构
2. 后台文案逐步从 Token 迁到 Account
3. 新账号接入默认走 profile
4. 手动导入 ST 保留为兼容入口
5. 增强 profile 登录后同步凭证能力
6. 让视频/编辑能力明确依赖 browser-backed account

## 明确不在第一阶段做的事

以下内容先不做，避免改动过大：

- 立即重命名数据库大表
- 一次性移除全部 ST 导入路径
- 一次性重写所有调度逻辑
- 一次性把所有 UI 文案完全替换

## 结论

长期正确方向是：

- 账号主对象 = `browser-backed account`
- 浏览器运行时是主身份
- `ST/cookie/AT` 是派生凭证

短期最佳落地方式是：

- 先复用现有 `tokens + browser_profiles + worker_slots`
- 在产品心智、后台交互、调度语义上先完成主次转换
- 再逐步推进自动凭证同步和模型正名
