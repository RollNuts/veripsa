// App logic — IMPORTS ./client (import coupling) and calls apiGet (cross-file call).
import { apiGet } from "./client";

export function loadAccounts(): Promise<Response> {
    return apiGet("/accounts");
}
