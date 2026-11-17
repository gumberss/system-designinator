"use strict";

const $ = (id) => document.getElementById(id);
let csrf = "";
let map = null;
let tracking = null;
let watch = null;
let sequence = 0;
let generation = 0;
let lastSent = null;
let pendingPosition = null;
let sending = false;
let cursor = null;
let lastQuery = "";
let marker = null;
const searchMarkers = [];
let navigation = null;
let navigationSocket = null;
let navigationRetry = null;
let navigationRevision = -1;
let navigationReconnects = 0;
let planningEV = false;
let conversation = null;
let placesRequest = null;
let placesCursor = null;
let placesBusy = false;
const placeMarkers = [];

function status(message, error = false) {
  $("status").textContent = message;
  $("status").classList.toggle("error", error);
}

async function api(path, options = {}) {
  const response = await fetch(path, {
    ...options,
    headers: { "Content-Type": "application/json", "X-CSRF-Token": csrf, ...options.headers },
    credentials: "same-origin",
  });
  const result = await response.json();
  if (!response.ok) throw new Error(result.detail || `HTTP ${response.status}`);
  return result;
}

function moved(a, b) {
  const rad = Math.PI / 180;
  const dlat = (a.latitude - b.latitude) * rad;
  const dlon = (a.longitude - b.longitude) * rad;
  const h = Math.sin(dlat / 2) ** 2 +
    Math.cos(a.latitude * rad) * Math.cos(b.latitude * rad) * Math.sin(dlon / 2) ** 2;
  return 6371000 * 2 * Math.asin(Math.sqrt(Math.min(1, h)));
}

async function sendPosition(position, ownGeneration) {
  if (!tracking || ownGeneration !== generation) return false;
  const c = position.coords;
  $("coordinates").textContent =
    `${c.latitude.toFixed(6)}, ${c.longitude.toFixed(6)} · precisão ±${Math.round(c.accuracy)} m`;
  if (map) {
    if (!marker) {
      marker = new maplibregl.Marker({ color: "#087c79" })
        .setLngLat([c.longitude, c.latitude]).addTo(map);
    } else {
      marker.setLngLat([c.longitude, c.latitude]);
    }
  }
  if (sending) {
    pendingPosition = position;
    return;
  }
  const elapsed = lastSent ? Date.now() - lastSent.sentAt : Infinity;
  if (lastSent && elapsed < 1000) return;
  if (lastSent && elapsed < 30000 && moved(c, lastSent.coords) < 10) return;
  sending = true;
  const payload = {
    event_id: crypto.randomUUID(), session_id: tracking, sequence: ++sequence,
    timestamp: new Date(position.timestamp).toISOString(),
    latitude: c.latitude, longitude: c.longitude, accuracy_m: c.accuracy,
    heading_deg: Number.isFinite(c.heading) ? c.heading : null,
    speed_mps: Number.isFinite(c.speed) ? c.speed : null,
  };
  try {
    const session = navigation;
    const result = await api(session ? "/api/v1/navigation/location" : "/api/v1/location", {
      method: "POST", body: JSON.stringify(session ? {...payload,navigation_session_id:session} : payload),
    });
    if (session && navigation === session && ownGeneration === generation) await updateNavigation(result);
    if (ownGeneration === generation) lastSent = { coords: c, sentAt: Date.now() };
    return true;
  } catch (error) {
    status(`Falha ao enviar GPS: ${error.message}`, true);
    return false;
  } finally {
    sending = false;
    const latest = pendingPosition;
    pendingPosition = null;
    if (latest && tracking && ownGeneration === generation) {
      void sendPosition(latest, ownGeneration);
    }
  }
}

