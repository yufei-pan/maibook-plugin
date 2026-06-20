# 麦书 (MaiBook)

让麦麦在每个聊天里**创作并长期维护属于她自己的书**。

麦书给麦麦一个**按聊天流隔离**的「笔记本」工作区：里面可以建立多本书，跨多次会话逐步写下去，
保持她自己的声音，又不会把聊天上下文撑爆。**这是麦麦本人的书，她是作者与主编**；信息不齐时
「不动笔」，先把要拍板的问题抛给麦麦自己决定（拿不准再问用户）。

## 核心概念

- **笔记本（工作区）**：每个聊天流一个，另有一个 `__global__` 全局工作区，作为麦麦写给自己的随笔/日记。一个笔记本里可放多本书。
- **书 = 一个目录**（不是单文件），便于分块、逐章编辑：
  - `book.toml` —— 元信息（书名/副标题/作者/标语/题材/基调/视角/篇幅/语言/标签/版本/状态/自动续写等）
  - `instructions.md` —— 本书专属创作说明（写手每次写作都会读到）
  - `manuscript/` —— 正文，**一章一个文件**（`00-chapter.md` 起，**第 0 章可作序章/楔子**；`01-chapter.md`…），小节用 `## 标题`
  - `bible/` —— 隐藏设定（`world.md`/`characters.md`/`plot-outline.md`/`stats.md`…），**不会编进成书**，但始终作为写作上下文
  - `summaries/` —— 各章滚动摘要（控制上下文预算）
  - `journal/` —— `questions.md`（待拍板的问题）/ `decisions.md`（已确认的正典）
  - `compiled/` —— 编译产物（单文件 `.md`）；`.history/` —— 修订快照（「版本」的真实落点）
- **专职写手**：正文由一次 `llm.generate` 生成，系统提示词里**自动注入麦麦的人格与表达风格**；麦麦本人做主编，通过工具下达指令、审稿、拍板。
- **上下文装配**：写作前按字符预算装配「要点 + 大纲 + 人物 + 设定 + 已确认的决定 + 历史摘要 + 上一章结尾」，即使大上下文模型也不会每次重发整本书。
- **就绪门禁**：新书初始为 `setup`，必要要素齐全前**拒绝写作**；齐全后 `check_ready` 置为 `ready`。
- **问答闭环**：缺信息时，写手输出 `===NEED_INFO===` 区块；程序把问题**抛给麦麦**（按需调用时体现在任务状态里；后台续写时通过 `maisaka.context.append` + `proactive.trigger` 唤醒她）。麦麦自行决定或问用户后，用 `setup_answer` 记入正典，流回下一次写作。
- **写作/修订非阻塞、完成后主动唤醒**：写一章常需数分钟，远超宿主 `plugin.invoke_tool` 的 RPC 超时（约 60s），硬等必然 `E_TIMEOUT`。因此 `write_chapter` / `write_revise` **不再等生成完成**：先做快速校验（就绪门禁、章号、参考稿等），立即返回，真正的生成在后台进行。**任务完成后由插件主动把正文（或待拍板问题）经 `context.append` 注入麦麦上下文，并用 `proactive.trigger` 唤醒她**——所以麦麦不必轮询（叫她轮询会让 Planner 反复调用直到耗光思考轮次）。这样麦麦和写手可以**同时工作**。同一本书同一时刻只允许一个写作/修订任务（再次发起会复用现有 `task_id`）；`write_status` 仍保留，仅供她想提前查看时用。
- **工具名按功能分类（不再统一前缀）**：宿主内置的 `tool_search` 对一个查询**只返回前 5 个命中**（按分数排序，同分按名称字母序截断）。早先所有工具都叫 `maibook_*`，麦麦搜 `maibook` 时 16 个工具同分，只拿到字母序前 5 个的完整定义，其余全靠猜。现按功能拆成 5 类前缀、**每类 ≤ 4 个**：`bookshelf_`（书库）/`setup_`（筹备设定）/`write_`（写作修订）/`review_`（阅读审阅）/`publish_`（交付）。这样搜任一类前缀都能在 5 个上限内拿到该类**全部**工具。**新增工具时务必并入既有类、保持每类 ≤ 4**，否则会重蹈截断覆辙。

## 工具一览（按功能分 5 类前缀，供麦麦的 Planner 调用）

> 每个工具简介都以 `【麦书/maibook·<类>】` 开头——麦麦在 deferred-tools 提示里一眼就能认出这些是麦书（写书）工具。
> 想真正调用某个工具时，用 `tool_search` 搜该类前缀（如 `write`/`review`/`bookshelf`），即可在 5 条上限内拿到该类**全部**工具的完整参数定义。

**`bookshelf_` —— 书库 / 书目层**

| 工具 | 作用 |
|------|------|
| `bookshelf_list` | 列出本聊天/全局的书及状态 |
| `bookshelf_create` | 新建书（初始 `setup`，可设 `autopilot`、`scope`） |
| `bookshelf_meta` | 批量改元信息（`updates` 对象） |
| `bookshelf_autopilot` | 设置某书是否后台自动续写 |

**`setup_` —— 筹备 / 设定 / 拍板**

| 工具 | 作用 |
|------|------|
| `setup_outline` | 写/更新分章大纲 |
| `setup_instructions` | 设置本书创作说明 |
| `setup_bible` | 写/追加隐藏设定（人物/世界/数值…） |
| `setup_answer` | 把拍板的决定写入正典并清空待答问题 |

