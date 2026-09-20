# Changelog

本文件记录 maibook-plugin（麦书 / MaiBook）的版本变更。

格式基于 [Keep a Changelog](https://keepachangelog.com/zh-CN/)，
版本号遵循 [Semantic Versioning](https://semver.org/lang/zh-CN/)。

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
