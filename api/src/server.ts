import express from "express";
import { randomUUID } from "node:crypto";
import { DefaultAzureCredential } from "@azure/identity";

const server = express();
const credential = new DefaultAzureCredential();
const maxInputLength = 2_000;
const scope = "https://ai.azure.com/.default";

// The session file API is the input channel: a client creates a session, PUTs its role,
// data and answers into `intake/`, then invokes the agent pinned to that session. The
// agent reads them from `$HOME` (see src/strong-loop/loop/inputs.py). Only these four
// paths may be written; everything else in the session filesystem is the agent's.
const allowedPaths = new Set(["intake/request.json", "intake/charter.yaml", "intake/data.csv", "intake/accept.json"]);
const maxUploadBytes = 50 * 1024 * 1024;
const sessionIdPattern = /^[A-Za-z0-9_-]{8,128}$/;

/** The project's session endpoint for this agent, derived from the Responses endpoint
 *  (`.../agents/<name>/endpoint/protocols/openai/responses?api-version=v1`). */
function sessionsUrl(): URL | null {
    const endpoint = process.env.FOUNDRY_AGENT_ENDPOINT;
    if (!endpoint) return null;
    const url = new URL(endpoint);
    const marker = "/endpoint/protocols/openai/responses";
    if (!url.pathname.endsWith(marker)) return null;
    url.pathname = url.pathname.slice(0, -marker.length) + "/endpoint/sessions";
    return url;
}

/** The signed-in user's display name, from Static Web Apps' auth header; used as the
 *  ratifier when a client accepts a pairing. Never trusted from the request body. */
function principalName(header: string | undefined): string | null {
    if (!header) return null;
    try {
        const principal = JSON.parse(Buffer.from(header, "base64").toString("utf8"));
        const name = typeof principal?.userDetails === "string" ? principal.userDetails.trim() : "";
        return name || null;
    } catch {
        return null;
    }
}

function requireAuth(request: express.Request, response: express.Response, requestId: string): string | null {
    const principal = request.header("x-ms-client-principal");
    if (!principal) {
        console.warn("Request rejected: missing client principal", requestId, request.path);
        response.status(401).json({ error: "Authentication is required.", requestId });
        return null;
    }
    return principal;
}

server.use(express.json({ limit: "8kb" }));
server.get("/api/health", (_request, response) => response.json({ status: "ok" }));

server.post("/api/sessions", async (request, response) => {
    const requestId = randomUUID();
    if (!requireAuth(request, response, requestId)) return;
    const url = sessionsUrl();
    if (!url) {
        response.status(503).json({ error: "The live agent is not configured.", requestId });
        return;
    }
    try {
        const token = await credential.getToken(scope);
        const upstream = await fetch(url, {
            method: "POST",
            headers: { Authorization: `Bearer ${token.token}`, "Content-Type": "application/json", "x-ms-client-request-id": requestId },
            body: "{}",   // no version indicator: pin to the agent's current version
        });
        const body = (await upstream.json().catch(() => ({}))) as { agent_session_id?: string; expires_at?: number };
        if (!upstream.ok || !body.agent_session_id) {
            console.error("Session create failed", requestId, upstream.status, body);
            response.status(502).json({ error: "The hosted agent could not open a session.", requestId });
            return;
        }
        console.log("Session created", requestId, body.agent_session_id);
        response.status(201).json({ agent_session_id: body.agent_session_id, expires_at: body.expires_at ?? null, requestId });
    } catch (error) {
        console.error("Session create failed", requestId, error);
        response.status(502).json({ error: "The hosted agent could not open a session.", requestId });
    }
});

