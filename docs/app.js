(function () {
  "use strict";

  const STATIC_REFRESH_INTERVAL_MS = 20000;
  const LIVE_REFRESH_INTERVAL_MS = 15000;
  const LIVE_FETCH_TIMEOUT_MS = 5000;
  const LIVE_URL_STORAGE_KEY = "kalshi_dashboard_live_url";

  const els = {
    connBadge: document.getElementById("conn-badge"),
    settingsBtn: document.getElementById("settings-btn"),
    settingsPanel: document.getElementById("settings-panel"),
    liveUrlInput: document.getElementById("live-url-input"),
    liveUrlSave: document.getElementById("live-url-save"),
    liveUrlClear: document.getElementById("live-url-clear"),

    loading: document.getElementById("loading"),
    content: document.getElementById("content"),
    error: document.getElementById("error"),
    ticker: document.getElementById("ticker"),
    countdown: document.getElementById("countdown"),
    btcPrice: document.getElementById("btc-price"),
    strikePrice: document.getElementById("strike-price"),
    strikeDeltaPct: document.getElementById("strike-delta-pct"),
    momentum: document.getElementById("momentum"),
    probLabel: document.getElementById("prob-label"),
    probBarFill: document.getElementById("prob-bar-fill"),
    probNote: document.getElementById("prob-note"),
    yesPrice: document.getElementById("yes-price"),
    noPrice: document.getElementById("no-price"),
    volume: document.getElementById("volume"),
    openInterest: document.getElementById("open-interest"),
    updatedAt: document.getElementById("updated-at"),

    recentSettledCard: document.getElementById("recent-settled-card"),
    recentSettledStrip: document.getElementById("recent-settled-strip"),

    backtestsCard: document.getElementById("backtests-card"),
    backtestsTableBody: document.querySelector("#backtests-table tbody"),
    backtestsNote: document.getElementById("backtests-note"),

    patternLogCard: document.getElementById("pattern-log-card"),
    patternTotal: document.getElementById("pattern-total"),
    patternSplit: document.getElementById("pattern-split"),
    patternTableBody: document.querySelector("#pattern-table tbody"),

    calibrationCard: document.getElementById("calibration-card"),
    calibrationBars: document.getElementById("calibration-bars"),
    calibrationBrier: document.getElementById("calibration-brier"),
    calibrationTrend: document.getElementById("calibration-trend"),

    exportBtn: document.getElementById("export-btn"),
    clearBtn: document.getElementById("clear-btn"),
  };

  let closeTimeMs = null;
  let lastPayload = null;
  let liveMode = false;

  // ---------- formatting helpers ----------

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

  function formatPct(value, digits) {
    if (value === null || value === undefined) return "—";
    return (value * 100).toFixed(digits === undefined ? 1 : digits) + "%";
  }

  function formatSignedPct(value) {
    if (value === null || value === undefined) return "—";
    const pct = value * 100;
    return (pct >= 0 ? "+" : "") + pct.toFixed(2) + "%";
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

  // ---------- live tile ----------

  function renderLiveTile(tile) {
    els.loading.classList.add("hidden");

    if (!tile || tile.error) {
      els.content.classList.add("hidden");
      els.error.classList.remove("hidden");
      els.error.textContent =
        tile && tile.error === "no_data_yet"
          ? "Waiting for the bot to log its first market snapshot."
          : "No active 15-min BTC market right now — one opens every quarter hour.";
      return;
    }

    els.error.classList.add("hidden");
    els.content.classList.remove("hidden");

    els.ticker.textContent = tile.ticker || "";
    els.btcPrice.textContent = formatUsd(tile.btc_price);
    els.strikePrice.textContent = formatUsd(tile.floor_strike);
    els.yesPrice.textContent = formatCents(tile.yes_ask);
    els.noPrice.textContent = formatCents(tile.no_ask);
    els.volume.textContent = formatCount(tile.volume);
    els.openInterest.textContent = formatCount(tile.open_interest);

    if (tile.btc_price && tile.floor_strike) {
      els.strikeDeltaPct.textContent = formatSignedPct((tile.btc_price - tile.floor_strike) / tile.floor_strike);
    } else {
      els.strikeDeltaPct.textContent = "—";
    }

    const m1 = tile.momentum_1m_pct;
    const m15 = tile.momentum_15m_pct;
    els.momentum.textContent =
      (m1 === null || m1 === undefined ? "—" : formatSignedPct(m1)) +
      " / " +
      (m15 === null || m15 === undefined ? "—" : formatSignedPct(m15));

    if (tile.probability_yes === null || tile.probability_yes === undefined) {
      els.probLabel.textContent = "gathering data…";
      els.probLabel.className = "prob-label";
      els.probBarFill.style.width = "50%";
      els.probNote.textContent = "the model needs a few more price samples before predicting";
    } else {
      const pctYes = tile.probability_yes * 100;
      const leaningYes = pctYes >= 50;
      const displayPct = leaningYes ? pctYes : 100 - pctYes;
      els.probLabel.textContent = displayPct.toFixed(1) + "% " + (leaningYes ? "YES" : "NO");
      els.probLabel.className = "prob-label " + (leaningYes ? "yes-lean" : "no-lean");
      els.probBarFill.style.width = pctYes.toFixed(1) + "%";
      els.probNote.textContent = tile.rationale || "";
    }

    if (tile.close_time) {
      closeTimeMs = new Date(tile.close_time).getTime();
      tickCountdown();
    }
  }

  // ---------- analytics sections (live-server only) ----------

  function renderRecentSettled(recentSettled) {
    if (!recentSettled || recentSettled.length === 0) {
      els.recentSettledCard.classList.add("hidden");
      return;
    }
    els.recentSettledCard.classList.remove("hidden");
    els.recentSettledStrip.innerHTML = "";
    recentSettled.forEach(function (item) {
      const pill = document.createElement("span");
      const isUp = item.result === "yes";
      pill.className = "pill " + (isUp ? "pill-up" : "pill-down");
      pill.textContent = isUp ? "U" : "D";
      pill.title = item.ticker + " — " + (isUp ? "up" : "down");
      els.recentSettledStrip.appendChild(pill);
    });
  }

  function pnlClass(value) {
    if (value === null || value === undefined) return "";
    return value >= 0 ? "pnl-positive" : "pnl-negative";
  }

  function renderBacktests(backtests) {
    if (!backtests || backtests.length === 0) {
      els.backtestsCard.classList.add("hidden");
      return;
    }
    els.backtestsCard.classList.remove("hidden");
    els.backtestsTableBody.innerHTML = "";
    let anyLowConfidence = false;

    backtests.forEach(function (r) {
      if (r.low_confidence) anyLowConfidence = true;
      const row = document.createElement("tr");
      if (r.low_confidence) row.className = "low-confidence";

      const ciText = formatPct(r.win_rate, 0) + " ±" + Math.round((r.ci_high - r.ci_low) * 50);
      const winRateCell = r.low_confidence
        ? '<span class="ci-badge low-confidence">' + ciText + "</span>"
        : ciText;

      row.innerHTML =
        "<td>" +
        r.name +
        '</td><td class="num">' +
        r.n +
        '</td><td class="num">' +
        winRateCell +
        '</td><td class="num ' +
        pnlClass(r.avg_pnl) +
        '">' +
        (r.avg_pnl >= 0 ? "+" : "") +
        "$" +
        r.avg_pnl.toFixed(2) +
        "</td>";
      els.backtestsTableBody.appendChild(row);
    });

    els.backtestsNote.classList.toggle("hidden", !anyLowConfidence);
  }

  function renderPatternLog(patternLog, backtests) {
    if (!patternLog) {
      els.patternLogCard.classList.add("hidden");
      return;
    }
    els.patternLogCard.classList.remove("hidden");
    els.patternTotal.textContent = String(patternLog.total_settled);
    const total = patternLog.total_settled || 1;
    els.patternSplit.textContent =
      formatPct(patternLog.up_count / total, 0) + " / " + formatPct(patternLog.down_count / total, 0);

    const labels = { favorite: "opening favorite", momentum: "momentum direction", model: "my model (initial)" };
    els.patternTableBody.innerHTML = "";
    (backtests || [])
      .filter(function (r) {
        return labels[r.name];
      })
      .forEach(function (r) {
        const row = document.createElement("tr");
        const ciLabel = formatPct(r.win_rate, 0) + " [" + formatPct(r.ci_low, 0) + "-" + formatPct(r.ci_high, 0) + "]";
        row.innerHTML = "<td>" + labels[r.name] + '</td><td class="num">' + ciLabel + "</td>";
        els.patternTableBody.appendChild(row);
      });
  }

  function renderCalibration(calibration, trend) {
    if (!calibration || !calibration.last || calibration.last.n === 0) {
      els.calibrationCard.classList.add("hidden");
      return;
    }
    els.calibrationCard.classList.remove("hidden");
    const summary = calibration.last;

    els.calibrationBars.innerHTML = "";
    const maxCount = Math.max.apply(
      null,
      summary.buckets.map(function (b) {
        return b.count;
      })
    );
    summary.buckets.forEach(function (b) {
      const bar = document.createElement("div");
      bar.className = "calibration-bar";
      const heightPct = maxCount > 0 ? Math.max(4, (b.count / maxCount) * 100) : 4;
      bar.style.height = heightPct + "%";
      bar.title = Math.round(b.lo * 100) + "-" + Math.round(b.hi * 100) + "%: n=" + b.count;
      els.calibrationBars.appendChild(bar);
    });

    els.calibrationBrier.textContent =
      "Brier score: " + (summary.brier_score === null ? "—" : summary.brier_score.toFixed(3)) + " (n=" + summary.n + ")";

    if (trend && trend.length >= 2) {
      const first = trend[0].brier_score;
      const last = trend[trend.length - 1].brier_score;
      if (first !== null && last !== null) {
        const improved = last < first;
        els.calibrationTrend.textContent = improved ? "trending better" : "trending worse";
        els.calibrationTrend.className = improved ? "trend-down" : "trend-up";
      } else {
        els.calibrationTrend.textContent = "";
      }
    } else {
      els.calibrationTrend.textContent = "";
    }
  }

  // ---------- connection / fetch ----------

  function getLiveUrl() {
    try {
      return localStorage.getItem(LIVE_URL_STORAGE_KEY) || "";
    } catch (e) {
      return "";
    }
  }

  function setLiveUrl(url) {
    try {
      if (url) localStorage.setItem(LIVE_URL_STORAGE_KEY, url);
      else localStorage.removeItem(LIVE_URL_STORAGE_KEY);
    } catch (e) {
      /* private browsing etc. -- ignore, this session just won't persist */
    }
  }

  function setConnBadge(mode) {
    els.connBadge.classList.remove("badge-live", "badge-muted");
    if (mode === "live") {
      els.connBadge.textContent = "live";
      els.connBadge.classList.add("badge-live");
    } else {
      els.connBadge.textContent = "static";
      els.connBadge.classList.add("badge-muted");
    }
  }

  function fetchWithTimeout(url, timeoutMs) {
    const controller = new AbortController();
    const timer = setTimeout(function () {
      controller.abort();
    }, timeoutMs);
    return fetch(url, { signal: controller.signal, cache: "no-store" }).finally(function () {
      clearTimeout(timer);
    });
  }

  function renderOfflineAnalytics() {
    els.recentSettledCard.classList.add("hidden");
    els.backtestsCard.classList.add("hidden");
    els.patternLogCard.classList.add("hidden");
    els.calibrationCard.classList.add("hidden");
  }

  function renderAnalytics(analytics) {
    renderRecentSettled(analytics.recent_settled);
    renderBacktests(analytics.backtests);
    renderPatternLog(analytics.pattern_log, analytics.backtests);
    renderCalibration(analytics.calibration, analytics.calibration_trend);
  }

  function fetchJson(url) {
    return fetchWithTimeout(url, LIVE_FETCH_TIMEOUT_MS).then(function (response) {
      if (!response.ok) throw new Error("HTTP " + response.status);
      return response.json();
    });
  }

  // Analytics (settled windows, backtests, pattern log, calibration) are
  // historical/aggregate, not real-time -- they're published to a static
  // file by your local bot every ~10 min, same as the live tile's static
  // fallback. Only genuinely real-time data needs the live server/ngrok.
  function fetchStatic() {
    const tilePromise = fetchJson("data.json?_=" + Date.now());
    const analyticsPromise = fetchJson("analytics.json?_=" + Date.now()).catch(function () {
      return null; // no published analytics yet -- don't fail the whole render over it
    });

    Promise.all([tilePromise, analyticsPromise])
      .then(function (results) {
        const tile = results[0];
        const analytics = results[1];
        liveMode = false;
        setConnBadge("static");
        lastPayload = {
          generated_at: tile.generated_at,
          live_tile: tile,
          recent_settled: analytics ? analytics.recent_settled : [],
          backtests: analytics ? analytics.backtests : [],
          pattern_log: analytics ? analytics.pattern_log : null,
          calibration: analytics ? analytics.calibration : null,
          calibration_trend: analytics ? analytics.calibration_trend : [],
        };
        renderLiveTile(tile);
        if (analytics && analytics.generated_at) {
          renderAnalytics(analytics);
        } else {
          renderOfflineAnalytics();
        }
        els.updatedAt.textContent = tile.generated_at ? "updated " + relativeTime(tile.generated_at) : "";
      })
      .catch(function () {
        if (els.content.classList.contains("hidden") && els.error.classList.contains("hidden")) {
          els.loading.textContent = "couldn't load market data — retrying…";
        }
      });
  }

  function fetchLive(liveUrl) {
    const url = liveUrl + (liveUrl.indexOf("?") === -1 ? "?" : "&") + "_=" + Date.now();
    fetchJson(url)
      .then(function (data) {
        liveMode = true;
        setConnBadge("live");
        lastPayload = data;
        renderLiveTile(data.live_tile);
        renderAnalytics(data);
        els.updatedAt.textContent = data.generated_at ? "updated " + relativeTime(data.generated_at) : "";
      })
      .catch(function () {
        // Live server unreachable this tick -- fall back to the static tile
        // rather than showing nothing.
        fetchStatic();
      });
  }

  function refresh() {
    const liveUrl = getLiveUrl();
    if (liveUrl) {
      fetchLive(liveUrl);
    } else {
      fetchStatic();
    }
  }

  // ---------- settings panel ----------

  els.settingsBtn.addEventListener("click", function () {
    els.settingsPanel.classList.toggle("hidden");
    els.liveUrlInput.value = getLiveUrl();
  });

  els.liveUrlSave.addEventListener("click", function () {
    const url = els.liveUrlInput.value.trim().replace(/\/$/, "");
    setLiveUrl(url);
    els.settingsPanel.classList.add("hidden");
    refresh();
  });

  els.liveUrlClear.addEventListener("click", function () {
    setLiveUrl("");
    els.liveUrlInput.value = "";
    els.settingsPanel.classList.add("hidden");
    refresh();
  });

  // ---------- export / clear ----------

  els.exportBtn.addEventListener("click", function () {
    if (!lastPayload) return;
    const blob = new Blob([JSON.stringify(lastPayload, null, 2)], { type: "application/json" });
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url;
    a.download = "kalshi-dashboard-" + new Date().toISOString().replace(/[:.]/g, "-") + ".json";
    document.body.appendChild(a);
    a.click();
    document.body.removeChild(a);
    URL.revokeObjectURL(url);
  });

  els.clearBtn.addEventListener("click", function () {
    const confirmed = window.confirm(
      "This only clears this browser's own cached view of the dashboard (your saved live-server URL, nothing else). " +
        "It cannot touch any real data on your machine. Continue?"
    );
    if (!confirmed) return;
    setLiveUrl("");
    els.liveUrlInput.value = "";
    lastPayload = null;
    refresh();
  });

  // ---------- boot ----------

  function scheduleNextRefresh() {
    const delay = getLiveUrl() ? LIVE_REFRESH_INTERVAL_MS : STATIC_REFRESH_INTERVAL_MS;
    setTimeout(function () {
      refresh();
      scheduleNextRefresh();
    }, delay);
  }

  els.liveUrlInput.value = getLiveUrl();
  refresh();
  scheduleNextRefresh();
  setInterval(tickCountdown, 1000);
})();
