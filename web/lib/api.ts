import "server-only";

/**
 * The only place that talks to the API. `server-only` makes importing this from browser code a
 * build error, so the key can't reach a client bundle (docs/10 section 9.1, D8).
 */

export class ApiError extends Error {
  constructor(
    readonly status: number,
    message: string,
  ) {
    super(message);
  }
}

function settings(): { url: string; key: string } {
  const url = process.env.HOOPSEDGE_API_URL;
  const key = process.env.HOOPSEDGE_API_KEY;
  if (!url || !key) throw new ApiError(500, "The dashboard isn't connected to the API yet");
  return { url: url.replace(/\/$/, ""), key };
}

async function detail(response: Response): Promise<string> {
  try {
    const body = (await response.json()) as { detail?: unknown };
    if (typeof body.detail === "string") return body.detail;
  } catch {
    // Not JSON, so the status below says all there is to say.
  }
  return `The API answered ${response.status}`;
}

export async function apiGet<T>(path: string, revalidate = 30): Promise<T> {
  const { url, key } = settings();
  const response = await fetch(`${url}${path}`, {
    headers: { "x-hoopsedge-key": key },
    next: { revalidate },
  });
  if (!response.ok) throw new ApiError(response.status, await detail(response));
  return (await response.json()) as T;
}

/** Fresh by design: a run in progress or a change just made must not be served from a cache. */
export async function apiGetFresh<T>(path: string): Promise<T> {
  const { url, key } = settings();
  const response = await fetch(`${url}${path}`, {
    headers: { "x-hoopsedge-key": key },
    cache: "no-store",
  });
  if (!response.ok) throw new ApiError(response.status, await detail(response));
  return (await response.json()) as T;
}

export async function apiSend<T>(
  method: "POST" | "PUT",
  path: string,
  body: unknown,
  idempotencyKey: string,
): Promise<T> {
  const { url, key } = settings();
  const response = await fetch(`${url}${path}`, {
    method,
    headers: {
      "x-hoopsedge-key": key,
      "idempotency-key": idempotencyKey,
      "content-type": "application/json",
    },
    body: JSON.stringify(body),
    cache: "no-store",
  });
  if (!response.ok) throw new ApiError(response.status, await detail(response));
  return (await response.json()) as T;
}

/** Null for a 404, so a page can show its own "not found" rather than the error screen. */
export async function apiGetOptional<T>(path: string, revalidate = 30): Promise<T | null> {
  try {
    return await apiGet<T>(path, revalidate);
  } catch (error) {
    if (error instanceof ApiError && error.status === 404) return null;
    throw error;
  }
}
