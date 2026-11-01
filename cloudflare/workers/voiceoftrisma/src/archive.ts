/* =========================================================
   VOICE OF TRISMA — MODUL ARCHIVE (dari archive-cache-worker)
   ---------------------------------------------------------
   1. Cron tiap 30 menit: rekap uploads Archive.org user
      (IA_USER) lalu simpan ke D1 (key "UPLOADS_DATA_FULL").
   2. GET /archive = API pencarian arsip: filter (query), sort,
      pagination (hits_per_page, page). Parameter lewat query string.
   ========================================================= */

import { Env, Route, json, d1GetJson, d1SetJson } from "./shared";

const IA_USER = "@16_i_gede_ananda_pradnyana";
const KV_KEY = "UPLOADS_DATA_FULL";

/* Rekap otomatis: scrape semua halaman uploads user → satu nilai KV. */
export async function updateArchiveCache(env: Env) {
	let allHits: any[] = [];
	let page = 1;
	let totalHits: number | null = null;

	try {
		while (true) {
			const resp = await fetch(`https://archive.org/services/search/beta/page_production/?page_type=account_details&page_target=${IA_USER}&page_elements=[%22uploads%22]&hits_per_page=1000&page=${page}`, { signal: AbortSignal.timeout(15000) });
			const json: any = await resp.json();
			const uploads = json?.response?.body?.page_elements?.uploads;
			const hits = uploads?.hits?.hits || [];

			if (totalHits === null) totalHits = uploads?.hits?.total || 0;

			allHits = [...allHits, ...hits.map((h: any) => ({
				id: h.fields.identifier,
				title: h.fields.title || h.fields.identifier,
				publicdate: h.fields.publicdate,
				downloads: h.fields.downloads || 0,
				subject: h.fields.subject || [],
				creator: h.fields.creator || [],
			}))];

			if (hits.length === 0 || allHits.length >= (totalHits || 0)) break;
			page++;
		}
		await d1SetJson(env.DB, KV_KEY, { items: allHits });
	} catch (e) {
		console.error(e);
	}
}

/* GET /archive — layani request API untuk frontend. */
async function handleArchive(request: Request, env: Env): Promise<Response> {
	const url = new URL(request.url);

	const parsed = await d1GetJson(env.DB, KV_KEY);
	if (!parsed) {
		return json({ error: "Cache empty or warming up." }, 404);
	}

	let items: any[] = [];
	try {
		items = (parsed as { items?: any[] }).items || [];
	} catch (e) {
		return json({ error: "Cache corrupt." }, 500);
	}

	// Params
	const query = url.searchParams.get("query")?.toLowerCase() || "";
	const sort = url.searchParams.get("sort") || "publicdate:desc";
	const rawHitsPerPage = url.searchParams.get("hits_per_page") || "24";
	const page = Math.max(1, parseInt(url.searchParams.get("page") || "1"));

	// Filter
	if (query) {
		items = items.filter((item) =>
			(item.title?.toLowerCase().includes(query)) ||
			(item.id?.toLowerCase().includes(query)) ||
			(item.subject?.some((s: string) => s.toLowerCase().includes(query)))
		);
	}

	// Sort (dibatasi field yang sah untuk mencegah property lookup sembarangan)
	const ALLOWED_SORT_FIELDS: Record<string, true> = {
		publicdate: true,
		title: true,
		downloads: true,
		id: true,
	};
	const [rawField, direction] = sort.split(":");
	const field = ALLOWED_SORT_FIELDS[rawField] ? rawField : "publicdate";
	items.sort((a, b) => {
		let valA = a[field] ?? 0;
		let valB = b[field] ?? 0;
		if (typeof valA === "number") return direction === "desc" ? valB - valA : valA - valB;
		return direction === "desc" ? String(valB).localeCompare(String(valA)) : String(valA).localeCompare(String(valB));
	});

	// Pagination
	const totalItems = items.length;
	let resultItems: any[] = [];
	let hitsLimit = totalItems;

	if (rawHitsPerPage !== "all") {
		hitsLimit = parseInt(rawHitsPerPage) || 24;
		resultItems = items.slice((page - 1) * hitsLimit, page * hitsLimit);
	} else {
		resultItems = items;
	}

	return json(
		{
			status: "success",
			total_results: totalItems,
			current_page: page,
			total_pages: Math.ceil(totalItems / hitsLimit),
			data: resultItems,
		},
		200,
		{ "Cache-Control": "public, max-age=180, s-maxage=300, stale-while-revalidate=60" }
	);
}

/* GET /archive/download/:path+ — Reverse proxy download file dari Archive.org */
async function handleArchiveDownload(request: Request, _env: Env): Promise<Response> {
	const url = new URL(request.url);
	let subpath = "";
	if (url.pathname.startsWith("/archive/download/")) {
		subpath = url.pathname.slice("/archive/download/".length);
	} else if (url.pathname.startsWith("/download/")) {
		subpath = url.pathname.slice("/download/".length);
	}

	if (!subpath) {
		return json({ error: "Missing archive path." }, 400);
	}

	const targetUrl = `https://archive.org/download/${subpath}${url.search}`;

	const reqHeaders = new Headers();
	const range = request.headers.get("Range");
	if (range) {
		reqHeaders.set("Range", range);
	}
	reqHeaders.set("User-Agent", request.headers.get("User-Agent") || "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36");
	reqHeaders.set("Accept", "*/*");

	let upstream: Response | null = null;
	const maxRetries = 2;

	for (let attempt = 0; attempt <= maxRetries; attempt++) {
		try {
			upstream = await fetch(targetUrl, {
				method: request.method,
				headers: reqHeaders,
				redirect: "follow",
			});

			if (upstream.status < 500 || attempt === maxRetries) {
				break;
			}
		} catch (err) {
			if (attempt === maxRetries) {
				return json({ error: "Proxy fetch failed", message: String(err) }, 502);
			}
		}
		const { promise, resolve } = Promise.withResolvers<void>();
		setTimeout(resolve, 1000);
		await promise;
	}

	if (!upstream) {
		return json({ error: "Proxy fetch failed" }, 502);
	}

	const resHeaders = new Headers();
	const passHeaders = [
		"content-type",
		"content-length",
		"content-range",
		"accept-ranges",
		"content-disposition",
		"etag",
		"last-modified",
		"cache-control",
	];
	for (const h of passHeaders) {
		const val = upstream.headers.get(h);
		if (val !== null) {
			resHeaders.set(h, val);
		}
	}

	resHeaders.set("Access-Control-Allow-Origin", "*");
	resHeaders.set("Access-Control-Allow-Methods", "GET, HEAD, OPTIONS");

	return new Response(upstream.body, {
		status: upstream.status,
		statusText: upstream.statusText,
		headers: resHeaders,
	});
}

export const archiveRoutes: Route[] = [
	{ method: "GET", pattern: "/archive", handler: handleArchive },
	{ method: "GET", pattern: "/archive/download/:path+", handler: handleArchiveDownload },
	{ method: "HEAD", pattern: "/archive/download/:path+", handler: handleArchiveDownload },
	{ method: "GET", pattern: "/download/:path+", handler: handleArchiveDownload },
	{ method: "HEAD", pattern: "/download/:path+", handler: handleArchiveDownload },
];
