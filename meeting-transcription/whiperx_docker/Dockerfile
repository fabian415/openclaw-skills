# WhisperX 語音轉錄 API
# 基底映像：PyTorch + CUDA 12.1（對應 NVIDIA 驅動 ≥ 525）
FROM pytorch/pytorch:2.3.0-cuda12.1-cudnn8-runtime

# ---- 系統相依套件 ----
RUN apt-get update && apt-get install -y --no-install-recommends \
        ffmpeg \
        git \
        curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# ---- Python 套件 ----
# 安裝 whisperx
# 安裝後立即移除 torchvision：音訊轉錄不需要它，
# 且 whisperx 的某個依賴會從 PyPI 拉入 CPU 版 torchvision，
# 與 CUDA torch 版本不符會造成 'circular import' 錯誤。
RUN pip install --no-cache-dir whisperx && \
    pip uninstall -y torchvision || true

RUN pip install --no-cache-dir \
        fastapi \
        "uvicorn[standard]" \
        python-multipart \
        funasr \
        deepmultilingualpunctuation \
        soundfile

# ---- 複製服務腳本 ----
COPY scripts/transcribe_diarize.py /app/
COPY scripts/transcribe_api.py /app/
COPY scripts/speaker_db.py /app/

# ---- 資料目錄 ----
RUN mkdir -p /app/uploads /app/outputs /app/speaker_profiles

# ---- 環境變數預設值（可由 docker-compose .env 覆蓋）----
ENV UPLOAD_DIR=/app/uploads \
    OUTPUT_DIR=/app/outputs \
    SPEAKER_DIR=/app/speaker_profiles \
    SPEAKER_SIMILARITY_THRESHOLD=0.75 \
    MAX_FILE_MB=500 \
    JOB_TTL_HOURS=48 \
    JOB_TIMEOUT_SECONDS=10800 \
    MAX_TERMS=30 \
    PORT=8000

EXPOSE 8000

# workers=1：GPU 記憶體只夠跑一個 Whisper 模型實例
CMD ["uvicorn", "transcribe_api:app", \
     "--host", "0.0.0.0", \
     "--port", "8000", \
     "--workers", "1", \
     "--log-level", "info"]
