import time, threading
class EnergyTracker:
    """Never raises. report always has duration_s/energy_kwh/co2_kg/tracker keys (values may be None)."""
    def __init__(self): self.report = dict(duration_s=None, energy_kwh=None, co2_kg=None, tracker="duration-only"); self._t = None
    def __enter__(self):
        self._t0 = time.perf_counter()
        try:
            from codecarbon import EmissionsTracker
            self._t = EmissionsTracker(save_to_file=False, log_level="error"); self._t.start()
        except BaseException: self._t = None
        return self
    def __exit__(self, *a):
        self.report["duration_s"] = time.perf_counter() - self._t0
        if self._t is not None:
            try:
                co2 = self._t.stop(); d = getattr(self._t, "final_emissions_data", None)
                self.report.update(co2_kg=co2, energy_kwh=getattr(d, "energy_consumed", None), tracker="codecarbon")
            except BaseException: pass
        return False
class InferenceMeter:
    """Serving-time cost. Energy is an ESTIMATE = busy_seconds * assumed_watts (labelled as such)."""
    def __init__(self, flops_per_sample, watts=15.0): self.f, self.w, self.calls, self.samples, self.sec = flops_per_sample, watts, 0, 0, 0.0; self.lock = threading.Lock()
    def record(self, n, sec):
        with self.lock: self.calls += 1; self.samples += n; self.sec += sec
    def report(self):
        return dict(calls=self.calls, samples=self.samples, busy_s=self.sec, total_flops=self.f * self.samples,
                    est_energy_kwh=self.sec * self.w / 3.6e6, assumed_watts=self.w, ms_per_call=(1000 * self.sec / self.calls) if self.calls else None)
