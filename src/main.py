from fastapi import FastAPI
from pydantic import BaseModel
from datetime import datetime
from typing import Optional, Callable
import numpy as np

app = FastAPI()

ENTRANCES = ["A", "B"]
TOTAL_CAPACITY = 100

# ── AI 파라미터 ───────────────────────────────────────────────────────────────
ZSCORE_WINDOW    = 20   # Z-score 윈도우 크기
ZSCORE_THRESH    = 3.0  # 이상치 판정 sigma (-3~+3 범위 delta에서 2.0은 너무 민감)
ZSCORE_MIN_DATA  = 5    # 윈도우 부족 시 고정 임계값 폴백
REGRESSION_WIN   = 20   # 선형 회귀에 사용할 스냅샷 개수
PREDICT_STEPS    = 10   # 몇 스텝 앞을 예측할지 (1스텝 ≈ 수신 주기)


class SensorData(BaseModel):
    timestamp: str
    entrance_id: str = "A"
    total_capacity: int = TOTAL_CAPACITY
    delta: int
    seq_num: Optional[int] = None
    checkpoint: Optional[int] = None
    is_correction: bool = False


class TcpEvent(BaseModel):
    time: str
    entrance_id: str
    event_type: str
    seq: int
    detail: str = ""


# ── 누적 상태 ─────────────────────────────────────────────────────────────────
acc_received: dict = {"A": 15, "B": 15}
acc_truth:    dict = {"A": 15, "B": 15}

# ── AI 상태 ───────────────────────────────────────────────────────────────────
delta_window: dict = {"A": [], "B": []}  # Z-score용 정상 delta 윈도우

ai_stats: dict = {
    eid: {
        "zscore_detected":    0,
        "hardlimit_detected": 0,
        "last_zscore":        None,
        "last_delta_mean":    None,
        "last_delta_std":     None,
        "gap_estimated":      0,     # 갭 추정 복구 횟수
    }
    for eid in ENTRANCES
}

sync_log:          list = []
compare_history:   list = []
_snapshot_counter: int  = 0

entrance_db:            dict = {"A": [], "B": []}
entrance_received_seqs: dict = {"A": set(), "B": set()}
entrance_stats: dict = {
    eid: {"total_received": 0, "duplicate_seq": 0, "corrupted_recovered": 0,
          "gap_packets": 0, "late_delivered": 0, "last_seq_num": None,
          "checkpoint_syncs": 0, "correction_syncs": 0}
    for eid in ENTRANCES
}
truth_db:      dict = {"A": [], "B": []}
tcp_event_log: list = []


# ── 공통 유틸 ─────────────────────────────────────────────────────────────────
def _status(occ: int) -> str:
    rate = occ / TOTAL_CAPACITY * 100
    if rate < 50:  return "Available"
    if rate < 80:  return "Warning"
    return "Full"


def _clamp(value: float) -> int:
    return max(0, min(TOTAL_CAPACITY, int(round(value))))


def _trim(db: list, limit: int = 60):
    if len(db) > limit:
        del db[:len(db) - limit]


def _track_seq(seq: Optional[int], received: set, stats: dict,
               fill_gap: Callable) -> bool:
    if seq is None:
        return False
    last = stats["last_seq_num"]
    is_late = False
    if last is not None and seq > last + 1:
        stats["gap_packets"] += seq - last - 1
        fill_gap(last + 1, seq)
    if last is not None and seq <= last:
        stats["late_delivered"] += 1
        is_late = True
    received.add(seq)
    stats["last_seq_num"] = max(last or 0, seq)
    return is_late


def _snapshot(trigger: str = ""):
    global _snapshot_counter
    _snapshot_counter += 1
    compare_history.append({
        "idx":         _snapshot_counter,
        "time":        datetime.now().strftime("%H:%M:%S"),
        "trigger":     trigger,
        "truth_ab":    _clamp(acc_truth["A"] + acc_truth["B"]),
        "received_ab": _clamp(acc_received["A"] + acc_received["B"]),
    })
    _trim(compare_history)


def _no_data() -> dict:
    return {
        "total_occupancy": 0, "total_capacity": TOTAL_CAPACITY,
        "occupancy_rate": "0.0%", "status": "Available",
        "entrances": {}, "no_data": True,
    }


