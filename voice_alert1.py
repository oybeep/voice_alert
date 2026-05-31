import sys
import time
import threading
import queue
import os
import pygame
import RPi.GPIO as GPIO
import statistics  # 노이즈 필터링용 중앙값 계산 모듈

# =========================================================
# 1. GPIO 초기 설정
# =========================================================
GPIO.setmode(GPIO.BCM)
GPIO.setwarnings(False)

# =========================================================
# 2. 초음파 센서 핀 설정
# =========================================================
SENSORS = {
    'front':       {'trig': 4,  'echo': 5,  'limit': 50},
    'left':        {'trig': 6,  'echo': 27, 'limit': 30},
    'right':       {'trig': 12, 'echo': 13, 'limit': 30},
    'left_under':  {'trig': 16, 'echo': 17, 'limit': 100}, # 하측 낙하 감지 기준 (100cm)
    'right_under': {'trig': 20, 'echo': 21, 'limit': 100}  # 하측 낙하 감지 기준 (100cm)
}

# GPIO setup
for name, pins in SENSORS.items():
    GPIO.setup(pins['trig'], GPIO.OUT)
    GPIO.setup(pins['echo'], GPIO.IN)

# =========================================================
# 3. 사운드 설정
# =========================================================
pygame.mixer.init()

VOLUME_LEVEL = 0.40  # 실전 주행용 볼륨

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
dist_data = {
    name: 999 for name in SENSORS
}

# =========================================================
# 6. 초음파 거리 측정 함수 (단일 측정)
# =========================================================
def get_distance(trig, echo):
    GPIO.output(trig, False)
    time.sleep(0.0002)

    GPIO.output(trig, True)
    time.sleep(0.00001)

    GPIO.output(trig, False)

    start = time.time()
    timeout = start + 0.03

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
# 6-1. 초음파 거리 안정화 함수 (노이즈 필터 적용)
# =========================================================
def get_stable_distance(trig, echo, samples=5):
    distances = []
    for _ in range(samples):
        dist = get_distance(trig, echo)
        if dist != 999 and dist > 0:
            distances.append(dist)
        time.sleep(0.01)

    if not distances:
        return 999

    return round(statistics.median(distances), 1)

# =========================================================
# 7. 초음파 스레드 (안정화 필터 적용 및 센서 간 간섭 최소화)
# =========================================================
def ultrasonic_worker():
    global dist_data
    while running:
        for name, pins in SENSORS.items():
            dist_data[name] = get_stable_distance(
                pins['trig'],
                pins['echo'],
                samples=5
            )
            # 센서 간 잔향 간섭(Cross-talk)을 예방하기 위해 딜레이를 0.05초로 유지
            time.sleep(0.05)

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
# 9. 오디오 스레드 (하단 좌/우 음성 완전 분리 가드 포함)
# =========================================================
def audio_worker():
    while running:
        try:
            task = audio_queue.get(timeout=0.01)

            with audio_queue.mutex:
                audio_queue.queue.clear()

            task_type = task['type']

            # =====================================
            # 주의 경고
            # =====================================
            if task_type == 'caution':
                direction = task['direction']
                
                if direction not in SENSORS:
                    audio_queue.task_done()
                    continue

                # 개별 파일 매핑 (caution_left_under.mp3 / caution_right_under.mp3)
                file_path = f"{CAUTION_DIR}caution_{direction}.mp3"
                
                # 🔄 만약 스피커 하드웨어 배선이 반대라면 아래 2줄 주석(#)을 풀어 소프트웨어로 스왑하세요.
                # if direction == 'left_under': file_path = f"{CAUTION_DIR}caution_right_under.mp3"
                # if direction == 'right_under': file_path = f"{CAUTION_DIR}caution_left_under.mp3"

                play_mp3(file_path)

            # =====================================
            # 객체 안내
            # =====================================
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

# =========================================================
# 10. 오디오 큐 함수
# =========================================================
def play_audio(data):
    if not audio_queue.full():
        audio_queue.put(data)

