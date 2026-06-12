import streamlit as st
import requests
import pandas as pd
import time

st.set_page_config(page_title="Smart Parking Dashboard", layout="wide")

# ── 사이드바 ───────────────────────────────────────────────────────────────
with st.sidebar:
    st.title("실행 방법")
    st.code("uvicorn src.main:app --reload",         language="bash")
    st.code("streamlit run dashboard/app.py",         language="bash")
    st.code("python data/sensor_simulation.py",       language="bash")
    st.caption("센서는 A·B를 항상 동시에 전송합니다.")

# ── 데이터 로드 ────────────────────────────────────────────────────────────
try:
    data       = requests.get("http://localhost:8000/status",    timeout=3).json()
    tcp        = requests.get("http://localhost:8000/stats",     timeout=3).json()
    compare    = requests.get("http://localhost:8000/compare",   timeout=3).json()
    history    = requests.get("http://localhost:8000/history",   timeout=3).json()
    tcp_events = requests.get("http://localhost:8000/tcp_events",timeout=3).json()
    ai_data    = requests.get("http://localhost:8000/ai_stats",  timeout=3).json()
    sync_data  = requests.get("http://localhost:8000/sync_log",  timeout=3).json()
except requests.exceptions.ConnectionError:
    st.title("스마트 주차장 실시간 모니터링")
    st.error("FastAPI 서버에 연결할 수 없습니다. `uvicorn src.main:app --reload` 를 먼저 실행하세요.")
    time.sleep(2); st.rerun(); st.stop()
except Exception as e:
    st.title("스마트 주차장 실시간 모니터링")
    st.error(f"데이터 로드 오류: {e}")
    time.sleep(2); st.rerun(); st.stop()

try:
    if data.get("no_data"):
        st.title("스마트 주차장 실시간 모니터링")
        st.warning("데이터가 아직 없습니다. `python data/sensor_simulation.py` 를 실행해 주세요.")
        time.sleep(2); st.rerun(); st.stop()

    # ── 헤더 ───────────────────────────────────────────────────────────────
    st.title("스마트 주차장 실시간 모니터링")
    st.caption("입구 A+B 합산 | TCP 시뮬 + Z-score 이상치 탐지 + 갭 추정 복구 + 선형 회귀 예측")

    # ── 전체 주차 현황 ─────────────────────────────────────────────────────
    st.subheader("전체 주차 현황")
    pred = data.get("prediction", {})

    c1, c2, c3, c4 = st.columns(4)
    c1.metric("현재 주차 대수", f"{data['total_occupancy']} / {data['total_capacity']} 대")
    c2.metric("점유율", data["occupancy_rate"])

    if pred.get("ready"):
        p_stat = pred["predicted_status"]
        trend  = pred["trend"]
        icon   = pred["trend_icon"]

        stat_label = {"Available": "🟢 Available", "Warning": "🟡 Warning", "Full": "🔴 Full"}
        c3.metric("약 3분 후 예측 상태", stat_label.get(p_stat, p_stat))
        c4.metric("현재 추세", f"{icon} {trend}")
    else:
        c3.metric("약 3분 후 예측 상태", "수집 중...")
        c4.metric("현재 추세", "수집 중...")

    s = data["status"]
    if s == "Available": st.success(f"현재 상태: {s}")
    elif s == "Warning":  st.warning(f"현재 상태: {s}")
    else:                 st.error(f"현재 상태: {s}")
    st.progress(int(float(data["occupancy_rate"].replace("%", ""))) / 100)

    # 예측 상태 변화 경고 배너
    if pred.get("ready") and pred["predicted_status"] != data["status"]:
        if pred["predicted_status"] == "Full":
            st.error("⚠️ 약 3분 후 **Full** 상태로 전환될 것으로 예측됩니다.")
        elif pred["predicted_status"] == "Warning":
            st.warning("⚠️ 약 3분 후 **Warning** 상태로 전환될 것으로 예측됩니다.")
        elif pred["predicted_status"] == "Available":
            st.success("✅ 약 3분 후 **Available** 상태로 전환될 것으로 예측됩니다.")

    # ── 비교 차트 ─────────────────────────────────────────────────────────
    st.subheader("실제 점유 vs 서버 수신값 비교")
    if compare:
        df_cmp = pd.DataFrame(compare).set_index("idx")
        st.caption(
            "**파란 선**: 실제 측정값 (A+B)  |  "
            "**주황 선**: 서버 수신값 (A+B, Z-score 필터 + 갭 추정 복구 적용)"
        )
        chart_df = df_cmp[["truth_ab", "received_ab"]].rename(
            columns={"truth_ab": "실제 점유 (A+B)", "received_ab": "서버 수신값 (A+B)"}
        )
        st.line_chart(chart_df, color=["#2196F3", "#FF5722"])
    else:
        st.info("비교 데이터 수집 중...")

