#!/usr/bin/env python3
"""
智慧 RFID 簽到系統
-------------------------------------------------
零件：RC522 RFID 讀卡機、LCD 1602（I²C 0x27）
      綠 LED（GPIO 6）、紅 LED（GPIO 13）、蜂鳴器（GPIO 17）
Flask：http://<樹莓派 IP>:5000
-------------------------------------------------
第一次使用前請先執行 scan_uid.py 取得兩張卡的 UID，
再填入下方 STUDENTS 字典。
"""

import RPi.GPIO as GPIO
import mfrc522 as MFRC522
import base64
import logging
import os
import json
import threading
import time
from datetime import datetime
from flask import Flask, jsonify, render_template, request

# ============================================================
#  日誌設定（所有模組共用，需在最早初始化）
# ============================================================
os.makedirs("logs", exist_ok=True)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler("logs/app.log", encoding="utf-8"),
    ],
)
logger = logging.getLogger(__name__)

# ============================================================
#  學生資料（UID → 姓名、學號）
#  請將 "CARD_UID_1" / "CARD_UID_2" 換成 scan_uid.py 印出的值
#  UID 格式範例："A1B2C3D4"
# ============================================================
STUDENTS = {
    "D7659E19": {"name": "李承曄",  "student_id": "111432005"},
}

# ============================================================
#  RFID Key A 驗證設定
#  KEY_A：MIFARE Classic 磁區金鑰，出廠預設為全 0xFF
#  AUTH_BLOCK：要驗證的區塊編號（Sector 1 的 Block 4）
# ============================================================
KEY_A      = [0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF]
AUTH_BLOCK = 4

# ============================================================
#  GPIO 腳位設定
# ============================================================
LED_GREEN = 6    # 授權成功 → 亮綠燈
LED_RED   = 13   # 未授權   → 亮紅燈
BUZZER    = 17   # 被動式蜂鳴器（Keyes 無源，需 PWM 驅動）
BUZZER_FREQ = 1000  # 固定音頻 1000 Hz

GPIO.setmode(GPIO.BCM)
GPIO.setwarnings(False)
GPIO.setup(LED_GREEN, GPIO.OUT, initial=GPIO.LOW)
GPIO.setup(LED_RED,   GPIO.OUT, initial=GPIO.LOW)
GPIO.setup(BUZZER,    GPIO.OUT, initial=GPIO.LOW)
_buzzer_pwm = GPIO.PWM(BUZZER, BUZZER_FREQ)

# ============================================================
#  LCD 初始化
# ============================================================
from RPLCD.i2c import CharLCD

try:
    mylcd = CharLCD(
        i2c_expander='PCF8574',
        address=0x27,
        port=1,
        cols=16,
        rows=2,
        dotsize=8,
        auto_linebreaks=False,
    )
    logger.info("LCD 初始化成功（I2C 0x27）")
except Exception as e:
    mylcd = None
    logger.warning("LCD 初始化失敗，將跳過實體顯示：%s", e)

# ============================================================
#  Flask 初始化
# ============================================================
app = Flask(__name__)
RECORDS_FILE   = os.path.join(os.path.dirname(__file__), "records.json")
STUDENTS_FILE  = os.path.join(os.path.dirname(__file__), "students.json")
FACES_DIR      = os.path.join(os.path.dirname(__file__), "static", "faces")
ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD", "admin123")

os.makedirs(FACES_DIR, exist_ok=True)

# 初次啟動若 students.json 不存在，從程式碼內的 STUDENTS 建立
if not os.path.exists(STUDENTS_FILE):
    with open(STUDENTS_FILE, "w", encoding="utf-8") as _f:
        json.dump(STUDENTS, _f, ensure_ascii=False, indent=2)


