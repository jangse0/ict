# Smart Parking Monitoring System

## YouTube Link

> **[시연 영상 링크를 여기에 삽입하세요]**

---

## System Overview

실시간 스마트 주차장 모니터링 시스템입니다.

비신뢰적 네트워크(패킷 손실·지연·손상) 환경에서도 주차 대수를 정확하게 집계하고, AI 기반 이상치 탐지와 선형 회귀 예측을 통해 혼잡도를 실시간으로 관리합니다.

입구 A·B 두 곳에서 각각 초기 15대가 주차된 상태로 시작하며, 두 입구의 변화량을 합산해 전체 주차 현황을 집계합니다.

**주요 기능**

| 기능 | 설명 |
|------|------|
| TCP 신뢰적 전송 시뮬레이션 | 패킷 손실 15%, 지연 1~2s, 손상 5% 환경에서 재전송·ACK 처리 |
| Z-score 이상치 탐지 | 손상된 센서값(예: delta=9999)을 자동 감지해 delta=0으로 대체 |
| 갭 추정 복구 | 누락된 패킷 구간을 최근 정상 delta 평균으로 추정·보간 |
| Checkpoint / ACK 보정 | 누적 오차를 주기적 절대값 동기화와 즉각 보정 패킷으로 해소 |
| 선형 회귀 예측 | 최근 20개 스냅샷으로 약 3분 후 혼잡도 상태를 예측 |
| 실시간 웹 대시보드 | Streamlit으로 점유율·이벤트 로그·비교 차트 시각화 |

---

## Logic

### 시스템 아키텍처

```
┌──────────────────────────────────────────────────────┐
│           data/sensor_simulation.py                  │
│  입구 A 스레드 (초기 15대) ──┐                       │
│                              ├── TCP 전송 → FastAPI  │
│  입구 B 스레드 (초기 15대) ──┘                       │
└──────────────────────────────────────────────────────┘
         │ /data, /truth, /tcp_log
         ▼
┌──────────────────────────────────────────────────────┐
│               src/main.py (FastAPI)                  │
│  1. 시퀀스 번호로 갭·중복·지연 패킷 추적             │
│  2. Z-score 이상치 탐지 (delta 윈도우 20개)          │
│  3. Checkpoint / ACK 보정 처리                       │
│  4. 선형 회귀로 PREDICT_STEPS(10) 후 혼잡도 예측     │
└──────────────────────────────────────────────────────┘
         │ REST API 폴링 (2초 주기)
         ▼
┌──────────────────────────────────────────────────────┐
│           dashboard/app.py (Streamlit)               │
│  - 전체 현황 메트릭 / 상태 배너                      │
│  - 실제값 vs 서버 수신값 비교 차트 (A+B 합산)        │
│  - TCP 이벤트 로그 / 서버 수신 로그 / 동기화 보정 로그│
└──────────────────────────────────────────────────────┘
```

### 핵심 처리 흐름

1. **센서 → 네트워크 레이어** (`tcp_simulator.py`): `NetworkLayer`가 손실/지연/손상을 무작위로 적용
2. **TCP 레이어**: `TCPSimulator`가 seq 번호를 붙여 전송하고, 실패 시 지수 백오프(RTO × 2)로 최대 3회 재전송
3. **서버 수신** (`main.py`): seq 갭 감지 → `_fill_gap()` 갭 추정 복구 → Z-score 이상치 탐지 → delta 누적
4. **Checkpoint 동기화**: 매 10번째 패킷에 `checkpoint=local_acc` 포함 → 서버 acc 강제 덮어쓰기
5. **ACK 보정**: 서버 ACK의 `server_acc`와 로컬 acc 차이 ≥ 5이면 즉시 보정 패킷 전송
6. **예측**: 최근 수신값 20개로 `np.polyfit(x, y, 1)` → 10스텝 후 A+B 합산 점유 대수 및 혼잡도 분류

---

## Project Structure

```
.
├── src/
│   ├── main.py               # FastAPI 백엔드 (이상치 탐지·동기화·예측)
│   └── tcp_simulator.py      # 비신뢰 네트워크 + TCP 재전송 시뮬레이터
├── dashboard/
│   └── app.py                # Streamlit 실시간 대시보드
├── data/
│   ├── sensor_simulation.py  # 입구 A·B 센서 시뮬레이터 (멀티스레드)
│   └── sample_readings.json  # 테스트용 샘플 패킷 데이터
├── requirements.txt
└── README.md
```

## How to Run

```bash
# 1. 의존성 설치
pip install -r requirements.txt

# 2. FastAPI 서버 실행 (터미널 1)
uvicorn src.main:app --reload

# 3. Streamlit 대시보드 실행 (터미널 2)
streamlit run dashboard/app.py

# 4. 센서 시뮬레이션 시작 (터미널 3)
python data/sensor_simulation.py
```
