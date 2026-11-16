"use client";
import { useCallback, useEffect, useRef, useState } from "react";
import { api, ApiError, errorMessage, RunJob } from "@/lib/api";

const active = (job: RunJob | null) => !!job && ["queued", "running", "cancelling"].includes(job.status);

export function useRun(strategyId: string) {
  const [job, setJob] = useState<RunJob | null>(null);
  const [jobId, setJobId] = useState<string | null>(null);
  const [starting, setStarting] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [retry, setRetry] = useState(0);
  const pending = useRef(false);
  const generation = useRef(0);
  const storageKey = `screening:${strategyId}`;

  useEffect(() => {
    generation.current++;
    setJob(null);
    setError(null);
    setStarting(false);
    pending.current = false;
    try { setJobId(sessionStorage.getItem(storageKey)); } catch { setJobId(null); }
    return () => { generation.current++; };
  }, [storageKey]);

  // 沒有追蹤中的任務時，定期查詢後端是否有人（其他裝置）正在選股，有就接上進度條
  const jobActive = active(job);
  const awaitingJob = !!jobId && !job;
  useEffect(() => {
    if (jobActive || awaitingJob) return;
    const controller = new AbortController();
    let timer: ReturnType<typeof setTimeout>;
    async function discover() {
      try {
        const { job: latest } = await api.latestRun(controller.signal);
        if (controller.signal.aborted) return;
        if (latest && active(latest) && latest.id !== jobId) { setJob(latest); setJobId(latest.id); return; }
      } catch { /* 後端未支援或暫時無法連線：略過 */ }
      if (!controller.signal.aborted) timer = setTimeout(discover, 10_000);
    }
    discover();
    return () => { controller.abort(); clearTimeout(timer); };
  }, [jobId, jobActive, awaitingJob]);

  useEffect(() => {
    if (!jobId) return;
    const controller = new AbortController();
    let timer: ReturnType<typeof setTimeout>;
    async function poll() {
      try {
        const next = await api.getRun(jobId!, controller.signal);
        if (controller.signal.aborted) return;
        setJob(next);
        setError(next.error);
        if (active(next)) timer = setTimeout(poll, 1000);
      } catch (err) {
        if (controller.signal.aborted) return;
        setError(errorMessage(err));
        if (err instanceof ApiError && err.status === 404) {
          try { sessionStorage.removeItem(storageKey); } catch {}
          setJobId(null);
          setJob(null);
        } else timer = setTimeout(poll, 4000);
      }
    }
    poll();
    return () => { controller.abort(); clearTimeout(timer); };
  }, [jobId, retry, storageKey]);

  const start = useCallback(async () => {
    if (!strategyId || pending.current) return;
    const version = generation.current;
    pending.current = true;
    setStarting(true);
    setError(null);
    try {
      const next = await api.startRun(strategyId);
      try {
        sessionStorage.setItem(storageKey, next.id);
        sessionStorage.setItem("screening:selected", strategyId);
      } catch {}
      if (version !== generation.current) return;
      setJob(next);
      setJobId(next.id);
    } catch (err) {
      if (version !== generation.current) return;
      if (err instanceof ApiError && err.status === 409) {
        // 已有人在選股：直接顯示那個任務的進度，不重複觸發
        try {
          const { job: latest } = await api.latestRun();
          if (latest && active(latest)) { setJob(latest); setJobId(latest.id); return; }
        } catch {}
      }
      setError(errorMessage(err));
    } finally {
      if (version === generation.current) { setStarting(false); pending.current = false; }
    }
  }, [strategyId, storageKey]);

  async function cancel() {
    if (!jobId) return;
    const version = generation.current;
    try {
      const next = await api.cancelRun(jobId);
      if (version === generation.current) { setJob(next); setRetry(value => value + 1); }
    } catch (err) { if (version === generation.current) setError(errorMessage(err)); }
  }

  return { job, result: job?.result ?? null, running: starting || active(job) || (!!jobId && !job), error, start, cancel };
}
