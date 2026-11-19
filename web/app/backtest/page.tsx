"use client";
import { useCallback, useEffect, useMemo, useState } from "react";
import { api, BacktestJob, BacktestResult, BacktestSide, CurvePoint, DaytradeBacktest, DaytradeCommand, errorMessage, SimTrade, Strategy } from "@/lib/api";
import { ErrorCard, Skeleton } from "@/components/Feedback";
import { readCache, writeCache } from "@/lib/cache";
import QuotaStatus from "@/components/QuotaStatus";

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
const PALETTE = ["#60a5fa", "#a78bfa", "#34d399"];
const DAYTRADE_COLOR = "#f472b6";
const MAX_PICK = 3;

export default function BacktestPage() {
  const [job, setJob] = useState<BacktestJob | null>(null);
  const [result, setResult] = useState<BacktestResult | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [tick, setTick] = useState(0);
  const [daytrade, setDaytrade] = useState<{ backtest: DaytradeBacktest | null; commands: DaytradeCommand[]; agentAt?: string | null } | null>(null);
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
      const wantsDaytrade = res.job.params?.include_daytrade || res.result?.request?.include_daytrade;
      if (wantsDaytrade) api.getDaytrade(controller.signal).then(d => setDaytrade({ backtest: d.backtest, commands: d.commands || [], agentAt: d.heartbeat.agent_at })).catch(() => {});
      const cmd = res.job.params?.daytrade_command || res.result?.request?.daytrade_command;
      if (res.job.status === "running") timer = setTimeout(refresh, 2_000);
      else if (cmd) timer = setTimeout(refresh, 15_000); // 等本機當沖回測
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
    {result && <Report result={result} stale={running} daytrade={daytrade} />}
  </div>;
}

function RunForm({ onStarted, lastJob }: { onStarted: () => void; lastJob: BacktestJob | null }) {
  const [strategies, setStrategies] = useState<Strategy[]>([]);
  const [picked, setPicked] = useState<string[]>(lastJob?.params?.strategy_ids?.length ? lastJob.params.strategy_ids : ["default"]);
  const [withDaytrade, setWithDaytrade] = useState(!!lastJob?.params?.include_daytrade);
  const [preset, setPreset] = useState("6m");
  const [custom, setCustom] = useState(isoDaysAgo(120));
  const [capital, setCapital] = useState(10000);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  useEffect(() => { api.listStrategies().then(d => setStrategies(d.strategies.filter(s => !s.error))).catch(() => {}); }, []);
  const chosen = PRESETS.find(p => p.id === preset)!;
  const start = preset === "custom" ? custom : chosen.start();
  const list = strategies.length ? strategies : [{ id: "default", name: "預設策略" } as Strategy];
  const toggle = (id: string) => setPicked(p => p.includes(id) ? p.filter(x => x !== id) : p.length >= MAX_PICK ? p : [...p, id]);
  const nothing = !picked.length && !withDaytrade;
  async function run() {
    setBusy(true); setError(null);
    try { await api.startBacktest(picked, withDaytrade, start, capital); onStarted(); }
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
        : <>從 {start} 到最新交易日・每個策略預估 {chosen.eta}</>}</p>
    </div>
    <fieldset><legend className="label">策略（可複選，一次最多 {MAX_PICK} 個，都會和抱 0050 並排比較）</legend>
      <div className="mt-2 flex flex-wrap gap-2">
        {list.map(s => { const on = picked.includes(s.id); return <label key={s.id} className={`flex cursor-pointer items-center gap-2 rounded-lg border px-3 py-1.5 text-sm ${on ? "border-accent bg-accent/10 text-blue-300" : "border-line text-muted"} ${!on && picked.length >= MAX_PICK ? "opacity-50" : ""}`}>
          <input type="checkbox" className="accent-blue-400" checked={on} disabled={!on && picked.length >= MAX_PICK} onChange={() => toggle(s.id)} />{s.name}</label>; })}
        <label className={`flex cursor-pointer items-center gap-2 rounded-lg border px-3 py-1.5 text-sm ${withDaytrade ? "border-pink-400/60 bg-pink-400/10 text-pink-300" : "border-line text-muted"}`}>
          <input type="checkbox" className="accent-pink-400" checked={withDaytrade} onChange={e => setWithDaytrade(e.target.checked)} />⚡ 當沖：動能延續與極短線套利</label>
      </div>
      {withDaytrade && <p className="mt-2 text-xs leading-5 text-muted">當沖要用盤中 3 分 K，由你電腦上的當沖程式（凱基 API）回測同一段期間（最多約 130 個交易日），算完自動出現在下面。本金固定用「每筆 10 萬 × 同時 2 檔」，不用這裡的本金；電腦和當沖程式要開著。</p>}
    </fieldset>
    <div className="grid gap-5 md:grid-cols-2">
      <div><label htmlFor="bt-capital" className="label">本金（元，一般策略與 0050 用）</label>
        <input id="bt-capital" type="number" min={1000} step={1000} className="input" value={capital} onChange={e => setCapital(Number(e.target.value))} /></div>
    </div>
    <div className="flex flex-wrap items-center gap-3">
      <button className="btn-primary" disabled={busy || capital < 1000 || nothing} onClick={run}>{busy ? "啟動中…" : "開始回測"}</button>
      <span className="text-xs text-muted">{nothing ? "請至少勾一個策略" : "在背景跑，可以關掉頁面或換裝置，回來會看到結果。"}</span>
      {lastJob?.status === "failed" && <span className="text-xs text-red-300">上次回測失敗：{lastJob.error?.replace(/^\w+(Error|Exception): /, "")}</span>}
    </div>
    {picked.length > 0 && <QuotaStatus need="第一次回測約需 300–500 次，之後同一天、第二個策略都會少很多" />}
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
    {p?.quota_note && <p className="text-sm text-amber-600">{p.quota_note}</p>}
    <div className="flex justify-between text-sm"><span>{job.phase}{job.current ? `：${job.current}` : "…"}</span><span className="font-mono text-muted">{job.completed} / {job.total ?? "—"}</span></div>
    <progress className="h-1.5 w-full" max={job.total || 1} value={job.total ? job.completed : undefined} />
    <p className="text-xs text-muted">先下載每檔股票的歷史資料，再逐日回放。所有裝置都看得到這個進度。</p>
  </section>;
}

