"""Simple pHash-based image similarity tester.

Public helpers:
    - compute_phash(image_b64) -> str
    - compare_hash_similarity(hash1, hash2) -> float

Usage:
    python test_phash_similarity.py --img1 test1.png --img2 test2.png
"""

from __future__ import annotations

import argparse
import base64
from pathlib import Path

import cv2
import numpy as np


def compute_phash(image_b64: str, size: int = 32, lowfreq: int = 8) -> str:
    """Return the image perceptual hash as a hex string from a base64 image."""
    if not image_b64:
        raise ValueError("Image base64 must not be empty")

    raw = base64.b64decode(image_b64 + "==", validate=False)
    image = cv2.imdecode(np.frombuffer(raw, dtype=np.uint8), cv2.IMREAD_GRAYSCALE)
    if image is None:
        raise ValueError("Failed to decode base64 image")

    resized = cv2.resize(image, (size, size), interpolation=cv2.INTER_AREA)
    dct = cv2.dct(np.float32(resized))
    low = dct[:lowfreq, :lowfreq]

    flat = low.flatten()
    threshold = np.median(flat[1:]) if flat.size > 1 else flat[0]
    bits = (low > threshold).astype(np.uint8).flatten()

    pad_len = (-len(bits)) % 8
    if pad_len:
        bits = np.pad(bits, (0, pad_len), constant_values=0)

    byts = np.packbits(bits)
    return "".join(f"{b:02x}" for b in byts.tolist())


def compare_hash_similarity(hash1: str, hash2: str) -> float:
    """Return similarity in [0, 1] based on normalized Hamming distance."""
    if not hash1 or not hash2:
        raise ValueError("Hash string must not be empty")

    bits1 = np.unpackbits(np.frombuffer(bytes.fromhex(hash1), dtype=np.uint8))
    bits2 = np.unpackbits(np.frombuffer(bytes.fromhex(hash2), dtype=np.uint8))
    if bits1.shape != bits2.shape:
        raise ValueError(f"Bit length mismatch: {bits1.shape} vs {bits2.shape}")
    total = int(bits1.size)
    if total <= 0:
        return 0.0
    dist = int(np.count_nonzero(bits1 != bits2))
    return 1.0 - (dist / total)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Compare two images using perceptual hash (pHash).")
    parser.add_argument("--img1", required=True, help="Path to the first image")
    parser.add_argument("--img2", required=True, help="Path to the second image")
    parser.add_argument("--size", type=int, default=32, help="Resize size before DCT, default 32")
    parser.add_argument("--lowfreq", type=int, default=8, help="Low-frequency DCT block size, default 8")
    return parser.parse_args()


def main() -> None:
    args = parse_args()

    img1_path = str(Path(args.img1).expanduser())
    img2_path = str(Path(args.img2).expanduser())

    with open(img1_path, "rb") as f:
        img1_b64 = base64.b64encode(f.read()).decode("utf-8")
    with open(img2_path, "rb") as f:
        img2_b64 = base64.b64encode(f.read()).decode("utf-8")

    hash1 = compute_phash(img1_b64, size=args.size, lowfreq=args.lowfreq)
    hash2 = compute_phash(img2_b64, size=args.size, lowfreq=args.lowfreq)
    score = compare_hash_similarity(hash1, hash2)
    dist = int(round((1.0 - score) * len(np.unpackbits(np.frombuffer(bytes.fromhex(hash1), dtype=np.uint8)))))

    print(f"img1: {img1_path}")
    print(f"img2: {img2_path}")
    print(f"phash1: {hash1}")
    print(f"phash2: {hash2}")
    print(f"hamming_distance: {dist}")
    print(f"similarity_score: {score:.4f}")
    print(f"judgement: {'very similar' if score >= 0.95 else 'similar' if score >= 0.85 else 'somewhat similar' if score >= 0.70 else 'different'}")


if __name__ == "__main__":
    main()
