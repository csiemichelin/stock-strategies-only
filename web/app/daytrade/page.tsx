"use client";
import { useCallback, useEffect, useState } from "react";
import { api, DaytradeEvent, DaytradeResponse, DaytradeReview, errorMessage } from "@/lib/api";
import { EmptyState, ErrorCard, Skeleton } from "@/components/Feedback";
import { readCache, writeCache } from "@/lib/cache";

const money = (v: number | null | undefined) => (v == null ? "—" : `${v > 0 ? "+" : ""}${Math.round(v).toLocaleString("zh-TW")}`);
const tone = (v: number | null | undefined) => (v == null || v === 0 ? "" : v > 0 ? "text-rise" : "text-fall");
const hm = (iso?: string | null) => (iso ? iso.slice(11, 19) : "—");
const MODE: Record<string, string> = { paper: "紙上模擬", "kgi-sim": "凱基測試環境", live: "真錢" };
const TYPE: Record<string, string> = { macro: "總經濾網", preopen: "試撮體檢", alert: "可進場", entry: "進場", exit: "出場", halt: "停止交易", summary: "收盤總結", info: "提醒", error: "錯誤" };
const STATS: [string, string][] = [["liquidity", "量或金額不足"], ["change", "漲幅不在 3.5–6.5%"], ["vwap", "收盤低於均價"], ["volume_burst", "沒有爆量 2 倍"], ["daytrader", "隔日沖券商佔比太高"]];

function inTradingHours() {
  const tw = new Date(Date.now() + 8 * 3_600_000);
  const m = tw.getUTCHours() * 60 + tw.getUTCMinutes();
  return tw.getUTCDay() >= 1 && tw.getUTCDay() <= 5 && m >= 8 * 60 + 25 && m <= 13 * 60 + 35;
}

export default function DaytradePage() {
  const [data, setData] = useState<DaytradeResponse | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [tick, setTick] = useState(0);
  const refresh = useCallback(() => setTick(t => t + 1), []);

  useEffect(() => {
    const cached = readCache<DaytradeResponse>("daytrade");
    if (cached) { setData(cached); setLoading(false); }
  }, []);

  useEffect(() => {
    const controller = new AbortController();
    let timer: ReturnType<typeof setTimeout>;
    api.getDaytrade(controller.signal).then(res => {
      setData(res); setError(null); writeCache("daytrade", res);
    }).catch(err => {
      if (!controller.signal.aborted) setError(errorMessage(err));
    }).finally(() => {
      if (controller.signal.aborted) return;
      setLoading(false);
      timer = setTimeout(refresh, inTradingHours() ? 5_000 : 60_000);
    });
    return () => { controller.abort(); clearTimeout(timer); };
  }, [tick, refresh]);

  return <div className="space-y-8">
    <div><p className="eyebrow">DAY TRADE</p><h1 className="mt-2 text-3xl font-semibold tracking-tight">當沖：動能延續與極短線套利</h1>
      <p className="mt-2 max-w-3xl text-sm leading-6 text-muted">盤後選出隔天候選 → 08:30 看夜盤、台積電 ADR、那斯達克 → 08:59 看試撮 → 09:03–09:15 用「突破開盤高點」或「回測均價不破」進場 → 停損 0.8%、停利約 1%、15 分鐘不動就走。下單與行情由你電腦上的本機程式（凱基 API）執行，這頁只顯示它回報的結果。</p></div>
    {error && <ErrorCard message={error} retry={refresh} />}
    {loading && !data ? <Skeleton rows={3} /> : data && <>
      <Status data={data} />
      <Plan data={data} />
      <Today events={data.today.events} />
      <Accuracy reviews={data.reviews || []} />
      <History data={data} />
      <Backtest data={data} />
    </>}
  </div>;
}

