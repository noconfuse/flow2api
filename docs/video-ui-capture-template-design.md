# 示教采集与 Workflow 模板设计

## 1. 目标

这份文档回答四个非常具体的问题：

1. `capture_mode` 到底采什么
2. 采集结果如何落成可复用的 operator
3. 动态参数如何和人工示教解耦
4. 文生视频、媒体工作台派生图生视频、编辑视频的模板应该长什么样

这里的设计目标不是做一个“录屏回放器”，而是建立一套：

- 可观察
- 可复用
- 可参数化
- 可维护

的 UI workflow 模板体系。

## 2. 核心原则

### 2.1 示教只负责发现路径，不负责最终执行

人工示教的价值是帮助系统识别：

- 正确入口
- 稳定锚点
- 合理顺序
- 关键状态切换

但正式执行时，不应直接回放“当时点了哪里、输过什么固定值”。

### 2.2 模板执行必须基于语义步骤

正式模板里的步骤应该是：

- `open_project`
- `select_text_video_mode`
- `select_reference_media`
- `set_time_range`
- `fill_prompt`
- `submit`

而不是：

- 点击第 4 个按钮
- 延迟 800ms
- 点击右下角 1 次

### 2.3 动态参数必须独立于示教内容

示教时输入的 prompt、选择的素材、设置的时间，只是为了帮助系统找到目标控件和交互路径。

这些值本身不能固化进正式模板。

## 3. 总体架构

```mermaid
flowchart LR
    A[人工示教流程] --> B[capture_mode 事件采集]
    B --> C[原始 Trace]
    C --> D[Trace 分析器]
    D --> E[Operator 候选]
    E --> F[Workflow 模板]
    F --> G[运行时参数注入]
    G --> H[正式 UI 执行]

    style B fill:#bbdefb,color:#0d47a1
    style D fill:#fff3e0,color:#e65100
    style F fill:#c8e6c9,color:#1a5e20
    style G fill:#f3e5f5,color:#7b1fa2
```

## 4. `capture_mode` 设计

`capture_mode` 建议做成扩展侧的一种运行模式，而不是单独脚本。

当模式开启后，扩展不主动替你执行任务，只做自动采集。

## 5. 采集范围

### 5.1 页面级上下文

每次流程开始和页面显著变化时，记录：

- `url`
- `title`
- `document.readyState`
- `project_id`
- 当前时间戳
- 当前标签页 id
- 当前账号 route key

### 5.2 点击事件

用户每次点击时，记录：

- 事件类型
- 点击时坐标
- 目标元素 tag
- 目标元素文本
- `aria-label`
- `role`
- `name`
- `placeholder`
- `data-*` 属性摘要
- CSS selector 候选
- XPath 候选
- 父节点与兄弟节点文本摘要
- 点击前截图
- 点击后截图

### 5.3 输入事件

用户每次输入时，记录：

- 输入目标元素类型
- 是否为 Slate 编辑器
- 是否为 `textarea`
- 是否为 `contenteditable`
- 输入前值摘要
- 输入后值摘要
- 输入动作类型
- 是否触发 `beforeinput` / `input` / `change`

注意：

- 不需要明文保存完整业务 prompt
- 可以记录为脱敏摘要、长度、分词片段或 hash

### 5.4 DOM 变化

页面在点击和输入后，记录一份变化摘要：

- Mutation 类型计数
- 新出现的 dialog/menu/button/chip
- 提交按钮候选集变化
- prompt 区域状态变化
- 模式 chip 变化

### 5.5 提交相关证据

如果用户执行了真正的提交动作，额外记录：

- 提交按钮候选集合
- 最终被点击元素摘要
- 点击前后按钮可用性
- 点击后页面变化
- 相关网络请求摘要
- 若可行，记录请求 path、method、status

## 6. 采集事件模型

建议统一事件结构：

```json
{
  "trace_id": "trace-uuid",
  "run_id": "demo-run-uuid",
  "event_seq": 17,
  "event_type": "ui.click",
  "ts": 1760000000000,
  "page": {
    "url": "https://labs.google/fx/zh/tools/flow/project/abc",
    "title": "Google Flow",
    "ready_state": "complete"
  },
  "target": {
    "tag": "button",
    "text": "创建",
    "role": "button",
    "aria_label": "创建",
    "dataset": {},
    "selector_candidates": [],
    "xpath_candidates": []
  },
  "context": {
    "surrounding_text": ["视频", "素材"],
    "screenshot_before": "artifact://trace/17-before.png",
    "screenshot_after": "artifact://trace/17-after.png"
  }
}
```

## 7. 事件类型

建议第一版只支持以下类型：

- `page.enter`
- `page.state`
- `ui.click`
- `ui.input`
- `ui.change`
- `ui.dialog_open`
- `ui.dialog_close`
- `ui.surface_shift`
- `network.summary`
- `submit.detected`

