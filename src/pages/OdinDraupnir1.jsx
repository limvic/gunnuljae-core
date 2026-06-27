import React, { useState, useEffect } from "react";

/*
 * 오딘의 드라우프니르 — ODIN'S DRAUPNIR v0.1
 * Trinity 상위 자동 "판단" 대시보드 (자동주문 아님 / 알림+점수+진입계획+리스크)
 *
 * v0.1 노트
 * - 실제 API 미연동. 아래 MOCK 한 곳에서만 데이터를 읽음 → 나중에 fetch로 교체.
 * - 데이터 없을 땐 "준비중" 원칙 유지 (가짜 숫자 착시 금지 → 화면 전체 DEMO 배지).
 * - RUNE TIME은 실시간(2026). 목업의 2025 박제 날짜 폐기.
 *
 * 단일 파일 안에서 컴포넌트를 이름별로 분리해 둠. 나중에 그대로 쪼개면 됨:
 *   OdinLayout / MarketStatusCard / MarketScanCard / SectorStrengthCard /
 *   RuneScoreCard / TopRuneStocksCard / GungnirSignalCard / JudgeVerdictCard /
 *   DraupnirVaultCard / RiskManagementCard / VavlogCard
 */

/* ───────────────────────── 디자인 토큰 (림빅 지정) ───────────────────────── */
const C = {
  bg: "#050608",
  gold: "#d6a84f",
  goldDim: "rgba(214,168,79,0.55)",
  goldFaint: "rgba(214,168,79,0.12)",
  up: "#22c55e",
  down: "#ef4444",
  card: "#0d1117",
  border: "rgba(214,168,79,0.35)",
  borderSoft: "rgba(214,168,79,0.18)",
  text: "#e8e4d8",
  textDim: "#8a8578",
  panel: "#0a0d12",
};

const DISPLAY = "'Cinzel','Trajan Pro',Georgia,serif";
const MONO = "'JetBrains Mono','SFMono-Regular',Menlo,Consolas,monospace";
const BODY = "-apple-system,'Apple SD Gothic Neo','Noto Sans KR',sans-serif";

/* ───────────────────────── MOCK DATA (교체 지점 단일) ───────────────────────── */
const MOCK = {
  scan: {
    kospi: 0.64, kosdaq: 1.12, value: "2.87조",
    sectorStrength: "2.4 / 5", mood: "BULLISH",
    theme: "2건 감지", news: "12건 수집",
  },
  sectors: [
    { name: "2차전지", score: 87 }, { name: "반도체", score: 82 },
    { name: "AI / 로봇", score: 78 }, { name: "바이오", score: 63 },
    { name: "게임", score: 58 }, { name: "자동차", score: 55 },
    { name: "조선", score: 48 }, { name: "금융", score: 40 },
  ],
  rune: {
    name: "삼성전자", code: "005930", total: 86, tier: "S",
    parts: [
      { k: "거래량", v: 18, max: 20 }, { k: "추세", v: 18, max: 20 },
      { k: "섹터", v: 13, max: 15 }, { k: "재무", v: 13, max: 15 },
      { k: "위치", v: 12, max: 15 }, { k: "수급", v: 9, max: 10 },
      { k: "리스크", v: 3, max: 5 },
    ],
  },
  top: [
    { r: 1, name: "삼성전자", score: 86 }, { r: 2, name: "SK하이닉스", score: 83 },
    { r: 3, name: "LG에너지솔루션", score: 81 }, { r: 4, name: "네이버", score: 78 },
    { r: 5, name: "카카오", score: 76 }, { r: 6, name: "현대차", score: 74 },
    { r: 7, name: "POSCO홀딩스", score: 72 }, { r: 8, name: "삼성SDI", score: 70 },
  ],
  gungnir: {
    name: "삼성전자", code: "005930", side: "LONG", status: "READY",
    entry: 78900, target: 85500, rr: 2.1, stop: 76200, stopPct: -3.4,
    checks: [
      { k: "시장 ON", ok: true }, { k: "섹터 ON", ok: true },
      { k: "룬 점수 80+", ok: true }, { k: "패턴 일치", ok: true },
      { k: "손절가 명확", ok: true }, { k: "R/R 2.0+", ok: true },
    ],
  },
  judge: {
    grade: "A+", label: "EXCELLENT",
    win: 83.7, expect: 4.02, risk: "LOW",
    memSimilar: 23, memWin: 78.3,
  },
  vault: {
    day: 4, cycle: 9,
    multiple: "1.1845x",   // DEMO — 실수익 아님
    note: "9거래일 단위 성과 복기",
  },
  risk: {
    daily: 1.25, dailyLimit: -2.0,
    positions: 3, maxPositions: 10,
    cash: 42, gauge: 24, level: "SAFE",
  },
  vavlog: [
    { t: "10:30:15", name: "삼성전자", act: "매수", res: "보유중", pct: 2.15 },
    { t: "09:45:22", name: "SK하이닉스", act: "매수", res: "보유중", pct: 1.82 },
    { t: "09:20:10", name: "LG에너지솔루션", act: "매수", res: "보유중", pct: -0.45 },
    { t: "어제", name: "현대차", act: "매도", res: "익절", pct: 3.21 },
    { t: "어제", name: "POSCO홀딩스", act: "매도", res: "익절", pct: 2.87 },
  ],
};