function Status({ data }: { data: DaytradeResponse }) {
  const hb = data.heartbeat;
  const last = [hb.at, hb.agent_at].filter(Boolean).sort().pop() || null;
  const age = last ? (Date.now() - new Date(last).getTime()) / 60_000 : null;
  const alive = age != null && age < 10;
  return <div className="card flex flex-wrap items-center gap-x-6 gap-y-2 text-sm">
    <span className="flex items-center gap-2"><span className={`h-2.5 w-2.5 rounded-full ${alive ? "bg-emerald-400" : "bg-zinc-500"}`} />
      本機程式{last ? `最後回報 ${last.slice(5, 10)} ${hm(last)}` : "這次雲端重啟後還沒回報"}{hb.note ? `（${hb.note}）` : ""}</span>
    {hb.mode && <span className={`rounded-md px-2 py-0.5 text-xs ${hb.mode === "live" ? "bg-red-500/15 text-red-300" : "bg-accent/10 text-blue-300"}`}>{MODE[hb.mode] || hb.mode}</span>}
    <span className="text-muted">含手續費與當沖稅，停利 1% / 停損 0.8% 時，勝率要約 7 成才打平。</span>
  </div>;
}

function Plan({ data }: { data: DaytradeResponse }) {
  const plan = data.plan;
  if (!plan) return <EmptyState title="還沒有當沖名單">本機程式每天 15:10 會跑盤後選股並上傳；也可以在電腦上執行 <code>start_daytrade.bat screen</code> 馬上選一次。</EmptyState>;
  return <section className="card space-y-4">
    <div className="flex flex-wrap items-baseline justify-between gap-2"><h2 className="text-lg font-semibold">{plan.trade_date} 觀察名單</h2>
      <span className="text-xs text-muted">用 {plan.data_date} 收盤資料選出{plan.received_at ? `・上傳於 ${plan.received_at.slice(5, 16).replace("T", " ")}` : ""}</span></div>
    {plan.candidates.length === 0 ? <p className="text-sm text-muted">沒有符合條件的標的，這天不做當沖。</p> :
      <div className="overflow-x-auto"><table className="w-full text-sm">
        <thead className="text-left text-xs text-muted"><tr><th className="py-2 pr-3">股票</th><th className="pr-3 text-right">評分</th><th className="pr-3 text-right">收盤</th><th className="pr-3 text-right">漲幅</th><th className="pr-3 text-right">成交張數</th><th className="pr-3 text-right">量 / 5 日均</th><th className="pr-3 text-right">成交金額</th><th className="pr-3 text-right">均價</th><th className="text-right">隔日沖買超</th></tr></thead>
        <tbody>{plan.candidates.map(c => <tr key={c.stock_id} className="border-t border-line">
          <td className="py-2 pr-3">{c.stock_id} {c.name}</td>
          <td className="pr-3 text-right" title={c.score_parts ? Object.entries(c.score_parts).map(([k, v]) => `${k} ${v}`).join("、") : undefined}>{c.score == null ? "—" : <ScoreBadge score={c.score} />}</td>
          <td className="pr-3 text-right">{c.close}</td>
          <td className="pr-3 text-right text-rise">+{c.change_pct.toFixed(2)}%</td><td className="pr-3 text-right">{Math.round(c.volume_lots).toLocaleString()}</td>
          <td className="pr-3 text-right">{c.volume_ratio.toFixed(1)} 倍</td><td className="pr-3 text-right">{(c.amount / 1e8).toFixed(1)} 億</td>
          <td className="pr-3 text-right">{c.vwap}</td><td className="text-right">{c.daytrader_share == null ? "—" : `${(c.daytrader_share * 100).toFixed(1)}%`}</td></tr>)}</tbody>
      </table></div>}
    {plan.stats?.total != null && <p className="text-xs leading-5 text-muted">全市場 {plan.stats.total} 檔：{STATS.filter(([k]) => plan.stats[k]).map(([k, l]) => `${l} ${plan.stats[k]}`).join("、")}，入選 {plan.candidates.length} 檔。</p>}
    {plan.notes?.length > 0 && <ul className="list-disc pl-5 text-xs text-muted">{plan.notes.map(n => <li key={n}>{n}</li>)}</ul>}
  </section>;
}

