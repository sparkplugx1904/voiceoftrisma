import { env, createExecutionContext, waitOnExecutionContext, SELF, applyD1Migrations } from "cloudflare:test";
import { beforeAll, describe, it, expect } from "vitest";
import { adminRoutes } from "../src/admin";
import { verifyToken, signToken, Env } from "../src/shared";
import { rateLimited, RL_WINDOW_MS } from "../src/index";
import { triggerPipelineGuard } from "../src/workflow";

const IncomingRequest = Request<unknown, IncomingRequestCfProperties>;

/* D1 tiruan untuk unit test handler (get → null, semua op no-op). */
function fakeD1() {
	return {
		prepare: () => ({
			bind: () => ({
				first: async () => null,
				all: async () => ({ results: [] }),
				run: async () => ({}),
			}),
		}),
	} as unknown as D1Database;
}

/* D1 in-memory yang benar-benar menyimpan nilai per key — dipakai untuk
   memverifikasi isi log aktivitas (admin_logs) di unit test. */
function memD1(store: Map<string, string>) {
	return {
		prepare: () => ({
			bind: (...args: string[]) => ({
				first: async () => {
					const key = String(args[0] ?? "");
					return store.has(key) ? { value: store.get(key) } : null;
				},
				all: async () => ({ results: [] }),
				run: async () => {
					// d1SetJson: INSERT ... VALUES (?, ?, updated_at) — arg[0]=key, arg[1]=value
					store.set(String(args[0]), String(args[1]));
					return {};
				},
			}),
		}),
	} as unknown as D1Database;
}

function fakeEnv() {
	return {
		DB: fakeD1(),
		VOT_ADMIN_STORE: null,
		VOT_STREAM_STATS: null,
		ARCHIVE_KV: null,
		VOT_METRICS_STORE: null,
		ADMIN_USERNAME: "test-admin",
		ADMIN_PASSWORD: "test-pass",
		SESSION_SECRET: "test-secret-for-unit",
		GITHUB_TOKEN: "test-token",
	};
}

/* Env tanpa secrets — harus ditolak oleh guard (jangan bandingkan "undefined"). */
function fakeEnvNoSecrets() {
	return { ...fakeEnv(), ADMIN_USERNAME: "", ADMIN_PASSWORD: "", SESSION_SECRET: "" };
}

/* Skema D1 untuk database test in-memory (cermin migrations/0001_init.sql). */
const MIGRATIONS = [
	{
		name: "0001_init",
		queries: [
			"CREATE TABLE IF NOT EXISTS kv_store (key TEXT PRIMARY KEY, value TEXT NOT NULL, updated_at INTEGER NOT NULL) WITHOUT ROWID",
			"CREATE TABLE IF NOT EXISTS samples (t INTEGER PRIMARY KEY, listeners INTEGER NOT NULL, status INTEGER NOT NULL) WITHOUT ROWID",
		],
	},
];

beforeAll(async () => {
	// Terapkan skema D1 (kv_store + samples) ke database test in-memory.
	await applyD1Migrations(env.DB, MIGRATIONS);
});

describe("samples D1 (time-series)", () => {
	it("history hanya memuat sample dalam 6 jam terakhir, terurut naik", async () => {
		const now = Math.floor(Date.now() / 1000);
		// seed: 1 sample lama (>6 jam) + 2 sample baru
		await env.DB.prepare("INSERT INTO samples (t, listeners, status) VALUES (?, ?, ?)")
			.bind(now - 7 * 3600, 9, 1)
			.run();
		await env.DB.prepare("INSERT INTO samples (t, listeners, status) VALUES (?, ?, ?)")
			.bind(now - 600, 2, 1)
			.run();
		await env.DB.prepare("INSERT INTO samples (t, listeners, status) VALUES (?, ?, ?)")
			.bind(now - 300, 5, 1)
			.run();

		const res = await SELF.fetch("https://example.com/stats?history=1");
		expect(res.status).toBe(200);
		const body = await res.json();
		expect(Array.isArray(body.history)).toBe(true);
		// sample tua ter-trim oleh query WHERE t >= cutoff
		expect(body.history).toHaveLength(2);
		expect(body.history[0][1]).toBe(2);
		expect(body.history[1][1]).toBe(5);
	});

	it("upsert per bucket: nilai terakhir menang (dedupe)", async () => {
		const t = Math.floor(Date.now() / 1000 / 300) * 300; // awal bucket saat ini
		await env.DB.prepare("INSERT INTO samples (t, listeners, status) VALUES (?, ?, ?) ON CONFLICT(t) DO UPDATE SET listeners = excluded.listeners, status = excluded.status")
			.bind(t, 3, 1)
			.run();
		await env.DB.prepare("INSERT INTO samples (t, listeners, status) VALUES (?, ?, ?) ON CONFLICT(t) DO UPDATE SET listeners = excluded.listeners, status = excluded.status")
			.bind(t, 7, 1)
			.run();

		const { results } = await env.DB.prepare("SELECT t, listeners, status FROM samples WHERE t = ?")
			.bind(t)
			.all<{ t: number; listeners: number; status: number }>();
		expect(results).toHaveLength(1);
		expect(results[0].listeners).toBe(7);
	});
});