# =========================================================
# 11. 스레드 시작
# =========================================================
threading.Thread(target=ultrasonic_worker, daemon=True).start()
threading.Thread(target=audio_worker, daemon=True).start()

# =========================================================
# 12. YOLO 타겟 클래스
# =========================================================
TARGET_OBJECTS = [
    'elevator', 'vending_machine', 'trash_bin', 'self_service_cafe',
    'water_dispenser', 'locker', 'door', 'obstacle', 'photo_copier',
    'person', 'lectern', 'desk', 'chair', 'signboard'
]

# =========================================================
# 13. 상태 저장 (★ 하단 센서 값 튐 방지용 연속 카운터 추가)
# =========================================================
caution_spoken = {
    name: False for name in SENSORS
}
under_trigger_count = {
    name: 0 for name in SENSORS  # 💡 하단 노이즈 제거용 연속 카운터 변수
}

last_spoken_object = None
last_speak_time = 0

print("시스템 시작")

# =========================================================
# 14. 메인 루프 (초음파 낙하 카운터 필터 + YOLO 유령 음성 차단 가드 통합)
# =========================================================
try:
    while True:
        current_time = time.time()

        # =====================================
        # 초음파 경고 처리 (낙하 및 장애물 분리 판정)
        # =====================================
        alert_triggered = False

        for direction, pins in SENSORS.items():
            if direction not in dist_data:
                continue

            current_dist = dist_data[direction]

            # 측정 에러 및 타임아웃 예외 스킵
            if current_dist == 999 or current_dist <= 0:
                continue

            # 🛠️ 1. 하단 센서 처리 (계단/낙하 감지: 순간 스파이크 노이즈 제거)
            if 'under' in direction:
                if current_dist >= pins['limit']:  # 100cm 이상 낭떠러지가 감지되었을 때
                    under_trigger_count[direction] += 1  # 카운트 1 누적
                    
                    # 💡 순간 연산 밀림으로 한 번(56 -> 90) 튄 값은 무시하고, 연속 2번 이상 감지 시 실제 낙하로 판단
                    if under_trigger_count[direction] >= 2:
                        if not caution_spoken[direction]:
                            play_audio({
                                'type': 'caution',
                                'direction': direction
                            })
                            caution_spoken[direction] = True
                        alert_triggered = True
                else:
                    # 정상 범위로 복귀하면 카운터 즉시 리셋 및 상태 해제
                    under_trigger_count[direction] = 0
                    if current_dist < (pins['limit'] - 10):
                        caution_spoken[direction] = False

            # 🛠️ 2. 일반 센서 처리 (전/측방 장애물 감지: 가까워지면 뜸)
            else:
                if current_dist < pins['limit']:
                    if not caution_spoken[direction]:
                        play_audio({
                            'type': 'caution',
                            'direction': direction
                        })
                        caution_spoken[direction] = True
                    alert_triggered = True
                else:
                    # 장애물에서 떨어지면 경고 상태 해제
                    if current_dist > (pins['limit'] + 10):
                        caution_spoken[direction] = False

        # =====================================
        # YOLO 출력 읽기
        # =====================================
        line = sys.stdin.readline()
        if not line:
            continue

        line = line.lower().strip()

        # 💡 [가드 1] 부팅 로그 무시 가드
        if ('x' not in line) or (':' not in line):
            continue

        # =====================================
        # YOLO 객체 분석
        # =====================================
        if current_time - last_speak_time > 3.0:
            for obj in TARGET_OBJECTS:
                if obj in line:
                    
                    # 💡 [가드 2] 엄격한 방향 추정 및 예외 처리
                    if "left" in line:
                        obj_dir = "left"
                    elif "right" in line:
                        obj_dir = "right"
                    elif "front" in line or "0:" in line:
                        obj_dir = "front"
                    else:
                        continue

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
    GPIO.cleanup()
    print("시스템 종료")
