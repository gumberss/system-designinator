import type { Express } from "express";
import { createApp } from "../services/trip-manager/app.js";
import { pool, dispatchBatch, publishEvents } from "../shared/store.js";
import { redis, Signals } from "../shared/redis.js";
import { MapIndex, mapApp } from "../services/quadtree-map/app.js";
import { locationApp } from "../services/location-manager/app.js";
import { finderApp } from "../services/find-driver/app.js";
import { requestApp } from "../services/request-vehicle/app.js";
import { flushLocations, initializeWriter, writerHealth } from "../services/location-manager/writer.js";
import { addresses, remote, serviceApp, finish } from "../shared/http.js";

const role = process.env.SERVICE ?? "trip-manager";
const defaults: Record<string, number> = {
  "trip-manager": 58110, "location-manager": 58120, "quadtree-map": 58130,
  "find-driver": 58140, "request-vehicle": 58150, "location-writer": 58160,
};
if (!(role in defaults)) throw new Error(`SERVICE desconhecido: ${role}`);
const port = Number(process.env.PORT ?? defaults[role]);
if (!Number.isInteger(port) || port < 1 || port > 65535) throw new Error("PORT invalida");
const host = process.env.HOST ?? "127.0.0.1";
let stopping = false;
let wakeShutdown = () => {};
async function delay(ms: number) {
  if (stopping) return;
  await new Promise<void>((resolve) => {
    const timer = setTimeout(resolve, ms);
    wakeShutdown = () => { clearTimeout(timer); resolve(); };
  });
}
await redis.connect();
const signals = new Signals();
await signals.start();
const index = new MapIndex();
if (role === "quadtree-map") await index.rebuild();
if (role === "location-writer") await initializeWriter();
let app: Express;
switch (role) {
  case "trip-manager": app = createApp(signals); break;
  case "location-manager": app = locationApp(signals); break;
  case "quadtree-map": app = mapApp(index); break;
  case "find-driver": app = finderApp(); break;
  case "request-vehicle": app = requestApp(); break;
  default: {
    const writer = serviceApp();
    writer.get("/health/ready", async (_req, res) => {
      res.json({ ready: true, service: role, ...await writerHealth() });
    });
    app = finish(writer);
  }
}
const server = app.listen(port, host, () => { console.log(`${role}: http://${host}:${port}`); });
async function worker() {
  while (!stopping) {
    try {
      if (role === "quadtree-map") await index.rebuild();
      if (role === "location-writer") {
        for (let batch = 0; batch < 8 && !stopping; batch++) if (!await flushLocations()) break;
      }
      if (role === "trip-manager") {
        await publishEvents();
        const pending = await dispatchBatch();
        // No database transaction is held across calls to other services.
        await Promise.all(pending.map(async (trip) => {
          try { await remote(addresses.finder(), `/internal/find/${trip.id}`, {}); }
          catch (error) { console.error("matching_retry_pending", { id: trip.id, error }); }
        }));
      }
    } catch (error) { console.error("worker_failed", { service: role, error }); }
    await delay(role === "quadtree-map" || role === "location-writer" ? 15_000 : 1000);
  }
}
const work = worker();
async function shutdown() {
  if (stopping) return;
  stopping = true;
  wakeShutdown();
  server.close();
  server.closeAllConnections();
  await work;
  await signals.close();
  redis.disconnect();
  await pool.end();
}
process.once("SIGTERM", () => { void shutdown(); });
process.once("SIGINT", () => { void shutdown(); });
