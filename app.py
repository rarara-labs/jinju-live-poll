"""
팀 실시간 설문 (Streamlit + Google Sheets)

- 관리자 화면 (기본 주소)        : 설문 만들기 / 링크 복사 / 마감 / 결과 공개
- 응답 화면   (주소?s=설문ID)    : 팀원이 링크 타고 들어와서 응답
- 결과 화면   (주소?s=설문ID&r=1): 결과만 보기 (공개했을 때만 보임)

secrets 에 sheet_url 이 없으면 '체험 모드'(메모리 저장)로 동작해요.
"""

import io
import json
import random
import time
import uuid
from datetime import datetime, timedelta, timezone

import altair as alt
import pandas as pd
import streamlit as st

KST = timezone(timedelta(hours=9))
SURVEY_HEADERS = ["survey_id", "title", "questions", "status", "results_public", "created_at"]
RESP_HEADERS = ["survey_id", "response_id", "submitted_at", "answers"]
TYPE_LABELS = {"single": "단일 선택", "multi": "복수 선택", "scale": "점수 (1~5)", "text": "주관식"}
REFRESH_SEC = 5

st.set_page_config(page_title="팀 설문", page_icon="🗳️", layout="centered")
st.markdown(
    """
    <style>
    .block-container {padding-top: 1.5rem; max-width: 720px;}
    div[role="radiogroup"] label {padding: 4px 0;}
    div[role="radiogroup"] label p, .stCheckbox label p {font-size: 1.05rem;}
    </style>
    """,
    unsafe_allow_html=True,
)


# ───────────────────────── 저장소 ─────────────────────────
def _retry(fn, *args, **kwargs):
    """구글 API 사용량 초과(429) 등 일시 오류 시 재시도."""
    for i in range(5):
        try:
            return fn(*args, **kwargs)
        except Exception:
            if i == 4:
                raise
            time.sleep(1.2 * (2 ** i) + random.random())


class SheetStore:
    def __init__(self):
        import gspread
        from google.oauth2.service_account import Credentials

        creds = Credentials.from_service_account_info(
            dict(st.secrets["gcp_service_account"]),
            scopes=[
                "https://www.googleapis.com/auth/spreadsheets",
                "https://www.googleapis.com/auth/drive",
            ],
        )
        self.book = gspread.authorize(creds).open_by_url(st.secrets["sheet_url"])
        self.ws_s = self._ws("surveys", SURVEY_HEADERS)
        self.ws_r = self._ws("responses", RESP_HEADERS)

    def _ws(self, name, headers):
        import gspread

        try:
            ws = self.book.worksheet(name)
        except gspread.WorksheetNotFound:
            ws = self.book.add_worksheet(title=name, rows=1000, cols=len(headers))
            ws.append_row(headers, value_input_option="RAW")
        return ws

    @staticmethod
    def _records(ws, headers):
        values = _retry(ws.get_all_values)
        rows = []
        for v in values[1:]:
            v = (v + [""] * len(headers))[: len(headers)]
            if v[0]:
                rows.append(dict(zip(headers, v)))
        return rows

    def surveys(self):
        return self._records(self.ws_s, SURVEY_HEADERS)

    def responses(self):
        return self._records(self.ws_r, RESP_HEADERS)

    def add_survey(self, row):
        _retry(self.ws_s.append_row, [row[h] for h in SURVEY_HEADERS], value_input_option="RAW")

    def add_response(self, row):
        _retry(self.ws_r.append_row, [row[h] for h in RESP_HEADERS], value_input_option="RAW")

    def update_survey(self, sid, field, value):
        ids = _retry(self.ws_s.col_values, 1)
        r = ids.index(sid) + 1
        c = SURVEY_HEADERS.index(field) + 1
        _retry(self.ws_s.update_cell, r, c, value)


class MemoryStore:
    """체험용: 앱이 재시작되면 데이터가 사라져요."""

    def __init__(self):
        self.s, self.r = [], []

    def surveys(self):
        return [dict(x) for x in self.s]

    def responses(self):
        return [dict(x) for x in self.r]

    def add_survey(self, row):
        self.s.append(dict(row))

    def add_response(self, row):
        self.r.append(dict(row))

    def update_survey(self, sid, field, value):
        for x in self.s:
            if x["survey_id"] == sid:
                x[field] = value


