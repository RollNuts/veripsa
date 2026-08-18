// HTTP client — the frontend IMPORT hub (app.ts imports this; dashboard.ts imports app.ts).
export function apiGet(path: string): Promise<Response> {
    return fetch(path);
}

export function apiPost(path: string, body: unknown): Promise<Response> {
    return fetch(path, { method: "POST", body: JSON.stringify(body) });
}
