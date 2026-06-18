"""麦书插件冒烟测试。

用 mock 的 ctx 走通主流程：setup 门禁 → 补全要素 → 就绪 → 写章 → NEED_INFO →
修订 → 三种交付 → 封面 → 写手模型解析回退。

运行：
    uv run --with tomli-w --with-editable ../maibot-plugin-sdk python tests/smoke_test.py
"""

from __future__ import annotations

import asyncio
import logging
import sys
import tempfile
import traceback
from pathlib import Path
from types import SimpleNamespace

# 让测试能 import 同目录上层的 plugin.py
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import plugin as plugin_module  # noqa: E402
from plugin import MaiBookPlugin  # noqa: E402

PERSONA = {
    "bot.nickname": "麦麦",
    "personality.personality": "古灵精怪、爱读书、偶尔毒舌但心软",
    "personality.reply_style": "俏皮、口语化、爱用比喻",
}


class MockLLM:
    """可切换行为的写手/摘要模型。"""

    def __init__(self) -> None:
        self.mode = "normal"  # normal | need_info | unknown_task
        self.models: list[str] = []
        self.writer_prompts: list[str] = []  # 记录写手（非摘要）调用收到的完整提示词
        self.last_writer_kwargs: dict = {}  # 记录写手调用收到的额外参数（用于校验 timeout_ms 透传）

    async def generate(self, prompt, model="", temperature=None, max_tokens=None, **kwargs):
        self.models.append(model)
        text = prompt if isinstance(prompt, str) else " ".join(str(item.get("content", "")) for item in prompt)
        if "编辑助手" in text:  # 摘要任务
            return {"success": True, "response": "（摘要）林夏带着会说话的罗盘启程，立下寻找沉城的目标。", "model": model}
        self.writer_prompts.append(text)
        self.last_writer_kwargs = dict(kwargs)
        if self.mode == "unknown_task":
            return {"success": False, "error": f"未找到名为 `{model}` 的模型配置"}
        if self.mode == "need_info":
            return {
                "success": True,
                "response": "===NEED_INFO===\n- 主角的真实身份要不要在第二章揭示？\n- 结局走向是开放式还是闭合式？",
                "model": model,
            }
        return {
            "success": True,
            "response": "## 启程\n潮声漫过礁石，海雾未散。\n\n林夏握紧那枚旧罗盘，一步步朝灯塔走去。",
            "model": model,
        }


def build_ctx(llm: MockLLM, counters: dict[str, int]) -> SimpleNamespace:
    async def cfg_get(path, default=None):
        return PERSONA.get(path, default)

    async def send_text(text, stream_id, **kwargs):
        counters["text"] += 1
        return True

    async def send_image(image_data, stream_id, **kwargs):
        counters["image"] += 1
        return True

    async def render_html2png(html, **kwargs):
        counters["render"] += 1
        return {"image_base64": "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+M8AAAMBAQDJ/pLvAAAAAElFTkSuQmCC", "mime_type": "image/png", "width": 800, "height": 1200}

    async def ctx_append(**kwargs):
        counters["append"] += 1
        return {"success": True}

    async def trigger(**kwargs):
        counters["trigger"] += 1
        return {"success": True}

    return SimpleNamespace(
        logger=logging.getLogger("maibook.test"),
        config=SimpleNamespace(get=cfg_get),
        llm=llm,
        send=SimpleNamespace(text=send_text, image=send_image),
        render=SimpleNamespace(html2png=render_html2png),
        maisaka=SimpleNamespace(
            context=SimpleNamespace(append=ctx_append),
            proactive=SimpleNamespace(trigger=trigger),
        ),
    )


def check(condition: bool, message: str, payload=None) -> None:
    if not condition:
        raise AssertionError(f"{message} | payload={payload!r}")


