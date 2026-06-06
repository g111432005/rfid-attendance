#!/usr/bin/env python3
"""
Flask 儀表板測試腳本（無硬體版）
-------------------------------------------------
用途：在非樹莓派環境驗證 Flask 路由與儀表板畫面是否正常。
      啟動後會自動寫入幾筆假紀錄，方便確認表格與統計顯示。
執行：python app_test.py
瀏覽：http://127.0.0.1:5000
-------------------------------------------------
"""

import base64
import json
import logging
import os
from datetime import datetime
from pathlib import Path

from flask import Flask, jsonify, render_template, request

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger(__name__)

BASE_DIR      = Path(__file__).parent
RECORDS_FILE  = BASE_DIR / "records.json"
STUDENTS_FILE = BASE_DIR / "students.json"
FACES_DIR     = BASE_DIR / "static" / "faces"
ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD", "admin123")

# ── 假紀錄（若 records.json 不存在則建立）────────────────────
FAKE_RECORDS = [
    {
        "name": "李承曄", "student_id": "111432005",
        "date": datetime.now().strftime("%Y-%m-%d"), "time": "08:30:00",
        "status": "check_in", "uid": "A1B2C3D4",
    },
    {
        "name": "測試學生", "student_id": "000000000",
        "date": datetime.now().strftime("%Y-%m-%d"), "time": "08:31:15",
        "status": "check_in", "uid": "B2C3D4E5",
    },
    {
        "name": "未知", "student_id": "N/A",
        "date": datetime.now().strftime("%Y-%m-%d"), "time": "08:35:02",
        "status": "unauthorized", "uid": "DEADBEEF",
    },
    {
        "name": "李承曄", "student_id": "111432005",
        "date": datetime.now().strftime("%Y-%m-%d"), "time": "09:15:30",
        "status": "check_out", "uid": "A1B2C3D4", "duration": "00:45:30",
    },
]

FAKE_STUDENTS = {
    "A1B2C3D4": {"name": "李承曄",  "student_id": "111432005"},
    "B2C3D4E5": {"name": "測試學生", "student_id": "000000000"},
}

if not RECORDS_FILE.exists():
    RECORDS_FILE.write_text(json.dumps(FAKE_RECORDS, ensure_ascii=False, indent=2), encoding="utf-8")
    logger.info("已寫入假紀錄至 records.json")

if not STUDENTS_FILE.exists():
    STUDENTS_FILE.write_text(json.dumps(FAKE_STUDENTS, ensure_ascii=False, indent=2), encoding="utf-8")
    logger.info("已寫入假學生資料至 students.json")

FACES_DIR.mkdir(parents=True, exist_ok=True)

# ── Flask ──────────────────────────────────────────────────────
app = Flask(__name__)

_lcd_state  = {"line1": "RFID Attendance ", "line2": "Scan your card  "}
_checked_in = {}   # uid -> checkin datetime，追蹤在場狀態（臉部辨識用）


