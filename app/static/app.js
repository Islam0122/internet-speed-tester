(() => {
  "use strict";

  const form = document.getElementById("speed-test-form");
  const urlInput = document.getElementById("url-input");
  const countInput = document.getElementById("count-input");
  const startButton = document.getElementById("start-button");
  const formError = document.getElementById("form-error");

  const progressSection = document.getElementById("progress-section");
  const progressLabel = document.getElementById("progress-label");
  const progressPercent = document.getElementById("progress-percent");
  const progressBar = document.getElementById("progress-bar");

  const statsSection = document.getElementById("stats-section");
  const resultsBody = document.getElementById("results-body");

  const statEls = {
    avgSpeed: document.getElementById("stat-avg-speed"),
    overallSpeed: document.getElementById("stat-overall-speed"),
    avgTime: document.getElementById("stat-avg-time"),
    totalSize: document.getElementById("stat-total-size"),
    success: document.getElementById("stat-success"),
    failed: document.getElementById("stat-failed"),
    totalTime: document.getElementById("stat-total-time"),
  };

  let isRunning = false;

  function showFormError(message) {
    formError.textContent = message;
    formError.hidden = false;
  }

  function clearFormError() {
    formError.hidden = true;
    formError.textContent = "";
  }

  function isLikelyValidUrl(value) {
    try {
      const parsed = new URL(value);
      return parsed.protocol === "http:" || parsed.protocol === "https:";
    } catch {
      return false;
    }
  }

  function setRunningState(running, requestsCount) {
    isRunning = running;
    startButton.disabled = running;
    startButton.textContent = running ? "Running..." : "Start Test";

    if (running) {
      progressSection.hidden = false;
      // The API runs the whole test as a single call, so we cannot report a
      // real per-request count/percentage here. Show an honest indeterminate
      // state instead of fabricating progress numbers.
      progressLabel.textContent = `Running ${requestsCount} sequential request(s)...`;
      progressPercent.textContent = "";
      progressBar.classList.add("indeterminate");
      progressBar.style.width = "";
    } else {
      progressBar.classList.remove("indeterminate");
    }
  }

  function formatNumber(value, decimals = 2) {
    if (value === null || value === undefined || Number.isNaN(value)) return "-";
    return Number(value).toFixed(decimals);
  }

  function renderResults(data) {
    statEls.avgSpeed.textContent = formatNumber(data.average_speed_mib_s);
    statEls.overallSpeed.textContent = formatNumber(data.overall_speed_mib_s);
    statEls.avgTime.textContent = formatNumber(data.average_time_seconds, 3);
    statEls.totalSize.textContent = formatNumber(data.total_size_mib);
    statEls.success.textContent = data.successful_requests;
    statEls.failed.textContent = data.failed_requests;
    statEls.totalTime.textContent = formatNumber(data.total_time_seconds, 3);

    resultsBody.innerHTML = "";
    for (const result of data.results) {
      const row = document.createElement("tr");

      const statusBadge = result.success
        ? '<span class="status-badge ok">Success</span>'
        : '<span class="status-badge fail">Failed</span>';

      row.innerHTML = `
        <td>${result.request_number}</td>
        <td>${statusBadge}</td>
        <td>${result.status_code ?? "-"}</td>
        <td>${formatNumber(result.size_mib)}</td>
        <td>${formatNumber(result.duration_seconds, 3)}</td>
        <td>${formatNumber(result.speed_mib_s)}</td>
        <td>${result.error ? escapeHtml(result.error) : "-"}</td>
      `;
      resultsBody.appendChild(row);
    }

    statsSection.hidden = false;
  }

  function escapeHtml(str) {
    const div = document.createElement("div");
    div.textContent = str;
    return div.innerHTML;
  }

  async function runSpeedTest(url, requestsCount) {
    const response = await fetch("/api/speed-test", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ url, requests_count: requestsCount }),
    });

    let payload;
    try {
      payload = await response.json();
    } catch {
      throw new Error("The server returned an unexpected response.");
    }

    if (!response.ok) {
      const detail = payload && payload.detail ? payload.detail : "Request failed.";
      const message = Array.isArray(detail)
        ? detail.map((d) => d.msg || JSON.stringify(d)).join(" ")
        : detail;
      throw new Error(message);
    }

    return payload;
  }

  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    if (isRunning) return;

    clearFormError();

    const url = urlInput.value.trim();
    const requestsCount = parseInt(countInput.value, 10);

    if (!url || !isLikelyValidUrl(url)) {
      showFormError("Please enter a valid HTTP or HTTPS URL.");
      return;
    }
    if (!Number.isInteger(requestsCount) || requestsCount < 1 || requestsCount > 50) {
      showFormError("Requests count must be a whole number between 1 and 50.");
      return;
    }

    setRunningState(true, requestsCount);
    statsSection.hidden = true;

    try {
      const data = await runSpeedTest(url, requestsCount);
      renderResults(data);
    } catch (err) {
      showFormError(err.message || "Something went wrong while running the test.");
    } finally {
      setRunningState(false, requestsCount);
      progressSection.hidden = true;
    }
  });

  urlInput.addEventListener("keydown", (event) => {
    if (event.key === "Enter" && !isRunning) {
      // Allow the native form submit to happen; nothing extra needed.
    }
  });
})();
