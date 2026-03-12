#!/usr/bin/env python3
"""
transcribe_api.py - WhisperX 語音轉錄 + 語者分離 API 服務

非同步任務流程：
  1. POST /transcribe  → 上傳音訊，立即回傳 job_id（HTTP 202）
  2. GET  /jobs/{id}   → 輪詢工作狀態（pending / running / done / failed）
  3. GET  /jobs/{id}/result → 下載完成的 Markdown 逐字稿

環境變數：
    HF_TOKEN        Hugging Face token（語者分離必要）
    PUNCT_BACKEND   標點補強後端：funasr / deepmulti / none（預設自動選擇）
    API_KEY         API 驗證金鑰（未設定則不驗證）
    UPLOAD_DIR      上傳暫存目錄（預設：/app/uploads）
    OUTPUT_DIR      輸出根目錄（預設：/app/outputs）
    MAX_FILE_MB     單檔上傳限制（預設：500 MB）
    JOB_TTL_HOURS   工作保留時間（預設：48 小時）
"""

import json
import os
import shutil
import sys
import threading
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timedelta
from enum import Enum
from pathlib import Path
from typing import List, Optional

from fastapi import BackgroundTasks, Depends, FastAPI, File, Form, Header, HTTPException, UploadFile
from fastapi.openapi.utils import get_openapi
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel, Field

# ---------------------------------------------------------------------------
# 設定
# ---------------------------------------------------------------------------

UPLOAD_DIR = Path(os.environ.get("UPLOAD_DIR", "/app/uploads"))
OUTPUT_DIR = Path(os.environ.get("OUTPUT_DIR", "/app/outputs"))
MAX_FILE_MB = int(os.environ.get("MAX_FILE_MB", "500"))
JOB_TTL_HOURS = int(os.environ.get("JOB_TTL_HOURS", "48"))
API_KEY = os.environ.get("API_KEY", "")

UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

ALLOWED_EXTENSIONS = {".mp3", ".mp4", ".wav", ".m4a", ".ogg", ".flac", ".webm", ".aac"}

# ---------------------------------------------------------------------------
# 工作狀態管理（檔案型，容器重啟後仍保留）
# ---------------------------------------------------------------------------

JOBS_DIR = OUTPUT_DIR / "_jobs"
JOBS_DIR.mkdir(parents=True, exist_ok=True)

_jobs_lock = threading.Lock()


