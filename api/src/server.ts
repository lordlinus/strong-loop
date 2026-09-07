import express from "express";
import { randomUUID } from "node:crypto";
import { DefaultAzureCredential } from "@azure/identity";

const server = express();
const credential = new DefaultAzureCredential();
const maxInputLength = 2_000;

server.use(express.json({ limit: "8kb" }));
server.get("/api/health", (_request, response) => response.json({ status: "ok" }));
server.post("/api/run", async (request, response) => {
    const requestId = randomUUID();
    const principal = request.header("x-ms-client-principal");
    if (!principal) {
        response.status(401).json({ error: "Authentication is required.", requestId });
        return;
    }

    const input = typeof request.body?.input === "string" ? request.body.input.trim() : "";
    if (!input || input.length > maxInputLength) {
        response.status(400).json({ error: `Input must contain between 1 and ${maxInputLength} characters.`, requestId });
        return;
    }

    const endpoint = process.env.FOUNDRY_AGENT_ENDPOINT;
    if (!endpoint) {
        response.status(503).json({ error: "The live agent is not configured.", requestId });
        return;
    }

    try {
        const token = await credential.getToken("https://ai.azure.com/.default");
        const upstream = await fetch(endpoint, {
            method: "POST",
            headers: {
                Accept: "text/event-stream",
                Authorization: `Bearer ${token.token}`,
                "Content-Type": "application/json",
                "x-ms-client-request-id": requestId,
            },
            body: JSON.stringify({ model: "strong-loop", input, stream: true }),
        });

        if (!upstream.ok || !upstream.body) {
            response.status(502).json({ error: "The hosted agent could not start the run.", requestId });
            return;
        }

        response.status(200);
        response.set({
            "Cache-Control": "no-cache, no-store",
            "Content-Type": upstream.headers.get("content-type") ?? "text/event-stream",
            "X-Accel-Buffering": "no",
            "X-Request-Id": requestId,
        });
        response.flushHeaders();

        const reader = upstream.body.getReader();
        while (true) {
            const { done, value } = await reader.read();
            if (done) break;
            response.write(Buffer.from(value));
        }
        response.end();
    } catch (error) {
        if (!response.headersSent) {
            response.status(502).json({ error: "The hosted agent request failed.", requestId });
        } else {
            response.end();
        }
        console.error("Foundry invocation failed", requestId, error);
    }
});

const port = Number(process.env.PORT ?? 8080);
server.listen(port, () => console.log(`strong-loop API listening on ${port}`));
