#!/usr/bin/env python3
"""
speaker_db.py - 聲紋庫管理：Speaker Enrollment + Identification

流程：
  1. 註冊（Enroll）：上傳 10-15 秒音檔 → 提取 embedding → 儲存至 speaker_profiles/
  2. 比對（Match）：轉錄完成後，對每位 diarized speaker 提取 embedding，
                    與聲紋庫進行 cosine similarity 比對，高於閾值則置換標籤。

依賴模型：pyannote/embedding（pyannote.audio，需接受 HF 使用條款）
    https://huggingface.co/pyannote/embedding

環境變數：
    HF_TOKEN                    Hugging Face token（必要）
    SPEAKER_SIMILARITY_THRESHOLD  比對閾值（預設 0.75，範圍 0-1）
"""

import gc
import json
import os
import tempfile
from pathlib import Path
from typing import Optional

import numpy as np

SIMILARITY_THRESHOLD = float(os.environ.get("SPEAKER_SIMILARITY_THRESHOLD", "0.75"))

# ---------------------------------------------------------------------------
# 工具函式
# ---------------------------------------------------------------------------

def _cosine_similarity(a: np.ndarray, b: np.ndarray) -> float:
    """計算兩個向量的 cosine similarity（-1 ~ 1，越高越相似）。"""
    norm = np.linalg.norm(a) * np.linalg.norm(b)
    return float(np.dot(a, b) / norm) if norm > 0 else 0.0


# ---------------------------------------------------------------------------
# Embedding 提取
# ---------------------------------------------------------------------------

def _load_audio_as_tensor(audio_path: str):
    """
    使用 librosa 載入音訊並轉為 torch tensor。
    回傳 {"waveform": Tensor[1, N], "sample_rate": 16000}
    繞過 pyannote.audio 的 AudioDecoder（某些版本有 import bug）。
    """
    import torch
    import librosa

    audio_np, _ = librosa.load(audio_path, sr=16000, mono=True)
    waveform = torch.tensor(audio_np, dtype=torch.float32).unsqueeze(0)
    return {"waveform": waveform, "sample_rate": 16000}


def extract_embedding(audio_path: str, hf_token: str, device: str = "cpu") -> np.ndarray:
    """
    從音訊檔提取聲紋 embedding。

    使用 pyannote/embedding（ECAPA-TDNN 架構），
    輸出 512 維特徵向量。

    Args:
        audio_path: 音訊檔路徑（支援 wav/mp3/m4a 等 ffmpeg 可讀格式）
        hf_token:   Hugging Face token
        device:     "cpu" 或 "cuda"

    Returns:
        numpy array，shape (512,)
    """
    import torch
    from pyannote.audio import Model, Inference

    model = Model.from_pretrained("pyannote/embedding", use_auth_token=hf_token)
    if device == "cuda" and torch.cuda.is_available():
        model = model.to(torch.device("cuda"))

    inference = Inference(model, window="whole")

    # 手動載入音訊以繞過 pyannote AudioDecoder 的版本相容問題
    audio_input = _load_audio_as_tensor(audio_path)
    embedding = np.array(inference(audio_input))

    del model, inference
    gc.collect()
    if device == "cuda":
        try:
            torch.cuda.empty_cache()
        except Exception:
            pass

    return embedding


# ---------------------------------------------------------------------------
# 聲紋庫 CRUD
# ---------------------------------------------------------------------------

