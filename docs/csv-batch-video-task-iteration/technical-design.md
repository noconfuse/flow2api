# 批量视频任务技术方案

## 1. 技术目标

技术实现围绕下面的产品形态展开：

- 前端只上传 Excel
- 后端解析 Excel
- 后端完成校验和异步执行
- 前端只回看任务状态与结果

技术设计的核心包括下面四层：

- Excel 解析层
- 任务校验层
- 批量任务持久化层
- 异步执行层

## 2. 总体流程

### 2.1 创建任务流程

1. 用户上传 `.xlsx`
2. 后端解析 workbook
3. 后端提取 `Tasks` sheet 行数据
4. 后端提取每行素材槽位对应的媒体
5. 后端做格式和业务校验
6. 返回预校验结果
7. 用户确认
8. 后端创建 `BatchJob` 和 `BatchJobItem`
9. 后端异步逐行执行
10. 执行结果持续回写数据库

### 2.2 查看任务流程

1. 前端读取任务列表
2. 前端读取任务详情
3. 后端返回整批状态和行级状态

## 3. 前端职责

`/batch` 页面只保留以下 API 交互：

- 下载模板
- 上传 Excel 并解析
- 确认创建任务
- 查询任务列表
- 查询任务详情

## 4. Excel 解析层

## 4.1 解析内容

后端解析 `.xlsx` 时需要同时读取：

- 单元格文本
- 下拉值
- 超链接
- 图片对象
- 视频对象或模板定义的视频嵌入元数据

解析输出统一收敛为行级结构：

- `task_type`
- `model`
- `duration`
- `aspect_ratio`
- `prompt`
- `image_1 ~ image_5`
- `video_1 ~ video_2`

## 4.2 媒体来源统一抽象

无论素材来自哪种来源，最终都统一抽象为：

- `source_type`
  - `embedded`
  - `remote_url`
- `slot_name`
- `mime_type`
- `file_name`
- `content_bytes` 或可延迟获取的内容句柄

这层抽象是后续校验和执行的统一入口。

## 4.3 关于本地媒体

如果要求“上传一个 Excel 就够”，那么本地媒体必须以可随 workbook 一起提交的形式存在。

因此技术上只认两种本地素材承载方式：

- 真正嵌入 workbook 的媒体对象
- 模板附加机制打包进 workbook 的媒体 part

纯文本本地路径不算有效输入。

## 5. 校验层

## 5.1 语法校验

语法校验负责：

- workbook 是否是合法 `.xlsx`
- 是否存在 `Tasks` sheet
- 表头是否匹配
- 必填列是否有值
- prompt 占位符格式是否合法

## 5.2 业务校验

业务校验负责：

- `task_type` 是否支持
- `model` 是否支持当前任务类型
- `duration` 是否是该模型允许的选项
- `aspect_ratio` 是否是该模型允许的选项
- `r2v` 是否至少有一张图
- `edit` 是否至少有一个视频
- prompt 引用的 `@image_n / @video_n` 是否在当前行真实存在

## 5.3 输出结构

预校验输出统一返回：

- 总行数
- 有效行数
- 错误行数
- 每行错误
- 每行警告
- 标准化后的行对象

## 6. 数据模型

### 6.1 batch_jobs

建议字段：

- `id`
- `job_id`
- `file_name`
- `template_version`
- `status`
- `total_count`
- `pending_count`
- `running_count`
- `success_count`
- `failed_count`
- `created_by`
- `created_at`
- `started_at`
- `finished_at`

### 6.2 batch_job_items

建议字段：

- `id`
- `job_id`
- `row_index`
- `system_row_key`
- `task_type`
- `normalized_payload`
- `status`
- `error_code`
- `error_message`
- `result_media_id`
- `result_url`
- `created_at`
- `started_at`
- `finished_at`

说明：

- `system_row_key` 替代用户输入 `row_id`
- `normalized_payload` 内保留行级槽位与 prompt 信息

## 7. 异步执行层

## 7.1 执行原则

一旦用户确认创建任务，执行就进入后端队列。

执行器负责：

- 取出待执行 item
- 根据 `task_type` 组装现有生成请求
- 复用现有统一生成主链路
- 回写结果与错误

## 7.2 与现有主链路的关系

- Excel 行先被标准化
- 标准化结果再被转换成现有 `generation_handler` 可消费的请求

这样可以继续复用：

- token 自动调度
- project 自动解析
- 并发控制
- video workflow
- 结果轮询

## 7.3 prompt 引用处理

`@image_1 ~ @video_2` 只是批量协议内部语义。

执行阶段需要增加一层“prompt 重写器”：

- 输入：
  - 行级 prompt
  - 槽位媒体
- 输出：
  - 上游最终可接受的 prompt

如果未来 Google Flow 明确支持 `@` 引用，可直接映射；
如果上游采用其他语法，则在这里统一转换。

## 8. API 设计

### 8.1 下载模板

- `GET /api/batch/template.xlsx`

返回当前版本 Excel 模板。

### 8.2 解析模板

- `POST /api/batch/excel/parse`

请求：

- 单个 `.xlsx` 文件

返回：

- 解析结果
- 校验结果
- 标准化任务项

### 8.3 创建批量任务

- `POST /api/batch/jobs`

请求：

- 模板文件名
- 解析得到的标准化任务项

返回：

- `job_id`
- 初始状态

### 8.4 查询任务列表

- `GET /api/batch/jobs`

### 8.5 查询任务详情

- `GET /api/batch/jobs/{job_id}`

## 9. 第一阶段技术边界

第一阶段建议明确边界，避免一开始就过重：

- 只支持 `.xlsx`
- 只支持一个 `Tasks` 业务表
- 先不做在线编辑
- 先不做任务重试
- 先不做结果导出
- 先不做多视频编辑高级语义

## 10. 结论

这套技术方案的重点是把下面三个责任边界彻底理顺：

- Excel 负责描述任务
- 页面负责提交和查看
- 后端负责解释和执行
