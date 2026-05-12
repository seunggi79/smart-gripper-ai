"""
gesture/gesture.py - WP1: MediaPipe 굽힘 비율 추출 + 서보 각도 비례 변환

역할:
  웹캠 → MediaPipe 손 랜드마크 → 손가락 굽힘 비율(0.0~1.0) 계산
  → 서보 각도(0~180) 변환 → gesture_queue에 전달

Queue에 넣는 값:
  'ANGLE:[0~180]' - 그리퍼 서보 각도  (예: 'ANGLE:90')
  'CW'            - 손목 시계 방향 회전
  'CCW'           - 손목 반시계 방향 회전

종료:
  웹캠 창에서 'q' 키 → stop_event를 set해서 모든 스레드 종료

MediaPipe 손 랜드마크 번호 참고:
  https://ai.google.dev/edge/mediapipe/solutions/vision/hand_landmarker
  주요 번호:
    0  = 손목(Wrist)
    5  = 검지 손바닥 관절(Index MCP)   ← mcp_idx
    8  = 검지 끝(Index Tip)            ← tip_idx
    9  = 중지 손바닥 관절(Middle MCP)   ← 손 크기 기준점
    12 = 중지 끝(Middle Tip)
    13 = 약지 손바닥 관절(Ring MCP)
    16 = 약지 끝(Ring Tip)
    17 = 소지 손바닥 관절(Pinky MCP)
    20 = 소지 끝(Pinky Tip)
"""

import queue
import time
import os
import cv2
import mediapipe as mp
from mediapipe.tasks import python as mp_python
from mediapipe.tasks.python import vision as mp_vision
from collections import deque
import urllib.request


# ── MediaPipe 모델 다운로드 ─────────────────────────────
# mp.solutions API는 0.10.30+에서 제거됨 → Tasks API + 별도 모델 파일 필요
_MODEL_PATH = os.path.join(os.path.dirname(__file__), 'hand_landmarker.task')
_MODEL_URL = (
    'https://storage.googleapis.com/mediapipe-models/'
    'hand_landmarker/hand_landmarker/float16/1/hand_landmarker.task'
)

if not os.path.exists(_MODEL_PATH):
    print("[gesture] 손 인식 모델 다운로드 중... (최초 1회)")
    urllib.request.urlretrieve(_MODEL_URL, _MODEL_PATH)
    print("[gesture] 모델 다운로드 완료")

# ── MediaPipe 초기화 ────────────────────────────────────
_base_options = mp_python.BaseOptions(model_asset_path=_MODEL_PATH)
_options = mp_vision.HandLandmarkerOptions(
    base_options=_base_options,
    running_mode=mp_vision.RunningMode.VIDEO,
    num_hands=1,
)
_landmarker = mp_vision.HandLandmarker.create_from_options(_options)

# 손 연결선 정의 (mp.solutions.hands.HAND_CONNECTIONS 대체)
_HAND_CONNECTIONS = [
    (0, 1), (1, 2), (2, 3), (3, 4),
    (5, 6), (6, 7), (7, 8),
    (9, 10), (10, 11), (11, 12),
    (13, 14), (14, 15), (15, 16),
    (17, 18), (18, 19), (19, 20),
    (0, 5), (5, 9), (9, 13), (13, 17), (0, 17),
]
# ────────────────────────────────────────────────────────


# ── 튜닝 파라미터 ───────────────────────────────────────
SERVO_MAX          = 180   # 서보 최대 각도 (도)
SMOOTH_FRAMES      = 5     # 이동 평균 프레임 수 (클수록 부드럽지만 반응 느려짐)
SEND_THRESHOLD     = 5     # 이전 전송값과 이 각도 이상 차이날 때만 전송 (노이즈 제거)
BEND_OFFSET        = 0.3   # 굽힘 비율 오프셋 - 완전 펴짐이 0.0이 되도록 조정
                           # 실험 방법: 손 완전히 펴고 각도가 0°에 가까운지 확인
                           # 아니면 이 값을 ±0.05씩 조절
ROTATION_THRESHOLD = 0.1   # 손목 회전 감지 기울기 임계값
# ────────────────────────────────────────────────────────


def _draw_landmarks(frame, landmarks):
    """랜드마크 점과 연결선을 OpenCV로 직접 그림 (mp.solutions.drawing_utils 대체)"""
    h, w = frame.shape[:2]
    pts = [(int(lm.x * w), int(lm.y * h)) for lm in landmarks]
    for a, b in _HAND_CONNECTIONS:
        cv2.line(frame, pts[a], pts[b], (0, 255, 0), 2)
    for pt in pts:
        cv2.circle(frame, pt, 4, (255, 0, 0), -1)