function clearLocalTracking() {
  navigation = null;
  navigationRevision = -1;
  if (navigationRetry !== null) clearTimeout(navigationRetry);
  navigationRetry = null;
  if (navigationSocket) navigationSocket.close();
  navigationSocket = null;
  $("navigation-status").textContent = "Navegação parada.";
  generation += 1;
  if (watch !== null) navigator.geolocation.clearWatch(watch);
  watch = null;
  tracking = null;
  pendingPosition = null;
  lastSent = null;
  if (marker) marker.remove();
  marker = null;
  $("coordinates").textContent = "Rastreamento desativado.";
  $("traffic-consent").checked = false;
  $("traffic-consent").disabled = false;
  $("traffic-status").textContent = "Contribuição de trânsito desativada.";
}

async function stopTracking() {
  clearLocalTracking();
  $("track").disabled = true;
  try {
    await api("/api/v1/location/current", { method: "DELETE" });
    $("stop").disabled = true;
    $("track").disabled = false;
    status("Rastreamento desativado e posição apagada no servidor.");
    return true;
  } catch (error) {
    $("stop").disabled = false;
    status(`GPS local parado, mas a exclusão no servidor falhou: ${error.message}. Tente novamente.`, true);
    return false;
  }
}

$("track").addEventListener("click", async () => {
  if (!navigator.geolocation || !window.isSecureContext) {
    status("GPS exige navegador compatível e HTTPS ou localhost.", true);
    return;
  }
  $("track").disabled = true;
  $("traffic-consent").disabled = true;
  let activationGeneration = generation;
  try {
    // Permission is obtained before creating server-side tracking consent.
    const first = await new Promise((resolve, reject) =>
      navigator.geolocation.getCurrentPosition(resolve, reject, {
        enableHighAccuracy: true, timeout: 15000, maximumAge: 0,
      }));
    const session = await api("/api/v1/location/tracking", {
      method: "POST", body: JSON.stringify({ tracking_consent: true }),
    });
    tracking = session.session_id;
    sequence = 0;
    const ownGeneration = ++generation;
    activationGeneration = ownGeneration;
    $("stop").disabled = false;
    if ($("traffic-consent").checked) await updateTrafficConsent(true);
    if (ownGeneration !== generation || !tracking) return;
    const sent = await sendPosition(first, ownGeneration);
    if (ownGeneration !== generation || !tracking) return;
    if (map) map.flyTo({ center: [first.coords.longitude, first.coords.latitude], zoom: 16 });
    watch = navigator.geolocation.watchPosition(
      (p) => void sendPosition(p, ownGeneration),
      async (error) => {
        if (error.code === 1) {
          const deleted = await stopTracking();
          if (deleted) status("Permissão GPS revogada. Rastreamento parado e posição apagada.", true);
        } else {
          status(`GPS indisponível: ${error.message}. Desative para apagar a posição.`, true);
        }
      },
      { enableHighAccuracy: true, maximumAge: 1000, timeout: 15000 },
    );
    if (sent) status("Localização ativa. Envio limitado por deslocamento e tempo.");
  } catch (error) {
    if (tracking) {
      const deleted = await stopTracking();
      status(`Localização não ativada: ${error.message}. ` +
        (deleted ? "Consentimento revogado no servidor." : "Exclusão pendente: tente desativar novamente."), true);
    } else {
      clearLocalTracking();
      $("track").disabled = false;
      status(`Localização não ativada: ${error.message}`, true);
    }
  } finally {
    if (activationGeneration === generation) $("traffic-consent").disabled = false;
  }
});
$("stop").addEventListener("click", () => void stopTracking());

