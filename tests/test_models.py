"""Model list and model selection, including sub-menus. The picker is simulated (no browser),
using the menu structure from the real page."""
import os
import re
import sys
import threading

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import config  # noqa: E402
import copilot  # noqa: E402
from copilot import CopilotError, CopilotSession  # noqa: E402
from models import model_id, option_name, picker_label, resolve, slug  # noqa: E402


@pytest.fixture(autouse=True)
def defaults(monkeypatch):
    monkeypatch.setattr(config, "MODEL_ID", "m365-copilot")
    monkeypatch.setattr(config, "MODEL_TIMEOUT", 0.6)
    monkeypatch.setattr(config, "MODELS", "")


def test_ids():
    assert slug("Think deeper") == "think-deeper"
    assert slug("GPT-5.6 Sol Quick response") == "gpt-5-6-sol-quick-response"
    assert model_id("Auto") == "m365-copilot-auto"


def test_picker_label_from_your_button():
    assert picker_label("Auto, Model Selector", "Auto") == "Auto"
    assert picker_label("Think deeper, model selector", "") == "Think deeper"
    assert picker_label("", "Quick response\n") == "Quick response"
    assert picker_label("Model Selector", "Auto") == "Auto"


def test_option_name_uses_main_line():
    assert option_name({"content": "Think deeper", "text": "Think deeper\nTakes longer"}) == "Think deeper"
    assert option_name({"content": "", "text": "\n Quick response \nFast answers"}) == "Quick response"
    assert option_name({"content": "", "text": ""}) == ""


def test_resolve():
    names = ["Auto", "Think deeper", "GPT-5.6 Sol Think deeper"]
    keys = {"GPT-5.6 Sol Think deeper": "Gpt_5_6_Reasoning"}
    assert resolve("m365-copilot", names) == (None, True)
    assert resolve("", names) == (None, True)
    assert resolve("m365-copilot-think-deeper", names) == ("Think deeper", True)
    assert resolve("think deeper", names) == ("Think deeper", True)
    assert resolve("m365-copilot-gpt-5-6-sol-think-deeper", names) == ("GPT-5.6 Sol Think deeper", True)
    assert resolve("Gpt_5_6_Reasoning", names, keys) == ("GPT-5.6 Sol Think deeper", True)
    assert resolve("gpt-5-6-reasoning", names, keys) == ("GPT-5.6 Sol Think deeper", True)
    assert resolve("gpt-4o", names, keys) == (None, False)


MAIN = [{"name": "Auto"}, {"name": "Quick response"}, {"name": "Think deeper"},
        {"name": "GPT-5.6 Sol", "submenu": True}]
SUB = [{"name": "GPT-5.6 Sol Quick response", "key": "Gpt_5_6_Chat"},
       {"name": "GPT-5.6 Sol Think deeper", "key": "Gpt_5_6_Reasoning"}]


class Loc:
    def __init__(self, page, sel):
        self.page, self.sel = page, sel

    first = property(lambda self: self)
    last = property(lambda self: self)

    def _target(self):
        m = re.fullmatch(r'\[data-relay-menu="(\w+)"\] \[data-relay-opt="(\d+)"\]', self.sel)
        return (m.group(1), int(m.group(2))) if m else None

    def wait_for(self, **kw):
        pass

    def count(self):
        return 0

    def click(self, **kw):
        p = self.page
        if self.sel == config.MODEL_PICKER_SEL:
            p.main_open, p.sub_open = p.picker_works, False
            return
        which, i = self._target()
        if which == "main" and p.main_open:
            if MAIN[i].get("submenu"):
                p.sub_open = p.sub_opens_on == "click"
            else:
                p.choose(MAIN[i]["name"])
        elif which == "sub" and p.sub_open:
            p.choose(p.sub[i]["name"])

    def hover(self, **kw):
        which, i = self._target()
        if which == "main" and self.page.main_open and MAIN[i].get("submenu") and self.page.sub_opens_on == "hover":
            self.page.sub_open = True

    def focus(self):
        self.page.focused = self._target()


class Keyboard:
    def __init__(self, page):
        self.page = page

    def press(self, key):
        p = self.page
        if key == "Escape":
            if p.sub_open:
                p.sub_open = False
            else:
                p.main_open = False
        elif key == "ArrowRight" and p.sub_opens_on == "arrow" and p.focused and p.focused[0] == "main":
            p.sub_open = bool(MAIN[p.focused[1]].get("submenu"))


