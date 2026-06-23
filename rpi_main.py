import sys
import time
import threading
import queue
import os
import pygame
import RPi.GPIO as GPIO
import socket
import cv2
import pickle
import struct
from collections import deque
import statistics
import requests

# =========================================================
# ⚙️ 카카오 디벨로퍼스 설정
# =========================================================
ACCESS_TOKEN = "7C5i-nXGdOMxHbsjcaYovQJAE8kJbtWZAAAAAQoXFO4AAAGe8rFCU7bGP5Eb7W-4"

direction_map = {
    "left": "왼쪽", "front": "정면", "right": "오른쪽",
    "right_under": "오른쪽 하단", "left_under": "왼쪽 하단", "front_under": "정면 하단"
}

# 🛠️ [팀원 API] 각 방향별 알림 전송 여부 추적 딕셔너리
alert_sent_status = {key: False for key in direction_map}

def send_kakao_alert(message):
    """메인 루프 병목 방지를 위해 별도 스레드에서 비동기로 실행될 함수"""
    def _send():
        try:
            url = "https://kapi.kakao.com/v2/api/talk/memo/default/send"
            headers = {"Authorization": f"Bearer {ACCESS_TOKEN}"}
            data = {
                "template_object": f'{{"object_type": "text", "text": "{message}", "link": {{"web_url": "https://developers.kakao.com"}}}}'
            }
            response = requests.post(url, headers=headers, data=data, timeout=3)
            if response.status_code == 200:
                print(f"[카카오톡 알림 전송 완료] {message}", flush=True)
            else:
                print(f"[카카오톡 에러] 코드 {response.status_code}", flush=True)
        except Exception as e:
            print(f"[카카오톡 전송 실패] {e}", flush=True)

    threading.Thread(target=_send, daemon=True).start()

# =========================================================
# 1. RPi.GPIO 초기화
# =========================================================
GPIO.setmode(GPIO.BCM)
GPIO.setwarnings(False)

SENSORS = {
    'front':       {'trig': 4,  'echo': 5,  'limit': 50},
    'left':        {'trig': 6,  'echo': 27, 'limit': 30},
    'right':       {'trig': 12, 'echo': 13, 'limit': 30},
    'left_under':  {'trig': 16, 'echo': 17, 'limit': 55},
    'right_under': {'trig': 20, 'echo': 21, 'limit': 55}
}

for name, pins in SENSORS.items():
    GPIO.setup(pins['trig'], GPIO.OUT)
    GPIO.setup(pins['echo'], GPIO.IN, pull_up_down=GPIO.PUD_DOWN)
    GPIO.output(pins['trig'], False)

# =========================================================
# 2. 사운드 및 큐 설정
# =========================================================
pygame.mixer.init()
VOLUME_LEVEL = 0.40

SOUND_DIR = "/home/team-d/obstacle_detection/sounds/"
OBJECT_DIR = SOUND_DIR + "objects/"
SYSTEM_DIR = SOUND_DIR + "system/"
CAUTION_DIR = SOUND_DIR + "caution/"
HELLO_DIR = SOUND_DIR + "hello/"

audio_queue = queue.Queue(maxsize=1)
yolo_queue = queue.Queue(maxsize=10)
running = True

# 🛠️ [초음파 최적화] 시작하자마자 울리는 오작동 방지를 위해 초기값을 0.0으로 세팅
dist_data = {name: 0.0 for name in SENSORS}
distance_buffer = {name: deque(maxlen=5) for name in SENSORS}
dist_lock = threading.Lock()

# =========================================================
# 3. 초음파 센서 측정 함수 및 워커 (정밀 보정 버전)
# =========================================================
def measure_distance(trig, echo):
    GPIO.output(trig, True)
    time.sleep(0.00001)
    GPIO.output(trig, False)

    timeout_start = time.time()
    pulse_start = timeout_start
    pulse_end = timeout_start

    # 🛠️ [초음파 최적화] 타임아웃 수식 수정을 통해 계단(허공) 감지 시 정확히 999.0 반환
    while GPIO.input(echo) == 0:
        pulse_start = time.time()
        if pulse_start - timeout_start > 0.04:
            return 999.0

    while GPIO.input(echo) == 1:
        pulse_end = time.time()
        if pulse_end - pulse_start > 0.04:
            return 999.0

    # 🛠️ [초음파 최적화] 음속 계산 계수 정밀 보정 (17150)
    return round((pulse_end - pulse_start) * 17150, 1)

