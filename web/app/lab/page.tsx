"use client";
import { useEffect, useState } from "react";
import { api, errorMessage, LabCandidate, LabFold, LabReport, LabSide } from "@/lib/api";
import { EmptyState, ErrorCard, Skeleton } from "@/components/Feedback";

const COLORS = ["#60a5fa", "#a78bfa", "#34d399", "#f472b6", "#fb923c"];
const BENCH_COLOR = "#fbbf24";
const UNIVERSE_COLOR = "#94a3b8";
const VERDICT: Record<LabCandidate["verdict"], { text: string; cls: string }> = {
  strong: { text: "證據強", cls: "border-rise/50 bg-rise/10 text-rise" },
  tentative: { text: "初步有證據", cls: "border-watch/50 bg-watch/10 text-watch" },
  promising: { text: "有跡象，名單偏誤未排除", cls: "border-accent/40 bg-accent/10 text-blue-300" },
  none: { text: "沒有足夠證據", cls: "border-line text-muted" },
};
const pct = (v: number | null | undefined, sign = true) => (v == null ? "—" : (sign && v > 0 ? "+" : "") + v.toFixed(2) + "%");
const tone = (v: number | null | undefined) => (v == null || v === 0 ? "" : v > 0 ? "text-rise" : "text-fall");

export default function LabPage() {
  const [report, setReport] = useState<LabReport | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  useEffect(() => {
    const controller = new AbortController();
    api.getLab(controller.signal).then(d => setReport(d.report))
      .catch(err => { if (!controller.signal.aborted) setError(errorMessage(err)); })
      .finally(() => { if (!controller.signal.aborted) setLoading(false); });
    return () => controller.abort();
  }, []);
  return <div className="space-y-8">
    <div><p className="eyebrow">STRATEGY LAB</p><h1 className="mt-2 text-3xl font-semibold tracking-tight">策略實驗室</h1>
      <p className="mt-2 max-w-3xl text-sm leading-6 text-muted">用多年的「樣本外」結果，檢驗每個策略有沒有證據贏過抱 0050：每半年一段，只用那一段之前的資料決定參數，再看接下來半年的實際成績。手續費、證交稅、除權息都算進去。這裡是驗證工具，不是投資建議。</p></div>
    {loading ? <Skeleton rows={4} /> : error ? <ErrorCard message={error} /> : !report
      ? <EmptyState title="還沒有驗證結果">每個月會自動跑一次（週六早上）；也可以到 GitHub 的 Actions 頁手動執行「Strategy Lab」。第一次約需一到兩個小時。</EmptyState>
      : <Report report={report} />}
  </div>;
}

