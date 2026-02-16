from __future__ import annotations

from collections import OrderedDict

from flask import current_app, g

from ..extensions import db
from ..models import AppSetting


DEFAULT_NOTICE_SYSTEM_PROMPT = "你是学习小组内容编辑助手，要求输出准确、紧凑、可读。"
DEFAULT_NOTICE_BLOG_TASK = (
    "把输入记录整理成一篇中文博客 HTML，输出必须是纯 HTML（不要 markdown 代码块）。"
    "结构包含：标题、导语、按主题分节的小标题、结语。"
    "保留关键事实和时间线，语言自然可读，避免空泛。"
)
DEFAULT_NOTICE_PODCAST_TASK = (
    "把输入记录整理成一份中文播客脚本，时长 3-6 分钟。"
    "需要有开场、主体、结尾，重点清晰，可直接用于语音合成。"
)
DEFAULT_NOTICE_POSTER_TASK = "把输入记录提炼成一份中文海报文案，包含标题、3-6 个重点、结语。"
DEFAULT_POSTER_SYSTEM_PROMPT = "你是视觉总监，请把学习记录提炼成适合图像模型的一段海报提示词。"
DEFAULT_POSTER_USER_TEMPLATE = (
    "请输出一段 200-450 字中文提示词，用于生成学习小组海报。"
    "包含主题、排版、颜色、风格、元素。只输出提示词本身。\n\n"
    "记录输入：\n{records_text}"
)
DEFAULT_VECTOR_CHAT_SYSTEM_PROMPT = (
    "你是 Benoss 本地知识库助手。"
    "只基于给定检索结果回答，不确定就明确说明证据不足。"
    "回答时尽量简洁，并优先给出结论。"
)


