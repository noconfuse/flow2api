# Excel 模板约定

## 1. 模板定位

Excel 模板是批量任务的唯一输入载体。

用户不需要在 `/batch` 页面重新配置模型和参数，也不需要理解系统内部字段。

用户只需要按照模板填写任务，然后上传 `.xlsx` 文件。

## 2. 文件格式

第一阶段接受：

- `.xlsx`

## 3. 工作表结构

建议模板包含两个工作表：

- `Tasks`
  - 用户可见、可编辑
  - 真正的任务数据表
- `Options`
  - 隐藏工作表
  - 用于数据验证下拉和模板版本信息

对用户来说，真正需要操作的只有 `Tasks`。

## 4. Tasks 表列定义

`Tasks` 工作表固定列如下：

```text
task_type | model | duration | aspect_ratio | prompt | image_1 | image_2 | image_3 | image_4 | image_5 | video_1 | video_2
```

字段说明：

- `task_type`
  - 任务类型
  - 可选值：`t2v`、`r2v`、`edit`
- `model`
  - 模型选择
  - 通过模板下拉从系统支持模型中选择
- `duration`
  - 视频时长
  - 通过模板下拉选择
- `aspect_ratio`
  - 横竖版
  - 通过模板下拉选择，至少支持 `16:9`、`9:16`
- `prompt`
  - 当前行提示词
  - 允许引用当前行素材槽位
- `image_1 ~ image_5`
  - 当前行图片槽位
- `video_1 ~ video_2`
  - 当前行视频槽位

## 5. 模板字段范围

模板中不包含以下字段：

- `row_id`
- `token_id`
- `project_id`
- `media_id`
- `start_time`
- `end_time`

原因：

- `row_id` 由系统生成
- `token_id / project_id / media_id` 是系统内部概念
- `start_time / end_time` 暂不纳入第一阶段模板范围

## 6. prompt 引用规则

`prompt` 中允许引用当前行固定槽位：

- `@image_1`
- `@image_2`
- `@image_3`
- `@image_4`
- `@image_5`
- `@video_1`
- `@video_2`

这一层引用只在当前行内有效，不允许跨行引用。

示例：

```text
让 @image_1 作为主体，参考 @image_2 的构图和 @image_3 的光影，生成自然连贯的视频
```

```text
基于 @video_1 生成更自然的镜头运动，保持主体身份一致
```

## 7. 素材槽位填写规则

### 7.1 图片槽位

图片槽位支持两种输入方式：

- 方式 A：在单元格中插入本地图片
- 方式 B：单元格填写公开可访问的图片 URL

### 7.2 视频槽位

视频槽位支持两种输入方式：

- 方式 A：通过模板支持的本地视频嵌入机制放入工作簿
- 方式 B：单元格填写公开可访问的视频 URL

## 8. 本地文件支持原则

- 如果用户只是手填本地磁盘路径，例如 `D:\a\b\c.mp4`
  - 后端无法直接读取用户本地磁盘
- 所谓“支持本地文件”，必须满足下面之一：
  - 文件被真正嵌入到 Excel 工作簿中
  - 模板配套脚本把本地文件打包进工作簿

因此第一阶段产品口径必须明确：

> 本地素材以随 Excel 一起提交的方式承载。

## 9. 模板交互建议

为了让用户体验简单，模板应尽量提供：

- `task_type` 下拉
- `model` 下拉
- `duration` 下拉
- `aspect_ratio` 下拉
- 表头高亮
- prompt 占位符提示
- 示例行

这些能力都属于模板层，不属于 `/batch` 页面。

## 10. 行级业务规则

### 10.1 t2v

- 必填：
  - `task_type`
  - `model`
  - `prompt`
- 素材槽位允许为空

### 10.2 r2v

- 必填：
  - `task_type`
  - `model`
  - `prompt`
  - 至少一个图片槽位

### 10.3 edit

- 必填：
  - `task_type`
  - `model`
  - `prompt`
  - 至少一个视频槽位

## 11. 模板示意

```text
task_type | model              | duration | aspect_ratio | prompt                                           | image_1 | image_2 | image_3 | image_4 | image_5 | video_1 | video_2
r2v       | veo_3_1_r2v_fast   | default  | 16:9         | 让 @image_1 作为主体，参考 @image_2 与 @image_3  | [图1]    | [图2]    | [图3]    |         |         |         |        
edit      | omni-flash-edit    | default  | 16:9         | 基于 @video_1 生成更自然的镜头运动               |         |         |         |         |         | [视频1]  |        
```

## 12. 结论

模板的核心是规则稳定。

只要把下面三件事稳定下来，这个方案就成立：

- 固定素材槽位
- 固定 prompt 引用语法
- 固定模型与参数下拉

页面就可以保持简单，后端也可以围绕统一协议实现。
