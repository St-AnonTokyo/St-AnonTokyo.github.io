#!/usr/bin/env python3
"""批量将常见图片格式转换为 WebP，并同步更新网站中的本地引用。

默认扫描脚本所在目录及其子目录：

* 支持 PNG/APNG、JPEG、GIF、BMP、TIFF、ICO、AVIF、SVG 和 HEIC/HEIF；
* 跳过路径中任一部分含有 ``icon``（不区分大小写）的图片；
* 使用 Pillow 生成质量为 82 的 WebP；
* 更新 Quarto、Markdown、HTML、CSS、XML 等文件中指向已转换图片的引用；
* 所有引用更新成功后，默认删除原图。

首次使用建议先运行 ``python convert.py --dry-run``。
基础依赖：``python -m pip install "Pillow>=11.3"``。
转换 SVG 和 HEIC/HEIF 时还需要：``python -m pip install resvg-py pillow-heif``。
"""

from __future__ import annotations

import argparse
import codecs
import io
import os
import re
import stat
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable, Match, Pattern
from urllib.parse import unquote, urlsplit


DEFAULT_QUALITY = 82
IGNORED_DIRECTORIES = {
    ".git", ".quarto", ".venv", "venv", "node_modules", "__pycache__", "site_libs",
}
IMAGE_SUFFIXES = {
    ".png", ".apng", ".jpg", ".jpeg", ".jpe", ".jfif", ".gif", ".bmp", ".dib",
    ".tif", ".tiff", ".ico", ".avif", ".svg", ".svgz", ".heic", ".heif",
}
REFERENCE_SUFFIXES = {
    ".qmd", ".md", ".markdown", ".yaml", ".yml", ".html", ".htm", ".css",
    ".scss", ".xml", ".json",
}
IMAGE_EXTENSION = "(?:" + "|".join(map(re.escape, sorted(IMAGE_SUFFIXES))) + ")"

# 匹配常见的 Markdown、YAML 和 HTML 属性中的无空格路径。
TOKEN_PATH = (
    r"(?<![\w./\\%+@~!:#?&-])"
    r"(?P<token>(?:[a-z][a-z0-9+.-]*:)?(?:[\\/]{1,2})?"
    rf"(?:[\w.%+@~!:-]+[\\/])*[\w.%+@~!-]+{IMAGE_EXTENSION})"
    r"(?![\w.%+@~!/\\-])"
)
URL_SUFFIX = r"[?#][^\s<>\"'(),]*"
SRCSET_REFERENCE = re.compile(rf"{TOKEN_PATH}|{URL_SUFFIX}", re.IGNORECASE)
REFERENCE = re.compile(
    # 引号和 Markdown 尖括号内允许空格，查询参数和锚点作为整体跳过。
    rf"(?P<quote>[\"'])(?P<quoted>[^\"'\r\n?#]*?{IMAGE_EXTENSION})(?:[?#][^\"'\r\n]*)?(?P=quote)"
    rf"|<(?P<angled>[^<>\r\n=?#]*?{IMAGE_EXTENSION})(?:[?#][^<>\r\n]*)?>"
    rf"|{TOKEN_PATH}|{URL_SUFFIX}",
    re.IGNORECASE,
)
URL_SCHEME = re.compile(r"^[a-z][a-z0-9+.-]*:", re.IGNORECASE)
SITE_URL = re.compile(r"^[ \t]*site-url:[ \t]*['\"]?(https?://[^\s'\"#]+)", re.MULTILINE)
MARKUP_TAG = re.compile(r"(<[a-zA-Z][\w:-]*(?:[^<>\"']|\"[^\"]*\"|'[^']*')*>)")
IMAGE_TYPE = re.compile(r"(\s+type\s*=\s*)(['\"]?)image/[\w.+-]+\2(?=[\s/>])", re.IGNORECASE)
IMAGE_ATTRIBUTE = re.compile(
    r"(\s+(?:src|srcset|data-src|data-srcset|href|url|content|poster|style)\s*=\s*"
    r"(?:\"[^\"]*\"|'[^']*'|[^\s>]+))",
    re.IGNORECASE,
)
SRCSET_ATTRIBUTE = re.compile(r"\s+(?:data-)?srcset\s*=", re.IGNORECASE)


@dataclass(frozen=True)
class Conversion:
    source: Path
    destination: Path


@dataclass(frozen=True)
class ReferenceUpdate:
    path: Path
    content: bytes
    reference_count: int