async function updateTrafficConsent(enabled) {
  if (!tracking) return;
  const session = tracking;
  const epoch = generation;
  await api("/api/v1/traffic/consent",{method:"POST",body:JSON.stringify({
    session_id:session,enabled,mode:"car",
  })});
  if (tracking !== session || epoch !== generation) return;
  $("traffic-status").textContent = enabled ?
    "Contribuição autorizada. Qualidade e agregação dependem dos dados e do worker." :
    "Contribuição revogada no servidor.";
}
$("traffic-consent").addEventListener("change",async()=>{
  const epoch = generation;
  $("traffic-consent").disabled = true;
  try {
    await updateTrafficConsent($("traffic-consent").checked);
  } catch(error) {
    const stopped = await stopTracking();
    $("traffic-status").textContent = `Alteração de consentimento falhou: ${error.message}. ` +
      (stopped ? "GPS parado e consentimento revogado." : "GPS local parado; exclusão pendente no servidor.");
    status("Não foi possível confirmar a alteração; o envio GPS foi interrompido.",true);
  } finally {
    if (epoch === generation) $("traffic-consent").disabled = false;
  }
});

async function search(append = false) {
  const query = append ? lastQuery : $("query").value.trim();
  if (query.length < 2) return;
  const offset = append ? cursor : 0;
  try {
    const data = await api(`/api/v1/geocoding/search?q=${encodeURIComponent(query)}&cursor=${offset}`);
    if (!append) {
      $("results").replaceChildren();
      searchMarkers.forEach((m) => m.remove());
      searchMarkers.length = 0;
    }
    data.items.forEach((item) => {
      const li = document.createElement("li");
      const button = document.createElement("button");
      button.textContent = item.name;
      button.onclick = () => {
        if (map) map.flyTo({ center: [item.longitude, item.latitude], zoom: 17 });
      };
      const detail = document.createElement("small");
      detail.textContent = `${item.category || "Categoria não informada"} · ${item.source_id} · ` +
        `${item.location_kind === "representative_point" ? "Ponto representativo" : "Coordenada OSM"} · ` +
        `${item.latitude.toFixed(5)}, ${item.longitude.toFixed(5)}`;
      li.append(button, detail);
      $("results").append(li);
      if (map) {
        searchMarkers.push(new maplibregl.Marker({ color: "#db793c" })
          .setLngLat([item.longitude, item.latitude]).addTo(map));
      }
    });
    cursor = data.next_cursor;
    lastQuery = query;
    $("more").hidden = cursor === null;
    status(data.items.length ? "Resultados do dataset OSM local." :
      "Nenhum resultado no recorte importado. A busca não cobre toda a cidade.");
  } catch (error) {
    status(`Falha na busca: ${error.message}`, true);
  }
}
$("search-form").addEventListener("submit", (event) => {
  event.preventDefault();
  void search();
});
$("more").addEventListener("click", () => void search(true));

let routePaths = [];
const instructionLabels = {
  depart: "Parta", continue: "Continue", turn_left: "Vire à esquerda",
  turn_right: "Vire à direita", arrive: "Chegue ao destino",
};

function clearTrafficLayer() {
  if (map && map.getLayer("traffic")) map.removeLayer("traffic");
  if (map && map.getSource("traffic")) map.removeSource("traffic");
}

function drawRoute(index) {
  const path = routePaths[index];
  if (!path) return;
  clearTrafficLayer();
  $("route-summary").textContent =
    `${(path.distance_m / 1000).toFixed(2)} km · ${(path.eta_s / 60).toFixed(1)} min estimados ` +
    `(${({baseline:"baseline sem trânsito",historical:"histórico",realtime:"trânsito recente",mixed:"fontes combinadas"})[path.traffic_source || "baseline"]})`;
  $("instructions").replaceChildren();
  path.instructions.forEach((step) => {
    const li = document.createElement("li");
    li.textContent = `${instructionLabels[step.action]}${step.name ? `: ${step.name}` : ""}` +
      (step.distance_m ? ` · ${Math.round(step.distance_m)} m` : "");
    $("instructions").append(li);
  });
  if (map && map.isStyleLoaded()) {
    const data = {type:"Feature",properties:{},geometry:path.geometry};
    if (map.getSource("route")) map.getSource("route").setData(data);
    else {
      map.addSource("route", {type:"geojson",data});
      map.addLayer({id:"route",type:"line",source:"route",
        paint:{"line-color":"#087c79","line-width":6}});
    }
    const points = path.geometry.coordinates;
    const bounds = points.reduce((b,p)=>b.extend(p),new maplibregl.LngLatBounds(points[0],points[0]));
    map.fitBounds(bounds,{padding:45,maxZoom:17});
  }
}

