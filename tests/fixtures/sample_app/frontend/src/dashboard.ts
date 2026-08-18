// Dashboard — IMPORTS ./app (so it is downstream of both app.ts and, transitively, client.ts).
import { loadAccounts } from "./app";

export function renderDashboard(): Promise<Response> {
    return loadAccounts();
}