def ultrasonic_worker():
    while running:
        for name, pins in SENSORS.items():
            if not running: break
            distance = measure_distance(pins['trig'], pins['echo'])

            # 999.0(계단)도 무조건 버퍼에 포함시켜 중간값 필터링 수행
            distance_buffer[name].append(distance)
            with dist_lock:
                dist_data[name] = round(statistics.median(distance_buffer[name]), 1)

            time.sleep(0.02)
        time.sleep(0.01)

# =========================================================
# 📡 4. 영상 송신(무선 스트리밍) + 텍스트 수신 통합 서버
# =========================================================
def network_server_worker():
    video_server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    video_server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    video_server.bind(('0.0.0.0', 9998))
    video_server.listen(1)

    text_server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    text_server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    text_server.bind(('0.0.0.0', 9999))
    text_server.listen(1)

    print("[RPi] 무선 네트워크 서버 오픈! PC 연결을 대기합니다...", flush=True)

    while running:
        try:
            v_conn, v_addr = video_server.accept()
            t_conn, t_addr = text_server.accept()
            print(f"[RPi] PC 브레인 도킹 완료! (영상/텍스트 세션 연결)", flush=True)

            cam = cv2.VideoCapture(0)
            cam.set(cv2.CAP_PROP_FRAME_WIDTH, 320)
            cam.set(cv2.CAP_PROP_FRAME_HEIGHT, 240)

            def text_receiver(conn):
                while running:
                    try:
                        data = conn.recv(1024)
                        if not data: break
                        line = data.decode('utf-8').strip().lower()
                        if line and not yolo_queue.full():
                            yolo_queue.put(line)
                    except: break

            t_thread = threading.Thread(target=text_receiver, args=(t_conn,), daemon=True)
            t_thread.start()

            while cam.isOpened() and running:
                ret, frame = cam.read()
                if not ret: break

                result, imgencode = cv2.imencode('.jpg', frame, [int(cv2.IMWRITE_JPEG_QUALITY), 45])
                data = pickle.dumps(imgencode, 0)
                size = len(data)

                v_conn.sendall(struct.pack(">L", size) + data)
                time.sleep(0.03)

            cam.release()
            v_conn.close()
            t_conn.close()
            print("[RPi] PC와 무선 연결 해제. 재대기합니다.", flush=True)
        except Exception as e:
            if running: print(f"[RPi 네트워크 에러] {e}", flush=True)
            time.sleep(1)

# =========================================================
# 5. 오디오 시스템
# =========================================================
def play_mp3(path):
    if os.path.exists(path):
        pygame.mixer.music.load(path)
        pygame.mixer.music.set_volume(VOLUME_LEVEL)
        pygame.mixer.music.play()
        while pygame.mixer.music.get_busy():
            time.sleep(0.01)

def audio_worker():
    global audio_queue
    while running:
        try:
            task = audio_queue.get(timeout=0.01)

            while not audio_queue.empty():
                try:
                    audio_queue.get_nowait()
                    audio_queue.task_done()
                except queue.Empty:
                    break

            task_type = task['type']

            if pygame.mixer.music.get_busy():
                pygame.mixer.music.stop()

            if task_type == 'caution':
                direction = task['direction']
                file_path = f"{CAUTION_DIR}caution_under.mp3" if 'under' in direction else f"{CAUTION_DIR}caution_{direction}.mp3"
                play_mp3(file_path)

            elif task_type == 'object_alert':
                direction = task['direction']
                cls_name = task['class_name']
                dir_file = f"{SYSTEM_DIR}{direction}.mp3"
                exist_file = f"{SYSTEM_DIR}exist.mp3"
                file1 = f"{OBJECT_DIR}{cls_name}.mp3"
                file2 = f"{OBJECT_DIR}{cls_name.replace('_', '')}.mp3"
                obj_file = file1 if os.path.exists(file1) else file2

                for f in [dir_file, obj_file, exist_file]:
                    if not audio_queue.empty():
                        break
                    play_mp3(f)

            audio_queue.task_done()
        except queue.Empty:
            continue

def play_audio(data):
    if not audio_queue.full():
        audio_queue.put(data)

