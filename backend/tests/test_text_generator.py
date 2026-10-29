"""`chat_json`：向模型要一段 JSON。

这个功能的失败模式**全在解析和兼容性上**，不在"请求发出去没有"：

- 端点不认 `response_format`（400）→ 不能整批任务跟着死
- 模型在 JSON 模式里照样加 ` ```json ` 围栏 → 不能解析失败
- 模型压根没听懂，回一段话 → 必须干净地返回 None 让调用方降级，
  而不是抛出去把整个智能排版变成一次失败

所以这里不碰网络：`_get_client` 换成假的，只断言**发出去的 payload** 和
**拿到回复后的行为**。真的调用一次 API 既慢又不可重复。
"""

import httpx
import pytest

from app import config
from app.services import text_generator


class _Response:
    """够用的 httpx.Response 替身：只要 status_code / json / raise_for_status。"""

    def __init__(self, content: str | None = None, status: int = 200):
        self.status_code = status
        self._content = content

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise httpx.HTTPStatusError(
                f"HTTP {self.status_code}",
                request=httpx.Request("POST", "http://test/chat/completions"),
                response=self,  # type: ignore[arg-type]
            )

    def json(self) -> dict:
        return {"choices": [{"message": {"content": self._content}}]}


class _Client:
    """按顺序吐预设响应，同时记下每一次的 payload。

    用完之后一直复用最后一个 —— 重试路径里同一份响应会被要好几遍。
    """

    def __init__(self, responses: list):
        self.responses = list(responses)
        self.payloads: list[dict] = []
        self.raises: list[Exception] = []

    async def post(self, url, headers=None, json=None, timeout=None):
        self.payloads.append(json)
        if self.raises:
            raise self.raises.pop(0)
        if len(self.responses) > 1:
            return self.responses.pop(0)
        return self.responses[0]


@pytest.fixture
def fake(monkeypatch):
    """装上假客户端，返回它 —— 断言 payload 用它，断言调用次数用 len(payloads)。"""

    def _install(*responses) -> _Client:
        client = _Client(list(responses))
        monkeypatch.setattr(text_generator, "_get_client", _make_getter(client))
        return client

    return _install


def _make_getter(client: _Client):
    async def _get() -> _Client:
        return client

    return _get


@pytest.fixture(autouse=True)
def _isolate(monkeypatch):
    """每个用例都从"端点支持 response_format"这个初始状态开始。

    `_json_mode_unsupported` 是模块级的，不隔离的话用例顺序会改变结果 ——
    而且症状是"单独跑绿、全量跑红"，最难查的那种。
    """
    monkeypatch.setattr(text_generator, "_json_mode_unsupported", False)
    monkeypatch.setattr(config, "API_KEY", "test-key")
    monkeypatch.setattr(config, "TEXT_MAX_RETRIES", 1)  # 重试要 sleep，测试里不要


# ---------------------------------------------------------------- 解析


@pytest.mark.parametrize(
    "raw,expected",
    [
        ('{"页":[{"页号":1}]}', {"页": [{"页号": 1}]}),
        # 模型在 JSON 模式里照样加围栏 —— 这是最常见的形状
        ('```json\n{"a": 1}\n```', {"a": 1}),
        ('```\n{"a": 1}\n```', {"a": 1}),
        # 前后有废话
        ('好的，这是结果：\n{"a": 1}\n希望有帮助！', {"a": 1}),
        ('[{"a": 1}, {"b": 2}]', [{"a": 1}, {"b": 2}]),
        # 字符串里的括号不能提前收尾
        ('{"颜色": "白}色", "n": 2}', {"颜色": "白}色", "n": 2}),
        ('{"颜色": "白\\"}色"}', {"颜色": '白"}色'}),
        ('  \n {"a": 1}  \n ', {"a": 1}),
        # 嵌套：只按最外层括号配平
        ('{"a": [1, {"b": 2}]}', {"a": [1, {"b": 2}]}),
    ],
)
def test_parse_json_reply_accepts_what_models_actually_send(raw: str, expected):
    assert text_generator._parse_json_reply(raw) == expected