def is_demo():
    try:
        return "sheet_url" not in st.secrets
    except Exception:
        return True


@st.cache_resource
def get_store():
    return MemoryStore() if is_demo() else SheetStore()


# 여러 사람이 동시에 새로고침해도 시트는 몇 초에 한 번만 읽도록 공용 캐시
@st.cache_data(ttl=REFRESH_SEC, show_spinner=False)
def load_surveys():
    return get_store().surveys()


@st.cache_data(ttl=REFRESH_SEC, show_spinner=False)
def load_responses():
    return get_store().responses()


def refresh_cache():
    load_surveys.clear()
    load_responses.clear()


def find_survey(sid):
    return next((s for s in load_surveys() if s["survey_id"] == sid), None)


def now_kst():
    return datetime.now(KST).strftime("%Y-%m-%d %H:%M:%S")


def base_url():
    try:
        url = st.secrets.get("app_url", "")
    except Exception:
        url = ""
    if not url:
        try:
            url = st.context.url or ""
        except Exception:
            url = ""
    return url.split("?")[0].rstrip("/")


def survey_link(sid, result=False):
    return f"{base_url()}/?s={sid}" + ("&r=1" if result else "")


# ───────────────────────── 결과 ─────────────────────────
def bar_chart(options, counts, total):
    df = pd.DataFrame({"보기": options, "응답수": counts})
    df["라벨"] = [f"{n}명 ({(n / total if total else 0):.0%})" for n in counts]
    base = alt.Chart(df).encode(
        y=alt.Y("보기:N", sort=options, title=None, axis=alt.Axis(labelLimit=220, labelFontSize=13)),
        x=alt.X("응답수:Q", title=None, axis=alt.Axis(tickMinStep=1),
                scale=alt.Scale(domain=[0, max(max(counts, default=0) * 1.35, 1)])),
    )
    chart = base.mark_bar(color="#3b6fd8", cornerRadiusEnd=4) + base.mark_text(
        align="left", dx=6, fontSize=13
    ).encode(text="라벨:N")
    return chart.properties(height=44 * len(options) + 10)


def render_results(survey, responses):
    questions = json.loads(survey["questions"])
    answers = [json.loads(r["answers"]) for r in responses]
    st.metric("총 응답", f"{len(answers)}명")
    for i, q in enumerate(questions):
        key = str(i)
        vals = [a.get(key) for a in answers if a.get(key) not in (None, "", [])]
        st.markdown(f"#### {i + 1}. {q['q']}")
        if q["type"] in ("single", "multi"):
            opts = q["options"]
            flat = [x for v in vals for x in (v if isinstance(v, list) else [v])]
            counts = [flat.count(o) for o in opts]
            st.altair_chart(bar_chart(opts, counts, len(vals)), width="stretch")
            if q["type"] == "multi":
                st.caption("복수 선택 · 비율은 이 문항 응답자 기준")
        elif q["type"] == "scale":
            nums = [int(v) for v in vals]
            opts = ["1", "2", "3", "4", "5"]
            counts = [nums.count(int(o)) for o in opts]
            avg = sum(nums) / len(nums) if nums else 0
            st.markdown(f"평균 **{avg:.1f}점** / 5점")
            st.altair_chart(bar_chart(opts, counts, len(nums)), width="stretch")
        else:
            if not vals:
                st.caption("아직 응답이 없어요.")
            for v in reversed(vals):
                st.info(v)


@st.fragment(run_every=REFRESH_SEC)
def live_results(sid, admin=False):
    survey = find_survey(sid)
    if not survey:
        return
    if not admin and survey["results_public"] != "Y":
        st.info("🔒 결과는 진행자가 공개하면 이 화면에 자동으로 나타나요.")
        return
    rows = [r for r in load_responses() if r["survey_id"] == sid]
    render_results(survey, rows)
    # 마감 + 공개된 최종 결과는 응답자도 이미지로 저장 가능
    if not admin and survey["status"] != "open" and rows:
        st.download_button("🖼 결과 이미지 저장", results_png_for(survey, rows),
                           file_name=f"{survey['title']}_결과.png", mime="image/png",
                           width="stretch", key=f"png_pub_{sid}")