def _finger_bend_ratio(landmarks, tip_idx, mcp_idx) -> float:
    """
    손가락 하나의 굽힘 비율 계산

    원리:
      손가락이 펴지면 tip이 mcp보다 위에 있다 (tip.y < mcp.y)
      손가락이 접히면 tip이 mcp 근처로 내려온다 (tip.y ≈ mcp.y 이상)
      이 차이를 손 크기로 나눠 카메라 거리에 무관하게 정규화

    정규화 기준:
      손목(0)~중지 MCP(9) 사이 거리를 손 크기로 사용
      이 거리는 손의 실제 크기에 비례하므로 카메라 거리 변화에 강함

    Args:
      landmarks : MediaPipe 랜드마크 배열
      tip_idx   : 손가락 끝 번호 (검지=8, 중지=12, 약지=16, 소지=20)
      mcp_idx   : 손가락 손바닥 관절 번호 (검지=5, 중지=9, 약지=13, 소지=17)

    Returns:
      0.0 = 완전히 펴짐, 1.0 = 완전히 접힘
    """
    hand_size = abs(landmarks[0].y - landmarks[9].y)
    if hand_size < 0.01:
        # 손이 너무 작게 잡혔거나 정면을 보는 경우 - 측정 불가
        return 0.0

    diff = landmarks[tip_idx].y - landmarks[mcp_idx].y
    # diff < 0: tip이 mcp 위 (펴짐) / diff > 0: tip이 mcp 아래 (접힘)
    ratio = (diff / hand_size) + BEND_OFFSET
    return max(0.0, min(1.0, ratio))


def _get_gripper_angle(landmarks) -> int:
    """
    4개 손가락의 평균 굽힘 비율 → 서보 각도(0~180) 변환

    엄지 제외 이유:
      엄지는 좌우 방향으로 움직여 y좌표 기반 계산이 부정확함

    Returns:
      0   = 그리퍼 완전 열림 (손 펼침)
      180 = 그리퍼 완전 닫힘 (주먹)
    """
    ratios = [
        _finger_bend_ratio(landmarks, 8,  5),   # 검지 (tip=8,  mcp=5)
        _finger_bend_ratio(landmarks, 12, 9),   # 중지 (tip=12, mcp=9)
        _finger_bend_ratio(landmarks, 16, 13),  # 약지 (tip=16, mcp=13)
        _finger_bend_ratio(landmarks, 20, 17),  # 소지 (tip=20, mcp=17)
    ]
    avg_ratio = sum(ratios) / len(ratios)
    return int(avg_ratio * SERVO_MAX)


def _get_wrist_rotation(landmarks) -> str | None:
    """
    손 기울기로 손목 회전 방향 판단

    원리:
      검지 MCP(5)와 소지 MCP(17)의 y 좌표 차이로 손이 기울어진 방향 판단
      diff < 0 → 검지 쪽이 위 → 시계 방향(CW)
      diff > 0 → 소지 쪽이 위 → 반시계 방향(CCW)

    Returns:
      'CW' | 'CCW' | None (수평 = 회전 없음)
    """
    diff = landmarks[5].y - landmarks[17].y
    if diff < -ROTATION_THRESHOLD:
        return 'CW'
    elif diff > ROTATION_THRESHOLD:
        return 'CCW'
    return None


