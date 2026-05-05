import React from 'react';

interface SparklineProps {
  values: (number | null)[];
  width?: number;
  height?: number;
  color?: string;
  fill?: boolean;
  yMax?: number;
  yMin?: number;
  strokeWidth?: number;
}

export const Sparkline: React.FC<SparklineProps> = ({
  values,
  width = 220,
  height = 44,
  color = '#0d6efd',
  fill = false,
  yMax,
  yMin = 0,
  strokeWidth = 1.5,
}) => {
  if (!values.length) return <svg width={width} height={height} aria-hidden="true" />;

  const numeric = values.filter((v): v is number => v != null);
  const dataMax = numeric.length ? Math.max(...numeric) : 1;
  const max = yMax ?? Math.max(dataMax, 1);
  const range = max - yMin || 1;
  const stepX = width / Math.max(values.length - 1, 1);

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

  return (
    <svg width={width} height={height} style={{ display: 'block' }}>
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
          points={seg.join(' ')}
        />
      ))}
    </svg>
  );
};

export default Sparkline;