/* ───────────────────────── 공통 UI ───────────────────────── */
function Card({ title, sub, right, children, span, glow }) {
  return (
    <section
      className="odin-card"
      style={{
        gridColumn: span ? `span ${span}` : undefined,
        background: C.card,
        border: `1px solid ${glow ? C.gold : C.borderSoft}`,
        borderRadius: 14,
        padding: "16px 18px",
        boxShadow: glow ? `0 0 0 1px ${C.goldFaint}, 0 0 24px rgba(214,168,79,0.12)` : "none",
        display: "flex", flexDirection: "column", gap: 12, minWidth: 0,
      }}
    >
      <header style={{ display: "flex", alignItems: "baseline", justifyContent: "space-between", gap: 8 }}>
        <div>
          <h3 style={{ margin: 0, font: `600 13px/1.2 ${DISPLAY}`, letterSpacing: "0.14em", color: C.gold, textTransform: "uppercase" }}>{title}</h3>
          {sub && <p style={{ margin: "3px 0 0", font: `400 11px/1.2 ${BODY}`, color: C.textDim }}>{sub}</p>}
        </div>
        {right}
      </header>
      {children}
    </section>
  );
}

const pct = (n) => `${n > 0 ? "+" : ""}${n.toFixed(2)}%`;
const won = (n) => `₩${n.toLocaleString()}`;
const upDown = (n) => (n >= 0 ? C.up : C.down);

/* ───────────────────────── MarketStatusCard ───────────────────────── */
function MarketStatusCard({ mode }) {
  const bull = true;
  return (
    <Card title="Market Status" sub="시장 상태">
      <div style={{ display: "flex", alignItems: "center", gap: 14 }}>
        <div style={{ font: `700 30px/1 ${DISPLAY}`, color: bull ? C.up : C.down, letterSpacing: "0.04em" }}>
          {bull ? "BULL" : "BEAR"}
        </div>
        <span style={{ fontSize: 26 }}>{bull ? "🐂" : "🐻"}</span>
      </div>
      <p style={{ margin: 0, font: `400 12px/1.4 ${BODY}`, color: C.textDim }}>
        {mode === "CASH" ? "현금 모드 — 신규 진입 보류" : "코스피 강세 흐름 감지"}
      </p>
      <div style={{ display: "flex", gap: 8, flexWrap: "wrap" }}>
        <Pill label="모드" value={mode} tone={mode === "CASH" ? C.down : C.up} />
        <Pill label="현금" value={`${MOCK.risk.cash}%`} tone={C.gold} />
      </div>
    </Card>
  );
}
function Pill({ label, value, tone }) {
  return (
    <span style={{ display: "inline-flex", gap: 6, alignItems: "center", padding: "4px 10px", borderRadius: 999, background: C.goldFaint, border: `1px solid ${C.borderSoft}` }}>
      <span style={{ font: `400 10px ${BODY}`, color: C.textDim }}>{label}</span>
      <span style={{ font: `600 11px ${MONO}`, color: tone || C.text }}>{value}</span>
    </span>
  );
}

