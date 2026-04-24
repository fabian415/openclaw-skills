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
    JOB_TIMEOUT_SECONDS 超時秒數（預設：6 小時）
"""

import json
import os
import re
import shutil
import sqlite3
import sys
import threading
import uuid
from contextlib import asynccontextmanager, contextmanager
from datetime import datetime, timedelta
from enum import Enum
from pathlib import Path
from typing import List, Optional

from fastapi import Depends, FastAPI, File, Form, Header, HTTPException, UploadFile
from fastapi.openapi.utils import get_openapi
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel, Field

# ---------------------------------------------------------------------------
# 設定
# ---------------------------------------------------------------------------

UPLOAD_DIR = Path(os.environ.get("UPLOAD_DIR", "/app/uploads"))
OUTPUT_DIR = Path(os.environ.get("OUTPUT_DIR", "/app/outputs"))
SPEAKER_DIR = Path(os.environ.get("SPEAKER_DIR", "/app/speaker_profiles"))
PROPER_NOUNS_CSV = Path(os.environ.get("PROPER_NOUNS_CSV", "/app/Proper_Nouns.csv"))
MAX_FILE_MB = int(os.environ.get("MAX_FILE_MB", "500"))
JOB_TTL_HOURS = int(os.environ.get("JOB_TTL_HOURS", "48"))
JOB_TIMEOUT_SECONDS = int(os.environ.get("JOB_TIMEOUT_SECONDS", str(6 * 60 * 60)))
API_KEY = os.environ.get("API_KEY", "")

UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
SPEAKER_DIR.mkdir(parents=True, exist_ok=True)

ALLOWED_EXTENSIONS = {".mp3", ".mp4", ".wav", ".m4a", ".ogg", ".flac", ".webm", ".aac"}

# ---------------------------------------------------------------------------
# 工作狀態管理（檔案型，容器重啟後仍保留）
# ---------------------------------------------------------------------------

JOBS_DIR = OUTPUT_DIR / "_jobs"
JOBS_DIR.mkdir(parents=True, exist_ok=True)
JOBS_DB = Path(os.environ.get("JOBS_DB", str(JOBS_DIR / "jobs.sqlite3")))

_jobs_lock = threading.Lock()
_queue_cv = threading.Condition()
_worker_stop = threading.Event()
_worker_thread: Optional[threading.Thread] = None
_nouns_lock = threading.Lock()
NOUN_TERM_PATTERN = re.compile(r"^[\w .-]+$")
MAX_PROPER_NOUNS = int(os.environ.get("MAX_PROPER_NOUNS", "48"))


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


class SpeakerProfile(BaseModel):
    """已註冊的 Speaker 聲紋資訊"""

    name: str = Field(description="Speaker 名稱", examples=["Alice"])
    source_file: str = Field(description="註冊時使用的音檔名稱", examples=["alice.wav"])
    dim: int = Field(description="Embedding 向量維度", examples=[512])
    has_audio: bool = Field(default=False, description="是否有可試聽的聲紋音檔")


class SpeakerListResponse(BaseModel):
    """GET /speakers 的回應"""

    total: int = Field(description="已註冊 Speaker 人數", examples=[3])
    speakers: List[SpeakerProfile] = Field(description="Speaker 清單（依名稱排序）")


class EnrollResponse(BaseModel):
    """POST /speakers/enroll 的回應"""

    name: str = Field(description="已註冊的 Speaker 名稱", examples=["Alice"])
    message: str = Field(description="說明訊息", examples=["Speaker Alice 註冊成功。"])


class NounListResponse(BaseModel):
    """GET /proper-nouns 的回應"""

    total: int = Field(description="專有名詞總數", examples=[10])
    terms: List[str] = Field(description="專有名詞清單（依原始順序）", examples=[["Rison", "NVIDIA", "GPU"]])


class NounAddRequest(BaseModel):
    """POST /proper-nouns 的請求體"""

    term: str = Field(description="要新增的專有名詞", examples=["WhisperX"])


class NounUpdateRequest(BaseModel):
    """PUT /proper-nouns/{term} 的請求體"""

    new_term: str = Field(description="修改後的專有名詞", examples=["WhisperX_v3"])


# ---------------------------------------------------------------------------
# Job 讀寫工具
# ---------------------------------------------------------------------------

def _job_file(job_id: str) -> Path:
    return JOBS_DIR / f"{job_id}.json"


@contextmanager
def _db_connect():
    conn = sqlite3.connect(JOBS_DB, timeout=30, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=30000")
    try:
        yield conn
    finally:
        conn.close()


def init_jobs_db():
    JOBS_DIR.mkdir(parents=True, exist_ok=True)
    with _jobs_lock, _db_connect() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS jobs (
                job_id TEXT PRIMARY KEY,
                status TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                started_at TEXT,
                audio_filename TEXT NOT NULL,
                audio_path TEXT,
                language TEXT NOT NULL,
                device TEXT NOT NULL,
                num_speakers INTEGER,
                add_punctuation INTEGER NOT NULL,
                duration_seconds REAL,
                num_speakers_detected INTEGER,
                output_path TEXT,
                error TEXT
            )
            """
        )
        conn.execute("CREATE INDEX IF NOT EXISTS idx_jobs_status_created ON jobs(status, created_at)")


