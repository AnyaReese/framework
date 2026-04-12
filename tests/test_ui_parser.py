from appium_android import AndroidAppiumClient
from ui_cls import BaseUI


def test_parse_simple_hierarchy():
    xml = """
    <hierarchy>
      <node class="android.widget.FrameLayout" bounds="[0,0][100,200]">
        <node class="android.widget.Button" text="OK" bounds="[10,10][50,40]" />
      </node>
    </hierarchy>
    """
    client = AndroidAppiumClient(server_url="http://localhost")
    uist = client.parse_xml_to_uist(xml, pixel_ratio=2)
    uist2, vid_map = BaseUI.post_process_ui(uist, screenshot_b64="")

    assert len(uist2["elements"]) == 1
    root = uist2["elements"][0]
    assert root["class"] == "android.widget.FrameLayout"
    child = root["subviews"][0]
    assert child["text"] == "OK"
    assert "absolute_frame" in child
    # We only assign ids for clickables (or sparse, labeled anchors).
    assert len(vid_map) >= 1
