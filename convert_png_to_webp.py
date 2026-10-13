#!/usr/bin/env python3
"""批量将 PNG 图片转换为 WebP，并同步更新 QMD 中的本地图片引用。

默认扫描脚本所在目录及其子目录：

* 跳过路径中任一部分含有 ``icon``（不区分大小写）的 PNG；
* 使用 Pillow 生成质量为 82 的 WebP；
* 仅把确实指向已转换图片的 QMD 引用由 ``.png`` 改为 ``.webp``；
* 所有 QMD 更新成功后，删除原 PNG。

首次使用建议先运行 ``python convert_png_to_webp.py --dry-run``。
实际转换需要 Pillow：``python -m pip install Pillow``。
"""

from __future__ import annotations

import argparse
import codecs
import os
import re
import stat
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable, Match, Pattern
from urllib.parse import unquote


DEFAULT_QUALITY = 82
IGNORED_DIRECTORIES = {".git", ".venv", "venv", "node_modules", "__pycache__"}

# 匹配常见的 Markdown、YAML 和 HTML 属性中的无空格路径。
TOKEN_PNG_REFERENCE = re.compile(
    r"(?<![\w./\\%+@~-])"
    r"(?P<path>[\\/]?(?:[\w.%+@~-]+[\\/])*[\w.%+@~-]+\.png)"
    r"(?![\w.%+@~-])",
    re.IGNORECASE,
)

# 额外支持引号内含空格的图片路径，例如 image: "my picture.png"。
QUOTED_PNG_REFERENCE = re.compile(
    r"(?P<quote>[\"'])(?P<path>[^\"'\r\n]*?\.png)"
    r"(?=(?:[?#][^\"'\r\n]*)?(?P=quote))",
    re.IGNORECASE,
)

# Markdown 允许用尖括号包住含空格的目标：![说明](<my picture.png>)。
ANGLE_PNG_REFERENCE = re.compile(
    r"<(?P<path>[^<>\r\n]*?\.png)(?=(?:[?#][^<>\r\n]*)?>)",
    re.IGNORECASE,
)

URL_SCHEME = re.compile(r"^[a-z][a-z0-9+.-]*:", re.IGNORECASE)


@dataclass(frozen=True)
class Conversion:
    source: Path
    destination: Path


@dataclass(frozen=True)
class QmdUpdate:
    path: Path
    content: bytes
    reference_count: int


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="将非 icon 的 PNG 转换为 WebP，并更新对应的 QMD 引用。"
    )
    parser.add_argument(
        "--root",
        type=Path,
        default=Path(__file__).resolve().parent,
        help="扫描目录；默认为脚本所在目录。",
    )
    parser.add_argument(
        "--quality",
        type=int,
        default=DEFAULT_QUALITY,
        metavar="0-100",
        help=f"有损 WebP 的质量，默认为 {DEFAULT_QUALITY}。",
    )
    parser.add_argument(
        "--lossless",
        action="store_true",
        help="使用无损 WebP（文件通常会比默认模式更大）。",
    )
    parser.add_argument(
        "--keep-originals",
        action="store_true",
        help="转换后保留原 PNG；QMD 引用仍会更新为 WebP。",
    )
    parser.add_argument(
        "--overwrite",
        action="store_true",
        help="允许覆盖已经存在的同名 WebP。",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="只显示将进行的转换和引用更新，不改动任何文件。",
    )
    return parser.parse_args()


def iter_project_files(root: Path, suffix: str) -> Iterable[Path]:
    """遍历项目文件，避开版本库和常见依赖/缓存目录。"""
    for current, directory_names, file_names in os.walk(root):
        current_path = Path(current)
        directory_names[:] = sorted(
            name
            for name in directory_names
            if name not in IGNORED_DIRECTORIES
            and not (current_path / name).is_symlink()
        )
        for name in sorted(file_names):
            path = current_path / name
            if not path.is_symlink() and path.suffix.casefold() == suffix.casefold():
                yield path


def is_icon(path: Path, root: Path) -> bool:
    """路径的任一部分包含 icon 时视作图标。"""
    return any("icon" in part.casefold() for part in path.relative_to(root).parts)


def relative_display(path: Path, root: Path) -> str:
    return path.relative_to(root).as_posix()


def discover_conversions(
    root: Path, overwrite: bool
) -> tuple[list[Conversion], list[Path], list[Conversion]]:
    conversions: list[Conversion] = []
    icons: list[Path] = []
    conflicts: list[Conversion] = []

    for source in iter_project_files(root, ".png"):
        if is_icon(source, root):
            icons.append(source)
            continue

        conversion = Conversion(source, source.with_suffix(".webp"))
        if conversion.destination.exists() and not overwrite:
            conflicts.append(conversion)
            continue
        conversions.append(conversion)

    return conversions, icons, conflicts


def load_pillow():
    try:
        from PIL import Image, ImageSequence, features
    except ImportError as exc:
        raise RuntimeError(
            "缺少 Pillow，请先运行：python -m pip install Pillow"
        ) from exc

    if not features.check("webp"):
        raise RuntimeError("当前 Pillow 不支持 WebP，请安装带 WebP 支持的 Pillow。")
    return Image, ImageSequence