function endpoint(raw) {
  const parts = raw.trim().split(",");
  if (parts.length === 2 && parts.every((p)=>p.trim() && Number.isFinite(Number(p)))) {
    return {latitude:Number(parts[0]),longitude:Number(parts[1])};
  }
  return raw.trim();
}

$("gps-source").addEventListener("click",async()=>{
  try {
    const current = await api("/api/v1/location/current");
    $("source").value = `${current.latitude}, ${current.longitude}`;
  } catch(error) {
    status(`Posição atual indisponível: ${error.message}`,true);
  }
});
$("route-choice").addEventListener("change",()=>drawRoute(Number($("route-choice").value)));
$("traffic-map").addEventListener("click",async()=>{
  const path = routePaths[Number($("route-choice").value || 0)];
  if (!path || !map || !map.isStyleLoaded()) {
    status("Calcule uma rota e aguarde o mapa antes de consultar trânsito.",true);
    return;
  }
  clearTrafficLayer();
  $("traffic-map").disabled = true;
  try {
    const segments = await Promise.all((path.segment_ids || []).map(
      id=>api(`/api/v1/traffic/segments/${encodeURIComponent(id)}`)));
    if (routePaths[Number($("route-choice").value || 0)] !== path) return;
    const features = segments.flatMap(segment=>segment.items)
      .filter(item=>item.source!=="baseline")
      .map(item=>{
        const ratio = item.effective_speed_mps/item.baseline_speed_mps;
        return {type:"Feature",geometry:item.geometry,properties:{
          color:ratio<0.35?"#c83d3d":ratio<0.7?"#e08a35":"#279d77",
        }};
      });
    if (!features.length) {
      status("Sem observações ou histórico suficientes: trânsito não inventado.");
      return;
    }
    map.addSource("traffic",{type:"geojson",data:{type:"FeatureCollection",features}});
    map.addLayer({id:"traffic",type:"line",source:"traffic",
      paint:{"line-color":["get","color"],"line-width":9}},map.getLayer("route")?"route":undefined);
    status("Camada de trânsito consultada. A rota/ETA anterior não é recalculada automaticamente.");
  } catch(error) {
    if (routePaths[Number($("route-choice").value || 0)] === path) {
      clearTrafficLayer();
      status(`Consulta de trânsito falhou: ${error.message}`,true);
    }
  } finally {
    $("traffic-map").disabled = false;
  }
});
$("route-form").addEventListener("submit",async(event)=>{
  event.preventDefault();
  if (navigation || planningEV) {
    status("Pare a navegação antes de planejar outra rota.",true);
    return;
  }
  $("calculate-route").disabled = true;
  $("route-summary").textContent = "Calculando…";
  $("instructions").replaceChildren();
  $("route-choice").hidden = true;
  routePaths = [];
  clearEVLayer();
  if (map && map.getLayer("route")) map.removeLayer("route");
  if (map && map.getSource("route")) map.removeSource("route");
  clearTrafficLayer();
  try {
    const result = await api("/api/v1/routes",{method:"POST",body:JSON.stringify({
      source:endpoint($("source").value),destination:endpoint($("destination").value),
      mode:$("mode").value,strategy:$("strategy").value,alternatives:2,
      preferences:{avoid_tolls:$("avoid-tolls").checked,avoid_motorways:$("avoid-motorways").checked},
    })});
    routePaths = [result,...result.alternatives];
    $("route-choice").replaceChildren();
    routePaths.forEach((_,index)=>{
      const option = document.createElement("option");
      option.value = String(index);
      option.textContent = index===0 ? "Rota recomendada" : `Alternativa ${index}`;
      $("route-choice").append(option);
    });
    $("route-choice").hidden = routePaths.length < 2;
    drawRoute(0);
    status(`Rota calculada. ${result.warnings.join(" ")}`);
  } catch(error) {
    $("route-summary").textContent = "Nenhuma rota calculada.";
    status(`Falha ao calcular rota: ${error.message}`,true);
  } finally {
    $("calculate-route").disabled = false;
  }
});

