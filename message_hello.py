import sys
import time
import threading
import queue
import os
import pygame
import pigpio          # 정밀 초음파 타이밍용 라이브러리

# =========================================================
# 1. pigpio 초기화
# =========================================================
pi = pigpio.pi()
if not pi.connected:
    print("pigpio 데몬에 연결 실패! 'sudo pigpiod' 를 실행하세요.")
    sys.exit(1)

# =========================================================
# 2. 초음파 센서 핀 설정 (하단 센서 limit을 60으로 수정)
# =========================================================
SENSORS = {
    'front':      {'trig': 4,  'echo': 5,  'limit': 50},
    'left':       {'trig': 6,  'echo': 27, 'limit': 30},
    'right':      {'trig': 12, 'echo': 13, 'limit': 30},
    'left_under': {'trig': 16, 'echo': 17, 'limit': 60}, 
    'right_under':{'trig': 20, 'echo': 21, 'limit': 60} 
}

# pigpio 모드 설정 + 상태 초기화
for name, pins in SENSORS.items():
    pi.set_mode(pins['trig'], pigpio.OUTPUT)
    pi.set_mode(pins['echo'], pigpio.INPUT)
    pins['start_tick'] = None
    pins['pending'] = False
    pins['last_trigger_time'] = 0

# =========================================================
# 3. 사운드 설정
# =========================================================
pygame.mixer.init()
VOLUME_LEVEL = 0.15
SOUND_DIR = "/home/team-d/obstacle_detection/sounds/"
OBJECT_DIR = SOUND_DIR + "objects/"
SYSTEM_DIR = SOUND_DIR + "system/"
CAUTION_DIR = SOUND_DIR + "caution/"
HELLO_DIR = SOUND_DIR + "hello/"

# =========================================================
# 4. 오디오 큐
# =========================================================
audio_queue = queue.Queue(maxsize=1)
running = True

# =========================================================
# 5. 거리 데이터 + Lock (스레드 안전)
# =========================================================
dist_data = {name: 999.0 for name in SENSORS}
dist_lock = threading.Lock()

# =========================================================
# 6. pigpio 콜백 (Echo rising/falling 감지)
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
                    distance = duration_us * 0.01715          # cm 변환
                    with dist_lock:
                        dist_data[name] = round(distance, 1)
                pins['pending'] = False
                pins['start_tick'] = None
            break

# 콜백 등록
for name, pins in SENSORS.items():
    pi.callback(pins['echo'], pigpio.EITHER_EDGE, echo_callback)

# =========================================================
# 7. 타임아웃 체크 함수
# =========================================================
def check_timeouts():
    current = time.time()
    for name, pins in SENSORS.items():
        if pins.get('pending') and pins.get('last_trigger_time'):
            if current - pins['last_trigger_time'] > 0.04:   # 40ms 타임아웃
                with dist_lock:
                    dist_data[name] = 999.0
                pins['pending'] = False
                pins['start_tick'] = None

# =========================================================
# 8. 초음파 워커 스레드 (pigpio 버전)
# =========================================================
def ultrasonic_worker():
    while running:
        for name, pins in SENSORS.items():
            pi.gpio_trigger(pins['trig'], 10, 1)
            pins['last_trigger_time'] = time.time()
            pins['pending'] = True
            time.sleep(0.055)          # 센서 간 간섭 방지 + 에코 대기

        check_timeouts()
        time.sleep(0.01)

# =========================================================
# 9~11. 오디오 관련 함수들
# =========================================================
def play_mp3(path):
    if os.path.exists(path):
        pygame.mixer.music.load(path)
        pygame.mixer.music.set_volume(VOLUME_LEVEL)
        pygame.mixer.music.play()
        while pygame.mixer.music.get_busy():
            time.sleep(0.01)

