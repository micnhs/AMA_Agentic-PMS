/* PMS agent demo - live countdowns and progress polling. Vanilla JS, no framework, no inline script
   (Content-Security-Policy: script-src 'self'). The page works without it; this only keeps it current. */
(function () {
  "use strict";

  var STEP_TEXT = {
    DONE: "\u2713 done", RUNNING: "\u25b6 running", QUEUED: "\u2026 queued", FAILED: "\u2717 failed",
    NOT_QUEUED: "\u2013 not started", SKIPPED: "\u2013 skipped"
  };

  function pad(n) { return (n < 10 ? "0" : "") + n; }

  function duration(seconds) {
    var s = Math.floor(Math.abs(seconds));
    var d = Math.floor(s / 86400); s -= d * 86400;
    var h = Math.floor(s / 3600); s -= h * 3600;
    var m = Math.floor(s / 60); s -= m * 60;
    return d + "d " + pad(h) + ":" + pad(m) + ":" + pad(s);
  }

  /* Countdowns: data-seconds is the remaining time when the page (or the last poll) was rendered. */
  function initCountdowns(root) {
    var now = Date.now();
    (root || document).querySelectorAll("[data-countdown]").forEach(function (el) {
      if (!el.hasAttribute("data-base-ms")) { el.setAttribute("data-base-ms", String(now)); }
    });
  }

  function tick() {
    var now = Date.now();
    document.querySelectorAll("[data-countdown]").forEach(function (el) {
      var seconds = parseFloat(el.getAttribute("data-seconds"));
      var base = parseFloat(el.getAttribute("data-base-ms"));
      if (isNaN(seconds) || isNaN(base)) { return; }
      var remaining = seconds - (now - base) / 1000;
      var overdue = remaining < 0;
      el.textContent = overdue ? "OVERDUE by " + duration(remaining) : duration(remaining);
      el.classList.toggle("overdue", overdue);
    });
  }

  function getJson(url) {
    return fetch(url, { credentials: "same-origin", headers: { "Accept": "application/json" } })
      .then(function (response) { return response.ok ? response.json() : null; })
      .catch(function () { return null; });
  }

  function stepStatus(info, job) {
    if (info && info.status) { return info.status; }
    var jobStatus = job && job.status;
    if (jobStatus === "QUEUED" || jobStatus === "RUNNING") { return "QUEUED"; }
    if (jobStatus === "SKIPPED") { return "SKIPPED"; }
    return "NOT_QUEUED";
  }

  function setStep(cell, info, job) {
    var status = stepStatus(info, job);
    if (cell.getAttribute("data-status") === status) { return false; }
    cell.setAttribute("data-status", status);
    cell.textContent = STEP_TEXT[status] || status.toLowerCase();
    cell.className = "step step-" + status.toLowerCase();
    return true;
  }

  function showNotice(id, text) {
    var notice = document.getElementById(id);
    if (!notice || !notice.hidden) { return; }
    notice.textContent = text + " ";
    var link = document.createElement("a");
    link.href = window.location.pathname;
    link.textContent = "Reload";
    notice.appendChild(link);
    notice.hidden = false;
  }

  /* Dashboard: update status, per-agent progress and countdowns in place; structural changes ask for a reload. */
  function pollDashboard() {
    getJson("/api/cases").then(function (data) {
      if (!data) { return; }
      var expected = [];
      document.querySelectorAll("tr.case-row").forEach(function (tr) { expected.push(tr.getAttribute("data-case-id")); });
      var critical = data.critical_queue || [];
      var shownCritical = [];
      document.querySelectorAll("#critical-queue tr.case-row").forEach(function (tr) {
        shownCritical.push(tr.getAttribute("data-case-id"));
      });
      var rows = critical.concat(data.cases || []);
      var changed = rows.length !== expected.length ||
        critical.map(function (r) { return r.case_id; }).join("|") !== shownCritical.join("|");
      rows.forEach(function (row) {
        var tr = document.querySelector('tr.case-row[data-case-id="' + row.case_id.replace(/"/g, "") + '"]');
        if (!tr) { changed = true; return; }
        if (tr.getAttribute("data-status") !== row.status) {
          tr.setAttribute("data-status", row.status);
          var label = tr.querySelector(".status-label");
          if (label) { label.textContent = row.status_label; }
          changed = true;
        }
        var steps = (row.progress && row.progress.steps) || {};
        var job = row.progress && row.progress.latest_job;
        tr.querySelectorAll("td[data-step]").forEach(function (cell) {
          setStep(cell, steps[cell.getAttribute("data-step")], job);
        });
        var cd = tr.querySelector("[data-countdown]");
        if (cd && row.countdown_seconds !== null && row.countdown_seconds !== undefined) {
          cd.setAttribute("data-seconds", String(row.countdown_seconds));
          cd.setAttribute("data-base-ms", String(Date.now()));
        }
      });
      if (changed) { showNotice("refresh-notice", "Cases or statuses changed."); }
    });
  }

  /* Case page: per-agent progress; a finished analysis or a new status asks for a reload (drafts appear). */
  function pollCase(section) {
    var caseId = section.getAttribute("data-case-id");
    getJson("/api/cases/" + encodeURIComponent(caseId) + "/progress").then(function (data) {
      if (!data) { return; }
      var steps = (data.progress && data.progress.steps) || {};
      var job = data.progress && data.progress.latest_job;
      section.querySelectorAll("td[data-step]").forEach(function (cell) {
        setStep(cell, steps[cell.getAttribute("data-step")], job);
      });
      if (data.analysis_state !== section.getAttribute("data-analysis-state") ||
          data.status !== section.getAttribute("data-status")) {
        showNotice("progress-notice", "The analysis state or the case status changed.");
      }
    });
  }

  document.addEventListener("DOMContentLoaded", function () {
    initCountdowns(document);
    tick();
    window.setInterval(tick, 1000);
    if (!window.fetch) { return; }
    var page = document.body.getAttribute("data-page");
    if (page === "dashboard") {
      window.setInterval(function () { if (!document.hidden) { pollDashboard(); } }, 5000);
    }
    var progress = document.getElementById("progress");
    if (page === "case" && progress) {
      window.setInterval(function () { if (!document.hidden) { pollCase(progress); } }, 3000);
    }
  });
}());