except Exception as e:
    st.error(f"렌더링 오류: {e}")

# ── 동기화 보정 로그 ──────────────────────────────────────────────────────
st.subheader("동기화 보정 로그")
try:
    if sync_data:
        sync_rows = []
        for e in reversed(sync_data):
            t = e.get("type", "")
            icon = "📌" if t == "CHECKPOINT" else "⚡"
            sync_rows.append({
                "":        icon,
                "시각":    e.get("time", "-"),
                "입구":    e.get("entrance_id", "-"),
                "유형":    t,
                "보정 전": e.get("before", "-"),
                "보정 후": e.get("after", "-"),
                "보정량":  f"{e.get('diff', 0):+d}",
            })
        st.dataframe(pd.DataFrame(sync_rows), use_container_width=True)
    else:
        st.info("보정 이력 수집 중...")
except Exception as e:
    st.error(f"동기화 로그 렌더링 오류: {e}")

# ── TCP 이벤트 로그 ───────────────────────────────────────────────────────
st.subheader("TCP 레이어 이벤트 로그")
try:
    EVENT_ICON = {
        "전송시작":   ("🔵", "전송 시작"),
        "패킷손실":   ("🔴", "패킷 손실"),
        "재전송":     ("🟡", "재전송 시도"),
        "데이터손상": ("🟠", "데이터 손상"),
        "ACK수신":    ("🟢", "ACK 수신"),
        "전송포기":   ("⛔", "전송 포기"),
    }
    if tcp_events:
        rows = []
        for e in reversed(tcp_events):
            icon, label = EVENT_ICON.get(e.get("event_type", ""), ("⬜", e.get("event_type", "-")))
            rows.append({
                "":       icon,
                "시각":   e.get("time", "-"),
                "입구":   e.get("entrance_id", "-"),
                "seq":    e.get("seq", "-"),
                "이벤트": label,
                "상세":   e.get("detail", ""),
            })
        st.dataframe(pd.DataFrame(rows), use_container_width=True)
    else:
        st.info("TCP 이벤트 수집 중...")
except Exception as e:
    st.error(f"TCP 로그 렌더링 오류: {e}")

# ── 서버 수신 로그 ────────────────────────────────────────────────────────
st.subheader("서버 수신 로그")
try:
    all_events = []
    for eid in ["A", "B"]:
        all_events.extend(history.get(eid, []))

    if all_events:
        all_events.sort(key=lambda x: x.get("seq_num") or 0, reverse=True)
        rows = []
        for e in all_events:
            if e.get("gap_estimated"):
                tag, status = "🔴", f"손실 → 갭 추정 복구 (delta={e.get('delta', 0):+d})"
            elif e.get("estimated"):
                tag, status = "🔴", "손실 → delta=0 추정"
            elif e.get("anomaly"):
                tag, status = "🤖", "Z-score 이상치 → delta=0"
            elif e.get("corrupted"):
                tag, status = "🟠", "손상 → delta=0 복구"
            elif e.get("is_correction"):
                tag, status = "⚡", "ACK 보정 패킷"
            elif e.get("late_delivered"):
                tag, status = "🟡", "지연 도착"
            else:
                tag, status = "🟢", "정상"
            rows.append({
                "":        tag,
                "수신 시각": e.get("received_at", "-"),
                "입구":    e.get("entrance_id", "-"),
                "seq":     e.get("seq_num", "-"),
                "delta":   f"{e.get('delta', 0):+d}",
                "상태":    status,
            })
        st.dataframe(pd.DataFrame(rows), use_container_width=True)
    else:
        st.info("수신 로그 수집 중... (sensor_simulation.py 실행 확인)")
except Exception as e:
    st.error(f"수신 로그 렌더링 오류: {e}")

time.sleep(2)
st.rerun()
