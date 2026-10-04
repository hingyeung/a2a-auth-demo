// Auth Arcade: draws the live auth events from the orchestrator agent's /events stream.
//
// Flow: EventSource -> queue -> one "beat" at a time. Each beat plays a short
// animation on the Phaser map, then shows the step's note and token card in
// the HTML panels, then waits for NEXT (or the auto-play timer).
// The events come from the real services. See common/common/events.py.

const $ = (id) => document.getElementById(id);

// ---------------- map layout (320 x 180 world, 16px tiles, zoom 3) ----------------

const W = 320, H = 180, ROAD_Y = 118;

// Where each actor stands, and the label under its building. Keys are the
// technical IDs used in event src/dst (agent1 = orchestrator agent, agent2 =
// repository agent); labels are what the player reads.
const PLACES = {
  user:     { x: 24,  y: ROAD_Y, label: "HOME" },
  keycloak: { x: 88,  y: 70,     label: "KEYCLOAK" },
  agent1:   { x: 136, y: ROAD_Y, label: "ORCHESTRATOR" },
  // labelDy: GATE sits between two long labels, so its label drops a row.
  gate:     { x: 184, y: ROAD_Y, label: "GATE", labelDy: 10 },
  agent2:   { x: 232, y: ROAD_Y, label: "REPO AGENT" },
  mcp:      { x: 296, y: ROAD_Y, label: "MCP" },
  // GitHub's login server (the OAuth authorization server for GitHub tokens).
  // Its own building, apart from the MCP vault: it gives out GitHub tokens,
  // the vault only accepts them.
  github:   { x: 264, y: 70,     label: "GITHUB" },
};
// Places up a side road. Their x is also the x of that side road.
const UP_PLACES = ["keycloak", "github"];

// Kenney sheet frames (12 tiles per row).
const TOWN = { grass: [0, 1, 2], road: 40, flower: 2, tree: 16, bush: 5 };
const BUILDINGS = [
  // [sheet, left col, top row, frame grid]
  ["town", 0, 4, [[52, 53, 55], [64, 65, 67], [84, 85, 87]]],          // home
  ["town", 4, 1, [[99, 100, 101], [111, 112, 113], [123, 124, 125]]],  // keycloak castle
  ["town", 7, 4, [[48, 49, 51], [60, 61, 63], [88, 89, 91]]],          // orchestrator agent keep
  ["town", 13, 4, [[48, 49, 50], [60, 61, 62], [76, 78, 79]]],         // repository agent tower
  ["dungeon", 17, 4, [[57, 58, 59], [57, 58, 59], [45, 46, 47]]],      // MCP vault
  ["dungeon", 15, 1, [[9, 10, 11], [21, 22, 23], [33, 34, 35]]],       // GitHub login server
];
const CHAR = { alice: 99, bob: 88, keycloak: 84, agent1: 96, agent2: 112, mcp: 87, github: 100 };
const ITEM = { userKey: 117, oboKey: 117, ghCoin: 93, scroll: 83, chest: 89, chestOpen: 91 };

// ---------------- state ----------------

const queue = [];
let busy = false;
let waiting = null;          // resolve() of the current NEXT wait
let auto = false;
let player = "alice";
let lastSeq = 0;
const tokens = {};           // label -> token view (from the events)
let scene = null;

// ---------------- Phaser scene ----------------

class Town extends Phaser.Scene {
  constructor() { super("town"); }

  preload() {
    this.load.spritesheet("town", "/static/arcade/assets/town.png", { frameWidth: 16, frameHeight: 16 });
    this.load.spritesheet("dungeon", "/static/arcade/assets/dungeon.png", { frameWidth: 16, frameHeight: 16 });
  }

