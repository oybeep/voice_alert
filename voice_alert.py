import sys
import os
import time
import threading
import queue
import RPi.GPIO as GPIO
import pygame

# =========================================================
# 초기 설정 및 오디오 믹서 초기화
# =========================================================
GPIO.setmode(GPIO.BCM)
GPIO.setwarnings(False)

# pygame 오디오 시스템 초기화
pygame.mixer.init()

# =========================================================
# 초음파 센서 핀 설정
# =========================================================
SENSORS = {
    'front': {'trig': 4, 'echo': 5},     # 전방
    'left': {'trig': 6, 'echo': 27},    # 좌측면
    'right': {'trig': 12, 'echo': 13},  # 우측면
    'under': {'trig': 16, 'echo': 17}   # 좌측하부/우측하부 통합 관리 (caution_under)
}

for name, pins in SENSORS.items():
    GPIO.setup(pins['trig'], GPIO.OUT)
    GPIO.setup(pins['echo'], GPIO.IN)

# =========================================================
# 오디오 파일 경로 설정
# =========================================================
SOUND_DIR = "/home/team-d/obstacle_detection/sounds/"
OBJECT_DIR = SOUND_DIR + "objects/"
SYSTEM_DIR = SOUND_DIR + "system/"
CAUTION_DIR = SOUND_DIR + "caution/"

# =========================================================
# 공유 데이터 및 큐 설정
# =========================================================
# 오디오 전용 큐 (병목 방지용 스레드 통신)
audio_queue = queue.Queue(maxsize=1)
dist_data = {name: 400 for name in SENSORS}
running = True

# =========================================================
# 초음파 거리 측정 함수
# =========================================================
def get_distance(trig, echo):
    GPIO.output(trig, False)
    time.sleep(0.0002)

    GPIO.output(trig, True)
    time.sleep(0.00001)
    GPIO.output(trig, False)

    start_time = time.time()
    timeout = start_time + 0.006  # 약 1m 이내 제한

    while GPIO.input(echo) == 0:
        start_time = time.time()
        if start_time > timeout:
            return 100.0

    while GPIO.input(echo) == 1:
        stop_time = time.time()
        if stop_time > timeout:
            return 100.0

    duration = stop_time - start_time
    distance = (duration * 34300) / 2
    return round(distance, 0)

# =========================================================
# 초음파 센서 모니터링 스레드
# =========================================================
def ultrasonic_thread():
    global dist_data, running
    while running:
        for name, pins in SENSORS.items():
            dist_data[name] = get_distance(pins['trig'], pins['echo'])
            time.sleep(0.002)

# =========================================================
# 🔊 초고속 오디오 재생 스레드 (핵심 최적화 파트)
# =========================================================
def audio_worker():
    global running
    while running:
        try:
            # 큐에서 작업 가져오기 (타임아웃을 짧게 주어 즉각 반응)
            task = audio_queue.get(timeout=0.005)

            # 최신 경보 우선을 위해 큐에 밀려있던 다른 알림은 즉시 청소
            with audio_queue.mutex:
                audio_queue.queue.clear()

            task_type = task['type']
            
            # Case 1: 초음파 센서 경고 재생
            if task_type == 'caution':
                direction = task['direction']
                file_path = f"{CAUTION_DIR}caution_{direction}.mp3"
                
                if os.path.exists(file_path):
                    pygame.mixer.music.load(file_path)
                    pygame.mixer.music.play()
                    while pygame.mixer.music.get_busy(): 
                        time.sleep(0.01)

            # Case 2: YOLO 객체 안내 문장 실시간 조립 재생
            elif task_type == 'object':
                class_name = task['class_name']
                
                file1 = f"{OBJECT_DIR}{clean_name}.mp3"  # 예: "desk" -> "책상이"
                file2 = f"{SYSTEM_DIR}front.mp3"         # "앞에"
                file3 = f"{SYSTEM_DIR}exist.mp3"         # "있습니다"

                # 세 개의 단어 파일을 공백 없이 연속 로드 및 재생
                for f_path in [file1, file2, file3]:
                    if os.path.exists(f_path):
                        pygame.mixer.music.load(f_path)
                        pygame.mixer.music.play()
                        while pygame.mixer.music.get_busy(): 
                            time.sleep(0.01)

            audio_queue.task_done()

        except queue.Empty:
            continue
        except Exception as e:
            print(f"오디오 재생 오류: {e}")

# =========================================================
# 스레드 구동
# =========================================================
t_sonic = threading.Thread(target=ultrasonic_thread, daemon=True)
t_audio = threading.Thread(target=audio_worker, daemon=True)

t_sonic.start()
t_audio.start()

# =========================================================
# 메인 예측 루프 (YOLO 텍스트 파이프라인 수신)
# =========================================================
print("보행 보조 시스템 시작 (로컬 단어 조립 모드)")

last_speak_time = 0
last_alert_time = 0

# 14개의 클래스 리스트
target_classes = [
    'elevator', 'vending_machine', 'trash_bin', 'self_service_cafe', 
    'water_dispenser', 'locker', 'door', 'obstacle', 'photo_copier', 
    'person', 'lectern', 'desk', 'chair', 'signboard'
]

try:
    for line in sys.stdin:
        current_time = time.time()

        # 1. 초음파 센서 근접 경고 감지 (50cm 미만)
        alert_triggered = False
        for direction, distance in dist_data.items():
            if distance < 50:
                if current_time - last_alert_time > 0.5:  # 경고 주기는 0.5초 커트
                    audio_queue.put({'type': 'caution', 'direction': direction})
                    last_alert_time = current_time
                    alert_triggered = True
                    break  # 하나의 센서라도 터지면 루프 탈출
        
        # 초음파 경고가 터졌다면 YOLO 안내는 한 템포 쉬어감 (안전 최우선)
        if alert_triggered:
            continue

        # 2. YOLO 객체 안내 기능 
        if current_time - last_speak_time > 3:  # 객체 안내는 3초 주기로 여유롭게
            detected_now = None
            
            for cls in target_classes:
                if cls in line:
                    detected_now = cls
                    break  # 가장 먼저 잡힌 핵심 객체 하나만 우선 안내

            if detected_now:
                audio_queue.put({'type': 'object', 'class_name': detected_now})
                last_speak_time = current_time

except KeyboardInterrupt:
    print("\n시스템 종료 중...")

finally:
    running = False
    time.sleep(0.1)
    GPIO.cleanup()
    print("GPIO 및 시스템 정리 완료.")
