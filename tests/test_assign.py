"""归段逻辑自检：仅依赖标准库。运行 `python3 tests/test_assign.py`。"""
import csv
import io
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import assign  # noqa: E402
from app.assign import AssignError, assign_to_path  # noqa: E402

SAMPLES = Path(__file__).resolve().parents[1] / "samples"


def check(name, cond, detail=""):
    print(("PASS" if cond else "FAIL"), "-", name)
    if not cond:
        if detail:
            print("  ", detail)
        raise SystemExit(1)


def run(sample_bytes, segment_files):
    tmpdir = tempfile.mkdtemp(prefix="assign_test_")
    out_path = os.path.join(tmpdir, "out.csv")
    segs = [(name, io.BytesIO(data)) for name, data in segment_files]
    try:
        stats = assign_to_path(io.BytesIO(sample_bytes), "flight.csv", segs, out_path)
        with open(out_path, encoding="utf-8", newline="") as fh:
            rows = list(csv.reader(fh))
        return stats, rows
    finally:
        try:
            os.unlink(out_path)
        except OSError:
            pass
        os.rmdir(tmpdir)


def main():
    sample_bytes = (SAMPLES / "assign_samples.csv").read_bytes()
    seg1 = (SAMPLES / "assign_segments_1.csv").read_bytes()
    seg2 = (SAMPLES / "assign_segments_2.csv").read_bytes()

    stats, rows = run(sample_bytes, [("seg1.csv", seg1), ("seg2.csv", seg2)])

    header = rows[0]
    check("追加归段列", header[-3:] == ["match_status", "segment_file_no", "segment_seq"],
          str(header))
    check("保留采样原列", header[:5] == ["tail_number", "sample_utc", "flight_no", "phase", "param_value"])
    check("保留采样行序", [r[2] for r in rows[1:]] == ["CA1001", "CA1001", "CA1001", "CA1002", "CA1002", "CA1003", "CA9999"])
    check("采样统计", stats["samples"] == 7, str(stats))
    check("航段统计", stats["segments"] == 5 and stats["segment_file_counts"] == [3, 2], str(stats))

    by_sample = {r[4]: r for r in rows[1:]}
    # B-8001 00:30 落在文件 1 seq=1
    check("唯一命中填文件序号与航段序号",
          by_sample["101"][-3:] == ["matched", "1", "1"],
          str(by_sample["101"]))
    # B-8002 01:30 同时落在文件 1 与文件 2，分别计数 → multiple
    check("跨文件重叠分别计数 → multiple",
          by_sample["201"][-3:] == ["multiple", "", ""],
          str(by_sample["201"]))
    # B-8003 命中文件 2
    check("文件 2 唯一命中",
          by_sample["301"][-3:] == ["matched", "2", "1"],
          str(by_sample["301"]))
    check("未知机尾 → unmatched",
          by_sample["901"][-3:] == ["unmatched", "", ""])

    # 边界：含起飞、不含落地
    boundary = (
        "tail_number,sample_utc,v\n"
        "X,2026-01-10T02:00:00Z,at_dep\n"
        "X,2026-01-10T04:00:00Z,at_arr\n"
    )
    seg = (
        "tail_number,seq,dep_utc,arr_utc\n"
        "X,1,2026-01-10T02:00:00Z,2026-01-10T04:00:00Z\n"
    )
    _stats, brows = run(boundary.encode(), [("s.csv", seg.encode())])
    check("sample_utc == dep 命中", brows[1][-3:] == ["matched", "1", "1"], str(brows[1]))
    check("sample_utc == arr 不命中", brows[2][-3:] == ["unmatched", "", ""], str(brows[2]))

    # 同一文件内的重叠也分别计数
    overlap_same = (
        "tail_number,seq,dep_utc,arr_utc\n"
        "Y,1,2026-01-10T00:00:00Z,2026-01-10T03:00:00Z\n"
        "Y,2,2026-01-10T01:00:00Z,2026-01-10T02:00:00Z\n"
    )
    same_sample = "tail_number,sample_utc\nY,2026-01-10T01:30:00Z\n"
    _stats, srows = run(same_sample.encode(), [("y.csv", overlap_same.encode())])
    check("同文件重叠 → multiple", srows[1][-3:] == ["multiple", "", ""], str(srows[1]))

    # 不要求采样排序，行序必须原样保留
    unsorted = (
        "tail_number,sample_utc,marker\n"
        "Z,2026-01-10T05:00:00Z,third\n"
        "Z,2026-01-10T01:00:00Z,first\n"
        "Z,2026-01-10T03:00:00Z,second\n"
    )
    zseg = (
        "tail_number,seq,dep_utc,arr_utc\n"
        "Z,1,2026-01-10T00:00:00Z,2026-01-10T02:00:00Z\n"
        "Z,2,2026-01-10T02:00:00Z,2026-01-10T04:00:00Z\n"
        "Z,3,2026-01-10T04:00:00Z,2026-01-10T06:00:00Z\n"
    )
    _stats, urows = run(unsorted.encode(), [("z.csv", zseg.encode())])
    check("未排序采样保留原行序", [r[2] for r in urows[1:]] == ["third", "first", "second"])
    check("未排序采样逐行归段",
          [r[-1] for r in urows[1:]] == ["3", "1", "2"], str(urows))

    # 表头大小写不敏感，但输出保留原始列名写法
    mixed = (
        "Tail_Number,Sample_UTC,Extra\r\n"
        "M,2026-01-10T01:00:00Z,keep\r\n"
    )
    mseg = (
        "TAIL_NUMBER,Seq,DEP_UTC,ARR_UTC\n"
        "M,1,2026-01-10T00:00:00Z,2026-01-10T02:00:00Z\n"
    )
    _stats, mrows = run(mixed.encode(), [("m.csv", mseg.encode())])
    check("保留原始表头写法",
          mrows[0] == ["Tail_Number", "Sample_UTC", "Extra",
                       "match_status", "segment_file_no", "segment_seq"],
          str(mrows[0]))
    check("表头大小写不敏感且取值原样保留",
          mrows[1][:3] == ["M", "2026-01-10T01:00:00Z", "keep"], str(mrows[1]))

    # 坏行：航段文件第 2 个数据行
    bad_seg = (
        "tail_number,seq,dep_utc,arr_utc\n"
        "B-1,1,2026-01-10T00:00:00Z,2026-01-10T02:00:00Z\n"
        "B-1,2,not-a-time,2026-01-10T04:00:00Z\n"
    )
    try:
        run("tail_number,sample_utc\nB-1,2026-01-10T01:00:00Z\n".encode(),
            [("bad.csv", bad_seg.encode())])
        check("航段坏行应报错", False)
    except AssignError as exc:
        check("航段坏行指出文件与数据行",
              exc.file_label == "航段文件 1「bad.csv」" and exc.data_row == 2, str(exc))

    # 坏行：飞参文件第 3 个数据行，且不得留下输出
    bad_sample = (
        "tail_number,sample_utc,v\n"
        "B-1,2026-01-10T01:00:00Z,ok\n"
        "B-1,bad-time,bad\n"
        "B-1,2026-01-10T02:00:00Z,ok\n"
    )
    good_seg = "tail_number,seq,dep_utc,arr_utc\nB-1,1,2026-01-10T00:00:00Z,2026-01-10T03:00:00Z\n"
    tmpdir = tempfile.mkdtemp(prefix="assign_bad_")
    out_path = os.path.join(tmpdir, "out.csv")
    try:
        try:
            assign_to_path(io.BytesIO(bad_sample.encode()), "samples.csv",
                           [("good.csv", io.BytesIO(good_seg.encode()))], out_path)
            check("飞参坏行应报错", False)
        except AssignError as exc:
            check("飞参坏行指出文件与数据行",
                  exc.file_label == "飞参文件「samples.csv」" and exc.data_row == 2, str(exc))
        check("坏行后不留下部分结果", not os.path.exists(out_path))
    finally:
        try:
            os.unlink(out_path)
        except OSError:
            pass
        os.rmdir(tmpdir)

    # 上限：临时收紧常量，确认超限报错
    old_samples, old_segments = assign.MAX_SAMPLE_ROWS, assign.MAX_SEGMENT_ROWS
    try:
        assign.MAX_SAMPLE_ROWS = 2
        many = b"tail_number,sample_utc\nA,2026-01-10T00:00:00Z\nA,2026-01-10T01:00:00Z\nA,2026-01-10T02:00:00Z\n"
        seg_a = b"tail_number,seq,dep_utc,arr_utc\nA,1,2026-01-10T00:00:00Z,2026-01-10T03:00:00Z\n"
        try:
            run(many, [("a.csv", seg_a)])
            check("采样超过上限应报错", False)
        except AssignError as exc:
            check("采样超限指出数据行", exc.data_row == 3, str(exc))

        assign.MAX_SAMPLE_ROWS = old_samples
        assign.MAX_SEGMENT_ROWS = 1
        two_segs = b"tail_number,seq,dep_utc,arr_utc\nA,1,2026-01-10T00:00:00Z,2026-01-10T01:00:00Z\nA,2,2026-01-10T01:00:00Z,2026-01-10T02:00:00Z\n"
        try:
            run(b"tail_number,sample_utc\nA,2026-01-10T00:30:00Z\n", [("a.csv", two_segs)])
            check("航段超过上限应报错", False)
        except AssignError as exc:
            check("航段超限指出数据行", exc.data_row == 2, str(exc))
    finally:
        assign.MAX_SAMPLE_ROWS, assign.MAX_SEGMENT_ROWS = old_samples, old_segments

    # 航段文件数量校验
    try:
        run(sample_bytes, [("s1.csv", seg1)])
        check("1 份航段文件允许", True)
    except AssignError as exc:
        check("1 份航段文件允许", False, str(exc))
    try:
        run(sample_bytes, [("s1.csv", seg1)] * 5)
        check("5 份航段文件应报错", False)
    except AssignError as exc:
        check("5 份航段文件指出数量", "1～4" in str(exc), str(exc))

    print("\n归段全部自检通过 ✅")


if __name__ == "__main__":
    main()