async function updateNavigation(view,force=false) {
  if (!navigation || view.session_id !== navigation ||
      view.revision<navigationRevision || (!force && view.revision===navigationRevision)) return;
  navigationRevision = view.revision;
  if (view.route && routePaths[0]?.route_id !== view.route_id) {
    routePaths = [view.route];
    $("route-choice").hidden = true;
    $("route-choice").value = "0";
    drawRoute(0);
  }
  const eta = Number.isFinite(view.eta_s) ? `${(view.eta_s/60).toFixed(1)} min estimados` : "ETA indisponível";
  const instruction = view.instruction;
  $("navigation-status").textContent =
    `${Math.round(view.remaining_m)} m restantes · ${eta} · ${view.state}. ` +
    (instruction ? `${instructionLabels[instruction.action] || instruction.action}: ${instruction.name || ""}. ` : "") +
    (view.warnings || []).join(" ");
  if (view.state === "arrived") {
    const stopped = await stopTracking();
    if (stopped) status("Destino alcançado. Navegação parada e GPS revogado.");
  }
}

function connectNavigation() {
  if (!navigation) return;
  const ownSession = navigation;
  const protocol = location.protocol === "https:" ? "wss:" : "ws:";
  navigationSocket = new WebSocket(
    `${protocol}//${location.host}/api/v1/navigation/${ownSession}/stream?resume_sequence=${navigationRevision}`);
  navigationSocket.onmessage = (event) => {
    if (navigation !== ownSession) return;
    try {
      const view = JSON.parse(event.data);
      if (view.type === "error") {
        void stopTracking();
        status(`Navegação interrompida: ${view.code}.`,true);
      } else if (view.type !== "heartbeat") {
        navigationReconnects = 0;
        void updateNavigation(view,view.type==="snapshot");
      }
    } catch(error) {
      void stopTracking();
      status(`Resposta de navegação inválida: ${error.message}`,true);
    }
  };
  navigationSocket.onclose = (event) => {
    if (navigation !== ownSession) return;
    if ([4401,4403,4404].includes(event.code) || navigationReconnects>=5) {
      void stopTracking();
      status("Stream indisponível; GPS local interrompido. Confirme a exclusão no servidor.",true);
      return;
    }
    navigationReconnects += 1;
    $("navigation-status").textContent = "Stream desconectado; reconectando com snapshot...";
    navigationRetry = setTimeout(connectNavigation,1000);
  };
}

$("navigate").addEventListener("click",async()=>{
  const route = routePaths[0];
  if (!tracking || !route?.route_id || Number($("route-choice").value || 0)!==0) {
    status("Ative o GPS e selecione uma rota recomendada calculada antes de navegar.",true);
    return;
  }
  if (navigation) return;
  $("navigate").disabled = true;
  const epoch = generation;
  try {
    const result = await api("/api/v1/navigation/start",{method:"POST",body:JSON.stringify({
      route_id:route.route_id,tracking_session_id:tracking,tracking_consent:true,
      traffic_consent:$("traffic-consent").checked,
    })});
    if (epoch !== generation || !tracking) {
      await api("/api/v1/navigation/stop",{method:"POST",body:JSON.stringify({session_id:result.session_id})});
      return;
    }
    navigation = result.session_id;
    navigationReconnects = 0;
    navigationRevision = -1;
    await updateNavigation(result);
    connectNavigation();
    status("Navegação experimental ativa. Desative para revogar GPS e trânsito.");
  } catch(error) {
    const stopped = await stopTracking();
    status(`Navegação não iniciada: ${error.message}. ` +
      (stopped ? "GPS parado." : "Exclusão pendente; tente desativar novamente."),true);
  } finally {
    $("navigate").disabled = false;
  }
});

