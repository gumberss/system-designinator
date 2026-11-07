import { Redis } from "ioredis";
import { HttpError, object, point, positiveInteger, uuid } from "./domain.js";
import type { Point } from "./domain.js";

export type Actor = "driver" | "rider";
export interface Location extends Point {
  id: string;
  actor: Actor;
  sequence: number;
  observedAt: string;
  receivedAt: number;
}
export const namespace = process.env.REDIS_PREFIX ?? "rides";
export const redisKey = (suffix: string) => `${namespace}:${suffix}`;
export function connectRedis() {
  const client = new Redis(process.env.REDIS_URL ?? "redis://127.0.0.1:56379", {
    lazyConnect: true, enableOfflineQueue: false, maxRetriesPerRequest: 1,
    connectTimeout: 2000, commandTimeout: 3000,
    retryStrategy: (attempt) => Math.min(attempt * 200, 2000),
  });
  client.on("error", (error) => console.error("redis_error", error.message));
  return client;
}
export const redis = connectRedis();
export function locationInput(value: unknown) {
  const v = object(value);
  if (typeof v.observedAt !== "string" || !Number.isFinite(Date.parse(v.observedAt)) ||
      Math.abs(Date.now() - Date.parse(v.observedAt)) > 30_000) {
    throw new HttpError(400, "observedAt deve estar a ate 30s do servidor.");
  }
  return { ...point(v), sequence: positiveInteger(v.sequence), observedAt: new Date(v.observedAt).toISOString() };
}
export function parseLocation(raw: string): Location {
  const v = object(JSON.parse(raw));
  const actor = v.actor;
  if (actor !== "driver" && actor !== "rider") throw new Error("Actor corrompido em Redis.");
  if (typeof v.observedAt !== "string" || !Number.isFinite(Date.parse(v.observedAt)) ||
      typeof v.receivedAt !== "number" || !Number.isFinite(v.receivedAt)) throw new Error("Timestamp corrompido em Redis.");
  return { id: uuid(v.id), actor, ...point(v), sequence: positiveInteger(v.sequence),
    observedAt: v.observedAt, receivedAt: v.receivedAt };
}
export const fresh = (p: Location, now = Date.now()) =>
  now - p.receivedAt < 15_000 && now - p.receivedAt >= -1000 &&
  now - Date.parse(p.observedAt) < 30_000;

const acceptScript = `
local raw=redis.call('HGET',KEYS[1],ARGV[1])
local v=cjson.decode(ARGV[2])
local oldSeq=tonumber(ARGV[3])
local oldTime=tonumber(ARGV[4])
if raw then
  local old=cjson.decode(raw)
  oldSeq=math.max(oldSeq,old.sequence)
  oldTime=math.max(oldTime,old.observedMs)
elseif redis.call('HLEN',KEYS[1])>=10000 then
  return -2
end
if v.sequence<=oldSeq or v.observedMs<oldTime then return -1 end
if redis.call('XLEN',KEYS[2])>=100000 then return -3 end
local now=redis.call('TIME')
v.receivedAt=tonumber(now[1])*1000+math.floor(tonumber(now[2])/1000)
local payload=cjson.encode(v)
redis.call('XADD',KEYS[2],'*','payload',payload)
redis.call('HSET',KEYS[1],ARGV[1],payload)
if v.actor=='driver' then
  redis.call('ZADD',KEYS[4],v.receivedAt,ARGV[1])
end
redis.call('PUBLISH',KEYS[3],payload)
return payload`;

export async function acceptLocation(actor: Actor, id: string, value: ReturnType<typeof locationInput>,
  baseline: { sequence: number; observedAt: string }) {
  const payload = { actor, id, ...value, observedMs: Date.parse(value.observedAt) };
  const raw = await redis.eval(acceptScript, 4, redisKey(`latest:${actor}`), redisKey("gps-stream"),
    redisKey(`location:${actor}:${id}`), redisKey("driver-changes"), id, JSON.stringify(payload),
    baseline.sequence, Date.parse(baseline.observedAt));
  if (raw === -1) throw new HttpError(409, "GPS fora de ordem ou sequencia repetida.");
  if (raw === -2 || raw === -3) throw new HttpError(503, "Limite local de localizacoes/backlog atingido; use backoff.");
  if (typeof raw !== "string") throw new Error("Resposta inesperada do script de GPS.");
  return parseLocation(raw);
}
export async function latest(actor: Actor, ids: string[]): Promise<Location[]> {
  if (!ids.length) return [];
  return (await redis.hmget(redisKey(`latest:${actor}`), ...ids))
    .filter((raw): raw is string => raw !== null).map(parseLocation);
}
export async function allLocations(actor: Actor) {
  return Object.values(await redis.hgetall(redisKey(`latest:${actor}`))).map(parseLocation);
}

// One subscriber connection per process, not per connected browser.
export class Signals {
  private readonly client = connectRedis();
  private readonly handlers = new Map<string, Set<(payload: string) => void>>();
  private pending: Promise<void> = Promise.resolve();
  private command(work: () => Promise<void>) {
    const next = this.pending.then(work);
    this.pending = next.catch((error) => { console.error("subscription_failed", error); });
    return next;
  }
  async start() {
    await this.client.connect();
    this.client.on("message", (channel, payload) => {
      for (const handler of this.handlers.get(channel) ?? []) handler(payload);
    });
  }
  async watch(channel: string, handler: (payload: string) => void) {
    let handlers = this.handlers.get(channel);
    if (!handlers) {
      handlers = new Set();
      this.handlers.set(channel, handlers);
    }
    handlers.add(handler);
    try { await this.command(async () => { await this.client.subscribe(channel); }); }
    catch (error) { handlers.delete(handler); throw error; }
    return () => {
      handlers.delete(handler);
      void this.command(async () => {
        if (handlers.size === 0 && this.handlers.get(channel) === handlers) {
          this.handlers.delete(channel);
          await this.client.unsubscribe(channel);
        }
      });
    };
  }
  async close() { this.client.disconnect(); }
}
