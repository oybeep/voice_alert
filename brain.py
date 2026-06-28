import sys
import time
import threading
import queue
import os
import pygame
import socket
import cv2
import pickle
import struct
import numpy as np
from collections import deque
import statistics
from ultralytics import YOLO

# =========================================================
# ⚙️ 1. 하드웨어 네트워크 주소 및 탐지 대상(Class) 설정
# =========================================================
# [팀원 가이드] 라즈베리 파이와 PC가 같은 와이파이(공유기 또는 핫스팟)에 연결되어야 합니다!
# 라즈베리 파이 터미널에서 'hostname -I'를 쳐서 나오는 IP 주소를 아래에 정확히 적어주세요.
RPI_IP = "172.20.10.3" 
V_PORT = 9998           # 영상 스트리밍 수신용 포트 (라즈베리 파이와 일치해야 함)
T_PORT = 9999           # YOLO 연산 결과 송신용 포트 (라즈베리 파이와 일치해야 함)

# CVAT 레이블링 및 가중치(Weights) 학습에 사용된 14개의 핵심 타겟 사물 리스트
# [팀원 가이드] 모델이 특정 사물을 무시하게 만들고 싶다면 이 리스트에서 제외하면 됩니다.
TARGET_OBJECTS = [
    'elevator', 'vending_machine', 'trash_bin', 'self_service_cafe',
    'water_dispenser', 'locker', 'door', 'cabinet', 'photo_copier',
    'person', 'lectern', 'desk', 'chair', 'signboard'
]

# =========================================================
# 🔌 2. 라즈베리 파이 서버에 쌍방향 소켓 도킹 시도
# =========================================================
print(f"[PC] 라즈베리 파이 무선 스트리밍 세션 연결 중... (IP: {RPI_IP})")
v_client = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
t_client = socket.socket(socket.AF_INET, socket.SOCK_STREAM)

try:
    # 라즈베리 파이가 서버 역할을 하므로, 파이의 코드가 먼저 실행되어 있어야 이 코드가 성공합니다.
    v_client.connect((RPI_IP, V_PORT))
    t_client.connect((RPI_IP, T_PORT))
    print("[PC] 라즈베리 파이와 무선 도킹 성공! 데이터 세션이 활성화되었습니다.")
except Exception as e:
    print(f"[PC 에러] 무선 연결 실패: {e}")
    print("[팁] 라즈베리 파이 코드가 켜져 있는지, IP 주소가 맞는지, 같은 와이파이인지 확인하세요.")
    exit(1)

# =========================================================
# 🧠 3. YOLOv8 인공지능 가중치 파일(Weights) 로드
# =========================================================
print("[PC] YOLOv8 모델 로딩 중...")
# [팀원 가이드] 파일 경로가 틀리거나 파일명이 바뀌면 이 부분을 수정하세요.
# 파일은 반드시 이 파이썬 스크립트와 '같은 폴더'에 있어야 찾을 수 있습니다.
model = YOLO("best_final.pt")

# =========================================================
# 💡 4. [딜레이 제어] 영상 수신 전용 백그라운드 스레드 구현
# =========================================================
# [구조 설명] 영상 수신과 인공지능(YOLO) 연산을 한 루프에서 처리하면 렉(병목 현상)이 생깁니다.
# 따라서 영상만 미친 듯이 빨리 받아오는 전용 스레드를 파서 'latest_frame'에 실시간으로 덮어씁니다.
latest_frame = None
is_running = True

def video_receive_thread():
    """ 라즈베리 파이에서 전송되는 JPEG 압축 영상 바이트를 실시간으로 해제하는 스레드 함수 """
    global latest_frame, is_running
    data = b""
    payload_size = struct.calcsize(">L") # 헤더 크기 계산 (Big-Endian Long 포맷)
    
    try:
        while is_running:
            # 1. 이미지 크기(Header) 추출: 데이터 패킷의 헤더(4바이트)를 먼저 채움
            while len(data) < payload_size:
                packet = v_client.recv(4096)
                if not packet: return
                data += packet
            
            packed_msg_size = data[:payload_size]
            data = data[payload_size:]
            msg_size = struct.unpack(">L", packed_msg_size)[0] # 실제 전송될 이미지 바이트 크기 분리
            
            # 2. 이미지 데이터(Payload) 추출: 앞서 계산한 크기만큼 실제 픽셀 바이트를 다 받을 때까지 수신
            while len(data) < msg_size:
                packet = v_client.recv(4096)
                if not packet: return
                data += packet
                
            frame_data = data[:msg_size]
            data = data[msg_size:]
            
            # 3. 직렬화 해제(Unpickle) 및 JPEG 압축 해제(imdecode) 후 OpenCV 프레임으로 변환
            imgencode = pickle.loads(frame_data)
            frame = cv2.imdecode(imgencode, cv2.IMREAD_COLOR)
            
            if frame is not None:
                # [딜레이 타파 핵심] 큐(Queue)를 쓰지 않고 전역 변수에 계속 덮어씌움으로써, 
                # 인공지능 연산 속도가 느려지더라도 항상 '가장 최근에 찍힌 실시간 화면'만 보장합니다.
                latest_frame = frame  
    except Exception as e:
        print(f"[수신 쓰레드 에러] 연결이 끊겼거나 패킷 오류가 발생했습니다: {e}")

