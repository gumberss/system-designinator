export type Category = "standard" | "comfort";
export type Status = "searching" | "assigned" | "arrived" | "in_progress" |
  "completed" | "cancelled" | "expired";
export interface Point { lat: number; lng: number }
export interface RideRequest {
  passengerId: string;
  category: Category;
  pickup: Point;
  destination: Point;
}
export interface Trip extends RideRequest {
  id: string;
  driverId: string | null;
  status: Status;
  version: number;
  fareCents: number;
  matchDistanceM: number | null;
}
export interface Driver extends Point {
  id: string;
  name: string;
  category: Category;
  sequence: number;
  busy: boolean;
}
export class HttpError extends Error {
  readonly status: number;
  constructor(status: number, message: string) {
    super(message);
    this.status = status;
  }
}
export function object(value: unknown): Record<string, unknown> {
  if (!value || typeof value !== "object" || Array.isArray(value)) {
    throw new HttpError(400, "Objeto JSON esperado.");
  }
  return value as Record<string, unknown>;
}
export function uuid(value: unknown): string {
  if (typeof value !== "string" ||
      !/^[0-9a-f]{8}-[0-9a-f]{4}-[1-8][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i.test(value)) {
    throw new HttpError(400, "UUID invalido.");
  }
  return value.toLowerCase();
}
export function idList(value: unknown): string[] {
  if (!Array.isArray(value) || value.length > 2000) throw new HttpError(400, "Lista de ate 2000 IDs esperada.");
  return [...new Set(value.map(uuid))];
}
export function point(value: unknown): Point {
  const p = object(value);
  if (typeof p.lat !== "number" || !Number.isFinite(p.lat) || Math.abs(p.lat) > 90 ||
      typeof p.lng !== "number" || !Number.isFinite(p.lng) || Math.abs(p.lng) > 180) {
    throw new HttpError(400, "Latitude/longitude invalidas.");
  }
  return { lat: p.lat, lng: p.lng };
}
export function category(value: unknown): Category {
  if (value !== "standard" && value !== "comfort") {
    throw new HttpError(400, "Categoria deve ser standard ou comfort.");
  }
  return value;
}
export function rideRequest(value: unknown): RideRequest {
  const v = object(value);
  return {
    passengerId: uuid(v.passengerId),
    category: category(v.category),
    pickup: point(v.pickup),
    destination: point(v.destination),
  };
}
export function positiveInteger(value: unknown): number {
  if (typeof value !== "number" || !Number.isSafeInteger(value) || value < 1) {
    throw new HttpError(400, "Inteiro positivo esperado.");
  }
  return value;
}
export function distance(a: Point, b: Point): number {
  const rad = Math.PI / 180;
  const h = Math.sin((b.lat - a.lat) * rad / 2) ** 2 +
    Math.cos(a.lat * rad) * Math.cos(b.lat * rad) *
    Math.sin((b.lng - a.lng) * rad / 2) ** 2;
  return 6_371_000 * 2 * Math.asin(Math.sqrt(Math.min(1, h)));
}
export function quote(request: RideRequest): number {
  return 600 + Math.round(distance(request.pickup, request.destination) *
    (request.category === "comfort" ? 0.32 : 0.22));
}
export function transition(from: Status, to: Status): boolean {
  return (to === "cancelled" && ["searching", "assigned", "arrived"].includes(from)) ||
    (from === "assigned" && to === "arrived") ||
    (from === "arrived" && to === "in_progress") ||
    (from === "in_progress" && to === "completed");
}
export function moveTowards(from: Point, to: Point, meters: number): Point {
  const ratio = Math.min(1, meters / Math.max(distance(from, to), 0.001));
  return { lat: from.lat + (to.lat - from.lat) * ratio, lng: from.lng + (to.lng - from.lng) * ratio };
}
