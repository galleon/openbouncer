"use strict";

// Hand-rolled SVG charts -- no charting library (see common.js for why: no
// third-party JS on pages holding a live bearer key). Two functions:
// renderSparkline (a tiny single-line trend, used in stat cards) and
// renderTimeSeries (a stacked bar chart with axes, used for "Request
// volume by model"). Both build real SVG DOM nodes (not innerHTML string
// interpolation) and size themselves via viewBox so they scale with their
// container.

const CHART_COLORS = ["#4f46e5", "#0ea5a4", "#d97706", "#db2777", "#65a30d", "#7c3aed"];

const SVG_NS = "http://www.w3.org/2000/svg";

function svgEl(tag, attrs) {
  const el = document.createElementNS(SVG_NS, tag);
  for (const [key, value] of Object.entries(attrs || {})) {
    el.setAttribute(key, value);
  }
  return el;
}

function colorFor(index) {
  return CHART_COLORS[index % CHART_COLORS.length];
}

// values: array of numbers, oldest first.
function renderSparkline(container, values) {
  container.innerHTML = "";
  const width = 100;
  const height = 32;
  const svg = svgEl("svg", {
    viewBox: `0 0 ${width} ${height}`,
    preserveAspectRatio: "none",
    class: "sparkline",
  });

  if (!values || values.length < 2) {
    container.appendChild(svg);
    return;
  }

  const max = Math.max(...values, 0);
  const min = Math.min(...values, 0);
  const range = max - min || 1;
  const stepX = width / (values.length - 1);

  const points = values.map((v, i) => {
    const x = i * stepX;
    const y = height - ((v - min) / range) * height;
    return [x, y];
  });

  const linePath = points.map(([x, y], i) => `${i === 0 ? "M" : "L"}${x.toFixed(2)},${y.toFixed(2)}`).join(" ");
  svg.appendChild(svgEl("path", { d: linePath, class: "sparkline-line" }));

  container.appendChild(svg);
}

// Picks at most maxTicks evenly-spaced indices out of [0, count) --
// showing a tick per bucket would be unreadable once a range has dozens
// of buckets (e.g. 96 for 24h at 15m step).
function pickTickIndices(count, maxTicks) {
  if (count <= maxTicks) {
    return Array.from({ length: count }, (_, i) => i);
  }
  const step = (count - 1) / (maxTicks - 1);
  const indices = new Set();
  for (let i = 0; i < maxTicks; i++) {
    indices.add(Math.round(i * step));
  }
  return Array.from(indices);
}

// totalSpanSeconds (first bucket to last bucket actually plotted) decides
// the granularity worth showing -- e.g. a 30-day range still buckets at a
// sub-day step (app/api/routes/activity.py's _RANGE_CONFIG), so labeling
// by bucket width alone would print the same handful of times-of-day over
// and over with no date, which is unreadable across many days. Once the
// plotted range crosses a day, show the date instead.
function formatAxisTimestamp(unixSeconds, totalSpanSeconds) {
  const date = new Date(unixSeconds * 1000);
  if (totalSpanSeconds < 24 * 3600) {
    return date.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
  }
  return date.toLocaleDateString([], { month: "short", day: "numeric" });
}

function formatAxisValue(v) {
  if (v >= 1000) return `${(v / 1000).toFixed(1)}k`;
  return Number.isInteger(v) ? String(v) : v.toFixed(2);
}