/* ───────────────────────── MarketScanCard (Huginn) ───────────────────────── */
function MarketScanCard() {
  const s = MOCK.scan;
  const Row = ({ k, v, tone }) => (
    <div style={{ display: "flex", justifyContent: "space-between", padding: "6px 0", borderBottom: `1px solid ${C.borderSoft}` }}>
      <span style={{ font: `400 12px ${BODY}`, color: C.textDim }}>{k}</span>
      <span style={{ font: `600 12px ${MONO}`, color: tone || C.text }}>{v}</span>
    </div>
  );
  return (
    <Card title="Market Scan" sub="Huginn · 생각 까마귀">
      <div>
        <Row k="KOSPI" v={pct(s.kospi)} tone={upDown(s.kospi)} />
        <Row k="KOSDAQ" v={pct(s.kosdaq)} tone={upDown(s.kosdaq)} />
        <Row k="거래대금" v={s.value} />
        <Row k="섹터 강도" v={s.sectorStrength} tone={C.gold} />
        <Row k="시장 분위기" v={s.mood} tone={C.up} />
        <Row k="특이 테마" v={s.theme} tone={C.gold} />
        <Row k="뉴스 분석" v={s.news} />
      </div>
    </Card>
  );
}

/* ───────────────────────── SectorStrengthCard ───────────────────────── */
function SectorStrengthCard() {
  return (
    <Card title="Sector Strength" sub="섹터 강도">
      <div style={{ display: "flex", flexDirection: "column", gap: 9 }}>
        {MOCK.sectors.map((s) => (
          <div key={s.name} style={{ display: "grid", gridTemplateColumns: "78px 1fr 28px", alignItems: "center", gap: 8 }}>
            <span style={{ font: `400 11px ${BODY}`, color: C.text }}>{s.name}</span>
            <div style={{ height: 6, borderRadius: 3, background: C.goldFaint, overflow: "hidden" }}>
              <div style={{ width: `${s.score}%`, height: "100%", borderRadius: 3, background: `linear-gradient(90deg, rgba(214,168,79,0.45), ${C.gold})` }} />
            </div>
            <span style={{ font: `600 11px ${MONO}`, color: C.gold, textAlign: "right" }}>{s.score}</span>
          </div>
        ))}
      </div>
    </Card>
  );
}

/* ───────────────────────── RuneScoreCard ───────────────────────── */
function RuneScoreCard() {
  const r = MOCK.rune;
  return (
    <Card title="Rune Score" sub="룬 점수 분석" right={<Tier t={r.tier} />}>
      <div style={{ display: "flex", alignItems: "baseline", gap: 10 }}>
        <span style={{ font: `700 40px/1 ${DISPLAY}`, color: C.gold }}>{r.total}</span>
        <span style={{ font: `400 14px ${MONO}`, color: C.textDim }}>/ 100</span>
      </div>
      <p style={{ margin: 0, font: `600 13px ${BODY}`, color: C.text }}>
        {r.name} <span style={{ color: C.textDim, fontWeight: 400, fontFamily: MONO, fontSize: 11 }}>({r.code})</span>
      </p>
      <div style={{ display: "flex", flexDirection: "column", gap: 7 }}>
        {r.parts.map((p) => (
          <div key={p.k} style={{ display: "grid", gridTemplateColumns: "52px 1fr 40px", alignItems: "center", gap: 8 }}>
            <span style={{ font: `400 11px ${BODY}`, color: C.textDim }}>{p.k}</span>
            <div style={{ height: 5, borderRadius: 3, background: C.goldFaint, overflow: "hidden" }}>
              <div style={{ width: `${(p.v / p.max) * 100}%`, height: "100%", background: C.gold }} />
            </div>
            <span style={{ font: `600 11px ${MONO}`, color: C.text, textAlign: "right" }}>{p.v}/{p.max}</span>
          </div>
        ))}
      </div>
    </Card>
  );
}
function Tier({ t }) {
  return (
    <span style={{ display: "inline-grid", placeItems: "center", width: 34, height: 34, borderRadius: 8, border: `1px solid ${C.gold}`, background: C.goldFaint, font: `700 15px ${DISPLAY}`, color: C.gold }}>{t}</span>
  );
}

