import { distance, moveTowards } from "../shared/domain.js";
import type { Driver, Trip } from "../shared/domain.js";

const base = process.env.API_URL ?? "http://127.0.0.1:58100";
async function call<T>(path: string, body?: unknown): Promise<T> {
  const response = await fetch(`${base}${path}`, {
    ...(body === undefined ? {} : { method: "POST", body: JSON.stringify(body) }),
    headers: { "Content-Type": "application/json" },
    signal: AbortSignal.timeout(5000),
  });
  if (!response.ok) throw new Error(`${path}: ${response.status} ${await response.text()}`);
  return (response.status === 204 ? undefined : await response.json()) as T;
}
console.log(`Simulador GPS conectado a ${base}. Ctrl+C para parar. Um simulador por banco.`);
while (true) {
  try {
    const [drivers, trips] = await Promise.all([
      call<Driver[]>("/api/demo/drivers"), call<Trip[]>("/api/demo/trips"),
    ]);
    for (const driver of drivers) {
      const trip = trips.find((t) => t.driverId === driver.id);
      const target = trip?.status === "in_progress" ? trip.destination : trip?.pickup;
      const position = target ? moveTowards(driver, target, 120) : driver;
      await call(`/api/drivers/${driver.id}/location`, {
        lat: position.lat, lng: position.lng,
        sequence: driver.sequence + 1, observedAt: new Date().toISOString(),
      });
      if (trip) {
        await call(`/api/riders/${trip.passengerId}/location`, {
          ...(trip.status === "in_progress" ? position : trip.pickup),
          sequence: Date.now(), observedAt: new Date().toISOString(),
        });
      }
      if (trip?.status === "assigned" && target && distance(position, target) < 60) {
        await call(`/api/trips/${trip.id}/status`, { status: "arrived", version: trip.version });
      }
      if (trip?.status === "in_progress" && target && distance(position, target) < 60) {
        await call(`/api/trips/${trip.id}/status`, { status: "completed", version: trip.version });
      }
    }
  } catch (error) {
    console.error("simulator_tick_failed", error);
  }
  await new Promise((resolve) => setTimeout(resolve, 4000));
}
