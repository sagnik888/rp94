"""Signal-affecting Pine v5.4 defaults. Chart-only inputs intentionally excluded."""
from dataclasses import dataclass

@dataclass
class Config:
    enable_s1: bool = True
    enable_s2: bool = True
    share_auto_mode: bool = True
    share_target_ladder: bool = True
    block_conflicting_entries: bool = False
    round_trip_cost_pct: float = 0.0
    emit_stop_updates: bool = True
    no_volume_policy: str = 'Price proxy' # or 'Block'
    s2_confirmed_mtf: bool = True
    s1_trade_mode: str = 'Auto'
    instrument_type: str = 'Auto-Detect'
    min_score: float = 60.
    conflict_margin: float = 20.
    min_adx: float = 20.
    use_15m_gate: bool = True
    s1_cooldown_bars: int = 1
    s1_force_eod: bool = True
    s1_eod_cutoff_ist_min: int = 925
    s1_stop_pct: float = .20
    hard_stop_enabled: bool = True
    hard_stop_intraday_pct: float = 1.50
    hard_stop_btst_pct: float = 2.50
    hard_stop_swing_pct: float = 5.00
    hard_stop_positional_pct: float = 8.00
    t1_r: float = 2.
    t2_r: float = 3.
    t3_r: float = 4.
    s1_exit_at_t3: bool = False
    s1_cancel_overnight_cash: bool = True
    s1_trail: bool = True
    s1_trail_start_r: float = 1.5
    s1_trail_atr: float = 1.8
    s1_lock_at_t1: bool = True
    s1_exit_confirm_bars: int = 3
    consecutive_loss_limit: int = 3
    circuit_pause_bars: int = 5
    s1_patterns: bool = True
    s1_pattern_scoring: bool = True
    s1_pat_single: bool = True
    s1_pat_double: bool = True
    s1_pat_triple: bool = True
    s1_pat_cont: bool = True
    s2_trade_mode: str = 'Auto'
    s2_market_region: str = 'Universal'
    s2_settings_mode: str = 'Auto'
    s2_enable_btst: bool = True
    s2_btst_window_min: int = 30
    s2_force_exit_ist_decimal: float = 15.40  # decimal hours, 15.40 = 15:24
    s2_risk_mode: str = 'Dynamic'
    s2_min_stop_pct: float = .10
    s2_dynamic_sensitivity: float = 1.0
    s2_atr_stop_mult: float = 2.
    s2_percent_stop_pct: float = .8
    s2_max_stop_pct: float = 0.
    s2_t1_manual: float = 1.2
    s2_t2_manual: float = 2.5
    s2_t3_manual: float = 4.5
    s2_circuit: bool = True
    s2_require_15m_alignment: bool = False
    s2_confidence_floor: int = 60
    s2_exit_confirm_input: int = 3
    s2_gap_skip: bool = True
    s2_daily_trend_lock: bool = False
    s2_max_bars_in_trade: int = 250
    s2_macd_entry_filter: bool = True
    s2_signal_gap: int = 0
    s2_trailing: bool = True
    s2_trail_start_manual: float = 1.5
    s2_trail_atr_manual: float = 2.5
    s2_lock_at_t1: bool = True
    s2_profit_lock_r: float = .6
    s2_aggressive_trail: bool = False
    s2_tsl_use_close: bool = True
    s2_early_be: bool = False
    s2_early_be_r: float = .8
    s2_exit_mode: str = 'Trend-Following'
    s2_smart_exit: bool = False
    s2_exit_weakness: bool = False
    s2_burst: bool = True
    s2_burst_bars: int = 0
    s2_burst_index_pct: float = .25
    s2_burst_stock_pct: float = .60
    s2_burst_commodity_pct: float = .50
    s2_burst_volume_mult: float = 1.1
    s2_burst_score_boost: int = 20
    s2_patterns: bool = True
    s2_orb: bool = True
    s2_orb_minutes: int = 30
    s2_divergence: bool = True
    s2_div_lookback: int = 14
    s2_div_score_boost: int = 15

    def validate(self):
        """Enforce source Pine `input` ranges (no silent dangerous overrides)."""
        import math
        choices={
            'no_volume_policy':{'Price proxy','Block'},
            's1_trade_mode':{'Auto','Intraday','BTST/STBT','Swing','Positional'},
            's2_trade_mode':{'Auto','Intraday','BTST/STBT','Swing','Positional'},
            's2_market_region':{'Universal','India (NSE/BSE)','US (NYSE/NASDAQ)'},
            's2_settings_mode':{'Auto','Manual'},
            's2_risk_mode':{'Dynamic','ATR','Percentage','Swing'},
            's2_exit_mode':{'Conservative','Balanced','Trend-Following'},
            'instrument_type':{'Auto-Detect','Nifty 50','Bank Nifty','FinNifty',
                               'Midcap','Sensex','Index','Stock','Commodity',
                               'Crypto','Forex','Futures'},
        }
        for field, allowed in choices.items():
            if getattr(self,field) not in allowed:raise ValueError(f'{field} must be one of {sorted(allowed)}')
        # Bounds copied from Pine v5.4 inputs. Chart-only visual settings were
        # deliberately excluded from this Python backend contract.
        bounds={
            'round_trip_cost_pct':(0,5),
            'min_score':(50,90), 'conflict_margin':(10,40), 'min_adx':(15,35),
            's1_cooldown_bars':(0,20), 's1_eod_cutoff_ist_min':(915,930),
            's1_stop_pct':(.05,10),
            'hard_stop_intraday_pct':(.10,1.50),
            'hard_stop_btst_pct':(.50,5), 'hard_stop_swing_pct':(1,10),
            'hard_stop_positional_pct':(8,10),
            't1_r':(1.5,5), 't2_r':(2,6), 't3_r':(2.5,10),
            's1_trail_start_r':(1,3), 's1_trail_atr':(1,3),
            's1_exit_confirm_bars':(2,5), 'consecutive_loss_limit':(2,6),
            'circuit_pause_bars':(5,30),
            's2_btst_window_min':(10,60), 's2_force_exit_ist_decimal':(15,15.5),
            's2_min_stop_pct':(0,5), 's2_dynamic_sensitivity':(.5,2),
            's2_atr_stop_mult':(.25,6), 's2_percent_stop_pct':(.1,10),
            's2_max_stop_pct':(0,15), 's2_t1_manual':(.5,3),
            's2_t2_manual':(1,6), 's2_t3_manual':(2,10),
            's2_confidence_floor':(45,90), 's2_exit_confirm_input':(0,5),
            's2_max_bars_in_trade':(50,500), 's2_signal_gap':(0,10),
            's2_trail_start_manual':(.5,3), 's2_trail_atr_manual':(1,5),
            's2_profit_lock_r':(.3,1.5), 's2_early_be_r':(.2,1.4),
            's2_burst_bars':(0,10), 's2_burst_index_pct':(.1,1),
            's2_burst_stock_pct':(.2,2), 's2_burst_commodity_pct':(.2,2),
            's2_burst_volume_mult':(.8,2), 's2_burst_score_boost':(10,35),
            's2_orb_minutes':(5,90), 's2_div_lookback':(5,30),
            's2_div_score_boost':(5,25),
        }
        for field,(lo,hi) in bounds.items():
            value=getattr(self,field)
            if isinstance(value,bool) or not isinstance(value,(int,float)) or not math.isfinite(value) or not lo<=value<=hi:
                raise ValueError(f'{field} must be finite and within Pine bounds [{lo}, {hi}] (got {value!r})')
        integers={'min_score','conflict_margin','s1_cooldown_bars','s1_eod_cutoff_ist_min',
                  's1_exit_confirm_bars','consecutive_loss_limit','circuit_pause_bars',
                  's2_btst_window_min','s2_confidence_floor','s2_exit_confirm_input',
                  's2_max_bars_in_trade','s2_signal_gap','s2_burst_bars',
                  's2_burst_score_boost','s2_orb_minutes','s2_div_lookback',
                  's2_div_score_boost'}
        for field in integers:
            if isinstance(getattr(self,field),bool) or not float(getattr(self,field)).is_integer():
                raise ValueError(f'{field} must be an integer')
        for field, value in vars(self).items():
            if isinstance(value,bool):continue
            if field in choices or field in bounds:continue
            raise ValueError(f'Unvalidated configuration field: {field}')

MACD_BY_TF = {'1':(3,10,3,.50),'3':(4,12,4,.48),'5':(5,13,5,.42),'15':(8,21,5,.38),
              '30':(10,24,7,.40),'60':(12,26,9,.45),'120':(12,26,9,.50),'240':(14,30,9,.55),
              'D':(12,26,9,.60),'W':(8,21,5,.55),'M':(5,13,5,.50)}

def tf_minutes(tf):
    if tf.isdigit(): return int(tf)
    import re
    m = re.fullmatch(r'(\d+)?([DWM])',tf)
    if not m: raise ValueError('unsupported timeframe '+tf)
    n=int(m[1] or '1')
    return n * {'D':1440,'W':10080,'M':43200}[m[2]]

def auto_mode(minutes):
    return ('Intraday' if minutes<=15 else 'BTST/STBT' if minutes<=60 else 'Swing' if minutes<=240 else 'Positional')

def mode_cap(cfg, mode, subtype='NONE'):
    if subtype in {'BTST','STBT'} or mode=='BTST/STBT': return cfg.hard_stop_btst_pct
    if mode=='Intraday': return cfg.hard_stop_intraday_pct
    if mode=='Swing': return cfg.hard_stop_swing_pct
    return cfg.hard_stop_positional_pct
