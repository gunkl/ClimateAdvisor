/* Issue #1230 R5/R6fe: the "Tonight in the bedroom" card as a self-contained component.
   Status mounts it on the live night plan (GET /api/climate_advisor/night_strategy, R4's NightStrategy.to_dict()
   with temperatures in the display unit and `unit` added), turned into the card's shape by
   NDNightCard.fromStrategy(payload). nd_night_sample.json (the approved Night mock) stays as a labelled fallback.
   It uses nothing from the rest of the page except the CSS tokens (--panel --ink --ink2 --muted --rule --chip --band
   --c1..--c5 --warn --warnbg) and the plan-list classes of chart_v2.css (plan-row, plan-time, plan-actor, ...).

     NDNightCard.mount(containerElement, data, { unit: 'fahrenheit' | 'celsius' }) -> { update(data) }
     NDNightCard.fromStrategy(payload) -> data

   With data.show_numbers === false (the zone's bedroom model is below medium confidence; there is no accuracy gate) the card
   shows the recommended plan and its steps only: no predicted temperatures, chart or comparison table.

   data = { note?: string, nights: [ { title, t: [iso...], out: [degF...], floor, ceiling, label?,
            rows: [ { key, label, bed: [degF...], bed_min, bed_max, bed_wake, miss_dh, heat_min, cool_min,
                      cost, tags, best } ] } ] }                 (all temperatures internal degF) */
