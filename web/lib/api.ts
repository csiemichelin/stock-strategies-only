const BASE = (process.env.NEXT_PUBLIC_API_BASE || "").replace(/\/$/, "");

export type StrategyParams = Record<string, number | boolean>;
export type Strategy = {
  id: string;
  name: string;
  description?: string;
  source?: "default" | "manual" | "ai";
  created_at?: string;
  updated_at?: string;
  params: StrategyParams;
  error?: string;
};
export type Market = { bullish: boolean; close?: number | null; ma20?: number | null; note: string };
export type Action = "BUY" | "WATCH" | "SKIP" | "ERROR";
export type StockResult = {
  stock_id: string;
  name?: string;
  action: Action;
  date?: string;
  signal_score?: number;
  entry_price?: number;
  stop_loss_price?: number;
  target_price?: number;
  risk_reward_ratio?: number;
  position_size_pct?: number;
  entry_rule?: string;
  risk_notes?: string[];
  components?: {
    fundamental_pass?: boolean;
    eps_min?: number | null;
    roe_min?: number | null;
    tech_score?: number;
    tech_signals?: string[];
    backtest_winrate?: number | null;
    backtest_samples?: number;
    volume_patterns?: string[];
    volume_verdict?: string;
  };
  trend?: { chg_5d?: number; chg_20d?: number; vol_ratio?: number; pct_from_high?: number; above_ma20?: boolean; above_ma60?: boolean };
};
export type RunResult = {
  strategy: { id: string; name: string };
  market: Market;
  downgraded: number;
  summary: { total: number; buy: number; watch: number; skip: number; error: number };
  results: StockResult[];
  reused?: { finished_at: string; data_day: string };
};
export type RunJob = {
  id: string;
  status: "queued" | "running" | "cancelling" | "completed" | "cancelled" | "failed";
  strategy: { id: string; name: string };
  completed: number;
  total: number | null;
  current: string | null;
  created_at: string;
  result: RunResult | null;
  error: string | null;
};
export type FinMindUsage = { used: number; limit: number; remaining: number };
export type SavedRun = { finished_at: string; result: RunResult };
export type SimPosition = { stock_id: string; name: string; shares: number; entry_price: number; entry_date: string; cost: number; stop_price: number; target_price: number; days_held: number; last_price: number };
export type SimOrder = { stock_id: string; name: string; signal_date: string; signal_score?: number; position_pct: number; ref_price?: number; stop_price?: number; target_price?: number };
export type SimTrade = { date: string; side: "BUY" | "SELL"; stock_id: string; name: string; shares: number; price: number; amount: number; fee: number; pnl?: number | null; pnl_pct?: number | null; reason: string };
export type SimPoint = { date: string; cash: number; holdings: number; total: number };
export type SimAccount = {
  slot?: string; status: "running" | "stopped"; mode: "forward" | "replay"; strategy: { id: string; name: string };
  params: { hold_days: number }; initial_capital: number; cash: number; start_date: string; last_date: string | null;
  created_at: string; stopped_at: string | null; positions: SimPosition[]; pending: SimOrder[]; trades: SimTrade[]; equity: SimPoint[];
  summary: { total: number; pnl: number; pnl_pct: number; cash: number; holdings: number; closed_trades: number; win_rate: number | null; max_drawdown_pct: number };
};
export type SimJob = { status: "idle" | "running" | "completed" | "failed" | "cancelled"; phase: string | null; completed: number; total: number | null; current: string | null; error: string | null };
export type SimResponse = { account: SimAccount | null; accounts?: SimAccount[]; job: SimJob };
export type CurvePoint = { date: string; total: number };
export type BacktestSide = {
  final: number; pnl: number; return_pct: number; max_drawdown_pct: number; curve: CurvePoint[];
  trades: number; win_rate: number | null; fees: number;
  avg_win_pct?: number | null; avg_loss_pct?: number | null; profit_factor?: number | null; avg_exposure_pct?: number;
};
export type BacktestRun = { strategy: { id: string; name: string }; result: BacktestSide; trades: SimTrade[]; skipped?: { stock_id: string; name: string; reason: string }[] };
export type BacktestResult = {
  strategy: { id: string; name: string } | null; capital: number; start_date: string; end_date: string; trading_days: number;
  result: BacktestSide | null; benchmark: Partial<BacktestSide> & { stock_id: string; name: string; available: boolean; shares?: number; error?: string };
  trades: SimTrade[]; finished_at?: string; skipped?: { stock_id: string; name: string; reason: string }[];
  runs?: BacktestRun[]; request?: BacktestJob["params"];
};
export type BacktestJob = {
  status: "idle" | "running" | "completed" | "failed" | "cancelled"; phase: string | null; completed: number; total: number | null;
  current: string | null; error: string | null; started_at: string | null;
  params: { strategy_id: string; strategy_name: string; start_date: string; capital: number; strategy_ids?: string[]; include_daytrade?: boolean; daytrade_command?: string | null } | null;
};
export type BacktestResponse = { job: BacktestJob; result: BacktestResult | null };
export type DaytradeCandidate = { stock_id: string; name: string; close: number; change_pct: number; volume_lots: number; volume_ratio: number; amount: number; vwap: number; daytrader_share: number | null; score?: number; score_parts?: Record<string, number> };
export type DaytradeReviewItem = { stock_id: string; name: string; score: number; recommended: boolean; outcome: "win" | "loss" | "flat" | "no_data"; entry?: number; exit?: number; exit_time?: string; ret_pct?: number; gap_pct?: number | null; max_up_pct?: number; max_down_pct?: number };
export type DaytradeReview = { trade_date: string; data_date: string; items: DaytradeReviewItem[]; take_profit_pct: number; stop_loss_pct: number; stats: { count: number; wins: number; losses: number; hit_rate: number | null; recommended: number; recommended_hit_rate: number | null; avg_ret_pct: number | null } };
export type DaytradePlan = { trade_date: string; data_date: string; candidates: DaytradeCandidate[]; stats: Record<string, number>; params: Record<string, number>; notes: string[]; received_at?: string };
export type DaytradeEvent = { type: string; ts: string; symbol?: string | null; name?: string | null; price?: number | null; shares?: number | null; pnl?: number | null; ok?: boolean | null; mode?: string | null; reason: string; extra?: Record<string, unknown> };
export type DaytradeDay = { date: string; trades: number; pnl: number; wins: number; mode?: string | null };
export type DaytradeBacktest = {
  summary: { days: number; trades: number; win_rate: number | null; pnl: number; profit_factor: number | null; breakeven_win_rate: number; by_mode: Record<string, number>; capital?: number; final?: number; return_pct?: number; max_drawdown_pct?: number };
  note?: string; received_at?: string; curve?: CurvePoint[]; start_date?: string; end_date?: string; request?: { id: string; start_date?: string };
  trades?: { date: string; stock_id: string; mode: string; entry: number; exit?: number; shares: number; pnl?: number; entry_reason?: string; exit_reason?: string }[];
};
export type DaytradeCommand = { id: string; type: "screen" | "backtest"; params: Record<string, unknown>; status: "pending" | "running" | "done" | "failed"; created_at: string; started_at?: string; finished_at?: string; message: string };
export type DaytradeResponse = { plan: DaytradePlan | null; today: { date: string; events: DaytradeEvent[] }; history: DaytradeDay[]; backtest: DaytradeBacktest | null; heartbeat: { at: string | null; mode: string | null; note: string | null; agent_at?: string | null }; commands?: DaytradeCommand[]; reviews?: DaytradeReview[] };
export class ApiError extends Error {
  constructor(message: string, public status: number) { super(message); }
}
export function errorMessage(error: unknown): string {
  return error instanceof Error ? error.message : "發生未預期的錯誤，請稍後再試";
}

