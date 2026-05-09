"""
network/client.py - WP2: TCP 소켓 클라이언트 (양방향)

역할:
  [송신] gesture_queue에서 명령을 꺼내 ESP32로 TCP 전송
  [수신] ESP32로부터 센서 이벤트를 받아 log_queue에 전달 → Discord 알림

전송 형식 (docs/protocol.md 참고):
  'ANGLE:[0~180]\n'  - 그리퍼 서보 각도  (예: 'ANGLE:90\n')
  'CW\n'             - 손목 시계 방향 회전
  'CCW\n'            - 손목 반시계 방향 회전

수신 형식 (펌웨어 팀 협의 후 확정):
  'GRIP_SUCCESS\n'   - 압력 센서 임계값 초과 → 파지 성공

ESP32 미연결 시 동작:
  연결 실패해도 프로그램은 계속 실행된다.
  gesture.py와 bot.py는 정상 동작하며 명령은 스킵된다.
"""

import queue
import socket
import threading
import time


def _connect(ip: str, port: int) -> socket.socket | None:
    """
    ESP32에 TCP 연결 시도

    Args:
      ip   : ESP32 IP 주소 (예: '192.168.0.42')
      port : ESP32 포트 번호 (예: 8080)

    Returns:
      연결 성공 → socket 객체
      연결 실패 → None (호출부에서 None 체크 후 스킵 처리)
    """
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.connect((ip, port))
        print(f"[client] ESP32 연결 성공! ({ip}:{port})")
        return sock
    except Exception as e:
        print(f"[client] ESP32 연결 실패: {e}")
        return None


# ── 수신 이벤트 처리 ─────────────────────────────────────
# ESP32에서 새로운 이벤트가 추가되면 여기에 등록
# key: ESP32가 보내는 문자열 / value: Discord에 전송할 메시지
RECEIVE_EVENTS = {
    'GRIP_SUCCESS': '파지 성공!',
    # 'GRIP_FAIL'  : '파지 실패',   ← 펌웨어 팀과 협의 후 추가 가능
}
# ────────────────────────────────────────────────────────


def _receive_loop(sock: socket.socket, log_queue: queue.Queue, stop_event: threading.Event, grip_event: threading.Event):
    """
    ESP32 → AI 수신 루프 (별도 스레드에서 실행)

    동작:
      소켓에서 데이터를 계속 읽으며 줄바꿈(\n) 단위로 메시지를 파싱한다.
      RECEIVE_EVENTS에 등록된 메시지가 오면 log_queue에 넣어 Discord로 전달한다.

    버퍼링 처리:
      TCP는 데이터가 여러 패킷으로 나뉘어 올 수 있다.
      buffer에 누적해두고 \n이 올 때마다 한 줄씩 잘라서 처리한다.
      예) 'GRIP_S' + 'UCCESS\n' → buffer에서 'GRIP_SUCCESS' 완성 후 처리

    ※ 펌웨어 팀과 협의 완료 후 RECEIVE_EVENTS에 이벤트 추가하면 바로 동작

    Args:
      sock       : ESP32와 연결된 소켓 (run_client에서 전달)
      log_queue  : Discord 알림용 메시지를 넣는 Queue
      stop_event : set되면 루프 종료
      grip_event : GRIP_SUCCESS 수신 시 set → gesture.py 웹캠 화면에 표시
    """
    buffer = ''
    sock.settimeout(1)  # 1초마다 루프를 돌며 stop_event 확인

    print("[client] 수신 대기 시작")

    while not stop_event.is_set():
        try:
            data = sock.recv(1024).decode('utf-8')
            if not data:
                # 소켓이 닫힘 (ESP32 연결 끊김)
                print("[client] ESP32 연결 끊김 (수신)")
                break
            buffer += data

            # \n 단위로 메시지 파싱
            while '\n' in buffer:
                line, buffer = buffer.split('\n', 1)
                line = line.strip()
                if not line:
                    continue

                if line in RECEIVE_EVENTS:
                    msg = RECEIVE_EVENTS[line]
                    print(f"[client] ESP32 수신: {line} → {msg}")
                    log_queue.put(msg)       # → bot.py → Discord
                    if line == 'GRIP_SUCCESS':
                        grip_event.set()     # → gesture.py 웹캠 화면에 표시
                else:
                    # 등록되지 않은 메시지 (디버그용)
                    print(f"[client] ESP32 알 수 없는 메시지: {line}")

        except socket.timeout:
            continue  # 1초 대기 후 stop_event 재확인
        except Exception as e:
            print(f"[client] 수신 오류: {e}")
            break

    print("[client] 수신 루프 종료")


def run_client(gesture_queue, log_queue, esp32_ip: str, esp32_port: int, stop_event, grip_event):
    """
    ClientThread 진입 함수 - main.py에서 호출됨

    동작 순서:
      1. ESP32에 TCP 연결
      2. 연결 성공 시 수신 스레드(_receive_loop) 시작
      3. gesture_queue에서 명령을 꺼내 ESP32로 송신

    송신/수신 분리 이유:
      송신은 gesture_queue를 기다리느라 블로킹될 수 있고
      수신은 소켓을 계속 읽어야 한다.
      같은 스레드에서 처리하면 한쪽이 막힐 때 다른 쪽도 멈추므로
      수신을 별도 스레드로 분리한다.

    Args:
      gesture_queue : gesture.py가 제스처 명령을 넣는 Queue
      log_queue     : bot.py에게 전달할 로그 메시지 Queue
      esp32_ip      : ESP32 IP 주소 (main.py의 ESP32_IP)
      esp32_port    : ESP32 포트   (main.py의 ESP32_PORT)
      stop_event    : set되면 루프 종료
    """
    sock = _connect(esp32_ip, esp32_port)

    # ESP32 연결 성공 시 수신 스레드 시작
    if sock:
        recv_thread = threading.Thread(
            target=_receive_loop,
            args=(sock, log_queue, stop_event, grip_event),
            name="ReceiveThread",
            daemon=True,
        )
        recv_thread.start()

    while not stop_event.is_set():
        # 1초 동안 제스처가 없으면 queue.Empty → continue로 재대기
        try:
            cmd = gesture_queue.get(timeout=1)
        except queue.Empty:
            continue

        # OPEN/CLOSE는 Discord 알림용 (ESP32에는 ANGLE로 이미 전달됨)
        if cmd in ('OPEN', 'CLOSE'):
            log_queue.put(f"그리퍼: {cmd}")
            continue

        # ANGLE:XX, CW, CCW → ESP32로 전송
        if sock:
            try:
                # ESP32 펌웨어가 줄바꿈(\n)을 명령 구분자로 사용
                sock.sendall((cmd + '\n').encode())
            except Exception as e:
                print(f"[client] 전송 오류: {e} → 재연결 시도")
                sock.close()
                time.sleep(1)
                sock = _connect(esp32_ip, esp32_port)

        # CW/CCW만 Discord로 전달 (ANGLE:XX는 너무 빈번해 제외)
        if cmd in ('CW', 'CCW'):
            log_queue.put(f"손목 회전: {cmd}")

    if sock:
        sock.close()
    print("[client] 종료")