  create() {
    scene = this;
    // Ground: grass everywhere, one road along row 7, a side road up to Keycloak.
    for (let r = 0; r < 12; r++) {
      for (let c = 0; c < 20; c++) {
        const f = (r * 7 + c * 13) % 11 === 0 ? TOWN.grass[1 + ((r + c) % 2)] : TOWN.grass[0];
        this.add.image(c * 16 + 8, r * 16 + 8, "town", f);
      }
    }
    for (let c = 0; c < 20; c++) this.add.image(c * 16 + 8, 7 * 16 + 8, "town", TOWN.road);
    for (const k of UP_PLACES) {
      for (let r = 4; r < 7; r++) this.add.image(PLACES[k].x, r * 16 + 8, "town", TOWN.road);
    }
    [[1, 1], [2, 2], [10, 1], [11, 2], [13, 1], [19, 0], [1, 10], [9, 10], [16, 10], [12, 9]].forEach(([c, r], i) =>
      this.add.image(c * 16 + 8, r * 16 + 8, "town", i % 3 ? TOWN.tree : TOWN.bush));

    for (const [sheet, col, row, grid] of BUILDINGS) {
      grid.forEach((line, dr) => line.forEach((f, dc) =>
        this.add.image((col + dc) * 16 + 8, (row + dr) * 16 + 8, sheet, f)));
    }
    // The gate between the orchestrator agent and the repository agent: a portcullis with four check lamps.
    this.gate = this.add.image(PLACES.gate.x, 6 * 16 + 8, "dungeon", 68);
    this.lamps = [0, 1, 2, 3].map((i) => {
      const x = PLACES.gate.x - 15 + i * 10;
      const lamp = this.add.rectangle(x, 4 * 16 + 10, 7, 7, 0x555566).setStrokeStyle(1, 0x000000);
      this.add.text(x, 4 * 16 - 6, String(i + 1), this.font(8)).setOrigin(0.5, 0);
      return lamp;
    });

    for (const [key, p] of Object.entries(PLACES)) {
      const ly = (UP_PLACES.includes(key) ? 2 : 8 * 16 + 2) + (p.labelDy || 0);
      const t = this.add.text(p.x, ly, p.label, this.font(8)).setOrigin(0.5, 0);
      t.setBackgroundColor("#000000aa").setPadding(1, 1, 1, 0);
    }

    this.chest = this.add.image(PLACES.mcp.x + 14, ROAD_Y - 4, "dungeon", ITEM.chest);
    this.store = this.add.image(PLACES.agent2.x + 14, ROAD_Y - 4, "dungeon", ITEM.chest).setVisible(false);

    this.actors = {
      user: this.add.sprite(PLACES.user.x, ROAD_Y, "dungeon", CHAR[player]),
      keycloak: this.add.sprite(PLACES.keycloak.x, PLACES.keycloak.y, "dungeon", CHAR.keycloak),
      agent1: this.add.sprite(PLACES.agent1.x, ROAD_Y, "dungeon", CHAR.agent1),
      agent2: this.add.sprite(PLACES.agent2.x, ROAD_Y, "dungeon", CHAR.agent2),
      mcp: this.add.sprite(PLACES.mcp.x - 10, ROAD_Y, "dungeon", CHAR.mcp),
      github: this.add.sprite(PLACES.github.x, PLACES.github.y, "dungeon", CHAR.github),
    };
    this.actors.mcp.setFlipX(true);

    this.banner = this.add.text(W / 2, H - 12, "", this.font(8, "#ffcf4a")).setOrigin(0.5, 0)
      .setBackgroundColor("#000000").setPadding(2, 1, 2, 0).setDepth(9);
    this.big = this.add.text(W / 2, H / 2 - 20, "", this.font(16, "#ffcf4a"))
      .setOrigin(0.5).setStroke("#000", 4).setVisible(false).setDepth(10);
    pump();  // events may have arrived while the scene was loading
  }

  font(size, color = "#f4efe3") {
    // Press Start 2P is an 8px pixel font: keep sizes at 8 or 16 so it stays sharp.
    return { fontFamily: '"Press Start 2P"', fontSize: `${size}px`, color };
  }

  tween(cfg) {
    return new Promise((res) => this.tweens.add({ ...cfg, onComplete: res }));
  }

  homeOf(who) {
    return who === "mcp" ? { x: PLACES.mcp.x - 10, y: ROAD_Y } : PLACES[who];
  }

