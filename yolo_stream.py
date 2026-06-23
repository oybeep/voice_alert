import cv2
import socket
import numpy as np
import pickle
import struct
import time
from ultralytics import YOLO

# =========================================================
# ⚙️ 변수 세팅
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

data = b""
payload_size = struct.calcsize(">L")
last_speak_time = 0
last_spoken_object = None

try:
    while True:
        # 🛠️ [딜레이 해결] 소켓에 밀려있는 오래된 패킷 버퍼 강제 초기화
        v_client.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 0)

        # 무선 패킷 데이터로부터 이미지 크기(Header) 추출하기
        while len(data) < payload_size:
            packet = v_client.recv(4096)
            if not packet: break
            data += packet
        
        if not data: break
        
        packed_msg_size = data[:payload_size]
        data = data[payload_size:]
        msg_size = struct.unpack(">L", packed_msg_size)[0]
        
        # 이미지 데이터(Payload)가 다 채워질 때까지 데이터 수신
        while len(data) < msg_size:
            packet = v_client.recv(4096)
            if not packet: break
            data += packet
            
        frame_data = data[:msg_size]
        data = data[msg_size:]
        
        # JPEG 압축 해제하여 일반 프레임 이미지로 변환
        imgencode = pickle.loads(frame_data)
        frame = cv2.imdecode(imgencode, cv2.IMREAD_COLOR)
        
        if frame is None: continue

        current_time = time.time()
        
        # 🛠️ [인식률 개선] conf를 0.25로 낮추어 소형 사물도 스무스하게 인식하도록 변경
        results = model(frame, conf=0.25, imgsz=320, show=False)

        line = ""
        for result in results:
            boxes = result.boxes
            for box in boxes:
                class_id = int(box.cls[0])
                obj = model.names[class_id]
                
                if obj in TARGET_OBJECTS:
                    h, w, _ = frame.shape
                    # 🛠️ [오타 수정] xyxn -> xyxyn 으로 교체 완료! (튕김 현상 해결)
                    box_center_x = box.xyxyn[0][0].item() * w
                    
                    # 💡 정면 조준(중앙 정렬) 필터링 기법 적용
                    if w * 0.2 <= box_center_x <= w * 0.8:
                        obj_dir = "front"
                        line = f"{obj_dir}_{obj}"
                        break
                    else:
                        line = ""

            if line:  
                break

        # 위험 사물이 식별되면 라즈베리 파이로 문자열 전송
        if line and (current_time - last_speak_time > 2.0):
            if line != last_spoken_object:
                print(f"[PC ➔ RPi 텍스트 송신] {line}")
                t_client.sendall((line + "\n").encode('utf-8'))
                last_spoken_object = line
                last_speak_time = current_time

        # 🛠️ [예외 처리] 아무것도 감지하지 못해도 스트리밍 화면 창은 무조건 유지!
        if len(results) > 0:
            cv2.imshow("Wireless PC Brain Engine (YOLOv8)", results[0].plot())
        else:
            cv2.imshow("Wireless PC Brain Engine (YOLOv8)", frame)
            
        if cv2.waitKey(1) & 0xFF == ord('q'):
            break

finally:
    cv2.destroyAllWindows()
    v_client.close()
    t_client.close()
    print("[PC] 종료 완료")