function clearEVLayer() {
  if (map && map.getLayer("ev-itinerary")) map.removeLayer("ev-itinerary");
  if (map && map.getSource("ev-itinerary")) map.removeSource("ev-itinerary");
  $("ev-summary").textContent = "Nenhum itinerário EV ativo.";
  $("ev-stops").replaceChildren();
}

$("ev-form").addEventListener("submit",async(event)=>{
  event.preventDefault();
  if (navigation || $("calculate-route").disabled || planningEV) {
    status("Pare a navegação/aguarde o cálculo atual antes de planejar EV.",true);
    return;
  }
  $("ev-plan").disabled = true;
  planningEV = true;
  $("calculate-route").disabled = true;
  clearEVLayer();
  $("ev-stops").replaceChildren();
  try {
    const source = endpoint($("source").value), destination = endpoint($("destination").value);
    if (typeof source === "string" || typeof destination === "string") {
      throw new Error("O plano EV exige coordenadas explícitas latitude, longitude.");
    }
    let vehicle;
    if ($("ev-profile").checked) {
      const value = id=>{
        if (!$(id).value.trim() || !Number.isFinite(Number($(id).value))) {
          throw new Error("Preencha todos os valores reais do perfil.");
        }
        return Number($(id).value);
      };
      vehicle = {battery_capacity_kwh:value("ev-capacity"),initial_soc_percent:value("ev-soc"),
        reserve_percent:value("ev-reserve"),consumption_kwh_per_100km:value("ev-consumption"),
        max_charging_power_kw:value("ev-power"),
        connectors:$("ev-connectors").value.split(",").map(x=>x.trim()).filter(Boolean)};
    }
    const result = await api("/api/v1/ev/trips",{method:"POST",body:JSON.stringify({source,destination,vehicle})});
    if (map && map.getLayer("route")) map.removeLayer("route");
    if (map && map.getSource("route")) map.removeSource("route");
    clearTrafficLayer();
    routePaths = [];
    $("route-choice").hidden = true;
    $("instructions").replaceChildren();
    if (!result.ev_planning_available) {
      routePaths = [result.ordinary_route];
      drawRoute(0);
      $("ev-summary").textContent = `Planejamento EV indisponível: ${result.fallback_reason}. Apenas rota comum, sem afirmar viabilidade EV.`;
    } else {
      $("route-summary").textContent = "Itinerário EV separado: não é uma sessão de navegação de todas as pernas.";
      $("ev-summary").textContent = `${(result.total_time_s/60).toFixed(1)} min estimados (fila desconhecida) · ` +
        `${result.energy.lower_kwh.toFixed(2)}–${result.energy.upper_kwh.toFixed(2)} kWh. ` +
        (result.warnings || []).join(" ");
      result.charging_stops.forEach(stop=>{
        const li = document.createElement("li");
        li.textContent = `${stop.name || stop.source_id} · ${stop.connector} · ` +
          `${stop.effective_power_limit_kw.toFixed(1)} kW máximos · ` +
          `${(stop.charging_time_interval_s.lower/60).toFixed(1)}–${(stop.charging_time_interval_s.upper/60).toFixed(1)} min de carga estimados · ` +
          `${stop.arrival_soc_percent.toFixed(1)}% → ${stop.departure_soc_percent.toFixed(1)}% · disponibilidade desconhecida`;
        $("ev-stops").append(li);
      });
      if (map && map.isStyleLoaded()) {
        const features = result.legs.map(leg=>({type:"Feature",properties:{},geometry:leg.route.geometry}));
        map.addSource("ev-itinerary",{type:"geojson",data:{type:"FeatureCollection",features}});
        map.addLayer({id:"ev-itinerary",type:"line",source:"ev-itinerary",
          paint:{"line-color":"#bb7b22","line-width":6}});
        const points = features.flatMap(f=>f.geometry.coordinates);
        if (points.length) map.fitBounds(points.reduce((b,p)=>b.extend(p),
          new maplibregl.LngLatBounds(points[0],points[0])),{padding:45,maxZoom:17});
      }
    }
    status("Plano EV consultado; verifique limitações, intervalos e disponibilidade desconhecida.");
  } catch(error) {
    clearEVLayer();
    $("ev-summary").textContent = `Plano EV falhou: ${error.message}`;
    status("Nenhum plano EV novo foi confirmado.",true);
  } finally {
    planningEV = false;
    $("ev-plan").disabled = false;
    $("calculate-route").disabled = false;
  }
});