function Today({ events }: { events: DaytradeEvent[] }) {
  const exits = events.filter(e => e.type === "exit");
  const pnl = exits.reduce((s, e) => s + (e.pnl || 0), 0);
  return <section className="card space-y-3">
    <div className="flex flex-wrap items-baseline justify-between gap-2"><h2 className="text-lg font-semibold">今天</h2>
      {exits.length > 0 && <span className="text-sm">已出場 {exits.length} 筆，損益 <b className={tone(pnl)}>{money(pnl)}</b> 元</span>}</div>
    {events.length === 0 ? <p className="text-sm text-muted">今天還沒有回報。交易日 08:30 起本機程式會陸續回報，這頁每 5 秒更新。</p> :
      <ol className="space-y-2 text-sm">{[...events].reverse().map((e, i) => <li key={`${e.ts}-${i}`} className="flex gap-3 border-t border-line pt-2 first:border-0 first:pt-0">
        <span className="w-16 shrink-0 font-mono text-xs text-muted">{hm(e.ts)}</span>
        <span className={`w-16 shrink-0 text-xs ${e.ok === false || e.type === "error" || e.type === "halt" ? "text-fall" : e.type === "entry" ? "text-blue-300" : "text-muted"}`}>{TYPE[e.type] || e.type}</span>
        <span className="min-w-0 flex-1">{e.symbol && <b className="mr-2">{e.symbol} {e.name}</b>}
          {e.price != null && <span className="mr-2">@{e.price}{e.shares ? `・${e.shares / 1000} 張` : ""}</span>}
          {e.pnl != null && <b className={`mr-2 ${tone(e.pnl)}`}>{money(e.pnl)} 元</b>}<span className="text-muted">{e.reason}</span></span>
      </li>)}</ol>}
  </section>;
}

function History({ data }: { data: DaytradeResponse }) {
  const h = data.history;
  if (!h.length) return null;
  const total = h.reduce((s, d) => s + d.pnl, 0), trades = h.reduce((s, d) => s + d.trades, 0), wins = h.reduce((s, d) => s + d.wins, 0);
  return <section className="card space-y-3">
    <div className="flex flex-wrap items-baseline justify-between gap-2"><h2 className="text-lg font-semibold">每日紀錄</h2>
      <span className="text-sm">{h.length} 天、{trades} 筆、勝率 {trades ? Math.round(wins / trades * 100) : 0}%，累計 <b className={tone(total)}>{money(total)}</b> 元</span></div>
    <div className="overflow-x-auto"><table className="w-full text-sm"><thead className="text-left text-xs text-muted"><tr><th className="py-2 pr-3">日期</th><th className="pr-3">模式</th><th className="pr-3 text-right">筆數</th><th className="pr-3 text-right">賺錢筆數</th><th className="text-right">損益（元）</th></tr></thead>
      <tbody>{[...h].reverse().slice(0, 30).map(d => <tr key={d.date} className="border-t border-line"><td className="py-2 pr-3">{d.date}</td><td className="pr-3 text-muted">{MODE[d.mode || ""] || "—"}</td><td className="pr-3 text-right">{d.trades}</td><td className="pr-3 text-right">{d.wins}</td><td className={`text-right ${tone(d.pnl)}`}>{money(d.pnl)}</td></tr>)}</tbody></table></div>
  </section>;
}