class JobStatus(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    DONE = "done"
    FAILED = "failed"


# ---------------------------------------------------------------------------
# Pydantic Models（用於 Swagger 文件）
# ---------------------------------------------------------------------------

class JobRecord(BaseModel):
    """轉錄工作的完整狀態記錄"""

    job_id: str = Field(
        description="工作唯一識別碼（UUID v4）",
        examples=["550e8400-e29b-41d4-a716-446655440000"],
    )
    status: JobStatus = Field(
        description="工作狀態：pending / running / done / failed"
    )
    created_at: str = Field(
        description="工作建立時間（ISO 8601 UTC）",
        examples=["2026-03-11T10:00:00Z"],
    )
    updated_at: str = Field(
        description="最後更新時間（ISO 8601 UTC）",
        examples=["2026-03-11T10:08:30Z"],
    )
    audio_filename: str = Field(
        description="原始音訊檔名",
        examples=["meeting-2026-03-11.mp3"],
    )
    language: str = Field(
        description="指定的語言代碼",
        examples=["zh"],
    )
    device: str = Field(
        description="使用的運算裝置",
        examples=["cuda"],
    )
    num_speakers: Optional[int] = Field(
        default=None,
        description="指定的語者人數（null 表示自動偵測）",
        examples=[3],
    )
    add_punctuation: bool = Field(
        description="是否啟用標點補強"
    )
    duration_seconds: Optional[float] = Field(
        default=None,
        description="音訊總時長（秒），完成後才有值",
        examples=[3600.0],
    )
    num_speakers_detected: Optional[int] = Field(
        default=None,
        description="實際偵測到的語者人數，完成後才有值",
        examples=[3],
    )
    output_path: Optional[str] = Field(
        default=None,
        description="容器內逐字稿檔案路徑（僅供參考）",
    )
    error: Optional[str] = Field(
        default=None,
        description="失敗時的錯誤訊息",
    )


class TranscribeResponse(BaseModel):
    """POST /transcribe 的回應"""

    job_id: str = Field(
        description="工作唯一識別碼，後續用此 ID 查詢狀態",
        examples=["550e8400-e29b-41d4-a716-446655440000"],
    )
    status: JobStatus = Field(
        description="初始狀態，固定為 pending"
    )
    message: str = Field(
        description="說明訊息",
        examples=["任務已建立，請使用 GET /jobs/{job_id} 查詢進度。"],
    )


class JobListResponse(BaseModel):
    """GET /jobs 的回應"""

    total: int = Field(description="工作總數", examples=[5])
    jobs: List[JobRecord] = Field(description="工作清單（依建立時間倒序）")


class DeleteResponse(BaseModel):
    """DELETE /jobs/{job_id} 的回應"""

    message: str = Field(
        description="刪除結果說明",
        examples=["工作 550e8400-e29b-41d4-a716-446655440000 已刪除。"],
    )


class HealthResponse(BaseModel):
    """GET /health 的回應"""

    status: str = Field(description="服務狀態", examples=["ok"])
    gpu: bool = Field(description="是否偵測到 GPU")
    gpu_name: Optional[str] = Field(
        default=None,
        description="GPU 型號",
        examples=["NVIDIA GeForce RTX 3090"],
    )
    gpu_memory_gb: Optional[float] = Field(
        default=None,
        description="GPU 顯示記憶體（GB）",
        examples=[24.0],
    )
    hf_token_set: bool = Field(description="HF_TOKEN 是否已設定")
    punct_backend: str = Field(description="標點補強後端（funasr / deepmulti / none / auto）")


class ErrorResponse(BaseModel):
    """錯誤回應"""

    detail: str = Field(description="錯誤訊息", examples=["找不到工作 550e8400..."])


# ---------------------------------------------------------------------------
# Job 讀寫工具
# ---------------------------------------------------------------------------

def _job_file(job_id: str) -> Path:
    return JOBS_DIR / f"{job_id}.json"


def save_job(record: JobRecord):
    with _jobs_lock:
        _job_file(record.job_id).write_text(
            record.model_dump_json(indent=2), encoding="utf-8"
        )


def load_job(job_id: str) -> Optional[JobRecord]:
    p = _job_file(job_id)
    if not p.exists():
        return None
    with _jobs_lock:
        data = json.loads(p.read_text(encoding="utf-8"))
    return JobRecord(**data)


def list_jobs() -> List[JobRecord]:
    records = []
    with _jobs_lock:
        for f in JOBS_DIR.glob("*.json"):
            try:
                data = json.loads(f.read_text(encoding="utf-8"))
                records.append(JobRecord(**data))
            except Exception:
                pass
    return sorted(records, key=lambda r: r.created_at, reverse=True)


def _now_iso() -> str:
    return datetime.utcnow().isoformat() + "Z"


# ---------------------------------------------------------------------------
# 核心轉錄邏輯（呼叫 transcribe_diarize.py）
# ---------------------------------------------------------------------------

def _parse_md_metadata(transcript_path: Path) -> tuple:
    """從 Markdown 逐字稿標頭解析 (duration_seconds, num_speakers)。"""
    duration_seconds = None
    num_speakers = None
    try:
        for line in transcript_path.read_text(encoding="utf-8").splitlines():
            if line.startswith("**總時長:**"):
                ts = line.split("**總時長:**")[-1].strip()
                parts = ts.split(":")
                if len(parts) == 3:
                    duration_seconds = int(parts[0]) * 3600 + int(parts[1]) * 60 + int(parts[2])
            elif line.startswith("**語者人數:**"):
                num_speakers = int(line.split("**語者人數:**")[-1].strip())
    except Exception:
        pass
    return duration_seconds, num_speakers


def _run_transcription(job_id: str, audio_path: Path, record: JobRecord):
    """以獨立 subprocess 執行轉錄，結束後 GPU 記憶體完全釋放。"""
    import subprocess

    record.status = JobStatus.RUNNING
    record.updated_at = _now_iso()
    save_job(record)

    script_path = Path(__file__).parent / "transcribe_diarize.py"
    # subprocess 輸出到 OUTPUT_DIR/job_id，
    # transcribe_diarize.py 會在其下建立 <stem>/<stem>_逐字稿.md
    out_base = OUTPUT_DIR / job_id
    out_base.mkdir(parents=True, exist_ok=True)

    cmd = [
        sys.executable, str(script_path),
        str(audio_path),
        "--output-dir", str(out_base),
        "--lang", record.language,
        "--device", record.device,
    ]
    if record.num_speakers:
        cmd += ["--num-speakers", str(record.num_speakers)]
    if not record.add_punctuation:
        cmd.append("--no-punctuation")

    log_path = out_base / "subprocess.log"
    print(f"[{job_id}] 啟動 subprocess: {' '.join(cmd)}", flush=True)

    try:
        with open(log_path, "w", encoding="utf-8") as log_f:
            proc = subprocess.run(
                cmd,
                stdout=log_f,
                stderr=subprocess.STDOUT,
                text=True,
                env=os.environ.copy(),
            )

        print(f"[{job_id}] subprocess 結束，returncode={proc.returncode}", flush=True)

        if proc.returncode != 0:
            last_lines = log_path.read_text(encoding="utf-8", errors="replace")[-1000:]
            raise RuntimeError(f"subprocess 退出碼 {proc.returncode}\n{last_lines}")

        # transcribe_diarize.py 輸出路徑：<out_base>/<stem>/<stem>_逐字稿.md
        transcript_path = out_base / audio_path.stem / f"{audio_path.stem}_逐字稿.md"
        if not transcript_path.exists():
            raise RuntimeError(f"找不到輸出檔案：{transcript_path}")

        duration_seconds, num_speakers_detected = _parse_md_metadata(transcript_path)

        record.status = JobStatus.DONE
        record.output_path = str(transcript_path)
        record.duration_seconds = duration_seconds
        record.num_speakers_detected = num_speakers_detected
        record.updated_at = _now_iso()

    except Exception as e:
        record.status = JobStatus.FAILED
        record.error = str(e)
        record.updated_at = _now_iso()

    finally:
        try:
            audio_path.unlink(missing_ok=True)
        except Exception:
            pass
        save_job(record)


# ---------------------------------------------------------------------------
# FastAPI 應用
# ---------------------------------------------------------------------------

@asynccontextmanager
async def lifespan(app: FastAPI):
    """啟動時清理過期工作"""
    cutoff = datetime.utcnow() - timedelta(hours=JOB_TTL_HOURS)
    for record in list_jobs():
        try:
            created = datetime.fromisoformat(record.created_at.rstrip("Z"))
            if created < cutoff:
                out_dir = OUTPUT_DIR / record.job_id
                if out_dir.exists():
                    shutil.rmtree(out_dir, ignore_errors=True)
                _job_file(record.job_id).unlink(missing_ok=True)
        except Exception:
            pass
    yield


app = FastAPI(
    title="WhisperX 語音轉錄 API",
    description="""
## 概述

將音訊上傳後，由後端以 WhisperX 進行：
1. **語音辨識**（Whisper large-v3）
2. **詞對齊**（force alignment）
3. **語者分離**（Pyannote speaker diarization）
4. **標點補強**（Groq API，可選）

最終輸出帶有時間戳記與語者標籤的 **Markdown 逐字稿**。

## 認證

請在所有請求的 Header 中帶入：
```
X-API-Key: <your-api-key>
```
若服務端未設定 `API_KEY` 環境變數，則不需認證。

## 非同步流程

```
POST /transcribe → 取得 job_id
       ↓
GET /jobs/{job_id}  （每 15 秒輪詢）
       ↓ status == "done"
GET /jobs/{job_id}/result  → 下載 Markdown
```
""",
    version="1.0.0",
    openapi_tags=[
        {"name": "轉錄", "description": "提交音訊並管理轉錄任務"},
        {"name": "工作管理", "description": "查詢、列出、刪除工作"},
        {"name": "系統", "description": "健康檢查與服務狀態"},
    ],
    lifespan=lifespan,
)


# ---- 驗證 ----

def verify_api_key(x_api_key: Optional[str] = Header(default=None, description="API 金鑰")):
    if not API_KEY:
        return
    if x_api_key != API_KEY:
        raise HTTPException(status_code=401, detail="Invalid or missing X-API-Key header.")


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@app.get(
    "/health",
    tags=["系統"],
    summary="服務健康檢查",
    description="回傳服務狀態、GPU 資訊及必要環境變數的設定情況。此端點不需認證。",
    response_model=HealthResponse,
)
def health():
    info: dict = {"status": "ok", "gpu": False, "gpu_name": None, "gpu_memory_gb": None}
    try:
        import torch
        if torch.cuda.is_available():
            info["gpu"] = True
            info["gpu_name"] = torch.cuda.get_device_name(0)
            info["gpu_memory_gb"] = round(
                torch.cuda.get_device_properties(0).total_memory / 1e9, 1
            )
    except Exception:
        pass
    info["hf_token_set"] = bool(os.environ.get("HF_TOKEN"))
    info["punct_backend"] = os.environ.get("PUNCT_BACKEND", "auto")
    return info


@app.post(
    "/transcribe",
    tags=["轉錄"],
    summary="上傳音訊，啟動轉錄任務",
    description="""
上傳音訊檔案後，服務立即回傳 `job_id`（HTTP 202），轉錄在背景非同步執行。

**支援格式**：mp3、mp4、wav、m4a、ogg、flac、webm、aac

**注意**：由於 GPU 記憶體限制，同一時間只處理一個任務，多個請求會依序排隊執行。
""",
    response_model=TranscribeResponse,
    status_code=202,
    responses={
        202: {"description": "任務已建立", "model": TranscribeResponse},
        400: {"description": "檔案格式錯誤或檔案為空", "model": ErrorResponse},
        401: {"description": "API Key 錯誤", "model": ErrorResponse},
        413: {"description": "檔案超過大小限制", "model": ErrorResponse},
    },
    dependencies=[Depends(verify_api_key)],
)
async def transcribe(
    background_tasks: BackgroundTasks,
    audio: UploadFile = File(
        ...,
        description="音訊檔（mp3 / wav / m4a / mp4 / ogg / flac / webm / aac）",
    ),
    lang: str = Form(
        default="zh",
        description="語言代碼。常用值：`zh`（繁/簡中）、`en`（英）、`ja`（日）、`auto`（自動偵測）",
    ),
    device: str = Form(
        default="auto",
        description="運算裝置。`auto` 會自動選擇 cuda > mps > cpu",
    ),
    num_speakers: Optional[int] = Form(
        default=None,
        description="預先指定語者人數（1–10）。不填則由 Pyannote 自動偵測，建議在已知人數時填入以提升準確率",
        ge=1,
        le=10,
    ),
    no_punctuation: bool = Form(
        default=False,
        description="設為 `true` 可跳過 Groq 標點補強（加快速度，但輸出無標點）",
    ),
):
    suffix = Path(audio.filename or "").suffix.lower()
    if suffix not in ALLOWED_EXTENSIONS:
        raise HTTPException(
            status_code=400,
            detail=f"不支援的檔案格式：{suffix or '（無副檔名）'}。支援：{', '.join(sorted(ALLOWED_EXTENSIONS))}",
        )

    job_id = str(uuid.uuid4())
    upload_path = UPLOAD_DIR / f"{job_id}{suffix}"

    chunk_size = 1024 * 1024
    total = 0
    max_bytes = MAX_FILE_MB * 1024 * 1024
    with open(upload_path, "wb") as f:
        while chunk := await audio.read(chunk_size):
            total += len(chunk)
            if total > max_bytes:
                upload_path.unlink(missing_ok=True)
                raise HTTPException(
                    status_code=413,
                    detail=f"檔案超過上限 {MAX_FILE_MB} MB。",
                )
            f.write(chunk)

    if total == 0:
        upload_path.unlink(missing_ok=True)
        raise HTTPException(status_code=400, detail="上傳檔案為空。")

    record = JobRecord(
        job_id=job_id,
        status=JobStatus.PENDING,
        created_at=_now_iso(),
        updated_at=_now_iso(),
        audio_filename=audio.filename or upload_path.name,
        language=lang,
        device=device,
        num_speakers=num_speakers,
        add_punctuation=not no_punctuation,
    )
    save_job(record)
    background_tasks.add_task(_run_transcription, job_id, upload_path, record)

    return JSONResponse(
        status_code=202,
        content=TranscribeResponse(
            job_id=job_id,
            status=JobStatus.PENDING,
            message="任務已建立，請使用 GET /jobs/{job_id} 查詢進度。",
        ).model_dump(),
    )


@app.get(
    "/jobs/{job_id}",
    tags=["工作管理"],
    summary="查詢工作狀態",
    description="""
回傳工作的完整狀態記錄。建議每 **15 秒**輪詢一次。

**狀態說明**：
- `pending`：排隊等待中
- `running`：轉錄進行中
- `done`：完成，可呼叫 `/jobs/{job_id}/result` 下載逐字稿
- `failed`：失敗，錯誤訊息在 `error` 欄位
""",
    response_model=JobRecord,
    responses={
        200: {"description": "工作狀態", "model": JobRecord},
        401: {"description": "API Key 錯誤", "model": ErrorResponse},
        404: {"description": "找不到工作", "model": ErrorResponse},
    },
    dependencies=[Depends(verify_api_key)],
)
def get_job(job_id: str):
    record = load_job(job_id)
    if not record:
        raise HTTPException(status_code=404, detail=f"找不到工作 {job_id}")
    return record


@app.get(
    "/jobs/{job_id}/result",
    tags=["工作管理"],
    summary="下載逐字稿 Markdown",
    description="""
工作狀態為 `done` 時，下載帶有時間戳記與語者標籤的 Markdown 逐字稿。

**回應格式**：`text/markdown; charset=utf-8`（檔案下載）

**逐字稿格式範例**：
```markdown
# 逐字稿 - meeting-2026-03-11

**語言:** zh
**總時長:** 01:00:00
**語者人數:** 3

---

**[00:00:05 → 00:00:32] Speaker 1:**
大家好，今天我們來討論第一季的業績報告。

**[00:00:33 → 00:01:10] Speaker 2:**
好的，根據數據顯示，本季營收成長了 15%。
```
""",
    responses={
        200: {"description": "Markdown 逐字稿檔案", "content": {"text/markdown": {}}},
        401: {"description": "API Key 錯誤", "model": ErrorResponse},
        404: {"description": "找不到工作", "model": ErrorResponse},
        409: {"description": "工作尚未完成", "model": ErrorResponse},
        410: {"description": "逐字稿檔案已被刪除", "model": ErrorResponse},
    },
    dependencies=[Depends(verify_api_key)],
)
def get_result(job_id: str):
    record = load_job(job_id)
    if not record:
        raise HTTPException(status_code=404, detail=f"找不到工作 {job_id}")
    if record.status != JobStatus.DONE:
        raise HTTPException(
            status_code=409,
            detail=f"工作尚未完成（目前狀態：{record.status}）。",
        )
    path = Path(record.output_path)
    if not path.exists():
        raise HTTPException(status_code=410, detail="逐字稿檔案已被刪除。")
    return FileResponse(
        path=str(path),
        media_type="text/markdown; charset=utf-8",
        filename=path.name,
    )


@app.delete(
    "/jobs/{job_id}",
    tags=["工作管理"],
    summary="刪除工作",
    description="刪除工作記錄及對應的逐字稿輸出目錄，釋放磁碟空間。",
    response_model=DeleteResponse,
    responses={
        200: {"description": "刪除成功", "model": DeleteResponse},
        401: {"description": "API Key 錯誤", "model": ErrorResponse},
        404: {"description": "找不到工作", "model": ErrorResponse},
    },
    dependencies=[Depends(verify_api_key)],
)
def delete_job(job_id: str):
    record = load_job(job_id)
    if not record:
        raise HTTPException(status_code=404, detail=f"找不到工作 {job_id}")

    out_dir = OUTPUT_DIR / job_id
    if out_dir.exists():
        shutil.rmtree(out_dir, ignore_errors=True)
    _job_file(job_id).unlink(missing_ok=True)

    return DeleteResponse(message=f"工作 {job_id} 已刪除。")


@app.get(
    "/jobs",
    tags=["工作管理"],
    summary="列出所有工作",
    description="""
列出所有工作，依建立時間倒序排列。可用 `status` 參數篩選。

**status 可用值**：`pending`、`running`、`done`、`failed`
""",
    response_model=JobListResponse,
    responses={
        200: {"description": "工作清單", "model": JobListResponse},
        401: {"description": "API Key 錯誤", "model": ErrorResponse},
    },
    dependencies=[Depends(verify_api_key)],
)
def list_all_jobs(
    status: Optional[str] = None,
):
    records = list_jobs()
    if status:
        records = [r for r in records if r.status == status]
    return JobListResponse(total=len(records), jobs=records)


# ---------------------------------------------------------------------------
# 自訂 OpenAPI schema（加入 API Key security scheme）
# ---------------------------------------------------------------------------

def custom_openapi():
    if app.openapi_schema:
        return app.openapi_schema
    schema = get_openapi(
        title=app.title,
        version=app.version,
        description=app.description,
        routes=app.routes,
        tags=app.openapi_tags,
    )
    schema["components"]["securitySchemes"] = {
        "ApiKeyAuth": {
            "type": "apiKey",
            "in": "header",
            "name": "X-API-Key",
            "description": "服務端設定 API_KEY 環境變數後啟用。請在此輸入對應的金鑰值。",
        }
    }
    schema["security"] = [{"ApiKeyAuth": []}]
    app.openapi_schema = schema
    return schema


app.openapi = custom_openapi


# ---------------------------------------------------------------------------
# 直接執行
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import uvicorn

    uvicorn.run(
        "transcribe_api:app",
        host="0.0.0.0",
        port=int(os.environ.get("PORT", "8000")),
        workers=1,
        log_level="info",
    )
