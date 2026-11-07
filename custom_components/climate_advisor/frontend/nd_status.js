/* Issue #1230 R5: Status tab wiring. Status is assembled later from the R2 chart, the R3 timeline list and the
   R4 night card; this file only owns the three mount points and mounts the finished "Tonight in the bedroom"
   component against sample data (nd_night_sample.json). R4 replaces the sample fetch with the live night plan. */
(function () {
  'use strict';
  var card = null;
  function show() {
    if (card) return;
    var host = ND.$('night-card');
    fetch('nd_night_sample.json').then(function (r) { return r.json(); }).then(function (d) {
      card = window.NDNightCard.mount(host, d, { unit: ND.unit });
    }).catch(function () {
      host.innerHTML = '<div class="empty">The sample night plan could not be loaded.</div>';
    });
  }
  // Mount points the other tracks fill: #status-slot-chart (R2), #status-slot-plan (R3), #night-card (R4).
  ND.register('status', { show: show });
})();
