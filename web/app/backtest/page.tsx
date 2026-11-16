"use client";
import { useCallback, useEffect, useMemo, useState } from "react";
import { api, BacktestJob, BacktestResult, BacktestSide, CurvePoint, errorMessage, Strategy } from "@/lib/api";
import { ErrorCard, Skeleton } from "@/components/Feedback";
import { readCache, writeCache } from "@/lib/cache";

const money = (v: number | null | undefined, digits = 0) =>
  v == null ? "—" : v.toLocaleString("zh-TW", { minimumFractionDigits: digits, maximumFractionDigits: digits });
const pct = (v: number | null | undefined, sign = true) => (v == null ? "—" : `${sign && v > 0 ? "+" : ""}${v.toFixed(2)}%`);
const tone = (v: number | null | undefined) => (v == null || v === 0 ? "" : v > 0 ? "text-rise" : "text-fall");
const twToday = () => new Date(Date.now() + 8 * 3_600_000);
const isoDaysAgo = (days: number) => new Date(twToday().getTime() - days * 86_400_000).toISOString().slice(0, 10);

type Preset = { id: string; label: string; start: () => string; eta: string };
const PRESETS: Preset[] = [
  { id: "1m", label: "近 1 個月", start: () => isoDaysAgo(30), eta: "約 3 分鐘" },
  { id: "3m", label: "近 3 個月", start: () => isoDaysAgo(91), eta: "約 4 分鐘" },
  { id: "6m", label: "近 6 個月", start: () => isoDaysAgo(182), eta: "約 6 分鐘" },
  { id: "ytd", label: "今年以來", start: () => `${twToday().getUTCFullYear()}-01-01`, eta: "約 5–10 分鐘" },
  { id: "1y", label: "近 1 年", start: () => isoDaysAgo(365), eta: "約 10 分鐘" },
  { id: "custom", label: "自訂", start: () => isoDaysAgo(120), eta: "依期間長短" },
];
const COLORS = { strategy: "#60a5fa", bench: "#fbbf24" };

export default function BacktestPage() {
  const [job, setJob] = useState<BacktestJob | null>(null);
  const [result, setResult] = useState<BacktestResult | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [tick, setTick] = useState(0);
  const refresh = useCallback(() => setTick(t => t + 1), []);

  useEffect(() => { // 先顯示這台裝置上次看到的結果
    const cached = readCache<BacktestResult>("backtest");
    if (cached) { setResult(cached); setLoading(false); }
  }, []);

  useEffect(() => {
    const controller = new AbortController();
    let timer: ReturnType<typeof setTimeout>;
    api.getBacktest(controller.signal).then(res => {
      setJob(res.job); setError(null);
      if (res.result) { setResult(res.result); writeCache("backtest", res.result); }
      if (res.job.status === "running") timer = setTimeout(refresh, 2_000);
    }).catch(err => {
      if (controller.signal.aborted) return;
      setError(errorMessage(err)); timer = setTimeout(refresh, 10_000);
    }).finally(() => { if (!controller.signal.aborted) setLoading(false); });
    return () => { controller.abort(); clearTimeout(timer); };
  }, [tick, refresh]);

  const running = job?.status === "running";
  return <div className="space-y-8">
    <div><p className="eyebrow">BACKTEST</p><h1 className="mt-2 text-3xl font-semibold tracking-tight">回測比較</h1>
      <p className="mt-2 max-w-3xl text-sm leading-6 text-muted">用過去的股價，把策略照模擬交易同一套規則逐日跑一遍（隔天開盤買零股、停損停利、持有上限、照實扣手續費與證交稅），和同期間「第一天全部買進 0050 一直抱著」並排比較。不會動到你的模擬帳戶。</p></div>
    {error && <ErrorCard message={error} retry={refresh} />}
    {loading ? <Skeleton rows={2} /> : running ? <Progress job={job!} onChange={refresh} /> : <RunForm onStarted={refresh} lastJob={job} />}
    {result && <Report result={result} stale={running} />}
  </div>;
}

