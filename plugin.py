"""麦书 (MaiBook) —— 让麦麦创作并长期维护属于自己的书。

设计要点见同目录 README.md。核心思路：
- 每个聊天流是一个「笔记本」工作区（外加一个 ``__global__`` 全局工作区），其中可建立多本书。
- 每本书是一个目录：``book.toml`` 元信息、``instructions.md`` 创作说明、``manuscript/`` 正文、
  ``bible/`` 隐藏设定、``summaries/`` 滚动摘要、``journal/`` 问答与决定及致谢（``credits.md``）、``.history/`` 修订快照、
  ``compiled/`` 编译产物。
- 正文由「专职写手模型」（一次性 ``llm.generate`` 调用）生成，系统提示词里注入麦麦的人格与表达
  风格；麦麦本人担任主编，通过工具下达指令、审稿、拍板。
- 信息不足时「不动笔」：开写前有就绪门禁；写作中允许写手输出 ``===NEED_INFO===`` 区块，由程序把
  问题抛回给麦麦（绝不直接发给编辑），由麦麦自行决定或再问聊天流中的编辑（editor）。
"""

from __future__ import annotations

import asyncio
import hashlib
import html as html_lib
import os
import re
import time
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

# 允许通过 bookshelf_meta 修改的元信息字段白名单
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

# 创作说明：不自动落盘，仅作为给麦麦的参考模板；须由她经 setup_instructions 写入 instructions.md
INSTRUCTIONS_TEMPLATE = """# 《{title}》创作说明

> 这是写手模型每次写作都会读到的「本书专属指令」。请用你自己的话把它写清楚，
> 写得越具体，成稿越贴近你想要的样子。可随时用 setup_instructions 修改。

## 这是一本怎样的书
（题材、基调、想带给读者的感觉……）

## 写作风格与约束
（叙事人称与时态、语言风格、单章篇幅、内容尺度、禁忌……）

## 必须坚持的设定
（与 bible/ 中的世界观、人物、时间线保持一致；列出绝对不能写错的关键事实）

## 推进节奏（建议）
按章号顺序一次只写/改一章（0→1→2…），这样前文衔接最稳。可以跳章或同时发起多个写章任务，但缺了中间章节时「上一章结尾」不会自动补上。

## 编辑（Editor）
本聊天流中参与讨论、给出建议的编辑，其贡献会记入 ``journal/credits.md`` 的致谢区。
"""


def _instructions_template(title: str) -> str:
    """返回给麦麦参考的创作说明模板（不写入文件）。"""
    return INSTRUCTIONS_TEMPLATE.format(title=title)

# 单次工具返回内容的展示上限，避免塞爆 Planner 上下文
MAX_RETURN_CHARS = 8000

CREDITS_TEMPLATE = """# 《{title}》致谢

作者：{author}

## 编辑（Editor）

参与本书创作讨论的编辑将记录于此。

## 创作模型（Models）

正文写作、章节修订与摘要等辅助生成所用模型将记录于此。
"""


# --------------------------------------------------------------------------- #
# 模块级工具函数
# --------------------------------------------------------------------------- #
def _now() -> str:
    """返回本地时区的 ISO 时间字符串（秒精度）。"""
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _ts() -> str:
    """返回用于历史快照文件名的紧凑时间戳（纳秒后缀，避免同秒内多次快照互相覆盖）。"""
    return datetime.now().strftime("%Y%m%d-%H%M%S") + f"-{time.time_ns()}"


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

    模型常凭工具名臆测参数名（如对 setup_outline 传 outline 而非 content），
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


def _llm_model_name(result: Mapping[str, Any] | None, fallback: str = "") -> str:
    """从 llm.generate 返回结构中提取实际模型名。"""
    if not result:
        return fallback
    for key in ("model", "model_name"):
        value = str(result.get(key) or "").strip()
        if value:
            return value
    return fallback