  // Walk an actor to a place (or back to its own place). Places up a side
  // road (Keycloak, GitHub) are reached along the road, then up that side road.
  async walk(who, to) {
    const a = this.actors[who];
    let tx, ty, lane;
    if (!to || to === who) {
      const h = this.homeOf(who);
      tx = h.x; ty = h.y; lane = h.x;
    } else {
      const p = PLACES[to];
      ty = p.y; lane = p.x; tx = p.x + (a.x < p.x ? -12 : 12);
    }
    if (Math.abs(a.x - tx) < 1 && Math.abs(a.y - ty) < 1) return;
    Sfx.walk();
    const legs = [];
    const up = a.y !== ROAD_Y;
    if (up && (ty === ROAD_Y || a.lane !== lane)) legs.push({ x: a.lane }, { y: ROAD_Y });
    if (ty !== ROAD_Y) {
      if (!up || a.lane !== lane) legs.push({ x: lane }, { y: ty });
      a.lane = lane;
    }
    legs.push({ x: tx });
    for (const leg of legs) {
      const dx = leg.x === undefined ? 0 : leg.x - a.x;
      const dy = leg.y === undefined ? 0 : leg.y - a.y;
      const d = Math.abs(dx) + Math.abs(dy);
      if (d < 1) continue;
      if (dx) a.setFlipX(dx < 0);
      await this.tween({ targets: a, ...leg, duration: 120 + d * 7 });
    }
  }

  // An item flies from one actor to another along an arc.
  async fly(frame, sheet, from, to, tint) {
    const a = this.actors[from] || PLACES[from], b = this.actors[to] || PLACES[to];
    const it = this.add.image(a.x, a.y - 10, sheet, frame).setDepth(5);
    if (tint) it.setTint(tint);
    const midY = Math.min(a.y, b.y) - 30;
    const curve = new Phaser.Curves.QuadraticBezier(
      new Phaser.Math.Vector2(a.x, a.y - 10),
      new Phaser.Math.Vector2((a.x + b.x) / 2, midY),
      new Phaser.Math.Vector2(b.x, b.y - 10));
    const t = { v: 0 };
    await this.tween({
      targets: t, v: 1, duration: 900,
      onUpdate: () => { const pt = curve.getPoint(t.v); it.setPosition(pt.x, pt.y); },
    });
    await this.tween({ targets: it, scale: 1.8, alpha: 0, duration: 250 });
    it.destroy();
  }

  // A word bubble over an actor ("401", "OK", "?").
  async pop(who, text, color = "#f4efe3", ms = 1100) {
    const a = this.actors[who] || PLACES[who];
    const t = this.add.text(a.x, a.y - 16, text, this.font(8, color))
      .setOrigin(0.5, 1).setBackgroundColor("#000000").setPadding(2, 2, 2, 1).setDepth(6);
    await this.tween({ targets: t, y: a.y - 22, duration: 250 });
    await new Promise((r) => setTimeout(r, ms));
    t.destroy();
  }

  async shake(who) {
    const a = this.actors[who];
    const x = a.x;
    await this.tween({ targets: a, x: x - 3, duration: 50, yoyo: true, repeat: 3 });
    a.x = x;
  }

  lamp(i, ok) {
    if (i < 0) return;
    this.lamps[i].setFillStyle(ok ? 0x5fd36a : 0xff5a5a, 1);
  }

  resetLamps() {
    this.lamps.forEach((l) => l.setFillStyle(0x555566));
  }

  async gateOpen(open) {
    await this.tween({ targets: this.gate, y: open ? 6 * 16 - 6 : 6 * 16 + 8, duration: 350 });
  }

  async bigText(text, color) {
    this.big.setText(text).setColor(color).setVisible(true).setScale(0.2);
    await this.tween({ targets: this.big, scale: 1, duration: 400, ease: "Back.Out" });
  }