function RunForm({ onStarted, lastJob }: { onStarted: () => void; lastJob: BacktestJob | null }) {
  const [strategies, setStrategies] = useState<Strategy[]>([]);
  const [strategyId, setStrategyId] = useState("default");
  const [preset, setPreset] = useState("6m");
  const [custom, setCustom] = useState(isoDaysAgo(120));
  const [capital, setCapital] = useState(10000);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  useEffect(() => { api.listStrategies().then(d => setStrategies(d.strategies.filter(s => !s.error))).catch(() => {}); }, []);
  const chosen = PRESETS.find(p => p.id === preset)!;
  const start = preset === "custom" ? custom : chosen.start();
  async function run() {
    setBusy(true); setError(null);
    try { await api.startBacktest(strategyId, start, capital); onStarted(); }
    catch (err) { setError(errorMessage(err)); } finally { setBusy(false); }
  }
  return <section className="card space-y-5">
    <h2 className="font-medium">設定回測</h2>
    <div><span className="label">回測期間</span>
      <div className="mt-2 flex flex-wrap gap-2" role="radiogroup" aria-label="回測期間">
        {PRESETS.map(p => <button key={p.id} type="button" role="radio" aria-checked={preset === p.id} onClick={() => setPreset(p.id)}
          className={`rounded-lg border px-3 py-1.5 text-sm transition ${preset === p.id ? "border-accent bg-accent/10 text-blue-300" : "border-line text-muted hover:text-text"}`}>{p.label}</button>)}
      </div>
      <p className="mt-2 text-xs text-muted">{preset === "custom"
        ? <span className="flex flex-wrap items-center gap-2">從 <input type="date" className="input w-auto py-1" value={custom} min={isoDaysAgo(365)} max={isoDaysAgo(7)} onChange={e => setCustom(e.target.value)} /> 到最新交易日（最長一年）</span>
        : <>從 {start} 到最新交易日・預估 {chosen.eta}</>}</p>
    </div>
    <div className="grid gap-5 md:grid-cols-2">
      <div><label htmlFor="bt-strategy" className="label">策略</label>
        <select id="bt-strategy" className="input" value={strategyId} onChange={e => setStrategyId(e.target.value)}>
          {(strategies.length ? strategies : [{ id: "default", name: "預設策略" } as Strategy]).map(s => <option key={s.id} value={s.id}>{s.name}</option>)}
        </select></div>
      <div><label htmlFor="bt-capital" className="label">本金（元）</label>
        <input id="bt-capital" type="number" min={1000} step={1000} className="input" value={capital} onChange={e => setCapital(Number(e.target.value))} /></div>
    </div>
    <div className="flex flex-wrap items-center gap-3">
      <button className="btn-primary" disabled={busy || capital < 1000} onClick={run}>{busy ? "啟動中…" : "開始回測"}</button>
      <span className="text-xs text-muted">在背景跑，可以關掉頁面或換裝置，回來會看到結果。</span>
      {lastJob?.status === "failed" && <span className="text-xs text-red-300">上次回測失敗：{lastJob.error}</span>}
    </div>
    {error && <ErrorCard message={error} />}
  </section>;
}

function Progress({ job, onChange }: { job: BacktestJob; onChange: () => void }) {
  const [busy, setBusy] = useState(false);
  const p = job.params;
  return <section className="card space-y-3" role="status" aria-live="polite">
    <div className="flex items-center justify-between gap-3">
      <h2 className="font-medium">回測中{p ? `：${p.strategy_name}・${p.start_date} 起・本金 ${money(p.capital)}` : ""}</h2>
      <button className="btn-ghost" disabled={busy} onClick={async () => { setBusy(true); try { await api.cancelBacktest(); } finally { setBusy(false); onChange(); } }}>取消</button>
    </div>
    <div className="flex justify-between text-sm"><span>{job.phase}{job.current ? `：${job.current}` : "…"}</span><span className="font-mono text-muted">{job.completed} / {job.total ?? "—"}</span></div>
    <progress className="h-1.5 w-full" max={job.total || 1} value={job.total ? job.completed : undefined} />
    <p className="text-xs text-muted">先下載每檔股票的歷史資料，再逐日回放。所有裝置都看得到這個進度。</p>
  </section>;
}

