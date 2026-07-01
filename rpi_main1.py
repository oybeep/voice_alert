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

# ToF 센서용 I2C 라이브러리 (SCL=GPIO3, SDA=GPIO2 결선 체크)
import board
import busio
import adafruit_vl53l0x

# =========================================================
# ⚙️ 1. 카카오 디벨로퍼스 및 핵심 임계값(Threshold) 설정
# =========================================================
#  카카오톡 토큰 만료 시 재발급받아 아래 문자열만 교체하면 됩니다.
ACCESS_TOKEN = "V6vqe5l-eiLXsvP4sm8jgiWEKanHg1k_AAAAAQoNDF4AAAGe-ATlELbGP5Eb7W-4"

#  카카오톡 긴급 알림을 보낼 절대 기준 거리입니다. (단위: cm)
# 테스트 환경에 따라 너무 자주 울리면 15.0이나 20.0 등으로 조절하세요.
THRESHOLD_DISTANCE = 10.0  

# 시스템 내부 영문 방향 ID와 음성/카카오톡 출력용 한글 매핑 테이블
direction_map = {
    "left": "왼쪽", "front": "정면", "right": "오른쪽",
    "center_under": "하단 중앙", "right_under": "오른쪽 하단", 
    "left_under": "왼쪽 하단", "front_under": "정면 하단"
}

# 각 방향별 카카오톡 경고 전송 여부 플래그 (중복 전송 방지용)
alert_status = {key: False for key in direction_map.keys()}

# PC(YOLOv8) 브레인으로부터 수신한 최신 사물명을 보관하는 전역 변수
latest_detected_obj = "장애물" 

def send_kakao_alert(message):
    """
    [비동기 카카오톡 전송 함수]
    메인 루프가 네트워크 대기 시간(Timeout)으로 인해 멈추는 것을 방지하기 위해 
    별도의 백그라운드 스레드(Daemon Thread)를 생성하여 전송합니다.
    """
    def _send():
        try:
            url = "https://kapi.kakao.com/v2/api/talk/memo/default/send"
            headers = {"Authorization": f"Bearer {ACCESS_TOKEN}"}
            data = {
                "template_object": f'{{"object_type": "text", "text": "{message}", "link": {{"web_url": "https://developers.kakao.com"}}}}'
            }
            # 전송 지연으로 보행기 제어가 멈추지 않도록 타임아웃을 2초로 타이트하게 제한
            response = requests.post(url, headers=headers, data=data, timeout=2)
            if response.status_code == 200:
                print(f"\n[알림 전송 완료] {message}", flush=True)
            else:
                print(f"\n[카카오톡 에러] 코드 {response.status_code} (토큰 만료 여부 확인 필요)", flush=True)
        except Exception as e:
            print(f"\n[전송 실패] 네트워크 연결 상태를 확인하세요: {e}", flush=True)

    threading.Thread(target=_send, daemon=True).start()

# =========================================================
# 2. RPi.GPIO 및 하드웨어 센서 초기화 (초음파 3개 + ToF 1개)
# =========================================================
GPIO.setmode(GPIO.BCM)      # 라즈베리 파이 핀 배열 기준을 BCM(GPIO 번호)으로 설정
GPIO.setwarnings(False)

# 🟢 상단 초음파 센서 핀 맵 및 개별 경고 거리 설정
#  센서 점퍼선 결선이 바뀌면 아래 'trig'와 'echo' 번호를 하드웨어와 맞추면 됩니다.
# 'limit'는 스피커로 "조심하세요" 경고 멘트를 내보낼 기준 거리(cm)입니다.
SENSORS_US = {
    'front': {'trig': 4,  'echo': 5,  'limit': 50},  # 정면 초음파: 50cm 이내 접근 시 경고
    'left':  {'trig': 6,  'echo': 27, 'limit': 30},  # 좌측 초음파: 30cm 이내 접근 시 경고
    'right': {'trig': 12, 'echo': 13, 'limit': 30}   # 우측 초음파: 30cm 이내 접근 시 경고
}

# 딕셔너리에 정의된 핀 설정대로 GPIO 입출력 모드 일괄 초기화
for name, pins in SENSORS_US.items():
    GPIO.setup(pins['trig'], GPIO.OUT)
    GPIO.setup(pins['echo'], GPIO.IN, pull_up_down=GPIO.PUD_DOWN) # 플로팅 방지 풀다운
    GPIO.output(pins['trig'], False)

# 🔵 하단 ToF 센서 (바닥 턱 및 계단 감지용 I2C 통신)
# 'limit'는 보행기가 지면으로부터 떠 있거나 계단을 만났을 때 측정되는 기준 거리(cm)입니다.
SENSORS_TOF = {
    'center_under': {'limit': 60}
}

