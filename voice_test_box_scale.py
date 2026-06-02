import sys
import time
import threading
import queue
import os
import re
import pygame
import RPi.GPIO as GPIO

# =========================================================
# 1. GPIO 초기 설정
# =========================================================
GPIO.setmode(GPIO.BCM)
GPIO.setwarnings(False)

# =========================================================
# 2. 초음파 센서 핀 설정 (하단 센서 주석 유지)
# =========================================================
SENSORS = {
    'front':       {'trig': 4,  'echo': 5,  'limit': 50},
    'left':        {'trig': 6,  'echo': 27, 'limit': 30},
    'right':       {'trig': 12, 'echo': 13, 'limit': 30},
}

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
        if start > timeout: return 999

    stop = time.time()
    while GPIO.input(echo) == 1:
        stop = time.time()
        if stop > timeout: return 999

    duration = stop - start
    return round((duration * 34300) / 2, 1)

def ultrasonic_worker():
    global dist_data
    while running:
        for name, pins in SENSORS.items():
            dist_data[name] = get_distance(pins['trig'], pins['echo'])
            time.sleep(0.04) 

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

threading.Thread(target=ultrasonic_worker, daemon=True).start()
threading.Thread(target=audio_worker, daemon=True).start()

# 🚨 [수정] 오작동을 유발하는 'obstacle' 검출 항목을 리스트에서 완전히 제거했습니다.
TARGET_OBJECTS = [
    'elevator', 'vending_machine', 'trash_bin', 'self_service_cafe',
    'water_dispenser', 'locker', 'door', 'photo_copier',
    'person', 'lectern', 'desk', 'chair', 'signboard'
]

caution_spoken = {name: False for name in SENSORS}
last_spoken_object = None
last_speak_time = 0

print("시스템 시작")

# =========================================================
# 14. 메인 루프 
# =========================================================
try:
    while True:
        current_time = time.time()

        # 실시간 센서값 터미널 모니터링
        print(f"[실시간 거리] 정면: {dist_data['front']}cm | 좌: {dist_data['left']}cm | 우: {dist_data['right']}cm        ", end="\r", flush=True)

        # 초음파 경고 처리
        for direction, pins in SENSORS.items():
            val = dist_data[direction]
            if val == 999 or val <= 0: continue

            if val < pins['limit']:
                if not caution_spoken[direction]:
                    play_audio({'type': 'caution', 'direction': direction})
                    caution_spoken[direction] = True
            else:
                if val > (pins['limit'] + 10):
                    caution_spoken[direction] = False

        # YOLO 출력 읽기
        line = sys.stdin.readline()
        if not line: continue
        line = line.lower().strip()

        # =========================================================
        # 🔍 터미널 텍스트에서 객체와 박스 크기 파싱 (안전 가드 강화)
        # =========================================================
        detected_candidates = []

        for obj in TARGET_OBJECTS:
            if obj in line:
                # 정규식으로 해당 오브젝트 뒤에 나오는 숫자(크기) 추출 (예: person: 320x240)
                match = re.search(rf"{obj}\s*:\s*(\d+)\s*x\s*(\d+)", line)
                
                if match:
                    width = int(match.group(1))
                    height = int(match.group(2))
                    area = width * height  # 박스 면적 계산
                    
                    # 방향 판단
                    if "left" in line: obj_dir = "left"
                    elif "right" in line: obj_dir = "right"
                    else: obj_dir = "front"

                    detected_candidates.append({'name': obj, 'dir': obj_dir, 'area': area})

        # 감지된 유효 객체가 있고, 말할 주기(3초)가 되었다면
        if detected_candidates and (current_time - last_speak_time > 3.0):
            # ⭐ 실제 크기(area) 데이터가 확보된 대상들만 크기순으로 내림차순 정렬!
            detected_candidates.sort(key=lambda x: x['area'], reverse=True)
            
            # 가장 크고 가까운 객체 1등 선택
            best_target = detected_candidates[0]
            
            current_identity = f"{best_target['dir']}_{best_target['name']}"

            if current_identity != last_spoken_object:
                play_audio({
                    'type': 'object_alert',
                    'direction': best_target['dir'],
                    'class_name': best_target['name']
                })
                last_spoken_object = current_identity
                last_speak_time = current_time

        time.sleep(0.01)

except KeyboardInterrupt:
    print("\n사용자 종료")
finally:
    running = False
    GPIO.cleanup()
    print("시스템 종료")