def audio_worker():
    while running:
        try:
            task = audio_queue.get(timeout=0.01)
            with audio_queue.mutex:
                audio_queue.queue.clear()
            task_type = task['type']

            if task_type == 'caution':
                direction = task['direction']
                if 'under' in direction:
                    file_path = f"{CAUTION_DIR}caution_under.mp3"
                else:
                    file_path = f"{CAUTION_DIR}caution_{direction}.mp3"
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
                    play_mp3(f)

            audio_queue.task_done()
        except queue.Empty:
            continue

def play_audio(data):
    if not audio_queue.full():
        audio_queue.put(data)

# =========================================================
# 💡 초기 부팅 인트로 방송 (카메라 시작 전 최초 1회 선행 재생)
# =========================================================
print("시스템 시작 (pigpio + 콜백 방식)")
start_hello_file = f"{HELLO_DIR}start.mp3"
if os.path.exists(start_hello_file):
    play_mp3(start_hello_file)
else:
    print(f"경고: {start_hello_file} 파일이 없습니다.")

# =========================================================
# 12. 스레드 구동 시작
# =========================================================
threading.Thread(target=ultrasonic_worker, daemon=True).start()
threading.Thread(target=audio_worker, daemon=True).start()

# =========================================================
# 13. YOLO 타겟 클래스
# =========================================================
TARGET_OBJECTS = [
    'elevator', 'vending_machine', 'trash_bin', 'self_service_cafe',
    'water_dispenser', 'locker', 'door', 'obstacle', 'photo_copier',
    'person', 'lectern', 'desk', 'chair', 'signboard'
]

caution_spoken = {name: False for name in SENSORS}
last_spoken_object = None
last_speak_time = 0

# =========================================================
# 14. 메인 루프 (하단 센서 판정 로직 분리 및 고도화)
# =========================================================
try:
    while True:
        current_time = time.time()

        # 초음파 경고 처리
        for direction, pins in SENSORS.items():
            with dist_lock:
                dist = dist_data[direction]

            # 예외 데이터 필터링
            if dist == 999.0 or dist <= 0:
                continue

            # 🛠️ [조건 분리 1] 하단 낙하 센서 처리
            if 'under' in direction:
                if dist >= pins['limit']:  # 60cm 이상 멀어지면 낙하(낭떠러지) 구역으로 감지!
                    if not caution_spoken[direction]:
                        play_audio({'type': 'caution', 'direction': direction})
                        caution_spoken[direction] = True
                else:
                    # 안정적인 바닥(60cm 미만)으로 복귀 시 경고 상태 해제 마진 제공
                    if dist < (pins['limit'] - 10):
                        caution_spoken[direction] = False

            # 🛠️ [조건 분리 2] 일반 전/측방 장애물 센서 처리
            else:
                if dist < pins['limit']:   # 설정 거리보다 가까워지면 충돌 경고!
                    if not caution_spoken[direction]:
                        play_audio({'type': 'caution', 'direction': direction})
                        caution_spoken[direction] = True
                else:
                    if dist > (pins['limit'] + 10):
                        caution_spoken[direction] = False

        # YOLO 출력 읽기
        line = sys.stdin.readline()
        if not line:
            continue
        line = line.lower().strip()

        if current_time - last_speak_time > 3.0:
            for obj in TARGET_OBJECTS:
                if obj in line:
                    if "left" in line:
                        obj_dir = "left"
                    elif "right" in line:
                        obj_dir = "right"
                    else:
                        obj_dir = "front"

                    current_identity = f"{obj_dir}_{obj}"
                    if current_identity != last_spoken_object:
                        play_audio({
                            'type': 'object_alert',
                            'direction': obj_dir,
                            'class_name': obj
                        })
                        last_spoken_object = current_identity
                        last_speak_time = current_time
                    break

        time.sleep(0.03)

except KeyboardInterrupt:
    print("\n사용자 종료")
finally:
    running = False
    pi.stop()           # pigpio 정리
    pygame.mixer.quit()
    print("시스템 종료")