def load_records() -> list:
    if not RECORDS_FILE.exists():
        return []
    try:
        return json.loads(RECORDS_FILE.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return []


def save_records(data: list):
    RECORDS_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def load_students() -> dict:
    if not STUDENTS_FILE.exists():
        return {}
    try:
        return json.loads(STUDENTS_FILE.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}


def save_students(data: dict):
    STUDENTS_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def check_admin(req) -> bool:
    """從 JSON body 或 query string 驗證管理者密碼。"""
    if req.is_json:
        return req.get_json(silent=True, force=True).get("password") == ADMIN_PASSWORD
    return req.args.get("password") == ADMIN_PASSWORD


# ── 主頁面 ─────────────────────────────────────────────────────
@app.route("/")
def index():
    return render_template("index.html")


# ── 簽到紀錄 ───────────────────────────────────────────────────
@app.route("/api/records")
def api_records():
    records = load_records()
    today   = datetime.now().strftime("%Y-%m-%d")

    auth_today   = [r for r in records if r.get("date") == today and r.get("status") == "check_in"]
    auth_total   = [r for r in records if r.get("status") in ("check_in", "face_in")]
    unauth_total = [r for r in records if r.get("status") == "unauthorized"]

    return jsonify({
        "today_count":        len(auth_today),
        "total_count":        len(auth_total),
        "unauthorized_count": len(unauth_total),
        "records":            list(reversed(records)),
    })


@app.route("/api/clear", methods=["POST"])
def api_clear():
    save_records([])
    logger.info("所有簽到紀錄已清除")
    return jsonify({"status": "ok"})


# ── LCD ────────────────────────────────────────────────────────
@app.route("/api/lcd")
def api_lcd():
    return jsonify(_lcd_state)


# ── 學生清單（公開，供臉部辨識比對用）─────────────────────────
@app.route("/api/students")
def api_students():
    students = load_students()
    result = {}
    for uid, info in students.items():
        result[uid] = {
            "name":       info["name"],
            "student_id": info["student_id"],
            "has_face":   (FACES_DIR / f"{uid}.jpg").exists(),
        }
    return jsonify({"students": result})


# ── 臉部辨識簽到／簽退 ────────────────────────────────────────
@app.route("/api/face-checkin", methods=["POST"])
def api_face_checkin():
    """
    接收瀏覽器端 face-api.js 比對後送來的 uid，
    依在場狀態決定簽到或簽退。
    """
    data = request.get_json(force=True) or {}
    uid  = data.get("uid", "").strip().upper()

    students = load_students()
    student  = students.get(uid)
    if not student:
        logger.warning("臉部辨識：找不到 UID=%s 的學生", uid)
        return jsonify({"status": "error", "message": "找不到符合的人臉"}), 404

    now = datetime.now()

    if uid in _checked_in:
        # ── 簽退 ──
        checkin_time = _checked_in.pop(uid)
        elapsed = int((now - checkin_time).total_seconds())
        h, r = divmod(elapsed, 3600)
        m, s = divmod(r, 60)
        duration = f"{h:02d}:{m:02d}:{s:02d}"
        record = {
            "name":       student["name"],
            "student_id": student["student_id"],
            "date":       now.strftime("%Y-%m-%d"),
            "time":       now.strftime("%H:%M:%S"),
            "status":     "face_out",
            "uid":        uid,
            "duration":   duration,
        }
        records = load_records()
        records.append(record)
        save_records(records)
        logger.info("臉部簽退：%s，在場時間：%s", student["name"], duration)
        return jsonify({
            "status": "ok", "action": "check_out",
            "name": student["name"], "duration": duration,
        })
    else:
        # ── 簽到 ──
        _checked_in[uid] = now
        record = {
            "name":       student["name"],
            "student_id": student["student_id"],
            "date":       now.strftime("%Y-%m-%d"),
            "time":       now.strftime("%H:%M:%S"),
            "status":     "face_in",
            "uid":        uid,
        }
        records = load_records()
        records.append(record)
        save_records(records)
        logger.info("臉部簽到：%s", student["name"])
        return jsonify({"status": "ok", "action": "check_in", "name": student["name"]})


# ── 管理者 API ─────────────────────────────────────────────────
@app.route("/api/admin/students", methods=["GET", "POST"])
def api_admin_students():
    if not check_admin(request):
        return jsonify({"error": "密碼錯誤"}), 403

    students = load_students()

    if request.method == "GET":
        result = {}
        for uid, info in students.items():
            result[uid] = {**info, "has_face": (FACES_DIR / f"{uid}.jpg").exists()}
        return jsonify({"students": result})

    # POST：新增學生
    body = request.get_json(force=True)
    uid  = body.get("uid", "").strip().upper()
    name = body.get("name", "").strip()
    sid  = body.get("student_id", "").strip()

    if not uid or not name or not sid:
        return jsonify({"error": "uid、name、student_id 為必填"}), 400
    if uid in students:
        return jsonify({"error": f"UID {uid} 已存在"}), 409

    students[uid] = {"name": name, "student_id": sid}
    save_students(students)
    logger.info("新增學生：%s（%s）UID=%s", name, sid, uid)
    return jsonify({"status": "ok", "uid": uid})


@app.route("/api/admin/students/<uid>", methods=["PUT"])
def api_admin_update_student(uid):
    if not check_admin(request):
        return jsonify({"error": "密碼錯誤"}), 403

    uid      = uid.upper()
    students = load_students()
    if uid not in students:
        return jsonify({"error": "找不到該 UID"}), 404

    body    = request.get_json(force=True)
    new_uid = body.get("new_uid", uid).strip().upper()
    name    = body.get("name", "").strip()
    sid     = body.get("student_id", "").strip()

    if not name or not sid:
        return jsonify({"error": "name、student_id 為必填"}), 400

    if new_uid != uid:
        if new_uid in students:
            return jsonify({"error": f"UID {new_uid} 已存在"}), 409
        old_face = FACES_DIR / f"{uid}.jpg"
        new_face = FACES_DIR / f"{new_uid}.jpg"
        if old_face.exists():
            old_face.rename(new_face)
        del students[uid]

    students[new_uid] = {"name": name, "student_id": sid}
    save_students(students)
    logger.info("更新學生：%s → %s（%s）UID=%s", uid, name, sid, new_uid)
    return jsonify({"status": "ok", "uid": new_uid})


@app.route("/api/admin/students/<uid>", methods=["DELETE"])
def api_admin_delete_student(uid):
    if not check_admin(request):
        return jsonify({"error": "密碼錯誤"}), 403

    uid      = uid.upper()
    students = load_students()
    if uid not in students:
        return jsonify({"error": "找不到該 UID"}), 404

    name = students.pop(uid)["name"]
    save_students(students)

    face_path = FACES_DIR / f"{uid}.jpg"
    if face_path.exists():
        face_path.unlink()

    logger.info("刪除學生：%s（UID=%s）", name, uid)
    return jsonify({"status": "ok"})


@app.route("/api/admin/students/<uid>/face", methods=["POST"])
def api_admin_upload_face(uid):
    if not check_admin(request):
        return jsonify({"error": "密碼錯誤"}), 403

    uid  = uid.upper()
    body = request.get_json(force=True)
    img_b64 = body.get("image", "")
    if not img_b64:
        return jsonify({"error": "缺少 image 欄位"}), 400

    try:
        img_data = base64.b64decode(img_b64)
    except Exception:
        return jsonify({"error": "Base64 解碼失敗"}), 400

    face_path = FACES_DIR / f"{uid}.jpg"
    face_path.write_bytes(img_data)
    logger.info("人臉照片已儲存：%s", face_path)
    return jsonify({"status": "ok"})


# ── 啟動 ───────────────────────────────────────────────────────
if __name__ == "__main__":
    print("=" * 45)
    print("  Flask 儀表板測試模式（無硬體）")
    print("  瀏覽器開啟 http://127.0.0.1:5000")
    print(f"  管理者密碼：{ADMIN_PASSWORD}")
    print("  Ctrl+C 結束")
    print("=" * 45)
    app.run(host="127.0.0.1", port=5000, debug=True)
