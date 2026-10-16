# Changelog

本文件记录 maibook-plugin（麦书 / MaiBook）的版本变更。

格式基于 [Keep a Changelog](https://keepachangelog.com/zh-CN/)，
版本号遵循 [Semantic Versioning](https://semver.org/lang/zh-CN/)。

## [0.1.5] - 2026-10-16

### 修复

- `publish_deliver`（`format=disk`）落盘文件名中的 `version` 现与书名一样经 `_safe_component` 清洗。`version` 可经 `bookshelf_meta` 写入任意字符串，含 `../` 时编译产物会写到 `compiled/` 之外
- 文档：`config.default.toml` 去掉已删除的「固定模型路径」说明；README 就绪门禁的工具名更正为 `review_ready`

### 变更

- manifest 兼容上限改为实际验证过的范围：宿主 `max_version` 1.99.99 → **1.3.99**，SDK `max_version` 2.99.99 → **2.14.99**（SDK 2.14.0 上测试通过）。插件仓库审核不再接受「永远兼容」式的上限

## [0.1.4] - 2026-10-14

### 修复

- 移除写手的「固定模型」回退路径：它借用宿主内部 `TaskConfig`/`LLMOrchestrator`，在宿主 1.3.0+ 上因 `TaskConfig` 删除 `slow_threshold` 必然失败，且会用「当前宿主不支持固定具体模型…」覆盖宿主的真实报错。具体模型名本就经公开 `ctx.llm.generate(model_name=…)` 直选，宿主报错现原样返回

### 变更

- 宿主最低版本升至 **1.2.5**（公开 LLM 能力区分 `task_name` 与 `model_name` 的首个版本，0.1.3 起即依赖）；SDK 仍为 **≥2.8.1**

## [0.1.3] - 2026-09-20

### 修复

- 写手与章节摘要的 LLM 调用改为按 Host 任务名走 `task_name`，具体模型走 `model_name`。SDK 2.8.1 会默认附带 `task_name="utils"`，若再把 `replyer`/`utils` 放进 `model`，Host 会当成具体模型名并报「未找到名为 … 的模型」

## [0.1.2] - 2026-08-01

### 变更

- 将带注释的配置模板改为 `config.default.toml`，运行期 `config.toml` 不再入库
- 在 `create_plugin` / `on_load` 中从模板补齐或恢复 Runner 生成的空壳配置

## [0.1.1] - 2026-07-10

### 变更

- 写手 system prompt 以具名 AI 生命体的「麦书」模块自居，并标注性格与表达风格（章节摘要 prompt 未改）

### 文档

- 文档 URL 改为 `main` 分支；停止跟踪 WebUI `config_back` 备份

## [0.1.0] - 2026-06-17

### 新增

- 首次发布：按聊天流隔离的笔记本，多本书目录（`book.toml`、`instructions.md`、`manuscript/`、`bible/`、`summaries/`、`journal/`）
- 专职写手模型生成正文，麦麦任主编；工具按 `bookshelf_` / `setup_` / `write_` / `review_` / `publish_` 前缀分组（【麦书/maibook·…】标签）
- 支持序章（章节 0 → `00-chapter.md`）；整书投递哨兵改为 `-1`
- `write_chapter` / `write_revise` 非阻塞，完成后经 `context.append` + `proactive.trigger` 唤醒 planner
- `review_read` 可选 `send=text/png` 发到聊天流
- N±1 章节衔接、编辑/模型记入 `journal/credits.md`、完成章推送给编辑
- `write_chapter` 覆盖写入与历史快照；创作说明须经 `setup_instructions` 由麦麦撰写
- 聊天长图：`device_scale_factor=1.0` + PNG→无损 WebP，适配 NapCat 默认 15s ack；声明 Pillow 依赖
- 超出限制时产生警告；添加 MIT LICENSE
