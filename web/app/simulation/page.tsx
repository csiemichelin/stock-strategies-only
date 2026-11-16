"use client";
import { useCallback, useEffect, useMemo, useState } from "react";
import { api, errorMessage, SimAccount, SimJob, SimPoint, Strategy } from "@/lib/api";
import { EmptyState, ErrorCard, Skeleton } from "@/components/Feedback";

const money = (v: number | null | undefined, digits = 0) =>
  v == null ? "—" : v.toLocaleString("zh-TW", { minimumFractionDigits: digits, maximumFractionDigits: digits });
const signed = (v: number, digits = 0) => `${v > 0 ? "+" : ""}${money(v, digits)}`;
const tone = (v: number | null | undefined) => (v == null || v === 0 ? "text-muted" : v > 0 ? "text-rise" : "text-fall");
const isoDaysAgo = (days: number) => {
  const d = new Date(Date.now() - days * 86_400_000 + 8 * 3_600_000);
  return d.toISOString().slice(0, 10);
};

export default function SimulationPage() {
  const [account, setAccount] = useState<SimAccount | null>(null);
  const [job, setJob] = useState<SimJob | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [tick, setTick] = useState(0);
  const refresh = useCallback(() => setTick(t => t + 1), []);

  useEffect(() => {
    const controller = new AbortController();
    let timer: ReturnType<typeof setTimeout>;
    api.getSim(controller.signal).then(res => {
      setAccount(res.account); setJob(res.job); setError(null);
      timer = setTimeout(refresh, res.job.status === "running" ? 2_000 : 30_000);
    }).catch(err => {
      if (controller.signal.aborted) return;
      setError(errorMessage(err)); timer = setTimeout(refresh, 10_000);
    }).finally(() => { if (!controller.signal.aborted) setLoading(false); });
    return () => { controller.abort(); clearTimeout(timer); };
  }, [tick, refresh]);

  const replaying = job?.status === "running";
  const running = account?.status === "running";
  return <div className="space-y-8">
    <div><p className="eyebrow">PAPER TRADING</p><h1 className="mt-2 text-3xl font-semibold tracking-tight">模擬交易</h1>
      <p className="mt-2 max-w-3xl text-sm leading-6 text-muted">用跟 LINE 每日選股同一套邏輯自動買賣：只買 BUY 訊號、隔天開盤價進場（零股）、碰到停損或停利就賣、最多持有 {account?.params.hold_days ?? 20} 個交易日。照實扣手續費 0.1425%（低消 1 元）與證交稅。所有裝置看到同一個帳戶。</p></div>
    {error && <ErrorCard message={error} retry={refresh} />}
    {loading ? <Skeleton rows={2} /> : replaying ? <ReplayProgress job={job!} onChange={refresh} /> : running ? <Account account={account!} onChange={refresh} /> : <>
      <StartForm onStarted={refresh} lastJob={job} />
      {account && <div className="space-y-3"><h2 className="text-lg font-medium">上次模擬結果</h2><Account account={account} onChange={refresh} /></div>}
    </>}
  </div>;
}

function StartForm({ onStarted, lastJob }: { onStarted: () => void; lastJob: SimJob | null }) {
  const [strategies, setStrategies] = useState<Strategy[]>([]);
  const [strategyId, setStrategyId] = useState("default");
  const [capital, setCapital] = useState(10000);
  const [mode, setMode] = useState<"forward" | "replay">("forward");
  const [startDate, setStartDate] = useState(isoDaysAgo(90));
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  useEffect(() => { api.listStrategies().then(d => setStrategies(d.strategies.filter(s => !s.error))).catch(() => {}); }, []);
  async function start() {
    setBusy(true); setError(null);
    try { await api.startSim(capital, strategyId, mode === "replay" ? startDate : undefined); onStarted(); }
    catch (err) { setError(errorMessage(err)); } finally { setBusy(false); }
  }
  return <section className="card space-y-5">
    <h2 className="font-medium">開始新的模擬</h2>
    <div className="grid gap-5 md:grid-cols-3">
      <div><label htmlFor="capital" className="label">投入資金（元）</label>
        <input id="capital" type="number" min={1000} step={1000} className="input" value={capital} onChange={e => setCapital(Number(e.target.value))} />
        <p className="mt-1 text-xs text-muted">預設 1 萬；每檔最多投入總資產 20%。</p></div>
      <div><label htmlFor="sim-strategy" className="label">策略</label>
        <select id="sim-strategy" className="input" value={strategyId} onChange={e => setStrategyId(e.target.value)}>
          {(strategies.length ? strategies : [{ id: "default", name: "預設策略" } as Strategy]).map(s => <option key={s.id} value={s.id}>{s.name}</option>)}
        </select></div>
      <div><span className="label">開始方式</span>
        <div className="mt-2 space-y-2 text-sm">
          <label className="flex items-center gap-2"><input type="radio" checked={mode === "forward"} onChange={() => setMode("forward")} />從今天開始，每天收盤後自動交易</label>
          <label className="flex items-center gap-2"><input type="radio" checked={mode === "replay"} onChange={() => setMode("replay")} />從過去某天開始回放，再接著每天交易</label>
          {mode === "replay" && <input type="date" className="input" value={startDate} min={isoDaysAgo(365)} max={isoDaysAgo(1)} onChange={e => setStartDate(e.target.value)} />}
        </div></div>
    </div>
    {mode === "replay" && <p className="text-xs leading-6 text-muted">回放會逐日用當天以前的股價重跑選股（基本面用最新年報、不含夜盤濾鏡），約需數分鐘，期間會用掉不少 FinMind 額度。</p>}
    <div className="flex items-center gap-3"><button className="btn-primary" disabled={busy || capital < 1000} onClick={start}>{busy ? "啟動中…" : "開始模擬"}</button>
      {lastJob?.status === "failed" && <span className="text-xs text-red-300">上次回放失敗：{lastJob.error}</span>}</div>
    {error && <ErrorCard message={error} />}
  </section>;
}