def load_students() -> dict:
    """從 students.json 載入學生資料，讀取失敗時回傳空字典"""
    try:
        with open(STUDENTS_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return {}


def save_students(data: dict):
    """將學生資料寫入 students.json"""
    with open(STUDENTS_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

# 防重複刷卡鎖（同一張卡 3 秒內只記錄一次）
_last_uid   = ""
_last_time  = 0
_COOLDOWN   = 3.0

# 目前在場名單（uid → {"student": dict, "timestamp": datetime}）
_checked_in = {}

# 目前 LCD 顯示內容（供網頁即時呈現）
_lcd_state = {"line1": "RFID Attendance ", "line2": "Scan your card  "}

# ============================================================
#  JSON 紀錄工具
# ============================================================
def load_records():
    if os.path.exists(RECORDS_FILE):
        with open(RECORDS_FILE, "r", encoding="utf-8") as f:
            try:
                return json.load(f)
            except json.JSONDecodeError:
                return []
    return []

def save_record(record: dict):
    records = load_records()
    records.append(record)
    with open(RECORDS_FILE, "w", encoding="utf-8") as f:
        json.dump(records, f, ensure_ascii=False, indent=2)

# ============================================================
#  硬體動作
# ============================================================
def beep(times: int = 1, duration: float = 0.1, gap: float = 0.08):
    """被動式蜂鳴器（PWM 固定 1000 Hz）：響 times 次"""
    for i in range(times):
        _buzzer_pwm.start(50)   # 50% duty cycle → 發聲
        time.sleep(duration)
        _buzzer_pwm.stop()      # 停止發聲
        if times > 1 and i < times - 1:
            time.sleep(gap)

def led_on_then_off(pin: int, duration: float = 2.0):
    """點亮 LED 持續 duration 秒後熄滅（在子執行緒執行）"""
    GPIO.output(pin, GPIO.HIGH)
    time.sleep(duration)
    GPIO.output(pin, GPIO.LOW)

def lcd_show(line1: str, line2: str):
    """更新 LCD 實體顯示，同步記錄至 _lcd_state 供網頁讀取"""
    global _lcd_state
    l1 = line1[:16].ljust(16)
    l2 = line2[:16].ljust(16)
    _lcd_state = {"line1": l1, "line2": l2}
    if mylcd is None:
        return
    try:
        mylcd.cursor_pos = (0, 0)
        mylcd.write_string(l1)
        mylcd.cursor_pos = (1, 0)
        mylcd.write_string(l2)
    except Exception as e:
        logger.warning("LCD 寫入失敗：%s", e)

def lcd_ready():
    """顯示待機畫面"""
    lcd_show("RFID Attendance ", "Scan your card  ")

# ============================================================
#  簽到 / 簽退邏輯
# ============================================================
def handle_checkin(student: dict, uid: str):
    now      = datetime.now()
    time_str = now.strftime("%H:%M:%S")
    date_str = now.strftime("%Y-%m-%d")

    # 記錄進場時間，供簽退計算在場時長
    _checked_in[uid] = {"student": student, "timestamp": now}

    print(f"[簽到] {student['name']} ({student['student_id']}) @ {time_str}")

    # LCD
    lcd_show("Welcome!        ", time_str)

    # 硬體回饋（綠燈 + 短嗶一聲）
    threading.Thread(target=led_on_then_off, args=(LED_GREEN, 2.0), daemon=True).start()
    beep(1, 0.15)

    save_record({
        "name":       student["name"],
        "student_id": student["student_id"],
        "date":       date_str,
        "time":       time_str,
        "status":     "check_in",
        "uid":        uid,
    })

    time.sleep(2.5)
    lcd_ready()


def handle_checkout(student: dict, uid: str):
    now          = datetime.now()
    time_str     = now.strftime("%H:%M:%S")
    date_str     = now.strftime("%Y-%m-%d")
    checkin_time = _checked_in.pop(uid)["timestamp"]

    # 計算在場時長
    delta        = now - checkin_time
    total_sec    = int(delta.total_seconds())
    hours        = total_sec // 3600
    minutes      = (total_sec % 3600) // 60
    seconds      = total_sec % 60
    duration_str = f"{hours:02d}:{minutes:02d}:{seconds:02d}"

    print(f"[簽退] {student['name']} ({student['student_id']}) 在場 {duration_str} @ {time_str}")

    # LCD 顯示在場時長
    lcd_show("Goodbye!        ", f"Stay:{duration_str}")

    # 硬體回饋（綠燈 + 雙嗶，與簽到做區分）
    threading.Thread(target=led_on_then_off, args=(LED_GREEN, 2.0), daemon=True).start()
    beep(2, 0.12)

    save_record({
        "name":       student["name"],
        "student_id": student["student_id"],
        "date":       date_str,
        "time":       time_str,
        "status":     "check_out",
        "uid":        uid,
        "duration":   duration_str,
    })

    time.sleep(2.5)
    lcd_ready()

def handle_unauthorized(uid: str):
    now      = datetime.now()
    time_str = now.strftime("%H:%M:%S")
    date_str = now.strftime("%Y-%m-%d")

    print(f"[拒絕] 未授權卡片 UID={uid} @ {time_str}")

    # LCD
    lcd_show("Access Denied!  ", f"UID:{uid[:8]}")

    # 硬體回饋（紅燈 + 三連嗶）
    threading.Thread(target=led_on_then_off, args=(LED_RED, 2.0), daemon=True).start()
    beep(3, 0.12)

    # 存檔（方便後台查看異常）
    save_record({
        "name":       "未知",
        "student_id": "N/A",
        "date":       date_str,
        "time":       time_str,
        "status":     "unauthorized",
        "uid":        uid,
    })

    time.sleep(2.5)
    lcd_ready()

# ============================================================
#  RFID 主迴圈（跑在 daemon 執行緒）
# ============================================================
def rfid_loop():
    global _last_uid, _last_time

    mfrc = MFRC522.MFRC522()
    lcd_ready()
    print("[系統] RFID 感應就緒，等待刷卡...")

    while True:
        status, _ = mfrc.MFRC522_Request(mfrc.PICC_REQIDL)
        if status != mfrc.MI_OK:
            time.sleep(0.1)
            continue

        status, uid_raw = mfrc.MFRC522_Anticoll()
        if status != mfrc.MI_OK:
            time.sleep(0.1)
            continue

        uid = "".join(f"{b:02X}" for b in uid_raw[:4])
        now = time.time()

        # 防重複刷卡
        if uid == _last_uid and (now - _last_time) < _COOLDOWN:
            time.sleep(0.1)
            continue

        _last_uid  = uid
        _last_time = now

        student = load_students().get(uid)
        if student is None:
            # UID 不在名單內，直接拒絕
            handle_unauthorized(uid)
            continue

        # UID 符合，進行 Key A 磁區驗證
        mfrc.MFRC522_SelectTag(uid_raw)
        auth_status = mfrc.MFRC522_Auth(
            mfrc.PICC_AUTHENT1A, AUTH_BLOCK, KEY_A, uid_raw
        )
        mfrc.MFRC522_StopCrypto1()

        if auth_status != mfrc.MI_OK:
            handle_unauthorized(uid)
        elif uid in _checked_in:
            handle_checkout(student, uid)
        else:
            handle_checkin(student, uid)

# ============================================================
#  Flask API 路由
# ============================================================
@app.route("/")
def index():
    return render_template("index.html")

@app.route("/api/records")
def api_records():
    records = load_records()
    today   = datetime.now().strftime("%Y-%m-%d")

    auth_today   = [r for r in records if r.get("date") == today and r.get("status") == "check_in"]
    auth_total   = [r for r in records if r.get("status") == "check_in"]
    unauth_total = [r for r in records if r.get("status") == "unauthorized"]

    return jsonify({
        "today_count":       len(auth_today),
        "total_count":       len(auth_total),
        "unauthorized_count":len(unauth_total),
        "records":           list(reversed(records)),   # 最新在最上面
    })

@app.route("/api/lcd")
def api_lcd():
    return jsonify(_lcd_state)


# ── 公開學生列表（供臉部辨識前端使用）────────────────────
@app.route("/api/students")
def api_students():
    students = load_students()
    result = {}
    for uid, info in students.items():
        has_face = os.path.exists(os.path.join(FACES_DIR, f"{uid}.jpg"))
        result[uid] = {**info, "has_face": has_face}
    return jsonify({"students": result})


# ── 臉部辨識簽到 ─────────────────────────────────────────
@app.route("/api/face-checkin", methods=["POST"])
def api_face_checkin():
    data     = request.json or {}
    uid      = data.get("uid", "").strip()
    students = load_students()
    student  = students.get(uid)
    if not student:
        return jsonify({"status": "error", "message": "找不到符合的人臉"}), 404
    if uid in _checked_in:
        handle_checkout(student, uid)
        return jsonify({"status": "ok", "action": "check_out", "name": student["name"]})
    handle_checkin(student, uid)
    return jsonify({"status": "ok", "action": "check_in", "name": student["name"]})


# ── 管理者：取得學生名單 ──────────────────────────────────
@app.route("/api/admin/students", methods=["GET"])
def api_admin_get_students():
    if request.args.get("password") != ADMIN_PASSWORD:
        return jsonify({"error": "unauthorized"}), 401
    students = load_students()
    result = {}
    for uid, info in students.items():
        has_face = os.path.exists(os.path.join(FACES_DIR, f"{uid}.jpg"))
        result[uid] = {**info, "has_face": has_face}
    return jsonify({"students": result})


# ── 管理者：新增學生 ─────────────────────────────────────
@app.route("/api/admin/students", methods=["POST"])
def api_admin_add_student():
    data = request.json or {}
    if data.get("password") != ADMIN_PASSWORD:
        return jsonify({"error": "unauthorized"}), 401
    uid        = data.get("uid", "").strip().upper()
    name       = data.get("name", "").strip()
    student_id = data.get("student_id", "").strip()
    if not uid or not name or not student_id:
        return jsonify({"error": "missing_fields"}), 400
    students       = load_students()
    students[uid]  = {"name": name, "student_id": student_id}
    save_students(students)
    return jsonify({"status": "ok"})


# ── 管理者：編輯學生 ─────────────────────────────────────
@app.route("/api/admin/students/<uid>", methods=["PUT"])
def api_admin_update_student(uid):
    data = request.json or {}
    if data.get("password") != ADMIN_PASSWORD:
        return jsonify({"error": "密碼錯誤"}), 403
    uid = uid.upper()
    students = load_students()
    if uid not in students:
        return jsonify({"error": "找不到該 UID"}), 404

    new_uid = data.get("new_uid", uid).strip().upper()
    name    = data.get("name", "").strip()
    sid     = data.get("student_id", "").strip()
    if not name or not sid:
        return jsonify({"error": "name、student_id 為必填"}), 400

    if new_uid != uid:
        if new_uid in students:
            return jsonify({"error": f"UID {new_uid} 已存在"}), 409
        # 重新命名人臉照片
        old_face = os.path.join(FACES_DIR, f"{uid}.jpg")
        new_face = os.path.join(FACES_DIR, f"{new_uid}.jpg")
        if os.path.exists(old_face):
            os.rename(old_face, new_face)
        del students[uid]

    students[new_uid] = {"name": name, "student_id": sid}
    save_students(students)
    logger.info("更新學生：%s → %s（%s）UID=%s", uid, name, sid, new_uid)
    return jsonify({"status": "ok", "uid": new_uid})


# ── 管理者：刪除學生 ─────────────────────────────────────
@app.route("/api/admin/students/<uid>", methods=["DELETE"])
def api_admin_delete_student(uid):
    data = request.json or {}
    if data.get("password") != ADMIN_PASSWORD:
        return jsonify({"error": "unauthorized"}), 401
    students = load_students()
    if uid not in students:
        return jsonify({"error": "not_found"}), 404
    del students[uid]
    save_students(students)
    face_path = os.path.join(FACES_DIR, f"{uid}.jpg")
    if os.path.exists(face_path):
        os.remove(face_path)
    return jsonify({"status": "ok"})


# ── 管理者：儲存臉部照片 ─────────────────────────────────
@app.route("/api/admin/students/<uid>/face", methods=["POST"])
def api_admin_save_face(uid):
    data = request.json or {}
    if data.get("password") != ADMIN_PASSWORD:
        return jsonify({"error": "unauthorized"}), 401
    image_b64 = data.get("image", "")
    if not image_b64:
        return jsonify({"error": "no_image"}), 400
    if "," in image_b64:
        image_b64 = image_b64.split(",")[1]
    img_bytes = base64.b64decode(image_b64)
    with open(os.path.join(FACES_DIR, f"{uid}.jpg"), "wb") as f:
        f.write(img_bytes)
    return jsonify({"status": "ok"})

@app.route("/api/clear", methods=["POST"])
def api_clear():
    with open(RECORDS_FILE, "w", encoding="utf-8") as f:
        json.dump([], f)
    print("[管理] 紀錄已清除")
    return jsonify({"status": "ok"})

# ============================================================
#  程式進入點
# ============================================================
if __name__ == "__main__":
    try:
        threading.Thread(target=rfid_loop, daemon=True).start()
        # 若有自簽憑證則跑 HTTPS（讓瀏覽器允許相機存取）
        # 生成方式：openssl req -x509 -newkey rsa:2048 -nodes \
        #           -out cert.pem -keyout key.pem -days 365 -subj "/CN=raspberrypi"
        _cert = os.path.join(os.path.dirname(__file__), "cert.pem")
        _key  = os.path.join(os.path.dirname(__file__), "key.pem")
        # port 443 需要 root，用 sudo python app_rfid.py 執行
        # port 5000 一般使用者即可，但學校網路可能封鎖
        _port = int(os.getenv("PORT", 443))
        if os.path.exists(_cert) and os.path.exists(_key):
            logger.info("以 HTTPS 模式啟動（port %d，憑證：%s）", _port, _cert)
            app.run(host="0.0.0.0", port=_port, debug=False,
                    ssl_context=(_cert, _key))
        else:
            logger.warning("未找到 cert.pem/key.pem，以 HTTP 啟動（相機功能需在 localhost 使用）")
            app.run(host="0.0.0.0", port=_port, debug=False)
    finally:
        GPIO.output(LED_GREEN, GPIO.LOW)
        GPIO.output(LED_RED,   GPIO.LOW)
        _buzzer_pwm.stop()
        GPIO.cleanup()
        if mylcd is not None:
            try:
                mylcd.clear()
                mylcd.close(clear=True)
            except Exception:
                pass
        logger.info("程式結束，GPIO 與 LCD 已釋放")