function Report({ result, stale }: { result: BacktestResult; stale: boolean }) {
  const s = result.result, b = result.benchmark;
  const diff = b.available && b.return_pct != null ? s.return_pct - b.return_pct : null;
  const rows: [string, (x: Partial<BacktestSide>) => React.ReactNode, boolean?][] = [
    ["期末資產", x => money(x.final), true],
    ["總報酬", x => <span className={tone(x.return_pct)}>{pct(x.return_pct)}</span>],
    ["損益（元）", x => <span className={tone(x.pnl)}>{x.pnl == null ? "—" : `${x.pnl > 0 ? "+" : ""}${money(x.pnl)}`}</span>],
    ["最大回撤", x => <span className="text-fall">{pct(x.max_drawdown_pct, false)}</span>],
    ["勝率（已結算交易）", x => (x.win_rate == null ? "—" : `${x.win_rate}%`)],
    ["交易次數", x => (x.trades == null ? "—" : `${x.trades} 筆`)],
    ["平均每筆獲利／虧損", x => (x.avg_win_pct == null && x.avg_loss_pct == null ? "—" : `${pct(x.avg_win_pct)}／${pct(x.avg_loss_pct)}`)],
    ["獲利因子（總賺÷總賠）", x => (x.profit_factor == null ? "—" : x.profit_factor.toFixed(2))],
    ["平均持股比例", x => (x.avg_exposure_pct == null ? "100%" : `${x.avg_exposure_pct}%`)],
    ["手續費＋證交稅", x => money(x.fees)],
  ];
  return <div className={`space-y-6 ${stale ? "opacity-60" : ""}`}>
    <div className="flex flex-wrap items-baseline justify-between gap-2">
      <h2 className="text-lg font-medium">{stale ? "上一次的結果" : "回測結果"}：{result.strategy.name}</h2>
      <span className="text-sm text-muted">{result.start_date} ～ {result.end_date}・{result.trading_days} 個交易日・本金 {money(result.capital)}</span>
    </div>
    {diff != null && <div className={`card text-base ${diff >= 0 ? "border-rise/40" : "border-fall/40"}`}>
      {diff >= 0 ? "📈 策略贏過抱 0050" : "📉 策略輸給抱 0050"} <b className={tone(diff)}>{Math.abs(diff).toFixed(2)}</b> 個百分點
      <span className="ml-2 text-sm text-muted">（策略 {pct(s.return_pct)}，0050 {pct(b.return_pct)}；最大回撤 {pct(s.max_drawdown_pct, false)} vs {pct(b.max_drawdown_pct, false)}）</span>
    </div>}
    <section className="card overflow-x-auto"><table className="w-full min-w-[520px] text-sm">
      <thead className="text-left text-xs text-muted"><tr><th className="py-2">指標</th>
        <th><span style={{ color: COLORS.strategy }}>●</span> 策略</th>
        <th><span style={{ color: COLORS.bench }}>●</span> 抱 0050{b.available && b.shares ? `（${b.shares} 股）` : ""}</th></tr></thead>
      <tbody>{rows.map(([label, f]) => <tr key={label} className="border-t border-line">
        <td className="py-2 text-muted">{label}</td><td className="font-mono">{f(s)}</td>
        <td className="font-mono">{b.available ? f(b) : "資料暫缺"}</td></tr>)}</tbody>
    </table></section>
    <section className="card"><h3 className="mb-3 font-medium">資產變化</h3>
      <Chart base={result.capital} series={[{ name: "策略", color: COLORS.strategy, points: s.curve }, ...(b.available && b.curve ? [{ name: "抱 0050", color: COLORS.bench, points: b.curve }] : [])]} /></section>
    <Trades trades={result.trades} />
    <p className="text-xs leading-6 text-muted">注意：回測用的是過去資料，不代表未來。基本面用目前最新的年報（等於提前知道答案，結果會比實際好看一點）、沒有夜盤濾鏡；買賣以日 K 判斷，同一天碰到停損和停利時保守算停損。期末還沒賣的持股以最後收盤價結算。</p>
  </div>;
}