def _record_sync(eid: str, sync_type: str, before: int, after: int):
    diff = after - before
    sync_log.append({
        "time": datetime.now().strftime("%H:%M:%S"),
        "entrance_id": eid, "type": sync_type,
        "before": before, "after": after, "diff": diff,
    })
    _trim(sync_log, 100)
    print(f"[입구 {eid}] [{sync_type}] acc {before} → {after} (보정량: {diff:+d})")


# ── AI 1: Z-score 이상치 탐지 ────────────────────────────────────────────────
def _zscore_detect(eid: str, delta: int) -> tuple[bool, str]:
    """
    최근 정상 delta 윈도우로 평균·표준편차를 계산하고,
    새 delta의 Z-score가 ZSCORE_THRESH 초과이면서
    동시에 정상 범위(-3~+3)를 벗어난 경우에만 이상치로 판정한다.

    delta가 -3~+3 범위 내이면 설령 Z-score가 높아도 정상으로 처리한다.
    손상값 9999처럼 절대적으로 불가능한 값만 이상치로 잡는다.
    윈도우 부족 시 고정 임계값(> TOTAL_CAPACITY)으로 폴백.
    """
    NORMAL_RANGE = 3   # sensor_simulation의 randint(-3, 3) 범위

    window = delta_window[eid]
    astat  = ai_stats[eid]

    # 정상 범위 내 delta는 Z-score 계산 없이 바로 통과
    if abs(delta) <= NORMAL_RANGE:
        # 윈도우 통계는 업데이트
        if len(window) >= ZSCORE_MIN_DATA:
            arr  = np.array(window[-ZSCORE_WINDOW:], dtype=float)
            mean = float(np.mean(arr))
            std  = float(np.std(arr))
            if std >= 0.01:
                zscore = abs(delta - mean) / std
                astat["last_zscore"]     = round(zscore, 2)
                astat["last_delta_mean"] = round(mean, 2)
                astat["last_delta_std"]  = round(std, 2)
        return False, ""

    # 정상 범위 초과 → Z-score or 고정 임계값으로 판정
    if len(window) < ZSCORE_MIN_DATA:
        if abs(delta) > TOTAL_CAPACITY:
            astat["hardlimit_detected"] += 1
            return True, f"hard-limit (window<{ZSCORE_MIN_DATA}): |{delta}|>{TOTAL_CAPACITY}"
        return False, ""

    arr  = np.array(window[-ZSCORE_WINDOW:], dtype=float)
    mean = float(np.mean(arr))
    std  = float(np.std(arr))

    if std < 0.01:
        astat["last_delta_mean"] = round(mean, 2)
        astat["last_delta_std"]  = 0.0
        if abs(delta) > TOTAL_CAPACITY:
            astat["hardlimit_detected"] += 1
            return True, f"hard-limit (std≈0): |{delta}|>{TOTAL_CAPACITY}"
        return False, ""

    zscore = abs(delta - mean) / std
    astat["last_zscore"]     = round(zscore, 2)
    astat["last_delta_mean"] = round(mean, 2)
    astat["last_delta_std"]  = round(std, 2)

    if zscore > ZSCORE_THRESH:
        astat["zscore_detected"] += 1
        reason = (f"z-score={zscore:.2f} > {ZSCORE_THRESH} "
                  f"(mean={mean:.1f}, std={std:.1f})")
        print(f"[입구 {eid}] [AI-이상치] delta={delta} {reason}")
        return True, reason

    return False, ""


# ── AI 2: 갭 복구 시 delta 추정 ──────────────────────────────────────────────
def _estimate_gap_delta(eid: str) -> int:
    """
    손실된 패킷의 delta를 최근 정상 delta 윈도우의 평균으로 추정한다.
    윈도우가 비어 있으면 0으로 폴백.
    """
    window = delta_window[eid]
    if not window:
        return 0
    recent = window[-5:] if len(window) >= 5 else window
    estimated = int(round(float(np.mean(recent))))
    return estimated