def image_has_alpha(image) -> bool:
    return "A" in image.getbands() or "transparency" in image.info


def webp_compatible_frame(image):
    mode = "RGBA" if image_has_alpha(image) else "RGB"
    return image.copy() if image.mode == mode else image.convert(mode)


def convert_image(
    conversion: Conversion,
    quality: int,
    lossless: bool,
    overwrite: bool,
    pillow,
) -> int:
    """先写临时文件，再原子替换目标；返回 WebP 文件大小。"""
    Image, ImageSequence = pillow
    source = conversion.source
    destination = conversion.destination

    if destination.exists() and not overwrite:
        raise FileExistsError(f"目标已存在：{destination}")

    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{destination.stem}.", suffix=".webp.tmp", dir=destination.parent
    )
    os.close(descriptor)
    temporary_path = Path(temporary_name)

    save_options = {
        "format": "WEBP",
        "lossless": lossless,
        "quality": quality,
        "method": 6,
    }

    try:
        with Image.open(source) as image:
            if getattr(image, "is_animated", False):
                frames = []
                durations = []
                default_duration = image.info.get("duration", 100)
                for frame in ImageSequence.Iterator(image):
                    durations.append(frame.info.get("duration", default_duration))
                    frames.append(webp_compatible_frame(frame))

                frames[0].save(
                    temporary_path,
                    save_all=True,
                    append_images=frames[1:],
                    duration=durations,
                    loop=image.info.get("loop", 0),
                    **save_options,
                )
            else:
                webp_compatible_frame(image).save(temporary_path, **save_options)

        if temporary_path.stat().st_size == 0:
            raise RuntimeError("生成了空的 WebP 文件。")

        os.chmod(temporary_path, stat.S_IMODE(source.stat().st_mode))
        os.replace(temporary_path, destination)
        return destination.stat().st_size
    finally:
        temporary_path.unlink(missing_ok=True)


def resolve_local_reference(reference: str, qmd: Path, root: Path) -> Path | None:
    """将 QMD 内的相对/根路径解析为项目中的本地文件。"""
    decoded = unquote(reference).replace("\\", "/")

    # 不碰 http:、https:、data:、C: 等带协议/盘符的路径。
    if URL_SCHEME.match(decoded) or decoded.startswith("//"):
        return None

    if decoded.startswith("~/"):
        candidate = root / decoded[2:]
    elif decoded.startswith("/"):
        candidate = root / decoded.lstrip("/")
    else:
        candidate = qmd.parent / decoded

    resolved = candidate.resolve()
    try:
        resolved.relative_to(root)
    except ValueError:
        return None
    return resolved


def replace_pattern(
    text: str,
    pattern: Pattern[str],
    qmd: Path,
    root: Path,
    converted: dict[Path, Path],
) -> tuple[str, int]:
    replacement_count = 0

    def replace(match: Match[str]) -> str:
        nonlocal replacement_count
        reference = match.group("path")
        source = resolve_local_reference(reference, qmd, root)
        if source is None or source not in converted:
            return match.group(0)

        replacement_count += 1
        new_reference = f"{reference[:-4]}.webp"
        path_start = match.start("path") - match.start(0)
        path_end = path_start + len(reference)
        return f"{match.group(0)[:path_start]}{new_reference}{match.group(0)[path_end:]}"

    return pattern.sub(replace, text), replacement_count


def update_qmd_text(
    text: str, qmd: Path, root: Path, converted: dict[Path, Path]
) -> tuple[str, int]:
    # 先处理有边界的路径，使带空格的文件名不会被拆成最后一个单词。
    text, angle_count = replace_pattern(
        text, ANGLE_PNG_REFERENCE, qmd, root, converted
    )
    text, quoted_count = replace_pattern(
        text, QUOTED_PNG_REFERENCE, qmd, root, converted
    )
    text, token_count = replace_pattern(text, TOKEN_PNG_REFERENCE, qmd, root, converted)
    return text, angle_count + quoted_count + token_count


def decode_qmd(content: bytes, path: Path) -> tuple[str, Callable[[str], bytes]]:
    if content.startswith(codecs.BOM_UTF8):
        return content.decode("utf-8-sig"), lambda text: text.encode("utf-8-sig")
    try:
        return content.decode("utf-8"), lambda text: text.encode("utf-8")
    except UnicodeDecodeError as exc:
        raise RuntimeError(f"QMD 不是有效的 UTF-8 文件：{path}") from exc


def plan_qmd_updates(
    root: Path, converted: dict[Path, Path]
) -> list[QmdUpdate]:
    updates: list[QmdUpdate] = []
    for qmd in iter_project_files(root, ".qmd"):
        original = qmd.read_bytes()
        text, encode = decode_qmd(original, qmd)
        updated_text, count = update_qmd_text(text, qmd, root, converted)
        if count:
            updates.append(QmdUpdate(qmd, encode(updated_text), count))
    return updates


