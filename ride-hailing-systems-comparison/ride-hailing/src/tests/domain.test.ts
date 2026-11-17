import { test } from "node:test";
import assert from "node:assert/strict";
import { distance, moveTowards, point, positiveInteger, quote, rideRequest, transition, uuid } from "../shared/domain.js";

test("coordenadas finitas e limites geograficos", () => {
  assert.deepEqual(point({ lat: -90, lng: 180 }), { lat: -90, lng: 180 });
  for (const lat of [91, NaN, Infinity, "1", null]) assert.throws(() => point({ lat, lng: 0 }));
  assert.throws(() => point({ lat: 0, lng: -181 }));
});
test("UUID e sequencia rejeitam entradas invalidas", () => {
  assert.equal(uuid("10000000-0000-4000-8000-000000000001"), "10000000-0000-4000-8000-000000000001");
  assert.throws(() => uuid("invalido"));
  for (const value of [0, -1, 1.5, Number.MAX_SAFE_INTEGER + 1, "1"]) assert.throws(() => positiveInteger(value));
});
test("contrato canonico descarta extras e normaliza ordem", () => {
  const a = rideRequest({ passengerId: "10000000-0000-4000-8000-000000000001",
    category: "standard", pickup: { lng: 0, lat: 0 }, destination: { lat: 1, lng: 1 }, extra: true });
  assert.equal(JSON.stringify(a.pickup), '{"lat":0,"lng":0}');
  assert.throws(() => rideRequest({ ...a, category: "flying" }));
});
test("distancia e preco em centavos", () => {
  const p = { lat: 0, lng: 0 }, d = { lat: 0, lng: .01 };
  assert.equal(distance(p, p), 0);
  assert.ok(distance(p, d) > 1111 && distance(p, d) < 1113);
  const request = { passengerId: "", category: "standard" as const, pickup: p, destination: d };
  assert.equal(quote(request), 845);
  assert.ok(quote({ ...request, category: "comfort" }) > quote(request));
});
test("maquina de estados nao ressuscita corridas nem cancela em andamento", () => {
  assert.ok(transition("assigned", "arrived"));
  assert.ok(transition("arrived", "in_progress"));
  assert.ok(transition("in_progress", "completed"));
  assert.ok(transition("searching", "cancelled"));
  assert.ok(!transition("in_progress", "cancelled"));
  assert.ok(!transition("completed", "assigned"));
  assert.ok(!transition("searching", "in_progress"));
});
test("simulacao aproxima sem ultrapassar o destino", () => {
  const p = { lat: -23.56, lng: -46.65 }, target = { lat: -23.561, lng: -46.652 };
  assert.ok(distance(moveTowards(p, target, 40), target) < distance(p, target));
  assert.deepEqual(moveTowards(p, target, 1000), target);
});