function Backtest({ data }: { data: DaytradeResponse }) {
  const b = data.backtest;
  if (!b) return <p className="text-xs text-muted">還沒有回測結果。可在電腦上執行 <code>start_daytrade.bat backtest</code>（用凱基歷史 3 分 K 粗估）。</p>;
  const s = b.summary;
  const items: [string, string, string?][] = [["回測天數", `${s.days} 天`], ["交易筆數", `${s.trades} 筆（突破 ${s.by_mode?.A ?? 0}／均價 ${s.by_mode?.B ?? 0}）`],
    ["勝率", s.win_rate == null ? "—" : `${s.win_rate}%`, s.win_rate != null && s.win_rate >= s.breakeven_win_rate ? "text-rise" : "text-fall"],
    ["打平勝率", `${s.breakeven_win_rate}%`], ["總損益", `${money(s.pnl)} 元`, tone(s.pnl)], ["獲利因子", s.profit_factor == null ? "—" : String(s.profit_factor)]];
  return <section className="card space-y-3"><div className="flex flex-wrap items-baseline justify-between gap-2"><h2 className="text-lg font-semibold">回測（3 分 K 粗估）</h2>
    {b.received_at && <span className="text-xs text-muted">{b.received_at.slice(0, 16).replace("T", " ")}</span>}</div>
    <dl className="grid grid-cols-2 gap-3 sm:grid-cols-3">{items.map(([k, v, c]) => <div key={k}><dt className="text-xs text-muted">{k}</dt><dd className={`mt-1 font-semibold ${c || ""}`}>{v}</dd></div>)}</dl>
    {b.note && <p className="text-xs text-muted">{b.note}</p>}
  </section>;
}

const GOOD = 60;
const OUT: Record<string, [string, string]> = { win: ["達標", "text-rise"], loss: ["停損", "text-fall"], flat: ["持平", "text-muted"], no_data: ["無資料", "text-muted"] };

function ScoreBadge({ score }: { score: number }) {
  const cls = score >= 80 ? "bg-rise/20 text-rise" : score >= GOOD ? "bg-amber-400/15 text-amber-300" : "bg-panel2 text-muted";
  return <span className={`rounded-md px-2 py-0.5 font-mono text-xs ${cls}`}>{score >= GOOD ? "⭐ " : ""}{score}</span>;
}