# I2C 버스를 초기화하고 VL53L0X ToF 센서 객체 연결 시도
try:
    i2c = busio.I2C(board.SCL, board.SDA)  # 물리 핀 기준 3번(SDA), 5번(SCL) 고정
    tof_sensor = adafruit_vl53l0x.VL53L0X(i2c)
    print("[하드웨어] ToF 센서 연결 성공 (I2C 통신 정상)")
except Exception as e:
    print(f"[하드웨어 에러] ToF 센서 하드웨어 결선 또는 I2C 활성화 상태를 확인하세요: {e}")
    tof_sensor = None

# =========================================================
# 3. 오디오 시스템, 데이터 큐(Queue) 및 미디언 필터 버퍼 설정
# =========================================================
pygame.mixer.init()
VOLUME_LEVEL = 0.40 # 🔊 스피커 볼륨 설정 (0.0 ~ 1.0) 시끄러우면 낮추세요.

# 각 상황별 MP3 파일이 저장된 라즈베리 파이 내부 절대 경로
SOUND_DIR = "/home/team-d/obstacle_detection/sounds/"
OBJECT_DIR = SOUND_DIR + "objects/"
SYSTEM_DIR = SOUND_DIR + "system/"
CAUTION_DIR = SOUND_DIR + "caution/"
HELLO_DIR = SOUND_DIR + "hello/"

# 멀티스레드 간 동기화를 위한 안전한 큐(Queue) 메모리 공간
audio_queue = queue.Queue(maxsize=1)    # 오디오 출력 스레드용 큐 (대기행렬 1개 제한으로 딜레이 방지)
yolo_queue = queue.Queue(maxsize=10)   # PC로부터 수신한 YOLO 데이터 보관 큐
running = True                         # 프로그램 전체 구동 제어 플래그

# [미디언 필터 핵심] 튀는 노이즈 값을 잡기 위해 각 센서별로 최근 5개의 측정 데이터 보관
all_sensors = list(direction_map.keys())
dist_data = {name: 0.0 for name in all_sensors}              # 최종 필터링된 거리가 저장될 공간
distance_buffer = {name: deque(maxlen=5) for name in all_sensors} # 실시간 원형 큐 버퍼 (크기 5)
dist_lock = threading.Lock()                                 # 멀티스레드 자원 충돌 방지용 락(Lock)

# 동일 구역에서 경고 음성이 쉬지 않고 무한 반복 출력되는 현상을 막기 위한 플래그
caution_spoken = {name: False for name in all_sensors}

# =========================================================
# 4-1. 초음파 센서 워커 (백그라운드 실시간 거리 연산)
# =========================================================
def measure_ultrasonic(trig, echo):
    """ 초음파 센서에 10us 트리거 신호를 보내 에코 핀의 High 유지 시간을 cm 거리로 환산 """
    GPIO.output(trig, True)
    time.sleep(0.00001)
    GPIO.output(trig, False)
    
    timeout_start = time.time()
    pulse_start = timeout_start
    pulse_end = timeout_start

    # 초음파 신호가 발사되어 에코 핀이 High가 될 때까지 대기 (최대 40ms 타임아웃)
    while GPIO.input(echo) == 0:
        pulse_start = time.time()
        if pulse_start - timeout_start > 0.04: return 999.0
        
    # 에코 핀이 Low로 떨어질 때까지 대기하여 왕복 시간 측정 (최대 40ms 타임아웃)
    while GPIO.input(echo) == 1:
        pulse_end = time.time()
        if pulse_end - pulse_start > 0.04: return 999.0

    # 편도 거리 변환 공식 적용 (음속 343m/s -> 17150cm/s 곱하기)
    return round((pulse_end - pulse_start) * 17150, 1)

def ultrasonic_worker():
    """ 3개의 초음파 센서를 무한 루프로 순회하며 계측 후 미디언 필터를 적용하는 스레드 함수 """
    while running:
        for name, pins in SENSORS_US.items():
            if not running: break
            distance = measure_ultrasonic(pins['trig'], pins['echo'])
            
            # 측정값을 원형 버퍼에 추가 (자동으로 오래된 데이터는 밀려남)
            distance_buffer[name].append(distance)
            
            # [미디언 필터 계산] 버퍼 내 5개 데이터 중 오름차순 기준 정중앙 값(Median)을 대표값으로 채택
            with dist_lock:
                dist_data[name] = round(statistics.median(distance_buffer[name]), 1)
            time.sleep(0.02)  # 센서 간 신호 간섭(Cross-talk)을 방지하기 위한 미세 딜레이
        time.sleep(0.01)