def _row_to_job(row: sqlite3.Row) -> JobRecord:
    return JobRecord(
        job_id=row["job_id"],
        status=JobStatus(row["status"]),
        created_at=row["created_at"],
        updated_at=row["updated_at"],
        audio_filename=row["audio_filename"],
        language=row["language"],
        device=row["device"],
        num_speakers=row["num_speakers"],
        add_punctuation=bool(row["add_punctuation"]),
        duration_seconds=row["duration_seconds"],
        num_speakers_detected=row["num_speakers_detected"],
        output_path=row["output_path"],
        error=row["error"],
    )


def _record_values(record: JobRecord, audio_path: Optional[Path] = None) -> dict:
    return {
        "job_id": record.job_id,
        "status": record.status.value,
        "created_at": record.created_at,
        "updated_at": record.updated_at,
        "audio_filename": record.audio_filename,
        "audio_path": str(audio_path) if audio_path else None,
        "language": record.language,
        "device": record.device,
        "num_speakers": record.num_speakers,
        "add_punctuation": 1 if record.add_punctuation else 0,
        "duration_seconds": record.duration_seconds,
        "num_speakers_detected": record.num_speakers_detected,
        "output_path": record.output_path,
        "error": record.error,
    }


def enqueue_job(record: JobRecord, audio_path: Path):
    with _jobs_lock, _db_connect() as conn:
        conn.execute(
            """
            INSERT INTO jobs (
                job_id, status, created_at, updated_at, audio_filename, audio_path,
                language, device, num_speakers, add_punctuation, duration_seconds,
                num_speakers_detected, output_path, error
            )
            VALUES (
                :job_id, :status, :created_at, :updated_at, :audio_filename, :audio_path,
                :language, :device, :num_speakers, :add_punctuation, :duration_seconds,
                :num_speakers_detected, :output_path, :error
            )
            """,
            _record_values(record, audio_path),
        )
    with _queue_cv:
        _queue_cv.notify()


def save_job(record: JobRecord):
    values = _record_values(record)
    values["started_at"] = _now_iso() if record.status == JobStatus.RUNNING else None
    with _jobs_lock, _db_connect() as conn:
        conn.execute(
            """
            UPDATE jobs
            SET status = :status,
                updated_at = :updated_at,
                started_at = COALESCE(started_at, :started_at),
                audio_filename = :audio_filename,
                language = :language,
                device = :device,
                num_speakers = :num_speakers,
                add_punctuation = :add_punctuation,
                duration_seconds = :duration_seconds,
                num_speakers_detected = :num_speakers_detected,
                output_path = :output_path,
                error = :error
            WHERE job_id = :job_id
            """,
            values,
        )


def load_job(job_id: str) -> Optional[JobRecord]:
    with _jobs_lock, _db_connect() as conn:
        row = conn.execute("SELECT * FROM jobs WHERE job_id = ?", (job_id,)).fetchone()
    return _row_to_job(row) if row else None


