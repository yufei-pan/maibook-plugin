"""麦书 (MaiBook) —— 让麦麦创作并长期维护属于自己的书。

设计要点见同目录 README.md。核心思路：
- 每个聊天流是一个「笔记本」工作区（外加一个 ``__global__`` 全局工作区），其中可建立多本书。
- 每本书是一个目录：``book.toml`` 元信息、``instructions.md`` 创作说明、``manuscript/`` 正文、
  ``bible/`` 隐藏设定、``summaries/`` 滚动摘要、``journal/`` 问答与决定、``.history/`` 修订快照、
  ``compiled/`` 编译产物。
- 正文由「专职写手模型」（一次性 ``llm.generate`` 调用）生成，系统提示词里注入麦麦的人格与表达
  风格；麦麦本人担任主编，通过工具下达指令、审稿、拍板。
- 信息不足时「不动笔」：开写前有就绪门禁；写作中允许写手输出 ``===NEED_INFO===`` 区块，由程序把
  问题抛回给麦麦（绝不直接发给用户），由麦麦自行决定或再问用户。
"""

from __future__ import annotations

import asyncio
import hashlib
import html as html_lib
import os
import re
from collections.abc import Mapping
from datetime import datetime
from pathlib import Path
from typing import Any

try:  # Python 3.11+
    import tomllib
except ModuleNotFoundError:  # pragma: no cover - 旧版本回退
    import tomli as tomllib  # type: ignore

import tomli_w

from maibot_sdk import Command, Field, MaiBotPlugin, PluginConfigBase, Tool
from maibot_sdk.types import ToolParameterInfo, ToolParamType

CURRENT_CONFIG_VERSION = "0.2.0"

GLOBAL_WORKSPACE = "__global__"
STATUS_SETUP = "setup"
STATUS_READY = "ready"
NEED_INFO_MARKER = "===NEED_INFO==="

# 允许通过 maibook_set_meta 修改的元信息字段白名单
META_FIELDS: tuple[str, ...] = (
    "title", "subtitle", "author", "coauthor", "tagline", "tags", "license", "version",
    "premise", "genre", "tone", "pov", "length_target", "language",
)

# 开写前必须具备的元信息字段
REQUIRED_META: dict[str, str] = {
    "premise": "一句话核心设定 / 前提",
    "genre": "题材",
    "tone": "基调 / 风格",
    "pov": "叙事视角与时态",
    "length_target": "目标篇幅（如 短篇 / 12 章 / 8 万字）",
    "language": "写作语言",
}

# 开写前必须具备的隐藏设定文件（bible/<name>.md 非空）
REQUIRED_BIBLE: dict[str, str] = {
    "plot-outline": "分章大纲 / 主要情节走向",
    "characters": "主要人物（至少主角）",
    "world": "故事背景 / 设定",
}

# 单次工具返回内容的展示上限，避免塞爆 Planner 上下文
MAX_RETURN_CHARS = 8000

INSTRUCTIONS_TEMPLATE = """# 《{title}》创作说明

> 这是写手模型每次写作都会读到的「本书专属指令」。请用你自己的话把它写清楚，
> 写得越具体，成稿越贴近你想要的样子。可随时用 maibook_set_instructions 修改。

## 这是一本怎样的书
（题材、基调、想带给读者的感觉……）

## 写作风格与约束
（叙事人称与时态、语言风格、单章篇幅、内容尺度、禁忌……）

## 必须坚持的设定
（与 bible/ 中的世界观、人物、时间线保持一致；列出绝对不能写错的关键事实）
"""


# --------------------------------------------------------------------------- #
# 模块级工具函数
# --------------------------------------------------------------------------- #
def _now() -> str:
    """返回本地时区的 ISO 时间字符串（秒精度）。"""
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _ts() -> str:
    """返回用于历史快照文件名的紧凑时间戳。"""
    return datetime.now().strftime("%Y%m%d-%H%M%S")


def _safe_component(value: str) -> str:
    """把任意字符串清洗为安全的单段文件/目录名。"""
    sanitized = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", str(value).strip())
    return sanitized.strip(". ")


def _safe_stream_dir(stream_id: str) -> str:
    """把 stream_id 映射为安全且稳定的工作区目录名。"""
    safe = _safe_component(stream_id)
    if safe:
        return safe
    return "s_" + hashlib.sha256(str(stream_id).encode("utf-8")).hexdigest()[:16]


def _slugify(title: str) -> str:
    """把书名转为可作为书目录名/ID 的 slug（保留中英文与数字）。"""
    base = re.sub(r"[^\w一-鿿]+", "-", str(title).strip().lower(), flags=re.UNICODE).strip("-")
    if not base:
        base = "book-" + hashlib.sha256(str(title).encode("utf-8")).hexdigest()[:8]
    return base[:64]


