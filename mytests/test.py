from pathlib import Path

from uied_runner import run_uied_tool


HERE = Path(__file__).resolve().parent
IMAGE_PATH = HERE / "test.JPG"

raw = run_uied_tool(str(IMAGE_PATH))
print("UIED run finished.")
print(f"image_path={raw['image_path']}")
print(f"output_root={raw['output_root']}")
print(f"ocr_texts={len(raw.get('ocr', {}).get('texts', []))}")
print(f"ip_compos={len(raw.get('ip', {}).get('compos', []))}")
print(f"merge_compos={len(raw.get('merge', {}).get('compos', []))}")