  resetStage() {
    this.big.setVisible(false);
    this.resetLamps();
    this.gate.y = 6 * 16 + 8;
    this.chest.setFrame(ITEM.chest);
    for (const [k, a] of Object.entries(this.actors)) {
      a.x = this.homeOf(k).x;
      a.y = PLACES[k].y;
      a.lane = UP_PLACES.includes(k) ? PLACES[k].x : undefined;
      a.setFlipX(k === "mcp");
    }
    this.actors.user.setFrame(CHAR[player]);
  }

  setBanner(leg) {
    const names = { H2A: "WORLD 1  H2A: HUMAN TO AGENT", A2A: "WORLD 2  A2A: AGENT TO AGENT",
                    CONSENT: "BONUS  GITHUB CONSENT", MCP: "WORLD 3  MCP: AGENT TO TOOL" };
    this.banner.setText(names[leg] || "").setVisible(!!names[leg]);
  }
}

// ---------------- animations per step ----------------

const GATE_LAMP = { bearer: 0, jwt: 1, caller: 2, scope: 3 };

async function animate(ev) {
  const s = scene;
  s.setBanner(ev.leg);
  switch (ev.step) {
    case "h2a.login.start":
      s.resetStage();
      await s.walk("user", "keycloak");
      await s.pop("keycloak", "PASSWORD?");
      break;
    case "h2a.login.token":
      Sfx.mint();
      await s.fly(ITEM.userKey, "town", "keycloak", "user");
      await s.walk("user");
      break;
    case "ask.start":
      await s.walk("user", "agent1");
      await s.pop("user", "LIST MY REPOS!");
      break;
    case "h2a.role.check":
      s.walk("user");
      await s.pop("agent1", ev.check.ok ? "ROLE OK" : "NO ROLE", ev.check.ok ? "#5fd36a" : "#ff5a5a");
      ev.check.ok ? Sfx.pass() : Sfx.deny();
      break;
    case "a2a.exchange.request":
      s.resetLamps();
      await s.walk("agent1", "keycloak");
      await s.pop("agent1", "SWAP PLEASE");
      break;
    case "a2a.exchange.token":
      Sfx.mint();
      await s.fly(ITEM.oboKey, "town", "keycloak", "agent1", 0xffcf4a);
      await s.walk("agent1");
      break;
    case "a2a.call":
      await s.walk("agent1", "gate");
      break;
    case "a2a.check.bearer": case "a2a.check.jwt": case "a2a.check.caller": case "a2a.check.scope": {
      const i = GATE_LAMP[ev.check.name];
      s.lamp(i, ev.check.ok);
      if (ev.check.ok) {
        Sfx.pass();
        await s.pop("gate", `CHECK ${i + 1} OK`, "#5fd36a", 600);
        if (ev.check.name === "scope") { Sfx.door(); await s.gateOpen(true); }
      } else {
        Sfx.deny();
        await s.pop("gate", String(ev.http?.status || "NO"), "#ff5a5a", 700);
        await s.shake("agent1");
      }
      break;
    }
    case "a2a.refused":
      await s.walk("agent1");
      Sfx.over();
      await s.bigText("GAME OVER", "#ff5a5a");
      break;
    case "mcp.token.lookup":
      await s.gateOpen(false);
      await s.pop("agent2", ev.check.ok ? "TOKEN FOUND" : "NO TOKEN", ev.check.ok ? "#5fd36a" : "#ffcf4a");
      break;
    case "consent.needed":
      await s.fly(ITEM.scroll, "town", "agent2", "agent1");
      await s.walk("agent1");
      break;
    case "a2a.input_required":
      await s.fly(ITEM.scroll, "town", "agent1", "user");
      await s.pop("user", "CONSENT LINK!", "#d38bff");
      break;
    case "consent.ticket":
      await s.walk("user", "agent2");
      await s.pop("agent2", "TICKET OK", "#5fd36a");
      break;
    case "mcp.probe":
      await s.walk("agent2", "mcp");
      await s.pop("mcp", String(ev.http?.status || 401), "#ff5a5a");
      break;
    case "mcp.discover":
      await s.pop("agent2", "WHO GIVES TOKENS?", "#ffcf4a", 800);
      await s.pop("mcp", "ASK GITHUB", "#ffcf4a", 800);
      await s.pop("github", "THAT'S ME", "#ffcf4a", 800);
      await s.walk("agent2");
      break;
    case "consent.authorize":
      await s.walk("user", "github");
      await s.pop("github", "LOG IN + AGREE?", "#d38bff", 900);
      await s.pop("user", "I AGREE", "#d38bff", 700);
      break;
    case "consent.callback":
      await s.pop("github", "HERE'S A CODE", "#d38bff", 700);
      await s.walk("user", "agent2");
      await s.fly(ITEM.scroll, "town", "user", "agent2");
      await s.pop("agent2", "TICKET OK", "#5fd36a", 600);
      break;
    case "consent.swap":
      await s.walk("agent2", "github");
      await s.pop("agent2", "CODE + VERIFIER", "#ffcf4a");
      break;
    case "consent.token":
      Sfx.mint();
      await s.fly(ITEM.ghCoin, "town", "github", "agent2");
      await s.walk("agent2");
      break;
    case "consent.stored":
      s.store.setVisible(true);
      Sfx.door();
      await s.walk("user");
      break;
    case "mcp.call":
      await s.walk("agent2", "mcp");
      await s.fly(ITEM.ghCoin, "town", "agent2", "mcp");
      break;
    case "mcp.result":
      if (ev.data?.ok) {
        Sfx.door();
        s.chest.setFrame(ITEM.chestOpen);
        await s.pop("mcp", "TREASURE!", "#5fd36a");
      } else {
        Sfx.deny();
        await s.pop("mcp", "401", "#ff5a5a");
      }
      await s.walk("agent2");
      break;
    case "a2a.done":
      await s.fly(ITEM.chestOpen, "dungeon", "agent2", "agent1");
      await s.walk("user", "agent1");
      Sfx.win();
      await s.bigText("STAGE CLEAR!", "#5fd36a");
      break;
    case "a2a.exchange.error":
      Sfx.deny();
      await s.pop("keycloak", "EXPIRED", "#ff5a5a");
      await s.walk("agent1");
      break;
    default:
      if (ev.from && ev.to && ev.from !== ev.to && scene.actors[ev.from]) await s.walk(ev.from, ev.to);
  }
}

