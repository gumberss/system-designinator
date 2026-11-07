import { serviceApp, finish, keyHeader, remote, addresses } from "../../shared/http.js";
import { HttpError, idList, object, positiveInteger, rideRequest, uuid } from "../../shared/domain.js";
import type { Status } from "../../shared/domain.js";
import { redisKey } from "../../shared/redis.js";
import type { Location, Signals } from "../../shared/redis.js";
import * as store from "../../shared/store.js";
export function createApp(signals: Signals) {
  const app = serviceApp();
  app.get("/health/ready", async (_req, res) => {
    await store.pool.query("SELECT event_sequence FROM trips LIMIT 0");
    await store.pool.query("SELECT sequence FROM driver_location LIMIT 0");
    res.json({ ready: true, service: "trip-manager" });
  });
  app.get("/api/passengers", async (_req, res) => {
    res.json((await store.pool.query("SELECT id,name FROM passengers ORDER BY name LIMIT 100")).rows);
  });
  app.post("/internal/requests", async (req, res) => {
    const result = await store.requestTrip(rideRequest(req.body), keyHeader(req.get("Idempotency-Key")));
    res.status(result.replay ? 200 : 201).json(result);
  });
  app.post("/internal/trips/:id/assign", async (req, res) => {
    const ids = idList(object(req.body).ids);
    const positions = await remote<Location[]>(addresses.locations(), "/internal/latest", { actor: "driver", ids });
    res.json(await store.assign(uuid(req.params.id), positions));
  });
  app.post("/internal/drivers", async (req, res) => {
    res.json(await store.drivers(idList(object(req.body).ids)));
  });
  app.get("/internal/entities/:actor/:id", async (req, res) => {
    const id = uuid(req.params.id);
    if (req.params.actor === "driver") {
      const driver = (await store.drivers([id]))[0];
      if (!driver) throw new HttpError(404, "Motorista nao encontrado.");
      res.json(driver);
    } else if (req.params.actor === "rider") {
      const result = await store.pool.query(`SELECT p.id,p.name,coalesce(l.sequence,0)::float8 AS sequence,
        coalesce(l.observed_at,'1970-01-01'::timestamptz) AS "observedAt"
        FROM passengers p LEFT JOIN rider_location l ON l.rider_id=p.id WHERE p.id=$1`, [id]);
      if (!result.rows.length) throw new HttpError(404, "Passageiro nao encontrado.");
      res.json(result.rows[0]);
    } else throw new HttpError(400, "Actor invalido.");
  });
  app.get("/api/demo/drivers", async (_req, res) => { res.json(await store.drivers()); });
  app.get("/api/demo/trips", async (_req, res) => { res.json(await store.activeTrips()); });
  app.get("/api/trips/:id", async (req, res) => { res.json(await store.getTrip(uuid(req.params.id))); });
  app.get("/api/trips/:id/route", async (req, res) => {
    const id = uuid(req.params.id);
    await store.getTrip(id);
    const actor = req.query.actor ?? "driver";
    if (actor !== "driver" && actor !== "rider") throw new HttpError(400, "Actor invalido.");
    const after = req.query.after === undefined ? 0 : Number(req.query.after);
    if (!Number.isSafeInteger(after) || after < 0) throw new HttpError(400, "Cursor de rota invalido.");
    const { rows } = await store.pool.query(`SELECT actor,sequence::float8 AS sequence,lat,lng,
      observed_at AS "observedAt",received_at AS "receivedAt" FROM route_points
      WHERE trip_id=$1 AND actor=$2 AND sequence>$3 ORDER BY sequence LIMIT 500`, [id, actor, after]);
    res.json({ points: rows, nextSequence: rows.at(-1)?.sequence ?? after, persistence: "async-batch" });
  });
  app.post("/api/trips/:id/status", async (req, res) => {
    const body = object(req.body);
    const statuses: Status[] = ["arrived", "in_progress", "completed", "cancelled"];
    const next = statuses.find((s) => s === body.status);
    if (!next) throw new HttpError(400, "Estado solicitado invalido.");
    const id = uuid(req.params.id), version = positiveInteger(body.version);
    const current = await store.getTrip(id);
    let location: Location | undefined;
    if ((next === "arrived" || next === "completed") && current.status !== next && current.driverId) {
      location = (await remote<Location[]>(addresses.locations(), "/internal/latest",
        { actor: "driver", ids: [current.driverId] }))[0];
    }
    res.json(await store.changeTrip(id, next, version, location));
  });
  let streams = 0;
  app.get("/api/trips/:id/events", async (req, res) => {
    const id = uuid(req.params.id);
    let cursor = req.get("Last-Event-ID") ?? "0";
    if (!/^\d{1,19}$/.test(cursor) || BigInt(cursor) > 9_223_372_036_854_775_807n) {
      throw new HttpError(400, "Cursor SSE invalido.");
    }
    await store.getTrip(id);
    const currentCursor = (await store.pool.query<{ value: string }>(
      "SELECT event_sequence::text AS value FROM trips WHERE id=$1", [id])).rows[0]!.value;
    if (BigInt(cursor) > BigInt(currentCursor)) {
      console.warn("sse_cursor_ahead_reset", { id, cursor, currentCursor });
      cursor = "0";
    }
    if (streams >= 200) throw new HttpError(503, "Limite local de streams atingido.");
    let wake = () => {};
    let dirty = true, closed = false;
    let unwatch: (() => void) | undefined;
    streams++;
    res.once("close", () => { closed = true; streams--; unwatch?.(); wake(); });
    const release = await signals.watch(redisKey(`trip:${id}`), () => { dirty = true; wake(); });
    if (closed) { release(); return; }
    unwatch = release;
    res.set({ "Content-Type": "text/event-stream", "Cache-Control": "no-cache, no-transform",
      "X-Accel-Buffering": "no" }).flushHeaders();
    try {
      while (!closed) {
        dirty = false;
        const batch = await store.events(id, cursor);
        for (const item of batch) {
          if (!res.write(`id: ${item.id}\nevent: ${item.type}\ndata: ${JSON.stringify(item.payload)}\n\n`)) {
            res.end(); return;
          }
          cursor = item.id;
        }
        if (batch.length === 100 || dirty) continue;
        if (!res.write(": heartbeat\n\n")) { res.end(); return; }
        await new Promise<void>((resolve) => {
          const timer = setTimeout(resolve, 10_000);
          wake = () => { clearTimeout(timer); resolve(); };
          if (dirty || closed) wake();
        });
      }
    } catch (error) {
      console.error("trip_sse_failed", { id, error });
      res.end();
    }
  });
  return finish(app);
}