def enroll_speaker(
    name: str,
    audio_path: Path,
    speaker_dir: Path,
    hf_token: str,
    device: str = "cpu",
) -> dict:
    """
    註冊 Speaker：提取聲紋 embedding 並儲存至 speaker_dir/{name}.json

    Args:
        name:        Speaker 顯示名稱（e.g. "Alice"）
        audio_path:  10-15 秒音訊檔
        speaker_dir: 聲紋庫目錄
        hf_token:    Hugging Face token
        device:      "cpu" 或 "cuda"

    Returns:
        profile dict（包含 name、dim、source_file）
    """
    speaker_dir.mkdir(parents=True, exist_ok=True)
    embedding = extract_embedding(str(audio_path), hf_token, device)

    profile = {
        "name": name,
        "embedding": embedding.tolist(),
        "source_file": audio_path.name,
        "dim": len(embedding),
    }

    profile_path = speaker_dir / f"{name}.json"
    profile_path.write_text(
        json.dumps(profile, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return {"name": name, "dim": len(embedding), "source_file": audio_path.name}


def delete_speaker(name: str, speaker_dir: Path) -> bool:
    """刪除 Speaker 聲紋檔及對應音檔。成功返回 True，找不到返回 False。"""
    profile_path = speaker_dir / f"{name}.json"
    if profile_path.exists():
        try:
            data = json.loads(profile_path.read_text(encoding="utf-8"))
            source_file = data.get("source_file", "")
            if source_file:
                (speaker_dir / source_file).unlink(missing_ok=True)
        except Exception:
            pass
        profile_path.unlink()
        return True
    return False


def load_speaker_profiles(speaker_dir: Path) -> dict:
    """
    載入所有已註冊的聲紋資料。

    Returns:
        {name: np.ndarray}
    """
    profiles = {}
    if not speaker_dir.exists():
        return profiles
    for f in speaker_dir.glob("*.json"):
        try:
            data = json.loads(f.read_text(encoding="utf-8"))
            profiles[data["name"]] = np.array(data["embedding"])
        except Exception:
            pass
    return profiles


def list_speaker_profiles(speaker_dir: Path) -> list:
    """列出所有已註冊的 Speaker 摘要（不含 embedding 向量）。"""
    speakers = []
    if not speaker_dir.exists():
        return speakers
    for f in speaker_dir.glob("*.json"):
        try:
            data = json.loads(f.read_text(encoding="utf-8"))
            source_file = data.get("source_file", "")
            has_audio = bool(source_file) and (speaker_dir / source_file).exists()
            speakers.append({
                "name": data["name"],
                "source_file": source_file,
                "dim": data.get("dim", 0),
                "has_audio": has_audio,
            })
        except Exception:
            pass
    return sorted(speakers, key=lambda x: x["name"])


# ---------------------------------------------------------------------------
# 聲紋比對（Diarization 後使用）
# ---------------------------------------------------------------------------

def match_diarized_speakers(
    diarize_segments,
    audio: np.ndarray,
    speaker_dir: Path,
    hf_token: str,
    device: str = "cpu",
    threshold: float = SIMILARITY_THRESHOLD,
) -> dict:
    """
    將 diarization 辨識到的 SPEAKER_XX 與聲紋庫比對，
    返回 {SPEAKER_00: "Alice", SPEAKER_01: "Bob", ...}

    未達閾值的 SPEAKER_XX 不會出現在返回字典中，
    transcribe_diarize.py 會將其 fallback 為 "Speaker N"。

    Args:
        diarize_segments: whisperx diarization 輸出的 pandas DataFrame
                          (欄位：start, end, speaker)
        audio:            16kHz 原始音訊 numpy array
        speaker_dir:      聲紋庫目錄
        hf_token:         Hugging Face token
        device:           "cpu" 或 "cuda"
        threshold:        cosine similarity 閾值（預設 0.75）

    Returns:
        {pyannote_speaker_id: enrolled_name}
    """
    import torch
    from pyannote.audio import Model, Inference

    profiles = load_speaker_profiles(speaker_dir)
    if not profiles:
        print("聲紋庫為空，跳過聲紋比對。")
        return {}

    print(f"聲紋庫：{list(profiles.keys())}，閾值：{threshold}")

    model = Model.from_pretrained("pyannote/embedding", use_auth_token=hf_token)
    if device == "cuda" and torch.cuda.is_available():
        model = model.to(torch.device("cuda"))
    inference = Inference(model, window="whole")

    speaker_name_map = {}
    unique_speakers = diarize_segments["speaker"].unique()

    for speaker_id in unique_speakers:
        segs = diarize_segments[diarize_segments["speaker"] == speaker_id].copy()
        segs = segs.assign(duration=segs["end"] - segs["start"])

        # 取最長 segment 提取 embedding（品質最佳）
        longest = segs.nlargest(1, "duration").iloc[0]
        start_s = float(longest["start"])
        end_s = float(longest["end"])

        start_sample = int(start_s * 16000)
        end_sample = int(end_s * 16000)
        seg_audio = audio[start_sample:end_sample]

        # 至少需要 1.5 秒音訊
        if len(seg_audio) < 24000:
            print(f"  {speaker_id} → 片段太短（{len(seg_audio)/16000:.1f}s），跳過比對")
            continue

        try:
            # 直接傳 waveform tensor，繞過 AudioDecoder
            waveform = torch.tensor(seg_audio, dtype=torch.float32).unsqueeze(0)
            audio_input = {"waveform": waveform, "sample_rate": 16000}
            embedding = np.array(inference(audio_input))

            # 與所有聲紋比對，取最高分
            best_name: Optional[str] = None
            best_score = -1.0
            for name, prof_emb in profiles.items():
                score = _cosine_similarity(embedding, prof_emb)
                if score > best_score:
                    best_score = score
                    best_name = name

            if best_name and best_score >= threshold:
                speaker_name_map[speaker_id] = best_name
                print(f"  {speaker_id} → {best_name}（similarity={best_score:.3f} ✓）")
            else:
                print(f"  {speaker_id} → 未辨識（最高：{best_name}={best_score:.3f}，未達閾值 {threshold}）")

        except Exception as e:
            print(f"  {speaker_id} → 比對失敗：{e}")

    del model, inference
    gc.collect()
    if device == "cuda":
        try:
            torch.cuda.empty_cache()
        except Exception:
            pass

    return speaker_name_map