function placeCenter() {
  const value = endpoint($("places-center").value.trim() || $("source").value);
  if (typeof value === "string") throw new Error("Informe coordenadas explícitas para o centro da busca.");
  return value;
}

function showPlaces(result,append=false) {
  if (!append) {
    clearPlaces();
  }
  result.items.forEach(item=>{
    const li = document.createElement("li");
    const button = document.createElement("button");
    button.textContent = `${item.name} · ${Math.round(item.distance_m)} m`;
    button.onclick = ()=>map?.flyTo({center:[item.longitude,item.latitude],zoom:17});
    const details = document.createElement("small");
    const opening = item.open_at === true ? "aberto" : item.open_at === false ? "fechado" : "desconhecido";
    details.textContent = `${item.source_id} · horário: ${opening} · ` +
      `avaliação: ${item.rating ?? "desconhecida"} · ambiente de trabalho: ${item.work_friendly ?? "não verificado"}`;
    li.append(button,details);
    $("places-results").append(li);
    if (map) placeMarkers.push(new maplibregl.Marker({color:"#8061bd"})
      .setLngLat([item.longitude,item.latitude]).addTo(map));
  });
  $("places-summary").textContent = result.explanation + " " + (result.warnings || []).join(" ");
  placesCursor = result.next_cursor;
  $("places-more").hidden = placesCursor === null || placesCursor === undefined;
}

function clearPlaces() {
  $("places-results").replaceChildren();
  placeMarkers.forEach(marker=>marker.remove());
  placeMarkers.length = 0;
  $("places-more").hidden = true;
}

async function searchPlaces(append=false) {
  if (placesBusy) return;
  placesBusy = true;
  $("places-find").disabled = true;
  if (!append) clearPlaces();
  try {
    const request = append ? {...placesRequest,cursor:placesCursor} : {
      center:placeCenter(),criteria:{query:$("places-query").value.trim() || null,
        categories:$("places-categories").value.split(",").map(x=>x.trim()).filter(Boolean),
        radius_m:Number($("places-radius").value),open_now:$("places-open").checked,
        wifi:$("places-wifi").checked},limit:10,
    };
    const result = await api("/api/v1/places/search",{method:"POST",body:JSON.stringify(request)});
    if (!append) placesRequest = request;
    showPlaces(result,append);
    status(result.items.length ? "Lugares recuperados do OSM local." : "Nenhum lugar atende aos dados/filtros verificados.");
  } catch(error) {
    $("places-summary").textContent = `Busca falhou: ${error.message}`;
    status("Não foi confirmada uma nova busca de lugares.",true);
  } finally {
    placesBusy = false;
    $("places-find").disabled = false;
  }
}
$("places-form").addEventListener("submit",event=>{event.preventDefault();void searchPlaces();});
$("places-more").addEventListener("click",()=>void searchPlaces(true));
$("conversation-reset").addEventListener("click",()=>{
  if (placesBusy) return;
  conversation = null;
  clearPlaces();
  $("conversation-message").value = "";
  $("places-summary").textContent = "Próxima mensagem inicia uma conversa nova; a antiga expira por TTL.";
});
$("conversation-form").addEventListener("submit",async(event)=>{
  event.preventDefault();
  if (placesBusy) return;
  placesBusy = true;
  $("conversation-send").disabled = true;
  clearPlaces();
  try {
    const body = {message:$("conversation-message").value,center:placeCenter(),limit:10};
    if (conversation) Object.assign(body,{conversation_id:conversation.id,revision:conversation.revision});
    else body.criteria = {query:$("places-query").value.trim() || null,
      categories:$("places-categories").value.split(",").map(x=>x.trim()).filter(Boolean),
      radius_m:Number($("places-radius").value),open_now:$("places-open").checked,
      wifi:$("places-wifi").checked};
    const result = await api("/api/v1/places/conversation",{method:"POST",body:JSON.stringify(body)});
    conversation = {id:result.conversation_id,revision:result.revision};
    placesRequest = {center:body.center,criteria:result.criteria,limit:10};
    showPlaces(result);
    status(`Conversa atualizada, revisão ${result.revision}. Critérios validados; fatos vêm do OSM.`);
  } catch(error) {
    $("places-summary").textContent = `Conversa falhou: ${error.message}. Nenhuma resposta simulada foi usada.`;
    status("Conversa não confirmada; modelo/configuração ou revisão podem estar indisponíveis.",true);
  } finally {
    placesBusy = false;
    $("conversation-send").disabled = false;
  }
});