/* ───────────────────────── TopRuneStocksCard ───────────────────────── */
function TopRuneStocksCard() {
  return (
    <Card title="Top Rune Stocks" sub="오늘의 룬 점수 상위 종목">
      <div style={{ display: "flex", flexDirection: "column" }}>
        {MOCK.top.map((s) => (
          <div key={s.r} style={{ display: "grid", gridTemplateColumns: "22px 1fr 36px", alignItems: "center", gap: 10, padding: "7px 0", borderBottom: `1px solid ${C.borderSoft}` }}>
            <span style={{ font: `600 12px ${MONO}`, color: s.r <= 3 ? C.gold : C.textDim }}>{s.r}</span>
            <span style={{ font: `400 12px ${BODY}`, color: C.text }}>{s.name}</span>
            <span style={{ font: `700 13px ${MONO}`, color: s.score >= 80 ? C.gold : C.text, textAlign: "right" }}>{s.score}</span>
          </div>
        ))}
      </div>
    </Card>
  );
}

/* ───────────────────────── GungnirSignalCard ───────────────────────── */
function GungnirSignalCard({ mode }) {
  const g = MOCK.gungnir;
  const blocked = mode === "CASH";
  return (
    <Card title="Gungnir Signal" sub="궁니르 진입 · 장산 신호" glow={!blocked}
      right={<span style={{ font: `700 11px ${MONO}`, letterSpacing: "0.1em", color: C.up, border: `1px solid ${C.up}`, borderRadius: 6, padding: "2px 8px" }}>{g.side}</span>}>
      <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between", gap: 10 }}>
        <div>
          <p style={{ margin: 0, font: `700 16px ${BODY}`, color: C.text }}>{g.name}
            <span style={{ font: `400 11px ${MONO}`, color: C.textDim }}> ({g.code})</span></p>
        </div>
        <span className={blocked ? "" : "odin-ready"} style={{ font: `700 18px ${DISPLAY}`, letterSpacing: "0.08em", color: blocked ? C.down : C.gold }}>
          {blocked ? "HOLD" : "READY"}
        </span>
      </div>
      <p style={{ margin: 0, font: `400 11px ${BODY}`, color: C.textDim }}>{blocked ? "현금 모드 — 진입 차단" : "진입 가능"}</p>

      <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: 8 }}>
        <Stat label="예상 진입가" value={won(g.entry)} tone={C.text} />
        <Stat label={`목표가 (R/R ${g.rr})`} value={won(g.target)} tone={C.up} />
        <Stat label="손절가" value={`${won(g.stop)} (${g.stopPct}%)`} tone={C.down} />
        <Stat label="신호 등급" value={`LONG · 룬 ${MOCK.rune.total}`} tone={C.gold} />
      </div>

      <div style={{ display: "flex", flexWrap: "wrap", gap: 6, marginTop: 2 }}>
        {g.checks.map((c) => (
          <span key={c.k} style={{ font: `500 10px ${BODY}`, color: c.ok ? C.up : C.textDim, border: `1px solid ${c.ok ? "rgba(34,197,94,0.4)" : C.borderSoft}`, borderRadius: 999, padding: "3px 9px" }}>
            {c.ok ? "✓ " : "· "}{c.k}
          </span>
        ))}
      </div>
    </Card>
  );
}
function Stat({ label, value, tone }) {
  return (
    <div style={{ background: C.panel, border: `1px solid ${C.borderSoft}`, borderRadius: 8, padding: "8px 10px" }}>
      <div style={{ font: `400 10px ${BODY}`, color: C.textDim, marginBottom: 3 }}>{label}</div>
      <div style={{ font: `600 13px ${MONO}`, color: tone || C.text }}>{value}</div>
    </div>
  );
}

