"""FastAPI 入口：航段时间清洗（DST 安全入仓）+ 飞参归段。"""
from __future__ import annotations

import json
import os
import re
import tempfile
import zipfile
from pathlib import Path

from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import FileResponse, Response
from starlette.background import BackgroundTask
from starlette.datastructures import UploadFile as StarletteUploadFile
from starlette.formparsers import MultiPartException

from .assign import AssignError, assign_to_path
from .solver import CsvFormatError, build_export_csv, solve_csv

BASE_DIR = Path(__file__).resolve().parent
SAMPLE_PATH = BASE_DIR.parent / "samples" / "dst_sample.csv"
ASSIGN_SAMPLE_DIR = BASE_DIR.parent / "samples"
INDEX_PATH = BASE_DIR / "static" / "index.html"
MAX_UPLOAD_BYTES = 10 * 1024 * 1024  # 10 MB（清洗 CSV）

ASSIGN_SAMPLE_FILES = {
    "assign_samples.csv": ASSIGN_SAMPLE_DIR / "assign_samples.csv",
    "assign_segments_1.csv": ASSIGN_SAMPLE_DIR / "assign_segments_1.csv",
    "assign_segments_2.csv": ASSIGN_SAMPLE_DIR / "assign_segments_2.csv",
}

# 兼容不同客户端使用的字段名；按顺序优先取第一个命中的字段
SAMPLE_FIELDS = (
    "samples", "sample_file", "sample_csv", "samples_csv", "sample",
    "flight_params", "flight_param", "flight_csv", "flight", "file",
)
SEGMENT_FIELDS = (
    "segments", "segment_files", "segment_file", "segment_csv",
    "segment_csvs", "utc_segments", "segment", "files",
)

app = FastAPI(title="航段时间清洗 · DST 安全入仓", version="1.1.0")


def _remove_file(path: str) -> None:
    try:
        os.unlink(path)
    except OSError:
        pass


def _is_upload(value) -> bool:
    return (isinstance(value, StarletteUploadFile)
            and bool(getattr(value, "filename", "") or ""))


def _pick_assign_uploads(form) -> tuple[UploadFile, list[UploadFile]]:
    """从 multipart 表单中取飞参与 1～4 份航段文件（支持常见字段别名）。"""
    uploads = [(key, value) for key, value in form.multi_items()
               if _is_upload(value)]
    if not uploads:
        raise HTTPException(400, "请上传 1 份飞参 CSV 与 1～4 份航段 CSV")

    sample = None
    for key in SAMPLE_FIELDS:
        matches = [value for field, value in uploads if field == key]
        if matches:
            sample = matches[0]
            break

    segments: list[UploadFile] = []
    for key in SEGMENT_FIELDS:
        matches = [value for field, value in uploads if field == key]
        if matches:
            segments = matches
            break

    if sample is None:
        # 未知字段名时按字段含义推断
        for key, value in uploads:
            lowered = key.lower()
            if any(tok in lowered for tok in ("sample", "flight", "param")):
                sample = value
                break
    if sample is None and segments:
        rest = [value for _key, value in uploads
                if all(value is not seg for seg in segments)]
        if rest:
            sample = rest[0]
        elif len(segments) >= 2:
            # 全部文件都放在同一个字段时的兜底：首个为飞参，其余为航段
            sample = segments[0]
            segments = segments[1:]

    if sample is None:
        raise HTTPException(400, "请上传 1 份飞参 CSV（multipart 字段 samples）")
    if not segments:
        # 兼容：除飞参外的其余上传文件都作为航段文件
        segments = [value for _field, value in uploads if value is not sample]
    if not segments:
        raise HTTPException(400, "请上传 1～4 份航段 CSV（multipart 字段 segments）")
    return sample, segments


async def _read_csv_text(file: UploadFile) -> str:
    data = await file.read()
    if len(data) > MAX_UPLOAD_BYTES:
        raise HTTPException(413, "文件超过 10 MB 上限")
    try:
        return data.decode("utf-8-sig")
    except UnicodeDecodeError:
        raise HTTPException(400, "文件不是合法的 UTF-8 编码 CSV")


@app.get("/", include_in_schema=False)
async def index():
    return FileResponse(INDEX_PATH)


@app.get("/api/sample", include_in_schema=False)
async def sample():
    """内置夏令时样例，可直接下载或在前端一键载入。"""
    return FileResponse(SAMPLE_PATH, media_type="text/csv; charset=utf-8",
                        filename="dst_sample.csv")


