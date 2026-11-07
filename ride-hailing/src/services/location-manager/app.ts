import { serviceApp, finish, addresses, remote } from "../../shared/http.js";
import { category, distance, HttpError, idList, object, point, uuid } from "../../shared/domain.js";
import { acceptLocation, fresh, latest, locationInput, parseLocation, redis, redisKey } from "../../shared/redis.js";
import type { Actor, Location, Signals } from "../../shared/redis.js";
import type { Driver } from "../../shared/domain.js";
import type { DriverRecord } from "../../shared/store.js";

export interface NearbyResult {
  drivers: (Driver & Location & { distanceM: number })[];
  indexAgeMs: number;
  indexed: number;
  deltaCount: number;
}
export async function nearby(p: { lat: number; lng: number }, kind: string): Promise<NearbyResult> {
  const snapshot = await remote<{ ids: string[]; builtAt: number; ageMs: number; points: number }>(
    addresses.map(), `/internal/candidates?lat=${p.lat}&lng=${p.lng}`);
  // The delta closes boundary/new-driver gaps left by a 15-second spatial snapshot.
  const changed = await redis.zrangebyscore(redisKey("driver-changes"), snapshot.builtAt, "+inf", "LIMIT", 0, 2001);
  if (changed.length > 2000) throw new HttpError(503, "Delta espacial excedeu limite local; aguarde novo indice.");
  const ids = [...new Set([...snapshot.ids, ...changed])];
  if (ids.length > 2000) throw new HttpError(503, "Limite local de candidatos atingido.");
  const positions = (await latest("driver", ids)).filter((v) => fresh(v) && distance(v, p) <= 5000);
  const metadata = await remote<DriverRecord[]>(addresses.trips(), "/internal/drivers",
    { ids: positions.map((v) => v.id) });
  const available = new Map(metadata.filter((d) => d.online && !d.busy && d.category === kind).map((d) => [d.id, d]));
  return { drivers: positions.filter((v) => available.has(v.id)).map((v) => ({
    ...available.get(v.id)!, ...v, distanceM: distance(v, p),
  })).sort((a, b) => a.distanceM - b.distanceM || a.id.localeCompare(b.id)).slice(0, 20),
  indexAgeMs: snapshot.ageMs, indexed: snapshot.points, deltaCount: changed.length };
}
function actor(value: unknown): Actor {
  if (value !== "driver" && value !== "rider") throw new HttpError(400, "Actor invalido.");
  return value;
}
export function locationApp(signals: Signals) {
  const app = serviceApp();
  app.get("/health/ready", async (_req, res) => {
    await redis.ping();
    res.json({ ready: true, service: "location-manager" });
  });
  app.get("/api/demo/drivers", async (_req, res) => {
    const records = await remote<DriverRecord[]>(addresses.trips(), "/api/demo/drivers");
    const current = new Map((await latest("driver", records.map((d) => d.id))).map((p) => [p.id, p]));
    res.json(records.map((d) => ({ ...d, ...current.get(d.id) })));
  });
  app.get("/api/drivers/nearby", async (req, res) => {
    if (req.query.lat === undefined || req.query.lng === undefined) throw new HttpError(400, "Coordenadas obrigatorias.");
    const result = await nearby(point({ lat: Number(req.query.lat), lng: Number(req.query.lng) }), category(req.query.category));
    res.set("X-Map-Age-Ms", String(result.indexAgeMs)).set("X-Map-Delta", String(result.deltaCount)).json(result.drivers);
  });
  app.post("/internal/latest", async (req, res) => {
    const value = object(req.body);
    res.json(await latest(actor(value.actor), idList(value.ids)));
  });
  for (const [plural, kind] of [["drivers", "driver"], ["riders", "rider"]] as const) {
    app.post(`/api/${plural}/:id/location`, async (req, res) => {
      const id = uuid(req.params.id), value = locationInput(req.body);
      let baseline = (await latest(kind, [id]))[0];
      if (!baseline) {
        const entity = await remote<{ sequence: number; observedAt: string }>(
          addresses.trips(), `/internal/entities/${kind}/${id}`);
        baseline = { ...value, id, actor: kind, receivedAt: 0,
          sequence: entity.sequence, observedAt: entity.observedAt };
      }
      const accepted = await acceptLocation(kind, id, value, baseline);
      if (kind === "driver") {
        // Also written atomically in the ingestion script (see redis.ts).
        res.set("X-GPS-Sequence", String(accepted.sequence));
      }
      res.status(204).end();
    });
    app.get(`/api/${plural}/:id/location`, async (req, res) => {
      const location = (await latest(kind, [uuid(req.params.id)]))[0];
      if (!location) throw new HttpError(404, "Sem GPS recebido neste cache.");
      res.json({ ...location, fresh: fresh(location) });
    });
    let streams = 0;
    app.get(`/api/${plural}/:id/events`, async (req, res) => {
      const id = uuid(req.params.id);
      await remote(addresses.trips(), `/internal/entities/${kind}/${id}`);
      if (streams >= 200) throw new HttpError(503, "Limite local de streams GPS atingido.");
      let closed = false, wake = () => {};
      let pending: Location | undefined;
      let unwatch: (() => void) | undefined;
      streams++;
      res.once("close", () => { closed = true; streams--; unwatch?.(); wake(); });
      const release = await signals.watch(redisKey(`location:${kind}:${id}`), (raw) => {
        pending = parseLocation(raw);
        wake();
      });
      if (closed) { release(); return; }
      unwatch = release;
      res.set({ "Content-Type": "text/event-stream", "Cache-Control": "no-cache, no-transform",
        "X-Accel-Buffering": "no" }).flushHeaders();
      let sequence = 0;
      try {
        pending = (await latest(kind, [id]))[0];
        while (!closed) {
          const value = pending;
          pending = undefined;
          if (value && value.sequence > sequence) {
            sequence = value.sequence;
            if (!res.write(`event: location\ndata: ${JSON.stringify(value)}\n\n`)) { res.end(); return; }
          } else if (!res.write(": heartbeat\n\n")) { res.end(); return; }
          await new Promise<void>((resolve) => {
            const timer = setTimeout(resolve, 10_000);
            wake = () => { clearTimeout(timer); resolve(); };
            if (pending || closed) wake();
          });
          // Pub/sub is only a wake-up; reconcile latest on reconnect/missed notifications.
          if (!pending && !closed) pending = (await latest(kind, [id]))[0];
        }
      } catch (error) {
        console.error("location_sse_failed", { id, error });
        res.end();
      }
    });
  }
  return finish(app);
}