def list_jobs() -> List[JobRecord]:
    with _jobs_lock, _db_connect() as conn:
        rows = conn.execute("SELECT * FROM jobs ORDER BY created_at DESC").fetchall()
    return [_row_to_job(row) for row in rows]


def delete_job_record(job_id: str):
    with _jobs_lock, _db_connect() as conn:
        conn.execute("DELETE FROM jobs WHERE job_id = ?", (job_id,))


def _mark_stale_running_failed(conn: sqlite3.Connection, now: str):
    timeout_cutoff = (datetime.utcnow() - timedelta(seconds=JOB_TIMEOUT_SECONDS)).isoformat() + "Z"
    conn.execute(
        """
        UPDATE jobs
        SET status = ?, updated_at = ?, error = ?
        WHERE status = ? AND started_at IS NOT NULL AND started_at < ?
        """,
        (
            JobStatus.FAILED.value,
            now,
            f"Job exceeded timeout of {JOB_TIMEOUT_SECONDS} seconds.",
            JobStatus.RUNNING.value,
            timeout_cutoff,
        ),
    )


def _claim_next_job() -> Optional[tuple[JobRecord, Path]]:
    now = _now_iso()
    with _jobs_lock, _db_connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        try:
            _mark_stale_running_failed(conn, now)
            running_count = conn.execute(
                "SELECT COUNT(*) FROM jobs WHERE status = ?",
                (JobStatus.RUNNING.value,),
            ).fetchone()[0]
            if running_count:
                conn.execute("COMMIT")
                return None

            row = conn.execute(
                "SELECT * FROM jobs WHERE status = ? ORDER BY created_at LIMIT 1",
                (JobStatus.PENDING.value,),
            ).fetchone()
            if not row:
                conn.execute("COMMIT")
                return None
            if not row["audio_path"]:
                conn.execute(
                    "UPDATE jobs SET status = ?, updated_at = ?, error = ? WHERE job_id = ?",
                    (
                        JobStatus.FAILED.value,
                        now,
                        "Queued audio path is missing; this legacy job cannot be resumed.",
                        row["job_id"],
                    ),
                )
                conn.execute("COMMIT")
                return None

            conn.execute(
                "UPDATE jobs SET status = ?, updated_at = ?, started_at = ?, error = NULL WHERE job_id = ?",
                (JobStatus.RUNNING.value, now, now, row["job_id"]),
            )
            row = conn.execute("SELECT * FROM jobs WHERE job_id = ?", (row["job_id"],)).fetchone()
            conn.execute("COMMIT")
            return _row_to_job(row), Path(row["audio_path"])
        except Exception:
            conn.execute("ROLLBACK")
            raise


def _migrate_json_jobs():
    for f in JOBS_DIR.glob("*.json"):
        try:
            data = json.loads(f.read_text(encoding="utf-8"))
            record = JobRecord(**data)
            with _jobs_lock, _db_connect() as conn:
                exists = conn.execute("SELECT 1 FROM jobs WHERE job_id = ?", (record.job_id,)).fetchone()
                if exists:
                    continue
                conn.execute(
                    """
                    INSERT INTO jobs (
                        job_id, status, created_at, updated_at, audio_filename, audio_path,
                        language, device, num_speakers, add_punctuation, duration_seconds,
                        num_speakers_detected, output_path, error
                    )
                    VALUES (
                        :job_id, :status, :created_at, :updated_at, :audio_filename, :audio_path,
                        :language, :device, :num_speakers, :add_punctuation, :duration_seconds,
                        :num_speakers_detected, :output_path, :error
                    )
                    """,
                    _record_values(record),
                )
        except Exception:
            pass


def _recover_interrupted_jobs():
    now = _now_iso()
    with _jobs_lock, _db_connect() as conn:
        conn.execute(
            """
            UPDATE jobs
            SET status = ?, updated_at = ?, started_at = NULL, error = NULL
            WHERE status = ?
            """,
            (JobStatus.PENDING.value, now, JobStatus.RUNNING.value),
        )