function Report({ report }: { report: LabReport }) {
  const b = report.benchmark;
  const cands = report.candidates;
  const u = report.universe;
  const passed = cands.filter(c => c.verdict === "strong" || c.verdict === "tentative");
  const promising = cands.filter(c => c.verdict === "promising");
  return <div className="space-y-6">
    <div className={"card text-base leading-7 " + (passed.length ? "border-rise/40" : "border-line")}>
      {passed.length
        ? <>有樣本外證據贏過 0050 的策略：<b>{passed.map(c => c.name).join("、")}</b>。</>
        : <>目前<b>沒有</b>任何候選策略有足夠的樣本外證據贏過抱 0050。</>}
      {promising.length > 0 && <> <b>{promising.map(c => c.name).join("、")}</b> 的數字贏過 0050，也贏過「等權重持有同一份名單」的對照，算是有跡象；但它們是從「現在的名單」裡選股，數字有虛胖，要換成當時的名單重新驗證才算數。</>}
      <span className="block text-sm text-muted">樣本外期間 {report.period.start} ～ {report.period.end}（約 {report.period.years} 年、{b.folds.length} 個半年區段）・同期{b.name}年化 {pct(b.cagr_pct)}、最大回撤 {pct(b.mdd_pct, false)}・上次驗證 {report.generated_at.slice(0, 10)}</span>
    </div>
    <section className="card overflow-x-auto"><table className="w-full min-w-[900px] text-sm">
      <thead className="text-left text-xs text-muted"><tr><th className="py-2">策略</th><th>樣本外年化</th><th>比 0050</th><th>比名單對照</th><th>最大回撤</th><th>贏的區段</th><th>t 值</th><th>平均持股</th><th>每年成本</th><th>結論</th></tr></thead>
      <tbody>
        <tr className="border-t border-line"><td className="py-2"><span style={{ color: BENCH_COLOR }}>●</span> {b.name}<div className="text-xs text-muted">對照組：買進後一直抱著</div></td>
          <td className={tone(b.cagr_pct)}>{pct(b.cagr_pct)}</td><td>—</td><td>—</td><td className="text-fall">{pct(b.mdd_pct, false)}</td><td>—</td><td>—</td><td>100%</td><td>—</td><td>—</td></tr>
        {u && <tr className="border-t border-line"><td className="py-2"><span style={{ color: UNIVERSE_COLOR }}>●</span> {u.name}<div className="max-w-xs text-xs leading-5 text-muted">對照組：現在名單裡的 {u.stocks} 檔個股全部等權重買進抱著。它贏 0050 的部分是「事後挑名單」的虛胖，不是選股能力。</div></td>
          <td className={tone(u.cagr_pct)}>{pct(u.cagr_pct)}</td><td className={tone(u.excess_cagr_pct)}>{pct(u.excess_cagr_pct)}</td><td>—</td><td className="text-fall">{pct(u.mdd_pct, false)}</td><td>—</td><td>—</td><td>100%</td><td>—</td><td>—</td></tr>}
        {cands.map((c, i) => <tr key={c.id} className="border-t border-line align-top">
          <td className="py-2"><span style={{ color: COLORS[i % COLORS.length] }}>●</span> {c.name}
            <div className="max-w-xs text-xs leading-5 text-muted">{c.desc}{c.current ? "（目前參數：" + c.current + "）" : ""}</div></td>
          <td className={tone(c.cagr_pct)}>{pct(c.cagr_pct)}</td>
          <td className={tone(c.excess_cagr_pct)}>{pct(c.excess_cagr_pct)}</td>
          <td className={c.vs_universe ? tone(c.vs_universe.excess_cagr_pct) : ""}>{c.vs_universe ? <>{pct(c.vs_universe.excess_cagr_pct)}<div className="text-xs text-muted">t {c.vs_universe.t_stat.toFixed(2)}</div></> : "—"}</td>
          <td className="text-fall">{pct(c.mdd_pct, false)}</td>
          <td>{c.win_folds} / {c.folds.length}</td>
          <td>{c.t_stat.toFixed(2)}</td>
          <td>{c.exposure_pct}%</td>
          <td>{c.cost_pct_per_year}%</td>
          <td><span className={"whitespace-nowrap rounded-md border px-2 py-0.5 text-xs " + VERDICT[c.verdict].cls}>{VERDICT[c.verdict].text}</span></td>
        </tr>)}
      </tbody></table></section>
    <section className="card space-y-3"><h2 className="font-medium">樣本外累積績效（起點 = 100）</h2><Chart bench={b} universe={u} cands={cands} /></section>
    <section className="card space-y-3"><h2 className="font-medium">每個半年區段有沒有贏 0050</h2>
      <p className="text-xs text-muted">每一格是一個半年區段：紅色＝那半年贏 0050，綠色＝輸，滑鼠移上去看數字。只有少數幾格贏、其他都輸，就不算穩定。</p>
      <div className="space-y-2">{cands.map(c => <div key={c.id} className="flex flex-wrap items-center gap-2 text-sm"><span className="w-40 shrink-0">{c.name}</span>
        <span className="flex flex-wrap gap-1">{c.folds.map(f => <span key={f.start} title={foldTitle(f)} className={"h-5 w-5 rounded-sm " + (f.excess_pct > 0 ? "bg-rise/70" : "bg-fall/50")} />)}</span></div>)}</div></section>
    <section className="card space-y-2 text-sm leading-6"><h2 className="font-medium">怎麼讀、有哪些限制</h2>
      <ul className="list-disc space-y-1 pl-5 text-muted">
        <li>「樣本外」＝決定參數時還看不到的資料。一般回測挑最漂亮的參數是事後諸葛，這裡不行。</li>
        <li>結論規則事先寫死：樣本外年化贏 0050、過半區段都贏、月超額報酬的 t 值 ≥ 2 才算「初步有證據」，t ≥ 3 才算「證據強」。從名單裡選股的策略還要用同樣的標準贏過「名單對照」，而且最多只標成「有跡象」。這次總共試了 {report.trials} 組參數；試得越多越容易碰巧好看，所以門檻要嚴。</li>
        {report.notes.map(n => <li key={n}>{n}</li>)}
        <li>資料：{report.data.stocks} 檔（其中 {report.data.companies} 檔個股；{report.data.with_revenue} 檔有月營收、{report.data.with_fundamentals} 檔有歷年財報），以本金 {report.capital.toLocaleString()} 元模擬。每月自動重新驗證一次。</li>
      </ul></section>
  </div>;
}

