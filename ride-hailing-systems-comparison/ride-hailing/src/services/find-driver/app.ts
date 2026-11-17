import { serviceApp, finish, addresses, remote } from "../../shared/http.js";
import { uuid } from "../../shared/domain.js";
import type { Driver, Trip } from "../../shared/domain.js";

export async function findDriver(id: string): Promise<Trip> {
  const trip = await remote<Trip>(addresses.trips(), `/api/trips/${id}`);
  if (trip.status !== "searching") return trip;
  const positions = await remote<Driver[]>(addresses.locations(),
    `/api/drivers/nearby?lat=${trip.pickup.lat}&lng=${trip.pickup.lng}&category=${trip.category}`);
  return remote<Trip>(addresses.trips(), `/internal/trips/${id}/assign`, { ids: positions.map((p) => p.id) });
}
export function finderApp() {
  const app = serviceApp();
  app.get("/health/ready", (_req, res) => { res.json({ ready: true, service: "find-driver" }); });
  app.post("/internal/find/:id", async (req, res) => { res.json(await findDriver(uuid(req.params.id))); });
  return finish(app);
}