# =========================================================
# 4-2. ToF 센서 워커 (백그라운드 실시간 바닥 고도 연산)
# =========================================================
def tof_worker():
    """ 하단 ToF 레이저 센서의 값을 읽어 cm로 변환 및 필터링하는 스레드 함수 """
    while running:
        if not running: break
        if tof_sensor is not None:
            try:
                # 센서 기본 반환값은 mm이므로 10을 나누어 cm로 변환
                dist_cm = tof_sensor.range / 10.0
                # 측정 한계 수치를 넘어가거나 음수가 찍히면 예외 에러값(999.0)으로 처리
                if dist_cm > 120.0 or dist_cm <= 0: dist_cm = 999.0
            except Exception: 
                dist_cm = 999.0
        else:
            dist_cm = 999.0

        # ToF 센서 데이터도 튀는 현상을 막기 위해 초음파와 동일하게 미디언 필터 적용
        distance_buffer['center_under'].append(dist_cm)
        with dist_lock:
            dist_data['center_under'] = round(statistics.median(distance_buffer['center_under']), 1)
        time.sleep(0.03)

# =========================================================
# 5. 영상 송신(스트리밍) + 텍스트(YOLO 결과) 수신 통합 네트워크 서버
# =========================================================
def network_server_worker():
    """
    [PC 도킹 통신 가이드]
    라즈베리 파이가 서버가 되어 2개의 소켓 포트를 개방합니다.
    - 9998 포트: 보행기 전방 카메라 영상을 PC 브레인으로 JPEG 압축 스트리밍 송신
    - 9999 포트: PC에서 YOLOv8로 연산한 사물 및 방향 문자열 데이터 수신
    """
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
            # PC 클라이언트의 연결 승인 (Blocking 모드, 연결 전까지 대기)
            v_conn, v_addr = video_server.accept()
            t_conn, t_addr = text_server.accept()
            print(f"[RPi] PC 브레인 도킹 완료! (영상/텍스트 세션 연결)", flush=True)

            # OpenCV 카메라 세팅 (라즈베리파이 캠 혹은 USB 웹캠 0번 부팅)
            cam = cv2.VideoCapture(0)
            cam.set(cv2.CAP_PROP_FRAME_WIDTH, 320)  # 연산 대역폭 확보를 위해 해상도 최적화
            cam.set(cv2.CAP_PROP_FRAME_HEIGHT, 240)

            def text_receiver(conn):
                """ PC가 보낸 문자열(형식: 'front_chair', 'left_desk')을 수신하여 큐에 삽입 """
                while running:
                    try:
                        data = conn.recv(1024)
                        if not data: break
                        line = data.decode('utf-8').strip().lower()
                        if line and not yolo_queue.full():
                            yolo_queue.put(line)
                    except: break

            # 텍스트 수신부만 별도의 서브 스레드로 분리 구동
            t_thread = threading.Thread(target=text_receiver, args=(t_conn,), daemon=True)
            t_thread.start()

            # 영상 프레임을 실시간으로 압축 및 직렬화하여 PC에 무선 스트리밍 송신
            while cam.isOpened() and running:
                ret, frame = cam.read()
                if not ret: break
                
                # 전송 대역폭 경감을 위해 화질(Quality)을 45%로 낮추어 JPEG 인코딩
                result, imgencode = cv2.imencode('.jpg', frame, [int(cv2.IMWRITE_JPEG_QUALITY), 45])
                data = pickle.dumps(imgencode, 0)
                size = len(data)
                
                # 패킷 크기를 상단 헤더에 struct 포맷(Big-Endian Long)으로 담아 데이터 전송
                v_conn.sendall(struct.pack(">L", size) + data)
                time.sleep(0.03) # 프레임 속도 조절 (약 30 FPS 내외 제한)

            # 세션 종료 시 안전하게 리소스 해제 후 무한루프를 돌며 PC 재접속 대기
            cam.release()
            v_conn.close()
            t_conn.close()
            print("[RPi] PC와 무선 연결 해제. 재대기합니다.", flush=True)
        except Exception as e:
            if running: print(f"[RPi 네트워크 에러] {e}", flush=True)
            time.sleep(1)

# =========================================================
# 6. 오디오 재생 및 안내 음성 관리 스레드
# =========================================================
def play_mp3(path):
    """ 지정된 경로의 MP3 사운드 파일 재생 함수 """
    if os.path.exists(path):
        pygame.mixer.music.load(path)
        pygame.mixer.music.set_volume(VOLUME_LEVEL)
        pygame.mixer.music.play()
        while pygame.mixer.music.get_busy(): time.sleep(0.01)