function ReplayProgress({ job, onChange }: { job: SimJob; onChange: () => void }) {
  const [busy, setBusy] = useState(false);
  return <section className="card space-y-3" role="status" aria-live="polite">
    <div className="flex items-center justify-between gap-3"><h2 className="font-medium">歷史回放中</h2>
      <button className="btn-ghost" disabled={busy} onClick={async () => { setBusy(true); try { await api.stopSim(); } finally { setBusy(false); onChange(); } }}>取消回放</button></div>
    <div className="flex justify-between text-sm"><span>{job.phase}{job.current ? `：${job.current}` : "…"}</span><span className="font-mono text-muted">{job.completed} / {job.total ?? "—"}</span></div>
    <progress className="h-1.5 w-full" max={job.total || 1} value={job.total ? job.completed : undefined} />
    <p className="text-xs text-muted">所有裝置都會看到這個進度。完成後會自動顯示模擬帳戶。</p>
  </section>;
}

function Account({ account, onChange }: { account: SimAccount; onChange: () => void }) {
  const s = account.summary;
  const [confirming, setConfirming] = useState(false);
  const [busy, setBusy] = useState(false);
  const running = account.status === "running";
  const trades = useMemo(() => [...account.trades].reverse(), [account.trades]);
  async function stop() {
    setBusy(true);
    try { await api.stopSim(); onChange(); } finally { setBusy(false); setConfirming(false); }
  }
  return <div className="space-y-6">
    <div className="flex flex-wrap items-center justify-between gap-3 text-sm text-muted">
      <span>{running ? "🟢 模擬進行中" : "⏹ 已中止"}・策略 {account.strategy.name}・{account.mode === "replay" ? "回放" : "開始"}於 {account.start_date}・最後更新 {account.last_date ?? "等待今天收盤"}</span>
      {running && (confirming
        ? <span className="flex items-center gap-2">以最後收盤價賣出全部持股並結束？<button className="btn-primary" disabled={busy} onClick={stop}>確定中止</button><button className="btn-ghost" onClick={() => setConfirming(false)}>取消</button></span>
        : <button className="btn-ghost" onClick={() => setConfirming(true)}>中止模擬</button>)}
    </div>
    <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-4">
      <Stat label="總資產" value={money(s.total)} sub={`本金 ${money(account.initial_capital)}`} />
      <Stat label="累計損益" value={signed(s.pnl)} sub={`${s.pnl_pct > 0 ? "+" : ""}${s.pnl_pct.toFixed(2)}%`} cls={tone(s.pnl)} />
      <Stat label="現金 / 持股市值" value={money(s.cash)} sub={`持股 ${money(s.holdings)}`} />
      <Stat label="已結算交易" value={`${s.closed_trades} 筆`} sub={`勝率 ${s.win_rate == null ? "—" : `${s.win_rate}%`}・最大回撤 ${s.max_drawdown_pct.toFixed(2)}%`} />
    </div>
    <section className="card"><h2 className="mb-3 font-medium">資產變化</h2><EquityChart points={account.equity} base={account.initial_capital} /></section>
    <section className="card overflow-x-auto"><h2 className="mb-3 font-medium">目前持倉（{account.positions.length}）</h2>
      {account.positions.length ? <table className="w-full min-w-[720px] text-sm"><thead className="text-left text-xs text-muted"><tr>
        <th className="py-2">股票</th><th>股數</th><th>成本價</th><th>現價</th><th>損益</th><th>停損 / 停利</th><th>持有</th></tr></thead>
        <tbody>{account.positions.map(p => { const pnl = p.last_price * p.shares - p.cost; return <tr key={p.stock_id} className="border-t border-line">
          <td className="py-2">{p.stock_id} {p.name}<div className="text-xs text-muted">{p.entry_date} 買進</div></td>
          <td className="font-mono">{p.shares}</td><td className="font-mono">{money(p.entry_price, 2)}</td><td className="font-mono">{money(p.last_price, 2)}</td>
          <td className={`font-mono ${tone(pnl)}`}>{signed(pnl)}<div className="text-xs">{signed(pnl / p.cost * 100, 2)}%</div></td>
          <td className="font-mono text-xs">{money(p.stop_price, 2)} / {money(p.target_price, 2)}</td>
          <td className="font-mono text-xs">{p.days_held} / {account.params.hold_days} 日</td></tr>; })}</tbody></table>
        : <p className="text-sm text-muted">目前沒有持股{running ? "，等待 BUY 訊號（大盤轉弱或沒有好標的時就不買）。" : "。"}</p>}
      {running && !!account.pending.length && <p className="mt-3 text-sm">⏳ 明天開盤買進：{account.pending.map(o => `${o.stock_id} ${o.name}`).join("、")}</p>}
    </section>
    <section className="card overflow-x-auto"><h2 className="mb-3 font-medium">交易紀錄</h2>
      {trades.length ? <table className="w-full min-w-[720px] text-sm"><thead className="text-left text-xs text-muted"><tr><th className="py-2">日期</th><th>買賣</th><th>股票</th><th>股數 × 價格</th><th>損益</th><th>原因</th></tr></thead>
        <tbody>{trades.map((t, i) => <tr key={i} className="border-t border-line">
          <td className="py-2 font-mono text-xs">{t.date}</td><td className={t.side === "BUY" ? "text-rise" : "text-fall"}>{t.side === "BUY" ? "買" : "賣"}</td>
          <td>{t.stock_id} {t.name}</td><td className="font-mono">{t.shares} × {money(t.price, 2)}</td>
          <td className={`font-mono ${tone(t.pnl)}`}>{t.pnl == null ? "—" : `${signed(t.pnl)}（${signed(t.pnl_pct ?? 0, 2)}%）`}</td>
          <td className="text-xs text-muted">{t.reason}</td></tr>)}</tbody></table>
        : <p className="text-sm text-muted">還沒有交易。</p>}
    </section>
  </div>;
}

