# 批量视频任务方案

本轮重新确认后的核心方向只有一句话：

> 前端只负责“任务列表 + 上传 Excel 创建批量任务”，真正的任务描述、模型选择、素材引用都收敛到 Excel 模板里。

## 当前文档

- `product.md`
  - 产品目标、页面形态、用户流程、任务状态与结果展示
- `excel-template.md`
  - Excel 模板字段约定、素材槽位规则、prompt 引用规则、模板填写体验
- `technical-design.md`
  - 后端解析、校验、任务持久化、异步执行、结果回写、接口定义
- `implementation-plan.md`
  - 实施顺序、阶段目标、风险控制与验收口径

## 当前统一结论

- `/batch` 页面只保留两个核心能力：
  - 任务列表
  - 创建批量任务
- 创建批量任务的动作只有一个：
  - 上传 Excel 文件
- 上传后流程固定为：
  - 解析 Excel
  - 校验格式与素材引用
  - 用户确认
  - 后端异步执行
- Excel 模板内固定预留：
  - `image_1 ~ image_5`
  - `video_1 ~ video_2`
- `prompt` 中允许引用：
  - `@image_1 ~ @image_5`
  - `@video_1 ~ @video_2`

## 阅读顺序

建议按下面顺序阅读：

1. `product.md`
2. `excel-template.md`
3. `technical-design.md`
4. `implementation-plan.md`