@dataclass(frozen=True)
class SiteLocation:
    host: str
    prefix: str
    directory: Path


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="将常见图片格式转换为 WebP，并更新网站中的图片引用。",
        epilog="支持的图片扩展名：" + "、".join(sorted(IMAGE_SUFFIXES)),
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
        help="转换后保留原图，同时更新对它的引用。",
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


def iter_project_files(root: Path, suffixes: set[str]) -> Iterable[Path]:
    """遍历项目文件，避开版本库和常见依赖/缓存目录。"""
    for current, directory_names, file_names in os.walk(root):
        current_path = Path(current)
        directory_names[:] = sorted(
            name
            for name in directory_names
            if name.casefold() not in IGNORED_DIRECTORIES
            and not (current_path / name).is_symlink()
        )
        for name in sorted(file_names):
            path = current_path / name
            if not path.is_symlink() and path.suffix.casefold() in suffixes:
                yield path


def is_icon(path: Path, root: Path) -> bool:
    """路径的任一部分包含 icon 时视作图标。"""
    return any("icon" in part.casefold() for part in path.relative_to(root).parts)


def relative_display(path: Path, root: Path) -> str:
    return path.relative_to(root).as_posix()


def discover_conversions(
    root: Path, overwrite: bool
) -> tuple[list[Conversion], list[Path], list[tuple[Conversion, str]]]:
    conversions: list[Conversion] = []
    icons: list[Path] = []
    conflicts: list[tuple[Conversion, str]] = []
    by_destination: dict[Path, list[Conversion]] = {}

    for source in iter_project_files(root, IMAGE_SUFFIXES):
        if is_icon(source, root):
            icons.append(source)
            continue

        conversion = Conversion(source, source.with_suffix(".webp"))
        by_destination.setdefault(conversion.destination, []).append(conversion)

    for destination, candidates in by_destination.items():
        if len(candidates) > 1:
            reason = "多张原图对应同一个 WebP"
        elif destination.is_symlink() or (destination.exists() and not destination.is_file()):
            reason = "目标路径不是普通文件"
        elif destination.exists() and not overwrite:
            reason = "目标已存在，如需覆盖请使用 --overwrite"
        else:
            conversions.extend(candidates)
            continue
        conflicts.extend((conversion, reason) for conversion in candidates)

    return conversions, icons, conflicts


def load_pillow():
    try:
        from PIL import Image, ImageOps, ImageSequence, features
    except ImportError as exc:
        raise RuntimeError(
            "缺少 Pillow，请先运行：python -m pip install Pillow"
        ) from exc

    if not features.check("webp"):
        raise RuntimeError("当前 Pillow 不支持 WebP，请安装带 WebP 支持的 Pillow。")
    return Image, ImageOps, ImageSequence, features


def image_has_alpha(image) -> bool:
    return any(band.upper() == "A" for band in image.getbands()) or "transparency" in image.info


def webp_compatible_frame(image):
    mode = "RGBA" if image_has_alpha(image) else "RGB"
    return image.copy() if image.mode == mode else image.convert(mode)


def open_source_image(source: Path, pillow):
    Image, _, _, features = pillow
    suffix = source.suffix.casefold()
    if suffix in {".svg", ".svgz"}:
        try:
            from resvg_py import svg_to_bytes
        except ImportError as exc:
            raise RuntimeError("转换 SVG 需要 resvg-py，请运行 python -m pip install resvg-py") from exc
        return Image.open(io.BytesIO(svg_to_bytes(svg_path=str(source))))
    if suffix in {".heic", ".heif"}:
        try:
            from pillow_heif import register_heif_opener
        except ImportError as exc:
            raise RuntimeError(
                "转换 HEIC/HEIF 需要 pillow-heif，请运行 python -m pip install pillow-heif"
            ) from exc
        register_heif_opener()
    if suffix == ".avif" and "avif" not in features.get_supported_modules():
        raise RuntimeError("转换 AVIF 需要带 AVIF 支持的 Pillow 11.3 或更新版本。")
    return Image.open(source)