先把事件类型压少，有助于后续分析稳定。

## 8. Trace 存储结构

建议每次示教采集落成一个 trace 包：

```text
trace/
  metadata.json
  events.jsonl
  dom/
    0001.json
    0002.json
  screenshots/
    0001-before.png
    0001-after.png
  network/
    0001.json
```

其中：

- `metadata.json`
  - trace 元数据
- `events.jsonl`
  - 按顺序排列的采集事件
- `dom/`
  - 关键时刻的 DOM 摘要
- `screenshots/`
  - 前后对比截图
- `network/`
  - 提交阶段请求摘要

## 9. 从 Trace 到 Operator

示教结果不能直接变成 workflow，需要先经过一层 operator 抽象。

operator 的职责是：

- 用语义目标描述动作
- 封装目标元素查找逻辑
- 接收运行时参数
- 输出统一的步骤结果

## 10. Operator 接口设计

建议 operator 统一接口：

```json
{
  "name": "select_reference_media",
  "params": {
    "reference_media_id": "runtime"
  },
  "locator_strategy": {
    "primary": "semantic",
    "fallback": ["text_anchor", "aria_anchor", "neighbor_anchor"]
  },
  "success_criteria": [
    "media_selected",
    "selected_media_matches_runtime"
  ],
  "failure_codes": [
    "REFERENCE_MEDIA_NOT_FOUND",
    "REFERENCE_MEDIA_SELECT_FAILED"
  ]
}
```

## 11. 推荐的 Operator 列表

建议第一版只实现这些 operator：

- `open_project`
- `ensure_video_surface`
- `select_text_video_mode`
- `select_edit_video_mode`
- `select_reference_media`
- `set_time_range`
- `fill_prompt`
- `submit`
- `collect_post_submit`

这些 operator 足够覆盖你当前要的完整流程。

## 12. Operator 输出结构

每个 operator 运行后返回：

```json
{
  "ok": true,
  "operator": "fill_prompt",
  "stage": "fill_prompt",
  "used_locator": {
    "strategy": "semantic",
    "evidence": ["contenteditable", "slate", "neighbor_text:提示词"]
  },
  "artifacts": {
    "screenshot_before": "artifact://...",
    "screenshot_after": "artifact://..."
  },
  "normalized_output": {
    "prompt_ready": true
  },
  "error_code": "",
  "error_message": ""
}
```

## 13. Locator 策略

为了避免纯回放脆弱性，建议 locator 使用多级策略：

### 13.1 一级：语义定位

优先依据：

- 按钮文本
- `aria-label`
- `role`
- 编辑器特征
- 模式 chip 文案

### 13.2 二级：邻域锚点

如果主目标不稳定，则参考：

- 附近标题
- 父容器文案
- 左右兄弟节点文本
- 所在 panel 区域特征

### 13.3 三级：结构兜底

最后才使用：

- selector 候选
- XPath 候选
- layout 位置约束

也就是说：

- selector 是兜底，不是主定位手段

## 14. 动态参数占位设计

运行时动态参数建议统一放在 `bindings` 里：

```json
{
  "bindings": {
    "prompt": "生成一段人物转身离开的镜头",
    "reference_media_id": "media_abc123",
    "start_seconds": 1.2,
    "end_seconds": 4.8,
    "duration_seconds": 8,
    "aspect_ratio": "16:9"
  }
}
```

模板里不存值，只存绑定关系。

例如：

```json
{
  "op": "fill_prompt",
  "bind": {
    "prompt": "$bindings.prompt"
  }
}
```

## 15. 动态参数分类

建议按三类处理：

### 15.1 文本参数

- `prompt`
- 其他可能的补充文本

处理方式：

- 注入到 `fill_prompt`

### 15.2 资源参数

- `reference_media_id`
- `selected_material_index`

处理方式：

- 注入到 `select_reference_media`
- 优先由 operator 按 `reference_media_id` 命中页面素材卡片
- 如果页面短时排序不稳定，可退回 `selected_material_index` 作为同项目素材列表的兜底选择

### 15.3 区间参数

- `start_seconds`
- `end_seconds`
- `start_frame_index`
- `end_frame_index`

处理方式：

- 注入到 `set_time_range`
- operator 内部先归一化为统一时间表达，再执行

## 16. 模板结构

建议 workflow 模板结构：

```json
{
  "template_id": "flow.edit_video.v1",
  "workflow_mode": "edit_video",
  "version": 1,
  "operators": [
    { "op": "open_project" },
    { "op": "ensure_video_surface" },
    { "op": "select_edit_video_mode" },
    {
      "op": "select_reference_media",
      "bind": { "reference_media_id": "$bindings.reference_media_id" }
    },
    {
      "op": "set_time_range",
      "bind": {
        "start_seconds": "$bindings.start_seconds",
        "end_seconds": "$bindings.end_seconds"
      }
    },
    {
      "op": "fill_prompt",
      "bind": { "prompt": "$bindings.prompt" }
    },
    { "op": "submit" },
    { "op": "collect_post_submit" }
  ]
}
```

