#!/usr/bin/env python3

import argparse
import fnmatch
import hashlib
import json
import os
import shutil
import sys
from collections import defaultdict
from datetime import datetime
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

LOG_FILENAME = ".organize_log.json"


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


def _ensure_dir(path, dry_run=False):
    if not dry_run:
        Path(path).mkdir(parents=True, exist_ok=True)


def _move_file(src, dst_dir, dry_run):
    dst = Path(dst_dir) / src.name
    if Path(src).resolve() == dst.resolve():
        return 0, str(src)
    if dst.exists():
        stem = dst.stem
        suffix = dst.suffix
        counter = 1
        while dst.exists():
            dst = Path(dst_dir) / f"{stem}_{counter}{suffix}"
            counter += 1

    size = src.stat().st_size
    action = "[DRY-RUN] 将移动" if dry_run else "移动"
    print(f"  {action}: {src.name} -> {dst.parent.name}/{dst.name}")

    if not dry_run:
        shutil.move(str(src), str(dst))
    return size, str(dst)


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
    total_removed = 0
    max_passes = 10
    for _ in range(max_passes):
        empty_dirs = _collect_empty_dirs(root)
        if not empty_dirs:
            break
        for d in empty_dirs:
            action = "[DRY-RUN] 将删除空文件夹" if dry_run else "删除空文件夹"
            print(f"  {action}: {d}")
            if not dry_run:
                try:
                    d.rmdir()
                except OSError:
                    pass
            total_removed += 1
        if dry_run:
            break
    return total_removed


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


DEFAULT_CATEGORY_NAMES = {"Images", "Documents", "Archives", "Videos", "Others", "Duplicates"}


def _global_dedup(files):
    md5_map = defaultdict(list)
    for f in files:
        md5 = _compute_md5(f)
        if md5 is None:
            continue
        md5_map[md5].append(f)

    unique_files = []
    duplicate_groups = []
    for md5_hash, paths in md5_map.items():
        if len(paths) == 1:
            unique_files.append(paths[0])
        else:
            kept = paths[0]
            unique_files.append(kept)
            duplicate_groups.append({
                "md5": md5_hash,
                "kept": str(kept),
                "duplicates": [str(p) for p in paths[1:]],
                "duplicate_size": sum(p.stat().st_size for p in paths[1:]),
            })

    return unique_files, duplicate_groups


def _save_operation_log(log_path, log_data):
    with open(log_path, "w", encoding="utf-8") as f:
        json.dump(log_data, f, ensure_ascii=False, indent=2)


def _load_operation_log(log_path):
    if not Path(log_path).exists():
        sys.exit(f"操作日志不存在: {log_path}")
    with open(log_path, "r", encoding="utf-8") as f:
        return json.load(f)


def _build_operation_log(root, move_record):
    log = {
        "version": 1,
        "timestamp": datetime.now().isoformat(),
        "root": str(root),
        "moves": move_record,
    }
    return log


def generate_report(stats, duplicate_groups, exclude_info, report_base):
    lines = []
    lines.append("=" * 60)
    lines.append("整理报告")
    lines.append("=" * 60)
    lines.append(f"  下载目录:         {stats['root']}")
    lines.append(f"  扫描文件总数:      {stats['total_files']}")
    lines.append(f"  排除文件数:        {stats['excluded']}")
    lines.append(f"  唯一文件数:        {stats['unique_files']}")
    lines.append(f"  重复文件数:        {stats['duplicates']}")
    lines.append(f"  已移动文件数:      {stats['moved']}")
    lines.append(f"  已删除空文件夹:    {stats['empty_dirs_removed']}")
    lines.append(f"  释放空间(重复):    {_format_size(stats['freed_space'])}")
    lines.append("-" * 60)
    lines.append("按类别统计:")
    for cat in sorted(stats["by_category"]):
        count = stats["by_category"][cat]
        if count:
            lines.append(f"  {cat:<15} {count} 个文件")
    lines.append("=" * 60)

    for line in lines:
        print(line)

    if report_base:
        _write_report_files(report_base, lines, stats, duplicate_groups, exclude_info)