function Accuracy({ reviews }: { reviews: DaytradeReview[] }) {
  const items = reviews.flatMap(r => r.items.filter(i => i.outcome !== "no_data").map(i => ({ ...i, date: r.trade_date })));
  if (!items.length) return <section className="card space-y-2"><h2 className="text-lg font-semibold">推薦命中率</h2>
    <p className="text-sm text-muted">每個交易日 13:40 後，當沖程式會用凱基 3 分 K 檢查名單：如果 09:03 進場，是先碰到停利 +1% 還是停損 -0.8%。累積幾天後這裡會出現命中率與圖表。評分 ≥ {GOOD} 分（⭐）算「推薦」。</p></section>;
  const rate = (xs: typeof items) => (xs.length ? Math.round(xs.filter(i => i.outcome === "win").length / xs.length * 1000) / 10 : null);
  const rec = items.filter(i => i.score >= GOOD), rest = items.filter(i => i.score < GOOD);
  const avg = items.reduce((s, i) => s + (i.ret_pct || 0), 0) / items.length;
  const buckets: [string, number, number][] = [["< 50", 0, 50], ["50–59", 50, 60], ["60–69", 60, 70], ["70–79", 70, 80], ["≥ 80", 80, 101]];
  const byBucket = buckets.map(([label, lo, hi]) => { const xs = items.filter(i => i.score >= lo && i.score < hi); return { label, n: xs.length, rate: rate(xs), avg: xs.length ? xs.reduce((s, i) => s + (i.ret_pct || 0), 0) / xs.length : null }; });
  let cw = 0, cn = 0;
  const daily = reviews.filter(r => r.items.some(i => i.outcome !== "no_data")).map(r => {
    const xs = r.items.filter(i => i.outcome !== "no_data" && i.score >= GOOD);
    cw += xs.filter(i => i.outcome === "win").length; cn += xs.length;
    return { date: r.trade_date, day: rate(xs as typeof items), cum: cn ? cw / cn * 100 : null, n: xs.length };
  });
  const latest = reviews[reviews.length - 1];
  const be = 69;
  return <section className="card space-y-5">
    <div className="flex flex-wrap items-baseline justify-between gap-2"><h2 className="text-lg font-semibold">推薦命中率</h2>
      <span className="text-xs text-muted">「命中」＝ 09:03 進場後先碰到停利 +1%（同一根 K 棒兩邊都碰到算停損）；報酬已扣手續費與當沖稅</span></div>
    <dl className="grid grid-cols-2 gap-3 sm:grid-cols-5">
      {([["檢討天數", `${daily.length} 天`], ["推薦（⭐）命中率", rec.length ? `${rate(rec)}%` : "—", rec.length ? `${rec.length} 檔` : ""], ["未推薦命中率", rest.length ? `${rate(rest)}%` : "—", rest.length ? `${rest.length} 檔` : ""],
        ["全部命中率", `${rate(items)}%`, `${items.length} 檔`], ["平均每筆報酬", `${avg > 0 ? "+" : ""}${avg.toFixed(2)}%`]] as [string, string, string?][]).map(([k, v, sub]) =>
        <div key={k} className="rounded-lg bg-panel2/60 p-3"><dt className="text-xs text-muted">{k}</dt><dd className="mt-1 font-mono text-lg">{v}</dd>{sub && <p className="text-xs text-muted">{sub}</p>}</div>)}
    </dl>
    <div className="grid gap-6 lg:grid-cols-2">
      <div><h3 className="mb-2 text-sm font-medium">分數越高越準嗎？（各分數區間命中率）</h3><BucketChart data={byBucket} breakeven={be} /></div>
      <div><h3 className="mb-2 text-sm font-medium">推薦（⭐）命中率走勢</h3><TrendChart data={daily} breakeven={be} /></div>
    </div>
    {latest && <div className="overflow-x-auto"><h3 className="mb-2 text-sm font-medium">{latest.trade_date} 檢討明細</h3>
      <table className="w-full min-w-[640px] text-sm"><thead className="text-left text-xs text-muted"><tr><th className="py-2 pr-3">股票</th><th className="pr-3 text-right">評分</th><th className="pr-3 text-right">開盤跳空</th><th className="pr-3 text-right">09:03 價</th><th className="pr-3">結果</th><th className="pr-3 text-right">報酬</th><th className="pr-3 text-right">盤中最高</th><th className="text-right">盤中最低</th></tr></thead>
        <tbody>{latest.items.map(i => <tr key={i.stock_id} className="border-t border-line">
          <td className="py-2 pr-3">{i.stock_id} {i.name}</td><td className="pr-3 text-right"><ScoreBadge score={i.score} /></td>
          <td className="pr-3 text-right font-mono">{i.gap_pct == null ? "—" : `${i.gap_pct > 0 ? "+" : ""}${i.gap_pct}%`}</td><td className="pr-3 text-right font-mono">{i.entry ?? "—"}</td>
          <td className={`pr-3 ${OUT[i.outcome][1]}`}>{OUT[i.outcome][0]}{i.exit_time && i.outcome !== "flat" ? ` ${i.exit_time}` : ""}</td>
          <td className={`pr-3 text-right font-mono ${tone(i.ret_pct)}`}>{i.ret_pct == null ? "—" : `${i.ret_pct > 0 ? "+" : ""}${i.ret_pct}%`}</td>
          <td className="pr-3 text-right font-mono text-rise">{i.max_up_pct == null ? "—" : `+${i.max_up_pct}%`}</td><td className="text-right font-mono text-fall">{i.max_down_pct == null ? "—" : `${i.max_down_pct}%`}</td></tr>)}</tbody></table></div>}
    <p className="text-xs leading-5 text-muted">停利 1%、停損 0.8% 加上手續費與當沖稅，命中率要約 {be}% 才打平（圖中虛線）。樣本少於 30 檔時命中率波動很大，先別下結論。</p>
  </section>;
}