(function () {
  'use strict';
  var COL = ['var(--c1)', 'var(--c2)', 'var(--c3)', 'var(--c4)', 'var(--c5)'];
  var DASH = ['', '6 3', '2 3', '8 3 2 3', '10 4'];

  // Issue #1341: every sentence is a catalog message (dashboard.night.*); the plan's own prose (labels, headline, notes)
  // is the API's `<field>_msg` wire message when sent, else the prose field it still carries (CAI18N.text).
  var I = window.CAI18N;
  var N = 'dashboard.night.';
  function esc(s) { return I.esc(s); }
  function t(key, params) { return I.t(N + key, params); }
  // plain sentence text -> HTML with each number in <bdi> (the same rule as iso() in nd_core.js; this card also runs standalone)
  var NUM_TOKEN = /[+\u2212-]?\p{Nd}+(?:[.,:]\p{Nd}+)*(?:\s?[AaPp]\.?[Mm]\.?)?(?:\s?°\s?[CF])?(?:\s?[–-]\s?\p{Nd}+(?:[.,]\p{Nd}+)*(?:\s?°\s?[CF])?)?%?/gu;
  function iso(text) {
    text = String(text == null ? '' : text);
    var out = '', last = 0, m;
    NUM_TOKEN.lastIndex = 0;
    while ((m = NUM_TOKEN.exec(text))) { out += esc(text.slice(last, m.index)) + '<bdi>' + esc(m[0]) + '</bdi>'; last = m.index + m[0].length; }
    return out + esc(text.slice(last));
  }
  function n1(v) { return I.fmt.number(v, { minimumFractionDigits: 1, maximumFractionDigits: 1, useGrouping: false }); }
  function n0(v) { return I.fmt.number(v, { minimumFractionDigits: 0, maximumFractionDigits: 0, useGrouping: false }); }

  // ---------- live payload -> card data ----------
  function resample(path, grid) { // [[iso, temp]] onto the grid's timestamps (linear; ends held)
    var pts = (path || []).map(function (p) { return [new Date(p[0]).getTime(), p[1]]; });
    return grid.map(function (g) {
      var x = new Date(g).getTime();
      if (!pts.length) return null;
      if (x <= pts[0][0]) return pts[0][1];
      for (var i = 1; i < pts.length; i++) {
        if (x <= pts[i][0]) {
          var a = pts[i - 1], b = pts[i];
          return b[0] === a[0] ? b[1] : a[1] + (b[1] - a[1]) * (x - a[0]) / (b[0] - a[0]);
        }
      }
      return pts[pts.length - 1][1];
    });
  }

  function fromStrategy(p) {
    var celsius = /c/i.test((p && p.unit) || '');
    function F(v) { return v == null ? null : (celsius ? v * 9 / 5 + 32 : v); }   // card values are internal degF
    function dF(v) { return v == null ? null : (celsius ? v * 9 / 5 : v); }
    var opts = (p && p.options) || [], grid = [];
    opts.some(function (o) { if (o.path && o.path.length) { grid = o.path.map(function (pt) { return pt[0]; }); return true; } return false; });
    var rows = opts.map(function (o) {
      var steps = o.n_actions || 0;
      var tags = [steps === 0 ? t('tag_nothing_to_do') : t('tag_steps', { n: steps })];
      if (o.runs === false) tags.push(t('tag_would_not_run'));
      tags.push(o.confidence === 'low' ? t('may_help') : t('tag_confidence', { level: o.confidence || 'medium' }));
      return {
        key: o.key, label: I.text(o, 'label'), bed: grid.length ? resample(o.path, grid).map(F) : [], // figures are null while the gate has not passed: never 0
        bed_min: F(o.bedroom_low_f), bed_max: F(o.bedroom_high_f), bed_wake: F(o.bedroom_wake_f),
        miss_dh: dF(o.discomfort_fh), heat_min: o.heat_min, cool_min: o.cool_min, cost: o.score,
        tags: tags.join(t('tag_separator')), best: o.key === p.recommended, actions: o.actions || [], confidence: o.confidence, note: I.text(o, 'note')
      };
    });
    if (rows.length && !rows.some(function (r) { return r.best; })) rows[0].best = true;
    return {
      live: true, source_note: p ? I.text(p, 'source_note') : '', show_numbers: !p || p.show_numbers !== false, unit: p && p.unit, headline: p ? I.text(p, 'headline') : '',
      tradeoffs: ((p && p.tradeoffs) || []).map(function (x) { return { text: I.text(x, 'text') }; }),
      nights: rows.length ? [{ title: t('title_tonight'), t: grid, out: null, floor: F(p.floor_f), ceiling: F(p.ceiling_f), start: p.start, end: p.end, rows: rows }] : []
    };
  }

  var ACTION_TEXT = {
    windows_open: 'act_windows_open', windows_close: 'act_windows_close', fan_on: 'act_fan_on', fan_off: 'act_fan_off'
  };
  function clock(iso) { var d = new Date(iso); return isNaN(d) ? '' : I.fmt.time(d); }
  function stepsHtml(row) { // one thing to do, at a time
    var acts = (row && row.actions) || [];
    if (!acts.length) return '<div class="note" id="night-steps">' + esc(t('steps_none')) + '</div>';
    var low = row.confidence === 'low';
    return '<ol class="plan-list" id="night-steps" aria-label="' + esc(t('steps_label')) + '">' + acts.map(function (a) {
      var you = a.actor !== 'ca';
      // each step is one whole sentence; a kind this card does not know is shown as its code, spaced out
      var text = a.through_night ? t('act_through_night')
        : ACTION_TEXT[a.kind] ? t(ACTION_TEXT[a.kind] + (a.predicted ? '_predicted' : ''))
        : String(a.kind || '').replace(/_/g, ' ');
      return '<li class="plan-row ' + (you ? 'plan-you' : 'plan-ca') + '"><span class="plan-time">' + iso(a.predicted ? t('about_time', { time: clock(a.ts) }) : clock(a.ts)) + '</span>' +
        '<span class="plan-actor">' + esc(t(you ? 'actor_you' : 'actor_ca')) + '</span><span class="plan-text">' + esc(text) +
        (low ? ' <span class="plan-qual">' + esc(t('may_help')) + '</span>' : '') + '</span></li>';
    }).join('') + '</ol>';
  }

  function mount(el, data, opts) {
    var celsius = !!(opts && /c/i.test(opts.unit || ''));
    var state = { ni: 0, off: {} };
    function D(f) { return celsius ? (f - 32) * 5 / 9 : f; }
    function d1(f) { return n1(D(f)); }
    function upper(s) { return String(s == null ? '' : s).toLocaleUpperCase(I.lang); }
    function sym() { return celsius ? '°C' : '°F'; }

    function render() {
      var nights = (data && data.nights) || [];
      if (!nights.length) { el.innerHTML = '<div class="empty">' + esc(t('empty')) + '</div>'; return; }
      if (state.ni >= nights.length) state.ni = 0;
      var N = nights[state.ni], rows = N.rows || [];
      var best = rows.filter(function (r) { return r.best; })[0] || rows[0];
      if (data.show_numbers === false || !(N.t && N.t.length > 1) || best.bed_min == null) { // plan only: no predicted figures (decision C11)
        el.innerHTML = '<div class="rec"><div class="sub">' + esc(t('recommended_for', { title: upper(N.title || t('title_tonight')) })) + '</div>' +
          '<div style="font-size:20px;font-weight:650;margin-top:2px">' + esc(best.label) + '</div>' +
          (data.headline ? '<div class="note" id="night-headline" style="margin-top:4px">' + esc(data.headline) + '</div>' : '') + '</div>' + stepsHtml(best) +
          '<div class="sub" id="night-nonumbers">' + esc(t('no_numbers')) + '</div>';
        return;
      }
      var W = 1000, H = 260, off = state.off; // the chart is drawn left to right (time runs that way) in every language
      var all = [];
      rows.forEach(function (r) { all = all.concat(r.bed); });
      all = all.concat(N.out || [], [N.floor - 2, N.ceiling + 1]).map(D);
      var lo = Math.floor(Math.min.apply(null, all)) - 1, hi = Math.ceil(Math.max.apply(null, all)) + 1;
      var ts = N.t.map(function (s) { return new Date(s).getTime(); }), t0 = ts[0], t1 = ts[ts.length - 1];
      function Y(v) { return H - ((v - lo) / (hi - lo)) * H; }
      function X(t) { return ((t - t0) / (t1 - t0)) * W; }
      function path(vs) { return vs.map(function (v, i) { return (i ? 'L' : 'M') + X(ts[i]).toFixed(1) + ' ' + Y(D(v)).toFixed(1); }).join(''); }

      var h = '';
      if (nights.length > 1) {
        h += '<div class="row" role="group" aria-label="' + esc(t('which_night')) + '">' + nights.map(function (n, i) {
          return '<button type="button" class="pill" data-night="' + i + '" aria-pressed="' + (i === state.ni) + '">' +
            esc(n.label || (i === 0 ? t('title_tonight') : n.title)) + '</button>';
        }).join('') + '</div>';
      }
      if (best) {
        h += '<div class="rec"><div class="sub">' + esc(t('recommended_for', { title: upper(N.title || t('title_tonight')) })) + '</div>' +
          '<div style="font-size:20px;font-weight:650;margin-top:2px">' + esc(best.label) + '</div>' +
          '<div class="note" style="margin-top:4px">' + (data.source_note ? '<span class="badge" id="night-source">' + esc(t('from_v4')) + '</span> ' : '') +
          iso(t('summary', { lo: d1(best.bed_min), hi: d1(best.bed_max), floor: n0(D(N.floor)), ceiling: n0(D(N.ceiling)), minutes: (best.heat_min || 0) + (best.cool_min || 0) })) + '</div>' +
          (data.headline ? '<div class="note" id="night-headline" style="margin-top:4px">' + esc(data.headline) + '</div>' : '') + '</div>' + stepsHtml(best);
      }
      h += '<div class="row"><span class="sub">' + esc(t('under_each_option')) + '</span>' + rows.map(function (r, i) {
        return '<button type="button" class="pill" data-opt="' + esc(r.key) + '" aria-pressed="' + (!off[r.key]) + '">' +
          '<svg width="22" height="10" aria-hidden="true"><line x1="1" y1="5" x2="21" y2="5" stroke="' + COL[i % 5] + '" stroke-width="2.5" stroke-dasharray="' + DASH[i % 5] + '" stroke-linecap="round"></line></svg>' +
          esc(r.label) + '</button>';
      }).join('') + '</div>';

      var yt = '', grid = '';
      var step = celsius ? 1 : 2;
      for (var v = Math.ceil(lo / step) * step; v <= hi; v += step) {
        yt += '<div style="position:absolute;inset-inline-end:2px;top:' + (Y(v) / H * 100) + '%;transform:translateY(-50%)"><bdi>' + esc(n0(v)) + '°</bdi></div>';
        grid += '<line x1="0" x2="' + W + '" y1="' + Y(v) + '" y2="' + Y(v) + '" stroke="var(--rule)" stroke-width="1" vector-effect="non-scaling-stroke"></line>';
      }
      var xt = '';
      for (var i = 0; i < ts.length; i += 8) {
        xt += '<div style="position:absolute;top:1px;left:' + (X(ts[i]) / W * 100) + '%;transform:translateX(-50%);white-space:nowrap">' +
          '<bdi>' + esc(I.fmt.hour(new Date(ts[i]))) + '</bdi></div>';
      }
      var lines = rows.map(function (r, i) {
        return '<path d="' + path(r.bed) + '" fill="none" stroke="' + COL[i % 5] + '" stroke-width="' + (r.best ? 3 : 2) + '" stroke-dasharray="' + DASH[i % 5] +
          '" vector-effect="non-scaling-stroke" opacity="' + (off[r.key] ? 0 : 1) + '"></path>';
      }).join('');
      var hasOut = !!(N.out && N.out.length);
      var outLo = hasOut ? n0(Math.min.apply(null, N.out.map(D))) : '', outHi = hasOut ? n0(Math.max.apply(null, N.out.map(D))) : '';
      var label = best ? t('chart_label_best', { label: best.label, lo: d1(best.bed_min), hi: d1(best.bed_max) }) : t('chart_label');
      h += '<div dir="ltr" style="display:flex;gap:8px"><div style="position:relative;width:30px;height:' + H + 'px;font-size:11px;color:var(--muted)" aria-hidden="true">' + yt + '</div>' +
        '<div style="position:relative;flex:1;min-width:0;height:' + H + 'px">' +
        '<svg viewBox="0 0 ' + W + ' ' + H + '" preserveAspectRatio="none" style="position:absolute;inset:0;width:100%;height:100%" role="img" aria-label="' + esc(label) + '">' + grid +
        '<rect x="0" y="' + Y(D(N.ceiling)) + '" width="' + W + '" height="' + (Y(D(N.floor)) - Y(D(N.ceiling))) + '" fill="var(--band)"></rect>' +
        (hasOut ? '<path d="' + path(N.out) + '" fill="none" stroke="var(--muted)" stroke-width="1.5" stroke-dasharray="2 3" vector-effect="non-scaling-stroke"></path>' : '') + lines + '</svg>' +
        '<div style="position:absolute;inset-inline-end:4px;top:2px;font-size:11px;color:var(--muted)">' + iso(hasOut ? t('legend_band_outdoor', { lo: outLo, hi: outHi }) : t('legend_band')) + '</div></div></div>' +
        '<div dir="ltr" style="display:flex;gap:8px"><div style="width:30px"></div><div style="position:relative;flex:1;min-width:0;height:16px;font-size:11px;color:var(--muted)" aria-hidden="true">' + xt + '</div></div>';

      var dash = '—';
      function mins(n) { return n ? I.t('common.duration.min', { n: n }) : dash; }
      h += '<div class="scroll-x"><table style="min-width:640px"><caption class="sr-only">' + esc(t('table_caption')) + '</caption><thead><tr><th>' + esc(t('col_option')) + '</th><th>' + esc(t('col_low_high')) +
        '</th><th>' + esc(t('col_at_wake')) + '</th><th>' + esc(t('col_outside_comfort')) + '</th><th>' + esc(t('col_heat')) + '</th><th>' + esc(t('col_ac')) + '</th><th>' + esc(t('col_score')) + '</th><th></th></tr></thead><tbody>' +
        rows.map(function (r) {
          return '<tr' + (r.best ? ' style="font-weight:600"' : '') + '><td>' + esc(r.label) + '</td><td class="num"><bdi>' + d1(r.bed_min) + '–' + d1(r.bed_max) + '°</bdi></td><td class="num"><bdi>' + d1(r.bed_wake) + '°</bdi></td>' +
            '<td class="num">' + (r.miss_dh ? '<bdi>' + esc(t('degree_hours', { value: celsius ? n1(r.miss_dh * 5 / 9) : I.fmt.number(r.miss_dh, { useGrouping: false }), unit: sym() })) + '</bdi>' : dash) + '</td>' +
            '<td class="num"><bdi>' + esc(mins(r.heat_min)) + '</bdi></td><td class="num"><bdi>' + esc(mins(r.cool_min)) + '</bdi></td>' +
            '<td class="num"><bdi>' + esc(I.fmt.number(Number(r.cost), { minimumFractionDigits: 2, maximumFractionDigits: 2, useGrouping: false })) + '</bdi></td><td style="color:var(--ink2)">' + esc(r.tags || '') + '</td></tr>';
        }).join('') + '</tbody></table></div>' +
        '<div class="sub">' + esc(t('score_note')) + '</div>';
      if (data.tradeoffs && data.tradeoffs.length) {
        h += '<ul class="note" id="night-tradeoffs" aria-label="' + esc(t('tradeoffs_label')) + '" style="margin:0;padding-inline-start:18px">' + data.tradeoffs.map(function (x) { return '<li>' + esc(x.text) + '</li>'; }).join('') + '</ul>';
      }
      if (data.source_note) h += '<div class="sub" id="night-source-note">' + esc(data.source_note) + '</div>';
      // The "Not validated yet." lead-in is the catalog's; the note is its body (only the labelled sample carries a card-level
      // note: the live strategy has none, its option notes ride on each row).
      if (data.note) h += '<div class="note-box"><strong>' + esc(t('not_validated')) + '</strong> ' + esc(data.note) + '</div>';
      el.innerHTML = h;
    }

    el.addEventListener('click', function (e) {
      var n = e.target.closest('[data-night]'), o = e.target.closest('[data-opt]');
      if (n) { state.ni = +n.getAttribute('data-night'); state.off = {}; render(); var f = el.querySelector('[data-night="' + state.ni + '"]'); if (f) f.focus(); }
      else if (o) {
        var k = o.getAttribute('data-opt');
        state.off[k] = !state.off[k]; render();
        var f2 = el.querySelector('[data-opt="' + k + '"]'); if (f2) f2.focus();
      }
    });
    render();
    return { update: function (d) { data = d; state = { ni: 0, off: {} }; render(); } };
  }

  window.NDNightCard = { mount: mount, fromStrategy: fromStrategy };
})();