type Col = { key: string; name: string; color: string; side: Partial<BacktestSide> | null; note?: string; trades?: SimTrade[] };

function daytradeColumn(result: BacktestResult, dt: { backtest: DaytradeBacktest | null; commands: DaytradeCommand[]; agentAt?: string | null } | null): Col | null {
  const cmdId = result.request?.daytrade_command;
  if (!result.request?.include_daytrade) return null;
  const bt = dt?.backtest;
  if (bt && cmdId && bt.request?.id === cmdId) {
    const s = bt.summary;
    return { key: "daytrade", name: "⚡ 當沖", color: DAYTRADE_COLOR, note: `本金 ${money(s.capital)}・${bt.start_date} 起`,
      side: { final: s.final, pnl: s.pnl, return_pct: s.return_pct, max_drawdown_pct: s.max_drawdown_pct, win_rate: s.win_rate, trades: s.trades, profit_factor: s.profit_factor, curve: bt.curve || [], avg_exposure_pct: undefined } };
  }
  const cmd = dt?.commands.find(c => c.id === cmdId);
  const agentAlive = dt?.agentAt ? Date.now() - new Date(dt.agentAt).getTime() < 3 * 60_000 : false;
  const note = !cmd ? "讀取中…" : cmd.status === "failed" ? `失敗：${cmd.message}` : cmd.status === "running" ? `你電腦上的當沖程式${cmd.message || "回測中…"}（第一次要抓幾個月資料，約 10–30 分鐘）`
    : agentAlive ? "排隊中，當沖程式馬上會開始" : "等你電腦上的當沖程式（請確認電腦開著、當沖程式在執行）";
  return { key: "daytrade", name: "⚡ 當沖", color: DAYTRADE_COLOR, side: null, note };
}

