# TODO: Token Backoffice Cookie / Session Management

## Background

当前后台主要把 `ST` 作为 token 管理的核心字段，但最近的 `refresh-at` 与 `personal` 浏览器链路已经证明：

- `ST -> AT` 仍然是直接刷新链路
- 但在 `personal` 模式下，浏览器 `cookie/session/resident context` 也是一等运行资产
- 仅管理 `ST` 会导致系统缺少“恢复新鲜会话”的正式入口
- 一旦库内 `ST` 与 `token.cookie` 一起老化，后台只能继续失败，人工需要去官网拿新值回填

因此，后台需要把 `cookie/session` 提升为正式管理对象，而不是继续只围绕 `ST` 运转。

## Goal

建立一套面向 Google Flow 账号池的后台能力，使单 token 管理页和批量运维链路都能：

- 导入和展示浏览器 cookie
- 从 cookie 中提取并同步 `__Secure-next-auth.session-token`
- 显示 resident/browser context 绑定状态
- 一键执行 `import-cookie -> refresh-at` 闭环
- 为后续文生视频、视频编辑、图片生成共用同一套会话资产底座

## Scope

本 TODO 聚焦后台与运维能力，不直接修改视频 submit 主链行为。

包含：

- 后台数据模型与接口设计
- 单 token 页面展示与操作入口
- 批量导入/批量校验/批量刷新能力
- 账号会话状态可观测性

不包含：

- 上游视频风控策略本身
- 浏览器指纹与 submit headers 调优
- resident 调度算法重构

## Why This Matters

从本轮排查可以得出几个稳定结论：

- `refresh-at` 失败时，问题常常不是“不会做 ST -> AT”
- 真正缺的是“如何把浏览器里真实且新鲜的 session 正式接回系统”
- `token.cookie`、`resident binding`、`ST` 之间目前缺少后台层的一等表达
- 运维上只能看见 `ST`，看不见 cookie/session 是否已经失效、是否和 resident context 对齐

## Proposed Backoffice Model

建议将 token 的会话资产拆成以下概念：

- `ST`
  - 角色：`st_to_at` 的直接输入
  - 来源：从最新 cookie/session 派生
- `cookie`
  - 角色：浏览器真实会话快照
  - 格式：支持 Cookie Header、JSON cookie 数组、Netscape `cookies.txt`
- `session_status`
  - 角色：标记当前 cookie/session 是否可用于 resident 绑定与 ST 提取
- `resident_binding_status`
  - 角色：标记 token 与常驻浏览器 context 是否已经成功对齐
- `last_session_refresh_at`
  - 角色：最近一次通过浏览器拿到新鲜 session 的时间
- `last_refresh_at_result`
  - 角色：最近一次 `refresh-at` 的结果与失败原因

## Proposed UI Changes

单 token 管理页建议新增：

- `Cookie` 文本导入框
- `Cookie 格式检测结果`
- `是否提取到 session-token`
- `提取出的 session-token 指纹`
- `resident 绑定状态`
- `最近一次 import-cookie 结果`
- `最近一次 refresh-at 结果`

操作按钮建议至少包含：

- `导入 Cookie`
- `验证 Cookie / Resident 绑定`
- `同步 Cookie -> ST`
- `执行 refresh-at`
- `导出当前会话摘要`

保留但降级：

- 手工编辑 `ST`
  - 保留作为应急入口
  - 不应再是后台唯一的会话维护方式

## Proposed API / Workflow

建议围绕现有能力形成正式闭环：

1. `import-cookie`
2. 解析 cookie 并提取 `__Secure-next-auth.session-token`
3. 回写 `token.cookie` 与 `tokens.st`
4. 可选执行 resident binding 校验
5. 再执行 `refresh-at`
6. 返回结构化结果给后台

建议补充的接口或返回字段：

- `GET /api/tokens/{id}/session-status`
- `POST /api/tokens/{id}/validate-cookie`
- `POST /api/tokens/{id}/sync-session-to-st`
- `POST /api/tokens/{id}/refresh-at-with-cookie`
- `POST /api/tokens/import-cookie-batch`

## Batch Operations

针对账号池运维，后续应支持：

- 批量导入 cookie
- 批量验证 cookie 是否可提取 session-token
- 批量检查 resident binding
- 批量刷新 AT
- 批量筛选“已失效 / 待更新 / 可用”的账号

## Observability

后台建议直接展示以下状态，避免继续依赖人工猜测：

- `cookie_present`
- `cookie_parse_ok`
- `session_token_present`
- `session_token_changed`
- `resident_binding_ok`
- `refresh_at_ok`
- `last_error_reason`

## Risks / Notes

- `cookie` 属于高敏感信息，后台展示时需要脱敏
- `session-token` 不应明文完整展示，只显示长度与摘要指纹
- 批量导入时要避免误把 A 账号 cookie 导入到 B token
- resident binding 的状态不能只靠缓存判断，需要有真实校验结果

## Implementation Priority

建议优先级如下：

1. 单 token 页面支持导入 cookie 并显示提取结果
2. 单 token 页面支持 `import-cookie -> refresh-at` 一键闭环
3. 后台展示 resident binding / session 状态
4. 批量导入与批量校验
5. 更完整的账号池会话运维视图

## Definition Of Done

满足以下条件可视为该 TODO 完成：

- 后台不再只依赖手工维护 `ST`
- 运维可以直接为单个账号导入新鲜 cookie
- 系统能明确显示 cookie 是否提取到新 session-token
- `refresh-at` 失败时，后台能看见失败是在 `ST`、`cookie/session` 还是 resident binding 层
- 批量账号会话状态可被筛选与运维