@app.get("/api/assign/sample.zip", include_in_schema=False)
def assign_sample_zip():
    """下载归段样例压缩包（飞参 + 两份航段，含跨文件重叠区间）。"""
    fd, path = tempfile.mkstemp(prefix="assign_sample_", suffix=".zip")
    os.close(fd)
    try:
        with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as archive:
            for name, source in ASSIGN_SAMPLE_FILES.items():
                archive.write(source, arcname=name)
    except BaseException:
        _remove_file(path)
        raise
    return FileResponse(
        path, media_type="application/zip", filename="assign_sample.zip",
        background=BackgroundTask(_remove_file, path))


@app.get("/api/assign/sample/{name}", include_in_schema=False)
def assign_sample_file(name: str):
    """下载单个归段样例文件（供网页一键载入）。"""
    path = ASSIGN_SAMPLE_FILES.get(name)
    if path is None or not path.exists():
        raise HTTPException(404, f"未知的归段样例文件: {name}")
    media = "application/zip" if name.endswith(".zip") else "text/csv; charset=utf-8"
    return FileResponse(path, media_type=media, filename=name)


@app.post("/api/solve")
async def solve(file: UploadFile = File(...)):
    """上传 CSV，返回每个机尾的求解结果（唯一解/无解/多解/错误）。"""
    text = await _read_csv_text(file)
    try:
        return solve_csv(text)
    except CsvFormatError as exc:
        raise HTTPException(400, str(exc))


@app.post("/api/export")
async def export(file: UploadFile = File(...), tail: str = Form(...)):
    """导出机尾的 UTC CSV。仅当整链唯一解时允许；tail=__all__ 导出全部可导出机尾。"""
    text = await _read_csv_text(file)
    try:
        result = solve_csv(text)
    except CsvFormatError as exc:
        raise HTTPException(400, str(exc))

    if tail == "__all__":
        tails = [t["tail"] for t in result["tails"] if t["status"] == "unique"]
        if not tails:
            raise HTTPException(409, "没有可导出的机尾：仅整链唯一解的机尾允许导出")
        filename = "utc_export_all.csv"
    else:
        match = next((t for t in result["tails"] if t["tail"] == tail), None)
        if match is None:
            raise HTTPException(404, f"CSV 中不存在机尾 {tail!r}")
        if match["status"] != "unique":
            raise HTTPException(
                409, f"机尾 {tail} 状态为 {match['status']}，非整链唯一解，禁止导出")
        tails = [tail]
        filename = f"utc_export_{tail}.csv"

    csv_text = build_export_csv(result, tails)
    return Response(
        content=csv_text,
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@app.post("/api/assign")
async def assign(request: Request):
    """上传 1 份飞参 CSV + 1～4 份 UTC 航段 CSV，返回归段结果 CSV。

    服务端先逐行读取输入并在磁盘临时文件写完全部结果，之后才开始返回下载；
    任何坏行都会返回 400 并指出文件与数据行，不产生部分结果。
    """
    try:
        async with request.form(max_files=16, max_fields=16) as form:
            sample, segments = _pick_assign_uploads(form)

            fd, out_path = tempfile.mkstemp(prefix="assign_result_",
                                            suffix=".csv")
            os.close(fd)
            segment_pairs = [(seg.filename or "", seg.file)
                             for seg in segments]
            try:
                stats = await run_in_threadpool(
                    assign_to_path, sample.file, sample.filename or "",
                    segment_pairs, out_path)
            except AssignError as exc:
                _remove_file(out_path)
                raise HTTPException(400, str(exc))
            except BaseException:
                _remove_file(out_path)
                raise

            stem = Path(sample.filename or "samples").stem
            safe_stem = (re.sub(r"[^0-9A-Za-z._-]+", "_", stem).strip("._")
                         or "samples")
            download_name = f"{safe_stem}_assigned.csv"
            headers = {
                "Content-Disposition": f'attachment; filename="{download_name}"',
                "X-Assign-Samples": str(stats["samples"]),
                "X-Assign-Matched": str(stats["matched"]),
                "X-Assign-Unmatched": str(stats["unmatched"]),
                "X-Assign-Multiple": str(stats["multiple"]),
                "X-Assign-Segments": str(stats["segments"]),
                "X-Assign-Summary": json.dumps(
                    stats, ensure_ascii=True, separators=(",", ":")),
            }
            # FileResponse 返回后上下文关闭上传临时文件；结果文件由后台任务删除。
            return FileResponse(
                out_path, media_type="text/csv; charset=utf-8",
                headers=headers,
                background=BackgroundTask(_remove_file, out_path))
    except MultiPartException as exc:
        raise HTTPException(400, f"multipart 上传解析失败：{exc}")


@app.get("/api/health", include_in_schema=False)
async def health():
    return {"status": "ok"}
