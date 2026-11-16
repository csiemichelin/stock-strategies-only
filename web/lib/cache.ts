// 開頁先顯示上次的資料（存在這台瀏覽器），背景再向後端拿最新的。
// Render 免費方案冷啟動要幾十秒，這樣畫面不會一直空白轉圈。
const PREFIX = "ssw-cache:";
const MAX_AGE = 7 * 24 * 3_600_000;

export function readCache<T>(key: string): T | null {
  try {
    const raw = localStorage.getItem(PREFIX + key);
    if (!raw) return null;
    const { t, v } = JSON.parse(raw) as { t: number; v: T };
    return Date.now() - t < MAX_AGE ? v : null;
  } catch {
    return null;
  }
}

export function writeCache<T>(key: string, value: T): void {
  try {
    localStorage.setItem(PREFIX + key, JSON.stringify({ t: Date.now(), v: value }));
  } catch {
    // 空間不足或隱私模式：略過
  }
}