def convert_image(
    conversion: Conversion,
    quality: int,
    lossless: bool,
    overwrite: bool,
    pillow,
) -> int:
    """先写临时文件，再原子替换目标；返回 WebP 文件大小。"""
    _, ImageOps, ImageSequence, _ = pillow
    source = conversion.source
    destination = conversion.destination

    if destination.is_symlink() or (destination.exists() and not overwrite):
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
        with open_source_image(source, pillow) as image:
            if image.mode in {"RGB", "RGBA", "RGBX", "P", "PA"} and image.info.get("icc_profile"):
                save_options["icc_profile"] = image.info["icc_profile"]

            if getattr(image, "is_animated", False):
                if image.format in {"TIFF", "HEIF"}:
                    raise RuntimeError("原图包含多页或多张照片，单个 WebP 无法按原结构保存。")
                frames = []
                durations = []
                default_duration = image.info.get("duration", 100)
                loop = image.info.get("loop", 1)
                for index, frame in enumerate(ImageSequence.Iterator(image)):
                    if index == 0 and image.info.get("default_image"):
                        continue  # APNG 的静态预览不属于动画。
                    frame.load()  # AVIF 在加载像素时才更新帧时长。
                    durations.append(frame.info.get("duration", default_duration))
                    frames.append(webp_compatible_frame(frame))

                frames[0].save(
                    temporary_path,
                    save_all=True,
                    append_images=frames[1:],
                    duration=durations,
                    loop=loop,
                    **save_options,
                )
            else:
                oriented_image = ImageOps.exif_transpose(image)
                webp_compatible_frame(oriented_image).save(temporary_path, **save_options)

        if temporary_path.stat().st_size == 0:
            raise RuntimeError("生成了空的 WebP 文件。")

        os.chmod(temporary_path, stat.S_IMODE(source.stat().st_mode))
        if overwrite:
            os.replace(temporary_path, destination)
        elif os.name == "nt":
            temporary_path.rename(destination)  # Windows 上目标存在时 rename 会失败。
        else:
            os.link(temporary_path, destination)
        return destination.stat().st_size
    finally:
        temporary_path.unlink(missing_ok=True)


def resolve_local_reference(
    reference: str, document: Path, root: Path, sites: list[SiteLocation]
) -> Path | None:
    """解析相对路径、站点根路径以及 Quarto 配置的站内绝对 URL。"""
    reference = reference.replace("\\", "/")
    decoded = unquote(reference)
    if decoded.startswith("//") or URL_SCHEME.match(decoded):
        try:
            url = urlsplit(reference)
        except ValueError:
            return None
        if url.scheme not in {"", "http", "https"}:
            return None
        path = unquote(url.path)
        for site in sites:
            if url.netloc.casefold() == site.host and path.startswith(site.prefix):
                candidate = site.directory / path.removeprefix(site.prefix)
                break
        else:
            return None
    elif decoded.startswith("~/"):
        candidate = root / decoded[2:]
    elif decoded.startswith("/"):
        candidate = root / decoded.lstrip("/")
        if not candidate.exists():
            for site in sites:
                if document.is_relative_to(site.directory):
                    candidate = site.directory / decoded.lstrip("/")
                    break
    else:
        candidate = document.parent / decoded

    resolved = candidate.resolve()
    try:
        resolved.relative_to(root)
    except ValueError:
        return None
    return resolved


def replace_pattern(
    text: str,
    pattern: Pattern[str],
    document: Path,
    root: Path,
    converted: set[Path],
    sites: list[SiteLocation],
) -> tuple[str, int]:
    replacement_count = 0

    def replace(match: Match[str]) -> str:
        nonlocal replacement_count
        group = match.lastgroup
        if group is None:
            return match.group()
        reference = match.group(group)
        source = resolve_local_reference(reference, document, root, sites)
        if source is None or source not in converted:
            return match.group()

        replacement_count += 1
        new_reference = reference.rsplit(".", 1)[0] + ".webp"
        path_start = match.start(group) - match.start()
        path_end = path_start + len(reference)
        return f"{match.group()[:path_start]}{new_reference}{match.group()[path_end:]}"

    return pattern.sub(replace, text), replacement_count


def update_reference_text(
    text: str, document: Path, root: Path, converted: set[Path], sites: list[SiteLocation]
) -> tuple[str, int]:
    total = 0

    def rewrite_references(fragment: str, pattern: Pattern[str] = REFERENCE) -> str:
        nonlocal total
        fragment, count = replace_pattern(fragment, pattern, document, root, converted, sites)
        total += count
        return fragment

    def rewrite_image_tag(tag: str) -> str:
        parts = IMAGE_ATTRIBUTE.split(tag)
        if len(parts) == 1:
            return rewrite_references(tag)
        count_before = total
        for index in range(1, len(parts), 2):
            attribute = parts[index]
            pattern = SRCSET_REFERENCE if SRCSET_ATTRIBUTE.match(attribute) else REFERENCE
            parts[index] = rewrite_references(attribute, pattern)
        tag = "".join(parts)
        if total > count_before:
            tag = IMAGE_TYPE.sub(r"\g<1>\g<2>image/webp\g<2>", tag, count=1)
        return tag

    # RSS 的 media:content 和 HTML 的 source 等元素可能同时声明图片类型。
    parts = MARKUP_TAG.split(text)
    for index, part in enumerate(parts):
        parts[index] = rewrite_image_tag(part) if index % 2 else rewrite_references(part)
    return "".join(parts), total