async function initialize() {
  try {
    const session = await api("/api/v1/auth/session", { method: "POST" });
    csrf = session.csrf_token;
    const config = await api("/api/v1/config");
    $("llm-state").textContent = config.local_model_configured ?
      "Modelo local configurado; disponibilidade será verificada na chamada." :
      "Modelo local não configurado. Busca sem IA disponível; conversa retorna erro explícito.";
    if (typeof maplibregl === "undefined") {
      status("MapLibre não carregou. Verifique o acesso ao CDN; busca e GPS continuam disponíveis.", true);
    } else {
      map = new maplibregl.Map({
        container: "map", center: [-46.655, -23.56], zoom: 15,
        style: config.map_style_url || {
          version: 8, sources: {}, layers: [
            { id: "background", type: "background", paint: { "background-color": "#e7eff4" } },
          ],
        },
      });
      map.addControl(new maplibregl.NavigationControl());
      map.addControl(new maplibregl.AttributionControl({ customAttribution:
        '<a href="https://www.openstreetmap.org/copyright" target="_blank">© OpenStreetMap contributors</a>' }));
      map.on("error", () => status("Falha ao carregar recurso do mapa. Verifique estilo/tiles.", true));
      if (config.map_style_url) $("map-notice").textContent = "Tiles externos configurados pelo operador.";
    }
    try {
      const dataset = await api("/api/v1/datasets/current");
      $("dataset").textContent =
        `OSM: ${dataset.node_count} nós · ${dataset.way_count} vias · ${dataset.place_count} nomes. ` +
        `Importado em ${new Date(dataset.imported_at).toLocaleString()}. ODbL 1.0.`;
      if (map) {
        const show = () => {
          map.addSource("local-roads", { type: "geojson", data: dataset.road_geojson });
          map.addLayer({ id: "local-roads", type: "line", source: "local-roads",
            paint: { "line-color": "#63869e", "line-width": 3 } });
          const [west, south, east, north] = dataset.bounds;
          map.fitBounds([[west, south], [east, north]], { padding: 35 });
        };
        if (map.isStyleLoaded()) show(); else map.once("load", show);
        status("Mapa pronto. Busque um lugar ou ative sua localização.");
      }
    } catch (error) {
      status(`Mapa sem dados: ${error.message}`, true);
    }
  } catch (error) {
    $("track").disabled = true;
    status(`Inicialização falhou: ${error.message}`, true);
  }
}
void initialize();