// ---------------- HTML panels ----------------

const TITLES = {
  "h2a.login.start": "LOGIN: OFF TO KEYCLOAK",
  "h2a.login.token": "KEY FORGED: USER TOKEN",
  "ask.start": "A QUEST FOR THE ORCHESTRATOR",
  "h2a.role.check": "ORCHESTRATOR CHECKS THE ROLE",
  "a2a.exchange.request": "TOKEN EXCHANGE (RFC 8693)",
  "a2a.exchange.token": "NEW KEY: OBO TOKEN",
  "a2a.call": "TO THE GATE OF THE REPO AGENT",
  "a2a.check.bearer": "GATE CHECK 1: BEARER",
  "a2a.check.jwt": "GATE CHECK 2: SIGNATURE, AUD, EXP",
  "a2a.check.caller": "GATE CHECK 3: CALLER ALLOWLIST",
  "a2a.check.scope": "GATE CHECK 4: SCOPE",
  "a2a.refused": "GAME OVER: 403",
  "mcp.token.lookup": "REPO AGENT CHECKS ITS TOKEN STORE",
  "consent.needed": "INPUT REQUIRED",
  "a2a.input_required": "BONUS STAGE: GITHUB CONSENT",
  "consent.ticket": "THE SIGNED TICKET",
  "mcp.probe": "KNOCK KNOCK: 401",
  "mcp.discover": "READ THE MAP (DISCOVERY)",
  "consent.authorize": "LOG IN AT GITHUB (NOT KEYCLOAK)",
  "consent.callback": "BACK WITH A ONE-TIME CODE",
  "consent.swap": "CODE SWAP (SERVER TO SERVER)",
  "consent.token": "GITHUB MINTS THE GITHUB TOKEN",
  "consent.stored": "TOKEN STORED. ASK AGAIN!",
  "mcp.call": "MCP TOOL CALL",
  "mcp.result": "THE VAULT OPENS",
  "a2a.done": "STAGE CLEAR!",
  "a2a.exchange.error": "EXCHANGE REFUSED",
};

