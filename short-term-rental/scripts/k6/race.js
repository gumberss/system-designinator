import http from 'k6/http';
import { check } from 'k6';
import { Counter } from 'k6/metrics';
import { available, cancel, integer, json, params, properties, startDate, dateAfter, urlFor, uuid } from './common.js';

const contenders = integer('CONTENDERS', 32, 2, 128);
const start = startDate();
const winners = new Counter('race_winners');
const conflicts = new Counter('race_conflicts');
http.setResponseCallback(http.expectedStatuses(200, 201, 409));

export const options = {
    vus: 1,
    iterations: 1,
    batch: contenders,
    batchPerHost: contenders,
    thresholds: {
        checks: ['rate==1'],
        http_req_failed: ['rate==0'],
        race_winners: ['count==1'],
        race_conflicts: [`count==${contenders - 1}`],
    },
};

export function setup() {
    const property = properties()[0];
    if (!available(property.id, start, dateAfter(start, 2))) {
        throw new Error('Janela de disputa ocupada; escolha outra START_DATE.');
    }
    return property;
}

export default function (property) {
    const requests = Array.from({ length: contenders }, (_, index) => ({
        url: urlFor(index),
        key: uuid(),
        body: {
            propertyId: property.id, guestId: uuid(),
            checkIn: start, checkOut: dateAfter(start, 2),
        },
    }));
    const responses = http.batch(requests.map((request) => ({
        method: 'POST', url: `${request.url}/bookings`,
        body: JSON.stringify(request.body), params: params(request.key, 'race create'),
    })));
    const created = [];
    winners.add(0);
    conflicts.add(0);
    try {
        responses.forEach((response, index) => {
            check(response, { 'disputa: somente 201 ou 409': (r) => r.status === 201 || r.status === 409 });
            if (response.status === 409) conflicts.add(1);
            if (response.status === 201) {
                const booking = response.json();
                created.push({ booking, request: requests[index] });
                winners.add(1);
            }
        });
        if (!check(created, { 'disputa: exatamente um vencedor': (items) => items.length === 1 })) {
            throw new Error(`Esperado um vencedor; encontrados ${created.length}.`);
        }
        const winner = created[0];
        const replayResponse = http.post(`${winner.request.url}/bookings`,
            JSON.stringify(winner.request.body), params(winner.request.key, 'race replay'));
        const replay = json(replayResponse, 200, 'replay do vencedor');
        check(replay, {
            'replay: mesmo ID e sem ressurreicao': (b) => b.id === winner.booking.id && b.status === 'confirmed',
        });
        check(replayResponse, {
            'replay: header identificado': (r) => r.headers['Idempotency-Replayed'] === 'true',
        });
        check(available(property.id, start, dateAfter(start, 2)),
            { 'disputa: intervalo ocupado depois do commit': (value) => value === false });
    } finally {
        const failures = [];
        for (const winner of created) {
            try { cancel(winner.booking.id); }
            catch (error) { failures.push(String(error)); }
        }
        if (failures.length) throw new Error(`Falha no cleanup: ${failures.join('; ')}`);
    }
    check(available(property.id, start, dateAfter(start, 2)),
        { 'cleanup: intervalo liberado': (value) => value === true });
}