async function jfetch<T>(path: string, init?: RequestInit, timeout = 30_000): Promise<T> {
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), timeout);
  const abort = () => controller.abort();
  init?.signal?.addEventListener("abort", abort, { once: true });
  if (init?.signal?.aborted) controller.abort();
  try {
    const headers = new Headers(init?.headers);
    if (init?.body) headers.set("Content-Type", "application/json");
    const res = await fetch(`${BASE}${path}`, { ...init, headers, signal: controller.signal, cache: "no-store" });
    if (!res.ok) {
      const body = await res.json().catch(() => null);
      const detail: unknown = body?.detail;
      const message = typeof detail === "string" ? detail
        : Array.isArray(detail) ? detail.map((item: { loc?: string[]; msg?: string }) => `${item.loc?.slice(1).join(".") || "欄位"}：${item.msg || "格式錯誤"}`).join("；")
        : `請求失敗（${res.status}），請稍後重試`;
      throw new ApiError(message, res.status);
    }
    return await res.json() as T;
  } catch (error) {
    if (init?.signal?.aborted) throw error;
    if (controller.signal.aborted) throw new Error("連線逾時，請稍後重試；背景選股任務會繼續執行");
    if (error instanceof TypeError) throw new Error("無法連線至服務，請確認後端已啟動後重試");
    throw error;
  } finally {
    clearTimeout(timer);
    init?.signal?.removeEventListener("abort", abort);
  }
}

