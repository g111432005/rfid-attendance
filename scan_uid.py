#!/usr/bin/env python3
"""
scan_uid.py — 掃描 RFID 卡片並印出 UID
用途：取得卡片 UID 後填入 students.json
使用方式：sudo python3 scan_uid.py
按 Ctrl+C 結束
"""

import signal
import sys
import time

import RPi.GPIO as GPIO
import mfrc522 as MFRC522

GPIO.setmode(GPIO.BCM)
GPIO.setwarnings(False)

running = True


def stop(sig, frame):
    global running
    print("\n結束掃描，GPIO 已釋放。")
    running = False
    GPIO.cleanup()
    sys.exit(0)


signal.signal(signal.SIGINT, stop)

reader = MFRC522.MFRC522()

print("=" * 40)
print("  RFID UID 掃描工具")
print("  將卡片靠近讀卡機")
print("  按 Ctrl+C 結束")
print("=" * 40)

last_uid = None

while running:
    status, _ = reader.MFRC522_Request(reader.PICC_REQIDL)
    if status != reader.MI_OK:
        time.sleep(0.1)
        continue

    status, uid = reader.MFRC522_Anticoll()
    if status != reader.MI_OK:
        time.sleep(0.1)
        continue

    uid_hex = "".join(f"{b:02X}" for b in uid[:4])

    # 同一張卡不重複印
    if uid_hex == last_uid:
        time.sleep(0.5)
        continue

    last_uid = uid_hex
    print(f"\n✅ 偵測到卡片")
    print(f"   UID（十六進位）: {uid_hex}")
    print(f"   填入 students.json 的格式：")
    print(f'   "{uid_hex}": {{"name": "姓名", "student_id": "學號"}}')
    print()

    time.sleep(1.5)
    last_uid = None  # 允許再次掃描同一張卡
