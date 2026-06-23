import cv2
import socket
import numpy as np
import pickle
import struct
import time
import threading  
from ultralytics import YOLO

# =========================================================
#  변수 세팅
# =========================================================
RPI_IP = "172.20.10.3" 
V_PORT = 9998
T_PORT = 9999

TARGET_OBJECTS = [
    'elevator', 'vending_machine', 'trash_bin', 'self_service_cafe',
    'water_dispenser', 'locker', 'door', 'obstacle', 'photo_copier',
    'person', 'lectern', 'desk', 'chair', 'signboard'
]

# 1. 라즈베리 파이 서버에 쌍방향 소켓 도킹 시도
print(f"[PC] 라즈베리 파이 무선 스트리밍 세션 연결 중...")
v_client = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
t_client = socket.socket(socket.AF_INET, socket.SOCK_STREAM)

try:
    v_client.connect((RPI_IP, V_PORT))
    t_client.connect((RPI_IP, T_PORT))
    print("[PC] 라즈베리 파이와 무선 도킹 성공!")
except Exception as e:
    print(f"[PC] 무선 연결 실패: {e}")
    exit(1)

# 2. 모델 로드
print("[PC] YOLOv8 모델 로딩 중...")
model = YOLO("best_complete4.pt")

# =========================================================
# 💡 [딜레이 제어] 영상 수신 전용 쓰레드 구현
# =========================================================
latest_frame = None
is_running = True

def video_receive_thread():
    global latest_frame, is_running
    data = b""
    payload_size = struct.calcsize(">L")
    
    try:
        while is_running:
            # 이미지 크기(Header) 추출
            while len(data) < payload_size:
                packet = v_client.recv(4096)
                if not packet: return
                data += packet
            
            packed_msg_size = data[:payload_size]
            data = data[payload_size:]
            msg_size = struct.unpack(">L", packed_msg_size)[0]
            
            # 이미지 데이터(Payload) 추출
            while len(data) < msg_size:
                packet = v_client.recv(4096)
                if not packet: return
                data += packet
                
            frame_data = data[:msg_size]
            data = data[msg_size:]
            
            # JPEG 압축 해제 및 디코딩
            imgencode = pickle.loads(frame_data)
            frame = cv2.imdecode(imgencode, cv2.IMREAD_COLOR)
            
            if frame is not None:
                latest_frame = frame  # 항상 최신 프레임으로 덮어쓰기 (큐 적체 원천 봉쇄)
    except Exception as e:
        print(f"[수신 쓰레드 에러] {e}")

# 쓰레드 시작
recv_th = threading.Thread(target=video_receive_thread, daemon=True)
recv_th.start()

# =========================================================
# 메인 제어 루프 (YOLO 인공지능 연산 및 제어 전담)
# =========================================================
last_speak_time = 0
last_spoken_object = None

try:
    while True:
        # 쓰레드가 프레임을 받아올 때까지 잠시 대기
        if latest_frame is None:
            time.sleep(0.01)
            continue
            
        # 최신 프레임 복사해오기
        frame = latest_frame.copy()
        current_time = time.time()
        
        results = model(frame, conf=0.75, imgsz=256, show=False, verbose=False)

        line = ""
        for result in results:
            boxes = result.boxes
            for box in boxes:
                class_id = int(box.cls[0])
                obj = model.names[class_id]
                
                if obj in TARGET_OBJECTS:
                    # 화면 제한 영역 필터링을 빼고, 화면 전체에서 감지되면 즉시 front로 지정
                    obj_dir = "front"
                    line = f"{obj_dir}_{obj}"
                    break

            if line:  
                break

        # =========================================================
        # 데이터 전송 제어 (기존 로직 유지)
        # =========================================================
        if line and (current_time - last_speak_time > 2.5):
            if (line != last_spoken_object) or (current_time - last_speak_time > 4.0):
                print(f"[PC ➔ RPi 텍스트 송신] {line}")
                try:
                    t_client.sendall((line + "\n").encode('utf-8'))
                    last_spoken_object = line
                    last_speak_time = current_time
                except Exception as send_err:
                    print(f"[PC 송신 에러] {send_err}")

        # 화면 출력
        if len(results) > 0:
            cv2.imshow("Wireless PC Brain Engine (YOLOv8)", results[0].plot())
        else:
            cv2.imshow("Wireless PC Brain Engine (YOLOv8)", frame)
            
        if cv2.waitKey(1) & 0xFF == ord('q'):
            break

finally:
    is_running = False
    cv2.destroyAllWindows()
    v_client.close()
    t_client.close()
    print("[PC] 종료 완료")
