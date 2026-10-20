/* The background of the sign-in page: a globe – the continents as dots, the US as a finer, brighter dot map –
   swinging between the Pacific and the Atlantic, with flights from the world's hubs landing one after the other
   where the US gateways are (Chicago, Los Angeles, New York …). Canvas 2D, no libraries, ~30 frames a second
   (slower when the system asks for reduced motion). Natural Earth 110m (public domain), bits row by row, most
   significant first: LAND = land in 2° cells, 90 rows x 180 columns (row i = latitude 89 - 2i, column j = longitude
   -179 + 2j); US = the lower 48 states in 0.5° cells of latitude 24..50, longitude -126..-66, 52 rows x 120 columns. */
(() => {
  "use strict";
  const canvas = document.getElementById("bg");
  const ctx = canvas && canvas.getContext && canvas.getContext("2d");
  if (!ctx) return;
  const US = "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAf////////4AAAAAAAAAEf/////////yEAAAAAAAHf///////////AAAAAAAD////////////4AAAABQD////////////8AAAADwD/////////////AAAADwD/////////////gAAAHwD/////////////4AAAf4D/////////////4AB//8D/////////////4AD//gD/////////////8AH/8AH/////////////4D//4AH/////////////4D//wAH/////////////w///wAD/////////////3///8AD////////////////+AAH////////////////8AAH////////////////wAAD////////////////wAAD///////////////+gAAB////////////////AAAA///////////////7AAAAf//////////////6AAAAf//////////////4AAAAP//////////////8AAAAP//////////////8AAAAH//////////////8AAAAD//////////////8AAAAB//////////////4AAAAAf/////////////gAAAAAH////////////+AAAAAAB////////////8AAAAAAA////////////wAAAAAAAA///////////gAAAAAAAAH/H////////AAAAAAAAAAAD///////+AAAAAAAAAAAA///////+AAAAAAAAAAAA/////gP+AAAAAAAAAAAAef/+/gEfAAAAAAAAAAAAMP/wDAAPAAAAAAAAAAAAAH/gAAAPgAAAAAAAAAAAAD+AAAAHgAAAAAAAAAAAAB8AAAAPgAAAAAAAAAAAAB4AAAAHwAAAAAAAAAAAAA4AAAAHwAAAAAAAAAAAAAYAAAADwAAAAAAAAAAAAAAAAAABwAAAAAAAAAAAAAAAAAAAgAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA";
  const LAND = "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAf4AP/AAAAAAAAAAAAAAAAAAAAAAAAX/z///+AAAAAAAABAAAAAAAAAAAAAYd8P///wAA+AAAAAA8AAAAAAAAAAAwAnw////4AAIAAAAAAGAAAAAAAAAAADivwAf//wAAAAADAAf/wAHYAAAAAADoi3sAP//gAAAAAMAD///sAAAAgBgACfwz/AD/+gAAAwAEHf8AABADf+AP4EAY2icP8AB///4A//EgAAAAAAAgHwAAAAAAeD4Af///gAFAAgAAAAAAAAzgAAAAAC4H8D/h//BhQAAAAAAAAAAAAP/////4A0B4AAAD5/////////////Af3////gHgA4AAAH5///////////LwAHgH///gHkAAAAAH4/////////+CIAABAB///4D+AAAAGCx/////////4A8AAIAAf///n/gAAAOCD/////////wA4AAAAAf///n/wAAAbP///////////AgAAAAAP/////wAAADf//////////9AAAAAAAF////0YAAAB///////////9AAAAAAAD////8EAAAB///////////5AAAAAAAD////2AAAAB/f5fP//////wAAAAAAAD////gAAAAfxnwPP//////jAAAAAAAD////AAAAAPCb3/n/////+CAAAAAAAD///8AAAAAfALf/n////+ECAAAAAAAB///8AAAAAGHQP/n/////mMAAAAAAAA///8AAAAAH+Ai///////E8AAAAAAAAf//wAAAAAP/AA///////BgAAAAAAAAP//gAAAAAf/73///////gAAAAAAAAAD/AQAAAAAf////f/////gAAAAAAAAAF+AQAAAAB///+/n/////AAAAAAAAAAC+AAAAAAB///+f0H////AAAAAAAAAAAeAwAAAAD////f/B///8gAAAAAAAAAAeGEAAAAH////v+B/z/AAAAAAAAAAAAPMAgAAAD////n+A/B+gAAAAAAAAAAAD8AAAAAD////n4AeB/AgAAAAAAAAAAAPAAAAAH////3gAcAfAgAAAAAAAAAAADAAAAAD////6AAcAfggAAAAAAAAAAABDwAAAD////8wAMATAIAAAAAAAAAAAAr/AAAB/////gAKASAAAAAAAAAAAAAAH/gAAA/////gACAAAIAAAAAAAAAAAAH/8AAAaH///AAAAsGAAAAAAAAAAAAAH/+AAAAB//+AAAAUOAAAAAAAAAAAAAP/+AAAAB//8AAAAYegAAAAAAAAAAAAP//gAAAD//4AAAAMeBgAAAAAAAAAAAP//8AAAB//wAAAAGdiuAAAAAAAAAAAf///AAAA//wAAAACAQHgAAAAAAAAAAP///gAAA//wAAAABwAHwgAAAAAAAAAH///AAAA//wAAAAACIDQIAAAAAAAAAH//+AAAAf/wAAAAAAAAAAAAAAAAAAAD//+AAAA//wgAAAAABxAAAAAAAAAAAD//+AAAA//wgAAAAAPxgBAAAAAAAAAA//8AAAA//jgAAAAAf5gAAAAAAAAAAAf/8AAAA//DgAAAAAf/gAAAAAAAAAAAf/8AAAAf/DAAAAAD//4CAAAAAAAAAAf/wAAAAf/DAAAAAH//4AAAAAAAAAAAf/AAAAAf+CAAAAAH//8AAAAAAAAAAAf/AAAAAP8AAAAAAH//+AAAAAAAAAAA/+AAAAAP8AAAAAAH//+AAAAAAAAAAA/+AAAAAH4AAAAAAD//+AAAAAAAAAAA/8AAAAAHwAAAAAADwf8AAAAAAAAAAA/gAAAAAAAAAAAAACAH4AIAAAAAAAAB/wAAAAAAAAAAAAAAAD4AEAAAAAAAAB+AAAAAAAAAAAAAAAAAAAGAAAAAAAAB6AAAAAAAAAAAAAAAAAwAMAAAAAAAAA8AAAAAAAAAAAAAAAAAQAYAAAAAAAAB4AAAAAAAAAAAAAAAAAAAwAAAAAAAAB4AAAAAAAAAAAAAAAAAAAAAAAAAAAADwAAAAAAAAAACAAAAAAAAAAAAAAAAADgAAAAAAAAAAAAAAAAAAAAAAAAAAAABwAAAAAAAAAAAAAAAAAAAAAAAAAAAAAwAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAACAAAAAAAAAAAAAAAAAAAAAAAAAAAAAMAAAAAAAAAeAAIP+f/gAAAAAAAAAAAMAAAAAAABP/+H//////AAAAAAAAAAA+AAAAAf////8////////AAAAAAAOEAPAAAB///////////////gAAAP//T//8AAAH//////////////+AAAH/////4AAAH///////////////8AAE//////4ABw////////////////8AAAD//////gCA////////////////wAAAf/////////////////////////+AAH/AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA";
  const RAD = Math.PI / 180;
  const vec = (lat, lon) => [Math.cos(lat * RAD) * Math.sin(lon * RAD), Math.sin(lat * RAD), Math.cos(lat * RAD) * Math.cos(lon * RAD)];

  // land dots: points spread evenly over the sphere (a Fibonacci lattice, so no rows show) that fall on land; no Antarctica
  const bitsOf = (b64) => Uint8Array.from(atob(b64), (ch) => ch.charCodeAt(0));
  const mask = bitsOf(LAND), usMask = bitsOf(US);
  const isLand = (row, col) => (mask[(row * 180 + col) >> 3] >> (7 - ((row * 180 + col) & 7))) & 1;
  const inUS = (lat, lon) => {
    if (lat < 24 || lat >= 50 || lon < -126 || lon >= -66) return false;
    const k = Math.floor((50 - lat) / 0.5) * 120 + Math.floor((lon + 126) / 0.5);
    return ((usMask[k >> 3] >> (7 - (k & 7))) & 1) === 1;
  };
  const dots = [];
  const N = 12000, GOLDEN = Math.PI * (3 - Math.sqrt(5));
  for (let i = 0; i < N; i++) {
    const y = 1 - (2 * (i + 0.5)) / N, r = Math.sqrt(1 - y * y), x = Math.cos(i * GOLDEN) * r, z = Math.sin(i * GOLDEN) * r;
    const lat = Math.asin(y) / RAD, lon = Math.atan2(x, z) / RAD;
    if (lat < -56 || inUS(lat, lon)) continue; // (the US has its own, finer dots)
    const row = Math.min(89, Math.max(0, Math.floor((90 - lat) / 2))), col = Math.min(179, Math.max(0, Math.floor((lon + 180) / 2)));
    if (isLand(row, col)) dots.push([x, y, z]);
  }
  // the US: a finer dot map (a honeycomb ~0.7° apart), so where the flights land can be recognised
  const usDots = [];
  for (let row = 0, lat = 49.65; lat > 24; row++, lat -= 0.7) {
    const step = 0.7 / Math.cos(lat * RAD);
    for (let lon = -125.95 + (row % 2) * (step / 2); lon < -66; lon += step) if (inUS(lat, lon)) usDots.push(vec(lat, lon));
  }

  // where the flights land (US gateways) and where they come from
  const GATEWAYS = {
    ORD: [41.98, -87.9], LAX: [33.94, -118.41], JFK: [40.64, -73.78], MIA: [25.8, -80.29],
    DFW: [32.9, -97.04], SFO: [37.62, -122.38], ATL: [33.64, -84.43], SEA: [47.45, -122.31],
  };
  const ORIGINS = {
    PVG: [31.14, 121.81], HKG: [22.31, 113.92], SZX: [22.64, 113.81], ICN: [37.46, 126.44], NRT: [35.77, 140.39], TPE: [25.08, 121.23],
    SIN: [1.36, 103.99], FRA: [50.04, 8.56], AMS: [52.31, 4.76], LHR: [51.47, -0.45], DXB: [25.25, 55.36], MEX: [19.44, -99.07], GRU: [-23.43, -46.47],
  };
  const gateways = Object.entries(GATEWAYS).map(([code, [lat, lon]], i) => ({ code, p: vec(lat, lon), phase: i * 0.29, flash: -1e9 }));
  const origins = Object.values(ORIGINS).map(([lat, lon]) => vec(lat, lon));

  // the view: seen a little from the south, so the US sits high on the globe (above the sign-in card); the globe
  // swings between the Pacific and the Atlantic around North America
  const TILT = -11 * RAD, CENTER = -98, SWING = 58, PERIOD = 52000;
  const reduced = window.matchMedia && matchMedia("(prefers-reduced-motion: reduce)").matches;
  const SPEED = reduced ? 0.5 : 1;
  const cosTilt = Math.cos(TILT), sinTilt = Math.sin(TILT);
  let w = 0, h = 0, cx = 0, cy = 0, R = 0, cosT = 1, sinT = 0;
  function view(p, lift = 1) { // -> [screen x, screen y, depth (z > 0: facing us), hidden behind the globe?]
    const x = p[0] * lift, y = p[1] * lift, z = p[2] * lift;
    const x1 = x * cosT + z * sinT, z1 = -x * sinT + z * cosT;
    const y2 = y * cosTilt - z1 * sinTilt, z2 = y * sinTilt + z1 * cosTilt;
    return [cx + R * x1, cy - R * y2, z2, z2 < 0 && x1 * x1 + y2 * y2 < 1];
  }

  const stars = Array.from({ length: 140 }, () => ({ x: Math.random(), y: Math.random(), r: 0.4 + Math.random() * 0.9, a: 0.08 + Math.random() * 0.3, t: Math.random() * 6 }));

  // flights: a great circle lifted above the surface; most come from abroad, some hop between gateways
  const flights = [];
  const pick = (list) => list[Math.floor(Math.random() * list.length)];
  function launch(now, progress = 0) {
    const to = pick(gateways);
    const from = Math.random() < 0.65 ? pick(origins) : pick(gateways.filter((g) => g !== to)).p;
    const a = from, b = to.p;
    const omega = Math.acos(Math.max(-1, Math.min(1, a[0] * b[0] + a[1] * b[1] + a[2] * b[2])));
    const dur = 2400 + 3600 * (omega / Math.PI);
    flights.push({ to, a, b, omega, dur, start: now - progress * dur, lift: 0.05 + 0.18 * (omega / Math.PI), end: null });
  }
  function along(f, t) { // the point at t (0..1) of the flight
    const s = Math.sin(f.omega) || 1, ka = Math.sin((1 - t) * f.omega) / s, kb = Math.sin(t * f.omega) / s;
    const lift = 1 + f.lift * Math.sin(Math.PI * t);
    return view([ka * f.a[0] + kb * f.b[0], ka * f.a[1] + kb * f.b[1], ka * f.a[2] + kb * f.b[2]], lift);
  }

  function resize() {
    const dpr = Math.min(window.devicePixelRatio || 1, 2);
    w = window.innerWidth;
    h = window.innerHeight;
    canvas.width = Math.round(w * dpr);
    canvas.height = Math.round(h * dpr);
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    cx = w / 2;
    cy = h / 2;
    R = Math.max(150, Math.min(w * 0.46, h * 0.46, 560));
  }

  function draw(now) {
    const lon = CENTER + SWING * Math.sin((2 * Math.PI * now) / PERIOD);
    cosT = Math.cos(-lon * RAD);
    sinT = Math.sin(-lon * RAD);
    // sky
    const sky = ctx.createLinearGradient(0, 0, 0, h);
    sky.addColorStop(0, "#04112a");
    sky.addColorStop(1, "#0a2148");
    ctx.globalAlpha = 1;
    ctx.fillStyle = sky;
    ctx.fillRect(0, 0, w, h);
    const glow = ctx.createRadialGradient(cx, cy, R * 0.6, cx, cy, R * 1.75);
    glow.addColorStop(0, "rgba(46, 108, 220, 0.30)");
    glow.addColorStop(1, "rgba(46, 108, 220, 0)");
    ctx.fillStyle = glow;
    ctx.fillRect(0, 0, w, h);
    ctx.fillStyle = "#cfe0ff";
    for (const s of stars) {
      ctx.globalAlpha = s.a * (0.6 + 0.4 * Math.sin(now / 1400 + s.t));
      ctx.fillRect(s.x * w, s.y * h, s.r, s.r);
    }
    // the globe: atmosphere, body, grid
    ctx.globalAlpha = 1;
    const air = ctx.createRadialGradient(cx, cy, R * 0.96, cx, cy, R * 1.16);
    air.addColorStop(0, "rgba(110, 165, 255, 0.35)");
    air.addColorStop(1, "rgba(110, 165, 255, 0)");
    ctx.fillStyle = air;
    ctx.beginPath();
    ctx.arc(cx, cy, R * 1.16, 0, 2 * Math.PI);
    ctx.fill();
    const body = ctx.createRadialGradient(cx - R * 0.35, cy - R * 0.4, R * 0.1, cx, cy, R);
    body.addColorStop(0, "#1b4590");
    body.addColorStop(1, "#071a3d");
    ctx.fillStyle = body;
    ctx.beginPath();
    ctx.arc(cx, cy, R, 0, 2 * Math.PI);
    ctx.fill();
    ctx.strokeStyle = "rgba(130, 180, 255, 0.10)";
    ctx.lineWidth = 1;
    for (let m = -180; m < 180; m += 20) gridLine((t) => vec(t, m), -80, 80);
    for (let lat = -60; lat <= 60; lat += 20) gridLine((t) => vec(lat, t), -180, 180);
    // continents
    ctx.fillStyle = "#9cc3ff";
    for (const d of dots) {
      const [x, y, z] = view(d);
      if (z <= 0) continue;
      ctx.globalAlpha = 0.18 + 0.72 * z;
      ctx.fillRect(x - 0.9, y - 0.9, 1.8, 1.8);
    }
    ctx.fillStyle = "#d9e8ff";
    for (const d of usDots) {
      const [x, y, z] = view(d);
      if (z <= 0) continue;
      ctx.globalAlpha = 0.3 + 0.7 * z;
      ctx.fillRect(x - 0.8, y - 0.8, 1.6, 1.6);
    }
    // where flights come from
    ctx.fillStyle = "#ffffff";
    for (const o of origins) {
      const [x, y, z] = view(o);
      if (z <= 0.05) continue;
      ctx.globalAlpha = 0.65 * z;
      ctx.beginPath();
      ctx.arc(x, y, 1.6, 0, 2 * Math.PI);
      ctx.fill();
    }
    // flights
    for (let i = flights.length - 1; i >= 0; i--) {
      const f = flights[i];
      const p = Math.min(1, (now - f.start) / f.dur);
      if (p >= 1 && f.end === null) { f.end = now; f.to.flash = now; } // landed
      const fade = f.end === null ? 1 : 1 - (now - f.end) / 900;
      if (fade <= 0) { flights.splice(i, 1); continue; }
      drawFlight(f, p, fade);
    }
    // the gateways: a pulse, and a flash when a flight lands
    for (const g of gateways) {
      const [x, y, z] = view(g.p);
      if (z <= 0.05) continue;
      const k = (now / 2600 + g.phase) % 1;
      ctx.globalAlpha = (1 - k) * 0.5 * z;
      ctx.strokeStyle = "#ff5a62";
      ctx.lineWidth = 1.2;
      ctx.beginPath();
      ctx.arc(x, y, 2 + k * 10, 0, 2 * Math.PI);
      ctx.stroke();
      const since = now - g.flash;
      if (since < 1300) { // a flight has just landed here
        const q = since / 1300;
        ctx.globalAlpha = (1 - q) * z;
        ctx.strokeStyle = "#ffffff";
        ctx.lineWidth = 2;
        ctx.beginPath();
        ctx.arc(x, y, 3 + q * 22, 0, 2 * Math.PI);
        ctx.stroke();
        const halo = ctx.createRadialGradient(x, y, 0, x, y, 14);
        halo.addColorStop(0, "rgba(255, 90, 98, 0.9)");
        halo.addColorStop(1, "rgba(255, 90, 98, 0)");
        ctx.fillStyle = halo;
        ctx.beginPath();
        ctx.arc(x, y, 14, 0, 2 * Math.PI);
        ctx.fill();
      }
      ctx.globalAlpha = z;
      ctx.fillStyle = "#ffffff";
      ctx.beginPath();
      ctx.arc(x, y, 2.4, 0, 2 * Math.PI);
      ctx.fill();
    }
    ctx.globalAlpha = 1;
  }

  function gridLine(point, from, to) {
    ctx.beginPath();
    let pen = false;
    for (let t = from; t <= to; t += 4) {
      const [x, y, z] = view(point(t));
      if (z <= 0) { pen = false; continue; }
      if (pen) ctx.lineTo(x, y); else ctx.moveTo(x, y);
      pen = true;
    }
    ctx.globalAlpha = 1;
    ctx.stroke();
  }

  function drawFlight(f, p, fade) {
    const STEPS = 48;
    // the route, faint
    ctx.lineWidth = 1;
    ctx.strokeStyle = "#7fb0ff";
    let prev = null;
    for (let s = 0; s <= STEPS; s++) {
      const q = along(f, s / STEPS);
      if (prev && !q[3] && !prev[3]) {
        ctx.globalAlpha = 0.16 * fade;
        ctx.beginPath();
        ctx.moveTo(prev[0], prev[1]);
        ctx.lineTo(q[0], q[1]);
        ctx.stroke();
      }
      prev = q;
    }
    // the trail: red, brighter towards the head
    const tail = 0.38, t0 = Math.max(0, p - tail);
    ctx.lineWidth = 2;
    ctx.lineCap = "round";
    prev = null;
    for (let s = 0; s <= 24; s++) {
      const t = t0 + ((p - t0) * s) / 24;
      const q = along(f, t);
      if (prev && !q[3] && !prev[3]) {
        const k = (t - t0) / (tail || 1);
        ctx.globalAlpha = Math.min(1, 0.15 + k) * fade;
        ctx.strokeStyle = k > 0.85 ? "#ffd6d8" : "#ff4b55";
        ctx.beginPath();
        ctx.moveTo(prev[0], prev[1]);
        ctx.lineTo(q[0], q[1]);
        ctx.stroke();
      }
      prev = q;
    }
    // the plane
    const head = along(f, p);
    if (!head[3] && p < 1) {
      const halo = ctx.createRadialGradient(head[0], head[1], 0, head[0], head[1], 9);
      halo.addColorStop(0, "rgba(255, 90, 98, 0.9)");
      halo.addColorStop(1, "rgba(255, 90, 98, 0)");
      ctx.globalAlpha = fade;
      ctx.fillStyle = halo;
      ctx.beginPath();
      ctx.arc(head[0], head[1], 9, 0, 2 * Math.PI);
      ctx.fill();
      ctx.fillStyle = "#ffffff";
      ctx.beginPath();
      ctx.arc(head[0], head[1], 2.2, 0, 2 * Math.PI);
      ctx.fill();
    }
  }

  resize();
  window.addEventListener("resize", resize);
  const t0 = performance.now();
  for (let i = 0; i < 5; i++) launch(0, 0.1 + i * 0.18);
  let last = -1e9, nextLaunch = 0;
  function frame(nowAbs) {
    requestAnimationFrame(frame);
    const now = (nowAbs - t0) * SPEED;
    if (now - last < 30 * SPEED) return; // ~30 frames a second is smooth enough here and easy on laptops
    last = now;
    if (now >= nextLaunch && flights.length < 9) { launch(now); nextLaunch = now + 350 + Math.random() * 650; }
    draw(now);
  }
  requestAnimationFrame(frame);
})();
