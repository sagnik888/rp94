"""
apex_emacross/options_module.py
CE / PE strike selection for NSE index options.
Mirrors Pine Script getStrike() in Section 7.
"""


def calc_atm(price: float, interval: float) -> float:
    """Round price to nearest strike interval."""
    return round(price / interval) * interval


def get_strike(
    is_ce:    bool,
    conf:     float,
    entry_px: float,
    config,
) -> float:
    """
    Select CE or PE strike based on strike_mode and confluence.

    Pine equivalent: getStrike(isCE, conf, ep) in SECTION 7.

    Returns:
        float strike price
    """
    interval = config.strike_interval
    atm      = calc_atm(entry_px, interval)

    mode = config.strike_mode

    if mode == "Always ATM":
        return atm
    elif mode == "Always OTM1":
        return (atm + interval) if is_ce else (atm - interval)
    elif mode == "Always ITM1":
        return (atm - interval) if is_ce else (atm + interval)
    else:   # "Smart Auto"
        # High confidence + trending + high vol → OTM1 (leveraged)
        # Moderate confidence → ATM
        # Low confidence → ITM1 (safer delta)
        high_vol    = getattr(config, "_cached_high_vol", False)
        is_trending = getattr(config, "_cached_is_trending", False)

        if conf >= 85.0 and high_vol and is_trending:
            return (atm + interval) if is_ce else (atm - interval)
        elif conf >= 70.0:
            return atm
        else:
            return (atm - interval) if is_ce else (atm + interval)
