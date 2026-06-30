# 浏览器 Worker 集群最小架构

## 目标

为 `100+ Google Flow 账号` 的号池项目设计一套可落地的真实浏览器执行架构，
重点解决：

- 视频等高风控链路需要走真实浏览器 / 真实项目页 / 真实 UI 上下文
- 不能要求 `100 个账号 = 100 个永远在线浏览器`
- 后端仍然保留账号池、调度、限流、可观测能力

这份设计关注的是 **最小可运行版本**，不是一步到位的最终平台。

## 核心判断

当前项目已经具备两类能力：

- 后端侧：
  - token 池
  - 调度器
  - `token -> extension_route_key` 绑定
  - route 在线状态与账号邮箱校验
- 插件侧：
  - WebSocket 常连
  - 当前项目页/邮箱/页面状态上报
  - 浏览器内 `get_token`
  - 浏览器内 `submit_json`

这套能力足以支撑 **请求级浏览器执行**，图片链路已经验证成功。

但对于视频链路，仅做到：

- 真浏览器
- 真登录态
- 真项目页
- 同页 token 获取
- 同页 submit

仍然不等于“完整官方前端成功路径”。

因此，后续高风控视频链路应升级到：

- `浏览器 worker 集群`
- `profile 资产化`
- `真实 UI 提交`

而不是继续假设“一个插件统一控制 100 个账号”。

## 设计原则

### 1. `账号` 与 `执行槽位` 解耦

100 个账号不等于 100 个常驻浏览器窗口。

推荐拆分为两层资产：

- `账号资产`
  - token
  - email
  - cookie/session
  - project 绑定信息
- `浏览器身份资产`
  - profile
  - proxy
  - 本地存储
  - service worker
  - 当前浏览器状态

以及一层在线资源：

- `执行槽位`
  - 某台机器上当前在线的一个 Chrome/profile 实例

### 2. `route_key` 不能再代表全部身份语义

当前项目里，`route_key` 更像“一个在线浏览器连接的名字”。

在 100+ 账号场景下，至少要区分：

- `profile_id`: 持久身份资产
- `worker_node_id`: 某台执行机器
- `slot_id`: 某个在线浏览器槽位
- `route_key`: 当前 WebSocket/扩展连接实例

关系建议是：

- 一个 `profile_id` 长期绑定一个账号
- 一个 `slot_id` 在某个时间点承载一个 `profile_id`
- 一个 `route_key` 只代表这个槽位当前这次在线连接

### 3. 视频链路优先使用真实 UI，图片链路保留轻模式

不要把所有请求都切到 UI 驱动。

建议分层：

- 图片/轻链路：
  - 继续走当前已验证成功的 `extension get_token + submit_json`
- 视频/高风控链路：
  - 升级为 `UI automation inside the real project page`

这样资源压力才可控。

## 最小实体模型

### 1. `tokens`

现有表继续保留，仍然是业务调度的主入口。

建议继续保留：

- `id`
- `email`
- `st / at`
- `current_project_id`
- `image_enabled / video_enabled`
- `extension_route_key`

但中期应弱化 `extension_route_key` 的最终语义，改为兼容字段。

### 2. `browser_profiles`

新增“浏览器身份资产”表：

- `profile_id`
- `token_id`
- `expected_email`
- `proxy_binding`
- `storage_path`
- `profile_type`
  - `chrome_local`
  - `fingerprint_browser`
  - `remote_desktop`
- `last_known_project_id`
- `health_status`
- `last_seen_at`
- `notes`

语义：

- 一个账号长期对应一个 profile
- profile 是资产，槽位只是运行时载体

### 3. `worker_nodes`

表示一台实际执行机器：

- `worker_node_id`
- `machine_label`
- `host`
- `platform`
- `max_slots`
- `status`
- `last_heartbeat_at`

### 4. `worker_slots`

表示某台机器上的一个在线浏览器实例：

- `slot_id`
- `worker_node_id`
- `route_key`
- `client_label`
- `profile_id`
- `current_email`
- `page_url`
- `project_id`
- `session_state`
- `worker_mode`
- `busy`
- `job_type`
- `last_seen_at`

### 5. `worker_jobs`

表示真实浏览器执行任务：

- `job_id`
- `token_id`
- `profile_id`
- `slot_id`
- `job_type`
  - `image_request_submit`
  - `video_ui_submit`
  - `refresh_session`
  - `warm_project_page`
- `status`
  - `queued`
  - `running`
  - `succeeded`
  - `failed`
  - `aborted`
- `request_payload`
- `result_payload`
- `error_message`
- `created_at`
- `started_at`
- `finished_at`

## 在线资源模型

### 推荐容量模型

对 `100+ 账号` 的最小建议不是 100 个在线浏览器，而是：

- `100` 个 profile 资产
- `6-12` 个在线槽位
- `1-3` 台 worker 节点

