"use client";
import { useCallback, useEffect, useState } from "react";
import Link from "next/link";
import { api, DaytradeCommand, DaytradeResponse, errorMessage } from "@/lib/api";
import { EmptyState, ErrorCard, Skeleton } from "@/components/Feedback";
import { readCache, writeCache } from "@/lib/cache";

export const DAYTRADE_ID = "__daytrade";

/** 今日訊號選「當沖」時：右邊是「請電腦上的當沖程式重新選股」，下面是名單。 */
export function useDaytrade(enabled = true) {
  const [data, setData] = useState<DaytradeResponse | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [tick, setTick] = useState(0);
  const refresh = useCallback(() => setTick(t => t + 1), []);
  useEffect(() => {
    const cached = readCache<DaytradeResponse>("daytrade");
    if (cached) setData(cached);
  }, []);
  useEffect(() => {
    if (!enabled) return;
    const controller = new AbortController();
    let timer: ReturnType<typeof setTimeout>;
    api.getDaytrade(controller.signal).then(res => {
      setData(res); setError(null); writeCache("daytrade", res);
      const busy = (res.commands || []).some(c => c.type === "screen" && (c.status === "pending" || c.status === "running"));
      timer = setTimeout(refresh, busy ? 5_000 : 60_000);
    }).catch(err => { if (!controller.signal.aborted) { setError(errorMessage(err)); timer = setTimeout(refresh, 15_000); } });
    return () => { controller.abort(); clearTimeout(timer); };
  }, [tick, refresh, enabled]);
  return { data, error, refresh };
}

const ago = (iso?: string | null) => (iso ? (Date.now() - new Date(iso).getTime()) / 60_000 : null);

export function DaytradeControl({ data, refresh }: { data: DaytradeResponse | null; refresh: () => void }) {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const last: DaytradeCommand | undefined = [...(data?.commands || [])].reverse().find(c => c.type === "screen");
  const active = last && (last.status === "pending" || last.status === "running");
  const agentAge = ago(data?.heartbeat.agent_at);
  const agentAlive = agentAge != null && agentAge < 3;
  async function go() {
    setBusy(true); setError(null);
    try { await api.daytradeCommand("screen"); refresh(); } catch (err) { setError(errorMessage(err)); } finally { setBusy(false); }
  }
  return <div className="space-y-3">
    <button className="btn-primary w-full" disabled={busy || !!active} onClick={go}>{active ? "當沖程式選股中…" : busy ? "送出中…" : "請電腦上的當沖程式重新選股"}</button>
    <p className="text-xs leading-5 text-muted">
      <span className={`mr-1 inline-block h-2 w-2 rounded-full ${agentAlive ? "bg-emerald-400" : "bg-zinc-500"}`} />
      {agentAlive ? "當沖程式在線上，約 1 分鐘內開始。" : "當沖程式目前沒有回應：請確認電腦開著、「當沖程式」在執行（設定每天自動執行後會開機自動啟動）。"}
      每個交易日 15:10 也會自動選隔天名單。用證交所／櫃買收盤資料＋凱基分點，不耗 FinMind 額度。
    </p>
    {last && <p className={`text-xs ${last.status === "failed" ? "text-red-300" : "text-muted"}`}>上次指令：{last.created_at.slice(5, 16).replace("T", " ")}・{({ pending: "排隊中", running: "執行中", done: "完成", failed: "失敗" } as const)[last.status]}{last.message ? `・${last.message}` : ""}</p>}
    {error && <ErrorCard message={error} />}
  </div>;
}

export function DaytradePlanView({ data, error }: { data: DaytradeResponse | null; error: string | null }) {
  if (error && !data) return <ErrorCard message={error} />;
  if (!data) return <Skeleton rows={2} />;
  const plan = data.plan;
  if (!plan) return <EmptyState title="還沒有當沖名單">按右上「請電腦上的當沖程式重新選股」，或等交易日 15:10 自動選股。</EmptyState>;
  return <section className="card space-y-4">
    <div className="flex flex-wrap items-baseline justify-between gap-2">
      <div><p className="eyebrow">DAY TRADE</p><h2 className="mt-1 text-xl font-semibold">⚡ {plan.trade_date} 當沖觀察名單</h2></div>
      <span className="text-xs text-muted">用 {plan.data_date} 收盤資料選出{plan.received_at ? `・${plan.received_at.slice(5, 16).replace("T", " ")} 更新` : ""}・<Link className="text-blue-300 hover:underline" href="/daytrade">盤前體檢與進出場 →</Link></span>
    </div>
    {plan.candidates.length === 0 ? <p className="text-sm text-muted">沒有符合條件的標的（量 ≥ 1 萬張、金額 ≥ 5 億、漲 3.5–6.5%、收盤 ≥ 均價、量 ≥ 5 日均 2 倍、隔日沖券商 &lt; 10%），這天不做當沖。</p> :
      <div className="overflow-x-auto"><table className="w-full min-w-[640px] text-sm">
        <thead className="text-left text-xs text-muted"><tr><th className="py-2">股票</th><th className="text-right">收盤</th><th className="text-right">漲幅</th><th className="text-right">成交張數</th><th className="text-right">量 / 5 日均</th><th className="text-right">金額</th><th className="text-right">隔日沖買超</th></tr></thead>
        <tbody>{plan.candidates.map(c => <tr key={c.stock_id} className="border-t border-line">
          <td className="py-2">{c.stock_id} {c.name}</td><td className="text-right font-mono">{c.close}</td>
          <td className="text-right font-mono text-rise">+{c.change_pct.toFixed(2)}%</td><td className="text-right font-mono">{Math.round(c.volume_lots).toLocaleString()}</td>
          <td className="text-right font-mono">{c.volume_ratio.toFixed(1)} 倍</td><td className="text-right font-mono">{(c.amount / 1e8).toFixed(1)} 億</td>
          <td className="text-right font-mono">{c.daytrader_share == null ? "—" : `${(c.daytrader_share * 100).toFixed(1)}%`}</td></tr>)}</tbody>
      </table></div>}
    {plan.notes?.length > 0 && <ul className="list-disc pl-5 text-xs text-muted">{plan.notes.map(n => <li key={n}>{n}</li>)}</ul>}
    <p className="text-xs text-muted">隔天 08:30 看夜盤／ADR、08:59 看試撮，09:03–09:15 才決定進不進場；停損 0.8%、停利約 1%，當天出場。</p>
  </section>;
}
