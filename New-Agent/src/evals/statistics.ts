export type ConfidenceInterval = {
  level: 0.95;
  lower: number;
  upper: number;
  method: "wilson";
};

/**
 * Wilson score interval is stable for small samples and extreme pass rates where
 * the normal approximation often produces impossible intervals below 0 or above 1.
 */
export function wilsonInterval(successes: number, total: number): ConfidenceInterval | undefined {
  if (!Number.isInteger(successes) || !Number.isInteger(total) || total <= 0 || successes < 0 || successes > total) {
    return undefined;
  }
  const z = 1.959963984540054;
  const rate = successes / total;
  const denominator = 1 + (z * z) / total;
  const center = (rate + (z * z) / (2 * total)) / denominator;
  const margin = z * Math.sqrt((rate * (1 - rate) + (z * z) / (4 * total)) / total) / denominator;
  return {
    level: 0.95,
    lower: Math.max(0, center - margin),
    upper: Math.min(1, center + margin),
    method: "wilson",
  };
}
