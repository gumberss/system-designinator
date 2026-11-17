import express from "express";
import type { ErrorRequestHandler, Express } from "express";
import { HttpError } from "./domain.js";

export function serviceApp() {
  const app = express();
  app.disable("x-powered-by");
  app.use(express.json({ limit: "64kb" }));
  let pending = 0;
  app.use((req, res, next) => {
    if (req.path.endsWith("/events")) { next(); return; }
    if (pending >= 64) { res.status(503).json({ error: "Servico ocupado; use backoff." }); return; }
    pending++;
    res.once("close", () => { pending--; });
    next();
  });
  return app;
}
export function finish(app: Express) {
  app.use((_req, res) => { res.status(404).json({ error: "Endpoint nao encontrado." }); });
  const errors: ErrorRequestHandler = (error: unknown, _req, res, _next) => {
    console.error("request_failed", error);
    if (res.headersSent) { res.end(); return; }
    if (error instanceof HttpError) { res.status(error.status).json({ error: error.message }); return; }
    if (error instanceof SyntaxError && "body" in error) {
      res.status(400).json({ error: "JSON invalido." }); return;
    }
    if (error instanceof Error && "type" in error && error.type === "entity.too.large") {
      res.status(413).json({ error: "JSON excede o limite de 64kb." }); return;
    }
    const code = error instanceof Error && "code" in error ? error.code : undefined;
    if (code === "23505") { res.status(409).json({ error: "Conflito de corrida ativa." }); return; }
    if (code === "23503") { res.status(404).json({ error: "Entidade nao encontrada." }); return; }
    if (error instanceof Error && (/timeout|Connection.*(closed|terminated)|Stream isn't writeable|reached the max retries/i.test(error.message) ||
        ["57014","53300","ECONNREFUSED","57P01","40P01","55P03"].includes(String(code)))) {
      res.status(503).json({ error: "Dependencia indisponivel ou ocupada; use backoff." }); return;
    }
    res.status(500).json({ error: "Erro interno; consulte o log." });
  };
  app.use(errors);
  return app;
}
export async function remote<T>(base: string, path: string, body?: unknown, key?: string): Promise<T> {
  let response: Response;
  try {
    response = await fetch(`${base}${path}`, {
      ...(body === undefined ? {} : { method: "POST", body: JSON.stringify(body) }),
      headers: { "Content-Type": "application/json", ...(key ? { "Idempotency-Key": key } : {}) },
      signal: AbortSignal.timeout(4000),
    });
  } catch (error) {
    console.error("upstream_unreachable", { base, path, error });
    throw new HttpError(503, "Servico dependente indisponivel; use a mesma chave no retry.");
  }
  if (!response.ok) {
    const detail: unknown = await response.json();
    console.error("upstream_rejected", { base, path, status: response.status, detail });
    const message = detail && typeof detail === "object" && "error" in detail && typeof detail.error === "string"
      ? detail.error : "Erro de contrato entre servicos.";
    throw new HttpError(response.status >= 500 ? 503 : response.status, message);
  }
  return (response.status === 204 ? undefined : await response.json()) as T;
}
export function keyHeader(value: string | undefined) {
  if (!value || value.length > 100 || !/^[\x21-\x7e]+$/.test(value)) {
    throw new HttpError(400, "Idempotency-Key ASCII de 1 a 100 caracteres obrigatoria.");
  }
  return value;
}
export const addresses = {
  trips: () => process.env.TRIP_URL ?? "http://127.0.0.1:58110",
  locations: () => process.env.LOCATION_URL ?? "http://127.0.0.1:58120",
  map: () => process.env.MAP_URL ?? "http://127.0.0.1:58130",
  finder: () => process.env.FINDER_URL ?? "http://127.0.0.1:58140",
};
