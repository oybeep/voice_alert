import cv2
import socket
import time
from ultralytics import YOLO

# =========================================================
# ⚙️ 내 환경에 맞게 수정할 변수
# =========================================================
RPI_IP = "172.20.10.3" 
PORT = 9999

TARGET_OBJECTS = [
    'elevator', 'vending_machine', 'trash_bin', 'self_service_cafe',
    'water_dispenser', 'locker', 'door', 'cabinet', 'photo_copier',
    'person', 'lectern', 'desk', 'chair', 'signboard'
]

# 1. 라즈베리 파이 서버에 소켓 연결 시도
client_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
print(f"[PC] 라즈베리 파이({RPI_IP})에 연결을 시도합니다...")
try:
    client_socket.connect((RPI_IP, PORT))
    print("[PC] 라즈베리 파이와 연결 성공!")
except Exception as e:
    print(f"[PC] 연결 실패: {e}")
    exit(1)

# 2. 내 컴퓨터 자원으로 YOLOv8 로드
print("[PC] YOLOv8 모델 로딩 중...")
model = YOLO("best_complete4.pt") # 내 컴퓨터 폴더에 같이 둔 모델 파일명

# 3. 내 컴퓨터에 꽂힌 카메라 열기 (0번 기본 웹캠)
cap = cv2.VideoCapture(0)

last_speak_time = 0
last_spoken_object = None

try:
    while cap.isOpened():
        success, frame = cap.read()
        if not success: continue

        current_time = time.time()
        
        # 내 고성능 PC 리소스로 끊김 없이 초고속 추론 가동 (conf 70% 설정)
        results = model(frame, conf=0.7, show=False)

        line = ""
        for result in results:
            boxes = result.boxes
            for box in boxes:
                class_id = int(box.cls[0])
                obj = model.names[class_id]
                
                if obj in TARGET_OBJECTS:
                    # 화면 가로 크기 기준으로 객체가 좌/우/정면 어디에 있는지 계산
                    h, w, _ = frame.shape
                    box_center_x = box.xyxn[0][0].item() * w
                    
                    if box_center_x < w * 0.33:
                        obj_dir = "left"
                    elif box_center_x > w * 0.66:
                        obj_dir = "right"
                    else:
                        obj_dir = "front"
                    
                    line = f"{obj_dir}_{obj}"
                    break # 프레임당 가장 먼저 걸린 핵심 객체 1개만 매핑

        # 3초 쿨타임 및 중복 사운드 방지 후 라즈베리 파이로 텍스트 데이터 쏘기
        if line and (current_time - last_speak_time > 3.0):
            if line != last_spoken_object:
                print(f"[PC ➔ RPi 텍스트 송신] {line}")
                # 문자열 끝에 줄바꿈(\n)을 붙여 소켓으로 전송
                client_socket.sendall((line + "\n").encode('utf-8'))
                last_spoken_object = line
                last_speak_time = current_time

        # 모니터링용 화면 띄우기
        if len(results) > 0:
            cv2.imshow("PC AI Brain Engine (YOLOv8 Live)", results[0].plot())
            
        if cv2.waitKey(1) & 0xFF == ord('q'):
            break

finally:
    cap.release()
    cv2.destroyAllWindows()
    client_socket.close()
    print("[PC] 종료 완료")