def audio_worker():
    """ 
    [오디오 동기화 가이드]
    여러 경고 음성이 겹쳐서 기괴하게 들리는 중첩 현상을 방지하기 위해 
    하나의 오디오 전용 스레드가 큐(Queue)를 소모하며 순차적으로 한 개씩만 재생하게 제어합니다.
    """
    global audio_queue
    while running:
        try:
            # 큐에 오디오 작업이 들어올 때까지 대기
            task = audio_queue.get(timeout=0.01)
            
            # 새롭고 더 급한 긴급 경고가 오면 큐 내부에 쌓여있던 옛날 오디오 작업은 즉시 폐기(Clear)
            while not audio_queue.empty():
                try:
                    audio_queue.get_nowait()
                    audio_queue.task_done()
                except queue.Empty: break

            task_type = task['type']
            
            # 현재 다른 안내 멘트가 재생 중이라면 즉시 중단(Stop)하고 새 경고음 우선 처리
            if pygame.mixer.music.get_busy(): pygame.mixer.music.stop()

            # 상황 [A] 센서 충돌 및 낙상 위험음 재생
            if task_type == 'caution':
                direction = task['direction']
                file_path = f"{CAUTION_DIR}caution_under.mp3" if 'under' in direction else f"{CAUTION_DIR}caution_{direction}.mp3"
                play_mp3(file_path)

            # 상황 [B] PC(YOLOv8) 사물 인지 매칭 안내음 콤보 재생 ("정면" + "의자" + "가 있습니다")
            elif task_type == 'object_alert':
                direction = task['direction']
                cls_name = task['class_name']
                dir_file = f"{SYSTEM_DIR}{direction}.mp3"   # 방향 파일 (예: front.mp3)
                exist_file = f"{SYSTEM_DIR}exist.mp3"       # "~가 있습니다" 고정 멘트 파일
                
                # 언더바 유무 대응 예외 처리 파일 매칭
                file1 = f"{OBJECT_DIR}{cls_name}.mp3"
                file2 = f"{OBJECT_DIR}{cls_name.replace('_', '')}.mp3"
                obj_file = file1 if os.path.exists(file1) else file2

                # 3개의 파일을 순서대로 콤보 재생하되, 재생 중에 다른 급한 사운드 큐가 들어오면 즉시 탈출
                for f in [dir_file, obj_file, exist_file]:
                    if not audio_queue.empty(): break
                    play_mp3(f)

            audio_queue.task_done()
        except queue.Empty: continue

def play_audio(data):
    """ 오디오 전용 큐에 안전하게 태스크를 집어넣는 진입 함수 """
    if not audio_queue.full(): audio_queue.put(data)

# =========================================================
# ⚙️ 시스템 부팅 및 통합 스레드 구동 시작
# =========================================================
print("라즈베리 파이 무선 스트리밍 통합 서버 가동...", flush=True)
start_hello_file = f"{HELLO_DIR}start.mp3"
if os.path.exists(start_hello_file): play_mp3(start_hello_file)

# 서비스 가동 시작 카카오톡 메시지 전송 테스트
send_kakao_alert("함께 걷는 눈: 보행기 안전 모니터링을 시작합니다.")

# 4개의 핵심 독립 스레드를 가동하여 병렬 멀티태스킹 처리
threading.Thread(target=ultrasonic_worker, daemon=True).start()
threading.Thread(target=tof_worker, daemon=True).start()
threading.Thread(target=audio_worker, daemon=True).start()
threading.Thread(target=network_server_worker, daemon=True).start()

# =========================================================
# 7. 메인 제어 루프 (통합 센서 데이터 감시 및 카카오톡 API 동기화)
# =========================================================
ALL_SENSORS_COMBINED = {**SENSORS_US, **SENSORS_TOF} # 초음파와 ToF 센서 리스트 병합

