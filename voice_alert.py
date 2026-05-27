import cv2
import time
import threading
import queue
import os
import sys
import RPi.GPIO as GPIO
from ultralytics import YOLO
import pygame

# =========================================================
# 1. 초기 설정 및 오디오 믹서 초기화
# =========================================================
GPIO.setmode(GPIO.BCM)
GPIO.setwarnings(False)
pygame.mixer.init()

# 하측부 제거한 3개 센서 핀 맵 및 타이트한 거리 설정 (50cm)
SENSORS = {
    'front': {'trig': 4, 'echo': 5, 'limit': 50},
    'left':  {'trig': 6, 'echo': 27, 'limit': 50},
    'right': {'trig': 12, 'echo': 13, 'limit': 50}
}

for name, pins in SENSORS.items():
    GPIO.setup(pins['trig'], GPIO.OUT)
    GPIO.setup(pins['echo'], GPIO.IN)

# 오디오 파일 경로 설정
SOUND_DIR = "/home/team-d/obstacle_detection/sounds/"
OBJECT_DIR = SOUND_DIR + "objects/"
SYSTEM_DIR = SOUND_DIR + "system/"
CAUTION_DIR = SOUND_DIR + "caution/"

# 공유 데이터 및 오디오 큐 설정
audio_queue = queue.Queue(maxsize=1)
dist_data = {name: 400 for name in SENSORS}
running = True

# =========================================================
# 2. 초음파 거리 측정 함수 (절대 굳지 않는 타임아웃 퓨즈 탑재)
# =========================================================
def get_distance(trig, echo):
    GPIO.output(trig, False)
    time.sleep(0.0002)
    GPIO.output(trig, True)
    time.sleep(0.00001)
    GPIO.output(trig, False)

    timeout = time.time() + 0.006  # 0.006초(약 1m) 지나면 미련 없이 탈출
    pulse_start = time.time()
    pulse_end = time.time()

    while GPIO.input(echo) == 0:
        pulse_start = time.time()
        if pulse_start > timeout: return 999.0

    while GPIO.input(echo) == 1:
        pulse_end = time.time()
        if pulse_end > timeout: return 999.0

    duration = pulse_end - pulse_start
    return round((duration * 34300) / 2, 0)

def ultrasonic_thread():
    global dist_data, running
    while running:
        for name, pins in SENSORS.items():
            dist_data[name] = get_distance(pins['trig'], pins['echo'])
            time.sleep(0.005)

# =========================================================
# 3. 오디오 재생 워커 스레드 (로컬 MP3 조립 방식)
# =========================================================
def audio_worker():
    global running
    while running:
        try:
            task = audio_queue.get(timeout=0.005)
            with audio_queue.mutex:
                audio_queue.queue.clear()  # 대기열 비워서 딜레이 방지

            task_type = task['type']
            
            if task_type == 'caution':
                direction = task['direction']
                file_path = f"{CAUTION_DIR}caution_{direction}.mp3"
                if os.path.exists(file_path):
                    pygame.mixer.music.load(file_path)
                    pygame.mixer.music.play()
                    while pygame.mixer.music.get_busy(): time.sleep(0.01)

            elif task_type == 'multi_objects':
                class_names = task['class_names']
                for cls in class_names:
                    file_obj = f"{OBJECT_DIR}{cls}.mp3"
                    if os.path.exists(file_obj):
                        pygame.mixer.music.load(file_obj)
                        pygame.mixer.music.play()
                        while pygame.mixer.music.get_busy(): time.sleep(0.01)
                
                for f_path in [f"{SYSTEM_DIR}front.mp3", f"{SYSTEM_DIR}exist.mp3"]:
                    if os.path.exists(f_path):
                        pygame.mixer.music.load(f_path)
                        pygame.mixer.music.play()
                        while pygame.mixer.music.get_busy(): time.sleep(0.01)

            audio_queue.task_done()
        except queue.Empty:
            continue

# 스레드 기동
threading.Thread(target=ultrasonic_thread, daemon=True).start()
threading.Thread(target=audio_worker, daemon=True).start()

# =========================================================
# 4. 🚀 핵심: 파이썬 내부에서 YOLO 모델 및 웹캠 직접 구동
# =========================================================
# 우리가 만든 전용 가중치 파일 로드
MODEL_PATH = "/home/team-d/obstacle_detection/best_complete2_ncnn_model"
print("우리 팀 YOLO NCNN 모델 로딩 중...")
model = YOLO(MODEL_PATH, task='detect')

print("카메라를 직접 활성화합니다...")
# 라즈베리파이 5 백엔드 버그 방지용 CAP_V4L2 명시 및 버퍼 최적화
cap = cv2.VideoCapture(0, cv2.CAP_V4L2)
if not cap.isOpened():
    cap = cv2.VideoCapture(4, cv2.CAP_V4L2) # 0번 안 열리면 4번 대안 진입

cap.set(cv2.CAP_PROP_FRAME_WIDTH, 320)   # 연산 속도를 위해 해상도 다이어트
cap.set(cv2.CAP_PROP_FRAME_HEIGHT, 240)
cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)      # 카메라에 프레임이 고여서 생기는 밀림 방지

if not cap.isOpened():
    print("❌ 에러: 카메라를 열 수 없습니다. 선 연결을 확인하세요.")
    GPIO.cleanup()
    sys.exit()

print("🎯 모든 시스템이 한 장의 코드로 통합 구동됩니다!")

target_classes = [
    'elevator', 'vending_machine', 'trash_bin', 'self_service_cafe', 
    'water_dispenser', 'locker', 'door', 'obstacle', 'photo_copier', 
    'person', 'lectern', 'desk', 'chair', 'signboard'
]

last_speak_time = 0
last_alert_time = 0

# =========================================================
# 5. 메인 통합 실시간 루프
# =========================================================
try:
    while cap.isOpened():
        ret, frame = cap.read()
        if not ret: break

        current_time = time.time()

        # [A] 초음파 센서 실시간 경고 (50cm 미만)
        alert_triggered = False
        for direction, pins in SENSORS.items():
            if dist_data[direction] < pins['limit']:
                if current_time - last_alert_time > 0.5:
                    audio_queue.put({'type': 'caution', 'direction': direction})
                    last_alert_time = current_time
                    alert_triggered = True
                    break
        
        if alert_triggered:
            continue

        # [B] 파이썬 내부에서 직접 YOLO 예측 (3초 주기로 제어하여 CPU 발열/부하 원천 차단)
        if current_time - last_speak_time > 3.0:
            # imgsz=320 경량화 옵션으로 라즈베리파이 속도 극대화
            results = model.predict(frame, conf=0.7, verbose=False, imgsz=320)
            
            detected_now = []
            for result in results:
                for box in result.boxes:
                    cls_id = int(box.cls[0])
                    cls_name = model.names[cls_id]
                    if cls_name in target_classes:
                        detected_now.append(cls_name)

            if detected_now:
                detected_now = list(set(detected_now))  # 중복 제거
                audio_queue.put({'type': 'multi_objects', 'class_names': detected_now})
                last_speak_time = current_time

        time.sleep(0.01) # CPU 숨 쉴 구멍 마련

except KeyboardInterrupt:
    print("\n보행 시스템 안전 종료.")
finally:
    running = False
    cap.release()
    GPIO.cleanup()
    print("모든 하드웨어 점유권이 안전하게 해제되었습니다.")
