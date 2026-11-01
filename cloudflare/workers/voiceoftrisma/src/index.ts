/* =========================================================
   VOICE OF TRISMA — WORKER GABUNGAN (router + cron)
   ---------------------------------------------------------
   Satu entry point untuk semua modul. Route prefix:
     /api/*     admin        (voiceoftrisma-admin-worker)
     /stats     stream-stats (voiceoftrisma-stream-stats)
     /archive   archive      (archive-cache-worker)
     /metrics   metrics      (voiceoftrisma-metrics-worker)
     /workflow  workflow     (workflow-trigger)

   *   Cron (named triggers) di-routing lewat event.cron:
   *     stream-stats setiap 5 menit · archive setiap 30 menit ·
   *     workflow 07:00 UTC · metrics setiap menit
   ========================================================= */

import { Env, Route, CORS_HEADERS, json, withCors } from "./shared";
import { adminRoutes } from "./admin";
import { statsRoutes, recordSample } from "./stream-stats";
import { archiveRoutes, updateArchiveCache } from "./archive";
import { metricsRoutes, updateMetrics } from "./metrics";
import { triggerWorkflows } from "./workflow";
import { RateLimitDO } from "./rate-limit";
// Re-export WAJIB: tanpa ini class DO tidak ikut ter-bundle oleh wrangler.
export { RateLimitDO };

function handleRoot(_request: Request, _env: Env): Response {
	return json({
		ok: true,
		service: "voiceoftrisma",
		endpoints: ["/api/*", "/stats", "/archive", "/archive/download/*", "/metrics"],
		time: new Date().toISOString(),
	});
}

const ROUTES: Route[] = [
	{ method: "GET", pattern: "/", handler: handleRoot },
	...adminRoutes,
	...statsRoutes,
	...archiveRoutes,
	...metricsRoutes,
];

const compiledRoutes = ROUTES.map((r) => ({ ...r, pattern: new URLPattern({ pathname: r.pattern }) }));

/* ============ Anti-DDoS layer-7 (in-memory, per-isolate) ============
   workers.dev tidak punya zona Cloudflare sendiri, jadi tidak bisa pakai
   WAF / rate-limit rules akun. Mitigasi di-worker:
     - per-IP : batas request per 10 detik (default 120)
     - global : circuit breaker per isolate (default 600 per 10 detik)
   Tanpa biaya D1 (murni memori); cache-buster (?t=...) tak bisa mem-bypass
   karena kunci per-IP. Nilai di-override via env.
*/
export const RL_WINDOW_MS = 10_000;
const rlHits = new Map<string, number[]>();

/** true bila `key` melewati `max` request dalam jendela 10 detik. Dijadikan export
    supaya bisa di-unit-test tanpa request HTTP. */
export function rateLimited(key: string, max: number, now: number): boolean {
	let arr = rlHits.get(key);
	if (!arr) {
		rlHits.set(key, [now]);
		return false;
	}
	while (arr.length > 0 && now - arr[0] > RL_WINDOW_MS) arr.shift();
	if (arr.length >= max) return true;
	arr.push(now);
	if (rlHits.size > 5000) {
		// hygiene: buang kunci yang sudah diam > 1 menit
		for (const [k, v] of rlHits) {
			if (now - v[v.length - 1] > 60_000) rlHits.delete(k);
		}
	}
	return false;
}

export default {
	async fetch(request: Request, env: Env, ctx: ExecutionContext): Promise<Response> {
		const url = new URL(request.url);

		// Normalisasi: buang trailing slash supaya "/stats/" == "/stats"
		// (dashboard & archive.js memakai trailing slash).
		const normalized = new URL(url.href);
		if (normalized.pathname.length > 1 && normalized.pathname.endsWith("/")) {
			normalized.pathname = normalized.pathname.slice(0, -1);
		}

		// Preflight CORS
		if (request.method === "OPTIONS") {
			return new Response(null, { status: 204, headers: CORS_HEADERS });
		}

		// Anti-DDoS layer-7 — DUA TIER:
		//  1) fast-path in-memory (per-isolate): serap lonjakan lokal dengan murah.
		//  2) durable-object "Global": counter TERPUSAT lintas-isolate (per-IP +
		//     global). Gagal memanggil DO = fail-open (jangan korbankan user sah).
		const ip = request.headers.get("CF-Connecting-IP") || request.headers.get("True-Client-IP") || "unknown";
		const ipMax = Number(env.MAX_REQ_IP_10S) || 120;
		const globalMax = Number(env.MAX_REQ_GLOBAL_10S) || 600;
		const now = Date.now();
		if (rateLimited(`ip:${ip}`, ipMax, now) || rateLimited("global", globalMax, now)) {
			return withCors(
				json({ error: "Terlalu banyak permintaan. Coba lagi sebentar lagi." }, 429, { "Retry-After": "10" })
			);
		}
		if (env.RATE_LIMITER) {
			try {
				const doId = env.RATE_LIMITER.idFromName("Global");
				const decision = await env.RATE_LIMITER
					.get(doId)
					.fetch(
						`https://rate-limit/?k=${encodeURIComponent(`ip:${ip}`)}&m=${ipMax}&g=${globalMax}`
					);
				const body = (await decision.json()) as { ok: boolean };
				if (!body.ok) {
					return withCors(
						json({ error: "Terlalu banyak permintaan. Coba lagi sebentar lagi." }, 429, { "Retry-After": "10" })
					);
				}
			} catch (e) {
				// fail-open: DO error jangan sampai memutus layanan (log saja)
				console.error("RATE_LIMITER DO gagal (fail-open):", e);
			}
		}

		for (const route of compiledRoutes) {
			if (route.method !== request.method) continue;
			if (!route.pattern.exec(normalized)) continue;

			try {
				return withCors(await route.handler(request, env, ctx));
			} catch (err) {
				console.error("Unhandled error:", err);
				return withCors(json({ error: "Internal server error" }, 500));
			}
		}

		return withCors(json({ error: "Not found" }, 404));
	},

	async scheduled(event: ScheduledEvent, env: Env): Promise<void> {
		// event.cron berisi string cron (named trigger tidak didukung endpoint
		// schedules akun ini — pakai string cron biasa sebagai kunci routing).
		switch (event.cron) {
			case "*/5 * * * *":
				await recordSample(env);
				break;
			case "*/30 * * * *":
				await updateArchiveCache(env);
				break;
			case "0 7 * * *":
				await triggerWorkflows(env);
				break;
			case "* * * * *":
				await updateMetrics(env);
				break;
			default:
				console.error("Cron tidak dikenal:", event.cron);
		}
	},
};
