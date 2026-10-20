/* The background of the sign-in page: a slowly turning globe – the continents as dots – with flights between
   logistics hubs drawn as red / white arcs. Canvas 2D, no libraries; ~30 frames a second, a still picture when
   the system asks for reduced motion. Land: Natural Earth 110m (public domain) as 2° cells, 90 rows x 180 columns
   (row i = latitude 89 - 2i, column j = longitude -179 + 2j, bits row by row, most significant first). */
(() => {
  "use strict";
  const canvas = document.getElementById("bg");
  const ctx = canvas && canvas.getContext && canvas.getContext("2d");
  if (!ctx) return;
  const LAND = "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAf4AP/AAAAAAAAAAAAAAAAAAAAAAAAX/z///+AAAAAAAABAAAAAAAAAAAAAYd8P///wAA+AAAAAA8AAAAAAAAAAAwAnw////4AAIAAAAAAGAAAAAAAAAAADivwAf//wAAAAADAAf/wAHYAAAAAADoi3sAP//gAAAAAMAD///sAAAAgBgACfwz/AD/+gAAAwAEHf8AABADf+AP4EAY2icP8AB///4A//EgAAAAAAAgHwAAAAAAeD4Af///gAFAAgAAAAAAAAzgAAAAAC4H8D/h//BhQAAAAAAAAAAAAP/////4A0B4AAAD5/////////////Af3////gHgA4AAAH5///////////LwAHgH///gHkAAAAAH4/////////+CIAABAB///4D+AAAAGCx/////////4A8AAIAAf///n/gAAAOCD/////////wA4AAAAAf///n/wAAAbP///////////AgAAAAAP/////wAAADf//////////9AAAAAAAF////0YAAAB///////////9AAAAAAAD////8EAAAB///////////5AAAAAAAD////2AAAAB/f5fP//////wAAAAAAAD////gAAAAfxnwPP//////jAAAAAAAD////AAAAAPCb3/n/////+CAAAAAAAD///8AAAAAfALf/n////+ECAAAAAAAB///8AAAAAGHQP/n/////mMAAAAAAAA///8AAAAAH+Ai///////E8AAAAAAAAf//wAAAAAP/AA///////BgAAAAAAAAP//gAAAAAf/73///////gAAAAAAAAAD/AQAAAAAf////f/////gAAAAAAAAAF+AQAAAAB///+/n/////AAAAAAAAAAC+AAAAAAB///+f0H////AAAAAAAAAAAeAwAAAAD////f/B///8gAAAAAAAAAAeGEAAAAH////v+B/z/AAAAAAAAAAAAPMAgAAAD////n+A/B+gAAAAAAAAAAAD8AAAAAD////n4AeB/AgAAAAAAAAAAAPAAAAAH////3gAcAfAgAAAAAAAAAAADAAAAAD////6AAcAfggAAAAAAAAAAABDwAAAD////8wAMATAIAAAAAAAAAAAAr/AAAB/////gAKASAAAAAAAAAAAAAAH/gAAA/////gACAAAIAAAAAAAAAAAAH/8AAAaH///AAAAsGAAAAAAAAAAAAAH/+AAAAB//+AAAAUOAAAAAAAAAAAAAP/+AAAAB//8AAAAYegAAAAAAAAAAAAP//gAAAD//4AAAAMeBgAAAAAAAAAAAP//8AAAB//wAAAAGdiuAAAAAAAAAAAf///AAAA//wAAAACAQHgAAAAAAAAAAP///gAAA//wAAAABwAHwgAAAAAAAAAH///AAAA//wAAAAACIDQIAAAAAAAAAH//+AAAAf/wAAAAAAAAAAAAAAAAAAAD//+AAAA//wgAAAAABxAAAAAAAAAAAD//+AAAA//wgAAAAAPxgBAAAAAAAAAA//8AAAA//jgAAAAAf5gAAAAAAAAAAAf/8AAAA//DgAAAAAf/gAAAAAAAAAAAf/8AAAAf/DAAAAAD//4CAAAAAAAAAAf/wAAAAf/DAAAAAH//4AAAAAAAAAAAf/AAAAAf+CAAAAAH//8AAAAAAAAAAAf/AAAAAP8AAAAAAH//+AAAAAAAAAAA/+AAAAAP8AAAAAAH//+AAAAAAAAAAA/+AAAAAH4AAAAAAD//+AAAAAAAAAAA/8AAAAAHwAAAAAADwf8AAAAAAAAAAA/gAAAAAAAAAAAAACAH4AIAAAAAAAAB/wAAAAAAAAAAAAAAAD4AEAAAAAAAAB+AAAAAAAAAAAAAAAAAAAGAAAAAAAAB6AAAAAAAAAAAAAAAAAwAMAAAAAAAAA8AAAAAAAAAAAAAAAAAQAYAAAAAAAAB4AAAAAAAAAAAAAAAAAAAwAAAAAAAAB4AAAAAAAAAAAAAAAAAAAAAAAAAAAADwAAAAAAAAAACAAAAAAAAAAAAAAAAADgAAAAAAAAAAAAAAAAAAAAAAAAAAAABwAAAAAAAAAAAAAAAAAAAAAAAAAAAAAwAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAACAAAAAAAAAAAAAAAAAAAAAAAAAAAAAMAAAAAAAAAeAAIP+f/gAAAAAAAAAAAMAAAAAAABP/+H//////AAAAAAAAAAA+AAAAAf////8////////AAAAAAAOEAPAAAB///////////////gAAAP//T//8AAAH//////////////+AAAH/////4AAAH///////////////8AAE//////4ABw////////////////8AAAD//////gCA////////////////wAAAf/////////////////////////+AAH/AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA";
  const RAD = Math.PI / 180;
  const vec = (lat, lon) => [Math.cos(lat * RAD) * Math.sin(lon * RAD), Math.sin(lat * RAD), Math.cos(lat * RAD) * Math.cos(lon * RAD)];

  // land dots: points spread evenly over the sphere (a Fibonacci lattice, so no rows show) that fall on land; no Antarctica
  const mask = Uint8Array.from(atob(LAND), (ch) => ch.charCodeAt(0));
  const isLand = (row, col) => (mask[(row * 180 + col) >> 3] >> (7 - ((row * 180 + col) & 7))) & 1;
  const dots = [];
  const N = 12000, GOLDEN = Math.PI * (3 - Math.sqrt(5));
  for (let i = 0; i < N; i++) {
    const y = 1 - (2 * (i + 0.5)) / N, r = Math.sqrt(1 - y * y), x = Math.cos(i * GOLDEN) * r, z = Math.sin(i * GOLDEN) * r;
    const lat = Math.asin(y) / RAD, lon = Math.atan2(x, z) / RAD;
    if (lat < -56) continue;
    const row = Math.min(89, Math.max(0, Math.floor((90 - lat) / 2))), col = Math.min(179, Math.max(0, Math.floor((lon + 180) / 2)));
    if (isLand(row, col)) dots.push([x, y, z]);
  }

  const HUBS = {
    LAX: [33.9, -118.4], VAN: [49.3, -123.1], CHI: [41.9, -87.6], NYC: [40.7, -74], MIA: [25.8, -80.2], MEX: [19.4, -99.1],
    SAO: [-23.5, -46.6], LON: [51.5, -0.1], RTM: [51.9, 4.5], FRA: [50.1, 8.7], DXB: [25.2, 55.3], JNB: [-26.2, 28],
    BOM: [19.1, 72.9], SIN: [1.35, 103.8], HKG: [22.3, 114.2], SHA: [31.2, 121.5], SEL: [37.6, 127], TYO: [35.7, 139.7], SYD: [-33.9, 151.2],
  };
  const LANES = [
    ["SHA", "LAX"], ["HKG", "LAX"], ["SHA", "CHI"], ["TYO", "LAX"], ["SEL", "LAX"], ["VAN", "HKG"], ["HKG", "SIN"],
    ["SIN", "RTM"], ["SHA", "RTM"], ["HKG", "FRA"], ["DXB", "FRA"], ["SHA", "DXB"], ["BOM", "DXB"], ["LON", "DXB"],
    ["JNB", "DXB"], ["NYC", "LON"], ["NYC", "RTM"], ["FRA", "CHI"], ["MIA", "SAO"], ["NYC", "SAO"], ["MEX", "CHI"],
    ["MIA", "LAX"], ["TYO", "CHI"], ["LAX", "SYD"], ["SIN", "SYD"],
  ];
  const hubs = Object.values(HUBS).map(([lat, lon], i) => ({ p: vec(lat, lon), phase: i * 0.37 }));

  // the view: a slight tilt shows more of the north, where most hubs are; the globe turns west to east
  const TILT = 18 * RAD, SPIN = (2 * Math.PI) / 110000, START = 160 * RAD;
  const reduced = window.matchMedia && matchMedia("(prefers-reduced-motion: reduce)").matches;
  let w = 0, h = 0, dpr = 1, cx = 0, cy = 0, R = 0, cosT = 1, sinT = 0;
  const cosTilt = Math.cos(TILT), sinTilt = Math.sin(TILT);
  function view(p, lift = 1) { // -> [screen x, screen y, depth (z > 0: facing us), hidden behind the globe?]
    const x = p[0] * lift, y = p[1] * lift, z = p[2] * lift;
    const x1 = x * cosT + z * sinT, z1 = -x * sinT + z * cosT;
    const y2 = y * cosTilt - z1 * sinTilt, z2 = y * sinTilt + z1 * cosTilt;
    return [cx + R * x1, cy - R * y2, z2, z2 < 0 && x1 * x1 + y2 * y2 < 1];
  }

  const stars = Array.from({ length: 140 }, () => ({ x: Math.random(), y: Math.random(), r: 0.4 + Math.random() * 0.9, a: 0.08 + Math.random() * 0.3, t: Math.random() * 6 }));

  // flights: a great circle from hub to hub, lifted above the surface
  const flights = [];
  function launch(now, progress = 0) {
    const busy = new Set(flights.map((f) => f.lane));
    const free = LANES.filter((l) => !busy.has(l.join()));
    const lane = free[Math.floor(Math.random() * free.length)];
    if (!lane) return;
    const [A, B] = Math.random() < 0.5 ? lane : [lane[1], lane[0]];
    const a = vec(...HUBS[A]), b = vec(...HUBS[B]);
    const omega = Math.acos(Math.max(-1, Math.min(1, a[0] * b[0] + a[1] * b[1] + a[2] * b[2])));
    const dur = 2600 + 3400 * (omega / Math.PI);
    flights.push({ lane: lane.join(), a, b, omega, dur, start: now - progress * dur, lift: 0.06 + 0.16 * (omega / Math.PI), end: null });
  }
  function along(f, t) { // the point at t (0..1) of the flight
    const s = Math.sin(f.omega) || 1, ka = Math.sin((1 - t) * f.omega) / s, kb = Math.sin(t * f.omega) / s;
    const lift = 1 + f.lift * Math.sin(Math.PI * t);
    return view([ka * f.a[0] + kb * f.b[0], ka * f.a[1] + kb * f.b[1], ka * f.a[2] + kb * f.b[2]], lift);
  }

  function resize() {
    dpr = Math.min(window.devicePixelRatio || 1, 2);
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
    const ang = START + now * SPIN;
    cosT = Math.cos(ang);
    sinT = Math.sin(ang);
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
    for (let lon = -180; lon < 180; lon += 20) gridLine((t) => vec(t, lon), -80, 80);
    for (let lat = -60; lat <= 60; lat += 20) gridLine((t) => vec(lat, t), -180, 180);
    // continents
    ctx.fillStyle = "#9cc3ff";
    for (const d of dots) {
      const [x, y, z] = view(d);
      if (z <= 0) continue;
      ctx.globalAlpha = 0.18 + 0.72 * z;
      ctx.fillRect(x - 0.9, y - 0.9, 1.8, 1.8);
    }
    // hubs
    for (const hub of hubs) {
      const [x, y, z] = view(hub.p);
      if (z <= 0.05) continue;
      const k = ((now / 2600 + hub.phase) % 1);
      ctx.globalAlpha = (1 - k) * 0.6 * z;
      ctx.strokeStyle = "#ff5a62";
      ctx.lineWidth = 1.2;
      ctx.beginPath();
      ctx.arc(x, y, 2 + k * 11, 0, 2 * Math.PI);
      ctx.stroke();
      ctx.globalAlpha = z;
      ctx.fillStyle = "#ffffff";
      ctx.beginPath();
      ctx.arc(x, y, 2.1, 0, 2 * Math.PI);
      ctx.fill();
    }
    // flights
    for (let i = flights.length - 1; i >= 0; i--) {
      const f = flights[i];
      const p = Math.min(1, (now - f.start) / f.dur);
      if (p >= 1 && f.end === null) f.end = now;
      const fade = f.end === null ? 1 : 1 - (now - f.end) / 900;
      if (fade <= 0) { flights.splice(i, 1); continue; }
      drawFlight(f, p, fade);
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
    // the lane, faint
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
  window.addEventListener("resize", () => { resize(); if (reduced) draw(40000); });
  if (reduced) { // one still picture
    for (let i = 0; i < 6; i++) launch(40000, 0.35 + i * 0.1);
    draw(40000);
    return;
  }
  const t0 = performance.now();
  for (let i = 0; i < 4; i++) launch(0, 0.15 + i * 0.2);
  let last = -1e9, nextLaunch = 0;
  function frame(nowAbs) {
    requestAnimationFrame(frame);
    const now = nowAbs - t0;
    if (now - last < 30) return; // ~30 frames a second is smooth enough here and easy on laptops
    last = now;
    if (now >= nextLaunch && flights.length < 7) { launch(now); nextLaunch = now + 500 + Math.random() * 700; }
    draw(now);
  }
  requestAnimationFrame(frame);
})();
