"""Compose unchanged native engines with explicit result ownership."""
import copy
from engine.scanner_modes import COMBINED_MODES


class CombinedSignalEngine:
    def __init__(self, mode, auto_trend_adx=25., **params):
        from engine.alt_scanners import AltSignalEngine
        self.signal_source = mode
        self.signal_mode = params.get('signal_mode', 'realtime')
        self.base_mode = COMBINED_MODES[mode]
        self.native_engines = {native: AltSignalEngine(signal_source=native,
            auto_trend_adx=auto_trend_adx, **params) for native in (self.base_mode, 's1')}

    def get_state(self, key):
        return self.native_engines[self.base_mode].get_state(key)

    def process(self, df, instrument='', timeframe='', **kwargs):
        # MultiTimeframeEngine owns observation age and invalidation. Each native
        # lane retains its own standalone cache rules and computation clock.
        cached_s1 = kwargs.pop('_s1_cached_result', None)
        cached_base = kwargs.pop('_base_cached_result', None)
        results = {}
        for mode, engine in self.native_engines.items():
            if mode == self.base_mode and cached_base is not None and not cached_base.get('error'):
                results[mode] = cached_base
                continue
            if mode == 's1' and cached_s1 is not None and not cached_s1.get('error'):
                results[mode] = cached_s1
                continue
            try:
                native_kwargs = dict(kwargs)
                if mode == 'quad_prime':
                    native_kwargs.pop('as_of', None)
                results[mode] = engine.process(df=df, instrument=instrument, timeframe=timeframe, **native_kwargs)
            except Exception as exc:
                results[mode] = {'error': str(exc) or type(exc).__name__, 'signals': [], 'state': None,
                                 'source_engine': mode}
        base = results[self.base_mode]
        # Base chart arrays remain intact; both native entry/exit histories are visible.
        merged = dict(base)
        merged.update(source_engine=self.signal_source, native_results=results)
        merged['signals'] = sorted([signal for result in results.values()
            if not result.get('error') for signal in result.get('signals', [])],
            key=lambda signal: (signal.timestamp, signal.signal_type,
                                str(getattr(signal, 'source_engine', ''))))
        merged['indicator_overlays'] = list(base.get('indicator_overlays') or []) + [
            {**overlay, 'name': 'S1 ' + str(overlay.get('name', ''))}
            for overlay in results['s1'].get('indicator_overlays', [])]
        # Never let a base error hide a healthy S1 lane, or bless a missing base state.
        if not base.get('state'):
            merged['state'] = {'position': 0, 'position_label': 'FLAT', 'is_trending': False}
        if any(not result.get('error') for result in results.values()):
            merged.pop('error', None)
        return merged


def native_mtf_view(result, mode):
    """Partition signals, state and confluence before native entry selection."""
    view = copy.copy(result)
    native = {}
    for tf in ('1min', '5min', '15min'):
        envelope = getattr(result, f'results_{tf}', None)
        lane = (envelope or {}).get('native_results', {}).get(mode)
        if lane and lane.get('error'):
            lane = None
        setattr(view, f'results_{tf}', lane)
        if lane:
            native[tf] = lane
    from engine.multi_timeframe import MultiTimeframeEngine
    # The envelope can be new while just one native lane was recomputed. Keep
    # each lane's original observation clock rather than promoting its sibling's
    # computation into fresh evidence for this lane.
    view.timeframe_metadata = {}
    for tf, metadata in (getattr(result, 'timeframe_metadata', {}) or {}).items():
        lane_metadata = dict(metadata)
        native_metadata = ((getattr(result, f'results_{tf}', None) or {})
                           .get('native_result_metadata') or {}).get(mode)
        if native_metadata:
            lane_metadata['result'] = dict(native_metadata)
        view.timeframe_metadata[tf] = lane_metadata
    weights = type('ConfluenceWeights', (), {'tf_weights': {'1min': .15, '5min': .40, '15min': .45}})()
    view.confluence_score, view.confluence_signal = MultiTimeframeEngine._compute_confluence(
        weights, native, result.instrument)
    view.active_signals = [signal for signal in getattr(result, 'active_signals', [])
                          if (getattr(signal, 'source_engine', '') == 's1') == (mode == 's1')]
    return view