/* ───────────────────────── JudgeVerdictCard ───────────────────────── */
function JudgeVerdictCard() {
  const j = MOCK.judge;
  return (
    <Card title="Judge Verdict" sub="Muninn · 기억 기반 판정">
      <div style={{ display: "flex", alignItems: "center", gap: 16 }}>
        <div style={{ display: "grid", placeItems: "center", width: 76, height: 76, borderRadius: "50%", border: `2px solid ${C.gold}`, background: "radial-gradient(circle, rgba(214,168,79,0.18), transparent 70%)" }}>
          <span style={{ font: `700 26px ${DISPLAY}`, color: C.gold, lineHeight: 1 }}>{j.grade}</span>
        </div>
        <div>
          <div style={{ font: `600 12px ${DISPLAY}`, letterSpacing: "0.12em", color: C.gold }}>{j.label}</div>
          <div style={{ font: `400 11px ${BODY}`, color: C.textDim, marginTop: 4 }}>유사 패턴 {j.memSimilar}건 · 승률 {j.memWin}%</div>
        </div>
      </div>
      <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr 1fr", gap: 8 }}>
        <Stat label="승률 예측" value={`${j.win}%`} tone={C.up} />
        <Stat label="기대 수익률" value={pct(j.expect)} tone={C.up} />
        <Stat label="리스크 레벨" value={j.risk} tone={C.up} />
      </div>
    </Card>
  );
}

/* ───────────────────────── DraupnirVaultCard (시그니처) ───────────────────────── */
function DraupnirVaultCard() {
  const v = MOCK.vault;
  const rings = Array.from({ length: v.cycle }, (_, i) => i < v.day);
  return (
    <Card title="Draupnir Vault" sub="복리의 시작은 보존이다" glow
      right={<span style={{ font: `400 10px ${MONO}`, color: C.textDim }}>{v.day} / {v.cycle} DAYS</span>}>
      <p style={{ margin: 0, font: `400 11px/1.5 ${BODY}`, color: C.textDim }}>
        9거래일마다 같은 무게의 고리가 떨어진다 — 원본 고리는 잃지 않는다.
      </p>
      <div style={{ display: "flex", gap: 8, alignItems: "center", justifyContent: "center", padding: "6px 0" }}>
        {rings.map((on, i) => (
          <span key={i} className={on ? "odin-ring-on" : ""} title={`Day ${i + 1}`}
            style={{
              width: 18, height: 18, borderRadius: "50%",
              border: `2px solid ${on ? C.gold : C.borderSoft}`,
              background: on ? "radial-gradient(circle, rgba(214,168,79,0.5), transparent 72%)" : "transparent",
              boxShadow: on ? `0 0 8px rgba(214,168,79,0.5)` : "none",
            }} />
        ))}
      </div>
      <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", background: C.panel, border: `1px solid ${C.borderSoft}`, borderRadius: 8, padding: "8px 12px" }}>
        <div>
          <div style={{ font: `400 10px ${BODY}`, color: C.textDim }}>복리 배수 <span style={{ color: C.gold }}>(DEMO)</span></div>
          <div style={{ font: `700 16px ${MONO}`, color: C.gold }}>{v.multiple}</div>
        </div>
        <div style={{ font: `400 10px ${BODY}`, color: C.textDim, textAlign: "right", maxWidth: 130 }}>{v.note}</div>
      </div>
    </Card>
  );
}