SETTING_DEFINITIONS: list[dict] = [
    {
        "key": "BOARD_DEFAULT_DAYS",
        "label": "Board 默认天数",
        "type": "int",
        "group": "站点",
        "description": "Board 页默认展示最近多少天。",
        "min": 1,
        "max": 30,
        "default": 7,
    },
    {
        "key": "DIGEST_TIMEZONE",
        "label": "Digest 时区",
        "type": "string",
        "group": "站点",
        "description": "日报任务计算“昨天”的时区，例如 Asia/Shanghai。",
        "default": "Asia/Shanghai",
    },
    {
        "key": "LOCAL_DAILY_ARCHIVE_DIR",
        "label": "本地归档目录",
        "type": "string",
        "group": "站点",
        "description": "按天保存 JSON 归档的目录。",
        "default": "data/daily-archive",
    },
    {
        "key": "LOCAL_VECTOR_STORE_DIR",
        "label": "本地向量库目录",
        "type": "string",
        "group": "站点",
        "description": "本地向量索引文件目录。",
        "default": "data/vector-store",
    },
    {
        "key": "VECTOR_AUTO_REBUILD",
        "label": "自动重建向量索引",
        "type": "bool",
        "group": "站点",
        "description": "归档更新后自动重建本地向量索引。",
        "default": True,
    },
    {
        "key": "VECTOR_TOP_K",
        "label": "向量检索默认 TopK",
        "type": "int",
        "group": "站点",
        "description": "聊天检索默认返回多少条候选。",
        "min": 1,
        "max": 20,
        "default": 6,
    },
    {
        "key": "VECTOR_MAX_DOCS",
        "label": "向量库最大文档数",
        "type": "int",
        "group": "站点",
        "description": "本地向量索引最多纳入多少条文档。",
        "min": 200,
        "max": 30000,
        "default": 4000,
    },
    {
        "key": "HOME_AUTO_BUILD_DAILY_ASSETS",
        "label": "首页自动生成日报资产",
        "type": "bool",
        "group": "站点",
        "description": "首页访问时自动补齐当天公开内容的博客/播客/海报。",
        "default": True,
    },
    {
        "key": "HOME_DIGEST_RETRY_MINUTES",
        "label": "日报重试间隔(分钟)",
        "type": "int",
        "group": "站点",
        "description": "日报失败/部分成功后，最短重试间隔。",
        "min": 1,
        "max": 720,
        "default": 30,
    },
    {
        "key": "AI_AUTOFILL_PROVIDER",
        "label": "AI Provider",
        "type": "choice",
        "group": "AI 基础",
        "description": "Notice 生成与向量问答使用的默认 provider。",
        "default": "",
        "options": [
            {"label": "关闭", "value": ""},
            {"label": "chatanywhere", "value": "chatanywhere"},
            {"label": "deepseek", "value": "deepseek"},
            {"label": "aliyun", "value": "aliyun"},
        ],
        "normalize": "provider",
    },
    {
        "key": "AI_REQUEST_TIMEOUT_SECONDS",
        "label": "AI 请求超时(秒)",
        "type": "int",
        "group": "AI 基础",
        "description": "所有 AI HTTP 请求超时。",
        "min": 10,
        "max": 1800,
        "default": 45,
    },
    {
        "key": "AI_MAX_NOTICE_RECORDS",
        "label": "Notice AI 最大记录数",
        "type": "int",
        "group": "AI 基础",
        "description": "Notice AI 生成时最多读取多少条记录。",
        "min": 20,
        "max": 500,
        "default": 180,
    },
    {
        "key": "AI_IMAGE_MODEL",
        "label": "图片生成模型",
        "type": "string",
        "group": "AI 基础",
        "description": "海报生成所用图像模型。",
        "default": "gpt-image-1",
    },
    {
        "key": "AI_TTS_MODEL",
        "label": "播客 TTS 模型",
        "type": "string",
        "group": "AI 基础",
        "description": "播客音频生成的语音模型。",
        "default": "gpt-4o-mini-tts",
    },
    {
        "key": "AI_TTS_VOICE",
        "label": "播客 TTS 音色",
        "type": "string",
        "group": "AI 基础",
        "description": "播客语音音色，例如 alloy。",
        "default": "alloy",
    },
    {
        "key": "AI_TTS_RESPONSE_FORMAT",
        "label": "播客音频格式",
        "type": "choice",
        "group": "AI 基础",
        "description": "语音接口返回格式。",
        "default": "mp3",
        "options": [
            {"label": "mp3", "value": "mp3"},
            {"label": "wav", "value": "wav"},
            {"label": "aac", "value": "aac"},
            {"label": "flac", "value": "flac"},
            {"label": "opus", "value": "opus"},
        ],
    },
    {
        "key": "AI_TTS_MAX_INPUT_CHARS",
        "label": "TTS 最大输入长度",
        "type": "int",
        "group": "AI 基础",
        "description": "播客脚本传给 TTS 前的最大字符数。",
        "min": 600,
        "max": 20000,
        "default": 3600,
    },
    {
        "key": "PODCAST_DEFAULT_STYLE",
        "label": "默认播客风格",
        "type": "choice",
        "group": "AI 基础",
        "description": "Notice 播客默认脚本风格。",
        "default": "dialogue",
        "options": [
            {"label": "对话式", "value": "dialogue"},
            {"label": "演讲式", "value": "speech"},
            {"label": "访谈式", "value": "interview"},
            {"label": "播报式", "value": "news"},
        ],
    },
    {
        "key": "CHAT_ANYWHERE_API_KEY",
        "label": "ChatAnywhere API Key",
        "type": "string",
        "group": "Provider: ChatAnywhere",
        "description": "用于 ChatAnywhere 鉴权。",
        "default": "",
        "secret": True,
    },
    {
        "key": "CHAT_ANYWHERE_API_BASE_URL",
        "label": "ChatAnywhere Base URL",
        "type": "string",
        "group": "Provider: ChatAnywhere",
        "description": "OpenAI 兼容接口根地址。",
        "default": "https://api.chatanywhere.tech/v1",
    },
    {
        "key": "CHAT_ANYWHERE_MODEL",
        "label": "ChatAnywhere 模型",
        "type": "string",
        "group": "Provider: ChatAnywhere",
        "description": "默认聊天模型。",
        "default": "gpt-4o-mini",
    },
    {
        "key": "DEEPSEEK_API_KEY",
        "label": "DeepSeek API Key",
        "type": "string",
        "group": "Provider: DeepSeek",
        "description": "用于 DeepSeek 鉴权。",
        "default": "",
        "secret": True,
    },
    {
        "key": "DEEPSEEK_API_BASE_URL",
        "label": "DeepSeek Base URL",
        "type": "string",
        "group": "Provider: DeepSeek",
        "description": "DeepSeek OpenAI 兼容接口地址。",
        "default": "https://api.deepseek.com/v1",
    },
    {
        "key": "DEEPSEEK_MODEL",
        "label": "DeepSeek 模型",
        "type": "string",
        "group": "Provider: DeepSeek",
        "description": "默认聊天模型。",
        "default": "deepseek-chat",
    },
    {
        "key": "ALIYUN_AI_API_KEY",
        "label": "Aliyun AI API Key",
        "type": "string",
        "group": "Provider: Aliyun",
        "description": "用于 DashScope 鉴权。",
        "default": "",
        "secret": True,
    },
    {
        "key": "ALIYUN_AI_API_BASE_URL",
        "label": "Aliyun Base URL",
        "type": "string",
        "group": "Provider: Aliyun",
        "description": "DashScope OpenAI 兼容接口地址。",
        "default": "https://dashscope.aliyuncs.com/compatible-mode/v1",
    },
    {
        "key": "ALIYUN_AI_MODEL",
        "label": "Aliyun 模型",
        "type": "string",
        "group": "Provider: Aliyun",
        "description": "默认聊天模型。",
        "default": "qwen-plus",
    },
    {
        "key": "PROMPT_NOTICE_SYSTEM",
        "label": "Notice System Prompt",
        "type": "text",
        "group": "提示词",
        "description": "Notice 文本生成统一 system prompt。",
        "default": DEFAULT_NOTICE_SYSTEM_PROMPT,
    },
    {
        "key": "PROMPT_NOTICE_BLOG_TASK",
        "label": "Notice 博客任务提示词",
        "type": "text",
        "group": "提示词",
        "description": "生成博客 HTML 的任务指令。",
        "default": DEFAULT_NOTICE_BLOG_TASK,
    },
    {
        "key": "PROMPT_NOTICE_PODCAST_TASK",
        "label": "Notice 播客任务提示词",
        "type": "text",
        "group": "提示词",
        "description": "生成播客脚本的任务指令。",
        "default": DEFAULT_NOTICE_PODCAST_TASK,
    },
    {
        "key": "PROMPT_NOTICE_POSTER_TASK",
        "label": "Notice 海报任务提示词",
        "type": "text",
        "group": "提示词",
        "description": "生成海报文案的任务指令。",
        "default": DEFAULT_NOTICE_POSTER_TASK,
    },
    {
        "key": "PROMPT_POSTER_SYSTEM",
        "label": "海报 System Prompt",
        "type": "text",
        "group": "提示词",
        "description": "海报提示词提炼阶段的 system prompt。",
        "default": DEFAULT_POSTER_SYSTEM_PROMPT,
    },
    {
        "key": "PROMPT_POSTER_USER_TEMPLATE",
        "label": "海报 User Prompt 模板",
        "type": "text",
        "group": "提示词",
        "description": "支持变量 {records_text}。",
        "default": DEFAULT_POSTER_USER_TEMPLATE,
    },
    {
        "key": "PROMPT_VECTOR_CHAT_SYSTEM",
        "label": "向量问答 System Prompt",
        "type": "text",
        "group": "提示词",
        "description": "首页向量机器人回答时使用的 system prompt。",
        "default": DEFAULT_VECTOR_CHAT_SYSTEM_PROMPT,
    },
]


