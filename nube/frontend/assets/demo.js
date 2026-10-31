// Modo demo: datos simulados con la misma forma que nube/api, para mostrar el dashboard sin backend
(() => {
  if (!location.hostname.endsWith('github.io') && !new URLSearchParams(location.search).has('demo')) return;

  const ruido = a => (Math.random() * 2 - 1) * a;
  const r1 = v => Math.round(v * 10) / 10;
  const fila = (ts, i) => ({
    timestamp: ts,
    temperatura: r1(24 + 3 * Math.sin(i / 9) + ruido(.3)),
    humedad_aire: r1(58 + 8 * Math.sin(i / 13 + 1) + ruido(1)),
    humedad_suelo: r1(42 + 6 * Math.sin(i / 20 + 2) + ruido(1)),
    luminosidad: r1(Math.max(0, 600 + 450 * Math.sin(i / 15)) + ruido(20)),
    lecturas: 6,
  });

  function responder(url) {
    const u = new URL(url, location.href), ahora = Date.now();
    if (u.pathname.endsWith('/ultimo')) return { ...fila(ahora, Math.floor(ahora / 10000)), estado_planta: 'OPTIMO' };
    if (u.pathname.endsWith('/historico')) // mas reciente primero, como la API
      return Array.from({ length: 60 }, (_, i) => fila(ahora - i * 10000, Math.floor(ahora / 10000) - i));
    if (u.pathname.endsWith('/rango')) {
      const a = +u.searchParams.get('desde'), b = +u.searchParams.get('hasta');
      const paso = Math.max(Math.floor((b - a) / 120), 10000), out = [];
      for (let t = a; t < b; t += paso) out.push(fila(t, Math.floor(t / paso)));
      return out;
    }
    if (u.pathname.endsWith('/estado'))
      return { entity_id: 'urn:ngsi-ld:Planta:001', estado: 'NORMAL', valor: 24, umbral: 30 };
    return null;
  }

  const fetchReal = window.fetch.bind(window);
  window.fetch = (input, init) => {
    const url = typeof input === 'string' ? input : input.url;
    if (!/:(8000|5050)\//.test(url)) return fetchReal(input, init);
    const d = responder(url);
    return Promise.resolve(d ? new Response(JSON.stringify(d), { headers: { 'Content-Type': 'application/json' } })
                             : new Response('', { status: 404 }));
  };

  // Websocket de alertas: no se conecta a nada y nunca se cierra, asi no hay reintentos
  window.WebSocket = class { close() {} send() {} };
})();
