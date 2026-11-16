"use client";
import { useEffect, useState } from "react";
import { api, FinMindUsage } from "@/lib/api";

/** FinMind 免費額度（每小時 600 次）目前用量；選股和回測共用。查不到就不顯示。 */
export default function QuotaStatus({ need }: { need?: string }) {
  const [usage, setUsage] = useState<FinMindUsage | null>(null);
  useEffect(() => {
    const controller = new AbortController();
    api.finmindUsage(controller.signal).then(r => setUsage(r.usage)).catch(() => {});
    return () => controller.abort();
  }, []);
  if (!usage) return null;
  const low = usage.remaining < 150;
  return <p className={`text-xs leading-6 ${low ? "text-watch" : "text-muted"}`}>
    FinMind 額度：這個小時已用 {usage.used} / {usage.limit}，剩 {usage.remaining} 次{need ? `（${need}）` : ""}。
    {low && " 剩得不多，建議約一小時後再跑，避免資料抓不完。"}
  </p>;
}
