"""
main.py - 스마트 그리퍼 AI 메인 진입점

실행 흐름:
  Thread 1 (gesture)  →  gesture_queue  →  Thread 2 (client)  →  ESP32
                ↑                                   ↓                ↓
           grip_event  ←──────────────────────────────         log_queue
           (파지 성공                                                  ↓
            웹캠 표시)                          Thread 3 (bot)  →  Discord

종료 방법:
  - 웹캠 창에서 'q' 키
  - 터미널에서 Ctrl+C

실행 방법:
  python main.py
"""

import os
import threading
import queue
import sys
from dotenv import load_dotenv

from gesture.gesture import run_gesture
from network.client import run_client
from bot.bot import run_bot

load_dotenv()

# ── 설정값 (.env 파일에서 읽음) ─────────────────────────
ESP32_IP   = os.getenv('ESP32_IP', '')      # ESP32 IP 주소 - firmware 팀에게 받을 것
ESP32_PORT = int(os.getenv('ESP32_PORT', '8080'))

QUEUE_MAX = 10  # Queue 최대 크기: 너무 크면 오래된 제스처가 쌓임
# ────────────────────────────────────────────────────────


def main():
    # --------------------------------------------------
    # Queue 생성
    # gesture_queue : gesture.py → client.py (제스처 명령 전달)
    # log_queue     : client.py  → bot.py    (Discord 알림 전달)
    # --------------------------------------------------
    gesture_queue = queue.Queue(maxsize=QUEUE_MAX)
    log_queue     = queue.Queue(maxsize=QUEUE_MAX)

    # --------------------------------------------------
    # 종료 이벤트
    # stop_event.set() 호출 시 모든 스레드가 루프를 빠져나와 정상 종료
    # --------------------------------------------------
    stop_event = threading.Event()

    # --------------------------------------------------
    # 파지 성공 이벤트
    # client.py가 ESP32로부터 GRIP_SUCCESS를 수신하면 set()
    # gesture.py가 웹캠 창에 파지 성공 메시지를 표시한 뒤 clear()
    # --------------------------------------------------
    grip_event = threading.Event()

    # --------------------------------------------------
    # 스레드 정의
    # daemon=True : main.py가 종료되면 스레드도 자동 종료 (안전망)
    # --------------------------------------------------
    threads = [
        threading.Thread(
            target=run_gesture,
            args=(gesture_queue, log_queue, stop_event, grip_event),
            name="GestureThread",
            daemon=True,
        ),
        threading.Thread(
            target=run_client,
            args=(gesture_queue, log_queue, ESP32_IP, ESP32_PORT, stop_event, grip_event),
            name="ClientThread",
            daemon=True,
        ),
        threading.Thread(
            target=run_bot,
            args=(log_queue, stop_event),
            name="BotThread",
            daemon=True,
        ),
    ]

    for t in threads:
        print(f"[main] {t.name} 시작")
        t.start()

    print("[main] 모든 스레드 실행 중.")
    print("[main] 종료 방법: 웹캠 창을 클릭해 포커스 준 뒤 'q' 키  |  터미널에서 Ctrl+C")

    # --------------------------------------------------
    # 메인 스레드는 여기서 대기
    # join(timeout=1) : 1초마다 루프를 돌며 KeyboardInterrupt 감지
    # --------------------------------------------------
    try:
        while not stop_event.is_set():
            for t in threads:
                t.join(timeout=1)
    except KeyboardInterrupt:
        print("\n[main] Ctrl+C 감지 → 종료 중...")
        stop_event.set()

    print("[main] 프로그램 종료")
    sys.exit(0)


if __name__ == "__main__":
    main()