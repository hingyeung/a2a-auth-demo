// Tiny 8-bit sound effects, made with WebAudio square waves. No sound files.
// Muted until the player turns sound on (browsers also need a click first).
const Sfx = (() => {
  let ctx = null;
  let on = false;

  function tone(freq, start, dur, type = "square", vol = 0.06) {
    const o = ctx.createOscillator();
    const g = ctx.createGain();
    o.type = type;
    o.frequency.setValueAtTime(freq, ctx.currentTime + start);
    g.gain.setValueAtTime(vol, ctx.currentTime + start);
    g.gain.exponentialRampToValueAtTime(0.0001, ctx.currentTime + start + dur);
    o.connect(g).connect(ctx.destination);
    o.start(ctx.currentTime + start);
    o.stop(ctx.currentTime + start + dur + 0.02);
  }

  function seq(notes, step = 0.08, type) {
    if (!on || !ctx) return;
    notes.forEach((f, i) => f && tone(f, i * step, step * 0.95, type));
  }

  return {
    toggle() {
      on = !on;
      if (on && !ctx) ctx = new (window.AudioContext || window.webkitAudioContext)();
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
