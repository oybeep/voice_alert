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

# ToF 센서용 I2C 라이브러리
import board
import busio
import adafruit_vl53l0x

# =========================================================
# ⚙️ 1. 카카오 디벨로퍼스 설정
# =========================================================
# 두 번째 코드의 최신 토큰 값으로 반영
ACCESS_TOKEN = "V6vqe5l-eiLXsvP4sm8jgiWEKanHg1k_AAAAAQoNDF4AAAGe-ATlELbGP5Eb7W-4"

# 두 번째 코드의 확장된 방향 맵 반영
direction_map = {
    "left": "왼쪽",
    "front": "정면",
    "right": "오른쪽",
    "center_under": "하단 중앙",
    "right_under": "오른쪽 하단",
    "left_under": "왼쪽 하단",
    "front_under": "정면 하단"
}

alert_status = {key: False for key in direction_map.keys()}

def send_kakao_alert(message):
    def _send():
        try:
            url = "https://kapi.kakao.com/v2/api/talk/memo/default/send"
            headers = {"Authorization": f"Bearer {ACCESS_TOKEN}"}
            data = {
                "template_object": f'{{"object_type": "text", "text": "{message}", "link": {{"web_url": "https://developers.kakao.com"}}}}'
            }
            # 지연 방지를 위해 타임아웃 2초 설정
            response = requests.post(url, headers=headers, data=data, timeout=2)
            if response.status_code == 200:
                print(f"\n[카카오톡 알림 전송 완료] {message}", flush=True)
            else:
                print(f"\n[카카오톡 에러] 코드 {response.status_code}", flush=True)
        except Exception as e:
            print(f"\n[카카오톡 전송 실패] {e}", flush=True)

    threading.Thread(target=_send, daemon=True).start()

# =========================================================
# 2. RPi.GPIO 및 하드웨어 센서 초기화 (초음파 3개 + ToF 1개)
# =========================================================
GPIO.setmode(GPIO.BCM)
GPIO.setwarnings(False)

# 🟢 상단 초음파 센서 (3개)
SENSORS_US = {
    'front': {'trig': 4,  'echo': 5,  'limit': 50},
    'left':  {'trig': 6,  'echo': 27, 'limit': 30},
    'right': {'trig': 12, 'echo': 13, 'limit': 30}
}

for name, pins in SENSORS_US.items():
    GPIO.setup(pins['trig'], GPIO.OUT)
    GPIO.setup(pins['echo'], GPIO.IN, pull_up_down=GPIO.PUD_DOWN)
    GPIO.output(pins['trig'], False)

# 🔵 하단 ToF 센서 (1개)
SENSORS_TOF = {
    'center_under': {'limit': 80}
}

# I2C 통신 및 ToF 센서 객체 생성
try:
    i2c = busio.I2C(board.SCL, board.SDA)
    tof_sensor = adafruit_vl53l0x.VL53L0X(i2c)
    print("[하드웨어] ToF 센서 연결 성공")
except Exception as e:
    print(f"[하드웨어 에러] ToF 센서를 찾을 수 없습니다: {e}")
    tof_sensor = None

# =========================================================
# 3. 사운드 및 큐, 데이터 버퍼 설정
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

# 모든 센서 데이터 통합 (초기값 0.0)
all_sensors = list(direction_map.keys()) # 모든 가용 방향에 대응 버퍼 준비
dist_data = {name: 0.0 for name in all_sensors}
distance_buffer = {name: deque(maxlen=5) for name in all_sensors}
dist_lock = threading.Lock()

caution_spoken = {name: False for name in all_sensors}

# =========================================================
# 4-1. 초음파 센서 전용 워커
# =========================================================
def measure_ultrasonic(trig, echo):
    GPIO.output(trig, True)
    time.sleep(0.00001)
    GPIO.output(trig, False)

    timeout_start = time.time()
    pulse_start = timeout_start
    pulse_end = timeout_start

    # 타임아웃 40ms 초과 시 허공(999.0) 반환
    while GPIO.input(echo) == 0:
        pulse_start = time.time()
        if pulse_start - timeout_start > 0.04: return 999.0

    while GPIO.input(echo) == 1:
        pulse_end = time.time()
        if pulse_end - pulse_start > 0.04: return 999.0

    return round((pulse_end - pulse_start) * 17150, 1)

def ultrasonic_worker():
    while running:
        for name, pins in SENSORS_US.items():
            if not running: break
            distance = measure_ultrasonic(pins['trig'], pins['echo'])

            distance_buffer[name].append(distance)
            with dist_lock:
                dist_data[name] = round(statistics.median(distance_buffer[name]), 1)
            time.sleep(0.02)
        time.sleep(0.01)

