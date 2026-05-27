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
# 초음파 센서 핀 설정 (최신 업데이트 핀 맵 반영)
# =========================================================
SENSORS = {
    'front': {'trig': 4, 'echo': 5},       # 전방 -> caution_front.mp3
    'left': {'trig': 6, 'echo': 27},      # 좌측면 -> caution_left.mp3
    'right': {'trig': 12, 'echo': 13},    # 우측면 -> caution_right.mp3
    'left_under': {'trig': 16, 'echo': 17},  # 좌측하부 -> caution_under.mp3 매칭
    'right_under': {'trig': 20, 'echo': 21}  # 우측하부 -> caution_under.mp3 매칭
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
# 🔊 초고속 오디오 재생 스레드
# =========================================================
def audio_worker():
    global running
    while running:
        try:
            task = audio_queue.get(timeout=0.005)

            # 최신 경보 우선 재생을 위해 대기 열 청소
            with audio_queue.mutex:
                audio_queue.queue.clear()

            task_type = task['type']
            
            # Case 1: 초음파 센서 경고 재생
            if task_type == 'caution':
                direction = task['direction']
                
                # 좌측하부(left_under)나 우측하부(right_under)는 둘 다 'under' 파일로 매칭
                if 'under' in direction:
                    file_path = f"{CAUTION_DIR}caution_under.mp3"
                else:
                    file_path = f"{CAUTION_DIR}caution_{direction}.mp3"
                
                if os.path.exists(file_path):
                    pygame.mixer.music.load(file_path)
                    pygame.mixer.music.play()
                    while pygame.mixer.music.get_busy(): 
                        time.sleep(0.01)

            # Case 2: YOLO 다중 객체 실시간 안내 문장 조립
            elif task_type == 'multi_objects':
                class_names = task['class_names']
                
                # 1. 감지된 객체 음성 연속 재생 (vending_machine.mp3 등 언더바 형태 그대로 로드)
                for cls in class_names:
                    file_obj = f"{OBJECT_DIR}{cls}.mp3"
                    if os.path.exists(file_obj):
                        pygame.mixer.music.load(file_obj)
                        pygame.mixer.music.play()
                        while pygame.mixer.music.get_busy(): 
                            time.sleep(0.01)
                
                # 2. 공통 서술어 믹싱 ("앞에" -> "있습니다")
                for f_path in [f"{SYSTEM_DIR}front.mp3", f"{SYSTEM_DIR}exist.mp3"]:
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
print("보행 보조 시스템 시작 (로컬 단어 조립 모드 - 5센서 대응)")

last_speak_time = 0
last_alert_time = 0

# 14개 핵심 클래스 리스트
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
                if current_time - last_alert_time > 0.5:  # 경고 주기 0.5초 커트
                    audio_queue.put({'type': 'caution', 'direction': direction})
                    last_alert_time = current_time
                    alert_triggered = True
                    break  # 하나라도 터지면 즉시 루프 탈출 후 재생 처리
        
        # 초음파 경고가 발생했다면 YOLO 안내는 한 템포 패스 (안전 최우선)
        if alert_triggered:
            continue

        # 2. YOLO 다중 객체 안내 기능 (3.0초 주기 유지)
        if current_time - last_speak_time > 3.0:
            detected_now = []
            
            for cls in target_classes:
                if cls in line:
                    detected_now.append(cls)

            if detected_now:
                # 중복 제거
                detected_now = list(set(detected_now))
                
                # 큐에 다중 객체 리스트 전달
                audio_queue.put({'type': 'multi_objects', 'class_names': detected_now})
                last_speak_time = current_time

except KeyboardInterrupt:
    print("\n시스템 종료 중...")

finally:
    running = False
    time.sleep(0.1)
    GPIO.cleanup()
    print("GPIO 및 시스템 정리 완료.")