# 수신 스레드 즉시 가동 (대몬 스레드로 설정하여 메인 프로그램 종료 시 자동 소멸)
recv_th = threading.Thread(target=video_receive_thread, daemon=True)
recv_th.start()

# =========================================================
# 🎯 5. 메인 제어 루프 (YOLO 인공지능 추론 및 결과 전송 전담)
# =========================================================
last_speak_time = 0      # 음성 안내 무한 연발 방지용: 직전 송신 타임스탬프 기록
last_spoken_object = None  # 음성 안내 중복 방지용: 직전에 인지했던 사물명 기록

try:
    print("[PC] 실시간 인공지능 모니터링 및 안내 데이터 전송 루프 가동 시작...")
    while True:
        # 백그라운드 스레드가 라즈베리 파이로부터 첫 프레임을 정상적으로 수신할 때까지 잠시 대기
        if latest_frame is None:
            time.sleep(0.01)
            continue
            
        # 연산 중에 값이 바뀌어 픽셀이 깨지는 것을 막기 위해 최신 프레임을 로컬 복사본으로 복제
        frame = latest_frame.copy()
        current_time = time.time()
        
        # 💡 YOLO 추론 하이퍼파라미터 조절 구역
        # - conf: 사물 인식 신뢰도 임계값 (0.75 = 인공지능이 75% 이상 확신할 때만 화면에 표시 및 안내함)
        #   너무 멍청하게 엉뚱한 걸 잡으면 이 값을 0.80으로 높이고, 사물을 너무 못 잡으면 0.65 정도로 낮추세요.
        # - imgsz: 모델 입력 해상도. 256으로 낮춰 하드웨어 연산 대역폭 확보 및 프레임 속도 극대화.
        results = model(frame, conf=0.75, imgsz=256, show=False, verbose=False)

        line = ""
        # 검출된 결과 데이터 바운딩 박스 파싱
        for result in results:
            boxes = result.boxes
            for box in boxes:
                class_id = int(box.cls[0])
                obj = model.names[class_id] # 인덱스로 매칭되는 텍스트 사물명 획득 (예: 'chair')
                
                # 검출된 사물이 우리가 지정한 필수 타겟 사물 리스트에 포함되어 있는지 검증
                if obj in TARGET_OBJECTS:
                    # [알고리즘 개정 가이드] 화면 분할 필터링을 제거하고 화면에 들어오면 즉시 중앙(front)으로 간주
                    obj_dir = "front"
                    line = f"{obj_dir}_{obj}" # 라즈베리 파이가 인식할 수 있는 문자열 규격 조립 ('front_chair')
                    break                     # 프레임당 가장 먼저 걸린 핵심 사물 하나만 우선 안내하기 위해 루프 탈출

            if line:  
                break # 다중 루프 탈출

        # =========================================================
        # 📡 6. 데이터 송신 제어 (보행자 피로도 경감을 위한 쿨타임 로직)
        # =========================================================
        # 조건 1: 한 번 음성이 매칭되어 전송된 후 최소 2.5초가 지나야 다음 안내가 나갈 수 있습니다.
        if line and (current_time - last_speak_time > 2.5):
            # 조건 2: 직전 사물과 '다른 새로운 사물'이 등장하면 2.5초 쿨타임 직후 즉시 멘트가 나가지만,
            # 조건 3: '동일한 사물'이 계속 앞에 머물러 있다면 최소 4.0초가 지나야 리마인드 멘트를 전송합니다.
            if (line != last_spoken_object) or (current_time - last_speak_time > 4.0):
                print(f"[PC ➔ RPi 텍스트 송신] {line} (음성 출력용 명령)")
                try:
                    # 라즈베리 파이에서 한 줄 단위로 읽을 수 있게 개행문자(\n)를 더해 UTF-8 바이트 스트림으로 송신
                    t_client.sendall((line + "\n").encode('utf-8'))
                    
                    # 제어 변수 업데이트 (쿨타임 리셋)
                    last_spoken_object = line
                    last_speak_time = current_time
                except Exception as send_err:
                    print(f"[PC 송신 에러] 라즈베리 파이와의 연결이 불안정합니다: {send_err}")

        # =========================================================
        # 📺 7. 화면 모니터링 윈도우 시각화 출력
        # =========================================================
        # 사물이 인식되었을 경우 바운딩 박스와 클래스명이 덧그려진 렌더링 화면(.plot())을 띄우고, 
        # 아무것도 안 잡힐 때는 깔끔한 원본 실시간 프레임을 송출합니다.
        if len(results) > 0:
            cv2.imshow("Wireless PC Brain Engine (YOLOv8)", results[0].plot())
        else:
            cv2.imshow("Wireless PC Brain Engine (YOLOv8)", frame)
            
        # [팀원 가이드] 화면을 클릭한 상태에서 키보드 영문 'q'를 누르면 안전하게 종료 프로세스로 진입합니다.
        if cv2.waitKey(1) & 0xFF == ord('q'):
            break

finally:
    # 프로그램 종료 시 소켓 닫기 및 모니터링 창 소멸 (자원 회수 필수)
    is_running = False
    cv2.destroyAllWindows()
    v_client.close()
    t_client.close()
    print("[PC] 모든 무선 세션이 닫혔으며 프로그램이 정상 종료되었습니다.")
