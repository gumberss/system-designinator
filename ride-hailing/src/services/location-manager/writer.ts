import { randomUUID } from "node:crypto";
import { pool, transaction } from "../../shared/store.js";
import { redis, redisKey, parseLocation } from "../../shared/redis.js";
import type { Location } from "../../shared/redis.js";

const group = "postgres-writer";
const consumer = `writer-${randomUUID()}`;
export async function initializeWriter() {
  try { await redis.xgroup("CREATE", redisKey("gps-stream"), group, "0", "MKSTREAM"); }
  catch (error) {
    if (!(error instanceof Error && error.message.includes("BUSYGROUP"))) throw error;
  }
}
interface Sample { streamId: string; value: Location }
function parseEntries(value: unknown): Sample[] {
  if (!Array.isArray(value)) throw new Error("Formato de entries Redis invalido.");
  return value.map((entry: unknown) => {
    if (!Array.isArray(entry) || typeof entry[0] !== "string" ||
        !Array.isArray(entry[1]) || entry[1][0] !== "payload" || typeof entry[1][1] !== "string") {
      throw new Error("GPS stream corrompido; lote nao confirmado.");
    }
    return { streamId: entry[0], value: parseLocation(entry[1][1]) };
  });
}
async function pendingOrNew(): Promise<Sample[]> {
  const own: unknown = await redis.xreadgroup("GROUP", group, consumer, "COUNT", 2000,
    "STREAMS", redisKey("gps-stream"), "0");
  if (Array.isArray(own) && Array.isArray(own[0])) {
    const records = parseEntries(own[0][1]);
    if (records.length) return records;
  }
  const claimed: unknown = await redis.xautoclaim(redisKey("gps-stream"), group, consumer, 30_000, "0-0", "COUNT", 2000);
  if (!Array.isArray(claimed)) throw new Error("Resposta XAUTOCLAIM invalida.");
  const recovered = parseEntries(claimed[1]);
  if (recovered.length) return recovered;
  const result: unknown = await redis.xreadgroup("GROUP", group, consumer, "COUNT", 2000,
    "STREAMS", redisKey("gps-stream"), ">");
  if (result === null) return [];
  if (!Array.isArray(result) || !Array.isArray(result[0])) throw new Error("Resposta XREADGROUP invalida.");
  return parseEntries(result[0][1]);
}
export async function flushLocations(): Promise<number> {
  const samples = await pendingOrNew();
  if (!samples.length) return 0;
  await persistSamples(samples.map((s) => s.value));
  // Commit before ACK. A crash here replays: SQL upserts and route PK deduplicate.
  const ids = samples.map((s) => s.streamId);
  await redis.eval(`
    redis.call('XACK',KEYS[1],ARGV[1],unpack(ARGV,2))
    return redis.call('XDEL',KEYS[1],unpack(ARGV,2))
  `, 1, redisKey("gps-stream"), group, ...ids);
  return samples.length;
}
export async function persistSamples(samples: Location[]) {
  const newest = new Map<string, Location>();
  for (const sample of samples) {
    const key = `${sample.actor}:${sample.id}`, previous = newest.get(key);
    if (!previous || previous.sequence < sample.sequence) newest.set(key, sample);
  }
  await transaction(async (client) => {
    const drivers = [...new Set(samples.filter((p) => p.actor === "driver").map((p) => p.id))].sort();
    const riders = [...new Set(samples.filter((p) => p.actor === "rider").map((p) => p.id))].sort();
    // Same driver lock used by assignment/release, before reading trip time windows.
    await client.query("SELECT id FROM drivers WHERE id=ANY($1::uuid[]) ORDER BY id FOR NO KEY UPDATE", [drivers]);
    for (const [actor, ids] of [["driver", drivers], ["rider", riders]] as const) {
      if (!ids.length) continue;
      const points = [...newest.values()].filter((p) => p.actor === actor);
      await client.query(`INSERT INTO ${actor}_location(${actor}_id,position,sequence,observed_at,received_at)
        SELECT id::uuid,ST_SetSRID(ST_Point(lng,lat),4326)::geography,sequence,
          "observedAt"::timestamptz,to_timestamp("receivedAt"/1000.0)
        FROM jsonb_to_recordset($1::jsonb) AS p(id text,lat float8,lng float8,sequence bigint,
          "observedAt" text,"receivedAt" float8)
        ON CONFLICT (${actor}_id) DO UPDATE SET position=excluded.position,sequence=excluded.sequence,
          observed_at=excluded.observed_at,received_at=excluded.received_at
        WHERE ${actor}_location.sequence<excluded.sequence`, [JSON.stringify(points)]);
      await client.query(`INSERT INTO route_points(trip_id,actor,sequence,lat,lng,observed_at,received_at)
        SELECT t.id,$2,p.sequence,p.lat,p.lng,p."observedAt"::timestamptz,to_timestamp(p."receivedAt"/1000.0)
        FROM jsonb_to_recordset($1::jsonb) AS p(id text,lat float8,lng float8,sequence bigint,
          "observedAt" text,"receivedAt" float8)
        JOIN trips t ON t.${actor === "driver" ? "driver_id" : "passenger_id"}=p.id::uuid
        WHERE t.assigned_at<=to_timestamp(p."receivedAt"/1000.0)
          AND (t.ended_at IS NULL OR to_timestamp(p."receivedAt"/1000.0)<=t.ended_at)
        ON CONFLICT DO NOTHING`, [JSON.stringify(samples.filter((p) => p.actor === actor)), actor]);
    }
  });
}
export async function writerHealth() {
  await pool.query("SELECT 1");
  return { streamLength: await redis.xlen(redisKey("gps-stream")),
    pending: await redis.xpending(redisKey("gps-stream"), group) };
}
