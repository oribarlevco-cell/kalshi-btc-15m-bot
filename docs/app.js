(function () {
  "use strict";

  const REFRESH_INTERVAL_MS = 20000;

  const els = {
    loading: document.getElementById("loading"),
    content: document.getElementById("content"),
    error: document.getElementById("error"),
    ticker: document.getElementById("ticker"),
    countdown: document.getElementById("countdown"),
    btcPrice: document.getElementById("btc-price"),
    strikePrice: document.getElementById("strike-price"),
    probLabel: document.getElementById("prob-label"),
    probBarFill: document.getElementById("prob-bar-fill"),
    probNote: document.getElementById("prob-note"),
    yesPrice: document.getElementById("yes-price"),
    noPrice: document.getElementById("no-price"),
    volume: document.getElementById("volume"),
    openInterest: document.getElementById("open-interest"),
    updatedAt: document.getElementById("updated-at"),
  };

  let closeTimeMs = null;
  let countdownTimer = null;

  function formatUsd(value) {
    if (value === null || value === undefined) return "—";
    return "$" + Number(value).toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 });
  }

  function formatCents(value) {
    if (value === null || value === undefined) return "—";
    return "$" + Number(value).toFixed(2);
  }

  function formatCount(value) {
    if (value === null || value === undefined) return "—";
    return Number(value).toLocaleString(undefined, { maximumFractionDigits: 0 });
  }

  function formatDuration(totalSeconds) {
    const s = Math.max(0, Math.round(totalSeconds));
    const mm = Math.floor(s / 60);
    const ss = s % 60;
    return String(mm).padStart(2, "0") + ":" + String(ss).padStart(2, "0");
  }

  function relativeTime(isoString) {
    const then = new Date(isoString).getTime();
    const seconds = Math.max(0, Math.round((Date.now() - then) / 1000));
    if (seconds < 60) return seconds + "s ago";
    return Math.round(seconds / 60) + "m ago";
  }

  function tickCountdown() {
    if (closeTimeMs === null) return;
    const remainingSeconds = (closeTimeMs - Date.now()) / 1000;
    els.countdown.textContent = formatDuration(remainingSeconds);
    els.countdown.classList.toggle("urgent", remainingSeconds <= 30 && remainingSeconds > 0);
  }

  function render(data) {
    els.loading.classList.add("hidden");

    if (data.error === "no_active_market") {
      els.content.classList.add("hidden");
      els.error.classList.remove("hidden");
      els.error.textContent = "No active 15-min BTC market right now — one opens every quarter hour.";
      return;
    }

    els.error.classList.add("hidden");
    els.content.classList.remove("hidden");

    els.ticker.textContent = data.ticker || "";
    els.btcPrice.textContent = formatUsd(data.btc_price);
    els.strikePrice.textContent = formatUsd(data.floor_strike);
    els.yesPrice.textContent = formatCents(data.yes_ask);
    els.noPrice.textContent = formatCents(data.no_ask);
    els.volume.textContent = formatCount(data.volume);
    els.openInterest.textContent = formatCount(data.open_interest);

    if (data.probability_yes === null || data.probability_yes === undefined) {
      els.probLabel.textContent = "gathering data…";
      els.probLabel.className = "prob-label";
      els.probBarFill.style.width = "50%";
      els.probNote.textContent = "the model needs a few more price samples before predicting";
    } else {
      const pctYes = data.probability_yes * 100;
      const leaningYes = pctYes >= 50;
      const displayPct = leaningYes ? pctYes : 100 - pctYes;
      els.probLabel.textContent = displayPct.toFixed(1) + "% " + (leaningYes ? "YES" : "NO");
      els.probLabel.className = "prob-label " + (leaningYes ? "yes-lean" : "no-lean");
      els.probBarFill.style.width = pctYes.toFixed(1) + "%";
      els.probNote.textContent = data.rationale || "";
    }

    closeTimeMs = new Date(data.close_time).getTime();
    tickCountdown();

    els.updatedAt.textContent = "updated " + relativeTime(data.generated_at);
  }

  function showFetchError() {
    if (els.content.classList.contains("hidden") && els.error.classList.contains("hidden")) {
      els.loading.textContent = "couldn't load market data — retrying…";
    }
  }

  function fetchData() {
    fetch("data.json?_=" + Date.now(), { cache: "no-store" })
      .then(function (response) {
        if (!response.ok) throw new Error("HTTP " + response.status);
        return response.json();
      })
      .then(render)
      .catch(showFetchError);
  }

  fetchData();
  setInterval(fetchData, REFRESH_INTERVAL_MS);

  countdownTimer = setInterval(tickCountdown, 1000);
})();