def _job_worker_loop():
    while not _worker_stop.is_set():
        claimed = _claim_next_job()
        if claimed:
            record, audio_path = claimed
            _run_transcription(record.job_id, audio_path, record)
            with _queue_cv:
                _queue_cv.notify_all()
            continue
        with _queue_cv:
            _queue_cv.wait(timeout=5)


def start_job_worker():
    global _worker_thread
    if _worker_thread and _worker_thread.is_alive():
        return
    _worker_stop.clear()
    _worker_thread = threading.Thread(target=_job_worker_loop, name="transcription-queue-worker", daemon=True)
    _worker_thread.start()


def stop_job_worker():
    _worker_stop.set()
    with _queue_cv:
        _queue_cv.notify_all()
    if _worker_thread and _worker_thread.is_alive():
        _worker_thread.join(timeout=10)


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
    if SPEAKER_DIR.exists() and any(SPEAKER_DIR.glob("*.json")):
        cmd += ["--speaker-dir", str(SPEAKER_DIR)]

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
                timeout=JOB_TIMEOUT_SECONDS,
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

    except subprocess.TimeoutExpired:
        record.status = JobStatus.FAILED
        record.error = f"Job exceeded timeout of {JOB_TIMEOUT_SECONDS} seconds."
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
    init_jobs_db()
    _migrate_json_jobs()
    _recover_interrupted_jobs()
    cutoff = datetime.utcnow() - timedelta(hours=JOB_TTL_HOURS)
    for record in list_jobs():
        try:
            created = datetime.fromisoformat(record.created_at.rstrip("Z"))
            if created < cutoff:
                out_dir = OUTPUT_DIR / record.job_id
                if out_dir.exists():
                    shutil.rmtree(out_dir, ignore_errors=True)
                delete_job_record(record.job_id)
        except Exception:
            pass
    start_job_worker()
    try:
        yield
    finally:
        stop_job_worker()


ALLOWED_ENROLL_EXTENSIONS = {".mp3", ".mp4", ".wav", ".m4a", ".ogg", ".flac", ".webm", ".aac"}

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
        {"name": "聲紋庫", "description": "Speaker 聲紋註冊與管理"},
        {"name": "專有名詞", "description": "管理 ASR 熱詞清單（提升辨識準確率）"},
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
    enqueue_job(record, upload_path)

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
    delete_job_record(job_id)

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
# 聲紋庫 Routes
# ---------------------------------------------------------------------------

