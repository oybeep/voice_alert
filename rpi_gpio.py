import sys
import time
import threading
import queue
import os
import pygame
import pigpio
import socket
from collections import deque
import statistics

# =========================================================
# 1. pigpio 초기화
# =========================================================
pi = pigpio.pi()
if not pi.connected:
    print("pigpio 데몬에 연결 실패! 'sudo pigpiod' 를 실행하세요.", flush=True)
    sys.exit(1)

# =========================================================
# 2. 초음파 센서 핀 설정 (기존 하드웨어 구성 그대로)
# =========================================================
SENSORS = {
    'front':       {'trig': 4,  'echo': 5,  'limit': 50},
    'left':        {'trig': 6,  'echo': 27, 'limit': 30},
    'right':       {'trig': 12, 'echo': 13, 'limit': 30},
    'left_under':  {'trig': 16, 'echo': 17, 'limit': 60},
    'right_under': {'trig': 20, 'echo': 21, 'limit': 60}
}

for name, pins in SENSORS.items():
    pi.set_mode(pins['trig'], pigpio.OUTPUT)
    pi.set_mode(pins['echo'], pigpio.INPUT)
    pi.set_pull_up_down(pins['echo'], pigpio.PUD_DOWN)
    pins['start_tick'] = None
    pins['pending'] = False
    pins['last_trigger_time'] = 0

# =========================================================
# 3. 사운드 및 큐 설정
# =========================================================
pygame.mixer.init()
VOLUME_LEVEL = 0.40

SOUND_DIR = "/home/team-d/obstacle_detection/sounds/"
OBJECT_DIR = SOUND_DIR + "objects/"
SYSTEM_DIR = SOUND_DIR + "system/"
CAUTION_DIR = SOUND_DIR + "caution/"
HELLO_DIR = SOUND_DIR + "hello/"

audio_queue = queue.Queue(maxsize=1)
yolo_queue = queue.Queue(maxsize=10)  # PC에서 받아온 텍스트가 쌓일 큐
running = True

dist_data = {name: 999.0 for name in SENSORS}
distance_buffer = {name: deque(maxlen=5) for name in SENSORS}
dist_lock = threading.Lock()

# =========================================================
# 4. 초음파 및 필터 스레드 로직 (기존 성공 버전 유지)
# =========================================================
def echo_callback(gpio, level, tick):
    for name, pins in SENSORS.items():
        if pins['echo'] == gpio:
            if level == pigpio.RISING_EDGE:
                pins['start_tick'] = tick
                pins['pending'] = True
            elif level == pigpio.FALLING_EDGE and pins.get('pending'):
                if pins['start_tick'] is not None:
                    duration_us = pigpio.tickDiff(pins['start_tick'], tick)
                    distance = duration_us * 0.01715
                    if 2.0 <= distance <= 400.0:
                        distance_buffer[name].append(distance)
                        filtered_distance = statistics.median(distance_buffer[name])
                        with dist_lock:
                            dist_data[name] = round(filtered_distance, 1)
                pins['pending'] = False
                pins['start_tick'] = None
            break

for name, pins in SENSORS.items():
    pi.callback(pins['echo'], pigpio.EITHER_EDGE, echo_callback)

def check_timeouts():
    current = time.time()
    for name, pins in SENSORS.items():
        if pins.get('pending') and pins.get('last_trigger_time'):
            if current - pins['last_trigger_time'] > 0.05:
                with dist_lock:
                    dist_data[name] = 999.0
                pins['pending'] = False
                pins['start_tick'] = None

def ultrasonic_worker():
    while running:
        for name, pins in SENSORS.items():
            if not running: break
            pi.gpio_trigger(pins['trig'], 10, 1)
            pins['last_trigger_time'] = time.time()
            pins['pending'] = True
            time.sleep(0.04)
        check_timeouts()
        time.sleep(0.01)

# =========================================================
# 📡 5. PC로부터 YOLO 인식 결과 문자열을 받는 소켓 서버 스레드
# =========================================================
def socket_receiver_worker():
    server_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server_socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    server_socket.bind(('0.0.0.0', 9999))  # 9999 포트 개방
    server_socket.listen(1)
    
    print("[RPi] PC AI 브레인의 연결을 기다리는 중 (Port: 9999)...", flush=True)
    
    while running:
        try:
            conn, addr = server_socket.accept()
            print(f"[RPi] PC 브레인 연결 완료: {addr}", flush=True)
            
            while running:
                data = conn.recv(1024)
                if not data:
                    break
                
                line = data.decode('utf-8').strip().lower()
                if line and not yolo_queue.full():
                    yolo_queue.put(line)
            conn.close()
            print("[RPi] PC와 연결이 끊겼습니다. 재대기합니다.", flush=True)
        except Exception as e:
            if running:
                print(f"[RPi 소켓 에러] {e}", flush=True)
            time.sleep(1)

# =========================================================
# 6. 오디오 재생 시스템 (기존 로직 유지)
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

# 스레드 구동
print("라즈베리 파이 바디 서버 엔진 가동...", flush=True)
start_hello_file = f"{HELLO_DIR}start.mp3"
if os.path.exists(start_hello_file): play_mp3(start_hello_file)

threading.Thread(target=ultrasonic_worker, daemon=True).start()
threading.Thread(target=audio_worker, daemon=True).start()
threading.Thread(target=socket_receiver_worker, daemon=True).start()

caution_spoken = {name: False for name in SENSORS}

# =========================================================
# 7. 메인 제어 루프
# =========================================================
try:
    while True:
        # [초음파 판정 알고리즘]
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

        # [PC 수신 텍스트 기반 음성 출력 알고리즘]
        try:
            line = yolo_queue.get_nowait()
        except queue.Empty:
            line = ""

        if line:
            parts = line.split('_')
            if len(parts) >= 2:
                obj_dir = parts[0]
                obj_cls = "_".join(parts[1:])
                print(f"[수신 신호 연동] 방향: {obj_dir} | 객체: {obj_cls}", flush=True)
                play_audio({
                    'type': 'object_alert',
                    'direction': obj_dir,
                    'class_name': obj_cls
                })

        time.sleep(0.01)

except KeyboardInterrupt:
    print("\n종료", flush=True)
finally:
    running = False
    pi.stop()
    pygame.mixer.quit()
