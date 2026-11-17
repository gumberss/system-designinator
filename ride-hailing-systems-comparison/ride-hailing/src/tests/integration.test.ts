import { test } from "node:test";
import assert from "node:assert/strict";
import { randomUUID } from "node:crypto";
import { readFile } from "node:fs/promises";
import type { AddressInfo } from "node:net";
import type { Server } from "node:http";
import type { Express } from "express";
import pg from "pg";
import type { Trip } from "../shared/domain.js";

test("servicos HTTP + Redis + PostgreSQL/PostGIS reais", { timeout: 180_000 }, async (t) => {
  const connectionString = process.env.DATABASE_URL ??
    "postgres://rides:local-development-only@127.0.0.1:55439/rides";
  const admin = new pg.Pool({ connectionString, max: 1 });
  const schema = `verify_${randomUUID().replaceAll("-", "")}`;
  const servers: Server[] = [];
  let cleanup: (() => Promise<void>) | undefined;
  const bind = async (app: Express) => {
    const server = app.listen(0, "127.0.0.1");
    servers.push(server);
    await new Promise<void>((resolve) => server.once("listening", resolve));
    return `http://127.0.0.1:${(server.address() as AddressInfo).port}`;
  };
  try {
    await admin.query(`CREATE SCHEMA ${schema}`);
    await admin.query(`SET search_path TO ${schema}, public`);
    await admin.query(await readFile(new URL("../../infra/001-schema.sql", import.meta.url), "utf8"));
    process.env.DATABASE_URL = connectionString;
    process.env.PGOPTIONS = `-c search_path=${schema},public`;
    process.env.REDIS_PREFIX = schema;
    const store = await import("../shared/store.js");
    const cache = await import("../shared/redis.js");
    const writer = await import("../services/location-manager/writer.js");
    const { MapIndex, mapApp } = await import("../services/quadtree-map/app.js");
    const { locationApp } = await import("../services/location-manager/app.js");
    const { createApp } = await import("../services/trip-manager/app.js");
    const { finderApp, findDriver } = await import("../services/find-driver/app.js");
    const { requestApp } = await import("../services/request-vehicle/app.js");
    await cache.redis.connect();
    const signals = new cache.Signals();
    await signals.start();
    const clearCache = async () => {
      const keys = await cache.redis.keys(`${schema}:*`);
      if (keys.length) await cache.redis.del(...keys);
    };
    cleanup = async () => {
      await signals.close();
      await clearCache();
      cache.redis.disconnect();
      await store.pool.end();
    };
    const tripUrls = [await bind(createApp(signals)), await bind(createApp(signals))];
    process.env.TRIP_URL = tripUrls[0]!;
    process.env.LOCATION_URL = await bind(locationApp(signals));
    const index = new MapIndex();
    process.env.MAP_URL = await bind(mapApp(index));
    process.env.FINDER_URL = await bind(finderApp());
    const requestUrls = [await bind(requestApp()), await bind(requestApp())];
    const passengerId = "10000000-0000-4000-8000-000000000001";
    const driverId = "20000000-0000-4000-8000-000000000001";
    const pickup = { lat: -23.5614, lng: -46.6559 };
    const destination = { lat: -23.568, lng: -46.648 };
    const body = { passengerId, category: "standard", pickup, destination };
    async function post(base: string, path: string, value: unknown, key?: string) {
      return fetch(`${base}${path}`, {
        method: "POST", headers: { "Content-Type": "application/json", ...(key ? { "Idempotency-Key": key } : {}) },
        body: JSON.stringify(value), signal: AbortSignal.timeout(12_000),
      });
    }
    let overloadResponses = 0;
    async function request(value = body, key = randomUUID(), replica = 0) {
      for (let attempt = 0; attempt < 5; attempt++) {
        const response = await post(requestUrls[replica]!, "/api/trips", value, key);
        if (response.status !== 503) return response;
        overloadResponses++;
        await response.arrayBuffer();
        await new Promise((resolve) => setTimeout(resolve, 300 * (attempt + 1)));
      }
      throw new Error("Budget de retry esgotado.");
    }
    async function gps(id: string, position = pickup, actor = "driver") {
      const current = (await cache.latest(actor === "driver" ? "driver" : "rider", [id]))[0];
      return post(process.env.LOCATION_URL!, `/api/${actor === "driver" ? "drivers" : "riders"}/${id}/location`,
        { ...position, sequence: (current?.sequence ?? 0) + 1, observedAt: new Date().toISOString() });
    }
    async function reset() {
      await store.pool.query(`TRUNCATE route_points,requests,trip_events,trips,rider_location;
        UPDATE drivers SET busy=false,online=true;
        UPDATE driver_location SET sequence=0,observed_at='1970-01-01',received_at='1970-01-01'`);
      await clearCache();
      await writer.initializeWriter();
      const records = await store.drivers();
      for (const d of records) assert.equal((await gps(d.id, d)).status, 204);
      await index.rebuild();
    }
    async function create(key = randomUUID()) {
      const response = await request(body, key);
      assert.equal(response.status, 201, await response.clone().text());
      return ((await response.json()) as { trip: Trip }).trip;
    }
    async function status(trip: Trip, state: string) {
      const response = await post(tripUrls[1]!, `/api/trips/${trip.id}/status`, { status: state, version: trip.version });
      assert.equal(response.status, 200, await response.clone().text());
      return await response.json() as Trip;
    }
    await t.test("schema limpo separa cadastro e checkpoint de localizacao", async () => {
      assert.equal((await store.drivers()).length, 4);
      const { rows } = await store.pool.query<{ column_name: string }>(
        `SELECT column_name FROM information_schema.columns
         WHERE table_schema=$1 AND table_name='drivers'`, [schema]);
      assert.ok(!rows.some(({ column_name }) => ["position", "sequence", "observed_at", "received_at"].includes(column_name)));
      assert.equal((await store.pool.query("SELECT count(*)::int AS n FROM driver_location")).rows[0]!.n, 4);
      assert.equal((await store.pool.query(
        "SELECT to_regclass($1) AS name", [`${schema}.schema_migrations`])).rows[0]!.name, null);
    });
    await t.test("hash recebe GPS sem sincronizar SQL, valida sequencia e prazo", async () => {
      await reset();
      assert.equal((await gps(driverId)).status, 204);
      assert.equal((await store.drivers()).find((d) => d.id === driverId)!.sequence, 0);
      const current = (await cache.latest("driver", [driverId]))[0]!;
      assert.equal(current.sequence, 2);
      assert.equal((await post(process.env.LOCATION_URL!, `/api/drivers/${driverId}/location`, current)).status, 409);
      assert.equal((await post(process.env.LOCATION_URL!, `/api/drivers/${driverId}/location`,
        { ...current, sequence: 3, observedAt: new Date(Date.now() - 31_000).toISOString() })).status, 400);
      assert.equal((await gps(randomUUID())).status, 404);
      assert.equal((await gps(passengerId, pickup, "rider")).status, 204);
    });
    await t.test("quadtree + delta encontra motorista novo e cruzamento entre regioes", async () => {
      await reset();
      await gps(driverId, { lat: 0, lng: 0 });
      await index.rebuild();
      await gps(driverId, pickup);
      const response = await fetch(`${process.env.LOCATION_URL}/api/drivers/nearby?lat=${pickup.lat}&lng=${pickup.lng}&category=standard`);
      assert.equal(response.status, 200);
      const drivers = await response.json() as { id: string }[];
      assert.ok(drivers.some((d) => d.id === driverId));
      assert.ok(Number(response.headers.get("X-Map-Delta")) >= 1);
      await store.pool.query("UPDATE drivers SET online=false WHERE id=$1", [driverId]);
      const none = await fetch(`${process.env.LOCATION_URL}/api/drivers/nearby?lat=0&lng=0&category=standard`);
      assert.deepEqual(await none.json(), []);
      const raw = await cache.redis.hgetall(cache.redisKey("latest:driver"));
      for (const [id, value] of Object.entries(raw)) {
        const v = JSON.parse(value) as { receivedAt: number };
        v.receivedAt -= 16_000;
        await cache.redis.hset(cache.redisKey("latest:driver"), id, JSON.stringify(v));
      }
      const stale = await fetch(`${process.env.LOCATION_URL}/api/drivers/nearby?lat=${pickup.lat}&lng=${pickup.lng}&category=standard`);
      assert.deepEqual(await stale.json(), []);
    });
    await t.test("delta e hash saturados rejeitam sem truncar nem confirmar GPS", async () => {
      await reset();
      const entries = Array.from({ length: 2001 }, () => [Date.now(), randomUUID()]).flat();
      await cache.redis.zadd(cache.redisKey("driver-changes"), ...entries);
      const response = await fetch(`${process.env.LOCATION_URL}/api/drivers/nearby?lat=${pickup.lat}&lng=${pickup.lng}&category=standard`);
      assert.equal(response.status, 503);
      const location = (await cache.latest("driver", [driverId]))[0]!;
      await cache.redis.del(cache.redisKey("latest:driver"));
      const fields: Record<string, string> = {};
      for (let i = 0; i < 10_000; i++) {
        const id = randomUUID();
        fields[id] = JSON.stringify({ ...location, id });
      }
      await cache.redis.hset(cache.redisKey("latest:driver"), fields);
      const before = await cache.redis.xlen(cache.redisKey("gps-stream"));
      assert.equal((await post(process.env.LOCATION_URL!, `/api/drivers/${driverId}/location`,
        { ...pickup, sequence: 10, observedAt: new Date().toISOString() })).status, 503);
      assert.equal(await cache.redis.hget(cache.redisKey("latest:driver"), driverId), null);
      assert.equal(await cache.redis.xlen(cache.redisKey("gps-stream")), before);
    });
    await t.test("40 pedidos concorrentes em duas request APIs: uma atribuicao e 39 searching", async () => {
      await reset();
      await store.pool.query("UPDATE drivers SET online=(id=$1)", [driverId]);
      const ids = Array.from({ length: 40 }, () => randomUUID());
      await store.pool.query("INSERT INTO passengers SELECT id,'Concorrente' FROM unnest($1::uuid[]) id", [ids]);
      const replies = await Promise.all(ids.map((id, i) => request({ ...body, passengerId: id }, randomUUID(), i % 2)));
      const trips: Trip[] = [];
      for (const response of replies) {
        assert.ok([200, 201].includes(response.status), await response.clone().text());
        trips.push(((await response.json()) as { trip: Trip }).trip);
      }
      assert.equal(new Set(trips.map((v) => v.id)).size, 40);
      assert.equal(trips.filter((v) => v.status === "assigned").length, 1);
      assert.equal(trips.filter((v) => v.status === "searching").length, 39);
      assert.equal((await store.pool.query("SELECT driver_id FROM trips WHERE status='assigned' GROUP BY driver_id HAVING count(*)>1")).rowCount, 0);
    });
    await t.test("24 retries retornam um ID; payload e chave conflitantes falham", async () => {
      await reset();
      const key = randomUUID();
      const replies = await Promise.all(Array.from({ length: 24 }, (_, i) => request(body, key, i % 2)));
      const ids = new Set<string>();
      for (const response of replies) {
        assert.ok([200, 201].includes(response.status), await response.clone().text());
        ids.add(((await response.json()) as { trip: Trip }).trip.id);
      }
      assert.equal(ids.size, 1);
      assert.equal((await store.pool.query("SELECT * FROM requests")).rowCount, 1);
      assert.equal((await request({ ...body, category: "comfort" }, key)).status, 409);
      assert.equal((await request(body, randomUUID())).status, 409);
      const trip = await store.getTrip([...ids][0]!);
      await status(trip, "cancelled");
      assert.equal(((await (await request(body, key)).json()) as { trip: Trip }).trip.status, "cancelled");
    });
    await t.test("UNIQUE protege SQL direto e transicoes exigem versao e GPS recente", async () => {
      await reset();
      const trip = await create();
      await assert.rejects(store.pool.query(`INSERT INTO trips
        (id,passenger_id,driver_id,category,pickup,destination,status,fare_cents)
        SELECT $1,$2,driver_id,category,pickup,destination,'assigned',fare_cents FROM trips WHERE id=$3`,
      [randomUUID(), "10000000-0000-4000-8000-000000000002", trip.id]), { code: "23505" });
      assert.equal((await post(tripUrls[0]!, `/api/trips/${trip.id}/status`,
        { status: "arrived", version: trip.version })).status, 409);
      await gps(trip.driverId!);
      const arrived = await status(trip, "arrived");
      assert.equal((await post(tripUrls[0]!, `/api/trips/${trip.id}/status`,
        { status: "in_progress", version: trip.version })).status, 409);
      const started = await status(arrived, "in_progress");
      assert.equal((await post(tripUrls[0]!, `/api/trips/${trip.id}/status`,
        { status: "cancelled", version: started.version })).status, 409);
      await gps(trip.driverId!, destination);
      const completed = await status(started, "completed");
      assert.equal(completed.status, "completed");
      assert.equal((await store.drivers()).find((v) => v.id === trip.driverId)!.busy, false);
    });
    await t.test("flush preserva todos os pontos da viagem inclusive apos conclusao; replay nao duplica", async () => {
      await reset();
      const trip = await create();
      await gps(trip.driverId!);
      await gps(passengerId, pickup, "rider");
      const arrived = await status(trip, "arrived");
      const started = await status(arrived, "in_progress");
      await gps(trip.driverId!, destination);
      await gps(passengerId, destination, "rider");
      await status(started, "completed");
      const raw = await cache.redis.xrange(cache.redisKey("gps-stream"), "-", "+");
      const samples = raw.map((entry) => cache.parseLocation(entry[1][1]!));
      const count = await writer.flushLocations();
      assert.ok(count >= 8);
      assert.equal(await cache.redis.xlen(cache.redisKey("gps-stream")), 0);
      const before = (await store.pool.query("SELECT count(*)::int AS n FROM route_points")).rows[0].n;
      assert.ok(before >= 4);
      await writer.persistSamples(samples);
      assert.equal((await store.pool.query("SELECT count(*)::int AS n FROM route_points")).rows[0].n, before);
      const route = await fetch(`${tripUrls[0]}/api/trips/${trip.id}/route?actor=driver`);
      assert.ok(((await route.json()) as { points: unknown[] }).points.length >= 2);
      assert.equal((await store.drivers()).find((v) => v.id === trip.driverId)!.sequence, 3);
    });
    await t.test("lote pendente recupera depois de falha entre leitura e commit", async () => {
      await reset();
      await store.pool.query("ALTER TABLE driver_location ADD CONSTRAINT force_flush_failure CHECK(sequence=0)");
      try { await assert.rejects(writer.flushLocations(), { code: "23514" }); }
      finally { await store.pool.query("ALTER TABLE driver_location DROP CONSTRAINT force_flush_failure"); }
      assert.ok(await cache.redis.xlen(cache.redisKey("gps-stream")) > 0);
      assert.ok(await writer.flushLocations() > 0);
      assert.equal(await cache.redis.xlen(cache.redisKey("gps-stream")), 0);
    });
    await t.test("writer recupera pending ocioso de outro consumidor apos crash", async () => {
      await reset();
      const stream = cache.redisKey("gps-stream");
      await cache.redis.xreadgroup("GROUP", "postgres-writer", "crashed-writer", "STREAMS", stream, ">");
      const entries = await cache.redis.xrange(stream, "-", "+");
      assert.equal(entries.length, 4);
      await cache.redis.xclaim(stream, "postgres-writer", "crashed-writer", 0,
        ...entries.map((entry) => entry[0]), "IDLE", 31_000);
      assert.equal(await writer.flushLocations(), entries.length);
      assert.equal(await cache.redis.xlen(stream), 0);
      assert.equal((await store.drivers()).find((v) => v.id === driverId)!.sequence, 1);
    });
    await t.test("falha no evento reverte pedido; ordens por corrida sao independentes", async () => {
      await reset();
      await store.pool.query("ALTER TABLE trip_events ADD CONSTRAINT force_event_failure CHECK(type<>'trip') NOT VALID");
      try { await assert.rejects(store.requestTrip({ ...body, category: "standard" }, randomUUID()), { code: "23514" }); }
      finally { await store.pool.query("ALTER TABLE trip_events DROP CONSTRAINT force_event_failure"); }
      assert.equal((await store.pool.query("SELECT * FROM trips")).rowCount, 0);
      const a = await store.requestTrip({ ...body, category: "standard" }, randomUUID());
      const b = await store.requestTrip({ ...body, passengerId: "10000000-0000-4000-8000-000000000002", category: "standard" }, randomUUID());
      assert.equal((await store.events(a.trip.id, "0"))[0]!.id, "1");
      assert.equal((await store.events(b.trip.id, "0"))[0]!.id, "1");
      const c1 = await store.pool.connect(), c2 = await store.pool.connect();
      try {
        await c1.query("BEGIN"); await c2.query("BEGIN");
        await c1.query("UPDATE trips SET event_sequence=event_sequence+1 WHERE id=$1", [a.trip.id]);
        await c2.query("SET LOCAL statement_timeout=500");
        await c2.query("UPDATE trips SET event_sequence=event_sequence+1 WHERE id=$1", [b.trip.id]);
      } finally {
        await c1.query("ROLLBACK"); await c2.query("ROLLBACK");
        c1.release(); c2.release();
      }
    });
    await t.test("indisponibilidade de mapa nao perde pedido; retry retoma a mesma intencao", async () => {
      await reset();
      const saved = process.env.MAP_URL;
      process.env.MAP_URL = "http://127.0.0.1:1";
      const key = randomUUID();
      try { assert.equal((await post(requestUrls[0]!, "/api/trips", body, key)).status, 503); }
      finally { process.env.MAP_URL = saved; }
      const persisted = (await store.pool.query<{ id: string }>("SELECT id FROM trips")).rows[0]!.id;
      const response = await request(body, key, 1);
      assert.equal(response.status, 200);
      assert.equal(((await response.json()) as { trip: Trip }).trip.id, persisted);
    });
    await t.test("dispatcher recupera searching e expira pedidos vencidos", async () => {
      await reset();
      await store.pool.query("UPDATE drivers SET online=false");
      const waiting = await create();
      assert.equal(waiting.status, "searching");
      await store.pool.query("UPDATE drivers SET online=true");
      const batch = await store.dispatchBatch();
      await Promise.all(batch.map((v) => findDriver(v.id)));
      assert.equal((await store.getTrip(waiting.id)).status, "assigned");
      await status(await store.getTrip(waiting.id), "cancelled");
      await store.pool.query("UPDATE drivers SET online=false");
      const expired = await create();
      await store.pool.query("UPDATE trips SET search_deadline=clock_timestamp()-interval '1 second' WHERE id=$1", [expired.id]);
      await store.dispatchBatch();
      assert.equal((await store.getTrip(expired.id)).status, "expired");
    });
    await t.test("SSE de estado reexecuta cursor da corrida; GPS transmite ultimo valor sem SQL polling", async () => {
      await reset();
      const trip = await create();
      const cursor = (await store.events(trip.id, "0")).at(-1)!.id;
      const cancelled = await status(trip, "cancelled");
      await store.publishEvents();
      const response = await fetch(`${tripUrls[1]}/api/trips/${trip.id}/events`,
        { headers: { "Last-Event-ID": cursor }, signal: AbortSignal.timeout(6000) });
      const reader = response.body!.getReader();
      const text = new TextDecoder().decode((await reader.read()).value);
      await reader.cancel();
      assert.match(text, /cancelled/);
      assert.ok(!text.includes(`id: ${cursor}\n`));
      assert.equal(cancelled.version, trip.version + 1);
      const gpsResponse = await fetch(`${process.env.LOCATION_URL}/api/drivers/${driverId}/events`,
        { signal: AbortSignal.timeout(6000) });
      const gpsReader = gpsResponse.body!.getReader();
      const gpsText = new TextDecoder().decode((await gpsReader.read()).value);
      await gpsReader.cancel();
      assert.match(gpsText, /event: location/);
      assert.match(gpsText, /"actor":"driver"/);
    });
    await t.test("cursor legado adiantado reinicia replay e preserva tipo location", async () => {
      await reset();
      const trip = await create();
      const { rows } = await store.pool.query<{ id: string }>(
        "UPDATE trips SET event_sequence=event_sequence+1 WHERE id=$1 RETURNING event_sequence::text AS id", [trip.id]);
      await store.pool.query("INSERT INTO trip_events(trip_id,id,type,payload) VALUES ($1,$2,'location',$3)",
        [trip.id, rows[0]!.id, JSON.stringify({ legacy: true })]);
      const response = await fetch(`${tripUrls[1]}/api/trips/${trip.id}/events`,
        { headers: { "Last-Event-ID": "999999" }, signal: AbortSignal.timeout(6000) });
      assert.equal(response.status, 200);
      const reader = response.body!.getReader(), decoder = new TextDecoder();
      let text = "";
      try {
        while (!text.includes('"legacy":true')) {
          const result = await reader.read();
          assert.equal(result.done, false);
          text += decoder.decode(result.value, { stream: true });
        }
      } finally { await reader.cancel(); }
      assert.match(text, /id: 1\nevent: trip/);
      assert.match(text, /event: location\ndata: \{"legacy":true\}/);
    });
    await t.test("desconexao durante subscribe libera assinatura tardia de estado e GPS", async () => {
      await reset();
      const trip = await create();
      const watch = signals.watch;
      for (const url of [`${tripUrls[0]}/api/trips/${trip.id}/events`,
        `${process.env.LOCATION_URL}/api/drivers/${driverId}/events`]) {
        let enter = () => {}, finishSubscribe = () => {}, released = 0;
        const entered = new Promise<void>((resolve) => { enter = resolve; });
        const gate = new Promise<void>((resolve) => { finishSubscribe = resolve; });
        signals.watch = async () => { enter(); await gate; return () => { released++; }; };
        const controller = new AbortController();
        const request = fetch(url, { signal: controller.signal }).catch((error: unknown) => {
          assert.ok(error instanceof Error && error.name === "AbortError");
        });
        try {
          await entered;
          controller.abort();
          await request;
          await new Promise((resolve) => setTimeout(resolve, 50));
          finishSubscribe();
          for (let attempt = 0; released === 0 && attempt < 20; attempt++) {
            await new Promise((resolve) => setTimeout(resolve, 10));
          }
          assert.equal(released, 1);
        } finally {
          controller.abort();
          finishSubscribe();
          signals.watch = watch;
        }
      }
    });
    await t.test("Redis indisponivel falha explicitamente sem fallback para SQL obsoleto", async () => {
      await reset();
      const ended = new Promise<void>((resolve) => cache.redis.once("end", resolve));
      cache.redis.disconnect();
      await ended;
      try {
        assert.equal((await post(process.env.LOCATION_URL!, `/api/drivers/${driverId}/location`,
          { ...pickup, sequence: 10, observedAt: new Date().toISOString() })).status, 503);
      }
      finally { await cache.redis.connect(); }
    });
    await t.test("contratos invalidos e pool saturado devolvem erro explicito", async () => {
      await reset();
      assert.equal((await post(requestUrls[0]!, "/api/trips", body)).status, 400);
      assert.equal((await request({ ...body, pickup: { lat: 91, lng: 0 } })).status, 400);
      assert.equal((await fetch(`${tripUrls[0]}/api/trips/no-uuid`)).status, 400);
      assert.equal((await post(requestUrls[0]!, "/api/trips", { text: "x".repeat(66000) }, "large")).status, 413);
      const held = await Promise.all(Array.from({ length: 10 }, () => store.pool.connect()));
      try { assert.equal((await fetch(`${tripUrls[0]}/health/ready`)).status, 503); }
      finally { held.forEach((v) => v.release()); }
    });
    console.log(`integration_overload_responses=${overloadResponses}; pedidos logicos usam ate 5 tentativas`);
  } finally {
    await Promise.all(servers.map((server) => new Promise<void>((resolve) => {
      server.close(() => resolve()); server.closeAllConnections();
    })));
    await cleanup?.();
    await admin.query(`DROP SCHEMA ${schema} CASCADE`);
    await admin.end();
  }
});
