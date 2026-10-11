"""In-process metrics with a Prometheus text rendering (NFR-012)."""

from __future__ import annotations


class Metrics:
    def __init__(self) -> None:
        self._gauges: dict[str, float] = {}
        self._counters: dict[str, float] = {}
        self._samples: dict[str, list[float]] = {}
        self._help: dict[str, str] = {}

    def describe(self, name: str, text: str) -> None:
        self._help[name] = text

    def gauge(self, name: str, value: float) -> None:
        self._gauges[name] = float(value)

    def inc(self, name: str, amount: float = 1.0) -> None:
        self._counters[name] = self._counters.get(name, 0.0) + amount

    def observe(self, name: str, value: float, keep: int = 500) -> None:
        samples = self._samples.setdefault(name, [])
        samples.append(float(value))
        if len(samples) > keep:
            del samples[: len(samples) - keep]

    def counter(self, name: str) -> float:
        return self._counters.get(name, 0.0)

    def quantile(self, name: str, q: float) -> float:
        samples = sorted(self._samples.get(name, []))
        if not samples:
            return 0.0
        return samples[min(len(samples) - 1, int(q * len(samples)))]

    def snapshot(self) -> dict[str, float]:
        data = {**self._gauges, **self._counters}
        for name in self._samples:
            data[f"{name}_p50"] = self.quantile(name, 0.50)
            data[f"{name}_p95"] = self.quantile(name, 0.95)
            data[f"{name}_count"] = float(len(self._samples[name]))
        return data

    def render(self) -> str:
        lines: list[str] = []
        for name, value in sorted(self._gauges.items()):
            if name in self._help:
                lines.append(f"# HELP hlbot_{name} {self._help[name]}")
            lines.append(f"# TYPE hlbot_{name} gauge")
            lines.append(f"hlbot_{name} {value:g}")
        for name, value in sorted(self._counters.items()):
            if name in self._help:
                lines.append(f"# HELP hlbot_{name} {self._help[name]}")
            lines.append(f"# TYPE hlbot_{name} counter")
            lines.append(f"hlbot_{name}_total {value:g}")
        for name in sorted(self._samples):
            lines.append(f"# TYPE hlbot_{name} summary")
            lines.append(f'hlbot_{name}{{quantile="0.5"}} {self.quantile(name, 0.5):g}')
            lines.append(f'hlbot_{name}{{quantile="0.95"}} {self.quantile(name, 0.95):g}')
            lines.append(f"hlbot_{name}_count {len(self._samples[name])}")
        return "\n".join(lines) + "\n"
