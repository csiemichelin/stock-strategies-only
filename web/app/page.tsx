"use client";
import { useEffect, useState } from "react";
import Link from "next/link";
import { api, errorMessage, Market, RunResult, SavedRun, Strategy } from "@/lib/api";
import { readCache, writeCache } from "@/lib/cache";
import { SourceBadge } from "@/components/ActionBadge";
import { EmptyState, ErrorCard, Skeleton } from "@/components/Feedback";
import RunControl from "@/components/RunControl";
import QuotaStatus from "@/components/QuotaStatus";
import SignalResults from "@/components/SignalResults";
import { useRun } from "@/hooks/useRun";
import { DAYTRADE_ID, DaytradeControl, DaytradePlanView, useDaytrade } from "@/components/DaytradePanel";

export default function Dashboard() {
  const [strategies, setStrategies] = useState<Strategy[]>([]);
  const [picked, setPicked] = useState("");
  const [market, setMarket] = useState<Market | null>(null);
  const [watchCount, setWatchCount] = useState<number | null>(null);
  const [loading, setLoading] = useState(true);
  const [marketLoading, setMarketLoading] = useState(true);
  const [watchLoading, setWatchLoading] = useState(true);
  const [errors, setErrors] = useState<Record<string, string>>({});
  const [reload, setReload] = useState(0);
  const isDaytrade = picked === DAYTRADE_ID;
  const run = useRun(isDaytrade ? "" : picked);
  const daytrade = useDaytrade(isDaytrade);
  const [lastRun, setLastRun] = useState<SavedRun | null>(null);
  useEffect(() => { // 上次選股結果：先顯示這台裝置記住的，再向後端拿（後端存在 Google Sheet）
    const cached = readCache<SavedRun>("lastRun");
    if (cached) setLastRun(cached);
    const controller = new AbortController();
    api.lastRunResult(controller.signal).then(({ run: saved }) => {
      if (saved && (!cached || saved.finished_at >= cached.finished_at)) { setLastRun(saved); writeCache("lastRun", saved); }
    }).catch(() => {});
    return () => controller.abort();
  }, []);
  const finishedResult = run.job?.status === "completed" ? run.result : null;
  useEffect(() => {
    if (!finishedResult) return;
    const saved = { finished_at: finishedResult.reused?.finished_at ?? new Date().toISOString(), result: finishedResult };
    setLastRun(saved); writeCache("lastRun", saved);
  }, [finishedResult]);
  useEffect(() => {
    const controller = new AbortController();
    const signal = controller.signal;
    const fail = (key: string, error: unknown) => { if (!signal.aborted) setErrors(prev => ({ ...prev, [key]: errorMessage(error) })); };
    const applyPicked = (usable: Strategy[]) => {
      let remembered = "";
      try { remembered = sessionStorage.getItem("screening:selected") || ""; } catch {}
      setPicked(prev => [prev, remembered, "default"].find(id => id === DAYTRADE_ID || usable.some(s => s.id === id)) || usable[0]?.id || "");
    };
    // 先顯示上次的資料，避免後端冷啟動時整頁空白
    const cachedStrategies = readCache<Strategy[]>("strategies");
    const cachedMarket = readCache<Market>("market");
    const cachedWatch = readCache<number>("watchCount");
    if (cachedStrategies?.length) { setStrategies(cachedStrategies); applyPicked(cachedStrategies); }
    if (cachedMarket) setMarket(cachedMarket);
    if (cachedWatch != null) setWatchCount(cachedWatch);
    setErrors({}); setLoading(!cachedStrategies?.length); setMarketLoading(!cachedMarket); setWatchLoading(cachedWatch == null);
    api.listStrategies(signal).then(data => {
      const usable = data.strategies.filter(s => !s.error);
      setStrategies(usable); writeCache("strategies", usable);
      applyPicked(usable);
      if (usable.length !== data.strategies.length) fail("strategies", new Error("部分策略檔無法讀取，請到策略庫查看"));
    }).catch(error => fail("strategies", error)).finally(() => { if (!signal.aborted) setLoading(false); });
    api.getMarket(signal).then(m => { setMarket(m); if (m.close != null) writeCache("market", m); }).catch(error => fail("market", error)).finally(() => { if (!signal.aborted) setMarketLoading(false); });
    api.getWatchlist(signal).then(data => { setWatchCount(data.items.length); if (data.error) fail("watchlist", new Error(data.error)); else writeCache("watchCount", data.items.length); }).catch(error => fail("watchlist", error)).finally(() => { if (!signal.aborted) setWatchLoading(false); });
    return () => controller.abort();
  }, [reload]);
  const selected = strategies.find(s => s.id === picked);
  const displayedMarket = run.result?.market || market;
  const marketKnown = displayedMarket?.close != null;
  return <div className="space-y-8">
    <div className="flex flex-wrap items-end justify-between gap-4"><div><p className="eyebrow">YOUR MARKET, IN FOCUS</p><h1 className="mt-2 text-3xl font-semibold tracking-tight">今日訊號</h1><p className="mt-2 text-sm text-muted">從市場脈動到個股判斷，讓每一次研究都有依據。</p></div><Link href="/strategies" className="btn-ghost">管理策略 <span aria-hidden="true">↗</span></Link></div>
    <div className="grid gap-4 md:grid-cols-3">
      <div className="card relative overflow-hidden"><p className="label">加權指數 · 市場狀態</p><div className={`mt-3 text-2xl font-semibold ${marketKnown ? displayedMarket?.bullish ? "text-rise" : "text-fall" : "text-muted"}`}>{marketLoading && !displayedMarket ? "載入中…" : !marketKnown ? "資料待確認" : displayedMarket?.bullish ? "▲ 站上均線" : "▼ 跌破均線"}</div><p className="mt-3 text-xs leading-6 text-muted">{errors.market || displayedMarket?.note || "市場資料尚未取得"}</p>{marketKnown && <div className="mt-3 border-t border-line pt-3 font-mono text-xs text-muted">加權 {displayedMarket?.close?.toLocaleString("zh-TW")} <span className="mx-2">/</span> 均線 {displayedMarket?.ma20?.toLocaleString("zh-TW", { maximumFractionDigits: 0 })}</div>}</div>
      <div className="card"><p className="label">我的觀察池</p><div className="mt-3 font-mono text-3xl">{watchLoading ? "—" : errors.watchlist ? "—" : watchCount ?? "—"}<span className="ml-2 text-sm text-muted">檔股票</span></div><p className="mt-3 text-xs leading-6 text-muted">{errors.watchlist ? "尚未連接觀察池，請確認 Google Sheet 設定。" : "同步 Google Sheet 中啟用的股票，每次執行重新讀取。"}</p></div>
      <div className="card"><p className="label">策略工作區</p><div className="mt-3 font-mono text-3xl">{loading ? "—" : strategies.length}<span className="ml-2 text-sm text-muted">個可用策略</span></div><p className="mt-3 text-xs leading-6 text-muted">用既有策略開始，或建立適合自己的篩選條件。</p><Link href="/strategies/ai" className="mt-3 inline-block text-xs text-blue-300 hover:underline">用 AI 設計策略 →</Link></div>
    </div>
    {!!Object.keys(errors).length && <ErrorCard message={Object.values(errors).join("；")} retry={() => setReload(value => value + 1)} />}
    <section className="card">
      <div className="mb-5 flex items-center gap-3"><span className="flex h-7 w-7 items-center justify-center rounded-lg bg-accent/10 font-mono text-xs text-accent">01</span><h2 className="font-medium">選擇策略，開始今日研究</h2></div>
      {loading ? <Skeleton rows={1} /> : !strategies.length ? <EmptyState title="先建立你的第一個策略"><Link href="/strategies/new" className="btn-primary mt-3">建立策略</Link></EmptyState> : <div className="grid gap-6 lg:grid-cols-[1fr_1fr]">
        <div><label htmlFor="strategy" className="label">本次使用策略</label><select id="strategy" className="input" value={picked} disabled={run.running} onChange={e => { setPicked(e.target.value); try { sessionStorage.setItem("screening:selected", e.target.value); } catch {} }}>{strategies.map(s => <option key={s.id} value={s.id}>{s.name}</option>)}<option value={DAYTRADE_ID}>⚡ 當沖：動能延續與極短線套利</option></select>
          {isDaytrade && <p className="mt-3 text-xs leading-6 text-muted">盤後挑爆量強勢股，隔天開盤 15 分鐘內進場、當天出場。行情與下單由你電腦上的當沖程式（凱基 API）執行。</p>}
          {selected && <div className="mt-3 space-y-2"><SourceBadge source={selected.source} /><p className="text-xs leading-6 text-muted">{selected.description}</p><Link className="text-xs text-blue-300 hover:underline" href={`/strategies/${selected.id}`}>查看與調整參數 →</Link></div>}
        </div><div className="space-y-3 lg:border-l lg:border-line lg:pl-6">{isDaytrade ? <DaytradeControl data={daytrade.data} refresh={daytrade.refresh} /> : <><RunControl run={run} disabled={!picked} /><QuotaStatus need="每天第一次選股約需 300–500 次" /></>}</div>
      </div>}
    </section>
    {isDaytrade ? <DaytradePlanView data={daytrade.data} error={daytrade.error} />
      : run.result ? <>{run.result.reused && <p className="rounded-xl border border-accent/30 bg-accent/5 p-4 text-sm leading-6">
        ♻️ 這個時段（資料日 {run.result.reused.data_day}）已經用同一套策略選過，資料不會變，直接顯示 {new Date(run.result.reused.finished_at).toLocaleString("zh-TW", { month: "numeric", day: "numeric", hour: "2-digit", minute: "2-digit" })} 的結果，沒有重跑、也不重複推 LINE。平日 14:30 收盤資料出來後再按就會重新選股。</p>}
      <QuotaNote result={run.result} /><SignalResults run={run.result} /></>
      : run.running ? null
      : lastRun ? <div className="space-y-3">
          <p className="text-sm text-muted">上次選股結果・{new Date(lastRun.finished_at).toLocaleString("zh-TW", { month: "numeric", day: "numeric", hour: "2-digit", minute: "2-digit" })}・策略 {lastRun.result.strategy?.name}（再按「開始選股」會更新）</p>
          <QuotaNote result={lastRun.result} /><SignalResults run={lastRun.result} /></div>
      : <EmptyState title="準備好，讓數據說話">選擇一個策略並開始選股。完成後可比較評分、篩選訊號，並展開查看個股風險與交易參考。</EmptyState>}
  </div>;
}

function QuotaNote({ result }: { result: RunResult }) {
  const hit = result.results.filter(r => r.action === "ERROR" && r.risk_notes?.some(n => /額度|限流/.test(n))).length;
  if (!hit) return null;
  return <div className="rounded-xl border border-watch/30 bg-watch/5 p-4 text-sm leading-6">
    ⚠️ 有 {hit} 檔因為 FinMind 免費額度（每小時 600 次）用完而沒有評分，顯示為 ERROR。約一小時後再按一次「開始選股」就會補上；同一小時內不要同時跑回測比較，兩者共用額度。
  </div>;
}