@pytest.mark.parametrize(
    "raw",
    [
        "",
        "   ",
        "抱歉，我无法完成这个请求。",
        "3",  # 裸标量：调用方要按 key 取字段，放过去只会在更远的地方炸
        '"一段话"',
        '{"a": 1',  # 括号不配平
        "null",
        "true",
    ],
)
def test_parse_json_reply_rejects_everything_else(raw: str):
    assert text_generator._parse_json_reply(raw) is None


# ---------------------------------------------------------------- response_format


async def test_chat_json_asks_for_json_first(fake):
    client = fake(_Response('{"框号": 3}'))
    assert await text_generator.chat_json("sys", "user") == {"框号": 3}
    assert client.payloads[0]["response_format"] == {"type": "json_object"}


async def test_plain_chat_never_sends_response_format(fake):
    """`chat` 是给生图提示词用的，要的是散文。别顺手给它加 JSON 模式。"""
    client = fake(_Response("一只小兔子"))
    assert await text_generator.chat("sys", "user") == "一只小兔子"
    assert "response_format" not in client.payloads[0]


async def test_chat_json_drops_response_format_when_the_endpoint_rejects_it(fake):
    """400 之后去掉重试一次，而且**记住**了。

    不记住的话，30 页的智能排版要挨 30 次 400，每次都白花一个来回。
    """
    client = fake(_Response(status=400), _Response('{"ok": 1}'))

    assert await text_generator.chat_json("sys", "user") == {"ok": 1}
    assert len(client.payloads) == 2
    assert "response_format" in client.payloads[0]
    assert "response_format" not in client.payloads[1]
    assert text_generator._json_mode_unsupported is True

    # 第二次调用直接不带 —— 不再探一遍
    assert await text_generator.chat_json("sys", "user") == {"ok": 1}
    assert len(client.payloads) == 3
    assert "response_format" not in client.payloads[2]


async def test_chat_json_does_not_swallow_other_http_errors(fake):
    """500 是服务端的事，不是"这个端点不认 response_format"。

    当成后者处理的话，一次 500 会让**后续所有**调用永久降级成无 JSON 模式，
    而且日志里一句"端点不支持"把真正的原因盖掉了。
    """
    fake(_Response(status=500))
    with pytest.raises(httpx.HTTPStatusError):
        await text_generator.chat_json("sys", "user")
    assert text_generator._json_mode_unsupported is False


# ---------------------------------------------------------------- 降级与抛出


async def test_chat_json_returns_none_instead_of_raising_on_a_bad_reply(fake):
    """模型没听懂时返回 None —— 调用方降级到纯算法，用户仍然拿到结果。"""
    fake(_Response("抱歉，我不能输出 JSON。"))
    assert await text_generator.chat_json("sys", "user") is None


async def test_chat_json_still_raises_when_the_model_is_unreachable(fake):
    """传输层连续失败**要抛**。

    和上一条的区别是"这一页没配好"与"模型根本没连上"。后者静默降级会把
    "模型挂了"伪装成"模型没配"，用户看到的是一本书排版正常但完全没被智能配置。
    """
    client = fake(_Response("x"))
    client.raises = [httpx.ConnectError("boom")]
    with pytest.raises(text_generator.TextGeneratorError):
        await text_generator.chat_json("sys", "user")


async def test_missing_key_raises_for_both_entry_points(fake, monkeypatch):
    """没配 key 是全局问题，两个入口都要抛 —— 由调用方决定怎么提示用户。"""
    monkeypatch.setattr(config, "API_KEY", "")
    fake(_Response('{"a": 1}'))
    with pytest.raises(text_generator.TextGeneratorError):
        await text_generator.chat("sys", "user")
    with pytest.raises(text_generator.TextGeneratorError):
        await text_generator.chat_json("sys", "user")


async def test_temperature_defaults_to_a_discriminative_value(fake):
    """选框是判别任务不是创作，默认温度要低。传错了会让模型"发挥"。"""
    client = fake(_Response('{"a": 1}'))
    await text_generator.chat_json("sys", "user")
    assert client.payloads[0]["temperature"] == 0.2