// data: { series: [{ label, points: [{t, v}, ...] }], height }
// Renders a stacked bar chart (one stack per time bucket) with X (time)
// and Y (value) axes. Points across series don't need to share exactly
// the same timestamps -- missing points on the union grid are treated as
// 0, and the bucket width is inferred from the gap between the first two
// timestamps actually present.
function renderTimeSeries(container, data) {
  container.innerHTML = "";
  const series = (data && data.series) || [];
  const width = 640;
  const height = data.height || 260;
  const padding = { top: 10, right: 10, bottom: 28, left: 44 };

  const svg = svgEl("svg", {
    viewBox: `0 0 ${width} ${height}`,
    preserveAspectRatio: "none",
    class: "timeseries-chart",
  });

  const nonEmptySeries = series.filter((s) => s.points && s.points.length > 0);
  if (nonEmptySeries.length === 0) {
    container.appendChild(svg);
    return;
  }

  const timestamps = Array.from(
    new Set(nonEmptySeries.flatMap((s) => s.points.map((p) => p.t))),
  ).sort((a, b) => a - b);
  const bucketSeconds = timestamps.length > 1 ? timestamps[1] - timestamps[0] : 3600;
  const totalSpanSeconds =
    timestamps.length > 1 ? timestamps[timestamps.length - 1] - timestamps[0] : bucketSeconds;

  const aligned = nonEmptySeries.map((s) => {
    const byTime = new Map(s.points.map((p) => [p.t, p.v]));
    return { label: s.label, values: timestamps.map((t) => byTime.get(t) || 0) };
  });

  const stackedMax = Math.max(
    ...timestamps.map((_, i) => aligned.reduce((sum, s) => sum + s.values[i], 0)),
    0.001,
  );

  const plotWidth = width - padding.left - padding.right;
  const plotHeight = height - padding.top - padding.bottom;
  const stepX = plotWidth / timestamps.length;
  const barWidth = Math.max(1, stepX * 0.7);

  const xAt = (i) => padding.left + i * stepX + stepX / 2;
  const yAt = (v) => padding.top + plotHeight - (v / stackedMax) * plotHeight;

  // Y-axis gridlines + value labels.
  const yTickCount = 4;
  for (let t = 0; t <= yTickCount; t++) {
    const value = (stackedMax / yTickCount) * t;
    const y = yAt(value);
    svg.appendChild(
      svgEl("line", {
        x1: padding.left,
        y1: y.toFixed(2),
        x2: width - padding.right,
        y2: y.toFixed(2),
        class: "timeseries-gridline",
      }),
    );
    const label = svgEl("text", {
      x: padding.left - 6,
      y: y.toFixed(2),
      class: "timeseries-axis-label timeseries-axis-label-y",
    });
    label.textContent = formatAxisValue(value);
    svg.appendChild(label);
  }

  // X-axis baseline.
  svg.appendChild(
    svgEl("line", {
      x1: padding.left,
      y1: padding.top + plotHeight,
      x2: width - padding.right,
      y2: padding.top + plotHeight,
      class: "timeseries-axis",
    }),
  );

  // Stacked bars, one stack per bucket.
  let cumulative = timestamps.map(() => 0);
  aligned.forEach((s, seriesIndex) => {
    const color = colorFor(seriesIndex);
    timestamps.forEach((_, i) => {
      const v = s.values[i];
      if (v <= 0) return;
      const barTop = yAt(cumulative[i] + v);
      const barBottom = yAt(cumulative[i]);
      svg.appendChild(
        svgEl("rect", {
          x: (xAt(i) - barWidth / 2).toFixed(2),
          y: barTop.toFixed(2),
          width: barWidth.toFixed(2),
          height: Math.max(0, barBottom - barTop).toFixed(2),
          fill: color,
        }),
      );
    });
    cumulative = cumulative.map((c, i) => c + s.values[i]);
  });

  // X-axis tick labels -- a bounded subset of buckets, not all of them.
  for (const i of pickTickIndices(timestamps.length, 6)) {
    const label = svgEl("text", {
      x: xAt(i).toFixed(2),
      y: padding.top + plotHeight + 16,
      class: "timeseries-axis-label timeseries-axis-label-x",
    });
    label.textContent = formatAxisTimestamp(timestamps[i], totalSpanSeconds);
    svg.appendChild(label);
  }

  container.appendChild(svg);

  const legend = document.createElement("div");
  legend.className = "timeseries-legend";
  aligned.forEach((s, i) => {
    const item = document.createElement("span");
    item.className = "timeseries-legend-item";
    const swatch = document.createElement("span");
    swatch.className = "timeseries-legend-swatch";
    swatch.style.background = colorFor(i);
    item.appendChild(swatch);
    item.appendChild(document.createTextNode(s.label));
    legend.appendChild(item);
  });
  container.appendChild(legend);
}