def decode_document(content: bytes, path: Path) -> tuple[str, Callable[[str], bytes]]:
    if content.startswith(codecs.BOM_UTF8):
        return content.decode("utf-8-sig"), lambda text: text.encode("utf-8-sig")
    try:
        return content.decode("utf-8"), lambda text: text.encode("utf-8")
    except UnicodeDecodeError as exc:
        raise RuntimeError(f"网站文件不是有效的 UTF-8：{path}") from exc


def discover_sites(documents: list[Path]) -> list[SiteLocation]:
    sites: list[SiteLocation] = []
    for path in documents:
        if path.name.casefold() not in {"_quarto.yml", "_quarto.yaml"}:
            continue
        text, _ = decode_document(path.read_bytes(), path)
        match = SITE_URL.search(text)
        if match:
            url = urlsplit(match[1])
            if url.netloc:
                prefix = unquote(url.path).rstrip("/") + "/"
                sites.append(SiteLocation(url.netloc.casefold(), prefix, path.parent))
    return sorted(sites, key=lambda site: (len(site.prefix), len(site.directory.parts)), reverse=True)


def plan_reference_updates(root: Path, converted: set[Path]) -> list[ReferenceUpdate]:
    if not converted:
        return []
    documents = list(iter_project_files(root, REFERENCE_SUFFIXES))
    sites = discover_sites(documents)
    updates: list[ReferenceUpdate] = []
    for document in documents:
        text, encode = decode_document(document.read_bytes(), document)
        updated_text, count = update_reference_text(text, document, root, converted, sites)
        if count:
            updates.append(ReferenceUpdate(document, encode(updated_text), count))
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
    conflicts: list[tuple[Conversion, str]],
    reference_updates: list[ReferenceUpdate],
) -> None:
    for conversion in conversions:
        print(
            f"[将转换] {relative_display(conversion.source, root)} -> "
            f"{relative_display(conversion.destination, root)}"
        )
    for update in reference_updates:
        print(
            f"[将更新] {relative_display(update.path, root)} "
            f"({update.reference_count} 处引用)"
        )
    for path in icons:
        print(f"[跳过 icon] {relative_display(path, root)}")
    for conversion, reason in conflicts:
        print(
            f"[跳过冲突] {relative_display(conversion.source, root)}：{reason}"
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
    planned_sources = {conversion.source.resolve() for conversion in conversions}

    try:
        planned_updates = plan_reference_updates(root, planned_sources)
    except (OSError, RuntimeError) as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return 1

    if args.dry_run:
        print_plan(root, conversions, icons, conflicts, planned_updates)
        print(
            f"\n预演完成：将转换 {len(conversions)} 张图片，"
            f"更新 {len(planned_updates)} 个网站文件；未改动文件。"
        )
        return 0

    if not conversions:
        print_plan(root, conversions, icons, conflicts, planned_updates)
        print("没有需要转换的图片。")
        return 0

    for path in icons:
        print(f"[跳过 icon] {relative_display(path, root)}")
    for conversion, reason in conflicts:
        print(f"[跳过冲突] {relative_display(conversion.source, root)}：{reason}")

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
        try:
            source_size = conversion.source.stat().st_size
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

    successful_sources = {conversion.source.resolve() for conversion in successful}

    try:
        reference_updates = plan_reference_updates(root, successful_sources)
        for update in reference_updates:
            atomic_write(update.path, update.content)
            print(
                f"[已更新] {relative_display(update.path, root)} "
                f"({update.reference_count} 处引用)"
            )
    except (OSError, RuntimeError) as exc:
        print(
            f"错误：更新图片引用失败，原图均已保留：{exc}", file=sys.stderr
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
        change_percent = abs(saved_bytes) / original_bytes * 100
        change_label = "节省" if saved_bytes >= 0 else "增大"
        size_summary = (
            f"，总体 {format_size(original_bytes)} -> {format_size(webp_bytes)}，"
            f"{change_label} {change_percent:.1f}%"
        )
    else:
        size_summary = ""

    print(
        f"\n完成：转换 {len(successful)} 张图片，更新 {len(reference_updates)} 个网站文件"
        f"{size_summary}。跳过 icon {len(icons)} 张、冲突 {len(conflicts)} 张。"
    )
    return 1 if conversion_errors or deletion_errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
