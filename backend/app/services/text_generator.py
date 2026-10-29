"""LLM 调用（OpenAI 兼容的 chat/completions）。

两个用途：

1. 把中文页文本转成中文生图提示词（`chat`）
2. 让模型做**判别**类决策并回一段 JSON（`chat_json`，智能排版选落点用它）

**分页绝不用 LLM** —— 绘本必须逐字保留原文，模型会顺手润色改写
（见 services/paginator.py 的说明）。

提示词用中文而不是英文：绘本是给中国儿童看的，中文提示词里再叮嘱"画面不要
出现文字"，模型才不会在画面上写一串英文标题。

移植自 StoryCraft 的 text_generator，改动是把 httpx.Client 换成 AsyncClient、
time.sleep 换成 asyncio.sleep —— 同步调用会阻塞整个事件循环，生图任务和
SSE 推送都会卡住。
"""

from __future__ import annotations

import asyncio
import json
import re
from typing import Any, Dict, List, Optional

import httpx

from app import config
from app.logger import get_logger

logger = get_logger()

_client: Optional[httpx.AsyncClient] = None
_client_lock = asyncio.Lock()


async def _get_client() -> httpx.AsyncClient:
    global _client
    async with _client_lock:
        if _client is None or _client.is_closed:
            _client = httpx.AsyncClient()
        return _client


async def close_client() -> None:
    global _client
    if _client is not None and not _client.is_closed:
        await _client.aclose()
    _client = None


def _timeout(read_timeout: float) -> httpx.Timeout:
    """连接/写入保持短超时以便快速失败，读超时放宽 ——
    推理模型批量生成提示词时响应可能远超 60s，读超时太短会被误杀。"""
    return httpx.Timeout(connect=30.0, read=read_timeout, write=60.0, pool=30.0)


class TextGeneratorError(RuntimeError):
    pass


async def _post_chat(
    system: str, user: str, temperature: float, *, json_mode: bool = False
) -> str:
    """调一次 chat/completions，返回正文文本。

    读超时随重试次数翻倍（300s → 600s → 900s），网络抖动时不至于整批任务失败。

    `json_mode` 加上 `response_format`。**HTTP 错误码不在这里吞**：400/422 要留给
    `chat_json` 判断"这个端点不认 response_format"，见那里的说明。
    """
    if not config.API_KEY:
        raise TextGeneratorError(
            "未配置 API_KEY，无法调用文本模型。请在 backend/.env 里填写。"
        )

    client = await _get_client()
    payload: Dict[str, Any] = {
        "model": config.TEXT_MODEL,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        "temperature": temperature,
    }
    if json_mode:
        payload["response_format"] = {"type": "json_object"}
    url = f"{config.API_ENDPOINT}/chat/completions"
    headers = {"Authorization": f"Bearer {config.API_KEY}", "Content-Type": "application/json"}

    last_error: Exception | None = None
    for attempt in range(config.TEXT_MAX_RETRIES):
        read_timeout = config.TEXT_TIMEOUT * (attempt + 1)
        try:
            logger.info("调用文本 API（第 %d 次，读超时 %ss）", attempt + 1, read_timeout)
            response = await client.post(
                url, headers=headers, json=payload, timeout=_timeout(read_timeout)
            )
            response.raise_for_status()
            data = response.json()
            return data["choices"][0]["message"]["content"].strip()
        except (httpx.TimeoutException, httpx.TransportError) as exc:
            last_error = exc
            logger.warning("文本 API 第 %d 次失败: %s", attempt + 1, exc)
            if attempt < config.TEXT_MAX_RETRIES - 1:
                await asyncio.sleep(2 * (attempt + 1))
        except (KeyError, IndexError, ValueError) as exc:
            # 响应结构不对，重试大概率还是不对，直接抛
            raise TextGeneratorError(f"文本 API 响应格式异常: {exc}") from exc

    raise TextGeneratorError(f"文本 API 连续失败 {config.TEXT_MAX_RETRIES} 次: {last_error}")


async def chat(system: str, user: str, temperature: float = 0.7) -> str:
    """调一次 chat/completions，返回正文文本。"""
    return await _post_chat(system, user, temperature)


# ---------------------------------------------------------------- JSON 输出

# 端点不认 `response_format` 时置上，之后不再白挨一次 400。
#
# 模块级而不是每次现探：这个能力是**端点**的属性，不是请求的属性 —— 一次不行
# 就永远不行。不记住的话，30 页的智能排版会挨 30 次 400，每次都多花一个来回。
_json_mode_unsupported = False

# ```json ... ``` 围栏。模型在 JSON 模式下仍然经常加，必须剥掉。
_FENCE = re.compile(r"^```[A-Za-z0-9_-]*\s*|\s*```$")