def atomic_write(path: Path, content: bytes) -> None:
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    temporary_path = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as temporary_file:
            temporary_file.write(content)
            temporary_file.flush()
            os.fsync(temporary_file.fileno())
        os.chmod(temporary_path, stat.S_IMODE(path.stat().st_mode))
        os.replace(temporary_path, path)
    finally:
        temporary_path.unlink(missing_ok=True)


def format_size(size: int) -> str:
    value = float(size)
    for unit in ("B", "KiB", "MiB", "GiB"):
        if value < 1024 or unit == "GiB":
            return f"{value:.1f} {unit}"
        value /= 1024
    raise AssertionError("unreachable")


def print_plan(
    root: Path,
    conversions: list[Conversion],
    icons: list[Path],
    conflicts: list[Conversion],
    qmd_updates: list[QmdUpdate],
) -> None:
    for conversion in conversions:
        print(
            f"[将转换] {relative_display(conversion.source, root)} -> "
            f"{relative_display(conversion.destination, root)}"
        )
    for update in qmd_updates:
        print(
            f"[将更新] {relative_display(update.path, root)} "
            f"({update.reference_count} 处引用)"
        )
    for path in icons:
        print(f"[跳过 icon] {relative_display(path, root)}")
    for conversion in conflicts:
        print(
            f"[跳过冲突] {relative_display(conversion.destination, root)} 已存在；"
            "如需覆盖请使用 --overwrite"
        )


def main() -> int:
    args = parse_args()
    root = args.root.expanduser().resolve()

    if not root.is_dir():
        print(f"错误：扫描目录不存在或不是目录：{root}", file=sys.stderr)
        return 2
    if not 0 <= args.quality <= 100:
        print("错误：--quality 必须在 0 到 100 之间。", file=sys.stderr)
        return 2

    conversions, icons, conflicts = discover_conversions(root, args.overwrite)
    planned_mapping = {
        conversion.source.resolve(): conversion.destination.resolve()
        for conversion in conversions
    }

    try:
        planned_qmd_updates = plan_qmd_updates(root, planned_mapping)
    except (OSError, RuntimeError) as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return 1

    if args.dry_run:
        print_plan(root, conversions, icons, conflicts, planned_qmd_updates)
        print(
            f"\n预演完成：将转换 {len(conversions)} 张图片，"
            f"更新 {len(planned_qmd_updates)} 个 QMD；未改动文件。"
        )
        return 0

    if not conversions:
        print_plan(root, conversions, icons, conflicts, planned_qmd_updates)
        print("没有需要转换的 PNG。")
        return 0

    try:
        pillow = load_pillow()
    except RuntimeError as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return 1

    successful: list[Conversion] = []
    conversion_errors = 0
    original_bytes = 0
    webp_bytes = 0

    for conversion in conversions:
        source_size = conversion.source.stat().st_size
        try:
            destination_size = convert_image(
                conversion,
                quality=args.quality,
                lossless=args.lossless,
                overwrite=args.overwrite,
                pillow=pillow,
            )
        except Exception as exc:  # 单个坏图不应阻止其他图片转换。
            conversion_errors += 1
            print(
                f"[转换失败] {relative_display(conversion.source, root)}：{exc}",
                file=sys.stderr,
            )
            continue

        successful.append(conversion)
        original_bytes += source_size
        webp_bytes += destination_size
        print(
            f"[已转换] {relative_display(conversion.source, root)} -> "
            f"{relative_display(conversion.destination, root)} "
            f"({format_size(source_size)} -> {format_size(destination_size)})"
        )

    successful_mapping = {
        conversion.source.resolve(): conversion.destination.resolve()
        for conversion in successful
    }

    try:
        qmd_updates = plan_qmd_updates(root, successful_mapping)
        for update in qmd_updates:
            atomic_write(update.path, update.content)
            print(
                f"[已更新] {relative_display(update.path, root)} "
                f"({update.reference_count} 处引用)"
            )
    except (OSError, RuntimeError) as exc:
        print(
            f"错误：更新 QMD 失败，原 PNG 均未删除：{exc}", file=sys.stderr
        )
        return 1

    deletion_errors = 0
    if not args.keep_originals:
        for conversion in successful:
            try:
                conversion.source.unlink()
                print(f"[已删除] {relative_display(conversion.source, root)}")
            except OSError as exc:
                deletion_errors += 1
                print(
                    f"[删除失败] {relative_display(conversion.source, root)}：{exc}",
                    file=sys.stderr,
                )

    saved_bytes = original_bytes - webp_bytes
    if original_bytes:
        saved_percent = saved_bytes / original_bytes * 100
        size_summary = (
            f"，总体 {format_size(original_bytes)} -> {format_size(webp_bytes)}，"
            f"节省 {saved_percent:.1f}%"
        )
    else:
        size_summary = ""

    print(
        f"\n完成：转换 {len(successful)} 张图片，更新 {len(qmd_updates)} 个 QMD"
        f"{size_summary}。跳过 icon {len(icons)} 张、冲突 {len(conflicts)} 张。"
    )
    return 1 if conversion_errors or deletion_errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
