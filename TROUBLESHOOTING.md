# Troubleshooting

개발 과정에서 마주친 주요 기술적 문제와 해결 과정을 기록합니다.

---

## 1. 갭 복구 시 이중 누적 버그

**증상**

패킷 손실 후 갭 복구(`_fill_gap`)가 실행되면, 실제 점유 대수보다 서버 누적값(`acc_received`)이 비정상적으로 높아지는 현상이 반복됨.

**원인 분석**

초기 구현에서 `_fill_gap()`이 누락 패킷의 추정 delta를 `acc_received`에 직접 더하고, 이후 실제 수신 패킷이 도착했을 때 일반 delta 누적 로직(`acc_received += actual_delta`)이 한 번 더 실행되어 이중 누적이 발생.

```python
# 문제 코드 (수정 전)
def _fill_gap(eid, from_seq, to_seq):
    estimated_delta = _estimate_gap_delta(eid)
    for s in range(from_seq, to_seq):
        acc_received[eid] += estimated_delta  # ← 여기서 이미 누적
        entrance_db[eid].append(...)          # 이후 실제 패킷 도착 시 또 누적됨
```

**해결**

`_fill_gap()`에서 `acc_received` 직접 수정을 제거하고, 추정 정보는 `entrance_db` 로그에만 기록하도록 분리. 실제 누적은 일반 수신 경로에서 한 번만 수행.

```python
# 수정 후: acc_received를 건드리지 않고 로그만 남김
def _fill_gap(eid, from_seq, to_seq):
    estimated_delta = _estimate_gap_delta(eid)
    for s in range(from_seq, to_seq):
        entrance_db[eid].append({..., "gap_estimated": True})
```

---

## 2. Z-score 이상치 탐지 과민 반응

**증상**

정상적인 delta 값(예: -2, +3)도 이상치로 분류되어 delta=0으로 처리되는 경우가 빈번하게 발생. 실제 점유 대수가 지속적으로 과소 집계됨.

**원인 분석**

초기 Z-score 임계값을 `ZSCORE_THRESH = 2.0`으로 설정했는데, 센서가 `randint(-3, 3)` 범위의 delta를 생성하므로 정상 분포에서도 Z-score 2.0을 넘는 케이스가 자주 발생함. 또한 윈도우가 특정 범위의 값만 쌓인 경우 표준편차가 작아져 과민해짐.

**해결**

두 가지 방어선을 적용:

1. 임계값을 `ZSCORE_THRESH = 3.0`으로 상향
2. `abs(delta) <= NORMAL_RANGE(3)` 조건을 추가하여 물리적으로 가능한 정상 범위 내의 값은 Z-score 계산 없이 즉시 통과시킴. 손상값(9999 등 절대적으로 불가능한 값)만 Z-score로 걸러냄.

---

## 3. Checkpoint 동기화 후 비교 차트 점프 현상

**증상**

Checkpoint 패킷이 도착해 `acc_received`가 강제 동기화되면, Streamlit 비교 차트에서 수신값 선이 순간적으로 크게 점프하는 시각적 이상이 발생.

**원인 분석**

Checkpoint 처리 시 `_snapshot()`이 즉시 호출되어 동기화 전·후 값이 같은 시각에 두 스냅샷으로 찍히는 구조. 차트가 두 점을 선형 보간하면서 수직에 가까운 선이 그려짐.

**해결**

Checkpoint 처리 경로에서 snapshot 호출 타이밍을 보정 후 단 1회로 제한하고, 대시보드 동기화 보정 로그 섹션에 보정 전·후 값을 별도 표시해 사용자가 점프의 원인을 확인할 수 있도록 UI를 보완함.

---

## 4. 멀티스레드 센서 시뮬레이션에서 서버 연결 오류 간헐 발생

**증상**

입구 A, B 스레드가 동시에 FastAPI 서버로 요청을 보낼 때 `requests.exceptions.ConnectionError`가 간헐적으로 발생하고 패킷이 유실됨.

**원인 분석**

FastAPI 서버가 완전히 기동되기 전에 센서 스레드가 첫 요청을 전송하거나, 두 스레드가 동시에 다수의 재전송을 시도할 때 서버 측 연결 수 제한에 도달.

**해결**

- `sensor_simulation.py` 실행 전 서버 기동(`uvicorn ... --reload`)을 선행 조건으로 명시
- `TCPSimulator`의 `send()` 메서드에서 `ConnectionError` 발생 시 해당 패킷을 `pending` 버퍼에 저장하고, 다음 루프에서 `drain_pending()`으로 재시도하는 방식으로 복구 처리
- `timeout=5`로 단일 요청 타임아웃을 명시해 스레드가 무기한 블로킹되는 상황을 방지

---

## 5. seq_num 중복 수신으로 인한 통계 오염

**증상**

재전송 성공 후 지연 도착한 원본 패킷이 추가로 수신되어, 동일 seq에 대한 delta가 두 번 누적되는 현상.

**원인 분석**

TCP 재전송 시뮬레이션에서 원본 패킷이 네트워크 레이어에서 지연된 상태로 살아있고, 재전송 패킷이 먼저 도착해 성공하더라도 지연된 원본이 나중에 도착.

**해결**

서버의 `_track_seq()`에서 입구별로 `received_seqs` 집합을 유지하고, 이미 처리한 seq가 수신되면 즉시 중복으로 판정해 `entrance_stats["duplicate_seq"]`만 증가시키고 delta 누적은 건너뜀.
