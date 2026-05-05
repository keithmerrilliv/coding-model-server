import React from 'react';

interface SparklineProps {
  values: (number | null)[];
  /** Omit to render at 100% of the parent's width (responsive via SVG viewBox). */
  width?: number;
  height?: number;
  color?: string;
  fill?: boolean;
  yMax?: number;
  yMin?: number;
  strokeWidth?: number;
}

// Logical horizontal coordinate space when running in flex mode. The SVG
// scales this to the container's actual width via preserveAspectRatio="none".
const FLEX_VIEW_W = 1000;

export const Sparkline: React.FC<SparklineProps> = ({
  values,
  width,
  height = 44,
  color = '#0d6efd',
  fill = false,
  yMax,
  yMin = 0,
  strokeWidth = 1.5,
}) => {
  const flex = width === undefined;
  const geomW = flex ? FLEX_VIEW_W : width;

  // Shared SVG props so empty-state and populated branches behave identically
  // under flex sizing — without `viewBox` an empty SVG with width="100%" can
  // collapse to 0 width inside CSS grid cells on some browsers (older Safari).
  const svgProps = flex
    ? {
        width: '100%' as const,
        height,
        viewBox: `0 0 ${FLEX_VIEW_W} ${height}`,
        preserveAspectRatio: 'none' as const,
      }
    : { width, height };

  if (!values.length) {
    return <svg {...svgProps} aria-hidden="true" style={{ display: 'block' }} />;
  }

  const numeric = values.filter((v): v is number => v != null);
  const dataMax = numeric.length ? Math.max(...numeric) : 1;
  const max = yMax ?? Math.max(dataMax, 1);
  const range = max - yMin || 1;
  const stepX = geomW / Math.max(values.length - 1, 1);

  const projected: (string | null)[] = values.map((v, i) => {
    if (v == null) return null;
    const x = i * stepX;
    const clamped = Math.min(Math.max(v, yMin), max);
    const y = height - ((clamped - yMin) / range) * height;
    return `${x.toFixed(1)},${y.toFixed(1)}`;
  });

  const rawSegments: string[][] = [];
  let current: string[] = [];
  for (const p of projected) {
    if (p == null) {
      if (current.length) { rawSegments.push(current); current = []; }
    } else {
      current.push(p);
    }
  }
  if (current.length) rawSegments.push(current);

  // SVG polyline/polygon renders nothing for a single point. Expand any
  // singleton segment into a 2-point horizontal stub so the chart actually
  // appears on the first tick of a new bucket window or when an isolated
  // sample is surrounded by nulls.
  const segments = rawSegments.map(seg => {
    if (seg.length !== 1) return seg;
    const [xs, ys] = seg[0].split(',');
    if (values.length === 1) {
      // Single-value chart: span the full geometry so the line communicates
      // "the value is X" across the whole chart area.
      return [`0,${ys}`, `${geomW.toFixed(1)},${ys}`];
    }
    // Otherwise an isolated mid-array point — emit a one-bucket-wide stub
    // centered on the point so it stays local. Clamp to the viewport so the
    // ends don't stick outside the SVG.
    const x = parseFloat(xs);
    const half = stepX / 2;
    const x1 = Math.max(x - half, 0).toFixed(1);
    const x2 = Math.min(x + half, geomW).toFixed(1);
    return [`${x1},${ys}`, `${x2},${ys}`];
  });

  // svgProps was computed above so the empty-state and populated branches
  // share the exact same sizing/viewBox. In flex mode, viewBox +
  // preserveAspectRatio="none" stretches the geometry to fill the container;
  // vector-effect="non-scaling-stroke" on each stroke keeps the line weight
  // visually constant despite the non-uniform scaling.
  return (
    <svg {...svgProps} style={{ display: 'block' }}>
      {fill && segments.map((seg, i) => {
        const xFirst = seg[0].split(',')[0];
        const xLast = seg[seg.length - 1].split(',')[0];
        return (
          <polygon
            key={`f-${i}`}
            fill={color}
            fillOpacity={0.18}
            stroke="none"
            points={`${xFirst},${height} ${seg.join(' ')} ${xLast},${height}`}
          />
        );
      })}
      {segments.map((seg, i) => (
        <polyline
          key={`l-${i}`}
          fill="none"
          stroke={color}
          strokeWidth={strokeWidth}
          strokeLinejoin="round"
          strokeLinecap="round"
          vectorEffect="non-scaling-stroke"
          points={seg.join(' ')}
        />
      ))}
    </svg>
  );
};

export default Sparkline;