const ENDINGS = {
  "a2a.done": (ev) =>
    "\n\nWHAT YOU SAW: one identity (sub) went all the way. The user token was for the orchestrator agent. " +
    "The OBO token was for the repository agent and named the orchestrator agent as the caller. The GitHub token was the user's own, " +
    "made by GitHub (not Keycloak) after the user agreed. " +
    "Each hop got a new token for exactly one audience.",
  "a2a.refused": () =>
    "\n\nWHAT YOU SAW: gates 1 to 3 passed. Bob is real, and the orchestrator agent really called. " +
    "Only gate 4 failed: Bob is not allowed. Identity (who) and authorisation (may they) are separate checks.",
};

function esc(v) {
  return String(v).replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
}
function show(v) {
  if (v == null) return "";
  if (typeof v === "number" && v > 1e9 && v < 1e11) return `${v} (${new Date(v * 1000).toLocaleTimeString()})`;
  return typeof v === "object" ? JSON.stringify(v) : String(v);
}

function tokenFor(ev) {
  if (ev.token) return ev.token;
  if (ev.step === "h2a.role.check") return tokens["User token"];
  if (ev.step.startsWith("a2a.check.")) return tokens["OBO token"];
  if (ev.step === "mcp.token.lookup" || ev.step === "mcp.result") return tokens["GitHub token"];
  return null;
}

// Which claim rows to light up for this step.
function highlights(ev) {
  const hl = {};
  if (ev.check) {
    const cls = ev.check.ok ? "hl-ok" : "hl-bad";
    ({ "realm_access.roles": ["realm_access.roles"], aud: ["aud", "exp", "iss"], scope: ["scope"],
       "azp / act.sub": ["azp", "act"] }[ev.check.claim] || []).forEach((k) => (hl[k] = cls));
  }
  if (ev.step === "a2a.exchange.token" && ev.data?.before?.claims) {
    const before = ev.data.before.claims;
    for (const [k, v] of Object.entries(ev.token.claims || {})) {
      if (JSON.stringify(before[k]) !== JSON.stringify(v)) hl[k] = "hl-new";
    }
  }
  return hl;
}

function renderCard(tok, hl = {}) {
  if (!tok) return;
  $("cardTitle").textContent = `TOKEN CARD: ${tok.label.toUpperCase()}`;
  if (tok.opaque) {
    $("card").innerHTML = `<table class="claims"><tr class="${hl.opaque || ""}"><td>value</td><td>${esc(tok.head)}</td></tr></table>` +
      `<p style="color:var(--dim);font-size:8px">Opaque token: not a JWT, so there are no claims to read. Only the MCP server can check it.</p>`;
    return;
  }
  const rows = Object.entries(tok.claims || {}).map(([k, v]) =>
    `<tr class="${hl[k] ? hl[k] + " flash" : ""}"><td>${esc(k)}</td><td>${esc(show(v))}</td></tr>`).join("");
  $("card").innerHTML = `<table class="claims">${rows}</table>` +
    `<details><summary>FULL DECODED JWT</summary><pre>${esc(JSON.stringify(tok.full, null, 2))}</pre></details>`;
}

function renderInventory() {
  $("inv").innerHTML = "";
  for (const label of Object.keys(tokens)) {
    const b = document.createElement("button");
    b.textContent = label.toUpperCase();
    b.onclick = () => renderCard(tokens[label]);
    $("inv").appendChild(b);
  }
}