适用场景：

- 视频任务并发不高
- 但希望账号池规模大

如果未来视频并发提高，再继续扩槽，而不是一开始就 100 浏览器常驻。

### 槽位调度策略

一个视频请求到来后：

1. 后端先从 token 池选账号
2. 找到该账号绑定的 `profile_id`
3. 看这个 profile 是否已经挂载在某个在线槽位
4. 如果在线且空闲，直接派发
5. 如果不在线，尝试：
   - 在空闲槽位挂载这个 profile
   - 或排队等待可用槽位
6. 执行完成后保留页面/项目态一段时间，提升同账号连续任务命中率

## 任务分层

### 层 1：请求级浏览器执行

复用当前能力：

- `get_token`
- `submit_json_via_browser`

适合：

- 图片生成
- 图片上传
- 低风险控制面请求

### 层 2：页面级真实 UI 执行

新增能力：

- 打开目标项目页
- 切换到视频模式
- 选择目标模型
- 填 prompt
- 点击官方提交按钮
- 监听官方前端真正发出的请求/响应

适合：

- 文生视频
- 图生视频
- 视频编辑

这是视频链路真正需要的下一阶段。

## 与现有代码的最小复用关系

### 可直接复用

#### `browser_captcha_extension.py`

当前已经具备：

- route 注册
- route 状态摘要
- `validate_connection_for_token()`
- `submit_json_via_browser()`

这可以直接演进成：

- `validate_slot_for_profile()`
- `dispatch_ui_job()`

#### `load_balancer.py`

当前已经具备：

- token 选择
- extension 在线性校验

下一步只需把“extension 在线检查”升级成“profile/slot 可用性检查”。

#### `admin.py`

当前已经具备：

- route 可视化
- token 与 route 的绑定查看

可以继续扩展为：

- profile 列表
- worker node 列表
- slot 状态页
- job 队列页

### 需要新增的最小能力

#### 插件指令类型

当前插件主要支持：

- `get_token`
- `submit_json`

最小新增：

- `ensure_project_page`
- `ui_submit_video`
- `capture_ui_state`

#### 后端调度抽象

新增一层 `browser_worker_scheduler`：

- 输入：
  - token_id
  - job_type
  - project_id
- 输出：
  - 分配到的 `slot_id / route_key / profile_id`

## 真实 UI 提交流程

以文生视频为例：

1. `generation_handler` 选中某个 token
2. 通过 token 找到绑定 profile
3. 通过 profile 找可用 slot
4. 后端发 `ui_submit_video` job 到该 slot
5. 插件/content script 在目标项目页执行：
   - 确认已登录正确邮箱
   - 确认在正确 project 页
   - 切视频模式
   - 选模型
   - 填 prompt
   - 点提交
6. 插件回传：
   - job status
   - 关键页面状态
   - 官方前端返回的 operation/task 信息
7. 后端进入轮询阶段

## 为什么这套架构适合号池

因为它天然支持：

- 账号池和浏览器池分离
- profile 资产沉淀
- 槽位数量按并发扩缩容
- 不同账号长期隔离
- 后端统一调度，而不是人工切浏览器

## 为什么不能只靠一个插件实例

因为真正需要隔离的是：

- cookie/session
- localStorage / IndexedDB
- service worker
- proxy
- 页面历史
- 项目上下文

这些都属于 `profile`，不是 `route_key` 本身。

一个插件连接只说明“有浏览器在线”，不说明“这就是可安全复用的账号身份单元”。

## 分阶段实施建议

### Phase A：资产模型落地

新增：

- `browser_profiles`
- `worker_nodes`
- `worker_slots`

并在后台可视化。

### Phase B：调度层升级

把当前：

- `token -> extension_route_key`

升级为：

- `token -> profile_id -> slot`

### Phase C：插件新增页面级指令

支持：

- `ensure_project_page`
- `ui_submit_video`
- `capture_ui_state`

### Phase D：仅视频切 UI

保持：

- 图片继续走请求级浏览器执行

新增：

- 视频切到真实 UI 提交

这样风险最小，也最符合现阶段已验证结果。

## 最小资源建议

对当前项目，先不要按 100 账号全量铺开。

建议第一版：

- `100` 个账号资产
- `100` 个 profile 记录
- `4-8` 个在线槽位
- 先只把 `token 7` 这一类高价值视频链路接到 UI 提交

等跑通后，再把更多账号迁入。

## 当前项目的建议下一步

最小顺序应是：

1. 先把 `profile` 提升为一等对象
2. 让 `load_balancer` 不再只看 `extension_route_key`
3. 给插件增加 `ui_submit_video`
4. 只在视频链路启用真实 UI 提交
5. 图片保留当前已成功的轻量模式

这样不会推翻已有成果，也不会让资源消耗立刻失控。
