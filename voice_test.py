import sys
import time
import threading
import queue
import os
import pygame
import RPi.GPIO as GPIO

# =========================================================
# 1. GPIO 초기 설정
# =========================================================
GPIO.setmode(GPIO.BCM)
GPIO.setwarnings(False)

# =========================================================
# 2. 초음파 센서 핀 설정 (하단 센서 임계값 70cm 설정)
# =========================================================
SENSORS = {
    'front':       {'trig': 4,  'echo': 5,  'limit': 50},
    'left':        {'trig': 6,  'echo': 27, 'limit': 30},
    'right':       {'trig': 12, 'echo': 13, 'limit': 30},
    # [주석 처리] 하단 낙하 센서 기능 비활성화
    # 'left_under':  {'trig': 16, 'echo': 17, 'limit': 70}, 
    # 'right_under': {'trig': 20, 'echo': 21, 'limit': 70}  
}

# GPIO setup
for name, pins in SENSORS.items():
    GPIO.setup(pins['trig'], GPIO.OUT)
    GPIO.setup(pins['echo'], GPIO.IN)

# =========================================================
# 3. 사운드 설정
# =========================================================
pygame.mixer.init()
VOLUME_LEVEL = 0.40

SOUND_DIR = "/home/team-d/obstacle_detection/sounds/"
OBJECT_DIR = SOUND_DIR + "objects/"
SYSTEM_DIR = SOUND_DIR + "system/"
CAUTION_DIR = SOUND_DIR + "caution/"

# =========================================================
# 4. 오디오 큐
# =========================================================
audio_queue = queue.Queue(maxsize=1)
running = True

# =========================================================
# 5. 거리 데이터 저장
# =========================================================
dist_data = {name: 999 for name in SENSORS}

# =========================================================
# 6. 초음파 거리 측정 함수
# =========================================================
def get_distance(trig, echo):
    GPIO.output(trig, False)
    time.sleep(0.0002)

    GPIO.output(trig, True)
    time.sleep(0.00001)
    GPIO.output(trig, False)

    start = time.time()
    timeout = start + 0.02  

    while GPIO.input(echo) == 0:
        start = time.time()
        if start > timeout:
            return 999

    stop = time.time()
    while GPIO.input(echo) == 1:
        stop = time.time()
        if stop > timeout:
            return 999

    duration = stop - start
    distance = (duration * 34300) / 2
    return round(distance, 1)

# =========================================================
# 7. 초음파 스레드 (잔향 마진 확보)
# =========================================================
def ultrasonic_worker():
    global dist_data
    while running:
        for name, pins in SENSORS.items():
            dist_data[name] = get_distance(pins['trig'], pins['echo'])
            time.sleep(0.04) 

# =========================================================
# 8. 오디오 재생 함수
# =========================================================
def play_mp3(path):
    if os.path.exists(path):
        pygame.mixer.music.load(path)
        pygame.mixer.music.set_volume(VOLUME_LEVEL)
        pygame.mixer.music.play()
        while pygame.mixer.music.get_busy():
            time.sleep(0.01)

# =========================================================
# 9. 오디오 스레드
# =========================================================
def audio_worker():
    while running:
        try:
            task = audio_queue.get(timeout=0.01)
            with audio_queue.mutex:
                audio_queue.queue.clear()

            task_type = task['type']

            if task_type == 'caution':
                direction = task['direction']
                # [주석 처리] 하단 음성 출력부 제거
                # if 'under' in direction:
                #     file_path = f"{CAUTION_DIR}caution_under.mp3"
                # else:
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

# 스레드 시작
threading.Thread(target=ultrasonic_worker, daemon=True).start()
threading.Thread(target=audio_worker, daemon=True).start()

# YOLO 타겟 클래스
TARGET_OBJECTS = [
    'elevator', 'vending_machine', 'trash_bin', 'self_service_cafe',
    'water_dispenser', 'locker', 'door', 'obstacle', 'photo_copier',
    'person', 'lectern', 'desk', 'chair', 'signboard'
]

# 상태 저장
caution_spoken = {name: False for name in SENSORS}
last_spoken_object = None
last_speak_time = 0

print("시스템 시작")

# =========================================================
# 14. 메인 루프 (터미널 실시간 모니터링 기능 탑재)
# =========================================================
try:
    while True:
        current_time = time.time()

        # 🖥️ 실시간 센서값 터미널 모니터링 (하단 센서 제거 버전)
        print(f"[실시간 거리] 정면: {dist_data['front']}cm | 좌: {dist_data['left']}cm | 우: {dist_data['right']}cm        ", end="\r", flush=True)

        # 초음파 경고 처리
        for direction, pins in SENSORS.items():
            val = dist_data[direction]

            if val == 999 or val <= 0:
                continue

            # 🛠️ [통삭제] 1. 하단 센서 처리 관련 조건문 커트
            # if 'under' in direction:
            #     if val >= pins['limit']:
            #         if not caution_spoken[direction]:
            #             play_audio({'type': 'caution', 'direction': direction})
            #             caution_spoken[direction] = True
            #     else:
            #         if val < (pins['limit'] - 10):
            #             caution_spoken[direction] = False

            # 🛠️ 2. 일반 전/측방 센서 처리: 임계값 미만으로 가까워질 때 경고
            # 하단 센서가 구조에서 빠졌으므로 일반 전/측방 로직만 상시 대기합니다.
            if val < pins['limit']:
                if not caution_spoken[direction]:
                    play_audio({'type': 'caution', 'direction': direction})
                    caution_spoken[direction] = True
            else:
                if val > (pins['limit'] + 10):
                    caution_spoken[direction] = False

        # YOLO 출력 읽기
        line = sys.stdin.readline()
        if not line:
            continue

        line = line.lower().strip()

        if ('x' not in line) or (':' not in line):
            continue

        # YOLO 객체 분석
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

        time.sleep(0.01)

except KeyboardInterrupt:
    print("\n사용자 종료")
finally:
    running = False
    GPIO.cleanup()
    print("시스템 종료")