def run_gesture(gesture_queue, log_queue, stop_event, grip_event):
    """
    GestureThread 진입 함수 - main.py에서 호출됨

    종료 방법:
      웹캠 창에서 'q' 키 → stop_event.set() → 모든 스레드 종료

    그리퍼 각도 전송 방식:
      - 최근 SMOOTH_FRAMES개 프레임의 각도를 이동 평균 → 떨림 감소
      - 이전 전송값과 SEND_THRESHOLD도 이상 차이날 때만 Queue에 추가
      - Queue 가득 찬 경우 현재 명령 버림 (put_nowait) → 블로킹 없음

    손목 회전 전송 방식:
      - 기울기 감지 즉시 전송
      - 중립(수평)으로 돌아오면 last_rotation 초기화 → 재진입 시 재전송 가능

    파지 성공 표시:
      - client.py가 ESP32로부터 GRIP_SUCCESS 수신 시 grip_event.set()
      - 웹캠 창에 3초간 '파지 성공!' 표시 후 grip_event.clear()

    Args:
      gesture_queue : main.py에서 생성된 Queue 객체
      log_queue     : Discord 알림용 Queue (테스트 키 'g'에서 사용)
      stop_event    : main.py에서 생성된 Event - set되면 루프 종료
      grip_event    : client.py가 GRIP_SUCCESS 수신 시 set하는 Event
    """
    cap = cv2.VideoCapture(0)  # 0 = 첫 번째 카메라 (USB 웹캠이면 1, 2... 변경)
    if not cap.isOpened():
        print("[gesture] 웹캠 연결 실패!")
        stop_event.set()
        return
    print("[gesture] 웹캠 연결 성공!")

    angle_buffer  = deque(maxlen=SMOOTH_FRAMES)
    last_angle    = -1   # 마지막 전송 각도 (-1 = 아직 전송 안 함)
    last_rotation = ''   # 마지막 전송 회전 명령
    last_state    = ''   # 마지막 출력 상태 ('OPEN' | 'CLOSE' | '')
    grip_until    = 0.0  # 파지 성공 메시지를 표시할 종료 시각 (time.time() 기준)

    while not stop_event.is_set():
        ret, frame = cap.read()
        if not ret:
            print("[gesture] 프레임 읽기 실패. 웹캠을 확인하세요.")
            break

        # MediaPipe Tasks API는 mp.Image 래퍼를 요구
        rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
        mp_image = mp.Image(image_format=mp.ImageFormat.SRGB, data=rgb)
        timestamp_ms = int(time.monotonic() * 1000)
        result = _landmarker.detect_for_video(mp_image, timestamp_ms)

        if result.hand_landmarks:
            for hand_landmarks in result.hand_landmarks:
                _draw_landmarks(frame, hand_landmarks)

                # ── 그리퍼 서보 각도 ─────────────────────
                raw_angle = _get_gripper_angle(hand_landmarks)
                angle_buffer.append(raw_angle)
                smoothed = int(sum(angle_buffer) / len(angle_buffer))

                if abs(smoothed - last_angle) >= SEND_THRESHOLD:
                    cmd = f'ANGLE:{smoothed}'
                    try:
                        gesture_queue.put_nowait(cmd)
                    except queue.Full:
                        pass  # 큐 꽉 참 → 이 프레임 스킵 (최신 명령이 더 중요)
                    last_angle = smoothed

                    # 상태(OPEN/CLOSE)가 바뀔 때만 로그 출력
                    if smoothed < 30:
                        state = 'OPEN'
                    elif smoothed > 150:
                        state = 'CLOSE'
                    else:
                        state = ''

                    if state and state != last_state:
                        print(f"[gesture] {state}")
                        try:
                            gesture_queue.put_nowait(state)  # Discord 알림용
                        except queue.Full:
                            pass
                    last_state = state

                # ── 손목 회전 ────────────────────────────
                rotation = _get_wrist_rotation(hand_landmarks)
                if rotation and rotation != last_rotation:
                    print(f"\n[gesture] 손목 회전: {rotation}")
                    try:
                        gesture_queue.put_nowait(rotation)
                    except queue.Full:
                        pass
                    last_rotation = rotation
                elif not rotation:
                    last_rotation = ''  # 중립으로 돌아오면 리셋 (재진입 허용)

        # 화면에 각도 + OPEN/CLOSE 상태 표시
        if last_angle < 30:
            state, color = 'OPEN',  (0, 255, 0)   # 초록
        elif last_angle > 150:
            state, color = 'CLOSE', (0, 0, 255)   # 빨강
        else:
            state, color = '',      (0, 255, 0)

        cv2.putText(frame, f"Gripper: {last_angle}deg  {state}",
                    (10, 40), cv2.FONT_HERSHEY_SIMPLEX, 1, color, 2)

        # 손목 회전 상태 표시
        if last_rotation == 'CW':
            rot_text, rot_color = 'Wrist: CW  >>>', (0, 200, 255)   # 주황
        elif last_rotation == 'CCW':
            rot_text, rot_color = 'Wrist: CCW <<<', (255, 200, 0)   # 하늘
        else:
            rot_text, rot_color = 'Wrist: -',       (200, 200, 200) # 회색

        cv2.putText(frame, rot_text,
                    (10, 80), cv2.FONT_HERSHEY_SIMPLEX, 1, rot_color, 2)

        # 파지 성공 표시
        # grip_event가 set되면 3초간 화면에 표시 후 clear
        if grip_event.is_set():
            grip_until = time.time() + 3.0  # 현재 시각 + 3초
            grip_event.clear()

        if time.time() < grip_until:
            cv2.putText(frame, 'GRIP SUCCESS!',
                        (10, 130), cv2.FONT_HERSHEY_SIMPLEX, 1, (0, 255, 255), 2)  # 노랑

        cv2.imshow('Hand Tracking', frame)

        key = cv2.waitKey(1) & 0xFF
        if key == ord('q'):
            stop_event.set()  # 모든 스레드에 종료 신호
            break
        elif key == ord('g'):
            # [테스트용] 'g' 키 → 압력 센서 파지 성공 시뮬레이션
            print("[gesture] 테스트: GRIP_SUCCESS 강제 발생")
            grip_event.set()              # 웹캠 창에 표시
            log_queue.put('파지 성공!')   # Discord 전송

    cap.release()
    cv2.destroyAllWindows()
    print("[gesture] 종료")