function renderStep(ev) {
  const title = ev.step === "mcp.result" && !ev.data?.ok ? "THE VAULT STAYS SHUT"
    : TITLES[ev.step] || ev.step.toUpperCase();
  $("stepTitle").innerHTML = `<span class="leg ${esc(ev.leg)}">${esc(ev.leg)}</span>${esc(title)}`;
  let html = esc(ev.note || "");
  if (ev.check) {
    const c = ev.check;
    html += `<div class="check ${c.ok ? "ok" : "bad"}">${c.ok ? "PASS" : "FAIL"}: ${esc(c.name)}<br>` +
      `<span>need: ${esc(show(c.expected))}<br>got: ${esc(show(c.actual))}</span></div>`;
  }
  if (ev.http?.status) html += `<div class="check ${ev.http.status < 400 ? "ok" : "bad"}">HTTP ${esc(ev.http.status)}<br><span>${esc(ev.http.body || "")}</span></div>`;
  if (ev.data?.scope && ev.step === "a2a.exchange.request") {
    html += `<div class="check ${ev.data.scope.includes("github.act") ? "ok" : "bad"}">scope asked for:<br><span>${esc(ev.data.scope.join(" "))}</span></div>`;
  }
  if (ev.data?.ticket_url && ev.step === "a2a.input_required") {
    html += `\n\n<a href="${esc(ev.data.ticket_url)}" target="_blank" rel="noopener">&#9654; OPEN THE GITHUB CONSENT LINK</a>`;
  }
  if ((ev.step === "mcp.discover" || ev.step === "consent.authorize") && ev.data) {
    html += `<div class="check ok"><span>GitHub login server: ${esc(ev.data.authorization_server || "")}` +
      `<br>authorize: ${esc(ev.data.authorization_endpoint)}` +
      (ev.data.token_endpoint ? `<br>token: ${esc(ev.data.token_endpoint)}` : "") + `</span></div>`;
  }
  if (ev.step === "consent.swap" && ev.data) {
    html += `<div class="check ok"><span>POST ${esc(ev.data.token_endpoint)}<br>sends: ${esc(ev.data.sends.join(" + "))}</span></div>`;
  }
  if (ev.step === "a2a.done" && ev.data?.final_text) {
    html += `<details><summary>THE TREASURE (TOOL RESULT)</summary><pre>${esc(ev.data.final_text)}</pre></details>`;
  }
  if (ENDINGS[ev.step]) html += esc(ENDINGS[ev.step](ev));
  $("say").innerHTML = html;

  if (ev.token) { tokens[ev.token.label] = ev.token; renderInventory(); }
  if (ev.leg === "CONSENT" && !ev.token) {
    // No Keycloak token takes part in consent. Say what does, instead of
    // leaving the last OBO token on screen.
    $("cardTitle").textContent = "NO TOKEN IN PLAY: SIGNED TICKET";
    $("card").innerHTML =
      `<p>The consent leg does not use the Keycloak tokens. The repository agent links it to the right user ` +
      `with a <b>signed ticket</b>: the user's sub plus a one-time nonce, signed by the repository agent, valid 5 minutes.</p>` +
      `<p style="color:var(--dim)">The GitHub token comes from GitHub's login server, after the user logs in ` +
      `to GitHub and agrees. Keycloak never sees it.</p>`;
    return;
  }
  renderCard(tokenFor(ev), highlights(ev));
}

// ---------------- beat player ----------------

function updateQueue() {
  $("queue").textContent = queue.length ? `${queue.length} MORE STEP${queue.length > 1 ? "S" : ""} WAITING` : "";
}

function waitNext() {
  return new Promise((res) => {
    if (auto) { setTimeout(res, 2600); return; }
    $("next").disabled = false;
    $("next").classList.toggle("blink", queue.length > 0);
    waiting = res;
  });
}

async function pump() {
  if (busy || !scene) return;
  busy = true;
  while (queue.length) {
    const ev = queue.shift();
    $("next").disabled = true;
    $("next").classList.remove("blink");
    updateQueue();
    renderStep(ev);
    try { await animate(ev); } catch (e) { console.error("[arcade] animation", ev.step, e); }
    updateQueue();
    if (queue.length) await waitNext();
  }
  busy = false;
}

function next() {
  if (!waiting) return;
  Sfx.step();
  $("next").disabled = true;
  $("next").classList.remove("blink");
  const w = waiting; waiting = null; w();
}