def _resolve_editor(kwargs: Mapping[str, Any], explicit: str = "") -> dict[str, str] | None:
    """从工具 kwargs 或显式参数解析聊天流中的编辑（editor）身份。"""
    name = str(explicit or "").strip()
    for key in ("editor", "editor_name", "user_nickname", "nickname"):
        if not name:
            name = str(kwargs.get(key) or "").strip()
    user_id = str(kwargs.get("user_id") or "").strip()
    platform = str(kwargs.get("platform") or "").strip()
    uinfo = kwargs.get("user_info")
    if isinstance(uinfo, dict):
        user_id = user_id or str(uinfo.get("user_id") or "").strip()
        if not name:
            name = str(uinfo.get("user_nickname") or uinfo.get("nickname") or "").strip()
    if not name and not user_id:
        return None
    display = name or user_id
    if name and user_id and user_id not in name:
        display = f"{name} ({user_id})"
    return {"name": name or user_id, "user_id": user_id, "platform": platform, "display": display}


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
        # 后台写作/修订任务登记：tool 立即返回 task_id，真正生成在后台进行，
        # 完成后由 _surface_task_completion 主动唤醒麦麦（不让她轮询 write_status）。
        self._tasks: dict[str, dict[str, Any]] = {}
        self._task_handles: dict[str, asyncio.Task[Any]] = {}
        self._active_book_tasks: dict[str, str] = {}  # book_dir -> 进行中的 task_id
        self._task_seq = 0

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
        """插件卸载：停止后台续写循环，并取消进行中的写作/修订任务。"""
        self._stop_background_loop()
        for handle in list(self._task_handles.values()):
            handle.cancel()
        self._task_handles.clear()
        self._active_book_tasks.clear()
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
                + "\n（这是你自己的书，可凭设定与记忆自行决定；拿不准再问编辑（editor）。定了用 setup_answer 记入设定。）",
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
        """把进展/问题注入麦麦的上下文并唤醒她处理（写作完成时的正文发送在 _surface_task_completion 中单独处理）。"""
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

    def _credits_path(self, book_dir: Path) -> Path:
        return book_dir / "journal" / "credits.md"

    def _append_editor_credit(
        self, book_dir: Path, editor: Mapping[str, str], *, topic: str = "", note: str = "编辑建议"
    ) -> None:
        """把参与本书讨论的编辑记入 journal/credits.md（去重：同一编辑+主题不重复追加）。"""
        display = str(editor.get("display") or editor.get("name") or "").strip()
        if not display:
            return
        path = self._credits_path(book_dir)
        existing = _read_text(path)
        if not existing.strip():
            title = str(self._read_meta(book_dir).get("title") or book_dir.name)
            author = str(self._read_meta(book_dir).get("author") or "麦麦")
            existing = CREDITS_TEMPLATE.format(title=title, author=author)
        topic_text = str(topic or "").strip()
        marker = f"**{display}**"
        if topic_text and marker in existing and topic_text in existing:
            return
        line = f"- **{display}** · {_now()}"
        if topic_text:
            line += f" · {topic_text}"
        if note:
            line += f"（{note}）"
        section_key = "## 编辑（Editor）"
        if section_key not in existing:
            existing = existing.rstrip() + f"\n\n{section_key}\n\n"
        parts = existing.split(section_key, 1)
        head = parts[0] + section_key
        tail = parts[1] if len(parts) > 1 else "\n\n"
        models_key = "## 创作模型（Models）"
        if models_key in tail:
            editor_body, models_rest = tail.split(models_key, 1)
            tail = editor_body.rstrip() + f"\n{line}\n\n" + models_key + models_rest
        else:
            tail = tail.rstrip() + f"\n{line}\n"
        _atomic_write(path, (head + tail).rstrip() + "\n")

    def _append_model_credit(
        self, book_dir: Path, model: str, role: str, *, chapter_no: int | None = None
    ) -> None:
        """把实际参与生成的模型记入 journal/credits.md。"""
        model = str(model or "").strip()
        if not model:
            return
        path = self._credits_path(book_dir)
        existing = _read_text(path)
        if not existing.strip():
            title = str(self._read_meta(book_dir).get("title") or book_dir.name)
            author = str(self._read_meta(book_dir).get("author") or "麦麦")
            existing = CREDITS_TEMPLATE.format(title=title, author=author)
        line = f"- **{model}** · {role}"
        if chapter_no is not None:
            line += f" · 第 {chapter_no} 章"
        line += f" · {_now()}"
        section_key = "## 创作模型（Models）"
        if section_key not in existing:
            existing = existing.rstrip() + f"\n\n{section_key}\n\n"
        existing = existing.rstrip() + f"\n{line}\n"
        _atomic_write(path, existing)

    def _record_generation_credits(
        self,
        book_dir: Path,
        *,
        role: str,
        chapter_no: int | None,
        generated: Mapping[str, Any],
        summary_result: Mapping[str, Any] | None = None,
    ) -> None:
        """写作/修订成功后记录所用模型（正文 + 摘要）。"""
        writer_model = (self.config.writer.writer_model or "").strip() or "replyer"
        self._append_model_credit(
            book_dir, _llm_model_name(generated, writer_model), role, chapter_no=chapter_no
        )
        if summary_result is not None:
            task = (self.config.context.summary_task or "utils").strip() or "utils"
            self._append_model_credit(
                book_dir, _llm_model_name(summary_result, task), "摘要", chapter_no=chapter_no
            )

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
                "content": f"在{'全局' if scope == 'global' else '本聊天'}里没有找到《{book}》。可用 bookshelf_list 查看，或用 bookshelf_create 新建。",
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
            "- 每次只写当前这一章的正文，不要夹带其它章节。\n"
            "- 严格保持与既有设定、人物、前文摘要的一致性，不要凭空发明与设定冲突的关键事实。\n"
            f"- 若缺少继续写作所必需、且你无法自行合理决定的关键信息：请在回复最前面单独输出一行 {NEED_INFO_MARKER}，"
            "随后用「- 」逐条列出需要确认的问题，然后停止，不要硬编。"
        )
        return "\n\n".join(lines)

    def _assemble_context(
        self, book_dir: Path, meta: Mapping[str, Any], task_text: str, *, target_chapter: int,
        include_next_head: bool = True,
    ) -> str:
        """在字符预算内组装写作上下文（要点→大纲→人物→设定→全部自定义设定→决定→摘要→前文衔接）。

        这是麦麦本人的书：bible/ 下的全部设定文件（含麦麦通过 setup_bible
        自行写入的任意自定义主题）都会作为参考资料带给写手，而不只是几个固定文件。

        target_chapter：本次写作/修订的章号。「上一章结尾」仅当第 target_chapter-1 章
        已存在且非空时才注入；「下一章开头」仅当第 target_chapter+1 章已存在且非空、且
        include_next_head 为 True 时注入（overwrite 丢弃重写时不带，后续章也可能重写）。
        二者均严格按 N±1 取邻章，缺中间章则不带。
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
            summaries = self._collect_summaries(book_dir, before_chapter=target_chapter)
            if summaries:
                sections.append(("【此前章节摘要】", summaries))
        prev_tail = self._previous_chapter_tail(book_dir, target_chapter)
        if prev_tail:
            sections.append(("【上一章结尾（用于衔接）】", prev_tail))
        if include_next_head:
            next_head = self._next_chapter_head(book_dir, target_chapter)
            if next_head:
                sections.append(("【下一章开头（用于衔接）】", next_head))

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

    def _collect_summaries(self, book_dir: Path, *, before_chapter: int | None = None) -> str:
        summaries_dir = book_dir / "summaries"
        if not summaries_dir.exists():
            return ""
        parts: list[str] = []
        for number in self._chapter_numbers(book_dir):
            if before_chapter is not None and number >= before_chapter:
                continue
            text = _read_text(summaries_dir / f"{number:02d}-chapter.md").strip()
            if text:
                parts.append(f"第 {number} 章：{text}")
        return "\n".join(parts)

    def _previous_chapter_tail(self, book_dir: Path, target_chapter: int, tail_chars: int = 1200) -> str:
        """取第 target_chapter-1 章的结尾片段用于衔接；该章不存在或为空时返回空。"""
        if target_chapter <= 0:
            return ""
        text = _read_text(self._chapter_path(book_dir, target_chapter - 1)).strip()
        return text[-tail_chars:] if text else ""

    def _next_chapter_head(self, book_dir: Path, target_chapter: int, head_chars: int = 1200) -> str:
        """取第 target_chapter+1 章的开头片段用于衔接；该章不存在或为空时返回空。"""
        text = _read_text(self._chapter_path(book_dir, target_chapter + 1)).strip()
        return text[:head_chars] if text else ""

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
            if "model" not in result and "model_name" not in result:
                result = dict(result)
                result["model"] = model
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

    async def _summarize_chapter(self, title: str, chapter_no: int, chapter_text: str) -> tuple[str, dict[str, Any] | None]:
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
                if "model" not in result and "model_name" not in result:
                    result = dict(result)
                    result["model"] = task
                return str(result["response"]).strip(), result
            self.ctx.logger.warning("麦书：章节摘要生成失败，回退为截断：%s", (result or {}).get("error"))
        except Exception as exc:  # noqa: BLE001
            self.ctx.logger.warning("麦书：章节摘要调用异常，回退为截断：%s", exc)
        return chapter_text[:400], None

    # ------------------------------------------------------------------ #
    # 写作核心（工具与后台共用）
    # ------------------------------------------------------------------ #
    async def _do_write_chapter(
        self, book_dir: Path, meta: Mapping[str, Any], *, chapter_no: Any, brief: str, target_words: int,
        draft: str = "", overwrite: bool = False,
    ) -> dict[str, Any]:
        """实际写一章；返回结构化结果（success / need_info / setup-not-ready）。

        overwrite=True 时：目标章已有正文则先快照到 .history，再完全丢弃旧稿从零生成（不把旧文带给写手）。
        """
        if meta.get("status") != STATUS_READY:
            missing = self._readiness(book_dir, meta)
            return {
                "success": False,
                "status": STATUS_SETUP,
                "missing": missing,
                "content": "这本书还没准备好开写。请先补全以下必要信息（你可以自己拍板，拿不准再问编辑（editor）），"
                "补全后用 review_ready 标记就绪：\n- " + "\n- ".join(missing),
            }

        if isinstance(chapter_no, str) and chapter_no.strip().lower() in ("", "next"):
            number = (self._chapter_numbers(book_dir)[-1] + 1) if self._chapter_numbers(book_dir) else 1
        else:
            try:
                number = int(chapter_no)
            except (TypeError, ValueError):
                number = (self._chapter_numbers(book_dir)[-1] + 1) if self._chapter_numbers(book_dir) else 1
        number = max(0, number)  # 允许第 0 章（序章/楔子）；续写下一章仍从 1 起

        title = str(meta.get("title", ""))
        persona = await self._persona()
        instructions = _read_text(book_dir / "instructions.md")
        system = self._writer_system_prompt(persona, meta, instructions)

        chapter_path = self._chapter_path(book_dir, number)
        existing_body = _read_text(chapter_path).strip()
        if existing_body and not overwrite:
            return {
                "success": False,
                "content": f"第 {number} 章已有内容。要完全丢弃旧稿从零重写：write_chapter chapter={number} overwrite=true brief=\"…\"；"
                f"要基于旧稿按意见修改：write_revise chapter={number} instruction=\"…\"。",
            }
        if existing_body and overwrite:
            _atomic_write(book_dir / ".history" / f"{number:02d}-chapter.{_ts()}.md", existing_body + "\n")
            old_summary = _read_text(book_dir / "summaries" / f"{number:02d}-chapter.md").strip()
            if old_summary:
                _atomic_write(book_dir / ".history" / f"{number:02d}-summary.{_ts()}.md", old_summary + "\n")

        task_lines = [f"请创作《{title}》的第 {number} 章。"]
        if overwrite and existing_body:
            task_lines.append(
                "【完全重写】本章旧稿已废弃，请勿沿用或参考任何旧正文；按当前设定与大纲从零写出一版全新的正文。"
            )
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
        user = self._assemble_context(
            book_dir, meta, "\n".join(task_lines), target_chapter=number,
            include_next_head=not (overwrite and existing_body),
        )

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
        summary, summary_result = await self._summarize_chapter(title, number, response_text)
        _atomic_write(book_dir / "summaries" / f"{number:02d}-chapter.md", summary)
        credit_role = "重写" if overwrite and existing_body else "写作"
        self._record_generation_credits(
            book_dir, role=credit_role, chapter_no=number, generated=generated, summary_result=summary_result,
        )

        updated = dict(meta)
        updated["status"] = STATUS_READY
        self._write_meta(book_dir, updated)

        word_count = len(re.sub(r"\s+", "", response_text))
        preview = response_text[:500] + ("……" if len(response_text) > 500 else "")
        verb = "重写" if overwrite and existing_body else "写好"
        return {
            "success": True,
            "status": "written",
            "chapter_no": number,
            "word_count": word_count,
            "path": str(chapter_path),
            "preview": preview,
            "content": f"已{verb}《{title}》第 {number} 章，约 {word_count} 字。预览：\n\n{preview}",
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
        if not _read_text(book_dir / "instructions.md").strip():
            missing.append("创作说明（instructions.md，用 setup_instructions 写入）")
        return missing

    def _append_questions(self, book_dir: Path, chapter_no: int, questions: list[str]) -> None:
        path = book_dir / "journal" / "questions.md"
        existing = _read_text(path)
        block = f"## {_now()} · 第 {chapter_no} 章\n" + "\n".join(f"- {item}" for item in questions) + "\n\n"
        _atomic_write(path, existing + block)

    # ------------------------------------------------------------------ #
    # 后台任务登记（写作/修订非阻塞化）
    # ------------------------------------------------------------------ #
    # 写一章往往要数分钟，远超宿主 plugin.invoke_tool 的 RPC 超时（约 60s）。
    # 因此写作/修订工具改为：先做快速校验并立即返回 task_id，真正的生成在后台
    # asyncio 任务里进行。任务结束后由 _surface_task_completion 主动把正文/问题
    # 注入麦麦上下文（context.append）、以 text 发到聊天供编辑阅读，并唤醒她（proactive.trigger）——所以**返回语里
    # 不要叫她去轮询**，否则 planner 会反复调 write_status 直到耗光思考轮次。
    # write_status 仍保留，仅供麦麦想主动查看时用。
    def _start_task(
        self, *, kind: str, book_dir: Path, meta: Mapping[str, Any], stream_id: str,
        chapter_label: str, runner: Any,
    ) -> dict[str, Any]:
        """登记并启动一个后台写作/修订任务；同一本书同时只允许一个在跑。"""
        book_key = str(book_dir)
        existing = self._active_book_tasks.get(book_key)
        if existing and self._tasks.get(existing, {}).get("status") == "running":
            rec = self._tasks[existing]
            return {
                "success": True, "status": "running", "task_id": existing,
                "content": f"《{meta.get('title', book_dir.name)}》正有一个任务在后台进行"
                f"（{existing}：{rec.get('detail', '进行中')}）。它完成后你会被主动唤醒，"
                "同一本书一次只推进一个，先等它好再发起新的写作/修订。",
            }
        self._task_seq += 1
        task_id = f"task-{self._task_seq}"
        record: dict[str, Any] = {
            "task_id": task_id, "kind": kind,
            "book": str(meta.get("slug") or book_dir.name),
            "title": str(meta.get("title", book_dir.name)),
            "scope": "global" if book_dir.parent.name == GLOBAL_WORKSPACE else "chat",
            "stream_id": stream_id,
            "status": "running", "chapter_no": None, "chapter_label": chapter_label,
            "word_count": None, "preview": "", "path": "", "questions": [],
            "error": "", "detail": f"正在{'写作' if kind == 'write' else '修订'}{chapter_label}……",
            "created": _now(), "updated": _now(),
        }
        self._tasks[task_id] = record
        self._active_book_tasks[book_key] = task_id
        self._task_handles[task_id] = asyncio.create_task(self._run_task(task_id, book_key, runner))
        self._prune_tasks()
        verb = "写作" if kind == "write" else "修订"
        wrote = "写好" if kind == "write" else "改好"
        return {
            "success": True, "status": "running", "task_id": task_id,
            "content": f"已在后台开始{verb}《{record['title']}》{chapter_label}（{task_id}）。"
            f"这会花上一些时间（至少一分钟），完成后你会被主动唤醒，{wrote}的正文也会加入你的上下文并发送到聊天供编辑阅读。"
            "你可以先去做别的事了。建议按章号顺序一次只推进一章（0→1→2…），同一本书后台同时只允许一个写作/修订任务；"
            "跳章或并行发起多个写章虽不被工具禁止，但缺中间章时前文衔接会缺失。",
        }

    async def _run_task(self, task_id: str, book_key: str, runner: Any) -> None:
        """在后台执行任务体，并把结果写回登记表。"""
        record = self._tasks[task_id]
        try:
            result = await runner()
        except asyncio.CancelledError:
            record["status"] = "cancelled"
            record["detail"] = "任务已取消"
            record["updated"] = _now()
            self._release_active(book_key, task_id)
            raise
        except Exception as exc:  # noqa: BLE001 - 后台任务异常需登记而非吞掉
            record["status"] = "failed"
            record["error"] = str(exc)
            record["detail"] = f"任务异常：{exc}"
            record["updated"] = _now()
            self.ctx.logger.error("麦书后台任务 %s 异常：%s", task_id, exc, exc_info=True)
            self._release_active(book_key, task_id)
            await self._surface_task_completion(record, book_key)
            return
        self._apply_result_to_record(record, result)
        self._release_active(book_key, task_id)
        await self._surface_task_completion(record, book_key)

    async def _surface_task_completion(self, record: Mapping[str, Any], book_key: str) -> None:
        """后台写作/修订任务结束后，主动把正文发到聊天、注入麦麦上下文并唤醒她——
        这样麦麦无需轮询 write_status；编辑能在聊天里直接读到，她被唤醒时正文也在上下文里。"""
        stream_id = str(record.get("stream_id") or "")
        if not stream_id:
            return
        title = str(record.get("title", ""))
        verb = "写" if record.get("kind") == "write" else "修订"
        chapter_no = record.get("chapter_no")
        chap = record.get("chapter_label") or (f"第 {chapter_no} 章" if chapter_no is not None else "")
        status = record.get("status")
        if status == "done":
            body = ""
            if chapter_no is not None:
                body = _read_text(self._chapter_path(Path(book_key), int(chapter_no))).strip()
            wc = record.get("word_count")
            sent_count, total_chunks = 0, 0
            if body:
                sent_count, total_chunks = await self._send_units_to_chat([(chap or "正文", body)], "text", stream_id)
            if sent_count > 0:
                chat_note = f"正文已以 text 分段发送到本聊天流（{sent_count}/{total_chunks} 段），编辑（editor）可直接阅读；"
            elif body:
                chat_note = "正文发送到聊天流失败（详见日志）；"
                self.ctx.logger.warning(
                    "麦书：《%s》%s 完成后未能把正文发到聊天（%d/%d 段成功）。",
                    title, chap, sent_count, total_chunks,
                )
            else:
                chat_note = ""
            header = f"【麦书】你刚{verb}好《{title}》{chap}" + (f"（约 {wc} 字）" if wc else "") + "。"
            if chat_note:
                header += chat_note
            header += "以下为你自己的上下文副本："
            content = header + "\n\n" + _clip(body) if body else (header + "\n\n" + str(record.get("detail", "")))
            await self._surface_to_mai(
                stream_id, content,
                intent=f"《{title}》{chap}已{verb}好，请决定是否审阅/继续往下推进",
                reason="maibook_task_done",
                book_title=title,
            )
        elif status == "need_info":
            qs = record.get("questions") or []
            content = (
                f"【麦书】{verb}《{title}》{chap}时，写手需要你先拍板几件事：\n- " + "\n- ".join(qs)
                + "\n（这是你自己的书，可凭设定/记忆自行决定，拿不准再问编辑（editor）；定了用 setup_answer 记入设定后再发起。）"
            )
            await self._surface_to_mai(
                stream_id, content,
                intent=f"为《{title}》定夺若干设定问题后再继续",
                reason="maibook_need_info",
                book_title=title,
            )
        elif status == "failed":
            content = f"【麦书】后台{verb}《{title}》{chap}失败了：{record.get('error') or record.get('detail')}。要不要换个方式或稍后重试？"
            await self._surface_to_mai(
                stream_id, content,
                intent=f"《{title}》{chap}{verb}作业失败，请决定是否重试",
                reason="maibook_task_failed",
                book_title=title,
            )

    def _release_active(self, book_key: str, task_id: str) -> None:
        if self._active_book_tasks.get(book_key) == task_id:
            self._active_book_tasks.pop(book_key, None)
        self._task_handles.pop(task_id, None)

    @staticmethod
    def _apply_result_to_record(record: dict[str, Any], result: Mapping[str, Any]) -> None:
        """把 _do_write_chapter / 修订的结构化结果落到任务登记表。"""
        record["updated"] = _now()
        status = result.get("status")
        if status == "need_info":
            record["status"] = "need_info"
            record["chapter_no"] = result.get("chapter_no")
            record["questions"] = list(result.get("questions", []))
            record["detail"] = "写手认为信息不足，需要先拍板几件事。"
        elif result.get("success"):
            record["status"] = "done"
            if result.get("chapter_no") is not None:
                record["chapter_no"] = result.get("chapter_no")
            record["word_count"] = result.get("word_count")
            record["preview"] = str(result.get("preview", ""))
            record["path"] = str(result.get("path", ""))
            record["detail"] = str(result.get("content", "已完成。"))
        else:
            record["status"] = "failed"
            record["error"] = str(result.get("error") or result.get("content") or "未知错误")
            record["detail"] = str(result.get("content") or record["error"])

    def _prune_tasks(self, keep: int = 40) -> None:
        """限制登记表大小：保留全部进行中的任务 + 最近若干个已结束任务。"""
        finished = [tid for tid, rec in self._tasks.items() if rec.get("status") != "running"]
        if len(finished) <= keep:
            return
        finished.sort(key=lambda tid: self._tasks[tid].get("created", ""))
        for tid in finished[: len(finished) - keep]:
            self._tasks.pop(tid, None)

    @staticmethod
    def _render_task(rec: Mapping[str, Any], *, detailed: bool) -> str:
        """把单个任务渲染为给麦麦看的可读文本。"""
        tid = rec.get("task_id")
        kind_label = "写作" if rec.get("kind") == "write" else "修订"
        title = rec.get("title", "")
        chap = rec.get("chapter_label") or (f"第 {rec.get('chapter_no')} 章" if rec.get("chapter_no") is not None else "")
        head = f"{tid}（{kind_label}《{title}》{chap}）"
        status = rec.get("status")
        if status == "running":
            return f"{head}：进行中…（开始于 {rec.get('created')}）"
        if status == "done":
            line = f"{head}：✅ 已完成"
            if rec.get("word_count") is not None:
                line += f"，第 {rec.get('chapter_no')} 章约 {rec.get('word_count')} 字"
            line += f"。读全文：review_read book=\"{rec.get('book')}\" chapter={rec.get('chapter_no')}"
            if detailed and rec.get("preview"):
                line += f"\n\n预览：\n{rec.get('preview')}"
            return line
        if status == "need_info":
            qs = rec.get("questions") or []
            line = f"{head}：⚠ 需要先拍板（写手暂停在第 {rec.get('chapter_no')} 章）：\n- " + "\n- ".join(qs)
            line += "\n（这是你自己的书，可凭设定/记忆自行决定，拿不准再问编辑（editor）；定了用 setup_answer 记入设定后再发起写作。）"
            return line
        if status == "cancelled":
            return f"{head}：已取消。"
        return f"{head}：❌ 失败 — {rec.get('error') or rec.get('detail')}"

    @staticmethod
    def _coalesce_bool(primary: Any, kwargs: Mapping[str, Any], *aliases: str) -> bool:
        """从显式参数与常见同义键里取布尔值。"""
        if isinstance(primary, bool):
            if primary:
                return True
        elif str(primary).strip().lower() in ("true", "1", "yes", "on"):
            return True
        for key in aliases:
            value = kwargs.get(key)
            if isinstance(value, bool) and value:
                return True
            if str(value or "").strip().lower() in ("true", "1", "yes", "on"):
                return True
        return False

    @staticmethod
    def _coalesce_chapter(primary: Any, kwargs: Mapping[str, Any]) -> str:
        """从显式参数与常见同义键里取章节号（planner 常把章号放在 chapter/chapter_no/n 等键）。"""
        candidates = [primary]
        for alias in ("chapter", "chapter_no", "chapter_number", "chapter_index", "chapter_num", "ch", "number", "n"):
            candidates.append(kwargs.get(alias))
        for value in candidates:
            text = str(value if value is not None else "").strip()
            if text:
                return text
        return ""

    # ------------------------------------------------------------------ #
    # 工具：书目管理
    # ------------------------------------------------------------------ #
    @Tool(
        "bookshelf_list",
        brief_description="【麦书/maibook·书库】列出当前聊天（及全局）笔记本里的所有书及其状态。",
        parameters=[
            ToolParameterInfo(
                name="scope", param_type=ToolParamType.STRING, required=False, default="chat",
                description="范围：chat=本聊天（默认），global=全局自留笔记，all=两者",
                enum_values=["chat", "global", "all"],
            ),
        ],
    )
    async def bookshelf_list(self, scope: str = "chat", **kwargs: Any) -> dict[str, Any]:
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
        "bookshelf_create",
        brief_description="【麦书/maibook·书库】新建一本书（初始为 setup 状态，需补全要素后才能开写）。建书时不预填创作说明，返回中会附带参考模板，请用 setup_instructions 写入。",
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
    async def bookshelf_create(
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
        _atomic_write(book_dir / "journal" / "questions.md", "")
        _atomic_write(book_dir / "journal" / "decisions.md", "")
        _atomic_write(
            book_dir / "journal" / "credits.md",
            CREDITS_TEMPLATE.format(title=title, author=persona.get("nickname", "麦麦")),
        )

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
        template = _instructions_template(title)
        return {
            "success": True,
            "slug": slug,
            "content": f"已建立《{title}》[slug={slug}]，作者署名「{meta['author']}」，当前为 setup（未就绪）。\n"
            "开写前还需补全：\n- " + "\n- ".join(missing)
            + "\n\n你可以自己拟定这些要素（拿不准再问编辑（editor））：用 bookshelf_meta 填要点、setup_outline 写大纲、"
            "setup_bible 写人物/设定，用 setup_instructions 写入创作说明，然后 review_ready 标记就绪。"
            "开写后建议按章号顺序一次推进一章。\n\n"
            "【创作说明参考模板（请用 setup_instructions 写入，不要留空）】\n" + template,
        }

    @Tool(
        "bookshelf_meta",
        brief_description="【麦书/maibook·书库】批量修改一本书的元信息（书名/副标题/作者/标语/题材/基调/视角/篇幅/语言/标签等）。",
        parameters=[
            ToolParameterInfo(name="book", param_type=ToolParamType.STRING, required=True, description="书的 slug"),
            ToolParameterInfo(
                name="updates", param_type=ToolParamType.OBJECT, required=True,
                description="要更新的字段对象，键取自：" + "、".join(META_FIELDS) + "（tags 传字符串数组）",
            ),
            ToolParameterInfo(name="scope", param_type=ToolParamType.STRING, required=False, default="chat", enum_values=["chat", "global"], description="范围"),
        ],
    )
    async def bookshelf_meta(self, book: str = "", updates: Any = None, scope: str = "chat", **kwargs: Any) -> dict[str, Any]:
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
        "setup_instructions",
        brief_description="【麦书/maibook·筹备】写入/覆盖本书创作说明 instructions.md（开写前必填，写手每次写作都会读到）。建书返回的参考模板仅作结构提示，须用本工具写入你自己的正文；需要再看模板可加 show_template=true。",
        parameters=[
            ToolParameterInfo(name="book", param_type=ToolParamType.STRING, required=True, description="书的 slug"),
            ToolParameterInfo(name="content", param_type=ToolParamType.STRING, required=False, default="", description="创作说明全文（Markdown）"),
            ToolParameterInfo(name="show_template", param_type=ToolParamType.BOOLEAN, required=False, default=False, description="为 true 时返回参考模板（不写入文件）；可与空 content 联用"),
            ToolParameterInfo(name="scope", param_type=ToolParamType.STRING, required=False, default="chat", enum_values=["chat", "global"], description="范围"),
        ],
    )
    async def setup_instructions(
        self, book: str = "", content: str = "", show_template: bool = False, scope: str = "chat", **kwargs: Any
    ) -> dict[str, Any]:
        scope = self._norm_scope(scope)
        book_dir, meta, error = self._resolve_book(kwargs, book, scope)
        if error:
            return error
        content = _coalesce_text(content, kwargs, "instructions", "text", "body", "markdown")
        show_template = self._coalesce_bool(show_template, kwargs, "show_template", "template")
        body = str(content or "").strip()
        title = str(meta.get("title") or book)
        if not body:
            if show_template:
                return {
                    "success": True,
                    "content": "【创作说明参考模板（请据此用 setup_instructions 写入你自己的正文）】\n"
                    + _instructions_template(title),
                }
            return {
                "success": False,
                "content": f"《{title}》的创作说明内容为空，未做任何改动。请把正文放进 content 后重试；"
                "需要看结构模板可加 show_template=true。",
            }
        async with self._lock(book_dir):
            _atomic_write(book_dir / "instructions.md", body + "\n")
        note = ""
        if show_template:
            note = "\n\n【参考模板】\n" + _clip(_instructions_template(title))
        return {"success": True, "content": f"已更新《{title}》的创作说明。{note}"}

    @Tool(
        "setup_outline",
        brief_description="【麦书/maibook·筹备】写入/更新本书的分章大纲 bible/plot-outline.md（情节骨架）。",
        parameters=[
            ToolParameterInfo(name="book", param_type=ToolParamType.STRING, required=True, description="书的 slug"),
            ToolParameterInfo(name="content", param_type=ToolParamType.STRING, required=True, description="大纲全文（Markdown）"),
            ToolParameterInfo(name="mode", param_type=ToolParamType.STRING, required=False, default="replace", enum_values=["replace", "append"], description="覆盖或追加"),
            ToolParameterInfo(name="scope", param_type=ToolParamType.STRING, required=False, default="chat", enum_values=["chat", "global"], description="范围"),
        ],
    )
    async def setup_outline(self, book: str = "", content: str = "", mode: str = "replace", scope: str = "chat", **kwargs: Any) -> dict[str, Any]:
        content = _coalesce_text(content, kwargs, "outline", "text", "body", "markdown")
        return await self._write_bible(book, "plot-outline", content, mode, scope, kwargs, label="分章大纲")

    @Tool(
        "setup_bible",
        brief_description="【麦书/maibook·筹备】写入/追加隐藏设定 bible/<topic>.md（人物、世界、数值、时间线等，不会出现在成书里）。",
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
    async def setup_bible(self, book: str = "", topic: str = "", content: str = "", mode: str = "append", scope: str = "chat", **kwargs: Any) -> dict[str, Any]:
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
        "review_read",
        brief_description="【麦书/maibook·审阅】读取一本书：概览/元信息/大纲/设定/正文清单/摘要/问题/决定/致谢（编辑与模型）；读某一章正文请直接传 chapter=<章号>（从 0 开始，第 0 章可作序章/楔子）。读到的内容**默认只回到你自己的上下文、不会自动发到聊天**；想顺便发到聊天就加 send=text（分段文本）或 send=png（长图）。",
        parameters=[
            ToolParameterInfo(name="book", param_type=ToolParamType.STRING, required=True, description="书的 slug"),
            ToolParameterInfo(
                name="chapter", param_type=ToolParamType.STRING, required=False, default="",
                description="要读的章节号（整数，从 0 开始；第 0 章可作序章/楔子）；想读某一章正文时填这个最省事。留空表示按 target 读其它内容。",
            ),
            ToolParameterInfo(
                name="target", param_type=ToolParamType.STRING, required=False, default="all",
                description="不读具体某章时的读取目标：all/metadata/instructions/outline/bible/bible:<名>/manuscript/chapter:<N>/summaries/questions/decisions/credits。读单章更推荐直接用 chapter 参数。",
            ),
            ToolParameterInfo(name="scope", param_type=ToolParamType.STRING, required=False, default="chat", enum_values=["chat", "global"], description="范围"),
            ToolParameterInfo(
                name="send", param_type=ToolParamType.STRING, required=False, default="",
                description="是否顺便发到聊天：留空（默认）＝读到的内容只进入你自己的上下文、不发聊天；text＝同时把这段分段发到聊天；png＝同时渲染成长图发到聊天。发送方式与 publish_deliver 一致。",
            ),
        ],
    )
    async def review_read(
        self, book: str = "", chapter: str = "", target: str = "all", scope: str = "chat", send: str = "", **kwargs: Any
    ) -> dict[str, Any]:
        scope = self._norm_scope(scope)
        book_dir, meta, error = self._resolve_book(kwargs, book, scope)
        if error:
            return error
        # planner 常把章号塞进 chapter/chapter_no/n 等键，却让 review_read 默认走 all 概览。
        # 这里把显式 chapter（及同义键）归一化为 target=chapter:<N>，让「读某一章」直达正文。
        chapter_req = self._coalesce_chapter(chapter, kwargs)
        if chapter_req:
            target = f"chapter:{chapter_req}"
        target = str(target or "all").strip().lower()
        result = self._read_target(book_dir, meta, target, book)

        # 默认：读到的内容只通过工具返回值进入麦麦自己的上下文，**不**自动发到聊天。
        # 仅当 send=text/png 时，复用 publish_deliver 的发送方式把这段也发到聊天。
        send = str(send or "").strip().lower()
        if send in ("text", "png") and result.get("success"):
            body = str(result.get("content", ""))
            stream_id = self._resolve_stream_id(kwargs)
            if not stream_id:
                result["content"] = "（想发到聊天，但拿不到当前聊天流；内容仅放进了你的上下文。）\n\n" + body
                result["sent_to_chat"] = 0
            else:
                label = self._read_label(target, str(meta.get("title", book)))
                sent, total = await self._send_units_to_chat([(label, body)], send, stream_id)
                kind = "长图" if send == "png" else "文本"
                if sent > 0:
                    note = f"（已把这段{kind}发送到聊天：{sent}/{total}。）"
                else:
                    note = f"（尝试把这段{kind}发到聊天失败：{total} 份都没发出去；内容仍在你的上下文里。）"
                result["content"] = note + "\n\n" + body
                result["sent_to_chat"] = sent
        return result

    def _read_target(self, book_dir: Path, meta: Mapping[str, Any], target: str, book: str) -> dict[str, Any]:
        """按 target 读取一本书的某部分，返回 {success, content}（纯读取，不涉及聊天发送）。"""
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
        if target == "credits":
            return {"success": True, "content": _clip(_read_text(self._credits_path(book_dir)) or "（暂无致谢记录）")}
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
            raw = target.split(":", 1)[1].strip()
            try:
                number = int(raw)
            except (TypeError, ValueError):
                return {"success": False, "content": f"章节号无效（{raw!r}），请传整数，如 chapter=3。"}
            available = self._chapter_numbers(book_dir)
            avail_text = "、".join(str(n) for n in available) if available else "（暂无正文）"
            if number < 0:
                return {
                    "success": False,
                    "content": f"章节号不能为负（{number}）。本书章节从第 0 章（序章）起，现有正文章节：{avail_text}。",
                }
            text = _read_text(self._chapter_path(book_dir, number))
            if not text.strip():
                return {"success": False, "content": f"第 {number} 章还没有内容。本书现有正文章节：{avail_text}。"}
            return {"success": True, "content": _clip(text)}
        return {"success": False, "content": f"无法识别的 target：{target}"}

    @staticmethod
    def _read_label(target: str, title: str) -> str:
        """给 review_read 发到聊天时用的标题（png 长图的 h1；text 发送不使用）。"""
        if target.startswith("chapter:"):
            return f"第 {target.split(':', 1)[1].strip()} 章"
        if target.startswith("bible:"):
            return f"《{title}》设定·{target.split(':', 1)[1]}"
        names = {
            "all": "概览", "overview": "概览", "metadata": "元信息", "instructions": "创作说明",
            "outline": "大纲", "bible": "设定", "manuscript": "目录", "summaries": "摘要",
            "questions": "待答问题", "decisions": "已记录的决定", "credits": "致谢（编辑与模型）",
        }
        return f"《{title}》" + names.get(target, target)

    @Tool(
        "review_ready",
        brief_description="【麦书/maibook·审阅】检查一本书是否具备开写所需的全部要素；齐全则标记为 ready。",
        parameters=[
            ToolParameterInfo(name="book", param_type=ToolParamType.STRING, required=True, description="书的 slug"),
            ToolParameterInfo(name="scope", param_type=ToolParamType.STRING, required=False, default="chat", enum_values=["chat", "global"], description="范围"),
        ],
    )
    async def review_ready(self, book: str = "", scope: str = "chat", **kwargs: Any) -> dict[str, Any]:
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
        return {"success": True, "ready": True, "content": f"《{meta.get('title', book)}》要素齐全，已标记为 ready，可以开写了。"
            "建议按章号顺序一次写一章（从第 0 章序章或第 1 章正文起），写完审一审再往下推进。"}

    # ------------------------------------------------------------------ #
    # 工具：写作与修订
    # ------------------------------------------------------------------ #
    @Tool(
        "write_chapter",
        brief_description="【麦书/maibook·写作】为一本「已就绪」的书写一章（缺信息会回报问题而不硬写）。续写用 chapter=next；已有章节要完全丢弃旧稿从零重写须 overwrite=true；基于旧稿按意见改请用 write_revise。",
        parameters=[
            ToolParameterInfo(name="book", param_type=ToolParamType.STRING, required=True, description="书的 slug"),
            ToolParameterInfo(name="chapter", param_type=ToolParamType.STRING, required=False, default="next", description="章节号；留空或 next 表示续写下一章。建议按顺序逐章写，不要跳号。"),
            ToolParameterInfo(name="overwrite", param_type=ToolParamType.BOOLEAN, required=False, default=False, description="目标章已有正文时：true=先快照旧稿到 .history 再完全丢弃重写（不把旧文/下一章开头带给写手）；false=拒绝覆盖"),
            ToolParameterInfo(name="brief", param_type=ToolParamType.STRING, required=False, default="", description="本章的特别要求/要点（可选）"),
            ToolParameterInfo(name="content", param_type=ToolParamType.STRING, required=False, default="", description="麦麦为本章提供的参考稿/草稿正文（可选）；写手会以它为基准来完成本章，保留其情节与关键设定"),
            ToolParameterInfo(name="target_words", param_type=ToolParamType.INTEGER, required=False, default=0, description="目标字数（可选，0 表示不限）"),
            ToolParameterInfo(name="scope", param_type=ToolParamType.STRING, required=False, default="chat", enum_values=["chat", "global"], description="范围"),
        ],
    )
    async def write_chapter(
        self, book: str = "", chapter: str = "next", overwrite: bool = False, brief: str = "", content: str = "",
        target_words: int = 0, scope: str = "chat", **kwargs: Any
    ) -> dict[str, Any]:
        scope = self._norm_scope(scope)
        book_dir, meta, error = self._resolve_book(kwargs, book, scope)
        if error:
            return error
        # 快速门禁同步返回（即时反馈），真正生成放后台，避免 RPC 超时。
        if meta.get("status") != STATUS_READY:
            missing = self._readiness(book_dir, meta)
            return {
                "success": False, "status": STATUS_SETUP, "missing": missing,
                "content": "这本书还没准备好开写。请先补全以下必要信息（你可以自己拍板，拿不准再问编辑（editor）），"
                "补全后用 review_ready 标记就绪：\n- " + "\n- ".join(missing),
            }

        chap_input = self._coalesce_chapter(chapter, kwargs) or "next"
        overwrite = self._coalesce_bool(overwrite, kwargs, "overwrite", "replace", "discard")
        existing = self._chapter_numbers(book_dir)
        if chap_input.strip().lower() in ("", "next"):
            planned = (existing[-1] + 1) if existing else 1
        else:
            try:
                planned = max(0, int(chap_input))
            except (TypeError, ValueError):
                planned = (existing[-1] + 1) if existing else 1
        if _read_text(self._chapter_path(book_dir, planned)).strip() and not overwrite:
            return {
                "success": False,
                "content": f"第 {planned} 章已有内容。要完全丢弃旧稿从零重写：write_chapter chapter={planned} overwrite=true brief=\"…\"；"
                f"要基于旧稿按意见修改：write_revise chapter={planned} instruction=\"…\"。",
            }
        chapter_label = f"第 {planned} 章" + ("（丢弃重写）" if overwrite else "")
        draft = content
        words = int(target_words or 0)

        async def _runner() -> dict[str, Any]:
            async with self._lock(book_dir):
                fresh = self._read_meta(book_dir)
                return await self._do_write_chapter(
                    book_dir, fresh, chapter_no=chap_input, brief=brief, draft=draft, target_words=words,
                    overwrite=overwrite,
                )

        return self._start_task(
            kind="write", book_dir=book_dir, meta=meta, stream_id=self._resolve_stream_id(kwargs),
            chapter_label=chapter_label, runner=_runner,
        )

    @Tool(
        "write_revise",
        brief_description="【麦书/maibook·写作】修订已写好的某一章（整章重写，或仅重写指定小节）；修订前自动快照到 .history，写手会看到旧稿。要完全丢弃旧稿从零写请用 write_chapter overwrite=true。",
        parameters=[
            ToolParameterInfo(name="book", param_type=ToolParamType.STRING, required=True, description="书的 slug"),
            ToolParameterInfo(name="chapter", param_type=ToolParamType.INTEGER, required=True, description="要修订的章节号"),
            ToolParameterInfo(name="instruction", param_type=ToolParamType.STRING, required=True, description="修订要求（怎么改）"),
            ToolParameterInfo(name="editor", param_type=ToolParamType.STRING, required=False, default="", description="提出修订意见的编辑（editor）；留空则尝试从当前聊天上下文识别"),
            ToolParameterInfo(name="target_section", param_type=ToolParamType.STRING, required=False, default="", description="只改某个「## 小节标题」时填该标题；留空则整章重写"),
            ToolParameterInfo(name="scope", param_type=ToolParamType.STRING, required=False, default="chat", enum_values=["chat", "global"], description="范围"),
        ],
    )
    async def write_revise(
        self, book: str = "", chapter: int = 0, instruction: str = "", editor: str = "", target_section: str = "", scope: str = "chat", **kwargs: Any
    ) -> dict[str, Any]:
        scope = self._norm_scope(scope)
        book_dir, meta, error = self._resolve_book(kwargs, book, scope)
        if error:
            return error
        # 快速校验同步返回；真正的生成放后台，避免 RPC 超时。
        chap_req = self._coalesce_chapter(chapter, kwargs)
        try:
            number = int(chap_req) if chap_req else int(chapter)
        except (TypeError, ValueError):
            return {"success": False, "content": "章节号无效（请传要修订的章号，如 chapter=2）。"}
        if number < 0:
            return {"success": False, "content": "章节号不能为负，请传 >=0 的章号（第 0 章为序章/楔子）。"}
        chapter_path = self._chapter_path(book_dir, number)
        original = _read_text(chapter_path)
        if not original.strip():
            available = self._chapter_numbers(book_dir)
            avail_text = "、".join(str(n) for n in available) if available else "（暂无正文）"
            return {"success": False, "content": f"第 {number} 章还没有内容，无法修订（现有章节：{avail_text}）。可用 write_chapter 先写。"}
        instruction = str(instruction or "").strip()
        if not instruction:
            return {"success": False, "content": "请说明怎么改（instruction）。"}
        section = str(target_section or "").strip()
        if section and self._extract_section(original, section) is None:
            return {"success": False, "content": f"在第 {number} 章里找不到小节「## {section}」。"}

        chapter_label = f"第 {number} 章" + (f"·小节「{section}」" if section else "")
        editor_info = _resolve_editor(kwargs, editor)

        async def _runner() -> dict[str, Any]:
            persona = await self._persona()
            fresh_meta = self._read_meta(book_dir)
            cur = _read_text(chapter_path)
            instructions = _read_text(book_dir / "instructions.md")
            system = self._writer_system_prompt(persona, fresh_meta, instructions)
            if section:
                extracted = self._extract_section(cur, section)
                if extracted is None:
                    return {"success": False, "content": f"在第 {number} 章里找不到小节「## {section}」。"}
                task = (
                    f"下面是《{fresh_meta.get('title','')}》第 {number} 章中「## {section}」小节的原文，请按要求重写"
                    f"该小节（只输出重写后的该小节正文，从「## {section}」开始）。\n\n修订要求：{instruction}\n\n原文：\n{extracted}"
                )
            else:
                task = (
                    f"下面是《{fresh_meta.get('title','')}》第 {number} 章的原文，请按要求整章重写（只输出重写后的正文）。\n\n"
                    f"修订要求：{instruction}\n\n原文：\n{cur}"
                )
            user = self._assemble_context(book_dir, fresh_meta, task, target_chapter=number)

            generated = await self._writer_generate(system, user)
            if not generated.get("success"):
                return {"success": False, "content": f"修订生成失败：{generated.get('error', '未知错误')}"}
            new_text = str(generated.get("response", "")).strip()
            if _extract_need_info(new_text) is not None:
                return {"success": False, "content": "写手认为信息不足，建议先用 write_chapter 流程澄清问题后再修订。"}

            async with self._lock(book_dir):
                # 快照
                _atomic_write(book_dir / ".history" / f"{number:02d}-chapter.{_ts()}.md", cur)
                if section:
                    merged = self._replace_section(cur, section, new_text)
                else:
                    merged = new_text if new_text.startswith("#") else f"# 第 {number} 章\n\n{new_text}\n"
                _atomic_write(chapter_path, merged)
                summary, summary_result = await self._summarize_chapter(str(fresh_meta.get("title", "")), number, merged)
                _atomic_write(book_dir / "summaries" / f"{number:02d}-chapter.md", summary)
                self._record_generation_credits(
                    book_dir, role="修订", chapter_no=number, generated=generated, summary_result=summary_result,
                )
                if editor_info:
                    self._append_editor_credit(
                        book_dir, editor_info, topic=f"第 {number} 章修订", note=instruction[:80] or "修订意见"
                    )

            return {
                "success": True, "chapter_no": number,
                "content": f"已修订《{fresh_meta.get('title', book)}》第 {number} 章（原稿已快照到 .history）。",
            }

        return self._start_task(
            kind="revise", book_dir=book_dir, meta=meta, stream_id=self._resolve_stream_id(kwargs),
            chapter_label=chapter_label, runner=_runner,
        )

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

    @Tool(
        "write_status",
        brief_description="【麦书/maibook·写作】（可选）主动查看后台写作/修订任务的进度与结果。写作/修订非阻塞：任务完成会自动唤醒你并把正文加入上下文，通常无需主动查；只有你想提前看看时才用本工具。",
        parameters=[
            ToolParameterInfo(name="task_id", param_type=ToolParamType.STRING, required=False, default="", description="要查的任务 id（write_chapter / write_revise 返回的那个）；留空则按 book 或列出全部"),
            ToolParameterInfo(name="book", param_type=ToolParamType.STRING, required=False, default="", description="只看某本书的任务（slug）；留空看全部"),
            ToolParameterInfo(name="scope", param_type=ToolParamType.STRING, required=False, default="chat", enum_values=["chat", "global"], description="范围"),
        ],
    )
    async def write_status(self, task_id: str = "", book: str = "", scope: str = "chat", **kwargs: Any) -> dict[str, Any]:
        task_id = str(task_id or kwargs.get("id", "") or kwargs.get("task", "")).strip()
        if task_id:
            rec = self._tasks.get(task_id)
            if rec is None:
                return {"success": False, "content": f"找不到任务 {task_id}（可能已完成并被清理，或 id 有误）。可用 write_status（不带参数）列出现有任务。"}
            return {"success": True, "status": rec.get("status"), "task_id": task_id, "content": self._render_task(rec, detailed=True)}

        book_slug = _safe_component(book) if str(book or "").strip() else ""
        items = [rec for rec in self._tasks.values() if not book_slug or rec.get("book") == book_slug]
        items.sort(key=lambda rec: str(rec.get("created", "")), reverse=True)
        if not items:
            where = f"《{book}》" if book_slug else "本插件"
            return {"success": True, "content": f"{where}当前没有后台写作/修订任务。"}
        running = [rec for rec in items if rec.get("status") == "running"]
        header = f"后台任务（共 {len(items)} 个，进行中 {len(running)} 个）："
        lines = [header] + [f"· {self._render_task(rec, detailed=False)}" for rec in items[:20]]
        return {"success": True, "running": len(running), "content": _clip("\n".join(lines))}

    # ------------------------------------------------------------------ #
    # 工具：问答闭环
    # ------------------------------------------------------------------ #
    @Tool(
        "review_questions",
        brief_description="【麦书/maibook·审阅】查看一本书当前待拍板/待澄清的问题。",
        parameters=[
            ToolParameterInfo(name="book", param_type=ToolParamType.STRING, required=True, description="书的 slug"),
            ToolParameterInfo(name="scope", param_type=ToolParamType.STRING, required=False, default="chat", enum_values=["chat", "global"], description="范围"),
        ],
    )
    async def review_questions(self, book: str = "", scope: str = "chat", **kwargs: Any) -> dict[str, Any]:
        scope = self._norm_scope(scope)
        book_dir, meta, error = self._resolve_book(kwargs, book, scope)
        if error:
            return error
        content = _read_text(book_dir / "journal" / "questions.md").strip()
        return {"success": True, "content": _clip(content or "（暂无待答问题）")}

    @Tool(
        "setup_answer",
        brief_description="【麦书/maibook·筹备】把一个已拍板的决定写入正典（decisions.md，并按需归入某个 bible 设定），随后清空待答问题；参与讨论的编辑会记入 credits.md 致谢。",
        parameters=[
            ToolParameterInfo(name="book", param_type=ToolParamType.STRING, required=True, description="书的 slug"),
            ToolParameterInfo(name="topic", param_type=ToolParamType.STRING, required=True, description="这个决定的主题/小标题"),
            ToolParameterInfo(name="answer", param_type=ToolParamType.STRING, required=True, description="拍板的内容/决定"),
            ToolParameterInfo(name="editor", param_type=ToolParamType.STRING, required=False, default="", description="给出此建议的编辑（editor）昵称；留空则尝试从当前聊天上下文识别"),
            ToolParameterInfo(name="bible_topic", param_type=ToolParamType.STRING, required=False, default="", description="若该决定也属于某项设定，填 bible 主题名（如 characters/world），会一并追加"),
            ToolParameterInfo(name="clear_questions", param_type=ToolParamType.BOOLEAN, required=False, default=True, description="是否清空该书的待答问题列表"),
            ToolParameterInfo(name="scope", param_type=ToolParamType.STRING, required=False, default="chat", enum_values=["chat", "global"], description="范围"),
        ],
    )
    async def setup_answer(
        self, book: str = "", topic: str = "", answer: str = "", editor: str = "", bible_topic: str = "",
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
        editor_info = _resolve_editor(kwargs, editor)
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
            if editor_info:
                self._append_editor_credit(book_dir, editor_info, topic=topic, note="编辑建议")
        credit_note = ""
        if editor_info:
            credit_note = f" 编辑 {editor_info['display']} 已记入致谢。"
        return {"success": True, "content": f"已把「{topic}」记入《{meta.get('title', book)}》的正典设定。{credit_note}"}

    # ------------------------------------------------------------------ #
    # 工具：交付与封面
    # ------------------------------------------------------------------ #
    @Tool(
        "publish_deliver",
        brief_description="【麦书/maibook·交付】交付成书：disk=合并为单个 .md 落盘并给出路径；text=直接分段发到聊天；png=渲染为长图发到聊天。",
        parameters=[
            ToolParameterInfo(name="book", param_type=ToolParamType.STRING, required=True, description="书的 slug"),
            ToolParameterInfo(name="format", param_type=ToolParamType.STRING, required=False, default="disk", enum_values=["disk", "text", "png"], description="交付形式"),
            ToolParameterInfo(name="scope", param_type=ToolParamType.STRING, required=False, default="chat", enum_values=["chat", "global"], description="范围"),
            ToolParameterInfo(name="chapter", param_type=ToolParamType.INTEGER, required=False, default=-1, description="仅交付某一章时填章节号（含第 0 章/序章）；留空或 -1 表示整本"),
        ],
    )
    async def publish_deliver(self, book: str = "", format: str = "disk", scope: str = "chat", chapter: int = -1, **kwargs: Any) -> dict[str, Any]:
        scope = self._norm_scope(scope)
        book_dir, meta, error = self._resolve_book(kwargs, book, scope)
        if error:
            return error
        fmt = str(format or "disk").strip().lower()
        try:
            chapter_no = int(chapter)
        except (TypeError, ValueError):
            chapter_no = -1

        units = self._gather_units(book_dir, meta, chapter_no)
        if not units:
            return {"success": False, "content": "没有可交付的内容（还没有正文）。"}

        if fmt == "disk":
            title = str(meta.get("title", book))
            version = str(meta.get("version", "0.1.0"))
            compiled = "\n\n".join(text for _, text in units)
            if chapter_no < 0:
                credits = _read_text(self._credits_path(book_dir)).strip()
                if credits:
                    compiled = compiled + "\n\n---\n\n" + credits
            out_path = book_dir / "compiled" / f"{_safe_component(title) or book}-v{version}.md"
            _atomic_write(out_path, compiled + "\n")
            return {"success": True, "path": str(out_path), "content": f"已编译《{title}》到本地文件：\n{out_path}"}

        stream_id = self._resolve_stream_id(kwargs)
        if not stream_id:
            return {"success": False, "content": "无法确定当前聊天流，无法发送到聊天。"}

        if fmt == "text":
            sent, total = await self._send_units_to_chat(units, "text", stream_id)
            if sent == 0:
                return {"success": False, "content": f"发送失败：{total} 段正文一段都没发出去（聊天发送均返回失败）。"}
            return {
                "success": True,
                "content": f"已直接发送 {sent}/{total} 段正文到聊天（绕过回复管线，避免被其它插件改写）。"
                + ("" if sent == total else f"（有 {total - sent} 段发送失败）"),
            }

        if fmt == "png":
            sent, total = await self._send_units_to_chat(units, "png", stream_id)
            if sent == 0:
                return {"success": False, "content": f"发送失败：{total} 张长图一张都没发出去（渲染或发送失败，详见日志）。"}
            return {
                "success": True,
                "content": f"已发送 {sent}/{total} 张长图到聊天。" + ("" if sent == total else f"（有 {total - sent} 张失败，详见日志）"),
            }

        return {"success": False, "content": f"未知交付形式：{fmt}"}

    async def _send_units_to_chat(self, units: list[tuple[str, str]], fmt: str, stream_id: str) -> tuple[int, int]:
        """把若干「单元」发到聊天流：text=分段发文本（绕过回复管线）；png=逐单元渲染长图发送。
        返回 (实际发出数, 总数)。供 publish_deliver 与 review_read(send=...) 共用。"""
        if fmt == "text":
            chunks = self._chunk_units(units)
            sent = 0
            for chunk in chunks:
                if await self.ctx.send.text(chunk, stream_id):
                    sent += 1
            return sent, len(chunks)
        if fmt == "png":
            sent = 0
            total = 0
            for label, text in units:
                total += 1
                try:
                    image_b64 = await self._render_chat_image(label, text)
                    if image_b64 and await self.ctx.send.image(image_b64, stream_id):
                        sent += 1
                except Exception as exc:  # noqa: BLE001
                    self.ctx.logger.warning("麦书：渲染/发送长图失败：%s", exc)
            return sent, total
        return 0, 0

    async def _render_chat_image(self, label: str, text: str) -> str:
        """把一段内容渲染成**适合发聊天的小体积图片**。
        关键：以 1x 像素比渲染——`render.html2png` 默认 2x，会让黑白文字长图体积翻几倍，
        在 NapCat 默认约 15s 的动作超时内常常传不完（动作其实已送达，但回执超时被误报为发送失败）。
        随后无损 WebP 重编码再压一道。返回图片 base64；渲染拿不到图时返回空串。"""
        html = self._content_html(label, text)
        rendered = await self.ctx.render.html2png(
            html,
            viewport={"width": 880, "height": self._estimate_height(text)},
            device_scale_factor=1.0,
        )
        image_b64 = (rendered or {}).get("image_base64", "") if isinstance(rendered, dict) else ""
        if not image_b64:
            return ""
        return self._to_compact_webp(image_b64)

    def _to_compact_webp(self, png_b64: str) -> str:
        """把 Host 渲染的 PNG 无损重编码为 WebP（黑白文字长图体积大幅下降，更容易在 NapCat
        默认动作超时内上传完成）。仅当 WebP 更小才替换；Pillow 不可用或编码异常时按原 PNG
        返回（仍是有效图片，只是更大）并告警——不静默掩盖。"""
        try:
            import base64
            import io

            from PIL import Image
        except Exception as exc:  # noqa: BLE001 - 缺 Pillow 时退回 PNG，并明确告警
            self.ctx.logger.warning("麦书：未能加载 Pillow，长图按原始 PNG 发送（体积更大，可能触发发送超时）：%s", exc)
            return png_b64
        try:
            raw = base64.b64decode(png_b64)
            with Image.open(io.BytesIO(raw)) as im:
                im.load()
                buf = io.BytesIO()
                im.save(buf, format="WEBP", lossless=True, method=6)
            webp = buf.getvalue()
        except Exception as exc:  # noqa: BLE001 - 压缩失败不应阻断发送
            self.ctx.logger.warning("麦书：长图 WebP 压缩失败，按原始 PNG 发送：%s", exc)
            return png_b64
        if len(webp) < len(raw):
            self.ctx.logger.info("麦书：长图压缩 PNG %d → WebP %d 字节", len(raw), len(webp))
            return base64.b64encode(webp).decode("ascii")
        return png_b64

    def _gather_units(self, book_dir: Path, meta: Mapping[str, Any], chapter_no: int) -> list[tuple[str, str]]:
        """收集要交付的「单元」：chapter_no>=0 仅某一章（含第 0 章/序章），否则整本=所有 manuscript/*.md（按名排序）。"""
        manuscript = book_dir / "manuscript"
        if not manuscript.exists():
            return []
        if chapter_no >= 0:
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
        "publish_cover",
        brief_description="【麦书/maibook·交付】渲染本书封面并直接送入上下文（供你和编辑（editor）一起打磨）；可传 style 调整观感后反复迭代。",
        parameters=[
            ToolParameterInfo(name="book", param_type=ToolParamType.STRING, required=True, description="书的 slug"),
            ToolParameterInfo(name="style", param_type=ToolParamType.STRING, required=False, default="", description="封面观感/风格描述（可选；不同描述会得到不同配色，便于迭代）"),
            ToolParameterInfo(name="scope", param_type=ToolParamType.STRING, required=False, default="chat", enum_values=["chat", "global"], description="范围"),
        ],
    )
    async def publish_cover(self, book: str = "", style: str = "", scope: str = "chat", **kwargs: Any) -> dict[str, Any]:
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
        "bookshelf_autopilot",
        brief_description="【麦书/maibook·书库】设置某本书是否开启后台自动续写（仍受插件总开关 allow_autopilot 约束）。",
        parameters=[
            ToolParameterInfo(name="book", param_type=ToolParamType.STRING, required=True, description="书的 slug"),
            ToolParameterInfo(name="enabled", param_type=ToolParamType.BOOLEAN, required=True, description="开启或关闭"),
            ToolParameterInfo(name="scope", param_type=ToolParamType.STRING, required=False, default="chat", enum_values=["chat", "global"], description="范围"),
        ],
    )
    async def bookshelf_autopilot(self, book: str = "", enabled: bool = False, scope: str = "chat", **kwargs: Any) -> dict[str, Any]:
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
    @Command("book_list_command", description="列出本聊天的书", pattern=r"^/book\s+list\s*$")
    async def cmd_list(self, **kwargs: Any) -> Any:
        stream_id = self._resolve_stream_id(kwargs)
        if not stream_id:
            return None
        books = self._list_books(self._workspace_dir("chat", stream_id))
        if not books:
            await self.ctx.send.text("本聊天还没有书。让我（麦麦）用 bookshelf_create 建一本吧。", stream_id)
            return None
        lines = ["本聊天的书："]
        for item in books:
            lines.append(f"·《{item['title']}》[{item['slug']}] {item['status']} {item['chapters']} 章")
        await self.ctx.send.text("\n".join(lines), stream_id)
        return None


def create_plugin() -> MaiBookPlugin:
    """插件工厂函数。"""
    return MaiBookPlugin()