# ───────────────────────── 결과 이미지 (PNG) ─────────────────────────
def _setup_korean_font():
    import os
    import matplotlib
    matplotlib.use("Agg")
    from matplotlib import font_manager, rcParams

    candidates = [
        "/usr/share/fonts/truetype/nanum/NanumGothic.ttf",  # Streamlit Cloud (packages.txt: fonts-nanum)
        "C:/Windows/Fonts/malgun.ttf",                       # 윈도우 로컬 실행
        "/System/Library/Fonts/AppleSDGothicNeo.ttc",        # 맥
    ]
    candidates += [f for f in font_manager.findSystemFonts()
                   if any(k in f for k in ("Nanum", "malgun", "NotoSansCJK", "NotoSansKR"))]
    for path in candidates:
        if os.path.exists(path):
            font_manager.fontManager.addfont(path)
            rcParams["font.family"] = font_manager.FontProperties(fname=path).get_name()
            break
    rcParams["axes.unicode_minus"] = False


@st.cache_data(show_spinner=False, max_entries=30)
def make_results_png(title, created_at, status, questions_json, answers_jsons):
    import textwrap
    import matplotlib.pyplot as plt

    _setup_korean_font()
    questions = json.loads(questions_json)
    answers = [json.loads(a) for a in answers_jsons]
    BLUE, GRAY, INK = "#3b6fd8", "#e9edf5", "#1f2430"
    MAX_TEXT = 10

    # 문항별 높이(인치) 계산
    blocks = []
    for i, q in enumerate(questions):
        vals = [a.get(str(i)) for a in answers if a.get(str(i)) not in (None, "", [])]
        if q["type"] in ("single", "multi"):
            h = 0.9 + 0.48 * len(q["options"])
        elif q["type"] == "scale":
            h = 0.9 + 0.48 * 5
        else:
            h = 0.9 + 0.38 * max(1, min(len(vals), MAX_TEXT) + (len(vals) > MAX_TEXT))
        blocks.append((q, vals, h))

    header_h = 1.3
    fig_h = header_h + sum(b[2] for b in blocks)
    fig = plt.figure(figsize=(8, fig_h), facecolor="white")
    gs = fig.add_gridspec(len(blocks) + 1, 1, height_ratios=[header_h] + [b[2] for b in blocks], hspace=0.45)

    ax = fig.add_subplot(gs[0]); ax.axis("off")
    ax.text(0, 0.72, title, fontsize=20, fontweight="bold", color=INK, transform=ax.transAxes)
    state = "최종 결과" if status != "open" else "중간 결과"
    ax.text(0, 0.25, f"{state} · 총 응답 {len(answers)}명 · {created_at[:10]}",
            fontsize=11, color="#6b7280", transform=ax.transAxes)

    for n, (q, vals, _) in enumerate(blocks, start=1):
        ax = fig.add_subplot(gs[n])
        qtitle = "\n".join(textwrap.wrap(f"{n}. {q['q']}", 40))
        if q["type"] in ("single", "multi", "scale"):
            if q["type"] == "scale":
                opts = ["1점", "2점", "3점", "4점", "5점"]
                nums = [int(v) for v in vals]
                counts = [nums.count(k) for k in range(1, 6)]
                if nums:
                    qtitle += f"   (평균 {sum(nums) / len(nums):.1f}점)"
            else:
                opts = q["options"]
                flat = [x for v in vals for x in (v if isinstance(v, list) else [v])]
                counts = [flat.count(o) for o in opts]
            total = len(vals)
            top = max(counts, default=0)
            y = list(range(len(opts)))[::-1]
            ax.barh(y, [max(top, 1) * 1.0] * len(opts), color=GRAY, height=0.62)
            ax.barh(y, counts, height=0.62,
                    color=[BLUE if c == top and c > 0 else "#9db5ea" for c in counts])
            for yy, c in zip(y, counts):
                pct = c / total if total else 0
                ax.text(max(top, 1) * 1.02, yy, f"{c}명 ({pct:.0%})", va="center", fontsize=11, color=INK)
            ax.set_yticks(y, ["\n".join(textwrap.wrap(o, 14)) for o in opts], fontsize=11)
            ax.set_xlim(0, max(top, 1) * 1.3)
            ax.set_xticks([])
            for s in ax.spines.values():
                s.set_visible(False)
            ax.tick_params(left=False)
        else:
            ax.axis("off")
            shown = vals[-MAX_TEXT:][::-1]
            if not shown:
                ax.text(0, 0.5, "응답 없음", fontsize=11, color="#9ca3af", transform=ax.transAxes)
            rows = len(shown) + (len(vals) > MAX_TEXT)
            for k, v in enumerate(shown):
                line = textwrap.shorten(str(v).replace("\n", " "), 60, placeholder="…")
                ax.text(0.01, 1 - (k + 0.5) / max(rows, 1), f"• {line}", fontsize=11,
                        color=INK, va="center", transform=ax.transAxes)
            if len(vals) > MAX_TEXT:
                ax.text(0.01, 0.5 / rows, f"… 외 {len(vals) - MAX_TEXT}건 (전체는 CSV 참고)",
                        fontsize=10, color="#6b7280", va="center", transform=ax.transAxes)
        ax.set_title(qtitle, loc="left", fontsize=13, fontweight="bold", color=INK, pad=10)

    buf = io.BytesIO()
    fig.savefig(buf, format="png", dpi=200, bbox_inches="tight", pad_inches=0.4, facecolor="white")
    plt.close(fig)
    return buf.getvalue()