## 17. 文生视频模板示例

```json
{
  "template_id": "flow.text_to_video.v1",
  "workflow_mode": "text_to_video",
  "version": 1,
  "operators": [
    { "op": "open_project" },
    { "op": "ensure_video_surface" },
    { "op": "select_text_video_mode" },
    {
      "op": "fill_prompt",
      "bind": { "prompt": "$bindings.prompt" }
    },
    { "op": "submit" },
    { "op": "collect_post_submit" }
  ]
}
```

## 18. 媒体工作台派生图生视频模板示例

```json
{
  "template_id": "flow.image_to_video_from_workbench.v1",
  "workflow_mode": "image_to_video",
  "version": 1,
  "operators": [
    { "op": "open_project" },
    { "op": "ensure_video_surface" },
    { "op": "select_edit_video_mode" },
    {
      "op": "select_reference_media",
      "bind": {
        "reference_media_id": "$bindings.reference_media_id",
        "selected_material_index": "$bindings.selected_material_index"
      }
    },
    {
      "op": "fill_prompt",
      "bind": { "prompt": "$bindings.prompt" }
    },
    { "op": "submit" },
    { "op": "collect_post_submit" }
  ]
}
```

约束说明：

- 来源图片必须已经位于当前账号、当前项目下
- 这类 workflow 当前统一从媒体工作台派生入口进入，不再从“生成测试”tab 独立进入
- `selected_material_index` 只作为 `reference_media_id` 未命中时的兜底，不应替代素材 identity

## 19. 编辑视频模板示例

```json
{
  "template_id": "flow.edit_video.v1",
  "workflow_mode": "edit_video",
  "version": 1,
  "operators": [
    { "op": "open_project" },
    { "op": "ensure_video_surface" },
    { "op": "select_edit_video_mode" },
    {
      "op": "select_reference_media",
      "bind": {
        "reference_media_id": "$bindings.reference_media_id"
      }
    },
    {
      "op": "set_time_range",
      "bind": {
        "start_seconds": "$bindings.start_seconds",
        "end_seconds": "$bindings.end_seconds",
        "start_frame_index": "$bindings.start_frame_index",
        "end_frame_index": "$bindings.end_frame_index"
      }
    },
    {
      "op": "fill_prompt",
      "bind": { "prompt": "$bindings.prompt" }
    },
    { "op": "submit" },
    { "op": "collect_post_submit" }
  ]
}
```

## 19. 示教采集如何辅助生成模板

人工跑一次完整流程后，分析器要做的不是“照着回放”，而是：

1. 识别操作序列
2. 将操作映射到 operator
3. 提取稳定锚点
4. 标记动态参数槽位
5. 输出模板候选

例如：

- 人工点击了某个素材卡片
- 分析器不保存“点第 2 张卡”
- 而是产出 `select_reference_media`，并记录找到该素材卡片时用到的锚点证据

## 21. 自动采集是否能完全替代人工建模

不能。

自动采集更适合帮助我们回答：

- 正确流程是什么
- 页面有哪些稳定锚点
- 某一步为何会失败

但真正的 operator 设计仍需要人工抽象。

因此比较务实的做法是：

- 用采集加速 operator 发现
- 用 operator 保障正式执行质量

## 21. 第一阶段推荐实现

如果按投入产出比排序，第一阶段建议先做：

1. `capture_mode` 事件采集
2. trace 包落盘
3. operator 框架
4. 两个模板：
   - `text_to_video`
   - `edit_video`
5. bindings 动态参数注入

先不要做：

- 自动从 trace 一键生成模板
- 通用可视化编排器
- 复杂 AI 分析器

## 22. 第二阶段可选增强

后面可以再加：

- trace diff 分析
- operator 回放对比工具
- 失败截图聚类
- 锚点稳定性评分
- 半自动模板生成器

## 23. 和当前项目的关系

这份文档与 [video-ui-workflow-redesign.md](file:///Users/baolei/workspace/gflow-proxy-server/docs/video-ui-workflow-redesign.md) 配合使用：

- `video-ui-workflow-redesign.md`
  - 负责定义新 workflow 总体架构
- 本文档
  - 负责定义 capture、operator、template、bindings 的具体落地方式

## 24. 总结

最适合当前项目的路线不是：

- 纯录制回放

而是：

> 用 `capture_mode` 帮你把人工黄金流程拆解成稳定锚点，再用 operator + template + bindings 把它变成正式可执行的 UI workflow。
