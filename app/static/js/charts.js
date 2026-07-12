(function () {
  var INK = "#111111";
  var ACCENT = "#B3492B";
  var LINE = "#E4E1DA";
  var PAPER = "#FAF9F6";

  var SENTIMENT_COLORS = {
    positive: INK,
    neutral: "rgba(17, 17, 17, 0.4)",
    negative: ACCENT,
    mixed: "rgba(17, 17, 17, 0.2)",
  };

  if (window.Chart) {
    Chart.defaults.font.family =
      "'Inter', -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif";
    Chart.defaults.color = INK;
  }

  function inkRamp(count) {
    var steps = [];
    for (var i = 0; i < count; i++) {
      var opacity = Math.max(0.22, 1 - i * (0.65 / Math.max(count - 1, 1)));
      steps.push("rgba(17, 17, 17, " + opacity.toFixed(2) + ")");
    }
    return steps;
  }

  function tooltipStyle() {
    return {
      backgroundColor: INK,
      titleColor: PAPER,
      bodyColor: PAPER,
      padding: 8,
      cornerRadius: 0,
      displayColors: false,
    };
  }

  function buildBarConfig(payload, horizontal) {
    return {
      type: "bar",
      data: {
        labels: payload.labels,
        datasets: [
          {
            data: payload.values,
            backgroundColor: inkRamp(payload.values.length),
            borderRadius: 2,
            maxBarThickness: 28,
          },
        ],
      },
      options: {
        indexAxis: horizontal ? "y" : "x",
        responsive: true,
        plugins: {
          legend: { display: false },
          tooltip: tooltipStyle(),
        },
        scales: {
          x: {
            grid: { color: LINE, drawTicks: false },
            beginAtZero: true,
            display: !horizontal || true,
          },
          y: {
            grid: { display: false },
          },
        },
      },
    };
  }

  function buildGroupedBarConfig(payload, horizontal) {
    return {
      type: "bar",
      data: {
        labels: payload.labels,
        datasets: [
          {
            label: payload.baseline_label || "Baseline",
            data: payload.baseline_values,
            backgroundColor: INK,
            borderRadius: 2,
            maxBarThickness: 24,
          },
          {
            label: payload.comparison_label || "Comparison",
            data: payload.comparison_values,
            backgroundColor: "rgba(17, 17, 17, 0.4)",
            borderRadius: 2,
            maxBarThickness: 24,
          },
        ],
      },
      options: {
        indexAxis: horizontal ? "y" : "x",
        responsive: true,
        plugins: {
          legend: {
            display: true,
            position: "bottom",
            labels: { color: INK, boxWidth: 10, font: { size: 11 } },
          },
          tooltip: tooltipStyle(),
        },
        scales: {
          x: { grid: { color: LINE, drawTicks: false }, beginAtZero: true },
          y: { grid: { display: false } },
        },
      },
    };
  }

  function buildDoughnutConfig(payload, colorMap) {
    var ramp = inkRamp(payload.labels.length);
    var colors = payload.labels.map(function (label, i) {
      if (colorMap && colorMap[label]) return colorMap[label];
      return ramp[i];
    });
    return {
      type: "doughnut",
      data: {
        labels: payload.labels,
        datasets: [
          {
            data: payload.values,
            backgroundColor: colors,
            borderColor: PAPER,
            borderWidth: 2,
          },
        ],
      },
      options: {
        responsive: true,
        plugins: {
          legend: {
            position: "bottom",
            labels: { color: INK, boxWidth: 10, font: { size: 11 } },
          },
          tooltip: tooltipStyle(),
        },
      },
    };
  }

  function buildChartConfig(type, payload) {
    switch (type) {
      case "bar-horizontal":
        return buildBarConfig(payload, true);
      case "bar-vertical":
        return buildBarConfig(payload, false);
      case "bar-horizontal-grouped":
        return buildGroupedBarConfig(payload, true);
      case "bar-vertical-grouped":
        return buildGroupedBarConfig(payload, false);
      case "doughnut-sentiment":
        return buildDoughnutConfig(payload, SENTIMENT_COLORS);
      case "doughnut":
        return buildDoughnutConfig(payload, null);
      default:
        return buildBarConfig(payload, true);
    }
  }

  function initCharts() {
    if (!window.Chart) return;

    document.querySelectorAll("canvas[data-chart-type]").forEach(function (canvas) {
      if (canvas.dataset.chartInitialized) return;

      var dataScript = document.querySelector(
        'script[data-chart-data-for="' + canvas.id + '"]'
      );
      if (!dataScript) return;

      var payload;
      try {
        payload = JSON.parse(dataScript.textContent);
      } catch (err) {
        return;
      }
      if (!payload.labels || !payload.labels.length) return;

      var config = buildChartConfig(canvas.dataset.chartType, payload);
      new Chart(canvas.getContext("2d"), config);
      canvas.dataset.chartInitialized = "true";
    });
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", initCharts);
  } else {
    initCharts();
  }
})();
