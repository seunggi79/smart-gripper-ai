"""
bot/bot.py - WP5: Discord 알림 봇

역할:
  log_queue에서 메시지를 꺼내서 Discord Webhook으로 전송한다.
  main.py의 BotThread가 이 함수를 실행한다.

Discord Webhook 설정 방법:
  1. Discord 채널 설정 → 연동 → 웹후크 → 새 웹후크 생성
  2. 웹후크 URL 복사
  3. 아래 WEBHOOK_URL에 붙여넣기
  (WEBHOOK_URL이 비어 있으면 Discord 전송은 건너뛰고 로그만 출력)
"""

import os
import queue
import requests
from dotenv import load_dotenv

load_dotenv()

# ── 설정값 (.env 파일에서 읽음) ─────────────────────────
WEBHOOK_URL = os.getenv('DISCORD_WEBHOOK_URL', '')
# ────────────────────────────────────────────────────────


def _send(msg: str):
    """
    Discord Webhook으로 메시지 전송

    Args:
      msg: 전송할 텍스트 메시지
    """
    if not WEBHOOK_URL:
        return
    try:
        # timeout=5: 5초 안에 응답 없으면 포기 (Discord 서버 장애 대비)
        requests.post(WEBHOOK_URL, json={'content': msg}, timeout=5)
    except Exception as e:
        print(f"[bot] Discord 전송 실패: {e}")


def run_bot(log_queue, stop_event):
    """
    BotThread 진입 함수 - main.py에서 호출됨

    동작:
      log_queue에서 메시지를 꺼내서 Discord로 전송
      1초 동안 메시지가 없으면 queue.Empty 예외 → 다시 대기

    Args:
      log_queue  : client.py가 로그 메시지를 넣는 Queue
      stop_event : main.py에서 생성된 Event - set되면 루프 종료
    """
    print("[bot] Discord 봇 시작")

    while not stop_event.is_set():
        try:
            msg = log_queue.get(timeout=1)
        except queue.Empty:
            # 1초 대기 중 메시지 없음 → 정상 상황, 다시 대기
            continue

        _send(msg)

    print("[bot] 종료")