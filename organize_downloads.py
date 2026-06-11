#!/usr/bin/env python3

import argparse
import fnmatch
import hashlib
import os
import shutil
import sys
from collections import defaultdict
from pathlib import Path

try:
    import yaml
except ImportError:
    yaml = None


DEFAULT_CATEGORIES = {
    "Images": [".jpg", ".jpeg", ".png", ".gif", ".bmp", ".svg", ".webp", ".ico", ".tiff", ".tif", ".heic", ".heif"],
    "Documents": [".pdf", ".doc", ".docx", ".xls", ".xlsx", ".ppt", ".pptx", ".txt", ".md", ".csv", ".json", ".xml", ".html", ".htm", ".rtf", ".odt", ".ods", ".odp"],
    "Archives": [".zip", ".rar", ".7z", ".tar", ".gz", ".bz2", ".xz", ".tgz", ".iso", ".dmg"],
    "Videos": [".mp4", ".avi", ".mkv", ".mov", ".wmv", ".flv", ".webm", ".m4v", ".mpg", ".mpeg", ".3gp"],
}

DEFAULT_EXCLUDE_PATTERNS = ["*.tmp", "*.crdownload", "*.part", "*.!ut", "Thumbs.db", "desktop.ini"]

CATEGORY_NAMES = ["Images", "Documents", "Archives", "Videos", "Others", "Duplicates"]