SETTING_DEFINITION_MAP = {item["key"]: item for item in SETTING_DEFINITIONS}


def _settings_cache() -> dict[str, str]:
    cache = getattr(g, "_benoss_settings_cache", None)
    if cache is None:
        cache = {row.key: row.value for row in AppSetting.query.all()}
        g._benoss_settings_cache = cache
    return cache


def _reset_settings_cache() -> None:
    if hasattr(g, "_benoss_settings_cache"):
        delattr(g, "_benoss_settings_cache")


def _normalize_provider(value: str) -> str:
    raw = str(value or "").strip().lower()
    aliases = {
        "chat_anywhere": "chatanywhere",
        "chat-anywhere": "chatanywhere",
        "dashscope": "aliyun",
    }
    return aliases.get(raw, raw)


def _to_bool(value, *, default: bool = False) -> bool:
    if isinstance(value, bool):
        return value
    text = str(value or "").strip().lower()
    if text in {"1", "true", "yes", "on"}:
        return True
    if text in {"0", "false", "no", "off"}:
        return False
    return bool(default)


def _resolved_raw_value(key: str) -> tuple[object, str]:
    cache = _settings_cache()
    if key in cache:
        return cache.get(key, ""), "override"
    if key in current_app.config:
        return current_app.config.get(key), "config"
    spec = SETTING_DEFINITION_MAP.get(key) or {}
    return spec.get("default", ""), "default"


