import http from 'k6/http';
import exec from 'k6/execution';
import { check } from 'k6';
import { Counter, Rate } from 'k6/metrics';
import { dateAfter, integer, params, properties, startDate, urlFor, uuid } from './common.js';

const readPercent = integer('READ_PERCENT', 100, 0, 100);
const readMode = __ENV.READ_MODE || 'catalog';
if (!['catalog', 'availability'].includes(readMode)) {
    throw new Error('READ_MODE deve ser catalog ou availability.');
}
const start = readPercent < 100 || readMode === 'availability' ? startDate() : null;
const confirmations = new Counter('booking_confirmations');
const readSuccesses = new Counter('read_successes');
const unexpected = new Rate('unexpected_response');

export const options = {
    vus: integer('VUS', 10, 1, 256),
    duration: __ENV.DURATION || '20s',
    thresholds: {
        checks: ['rate==1'],
        unexpected_response: ['rate==0'],
        http_req_failed: ['rate==0'],
    },
};

export function setup() {
    return properties();
}

export default function (items) {
    const iteration = exec.scenario.iterationInTest;
    const property = items[iteration % items.length];
    const url = urlFor(iteration);
    if (Math.random() * 100 < readPercent) {
        const path = readMode === 'catalog'
            ? `/properties/${property.id}`
            : `/properties/${property.id}/availability?checkIn=${start}&checkOut=${dateAfter(start, 2)}`;
        const response = http.get(`${url}${path}`, { tags: { name: `read ${readMode}` } });
        const ok = check(response, {
            'leitura: HTTP 200 e imovel correto': (r) => r.status === 200 &&
                (readMode === 'catalog' ? r.json().id : r.json().propertyId) === property.id,
        });
        unexpected.add(!ok);
        if (ok) readSuccesses.add(1);
        return;
    }

    const checkIn = dateAfter(start, Math.floor(iteration / items.length) * 2);
    const request = {
        propertyId: property.id,
        guestId: uuid(),
        checkIn,
        checkOut: dateAfter(checkIn, 2),
    };
    const response = http.post(`${url}/bookings`, JSON.stringify(request), params(uuid(), 'booking create'));
    const ok = check(response, {
        'reserva: HTTP 201, ID e status confirmed': (r) => r.status === 201 &&
            !!r.json().id && r.json().propertyId === property.id && r.json().status === 'confirmed',
    });
    unexpected.add(!ok);
    if (ok) confirmations.add(1);
}