# ── AI 3: 선형 회귀 기반 예측 ────────────────────────────────────────────────
def _predict() -> dict:
    """
    최근 REGRESSION_WIN 개의 수신 점유 대수(A+B)로 선형 회귀를 수행해
    PREDICT_STEPS 스텝 후 점유 대수를 예측하고 혼잡도 추세를 분류한다.
    """
    if len(compare_history) < 3:
        return {"ready": False}

    snapshots = compare_history[-REGRESSION_WIN:]
    y = np.array([s["received_ab"] for s in snapshots], dtype=float)
    x = np.arange(len(y), dtype=float)

    coeffs     = np.polyfit(x, y, 1)
    slope      = float(coeffs[0])
    predicted  = float(np.polyval(coeffs, len(y) - 1 + PREDICT_STEPS))
    predicted  = _clamp(predicted)

    # 혼잡도 추세 분류
    if slope > 1.0:    trend, trend_icon = "급증",  "🔺"
    elif slope > 0.3:  trend, trend_icon = "증가",  "↗"
    elif slope < -1.0: trend, trend_icon = "급감",  "🔻"
    elif slope < -0.3: trend, trend_icon = "감소",  "↘"
    else:              trend, trend_icon = "안정",  "➡"

    return {
        "ready":        True,
        "slope":        round(slope, 3),
        "predicted":    predicted,
        "predict_steps": PREDICT_STEPS,
        "trend":        trend,
        "trend_icon":   trend_icon,
        "predicted_status": _status(predicted),
    }


# ── 갭 채우기 (AI 추정 delta 사용) ───────────────────────────────────────────
def _fill_gap(eid: str, from_seq: int, to_seq: int):
    """
    손실된 seq 구간을 로그에 기록한다.
    acc_received는 여기서 건드리지 않는다.
    기존에 acc_received를 직접 수정하던 게 이중 누적 버그의 원인이었음.
    """
    estimated_delta = _estimate_gap_delta(eid)
    ai_stats[eid]["gap_estimated"] += (to_seq - from_seq)

    for s in range(from_seq, to_seq):
        entrance_db[eid].append({
            "received_at":    datetime.now().strftime("%H:%M:%S"),
            "sensor_time":    "N/A (누락)", "entrance_id": eid, "seq_num": s,
            "delta":          estimated_delta,
            "corrupted":      False,
            "estimated":      True,
            "late_delivered": False,
            "anomaly":        False,
            "gap_estimated":  True,
        })
    _trim(entrance_db[eid])
    _snapshot(f"gap-{eid}")


# ── 데이터 처리 ───────────────────────────────────────────────────────────────
def _process(data: SensorData) -> dict:
    eid   = data.entrance_id
    stats = entrance_stats[eid]
    stats["total_received"] += 1
    seq = data.seq_num

    if seq is not None and seq in entrance_received_seqs[eid]:
        stats["duplicate_seq"] += 1
        return {"message": "Duplicate seq ignored", "ack_num": seq, "server_acc": acc_received[eid]}

    is_late = _track_seq(
        seq, entrance_received_seqs[eid], stats,
        lambda f, t: _fill_gap(eid, f, t),
    )

    # ── [AI 1] Z-score 이상치 탐지 ───────────────────────────────────────
    anomaly, anomaly_reason = _zscore_detect(eid, data.delta)

    if anomaly:
        actual_delta = 0
        stats["corrupted_recovered"] += 1
        print(f"[입구 {eid}] [AI-손상처리] seq={seq} delta={data.delta} → 0  ({anomaly_reason})")
    else:
        actual_delta = data.delta
        delta_window[eid].append(actual_delta)
        if len(delta_window[eid]) > ZSCORE_WINDOW:
            delta_window[eid].pop(0)

    # ── Checkpoint 동기화 ─────────────────────────────────────────────────
    if data.checkpoint is not None and not anomaly:
        before = acc_received[eid]
        acc_received[eid] = data.checkpoint
        stats["checkpoint_syncs"] += 1
        _record_sync(eid, "CHECKPOINT", before, data.checkpoint)
        _snapshot(f"checkpoint-{eid}")
        return {
            "message":    "Checkpoint sync applied",
            "ack_num":    seq,
            "server_acc": acc_received[eid],
        }

    # ── ACK 보정 패킷 처리 ────────────────────────────────────────────────
    if data.is_correction and not anomaly:
        before = acc_received[eid]
        acc_received[eid] += actual_delta
        stats["correction_syncs"] += 1
        _record_sync(eid, "CORRECTION", before, acc_received[eid])
        _snapshot(f"correction-{eid}")
        entrance_db[eid].append({
            "received_at": datetime.now().strftime("%H:%M:%S"),
            "sensor_time": data.timestamp, "entrance_id": eid, "seq_num": seq,
            "delta": actual_delta, "corrupted": False,
            "estimated": False, "late_delivered": is_late,
            "is_correction": True, "anomaly": False,
        })
        _trim(entrance_db[eid])
        return {
            "message":    "Correction applied",
            "ack_num":    seq,
            "server_acc": acc_received[eid],
        }

    # ── 일반 delta 누적 ───────────────────────────────────────────────────
    acc_received[eid] += actual_delta

    entrance_db[eid].append({
        "received_at":    datetime.now().strftime("%H:%M:%S"),
        "sensor_time":    data.timestamp, "entrance_id": eid, "seq_num": seq,
        "delta":          actual_delta, "corrupted": anomaly,
        "estimated":      False, "late_delivered": is_late,
        "is_correction":  False, "anomaly": anomaly,
        "anomaly_reason": anomaly_reason,
        "gap_estimated":  False,
    })
    _trim(entrance_db[eid])
    _snapshot(f"recv-{eid}")

    total_occ = _clamp(acc_received["A"] + acc_received["B"])
    print(f"[입구 {eid}] [완료] seq={seq} delta={actual_delta:+d} | "
          f"A={acc_received['A']} B={acc_received['B']} 합계={total_occ}대")

    return {
        "message":    "Data processed successfully",
        "ack_num":    seq,
        "server_acc": acc_received[eid],
    }