try:
    print("모니터링 시작...")
    while True:
        # 터미널 창 한 줄에 실시간 정제 필터링 거리를 예쁘게 덮어쓰기 형태로 출력 (\r 적용)
        print(f"[LIVE] 정면:{dist_data['front']}cm | 좌:{dist_data['left']}cm | 우:{dist_data['right']}cm | 하단:{dist_data['center_under']}cm    ", end="\r", flush=True)

        # -------------------------------------------------
        # 7-1. PC(YOLOv8) 비동기 수신 데이터 파싱 및 실시간 변수 갱신
        # -------------------------------------------------
        try: line = yolo_queue.get_nowait()
        except queue.Empty: line = ""

        if line:
            parts = line.split('_')
            if len(parts) >= 2:
                obj_dir = parts[0]          # 방향 ID 추출 (예: 'front')
                obj_cls = "_".join(parts[1:]) # 사물 클래스명 추출 (예: 'chair')
                
                # 💡 핵심 연동 변수: 카카오톡 긴급 알림 메시지에 심어줄 사물 이름을 실시간 갱신합니다.
                latest_detected_obj = obj_cls 
                
                # 스피커 음성 조합 안내 시작 지시
                play_audio({'type': 'object_alert', 'direction': obj_dir, 'class_name': obj_cls})
                
                # 사물 검출은 카카오톡 폭탄 방지를 위해 터미널에 [Log]로만 찍히게 처리
                obj_dir_kr = direction_map.get(obj_dir, obj_dir)
                print(f"\n[Log] 인식된 사물: {obj_cls} | 방향: {obj_dir_kr} (안내 음성 출력)", flush=True)

        # -------------------------------------------------
        # 7-2. 통합 센서 기반 장애물 거리 연산 및 위험 알림 제어
        # -------------------------------------------------
        for direction, pins in ALL_SENSORS_COMBINED.items():
            # 공유 자원 딕셔너리에서 다른 스레드의 쓰기 동작 간섭 없이 안전하게 거리값 획득
            with dist_lock: dist = dist_data[direction]

            if dist <= 0: continue # 센서 에러 초기화 유효값 패스
            direction_kr = direction_map.get(direction, direction)

            # [A] 기본 하드웨어 센서 한계값(limit) 내부 진입 시 스피커 사운드 경고음 발생 구역
            if 'under' in direction:
                # 하단 센서(ToF): 바닥 거리가 limit보다 확 멀어지거나(계단) 999.0일 때 낙상 경고
                if dist >= pins['limit'] or dist == 999.0:
                    if not caution_spoken[direction]:
                        play_audio({'type': 'caution', 'direction': direction})
                        caution_spoken[direction] = True
                else:
                    # 안정권 안으로 다시 들어오면 음성 플래그 리셋 (히스테리시스 오차 3cm 반영)
                    if dist < (pins['limit'] - 3):
                        caution_spoken[direction] = False
            else:
                # 상단 센서(초음파): 장애물이 limit 수치보다 안쪽으로 다가오면 충돌 주의 경고음 발생
                if dist != 999.0 and dist < pins['limit']:
                    if not caution_spoken[direction]:
                        play_audio({'type': 'caution', 'direction': direction})
                        caution_spoken[direction] = True
                else:
                    # 센서 값의 경계면에서 음성이 덜덜덜 떨리며 연발하는 것을 막기 위해 안전 마진 +10cm 부여
                    if dist > (pins['limit'] + 10) or dist == 999.0:
                        caution_spoken[direction] = False

            # [B] 카카오톡 실시간 연동 조건문: 10cm 이하 최접근 긴급 위험 구역
            # 실제 주행 중 10cm가 너무 가깝다면 상단의 THRESHOLD_DISTANCE 변수를 키우세요.
            if dist <= THRESHOLD_DISTANCE:
                # 아직 이 방향에 대해 카카오톡 경고를 발송하지 않은 상태(False)라면 첫 1회 즉각 발송
                if not alert_status.get(direction, False):
                    msg = f"🚨 긴급 경고: 보행기 {direction_kr} 쪽에 장애물 [{latest_detected_obj}]이(가) 접근했습니다! (거리: {dist:.1f}cm)"
                    send_kakao_alert(msg)
                    alert_status[direction] = True # 발송 완료 플래그(True) 전환하여 무한 카톡 전송 차단
            
            # 위험 상황이 완전히 해제되어 다시 안전거리(10cm 초과)가 확보된 경우
            else:
                if alert_status.get(direction, False):
                    print(f"\n[안전] {direction_kr} 방향의 위험이 해제되었습니다.", flush=True)
                    alert_status[direction] = False # 상태 플래그를 False로 돌려 다음 위험 상황에 대비

        time.sleep(0.1)  # 메인 CPU 가열 및 리소스 과소비를 막기 위한 주기 컨트롤러

except KeyboardInterrupt:
    print("\n[시스템] 사용자에 의해 Ctrl+C 종료 명령이 감지되었습니다.", flush=True)
finally:
    running = False
    send_kakao_alert("함께 걷는 눈: 보행기 운행을 종료합니다.")
    GPIO.cleanup()         # 라즈베리 파이 GPIO 사용 핀 초기 상태 복원 (필수)
    pygame.mixer.quit()   # 사운드 믹서 인스턴스 소멸