def results_png_for(survey, responses):
    return make_results_png(survey["title"], survey["created_at"], survey["status"],
                            survey["questions"], tuple(r["answers"] for r in responses))


def results_csv(survey, responses):
    questions = json.loads(survey["questions"])
    out = []
    for r in responses:
        a = json.loads(r["answers"])
        row = {"제출시각": r["submitted_at"]}
        for i, q in enumerate(questions):
            v = a.get(str(i), "")
            row[f"{i + 1}. {q['q']}"] = ", ".join(v) if isinstance(v, list) else v
        out.append(row)
    return pd.DataFrame(out).to_csv(index=False).encode("utf-8-sig")


# ───────────────────────── 응답 화면 ─────────────────────────
def page_vote(sid, result_only):
    survey = find_survey(sid)
    if not survey:
        st.error("설문을 찾을 수 없어요. 링크를 다시 확인해 주세요.")
        return
    st.title(survey["title"])
    done_key = f"done_{sid}"

    if st.session_state.get(done_key) or result_only or survey["status"] != "open":
        if st.session_state.get(done_key):
            st.success("✅ 응답이 제출됐어요. 감사합니다!")
        elif survey["status"] != "open" and not result_only:
            st.warning("응답이 마감된 설문이에요.")
        st.divider()
        live_results(sid)
        return

    questions = json.loads(survey["questions"])
    with st.form("vote_form"):
        answers = {}
        for i, q in enumerate(questions):
            mark = " <span style='color:#e5484d'>*</span>" if q.get("required") else ""
            label = f"**{i + 1}. {q['q']}**"
            if q["type"] == "single":
                answers[str(i)] = st.radio(label + (" *" if q.get("required") else ""),
                                           q["options"], index=None, key=f"q{i}")
            elif q["type"] == "multi":
                st.markdown(label + mark + " <small>(여러 개 선택 가능)</small>", unsafe_allow_html=True)
                answers[str(i)] = [o for j, o in enumerate(q["options"])
                                   if st.checkbox(o, key=f"q{i}_{j}")]
            elif q["type"] == "scale":
                v = st.radio(label + (" *" if q.get("required") else ""), [1, 2, 3, 4, 5],
                             index=None, horizontal=True, key=f"q{i}")
                st.caption("1 = 전혀 아니다 · 5 = 매우 그렇다")
                answers[str(i)] = v
            else:
                answers[str(i)] = st.text_area(label + (" *" if q.get("required") else ""),
                                               key=f"q{i}").strip()
            st.write("")
        submitted = st.form_submit_button("제출하기", type="primary", width="stretch")

    if submitted:
        missing = [str(i + 1) for i, q in enumerate(questions)
                   if q.get("required") and answers[str(i)] in (None, "", [])]
        if missing:
            st.error(f"필수 문항에 답해 주세요: {', '.join(missing)}번")
            return
        # 마감 직후 제출 방지
        refresh_cache()
        latest = find_survey(sid)
        if not latest or latest["status"] != "open":
            st.warning("방금 응답이 마감됐어요.")
            return
        get_store().add_response({
            "survey_id": sid,
            "response_id": uuid.uuid4().hex[:12],
            "submitted_at": now_kst(),
            "answers": json.dumps(answers, ensure_ascii=False),
        })
        refresh_cache()
        st.session_state[done_key] = True
        st.rerun()