// ---------------- controls + event stream ----------------

let loggedIn = false;
let asking = false;           // an /ask request is in flight

async function refreshWho() {
  try {
    const j = await (await fetch("/whoami")).json();
    loggedIn = j.logged_in;
    if (j.user) { player = j.user; $("user").value = j.user; }
    $("who").textContent = loggedIn ? `PLAYER: ${j.user.toUpperCase()}` : "NOT LOGGED IN";
    $("login").textContent = loggedIn ? "LOGOUT" : "INSERT COIN (LOGIN)";
    $("user").disabled = loggedIn;
    $("ask").disabled = !loggedIn || asking;
    if (scene && !busy) scene.actors.user.setFrame(CHAR[player] ?? CHAR.alice);
  } catch (e) { /* orchestrator agent restarting; try again on the next tick */ }
}

function connect() {
  const es = new EventSource(`/events?since=${lastSeq}`);
  es.addEventListener("auth", (m) => {
    const ev = JSON.parse(m.data);
    if (ev.seq <= lastSeq) return;
    lastSeq = ev.seq;
    queue.push(ev);
    updateQueue();
    pump();
  });
  es.onerror = () => { es.close(); setTimeout(connect, 2000); };
}

$("login").onclick = () => {
  location.href = loggedIn ? "/logout?next=arcade"
    : `/login?next=arcade&user=${encodeURIComponent($("user").value)}`;
};
$("user").onchange = () => { player = $("user").value; if (scene) scene.actors.user.setFrame(CHAR[player]); };
$("ask").onclick = async () => {
  asking = true;
  $("ask").disabled = true;
  try {
    const r = await fetch("/ask", {
      method: "POST",
      headers: { "content-type": "application/x-www-form-urlencoded" },
      body: "prompt=" + encodeURIComponent($("prompt").value),
    });
    if (r.status === 401) await refreshWho();
  } finally {
    asking = false;
    $("ask").disabled = !loggedIn;
  }
};
$("next").onclick = next;
$("auto").onclick = () => {
  auto = !auto;
  $("auto").textContent = `AUTO: ${auto ? "ON" : "OFF"}`;
  $("auto").classList.toggle("on", auto);
  if (auto) next();
};
const showSound = (on) => {
  $("sound").textContent = `SOUND: ${on ? "ON" : "OFF"}`;
  $("sound").classList.toggle("on", on);
};
$("sound").onclick = () => showSound(Sfx.toggle());
showSound(Sfx.isOn()); // remembered across the Keycloak login/logout reload
$("reset").onclick = async () => {
  await fetch("/events/clear", { method: "POST" });
  queue.length = 0;
  for (const k of Object.keys(tokens)) delete tokens[k];
  renderInventory();
  $("card").innerHTML = '<span style="color:var(--dim)">No token yet.</span>';
  $("cardTitle").textContent = "TOKEN CARD";
  $("stepTitle").textContent = loggedIn ? "PRESS ASK ORCHESTRATOR" : "PRESS INSERT COIN TO START";
  $("say").textContent = "New game. The next steps you trigger will show up here.";
  scene?.resetStage(); scene?.setBanner("");
  next();
};
document.addEventListener("keydown", (e) => {
  if ((e.code === "Space" || e.code === "Enter") && !["INPUT", "SELECT", "BUTTON"].includes(e.target.tagName)) {
    e.preventDefault(); next();
  }
});

// Wait for the pixel font so canvas text renders in it, then start.
document.fonts.load('8px "Press Start 2P"').finally(async () => {
  await refreshWho();
  new Phaser.Game({
    type: Phaser.AUTO, parent: "stage", width: W, height: H, pixelArt: true,
    backgroundColor: "#000000", scene: Town,
    scale: { mode: Phaser.Scale.FIT, autoCenter: Phaser.Scale.CENTER_BOTH },
    callbacks: { postBoot: () => { connect(); pump(); } },
  });
  setInterval(refreshWho, 5000);
});
