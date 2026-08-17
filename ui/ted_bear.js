/* ted_bear.js — Ted's companion, drawn once and reused everywhere.
 *
 * Ted is named after a teddy bear, so this is a plain old plush bear: honey
 * body, tan muzzle and belly, bead eyes, triangular nose, small friendly
 * mouth. No clothes, no accessories, no glossy anime eyes, no orbit ring.
 *
 * WHY THIS IS A SHARED FILE. The bear appears in two places — the chat header
 * and the floating desk-pet window — and a sprite copied into both would drift
 * the first time one was touched. pywebview serves the ui/ directory over a
 * local HTTP server, so both pages load this same file. Verified, not assumed.
 *
 * Everything is fillRect on an integer grid. No images, no animation library,
 * nothing to fetch: the bear must render instantly and work offline.
 */
(function (global) {
  'use strict';

  /* The five states a caller may ask for. "responding" is Ted producing the
     reply, which is visibly different from "thinking" about it. */
  var STATES = ['idle', 'thinking', 'responding', 'success', 'error'];

  var GRID = 20;

  /* Deliberately small. A limited palette is what makes pixel art read as
     pixel art rather than as a small photograph. */
  var PALETTE = {
    O: '#6b4423',   /* outline — dark brown                    */
    B: '#d19a53',   /* body — warm honey/golden brown           */
    S: '#a97434',   /* shadow — slightly darker than the body   */
    T: '#f0d5ab',   /* tan — muzzle and belly                   */
    D: '#2e1c0e'    /* features — eyes, nose, mouth             */
  };

  var SPARKLE = '#16dede';   /* cyan, and ONLY for active AI states */

  /* Chunky old-fashioned plush proportions: big head, short limbs, low centre
     of gravity. Rows are exactly GRID wide and validated at load. */
  var BEAR = [
    '....OO........OO....',
    '...OBBO......OBBO...',
    '..OOBBOOOOOOOOBBOO..',
    '..OBBBBBBBBBBBBBBO..',
    '.OBBBBBBBBBBBBBBBBO.',
    '.OBBBBBBBBBBBBBBBSO.',
    '.OBBBDBBBBBBBBDBBSO.',
    '.OBBBBBBBBBBBBBBBSO.',
    '.OBBBTTTDDDDTTTBBSO.',
    '.OBBTTTTTDDTTTTTBSO.',
    '.OBBTTTTTTTTTTTTBSO.',
    '.OBBTTTDTTTTDTTTBSO.',
    '..OBBTTTTDDTTTTBBO..',
    '...OOBBBBBBBBBBOO...',
    '....OOBBBBBBBBOO....',
    '..OOOBBBTTTTBBBOOO..',
    '.OBBBOBBTTTTBBOBBBO.',
    '.OBBBOBBTTTTBBOBBBO.',
    '.OBSBOOBBBBBBOOBSBO.',
    '..OOO..OOOOOO..OOO..'
  ];

  BEAR.forEach(function (row, i) {
    if (row.length !== GRID) {
      console.error('ted_bear: row ' + i + ' is ' + row.length + ', expected ' + GRID);
    }
  });

  /* Where the face lives, so expressions can be drawn over the body instead of
     duplicating the whole sprite once per mood. */
  /* All of these are symmetric about column 9.5, which is what stops the face
     drifting off-centre when an expression is redrawn. */
  var EYE_ROW = 6, EYE_L = 5, EYE_R = 14;
  var MOUTH = { cornerRow: 11, centreRow: 12, left: 7, right: 12,
                midL: 9, midR: 10 };

  function clampState(s) {
    return STATES.indexOf(s) === -1 ? 'idle' : s;
  }

  /* ── one mounted bear ─────────────────────────────────────────────────── */
  function mount(canvas, options) {
    options = options || {};
    var scale = Math.max(1, Math.round(options.scale || 2));
    var state = clampState(options.state);
    /* A long idle is a flavour of idle, not a sixth state: the caller's
       contract stays the five documented ones, while the desk pet can still
       look sleepy after a few quiet minutes. */
    var longIdle = false;
    var frame = 0;
    var blinkAt = 60 + Math.floor(Math.random() * 120);
    var sparks = [];
    var revertTo = 'idle';
    var revertTimer = null;
    var raf = null;
    var alive = true;
    var ctx = canvas.getContext('2d');

    function applySize() {
      canvas.width = GRID * scale;
      canvas.height = GRID * scale;
      canvas.style.width = (GRID * scale) + 'px';
      canvas.style.height = (GRID * scale) + 'px';
      /* Backing store and CSS size are kept identical on purpose. A 20-cell
         buffer stretched to an arbitrary CSS box turns the bear into a smear,
         which is exactly the bug this project already hit once. */
      ctx.imageSmoothingEnabled = false;
    }
    applySize();

    function px(cx, cy, colour, w, h) {
      ctx.fillStyle = colour;
      ctx.fillRect(Math.round(cx * scale), Math.round(cy * scale),
                   (w || 1) * scale, (h || 1) * scale);
    }

    /* Each state returns only how the body is PLACED, so the sprite itself is
       never duplicated per mood. Adding a state means a case here, not another
       twenty rows of pixels. */
    function motion() {
      var t = frame;
      if (state === 'thinking') {
        /* A slow lean, as if considering. Whole pixels only — half a pixel on
           a pixel-art sprite is just blur. */
        return { dx: Math.round(Math.sin(t / 26) * 1.2), dy: 0, squash: 0 };
      }
      if (state === 'responding') {
        /* A small quick nod, the rhythm of talking. */
        return { dx: 0, dy: Math.round(Math.sin(t / 7)), squash: 0 };
      }
      if (state === 'success') {
        var hop = Math.sin(t / 5);
        return { dx: 0, dy: -Math.abs(Math.round(hop * 2)),
                 squash: hop > 0.92 ? 1 : 0 };
      }
      if (state === 'error') {
        /* A short shiver that settles, rather than a permanent sad pose. */
        var shake = frame < 26 ? Math.round(Math.sin(t / 1.6)) : 0;
        return { dx: shake, dy: 1, squash: 0 };
      }
      if (longIdle) {
        return { dx: 0, dy: 1 + Math.round(Math.sin(t / 60) * 0.5), squash: 0 };
      }
      return { dx: 0, dy: Math.round(Math.sin(t / 30) * 0.9), squash: 0 };
    }

    function drawBody(m) {
      for (var r = 0; r < BEAR.length; r++) {
        var row = BEAR[r];
        for (var c = 0; c < row.length; c++) {
          var key = row[c];
          if (key === '.') continue;
          /* Squash flattens the top half at the peak of a bounce. That sells a
             hop far more than the height of the hop does. */
          var y = r + m.dy + (m.squash && r < 10 ? 1 : 0);
          px(c + m.dx, y, PALETTE[key]);
        }
      }
    }

    /* The face is drawn over the body so an expression can change without a
       second sprite. Only the eyes and mouth ever move. */
    function drawFace(m) {
      var y = EYE_ROW + m.dy;
      var blinking = (frame - blinkAt >= 0 && frame - blinkAt < 5);

      [EYE_L, EYE_R].forEach(function (ex) {
        var x = ex + m.dx;
        if (state === 'success') {
          /* Happy eyes: a shallow inverted v. */
          px(x, y, PALETTE.D);
          px(x - 1, y - 1, PALETTE.D);
          px(x + 1, y - 1, PALETTE.D);
        } else if (state === 'error') {
          px(x, y, PALETTE.D);
          px(x, y - 1, PALETTE.D);          /* wide, startled */
        } else if (state === 'thinking') {
          px(x, y - 1, PALETTE.D);          /* looking up */
        } else if (blinking || (longIdle && state === 'idle')) {
          px(x - 1, y, PALETTE.D, 3, 1);    /* closed / heavy-lidded */
        } else {
          px(x, y, PALETTE.D);              /* the bead eye */
        }
      });

      /* Mouth. The sprite already carries a neutral smile, so only the states
         that genuinely differ redraw it — and each one paints tan back over
         the default before drawing, or the two mouths overlap into a smudge. */
      function clearMouth() {
        px(MOUTH.left + m.dx, MOUTH.cornerRow + m.dy, PALETTE.T, 6, 1);
        px(MOUTH.midL + m.dx, MOUTH.centreRow + m.dy, PALETTE.T, 2, 1);
      }
      if (state === 'success') {
        /* Open and pleased: a small round mouth. */
        clearMouth();
        px(MOUTH.midL + m.dx, MOUTH.cornerRow + m.dy, PALETTE.D, 2, 2);
      } else if (state === 'error') {
        /* A flat line — concerned, not miserable. */
        clearMouth();
        px(MOUTH.midL - 1 + m.dx, MOUTH.cornerRow + 1 + m.dy, PALETTE.D, 4, 1);
      } else if (state === 'responding') {
        /* Opens and closes while Ted is producing words. */
        clearMouth();
        if (Math.sin(frame / 6) > 0) {
          px(MOUTH.midL + m.dx, MOUTH.cornerRow + m.dy, PALETTE.D, 2, 2);
        } else {
          px(MOUTH.left + m.dx, MOUTH.cornerRow + m.dy, PALETTE.D);
          px(MOUTH.right + m.dx, MOUTH.cornerRow + m.dy, PALETTE.D);
          px(MOUTH.midL + m.dx, MOUTH.centreRow + m.dy, PALETTE.D, 2, 1);
        }
      }
    }

    /* Cyan is reserved for the states where Ted is actually working, and each
       gets exactly one cyan device so they stay distinguishable: dots above
       the head for thinking, sparkles for success. "responding" deliberately
       has none — it already reads through the nod and the moving mouth, and a
       third cyan effect would just make the bear busy. */
    function drawSparkles() {
      if (state !== 'success') return;
      if (frame % 6 === 0 && sparks.length < 8) {
        sparks.push({ x: 2 + Math.random() * 16, y: 3 + Math.random() * 7,
                      life: 14 });
      }
      sparks = sparks.filter(function (s) { return --s.life > 0; });
      sparks.forEach(function (s) {
        /* Blink out near the end. A one-pixel dot that simply vanishes reads
           as a rendering glitch. */
        if (s.life < 5 && s.life % 2 === 0) return;
        px(s.x, s.y - (14 - s.life) * 0.16, SPARKLE);
      });
    }

    function drawThinkingDots() {
      if (state !== 'thinking') return;
      var lit = Math.floor(frame / 15) % 4;
      for (var i = 0; i < 3; i++) {
        if (i >= lit) continue;
        px(8 + i * 2, 0.5 + Math.sin((frame + i * 8) / 11) * 0.4, SPARKLE);
      }
    }

    function draw() {
      if (!alive) return;
      ctx.clearRect(0, 0, canvas.width, canvas.height);
      var m = motion();
      drawThinkingDots();
      drawBody(m);
      drawFace(m);
      drawSparkles();
      if (frame >= blinkAt + 5) {
        blinkAt = frame + 90 + Math.floor(Math.random() * 200);
      }
      frame++;
      raf = global.requestAnimationFrame(draw);
    }

    var api = {
      /* The whole public contract. `hold` makes the state momentary, decaying
         back to whatever Ted is actually doing — so a finished tool call
         cannot leave the bear celebrating for the rest of the evening. */
      setState: function (next, hold) {
        next = clampState(next);
        if (next === 'success' || next === 'error') sparks = [];
        if (next !== state) frame = 0;
        state = next;
        global.clearTimeout(revertTimer);
        if (hold) {
          revertTimer = global.setTimeout(function () {
            state = revertTo;
            frame = 0;
          }, hold);
        } else {
          revertTo = next;
        }
        return api;
      },
      getState: function () { return state; },
      /* Long idle is a look, not a state, so it never appears in the contract
         above and never surprises a caller reading getState(). */
      setLongIdle: function (on) { longIdle = !!on; return api; },
      setScale: function (n) {
        scale = Math.max(1, Math.round(n || 2));
        applySize();
        return api;
      },
      destroy: function () {
        alive = false;
        global.clearTimeout(revertTimer);
        if (raf) global.cancelAnimationFrame(raf);
      }
    };

    draw();
    return api;
  }

  global.TedBear = {
    STATES: STATES,
    GRID: GRID,
    PALETTE: PALETTE,
    SPRITE: BEAR,
    mount: mount
  };
})(window);
