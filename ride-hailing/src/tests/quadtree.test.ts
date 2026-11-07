import { test } from "node:test";
import assert from "node:assert/strict";
import { Quadtree } from "../services/quadtree-map/quadtree.js";
import { distance } from "../shared/domain.js";

test("quadtree subdivide folhas e encontra candidatos em ambos os lados da fronteira", () => {
  const tree = new Quadtree([
    { id: "a", lat: -.001, lng: -.001 }, { id: "b", lat: .001, lng: .001 },
    { id: "far", lat: 20, lng: 20 },
  ], 1);
  assert.ok(tree.leaves > 1);
  assert.deepEqual(tree.within({ lat: 0, lng: 0 }, 1000).map((p) => p.id).sort(), ["a", "b"]);
});
test("quadtree limita profundidade quando pontos coincidem", () => {
  const tree = new Quadtree(Array.from({ length: 100 }, (_, i) => ({ id: String(i), lat: 0, lng: 0 })), 2, 8);
  assert.equal(tree.within({ lat: 0, lng: 0 }, 0).length, 100);
  assert.ok(tree.leaves <= 25);
});
test("consulta considera antimeridiano e polos", () => {
  const tree = new Quadtree([
    { id: "east", lat: 0, lng: 179.99 }, { id: "west", lat: 0, lng: -179.99 },
    { id: "north", lat: 89.99, lng: 120 }, { id: "south", lat: -89.99, lng: -120 },
  ], 1);
  assert.equal(tree.within({ lat: 0, lng: 180 }, 3000).length, 2);
  assert.equal(tree.within({ lat: 90, lng: 0 }, 3000)[0]!.id, "north");
  assert.equal(tree.within({ lat: -90, lng: 0 }, 3000)[0]!.id, "south");
});
test("quadtree confere com busca exaustiva em dataset deterministico", () => {
  let state = 42;
  const random = () => { state = (state * 1664525 + 1013904223) >>> 0; return state / 2 ** 32; };
  const points = Array.from({ length: 5000 }, (_, i) => ({
    id: String(i), lat: -24 + random(), lng: -47 + random(),
  }));
  const tree = new Quadtree(points);
  for (let i = 0; i < 50; i++) {
    const center = { lat: -24 + random(), lng: -47 + random() };
    const radius = random() * 20_000;
    assert.deepEqual(tree.within(center, radius).map((p) => p.id).sort(),
      points.filter((p) => distance(p, center) <= radius).map((p) => p.id).sort());
  }
});
test("snapshot nao e alterado por mutacao do array original", () => {
  const points = [{ id: "a", lat: 0, lng: 0 }];
  const tree = new Quadtree(points);
  points[0]!.lat = 10;
  assert.equal(tree.within({ lat: 0, lng: 0 }, 1).length, 1);
  assert.throws(() => new Quadtree([{ id: "a", lat: 91, lng: 0 }]));
  assert.throws(() => new Quadtree([{ id: "a", lat: 0, lng: 0 }, { id: "a", lat: 1, lng: 1 }]));
});
