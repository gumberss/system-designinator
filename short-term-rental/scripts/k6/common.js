import http from 'k6/http';
import { check } from 'k6';

export const baseUrl = origin(__ENV.BASE_URL || 'http://localhost:58080');
const secondUrl = __ENV.SECOND_URL ? origin(__ENV.SECOND_URL) : null;

function origin(value) {
    if (!/^https?:\/\/[^/?#]+\/?$/.test(value)) {
        throw new Error('BASE_URL/SECOND_URL devem ser origens HTTP(S), sem caminho ou query.');
    }
    return value.replace(/\/$/, '');
}

export function integer(name, fallback, min, max) {
    const text = __ENV[name] === undefined ? String(fallback) : __ENV[name];
    const value = Number(text);
    if (!/^\d+$/.test(text) || !Number.isSafeInteger(value) || value < min || value > max) {
        throw new Error(`${name} deve ser um inteiro entre ${min} e ${max}.`);
    }
    return value;
}

export function urlFor(index) {
    return secondUrl && index % 2 === 1 ? secondUrl : baseUrl;
}

export function uuid() {
    return 'xxxxxxxx-xxxx-4xxx-yxxx-xxxxxxxxxxxx'.replace(/[xy]/g, (letter) => {
        const value = Math.floor(Math.random() * 16);
        return (letter === 'x' ? value : (value & 3) | 8).toString(16);
    });
}

export function dateAfter(start, days) {
    const date = new Date(`${start}T00:00:00Z`);
    date.setUTCDate(date.getUTCDate() + days);
    if (!Number.isFinite(date.getTime()) || date.getUTCFullYear() > 9999) {
        throw new Error('Janela de carga excedeu as datas suportadas.');
    }
    return date.toISOString().slice(0, 10);
}

export function startDate() {
    const value = __ENV.START_DATE;
    if (!value || !/^\d{4}-\d{2}-\d{2}$/.test(value) || value.startsWith('0000') ||
        dateAfter(value, 0) !== value) {
        throw new Error('Informe START_DATE=yyyy-MM-dd com uma janela livre para o teste.');
    }
    return value;
}

export function json(response, expected, operation) {
    if (!check(response, { [`${operation}: HTTP ${expected}`]: (r) => r.status === expected })) {
        throw new Error(`${operation}: HTTP ${response.status}; ${response.body}`);
    }
    return response.json();
}

export function properties() {
    json(http.get(`${baseUrl}/health/ready`, { tags: { name: 'setup readiness' } }), 200, 'readiness');
    const items = json(http.get(`${baseUrl}/properties?limit=1000`,
        { tags: { name: 'setup properties' } }), 200, 'catalogo');
    if (!Array.isArray(items) || items.length === 0 || items.some((item) => !item.id)) {
        throw new Error('Catalogo vazio ou invalido.');
    }
    return items;
}

export function params(key, name) {
    return {
        headers: { 'Content-Type': 'application/json', 'Idempotency-Key': key },
        tags: { name },
        timeout: '30s',
    };
}

export function available(propertyId, start, end) {
    const result = json(http.get(
        `${baseUrl}/properties/${propertyId}/availability?checkIn=${start}&checkOut=${end}`,
        { tags: { name: 'fixture availability' } }), 200, 'disponibilidade');
    return result.available;
}

export function cancel(id) {
    const booking = json(http.post(`${baseUrl}/bookings/${id}/cancel`, null,
        { tags: { name: 'cleanup cancel' } }), 200, 'cancelamento');
    if (!check(booking, { 'cleanup: reserva cancelada': (b) => b.id === id && b.status === 'cancelled' })) {
        throw new Error(`Cancelamento nao confirmado para ${id}.`);
    }
}
