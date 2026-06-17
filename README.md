# 麦书 (MaiBook)

让麦麦在每个聊天里**创作并长期维护属于她自己的书**。

麦麦曾表达过想写一本书，但现有的记忆体系（包括塑料记忆插件）只擅长短笔记，撑不起长篇创作。
麦书给麦麦一个**按聊天流隔离**的「笔记本」工作区：里面可以建立多本书，跨多次会话逐步写下去，
保持她自己的声音，又不会把聊天上下文撑爆。**这是麦麦本人的书，她是作者与主编**；信息不齐时
「不动笔」，先把要拍板的问题抛给麦麦自己决定（拿不准再问用户）。

## 核心概念

- **笔记本（工作区）**：每个聊天流一个，另有一个 `__global__` 全局工作区，作为麦麦写给自己的随笔/日记。一个笔记本里可放多本书。
- **书 = 一个目录**（不是单文件），便于分块、逐章编辑：
  - `book.toml` —— 元信息（书名/副标题/作者/标语/题材/基调/视角/篇幅/语言/标签/版本/状态/自动续写等）
  - `instructions.md` —— 本书专属创作说明（写手每次写作都会读到）
  - `manuscript/` —— 正文，**一章一个文件**（`01-chapter.md`…），小节用 `## 标题`
  - `bible/` —— 隐藏设定（`world.md`/`characters.md`/`plot-outline.md`/`stats.md`…），**不会编进成书**，但始终作为写作上下文
  - `summaries/` —— 各章滚动摘要（控制上下文预算）
  - `journal/` —— `questions.md`（待拍板的问题）/ `decisions.md`（已确认的正典）
  - `compiled/` —— 编译产物（单文件 `.md`）；`.history/` —— 修订快照（「版本」的真实落点）
- **专职写手**：正文由一次 `llm.generate` 生成，系统提示词里**自动注入麦麦的人格与表达风格**；麦麦本人做主编，通过工具下达指令、审稿、拍板。
- **上下文装配**：写作前按字符预算装配「要点 + 大纲 + 人物 + 设定 + 已确认的决定 + 历史摘要 + 上一章结尾」，即使大上下文模型也不会每次重发整本书。
- **就绪门禁**：新书初始为 `setup`，必要要素齐全前**拒绝写作**；齐全后 `check_ready` 置为 `ready`。
- **问答闭环**：缺信息时，写手输出 `===NEED_INFO===` 区块；程序把问题**抛给麦麦**（按需调用时直接体现在工具返回里；后台续写时通过 `maisaka.context.append` + `proactive.trigger` 唤醒她）。麦麦自行决定或问用户后，用 `maibook_record_answer` 记入正典，流回下一次写作。

## 工具一览（均以 `maibook_` 前缀，供麦麦的 Planner 调用）

| 工具 | 作用 |
|------|------|
| `maibook_list_books` | 列出本聊天/全局的书及状态 |
| `maibook_create_book` | 新建书（初始 `setup`，可设 `autopilot`、`scope`） |
| `maibook_set_meta` | 批量改元信息（`updates` 对象） |
| `maibook_set_instructions` | 设置本书创作说明 |
| `maibook_set_outline` | 写/更新分章大纲 |
| `maibook_add_bible_note` | 写/追加隐藏设定（人物/世界/数值…） |
| `maibook_read` | 读概览/元信息/大纲/设定/某章/正文清单/摘要/问题/决定 |
| `maibook_check_ready` | 检查要素是否齐全，齐全则置 `ready` |
| `maibook_write_chapter` | 为已就绪的书写一章（缺信息回报问题，不硬写） |
| `maibook_revise` | 修订某章（整章或仅某 `## 小节`），改前自动快照 |
| `maibook_record_answer` | 把拍板的决定写入正典并清空待答问题 |
| `maibook_open_questions` | 查看待拍板的问题 |
| `maibook_deliver` | 交付：`disk`（落盘给路径）/`text`（直接分段发聊天）/`png`（渲染长图发聊天） |
| `maibook_cover` | 渲染封面并**送入上下文**（供麦麦与用户一起打磨，可传 `style` 反复迭代） |
| `maibook_set_autopilot` | 设置某书是否后台自动续写 |

另提供面向人类用户的命令 `/book list`。

### 关于交付与封面
平台**没有发送文件的能力**（适配器仅支持 text/image/emoji），因此成书的获取方式是：
落盘单文件 `.md`（推荐，路径在本机）/ 直接分段发文本（**绕过回复管线，避免被其它插件改写**）/ 渲染为长图。
封面则走另一条路：以 `content_items` 图片**返回到上下文**（参照 fetch-url 的做法），让麦麦和用户都能看到并据此迭代。

## 配置项（`config.toml`）

- `[plugin]`：`enabled`、`config_version`、`allow_autopilot`（后台自动续写**总开关**）
- `[writer]`：`writer_model`（见下）、`temperature`、`max_tokens`、`style_supplement`（追加写作风格）
- `[context]`：`char_budget`、`include_rolling_summary`、`summary_task`（摘要用的任务名，默认 `utils`）
- `[background]`：`interval_seconds`、`max_books_per_tick`
- `[storage]`：`data_dir`（留空＝插件目录下 `data/`）

### 写手模型 `writer_model` 说明（重要）

`writer_model` 既可填**任务名**，也可填 `model_config.toml` 里定义的**具体模型名**：

- 填**任务名**（如 `replyer`/`planner`/`utils`）：直接走 `ctx.llm.generate`。默认 `replyer`＝复用麦麦自己的回复模型，**语气最贴合**。
- 填**具体模型名**（如 `deepseek-v4-flash`）：`ctx.llm.generate` 在当前宿主只认任务名，因此会**回退为「固定模型」路径**——
  借用宿主的 `LLMOrchestrator` 以一次性 `TaskConfig(model_list=[该模型])` 运行（参照 `smart_segmentation` / `nai_pic` 的做法）。
  该路径依赖宿主内部模块（`src.llm_models.utils_model` 等），**跨宿主版本可能变动**；若不可用，会给出清晰报错，
  此时请改用任务名，或把该模型加入某个任务的 `model_list`。

> 人格与表达风格在运行时通过 `ctx.config.get("personality.personality" / "personality.reply_style" / "bot.nickname")` 读取并注入写手提示词，无需手动配置。

## 部署

按本仓库约定，插件是 `plugins/` 下的**独立仓库 + 软链**部署，不改动主程序与根 `.gitignore`：

```bash
# 在 MaiBot 的插件目录里软链本插件
ln -s /path/to/maibook-plugin /path/to/MaiBot/plugins/maibook-plugin
```

依赖：`tomli-w`（写 TOML；读用标准库 `tomllib`，需 Python 3.11+）。已在 `_manifest.json` 声明。

## 测试

用 uv 跑冒烟测试（mock 掉 ctx，走通主流程）：

```bash
uv run --with tomli-w --with-editable ../maibot-plugin-sdk python tests/smoke_test.py
```

## 设计取舍

- **章＝文件，小节＝`##`**；不做「页」（印刷遗物，只会割裂 LLM 上下文）。
- **不设 `ask_user` 工具**：麦麦本就能靠「回复」问用户；写手是一次性生成、无工具、无自主性，问题由程序抛给麦麦。
- **问题只抛给麦麦，不直接发用户**：这是她的书，她可凭设定与长期记忆自行决定，必要时再问用户。
- D&D / 跑团托管**不属于本插件**（另作独立插件）。