# ───────────────────────── 관리자 화면 ─────────────────────────
def admin_password():
    try:
        return st.secrets.get("admin_password", "admin" if is_demo() else None)
    except Exception:
        return "admin"


def page_login():
    st.title("🗳️ 팀 설문 관리")
    pw = st.text_input("관리자 비밀번호", type="password")
    if st.button("들어가기", type="primary"):
        if pw and pw == admin_password():
            st.session_state["admin"] = True
            st.rerun()
        else:
            st.error("비밀번호가 맞지 않아요.")
    if is_demo():
        st.caption("체험 모드 · 비밀번호: admin (데이터는 앱이 재시작되면 사라져요)")


def tab_create():
    last = st.session_state.get("last_created")
    if last:
        st.success("설문이 만들어졌어요! 이 링크를 팀 채팅방에 보내 주세요.")
        st.code(survey_link(last), language=None)
        st.caption("마감·결과 공개는 '📋 설문 관리' 탭에서 할 수 있어요.")
        st.divider()

    ver = st.session_state.setdefault("form_ver", 0)
    k = lambda name: f"c{ver}_{name}"

    title = st.text_input("설문 제목", placeholder="예) 11월 팀 회의 설문", key=k("title"))
    n = st.number_input("문항 수", min_value=1, max_value=20, value=1, step=1, key=k("n"))

    questions = []
    for i in range(int(n)):
        with st.container(border=True):
            q = st.text_input(f"{i + 1}번 질문", key=k(f"q{i}"))
            c1, c2 = st.columns([2, 1])
            qtype = c1.selectbox("유형", list(TYPE_LABELS), format_func=TYPE_LABELS.get, key=k(f"t{i}"))
            req = c2.checkbox("필수", value=True, key=k(f"r{i}"))
            opts = []
            if qtype in ("single", "multi"):
                raw = st.text_area("보기 (한 줄에 하나씩)", key=k(f"o{i}"),
                                   placeholder="찬성\n반대\n잘 모르겠음", height=110)
                opts = [o.strip() for o in raw.splitlines() if o.strip()]
            questions.append({"q": q.strip(), "type": qtype, "options": opts, "required": req})

    if st.button("설문 만들기", type="primary", width="stretch"):
        errors = []
        if not title.strip():
            errors.append("설문 제목을 입력해 주세요.")
        for i, q in enumerate(questions):
            if not q["q"]:
                errors.append(f"{i + 1}번 질문을 입력해 주세요.")
            if q["type"] in ("single", "multi") and len(q["options"]) < 2:
                errors.append(f"{i + 1}번 보기를 2개 이상 입력해 주세요.")
            if len(set(q["options"])) != len(q["options"]):
                errors.append(f"{i + 1}번 보기에 중복이 있어요.")
        if errors:
            for e in errors:
                st.error(e)
            return
        sid = "s" + uuid.uuid4().hex[:7]
        get_store().add_survey({
            "survey_id": sid,
            "title": title.strip(),
            "questions": json.dumps(questions, ensure_ascii=False),
            "status": "open",
            "results_public": "N",
            "created_at": now_kst(),
        })
        refresh_cache()
        st.session_state["form_ver"] = ver + 1
        st.session_state["selected"] = sid
        st.session_state["just_created"] = sid
        st.session_state["last_created"] = sid
        st.rerun()