/* ───────────────────────── RiskManagementCard ───────────────────────── */
function RiskManagementCard() {
  const r = MOCK.risk;
  const canTrade = r.daily > r.dailyLimit && r.positions < r.maxPositions;
  return (
    <Card title="Risk Management" sub="리스크 관리">
      <div style={{ display: "flex", alignItems: "center", gap: 16 }}>
        <Gauge pct={r.gauge} />
        <div style={{ flex: 1, display: "flex", flexDirection: "column", gap: 6 }}>
          <RiskRow k="일일 손익" v={pct(r.daily)} tone={upDown(r.daily)} />
          <RiskRow k="최대 손실 제한" v={`${r.dailyLimit.toFixed(2)}%`} tone={C.down} />
          <RiskRow k="포지션 수" v={`${r.positions} / ${r.maxPositions}`} />
          <RiskRow k="현금 보유율" v={`${r.cash}%`} tone={C.gold} />
        </div>
      </div>
      <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", borderTop: `1px solid ${C.borderSoft}`, paddingTop: 10 }}>
        <span style={{ font: `400 11px ${BODY}`, color: C.textDim }}>오늘 거래 가능</span>
        <span style={{ font: `700 12px ${MONO}`, letterSpacing: "0.08em", color: canTrade ? C.up : C.down }}>
          {canTrade ? "YES · SAFE" : "NO · STOP"}
        </span>
      </div>
    </Card>
  );
}
function RiskRow({ k, v, tone }) {
  return (
    <div style={{ display: "flex", justifyContent: "space-between" }}>
      <span style={{ font: `400 11px ${BODY}`, color: C.textDim }}>{k}</span>
      <span style={{ font: `600 12px ${MONO}`, color: tone || C.text }}>{v}</span>
    </div>
  );
}
function Gauge({ pct: p }) {
  const r = 26, circ = 2 * Math.PI * r;
  return (
    <svg width="68" height="68" viewBox="0 0 68 68" aria-label={`리스크 ${p}%`}>
      <circle cx="34" cy="34" r={r} fill="none" stroke={C.goldFaint} strokeWidth="6" />
      <circle cx="34" cy="34" r={r} fill="none" stroke={C.gold} strokeWidth="6" strokeLinecap="round"
        strokeDasharray={circ} strokeDashoffset={circ * (1 - p / 100)} transform="rotate(-90 34 34)" />
      <text x="34" y="32" textAnchor="middle" fill={C.textDim} style={{ font: `400 8px ${BODY}` }}>RISK</text>
      <text x="34" y="44" textAnchor="middle" fill={C.gold} style={{ font: `700 14px ${MONO}` }}>{p}%</text>
    </svg>
  );
}

