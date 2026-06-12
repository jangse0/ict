"""
TCP 프로토콜 핵심 기능 시뮬레이터

실제 TCP가 비신뢰적 IP 레이어 위에서 신뢰성을 보장하는 방식을 모방:
  - 비신뢰 네트워크 레이어: 패킷 손실 / 전송 지연 / 데이터 손상
  - TCP 레이어: 시퀀스 번호, ACK, RTO 기반 재전송, 지수 백오프

[변경사항]
  send_with_ack(): 기존 send()를 확장. ACK 응답에서 server_acc를 추출해
  (성공여부, server_acc) 튜플로 반환한다. 센서가 이 값으로 누적 오차를 감지한다.
"""

import random
import time
import requests
from dataclasses import dataclass
from datetime import datetime
from typing import Optional, Tuple


@dataclass
class TCPPacket:
    seq_num: int
    data: dict


class NetworkLayer:
    """
    비신뢰적 네트워크 레이어 (IP layer 역할).
    실제 환경의 패킷 손실, 전송 지연, 데이터 손상을 시뮬레이션한다.
    """

    def __init__(
        self,
        loss_rate: float = 0.15,
        min_delay: float = 1.0,
        max_delay: float = 2.0,
        corruption_rate: float = 0.05,
    ):
        self.loss_rate = loss_rate
        self.min_delay = min_delay
        self.max_delay = max_delay
        self.corruption_rate = corruption_rate

    def transmit(self, data: dict) -> Tuple[str, Optional[dict]]:
        """
        패킷 전송 시도. 반환값: ("sent"|"lost"|"corrupted", data|None)
        """
        if random.random() < self.loss_rate:
            return "lost", None

        delay = random.uniform(self.min_delay, self.max_delay)
        print(f"    [네트워크] 전송 지연 {delay:.2f}s 발생...")
        time.sleep(delay)

        if random.random() < self.corruption_rate:
            corrupted = data.copy()
            corrupted["delta"] = 9999   # 수신 측이 감지할 수 있는 불가능한 값
            print("    [네트워크] 데이터 손상 발생!")
            return "corrupted", corrupted

        return "sent", data


