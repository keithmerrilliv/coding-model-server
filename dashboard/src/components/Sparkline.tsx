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

  if (!values.length) {
    return flex
      ? <svg width="100%" height={height} aria-hidden="true" style={{ display: 'block' }} />
      : <svg width={width} height={height} aria-hidden="true" />;
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

  const segments: string[][] = [];
  let current: string[] = [];
  for (const p of projected) {
    if (p == null) {
      if (current.length) { segments.push(current); current = []; }
    } else {
      current.push(p);
    }
  }
  if (current.length) segments.push(current);

  // In flex mode, viewBox + preserveAspectRatio="none" lets the geometry
  // stretch horizontally to fill the container. vector-effect on the strokes
  // keeps line weight visually constant despite the non-uniform scale.
  const svgProps = flex
    ? {
        width: '100%',
        height,
        viewBox: `0 0 ${FLEX_VIEW_W} ${height}`,
        preserveAspectRatio: 'none' as const,
      }
    : { width, height };

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
