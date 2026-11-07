import { serviceApp, finish } from "../../shared/http.js";
import { HttpError, point } from "../../shared/domain.js";
import { allLocations, fresh, redis } from "../../shared/redis.js";
import { Quadtree } from "./quadtree.js";

export class MapIndex {
  private tree = new Quadtree([]);
  private builtAt = 0;
  async rebuild() {
    const clock = await redis.time();
    const started = Number(clock[0]) * 1000 + Math.floor(Number(clock[1]) / 1000);
    const positions = (await allLocations("driver")).filter((p) => fresh(p, started));
    const next = new Quadtree(positions);
    this.tree = next;
    this.builtAt = started;
  }
  stats() {
    return { builtAt: this.builtAt, ageMs: Date.now() - this.builtAt, points: this.tree.size,
      leaves: this.tree.leaves, refreshMs: 15_000 };
  }
  query(center: { lat: number; lng: number }) {
    if (!this.builtAt || Date.now() - this.builtAt > 30_000) throw new HttpError(503, "Quadtree desatualizada.");
    const candidates = this.tree.within(center, 5000);
    if (candidates.length > 2000) throw new HttpError(503, "Celula densa excede limite local de candidatos.");
    return { ids: candidates.map((p) => p.id), ...this.stats() };
  }
}
export function mapApp(index: MapIndex) {
  const app = serviceApp();
  app.get("/health/ready", (_req, res) => {
    if (index.stats().ageMs > 30_000) throw new HttpError(503, "Indice ainda nao pronto.");
    res.json({ ready: true, ...index.stats() });
  });
  app.get("/internal/candidates", (req, res) => {
    if (req.query.lat === undefined || req.query.lng === undefined) throw new HttpError(400, "Coordenadas obrigatorias.");
    res.json(index.query(point({ lat: Number(req.query.lat), lng: Number(req.query.lng) })));
  });
  app.get("/api/system/map", (_req, res) => { res.json(index.stats()); });
  return finish(app);
}