describe("router worker gabungan", () => {
	it("GET / -> info root", async () => {
		const res = await SELF.fetch("https://example.com/");
		expect(res.status).toBe(200);
		const body = await res.json();
		expect(body.service).toBe("voiceoftrisma");
	});

	it("GET /api/health -> 200 + CORS", async () => {
		const res = await SELF.fetch("https://example.com/api/health");
		expect(res.status).toBe(200);
		expect(res.headers.get("Access-Control-Allow-Origin")).toBe("*");
	});

	it("POST /api/login dengan kredensial test -> token (unit test handler)", async () => {
		const loginRoute = adminRoutes.find((r) => r.method === "POST" && r.pattern === "/api/login");
		expect(loginRoute).toBeDefined();
		const request = new IncomingRequest("https://example.com/api/login", {
			method: "POST",
			headers: { "Content-Type": "application/json" },
			body: JSON.stringify({ username: "test-admin", password: "test-pass" }),
		});
		const res = await loginRoute!.handler(request, fakeEnv() as any, createExecutionContext());
		expect(res.status).toBe(200);
		const body = await res.json();
		expect(typeof body.token).toBe("string");
		// Token harus bisa diverifikasi ulang (stateless, HMAC).
		const verified = await verifyToken(fakeEnv() as any, body.token);
		expect(verified?.u).toBe("test-admin");
	});

	it("POST /api/login salah -> 401", async () => {
		const res = await SELF.fetch("https://example.com/api/login", {
			method: "POST",
			headers: { "Content-Type": "application/json" },
			body: JSON.stringify({ username: "test-admin", password: "salah" }),
		});
		expect(res.status).toBe(401);
	});

	it("login gagal tercatat 'login_failed' + IP (salah password)", async () => {
		const store = new Map<string, string>();
		const loginRoute = adminRoutes.find((r) => r.method === "POST" && r.pattern === "/api/login");
		expect(loginRoute).toBeDefined();
		const request = new IncomingRequest("https://example.com/api/login", {
			method: "POST",
			headers: { "Content-Type": "application/json", "CF-Connecting-IP": "198.51.100.4" },
			body: JSON.stringify({ username: "test-admin", password: "salah" }),
		});
		const res = await loginRoute!.handler(request, { ...fakeEnv(), DB: memD1(store) } as any, createExecutionContext());
		expect(res.status).toBe(401);
		const logs = JSON.parse(store.get("admin_logs")!);
		expect(logs).toHaveLength(1);
		expect(logs[0].action).toBe("login_failed");
		expect(logs[0].detail.user).toBe("test-admin");
		expect(logs[0].ip).toBe("198.51.100.4");
		expect(typeof logs[0].t).toBe("string");
	});

	it("login sukses tercatat 'login' dengan info akses (IP & User-Agent)", async () => {
		const store = new Map<string, string>();
		const loginRoute = adminRoutes.find((r) => r.method === "POST" && r.pattern === "/api/login");
		expect(loginRoute).toBeDefined();
		const request = new IncomingRequest("https://example.com/api/login", {
			method: "POST",
			headers: {
				"Content-Type": "application/json",
				"CF-Connecting-IP": "203.0.113.9",
				"User-Agent": "VoT-Test/2.0 Mobile",
			},
			body: JSON.stringify({ username: "test-admin", password: "test-pass" }),
		});
		const res = await loginRoute!.handler(request, { ...fakeEnv(), DB: memD1(store) } as any, createExecutionContext());
		expect(res.status).toBe(200);
		const logs = JSON.parse(store.get("admin_logs")!);
		expect(logs[0].action).toBe("login");
		expect(logs[0].detail.user).toBe("test-admin");
		expect(logs[0].ip).toBe("203.0.113.9");
		expect(logs[0].ua).toBe("VoT-Test/2.0 Mobile");
		expect(typeof logs[0].org).toBe("string"); // meta wajib ada walau kosong saat dev
	});

	it("rate limit login: percobaan ke-6 dari IP sama -> 429 + login_locked tercatat sekali", async () => {
		const store = new Map<string, string>();
		const loginRoute = adminRoutes.find((r) => r.method === "POST" && r.pattern === "/api/login");
		expect(loginRoute).toBeDefined();
		const env = { ...fakeEnv(), DB: memD1(store) } as any;
		const headers = { "Content-Type": "application/json", "CF-Connecting-IP": "203.0.113.77" };
		const attempt = (password: string) =>
			loginRoute!.handler(
				new IncomingRequest("https://example.com/api/login", {
					method: "POST",
					headers,
					body: JSON.stringify({ username: "test-admin", password }),
				}),
				env,
				createExecutionContext()
			);

		for (let i = 1; i <= 5; i++) {
			const res = await attempt("salah");
			expect(res.status).toBe(401); // 5 pertama: gagal biasa
		}
		const locked = await attempt("salah");
		expect(locked.status).toBe(429); // ke-6: diblokir
		expect(locked.headers.get("Retry-After")).toBeTruthy();

		const logs = JSON.parse(store.get("admin_logs")!);
		const lockedLogs = logs.filter((l: { action: string }) => l.action === "login_locked");
		const failedLogs = logs.filter((l: { action: string }) => l.action === "login_failed");
		expect(failedLogs).toHaveLength(5); // hanya 5 percobaan tercatat
		expect(lockedLogs).toHaveLength(1); // lock tercatat tepat sekali, bukan tiap spam

		// Percobaan berikutnya (masih diblokir) TIDAK menambah log baru.
		const blockedAgain = await attempt("salah");
		expect(blockedAgain.status).toBe(429);
		expect(JSON.parse(store.get("admin_logs")!)).toHaveLength(6);
	});

	it("POST /api/login saat secrets belum terpasang -> 503 (guard)", async () => {
		const loginRoute = adminRoutes.find((r) => r.method === "POST" && r.pattern === "/api/login");
		expect(loginRoute).toBeDefined();
		const request = new IncomingRequest("https://example.com/api/login", {
			method: "POST",
			headers: { "Content-Type": "application/json" },
			body: JSON.stringify({ username: "undefined", password: "undefined" }),
		});
		const res = await loginRoute!.handler(request, fakeEnvNoSecrets() as any, createExecutionContext());
		expect(res.status).toBe(503);
	});

	it("verifyToken tanpa SESSION_SECRET -> null (guard)", async () => {
		const token = await signToken(fakeEnv() as any, "test-admin");
		const verified = await verifyToken(fakeEnvNoSecrets() as any, token);
		expect(verified).toBeNull();
	});

	it("GET /api/jadwal -> seed default saat D1 kosong", async () => {
		const res = await SELF.fetch("https://example.com/api/jadwal");
		expect(res.status).toBe(200);
		const body = await res.json();
		expect(typeof body.jadwal).toBe("object");
	});

	it("GET /stats?history=1 -> struktur history (D1 kosong)", async () => {
		const res = await SELF.fetch("https://example.com/stats?history=1");
		expect(res.status).toBe(200);
		const body = await res.json();
		expect(Array.isArray(body.history)).toBe(true);
	});

	it("GET /metrics -> data belum tersedia saat D1 kosong", async () => {
		const res = await SELF.fetch("https://example.com/metrics");
		expect(res.status).toBe(200);
		const body = await res.json();
		expect(body.message).toBe("Data belum tersedia");
	});

	it("GET /workflow -> 404 (tidak ada rute HTTP untuk workflow - cron only)", async () => {
		const res = await SELF.fetch("https://example.com/workflow");
		expect(res.status).toBe(404);
	});

	it("route tak dikenal -> 404", async () => {
		const res = await SELF.fetch("https://example.com/tidak-ada");
		expect(res.status).toBe(404);
	});

	it("OPTIONS -> 204 + CORS headers", async () => {
		const res = await SELF.fetch("https://example.com/api/login", { method: "OPTIONS" });
		expect(res.status).toBe(204);
		expect(res.headers.get("Access-Control-Allow-Methods")).toContain("POST");
	});

	it("security headers (nosniff, frame-options) terpasang di respons", async () => {
		const res = await SELF.fetch("https://example.com/api/health");
		expect(res.headers.get("X-Content-Type-Options")).toBe("nosniff");
		expect(res.headers.get("X-Frame-Options")).toBe("DENY");
	});

	it("logout mencabut token yang diterbitkan sebelumnya (anti session replay)", async () => {
		const store = new Map<string, string>();
		const testEnv = { ...fakeEnv(), DB: memD1(store) } as unknown as Env;
		const logoutRoute = adminRoutes.find((r) => r.method === "POST" && r.pattern === "/api/logout");
		expect(logoutRoute).toBeDefined();

		const token = await signToken(testEnv, "test-admin");
		const validBefore = await verifyToken(testEnv, token);
		expect(validBefore?.u).toBe("test-admin");

		const delay1 = Promise.withResolvers<void>();
		setTimeout(delay1.resolve, 10);
		await delay1.promise;

		// Request logout menggunakan token
		const logoutReq = new IncomingRequest("https://example.com/api/logout", {
			method: "POST",
			headers: { Authorization: `Bearer ${token}` },
		});
		const logoutRes = await logoutRoute!.handler(logoutReq, testEnv, createExecutionContext());
		expect(logoutRes.status).toBe(200);

		// Token lama harus ditolak setelah logout
		const validAfter = await verifyToken(testEnv, token);
		expect(validAfter).toBeNull();

		const delay2 = Promise.withResolvers<void>();
		setTimeout(delay2.resolve, 10);
		await delay2.promise;

		// Token baru yang dibuat setelah logout harus tetap valid
		const newToken = await signToken(testEnv, "test-admin");
		const validNew = await verifyToken(testEnv, newToken);
		expect(validNew?.u).toBe("test-admin");
	});

	/* ---------------- Anti-DDoS layer-7 (unit test limiter) ---------------- */

	it("rateLimited: request ke-6 dalam jendela ditolak, lalu reset setelah 10 detik", () => {
		const start = 1_700_000_000_000;
		const key = "t:unit";
		for (let i = 0; i < 5; i++) {
			expect(rateLimited(key, 5, start + i)).toBe(false); // 5 pertama lolos
		}
		expect(rateLimited(key, 5, start + 5)).toBe(true); // ke-6 ditolak
		expect(rateLimited(key, 5, start + 5)).toBe(true); // masih ditolak (belum lewat jendela)
		// Setelah > RL_WINDOW_MS, jendela bergeser → request baru lolos lagi.
		expect(rateLimited(key, 5, start + RL_WINDOW_MS + 1)).toBe(false);
	});

	it("rateLimited: key berbeda punya bucket terpisah (bukan global)", () => {
		expect(rateLimited("t:ipA", 1, 1_700_000_000_000)).toBe(false);
		expect(rateLimited("t:ipB", 1, 1_700_000_000_001)).toBe(false); // ipB tidak terpengaruh ipA
	});

	it("RateLimitDO (durable object): request ke-4 dalam jendela ditolak (max=3)", async () => {
		const ns = (env as any).RATE_LIMITER;
		if (!ns) {
			// binding tak tersedia di runtime test — lewati (di versi prod ada).
			return;
		}
		const id = ns.idFromName("testRateLimitUnit");
		const stub = ns.get(id);
		const q = (m: number) => `https://rl/?k=ip:do-test&m=${m}&g=1000000`;
		for (let i = 0; i < 3; i++) {
			const r = await stub.fetch(q(3));
			const body = (await r.json()) as { ok: boolean };
			expect(body.ok).toBe(true); // 3 pertama lolos
		}
		const r4 = await stub.fetch(q(3));
		const body4 = (await r4.json()) as { ok: boolean };
		expect(body4.ok).toBe(false); // ke-4 ditolak (per-IP max=3)
		// IP lain tidak terblok (bucket terpisah)
		const other = await ns.get(ns.idFromName("testRateLimitUnit2")).fetch(q(3));
		expect(((await other.json()) as { ok: boolean }).ok).toBe(true);
	});

	it("triggerPipelineGuard: mengembalikan error jika GITHUB_TOKEN tidak ada", async () => {
		const res = await triggerPipelineGuard({ ...fakeEnv(), GITHUB_TOKEN: "" } as unknown as Env);
		expect(res.ok).toBe(false);
		expect(res.error).toBe("GITHUB_TOKEN missing");
	});
});
