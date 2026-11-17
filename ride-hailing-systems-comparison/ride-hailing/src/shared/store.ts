import { randomUUID, createHash } from "node:crypto";
import pg from "pg";
import type { PoolClient } from "pg";
import { HttpError, quote, transition, distance } from "./domain.js";
import type { Category, Driver, Point, RideRequest, Status, Trip } from "./domain.js";
import { redis, redisKey, fresh } from "./redis.js";
import type { Location } from "./redis.js";

export const pool = new pg.Pool({
  connectionString: process.env.DATABASE_URL ??
    "postgres://rides:local-development-only@127.0.0.1:55439/rides",
  max: 10,
  connectionTimeoutMillis: 2000,
  idleTimeoutMillis: 10_000,
  statement_timeout: 4000,
  idle_in_transaction_session_timeout: 5000,
});
pool.on("error", (error) => console.error("postgres_pool_error", error));

const tripColumns = `id, passenger_id AS "passengerId", driver_id AS "driverId",
  category, status, version, fare_cents AS "fareCents", match_distance_m AS "matchDistanceM",
  json_build_object('lat', ST_Y(pickup::geometry), 'lng', ST_X(pickup::geometry)) AS pickup,
  json_build_object('lat', ST_Y(destination::geometry), 'lng', ST_X(destination::geometry)) AS destination`;
const driverColumns = `d.id, d.name, d.category, d.busy, d.online, d.vehicle_name AS "vehicleName",
  l.sequence::float8 AS sequence, l.observed_at AS "observedAt",
  ST_Y(l.position::geometry) AS lat, ST_X(l.position::geometry) AS lng`;
const active = "'assigned','arrived','in_progress'";

export async function transaction<T>(work: (client: PoolClient) => Promise<T>): Promise<T> {
  const client = await pool.connect();
  try {
    await client.query("BEGIN");
    const result = await work(client);
    await client.query("COMMIT");
    return result;
  } catch (error) {
    try { await client.query("ROLLBACK"); }
    catch (rollbackError) { console.error("rollback_failed", rollbackError); }
    throw error;
  } finally { client.release(); }
}

async function readTrip(client: PoolClient, id: string, lock = false): Promise<Trip> {
  const { rows } = await client.query<Trip>(
    `SELECT ${tripColumns} FROM trips WHERE id=$1 ${lock ? "FOR UPDATE" : ""}`, [id]);
  const trip = rows[0];
  if (!trip) throw new HttpError(404, "Corrida nao encontrada.");
  return trip;
}
export async function getTrip(id: string): Promise<Trip> {
  return transaction((c) => readTrip(c, id));
}
async function event(client: PoolClient, trip: Trip, type: string, payload: unknown): Promise<void> {
  const { rows } = await client.query<{ value: string }>(
    "UPDATE trips SET event_sequence=event_sequence+1 WHERE id=$1 RETURNING event_sequence::text AS value", [trip.id]);
  const clock = rows[0];
  if (!clock) throw new Error("Corrida ausente ao registrar evento.");
  await client.query("INSERT INTO trip_events(id,trip_id,type,payload) VALUES ($1,$2,$3,$4)",
    [clock.value, trip.id, type, JSON.stringify(payload)]);
}

export async function assign(id: string, candidates: Location[]): Promise<Trip> {
  return transaction(async (client) => {
    const trip = await readTrip(client, id, true);
    if (trip.status !== "searching") return trip;
    const deadline = await client.query<{ expired: boolean }>(
      "SELECT search_deadline<=clock_timestamp() AS expired FROM trips WHERE id=$1", [id]);
    if (deadline.rows[0]!.expired) return expire(client, trip);
    const ranked = candidates.filter((p) => p.actor === "driver" && fresh(p) && distance(p, trip.pickup) <= 5000)
      .sort((a, b) => distance(a, trip.pickup) - distance(b, trip.pickup) || a.id.localeCompare(b.id));
    for (const candidate of ranked) {
      const { rows } = await client.query<{ id: string }>(`
        SELECT d.id FROM drivers d WHERE d.id=$1 AND d.online AND NOT d.busy AND d.category=$2
        AND NOT EXISTS (SELECT 1 FROM trips t WHERE t.driver_id=d.id AND t.status IN (${active}))
        FOR NO KEY UPDATE OF d SKIP LOCKED`, [candidate.id, trip.category]);
      if (!rows.length) continue;
      if (!fresh(candidate)) continue;
      await client.query("UPDATE drivers SET busy=true WHERE id=$1", [candidate.id]);
      await client.query(`UPDATE trips SET driver_id=$2,status='assigned',version=version+1,
        assigned_at=clock_timestamp(),match_distance_m=$3 WHERE id=$1`,
      [id, candidate.id, distance(candidate, trip.pickup)]);
      const assigned = await readTrip(client, id);
      await event(client, assigned, "trip", assigned);
      return assigned;
    }
    return trip;
  });
}

