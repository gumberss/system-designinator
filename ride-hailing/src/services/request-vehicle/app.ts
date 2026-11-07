import { serviceApp, finish, addresses, keyHeader, remote } from "../../shared/http.js";
import { rideRequest } from "../../shared/domain.js";
import type { Trip } from "../../shared/domain.js";

export function requestApp() {
  const app = serviceApp();
  app.get("/health/ready", async (_req, res) => {
    await Promise.all([addresses.trips(), addresses.locations(), addresses.map(), addresses.finder()]
      .map((base) => remote(base, "/health/ready")));
    res.json({ ready: true, service: "request-vehicle" });
  });
  app.post("/api/trips", async (req, res) => {
    const body = rideRequest(req.body), key = keyHeader(req.get("Idempotency-Key"));
    const result = await remote<{ trip: Trip; replay: boolean }>(
      addresses.trips(), "/internal/requests", body, key);
    if (result.trip.status === "searching") {
      // Persistence precedes matching. Upstream failure is 503, not a second request.
      result.trip = await remote<Trip>(addresses.finder(), `/internal/find/${result.trip.id}`, {});
    }
    res.status(result.replay ? 200 : 201).json(result);
  });
  return finish(app);
}
