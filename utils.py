from __future__ import annotations

import io
import json
import os
import random
import time
from typing import Callable, TYPE_CHECKING

import cv2
import numpy as np
from PIL import Image, ImageDraw, ImageFont

if TYPE_CHECKING:
    # Available on newer OpenCV builds; keeps type checkers happy without
    # requiring the attribute at runtime on older versions.
    from cv2.typing import MatLike, Point, Scalar
else:
    MatLike = np.ndarray
    Point = tuple[int, int]
    Scalar = tuple[int, int, int] | tuple[int, int, int, int]

# Configure logging
import logging
logger = logging.getLogger(__name__)

logging.getLogger('PIL').setLevel(logging.ERROR)

def sleep(seconds: float|None = None):
    seconds = seconds if seconds is not None else random.uniform(0.5, 1.5)
    time.sleep(seconds)

def wait_until(func: Callable[[], object], condition: Callable[[object], bool], timeout: float = 10.0, interval: float|None = None) -> object:
    start_time = time.time()
    while time.time() - start_time < timeout:
        if condition(ret := func()):
            return ret
        sleep(interval)
    try:
        raise TimeoutError(f"Timeout exceeded: {timeout} seconds")
    except Exception as e:
        logger.error('While waiting for condition:')
        raise e

def randint(a: int, b: int) -> int:
    return random.randint(a, b)

def rand(a: float = 0.0, b: float = 1.0) -> float:
    return random.uniform(a, b)

def cv2PutText(
    img: MatLike,
    text: str,
    org: Point,
    fontFace: int,
    fontScale: float,
    color: Scalar,
    thickness: int = 1,
    lineType: int = cv2.LINE_8,
    bottomLeftOrigin: bool = False,
) -> MatLike:
    """Draw text onto an OpenCV image while keeping type-compatible hints."""
    if not isinstance(img, np.ndarray):
        raise TypeError("cv2PutText expects an OpenCV image (numpy.ndarray)")

    pil_img = Image.fromarray(cv2.cvtColor(img, cv2.COLOR_BGR2RGB))
    draw = ImageDraw.Draw(pil_img)
    fontStyle = ImageFont.truetype("./simsun.ttc", int(20 * fontScale), encoding="utf-8")
    # Ensure org is a tuple of floats for PIL.ImageDraw.text (typing expects tuple[float, float])
    org_xy = (float(org[0]), float(org[1]))
    draw.text(org_xy, text, color, font=fontStyle, stroke_width=thickness, stroke_fill=color)

    updated = cv2.cvtColor(np.asarray(pil_img), cv2.COLOR_RGB2BGR)
    np.copyto(img, updated)
    return img

def base64_img(path: str) -> str:
    import base64
    with open(path, 'rb') as f:
        return base64.b64encode(f.read()).decode('utf-8')

def base64_imglike(img: MatLike) -> str:
    import base64
    _, buffer = cv2.imencode('.png', img)
    return base64.b64encode(buffer.tobytes()).decode('utf-8')

def suppress_status_bar(image: str|io.BytesIO, status_bar_pixels: int) -> str|io.BytesIO:
    """Suppresses the status bar at the top of the image (base64 or BytesIO)"""
    if isinstance(image, str):
        import base64
        image_data = base64.b64decode(image)
    else:
        image_data = image

    # Ensure we pass a buffer-compatible object to np.frombuffer:
    # io.BytesIO does not implement the buffer protocol directly, but
    # BytesIO.getbuffer() returns a memoryview which does.
    if isinstance(image_data, io.BytesIO):
        buf = image_data.getbuffer()
    else:
        buf = image_data

    nparr = np.frombuffer(buf, np.uint8)
    img = cv2.imdecode(nparr, cv2.IMREAD_COLOR)
    
    img[0:status_bar_pixels, :] = [0, 0, 0]
    
    _, buffer = cv2.imencode('.png', img)
    
    if isinstance(image, str):
        import base64
        return base64.b64encode(buffer.tobytes()).decode('utf-8')
    else:
        return io.BytesIO(buffer.tobytes())