# ── 엔드포인트 ────────────────────────────────────────────────────────────────
@app.get("/")
async def root():
    return {"message": "Smart Parking — Z-score + 갭추정 + 선형회귀 예측 + Checkpoint/ACK 동기화"}


@app.post("/data")
async def collect_data(data: SensorData):
    return _process(data)


@app.post("/truth")
async def store_truth(data: SensorData):
    eid = data.entrance_id if data.entrance_id in ENTRANCES else "A"
    acc_truth[eid] += data.delta
    truth_db[eid].append({
        "sensor_time": data.timestamp,
        "delta": data.delta,
        "entrance_id": eid,
    })
    _trim(truth_db[eid])
    return {"ok": True}


@app.get("/status")
async def get_status():
    has_data = any(entrance_db[eid] for eid in ENTRANCES)
    if not has_data:
        return _no_data()

    occ = _clamp(acc_received["A"] + acc_received["B"])
    entrances = {}
    for eid in ENTRANCES:
        if entrance_db[eid]:
            last = entrance_db[eid][-1]
            entrances[eid] = {**last, "occupancy": _clamp(acc_received[eid])}

    rate = occ / TOTAL_CAPACITY * 100
    prediction = _predict()

    return {
        "total_occupancy": occ,
        "total_capacity":  TOTAL_CAPACITY,
        "occupancy_rate":  f"{rate:.1f}%",
        "status":          _status(occ),
        "entrances":       entrances,
        "no_data":         False,
        "prediction":      prediction,
    }


@app.get("/history")
async def get_history():
    return entrance_db


@app.get("/stats")
async def get_stats():
    return entrance_stats


@app.get("/compare")
async def get_compare():
    return compare_history[-40:]


@app.get("/ai_stats")
async def get_ai_stats():
    result = {}
    for eid in ENTRANCES:
        result[eid] = {
            **ai_stats[eid],
            "window_size":   len(delta_window[eid]),
            "window_ready":  len(delta_window[eid]) >= ZSCORE_MIN_DATA,
            "zscore_thresh": ZSCORE_THRESH,
        }
    return result


@app.get("/sync_log")
async def get_sync_log():
    return sync_log[-50:]


@app.post("/tcp_log")
async def store_tcp_log(event: TcpEvent):
    tcp_event_log.append(event.model_dump())
    _trim(tcp_event_log, 300)
    return {"ok": True}


@app.get("/tcp_events")
async def get_tcp_events():
    return tcp_event_log[-60:]
