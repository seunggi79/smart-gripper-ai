# Smart Gripper - AI

MediaPipe 손 인식으로 굽힘 비율을 계산해 ESP32 그리퍼를 제어하는 AI 파트입니다.

## 시작하기

### 1. 저장소 클론
```bash
git clone <저장소 주소>
cd smart-gripper/ai
```

### 2. 가상환경 생성 및 활성화
```bash
# 생성
python -m venv venv

# 활성화 (Windows)
venv\Scripts\activate

# 활성화 (Mac/Linux)
source venv/bin/activate
```

### 3. 패키지 설치
```bash
pip install -r requirements.txt
```

### 4. 설정값 입력

`.env.example`을 복사해서 `.env`로 이름 바꾼 뒤 값을 입력:
```
DISCORD_WEBHOOK_URL=https://discord.com/api/webhooks/...
ESP32_IP=192.168.0.xxx
ESP32_PORT=8080
```

- `DISCORD_WEBHOOK_URL` : Discord 채널 설정 → 연동 → 웹후크에서 복사
- `ESP32_IP` : firmware 팀에게 받을 것
- `.env` 파일은 `.gitignore`에 등록되어 있어 GitHub에 올라가지 않음

> **키 없이도 제스처 인식은 동작합니다.**
> `DISCORD_WEBHOOK_URL`, `ESP32_IP`를 비워두거나 `.env` 파일 없이 실행해도 웹캠 손 인식과 각도 계산은 정상 동작합니다. 해당 키가 없으면 Discord 알림과 ESP32 전송만 건너뜁니다.

### 5. 실행
```bash
python main.py
```

| 키 | 동작 |
|----|------|
| `q` | 프로그램 종료 (웹캠 창 포커스 상태에서) |
| `g` | 파지 성공 시뮬레이션 (테스트용) |

---

## 폴더 구조

```
ai/
├── main.py              # 진입점 - 여기서 실행
├── gesture/
│   └── gesture.py       # WP1: 굽힘 비율 추출 + 서보 각도 변환
├── network/
│   └── client.py        # WP2: TCP 양방향 통신 (송신/수신)
├── bot/
│   └── bot.py           # WP5: Discord Webhook 알림
├── .env                 # 설정값 (GitHub 비공개)
├── .env.example         # 설정값 양식 (GitHub 공개)
└── requirements.txt
```

## 제스처 명령

| 제스처 | 전송 명령 | 동작 |
|--------|----------|------|
| 손가락 굽힘 정도 | `ANGLE:[0~180]` | 그리퍼 서보 각도 제어 |
| 주먹 + 검지쪽 위로 기울임 | `CW` | 손목 시계 방향 회전 |
| 주먹 + 소지쪽 위로 기울임 | `CCW` | 손목 반시계 방향 회전 |

- 손 완전히 펼침 → `ANGLE:0` (그리퍼 열림)
- 주먹 → `ANGLE:180` (그리퍼 닫힘)
- Discord 알림은 OPEN(0~30°) / CLOSE(150~180°) / CW / CCW 상태 변경 시에만 전송

---

## 전체 데이터 흐름

```
웹캠 → [gesture.py] → gesture_queue → [client.py] → ESP32 (TCP)
            ↑                               ↓              ↓
        grip_event ←─────────────────────────        log_queue
      (파지 성공                                           ↓
       웹캠 표시)                               [bot.py] → Discord
```

### 단계별 설명

1. **gesture.py (GestureThread)**
   - 웹캠에서 매 프레임을 읽어 MediaPipe로 손 랜드마크 21개 추출
   - 손가락 4개(검지~소지) 굽힘 비율(0.0~1.0) 계산 → 서보 각도(0~180) 변환
   - 최근 5프레임 이동 평균으로 스무딩, 5도 이상 변할 때만 `gesture_queue`에 추가
   - 손목 기울기로 CW/CCW 판단
   - `grip_event` 감지 시 웹캠 창에 3초간 'GRIP SUCCESS!' 표시

2. **client.py (ClientThread + ReceiveThread)**
   - `gesture_queue`에서 명령을 꺼내 ESP32로 TCP 전송 (`ANGLE:90\n` 형식)
   - 전송 실패 시 자동 재연결
   - OPEN/CLOSE/CW/CCW 상태 변경 시에만 `log_queue`에 기록
   - ReceiveThread: ESP32로부터 `GRIP_SUCCESS` 수신 시 `log_queue` + `grip_event` 처리

3. **bot.py (BotThread)**
   - `log_queue`에서 메시지를 꺼내 Discord Webhook으로 전송

### 각 파일 담당자

| 파일 | Work Package | 설명 |
|------|-------------|------|
| `gesture/gesture.py` | WP1 | MediaPipe 굽힘 비율 + 서보 각도 변환 |
| `network/client.py` | WP2 | ESP32 TCP 양방향 통신 |
| `bot/bot.py` | WP5 | Discord Webhook 알림 |

---

## 자주 묻는 것들

**Q. 웹캠이 두 개 이상인데 카메라가 안 잡혀요**
→ `gesture.py`의 `cv2.VideoCapture(0)` 에서 `0`을 `1`, `2`로 바꿔보세요.

**Q. Discord 알림 없이 테스트하고 싶어요**
→ `.env`의 `DISCORD_WEBHOOK_URL`을 비워두면 Discord 전송을 건너뜁니다. 나머지 기능은 정상 동작합니다.

**Q. ESP32 없이 테스트하고 싶어요**
→ `.env`의 `ESP32_IP`를 비워두면 소켓 연결 실패 후 gesture/bot은 정상 동작합니다.

**Q. 압력 센서 파지 성공을 테스트하고 싶어요**
→ 웹캠 창 포커스 상태에서 `g` 키를 누르면 GRIP_SUCCESS를 시뮬레이션합니다.
