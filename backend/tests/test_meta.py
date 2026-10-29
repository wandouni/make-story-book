"""元数据接口与"默认值"这条不变量。

`/meta/defaults` 存在的唯一理由是**前端不该写死默认版式和底衬不透明度**：
用户点「分页」时发出去的是界面上那个值，前端写死的话改 `config.DEFAULT_PRESET`
界面上不会有任何变化，两边迟早对不上。所以这里钉的是"接口报的就是 config 里
那两个常量"，而不是某个具体的值 —— 默认值本身改了不该让测试红。
"""

import json

import pytest
from fastapi.testclient import TestClient

from app import config
from app.main import app
from app.models.book import PresetPage, PresetPageOptions, TextStyle


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "BOOKS_DIR", tmp_path / "books")
    with TestClient(app) as c:
        yield c


def test_defaults_endpoint_matches_config(client):
    """前端读的就是这几个数，漂了就是界面上悄悄换了个默认版式/字体。"""
    resp = client.get("/api/meta/defaults")
    assert resp.status_code == 200

    body = resp.json()
    assert body["preset"] == config.DEFAULT_PRESET
    assert body["overlayPanelOpacity"] == config.DEFAULT_OVERLAY_PANEL_OPACITY
    assert body["fontFamily"] == config.DEFAULT_FONT_FAMILY


def test_defaults_endpoint_returns_a_real_preset(client):
    """默认版式必须是 PRESETS 里存在的键。

    拼错一个字母的话，前端下拉框选不中任何一项（显示空白），发过去后端也
    渲染不出页面 —— 而这两处的报错都不指向"名字写错了"。
    """
    assert client.get("/api/meta/defaults").json()["preset"] in config.PRESETS


def test_preset_page_defaults_come_from_config():
    """模型默认值走 config 常量，不是各写一份。"""
    assert PresetPage(text="正文。").preset == config.DEFAULT_PRESET
    assert (
        PresetPageOptions().overlayPanelOpacity == config.DEFAULT_OVERLAY_PANEL_OPACITY
    )
    assert PresetPageOptions().fontFamily == config.DEFAULT_FONT_FAMILY
    # 自由页的文字块走的是另一个模型（TextStyle），同样不能各写一份 ——
    # 两处不一致的表现是"预设页是楷体、自由页是宋体"。
    assert TextStyle().fontFamily == config.DEFAULT_FONT_FAMILY


def test_default_font_family_is_self_hosted():
    """默认字体必须是 fonts.json 里有的那个，名字要逐字相同。

    名字写错的话字体下拉选不中它（显示成空白），而页面会静默回退到系统字体 ——
    预览用的是本机字体、headless Chromium 用的是另一套，于是"预览好看、导出跑版"。
    这条测试是那个错误唯一能被挡住的地方。
    """
    manifest = json.loads((config.FONTS_DIR / "fonts.json").read_text(encoding="utf-8"))
    families = {entry["family"] for entry in manifest}
    assert config.DEFAULT_FONT_FAMILY in families


def test_overlay_panel_is_off_by_default():
    """底衬默认关掉是产品决定（见 config 里的说明），别顺手调回去。

    这一条盯的是**行为**：默认满版叠字的页面上不该出现那块半透明白板。
    """
    assert config.DEFAULT_OVERLAY_PANEL_OPACITY == 0