@app.post(
    "/speakers/enroll",
    tags=["聲紋庫"],
    summary="註冊 Speaker 聲紋",
    description="""
上傳 **10–15 秒**單人錄音，提取聲紋 embedding 並存入聲紋庫。

後續轉錄任務將自動比對聲紋庫，將 `Speaker 1`、`Speaker 2` 等標籤
置換為已知的 Speaker 名稱。

**建議錄音條件**：
- 安靜環境、清晰發音
- 10–15 秒（太短會影響準確率）
- 與實際會議使用的麥克風相同或相近

**注意**：若使用相同名稱重複上傳，將**覆蓋**既有的聲紋資料。
""",
    response_model=EnrollResponse,
    status_code=201,
    responses={
        201: {"description": "註冊成功", "model": EnrollResponse},
        400: {"description": "檔案格式錯誤或名稱無效", "model": ErrorResponse},
        401: {"description": "API Key 錯誤", "model": ErrorResponse},
        500: {"description": "聲紋提取失敗（HF_TOKEN 未設定或模型載入失敗）", "model": ErrorResponse},
    },
    dependencies=[Depends(verify_api_key)],
)
async def enroll_speaker(
    audio: UploadFile = File(
        ...,
        description="10–15 秒單人錄音（wav / mp3 / m4a 等）",
    ),
    name: str = Form(
        ...,
        description="Speaker 名稱（英文或中文，不含 / \\ . 等特殊字元）",
        examples=["Alice"],
    ),
    device: str = Form(
        default="auto",
        description="提取 embedding 使用的裝置（auto / cpu / cuda）",
    ),
):
    # 驗證名稱
    import re
    if not name or not re.match(r'^[\w\u4e00-\u9fff\- ]+$', name):
        raise HTTPException(
            status_code=400,
            detail="名稱無效：僅允許英數字、中文、底線、連字號、空格。",
        )

    suffix = Path(audio.filename or "").suffix.lower()
    if suffix not in ALLOWED_ENROLL_EXTENSIONS:
        raise HTTPException(
            status_code=400,
            detail=f"不支援的格式：{suffix}。支援：{', '.join(sorted(ALLOWED_ENROLL_EXTENSIONS))}",
        )

    hf_token = os.environ.get("HF_TOKEN", "")
    if not hf_token:
        raise HTTPException(status_code=500, detail="HF_TOKEN 未設定，無法載入 pyannote/embedding 模型。")

    # 儲存上傳音訊（暫存）
    enroll_path = UPLOAD_DIR / f"enroll_{name}{suffix}"
    content = await audio.read()
    if not content:
        raise HTTPException(status_code=400, detail="上傳檔案為空。")
    enroll_path.write_bytes(content)

    try:
        resolved_device = device
        if device == "auto":
            try:
                import torch
                resolved_device = "cuda" if torch.cuda.is_available() else "cpu"
            except ImportError:
                resolved_device = "cpu"

        # 若重新註冊，先刪除舊的音檔（避免殘留不同副檔名的舊檔）
        old_profile_path = SPEAKER_DIR / f"{name}.json"
        if old_profile_path.exists():
            try:
                old_data = json.loads(old_profile_path.read_text(encoding="utf-8"))
                old_audio = old_data.get("source_file", "")
                if old_audio:
                    (SPEAKER_DIR / old_audio).unlink(missing_ok=True)
            except Exception:
                pass

        from speaker_db import enroll_speaker as _enroll
        _enroll(
            name=name,
            audio_path=enroll_path,
            speaker_dir=SPEAKER_DIR,
            hf_token=hf_token,
            device=resolved_device,
        )

        # 將音檔移至聲紋庫目錄以供試聽（覆蓋舊檔）
        audio_dest = SPEAKER_DIR / f"{name}{suffix}"
        shutil.move(str(enroll_path), str(audio_dest))

        # 更新 JSON profile 的 source_file 為實際存放的檔名
        profile_path = SPEAKER_DIR / f"{name}.json"
        profile_data = json.loads(profile_path.read_text(encoding="utf-8"))
        profile_data["source_file"] = audio_dest.name
        profile_path.write_text(json.dumps(profile_data, ensure_ascii=False, indent=2), encoding="utf-8")

    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"聲紋提取失敗：{e}")
    finally:
        enroll_path.unlink(missing_ok=True)

    return JSONResponse(
        status_code=201,
        content=EnrollResponse(
            name=name,
            message=f"Speaker {name} 註冊成功。",
        ).model_dump(),
    )


@app.get(
    "/speakers",
    tags=["聲紋庫"],
    summary="列出已註冊的 Speaker",
    description="列出聲紋庫中所有已註冊的 Speaker（不含 embedding 向量）。",
    response_model=SpeakerListResponse,
    responses={
        200: {"description": "Speaker 清單", "model": SpeakerListResponse},
        401: {"description": "API Key 錯誤", "model": ErrorResponse},
    },
    dependencies=[Depends(verify_api_key)],
)
def list_speakers():
    from speaker_db import list_speaker_profiles
    speakers = list_speaker_profiles(SPEAKER_DIR)
    return SpeakerListResponse(total=len(speakers), speakers=speakers)


_AUDIO_MEDIA_TYPES = {
    ".mp3": "audio/mpeg",
    ".wav": "audio/wav",
    ".m4a": "audio/mp4",
    ".ogg": "audio/ogg",
    ".flac": "audio/flac",
    ".webm": "audio/webm",
    ".aac": "audio/aac",
    ".mp4": "audio/mp4",
}