def _write_report_files(report_base, console_lines, stats, duplicate_groups, exclude_info):
    json_report = {
        "timestamp": datetime.now().isoformat(),
        "root": stats["root"],
        "total_files": stats["total_files"],
        "excluded": stats["excluded"],
        "unique_files": stats["unique_files"],
        "duplicates": stats["duplicates"],
        "moved": stats["moved"],
        "empty_dirs_removed": stats["empty_dirs_removed"],
        "freed_space_bytes": stats["freed_space"],
        "freed_space_human": _format_size(stats["freed_space"]),
        "by_category": dict(stats["by_category"]),
        "duplicate_groups": duplicate_groups,
        "excluded_files": [str(p) for p in exclude_info.get("excluded_files", [])],
        "moves": [{"source": m["source"], "destination": m["dest"]} for m in stats.get("move_record", [])],
    }

    json_path = f"{report_base}.json"
    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(json_report, f, ensure_ascii=False, indent=2)
    print(f"\nJSON 报告已保存: {json_path}")

    md_lines = _build_markdown_report(stats, duplicate_groups, exclude_info)
    md_path = f"{report_base}.md"
    with open(md_path, "w", encoding="utf-8") as f:
        f.write("\n".join(md_lines))
    print(f"Markdown 报告已保存: {md_path}")


def _build_markdown_report(stats, duplicate_groups, exclude_info):
    lines = []
    lines.append("# 下载文件夹整理报告")
    lines.append("")
    lines.append(f"**整理时间**: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}  ")
    lines.append(f"**整理目录**: `{stats['root']}`  ")
    lines.append("")
    lines.append("## 概览")
    lines.append("")
    lines.append(f"| 指标 | 数值 |")
    lines.append(f"|------|------|")
    lines.append(f"| 扫描文件总数 | {stats['total_files']} |")
    lines.append(f"| 排除文件数 | {stats['excluded']} |")
    lines.append(f"| 唯一文件数 | {stats['unique_files']} |")
    lines.append(f"| 重复文件数 | {stats['duplicates']} |")
    lines.append(f"| 已移动文件数 | {stats['moved']} |")
    lines.append(f"| 已删除空文件夹 | {stats['empty_dirs_removed']} |")
    lines.append(f"| 释放空间(重复) | {_format_size(stats['freed_space'])} |")
    lines.append("")

    lines.append("## 按类别统计")
    lines.append("")
    lines.append("| 类别 | 文件数 |")
    lines.append("|------|--------|")
    for cat in sorted(stats["by_category"]):
        count = stats["by_category"][cat]
        if count:
            lines.append(f"| {cat} | {count} |")
    lines.append("")

    if stats.get("move_record"):
        lines.append("## 文件移动明细")
        lines.append("")
        lines.append("| 源路径 | 目标路径 | 大小 |")
        lines.append("|--------|----------|------|")
        for m in stats["move_record"]:
            src_name = Path(m["source"]).name
            dst_name = Path(m["dest"]).name
            lines.append(f"| {src_name} | {Path(m['dest']).parent.name}/{dst_name} | {_format_size(m['size'])} |")
        lines.append("")

    if duplicate_groups:
        lines.append("## 重复文件详情")
        lines.append("")
        total_dup_size = sum(g["duplicate_size"] for g in duplicate_groups)
        lines.append(f"共 `{len(duplicate_groups)}` 组重复，释放空间: `{_format_size(total_dup_size)}`  ")
        lines.append("")
        for i, group in enumerate(duplicate_groups, 1):
            lines.append(f"### 重复组 {i} (MD5: `{group['md5'][:12]}...`)")
            lines.append("")
            lines.append(f"- **保留**: {Path(group['kept']).name}  ")
            for dup in group["duplicates"]:
                lines.append(f"- **重复**: {Path(dup).name}  ")
            lines.append(f"- 本组释放: {_format_size(group['duplicate_size'])}  ")
            lines.append("")

    if exclude_info.get("excluded_files"):
        lines.append("## 排除的文件")
        lines.append("")
        for f in exclude_info["excluded_files"]:
            lines.append(f"- {Path(f).name}  ")
        lines.append("")

    return lines


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
        print("⚠ 演练模式 — 不会实际修改任何文件")
    print()

    all_files = []
    excluded_files = []
    for entry in root.rglob("*"):
        if entry.is_file():
            if should_exclude(entry.name, exclude_patterns):
                excluded_files.append(entry)
                continue
            if entry.relative_to(root).parts[0] in category_names:
                continue
            all_files.append(entry)

    total_files = len(all_files) + len(excluded_files)
    print(f"扫描到 {len(all_files)} 个待处理文件（已排除 {len(excluded_files)} 个匹配排除模式的文件）")
    print()

    unique_files, duplicate_groups = _global_dedup(all_files)

    classified = classify_files(unique_files, categories)

    category_dirs = {}
    for cat in sorted(category_names):
        if cat == "Duplicates":
            continue
        category_dirs[cat] = root / cat

    duplicates_dir = root / "Duplicates"

    exclude_info = {"excluded_files": excluded_files}

    move_record = []
    stats = {
        "root": str(root),
        "total_files": total_files,
        "excluded": len(excluded_files),
        "unique_files": len(unique_files),
        "duplicates": sum(len(g["duplicates"]) for g in duplicate_groups),
        "moved": 0,
        "empty_dirs_removed": 0,
        "freed_space": 0,
        "by_category": defaultdict(int),
        "move_record": move_record,
    }

    for cat in sorted(category_names):
        if cat == "Duplicates":
            continue
        file_list = classified.get(cat, [])
        if not file_list:
            continue

        print(f"--- {cat} ({len(file_list)} 个文件) ---")
        _ensure_dir(category_dirs[cat], dry_run)
        for f in file_list:
            size, dest = _move_file(f, category_dirs[cat], dry_run)
            stats["moved"] += 1
            stats["by_category"][cat] += 1
            move_record.append({"source": str(f), "dest": dest, "size": size})
        print()

    if duplicate_groups:
        print(f"--- 重复文件 ({stats['duplicates']} 个) ---")
        _ensure_dir(duplicates_dir, dry_run)
        for group in duplicate_groups:
            for dup_path in group["duplicates"]:
                dup = Path(dup_path)
                size, dest = _move_file(dup, duplicates_dir, dry_run)
                stats["freed_space"] += size
                stats["moved"] += 1
                stats["by_category"]["Duplicates"] += 1
                move_record.append({
                    "source": str(dup),
                    "dest": dest,
                    "size": size,
                    "is_duplicate": True,
                    "duplicate_of": group["kept"],
                })
        print()
        print(f"发现 {stats['duplicates']} 个重复文件（{len(duplicate_groups)} 组），已移至 Duplicates/")
        print()

    print("--- 清理空文件夹 ---")
    stats["empty_dirs_removed"] = _cleanup_empty_dirs(root, dry_run)
    print()

    operate_ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    report_base = str(root / f"organize_report_{operate_ts}")

    generate_report(stats, duplicate_groups, exclude_info, report_base if not dry_run else None)

    if not dry_run:
        log_path = root / LOG_FILENAME
        if log_path.exists():
            backup_path = root / f".organize_log_{operate_ts}.bak"
            shutil.move(str(log_path), str(backup_path))
            print(f"旧操作日志已备份: {backup_path.name}")

        log_data = _build_operation_log(root, move_record)
        _save_operation_log(log_path, log_data)
        print(f"操作日志已保存: {log_path.name}")
        print(f"恢复命令: python organize_downloads.py --undo \"{root}\"")