def tab_manage():
    surveys = sorted(load_surveys(), key=lambda s: s["created_at"], reverse=True)
    if not surveys:
        st.info("아직 만든 설문이 없어요. '새 설문' 탭에서 만들어 보세요.")
        return

    ids = [s["survey_id"] for s in surveys]
    sel = st.session_state.get("selected")
    idx = ids.index(sel) if sel in ids else 0
    names = {s["survey_id"]: f"{s['title']}  ·  {s['created_at'][:16]}" for s in surveys}
    sid = st.selectbox("설문 선택", ids, index=idx, format_func=names.get)
    st.session_state["selected"] = sid
    survey = find_survey(sid)
    responses = [r for r in load_responses() if r["survey_id"] == sid]

    if st.session_state.pop("just_created", None) == sid:
        st.success("설문이 만들어졌어요! 아래 링크를 팀 채팅방에 보내 주세요.")

    st.markdown("**응답 링크** (오른쪽 위 아이콘으로 복사)")
    st.code(survey_link(sid), language=None)

    is_open = survey["status"] == "open"
    is_public = survey["results_public"] == "Y"
    c1, c2, c3 = st.columns(3)
    c1.metric("응답", f"{len(responses)}명")
    c2.metric("상태", "응답 중" if is_open else "마감")
    c3.metric("결과", "공개" if is_public else "비공개")

    b1, b2 = st.columns(2)
    if b1.button("⏹ 응답 마감" if is_open else "▶ 응답 다시 받기", width="stretch"):
        get_store().update_survey(sid, "status", "closed" if is_open else "open")
        refresh_cache()
        st.rerun()
    if b2.button("🔒 결과 숨기기" if is_public else "📢 결과 공개하기",
                 type="secondary" if is_public else "primary", width="stretch"):
        get_store().update_survey(sid, "results_public", "N" if is_public else "Y")
        refresh_cache()
        st.rerun()

    if is_public:
        st.caption("공개 중이에요. 응답한 사람 화면에 결과가 자동으로 나타나요. 결과만 보는 링크:")
        st.code(survey_link(sid, result=True), language=None)

    with st.expander("QR코드 (필요할 때만)"):
        try:
            import qrcode

            img = qrcode.make(survey_link(sid))
            buf = io.BytesIO()
            img.save(buf, format="PNG")
            st.image(buf.getvalue(), width=260)
        except Exception:
            st.caption("qrcode 패키지가 없어요. requirements.txt를 확인해 주세요.")

    if responses:
        if not is_open:
            st.success("투표가 마감됐어요. 아래 버튼으로 결과 이미지를 저장해 두세요!")
        d1, d2 = st.columns(2)
        d1.download_button("🖼 결과 이미지 (PNG)", results_png_for(survey, responses),
                           file_name=f"{survey['title']}_결과.png", mime="image/png",
                           type="primary" if not is_open else "secondary", width="stretch")
        d2.download_button("📥 응답 엑셀 (CSV)", results_csv(survey, responses),
                           file_name=f"{survey['title']}_응답.csv", mime="text/csv",
                           width="stretch")

    st.divider()
    st.subheader("실시간 결과 (관리자만 보기)")
    live_results(sid, admin=True)


def page_admin():
    top = st.columns([4, 1])
    top[0].title("🗳️ 팀 설문 관리")
    if top[1].button("로그아웃"):
        st.session_state["admin"] = False
        st.rerun()
    if is_demo():
        st.warning("체험 모드예요. 앱이 재시작되면 설문이 사라져요. (secrets에 sheet_url 설정 시 시트에 저장)")
    t_manage, t_create = st.tabs(["📋 설문 관리", "➕ 새 설문"])
    with t_create:
        tab_create()
    with t_manage:
        tab_manage()


# ───────────────────────── 라우팅 ─────────────────────────
qp = st.query_params
if qp.get("s"):
    page_vote(qp.get("s"), qp.get("r") == "1")
elif st.session_state.get("admin"):
    page_admin()
else:
    page_login()
