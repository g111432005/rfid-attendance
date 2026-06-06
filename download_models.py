#!/usr/bin/env python3
"""
下載 face-api.js 模型檔案
-------------------------------------------------
用途：將 face-api.js 所需的模型權重下載至 static/models/，
      讓瀏覽器可在本機載入，無需依賴 CDN。
執行：python download_models.py
-------------------------------------------------
"""

import logging
import os
import urllib.request
from pathlib import Path

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger(__name__)

# 目標資料夾
MODELS_DIR = Path(__file__).parent / "static" / "models"

# GitHub raw 下載根目錄
BASE_URL = (
    "https://raw.githubusercontent.com/"
    "justadudewhohacks/face-api.js/master/weights/"
)

# 需要的模型檔案清單
MODEL_FILES = [
    # ── SSD MobileNet v1（人臉偵測）──────────────────────────
    "ssd_mobilenetv1_model-weights_manifest.json",
    "ssd_mobilenetv1_model-shard1",
    "ssd_mobilenetv1_model-shard2",
    # ── 68 點臉部特徵點──────────────────────────────────────
    "face_landmark_68_model-weights_manifest.json",
    "face_landmark_68_model-shard1",
    # ── 臉部辨識特徵向量──────────────────────────────────────
    "face_recognition_model-weights_manifest.json",
    "face_recognition_model-shard1",
    "face_recognition_model-shard2",
]


def download_file(filename: str, dest_dir: Path) -> bool:
    """下載單一模型檔案，若已存在則略過。回傳是否成功。"""
    dest = dest_dir / filename
    if dest.exists():
        logger.info("已存在，略過：%s", filename)
        return True

    url = BASE_URL + filename
    logger.info("下載中：%s", filename)
    try:
        urllib.request.urlretrieve(url, dest)
        size_kb = dest.stat().st_size // 1024
        logger.info("完成：%s（%d KB）", filename, size_kb)
        return True
    except Exception as e:
        logger.error("下載失敗：%s，原因：%s", filename, str(e))
        # 清除可能殘留的不完整檔案
        if dest.exists():
            dest.unlink()
        return False


def main():
    MODELS_DIR.mkdir(parents=True, exist_ok=True)
    logger.info("模型目標路徑：%s", MODELS_DIR)
    logger.info("共需下載 %d 個檔案", len(MODEL_FILES))

    success, failed = 0, []
    for filename in MODEL_FILES:
        if download_file(filename, MODELS_DIR):
            success += 1
        else:
            failed.append(filename)

    logger.info("=" * 45)
    logger.info("完成：%d 個成功，%d 個失敗", success, len(failed))
    if failed:
        logger.warning("失敗的檔案：%s", ", ".join(failed))
        logger.warning("請確認網路連線後重新執行本腳本")
    else:
        logger.info("所有模型已就緒，可啟動 Flask 應用程式")


if __name__ == "__main__":
    main()