export async function requestTrip(request: RideRequest, key: string): Promise<{ trip: Trip; replay: boolean }> {
  const fingerprint = createHash("sha256").update(JSON.stringify(request)).digest("hex");
  return transaction(async (client) => {
    const passenger = await client.query(
      "SELECT id FROM passengers WHERE id=$1 FOR NO KEY UPDATE", [request.passengerId]);
    if (!passenger.rowCount) throw new HttpError(404, "Passageiro nao encontrado.");
    const previous = await client.query<{ fingerprint: string; trip_id: string }>(
      "SELECT fingerprint,trip_id FROM requests WHERE passenger_id=$1 AND idempotency_key=$2",
      [request.passengerId, key]);
    const existing = previous.rows[0];
    if (existing) {
      if (existing.fingerprint !== fingerprint) throw new HttpError(409, "Chave reutilizada com outro pedido.");
      return { trip: await readTrip(client, existing.trip_id), replay: true };
    }
    const current = await client.query(`SELECT id FROM trips WHERE passenger_id=$1
      AND status IN ('searching',${active})`, [request.passengerId]);
    if (current.rowCount) throw new HttpError(409, "Passageiro ja possui corrida ativa.");
    const id = randomUUID();
    await client.query(`INSERT INTO trips
      (id,passenger_id,category,pickup,destination,status,fare_cents)
      VALUES ($1,$2,$3,ST_SetSRID(ST_Point($4,$5),4326)::geography,
        ST_SetSRID(ST_Point($6,$7),4326)::geography,'searching',$8)`,
    [id, request.passengerId, request.category, request.pickup.lng, request.pickup.lat,
      request.destination.lng, request.destination.lat, quote(request)]);
    await client.query("INSERT INTO requests VALUES ($1,$2,$3,$4)",
      [request.passengerId, key, fingerprint, id]);
    const trip = await readTrip(client, id);
    await event(client, trip, "trip", trip);
    return { trip, replay: false };
  });
}

async function expire(client: PoolClient, trip: Trip) {
  await client.query("UPDATE trips SET status='expired',version=version+1,ended_at=clock_timestamp() WHERE id=$1", [trip.id]);
  const updated = await readTrip(client, trip.id);
  await event(client, updated, "trip", updated);
  return updated;
}
export async function dispatchBatch(): Promise<Trip[]> {
  return transaction(async (client) => {
    const { rows } = await client.query<{ id: string; expired: boolean }>(`SELECT id,
      search_deadline<=clock_timestamp() AS expired FROM trips WHERE status='searching'
      AND (last_dispatch_at IS NULL OR last_dispatch_at<clock_timestamp()-interval '2 seconds')
      ORDER BY last_dispatch_at NULLS FIRST,created_at LIMIT 20 FOR UPDATE SKIP LOCKED`);
    const pending: Trip[] = [];
    for (const candidate of rows) {
      const trip = await readTrip(client, candidate.id);
      if (candidate.expired) await expire(client, trip);
      else {
        await client.query("UPDATE trips SET last_dispatch_at=clock_timestamp() WHERE id=$1", [trip.id]);
        pending.push(trip);
      }
    }
    return pending;
  });
}
export interface DriverRecord extends Driver { online: boolean; observedAt: string; vehicleName: string }
export async function drivers(ids?: string[]): Promise<DriverRecord[]> {
  return (await pool.query<DriverRecord>(`SELECT ${driverColumns} FROM drivers d
    JOIN driver_location l ON l.driver_id=d.id ${ids ? "WHERE d.id=ANY($1::uuid[])" : ""}
    ORDER BY d.id LIMIT 2000`, ids ? [ids] : [])).rows;
}
export async function changeTrip(id: string, next: Status, expectedVersion: number, location?: Location): Promise<Trip> {
  return transaction(async (client) => {
    const trip = await readTrip(client, id, true);
    if (trip.status === next) return trip;
    if (trip.version !== expectedVersion) throw new HttpError(409, "Versao desatualizada; releia a corrida.");
    if (!transition(trip.status, next)) throw new HttpError(409, "Transicao de estado invalida.");
    if (next === "arrived" || next === "completed") {
      const target = next === "arrived" ? trip.pickup : trip.destination;
      if (!location || location.id !== trip.driverId || location.actor !== "driver" ||
          !fresh(location) || distance(location, target) > 80) {
        throw new HttpError(409, "Motorista sem GPS recente a ate 80m do ponto.");
      }
    }
    if ((next === "completed" || next === "cancelled") && trip.driverId) {
      await client.query("UPDATE drivers SET busy=false WHERE id=$1", [trip.driverId]);
    }
    await client.query(`UPDATE trips SET status=$2,version=version+1,
      ended_at=CASE WHEN $2 IN ('completed','cancelled') THEN clock_timestamp() ELSE ended_at END WHERE id=$1`, [id, next]);
    const updated = await readTrip(client, id);
    await event(client, updated, "trip", updated);
    return updated;
  });
}

export interface TripEvent { id: string; type: string; payload: unknown }
export async function events(tripId: string, after: string): Promise<TripEvent[]> {
  return (await pool.query<TripEvent>(`SELECT id::text,type,payload FROM trip_events
    WHERE trip_id=$1 AND id>$2::bigint ORDER BY id LIMIT 100`, [tripId, after])).rows;
}
export async function activeTrips(): Promise<Trip[]> {
  return (await pool.query<Trip>(`SELECT ${tripColumns} FROM trips
    WHERE status IN (${active}) ORDER BY id LIMIT 100`)).rows;
}
export async function publishEvents() {
  await transaction(async (client) => {
    const { rows } = await client.query<{ trip_id: string; id: string }>(
      `SELECT trip_id,id::text FROM trip_events WHERE published_at IS NULL
       ORDER BY created_at LIMIT 50 FOR UPDATE SKIP LOCKED`);
    for (const row of rows) {
      await redis.publish(redisKey(`trip:${row.trip_id}`), row.id);
      await client.query("UPDATE trip_events SET published_at=clock_timestamp() WHERE trip_id=$1 AND id=$2",
        [row.trip_id, row.id]);
    }
  });
}
