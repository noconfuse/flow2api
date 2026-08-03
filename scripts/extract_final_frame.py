"""Probe the production video-to-final-frame path.

HTTP(S) 输入严格复用生产链路：source URL → FileCache → imageio 文件路径解码。
本地文件输入只跳过 FileCache，直接验证同一个路径解码器。
"""

from __future__ import annotations

import argparse
import asyncio
import sys
import time
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.services.batch_executor import BatchExecutor  # noqa: E402
from src.services.file_cache import FileCache  # noqa: E402


def _build_executor(cache_dir: Path) -> BatchExecutor:
    executor = BatchExecutor.__new__(BatchExecutor)
    executor.generation_handler = SimpleNamespace(
        file_cache=FileCache(cache_dir=str(cache_dir))
    )
    return executor


async def _run(target: str, output_png: Path, cache_dir: Path) -> int:
    started_at = time.monotonic()
    executor = _build_executor(cache_dir)
    is_url = target.startswith(("http://", "https://"))

    try:
        video_path = (
            await executor._cache_video_path(target)
            if is_url
            else Path(target).expanduser().resolve()
        )
        if not video_path.is_file():
            raise FileNotFoundError(f"视频文件不存在: {video_path}")
        png_bytes = await asyncio.to_thread(
            executor._decode_last_frame_from_path,
            str(video_path),
        )
    except Exception as exc:
        print(f"[FAIL] {type(exc).__name__}: {exc}")
        return 1

    if not png_bytes:
        print("[FAIL] 没有解码出视频帧")
        return 2

    output_png.parent.mkdir(parents=True, exist_ok=True)
    await asyncio.to_thread(output_png.write_bytes, png_bytes)
    print(f"[OK] source: {video_path}")
    print(f"[OK] frame:  {output_png} ({len(png_bytes):,} bytes)")
    print(f"[OK] elapsed: {time.monotonic() - started_at:.2f}s")
    return 0


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="验证生产 video → FileCache → final-frame 单一路径"
    )
    parser.add_argument("target", help="本地视频路径或 HTTP(S) source URL")
    parser.add_argument(
        "--out",
        default=str(ROOT / ".dbg" / "final_frame.png"),
        help="PNG 输出路径",
    )
    parser.add_argument(
        "--cache-dir",
        default=str(ROOT / "tmp"),
        help="FileCache 目录",
    )
    return parser.parse_args()


def main() -> int:
    args = _parse_args()
    return asyncio.run(
        _run(
            args.target,
            Path(args.out).expanduser().resolve(),
            Path(args.cache_dir).expanduser().resolve(),
        )
    )


if __name__ == "__main__":
    raise SystemExit(main())
