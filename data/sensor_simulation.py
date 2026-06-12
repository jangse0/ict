"""
센서 시뮬레이터 — 입구 A, B 동시 전송 (변화량 기반)

각 입구는 해당 입구를 통과한 자동차의 순 변화량(delta)을 측정한다.
  delta > 0 : 이 입구로 들어온 차가 더 많음
  delta < 0 : 이 입구로 나간 차가 더 많음
서버가 A+B delta를 누적해 전체 주차 대수를 계산한다.

[복구 전략]
1. 주기적 Checkpoint: CHECKPOINT_INTERVAL 번마다 local_acc 절대값을 함께 전송.
   서버가 checkpoint를 수신하면 acc_received를 강제 동기화해 누적 오차를 리셋한다.
2. ACK 기반 즉각 보정: 서버 ACK에 server_acc가 포함되며, 센서의 local_acc와
   비교해 CORRECTION_THRESHOLD 이상 차이가 나면 즉시 보정 패킷을 전송한다.
"""
import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))

import time
import random
import threading
import requests
from datetime import datetime
from tcp_simulator import NetworkLayer, TCPSimulator

SERVER_URL  = "http://127.0.0.1:8000/data"
TRUTH_URL   = "http://127.0.0.1:8000/truth"
TCP_LOG_URL = "http://127.0.0.1:8000/tcp_log"
MAX_PENDING = 10
TOTAL_CAPACITY = 100

# ── 복구 전략 파라미터 ────────────────────────────────────────────
CHECKPOINT_INTERVAL  = 10   # 매 N번째 패킷마다 절대값(checkpoint) 포함 전송
CORRECTION_THRESHOLD = 5    # 서버 acc와 로컬 acc 차이가 이 값 이상이면 즉시 보정


def post_truth(data: dict):
    """실제 생성값을 네트워크 시뮬 없이 즉시 기록한다."""
    try:
        requests.post(TRUTH_URL, json=data, timeout=2)
    except Exception:
        pass


def drain_pending(label: str, pending: list, tcp: TCPSimulator) -> list:
    if not pending:
        return []
    print(f"[{label}] [복구 큐] {len(pending)}개 재전송 시도...")
    remaining = []
    for old in pending:
        if tcp.send(old):
            print(f"  [{label}] [복구 성공] 원본: {old['timestamp']}")
        else:
            remaining.append(old)
    return remaining


def send_correction(entrance_id: str, local_acc: int, server_acc: int, tcp: TCPSimulator):
    """
    서버 누적값과 로컬 누적값의 차이가 CORRECTION_THRESHOLD 이상일 때
    checkpoint 패킷을 즉시 전송해 서버를 강제 동기화한다.
    """
    diff = local_acc - server_acc
    print(f"[입구 {entrance_id}] [보정] 오차 감지 로컬={local_acc} 서버={server_acc} "
          f"차이={diff:+d} → 보정 패킷 전송")
    correction = {
        "timestamp":    datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "entrance_id":  entrance_id,
        "total_capacity": TOTAL_CAPACITY,
        "delta":        0,           # 이 패킷 자체의 변화량은 0
        "checkpoint":   local_acc,   # 서버가 이 값으로 acc를 덮어씀
        "is_correction": True,
    }
    if not tcp.send(correction):
        print(f"[입구 {entrance_id}] [보정 실패] 보정 패킷 전송 불가")


def run_entrance(entrance_id: str, initial_acc: int):
    """
    한 입구 센서의 루프.
    local_acc : 이 입구를 통한 누적 순 변화 (센서 로컬 기준의 진실값)
    cycle     : 전송 횟수 카운터 (checkpoint 주기 판단에 사용)
    """
    print(f"[입구 {entrance_id}] 시작 (초기 누적: {initial_acc:+d})")

    network = NetworkLayer(loss_rate=0.15, min_delay=1.0, max_delay=2.0, corruption_rate=0.05)
    tcp = TCPSimulator(network=network, server_url=SERVER_URL, label=entrance_id,
                       log_url=TCP_LOG_URL)

    local_acc = initial_acc
    pending   = []
    cycle     = 0

    while True:
        pending = drain_pending(f"입구 {entrance_id}", pending, tcp)

        delta      = random.randint(-3, 3)
        local_acc += delta
        cycle     += 1

        # ── checkpoint 포함 여부 결정 ──────────────────────────────
        is_checkpoint = (cycle % CHECKPOINT_INTERVAL == 0)

        data = {
            "timestamp":    datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "entrance_id":  entrance_id,
            "total_capacity": TOTAL_CAPACITY,
            "delta":        delta,
        }
        if is_checkpoint:
            data["checkpoint"] = local_acc
            print(f"[입구 {entrance_id}] [CHECKPOINT #{cycle}] "
                  f"변화량: {delta:+d}  누적: {local_acc:+d} → 절대값 포함 전송")
        else:
            print(f"[입구 {entrance_id}] 변화량: {delta:+d}  (이 입구 누적: {local_acc:+d})")

        post_truth(data)

        # ── TCP 전송 및 ACK에서 server_acc 추출 ────────────────────
        ok, server_acc = tcp.send_with_ack(data)

        if ok:
            # ── ACK 기반 즉각 보정 (방법 2) ──────────────────────
            if server_acc is not None:
                diff = abs(local_acc - server_acc)
                if diff >= CORRECTION_THRESHOLD:
                    send_correction(entrance_id, local_acc, server_acc, tcp)
        else:
            if len(pending) < MAX_PENDING:
                pending.append(data)
                print(f"[입구 {entrance_id}] [버퍼] 대기: {len(pending)}개")
            else:
                print(f"[입구 {entrance_id}] [포화] 패킷 폐기")

        time.sleep(2)


if __name__ == "__main__":
    print("=== 스마트 주차 센서 시뮬레이션 시작 ===")
    print(f"  전체 용량: {TOTAL_CAPACITY}대")
    print(f"  입구 A, B 동시 전송 — 각 입구에서 변화량(delta)을 측정")
    print(f"  초기 주차 대수: 입구 A 15대, 입구 B 15대")
    print(f"  네트워크: 손실 15%, 지연 1~2s, 손상 5%")
    print(f"  [복구] Checkpoint: 매 {CHECKPOINT_INTERVAL}번째 패킷, "
          f"보정 임계값: ±{CORRECTION_THRESHOLD}대\n")

    threads = [
        threading.Thread(target=run_entrance, args=("A", 15), daemon=True),
        threading.Thread(target=run_entrance, args=("B", 15), daemon=True),
    ]
    for t in threads:
        t.start()

    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        print("\n=== 시뮬레이션 종료 ===")