/* ───────────────────────── VavlogCard ───────────────────────── */
function VavlogCard() {
  return (
    <Card title="VAVLOG" sub="최근 매매 로그" span={2}>
      <div style={{ overflowX: "auto" }}>
        <table style={{ width: "100%", borderCollapse: "collapse", minWidth: 420 }}>
          <thead>
            <tr>
              {["시간", "종목", "신호", "결과", "수익률"].map((h) => (
                <th key={h} style={{ textAlign: "left", font: `500 10px ${BODY}`, color: C.textDim, padding: "0 8px 8px", letterSpacing: "0.06em" }}>{h}</th>
              ))}
            </tr>
          </thead>
          <tbody>
            {MOCK.vavlog.map((l, i) => (
              <tr key={i} style={{ borderTop: `1px solid ${C.borderSoft}` }}>
                <td style={{ padding: "8px", font: `400 11px ${MONO}`, color: C.textDim }}>{l.t}</td>
                <td style={{ padding: "8px", font: `400 12px ${BODY}`, color: C.text }}>{l.name}</td>
                <td style={{ padding: "8px", font: `600 11px ${MONO}`, color: l.act === "매수" ? C.up : C.gold }}>{l.act}</td>
                <td style={{ padding: "8px", font: `400 11px ${BODY}`, color: l.res === "손절" ? C.down : C.text }}>{l.res}</td>
                <td style={{ padding: "8px", font: `600 12px ${MONO}`, color: upDown(l.pct) }}>{pct(l.pct)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </Card>
  );
}

/* ───────────────────────── OdinLayout (헤더 + 네비 + 그리드) ───────────────────────── */
const NAV = ["Dashboard", "Market Scan", "Rune Score", "Gungnir Signal", "Draupnir Vault", "VAVLOG", "Settings"];

function RuneTime() {
  const [now, setNow] = useState(new Date());
  useEffect(() => {
    const id = setInterval(() => setNow(new Date()), 1000);
    return () => clearInterval(id);
  }, []);
  const d = now;
  const date = `${d.getFullYear()}.${String(d.getMonth() + 1).padStart(2, "0")}.${String(d.getDate()).padStart(2, "0")}`;
  const time = d.toLocaleTimeString("ko-KR", { hour12: false });
  return (
    <div style={{ textAlign: "right" }}>
      <div style={{ font: `400 9px ${BODY}`, color: C.textDim, letterSpacing: "0.1em" }}>RUNE TIME</div>
      <div style={{ font: `600 13px ${MONO}`, color: C.gold }}>{time}</div>
      <div style={{ font: `400 10px ${MONO}`, color: C.textDim }}>{date}</div>
    </div>
  );
}

function OdinMark() {
  return (
    <svg width="34" height="34" viewBox="0 0 40 40" aria-hidden>
      <circle cx="20" cy="20" r="16" fill="none" stroke={C.gold} strokeWidth="1.4" />
      <circle cx="20" cy="20" r="11" fill="none" stroke={C.goldDim} strokeWidth="1" />
      <path d="M20 6 L20 34 M6 20 L34 20 M11 11 L29 29 M29 11 L11 29" stroke={C.goldDim} strokeWidth="0.7" />
      <circle cx="20" cy="20" r="3" fill={C.gold} />
    </svg>
  );
}

export default function OdinDraupnir() {
  const [tab, setTab] = useState("Dashboard");
  const [mode, setMode] = useState("NORMAL"); // NORMAL ↔ CASH

  return (
    <div style={{ minHeight: "100vh", background: C.bg, color: C.text, fontFamily: BODY }}>
      <style>{`
        @import url('https://fonts.googleapis.com/css2?family=Cinzel:wght@500;600;700&family=JetBrains+Mono:wght@400;600;700&display=swap');
        * { box-sizing: border-box; }
        .odin-shell { display: grid; grid-template-columns: 1fr; }
        .odin-nav { display: flex; gap: 6px; overflow-x: auto; padding: 4px 0; }
        .odin-nav::-webkit-scrollbar { height: 0; }
        .odin-grid { display: grid; grid-template-columns: 1fr; gap: 14px; }
        .odin-card table::-webkit-scrollbar { height: 6px; }
        @keyframes odinPulse { 0%,100% { opacity: 1; } 50% { opacity: 0.35; } }
        @keyframes readyGlow { 0%,100% { text-shadow: 0 0 6px rgba(214,168,79,0.5); } 50% { text-shadow: 0 0 16px rgba(214,168,79,0.9); } }
        .odin-live { animation: odinPulse 1.6s ease-in-out infinite; }
        .odin-ready { animation: readyGlow 2s ease-in-out infinite; }
        @media (min-width: 720px) { .odin-grid { grid-template-columns: 1fr 1fr; } }
        @media (min-width: 1040px) {
          .odin-shell { grid-template-columns: 188px 1fr; }
          .odin-nav { flex-direction: column; overflow: visible; }
          .odin-grid { grid-template-columns: repeat(3, 1fr); }
        }
        @media (prefers-reduced-motion: reduce) { .odin-live, .odin-ready { animation: none; } }
      `}</style>

      {/* HEADER */}
      <header style={{ display: "flex", alignItems: "center", justifyContent: "space-between", gap: 12, padding: "14px 18px", borderBottom: `1px solid ${C.border}`, background: "linear-gradient(180deg, rgba(214,168,79,0.06), transparent)" }}>
        <div style={{ display: "flex", alignItems: "center", gap: 12, minWidth: 0 }}>
          <OdinMark />
          <div style={{ minWidth: 0 }}>
            <h1 style={{ margin: 0, font: `700 17px/1 ${DISPLAY}`, letterSpacing: "0.16em", color: C.gold, whiteSpace: "nowrap" }}>ODIN'S DRAUPNIR</h1>
            <p style={{ margin: "3px 0 0", font: `400 9px ${BODY}`, letterSpacing: "0.22em", color: C.textDim }}>오딘의 드라우프니르 · AUTOMATED JUDGMENT</p>
          </div>
        </div>
        <div style={{ display: "flex", alignItems: "center", gap: 16 }}>
          <div style={{ display: "flex", alignItems: "center", gap: 6 }}>
            <span className="odin-live" style={{ width: 7, height: 7, borderRadius: "50%", background: C.up, display: "inline-block" }} />
            <span style={{ font: `600 10px ${MONO}`, color: C.up, letterSpacing: "0.08em" }}>LIVE</span>
          </div>
          <button onClick={() => setMode(mode === "NORMAL" ? "CASH" : "NORMAL")}
            style={{ cursor: "pointer", font: `700 10px ${MONO}`, letterSpacing: "0.1em", color: mode === "CASH" ? C.down : C.gold, background: "transparent", border: `1px solid ${mode === "CASH" ? C.down : C.gold}`, borderRadius: 7, padding: "6px 12px" }}>
            {mode}
          </button>
          <RuneTime />
        </div>
      </header>

      <div className="odin-shell" style={{ padding: 16, gap: 16 }}>
        {/* NAV */}
        <nav className="odin-nav">
          {NAV.map((n) => {
            const active = n === tab;
            return (
              <button key={n} onClick={() => setTab(n)}
                style={{ cursor: "pointer", textAlign: "left", whiteSpace: "nowrap", font: `${active ? 600 : 400} 12px ${BODY}`, color: active ? C.gold : C.textDim, background: active ? C.goldFaint : "transparent", border: `1px solid ${active ? C.border : "transparent"}`, borderRadius: 8, padding: "9px 12px" }}>
                {n}
              </button>
            );
          })}
          <div style={{ marginTop: "auto", paddingTop: 14, font: `400 9px/1.6 ${BODY}`, color: C.textDim, letterSpacing: "0.1em" }}>
            KNOWLEDGE IS POWER<br />RUNE IS EDGE<br /><span style={{ color: C.goldDim }}>v0.1 · DEMO DATA</span>
          </div>
        </nav>

        {/* MAIN */}
        {tab === "Dashboard" ? (
          <main className="odin-grid">
            <MarketStatusCard mode={mode} />
            <MarketScanCard />
            <SectorStrengthCard />
            <RuneScoreCard />
            <TopRuneStocksCard />
            <GungnirSignalCard mode={mode} />
            <JudgeVerdictCard />
            <DraupnirVaultCard />
            <RiskManagementCard />
            <VavlogCard />
          </main>
        ) : (
          <main>
            <div style={{ display: "grid", placeItems: "center", minHeight: 240, border: `1px dashed ${C.border}`, borderRadius: 14, background: C.card, textAlign: "center", padding: 24 }}>
              <div>
                <div style={{ font: `600 14px ${DISPLAY}`, letterSpacing: "0.12em", color: C.gold }}>{tab}</div>
                <p style={{ margin: "10px 0 0", font: `400 12px ${BODY}`, color: C.textDim }}>준비중 — v0.1은 Dashboard 중심입니다.</p>
              </div>
            </div>
          </main>
        )}
      </div>
    </div>
  );
}