function Trades({ trades }: { trades: BacktestResult["trades"] }) {
  const list = useMemo(() => [...trades].reverse(), [trades]);
  return <details className="card overflow-x-auto"><summary className="cursor-pointer font-medium">交易紀錄（{trades.filter(t => t.side === "SELL").length} 筆賣出）</summary>
    {list.length ? <table className="mt-3 w-full min-w-[720px] text-sm"><thead className="text-left text-xs text-muted"><tr><th className="py-2">日期</th><th>買賣</th><th>股票</th><th>股數 × 價格</th><th>損益</th><th>原因</th></tr></thead>
      <tbody>{list.map((t, i) => <tr key={i} className="border-t border-line">
        <td className="py-2 font-mono text-xs">{t.date}</td><td className={t.side === "BUY" ? "text-rise" : "text-fall"}>{t.side === "BUY" ? "買" : "賣"}</td>
        <td>{t.stock_id} {t.name}</td><td className="font-mono">{t.shares} × {money(t.price, 2)}</td>
        <td className={`font-mono ${tone(t.pnl)}`}>{t.pnl == null ? "—" : `${t.pnl > 0 ? "+" : ""}${money(t.pnl)}（${pct(t.pnl_pct ?? 0)}）`}</td>
        <td className="text-xs text-muted">{t.reason}</td></tr>)}</tbody></table>
      : <p className="mt-3 text-sm text-muted">這段期間策略沒有買進（沒有 BUY 訊號，或大盤跌破月線時不進場）。</p>}
  </details>;
}

function Chart({ base, series }: { base: number; series: { name: string; color: string; points: CurvePoint[] }[] }) {
  const all = series.flatMap(s => s.points);
  if (all.length < 2) return <p className="text-sm text-muted">資料點不足。</p>;
  const dates = series[0].points.map(p => p.date);
  const W = 800, H = 240, P = 36;
  const values = all.map(p => p.total);
  const min = Math.min(base, ...values), max = Math.max(base, ...values), span = max - min || 1;
  const x = (i: number) => P + (i / Math.max(1, dates.length - 1)) * (W - 2 * P);
  const y = (v: number) => H - P - ((v - min) / span) * (H - 2 * P);
  const at = new Map(dates.map((d, i) => [d, i]));
  return <div>
    <div className="mb-2 flex flex-wrap gap-4 text-xs text-muted">{series.map(s => <span key={s.name}><span style={{ color: s.color }}>━</span> {s.name}</span>)}</div>
    <svg viewBox={`0 0 ${W} ${H}`} className="w-full" role="img" aria-label={series.map(s => `${s.name} 期末 ${money(s.points[s.points.length - 1]?.total)} 元`).join("，")}>
      <line x1={P} x2={W - P} y1={y(base)} y2={y(base)} stroke="#475569" strokeDasharray="4 4" />
      <text x={W - P} y={y(base) - 6} textAnchor="end" fontSize="11" fill="#94a3b8">本金 {money(base)}</text>
      {series.map(s => <path key={s.name} fill="none" stroke={s.color} strokeWidth="2"
        d={s.points.filter(p => at.has(p.date)).map((p, i) => `${i ? "L" : "M"}${x(at.get(p.date)!).toFixed(1)},${y(p.total).toFixed(1)}`).join("")} />)}
      <text x={P} y={H - 10} fontSize="11" fill="#94a3b8">{dates[0]}</text>
      <text x={W - P} y={H - 10} textAnchor="end" fontSize="11" fill="#94a3b8">{dates[dates.length - 1]}</text>
    </svg>
    <p className="mt-1 text-xs text-muted">每日收盤市值（未扣期末賣出的費稅）。</p>
  </div>;
}
