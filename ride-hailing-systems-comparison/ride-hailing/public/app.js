const $ = (id) => document.getElementById(id);
let trip = null, selected = null, source = null, candidates = [], pending = null;
let driverSource = null, riderSource = null, rider = null, driverTracking = null;
const terminal = new Set(["completed", "cancelled", "expired"]);
async function api(path, body, key) {
  const response = await fetch(`/api${path}`, {
    ...(body === undefined ? {} : { method: "POST", body: JSON.stringify(body) }),
    headers: { "Content-Type": "application/json", ...(key ? { "Idempotency-Key": key } : {}) },
    signal: AbortSignal.timeout(6000),
  });
  const result = await response.json();
  if (!response.ok) throw new Error(`${response.status}: ${result.error}`);
  return result;
}
function pickup() { return { lat: Number($("pickupLat").value), lng: Number($("pickupLng").value) }; }
function destination() { return { lat: Number($("destinationLat").value), lng: Number($("destinationLng").value) }; }
function showTrip(value) {
  if (trip && trip.id === value.id && trip.version > value.version) return;
  trip = value;
  localStorage.setItem("ride-hailing-trip", trip.id);
  $("trip").textContent = JSON.stringify({
    corrida: trip.id, estado: trip.status, versao: trip.version,
    motoristaEscolhido: selected?.id === trip.driverId ? selected.name : trip.driverId,
    distanciaNoMatchingMetros: trip.matchDistanceM === null ? null : Math.round(trip.matchDistanceM),
    precoFixoSimuladoBRL: (trip.fareCents / 100).toFixed(2),
    criterio: "Categoria + GPS recente + raio 5km + menor distancia disponivel",
  }, null, 2);
  $("start").disabled = trip.status !== "arrived";
  $("cancel").disabled = !["searching", "assigned", "arrived"].includes(trip.status);
  $("request").disabled = terminal.has(trip.status);
  if (terminal.has(trip.status)) {
    driverSource?.close(); riderSource?.close();
    driverSource = null; riderSource = null; driverTracking = null;
  } else if (trip.driverId && driverTracking !== trip.driverId) {
    void action(() => trackPositions(trip))();
  }
  draw();
}
function log(text) {
  const li = document.createElement("li");
  li.textContent = `${new Date().toLocaleTimeString()} ${text}`;
  $("events").prepend(li);
  while ($("events").children.length > 50) $("events").lastChild.remove();
}
async function track(value) {
  source?.close();
  driverSource?.close(); riderSource?.close(); driverTracking = null;
  selected = null;
  trip = null;
  $("passenger").value = value.passengerId;
  $("category").value = value.category;
  $("pickupLat").value = value.pickup.lat;
  $("pickupLng").value = value.pickup.lng;
  $("destinationLat").value = value.destination.lat;
  $("destinationLng").value = value.destination.lng;
  showTrip(value);
  source = new EventSource(`/api/trips/${value.id}/events`);
  source.onopen = () => { $("connection").textContent = "SSE conectado"; };
  source.onerror = () => { $("connection").textContent = "SSE desconectado; reconectando com cursor..."; };
  source.addEventListener("trip", (event) => {
    const snapshot = JSON.parse(event.data);
    showTrip(snapshot);
    log(`#${event.lastEventId} estado ${snapshot.status}, v${snapshot.version}`);
  });
}
async function trackPositions(value) {
  driverTracking = value.driverId;
  driverSource?.close(); riderSource?.close();
  const identity = value.id;
  const drivers = await api("/demo/drivers");
  if (trip?.id !== identity || terminal.has(trip.status)) return;
  selected = drivers.find((d) => d.id === value.driverId) ?? null;
  driverSource = new EventSource(`/api/drivers/${value.driverId}/events`);
  driverSource.onerror = () => { $("connection").textContent = "GPS desconectado; ressincronizando ultimo ponto..."; };
  driverSource.onopen = () => { $("connection").textContent = "Estado + GPS conectados"; };
  driverSource.addEventListener("location", (event) => {
    const location = JSON.parse(event.data);
    if (trip?.id !== identity || location.id !== trip.driverId || terminal.has(trip.status)) return;
    if (selected?.id === location.id && selected.sequence > location.sequence) return;
    selected = { ...location, name: selected?.name ?? location.id };
    showTrip(trip);
    log(`GPS motorista seq=${location.sequence} (Redis -> SSE)`);
  });
  riderSource = new EventSource(`/api/riders/${value.passengerId}/events`);
  riderSource.addEventListener("location", (event) => {
    rider = JSON.parse(event.data);
    draw();
  });
  showTrip(trip);
}
function draw() {
  const group = $("markers");
  group.replaceChildren();
  const p = trip?.pickup ?? pickup(), d = trip?.destination ?? destination();
  const markers = [
    ...candidates.map((driver) => ({ ...driver, label: driver.name, color: "#237bc1" })),
    { ...p, label: "Embarque", color: "#0c8b68" },
    { ...d, label: "Destino", color: "#8954c0" },
    ...(rider ? [{ ...rider, label: "Passageiro", color: "#00a4a8" }] : []),
    ...(selected && !terminal.has(trip?.status) ? [{ ...selected, label: "Escolhido", color: "#d97913" }] : []),
  ].filter((m) => Number.isFinite(m.lat) && Number.isFinite(m.lng));
  if (!markers.length) return;
  const minLat = Math.min(...markers.map((m) => m.lat)) - .002;
  const maxLat = Math.max(...markers.map((m) => m.lat)) + .002;
  const minLng = Math.min(...markers.map((m) => m.lng)) - .002;
  const maxLng = Math.max(...markers.map((m) => m.lng)) + .002;
  const x = (m) => 50 + (m.lng - minLng) / (maxLng - minLng) * 630;
  const y = (m) => 420 - (m.lat - minLat) / (maxLat - minLat) * 370;
  function svg(tag, attributes) {
    const node = document.createElementNS("http://www.w3.org/2000/svg", tag);
    for (const [key, value] of Object.entries(attributes)) node.setAttribute(key, String(value));
    group.append(node);
    return node;
  }
  if (selected && trip && !terminal.has(trip.status)) {
    const target = trip.status === "in_progress" ? d : p;
    svg("line", { x1: x(selected), y1: y(selected), x2: x(target), y2: y(target),
      stroke: "#d97913", "stroke-width": 2, "stroke-dasharray": "6 6" });
  }
  for (const m of markers) {
    svg("circle", { cx: x(m), cy: y(m), r: 9, fill: m.color, stroke: "white", "stroke-width": 3 });
    svg("text", { x: x(m) + 12, y: y(m) - 12, fill: "#172a3a", "font-size": 13 }).textContent = m.label;
  }
}
function action(fn) {
  return async () => {
    $("error").textContent = "";
    try { await fn(); }
    catch (error) { $("error").textContent = error.message; }
  };
}
$("request").onclick = action(async () => {
  const body = { passengerId: $("passenger").value, category: $("category").value, pickup: pickup(), destination: destination() };
  const fingerprint = JSON.stringify(body);
  if (!pending || pending.fingerprint !== fingerprint) pending = { fingerprint, key: crypto.randomUUID() };
  const result = await api("/trips", body, pending.key);
  await track(result.trip);
  log(result.replay ? "Retry idempotente: mesma corrida." : "Pedido persistido.");
});
for (const [id, status] of [["start", "in_progress"], ["cancel", "cancelled"]]) {
  $(id).onclick = action(async () => { showTrip(await api(`/trips/${trip.id}/status`, { status, version: trip.version })); });
}
$("new").onclick = action(async () => {
  if (trip && !terminal.has(trip.status)) throw new Error("Conclua ou cancele a corrida antes de preparar outra.");
  source?.close(); driverSource?.close(); riderSource?.close();
  source = null; driverSource = null; riderSource = null; driverTracking = null;
  trip = null; selected = null; pending = null; rider = null;
  localStorage.removeItem("ride-hailing-trip");
  $("trip").textContent = "Nenhuma corrida."; $("request").disabled = false;
  $("connection").textContent = "Sem corrida selecionada";
  draw();
});
$("route").onclick = action(async () => {
  if (!trip) throw new Error("Selecione uma corrida para consultar a rota.");
  const [driver, passenger] = await Promise.all([
    api(`/trips/${trip.id}/route?actor=driver`), api(`/trips/${trip.id}/route?actor=rider`),
  ]);
  $("routeResult").textContent = JSON.stringify({
    pontosMotorista: driver.points.length, pontosPassageiro: passenger.points.length,
    ultimoPonto: driver.points.at(-1), cursorMotorista: driver.nextSequence,
    observacao: "Pagina de ate 500 pontos; persistencia assincrona em lotes de ~15s.",
  }, null, 2);
});
async function refresh() {
  try {
    const p = pickup();
    candidates = await api(`/drivers/nearby?lat=${p.lat}&lng=${p.lng}&category=${$("category").value}`);
    const index = await api("/system/map");
    $("pipeline").textContent = `Quadtree: ${index.points} pontos, ${index.leaves} folhas; snapshot ha ${Math.round(index.ageMs / 1000)}s. Hash Redis refinado a cada consulta, delta corrige cruzamento de regioes.`;
    $("nearby").textContent = `${candidates.length} disponiveis: ${candidates.map((d) => `${d.name} (${Math.round(d.distanceM)}m)`).join(", ") || "nenhum. Inicie o simulador GPS."}`;
    draw();
  } catch (error) { $("nearby").textContent = `Falha na busca: ${error.message}`; }
  setTimeout(refresh, 3000);
}
await action(async () => {
  const passengers = await api("/passengers");
  for (const passenger of passengers) {
    const option = document.createElement("option"); option.value = passenger.id; option.textContent = passenger.name;
    $("passenger").append(option);
  }
  const saved = localStorage.getItem("ride-hailing-trip");
  if (saved) await track(await api(`/trips/${saved}`));
})();
void refresh();
async function riderHeartbeat() {
  try {
    if ((!trip || terminal.has(trip.status)) && $("passenger").value) {
      const response = await fetch(`/api/riders/${$("passenger").value}/location`, {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ ...pickup(), sequence: Date.now(), observedAt: new Date().toISOString() }),
        signal: AbortSignal.timeout(5000),
      });
      if (!response.ok) throw new Error(`GPS passageiro: HTTP ${response.status}`);
    }
  } catch (error) { $("error").textContent = error.message; }
  setTimeout(riderHeartbeat, 4000);
}
void riderHeartbeat();