function Report({ result, stale, daytrade }: { result: BacktestResult; stale: boolean; daytrade: { backtest: DaytradeBacktest | null; commands: DaytradeCommand[]; agentAt?: string | null } | null }) {
  const b = result.benchmark;
  const runs = result.runs ?? (result.result && result.strategy ? [{ strategy: result.strategy, result: result.result, trades: result.trades, skipped: result.skipped }] : []);
  const dtCol = daytradeColumn(result, daytrade);
  const cols: Col[] = [
    ...runs.map((r, i) => ({ key: r.strategy.id, name: r.strategy.name, color: runs.length === 1 ? COLORS.strategy : PALETTE[i % PALETTE.length], side: r.result, trades: r.trades })),
    ...(dtCol ? [dtCol] : []),
    { key: "0050", name: `抱 0050${b.available && b.shares ? `（${b.shares} 股）` : ""}`, color: COLORS.bench, side: b.available ? { ...b, avg_exposure_pct: 100 } : null, note: b.available ? undefined : "資料暫缺" },
  ];
  const skipped = runs.flatMap(r => r.skipped || []).filter((x, i, a) => a.findIndex(y => y.stock_id === x.stock_id) === i);
  const rows: [string, (x: Partial<BacktestSide>) => React.ReactNode][] = [
    ["期末資產", x => money(x.final)],
    ["總報酬", x => <span className={tone(x.return_pct)}>{pct(x.return_pct)}</span>],
    ["損益（元）", x => <span className={tone(x.pnl)}>{x.pnl == null ? "—" : `${x.pnl > 0 ? "+" : ""}${money(x.pnl)}`}</span>],
    ["最大回撤", x => <span className="text-fall">{pct(x.max_drawdown_pct, false)}</span>],
    ["勝率（已結算交易）", x => (x.win_rate == null ? "—" : `${x.win_rate}%`)],
    ["交易次數", x => (x.trades == null ? "—" : `${x.trades} 筆`)],
    ["平均每筆獲利／虧損", x => (x.avg_win_pct == null && x.avg_loss_pct == null ? "—" : `${pct(x.avg_win_pct)}／${pct(x.avg_loss_pct)}`)],
    ["獲利因子（總賺÷總賠）", x => (x.profit_factor == null ? "—" : x.profit_factor.toFixed(2))],
    ["平均持股比例", x => (x.avg_exposure_pct == null ? "—" : `${x.avg_exposure_pct}%`)],
    ["手續費＋稅", x => (x.fees == null ? "—" : money(x.fees))],
  ];
  const scored = cols.filter(c => c.side?.return_pct != null && c.key !== "0050");
  const best = scored.length ? scored.reduce((a, c) => (c.side!.return_pct! > a.side!.return_pct! ? c : a)) : null;
  const benchRet = b.available ? b.return_pct : null;
  const title = cols.filter(c => c.key !== "0050").map(c => c.name).join("、");
  return <div className={`space-y-6 ${stale ? "opacity-60" : ""}`}>
    <div className="flex flex-wrap items-baseline justify-between gap-2">
      <h2 className="text-lg font-medium">{stale ? "上一次的結果" : "回測結果"}：{title}</h2>
      <span className="text-sm text-muted">{result.start_date} ～ {result.end_date}・{result.trading_days} 個交易日・本金 {money(result.capital)}</span>
    </div>
    {!!skipped.length && <div className="rounded-xl border border-watch/30 bg-watch/5 p-4 text-sm leading-6">
      ⚠️ 有 {skipped.length} 檔沒抓到資料、沒放進回測（{skipped.slice(0, 6).map(x => x.stock_id).join("、")}{skipped.length > 6 ? "…" : ""}）。
      {skipped.some(x => /額度|限流/.test(x.reason)) ? "原因是 FinMind 免費額度（每小時 600 次）用完，約一小時後重跑，並避免同時按「開始選股」。" : `原因：${skipped[0].reason}`}
    </div>}
    {best && benchRet != null && <div className={`card text-base ${best.side!.return_pct! >= benchRet ? "border-rise/40" : "border-fall/40"}`}>
      {scored.length > 1 ? <>🏆 報酬最高：<b>{best.name}</b> {pct(best.side!.return_pct)}，</> : null}
      {scored.map(c => { const d = c.side!.return_pct! - benchRet; return <span key={c.key} className="mr-3">{c.name}{d >= 0 ? " 贏" : " 輸"} 0050 <b className={tone(d)}>{Math.abs(d).toFixed(2)}</b> 個百分點</span>; })}
      <span className="text-sm text-muted">（0050 {pct(benchRet)}）</span>
    </div>}
    <section className="card overflow-x-auto"><table className="w-full min-w-[560px] text-sm">
      <thead className="text-left text-xs text-muted"><tr><th className="py-2">指標</th>
        {cols.map(c => <th key={c.key} className="pr-3"><span style={{ color: c.color }}>●</span> {c.name}{c.note && c.side ? <div className="font-normal">{c.note}</div> : null}</th>)}</tr></thead>
      <tbody>{rows.map(([label, f]) => <tr key={label} className="border-t border-line">
        <td className="py-2 text-muted">{label}</td>
        {cols.map(c => <td key={c.key} className="pr-3 font-mono">{c.side ? f(c.side) : label === "期末資產" ? <span className="font-sans text-xs text-muted">{c.note}</span> : ""}</td>)}</tr>)}</tbody>
    </table></section>
    <section className="card"><h3 className="mb-3 font-medium">資產變化（以本金為 100%）</h3>
      <Chart series={cols.filter(c => c.side?.curve?.length).map(c => ({ name: c.name, color: c.color, base: result.capital, points: c.side!.curve! }))} capitalByKey={Object.fromEntries(cols.map(c => [c.name, c.key === "daytrade" ? (daytrade?.backtest?.summary.capital ?? result.capital) : result.capital]))} /></section>
    {runs.map(r => <Trades key={r.strategy.id} name={runs.length > 1 ? r.strategy.name : ""} trades={r.trades} />)}
    <p className="text-xs leading-6 text-muted">注意：回測用的是過去資料，不代表未來。一般策略：基本面用目前最新的年報（等於提前知道答案，結果會比實際好看一點）、沒有夜盤濾鏡；買賣以日 K 判斷，同一天碰到停損和停利時保守算停損，期末還沒賣的持股以最後收盤價結算。當沖：用凱基 3 分 K 粗估，看不到五檔、大單、試撮量與夜盤濾網，含手續費與當沖稅。</p>
  </div>;
}