class TCPSimulator:
    """
    TCP 신뢰적 전송 시뮬레이터.

    패킷 손실 시 RTO(Retransmission Timeout) 대기 후 재전송하며,
    재전송이 반복될수록 RTO를 2배씩 늘린다 (지수 백오프, Exponential Backoff).
    MAX_RETRIES 초과 시 해당 패킷 전송을 포기한다.
    """

    INITIAL_RTO = 1.0  # 초기 재전송 타임아웃 (실제 TCP: ~1s)
    MAX_RETRIES = 3    # 최대 재전송 횟수 (실제 TCP: 보통 3~5회)

    def __init__(self, network: NetworkLayer, server_url: str, label: str = "",
                 log_url: Optional[str] = None):
        self.network = network
        self.server_url = server_url
        self.log_url = log_url
        self._entrance_id = label
        self._label = f"[입구 {label}] " if label else ""
        self._seq_num = 0
        self.stats = {
            "total_sent": 0,
            "success": 0,
            "failed": 0,
            "network_loss_events": 0,
            "retransmissions": 0,
            "corruption_events": 0,
        }

    def _log(self, event_type: str, seq: int, detail: str = ""):
        if not self.log_url:
            return
        try:
            requests.post(self.log_url, json={
                "time": datetime.now().strftime("%H:%M:%S"),
                "entrance_id": self._entrance_id,
                "event_type": event_type,
                "seq": seq,
                "detail": detail,
            }, timeout=1)
        except Exception:
            pass

    def _next_seq(self) -> int:
        self._seq_num += 1
        return self._seq_num

    def _attempt_transmit(self, packet_data: dict, seq: int) -> Tuple[bool, Optional[int]]:
        """
        단일 전송 시도. 반환값: (성공여부, server_acc or None)
        """
        status, result_data = self.network.transmit(packet_data)

        if status == "lost":
            self.stats["network_loss_events"] += 1
            print(f"  {self._label}[패킷 손실] seq={seq} 드롭됨 → ACK 없음, 재전송 예정")
            self._log("패킷손실", seq, f"드롭")
            return False, None

        if status == "corrupted":
            self.stats["corruption_events"] += 1
            print(f"  {self._label}[데이터 손상] seq={seq} 손상값 그대로 서버 전송 → 서버 Z-score 탐지")
            self._log("데이터손상", seq, "손상값 서버 전달 → Z-score 2차 방어")
            # 손상 패킷을 드롭하지 않고 서버로 그대로 전달
            # 서버의 Z-score가 2차 방어선으로 탐지해 delta=0 처리
            try:
                response = requests.post(self.server_url, json=result_data, timeout=5)
                if response.status_code == 200:
                    body = response.json()
                    ack_num    = body.get("ack_num")
                    server_acc = body.get("server_acc")
                    if ack_num == seq:
                        self.stats["success"] += 1
                        return True, server_acc
            except Exception as e:
                print(f"  {self._label}[연결 오류] {e}")
            return False, None

        try:
            response = requests.post(self.server_url, json=result_data, timeout=5)
            if response.status_code == 200:
                body = response.json()
                ack_num   = body.get("ack_num")
                server_acc = body.get("server_acc")   # 서버 누적값 (방법 2)
                if ack_num == seq:
                    return True, server_acc
                else:
                    print(f"  {self._label}[ACK 불일치] 예상={seq}, 수신={ack_num}")
        except requests.exceptions.Timeout:
            print(f"  {self._label}[타임아웃] seq={seq} 서버 응답 없음 → 재전송 예정")
        except Exception as e:
            print(f"  {self._label}[연결 오류] {e}")

        return False, None

    def send_with_ack(self, data: dict) -> Tuple[bool, Optional[int]]:
        """
        TCP 신뢰적 전송 (확장판).
        성공 시 (True, server_acc), 최종 실패 시 (False, None) 반환.
        server_acc는 서버의 현재 누적값으로, 센서가 오차 감지에 활용한다.
        """
        seq = self._next_seq()
        self.stats["total_sent"] += 1
        packet_data = {**data, "seq_num": seq}
        rto = self.INITIAL_RTO

        print(f"\n{self._label}[TCP] seq={seq} 전송 시작")
        self._log("전송시작", seq)

        for attempt in range(self.MAX_RETRIES + 1):
            if attempt > 0:
                print(f"  {self._label}[TCP 재전송] seq={seq}, "
                      f"시도 {attempt}/{self.MAX_RETRIES}, RTO 대기 {rto:.1f}s")
                self._log("재전송", seq, f"시도 {attempt}/{self.MAX_RETRIES} | RTO {rto:.1f}s")
                time.sleep(rto)
                rto *= 2
                self.stats["retransmissions"] += 1

            ok, server_acc = self._attempt_transmit(packet_data, seq)
            if ok:
                print(f"  {self._label}[ACK 수신] seq={seq} → 전송 성공 "
                      f"(server_acc={server_acc})")
                self._log("ACK수신", seq, f"{'재전송 후 성공' if attempt > 0 else '정상'} "
                          f"server_acc={server_acc}")
                self.stats["success"] += 1
                return True, server_acc

        print(f"{self._label}[TCP] seq={seq} 최대 재전송 초과 → 전송 포기")
        self._log("전송포기", seq, f"최대 {self.MAX_RETRIES}회 재전송 초과")
        self.stats["failed"] += 1
        return False, None

    # 하위 호환 — 기존 코드가 send()를 호출하는 경우 (pending drain 등)
    def send(self, data: dict) -> bool:
        ok, _ = self.send_with_ack(data)
        return ok

    def print_stats(self):
        s = self.stats
        total = s["total_sent"] or 1
        print("\n" + "=" * 50)
        print("=== TCP 전송 통계 ===")
        print(f"  총 패킷:          {s['total_sent']}")
        print(f"  성공:             {s['success']} ({s['success']/total*100:.1f}%)")
        print(f"  최종 실패:        {s['failed']}")
        print(f"  네트워크 손실:    {s['network_loss_events']}회")
        print(f"  재전송:           {s['retransmissions']}회")
        print(f"  손상 감지:        {s['corruption_events']}회")
        print("=" * 50)