def load_config(config_path):
    if yaml is None:
        sys.exit("缺少 PyYAML 依赖，请执行: pip install pyyaml")

    if config_path and Path(config_path).exists():
        with open(config_path, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
    else:
        data = {}

    categories = data.get("categories", DEFAULT_CATEGORIES)
    exclude_patterns = data.get("exclude_patterns", DEFAULT_EXCLUDE_PATTERNS)

    if not isinstance(categories, dict):
        sys.exit("配置文件错误: 'categories' 必须是一个字典")

    _validate_categories(categories)

    normalized = {}
    for category, exts in categories.items():
        normalized[category] = [ext.lower() if ext.startswith(".") else f".{ext.lower()}" for ext in exts]

    return normalized, exclude_patterns


def _validate_categories(categories):
    custom_names = set(categories.keys())
    for reserved in CATEGORY_NAMES:
        custom_names.discard(reserved)
    if custom_names and "Others" not in custom_names:
        return  # 用户自定义映射是被允许的
    conflicts = set(categories.keys()) & {"Duplicates"}
    if conflicts:
        sys.exit(f"配置文件错误: 不能使用保留类别名: {conflicts}")


def should_exclude(file_name, exclude_patterns):
    for pattern in exclude_patterns:
        if fnmatch.fnmatch(file_name, pattern):
            return True
    return False


def _compute_md5(file_path):
    h = hashlib.md5()
    try:
        with open(file_path, "rb") as f:
            while True:
                chunk = f.read(65536)
                if not chunk:
                    break
                h.update(chunk)
        return h.hexdigest()
    except (OSError, PermissionError):
        return None


def _build_ext_to_category(categories):
    mapping = {}
    for category, extensions in categories.items():
        for ext in extensions:
            mapping[ext] = category
    return mapping


def scan_files(root_dir, exclude_patterns, category_names):
    root = Path(root_dir).resolve()
    all_files = []
    for entry in root.rglob("*"):
        if entry.is_file():
            if should_exclude(entry.name, exclude_patterns):
                continue
            if entry.relative_to(root).parts[0] in category_names:
                continue
            all_files.append(entry)
    return all_files


def classify_files(files, categories):
    ext_map = _build_ext_to_category(categories)
    classified = defaultdict(list)
    for f in files:
        ext = f.suffix.lower()
        category = ext_map.get(ext, "Others")
        classified[category].append(f)
    return classified


def _deduplicate_category(file_list):
    md5_map = defaultdict(list)
    for f in file_list:
        md5 = _compute_md5(f)
        if md5 is None:
            continue
        md5_map[md5].append(f)

    unique_files = []
    duplicate_files = []
    for paths in md5_map.values():
        if paths:
            unique_files.append(paths[0])
            for dup in paths[1:]:
                duplicate_files.append(dup)

    return unique_files, duplicate_files


def _ensure_dir(path, dry_run=False):
    if not dry_run:
        Path(path).mkdir(parents=True, exist_ok=True)


def _move_file(src, dst_dir, dry_run):
    dst = Path(dst_dir) / src.name
    if Path(src).resolve() == dst.resolve():
        return 0
    if dst.exists():
        stem = dst.stem
        suffix = dst.suffix
        counter = 1
        while dst.exists():
            dst = Path(dst_dir) / f"{stem}_{counter}{suffix}"
            counter += 1

    size = src.stat().st_size
    action = f"[DRY-RUN] 将移动" if dry_run else "移动"
    print(f"  {action}: {src.name} -> {dst.parent.name}/{dst.name}")

    if not dry_run:
        shutil.move(str(src), str(dst))
    return size


def _collect_empty_dirs(root):
    empty = []
    for entry in Path(root).rglob("*"):
        if entry.is_dir():
            try:
                if not any(entry.iterdir()):
                    empty.append(entry)
            except OSError:
                pass
    return sorted(empty, key=lambda p: len(p.parts), reverse=True)


def _cleanup_empty_dirs(root, dry_run):
    empty_dirs = _collect_empty_dirs(root)
    for d in empty_dirs:
        action = f"[DRY-RUN] 将删除空文件夹" if dry_run else "删除空文件夹"
        print(f"  {action}: {d}")
        if not dry_run:
            try:
                d.rmdir()
            except OSError:
                pass
    return len(empty_dirs)


def generate_report(stats):
    print()
    print("=" * 60)
    print("整理报告")
    print("=" * 60)
    print(f"  下载目录:         {stats['root']}")
    print(f"  扫描文件总数:      {stats['total_files']}")
    print(f"  排除文件数:        {stats['excluded']}")
    print(f"  重复文件数:        {stats['duplicates']}")
    print(f"  已移动文件数:      {stats['moved']}")
    print(f"  已删除空文件夹:    {stats['empty_dirs_removed']}")
    print(f"  释放空间:          {_format_size(stats['freed_space'])}")
    print("-" * 60)
    print("按类别统计:")
    for cat in sorted(stats["by_category"]):
        count = stats["by_category"][cat]
        if count:
            print(f"  {cat:<15} {count} 个文件")
    print("=" * 60)


def _format_size(size_bytes):
    for unit in ["B", "KB", "MB", "GB", "TB"]:
        if abs(size_bytes) < 1024:
            return f"{size_bytes:.1f} {unit}"
        size_bytes /= 1024
    return f"{size_bytes:.1f} PB"


def _generate_default_config(output_path):
    config = {
        "categories": DEFAULT_CATEGORIES,
        "exclude_patterns": DEFAULT_EXCLUDE_PATTERNS,
    }
    with open(output_path, "w", encoding="utf-8") as f:
        yaml.dump(config, f, allow_unicode=True, default_flow_style=False, sort_keys=False)
    print(f"默认配置文件已生成: {output_path}")


def organize(download_dir, config_path, dry_run, extra_exclude):
    if yaml is None:
        sys.exit("缺少 PyYAML 依赖，请执行: pip install pyyaml")

    categories, exclude_patterns = load_config(config_path)
    exclude_patterns = list(set(exclude_patterns + extra_exclude))

    category_names = set(categories.keys()) | {"Others", "Duplicates"}

    root = Path(download_dir).resolve()
    if not root.exists():
        sys.exit(f"目录不存在: {root}")
    if not root.is_dir():
        sys.exit(f"不是目录: {root}")

    print(f"整理目录: {root}")
    if dry_run:
        print("⚡ 演练模式 — 不会实际修改任何文件")
    print()

    files = scan_files(root, exclude_patterns, category_names)
    total_files = len(files)
    print(f"扫描到 {total_files} 个文件（已排除匹配排除模式的文件）")
    print()

    classified = classify_files(files, categories)

    category_dirs = {}
    for cat in sorted(category_names):
        if cat == "Duplicates":
            continue
        category_dirs[cat] = root / cat

    duplicates_dir = root / "Duplicates"

    stats = {
        "root": str(root),
        "total_files": total_files,
        "excluded": 0,
        "duplicates": 0,
        "moved": 0,
        "empty_dirs_removed": 0,
        "freed_space": 0,
        "by_category": defaultdict(int),
    }

    for cat in sorted(category_names):
        if cat == "Duplicates":
            continue
        file_list = classified.get(cat, [])
        if not file_list:
            continue

        print(f"--- {cat} ({len(file_list)} 个文件) ---")
        unique_files, duplicate_files = _deduplicate_category(file_list)

        _ensure_dir(category_dirs[cat], dry_run)
        for f in unique_files:
            space = _move_file(f, category_dirs[cat], dry_run)
            stats["freed_space"] += space  # 计入释放空间
            stats["moved"] += 1
            stats["by_category"][cat] += 1

        if duplicate_files:
            _ensure_dir(duplicates_dir, dry_run)
            for f in duplicate_files:
                space = _move_file(f, duplicates_dir, dry_run)
                stats["freed_space"] += space
                stats["moved"] += 1
                stats["duplicates"] += 1
                stats["by_category"]["Duplicates"] += 1

        print()

    if stats["duplicates"] > 0:
        print(f"发现 {stats['duplicates']} 个重复文件，已移至 Duplicates/")
        print()

    print("--- 清理空文件夹 ---")
    stats["empty_dirs_removed"] = _cleanup_empty_dirs(root, dry_run)
    print()

    generate_report(stats)


def main():
    parser = argparse.ArgumentParser(
        description="整理混乱的下载文件夹——按扩展名分类、MD5去重、清理空文件夹。"
    )
    parser.add_argument(
        "directory",
        nargs="?",
        default=None,
        help="要整理的目录路径（默认：当前用户的下载文件夹）",
    )
    parser.add_argument(
        "--config", "-c",
        default=None,
        help="YAML 配置文件路径（自定义扩展名映射和排除模式）",
    )
    parser.add_argument(
        "--dry-run", "-n",
        action="store_true",
        help="演练模式：只打印将要执行的操作，不实际移动文件",
    )
    parser.add_argument(
        "--exclude", "-e",
        action="append",
        default=[],
        help="追加排除的文件名模式（支持通配符，如 '*.tmp'），可多次指定",
    )
    parser.add_argument(
        "--generate-config",
        metavar="PATH",
        help="生成默认 YAML 配置文件并退出",
    )

    args = parser.parse_args()

    if args.generate_config:
        _generate_default_config(args.generate_config)
        return

    if args.directory:
        download_dir = args.directory
    else:
        download_dir = Path.home() / "Downloads"
        if not download_dir.exists():
            sys.exit(f"默认下载目录不存在: {download_dir}，请手动指定目录路径")

    organize(
        download_dir=download_dir,
        config_path=args.config,
        dry_run=args.dry_run,
        extra_exclude=args.exclude,
    )


if __name__ == "__main__":
    main()