server.put("/api/sessions/:id/files", express.raw({ type: () => true, limit: maxUploadBytes }), async (request, response) => {
    const requestId = randomUUID();
    const principal = requireAuth(request, response, requestId);
    if (!principal) return;
    const sessionId = String(request.params.id ?? "");
    const path = typeof request.query.path === "string" ? request.query.path : "";
    if (!sessionIdPattern.test(sessionId)) {
        response.status(400).json({ error: "Invalid session id.", requestId });
        return;
    }
    if (!allowedPaths.has(path)) {
        response.status(400).json({ error: `path must be one of ${[...allowedPaths].join(", ")}`, requestId });
        return;
    }
    const base = sessionsUrl();
    if (!base) {
        response.status(503).json({ error: "The live agent is not configured.", requestId });
        return;
    }
    let body: Buffer = Buffer.isBuffer(request.body) ? request.body : Buffer.alloc(0);
    if (path === "intake/accept.json") {
        // The ratifier is whoever is signed in — the body may not choose someone else.
        let accept: Record<string, unknown>;
        try {
            accept = JSON.parse(body.toString("utf8") || "{}");
            if (accept === null || typeof accept !== "object" || Array.isArray(accept)) throw new Error("not an object");
        } catch {
            response.status(400).json({ error: "accept.json must be a JSON object.", requestId });
            return;
        }
        const name = principalName(principal);
        body = Buffer.from(JSON.stringify({ ...accept, ratified_by: name ?? undefined }));
    }
    const url = new URL(base);
    url.pathname += `/${encodeURIComponent(sessionId)}/files/content`;
    url.searchParams.set("path", path);
    try {
        const token = await credential.getToken(scope);
        const upstream = await fetch(url, {
            method: "PUT",
            headers: { Authorization: `Bearer ${token.token}`, "Content-Type": "application/octet-stream", "x-ms-client-request-id": requestId },
            body: new Uint8Array(body),
        });
        const result = await upstream.json().catch(() => ({}));
        if (!upstream.ok) {
            console.error("Session file upload failed", requestId, upstream.status, result);
            response.status(502).json({ error: "The hosted agent did not accept the file.", requestId });
            return;
        }
        console.log("Session file uploaded", requestId, { sessionId, path, bytes: body.length });
        response.status(201).json({ path, bytes_written: body.length, requestId });
    } catch (error) {
        console.error("Session file upload failed", requestId, error);
        response.status(502).json({ error: "The hosted agent did not accept the file.", requestId });
    }
});

server.post("/api/run", async (request, response) => {
    const requestId = randomUUID();
    const startedAt = Date.now();
    const principal = request.header("x-ms-client-principal");
    console.log("Run request received", requestId, { authenticated: Boolean(principal) });
    if (!principal) {
        console.warn("Run request rejected: missing client principal", requestId);
        response.status(401).json({ error: "Authentication is required.", requestId });
        return;
    }

    const input = typeof request.body?.input === "string" ? request.body.input.trim() : "";
    if (!input || input.length > maxInputLength) {
        response.status(400).json({ error: `Input must contain between 1 and ${maxInputLength} characters.`, requestId });
        return;
    }
    const sessionId = typeof request.body?.agent_session_id === "string" ? request.body.agent_session_id : "";
    if (sessionId && !sessionIdPattern.test(sessionId)) {
        response.status(400).json({ error: "Invalid session id.", requestId });
        return;
    }

    const endpoint = process.env.FOUNDRY_AGENT_ENDPOINT;
    if (!endpoint) {
        response.status(503).json({ error: "The live agent is not configured.", requestId });
        return;
    }

    response.status(200);
    response.set({
        "Cache-Control": "no-cache, no-store",
        "Content-Type": "text/event-stream",
        "X-Accel-Buffering": "no",
        "X-Request-Id": requestId,
    });
    response.flushHeaders();
    response.write(": proxy-connected\n\n");

    const heartbeat = setInterval(() => response.write(": proxy-waiting\n\n"), 10_000);
    response.on("close", () => clearInterval(heartbeat));
    try {
        console.log("Acquiring Foundry token", requestId);
        const token = await credential.getToken(scope);
        console.log("Calling Foundry agent", requestId, { elapsedMs: Date.now() - startedAt });
        const controller = new AbortController();
        const timeout = setTimeout(() => controller.abort(), 60_000);
        const upstream = await fetch(endpoint, {
            signal: controller.signal,
            method: "POST",
            headers: {
                Accept: "text/event-stream",
                Authorization: `Bearer ${token.token}`,
                "Content-Type": "application/json",
                "x-ms-client-request-id": requestId,
            },
            body: JSON.stringify({ model: "strong-loop", input, stream: true, ...(sessionId ? { agent_session_id: sessionId } : {}) }),
        });
        clearTimeout(timeout);
        clearInterval(heartbeat);
        console.log("Foundry response received", requestId, {
            elapsedMs: Date.now() - startedAt,
            status: upstream.status,
        });

        if (!upstream.ok || !upstream.body) {
            response.write(`event: error\ndata: ${JSON.stringify({ error: "The hosted agent could not start the run.", requestId })}\n\n`);
            response.end();
            return;
        }

        const reader = upstream.body.getReader();
        while (true) {
            const { done, value } = await reader.read();
            if (done) break;
            response.write(Buffer.from(value));
        }
        response.end();
        console.log("Run request completed", requestId, { elapsedMs: Date.now() - startedAt });
    } catch (error) {
        clearInterval(heartbeat);
        response.write(`event: error\ndata: ${JSON.stringify({ error: "The hosted agent request failed.", requestId })}\n\n`);
        response.end();
        console.error("Foundry invocation failed", requestId, error);
    }
});

const port = Number(process.env.PORT ?? 8080);
server.listen(port, () => console.log(`strong-loop API listening on ${port}`));
