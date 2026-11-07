import { distance } from "../../shared/domain.js";
import type { Point } from "../../shared/domain.js";

export interface SpatialPoint extends Point { id: string }
interface Box { west: number; east: number; south: number; north: number }
interface Node { box: Box; points: SpatialPoint[]; children?: Node[] }

export class Quadtree {
  private readonly root: Node;
  readonly size: number;
  readonly leaves: number;
  constructor(points: SpatialPoint[], capacity = 32, maxDepth = 16) {
    if (!Number.isInteger(capacity) || capacity < 1 || !Number.isInteger(maxDepth) || maxDepth < 0) {
      throw new Error("Capacidade/profundidade invalidas.");
    }
    const ids = new Set<string>();
    for (const p of points) {
      if (!Number.isFinite(p.lat) || Math.abs(p.lat) > 90 || !Number.isFinite(p.lng) ||
          Math.abs(p.lng) > 180 || ids.has(p.id)) throw new Error("Ponto invalido ou ID duplicado.");
      ids.add(p.id);
    }
    let leaves = 0;
    const build = (box: Box, items: SpatialPoint[], depth: number): Node => {
      if (items.length <= capacity || depth === maxDepth) {
        leaves++;
        return { box, points: items };
      }
      const midLng = (box.west + box.east) / 2, midLat = (box.south + box.north) / 2;
      const buckets: SpatialPoint[][] = [[], [], [], []];
      for (const p of items) buckets[(p.lat >= midLat ? 2 : 0) + (p.lng >= midLng ? 1 : 0)]!.push(p);
      return { box, points: [], children: buckets.map((bucket, i) => build({
        west: i % 2 ? midLng : box.west, east: i % 2 ? box.east : midLng,
        south: i >= 2 ? midLat : box.south, north: i >= 2 ? box.north : midLat,
      }, bucket, depth + 1)) };
    };
    this.root = build({ west: -180, east: 180, south: -90, north: 90 }, points.map((p) => ({ ...p })), 0);
    this.size = points.length;
    this.leaves = leaves;
  }
  within(center: Point, radiusM: number): SpatialPoint[] {
    if (!Number.isFinite(radiusM) || radiusM < 0) throw new Error("Raio invalido.");
    const angular = radiusM / 6_371_000;
    const latRad = center.lat * Math.PI / 180;
    const deltaLat = angular * 180 / Math.PI;
    const south = Math.max(-90, center.lat - deltaLat), north = Math.min(90, center.lat + deltaLat);
    const deltaLng = south === -90 || north === 90 || angular >= Math.PI
      ? 180 : Math.asin(Math.min(1, Math.sin(angular) / Math.cos(latRad))) * 180 / Math.PI;
    const west = center.lng - deltaLng, east = center.lng + deltaLng;
    const ranges = deltaLng >= 180 ? [[-180, 180]] :
      west < -180 ? [[west + 360, 180], [-180, east]] :
      east > 180 ? [[west, 180], [-180, east - 360]] : [[west, east]];
    const result: SpatialPoint[] = [];
    const visit = (node: Node) => {
      if (node.box.north < south || node.box.south > north ||
          !ranges.some(([w, e]) => node.box.east >= w! && node.box.west <= e!)) return;
      if (node.children) { for (const child of node.children) visit(child); }
      else for (const p of node.points) if (distance(center, p) <= radiusM) result.push(p);
    };
    visit(this.root);
    return result;
  }
}
