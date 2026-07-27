# 浏览器 Worker 集群现状与演进

## 目标

这份文档不再把浏览器 worker 体系当成“纯规划稿”。

当前项目里，浏览器 profile 资产、worker 节点/槽位/任务表，以及对应后台只读接口都已经落地；真正还在继续演进的，是“如何从当前 host bridge + extension 路径，进一步收敛到更完整的浏览器 worker 集群”。

这份文档重点回答两件事：

1. 当前已经落地了什么
2. 后续还应该往哪演进

重点问题仍然是：

- 视频等高风控链路需要走真实浏览器 / 真实项目页 / 真实 UI 上下文
- 不能要求 `100 个账号 = 100 个永远在线浏览器`
- 后端仍然保留账号池、调度、限流、可观测能力

当前设计关注的是 **现状对齐后的最小可运行版本**，不是一步到位的最终平台。

## 核心判断

当前项目已经具备三类能力：

- 后端侧：
  - token 池
  - 调度器
  - `token -> extension_route_key` 绑定
  - route 在线状态与账号邮箱校验
  - `browser_profiles / worker_nodes / worker_slots / worker_jobs` 表
  - 后台 browser worker 只读接口
- 插件侧：
  - WebSocket 常连
  - 当前项目页/邮箱/页面状态上报
  - 浏览器内 `get_token`
  - 浏览器内 `submit_json`
  - `video_ui_workflow`
- 宿主机侧：
  - `host_bridge` 自动拉起浏览器
  - per-token 扩展副本与启动参数注入
  - Chrome for Testing 强约束

也就是说，这套系统已经不只是“单个浏览器扩展连上后台”。

它已经有了浏览器资产、在线槽位视图、任务记录，以及视频 UI 工作流。

## 当前已落地能力

### 1. 浏览器资产表已经存在

以下表已经在数据库初始化阶段创建，而不是停留在设计里：

- `browser_profiles`
- `worker_nodes`
- `worker_slots`
- `worker_jobs`

见 [database.py](file:///Users/baolei/workspace/gflow-proxy-server/src/core/database.py#L1417-L1493)。

### 2. 后台只读接口已经存在

当前后台已经可以读取：

- `GET /api/browser-worker/profiles`
- `GET /api/browser-worker/nodes`
- `GET /api/browser-worker/slots`
- `GET /api/browser-worker/jobs`

见 [admin.py](file:///Users/baolei/workspace/gflow-proxy-server/src/api/admin.py#L2861-L2897)。

### 3. profile 视图会从 token 自动同步

`browser_profiles` 不是完全手工维护。

当前已有：

- `sync_browser_profiles_from_tokens()`
- `sync_extension_routes_to_worker_slots()`

见 [database.py](file:///Users/baolei/workspace/gflow-proxy-server/src/core/database.py#L1753-L2132)。

这意味着当前的 profile/slot 视图，已经开始从现有 token 和扩展在线状态自动派生。

### 4. 视频主链路已经切到 `video_ui_workflow`

当前文生视频与视频编辑主链路已经直接派发 `video_ui_workflow`，而不是停留在旧的 `probe/type/click` 三段式。

见 [flow_client.py](file:///Users/baolei/workspace/gflow-proxy-server/src/services/flow_client.py#L3301-L3486) 与 [background.js](file:///Users/baolei/workspace/gflow-proxy-server/extension/background.js#L8602-L8670)。

### 5. 当前仍是 host bridge 驱动的浏览器准备模型

虽然 worker 相关结构已存在，但浏览器准备主路径仍然是：

- 容器命中 token
- 后端通过 `host_bridge` 请求宿主机拉起浏览器
- 启动器生成 per-token 扩展副本
- 扩展上线后再同步 route/slot 视图

这意味着当前实现更像：

- `host bridge + token browser + worker views`

而不是“完全独立、可跨多节点调度的成熟 worker 集群”。

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

## 实体模型

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

当前已落地的“浏览器身份资产”表字段包括：

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

当前已落地字段包括：

- `worker_node_id`
- `machine_label`
- `host`
- `platform`
- `max_slots`
- `status`
- `last_heartbeat_at`

### 4. `worker_slots`

表示某台机器上的一个在线浏览器实例。

当前已落地字段包括：

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

表示真实浏览器执行任务。

当前已落地字段包括：

- `job_id`
- `token_id`
- `profile_id`
- `slot_id`
- `job_type`
  - `image_request_submit`
  - `video_ui_workflow`
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

以上字段定义见 [database.py](file:///Users/baolei/workspace/gflow-proxy-server/src/core/database.py#L1417-L1493)。

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

- profile 列表接口
- worker node 列表接口
- slot 列表接口
- job 列表接口

现阶段更需要做的是：

- 把这些只读接口进一步接入后台页面
- 把“设计存在”升级成“运维可见”

### 需要新增的最小能力

#### 插件指令类型

当前插件主要支持：

- `get_token`
- `submit_json`
- `video_ui_workflow`

后续仍可继续补：

- `ensure_project_page`
- 更细粒度的 UI state capture
- 更独立的 worker job 指令语义

#### 后端调度抽象

后续建议新增一层更明确的 `browser_worker_scheduler`：

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

## 当前缺口

虽然浏览器 worker 相关模型已经落地，但当前还存在几个明显缺口：

- 浏览器准备仍偏向单机宿主桥，而不是独立 worker node 生命周期管理
- `worker_nodes / worker_slots / worker_jobs` 目前更偏“同步视图 + 记录表”，而不是完整调度内核
- 后台虽有接口，但还没有形成完整的 browser worker 运维页面
- 视频任务前仍可能依赖 `force_relaunch`，浏览器复用策略还不够成熟

## 分阶段实施建议

### Phase A：把已落地资产模型真正用起来

当前重点不是“再建表”，而是：

- 持续同步 `browser_profiles`
- 让 `worker_nodes / worker_slots` 成为稳定运维视图
- 在后台补齐 profile/slot/job 页面

### Phase B：调度层升级

把当前：

- `token -> extension_route_key`

升级为：

- `token -> profile_id -> slot`

### Phase C：插件与 worker job 进一步解耦

支持：

- 更明确的 worker job 指令
- 更独立的页面准备与状态捕获
- 更稳定的浏览器复用与恢复策略

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

1. 继续把 `profile` 从“同步出来的视图”提升为更稳定的一等对象
2. 让调度层不再只看 `extension_route_key`
3. 把 worker 只读接口接到更完整的后台运维页面
4. 继续把视频链路收敛到真实 UI 工作流
5. 图片链路保留当前已成功的轻量模式

这样不会推翻已有成果，也不会让资源消耗立刻失控。