def _first_json_value(text: str) -> str | None:
    """抠出第一个**括号配平**的 `{...}` 或 `[...]`。

    字符串里的括号要跳过（`{"颜色": "白}"}` 这种不能提前收尾），转义也要认。
    只按最外层那个括号配平 —— 里面的 `[]` 交给 `json.loads`。
    """
    start = next((i for i, ch in enumerate(text) if ch in "{["), None)
    if start is None:
        return None

    opener, closer = ("{", "}") if text[start] == "{" else ("[", "]")
    depth = 0
    in_string = False
    escaped = False
    for i in range(start, len(text)):
        ch = text[i]
        if in_string:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                in_string = False
            continue
        if ch == '"':
            in_string = True
        elif ch == opener:
            depth += 1
        elif ch == closer:
            depth -= 1
            if depth == 0:
                return text[start : i + 1]
    return None


def _parse_json_reply(text: str) -> Any | None:
    """从模型回复里解析出 JSON 对象或数组。解析不出来返回 None。

    三件事都要忍，它们都是实测会发生的：

    1. ` ```json ` 围栏 —— 就算带上了 `response_format` 也会加
    2. 前后有废话（"好的，这是结果："）—— 配平扫描抠出来
    3. 返回的不是 JSON（模型没听懂），或者是个裸字符串/数字 —— 一律当失败

    **只认 dict 和 list**：调用方要按 key 取字段，一个裸的 `"3"` 放过去只会
    在更远的地方炸，那时已经看不出是模型的问题了。
    """
    if not text or not text.strip():
        return None
    body = _FENCE.sub("", text.strip()).strip()
    for candidate in (body, _first_json_value(body)):
        if not candidate:
            continue
        try:
            parsed = json.loads(candidate)
        except ValueError:
            continue
        if isinstance(parsed, (dict, list)):
            return parsed
    return None


async def chat_json(system: str, user: str, temperature: float = 0.2) -> Any | None:
    """要一段 JSON，返回解析好的对象；**解析失败返回 None，不抛**。

    抛与不抛的分工是刻意的：

    - 解析失败 → None。调用方（智能排版）降级到纯算法，用户仍然拿到一个结果
    - 没配 key / 传输层连续失败 → 抛 `TextGeneratorError`。那是整单该中止的事，
      不是"这一页没配好"，静默降级会把"模型挂了"伪装成"模型没配"

    `temperature=0.2`：这是判别任务（挑哪个框、给什么颜色），不是创作，不要发挥。
    """
    global _json_mode_unsupported

    use_json_mode = not _json_mode_unsupported
    try:
        raw = await _post_chat(system, user, temperature, json_mode=use_json_mode)
    except httpx.HTTPStatusError as exc:
        # 有些兼容端点根本不认 response_format，直接 400/422。去掉重试一次，
        # 并记下来 —— 见 _json_mode_unsupported 的说明。
        if not use_json_mode or exc.response.status_code not in (400, 422):
            raise
        logger.warning("端点不支持 response_format（%s），去掉重试", exc.response.status_code)
        _json_mode_unsupported = True
        raw = await _post_chat(system, user, temperature, json_mode=False)

    parsed = _parse_json_reply(raw)
    if parsed is None:
        logger.warning("模型返回的不是 JSON，交给调用方降级：%s", raw[:200])
    return parsed


# ---------------------------------------------------------------- 提示词生成

_SYSTEM = "你是一个专业的儿童绘本插画设计师，擅长为儿童故事创作温馨、生动的AI绘画提示词。"

_NUMBERED_LINE = re.compile(r"^\s*\d+[.、)]\s*")


def _clean_lines(text: str) -> List[str]:
    """模型经常不听话地加上序号或 markdown 列表符，统一剥掉。

    只剥行首的编号/项目符号，不动正文 —— 提示词里出现数字是正常的。
    """
    lines = []
    for raw in text.splitlines():
        line = _NUMBERED_LINE.sub("", raw.strip())
        line = line.strip().strip("*`").strip()
        if line:
            lines.append(line)
    return lines


def _design_line(design_hints: Optional[List[str]], index: int) -> str:
    """取第 index 页的镜头要求，拼成要写进 LLM 指令的一行。"""
    if not design_hints or index >= len(design_hints):
        return ""
    hint = design_hints[index].strip()
    if not hint:
        return ""
    return f"\n   这一页的镜头要求（必须体现在画面里）：{hint}"