**`write_` —— 写作 / 修订 / 进度**

| 工具 | 作用 |
|------|------|
| `write_chapter` | 为已就绪的书写一章（**非阻塞**：立即返回 `task_id`，后台生成；缺信息回报问题，不硬写）；可传 `content` 作为本章参考稿/草稿，写手会在其基础上完成 |
| `write_revise` | 修订某章（整章或仅某 `## 小节`，**非阻塞**：立即返回 `task_id`），改前自动快照 |
| `write_status` | （可选）主动查后台写作/修订任务的进度；通常任务**完成会自动唤醒你并把正文加入上下文**，无需主动查 |

**`review_` —— 阅读 / 审阅 / 问答**

| 工具 | 作用 |
|------|------|
| `review_read` | 读概览/元信息/大纲/设定/正文清单/摘要/问题/决定；**读某一章正文直接传 `chapter=<章号>`**（从 0 开始，**第 0 章可作序章/楔子**）。读到的内容**默认只回麦麦自己的上下文、不发聊天**；加 `send=text`/`send=png` 可顺便把这段发到聊天（复用 `publish_deliver` 的发送方式） |
| `review_ready` | 检查要素是否齐全，齐全则置 `ready` |
| `review_questions` | 查看待拍板的问题 |

**`publish_` —— 交付 / 成书**

| 工具 | 作用 |
|------|------|
| `publish_deliver` | 交付：`disk`（落盘给路径）/`text`（直接分段发聊天）/`png`（渲染长图发聊天） |
| `publish_cover` | 渲染封面并**送入上下文**（供麦麦与用户一起打磨，可传 `style` 反复迭代） |

另提供面向人类用户的命令 `/book list`。

### 关于交付与封面
平台**没有发送文件的能力**（适配器仅支持 text/image/emoji），因此成书的获取方式是：
落盘单文件 `.md`（推荐，路径在本机）/ 直接分段发文本（**绕过回复管线，避免被其它插件改写**）/ 渲染为长图。
封面则走另一条路：以 `content_items` 图片**返回到上下文**（参照 fetch-url 的做法），让麦麦和用户都能看到并据此迭代。

发到聊天的长图特意**控制体积**：以 1x 像素比渲染（`render.html2png` 默认 2x，会让黑白文字长图体积翻几倍），再用 **Pillow 无损 WebP** 重编码。这样在 NapCat 默认约 15s 的动作超时内也能传完——否则动作其实已送达、却因回执超时被宿主误报「发送失败」（日志里是 `[SendService] Platform IO 发送失败 … error=`，空 error 即回执超时）。Pillow 缺失时回退原 PNG 并告警。

## 配置项（`config.toml`）

- `[plugin]`：`enabled`、`config_version`、`allow_autopilot`（后台自动续写**总开关**）
- `[writer]`：`writer_model`（见下）、`temperature`、`max_tokens`、`timeout_seconds`（单次写作/修订调用超时，默认 600s；写一章常需数分钟，<=0 用宿主默认）、`style_supplement`（追加写作风格）
- `[context]`：`char_budget`（写作上下文字符预算，默认 262144；`bible/` 下**全部**设定文件——含通过 `setup_bible` 写入的任意自定义主题——都会作为参考资料带给写手）、`include_rolling_summary`、`summary_task`（摘要用的任务名，默认 `utils`）
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

依赖：`tomli-w`（写 TOML；读用标准库 `tomllib`，需 Python 3.11+）、`Pillow`（把发到聊天的长图无损重编码为 WebP 以控制体积）。均已在 `_manifest.json` 声明。

## 测试

用 uv 跑冒烟测试（mock 掉 ctx，走通主流程）：

```bash
uv run --with tomli-w --with pillow --with-editable ../maibot-plugin-sdk python tests/smoke_test.py
```

## 设计取舍

- **章＝文件，小节＝`##`**；不做「页」（印刷遗物，只会割裂 LLM 上下文）。
- **写作/修订非阻塞 + 完成主动唤醒（不轮询）**：宿主的工具 RPC 有约 60s 超时，而写一章要几分钟——同步等必然超时（`E_TIMEOUT`）。故改为「立即返回」，后台 asyncio 任务里生成，同书互斥（一次一个）；任务结束后用 `context.append` 注入正文/问题 + `proactive.trigger` 主动唤醒麦麦。**不让她轮询**——否则 Planner 会反复调 `write_status` 直到耗光默认思考轮次。这样麦麦不被一次写作卡住，能边写边干别的，写好了又会被主动叫回来看。
- **工具名按功能分类、每类 ≤ 4**：宿主 `tool_search` 每查只回前 5 个命中且同分按名称字母序截断；统一 `maibook_*` 前缀会让 16 个工具同分、只露出字母序前 5 个，麦麦看不到其余定义只能臆测。拆成 `bookshelf_`/`setup_`/`write_`/`review_`/`publish_` 五类、每类 ≤ 4，搜任一类前缀都能在上限内拿全该类。这是 SDK/宿主未文档化的限制，只能在插件侧用命名规避——**新增工具必须并入既有类并保持每类 ≤ 4**。
- **不设 `ask_user` 工具**：麦麦本就能靠「回复」问用户；写手是一次性生成、无工具、无自主性，问题由程序抛给麦麦。
- **问题只抛给麦麦，不直接发用户**：这是她的书，她可凭设定与长期记忆自行决定，必要时再问用户。
- D&D / 跑团托管**不属于本插件**（另作独立插件）。
