// 雲端部署用：全站帳密保護 + /api/* 由伺服器端代理到後端並附上 API key
// 未設定 BASIC_AUTH_USER / API_BASE 時不做任何事（本機開發不受影響）
import { NextResponse, type NextRequest } from "next/server";

function unauthorized() {
  return new NextResponse("需要登入", {
    status: 401,
    headers: { "WWW-Authenticate": 'Basic realm="stock-strategies", charset="UTF-8"' },
  });
}

function safeEqual(a: string, b: string) {
  if (a.length !== b.length) return false;
  let diff = 0;
  for (let i = 0; i < a.length; i++) diff |= a.charCodeAt(i) ^ b.charCodeAt(i);
  return diff === 0;
}

export function middleware(req: NextRequest) {
  const user = process.env.BASIC_AUTH_USER;
  const pass = process.env.BASIC_AUTH_PASSWORD;
  if (user && pass) {
    const header = req.headers.get("authorization") || "";
    const [scheme, encoded] = header.split(" ");
    let ok = false;
    if (scheme === "Basic" && encoded) {
      const decoded = new TextDecoder().decode(Uint8Array.from(atob(encoded), (c) => c.charCodeAt(0)));
      const i = decoded.indexOf(":");
      ok = i >= 0 && safeEqual(decoded.slice(0, i), user) && safeEqual(decoded.slice(i + 1), pass);
    }
    if (!ok) return unauthorized();
  }

  const apiBase = (process.env.API_BASE || "").replace(/\/$/, "");
  if (apiBase && req.nextUrl.pathname.startsWith("/api/")) {
    const target = new URL(apiBase + req.nextUrl.pathname + req.nextUrl.search);
    const headers = new Headers(req.headers);
    headers.delete("authorization");
    headers.delete("host");
    if (process.env.API_KEY) headers.set("x-api-key", process.env.API_KEY);
    return NextResponse.rewrite(target, { request: { headers } });
  }
  return NextResponse.next();
}

export const config = {
  matcher: ["/((?!_next/static|_next/image|favicon.ico).*)"],
};
