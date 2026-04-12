"""Smoke-ish tests for AndroidAppiumClient without real device.

These unit tests mock driver methods to validate our wrapper logic. Real e2e
behavior (screenshots, adb) is outside sandbox scope.
"""

import base64
import types

import pytest

from appium_android import AndroidAppiumClient


class DummyDriver:
    def __init__(self):
        self._page_source = "<hierarchy></hierarchy>"
        self.page_source_sequence = []

    @property
    def page_source(self):
        if self.page_source_sequence:
            self._page_source = self.page_source_sequence.pop(0)
        return self._page_source

    # Appium WebDriver interface bits used
    def tap(self, coords):
        self.tapped = coords

    def back(self):
        self.back_called = True

    def find_element(self, by, value):  # pragma: no cover - simple dummy
        el = types.SimpleNamespace()
        el.clear = lambda: None
        el.send_keys = lambda txt: setattr(self, "sent", (by, value, txt))
        el.click = lambda: setattr(self, "clicked", (by, value))
        return el

    def get_screenshot_as_png(self):
        return base64.b64decode(base64.b64encode(b"png"))

    def execute_script(self, *_args, **_kwargs):
        return {"pixelRatio": 2}


def _client_with_dummy():
    client = AndroidAppiumClient("http://localhost:4723")
    client.driver = DummyDriver()
    return client


def test_wait_for_stable_page_returns_last_when_unstable():
    client = _client_with_dummy()
    client.driver.page_source_sequence = ["a", "b", "b"]
    xml = client.wait_for_stable_page(timeout=1, interval=0.01)
    assert xml == "b"


def test_screenshot_base64():
    client = _client_with_dummy()
    assert client.screenshot_base64().startswith("cG5n")


def test_click_and_send_keys():
    client = _client_with_dummy()
    client.click("id", "foo")
    assert client.driver.clicked == ("id", "foo")
    client.send_keys("id", "bar", "text")
    # send_keys focuses the target element (click), so clicked reflects the last focused element
    assert client.driver.clicked == ("id", "bar")
    assert client.driver.sent == ("id", "bar", "text")


def test_send_keys_by_element_id():
    client = _client_with_dummy()
    client.send_keys_by_element_id(123, "hi")
    assert client.driver.sent == ("id", "123", "hi")