def mkdir(path: str):
    if not os.path.exists(path):
        os.makedirs(path, exist_ok=True)

def run_process(cmd: list[str]) -> str:
    import subprocess
    return subprocess.run(cmd, capture_output=True, text=True, check=True).stdout

def crop_image(image: str|io.BytesIO, x: int, y: int, width: int, height: int) -> io.BytesIO:
    if isinstance(image, str):
        import base64
        image_data = io.BytesIO(base64.b64decode(image))
    else:
        image_data = image
    img = Image.open(image_data)
    img = img.crop((x, y, x + width, y + height))
    # Convert PIL Image to bytes
    img_byte_arr = io.BytesIO()
    img.save(img_byte_arr, format='PNG')
    img_byte_arr.seek(0)
    return img_byte_arr

def _dump_xml(uielements: dict) -> str:
    """
    将 UI 树结构转为 GPT 可读的 XML 字符串
    支持 iOS (type) 和 Android (class) 双平台
    """
    xml_lines = []

    def dump(data: dict, indent: int = 0):
        if not isinstance(data, dict) or 'id' not in data:
            return

        space = "  " * indent

        # 1. 统一获取控件类型：iOS 用 type，Android 用 class
        typ = data.get('type') or data.get('class') or 'Unknown'
        if isinstance(typ, list):
            typ = '|'.join(typ)
        # 取短名（如 Button 而不是 android.widget.Button）
        short_type = typ.split('.')[-1] if '.' in typ else typ.replace('XCUIElementType', '')

        # 2. 统一获取文本
        text = (data.get('text') or data.get('name') or data.get('content_desc') or
                data.get('value') or data.get('ocr_text') or '')
        text = text.strip()
        if len(text) > 50:
            text = text[:47] + '...'

        # 3. 拼接标签
        tag = f"{short_type} id={data['id']}"
        if text:
            tag += f" text=\"{text}\""
        if data.get('icon_label'):
            tag += f" icon=\"{data['icon_label']}\""
        if data.get('ocr_text') and data.get('ocr_text') != text:
            tag += f" ocr=\"{data['ocr_text']}\""

        xml_lines.append(f"{space}<{tag}>")

        # 4. 递归子节点
        for subview in data.get('subviews', []):
            dump(subview, indent + 1)

        # 5. 关闭标签
        xml_lines.append(f"{space}</{short_type}>")

    # 开始遍历
    if isinstance(uielements, dict) and 'elements' in uielements:
        for root in uielements['elements']:
            dump(root)
    elif isinstance(uielements, dict):
        dump(uielements)
    elif isinstance(uielements, list):
        for item in uielements:
            dump(item)

    return "\n".join(xml_lines)

_time_consumed = []
def time_consumed(func):
    import functools, time
    @functools.wraps(func)
    def wrapper(*args, **kwargs):
        start_time = time.time()
        result = func(*args, **kwargs)
        end_time = time.time()
        global _time_consumed
        _time_consumed.append((func.__qualname__, end_time - start_time))
        _time_consumed = _time_consumed[-100:]
        logger.debug(f"Function {func.__qualname__} consumed {end_time - start_time} seconds")
        return result
    return wrapper

def clear_time_consumed():
    global _time_consumed
    _time_consumed.clear()

def get_time_consumed(func_name: str) -> list[float]:
    return [t for n, t in _time_consumed if n == func_name]

_token_record = []
def token_record(func_name: str, input_token: int, output_token: int):
    global _token_record
    _token_record.append((func_name, (input_token, output_token)))
    _token_record = _token_record[-100:]

def clear_token_record():
    global _token_record
    _token_record.clear()

def get_token_consumed(func_name: str) -> list[tuple[int, int]]:
    return [t for n, t in _token_record if n == func_name]

__all__ = ['sleep', 'wait_until', 'cv2PutText', 'rand', 'randint', 'base64_img', 'base64_imglike', 'suppress_status_bar', 'mkdir', 'run_process', 'crop_image', 'time_consumed', 'clear_time_consumed', 'get_time_consumed', 'token_record', 'clear_token_record', 'get_token_consumed']