# =========================================================
# ⚙️ 시스템 부팅 알림 수행
# =========================================================
print("라즈베리 파이 무선 스트리밍 통합 서버 가동...", flush=True)
start_hello_file = f"{HELLO_DIR}start.mp3"
if os.path.exists(start_hello_file): play_mp3(start_hello_file)

send_kakao_alert("함께 걷는 눈: 보행기 안전 모니터링을 시작합니다.")

threading.Thread(target=ultrasonic_worker, daemon=True).start()
threading.Thread(target=audio_worker, daemon=True).start()
threading.Thread(target=network_server_worker, daemon=True).start()

# 연속 경보 방지를 위한 플래그 딕셔너리
caution_spoken = {name: False for name in SENSORS}

# =========================================================
# 6. 메인 제어 루프
# =========================================================
try:
    while True:
        # --- 6-1. 초음파 센서 기반 장애물 탐지 ---
        for direction, pins in SENSORS.items():
            with dist_lock: dist = dist_data[direction]

            # 🛠️ [초음파 최적화 반영] 초기값(0.0)이나 물리적 오류값 무시
            if dist <= 0: continue

            direction_kr = direction_map.get(direction, direction)

            if 'under' in direction:
                # [계단/평지 구분 로직] 거리가 limit 이상이거나 999.0(허공)이면 계단으로 판정
                if dist >= pins['limit'] or dist == 999.0:
                    if not caution_spoken[direction]:
                        play_audio({'type': 'caution', 'direction': direction})
                        caution_spoken[direction] = True

                    # 팀원 API 딕셔너리 연동 (1회만 발송)
                    if not alert_sent_status.get(direction, False):
                        send_kakao_alert(f"⚠️ 경고: 보행기 {direction_kr} 쪽에 낙상/턱 위험 구역이 감지되었습니다! 확인이 필요합니다.")
                        alert_sent_status[direction] = True

                # [계단/평지 구분 로직] 거리가 한계치 미만이면 정상 평지로 판정
                else:
                    if dist < (pins['limit'] - 3):
                        caution_spoken[direction] = False
                        if alert_sent_status.get(direction, True):
                            alert_sent_status[direction] = False
            else:
                # 상단 센서에서 999.0은 평지(앞에 아무것도 없음)를 의미하므로 안전 상태로 초기화하고 무시
                if dist == 999.0:
                    caution_spoken[direction] = False
                    if alert_sent_status.get(direction, True):
                        alert_sent_status[direction] = False
                    continue

                if dist < pins['limit']:
                    if not caution_spoken[direction]:
                        play_audio({'type': 'caution', 'direction': direction})
                        caution_spoken[direction] = True

                    # 팀원 API 딕셔너리 연동 (1회만 발송)
                    if not alert_sent_status.get(direction, False):
                        send_kakao_alert(f"🚨 경고: 보행기 {direction_kr} 쪽에 장애물이 너무 가까이 접근했습니다! (거리: {dist:.1f}cm)")
                        alert_sent_status[direction] = True
                else:
                    if dist > (pins['limit'] + 10):
                        caution_spoken[direction] = False
                        if alert_sent_status.get(direction, True):
                            alert_sent_status[direction] = False

        # --- 6-2. PC(YOLOv8) 수신 데이터 기반 전방 고정 시설물 탐지 ---
        try: line = yolo_queue.get_nowait()
        except queue.Empty: line = ""

        if line:
            parts = line.split('_')
            if len(parts) >= 2:
                obj_dir = parts[0]
                obj_cls = "_".join(parts[1:])
                print(f"[연동 신호 수신] 방향: {obj_dir} | 객체: {obj_cls}", flush=True)
                play_audio({'type': 'object_alert', 'direction': obj_dir, 'class_name': obj_cls})

                obj_dir_kr = direction_map.get(obj_dir, obj_dir)
                send_kakao_alert(f"🔍 알림: {obj_dir_kr} 전방에 시설물 [{obj_cls}] 사물이 식별되어 안내 음성을 출력했습니다.")

        time.sleep(0.01)

except KeyboardInterrupt:
    print("\n종료 프로세스", flush=True)
finally:
    running = False
    send_kakao_alert("함께 걷는 눈: 보행기 운행을 종료합니다.")
    GPIO.cleanup()
    pygame.mixer.quit()