async def main() -> None:
    llm = MockLLM()
    counters = {"text": 0, "image": 0, "render": 0, "append": 0, "trigger": 0}
    tmp = Path(tempfile.mkdtemp(prefix="maibook-test-"))

    p = MaiBookPlugin()
    p.set_plugin_config(MaiBookPlugin.build_default_config())
    p._set_context(build_ctx(llm, counters))  # noqa: SLF001 - 测试注入
    p.config.storage.data_dir = str(tmp)

    kw = {"stream_id": "test:group:42"}

    # 1) 新建书
    r = await p.maibook_create_book(title="星海拾遗", genre="奇幻", autopilot=True, scope="chat", **kw)
    check(r["success"], "create_book 应成功", r)
    slug = r["slug"]
    book_dir = p._book_dir("chat", kw["stream_id"], slug)
    check(p._meta_path(book_dir).exists(), "book.toml 应存在", str(book_dir))

    # 2) 未就绪时拒绝写作
    r = await p.maibook_write_chapter(book=slug, **kw)
    check(not r["success"] and r.get("status") == "setup" and r.get("missing"), "setup 门禁应拦截写作", r)

    # 3) 补全元信息
    r = await p.maibook_set_meta(
        book=slug,
        updates={
            "premise": "少女与会说话的罗盘一起寻找沉没之城",
            "tone": "温暖冒险",
            "pov": "第三人称过去时",
            "length_target": "3 章",
            "language": "中文",
            "subtitle": "罗盘与灯塔",
            "tagline": "向着海平线",
            "tags": ["奇幻", "冒险", "成长"],
        },
        **kw,
    )
    check(r["success"], "set_meta 应成功", r)

    # 4) 大纲 + 设定
    check((await p.maibook_set_outline(book=slug, content="第1章 启程；第2章 风暴；第3章 沉城", **kw))["success"], "set_outline 应成功")
    check((await p.maibook_add_bible_note(book=slug, topic="characters", content="林夏：14 岁，倔强好奇。", **kw))["success"], "add_bible_note(characters) 应成功")
    check((await p.maibook_add_bible_note(book=slug, topic="world", content="星海大陆，潮汐即魔法。", **kw))["success"], "add_bible_note(world) 应成功")

    # 4b) 回归：模型常把大纲正文放在 outline 键而非 content（历史 bug：静默写空并谎报成功）。
    #     用 outline 别名应能正确写入；真正空内容必须明确报错，且不得清空已有大纲。
    r = await p.maibook_set_outline(book=slug, outline="第1章 启程；第2章 风暴；第3章 沉城（outline 别名）", **kw)
    check(r["success"], "set_outline 用 outline 别名应成功", r)
    check(
        (book_dir / "bible" / "plot-outline.md").read_text(encoding="utf-8").strip() != "",
        "outline 别名应真正写入大纲文件（不得为空）",
    )
    r = await p.maibook_set_outline(book=slug, content="   ", mode="replace", **kw)
    check(not r["success"], "空内容的 set_outline 应明确报错而非谎报成功", r)
    check(
        (book_dir / "bible" / "plot-outline.md").read_text(encoding="utf-8").strip() != "",
        "空内容写入不得清空已有大纲",
    )

    # 4c) 回归：set_instructions 同样不得静默写空/谎报成功，且容忍 instructions 别名。
    r = await p.maibook_set_instructions(book=slug, content="   ", **kw)
    check(not r["success"], "空 set_instructions 应明确报错", r)
    r = await p.maibook_set_instructions(book=slug, instructions="# 写作说明\n保持温暖。", **kw)
    check(r["success"], "set_instructions 用 instructions 别名应成功", r)
    check(
        (book_dir / "instructions.md").read_text(encoding="utf-8").strip() != "",
        "instructions 别名应真正写入",
    )

    # 4d) 回归：set_meta 应回报被忽略的未知字段，并容忍字段平铺在顶层。
    r = await p.maibook_set_meta(book=slug, updates={"tone": "冷峻", "bogus_field": "x"}, **kw)
    check(r["success"] and "bogus_field" in r["content"], "set_meta 应在回执里点名被忽略的字段", r)
    r = await p.maibook_set_meta(book=slug, subtitle="平铺测试", **kw)
    check(r["success"], "set_meta 应容忍字段平铺在顶层（不经 updates）", r)

    # 5) 就绪
    r = await p.maibook_check_ready(book=slug, **kw)
    check(r.get("ready") is True, "check_ready 应通过", r)

    # 6) 写第 1 章
    r = await p.maibook_write_chapter(book=slug, **kw)
    check(r["success"] and r.get("chapter_no") == 1 and r.get("word_count", 0) > 0, "写第 1 章应成功", r)
    check(p._chapter_path(book_dir, 1).exists(), "第 1 章文件应存在")
    check((book_dir / "summaries" / "01-chapter.md").exists(), "第 1 章摘要应存在")

    # 7) NEED_INFO：不落稿，问题入档
    llm.mode = "need_info"
    r = await p.maibook_write_chapter(book=slug, chapter="2", **kw)
    check(r.get("status") == "need_info" and r.get("questions"), "缺信息应回报 need_info", r)
    check((book_dir / "journal" / "questions.md").read_text(encoding="utf-8").strip() != "", "问题应写入 questions.md")
    check(not p._chapter_path(book_dir, 2).exists(), "缺信息时不应落稿第 2 章")
    llm.mode = "normal"

    # 8) 记录决定 → 正典；清空问题
    r = await p.maibook_record_answer(book=slug, topic="结局", answer="采用开放式结局。", bible_topic="plot-outline", **kw)
    check(r["success"], "record_answer 应成功", r)
    check((book_dir / "journal" / "decisions.md").read_text(encoding="utf-8").strip() != "", "决定应写入 decisions.md")
    check((book_dir / "journal" / "questions.md").read_text(encoding="utf-8").strip() == "", "问题应被清空")

    # 9) 修订第 1 章 → 历史快照
    r = await p.maibook_revise(book=slug, chapter=1, instruction="开头更紧凑一些", **kw)
    check(r["success"], "revise 应成功", r)
    check(list((book_dir / ".history").glob("01-chapter.*.md")), "修订应留下历史快照")

    # 9b) 回归：bible/ 下的全部自定义设定 + 本章参考稿都要进入写手上下文，且超时可配置透传。
    check(
        (await p.maibook_add_bible_note(book=slug, topic="logic-theory", content="共生律：碎片以伪随机噪声互证存在。", **kw))["success"],
        "add_bible_note(自定义主题) 应成功",
    )
    p.config.writer.timeout_seconds = 600
    llm.writer_prompts.clear()
    r = await p.maibook_write_chapter(
        book=slug, chapter="2", content="## 风暴\n罗盘在掌心发烫，林夏盯着翻涌的海平线。", **kw
    )
    check(r["success"] and r.get("chapter_no") == 2, "带参考稿写第 2 章应成功", r)
    writer_prompt = llm.writer_prompts[-1]
    check("共生律" in writer_prompt, "自定义 bible 设定（非固定槽位）应进入写手上下文", writer_prompt[-400:])
    check("罗盘在掌心发烫" in writer_prompt, "本章参考稿（content）应进入写手上下文", writer_prompt[-400:])
    check(llm.last_writer_kwargs.get("timeout_ms") == 600000, "写手调用应透传可配置超时 timeout_ms", llm.last_writer_kwargs)

    # 9c) 回归：写作上下文超字符预算时必须告警，不得静默丢弃 bible 设定/参考资料。
    captured_warnings: list[str] = []

    class _WarnCapture(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            captured_warnings.append(record.getMessage())

    cap_handler = _WarnCapture(level=logging.WARNING)
    p.ctx.logger.addHandler(cap_handler)
    original_level = p.ctx.logger.level
    p.ctx.logger.setLevel(logging.WARNING)
    original_budget = p.config.context.char_budget
    try:
        big_lore = "潮汐魔法的细则与禁忌，逐条记录绝不能写错的关键事实。" * 200  # 远超最小预算，强制裁剪
        check(
            (await p.maibook_add_bible_note(book=slug, topic="big-lore", content=big_lore, **kw))["success"],
            "add_bible_note(big-lore) 应成功",
        )
        p.config.context.char_budget = 2000  # 最小预算，强制超预算裁剪
        r = await p.maibook_revise(book=slug, chapter=1, instruction="再紧凑一点", **kw)
        check(r["success"], "超预算时 revise 仍应成功（仅裁剪、不报错）", r)
        check(
            any("字符预算" in msg for msg in captured_warnings),
            "上下文超预算裁剪时应产生告警（不得静默丢弃设定/参考资料）",
            captured_warnings,
        )
    finally:
        p.config.context.char_budget = original_budget
        p.ctx.logger.setLevel(original_level)
        p.ctx.logger.removeHandler(cap_handler)

    # 10) 三种交付
    r = await p.maibook_deliver(book=slug, format="disk", **kw)
    check(r["success"] and Path(r["path"]).exists(), "disk 交付应产出文件", r)
    r = await p.maibook_deliver(book=slug, format="text", **kw)
    check(r["success"] and counters["text"] > 0, "text 交付应直接发送", (r, counters))
    r = await p.maibook_deliver(book=slug, format="png", **kw)
    check(r["success"] and counters["render"] > 0 and counters["image"] > 0, "png 交付应渲染并发图", (r, counters))

    # 10b) 回归：发送全部失败时，deliver 不得谎报 success（否则 planner 以为发出去了）。
    async def _failing_send(text, stream_id, **kwargs):
        return False

    original_send_text = p.ctx.send.text
    p.ctx.send.text = _failing_send
    r = await p.maibook_deliver(book=slug, format="text", **kw)
    check(not r["success"], "全部发送失败时 text 交付应回报 success=False", r)
    p.ctx.send.text = original_send_text

    # 11) 封面：content_items 图片入上下文
    r = await p.maibook_cover(book=slug, style="深蓝、海雾、复古", **kw)
    check(r["success"] and r.get("content_items"), "cover 应返回 content_items", r)
    check(r["content_items"][0]["content_type"] == "image" and r["content_items"][0]["data"], "封面应是图片 payload")

    # 12) 列书
    r = await p.maibook_list_books(scope="chat", **kw)
    check(r["success"] and r.get("count", 0) >= 1, "list_books 应列出书", r)

    # 13) 写手模型解析：未知任务名 → 尝试固定模型（测试环境无宿主内部模块）→ 友好报错
    p.config.writer.writer_model = "deepseek-v4-flash"
    llm.mode = "unknown_task"
    gen = await p._writer_generate("系统提示", "用户提示")
    check(not gen["success"] and ("固定" in gen.get("error", "") or "宿主" in gen.get("error", "")), "未知模型名应回退并友好报错", gen)
    p.config.writer.writer_model = "replyer"
    llm.mode = "normal"

    # 14) 全局笔记本隔离
    r = await p.maibook_create_book(title="麦麦的日记", scope="global", **kw)
    check(r["success"], "global 建书应成功", r)
    chat_books = p._list_books(p._workspace_dir("chat", kw["stream_id"]))
    global_books = p._list_books(p._workspace_dir("global", ""))
    check(len(chat_books) == 1 and len(global_books) == 1, "聊天与全局工作区应彼此隔离", (chat_books, global_books))

    print("ALL SMOKE TESTS PASSED ✅")
    print(f"  用过的写手任务名/模型名: {sorted(set(llm.models))}")
    print(f"  发送统计: {counters}")
    print(f"  数据目录: {tmp}")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except Exception:  # noqa: BLE001
        print("SMOKE TEST FAILED ❌", file=sys.stderr)
        traceback.print_exc()
        sys.exit(1)
