export type VisitProxyRequest = {
  baseUrl: string;
  timeoutMs: number;
  userId: string;
  method: string;
  originalUrl: string;
  body?: unknown;
};

export type VisitProxyResponse = {
  status: number;
  contentType: string;
  body: Uint8Array;
};

export class VisitProxyError extends Error {
  constructor(
    message: string,
    public readonly statusCode: number,
  ) {
    super(message);
    this.name = "VisitProxyError";
  }
}

type FetchLike = typeof fetch;

export function visitUpstreamUrl(baseUrl: string, originalUrl: string): URL {
  const path = new URL(originalUrl, "http://new-agent.local");
  if (path.pathname !== "/api/visit" && !path.pathname.startsWith("/api/visit/")) {
    throw new VisitProxyError("非法的拜访业务路径", 400);
  }
  const upstream = new URL(baseUrl);
  upstream.pathname = path.pathname;
  upstream.search = path.search;
  return upstream;
}

export async function forwardVisitRequest(
  request: VisitProxyRequest,
  fetchImpl: FetchLike = fetch,
): Promise<VisitProxyResponse> {
  const method = request.method.toUpperCase();
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), request.timeoutMs);
  timer.unref();

  try {
    const upstream = await fetchImpl(visitUpstreamUrl(request.baseUrl, request.originalUrl), {
      method,
      headers: {
        accept: "application/json",
        "content-type": "application/json",
        // 必须使用 New-Agent 已验证的用户，不透传浏览器自报的身份。
        "x-user-id": request.userId,
      },
      body: method === "GET" || method === "HEAD"
        ? undefined
        : JSON.stringify(request.body ?? {}),
      signal: controller.signal,
    });
    return {
      status: upstream.status,
      contentType: upstream.headers.get("content-type") || "application/json; charset=utf-8",
      body: new Uint8Array(await upstream.arrayBuffer()),
    };
  } catch (error) {
    if (error instanceof VisitProxyError) throw error;
    if (controller.signal.aborted) {
      throw new VisitProxyError("拜访服务请求超时", 504);
    }
    throw new VisitProxyError("无法连接拜访服务，请确认 Spring Boot 已启动", 502);
  } finally {
    clearTimeout(timer);
  }
}
