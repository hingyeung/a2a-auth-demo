// Tiny 8-bit sound effects, made with WebAudio square waves. No sound files.
// Muted until the player turns sound on. The choice is remembered in
// localStorage because login and logout go through Keycloak, which reloads
// the page and throws away the old AudioContext. Browsers only let a context
// run after a user gesture, so we (re)arm it on every click or key press.
const Sfx = (() => {
  const KEY = "arcade.sound";
  const VOL = 0.15;
  let ctx = null;
  let master = null;
  let on = false;

  try { on = localStorage.getItem(KEY) === "on"; } catch { /* storage blocked */ }

  function save() {
    try { localStorage.setItem(KEY, on ? "on" : "off"); } catch { /* storage blocked */ }
  }

  // Create the context if needed and wake it up if the browser suspended it.
  // Must be called from a user gesture to be sure it works.
  function arm() {
    if (!on) return;
    try {
      if (!ctx) {
        const AC = window.AudioContext || window.webkitAudioContext;
        if (!AC) return;
        ctx = new AC();
        master = ctx.createGain();
        master.gain.value = VOL;
        master.connect(ctx.destination);
      }
      if (ctx.state !== "running") ctx.resume().catch(() => {});
    } catch { /* no audio available */ }
  }

  // Any later gesture re-arms the context (first gesture after a reload, or a
  // context the browser suspended while the tab was in the background).
  for (const ev of ["pointerdown", "keydown", "touchend"]) {
    document.addEventListener(ev, arm, { capture: true, passive: true });
  }
  // The page may still hold a user activation from the redirect, so try now.
  arm();

  function tone(freq, start, dur, type) {
    const t = ctx.currentTime + start;
    const o = ctx.createOscillator();
    const g = ctx.createGain();
    o.type = type;
    o.frequency.setValueAtTime(freq, t);
    g.gain.setValueAtTime(0, ctx.currentTime);
    g.gain.setValueAtTime(1, t);
    g.gain.exponentialRampToValueAtTime(0.001, t + dur);
    o.connect(g).connect(master);
    o.start(t);
    o.stop(t + dur + 0.02);
  }

  function seq(notes, step = 0.08, type = "square") {
    if (!on) return;
    if (!ctx || ctx.state !== "running") {
      // Not allowed to play yet. Try to wake it, and skip this sound rather
      // than queueing it up to burst out later.
      arm();
      if (!ctx || ctx.state !== "running") return;
    }
    notes.forEach((f, i) => f && tone(f, i * step, step * 0.95, type));
  }

  return {
    isOn: () => on,
    toggle() {
      on = !on;
      save();
      arm();
      if (on && ctx) {
        // Confirm blip. If resume() is still pending, play once it settles.
        const blip = () => seq([523, 784], 0.06);
        ctx.state === "running" ? blip() : ctx.resume().then(blip, () => {});
      }
      return on;
    },
    step: () => seq([660], 0.04),
    walk: () => seq([220, 0, 247], 0.05, "triangle"),
    mint: () => seq([523, 659, 784, 1047], 0.07),
    pass: () => seq([784, 1047], 0.07),
    deny: () => seq([140, 110, 90], 0.14, "sawtooth"),
    door: () => seq([196, 262, 330], 0.09),
    win: () => seq([523, 659, 784, 1047, 0, 784, 1047], 0.1),
    over: () => seq([392, 330, 262, 196, 0, 131], 0.16, "triangle"),
  };
})();