export const api = {
  listStrategies: (signal?: AbortSignal) => jfetch<{ strategies: Strategy[] }>("/api/strategies", { signal }),
  getDefaults: (signal?: AbortSignal) => jfetch<{ params: StrategyParams }>("/api/strategies/defaults", { signal }),
  getStrategy: (id: string, signal?: AbortSignal) => jfetch<Strategy>(`/api/strategies/${encodeURIComponent(id)}`, { signal }),
  saveStrategy: (s: Partial<Strategy>) => jfetch<Strategy>("/api/strategies", { method: "POST", body: JSON.stringify(s) }),
  deleteStrategy: (id: string) => jfetch<{ ok: boolean }>(`/api/strategies/${encodeURIComponent(id)}`, { method: "DELETE" }),
  generateAI: (prompt: string, name?: string) => jfetch<Strategy>("/api/strategies/generate", { method: "POST", body: JSON.stringify({ prompt, name }) }, 120_000),
  getMarket: (signal?: AbortSignal) => jfetch<Market>("/api/market", { signal }),
  getWatchlist: (signal?: AbortSignal) => jfetch<{ items: { stock_id: string; name?: string }[]; error?: string }>("/api/watchlist", { signal }),
  startRun: (strategy_id: string) => jfetch<RunJob>("/api/runs", { method: "POST", body: JSON.stringify({ strategy_id }) }),
  getRun: (id: string, signal?: AbortSignal) => jfetch<RunJob>(`/api/runs/${encodeURIComponent(id)}`, { signal }),
  getSim: (signal?: AbortSignal) => jfetch<SimResponse>("/api/sim", { signal }),
  startSim: (capital: number, strategy_ids: string[], start_date?: string) => jfetch<SimResponse>("/api/sim/start", { method: "POST", body: JSON.stringify({ capital, strategy_ids, start_date: start_date || null }) }),
  stopSim: (slot?: string) => jfetch<SimResponse>("/api/sim/stop", slot == null ? { method: "POST" } : { method: "POST", body: JSON.stringify({ slot }) }),
  removeSim: (slot: string) => jfetch<SimResponse>("/api/sim/remove", { method: "POST", body: JSON.stringify({ slot }) }),
  getBacktest: (signal?: AbortSignal) => jfetch<BacktestResponse>("/api/backtest", { signal }),
  startBacktest: (strategy_ids: string[], include_daytrade: boolean, start_date: string, capital: number) => jfetch<BacktestResponse>("/api/backtest", { method: "POST", body: JSON.stringify({ strategy_ids, include_daytrade, start_date, capital }) }),
  cancelBacktest: () => jfetch<BacktestResponse>("/api/backtest/cancel", { method: "POST" }),
  latestRun: (signal?: AbortSignal) => jfetch<{ job: RunJob | null }>("/api/runs/latest", { signal }),
  finmindUsage: (signal?: AbortSignal) => jfetch<{ usage: FinMindUsage | null }>("/api/finmind/usage", { signal }),
  lastRunResult: (signal?: AbortSignal) => jfetch<{ run: SavedRun | null }>("/api/runs/last-result", { signal }),
  getDaytrade: (signal?: AbortSignal) => jfetch<DaytradeResponse>("/api/daytrade", { signal }),
  daytradeCommand: (type: "screen" | "backtest", params: Record<string, unknown> = {}) => jfetch<DaytradeCommand>("/api/daytrade/commands", { method: "POST", body: JSON.stringify({ type, params }) }),
  cancelRun: (id: string) => jfetch<RunJob>(`/api/runs/${encodeURIComponent(id)}`, { method: "DELETE" }),
};