function Stat({ label, value, sub, cls = "" }: { label: string; value: string; sub?: string; cls?: string }) {
  return <div className="card"><p className="label">{label}</p><div className={`mt-2 font-mono text-2xl ${cls}`}>{value}</div>{sub && <p className={`mt-1 text-xs ${cls || "text-muted"}`}>{sub}</p>}</div>;
}

function EquityChart({ points, base }: { points: SimPoint[]; base: number }) {
  if (points.length < 2) return <EmptyState title="資料累積中">每個交易日收盤後會新增一個點。</EmptyState>;
  const W = 800, H = 220, P = 32;
  const values = points.map(p => p.total);
  const min = Math.min(base, ...values), max = Math.max(base, ...values);
  const span = max - min || 1;
  const x = (i: number) => P + (i / (points.length - 1)) * (W - 2 * P);
  const y = (v: number) => H - P - ((v - min) / span) * (H - 2 * P);
  const path = points.map((p, i) => `${i ? "L" : "M"}${x(i).toFixed(1)},${y(p.total).toFixed(1)}`).join("");
  const last = points[points.length - 1].total;
  const color = last >= base ? "#f87171" : "#4ade80";
  return <svg viewBox={`0 0 ${W} ${H}`} className="w-full" role="img" aria-label={`總資產從 ${money(points[0].total)} 變成 ${money(last)}`}>
    <line x1={P} x2={W - P} y1={y(base)} y2={y(base)} stroke="#475569" strokeDasharray="4 4" />
    <text x={W - P} y={y(base) - 6} textAnchor="end" fontSize="11" fill="#94a3b8">本金 {money(base)}</text>
    <path d={path} fill="none" stroke={color} strokeWidth="2" />
    <text x={P} y={H - 8} fontSize="11" fill="#94a3b8">{points[0].date}</text>
    <text x={W - P} y={H - 8} textAnchor="end" fontSize="11" fill="#94a3b8">{points[points.length - 1].date}・{money(last)}</text>
  </svg>;
}
