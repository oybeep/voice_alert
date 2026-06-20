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

# =========================================================
# 1. RPi.GPIO 초기화
# =========================================================
GPIO.setmode(GPIO.BCM)
GPIO.setwarnings(False)

SENSORS = {
    'front':       {'trig': 4,  'echo': 5,  'limit': 50},
    'left':        {'trig': 6,  'echo': 27, 'limit': 30},
    'right':       {'trig': 12, 'echo': 13, 'limit': 30},
    'left_under':  {'trig': 16, 'echo': 17, 'limit': 60},
    'right_under': {'trig': 20, 'echo': 21, 'limit': 60}
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

dist_data = {name: 999.0 for name in SENSORS}
distance_buffer = {name: deque(maxlen=5) for name in SENSORS}
dist_lock = threading.Lock()

# =========================================================
# 3. 초음파 센서 측정 함수 및 워커
# =========================================================
def measure_distance(trig, echo):
    GPIO.output(trig, True)
    time.sleep(0.00001)
    GPIO.output(trig, False)

    pulse_start = time.time()
    pulse_end = time.time()
    timeout = time.time()

    while GPIO.input(echo) == 0:
        pulse_start = time.time()
        if pulse_start - timeout > 0.02: return 999.0

    timeout = time.time()
    while GPIO.input(echo) == 1:
        pulse_end = time.time()
        if pulse_end - timeout > 0.02: return 999.0

    return round((pulse_end - pulse_start) * 17000, 1)

def ultrasonic_worker():
    while running:
        for name, pins in SENSORS.items():
            if not running: break
            distance = measure_distance(pins['trig'], pins['echo'])
            if 2.0 <= distance <= 400.0:
                distance_buffer[name].append(distance)
                with dist_lock: dist_data[name] = round(statistics.median(distance_buffer[name]), 1)
            else:
                with dist_lock: dist_data[name] = 999.0
            time.sleep(0.02)
        time.sleep(0.01)

# =========================================================
# 📡 4. 영상 송신(무선 스트리밍) + 텍스트 수신 통합 서버
# =========================================================
def network_server_worker():
    # 영상 송신용 소켓 (Port: 9998)
    video_server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    video_server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    video_server.bind(('0.0.0.0', 9998))
    video_server.listen(1)

    # 결과 텍스트 수신용 소켓 (Port: 9999)
    text_server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    text_server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    text_server.bind(('0.0.0.0', 9999))
    text_server.listen(1)

    print("[RPi] 무선 네트워크 서버 오픈! PC 연결을 대기합니다...", flush=True)

    while running:
        try:
            # 두 포트 모두 PC 연결 승인
            v_conn, v_addr = video_server.accept()
            t_conn, t_addr = text_server.accept()
            print(f"[RPi] PC 브레인 도킹 완료! (영상/텍스트 세션 연결)", flush=True)

            # 라즈베리 파이에 연결된 웹캠 열기
            cam = cv2.VideoCapture(0)
            # 무선 대역폭 최적화를 위해 해상도 다운스케일링 ($320 \times 240$)
            cam.set(cv2.CAP_PROP_FRAME_WIDTH, 320)
            cam.set(cv2.CAP_PROP_FRAME_HEIGHT, 240)

            # 텍스트 수신을 위한 별도 스레드 구동
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

            # 메인 루프에서 영상 연속 프레임 무선 전송
            while cam.isOpened() and running:
                ret, frame = cam.read()
                if not ret: break

                # 이미지 JPEG 압축 (무선 전송 속도 향상, 렉 방지)
                result, imgencode = cv2.imencode('.jpg', frame, [int(cv2.IMWRITE_JPEG_QUALITY), 70])
                data = pickle.dumps(imgencode, 0)
                size = len(data)

                # 프레임 크기 데이터 정보 + 실제 이미지 데이터 전송
                v_conn.sendall(struct.pack(">L", size) + data)
                time.sleep(0.03) # 약 30 FPS 유지용 제한

            cam.release()
            v_conn.close()
            t_conn.close()
            print("[RPi] PC와 무선 연결 해제. 재대기합니다.", flush=True)
        except Exception as e:
            if running: print(f"[RPi 네트워크 에러] {e}", flush=True)
            time.sleep(1)

# =========================================================
# 5. 오디오 시스템 (기존 유지)
# =========================================================
def play_mp3(path):
    if os.path.exists(path):
        pygame.mixer.music.load(path)
        pygame.mixer.music.set_volume(VOLUME_LEVEL)
        pygame.mixer.music.play()
        while pygame.mixer.music.get_busy(): time.sleep(0.01)

def audio_worker():
    while running:
        try:
            task = audio_queue.get(timeout=0.01)
            with audio_queue.mutex: audio_queue.queue.clear()
            task_type = task['type']

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
                for f in [dir_file, obj_file, exist_file]: play_mp3(f)

            audio_queue.task_done()
        except queue.Empty: continue

def play_audio(data):
    if not audio_queue.full(): audio_queue.put(data)

# 가동
print("라즈베리 파이 무선 스트리밍 통합 서버 가동...", flush=True)
start_hello_file = f"{HELLO_DIR}start.mp3"
if os.path.exists(start_hello_file): play_mp3(start_hello_file)

threading.Thread(target=ultrasonic_worker, daemon=True).start()
threading.Thread(target=audio_worker, daemon=True).start()
threading.Thread(target=network_server_worker, daemon=True).start()

caution_spoken = {name: False for name in SENSORS}

# =========================================================
# 6. 메인 제어 루프
# =========================================================
try:
    while True:
        for direction, pins in SENSORS.items():
            with dist_lock: dist = dist_data[direction]
            if dist == 999.0 or dist <= 0: continue

            if 'under' in direction:
                if dist >= pins['limit']:
                    if not caution_spoken[direction]:
                        play_audio({'type': 'caution', 'direction': direction})
                        caution_spoken[direction] = True
                else:
                    if dist < (pins['limit'] - 3): caution_spoken[direction] = False
            else:
                if dist < pins['limit']:
                    if not caution_spoken[direction]:
                        play_audio({'type': 'caution', 'direction': direction})
                        caution_spoken[direction] = True
                else:
                    if dist > (pins['limit'] + 10): caution_spoken[direction] = False

        try: line = yolo_queue.get_nowait()
        except queue.Empty: line = ""

        if line:
            parts = line.split('_')
            if len(parts) >= 2:
                obj_dir = parts[0]
                obj_cls = "_".join(parts[1:])
                print(f"[연동 신호 수신] 방향: {obj_dir} | 객체: {obj_cls}", flush=True)
                play_audio({'type': 'object_alert', 'direction': obj_dir, 'class_name': obj_cls})

        time.sleep(0.01)

except KeyboardInterrupt:
    print("\n종료 프로세스", flush=True)
finally:
    running = False
    GPIO.cleanup()
    pygame.mixer.quit()
