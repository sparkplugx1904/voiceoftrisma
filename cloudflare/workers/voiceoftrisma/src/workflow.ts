/* =========================================================
   VOICE OF TRISMA — MODUL WORKFLOW TRIGGER (cron only)
   ---------------------------------------------------------
   - Cron 07:00 UTC: trigger GHA record+transcript via GitHub API.
   - Cron 0 * * * *: trigger GHA pipeline-guard via GitHub API:
       * 00:00, 06:00, 12:00, 18:00 WITA (UTC 16, 22, 04, 10):
         mode transcribe_only (limit 10 runner paralel)
       * Jam lainnya (tiap 1 jam sekali):
         mode describe_only (limit 5 runner paralel)
   MODUL INI TIDAK MEMILIKI ENDPOINT HTTP APAPUN.
   Semua eksekusi hanya dapat dipicu oleh internal scheduled cron.
   ========================================================= */

import { Env } from "./shared";

const GITHUB_OWNER = "sparkplugx1904";
const GITHUB_REPO = "voiceoftrisma";
const RECORD_WORKFLOWS = ["main+transcript_v1.0.yml", "main+transcript_v2.0.yml"];
export async function triggerWorkflow(
	workflow: string,
	token: string,
	inputs?: Record<string, string>
): Promise<{ workflow: string; status: number; ok: boolean; error?: string }> {
	const url = `https://api.github.com/repos/${GITHUB_OWNER}/${GITHUB_REPO}/actions/workflows/${encodeURIComponent(workflow)}/dispatches`;

	const body: { ref: string; inputs?: Record<string, string> } = { ref: "main" };
	if (inputs) {
		body.inputs = inputs;
	}

	const res = await fetch(url, {
		method: "POST",
		headers: {
			Authorization: `Bearer ${token}`,
			Accept: "application/vnd.github+json",
			"X-GitHub-Api-Version": "2022-11-28",
			"Content-Type": "application/json",
			"User-Agent": "voiceoftrisma-worker",
		},
		body: JSON.stringify(body),
		signal: AbortSignal.timeout(15000), // jangan biarkan cron menggantung
	});
	let error: string | undefined;
	if (!res.ok) {
		try {
			error = await res.text();
		} catch {
			error = res.statusText;
		}
	}

	return { workflow, status: res.status, ok: res.ok, error };
}

/* Cron 07:00 UTC: trigger semua workflow rekaman. */
export async function triggerWorkflows(
	env: Env
): Promise<{ workflow: string; status: number; ok: boolean; error?: string }[]> {
	const token: string | undefined = env.GITHUB_TOKEN;
	if (!token) {
		console.error("GITHUB_TOKEN secret missing");
		return [];
	}

	const results = await Promise.all(RECORD_WORKFLOWS.map((wf) => triggerWorkflow(wf, token)));

	for (const r of results) {
		if (r.ok) {
			console.log(`[ OK ] ${r.workflow} triggered (${r.status})`);
		} else {
			console.error(`[FAIL] ${r.workflow} → ${r.status}: ${r.error || "Unknown error"}`);
		}
	}
	return results;
}

/* Cron 0 * * * *: trigger pipeline guard.
   - Jam 00:00, 06:00, 12:00, 18:00 WITA (UTC 16, 22, 04, 10):
     transcribe_only (limit 10 runner paralel)
   - Auto describe_only tiap 1 jam dinonaktifkan sementara.
*/
export async function triggerPipelineGuard(
	env: Env
): Promise<{ workflow: string; status: number; ok: boolean; error?: string }> {
	const token: string | undefined = env.GITHUB_TOKEN;
	if (!token) {
		console.error("GITHUB_TOKEN secret missing");
		return { workflow: "pipeline-guard.yml", status: 500, ok: false, error: "GITHUB_TOKEN missing" };
	}

	const utcHour = new Date().getUTCHours();
	// WITA = UTC+8. WITA 00, 06, 12, 18 -> UTC 16, 22, 04, 10
	const isTranscribeHour = [4, 10, 16, 22].includes(utcHour);

	// Auto describe-only tiap 1 jam dinonaktifkan sementara (hanya jalankan transcribe_only pada jam 00, 06, 12, 18 WITA)
	if (!isTranscribeHour) {
		console.log("[ SKIP ] Auto describe_only tiap jam dinonaktifkan.");
		return { workflow: "pipeline-guard.yml", status: 200, ok: true };
	}

	const mode = "transcribe_only";
	const limit = "10";
	const result = await triggerWorkflow("pipeline-guard.yml", token, { mode, limit, max_scan: "1000" });
	if (result.ok) {
		console.log(`[ OK ] pipeline-guard.yml triggered (mode=${mode}, limit=${limit})`);
	} else {
		console.error(`[FAIL] pipeline-guard.yml → ${result.status}: ${result.error || "Unknown error"}`);
	}
	return result;
}