def _atomic_write(path: Path, text: str) -> None:
    """原子写入文本文件（先写 .tmp 再 os.replace）。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


def _read_text(path: Path) -> str:
    """读取文本文件；文件不存在时返回空串。"""
    try:
        return path.read_text(encoding="utf-8")
    except (FileNotFoundError, NotADirectoryError):
        return ""


def _coalesce_text(primary: str, kwargs: Mapping[str, Any], *aliases: str) -> str:
    """取正文：优先用显式参数；为空时回退到 kwargs 里常见的同义键。

    模型常凭工具名臆测参数名（如对 maibook_set_outline 传 outline 而非 content），
    这里做输入归一化，让这类调用也能正确写入。注意：这不是兜底——若各处都为空，
    返回空串交由调用方显式报错，绝不静默写空。
    """
    if str(primary or "").strip():
        return str(primary)
    for alias in aliases:
        value = kwargs.get(alias, "")
        if str(value or "").strip():
            return str(value)
    return str(primary or "")


def _toml_safe(meta: Mapping[str, Any]) -> dict[str, Any]:
    """剔除 None，得到可被 tomli_w 序列化的字典。"""
    return {key: value for key, value in meta.items() if value is not None}


def _flatten_messages(messages: list[dict[str, Any]]) -> str:
    """把 system/user 消息列表压平为单段提示词（用于固定模型路径）。"""
    return "\n\n".join(str(item.get("content", "")).strip() for item in messages if str(item.get("content", "")).strip())


def _clip(text: str, limit: int = MAX_RETURN_CHARS) -> str:
    """裁剪过长文本用于工具返回展示。"""
    if len(text) <= limit:
        return text
    return text[:limit] + f"\n\n……（内容过长，仅展示前 {limit} 字）"


def _meta_brief(meta: Mapping[str, Any]) -> str:
    """把书的关键元信息渲染为要点列表。"""
    fields = [
        ("title", "书名"), ("subtitle", "副标题"), ("author", "作者"), ("coauthor", "联合作者"),
        ("tagline", "标语"), ("premise", "前提"), ("genre", "题材"), ("tone", "基调"),
        ("pov", "视角/时态"), ("length_target", "目标篇幅"), ("language", "语言"),
        ("version", "版本"), ("status", "状态"),
    ]
    lines: list[str] = []
    for key, label in fields:
        value = meta.get(key)
        if isinstance(value, list):
            value = "、".join(str(item) for item in value)
        if str(value or "").strip():
            lines.append(f"- {label}：{value}")
    tags = meta.get("tags") or []
    if isinstance(tags, list) and tags:
        lines.append("- 标签：" + "、".join(str(tag) for tag in tags))
    return "\n".join(lines)


def _extract_need_info(text: str) -> list[str] | None:
    """从写手输出中解析 NEED_INFO 区块；没有则返回 None。"""
    idx = text.find(NEED_INFO_MARKER)
    if idx == -1:
        return None
    tail = text[idx + len(NEED_INFO_MARKER):]
    questions = [
        re.sub(r"^[-*]\s*", "", line).strip()
        for line in tail.splitlines()
        if line.strip().startswith(("-", "*"))
    ]
    questions = [item for item in questions if item]
    return questions or ["（写手表示信息不足，但未给出具体问题）"]


def _target_chapter_count(meta: Mapping[str, Any]) -> int | None:
    """从 length_target 中解析「N 章」的目标章节数；解析不到返回 None。"""
    match = re.search(r"(\d+)\s*章", str(meta.get("length_target", "")))
    return int(match.group(1)) if match else None


# --------------------------------------------------------------------------- #
# 配置模型
# --------------------------------------------------------------------------- #
class PluginSection(PluginConfigBase):
    """基础设置。"""

    __ui_label__ = "基础设置"

    enabled: bool = Field(default=True, description="是否启用插件")
    config_version: str = Field(default=CURRENT_CONFIG_VERSION, description="配置版本号")
    allow_autopilot: bool = Field(default=False, description="后台自动续写总开关")


class WriterSection(PluginConfigBase):
    """写手模型设置。"""

    __ui_label__ = "写手模型"

    writer_model: str = Field(default="replyer", description="专职写手模型：任务名或 model_config 中的具体模型名")
    temperature: float = Field(default=0.8, description="写作温度")
    max_tokens: int = Field(default=8000, description="单次写作最大 token 数，<=0 表示交由宿主配置")
    timeout_seconds: int = Field(default=600, description="单次写作/修订调用的超时时间（秒），<=0 表示交由宿主默认值")
    style_supplement: str = Field(default="", description="追加到人格/表达风格之后的写作风格说明")


class ContextSection(PluginConfigBase):
    """写作上下文设置。"""

    __ui_label__ = "上下文"

    char_budget: int = Field(default=262144, description="写作上下文字符预算上限（含全部 bible 设定与参考稿）")
    include_rolling_summary: bool = Field(default=True, description="是否包含历史章节滚动摘要")
    summary_task: str = Field(default="utils", description="生成章节摘要使用的任务名")


class BackgroundSection(PluginConfigBase):
    """后台续写设置。"""

    __ui_label__ = "后台续写"

    interval_seconds: int = Field(default=1800, description="后台续写轮询间隔（秒），最小 60")
    max_books_per_tick: int = Field(default=1, description="每轮最多推进多少本书")


class StorageSection(PluginConfigBase):
    """存储设置。"""

    __ui_label__ = "存储"

    data_dir: str = Field(default="", description="数据根目录；留空表示插件目录下 data/")


class MaiBookConfig(PluginConfigBase):
    """麦书插件配置。"""

    plugin: PluginSection = Field(default_factory=PluginSection)
    writer: WriterSection = Field(default_factory=WriterSection)
    context: ContextSection = Field(default_factory=ContextSection)
    background: BackgroundSection = Field(default_factory=BackgroundSection)
    storage: StorageSection = Field(default_factory=StorageSection)


# --------------------------------------------------------------------------- #
# 插件主体
# --------------------------------------------------------------------------- #
class MaiBookPlugin(MaiBotPlugin):
    """麦书插件主体。"""

    config_model = MaiBookConfig

    def __init__(self) -> None:
        super().__init__()
        self._plugin_dir = Path(__file__).resolve().parent
        self._locks: dict[str, asyncio.Lock] = {}
        self._bg_task: asyncio.Task[None] | None = None
        self._bg_stop = asyncio.Event()

    # ------------------------------------------------------------------ #
    # 生命周期
    # ------------------------------------------------------------------ #
    async def on_load(self) -> None:
        """插件加载：按需启动后台续写循环。"""
        self._restart_background_loop()
        self.ctx.logger.info(
            "麦书插件已加载：写手模型=%s，后台续写总开关=%s",
            self.config.writer.writer_model,
            "开" if self.config.plugin.allow_autopilot else "关",
        )

    async def on_unload(self) -> None:
        """插件卸载：停止后台续写循环。"""
        self._stop_background_loop()
        self.ctx.logger.info("麦书插件已卸载")

    async def on_config_update(self, scope: str, config_data: dict[str, Any], version: str) -> None:
        """配置热更新：重启后台续写循环以应用新参数。"""
        if scope == "self":
            self._restart_background_loop()
            self.ctx.logger.info("麦书插件配置已更新: version=%s", version)

    # ------------------------------------------------------------------ #
    # 后台续写循环（参照 RSS 阅读器插件的模式）
    # ------------------------------------------------------------------ #
    def _restart_background_loop(self) -> None:
        """（重新）启动后台续写循环。"""
        self._stop_background_loop()
        self._bg_stop = asyncio.Event()
        self._bg_task = asyncio.create_task(self._background_loop())

    def _stop_background_loop(self) -> None:
        """停止后台续写循环。"""
        if self._bg_task is not None:
            self._bg_stop.set()
            self._bg_task.cancel()
            self._bg_task = None

    async def _background_loop(self) -> None:
        """周期性推进开启了 autopilot 的书。"""
        while not self._bg_stop.is_set():
            try:
                if self.config.plugin.allow_autopilot:
                    await self._background_tick()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - 后台循环需吞掉单轮异常以持续运行
                self.ctx.logger.error("麦书后台续写异常：%s", exc, exc_info=True)
            interval = max(60, int(self.config.background.interval_seconds))
            try:
                await asyncio.wait_for(self._bg_stop.wait(), timeout=interval)
                break
            except asyncio.TimeoutError:
                continue

    async def _background_tick(self) -> None:
        """扫描所有工作区，推进若干本开启了 autopilot 且已就绪的书。"""
        root = self._data_root()
        if not root.exists():
            return
        cap = max(1, int(self.config.background.max_books_per_tick))
        advanced = 0
        for workspace in sorted(root.iterdir()):
            if not workspace.is_dir():
                continue
            for book_dir in sorted(workspace.iterdir()):
                if advanced >= cap:
                    return
                if not book_dir.is_dir():
                    continue
                meta = self._read_meta(book_dir)
                if not meta or not bool(meta.get("autopilot")) or meta.get("status") != STATUS_READY:
                    continue
                advanced += 1
                await self._autopilot_advance(book_dir)

    async def _autopilot_advance(self, book_dir: Path) -> None:
        """后台推进单本书：写下一章，或在缺信息/已达目标时知会麦麦。"""
        async with self._lock(book_dir):
            meta = self._read_meta(book_dir)
            if not meta:
                return
            stream_id = str(meta.get("stream_id", ""))
            title = str(meta.get("title", ""))
            target = _target_chapter_count(meta)
            done = len(self._chapter_numbers(book_dir))
            if target is not None and done >= target:
                await self._surface_to_mai(
                    stream_id,
                    f"你正在写的《{title}》已写到目标章数（{done}/{target} 章）。要不要通读收尾、写后记或定稿？",
                    intent=f"《{title}》初稿章数已达标，请决定如何收尾",
                    reason="maibook_draft_complete",
                    book_title=title,
                )
                return
            result = await self._do_write_chapter(book_dir, meta, chapter_no="next", brief="", target_words=0)

        if result.get("status") == "need_info":
            await self._surface_to_mai(
                stream_id,
                f"你正在写的《{title}》卡在一个需要拍板的地方：\n- " + "\n- ".join(result.get("questions", []))
                + "\n（这是你自己的书，可凭设定与记忆自行决定；拿不准再问用户。定了用 maibook_record_answer 记入设定。）",
                intent=f"为《{title}》定夺若干设定问题并继续推进",
                reason="maibook_need_info",
                book_title=title,
            )
        elif result.get("success") and stream_id:
            await self._surface_to_mai(
                stream_id,
                f"你为《{title}》写完了第 {result.get('chapter_no')} 章（约 {result.get('word_count')} 字）。要不要审一审、和大家说说，或继续往下写？",
                intent=f"《{title}》新写好一章，请决定是否审阅或分享",
                reason="maibook_progress",
                book_title=title,
            )

    async def _surface_to_mai(
        self, stream_id: str, content: str, *, intent: str, reason: str, book_title: str
    ) -> None:
        """把进展/问题注入麦麦的上下文并唤醒她处理（绝不直接发给用户）。"""
        if not stream_id:
            return
        try:
            await self.ctx.maisaka.context.append(
                stream_id=stream_id,
                segments=[{"type": "text", "content": content}],
                source_kind="plugin:maibook",
            )
            await self.ctx.maisaka.proactive.trigger(
                stream_id=stream_id,
                intent=intent,
                reason=reason,
                metadata={"book": book_title},
            )
        except Exception as exc:  # noqa: BLE001
            self.ctx.logger.warning("麦书：向麦麦抛出内容失败：%s", exc)

    # ------------------------------------------------------------------ #
    # 路径与存储
    # ------------------------------------------------------------------ #
    def _data_root(self) -> Path:
        custom = (self.config.storage.data_dir or "").strip()
        if custom:
            return Path(custom).expanduser()
        return self._plugin_dir / "data"

    def _workspace_dir(self, scope: str, stream_id: str) -> Path:
        if scope == "global":
            return self._data_root() / GLOBAL_WORKSPACE
        return self._data_root() / _safe_stream_dir(stream_id)

    def _book_dir(self, scope: str, stream_id: str, slug: str) -> Path:
        return self._workspace_dir(scope, stream_id) / _safe_component(slug)

    def _lock(self, book_dir: Path) -> asyncio.Lock:
        key = str(book_dir)
        lock = self._locks.get(key)
        if lock is None:
            lock = asyncio.Lock()
            self._locks[key] = lock
        return lock

    def _meta_path(self, book_dir: Path) -> Path:
        return book_dir / "book.toml"

    def _read_meta(self, book_dir: Path) -> dict[str, Any]:
        raw = _read_text(self._meta_path(book_dir))
        if not raw:
            return {}
        try:
            return dict(tomllib.loads(raw))
        except Exception:  # noqa: BLE001 - 损坏的元信息视为空
            return {}

    def _write_meta(self, book_dir: Path, meta: Mapping[str, Any]) -> None:
        payload = dict(meta)
        payload["updated"] = _now()
        _atomic_write(self._meta_path(book_dir), tomli_w.dumps(_toml_safe(payload)))

    def _chapter_path(self, book_dir: Path, number: int) -> Path:
        return book_dir / "manuscript" / f"{number:02d}-chapter.md"

    def _chapter_numbers(self, book_dir: Path) -> list[int]:
        manuscript = book_dir / "manuscript"
        if not manuscript.exists():
            return []
        numbers: list[int] = []
        for child in manuscript.iterdir():
            match = re.fullmatch(r"(\d+)-chapter\.md", child.name)
            if match:
                numbers.append(int(match.group(1)))
        return sorted(numbers)

    def _resolve_book(
        self, kwargs: Mapping[str, Any], book: str, scope: str
    ) -> tuple[Path | None, dict[str, Any], dict[str, Any] | None]:
        """定位一本书；返回 (book_dir, meta, error)。error 非空表示失败。"""
        slug = _safe_component(book)
        if not slug:
            return None, {}, {"success": False, "content": "请提供书名/slug（book 参数）。"}
        stream_id = self._resolve_stream_id(kwargs)
        if scope != "global" and not stream_id:
            return None, {}, {"success": False, "content": "无法确定当前聊天流，请稍后重试。"}
        book_dir = self._book_dir(scope, stream_id, slug)
        meta = self._read_meta(book_dir)
        if not meta:
            return None, {}, {
                "success": False,
                "content": f"在{'全局' if scope == 'global' else '本聊天'}里没有找到《{book}》。可用 maibook_list_books 查看，或用 maibook_create_book 新建。",
            }
        return book_dir, meta, None

    @staticmethod
    def _resolve_stream_id(kwargs: Mapping[str, Any]) -> str:
        for key in ("stream_id", "session_id", "chat_id"):
            value = str(kwargs.get(key, "") or "").strip()
            if value:
                return value
        return ""

    @staticmethod
    def _norm_scope(scope: str) -> str:
        return "global" if str(scope or "").strip().lower() == "global" else "chat"

    # ------------------------------------------------------------------ #
    # 人格与写手
    # ------------------------------------------------------------------ #
    async def _persona(self) -> dict[str, str]:
        """读取宿主中麦麦的昵称、人格与表达风格。"""
        nickname = await self.ctx.config.get("bot.nickname", "麦麦") or "麦麦"
        personality = await self.ctx.config.get("personality.personality", "") or ""
        reply_style = await self.ctx.config.get("personality.reply_style", "") or ""
        return {"nickname": str(nickname), "personality": str(personality), "reply_style": str(reply_style)}

    def _writer_system_prompt(self, persona: Mapping[str, str], meta: Mapping[str, Any], instructions: str) -> str:
        """组装写手系统提示词：人格 + 表达风格 + 本书说明 + 写作纪律。"""
        nickname = persona.get("nickname") or "麦麦"
        lines = [
            f"你是{nickname}，正在创作完全属于你自己的书《{meta.get('title', '')}》。这是你本人的作品，"
            "请始终以你自己的人格、审美与表达风格来写，而不是写成千篇一律的范文。",
        ]
        if persona.get("personality"):
            lines.append(f"你的人格设定：{persona['personality']}")
        if persona.get("reply_style"):
            lines.append(f"你的表达风格：{persona['reply_style']}")
        supplement = (self.config.writer.style_supplement or "").strip()
        if supplement:
            lines.append(f"额外写作风格要求：{supplement}")
        if instructions.strip():
            lines.append("本书的创作说明（务必遵守）：\n" + instructions.strip())
        lines.append(
            "写作纪律：\n"
            "- 只输出正文本身，不要输出解释、标题编号之外的元信息、寒暄或与正文无关的话。\n"
            "- 严格保持与既有设定、人物、前文摘要的一致性，不要凭空发明与设定冲突的关键事实。\n"
            f"- 若缺少继续写作所必需、且你无法自行合理决定的关键信息：请在回复最前面单独输出一行 {NEED_INFO_MARKER}，"
            "随后用「- 」逐条列出需要确认的问题，然后停止，不要硬编。"
        )
        return "\n\n".join(lines)

    def _assemble_context(self, book_dir: Path, meta: Mapping[str, Any], task_text: str) -> str:
        """在字符预算内组装写作上下文（要点→大纲→人物→设定→全部自定义设定→决定→摘要→前文衔接）。

        这是麦麦本人的书：bible/ 下的全部设定文件（含麦麦通过 maibook_add_bible_note
        自行写入的任意自定义主题）都会作为参考资料带给写手，而不只是几个固定文件。
        """
        budget = max(2000, int(self.config.context.char_budget))
        bible_dir = book_dir / "bible"
        # 固定槽位：保证大纲/人物/世界等核心设定的顺序与标签稳定
        named_slots: list[tuple[str, str]] = [
            ("plot-outline", "【分章大纲】"),
            ("characters", "【人物】"),
            ("world", "【设定】"),
            ("stats", "【数值/其它设定】"),
        ]
        known_stems = {stem for stem, _ in named_slots}
        sections: list[tuple[str, str]] = [("【本书要点】", _meta_brief(meta))]
        for stem, label in named_slots:
            sections.append((label, _read_text(bible_dir / f"{stem}.md")))
        # 其余所有自定义 bible 文件（按文件名排序）一并作为参考资料带上
        if bible_dir.exists():
            for child in sorted(bible_dir.glob("*.md")):
                if child.stem in known_stems:
                    continue
                sections.append((f"【设定·{child.stem}】", _read_text(child)))
        sections.append(("【已确认的决定（视为正典）】", _read_text(book_dir / "journal" / "decisions.md")))
        if self.config.context.include_rolling_summary:
            summaries = self._collect_summaries(book_dir)
            if summaries:
                sections.append(("【此前章节摘要】", summaries))
        prev_tail = self._previous_chapter_tail(book_dir)
        if prev_tail:
            sections.append(("【上一章结尾（用于衔接）】", prev_tail))

        remaining = budget - len(task_text) - 64
        included: list[str] = []
        truncated: tuple[str, int, int] | None = None  # (标签, 完整长度, 实际保留)
        dropped: list[str] = []  # 因预算耗尽被整段省略的非空参考资料
        for label, body in sections:
            body = (body or "").strip()
            if not body:
                continue
            if remaining <= 0:
                dropped.append(label)
                continue
            block = f"{label}\n{body}"
            if len(block) <= remaining:
                included.append(block)
                remaining -= len(block) + 2
            else:
                included.append(block[:remaining] + "……（略）")
                truncated = (label, len(block), remaining)
                remaining = 0
        if truncated or dropped:
            # 不静默兜底：上下文超预算被裁剪时必须告警，便于排查“设定没带全”
            title = str(meta.get("title") or book_dir.name)
            detail: list[str] = []
            if truncated:
                t_label, full_len, kept = truncated
                detail.append(f"{t_label} 被截断（保留 {kept}/{full_len} 字）")
            if dropped:
                detail.append("整段省略：" + "、".join(dropped))
            self.ctx.logger.warning(
                "麦书：《%s》写作上下文超出字符预算（budget=%d，任务文本 %d 字），已裁剪部分参考资料——%s。"
                "如需带上全部 bible 设定/参考稿，请调大 context.char_budget。",
                title, budget, len(task_text), "；".join(detail),
            )
        return "\n\n".join(included + [task_text])

    def _collect_summaries(self, book_dir: Path) -> str:
        summaries_dir = book_dir / "summaries"
        if not summaries_dir.exists():
            return ""
        parts: list[str] = []
        for number in self._chapter_numbers(book_dir):
            text = _read_text(summaries_dir / f"{number:02d}-chapter.md").strip()
            if text:
                parts.append(f"第 {number} 章：{text}")
        return "\n".join(parts)

    def _previous_chapter_tail(self, book_dir: Path, tail_chars: int = 1200) -> str:
        numbers = self._chapter_numbers(book_dir)
        if not numbers:
            return ""
        text = _read_text(self._chapter_path(book_dir, numbers[-1])).strip()
        return text[-tail_chars:] if text else ""

    async def _writer_generate(self, system: str, user: str) -> dict[str, Any]:
        """调用写手模型：优先按任务名走 ctx.llm.generate，名称无法解析时回退固定模型。"""
        writer = self.config.writer
        model = (writer.writer_model or "").strip() or "replyer"
        temperature = writer.temperature
        max_tokens = writer.max_tokens if writer.max_tokens and writer.max_tokens > 0 else None
        timeout_ms = int(writer.timeout_seconds * 1000) if writer.timeout_seconds and writer.timeout_seconds > 0 else None
        messages = [{"role": "system", "content": system}, {"role": "user", "content": user}]

        try:
            result = await self.ctx.llm.generate(
                prompt=messages, model=model, temperature=temperature, max_tokens=max_tokens, timeout_ms=timeout_ms
            )
        except Exception as exc:  # noqa: BLE001
            result = {"success": False, "error": f"调用模型异常：{exc}"}

        if isinstance(result, dict) and result.get("success") and str(result.get("response", "")).strip():
            return result

        error_text = str((result or {}).get("error") or "")
        # 任务名未命中（典型错误含「未找到」）→ writer_model 可能是具体模型名，尝试固定该模型
        looks_like_unknown_task = (not result) or ("未找到" in error_text) or ("task" in error_text.lower())
        if looks_like_unknown_task:
            pinned = await self._writer_generate_pinned(_flatten_messages(messages), temperature, max_tokens)
            if pinned.get("success"):
                return pinned
            return {"success": False, "error": pinned.get("error") or error_text or "生成失败", "response": ""}
        return {"success": False, "error": error_text or "生成失败", "response": ""}

    async def _writer_generate_pinned(
        self, prompt_text: str, temperature: float | None, max_tokens: int | None
    ) -> dict[str, Any]:
        """固定到具体模型生成（参照 smart_segmentation / nai_pic 的做法，依赖宿主内部模块）。"""
        model_name = (self.config.writer.writer_model or "").strip()
        if not model_name:
            return {"success": False, "error": "未配置写手模型"}
        try:
            from src.config.model_configs import TaskConfig  # 宿主内部模块，跨版本可能变动
            from src.llm_models.utils_model import LLMOrchestrator
        except Exception as exc:  # noqa: BLE001
            return {
                "success": False,
                "error": f"当前宿主不支持固定具体模型（请将 writer_model 设为任务名，或把该模型加入某任务的 model_list）：{exc}",
            }

        class _PinnedOrchestrator(LLMOrchestrator):  # type: ignore[misc, valid-type]
            """覆盖任务配置解析，强制使用固定的单模型任务配置。"""

            def __init__(self, task_config: Any, request_type: str = "") -> None:
                self._pinned = task_config
                super().__init__(task_name="replyer", request_type=request_type)

            def _get_task_config_or_raise(self) -> Any:
                return self._pinned

            def _refresh_task_config(self) -> Any:
                return self._pinned

        try:
            task_config = TaskConfig(
                model_list=[model_name],
                temperature=float(temperature) if temperature is not None else 0.8,
                max_tokens=int(max_tokens) if max_tokens else 8000,
                selection_strategy="random",
                slow_threshold=30.0,
            )
            orchestrator = _PinnedOrchestrator(task_config, request_type="plugin.maibook")
            result = await orchestrator.generate_response_async(
                prompt=prompt_text,
                temperature=float(temperature) if temperature is not None else None,
                max_tokens=int(max_tokens) if max_tokens else None,
            )
            return {
                "success": True,
                "response": str(getattr(result, "response", "") or ""),
                "reasoning": str(getattr(result, "reasoning", "") or ""),
                "model": str(getattr(result, "model_name", model_name) or model_name),
            }
        except Exception as exc:  # noqa: BLE001
            return {"success": False, "error": f"固定模型 `{model_name}` 生成失败：{exc}"}

    async def _summarize_chapter(self, title: str, chapter_no: int, chapter_text: str) -> str:
        """为单章生成滚动摘要（用速度快的小模型任务）。失败时回退为开头截断并记日志。"""
        task = (self.config.context.summary_task or "utils").strip() or "utils"
        prompt = [
            {
                "role": "system",
                "content": "你是严谨的小说编辑助手。请用简洁中文为给定章节写要点摘要，覆盖：关键情节推进、"
                "出场人物及其状态变化、新引入的设定或伏笔、与后续相关的悬念。只输出摘要本身。",
            },
            {"role": "user", "content": f"《{title}》第 {chapter_no} 章正文：\n\n{chapter_text}"},
        ]
        writer = self.config.writer
        timeout_ms = int(writer.timeout_seconds * 1000) if writer.timeout_seconds and writer.timeout_seconds > 0 else None
        try:
            result = await self.ctx.llm.generate(
                prompt=prompt, model=task, temperature=0.3, max_tokens=600, timeout_ms=timeout_ms
            )
            if isinstance(result, dict) and result.get("success") and str(result.get("response", "")).strip():
                return str(result["response"]).strip()
            self.ctx.logger.warning("麦书：章节摘要生成失败，回退为截断：%s", (result or {}).get("error"))
        except Exception as exc:  # noqa: BLE001
            self.ctx.logger.warning("麦书：章节摘要调用异常，回退为截断：%s", exc)
        return chapter_text[:400]

    # ------------------------------------------------------------------ #
    # 写作核心（工具与后台共用）
    # ------------------------------------------------------------------ #
    async def _do_write_chapter(
        self, book_dir: Path, meta: Mapping[str, Any], *, chapter_no: Any, brief: str, target_words: int, draft: str = ""
    ) -> dict[str, Any]:
        """实际写一章；返回结构化结果（success / need_info / setup-not-ready）。"""
        if meta.get("status") != STATUS_READY:
            missing = self._readiness(book_dir, meta)
            return {
                "success": False,
                "status": STATUS_SETUP,
                "missing": missing,
                "content": "这本书还没准备好开写。请先补全以下必要信息（你可以自己拍板，拿不准再问用户），"
                "补全后用 maibook_check_ready 标记就绪：\n- " + "\n- ".join(missing),
            }

        if isinstance(chapter_no, str) and chapter_no.strip().lower() in ("", "next"):
            number = (self._chapter_numbers(book_dir)[-1] + 1) if self._chapter_numbers(book_dir) else 1
        else:
            try:
                number = int(chapter_no)
            except (TypeError, ValueError):
                number = (self._chapter_numbers(book_dir)[-1] + 1) if self._chapter_numbers(book_dir) else 1
        number = max(1, number)

        title = str(meta.get("title", ""))
        persona = await self._persona()
        instructions = _read_text(book_dir / "instructions.md")
        system = self._writer_system_prompt(persona, meta, instructions)

        task_lines = [f"请创作《{title}》的第 {number} 章。"]
        if brief.strip():
            task_lines.append(f"本章特别要求：{brief.strip()}")
        if target_words and target_words > 0:
            task_lines.append(f"目标字数约 {target_words} 字。")
        if draft.strip():
            task_lines.append(
                "【麦麦提供的本章参考稿】\n"
                "以下是麦麦本人为本章写好的参考稿，请把它当作本章的基准：保留其情节走向、关键设定与已写明的内容，"
                "在此基础上完成、润色、扩写为最终正文，不要另起炉灶或与之冲突。\n\n" + draft.strip()
            )
        task_lines.append("请直接开始写本章正文（可用「## 小节标题」分节）。")
        user = self._assemble_context(book_dir, meta, "\n".join(task_lines))

        generated = await self._writer_generate(system, user)
        if not generated.get("success"):
            return {"success": False, "content": f"写手生成失败：{generated.get('error', '未知错误')}"}

        response_text = str(generated.get("response", "")).strip()
        need_info = _extract_need_info(response_text)
        if need_info is not None:
            self._append_questions(book_dir, number, need_info)
            return {"success": True, "status": "need_info", "chapter_no": number, "questions": need_info}

        chapter_path = self._chapter_path(book_dir, number)
        _atomic_write(chapter_path, f"# 第 {number} 章\n\n{response_text}\n")
        summary = await self._summarize_chapter(title, number, response_text)
        _atomic_write(book_dir / "summaries" / f"{number:02d}-chapter.md", summary)

        updated = dict(meta)
        updated["status"] = STATUS_READY
        self._write_meta(book_dir, updated)

        word_count = len(re.sub(r"\s+", "", response_text))
        preview = response_text[:500] + ("……" if len(response_text) > 500 else "")
        return {
            "success": True,
            "status": "written",
            "chapter_no": number,
            "word_count": word_count,
            "path": str(chapter_path),
            "preview": preview,
            "content": f"已写好《{title}》第 {number} 章，约 {word_count} 字。预览：\n\n{preview}",
        }

    def _readiness(self, book_dir: Path, meta: Mapping[str, Any]) -> list[str]:
        """返回开写前仍缺失的必要信息清单。"""
        missing: list[str] = []
        for field, label in REQUIRED_META.items():
            if not str(meta.get(field, "")).strip():
                missing.append(f"{label}（book.toml: {field}）")
        for name, label in REQUIRED_BIBLE.items():
            if not _read_text(book_dir / "bible" / f"{name}.md").strip():
                missing.append(f"{label}（bible/{name}.md）")
        return missing

    def _append_questions(self, book_dir: Path, chapter_no: int, questions: list[str]) -> None:
        path = book_dir / "journal" / "questions.md"
        existing = _read_text(path)
        block = f"## {_now()} · 第 {chapter_no} 章\n" + "\n".join(f"- {item}" for item in questions) + "\n\n"
        _atomic_write(path, existing + block)

    # ------------------------------------------------------------------ #
    # 工具：书目管理
    # ------------------------------------------------------------------ #
    @Tool(
        "maibook_list_books",
        brief_description="列出当前聊天（及全局）笔记本里的所有书及其状态。",
        parameters=[
            ToolParameterInfo(
                name="scope", param_type=ToolParamType.STRING, required=False, default="chat",
                description="范围：chat=本聊天（默认），global=全局自留笔记，all=两者",
                enum_values=["chat", "global", "all"],
            ),
        ],
    )
    async def maibook_list_books(self, scope: str = "chat", **kwargs: Any) -> dict[str, Any]:
        scope = str(scope or "chat").strip().lower()
        stream_id = self._resolve_stream_id(kwargs)
        targets: list[tuple[str, Path]] = []
        if scope in ("chat", "all") and stream_id:
            targets.append(("本聊天", self._workspace_dir("chat", stream_id)))
        if scope in ("global", "all"):
            targets.append(("全局", self._workspace_dir("global", "")))
        if not targets:
            return {"success": False, "content": "无法确定当前聊天流，请稍后重试。"}

        lines: list[str] = []
        total = 0
        for label, workspace in targets:
            books = self._list_books(workspace)
            lines.append(f"## {label}（{len(books)} 本）")
            for item in books:
                total += 1
                lines.append(
                    f"- 《{item['title']}》[slug={item['slug']}] 状态={item['status']}"
                    f" 章数={item['chapters']} 自动续写={'开' if item['autopilot'] else '关'}"
                )
            if not books:
                lines.append("（暂无）")
        return {"success": True, "count": total, "content": "\n".join(lines)}

    def _list_books(self, workspace: Path) -> list[dict[str, Any]]:
        result: list[dict[str, Any]] = []
        if not workspace.exists():
            return result
        for child in sorted(workspace.iterdir()):
            if not child.is_dir():
                continue
            meta = self._read_meta(child)
            if not meta:
                continue
            result.append({
                "slug": child.name,
                "title": str(meta.get("title", child.name)),
                "status": str(meta.get("status", STATUS_SETUP)),
                "autopilot": bool(meta.get("autopilot")),
                "chapters": len(self._chapter_numbers(child)),
            })
        return result

    @Tool(
        "maibook_create_book",
        brief_description="新建一本书（初始为 setup 状态，需补全要素后才能开写）。",
        parameters=[
            ToolParameterInfo(name="title", param_type=ToolParamType.STRING, required=True, description="书名（同时作为 ID 来源）"),
            ToolParameterInfo(name="premise", param_type=ToolParamType.STRING, required=False, default="", description="一句话核心设定/前提（可选，之后也能补）"),
            ToolParameterInfo(name="genre", param_type=ToolParamType.STRING, required=False, default="", description="题材（可选）"),
            ToolParameterInfo(name="autopilot", param_type=ToolParamType.BOOLEAN, required=False, default=False, description="是否开启后台自动续写（就绪后生效，受总开关约束）"),
            ToolParameterInfo(
                name="scope", param_type=ToolParamType.STRING, required=False, default="chat",
                description="chat=本聊天（默认），global=麦麦的全局自留笔记", enum_values=["chat", "global"],
            ),
        ],
    )
    async def maibook_create_book(
        self, title: str = "", premise: str = "", genre: str = "", autopilot: bool = False,
        scope: str = "chat", **kwargs: Any,
    ) -> dict[str, Any]:
        title = str(title or "").strip()
        if not title:
            return {"success": False, "content": "请提供书名（title）。"}
        scope = self._norm_scope(scope)
        stream_id = self._resolve_stream_id(kwargs)
        if scope != "global" and not stream_id:
            return {"success": False, "content": "无法确定当前聊天流，请稍后重试。"}

        slug = _slugify(title)
        book_dir = self._book_dir(scope, stream_id, slug)
        if self._meta_path(book_dir).exists():
            return {"success": False, "content": f"《{title}》[slug={slug}] 已存在。换个书名，或直接用现有的这本。"}

        persona = await self._persona()
        # 建好目录骨架
        for sub in ("manuscript", "bible", "summaries", "journal", "compiled"):
            (book_dir / sub).mkdir(parents=True, exist_ok=True)
        _atomic_write(book_dir / "instructions.md", INSTRUCTIONS_TEMPLATE.format(title=title))
        _atomic_write(book_dir / "journal" / "questions.md", "")
        _atomic_write(book_dir / "journal" / "decisions.md", "")

        meta: dict[str, Any] = {
            "title": title,
            "slug": slug,
            "author": persona.get("nickname", "麦麦"),
            "version": "0.1.0",
            "status": STATUS_SETUP,
            "autopilot": bool(autopilot),
            "scope": scope,
            "stream_id": stream_id,
            "created": _now(),
            "premise": str(premise or "").strip(),
            "genre": str(genre or "").strip(),
        }
        self._write_meta(book_dir, meta)
        missing = self._readiness(book_dir, meta)
        return {
            "success": True,
            "slug": slug,
            "content": f"已建立《{title}》[slug={slug}]，作者署名「{meta['author']}」，当前为 setup（未就绪）。\n"
            "开写前还需补全：\n- " + "\n- ".join(missing)
            + "\n\n你可以自己拟定这些要素（拿不准再问用户）：用 maibook_set_meta 填要点、maibook_set_outline 写大纲、"
            "maibook_add_bible_note 写人物/设定，然后 maibook_check_ready 标记就绪。",
        }

    @Tool(
        "maibook_set_meta",
        brief_description="批量修改一本书的元信息（书名/副标题/作者/标语/题材/基调/视角/篇幅/语言/标签等）。",
        parameters=[
            ToolParameterInfo(name="book", param_type=ToolParamType.STRING, required=True, description="书的 slug"),
            ToolParameterInfo(
                name="updates", param_type=ToolParamType.OBJECT, required=True,
                description="要更新的字段对象，键取自：" + "、".join(META_FIELDS) + "（tags 传字符串数组）",
            ),
            ToolParameterInfo(name="scope", param_type=ToolParamType.STRING, required=False, default="chat", enum_values=["chat", "global"], description="范围"),
        ],
    )
    async def maibook_set_meta(self, book: str = "", updates: Any = None, scope: str = "chat", **kwargs: Any) -> dict[str, Any]:
        scope = self._norm_scope(scope)
        book_dir, _, error = self._resolve_book(kwargs, book, scope)
        if error:
            return error
        # 容忍模型把字段直接平铺在顶层（而非塞进 updates 对象）：从 kwargs 里捞已知字段兜上。
        ignored: list[str] = []
        if isinstance(updates, Mapping):
            raw_updates = dict(updates)
        else:
            raw_updates = {key: kwargs[key] for key in META_FIELDS if key in kwargs}
            if not raw_updates:
                return {"success": False, "content": "updates 需要是一个字段对象（键取自：" + "、".join(META_FIELDS) + "）。"}
        applied: list[str] = []
        normalized: dict[str, Any] = {}
        for key, value in raw_updates.items():
            if key not in META_FIELDS:
                ignored.append(str(key))
                continue
            if key == "tags":
                value = [str(item).strip() for item in value if str(item).strip()] if isinstance(value, list) else [
                    part.strip() for part in str(value).split(",") if part.strip()
                ]
            normalized[key] = value
            applied.append(key)
        if not applied:
            return {"success": False, "content": "没有可更新的字段。可用字段：" + "、".join(META_FIELDS)}
        async with self._lock(book_dir):
            new_meta = dict(self._read_meta(book_dir))
            new_meta.update(normalized)
            self._write_meta(book_dir, new_meta)
        note = f"（忽略了不认识的字段：{('、'.join(ignored))}；可用字段：{'、'.join(META_FIELDS)}）" if ignored else ""
        return {"success": True, "content": f"已更新《{new_meta.get('title', book)}》的字段：{('、'.join(applied))}。{note}"}

    @Tool(
        "maibook_set_instructions",
        brief_description="设置/覆盖本书的创作说明 instructions.md（写手每次写作都会读到）。",
        parameters=[
            ToolParameterInfo(name="book", param_type=ToolParamType.STRING, required=True, description="书的 slug"),
            ToolParameterInfo(name="content", param_type=ToolParamType.STRING, required=True, description="创作说明全文（Markdown）"),
            ToolParameterInfo(name="scope", param_type=ToolParamType.STRING, required=False, default="chat", enum_values=["chat", "global"], description="范围"),
        ],
    )
    async def maibook_set_instructions(self, book: str = "", content: str = "", scope: str = "chat", **kwargs: Any) -> dict[str, Any]:
        scope = self._norm_scope(scope)
        book_dir, meta, error = self._resolve_book(kwargs, book, scope)
        if error:
            return error
        content = _coalesce_text(content, kwargs, "instructions", "text", "body", "markdown")
        body = str(content or "").strip()
        if not body:
            return {
                "success": False,
                "content": f"《{meta.get('title', book)}》的创作说明内容为空，未做任何改动。"
                "请把正文放进 content 参数后重试（已保留原有内容）。",
            }
        async with self._lock(book_dir):
            _atomic_write(book_dir / "instructions.md", body + "\n")
        return {"success": True, "content": f"已更新《{meta.get('title', book)}》的创作说明。"}

    @Tool(
        "maibook_set_outline",
        brief_description="写入/更新本书的分章大纲 bible/plot-outline.md（情节骨架）。",
        parameters=[
            ToolParameterInfo(name="book", param_type=ToolParamType.STRING, required=True, description="书的 slug"),
            ToolParameterInfo(name="content", param_type=ToolParamType.STRING, required=True, description="大纲全文（Markdown）"),
            ToolParameterInfo(name="mode", param_type=ToolParamType.STRING, required=False, default="replace", enum_values=["replace", "append"], description="覆盖或追加"),
            ToolParameterInfo(name="scope", param_type=ToolParamType.STRING, required=False, default="chat", enum_values=["chat", "global"], description="范围"),
        ],
    )
    async def maibook_set_outline(self, book: str = "", content: str = "", mode: str = "replace", scope: str = "chat", **kwargs: Any) -> dict[str, Any]:
        content = _coalesce_text(content, kwargs, "outline", "text", "body", "markdown")
        return await self._write_bible(book, "plot-outline", content, mode, scope, kwargs, label="分章大纲")

    @Tool(
        "maibook_add_bible_note",
        brief_description="写入/追加隐藏设定 bible/<topic>.md（人物、世界、数值、时间线等，不会出现在成书里）。",
        parameters=[
            ToolParameterInfo(
                name="book", param_type=ToolParamType.STRING, required=True, description="书的 slug"),
            ToolParameterInfo(
                name="topic", param_type=ToolParamType.STRING, required=True,
                description="设定主题，如 characters / world / stats / timeline 等（会作为 bible/<topic>.md）"),
            ToolParameterInfo(name="content", param_type=ToolParamType.STRING, required=True, description="设定内容（Markdown）"),
            ToolParameterInfo(name="mode", param_type=ToolParamType.STRING, required=False, default="append", enum_values=["append", "replace"], description="追加或覆盖"),
            ToolParameterInfo(name="scope", param_type=ToolParamType.STRING, required=False, default="chat", enum_values=["chat", "global"], description="范围"),
        ],
    )
    async def maibook_add_bible_note(self, book: str = "", topic: str = "", content: str = "", mode: str = "append", scope: str = "chat", **kwargs: Any) -> dict[str, Any]:
        topic_name = _safe_component(topic).lower().replace(" ", "-")
        if not topic_name:
            return {"success": False, "content": "请提供设定主题 topic。"}
        content = _coalesce_text(content, kwargs, "text", "note", "body", "markdown")
        return await self._write_bible(book, topic_name, content, mode, scope, kwargs, label=f"设定「{topic}」")

    async def _write_bible(
        self, book: str, name: str, content: str, mode: str, scope: str, kwargs: Mapping[str, Any], *, label: str
    ) -> dict[str, Any]:
        scope = self._norm_scope(scope)
        book_dir, meta, error = self._resolve_book(kwargs, book, scope)
        if error:
            return error
        path = book_dir / "bible" / f"{name}.md"
        body = str(content or "").strip()
        if not body:
            return {
                "success": False,
                "content": f"《{meta.get('title', book)}》的{label}内容为空，未做任何改动。"
                "请把正文放进 content 参数后重试（已保留原有内容）。",
            }
        async with self._lock(book_dir):
            if str(mode).strip().lower() == "append":
                existing = _read_text(path)
                merged = (existing.rstrip() + "\n\n" + body).strip() if existing.strip() else body
                _atomic_write(path, merged + "\n")
            else:
                _atomic_write(path, body + "\n")
        return {"success": True, "content": f"已更新《{meta.get('title', book)}》的{label}。"}

    # ------------------------------------------------------------------ #
    # 工具：阅读与就绪
    # ------------------------------------------------------------------ #
    @Tool(
        "maibook_read",
        brief_description="读取一本书的内容：概览/元信息/大纲/设定/某章/正文清单/摘要/问题/决定。",
        parameters=[
            ToolParameterInfo(name="book", param_type=ToolParamType.STRING, required=True, description="书的 slug"),
            ToolParameterInfo(
                name="target", param_type=ToolParamType.STRING, required=False, default="all",
                description="读取目标：all/metadata/instructions/outline/bible/bible:<名>/manuscript/chapter:<N>/summaries/questions/decisions",
            ),
            ToolParameterInfo(name="scope", param_type=ToolParamType.STRING, required=False, default="chat", enum_values=["chat", "global"], description="范围"),
        ],
    )
    async def maibook_read(self, book: str = "", target: str = "all", scope: str = "chat", **kwargs: Any) -> dict[str, Any]:
        scope = self._norm_scope(scope)
        book_dir, meta, error = self._resolve_book(kwargs, book, scope)
        if error:
            return error
        target = str(target or "all").strip().lower()
        title = meta.get("title", book)

        if target in ("all", "overview"):
            chapters = self._chapter_numbers(book_dir)
            missing = self._readiness(book_dir, meta)
            body = (
                f"# 《{title}》概览\n\n{_meta_brief(meta)}\n\n"
                f"- 正文章数：{len(chapters)}（{chapters or '无'}）\n"
                f"- 就绪情况：{'已就绪' if not missing else '未就绪，缺：' + '、'.join(missing)}"
            )
            return {"success": True, "content": _clip(body)}
        if target == "metadata":
            return {"success": True, "content": _clip(tomli_w.dumps(_toml_safe(meta)))}
        if target == "instructions":
            return {"success": True, "content": _clip(_read_text(book_dir / "instructions.md") or "（暂无创作说明）")}
        if target == "outline":
            return {"success": True, "content": _clip(_read_text(book_dir / "bible" / "plot-outline.md") or "（暂无大纲）")}
        if target == "questions":
            return {"success": True, "content": _clip(_read_text(book_dir / "journal" / "questions.md") or "（暂无待答问题）")}
        if target == "decisions":
            return {"success": True, "content": _clip(_read_text(book_dir / "journal" / "decisions.md") or "（暂无已记录的决定）")}
        if target == "summaries":
            return {"success": True, "content": _clip(self._collect_summaries(book_dir) or "（暂无摘要）")}
        if target == "manuscript":
            chapters = self._chapter_numbers(book_dir)
            lines = [f"《{title}》正文章节（{len(chapters)} 章）："]
            for number in chapters:
                text = _read_text(self._chapter_path(book_dir, number))
                char_count = len(re.sub(r"\s+", "", text))
                lines.append(f"- 第 {number} 章：约 {char_count} 字")
            return {"success": True, "content": "\n".join(lines)}
        if target.startswith("bible:"):
            name = _safe_component(target.split(":", 1)[1]).lower()
            return {"success": True, "content": _clip(_read_text(book_dir / "bible" / f"{name}.md") or f"（bible/{name}.md 为空）")}
        if target == "bible":
            bible_dir = book_dir / "bible"
            parts: list[str] = []
            if bible_dir.exists():
                for child in sorted(bible_dir.glob("*.md")):
                    parts.append(f"## {child.stem}\n{_read_text(child).strip()}")
            return {"success": True, "content": _clip("\n\n".join(parts) or "（暂无设定）")}
        if target.startswith("chapter:"):
            try:
                number = int(target.split(":", 1)[1])
            except (TypeError, ValueError):
                return {"success": False, "content": "章节号无效，应形如 chapter:3。"}
            text = _read_text(self._chapter_path(book_dir, number))
            if not text:
                return {"success": False, "content": f"第 {number} 章还没有内容。"}
            return {"success": True, "content": _clip(text)}
        return {"success": False, "content": f"无法识别的 target：{target}"}

    @Tool(
        "maibook_check_ready",
        brief_description="检查一本书是否具备开写所需的全部要素；齐全则标记为 ready。",
        parameters=[
            ToolParameterInfo(name="book", param_type=ToolParamType.STRING, required=True, description="书的 slug"),
            ToolParameterInfo(name="scope", param_type=ToolParamType.STRING, required=False, default="chat", enum_values=["chat", "global"], description="范围"),
        ],
    )
    async def maibook_check_ready(self, book: str = "", scope: str = "chat", **kwargs: Any) -> dict[str, Any]:
        scope = self._norm_scope(scope)
        book_dir, meta, error = self._resolve_book(kwargs, book, scope)
        if error:
            return error
        missing = self._readiness(book_dir, meta)
        if missing:
            return {
                "success": True, "ready": False, "missing": missing,
                "content": f"《{meta.get('title', book)}》还差这些才能开写：\n- " + "\n- ".join(missing),
            }
        async with self._lock(book_dir):
            new_meta = dict(self._read_meta(book_dir))
            new_meta["status"] = STATUS_READY
            self._write_meta(book_dir, new_meta)
        return {"success": True, "ready": True, "content": f"《{meta.get('title', book)}》要素齐全，已标记为 ready，可以开写了。"}

    # ------------------------------------------------------------------ #
    # 工具：写作与修订
    # ------------------------------------------------------------------ #
    @Tool(
        "maibook_write_chapter",
        brief_description="为一本「已就绪」的书写一章（缺信息会回报问题而不硬写）。",
        parameters=[
            ToolParameterInfo(name="book", param_type=ToolParamType.STRING, required=True, description="书的 slug"),
            ToolParameterInfo(name="chapter", param_type=ToolParamType.STRING, required=False, default="next", description="章节号；留空或 next 表示续写下一章"),
            ToolParameterInfo(name="brief", param_type=ToolParamType.STRING, required=False, default="", description="本章的特别要求/要点（可选）"),
            ToolParameterInfo(name="content", param_type=ToolParamType.STRING, required=False, default="", description="麦麦为本章提供的参考稿/草稿正文（可选）；写手会以它为基准来完成本章，保留其情节与关键设定"),
            ToolParameterInfo(name="target_words", param_type=ToolParamType.INTEGER, required=False, default=0, description="目标字数（可选，0 表示不限）"),
            ToolParameterInfo(name="scope", param_type=ToolParamType.STRING, required=False, default="chat", enum_values=["chat", "global"], description="范围"),
        ],
    )
    async def maibook_write_chapter(
        self, book: str = "", chapter: str = "next", brief: str = "", content: str = "", target_words: int = 0, scope: str = "chat", **kwargs: Any
    ) -> dict[str, Any]:
        scope = self._norm_scope(scope)
        book_dir, meta, error = self._resolve_book(kwargs, book, scope)
        if error:
            return error
        async with self._lock(book_dir):
            meta = self._read_meta(book_dir)
            result = await self._do_write_chapter(
                book_dir, meta, chapter_no=chapter, brief=brief, draft=content, target_words=int(target_words or 0)
            )
        if result.get("status") == "need_info":
            result["content"] = (
                f"《{meta.get('title', book)}》第 {result.get('chapter_no')} 章先别急着写——写手需要你先拍板几件事"
                "（这是你自己的书，可凭设定与记忆自行决定，拿不准再问用户；定了用 maibook_record_answer 记入设定）：\n- "
                + "\n- ".join(result.get("questions", []))
            )
        return result

    @Tool(
        "maibook_revise",
        brief_description="修订已写好的某一章（整章重写，或仅重写指定小节）；修订前自动快照到 .history。",
        parameters=[
            ToolParameterInfo(name="book", param_type=ToolParamType.STRING, required=True, description="书的 slug"),
            ToolParameterInfo(name="chapter", param_type=ToolParamType.INTEGER, required=True, description="要修订的章节号"),
            ToolParameterInfo(name="instruction", param_type=ToolParamType.STRING, required=True, description="修订要求（怎么改）"),
            ToolParameterInfo(name="target_section", param_type=ToolParamType.STRING, required=False, default="", description="只改某个「## 小节标题」时填该标题；留空则整章重写"),
            ToolParameterInfo(name="scope", param_type=ToolParamType.STRING, required=False, default="chat", enum_values=["chat", "global"], description="范围"),
        ],
    )
    async def maibook_revise(
        self, book: str = "", chapter: int = 0, instruction: str = "", target_section: str = "", scope: str = "chat", **kwargs: Any
    ) -> dict[str, Any]:
        scope = self._norm_scope(scope)
        book_dir, meta, error = self._resolve_book(kwargs, book, scope)
        if error:
            return error
        try:
            number = int(chapter)
        except (TypeError, ValueError):
            return {"success": False, "content": "章节号无效。"}
        chapter_path = self._chapter_path(book_dir, number)
        original = _read_text(chapter_path)
        if not original.strip():
            return {"success": False, "content": f"第 {number} 章还没有内容，无法修订（可用 maibook_write_chapter 先写）。"}
        if not str(instruction or "").strip():
            return {"success": False, "content": "请说明怎么改（instruction）。"}

        persona = await self._persona()
        instructions = _read_text(book_dir / "instructions.md")
        system = self._writer_system_prompt(persona, meta, instructions)
        section = str(target_section or "").strip()

        if section:
            extracted = self._extract_section(original, section)
            if extracted is None:
                return {"success": False, "content": f"在第 {number} 章里找不到小节「## {section}」。"}
            task = (
                f"下面是《{meta.get('title','')}》第 {number} 章中「## {section}」小节的原文，请按要求重写"
                f"该小节（只输出重写后的该小节正文，从「## {section}」开始）。\n\n修订要求：{instruction}\n\n原文：\n{extracted}"
            )
        else:
            task = (
                f"下面是《{meta.get('title','')}》第 {number} 章的原文，请按要求整章重写（只输出重写后的正文）。\n\n"
                f"修订要求：{instruction}\n\n原文：\n{original}"
            )
        user = self._assemble_context(book_dir, meta, task)

        generated = await self._writer_generate(system, user)
        if not generated.get("success"):
            return {"success": False, "content": f"修订生成失败：{generated.get('error', '未知错误')}"}
        new_text = str(generated.get("response", "")).strip()
        if _extract_need_info(new_text) is not None:
            return {"success": False, "content": "写手认为信息不足，建议先用 maibook_write_chapter 流程澄清问题后再修订。"}

        async with self._lock(book_dir):
            # 快照
            _atomic_write(book_dir / ".history" / f"{number:02d}-chapter.{_ts()}.md", original)
            if section:
                merged = self._replace_section(original, section, new_text)
            else:
                merged = new_text if new_text.startswith("#") else f"# 第 {number} 章\n\n{new_text}\n"
            _atomic_write(chapter_path, merged)
            summary = await self._summarize_chapter(str(meta.get("title", "")), number, merged)
            _atomic_write(book_dir / "summaries" / f"{number:02d}-chapter.md", summary)

        return {"success": True, "content": f"已修订《{meta.get('title', book)}》第 {number} 章（原稿已快照到 .history）。"}

    @staticmethod
    def _extract_section(text: str, section_title: str) -> str | None:
        pattern = re.compile(rf"(^##\s+{re.escape(section_title)}\s*$.*?)(?=^##\s+|\Z)", re.MULTILINE | re.DOTALL)
        match = pattern.search(text)
        return match.group(1).strip() if match else None

    @staticmethod
    def _replace_section(text: str, section_title: str, new_block: str) -> str:
        pattern = re.compile(rf"(^##\s+{re.escape(section_title)}\s*$.*?)(?=^##\s+|\Z)", re.MULTILINE | re.DOTALL)
        replacement = new_block.strip() + "\n\n"
        return pattern.sub(lambda _m: replacement, text, count=1)

    # ------------------------------------------------------------------ #
    # 工具：问答闭环
    # ------------------------------------------------------------------ #
    @Tool(
        "maibook_open_questions",
        brief_description="查看一本书当前待拍板/待澄清的问题。",
        parameters=[
            ToolParameterInfo(name="book", param_type=ToolParamType.STRING, required=True, description="书的 slug"),
            ToolParameterInfo(name="scope", param_type=ToolParamType.STRING, required=False, default="chat", enum_values=["chat", "global"], description="范围"),
        ],
    )
    async def maibook_open_questions(self, book: str = "", scope: str = "chat", **kwargs: Any) -> dict[str, Any]:
        scope = self._norm_scope(scope)
        book_dir, meta, error = self._resolve_book(kwargs, book, scope)
        if error:
            return error
        content = _read_text(book_dir / "journal" / "questions.md").strip()
        return {"success": True, "content": _clip(content or "（暂无待答问题）")}

    @Tool(
        "maibook_record_answer",
        brief_description="把一个已拍板的决定写入正典（decisions.md，并按需归入某个 bible 设定），随后清空待答问题。",
        parameters=[
            ToolParameterInfo(name="book", param_type=ToolParamType.STRING, required=True, description="书的 slug"),
            ToolParameterInfo(name="topic", param_type=ToolParamType.STRING, required=True, description="这个决定的主题/小标题"),
            ToolParameterInfo(name="answer", param_type=ToolParamType.STRING, required=True, description="拍板的内容/决定"),
            ToolParameterInfo(name="bible_topic", param_type=ToolParamType.STRING, required=False, default="", description="若该决定也属于某项设定，填 bible 主题名（如 characters/world），会一并追加"),
            ToolParameterInfo(name="clear_questions", param_type=ToolParamType.BOOLEAN, required=False, default=True, description="是否清空该书的待答问题列表"),
            ToolParameterInfo(name="scope", param_type=ToolParamType.STRING, required=False, default="chat", enum_values=["chat", "global"], description="范围"),
        ],
    )
    async def maibook_record_answer(
        self, book: str = "", topic: str = "", answer: str = "", bible_topic: str = "",
        clear_questions: bool = True, scope: str = "chat", **kwargs: Any,
    ) -> dict[str, Any]:
        scope = self._norm_scope(scope)
        book_dir, _, error = self._resolve_book(kwargs, book, scope)
        if error:
            return error
        topic = str(topic or "").strip() or "决定"
        answer = str(answer or "").strip()
        if not answer:
            return {"success": False, "content": "请提供拍板的内容（answer）。"}
        async with self._lock(book_dir):
            meta = self._read_meta(book_dir)
            decisions_path = book_dir / "journal" / "decisions.md"
            existing = _read_text(decisions_path)
            block = f"## {_now()} · {topic}\n{answer}\n\n"
            _atomic_write(decisions_path, existing + block)
            if str(bible_topic or "").strip():
                name = _safe_component(bible_topic).lower().replace(" ", "-")
                bible_path = book_dir / "bible" / f"{name}.md"
                bible_existing = _read_text(bible_path)
                merged = (bible_existing.rstrip() + f"\n\n## {topic}\n{answer}").strip()
                _atomic_write(bible_path, merged + "\n")
            if clear_questions:
                _atomic_write(book_dir / "journal" / "questions.md", "")
        return {"success": True, "content": f"已把「{topic}」记入《{meta.get('title', book)}》的正典设定。"}

    # ------------------------------------------------------------------ #
    # 工具：交付与封面
    # ------------------------------------------------------------------ #
    @Tool(
        "maibook_deliver",
        brief_description="交付成书：disk=合并为单个 .md 落盘并给出路径；text=直接分段发到聊天；png=渲染为长图发到聊天。",
        parameters=[
            ToolParameterInfo(name="book", param_type=ToolParamType.STRING, required=True, description="书的 slug"),
            ToolParameterInfo(name="format", param_type=ToolParamType.STRING, required=False, default="disk", enum_values=["disk", "text", "png"], description="交付形式"),
            ToolParameterInfo(name="scope", param_type=ToolParamType.STRING, required=False, default="chat", enum_values=["chat", "global"], description="范围"),
            ToolParameterInfo(name="chapter", param_type=ToolParamType.INTEGER, required=False, default=0, description="仅交付某一章时填章节号；0 表示整本"),
        ],
    )
    async def maibook_deliver(self, book: str = "", format: str = "disk", scope: str = "chat", chapter: int = 0, **kwargs: Any) -> dict[str, Any]:
        scope = self._norm_scope(scope)
        book_dir, meta, error = self._resolve_book(kwargs, book, scope)
        if error:
            return error
        fmt = str(format or "disk").strip().lower()
        try:
            chapter_no = int(chapter or 0)
        except (TypeError, ValueError):
            chapter_no = 0

        units = self._gather_units(book_dir, meta, chapter_no)
        if not units:
            return {"success": False, "content": "没有可交付的内容（还没有正文）。"}

        if fmt == "disk":
            title = str(meta.get("title", book))
            version = str(meta.get("version", "0.1.0"))
            compiled = "\n\n".join(text for _, text in units)
            out_path = book_dir / "compiled" / f"{_safe_component(title) or book}-v{version}.md"
            _atomic_write(out_path, compiled + "\n")
            return {"success": True, "path": str(out_path), "content": f"已编译《{title}》到本地文件：\n{out_path}"}

        stream_id = self._resolve_stream_id(kwargs)
        if not stream_id:
            return {"success": False, "content": "无法确定当前聊天流，无法发送到聊天。"}

        if fmt == "text":
            chunks = self._chunk_units(units)
            sent = 0
            for chunk in chunks:
                ok = await self.ctx.send.text(chunk, stream_id)
                sent += 1 if ok else 0
            if sent == 0:
                return {"success": False, "content": f"发送失败：{len(chunks)} 段正文一段都没发出去（聊天发送均返回失败）。"}
            return {
                "success": True,
                "content": f"已直接发送 {sent}/{len(chunks)} 段正文到聊天（绕过回复管线，避免被其它插件改写）。"
                + ("" if sent == len(chunks) else f"（有 {len(chunks) - sent} 段发送失败）"),
            }

        if fmt == "png":
            sent = 0
            total = 0
            for label, text in units:
                total += 1
                html = self._content_html(label, text)
                try:
                    rendered = await self.ctx.render.html2png(html, viewport={"width": 880, "height": self._estimate_height(text)})
                    image_b64 = (rendered or {}).get("image_base64", "")
                    if image_b64 and await self.ctx.send.image(image_b64, stream_id):
                        sent += 1
                except Exception as exc:  # noqa: BLE001
                    self.ctx.logger.warning("麦书：渲染/发送长图失败：%s", exc)
            if sent == 0:
                return {"success": False, "content": f"发送失败：{total} 张长图一张都没发出去（渲染或发送失败，详见日志）。"}
            return {
                "success": True,
                "content": f"已发送 {sent}/{total} 张长图到聊天。" + ("" if sent == total else f"（有 {total - sent} 张失败，详见日志）"),
            }

        return {"success": False, "content": f"未知交付形式：{fmt}"}

    def _gather_units(self, book_dir: Path, meta: Mapping[str, Any], chapter_no: int) -> list[tuple[str, str]]:
        """收集要交付的「单元」：整本=所有 manuscript/*.md（按名排序），或仅某一章。"""
        manuscript = book_dir / "manuscript"
        if not manuscript.exists():
            return []
        if chapter_no and chapter_no > 0:
            text = _read_text(self._chapter_path(book_dir, chapter_no)).strip()
            return [(f"第 {chapter_no} 章", text)] if text else []
        units: list[tuple[str, str]] = []
        for child in sorted(manuscript.glob("*.md")):
            text = _read_text(child).strip()
            if text:
                units.append((child.stem, text))
        return units

    @staticmethod
    def _chunk_units(units: list[tuple[str, str]], max_chars: int = 1500) -> list[str]:
        chunks: list[str] = []
        for _, text in units:
            paragraphs = [p for p in text.split("\n\n") if p.strip()]
            buffer = ""
            for paragraph in paragraphs:
                if buffer and len(buffer) + len(paragraph) + 2 > max_chars:
                    chunks.append(buffer)
                    buffer = paragraph
                else:
                    buffer = f"{buffer}\n\n{paragraph}" if buffer else paragraph
            if buffer:
                chunks.append(buffer)
        return chunks

    @staticmethod
    def _estimate_height(text: str) -> int:
        return max(400, min(20000, 360 + len(text) // 2))

    @staticmethod
    def _content_html(title: str, markdown: str) -> str:
        blocks: list[str] = []
        for block in markdown.split("\n\n"):
            stripped = block.strip()
            if not stripped:
                continue
            if stripped.startswith("## "):
                blocks.append(f"<h2>{html_lib.escape(stripped[3:].strip())}</h2>")
            elif stripped.startswith("# "):
                blocks.append(f"<h1>{html_lib.escape(stripped[2:].strip())}</h1>")
            else:
                blocks.append("<p>" + html_lib.escape(stripped).replace("\n", "<br>") + "</p>")
        body = "\n".join(blocks)
        return (
            "<!doctype html><html><head><meta charset='utf-8'><style>"
            "body{margin:0;padding:48px 56px;background:#fbf7ef;color:#23201b;"
            "font-family:'Noto Serif SC','Songti SC',serif;font-size:20px;line-height:1.9;width:880px;box-sizing:border-box;}"
            "h1{font-size:30px;margin:0 0 24px;}h2{font-size:24px;margin:30px 0 12px;}"
            "p{margin:0 0 16px;text-indent:2em;}"
            f"</style></head><body>{body}</body></html>"
        )

    @Tool(
        "maibook_cover",
        brief_description="渲染本书封面并直接送入上下文（供你和用户一起打磨）；可传 style 调整观感后反复迭代。",
        parameters=[
            ToolParameterInfo(name="book", param_type=ToolParamType.STRING, required=True, description="书的 slug"),
            ToolParameterInfo(name="style", param_type=ToolParamType.STRING, required=False, default="", description="封面观感/风格描述（可选；不同描述会得到不同配色，便于迭代）"),
            ToolParameterInfo(name="scope", param_type=ToolParamType.STRING, required=False, default="chat", enum_values=["chat", "global"], description="范围"),
        ],
    )
    async def maibook_cover(self, book: str = "", style: str = "", scope: str = "chat", **kwargs: Any) -> dict[str, Any]:
        scope = self._norm_scope(scope)
        book_dir, meta, error = self._resolve_book(kwargs, book, scope)
        if error:
            return error
        html = self._cover_html(meta, str(style or ""))
        try:
            rendered = await self.ctx.render.html2png(html, viewport={"width": 800, "height": 1200})
        except Exception as exc:  # noqa: BLE001
            return {"success": False, "content": f"封面渲染失败：{exc}"}
        image_b64 = (rendered or {}).get("image_base64", "")
        if not image_b64:
            return {"success": False, "content": "封面渲染没有产出图片。"}
        title = str(meta.get("title", book))
        return {
            "success": True,
            "content": f"这是《{title}》的封面草图（已送入上下文）。看看哪里想调整——配色、排布、标语都可以说，我再改。",
            "content_items": [
                {
                    "content_type": "image",
                    "data": image_b64,
                    "mime_type": "image/png",
                    "name": f"cover_{_safe_component(book) or 'book'}.png",
                    "description": f"《{title}》的封面草图",
                }
            ],
        }

    @staticmethod
    def _cover_html(meta: Mapping[str, Any], style_hint: str) -> str:
        slug = str(meta.get("slug", meta.get("title", "book")))
        seed = f"{slug}|{style_hint.strip()}"
        digest = int(hashlib.sha256(seed.encode("utf-8")).hexdigest(), 16)
        hue = digest % 360
        hue2 = (hue + 40) % 360
        title = html_lib.escape(str(meta.get("title", "")))
        subtitle = html_lib.escape(str(meta.get("subtitle", "")))
        author = html_lib.escape(str(meta.get("author", "")))
        tagline = html_lib.escape(str(meta.get("tagline", "")))
        return (
            "<!doctype html><html><head><meta charset='utf-8'><style>"
            f"body{{margin:0;width:800px;height:1200px;box-sizing:border-box;"
            f"background:linear-gradient(150deg,hsl({hue},58%,30%),hsl({hue2},52%,16%));"
            "color:#f6f1e7;font-family:'Noto Serif SC','Songti SC',serif;"
            "display:flex;flex-direction:column;justify-content:space-between;padding:88px 72px;}"
            ".tag{font-size:24px;letter-spacing:4px;opacity:.85;}"
            ".title{font-size:76px;line-height:1.15;font-weight:700;margin-top:24px;}"
            ".sub{font-size:32px;opacity:.9;margin-top:28px;}"
            ".author{font-size:30px;opacity:.95;text-align:right;}"
            f"</style></head><body><div><div class='tag'>{tagline}</div>"
            f"<div class='title'>{title}</div><div class='sub'>{subtitle}</div></div>"
            f"<div class='author'>{author} 著</div></body></html>"
        )

    @Tool(
        "maibook_set_autopilot",
        brief_description="设置某本书是否开启后台自动续写（仍受插件总开关 allow_autopilot 约束）。",
        parameters=[
            ToolParameterInfo(name="book", param_type=ToolParamType.STRING, required=True, description="书的 slug"),
            ToolParameterInfo(name="enabled", param_type=ToolParamType.BOOLEAN, required=True, description="开启或关闭"),
            ToolParameterInfo(name="scope", param_type=ToolParamType.STRING, required=False, default="chat", enum_values=["chat", "global"], description="范围"),
        ],
    )
    async def maibook_set_autopilot(self, book: str = "", enabled: bool = False, scope: str = "chat", **kwargs: Any) -> dict[str, Any]:
        scope = self._norm_scope(scope)
        book_dir, meta, error = self._resolve_book(kwargs, book, scope)
        if error:
            return error
        async with self._lock(book_dir):
            new_meta = dict(self._read_meta(book_dir))
            new_meta["autopilot"] = bool(enabled)
            self._write_meta(book_dir, new_meta)
        master = self.config.plugin.allow_autopilot
        note = "" if master else "（注意：插件总开关 allow_autopilot 当前关闭，后台不会真正自动推进。）"
        return {"success": True, "content": f"《{meta.get('title', book)}》的后台自动续写已{'开启' if enabled else '关闭'}。{note}"}

    # ------------------------------------------------------------------ #
    # 命令（面向人类用户的便捷管理）
    # ------------------------------------------------------------------ #
    @Command("maibook_list", description="列出本聊天的书", pattern=r"^/book\s+list\s*$")
    async def cmd_list(self, **kwargs: Any) -> Any:
        stream_id = self._resolve_stream_id(kwargs)
        if not stream_id:
            return None
        books = self._list_books(self._workspace_dir("chat", stream_id))
        if not books:
            await self.ctx.send.text("本聊天还没有书。让我（麦麦）用 maibook_create_book 建一本吧。", stream_id)
            return None
        lines = ["本聊天的书："]
        for item in books:
            lines.append(f"·《{item['title']}》[{item['slug']}] {item['status']} {item['chapters']} 章")
        await self.ctx.send.text("\n".join(lines), stream_id)
        return None


def create_plugin() -> MaiBookPlugin:
    """插件工厂函数。"""
    return MaiBookPlugin()
