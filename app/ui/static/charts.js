"use strict";

// Hand-rolled SVG charts -- no charting library (see common.js for why: no
// third-party JS on pages holding a live bearer key). Two functions:
// renderSparkline (a tiny single-line trend, used in stat cards) and
// renderTimeSeries (a stacked area chart, used for "Request volume by
// model"). Both build real SVG DOM nodes (not innerHTML string
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

// data: { series: [{ label, points: [{t, v}, ...] }], height }
// Renders a stacked area chart. Points across series don't need to share
// exactly the same timestamps -- missing points on the union grid are
// treated as 0.
function renderTimeSeries(container, data) {
  container.innerHTML = "";
  const series = (data && data.series) || [];
  const width = 600;
  const height = data.height || 220;
  const padding = { top: 10, right: 10, bottom: 24, left: 10 };

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
  const stepX = timestamps.length > 1 ? plotWidth / (timestamps.length - 1) : 0;

  const xAt = (i) => padding.left + i * stepX;
  const yAt = (v) => padding.top + plotHeight - (v / stackedMax) * plotHeight;

  // Baseline.
  svg.appendChild(
    svgEl("line", {
      x1: padding.left,
      y1: padding.top + plotHeight,
      x2: width - padding.right,
      y2: padding.top + plotHeight,
      class: "timeseries-axis",
    }),
  );

  let cumulative = timestamps.map(() => 0);
  aligned.forEach((s, seriesIndex) => {
    const nextCumulative = cumulative.map((c, i) => c + s.values[i]);

    const topPoints = timestamps.map((_, i) => [xAt(i), yAt(nextCumulative[i])]);
    const bottomPoints = timestamps.map((_, i) => [xAt(i), yAt(cumulative[i])]).reverse();
    const areaPoints = topPoints.concat(bottomPoints);
    const areaPath =
      areaPoints.map(([x, y], i) => `${i === 0 ? "M" : "L"}${x.toFixed(2)},${y.toFixed(2)}`).join(" ") + " Z";

    const color = colorFor(seriesIndex);
    const area = svgEl("path", { d: areaPath, fill: color, "fill-opacity": "0.55", stroke: "none" });
    svg.appendChild(area);

    const linePath = topPoints
      .map(([x, y], i) => `${i === 0 ? "M" : "L"}${x.toFixed(2)},${y.toFixed(2)}`)
      .join(" ");
    svg.appendChild(svgEl("path", { d: linePath, fill: "none", stroke: color, "stroke-width": "1.5" }));

    cumulative = nextCumulative;
  });

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
