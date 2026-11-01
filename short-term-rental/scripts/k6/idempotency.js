import http from 'k6/http';
import { check } from 'k6';
import exec from 'k6/execution';
import { cancel, dateAfter, integer, json, params, properties, startDate, available, urlFor, uuid } from './common.js';

const start = startDate();
const vus = integer('VUS', 16, 1, 128);
const iterations = integer('ITERATIONS', 64, vus, 10000);
export const options = {
    scenarios: {
        replays: {
            executor: 'shared-iterations',
            vus,
            iterations,
            maxDuration: '2m',
        },
    },
    thresholds: { checks: ['rate==1'], http_req_failed: ['rate==0'] },
};

export function setup() {
    const property = properties()[0];
    const body = {
        propertyId: property.id, guestId: uuid(),
        checkIn: start, checkOut: dateAfter(start, 2),
    };
    if (!available(property.id, body.checkIn, body.checkOut)) {
        throw new Error('Janela de idempotencia ocupada; escolha outra START_DATE.');
    }
    const key = uuid();
    const booking = json(http.post(`${urlFor(0)}/bookings`,
        JSON.stringify(body), params(key, 'fixture create')), 201, 'criacao inicial');
    return { body, key, id: booking.id };
}

export default function (fixture) {
    const response = http.post(`${urlFor(exec.scenario.iterationInTest)}/bookings`,
        JSON.stringify(fixture.body), params(fixture.key, 'idempotency replay'));
    check(response, {
        'replay: HTTP 200, mesmo ID e confirmed': (r) => r.status === 200 &&
            r.json().id === fixture.id && r.json().status === 'confirmed',
        'replay: header identificado': (r) => r.headers['Idempotency-Replayed'] === 'true',
    });
}

export function teardown(fixture) {
    try {
        http.setResponseCallback(http.expectedStatuses(200, 409));
        const changed = http.post(`${urlFor(0)}/bookings`,
            JSON.stringify({ ...fixture.body, guestId: uuid() }), params(fixture.key, 'payload mismatch'));
        check(changed, { 'mesma chave com outro payload: HTTP 409': (r) => r.status === 409 });
        http.setResponseCallback(http.expectedStatuses(200));
    } finally {
        cancel(fixture.id);
    }
    const replay = json(http.post(`${urlFor(0)}/bookings`,
        JSON.stringify(fixture.body), params(fixture.key, 'cancelled replay')), 200, 'replay cancelado');
    check(replay, { 'replay: nao ressuscita reserva cancelada': (b) => b.id === fixture.id && b.status === 'cancelled' });
}