function foldTitle(f: LabFold) {
  return f.start + " ～ " + f.end + "：策略 " + pct(f.ret_pct) + "，0050 " + pct(f.bench_pct) + "，差 " + pct(f.excess_pct) + (f.config ? "（參數：" + f.config + "）" : "");
}

function Chart({ bench, universe, cands }: { bench: LabSide; universe?: LabSide; cands: LabCandidate[] }) {
  const W = 900, H = 320, L = 44, R = 8, T = 10, B = 24;
  const series = [{ name: bench.name, color: BENCH_COLOR, pts: bench.curve }, ...(universe ? [{ name: universe.name, color: UNIVERSE_COLOR, pts: universe.curve }] : []), ...cands.map((c, i) => ({ name: c.name, color: COLORS[i % COLORS.length], pts: c.curve }))];
  const values = series.flatMap(s => s.pts.map(p => p[1]));
  const lo = Math.min(...values, 100), hi = Math.max(...values, 100);
  const n = Math.max(...series.map(s => s.pts.length), 2);
  const x = (i: number) => L + (i / (n - 1)) * (W - L - R);
  const y = (v: number) => T + (1 - (Math.log(v) - Math.log(lo)) / ((Math.log(hi) - Math.log(lo)) || 1)) * (H - T - B);
  const years = bench.curve.map((p, i) => ({ year: p[0].slice(0, 4), i })).filter((p, k, a) => k > 0 && p.year !== a[k - 1].year);
  const ticks = [12.5, 25, 50, 100, 200, 400, 800, 1600, 3200].filter(v => v === 100 || (v >= lo && v <= hi));
  return <div>
    <svg viewBox={"0 0 " + W + " " + H} className="w-full" role="img" aria-label="各策略與 0050 的樣本外累積績效">
      {ticks.map(v => <g key={v}><line x1={L} x2={W - R} y1={y(v)} y2={y(v)} stroke="#2a313c" strokeDasharray={v === 100 ? undefined : "3 3"} />
        <text x={L - 6} y={y(v) + 4} textAnchor="end" fontSize="11" fill="#a2aab7">{Math.round(v)}</text></g>)}
      {years.map(p => <text key={p.year} x={x(p.i)} y={H - 6} textAnchor="middle" fontSize="11" fill="#a2aab7">{p.year}</text>)}
      {series.map(s => <path key={s.name} d={"M" + s.pts.map((p, i) => x(i).toFixed(1) + "," + y(p[1]).toFixed(1)).join(" L")} fill="none" stroke={s.color} strokeWidth={s.color === BENCH_COLOR ? 2.5 : 1.5} />)}
    </svg>
    <div className="mt-2 flex flex-wrap gap-x-4 gap-y-1 text-xs text-muted">{series.map(s => <span key={s.name}><span style={{ color: s.color }}>●</span> {s.name}（{pct(s.pts[s.pts.length - 1][1] - 100)}）</span>)}</div>
    <p className="mt-1 text-xs text-muted">縱軸是對數刻度，方便比較長期成長。</p>
  </div>;
}