function Trades({ trades, name }: { trades: BacktestResult["trades"]; name: string }) {
  const list = useMemo(() => [...trades].reverse(), [trades]);
  return <details className="card overflow-x-auto"><summary className="cursor-pointer font-medium">{name ? `${name}：` : ""}交易紀錄（{trades.filter(t => t.side === "SELL").length} 筆賣出）</summary>
    {list.length ? <table className="mt-3 w-full min-w-[720px] text-sm"><thead className="text-left text-xs text-muted"><tr><th className="py-2">日期</th><th>買賣</th><th>股票</th><th>股數 × 價格</th><th>損益</th><th>原因</th></tr></thead>
      <tbody>{list.map((t, i) => <tr key={i} className="border-t border-line">
        <td className="py-2 font-mono text-xs">{t.date}</td><td className={t.side === "BUY" ? "text-rise" : "text-fall"}>{t.side === "BUY" ? "買" : "賣"}</td>
        <td>{t.stock_id} {t.name}</td><td className="font-mono">{t.shares} × {money(t.price, 2)}</td>
        <td className={`font-mono ${tone(t.pnl)}`}>{t.pnl == null ? "—" : `${t.pnl > 0 ? "+" : ""}${money(t.pnl)}（${pct(t.pnl_pct ?? 0)}）`}</td>
        <td className="text-xs text-muted">{t.reason}</td></tr>)}</tbody></table>
      : <p className="mt-3 text-sm text-muted">這段期間策略沒有買進（沒有 BUY 訊號，或大盤跌破月線時不進場）。</p>}
  </details>;
}

function Chart({ series, capitalByKey }: { series: { name: string; color: string; base: number; points: CurvePoint[] }[]; capitalByKey: Record<string, number> }) {
  // 各條線本金不同（當沖固定 20 萬），一律換成「本金的 %」畫在同一張圖
  const norm = series.map(s => ({ ...s, points: s.points.map(p => ({ date: p.date, total: (p.total / (capitalByKey[s.name] || s.base || 1)) * 100 })) }));
  const all = norm.flatMap(s => s.points);
  if (all.length < 2) return <p className="text-sm text-muted">資料點不足。</p>;
  const dates = Array.from(new Set(all.map(p => p.date))).sort();
  const W = 800, H = 240, P = 36, base = 100;
  const values = all.map(p => p.total);
  const min = Math.min(base, ...values), max = Math.max(base, ...values), span = max - min || 1;
  const x = (i: number) => P + (i / Math.max(1, dates.length - 1)) * (W - 2 * P);
  const y = (v: number) => H - P - ((v - min) / span) * (H - 2 * P);
  const at = new Map(dates.map((d, i) => [d, i]));
  return <div>
    <div className="mb-2 flex flex-wrap gap-4 text-xs text-muted">{norm.map(s => <span key={s.name}><span style={{ color: s.color }}>━</span> {s.name} {pct((s.points[s.points.length - 1]?.total ?? 100) - 100)}</span>)}</div>
    <svg viewBox={`0 0 ${W} ${H}`} className="w-full" role="img" aria-label={norm.map(s => `${s.name} 期末 ${((s.points[s.points.length - 1]?.total ?? 100) - 100).toFixed(2)}%`).join("，")}>
      <line x1={P} x2={W - P} y1={y(base)} y2={y(base)} stroke="#475569" strokeDasharray="4 4" />
      <text x={W - P} y={y(base) - 6} textAnchor="end" fontSize="11" fill="#94a3b8">本金</text>
      {norm.map(s => <path key={s.name} fill="none" stroke={s.color} strokeWidth="2"
        d={s.points.filter(p => at.has(p.date)).map((p, i) => `${i ? "L" : "M"}${x(at.get(p.date)!).toFixed(1)},${y(p.total).toFixed(1)}`).join("")} />)}
      <text x={P} y={H - 10} fontSize="11" fill="#94a3b8">{dates[0]}</text>
      <text x={W - P} y={H - 10} textAnchor="end" fontSize="11" fill="#94a3b8">{dates[dates.length - 1]}</text>
    </svg>
    <p className="mt-1 text-xs text-muted">每日收盤市值 ÷ 本金（一般策略未扣期末賣出的費稅）。</p>
  </div>;
}
