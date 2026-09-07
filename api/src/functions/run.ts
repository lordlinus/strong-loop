import { randomUUID } from "node:crypto";
import { DefaultAzureCredential } from "@azure/identity";
import { app, HttpRequest, HttpResponseInit, InvocationContext } from "@azure/functions";

const credential = new DefaultAzureCredential();
const tokenScope = "https://ai.azure.com/.default";
const maxInputLength = 2_000;

interface RunRequest {
    input?: unknown;
}

function problem(status: number, message: string, requestId: string): HttpResponseInit {
    return {
        status,
        jsonBody: { error: message, requestId },
        headers: { "Cache-Control": "no-store" },
    };
}

export async function run(request: HttpRequest, context: InvocationContext): Promise<HttpResponseInit> {
    const requestId = randomUUID();
    const principal = request.headers.get("x-ms-client-principal");
    if (!principal) {
        return problem(401, "Authentication is required.", requestId);
    }

    let payload: RunRequest;
    try {
        payload = await request.json() as RunRequest;
    } catch {
        return problem(400, "Request body must be valid JSON.", requestId);
    }

    const input = typeof payload.input === "string" ? payload.input.trim() : "";
    if (!input || input.length > maxInputLength) {
        return problem(400, `Input must contain between 1 and ${maxInputLength} characters.`, requestId);
    }

    const endpoint = process.env.FOUNDRY_AGENT_ENDPOINT;
    if (!endpoint) {
        context.error("FOUNDRY_AGENT_ENDPOINT is not configured", { requestId });
        return problem(503, "The live agent is not configured.", requestId);
    }

    try {
        const accessToken = await credential.getToken(tokenScope);
        const upstream = await fetch(endpoint, {
            method: "POST",
            headers: {
                Accept: "text/event-stream",
                Authorization: `Bearer ${accessToken.token}`,
                "Content-Type": "application/json",
                "x-ms-client-request-id": requestId,
            },
            body: JSON.stringify({
                model: "strong-loop",
                input,
                stream: true,
            }),
        });

        if (!upstream.ok || !upstream.body) {
            context.error("Foundry invocation failed", {
                requestId,
                status: upstream.status,
            });
            return problem(502, "The hosted agent could not start the run.", requestId);
        }

        return {
            status: 200,
            body: upstream.body,
            headers: {
                "Cache-Control": "no-cache, no-store",
                "Content-Type": upstream.headers.get("content-type") ?? "text/event-stream",
                "X-Accel-Buffering": "no",
                "X-Request-Id": requestId,
            },
        };
    } catch (error) {
        context.error("Foundry invocation raised an exception", {
            requestId,
            error: error instanceof Error ? error.message : String(error),
        });
        return problem(502, "The hosted agent request failed.", requestId);
    }
}

app.http("run", {
    methods: ["POST"],
    authLevel: "anonymous",
    route: "run",
    handler: run,
});

app.http("health", {
    methods: ["GET"],
    authLevel: "anonymous",
    route: "health",
    handler: async () => ({
        status: 200,
        jsonBody: { status: "ok" },
        headers: { "Cache-Control": "no-store" },
    }),
});