# =========================================================
# 4-2. ToF 센서 전용 워커
# =========================================================
def tof_worker():
    while running:
        if not running: break

        if tof_sensor is not None:
            try:
                # VL53L0X 센서는 mm 단위로 반환하므로 10을 나누어 cm로 변환
                dist_cm = tof_sensor.range / 10.0

                # 측정 범위를 벗어난 비정상 값이거나 너무 멀면 허공(계단)으로 간주
                if dist_cm > 120.0 or dist_cm <= 0:
                    dist_cm = 999.0
            except Exception:
                dist_cm = 999.0
        else:
            dist_cm = 999.0

        distance_buffer['center_under'].append(dist_cm)
        with dist_lock:
            dist_data['center_under'] = round(statistics.median(distance_buffer['center_under']), 1)

        time.sleep(0.03)

# =========================================================
# 5. 영상 송신(무선 스트리밍) + 텍스트 수신 통합 서버
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
# 6. 오디오 시스템
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
                    if not audio_queue.empty(): break
                    play_mp3(f)

            audio_queue.task_done()
        except queue.Empty:
            continue

def play_audio(data):
    if not audio_queue.full():
        audio_queue.put(data)

# =========================================================
# ⚙️ 시스템 부팅 알림 및 스레드 가동
# =========================================================
print("라즈베리 파이 무선 스트리밍 통합 서버 가동...", flush=True)
start_hello_file = f"{HELLO_DIR}start.mp3"
if os.path.exists(start_hello_file): play_mp3(start_hello_file)

send_kakao_alert("함께 걷는 눈: 보행기 안전 모니터링을 시작합니다.")

threading.Thread(target=ultrasonic_worker, daemon=True).start()
threading.Thread(target=tof_worker, daemon=True).start()
threading.Thread(target=audio_worker, daemon=True).start()
threading.Thread(target=network_server_worker, daemon=True).start()

# =========================================================
# 7. 메인 제어 루프
# =========================================================
ALL_SENSORS_COMBINED = {**SENSORS_US, **SENSORS_TOF}

try:
    while True:
        # 실시간 모니터링 출력
        print(f"[LIVE] 정면:{dist_data['front']}cm | 좌:{dist_data['left']}cm | 우:{dist_data['right']}cm | 하단:{dist_data['center_under']}cm    ", end="\r", flush=True)

        # --- 7-1. 통합 센서 기반 장애물 탐지 ---
        for direction, pins in ALL_SENSORS_COMBINED.items():
            with dist_lock: dist = dist_data[direction]

            # 완전한 오류값(0 이하) 무시
            if dist <= 0: continue

            direction_kr = direction_map.get(direction, direction)

            # 🛠️ 하단 센서 (ToF) - 계단 / 낙하 감지 (일정 거리 이상이거나 측정 불가일 때 작동)
            if 'under' in direction:
                if dist >= pins['limit'] or dist == 999.0:
                    if not caution_spoken[direction]:
                        play_audio({'type': 'caution', 'direction': direction})
                        caution_spoken[direction] = True

                    if not alert_status.get(direction, False):
                        send_kakao_alert(f"⚠️ 경고: 보행기 {direction_kr}에 낙상/턱 위험 구역이 감지되었습니다! (거리: {dist}cm)")
                        alert_status[direction] = True
                else:
                    if dist < (pins['limit'] - 3):
                        caution_spoken[direction] = False
                        alert_status[direction] = False

            # 🛠️ 상단 센서 (초음파) - 전/측방 충돌 감지
            else:
                if dist == 999.0:
                    caution_spoken[direction] = False
                    continue

                if dist < pins['limit']:
                    if not caution_spoken[direction]:
                        play_audio({'type': 'caution', 'direction': direction})
                        caution_spoken[direction] = True

                    if not alert_status.get(direction, False):
                        send_kakao_alert(f"🚨 경고: 보행기 {direction_kr} 쪽에 장애물이 너무 가까이 접근했습니다! (거리: {dist:.1f}cm)")
                        alert_status[direction] = True
                else:
                    if dist > (pins['limit'] + 10):
                        caution_spoken[direction] = False
                        alert_status[direction] = False

        # --- 7-2. PC(YOLOv8) 수신 데이터 기반 고정 시설물 탐지 ---
        # 수정사항: 사물 인식 알림은 카카오톡으로 전송하지 않고 터미널 [Log] 및 안내 음성만 출력하도록 변경
        try: line = yolo_queue.get_nowait()
        except queue.Empty: line = ""

        if line:
            parts = line.split('_')
            if len(parts) >= 2:
                obj_dir = parts[0]
                obj_cls = "_".join(parts[1:])

                # 음성 안내 큐 전달
                play_audio({'type': 'object_alert', 'direction': obj_dir, 'class_name': obj_cls})

                # 카카오톡 전송을 제거하고 요구하신 터미널 형식의 로그 출력으로 대체
                obj_dir_kr = direction_map.get(obj_dir, obj_dir)
                print(f"\n[Log] 인식된 사물: {obj_cls} | 방향: {obj_dir_kr} (안내 음성 출력)", flush=True)

        time.sleep(0.01)

except KeyboardInterrupt:
    print("\n종료 프로세스", flush=True)
finally:
    running = False
    send_kakao_alert("함께 걷는 눈: 보행기 운행을 종료합니다.")
    GPIO.cleanup()
    pygame.mixer.quit()
