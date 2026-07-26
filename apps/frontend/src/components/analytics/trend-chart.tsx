"use client";

import { Area, AreaChart, CartesianGrid, ResponsiveContainer, Tooltip, XAxis, YAxis } from "recharts";

import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { EmptyState } from "@/components/ui/empty-state";

interface TrendChartProps {
  title: string;
  description?: string;
  data: { day: string; value: number }[];
  color?: string;
  valueFormatter?: (value: number) => string;
}

function formatDay(day: string): string {
  return new Date(day).toLocaleDateString(undefined, { month: "short", day: "numeric" });
}

export function TrendChart({
  title,
  description,
  data,
  color = "hsl(var(--primary))",
  valueFormatter = (value) => String(value),
}: TrendChartProps) {
  return (
    <Card>
      <CardHeader>
        <CardTitle className="text-base">{title}</CardTitle>
        {description ? <p className="text-sm text-muted-foreground">{description}</p> : null}
      </CardHeader>
      <CardContent>
        {data.length === 0 ? (
          <EmptyState title="No data" description="Nothing in this range yet." />
        ) : (
          <ResponsiveContainer width="100%" height={220}>
            <AreaChart data={data} margin={{ left: -16, right: 16, top: 8 }}>
              <defs>
                <linearGradient id={`trend-${title}`} x1="0" y1="0" x2="0" y2="1">
                  <stop offset="5%" stopColor={color} stopOpacity={0.35} />
                  <stop offset="95%" stopColor={color} stopOpacity={0} />
                </linearGradient>
              </defs>
              <CartesianGrid strokeDasharray="3 3" stroke="hsl(var(--border))" vertical={false} />
              <XAxis
                dataKey="day"
                tickFormatter={formatDay}
                stroke="hsl(var(--muted-foreground))"
                fontSize={12}
              />
              <YAxis allowDecimals={false} stroke="hsl(var(--muted-foreground))" fontSize={12} />
              <Tooltip
                labelFormatter={(label) => formatDay(String(label))}
                formatter={(value: number) => valueFormatter(value)}
                contentStyle={{
                  backgroundColor: "hsl(var(--card))",
                  border: "1px solid hsl(var(--border))",
                  borderRadius: "var(--radius)",
                  fontSize: 12,
                }}
              />
              <Area
                type="monotone"
                dataKey="value"
                stroke={color}
                fill={`url(#trend-${title})`}
                strokeWidth={2}
              />
            </AreaChart>
          </ResponsiveContainer>
        )}
      </CardContent>
    </Card>
  );
}