@app.get(
    "/speakers/{name}/audio",
    tags=["聲紋庫"],
    summary="試聽 Speaker 聲紋音檔",
    description="串流播放指定 Speaker 註冊時上傳的聲紋音檔，供前端試聽使用。",
    responses={
        200: {"description": "音訊串流", "content": {"audio/*": {}}},
        401: {"description": "API Key 錯誤", "model": ErrorResponse},
        404: {"description": "找不到 Speaker 或無音檔", "model": ErrorResponse},
    },
    dependencies=[Depends(verify_api_key)],
)
def get_speaker_audio(name: str):
    profile_path = SPEAKER_DIR / f"{name}.json"
    if not profile_path.exists():
        raise HTTPException(status_code=404, detail=f"找不到 Speaker：{name}")

    try:
        data = json.loads(profile_path.read_text(encoding="utf-8"))
    except Exception:
        raise HTTPException(status_code=500, detail="讀取聲紋資料失敗。")

    source_file = data.get("source_file", "")
    audio_path = SPEAKER_DIR / source_file if source_file else None
    if not source_file or not audio_path.exists():
        raise HTTPException(status_code=404, detail=f"Speaker {name} 沒有可試聽的音檔。")

    media_type = _AUDIO_MEDIA_TYPES.get(audio_path.suffix.lower(), "application/octet-stream")
    return FileResponse(path=audio_path, media_type=media_type, filename=source_file)


@app.delete(
    "/speakers/{name}",
    tags=["聲紋庫"],
    summary="刪除 Speaker 聲紋",
    description="從聲紋庫中移除指定 Speaker 的聲紋資料。",
    response_model=DeleteResponse,
    responses={
        200: {"description": "刪除成功", "model": DeleteResponse},
        401: {"description": "API Key 錯誤", "model": ErrorResponse},
        404: {"description": "找不到 Speaker", "model": ErrorResponse},
    },
    dependencies=[Depends(verify_api_key)],
)
def delete_speaker(name: str):
    from speaker_db import delete_speaker as _delete
    if not _delete(name, SPEAKER_DIR):
        raise HTTPException(status_code=404, detail=f"找不到 Speaker：{name}")
    return DeleteResponse(message=f"Speaker {name} 已從聲紋庫刪除。")


# ---------------------------------------------------------------------------
# 專有名詞（Proper Nouns）工具函式
# ---------------------------------------------------------------------------

def _load_nouns() -> List[str]:
    """從 CSV 讀取專有名詞清單。"""
    if not PROPER_NOUNS_CSV.exists():
        return []
    with _nouns_lock:
        text = PROPER_NOUNS_CSV.read_text(encoding="utf-8").strip()
    if not text:
        return []
    return [t.strip() for t in text.split(",") if t.strip()]


def _save_nouns(terms: List[str]):
    """將專有名詞清單寫回 CSV。"""
    with _nouns_lock:
        PROPER_NOUNS_CSV.write_text(", ".join(terms), encoding="utf-8")


def _validate_noun_term(term: str, field_name: str) -> str:
    term = term.strip()
    if not term:
        raise HTTPException(status_code=422, detail=f"{field_name} cannot be empty")
    if not NOUN_TERM_PATTERN.fullmatch(term):
        raise HTTPException(
            status_code=422,
            detail=f"{field_name} can only contain letters, numbers, spaces, '_', '-', and '.'",
        )
    return term


def _ensure_noun_count_within_limit(terms: List[str], *, adding: bool = False):
    next_total = len(terms) + (1 if adding else 0)
    if next_total > MAX_PROPER_NOUNS:
        raise HTTPException(
            status_code=422,
            detail=f"proper nouns cannot exceed {MAX_PROPER_NOUNS} terms",
        )


# ---------------------------------------------------------------------------
# 專有名詞 Routes
# ---------------------------------------------------------------------------

@app.get(
    "/proper-nouns",
    tags=["專有名詞"],
    summary="列出所有專有名詞",
    description="回傳目前 ASR 熱詞清單中的所有專有名詞，依原始 CSV 順序排列。",
    response_model=NounListResponse,
    responses={
        200: {"description": "專有名詞清單", "model": NounListResponse},
        401: {"description": "API Key 錯誤", "model": ErrorResponse},
    },
    dependencies=[Depends(verify_api_key)],
)
def list_proper_nouns():
    terms = _load_nouns()
    return NounListResponse(total=len(terms), terms=terms)


