"""飞参采样按 UTC 航段归段（streaming）。

业务规则
--------
- 飞参 CSV 必含 ``tail_number`` 与 ``sample_utc``；除这两列外保留全部原列与行序。
- 航段 CSV 为清洗后导出的 UTC 航段，必含 ``tail_number,seq,dep_utc,arr_utc``。
- 按机尾号匹配起降区间：``dep_utc <= sample_utc < arr_utc``（含起飞、不含落地）。
- 不同航段文件的记录分别计数；命中记录恰好 1 条时写入航段文件上传序号与 seq，
  0 条标记 ``unmatched``，多于 1 条标记 ``multiple``。
- 单次最多 200 万条采样、合计 20 万个航段；航段文件 1～4 份。
- 飞参逐行读取、结果逐行写入磁盘，服务端不把整份飞参或结果留在内存。
- 任一输入行非法时抛出 :class:`AssignError`（含文件与数据行号），不产生部分结果。
"""
from __future__ import annotations

import bisect
import csv
import io
import os
from dataclasses import dataclass, field
from datetime import datetime, timezone

MAX_SAMPLE_ROWS = 2_000_000
MAX_SEGMENT_ROWS = 200_000
MIN_SEGMENT_FILES = 1
MAX_SEGMENT_FILES = 4

SAMPLE_REQUIRED = ("tail_number", "sample_utc")
SEGMENT_REQUIRED = ("tail_number", "seq", "dep_utc", "arr_utc")

STATUS_MATCHED = "matched"
STATUS_UNMATCHED = "unmatched"
STATUS_MULTIPLE = "multiple"
OUTPUT_COLUMNS = ("match_status", "segment_file_no", "segment_seq")

_EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)
_NEG = -(1 << 62)  # 小于任何合理 epoch 微秒值，用作线段树哨兵
_TREE_THRESHOLD = 16  # 重叠航段较多时改用线段树查询


class AssignError(Exception):
    """输入文件或数据行不合法；str(exc) 直接面向用户。"""

    def __init__(self, file_label: str, data_row: int | None, message: str):
        self.file_label = file_label
        self.data_row = data_row
        self.message = message
        super().__init__(self._render())

    def _render(self) -> str:
        where = self.file_label
        if self.data_row is not None:
            where += f"第 {self.data_row} 个数据行"
        return f"{where}：{self.message}"


# ---------------------------------------------------------------------------
# 基础解析
# ---------------------------------------------------------------------------