function BucketChart({ data, breakeven }: { data: { label: string; n: number; rate: number | null }[]; breakeven: number }) {
  const W = 400, H = 200, P = 28, bw = (W - 2 * P) / data.length;
  const y = (v: number) => H - P - (v / 100) * (H - 2 * P);
  return <svg viewBox={`0 0 ${W} ${H}`} className="w-full" role="img" aria-label={data.map(d => `${d.label} 分 ${d.rate ?? "無資料"}%`).join("，")}>
    <line x1={P} x2={W - P} y1={y(breakeven)} y2={y(breakeven)} stroke="#f87171" strokeDasharray="4 4" />
    <text x={P} y={y(breakeven) - 4} fontSize="10" fill="#f87171">打平 {breakeven}%</text>
    {data.map((d, i) => <g key={d.label}>
      {d.rate != null && <rect x={P + i * bw + 6} y={y(d.rate)} width={bw - 12} height={H - P - y(d.rate)} rx="3" fill={d.label.startsWith("< 50") || d.label.startsWith("50") ? "#64748b" : "#fbbf24"} />}
      <text x={P + i * bw + bw / 2} y={d.rate != null ? y(d.rate) - 4 : H - P - 4} textAnchor="middle" fontSize="10" fill="#cbd5e1">{d.rate != null ? `${d.rate}%` : "—"}</text>
      <text x={P + i * bw + bw / 2} y={H - 12} textAnchor="middle" fontSize="10" fill="#94a3b8">{d.label}</text>
      <text x={P + i * bw + bw / 2} y={H - 1} textAnchor="middle" fontSize="9" fill="#64748b">{d.n} 檔</text>
    </g>)}
  </svg>;
}

function TrendChart({ data, breakeven }: { data: { date: string; day: number | null; cum: number | null; n: number }[]; breakeven: number }) {
  if (data.length < 2) return <p className="text-sm text-muted">至少要兩天的檢討才畫得出走勢。</p>;
  const W = 400, H = 200, P = 28;
  const x = (i: number) => P + (i / (data.length - 1)) * (W - 2 * P);
  const y = (v: number) => H - P - (v / 100) * (H - 2 * P);
  const cum = data.map((d, i) => (d.cum == null ? null : `${x(i).toFixed(1)},${y(d.cum).toFixed(1)}`)).filter(Boolean);
  return <svg viewBox={`0 0 ${W} ${H}`} className="w-full" role="img" aria-label={`累計推薦命中率 ${data[data.length - 1].cum?.toFixed(1) ?? "—"}%`}>
    {[0, 50, 100].map(v => <text key={v} x={P - 4} y={y(v) + 3} textAnchor="end" fontSize="9" fill="#64748b">{v}%</text>)}
    <line x1={P} x2={W - P} y1={y(breakeven)} y2={y(breakeven)} stroke="#f87171" strokeDasharray="4 4" />
    {data.map((d, i) => d.day != null && <circle key={d.date} cx={x(i)} cy={y(d.day)} r={2 + Math.min(d.n, 5)} fill="#60a5fa" opacity="0.35"><title>{`${d.date} 當天 ${d.day}%（${d.n} 檔）`}</title></circle>)}
    {cum.length > 1 && <polyline fill="none" stroke="#fbbf24" strokeWidth="2" points={cum.join(" ")} />}
    <text x={P} y={H - 8} fontSize="10" fill="#94a3b8">{data[0].date.slice(5)}</text>
    <text x={W - P} y={H - 8} textAnchor="end" fontSize="10" fill="#94a3b8">{data[data.length - 1].date.slice(5)}</text>
    <text x={P} y={12} fontSize="10" fill="#fbbf24">━ 累計</text><text x={P + 50} y={12} fontSize="10" fill="#60a5fa">● 當天</text>
  </svg>;
}
