// Usage telemetry: who signed in and what they did, sent to Application Insights as custom
// events. Imported before anything else so the SDK's auto-collection can patch express.
// Off when APPLICATIONINSIGHTS_CONNECTION_STRING is unset (local runs, tests).
import type { Request } from "express";
import { DefaultAzureCredential } from "@azure/identity";
import * as appInsights from "applicationinsights";

process.env.OTEL_SERVICE_NAME ??= `strong-loop-${process.env.APP_MODE ?? "api"}`;

let client: appInsights.TelemetryClient | null = null;
if (process.env.APPLICATIONINSIGHTS_CONNECTION_STRING) {
    try {
        appInsights.setup().setAutoCollectConsole(true, true);
        // The Application Insights resource has local (key) auth disabled: ingestion must be
        // Entra-authenticated, as the app's managed identity (AZURE_CLIENT_ID), which holds
        // Monitoring Metrics Publisher on it. Without this every item is dropped silently.
        appInsights.defaultClient.config.aadTokenCredential = new DefaultAzureCredential();
        appInsights.start();
        client = appInsights.defaultClient;
    } catch (error) {
        console.error("Application Insights did not start; usage events are off", error);
    }
}

export interface Principal {
    user: string | null;       // the sign-in name Static Web Apps reports (email / UPN)
    provider: string | null;   // aad | github
    userId: string | null;
}

/** Who is signed in, from Static Web Apps' auth header. Never trusted from the body. */
export function principalOf(header: string | undefined): Principal {
    const none = { user: null, provider: null, userId: null };
    if (!header) return none;
    try {
        const p = JSON.parse(Buffer.from(header, "base64").toString("utf8"));
        const text = (v: unknown) => (typeof v === "string" && v.trim() ? v.trim() : null);
        return { user: text(p?.userDetails), provider: text(p?.identityProvider), userId: text(p?.userId) };
    } catch {
        return none;
    }
}

/** One usage event. Properties are identifiers and counts only — never file contents. */
export function track(name: string, request: Request, properties: Record<string, unknown> = {}): void {
    if (!client) return;
    const who = principalOf(request.header("x-ms-client-principal"));
    const flat: Record<string, string> = {};
    for (const [key, value] of Object.entries({ ...who, ...properties })) {
        if (value !== null && value !== undefined) flat[key] = String(value);
    }
    try {
        client.trackEvent({ name, properties: flat });
    } catch (error) {
        console.error("usage event dropped", name, error);
    }
}