def parse_utc(text: str) -> datetime | None:
    """解析 UTC 时间文本；无时区信息时按 UTC 处理，失败返回 None。"""
    text = (text or "").strip()
    if not text:
        return None
    if text[-1] in ("Z", "z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        # 兼容少量非 ISO 写法（例如使用斜杠日期）
        for fmt in ("%Y/%m/%d %H:%M:%S", "%Y/%m/%d %H:%M"):
            try:
                parsed = datetime.strptime(text, fmt)
                break
            except ValueError:
                continue
        else:
            return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def utc_to_us(value: datetime) -> int:
    """UTC datetime → 精确的 epoch 微秒整数，避免浮点误差。"""
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    delta = value.astimezone(timezone.utc) - _EPOCH
    return ((delta.days * 86400 + delta.seconds) * 1_000_000
            + delta.microseconds)


def _cell(record: list, index: int) -> str:
    return record[index].strip() if index < len(record) else ""


def _normalise_width(record: list, header: list, file_label: str,
                     data_row: int) -> list:
    """把数据行补齐/裁齐到表头宽度；非空的额外列视为坏行。"""
    if len(record) > len(header):
        extras = record[len(header):]
        if any(str(c).strip() for c in extras):
            raise AssignError(
                file_label, data_row,
                f"列数多于表头（表头 {len(header)} 列，本行 {len(record)} 列）")
        return list(record[:len(header)])
    if len(record) < len(header):
        return list(record) + [""] * (len(header) - len(record))
    return list(record)


def _open_csv(file_obj, file_label: str):
    """打开 UTF-8 CSV，返回 (规范化表头, 数据行迭代器)。

    数据行迭代器产出 ``(data_row, record)``，data_row 从 1 起、跳过纯空白记录。
    """
    try:
        file_obj.seek(0)
    except (AttributeError, OSError):
        pass
    text = io.TextIOWrapper(file_obj, encoding="utf-8-sig", newline="")
    reader = csv.reader(text)

    def cleanup():
        # 不让 TextIOWrapper 关闭调用方的底层文件
        try:
            text.detach()
        except Exception:
            pass

    header = None
    try:
        for record in reader:
            if not any(str(c).strip() for c in record):
                continue
            header = [str(c).lstrip("﻿") for c in record]
            break
    except UnicodeDecodeError:
        cleanup()
        raise AssignError(file_label, None, "文件不是合法的 UTF-8 编码")
    except csv.Error as exc:
        cleanup()
        raise AssignError(file_label, None, f"CSV 解析失败：{exc}")
    except BaseException:
        cleanup()
        raise

    if header is None:
        cleanup()
        raise AssignError(file_label, None, "CSV 内容为空")

    def rows():
        data_row = 0
        try:
            for record in reader:
                if not any(str(c).strip() for c in record):
                    continue
                data_row += 1
                yield data_row, record
        except UnicodeDecodeError:
            raise AssignError(file_label, data_row + 1,
                              "文件不是合法的 UTF-8 编码")
        except csv.Error as exc:
            raise AssignError(file_label, data_row + 1,
                              f"CSV 解析失败：{exc}")
        finally:
            cleanup()

    return header, rows()


def _header_index(header: list, required: tuple, file_label: str) -> dict:
    normalised = [str(c).strip().lower() for c in header]
    missing = [name for name in required if name not in normalised]
    if missing:
        raise AssignError(
            file_label, None,
            "缺少必需列: " + ", ".join(missing)
            + "。需要的表头为: " + ", ".join(required))
    return {name: normalised.index(name) for name in required}


# ---------------------------------------------------------------------------
# 航段索引
# ---------------------------------------------------------------------------

@dataclass
class _TailIndex:
    segments: list          # [(dep_us, arr_us, file_no, seq)]
    starts: list
    ends: list
    overlapping: bool
    prefix_max: list = field(default_factory=list)
    tree: list | None = None
    tree_size: int = 0

    def match(self, when_us: int) -> tuple[int, list]:
        """返回 (命中记录数上限 2, 命中记录列表)。

        命中列表在 0/1 条时完整；2 条表示“多段”，无需继续找第三条。
        """
        hi = bisect.bisect_right(self.starts, when_us)
        if hi == 0:
            return 0, []
        if not self.overlapping:
            item = self.segments[hi - 1]
            return (1, [item]) if item[1] > when_us else (0, [])
        if self.prefix_max[hi - 1] <= when_us:
            return 0, []

        if self.tree is not None:
            first = _tree_find_first(self.tree, self.tree_size, hi, when_us, 0)
            if first < 0:
                return 0, []
            second = _tree_find_first(
                self.tree, self.tree_size, hi, when_us, first + 1)
            if second < 0:
                return 1, [self.segments[first]]
            return 2, [self.segments[first], self.segments[second]]

        # 小规模重叠：从后向前扫描，prefix_max 为 0 时提前结束
        found = []
        idx = hi - 1
        while idx >= 0 and self.prefix_max[idx] > when_us:
            if self.ends[idx] > when_us:
                found.append(self.segments[idx])
                if len(found) >= 2:
                    return 2, found
            idx -= 1
        return len(found), found


def _build_tail_index(segments: list) -> _TailIndex:
    segments.sort(key=lambda item: item[0])
    starts = [item[0] for item in segments]
    ends = [item[1] for item in segments]

    overlapping = False
    max_end = None
    for dep, arr, _file_no, _seq in segments:
        if max_end is not None and dep < max_end:
            overlapping = True
            break
        if max_end is None or arr > max_end:
            max_end = arr

    prefix_max = []
    tree = None
    tree_size = 0
    if overlapping:
        running = _NEG
        for arr in ends:
            if arr > running:
                running = arr
            prefix_max.append(running)
        if len(segments) > _TREE_THRESHOLD:
            tree_size = 1
            while tree_size < len(segments):
                tree_size <<= 1
            tree = [_NEG] * (tree_size * 2)
            for i, arr in enumerate(ends):
                tree[tree_size + i] = arr
            for i in range(tree_size - 1, 0, -1):
                left = tree[i << 1]
                right = tree[i << 1 | 1]
                tree[i] = left if left >= right else right

    return _TailIndex(segments, starts, ends, overlapping,
                      prefix_max, tree, tree_size)


def _tree_find_first(tree: list, size: int, hi: int, when_us: int,
                     lo: int) -> int:
    """在线段树 [lo, hi) 中找最左侧 end > when_us 的下标，找不到返回 -1。"""
    stack = [(1, 0, size)]
    while stack:
        node, left, right = stack.pop()
        if right <= lo or left >= hi or tree[node] <= when_us:
            continue
        if right - left == 1:
            return left
        mid = (left + right) >> 1
        # 后压右子树，保证左子树先出栈
        stack.append((node << 1 | 1, mid, right))
        stack.append((node << 1, left, mid))
    return -1


# ---------------------------------------------------------------------------
# 输入读取
# ---------------------------------------------------------------------------

def _read_segments(segment_files: list) -> tuple[dict, list]:
    """读取 1～4 份航段 CSV。

    segment_files: [(filename, binary_file), ...]（上传顺序即文件序号）。
    返回 (按机尾的索引 dict, 每份文件的航段数列表)。
    """
    if not (MIN_SEGMENT_FILES <= len(segment_files) <= MAX_SEGMENT_FILES):
        raise AssignError(
            "航段文件", None,
            f"需要 {MIN_SEGMENT_FILES}～{MAX_SEGMENT_FILES} 份航段 CSV，"
            f"实际收到 {len(segment_files)} 份")

    tails: dict[str, list] = {}
    per_file_counts = []
    total = 0

    for file_no, (filename, file_obj) in enumerate(segment_files, start=1):
        label = f"航段文件 {file_no}「{filename or '未命名'}」"
        header, rows = _open_csv(file_obj, label)
        try:
            idx = _header_index(header, SEGMENT_REQUIRED, label)
        except BaseException:
            rows.close()
            raise
        count = 0
        for data_row, record in rows:
            record = _normalise_width(record, header, label, data_row)

            tail = _cell(record, idx["tail_number"])
            if not tail:
                raise AssignError(label, data_row, "tail_number（机尾号）为空")

            seq_raw = _cell(record, idx["seq"])
            try:
                seq = int(seq_raw)
            except ValueError:
                raise AssignError(
                    label, data_row, f"seq 不是整数: {seq_raw!r}")

            dep_raw = _cell(record, idx["dep_utc"])
            dep = parse_utc(dep_raw)
            if dep is None:
                raise AssignError(
                    label, data_row,
                    f"dep_utc 无法解析为 UTC 时间: {dep_raw!r}")
            arr_raw = _cell(record, idx["arr_utc"])
            arr = parse_utc(arr_raw)
            if arr is None:
                raise AssignError(
                    label, data_row,
                    f"arr_utc 无法解析为 UTC 时间: {arr_raw!r}")

            dep_us = utc_to_us(dep)
            arr_us = utc_to_us(arr)
            if arr_us <= dep_us:
                raise AssignError(
                    label, data_row,
                    f"arr_utc 必须晚于 dep_utc（当前 {arr_raw} <= {dep_raw}）")

            total += 1
            if total > MAX_SEGMENT_ROWS:
                raise AssignError(
                    label, data_row,
                    f"航段合计超过上限 {MAX_SEGMENT_ROWS} 条")
            tails.setdefault(tail, []).append((dep_us, arr_us, file_no, seq))
            count += 1
        per_file_counts.append(count)

    if total == 0:
        raise AssignError("航段文件", None, "航段 CSV 没有任何数据行")
    return tails, per_file_counts


def assign_to_path(sample_file, sample_filename: str, segment_files: list,
                   out_path: str) -> dict:
    """流式归段并写入 ``out_path``；出错时删除输出文件并抛出 AssignError。

    sample_file / segment_files 中每个 file 均为可 seek 的二进制文件对象。
    返回统计信息（不含任何采样内容）。
    """
    sample_label = f"飞参文件「{sample_filename or '未命名'}」"

    try:
        tails, segment_counts = _read_segments(segment_files)
        indexes = {tail: _build_tail_index(items)
                   for tail, items in tails.items()}
        del tails

        header, rows = _open_csv(sample_file, sample_label)
        try:
            idx = _header_index(header, SAMPLE_REQUIRED, sample_label)
        except BaseException:
            rows.close()
            raise
        output_header = list(header)
        used = set(output_header)
        for base in OUTPUT_COLUMNS:
            name = base
            suffix = 2
            while name in used:
                name = f"{base}_{suffix}"
                suffix += 1
            output_header.append(name)
            used.add(name)

        stats = {
            "samples": 0,
            "matched": 0,
            "unmatched": 0,
            "multiple": 0,
            "segments": sum(segment_counts),
            "segment_files": len(segment_files),
            "segment_file_counts": segment_counts,
        }

        with open(out_path, "w", encoding="utf-8", newline="") as out:
            writer = csv.writer(out, lineterminator="\n")
            writer.writerow(output_header)

            for data_row, record in rows:
                if data_row > MAX_SAMPLE_ROWS:
                    raise AssignError(
                        sample_label, data_row,
                        f"采样超过上限 {MAX_SAMPLE_ROWS} 条")
                record = _normalise_width(record, header, sample_label,
                                          data_row)

                tail = _cell(record, idx["tail_number"])
                if not tail:
                    raise AssignError(
                        sample_label, data_row,
                        "tail_number（机尾号）为空")
                raw_utc = _cell(record, idx["sample_utc"])
                sample_dt = parse_utc(raw_utc)
                if sample_dt is None:
                    raise AssignError(
                        sample_label, data_row,
                        f"sample_utc 无法解析为 UTC 时间: {raw_utc!r}")

                tail_index = indexes.get(tail)
                if tail_index is None:
                    count, matches = 0, []
                else:
                    count, matches = tail_index.match(utc_to_us(sample_dt))

                if count == 0:
                    appended = [STATUS_UNMATCHED, "", ""]
                    stats["unmatched"] += 1
                elif count == 1:
                    item = matches[0]
                    appended = [STATUS_MATCHED, item[2], item[3]]
                    stats["matched"] += 1
                else:
                    appended = [STATUS_MULTIPLE, "", ""]
                    stats["multiple"] += 1

                writer.writerow(list(record) + appended)
                stats["samples"] += 1

        if stats["samples"] == 0:
            raise AssignError(sample_label, None, "CSV 没有任何数据行")
        return stats
    except BaseException:
        try:
            os.unlink(out_path)
        except OSError:
            pass
        raise
