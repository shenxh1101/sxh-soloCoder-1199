#!/usr/bin/env python3

import argparse
import fnmatch
import hashlib
import json
import os
import shutil
import sys
from collections import defaultdict
from datetime import datetime, timedelta
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

DEFAULT_EXCLUDE_PATTERNS = [
    "*.tmp", "*.crdownload", "*.part", "*.!ut", "Thumbs.db", "desktop.ini",
    "organize_report_*.json", "organize_report_*.md",
    ".organize_log.json", ".organize_log_*.json", ".organize_log_*.bak",
    ".organize_history.json",
    ".organize_md5_cache.json",
]

LOG_FILENAME = ".organize_log.json"
HISTORY_FILENAME = ".organize_history.json"
MD5_CACHE_FILENAME = ".organize_md5_cache.json"

CONFLICT_STRATEGIES = {"auto_rename", "skip", "overwrite_backup"}
DEFAULT_CONFLICT_STRATEGY = "auto_rename"
DEFAULT_HISTORY_RETENTION_COUNT = 30
DEFAULT_HISTORY_RETENTION_DAYS = 90


# ============================================================
#  配置加载
# ============================================================

def load_config(config_path):
    if yaml is None:
        sys.exit("缺少 PyYAML 依赖，请执行: pip install pyyaml")

    if config_path and Path(config_path).exists():
        with open(config_path, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
    else:
        data = {}

    categories = data.get("categories", DEFAULT_CATEGORIES)
    exclude_patterns = list(DEFAULT_EXCLUDE_PATTERNS)
    user_excludes = data.get("exclude_patterns", [])
    exclude_patterns.extend(user_excludes)

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


# ============================================================
#  工具函数
# ============================================================

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


def _move_file(src, dst_dir, dry_run, conflict_strategy=DEFAULT_CONFLICT_STRATEGY):
    dst = Path(dst_dir) / src.name
    conflict_action = None
    backup_path = None

    if Path(src).resolve() == dst.resolve():
        return 0, str(src), conflict_action, backup_path

    if dst.exists():
        if conflict_strategy == "skip":
            label = "[DRY-RUN] 将" if dry_run else ""
            print(f"  {label}跳过(冲突): {src.name}")
            return 0, str(src), "skip", None
        elif conflict_strategy == "overwrite_backup":
            ts = datetime.now().strftime("%Y%m%d_%H%M%S")
            bak = dst.parent / f"{dst.stem}.bak_{ts}{dst.suffix}"
            label = "[DRY-RUN] 将" if dry_run else ""
            print(f"  {label}备份: {dst.name} -> {bak.parent.name}/{bak.name}")
            if not dry_run:
                shutil.move(str(dst), str(bak))
            conflict_action = "overwrite_backup"
            backup_path = str(bak)
        else:
            stem = dst.stem
            suffix = dst.suffix
            counter = 1
            while dst.exists():
                dst = Path(dst_dir) / f"{stem}_{counter}{suffix}"
                counter += 1
            conflict_action = "auto_rename"

    size = src.stat().st_size
    tag = "[DRY-RUN] 将" if dry_run else ""
    if conflict_action:
        print(f"  {tag}移动: {src.name} -> {dst.parent.name}/{dst.name}  (冲突: {conflict_action})")
    else:
        print(f"  {tag}移动: {src.name} -> {dst.parent.name}/{dst.name}")

    if not dry_run:
        shutil.move(str(src), str(dst))
    return size, str(dst), conflict_action, backup_path


def _format_size(size_bytes):
    for unit in ["B", "KB", "MB", "GB", "TB"]:
        if abs(size_bytes) < 1024:
            return f"{size_bytes:.1f} {unit}"
        size_bytes /= 1024
    return f"{size_bytes:.1f} PB"


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
            label = "[DRY-RUN] 将" if dry_run else ""
            print(f"  {label}删除空文件夹: {d}")
            if not dry_run:
                try:
                    d.rmdir()
                except OSError:
                    pass
            total_removed += 1
        if dry_run:
            break
    return total_removed


def _check_dir_exists(path):
    try:
        return Path(path).exists()
    except OSError:
        return False


# ============================================================
#  预测空目录
# ============================================================

def _predict_empty_dirs(root, category_names, moved_sources):
    root = Path(root)
    moved_set = {str(p.resolve()) for p in moved_sources if _check_dir_exists(p)}

    dir_files_map = defaultdict(set)
    for entry in root.rglob("*"):
        if entry.is_file():
            entry_dir = entry.parent
            dir_files_map[entry_dir].add(str(entry.resolve()))

    dir_hierarchy = defaultdict(set)
    for d in dir_files_map:
        parent = d.parent
        while parent != root.parent and parent != parent.parent:
            dir_hierarchy[parent].add(d)
            parent = parent.parent

    fully_moved = set()
    for d, files in dir_files_map.items():
        if files and files.issubset(moved_set):
            fully_moved.add(d)

    will_be_empty = set()
    for d in fully_moved:
        if d.name not in category_names:
            will_be_empty.add(d)

    current_empty = {d for d in _collect_empty_dirs(root)}

    changed = True
    max_passes = 10
    for _ in range(max_passes):
        if not changed:
            break
        changed = False
        for d in list(dir_hierarchy.keys()):
            if d in will_be_empty or d in current_empty:
                continue
            children = dir_hierarchy[d]
            if children and all(
                c in will_be_empty or c in current_empty
                for c in children
            ):
                if d.name not in category_names:
                    will_be_empty.add(d)
                    changed = True

    predicted = current_empty | will_be_empty
    return sorted(predicted, key=lambda p: len(Path(p).parts), reverse=True)


# ============================================================
#  MD5 缓存
# ============================================================

def _load_md5_cache(root):
    cache_path = Path(root) / MD5_CACHE_FILENAME
    if not cache_path.exists():
        return {}, {"hits": 0, "misses": 0, "bytes_saved": 0}
    try:
        with open(cache_path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (json.JSONDecodeError, OSError):
        return {}, {"hits": 0, "misses": 0, "bytes_saved": 0}

    if isinstance(data, dict):
        stats = data.pop("__stats__", {"hits": 0, "misses": 0, "bytes_saved": 0})
        return data, stats
    return {}, {"hits": 0, "misses": 0, "bytes_saved": 0}


def _save_md5_cache(root, cache, stats=None):
    cache_path = Path(root) / MD5_CACHE_FILENAME
    if stats is not None:
        cache["__stats__"] = stats
    with open(cache_path, "w", encoding="utf-8") as f:
        json.dump(cache, f, ensure_ascii=False, indent=2)


def _get_md5_cached(file_path, cache):
    path_str = str(file_path)
    try:
        stat = file_path.stat()
        size = stat.st_size
        mtime = stat.st_mtime
    except OSError:
        return None, False

    entry = cache.get(path_str)
    if entry and entry.get("size") == size and entry.get("mtime") == mtime:
        return entry.get("md5"), True

    md5 = _compute_md5(file_path)
    if md5:
        cache[path_str] = {"md5": md5, "size": size, "mtime": mtime}
    return md5, False


def _clean_stale_cache(cache):
    stale = []
    for path_str in list(cache.keys()):
        if not _check_dir_exists(path_str):
            stale.append(path_str)
    for s in stale:
        del cache[s]
    return len(stale)


# ============================================================
#  缓存维护
# ============================================================

def cache_stats(root, detail=False):
    cache, cache_stats_data = _load_md5_cache(root)
    cache_path = Path(root) / MD5_CACHE_FILENAME

    if not cache:
        print("MD5 缓存为空。")
        return

    total = len(cache)
    stale_count = sum(1 for p in cache if not _check_dir_exists(p))
    total_size = cache_path.stat().st_size if cache_path.exists() else 0
    cached_sizes = sum(e.get("size", 0) for e in cache.values())

    total_hits = cache_stats_data.get("hits", 0)
    total_misses = cache_stats_data.get("misses", 0)
    bytes_saved = cache_stats_data.get("bytes_saved", 0)
    total_ops = total_hits + total_misses
    hit_rate = (total_hits / total_ops * 100) if total_ops > 0 else 0

    print("=" * 50)
    print("MD5 缓存统计")
    print("=" * 50)
    print(f"  缓存条目:     {total}")
    print(f"  失效条目:     {stale_count}")
    print(f"  有效条目:     {total - stale_count}")
    print(f"  缓存文件大小: {_format_size(total_size)}")
    print(f"  缓存覆盖文件: {_format_size(cached_sizes)}")
    print("-" * 50)
    print("累计命中统计:")
    print(f"  累计命中:     {total_hits} 次")
    print(f"  累计重算:     {total_misses} 次")
    print(f"  命中率:       {hit_rate:.1f}%")
    print(f"  省去重复计算: {_format_size(bytes_saved)}")
    print("=" * 50)

    if detail:
        print()
        print("缓存详情:")
        for path_str, entry in sorted(cache.items()):
            name = Path(path_str).name
            exists = _check_dir_exists(path_str)
            status = "" if exists else " [失效]"
            print(f"  {name}{status}  MD5={entry.get('md5', '?')[:12]}...  "
                  f"{_format_size(entry.get('size', 0))}")


def cache_clean(root):
    cache, cache_stats_data = _load_md5_cache(root)
    if not cache:
        print("MD5 缓存为空，无需清理。")
        return

    removed = _clean_stale_cache(cache)
    _save_md5_cache(root, cache, cache_stats_data)
    print(f"缓存清理完成: 移除 {removed} 条失效条目, 剩余 {len(cache)} 条")


def cache_rebuild(root):
    root = Path(root)
    cache_path = root / MD5_CACHE_FILENAME
    if cache_path.exists():
        cache_path.unlink()

    categories, _ = load_config(None) if yaml else (DEFAULT_CATEGORIES, [])
    if yaml is None:
        categories = DEFAULT_CATEGORIES

    category_names = set(categories.keys()) | {"Others", "Duplicates"}
    category_dirs = {}
    for cat in sorted(category_names):
        if cat == "Duplicates":
            continue
        category_dirs[cat] = root / cat

    existing_files = _scan_existing_category_files(root, category_dirs)
    cache = {}
    for path_str, file_path in sorted(existing_files.items()):
        md5 = _compute_md5(file_path)
        if md5:
            try:
                stat = file_path.stat()
                cache[path_str] = {"md5": md5, "size": stat.st_size, "mtime": stat.st_mtime}
            except OSError:
                pass

    _save_md5_cache(root, cache, {"hits": 0, "misses": 0, "bytes_saved": 0})
    print(f"缓存重建完成: {len(cache)} 个文件已索引")


# ============================================================
#  智能判重
# ============================================================

def _scan_existing_category_files(root, category_dirs):
    existing = {}
    scan_dirs = set(category_dirs.values())
    for cat_dir in scan_dirs:
        if not cat_dir.exists():
            continue
        for entry in cat_dir.rglob("*"):
            if entry.is_file():
                existing[str(entry)] = entry
    return existing


def _build_existing_md5_index(existing_files, root):
    cache, cache_stats_data = _load_md5_cache(root)
    index = {}
    cache_hits = 0
    cache_misses = 0
    bytes_saved = 0

    for path_str, file_path in sorted(existing_files.items()):
        md5, is_hit = _get_md5_cached(file_path, cache)
        if md5 is None:
            continue
        if is_hit:
            cache_hits += 1
            entry_size = cache.get(path_str, {}).get("size", 0)
            bytes_saved += entry_size
        else:
            cache_misses += 1
        if md5 not in index:
            index[md5] = file_path

    _clean_stale_cache(cache)

    cache_stats_data["hits"] = cache_stats_data.get("hits", 0) + cache_hits
    cache_stats_data["misses"] = cache_stats_data.get("misses", 0) + cache_misses
    cache_stats_data["bytes_saved"] = cache_stats_data.get("bytes_saved", 0) + bytes_saved
    _save_md5_cache(root, cache, cache_stats_data)

    if cache_hits + cache_misses > 0:
        print(f"  MD5 缓存: {cache_hits} 命中, {cache_misses} 重算, 共 {len(index)} 个唯一文件")
    return index


def _global_dedup_with_existing(new_files, existing_md5_index):
    new_md5_map = defaultdict(list)
    for f in new_files:
        md5 = _compute_md5(f)
        if md5 is None:
            continue
        new_md5_map[md5].append(f)

    unique_files = []
    duplicate_groups = []

    for md5_hash, paths in new_md5_map.items():
        existing_file = existing_md5_index.get(md5_hash)

        if existing_file is not None:
            for dup in paths:
                duplicate_groups.append({
                    "md5": md5_hash,
                    "kept": str(existing_file),
                    "kept_is_existing": True,
                    "duplicates": [str(dup)],
                    "duplicate_size": dup.stat().st_size,
                })
        elif len(paths) == 1:
            unique_files.append(paths[0])
        else:
            kept = paths[0]
            unique_files.append(kept)
            duplicate_groups.append({
                "md5": md5_hash,
                "kept": str(kept),
                "kept_is_existing": False,
                "duplicates": [str(p) for p in paths[1:]],
                "duplicate_size": sum(p.stat().st_size for p in paths[1:]),
            })

    return unique_files, duplicate_groups


# ============================================================
#  历史管理与保留策略
# ============================================================

def _load_history(root):
    history_path = Path(root) / HISTORY_FILENAME
    if not history_path.exists():
        return []
    try:
        with open(history_path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return []


def _save_history(root, entries):
    history_path = Path(root) / HISTORY_FILENAME
    with open(history_path, "w", encoding="utf-8") as f:
        json.dump(entries, f, ensure_ascii=False, indent=2)


def _prune_history(root, entries, max_count=None, max_days=None):
    if not max_count and not max_days:
        return entries, []

    cutoff_date = None
    if max_days:
        cutoff_date = datetime.now() - timedelta(days=max_days)

    expired = []
    kept = []

    for e in entries:
        ts_str = e.get("timestamp", "")
        try:
            ts = datetime.fromisoformat(ts_str)
        except (ValueError, TypeError):
            kept.append(e)
            continue

        if cutoff_date and ts < cutoff_date:
            expired.append(e)
        else:
            kept.append(e)

    if max_count and len(kept) > max_count:
        by_ts = sorted(kept, key=lambda e: e.get("timestamp", ""))
        to_keep = by_ts[-max_count:]
        expired.extend(e for e in kept if e not in to_keep)
        kept = to_keep

    return kept, expired


def _cleanup_expired_history(root, expired):
    for e in expired:
        report_base = e.get("report_base", "")
        for ext in [".json", ".md"]:
            p = Path(f"{report_base}{ext}")
            if p.exists():
                try:
                    p.unlink()
                except OSError:
                    pass

        log_path = e.get("log_backup")
        if log_path:
            lp = Path(log_path)
            if lp.exists():
                try:
                    lp.unlink()
                except OSError:
                    pass


def _append_history_entry(root, ts, stats, report_base, log_backup_path,
                          retention_count=None, retention_days=None):
    entries = _load_history(root)

    next_index = max((e["index"] for e in entries), default=0) + 1
    entry = {
        "index": next_index,
        "timestamp": ts.isoformat(),
        "total_files": stats["total_files"],
        "duplicates": stats["duplicates"],
        "freed_space": _format_size(stats["freed_space"]),
        "report_base": report_base,
        "log_backup": str(log_backup_path) if log_backup_path else None,
    }
    entries.append(entry)

    entries, expired = _prune_history(root, entries, retention_count, retention_days)
    _cleanup_expired_history(root, expired)

    if expired:
        print(f"历史清理: 移除了 {len(expired)} 条过期记录")

    _save_history(root, entries)


def show_history(root):
    entries = _load_history(root)
    if not entries:
        print("暂无整理历史记录。")
        return
    print(f"整理历史 ({len(entries)} 条):")
    print("-" * 70)
    for e in entries:
        print(f"  [{e['index']}] {e['timestamp'][:19]}")
        print(f"      文件: {e['total_files']}  重复: {e['duplicates']}  释放: {e['freed_space']}")
    print("-" * 70)
    print("查看报告: python organize_downloads.py --history-report <编号>")
    print("恢复某次: python organize_downloads.py --history-undo <编号>")


def show_history_report(root, index):
    entries = _load_history(root)
    entry = _find_history_entry(entries, index)
    report_base = entry.get("report_base", "")
    md_path = f"{report_base}.md"
    if Path(md_path).exists():
        with open(md_path, "r", encoding="utf-8") as f:
            print(f.read())
    else:
        print(f"报告文件不存在: {md_path}")


def history_undo(root, index):
    entries = _load_history(root)
    entry = _find_history_entry(entries, index)
    log_path = entry.get("log_backup")
    if not log_path or not Path(log_path).exists():
        sys.exit(f"操作日志备份不存在: {log_path}")

    log_data = _load_json(log_path)
    _execute_undo(Path(root), log_data)
    print(f"\n已恢复第 [{index}] 次整理的 {len(log_data.get('moves', []))} 个文件")

    archived = Path(log_path)
    if archived.exists():
        archived.unlink()
        print(f"归档日志已删除: {archived.name}")


def _find_history_entry(entries, index):
    for e in entries:
        if e["index"] == index:
            return e
    sys.exit(f"未找到编号为 {index} 的历史记录，可用 --history 查看列表。")


# ============================================================
#  健康检查
# ============================================================

def health_check(root, clean=False):
    issues = []

    entries = _load_history(root)
    history_by_report = {}
    history_by_log = {}
    for e in entries:
        rb = e.get("report_base", "")
        if rb:
            history_by_report[rb] = e
        lb = e.get("log_backup", "")
        if lb:
            history_by_log[lb] = e

    # 检查历史引用的报告是否存在
    for report_base, e in history_by_report.items():
        for ext in [".json", ".md"]:
            p = Path(f"{report_base}{ext}")
            if not p.exists():
                issues.append({
                    "type": "broken_report_link",
                    "detail": f"历史记录 [{e['index']}] 引用的报告不存在: {p.name}",
                    "path": str(p),
                })

    # 检查历史引用的日志是否存在
    for log_path, e in history_by_log.items():
        lp = Path(log_path)
        if not lp.exists():
            issues.append({
                "type": "broken_log_link",
                "detail": f"历史记录 [{e['index']}] 引用的日志不存在: {lp.name}",
                "path": log_path,
            })

    # 检查磁盘上的报告是否有对应的历史记录
    found_report_bases = set()
    for p in Path(root).glob("organize_report_*.json"):
        report_base = str(p.with_suffix(""))
        found_report_bases.add(report_base)
        if report_base not in history_by_report:
            issues.append({
                "type": "orphan_report",
                "detail": f"磁盘上的报告无对应历史记录: {p.name}",
                "path": str(p),
            })
    for p in Path(root).glob("organize_report_*.md"):
        report_base = str(p.with_suffix(""))
        if report_base in found_report_bases:
            continue
        if report_base not in history_by_report:
            issues.append({
                "type": "orphan_report",
                "detail": f"磁盘上的报告无对应历史记录: {p.name}",
                "path": str(p),
            })

    # 检查磁盘上的归档日志是否有对应的历史记录
    for p in Path(root).glob(".organize_log_*.json"):
        log_path = str(p)
        if log_path not in history_by_log:
            issues.append({
                "type": "orphan_log",
                "detail": f"磁盘上的归档日志无对应历史记录: {p.name}",
                "path": log_path,
            })

    # 检查 MD5 缓存一致性
    cache, cache_stats_data = _load_md5_cache(root)
    for path_str in list(cache.keys()):
        if not _check_dir_exists(path_str):
            issues.append({
                "type": "stale_cache",
                "detail": f"MD5 缓存引用已不存在的文件: {Path(path_str).name}",
                "path": path_str,
            })

    if not issues:
        print("健康检查通过，未发现问题。")
        return

    print(f"健康检查发现 {len(issues)} 个问题:\n")
    by_type = defaultdict(list)
    for iss in issues:
        by_type[iss["type"]].append(iss)

    type_labels = {
        "broken_report_link": "断链报告 (历史引用但报告不存在)",
        "broken_log_link": "断链日志 (历史引用但日志不存在)",
        "orphan_report": "孤儿报告 (磁盘存在但无历史记录)",
        "orphan_log": "孤儿日志 (磁盘存在但无历史记录)",
        "stale_cache": "过期缓存 (缓存引用的文件已不存在)",
    }

    for t, items in sorted(by_type.items()):
        label = type_labels.get(t, t)
        print(f"  [{label}] ({len(items)} 个)")
        for iss in items:
            print(f"    - {iss['detail']}")
        print()

    if clean:
        cleaned = 0
        for iss in issues:
            p = Path(iss["path"])
            if p.exists():
                try:
                    p.unlink()
                    cleaned += 1
                except OSError:
                    pass

        if issues:
            _clean_stale_cache(cache)
            _save_md5_cache(root, cache, cache_stats_data)

        print(f"清理完成: 移除了 {cleaned} 个文件/条目")
    else:
        print("使用 --health-clean 可自动清理以上问题。")


# ============================================================
#  分析阶段
# ============================================================

def analyze(root, categories, exclude_patterns, extra_exclude):
    exclude_patterns = list(set(exclude_patterns + extra_exclude))
    category_names = set(categories.keys()) | {"Others", "Duplicates"}

    category_dirs = {}
    for cat in sorted(category_names):
        if cat == "Duplicates":
            continue
        category_dirs[cat] = root / cat

    duplicates_dir = root / "Duplicates"

    new_files = []
    excluded_files = []
    for entry in root.rglob("*"):
        if entry.is_file():
            if should_exclude(entry.name, exclude_patterns):
                excluded_files.append(entry)
                continue
            if entry.relative_to(root).parts[0] in category_names:
                continue
            new_files.append(entry)

    existing_files = _scan_existing_category_files(root, category_dirs)
    existing_md5_index = _build_existing_md5_index(existing_files, root)

    unique_files, duplicate_groups = _global_dedup_with_existing(new_files, existing_md5_index)

    classified = classify_files(unique_files, categories)

    all_moved = list(unique_files)
    for g in duplicate_groups:
        for dup_path in g["duplicates"]:
            all_moved.append(Path(dup_path))

    empty_dir_plan = _predict_empty_dirs(root, category_names, all_moved)

    plan = {
        "root": root,
        "categories": categories,
        "category_names": category_names,
        "category_dirs": category_dirs,
        "duplicates_dir": duplicates_dir,
        "new_files": new_files,
        "excluded_files": excluded_files,
        "unique_files": unique_files,
        "duplicate_groups": duplicate_groups,
        "classified": classified,
        "empty_dir_plan": empty_dir_plan,
        "existing_md5_count": len(existing_md5_index),
    }
    return plan


def preview_plan(plan):
    print("=" * 60)
    print("整理预览")
    print("=" * 60)
    print(f"  目录:           {plan['root']}")
    print(f"  待处理新文件:    {len(plan['new_files'])}")
    print(f"  排除文件:        {len(plan['excluded_files'])}")
    print(f"  已有分类文件:    {plan['existing_md5_count']} (用于判重)")
    print(f"  唯一新文件:      {len(plan['unique_files'])}")
    dup_count = sum(len(g["duplicates"]) for g in plan["duplicate_groups"])
    print(f"  重复文件:        {dup_count}")
    print(f"  待清理空目录:    {len(plan['empty_dir_plan'])}")
    print()

    if plan["excluded_files"]:
        print("排除的文件:")
        for f in plan["excluded_files"]:
            print(f"  - {f.name}")
        print()

    if plan["unique_files"]:
        print("将移动到分类目录:")
        for cat, files in sorted(plan["classified"].items()):
            print(f"  [{cat}] ({len(files)} 个)")
            for f in files:
                print(f"    {f.name}")
        print()

    if plan["duplicate_groups"]:
        print("将移动到 Duplicates/ 的重复文件:")
        for group in plan["duplicate_groups"]:
            kept_name = Path(group["kept"]).name
            kept_note = " (已有文件)" if group.get("kept_is_existing") else ""
            print(f"  保留: {kept_name}{kept_note}")
            for dup in group["duplicates"]:
                print(f"    -> 重复: {Path(dup).name}")
        print()

    if plan["empty_dir_plan"]:
        print("将清理的空目录（含移动后会变空的目录）:")
        for d in plan["empty_dir_plan"]:
            print(f"  - {d}")
        print()

    print("=" * 60)


# ============================================================
#  执行阶段
# ============================================================

def execute(plan, dry_run, conflict_strategy=DEFAULT_CONFLICT_STRATEGY,
            retention_count=None, retention_days=None):
    root = plan["root"]
    category_dirs = plan["category_dirs"]
    duplicates_dir = plan["duplicates_dir"]
    categories = plan["categories"]
    unique_files = plan["unique_files"]
    duplicate_groups = plan["duplicate_groups"]
    excluded_files = plan["excluded_files"]
    new_files = plan["new_files"]
    category_names = plan["category_names"]
    predicted_empty_count = len(plan["empty_dir_plan"])

    classified = classify_files(unique_files, categories)

    total_files = len(new_files) + len(excluded_files)

    move_record = []
    conflict_stats = {"auto_rename": 0, "skip": 0, "overwrite_backup": 0}
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
        "conflict_stats": conflict_stats,
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
            size, dest, conflict, backup = _move_file(f, category_dirs[cat], dry_run, conflict_strategy)
            if conflict == "skip":
                conflict_stats["skip"] += 1
                move_record.append({
                    "source": str(f), "dest": dest, "size": size,
                    "conflict": "skip", "status": "skipped",
                })
                continue
            if conflict:
                conflict_stats[conflict] += 1
            stats["moved"] += 1
            stats["by_category"][cat] += 1
            entry = {"source": str(f), "dest": dest, "size": size}
            if conflict:
                entry["conflict"] = conflict
                entry["status"] = "moved_with_conflict"
            if backup:
                entry["backup_path"] = backup
            move_record.append(entry)
        print()

    if duplicate_groups:
        print(f"--- 重复文件 ({stats['duplicates']} 个) ---")
        _ensure_dir(duplicates_dir, dry_run)
        for group in duplicate_groups:
            for dup_path in group["duplicates"]:
                dup = Path(dup_path)
                size, dest, conflict, backup = _move_file(dup, duplicates_dir, dry_run, conflict_strategy)
                if conflict == "skip":
                    conflict_stats["skip"] += 1
                    stats["duplicates"] -= 1
                    move_record.append({
                        "source": str(dup), "dest": dest, "size": size,
                        "conflict": "skip", "status": "skipped",
                        "is_duplicate": True,
                        "duplicate_of": group["kept"],
                    })
                    continue
                if conflict:
                    conflict_stats[conflict] += 1
                stats["freed_space"] += size
                stats["moved"] += 1
                stats["by_category"]["Duplicates"] += 1
                kept_note = " (已有)" if group.get("kept_is_existing") else ""
                entry = {
                    "source": str(dup),
                    "dest": dest,
                    "size": size,
                    "is_duplicate": True,
                    "duplicate_of": group["kept"] + kept_note,
                }
                if conflict:
                    entry["conflict"] = conflict
                    entry["status"] = "moved_with_conflict"
                if backup:
                    entry["backup_path"] = backup
                move_record.append(entry)
        print()
        print(f"发现 {stats['duplicates']} 个重复文件（{len(duplicate_groups)} 组），已移至 Duplicates/")
        print()

    print("--- 清理空文件夹 ---")
    if dry_run:
        for d in plan["empty_dir_plan"]:
            print(f"  [DRY-RUN] 将删除空文件夹: {d}")
        stats["empty_dirs_removed"] = predicted_empty_count
    else:
        actual_removed = _cleanup_empty_dirs(root, dry_run)
        stats["empty_dirs_removed"] = actual_removed
    print()

    operate_ts = datetime.now()
    ts_str = operate_ts.strftime("%Y%m%d_%H%M%S")
    report_base = str(root / f"organize_report_{ts_str}")

    exclude_info = {"excluded_files": excluded_files}
    generate_report(stats, duplicate_groups, exclude_info, report_base if not dry_run else None)

    if not dry_run:
        archived_log_path = root / f".organize_log_{ts_str}.json"

        log_data = _build_operation_log(root, move_record)
        _save_json(archived_log_path, log_data)
        _save_json(root / LOG_FILENAME, log_data)
        print(f"操作日志已保存: {LOG_FILENAME}")
        print(f"恢复本次: python organize_downloads.py --undo \"{root}\"")

        _append_history_entry(
            root, operate_ts, stats, report_base, str(archived_log_path),
            retention_count=retention_count if retention_count is not None else DEFAULT_HISTORY_RETENTION_COUNT,
            retention_days=retention_days if retention_days is not None else DEFAULT_HISTORY_RETENTION_DAYS,
        )
        print(f"历史记录已更新: {HISTORY_FILENAME}")


# ============================================================
#  操作日志
# ============================================================

def _save_json(path, data):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def _load_json(path):
    if not Path(path).exists():
        sys.exit(f"文件不存在: {path}")
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def _build_operation_log(root, move_record):
    return {
        "version": 1,
        "timestamp": datetime.now().isoformat(),
        "root": str(root),
        "moves": move_record,
    }


def _execute_undo(root, log_data):
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

    print()
    print("--- 清理空文件夹 ---")
    removed = _cleanup_empty_dirs(root, dry_run=False)
    print(f"已删除 {removed} 个空文件夹")


# ============================================================
#  报告生成
# ============================================================

def generate_report(stats, duplicate_groups, exclude_info, report_base):
    conflict_stats = stats.get("conflict_stats", {})
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
    if conflict_stats:
        lines.append(f"  冲突处理:          "
                     f"自动重命名 {conflict_stats.get('auto_rename', 0)}, "
                     f"跳过 {conflict_stats.get('skip', 0)}, "
                     f"覆盖前备份 {conflict_stats.get('overwrite_backup', 0)}")
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
        _write_report_files(report_base, stats, duplicate_groups, exclude_info)


def _write_report_files(report_base, stats, duplicate_groups, exclude_info):
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
        "conflict_stats": stats.get("conflict_stats", {}),
        "by_category": dict(stats["by_category"]),
        "duplicate_groups": duplicate_groups,
        "excluded_files": [str(p) for p in exclude_info.get("excluded_files", [])],
        "moves": [{
            "source": m["source"],
            "destination": m["dest"],
            "conflict": m.get("conflict"),
            "backup_path": m.get("backup_path"),
            "status": m.get("status", "moved"),
        } for m in stats.get("move_record", [])],
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


CONFLICT_LABELS = {
    "auto_rename": "自动重命名",
    "skip": "跳过",
    "overwrite_backup": "覆盖前备份",
}


def _build_markdown_report(stats, duplicate_groups, exclude_info):
    conflict_stats = stats.get("conflict_stats", {})
    lines = []
    lines.append("# 下载文件夹整理报告")
    lines.append("")
    lines.append(f"**整理时间**: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}  ")
    lines.append(f"**整理目录**: `{stats['root']}`  ")
    lines.append("")
    lines.append("## 概览")
    lines.append("")
    lines.append("| 指标 | 数值 |")
    lines.append("|------|------|")
    lines.append(f"| 扫描文件总数 | {stats['total_files']} |")
    lines.append(f"| 排除文件数 | {stats['excluded']} |")
    lines.append(f"| 唯一文件数 | {stats['unique_files']} |")
    lines.append(f"| 重复文件数 | {stats['duplicates']} |")
    lines.append(f"| 已移动文件数 | {stats['moved']} |")
    lines.append(f"| 已删除空文件夹 | {stats['empty_dirs_removed']} |")
    lines.append(f"| 释放空间(重复) | {_format_size(stats['freed_space'])} |")
    if conflict_stats:
        conflict_summary = ", ".join(
            f"{CONFLICT_LABELS.get(k, k)} {v}" for k, v in conflict_stats.items() if v
        )
        lines.append(f"| 冲突处理 | {conflict_summary} |")
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
        has_conflicts = any(m.get("conflict") for m in stats["move_record"])
        has_backups = any(m.get("backup_path") for m in stats["move_record"])
        if has_conflicts or has_backups:
            lines.append("| 源路径 | 目标路径 | 大小 | 冲突处理 | 备份路径 |")
            lines.append("|--------|----------|------|----------|----------|")
        else:
            lines.append("| 源路径 | 目标路径 | 大小 |")
            lines.append("|--------|----------|------|")
        for m in stats["move_record"]:
            src_name = Path(m["source"]).name
            dst_name = Path(m["dest"]).name
            size_str = _format_size(m["size"])
            dst_parent = Path(m["dest"]).parent.name
            if has_conflicts or has_backups:
                conflict_label = CONFLICT_LABELS.get(m.get("conflict", ""), "")
                backup_full = m.get("backup_path", "")
                if backup_full:
                    try:
                        backup_rel = str(Path(backup_full).relative_to(Path(stats["root"])))
                    except (ValueError, OSError):
                        backup_rel = Path(backup_full).name
                else:
                    backup_rel = ""
                if m.get("status") == "skipped":
                    lines.append(f"| {src_name} | (未移动) | {size_str} | {conflict_label} | |")
                else:
                    lines.append(f"| {src_name} | {dst_parent}/{dst_name} | {size_str} | {conflict_label} | {backup_rel} |")
            else:
                lines.append(f"| {src_name} | {dst_parent}/{dst_name} | {size_str} |")
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
            kept_note = " **(已有文件，未移动)**" if group.get("kept_is_existing") else ""
            lines.append(f"- **保留**: {Path(group['kept']).name}{kept_note}  ")
            for dup in group["duplicates"]:
                lines.append(f"- **重复**: {Path(dup).name} → Duplicates/  ")
            lines.append(f"- 本组释放: {_format_size(group['duplicate_size'])}  ")
            lines.append("")

    if exclude_info.get("excluded_files"):
        lines.append("## 排除的文件")
        lines.append("")
        for f in exclude_info["excluded_files"]:
            lines.append(f"- {Path(f).name}  ")
        lines.append("")

    return lines


# ============================================================
#  主流程
# ============================================================

def organize(download_dir, config_path, dry_run, extra_exclude, confirm, conflict_strategy,
             retention_count=None, retention_days=None):
    if yaml is None:
        sys.exit("缺少 PyYAML 依赖，请执行: pip install pyyaml")

    categories, exclude_patterns = load_config(config_path)
    root = Path(download_dir).resolve()
    if not root.exists():
        sys.exit(f"目录不存在: {root}")
    if not root.is_dir():
        sys.exit(f"不是目录: {root}")

    print(f"整理目录: {root}")
    if dry_run:
        print("[!] 演练模式 — 不会实际修改任何文件")
    print()

    plan = analyze(root, categories, exclude_patterns, extra_exclude)

    print(f"扫描到 {len(plan['new_files'])} 个新文件"
          f"（已排除 {len(plan['excluded_files'])} 个，已有分类索引 {plan['existing_md5_count']} 个文件）")
    print()

    if confirm:
        preview_plan(plan)
        answer = input("是否继续执行? (y/N): ").strip().lower()
        if answer not in ("y", "yes"):
            print("已取消。")
            return
        print()

    if dry_run:
        preview_plan(plan)

    execute(plan, dry_run, conflict_strategy, retention_count, retention_days)


def undo(directory):
    root = Path(directory).resolve()
    if not root.exists():
        sys.exit(f"目录不存在: {root}")

    log_path = root / LOG_FILENAME
    log_data = _load_json(log_path)
    _execute_undo(root, log_data)

    if log_path.exists():
        log_path.unlink()
        print(f"操作日志已删除: {log_path.name}")


def _generate_default_config(output_path):
    user_patterns = ["*.tmp", "*.crdownload", "*.part", "*.!ut", "Thumbs.db", "desktop.ini"]
    config = {
        "categories": DEFAULT_CATEGORIES,
        "exclude_patterns": user_patterns,
    }
    with open(output_path, "w", encoding="utf-8") as f:
        yaml.dump(config, f, allow_unicode=True, default_flow_style=False, sort_keys=False)
    print(f"默认配置文件已生成: {output_path}")


# ============================================================
#  CLI
# ============================================================

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
        help="YAML 配置文件路径",
    )
    parser.add_argument(
        "--dry-run", "-n",
        action="store_true",
        help="演练模式：只打印将要执行的操作，不实际修改文件",
    )
    parser.add_argument(
        "--exclude", "-e",
        action="append",
        default=[],
        help="追加排除的文件名模式（支持通配符），可多次指定",
    )
    parser.add_argument(
        "--confirm",
        action="store_true",
        help="确认模式：先预览完整计划，确认后再实际执行",
    )
    parser.add_argument(
        "--on-conflict",
        choices=sorted(CONFLICT_STRATEGIES),
        default=DEFAULT_CONFLICT_STRATEGY,
        help=f"文件名冲突处理策略: auto_rename(默认), skip, overwrite_backup",
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
    parser.add_argument(
        "--history",
        action="store_true",
        help="列出最近几次整理记录",
    )
    parser.add_argument(
        "--history-report",
        metavar="INDEX",
        type=int,
        help="查看指定编号的整理报告",
    )
    parser.add_argument(
        "--history-undo",
        metavar="INDEX",
        type=int,
        help="恢复指定编号的整理操作",
    )
    parser.add_argument(
        "--history-retention-count",
        metavar="N",
        type=int,
        default=DEFAULT_HISTORY_RETENTION_COUNT,
        help=f"历史记录保留数量上限（默认: {DEFAULT_HISTORY_RETENTION_COUNT}）",
    )
    parser.add_argument(
        "--history-retention-days",
        metavar="N",
        type=int,
        default=DEFAULT_HISTORY_RETENTION_DAYS,
        help=f"历史记录保留天数上限（默认: {DEFAULT_HISTORY_RETENTION_DAYS}）",
    )
    parser.add_argument(
        "--health-check",
        action="store_true",
        help="健康检查：扫描报告/历史/日志/缓存之间的一致性问题",
    )
    parser.add_argument(
        "--health-clean",
        action="store_true",
        help="健康检查并自动清理发现的问题",
    )
    parser.add_argument(
        "--cache-stats",
        action="store_true",
        help="查看 MD5 缓存统计信息",
    )
    parser.add_argument(
        "--cache-stats-detail",
        action="store_true",
        help="查看 MD5 缓存详细条目",
    )
    parser.add_argument(
        "--cache-clean",
        action="store_true",
        help="清理 MD5 缓存中的失效条目",
    )
    parser.add_argument(
        "--cache-rebuild",
        action="store_true",
        help="强制重建 MD5 缓存",
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

    if args.health_check or args.health_clean:
        health_check(download_dir, clean=args.health_clean)
        return

    if args.cache_stats or args.cache_stats_detail:
        cache_stats(download_dir, detail=args.cache_stats_detail)
        return

    if args.cache_clean:
        cache_clean(download_dir)
        return

    if args.cache_rebuild:
        cache_rebuild(download_dir)
        return

    if args.history:
        show_history(download_dir)
        return

    if args.history_report:
        show_history_report(download_dir, args.history_report)
        return

    if args.history_undo:
        history_undo(download_dir, args.history_undo)
        return

    if args.undo:
        undo(download_dir)
        return

    organize(
        download_dir=download_dir,
        config_path=args.config,
        dry_run=args.dry_run,
        extra_exclude=args.exclude,
        confirm=args.confirm,
        conflict_strategy=args.on_conflict,
        retention_count=args.history_retention_count,
        retention_days=args.history_retention_days,
    )


if __name__ == "__main__":
    main()