class PickerPage:
    def __init__(self, selected="Auto", picker_works=True, selection_sticks=True, sub_opens_on="click",
                 sub_label="full", sub=SUB):
        self.selected, self.main_open, self.sub_open, self.focused = selected, False, False, None
        self.picker_works, self.selection_sticks = picker_works, selection_sticks
        self.sub_opens_on, self.sub_label, self.sub = sub_opens_on, sub_label, sub
        self.url = "https://m365.cloud.microsoft/chat/"
        self.keyboard = Keyboard(self)
        self.menus_opened = 0

    def choose(self, name):
        if self.selection_sticks:
            self.selected = name
        self.main_open = self.sub_open = False

    def label(self):
        if self.sub_label == "short" and self.selected.startswith("GPT-5.6 Sol "):
            return self.selected[len("GPT-5.6 Sol "):]
        return self.selected

    def _opts(self, items):
        return [{"content": o["name"], "text": o["name"] + "\nDescription", "checked": o["name"] == self.selected,
                 "disabled": False, "submenu": bool(o.get("submenu")), "key": o.get("key", "")} for o in items]

    def evaluate(self, script, arg=None):
        if script is copilot.PICKER_JS:
            return {"aria": f"{self.label()}, Model Selector", "text": self.label()}
        if script is copilot.MENU_OPEN_JS:
            return self.main_open or self.sub_open
        if script is copilot.MENU_JS:
            if arg[3] == "main":
                if self.main_open:
                    self.menus_opened += 1
                return self._opts(MAIN) if self.main_open else None
            return self._opts(self.sub) if self.sub_open else None
        raise AssertionError("unexpected script")

    def locator(self, sel):
        return Loc(self, sel)

    def goto(self, url, **kw):
        self.url = url

    def wait_for_load_state(self, *a, **kw):
        pass


def session():
    s = CopilotSession.__new__(CopilotSession)
    s.show_window, s.discover_models, s.name = False, True, "t"
    s._models = {"names": [], "current": None, "fetched": 0.0, "source": "none", "paths": {}, "keys": {}}
    s._models_lock = threading.Lock()
    s._models_refresh = None
    return s


ALL = ["Auto", "Quick response", "Think deeper", "GPT-5.6 Sol Quick response", "GPT-5.6 Sol Think deeper"]


@pytest.mark.parametrize("opens_on", ["click", "hover", "arrow"])
def test_refresh_reads_main_menu_and_submenu(opens_on):
    s, page = session(), PickerPage(selected="Quick response", sub_opens_on=opens_on)
    assert s._refresh_models(page) == ALL
    assert s._models["paths"]["GPT-5.6 Sol Think deeper"] == ["GPT-5.6 Sol"]
    assert s._models["paths"]["Auto"] == []
    assert s.model_keys() == {"GPT-5.6 Sol Quick response": "Gpt_5_6_Chat",
                              "GPT-5.6 Sol Think deeper": "Gpt_5_6_Reasoning"}
    assert s.current_model == "Quick response"
    assert not page.main_open and not page.sub_open


def test_refresh_submenu_that_wont_open_keeps_main_models():
    s, page = session(), PickerPage(sub_opens_on="never")
    assert s._refresh_models(page) == ["Auto", "Quick response", "Think deeper"]
    assert not page.main_open and not page.sub_open


def test_refresh_failure_keeps_old_list():
    s = session()
    s._models["names"] = ["Auto"]
    assert s._refresh_models(PickerPage(picker_works=False)) == ["Auto"]
    assert s._models["fetched"] > 0


def refreshed(**kw):
    s, page = session(), PickerPage(**kw)
    s._refresh_models(page)
    return s, page


def test_select_top_level_model():
    s, page = refreshed()
    s._select_model(page, "think deeper")
    assert page.selected == "Think deeper" and not page.main_open


def test_select_submenu_model():
    s, page = refreshed()
    s._select_model(page, "GPT-5.6 Sol Think deeper")
    assert page.selected == "GPT-5.6 Sol Think deeper" and not page.main_open and not page.sub_open


@pytest.mark.parametrize("opens_on", ["hover", "arrow"])
def test_select_submenu_model_other_open_methods(opens_on):
    s, page = refreshed(sub_opens_on=opens_on)
    s._select_model(page, "GPT-5.6 Sol Quick response")
    assert page.selected == "GPT-5.6 Sol Quick response"


def test_select_submenu_model_button_shows_short_label():
    s, page = refreshed(sub_label="short")
    s._select_model(page, "GPT-5.6 Sol Think deeper")
    assert page.selected == "GPT-5.6 Sol Think deeper"
    s._select_model(page, "Think deeper")
    assert page.selected == "Think deeper"
    s._select_model(page, "GPT-5.6 Sol Think deeper")
    assert page.selected == "GPT-5.6 Sol Think deeper"


def test_select_already_selected_does_not_open_menu():
    s, page = refreshed(selected="GPT-5.6 Sol Quick response")
    page.menus_opened = 0
    s._select_model(page, "GPT-5.6 Sol Quick response")
    assert page.menus_opened == 0


def test_select_without_known_path_searches_submenus():
    s, page = session(), PickerPage()
    s._select_model(page, "GPT-5.6 Sol Think deeper")
    assert page.selected == "GPT-5.6 Sol Think deeper"


def test_select_unknown_model_errors():
    s, page = refreshed()
    with pytest.raises(CopilotError, match="not in the model picker"):
        s._select_model(page, "GPT-9")
    assert not page.main_open and not page.sub_open


def test_select_model_that_does_not_stick_errors():
    s, page = refreshed()
    page.selection_sticks = False
    with pytest.raises(CopilotError, match="still shows"):
        s._select_model(page, "GPT-5.6 Sol Think deeper")


def test_configured_models_override_picker(monkeypatch):
    monkeypatch.setattr(config, "MODELS", "Auto, GPT-5.6 Sol Think deeper ,")
    assert copilot.configured_models() == ["Auto", "GPT-5.6 Sol Think deeper"]