async def generate_prompts_batch(
    texts: List[str],
    character_description: str = "",
    design_hints: Optional[List[str]] = None,
) -> List[str]:
    """批量把中文页文本转成中文生图提示词，顺序与输入一致。

    `design_hints` 与 `texts` 一一对应，是每一页该用的镜头语言（景别、机位、
    光线、画面结构）。传进来是**已经拼好的一句话**，语序由
    prompt_builder.design_hint 统一决定 —— 在这里再拼一遍就成了第二套规则。

    把镜头要求交给模型，是为了让**画面内容本身**契合（远景就真的把人物画小），
    而不是只在提示词末尾贴一句"远景"跟模型写的中景打架。最终提示词里仍会
    再带一次，见 prompt_builder.enhance_prompt。

    数量对不上时降级为逐条生成 —— 批量请求便宜但容易少给一行，
    少给一行就会让后面所有页的图错位一页，这是必须挡住的事。
    """
    if not texts:
        return []

    scenes = "\n".join(
        f"{i + 1}. {text}{_design_line(design_hints, i)}" for i, text in enumerate(texts)
    )
    character_note = (
        f"\n\n**角色固定特征（必须出现在每一条提示词里）**：\n{character_description}"
        if character_description.strip()
        else ""
    )

    prompt = f"""请为以下儿童绘本的每一页生成一条中文 AI 绘画提示词。

原文（每行一页）：
{scenes}
{character_note}

要求：
1. 必须用中文，不要出现任何英文单词、字母或数字
2. 每条要描述画面内容、角色动作、表情、场景；**若该页给了镜头要求，画面必须照着那个景别、视角、光线和画面结构来写**（比如要求远景就把人物画小、要求特写就只拍脸，不要每页都写成一样的构图）
3. 各页的镜头要有明显区别，不要用同一套描述套在每一页上
4. 若给了角色固定特征，每条提示词都必须包含，保证各页角色长相一致
5. 画面温馨、明亮、色彩丰富，适合儿童绘本
6. 画面里只画场景和角色，**不要出现任何文字**（标题、招牌、书名、标语、字母都不要写）
7. 严格按原文顺序，**每页输出一行**，共 {len(texts)} 行
8. 只输出提示词本身，不要序号、不要解释、不要空行

中文提示词："""

    raw = await chat(_SYSTEM, prompt, temperature=0.7)
    lines = _clean_lines(raw)

    if len(lines) != len(texts):
        logger.warning(
            "提示词数量 %d 与页数 %d 不匹配，降级为逐页生成", len(lines), len(texts)
        )
        # 降级路径也要带上镜头要求：只写在批量指令里的话，一旦降级
        # "各页镜头要有区别"这条约束就静默失效了，而那正是这个功能的目的
        return [
            await generate_prompt_single(
                text,
                character_description,
                (design_hints[i] if design_hints and i < len(design_hints) else ""),
            )
            for i, text in enumerate(texts)
        ]

    return lines


async def generate_prompt_single(
    text: str, character_description: str = "", design_hint: str = ""
) -> str:
    """单页生成。**看不到邻居页**，所以"各页镜头要有区别"这条在这里做不到 ——
    镜头要求由调用方按页码算好传进来（页码是跨页信息的唯一来源）。"""
    character_note = (
        f"\n\n角色固定特征（必须包含）：{character_description}"
        if character_description.strip()
        else ""
    )
    design_note = (
        f"\n这一页的镜头要求（必须体现在画面里）：{design_hint}"
        if design_hint.strip()
        else ""
    )
    prompt = f"""请为以下儿童绘本的一页生成一条中文 AI 绘画提示词。

页面内容：{text}
{character_note}
{design_note}

要求：
1. 必须用中文，不要出现任何英文单词、字母或数字
2. 描述画面内容、角色动作、表情、场景；若给了镜头要求，画面必须照着它的景别、视角、光线和画面结构来写
3. 若给了角色固定特征，必须包含
4. 温馨、明亮、色彩丰富，适合儿童绘本
5. 画面里只画场景和角色，**不要出现任何文字**（标题、招牌、书名、标语、字母都不要写）
6. 只输出提示词本身，不要序号、不要解释

中文提示词："""
    return (await chat(_SYSTEM, prompt, temperature=0.7)).strip()


async def refine_prompt(original: str, instruction: str) -> str:
    """按用户的自然语言要求改写一条提示词。"""
    prompt = f"""下面是一条儿童绘本插画的中文提示词：

{original}

请按这个要求修改它：{instruction}

要求：
1. 仍然用中文，不要出现英文单词、字母或数字
2. 保持儿童绘本风格
3. 画面里不要出现任何文字（标题、招牌、书名、字母都不要）
4. 只输出修改后的提示词，不要任何解释

修改后的提示词："""
    return (await chat(_SYSTEM, prompt, temperature=0.7)).strip()