@app.post(
    "/proper-nouns",
    tags=["專有名詞"],
    summary="新增專有名詞",
    description="將一個新詞加入 ASR 熱詞清單末尾。若該詞已存在，回傳 409。",
    response_model=NounListResponse,
    status_code=201,
    responses={
        201: {"description": "新增成功，回傳更新後的完整清單", "model": NounListResponse},
        401: {"description": "API Key 錯誤", "model": ErrorResponse},
        409: {"description": "專有名詞已存在", "model": ErrorResponse},
    },
    dependencies=[Depends(verify_api_key)],
)
def add_proper_noun(body: NounAddRequest):
    term = _validate_noun_term(body.term, "term")
    terms = _load_nouns()
    if term in terms:
        raise HTTPException(status_code=409, detail=f"專有名詞「{term}」已存在。")
    _ensure_noun_count_within_limit(terms, adding=True)
    terms.append(term)
    _save_nouns(terms)
    return JSONResponse(
        status_code=201,
        content=NounListResponse(total=len(terms), terms=terms).model_dump(),
    )


@app.delete(
    "/proper-nouns",
    tags=["專有名詞"],
    summary="Delete all proper nouns",
    description="Clear all proper nouns used for ASR prompt injection.",
    response_model=NounListResponse,
    responses={
        200: {"description": "Deleted successfully; returns an empty list.", "model": NounListResponse},
        401: {"description": "Invalid API key", "model": ErrorResponse},
    },
    dependencies=[Depends(verify_api_key)],
)
def delete_all_proper_nouns():
    _save_nouns([])
    return NounListResponse(total=0, terms=[])


@app.put(
    "/proper-nouns/{term}",
    tags=["專有名詞"],
    summary="修改專有名詞",
    description="將清單中的指定詞修改為新的名稱，保留原始位置。若原詞不存在回傳 404；若新詞已存在回傳 409。",
    response_model=NounListResponse,
    responses={
        200: {"description": "修改成功，回傳更新後的完整清單", "model": NounListResponse},
        401: {"description": "API Key 錯誤", "model": ErrorResponse},
        404: {"description": "找不到指定的專有名詞", "model": ErrorResponse},
        409: {"description": "新名稱已存在於清單中", "model": ErrorResponse},
    },
    dependencies=[Depends(verify_api_key)],
)
def update_proper_noun(term: str, body: NounUpdateRequest):
    new_term = _validate_noun_term(body.new_term, "new_term")
    terms = _load_nouns()
    if term not in terms:
        raise HTTPException(status_code=404, detail=f"找不到專有名詞「{term}」。")
    _ensure_noun_count_within_limit(terms)
    if new_term != term and new_term in terms:
        raise HTTPException(status_code=409, detail=f"專有名詞「{new_term}」已存在。")
    idx = terms.index(term)
    terms[idx] = new_term
    _save_nouns(terms)
    return NounListResponse(total=len(terms), terms=terms)


@app.delete(
    "/proper-nouns/{term}",
    tags=["專有名詞"],
    summary="刪除專有名詞",
    description="從 ASR 熱詞清單中移除指定的專有名詞。若不存在回傳 404。",
    response_model=NounListResponse,
    responses={
        200: {"description": "刪除成功，回傳更新後的完整清單", "model": NounListResponse},
        401: {"description": "API Key 錯誤", "model": ErrorResponse},
        404: {"description": "找不到指定的專有名詞", "model": ErrorResponse},
    },
    dependencies=[Depends(verify_api_key)],
)
def delete_proper_noun(term: str):
    terms = _load_nouns()
    if term not in terms:
        raise HTTPException(status_code=404, detail=f"找不到專有名詞「{term}」。")
    terms.remove(term)
    _save_nouns(terms)
    return NounListResponse(total=len(terms), terms=terms)


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