def undo(directory):
    root = Path(directory).resolve()
    if not root.exists():
        sys.exit(f"目录不存在: {root}")

    log_path = root / LOG_FILENAME
    log_data = _load_operation_log(log_path)

    moves = log_data.get("moves", [])
    if not moves:
        print("操作日志为空，无需恢复。")
        return

    print(f"从操作日志恢复 {len(moves)} 个文件")
    print(f"操作时间: {log_data.get('timestamp', '未知')}")
    print()

    restored = 0
    skipped = 0
    conflicts = 0

    for i, entry in enumerate(moves, 1):
        source = Path(entry["source"])
        dest = Path(entry.get("dest"))
        if not dest.exists():
            print(f"  [{i}/{len(moves)}] 跳过: 文件不存在 {dest}")
            skipped += 1
            continue

        if source.exists():
            print(f"  [{i}/{len(moves)}] 冲突: 原位置已存在文件 {source.name}")
            alt = source.parent / f"{source.stem}_restored{source.suffix}"
            print(f"        -> 恢复到: {alt}")
            if not alt.exists():
                shutil.move(str(dest), str(alt))
                restored += 1
            else:
                print(f"        -> 跳过: 替代位置也冲突")
                skipped += 1
            conflicts += 1
        else:
            source.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(dest), str(source))
            print(f"  [{i}/{len(moves)}] 恢复: {dest.name} -> {source}")
            restored += 1

    print()
    print(f"恢复完成: {restored} 个文件已恢复, {conflicts} 个冲突, {skipped} 个跳过")

    if restored > 0:
        log_path.unlink()
        print(f"操作日志已删除: {log_path.name}")

    print()
    print("--- 清理空文件夹 ---")
    removed = _cleanup_empty_dirs(root, dry_run=False)
    print(f"已删除 {removed} 个空文件夹")


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
    parser.add_argument(
        "--undo",
        action="store_true",
        help="恢复模式：将上一次整理移动的文件还原到原位置",
    )

    args = parser.parse_args()

    if args.generate_config:
        _generate_default_config(args.generate_config)
        return

    if args.undo:
        if args.directory:
            undo(args.directory)
        else:
            download_dir = Path.home() / "Downloads"
            if not download_dir.exists():
                sys.exit(f"默认下载目录不存在: {download_dir}，请手动指定目录路径")
            undo(str(download_dir))
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