def _coerce_value(value: object, spec: dict, *, strict: bool) -> object:
    kind = str(spec.get("type") or "string")
    default = spec.get("default")

    if kind == "int":
        text = str(value if value is not None else "").strip()
        if text == "":
            return int(default or 0)
        try:
            number = int(text)
        except Exception as exc:
            if strict:
                raise ValueError(f"{spec['label']} 需要整数") from exc
            return int(default or 0)

        min_v = spec.get("min")
        max_v = spec.get("max")
        if min_v is not None and number < int(min_v):
            if strict:
                raise ValueError(f"{spec['label']} 不能小于 {min_v}")
            number = int(min_v)
        if max_v is not None and number > int(max_v):
            if strict:
                raise ValueError(f"{spec['label']} 不能大于 {max_v}")
            number = int(max_v)
        return number

    if kind == "bool":
        return _to_bool(value, default=bool(default))

    text = str(value if value is not None else "")
    if str(spec.get("normalize") or "") == "provider":
        text = _normalize_provider(text)
    if kind in {"string", "choice"}:
        text = text.strip()
    options = spec.get("options") or []
    if kind == "choice" and options:
        allowed = {str(item.get("value", "")) for item in options}
        if text not in allowed:
            if strict:
                raise ValueError(f"{spec['label']} 选项无效")
            return str(default or "")
    return text


def _serialize_value(value: object, spec: dict) -> str:
    kind = str(spec.get("type") or "string")
    if kind == "bool":
        return "1" if bool(value) else "0"
    return str(value if value is not None else "")


def get_setting_str(key: str, *, default: str = "") -> str:
    spec = SETTING_DEFINITION_MAP.get(key) or {"key": key, "type": "string", "default": default}
    value, _ = _resolved_raw_value(key)
    resolved = _coerce_value(value, spec, strict=False)
    return str(resolved if resolved is not None else "")


def get_setting_int(key: str, *, default: int = 0) -> int:
    spec = SETTING_DEFINITION_MAP.get(key) or {"key": key, "type": "int", "default": default}
    value, _ = _resolved_raw_value(key)
    resolved = _coerce_value(value, spec, strict=False)
    try:
        return int(resolved)
    except Exception:
        return int(default)


def get_setting_bool(key: str, *, default: bool = False) -> bool:
    spec = SETTING_DEFINITION_MAP.get(key) or {"key": key, "type": "bool", "default": default}
    value, _ = _resolved_raw_value(key)
    resolved = _coerce_value(value, spec, strict=False)
    return bool(resolved)


def format_prompt_template(template: str, *, records_text: str) -> str:
    raw = str(template or "")
    try:
        return raw.format(records_text=records_text)
    except Exception:
        return raw + "\n\n记录输入：\n" + records_text


def admin_settings_payload() -> dict:
    groups: OrderedDict[str, list[dict]] = OrderedDict()
    for spec in SETTING_DEFINITIONS:
        key = spec["key"]
        raw_value, source = _resolved_raw_value(key)
        value = _coerce_value(raw_value, spec, strict=False)

        item = {
            "key": key,
            "label": spec.get("label", key),
            "type": spec.get("type", "string"),
            "description": spec.get("description", ""),
            "value": value,
            "source": source,
            "secret": bool(spec.get("secret")),
        }
        if "options" in spec:
            item["options"] = spec["options"]
        if "min" in spec:
            item["min"] = spec["min"]
        if "max" in spec:
            item["max"] = spec["max"]
        if "default" in spec:
            item["default"] = _coerce_value(spec.get("default"), spec, strict=False)

        group_name = str(spec.get("group") or "其他")
        groups.setdefault(group_name, []).append(item)

    return {"groups": [{"name": name, "items": items} for name, items in groups.items()]}


def save_admin_settings(values: dict, *, reset_keys: list[str] | None = None) -> dict:
    reset = [str(item or "").strip() for item in (reset_keys or []) if str(item or "").strip()]
    for key in reset:
        if key not in SETTING_DEFINITION_MAP:
            raise ValueError(f"unknown setting key: {key}")

    for key in reset:
        AppSetting.query.filter_by(key=key).delete()

    cleaned = {str(k): v for k, v in (values or {}).items()}
    for key, raw_value in cleaned.items():
        if key not in SETTING_DEFINITION_MAP:
            raise ValueError(f"unknown setting key: {key}")
        spec = SETTING_DEFINITION_MAP[key]
        resolved = _coerce_value(raw_value, spec, strict=True)
        stored = _serialize_value(resolved, spec)

        row = AppSetting.query.filter_by(key=key).first()
        if not row:
            row = AppSetting(key=key, value=stored)
            db.session.add(row)
        else:
            row.value = stored

    db.session.commit()
    _reset_settings_cache()
    return admin_settings_payload()
