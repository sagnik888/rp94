"""AUTO-TRANSCRIBED candlestick expressions from supplied Pine v6. Do not edit casually.

These are score-driving rules, not drawings. See PINE_ORIGINAL_REFERENCE.pine.
"""

def nz(x, default=0.0):
    return default if x is None or x!=x else x

def s1_pattern(ctx, lag, cfg):
    # Source section 3c. All declarations correspond to Pine rules.
    open, high, low, close = (ctx[k] for k in ('open','high','low','close'))
    atrV, ema9, ema21, bullTrend, bearTrend = (ctx[k] for k in ('atrV','ema9','ema21','bullTrend','bearTrend'))
    volAbove, volSpike, bar_index = (ctx[k] for k in ('volAbove','volSpike','bar_index'))
    hi_prev20,lo_prev20 = ctx['hi_prev20'],ctx['lo_prev20']
    c_body0,c_body1,c_body2 = abs(close-open),abs(lag('close',1)-lag('open',1)),abs(lag('close',2)-lag('open',2))
    c_range0,c_range1,c_range2 = high-low,lag('high',1)-lag('low',1),lag('high',2)-lag('low',2)
    c_uWick0 = high-max(close,open)
    c_lWick0 = min(close,open)-low
    c_uWick1 = lag('high',1)-max(lag('close',1),lag('open',1))
    c_lWick1 = min(lag('close',1),lag('open',1))-lag('low',1)
    c_uWick2 = lag('high',2)-max(lag('close',2),lag('open',2))
    c_lWick2 = min(lag('close',2),lag('open',2))-lag('low',2)
    c_bull0,c_bear0 = close>open,close<open
    c_bull1,c_bear1 = lag('close',1)>lag('open',1),lag('close',1)<lag('open',1)
    c_bull2,c_bear2 = lag('close',2)>lag('open',2),lag('close',2)<lag('open',2)
    c_doji0 = c_range0>0 and c_body0<=c_range0*0.10
    c_doji1 = c_range1>0 and c_body1<=c_range1*0.10
    p_minBody,p_bigBody = nz(atrV)*.15,nz(atrV)*.55
    showPatterns=cfg.s1_patterns
    enableS1=cfg.enable_s1
    showPatLabels=False
    patSingleGrp,patDoubleGrp,patTripleGrp,patContGrp=(cfg.s1_pat_single,cfg.s1_pat_double,cfg.s1_pat_triple,cfg.s1_pat_cont)
    p_nearRes  = high >= hi_prev20 * 0.997
    p_nearSup  = low  <= lo_prev20  * 1.003
    p_priorDn1 = bar_index >= 2 and c_bear1
    p_priorDn2 = bar_index >= 3 and c_bear1 and c_bear2
    p_priorUp1 = bar_index >= 2 and c_bull1
    p_priorUp2 = bar_index >= 3 and c_bull1 and c_bull2
    p_inDnTrnd = close < ema21 and p_priorDn1
    p_inUpTrnd = close > ema21 and p_priorUp1
    p_bullRevC = p_priorDn2 or (p_priorDn1 and close < ema21) or (p_nearSup and p_priorDn1)
    p_bearRevC = p_priorUp2 or (p_priorUp1 and close > ema21) or (p_nearRes and p_priorUp1)
    p_patSzOK  = c_range0 >= nz(atrV) * 0.25
    p_volOK    = volAbove or volSpike
    s1_patternActive = showPatterns and True and (enableS1 or showPatLabels)
    pat_doji            = s1_patternActive and patSingleGrp and c_doji0 and c_range0 >= p_minBody
    pat_dragonfly       = s1_patternActive and patSingleGrp and c_doji0 and c_range0 >= p_minBody and c_lWick0 >= c_range0 * 0.65 and c_uWick0 <= c_range0 * 0.10
    pat_gravestone      = s1_patternActive and patSingleGrp and c_doji0 and c_range0 >= p_minBody and c_uWick0 >= c_range0 * 0.65 and c_lWick0 <= c_range0 * 0.10
    pat_hammerStruct    = s1_patternActive and patSingleGrp and c_body0 >= p_minBody and c_lWick0 >= c_body0 * 2.0 and c_uWick0 <= c_body0 * 0.35
    pat_invHammerStruct = s1_patternActive and patSingleGrp and c_body0 >= p_minBody and c_uWick0 >= c_body0 * 2.0 and c_lWick0 <= c_body0 * 0.35
    pat_bullMarubozu    = s1_patternActive and patSingleGrp and c_bull0 and c_body0 >= p_bigBody and c_uWick0 <= c_body0 * 0.05 and c_lWick0 <= c_body0 * 0.05
    pat_bearMarubozu    = s1_patternActive and patSingleGrp and c_bear0 and c_body0 >= p_bigBody and c_uWick0 <= c_body0 * 0.05 and c_lWick0 <= c_body0 * 0.05
    pat_bullBeltHold    = s1_patternActive and patSingleGrp and c_bull0 and c_body0 >= p_bigBody * 0.65 and c_lWick0 <= c_body0 * 0.06
    pat_bearBeltHold    = s1_patternActive and patSingleGrp and c_bear0 and c_body0 >= p_bigBody * 0.65 and c_uWick0 <= c_body0 * 0.06
    pat_bullEngulf   = s1_patternActive and patDoubleGrp and bar_index >= 2 and c_bear1 and c_bull0 and c_body1 >= p_minBody and c_body0 >= p_minBody and open <= lag("close",1) and close >= lag("open",1)
    pat_bearEngulf   = s1_patternActive and patDoubleGrp and bar_index >= 2 and c_bull1 and c_bear0 and c_body1 >= p_minBody and c_body0 >= p_minBody and open >= lag("close",1) and close <= lag("open",1)
    pat_bullHarami   = s1_patternActive and patDoubleGrp and bar_index >= 2 and c_bear1 and c_bull0 and c_body1 >= p_bigBody * 0.65 and c_body0 < c_body1 * 0.55 and open > lag("close",1) and close < lag("open",1)
    pat_bearHarami   = s1_patternActive and patDoubleGrp and bar_index >= 2 and c_bull1 and c_bear0 and c_body1 >= p_bigBody * 0.65 and c_body0 < c_body1 * 0.55 and open < lag("close",1) and close > lag("open",1)
    pat_bullHaramiX  = s1_patternActive and patDoubleGrp and bar_index >= 2 and c_bear1 and c_doji0 and c_body1 >= p_bigBody * 0.65 and high <= lag("open",1) and low >= lag("close",1)
    pat_bearHaramiX  = s1_patternActive and patDoubleGrp and bar_index >= 2 and c_bull1 and c_doji0 and c_body1 >= p_bigBody * 0.65 and high <= lag("close",1) and low >= lag("open",1)
    pat_piercing     = s1_patternActive and patDoubleGrp and bar_index >= 2 and c_bear1 and c_bull0 and c_body1 >= p_bigBody * 0.55 and open < lag("close",1) and close > (lag("close",1) + (lag("open",1) - lag("close",1)) * 0.50) and close < lag("open",1)
    pat_darkCloud    = s1_patternActive and patDoubleGrp and bar_index >= 2 and c_bull1 and c_bear0 and c_body1 >= p_bigBody * 0.55 and open > lag("close",1) and close < (lag("open",1) + (lag("close",1) - lag("open",1)) * 0.50) and close > lag("open",1)
    pat_tweezBot     = s1_patternActive and patDoubleGrp and bar_index >= 2 and c_bear1 and c_bull0 and abs(low - lag("low",1)) <= nz(atrV) * 0.12
    pat_tweezTop     = s1_patternActive and patDoubleGrp and bar_index >= 2 and c_bull1 and c_bear0 and abs(high - lag("high",1)) <= nz(atrV) * 0.12
    pat_bullKicker   = s1_patternActive and patDoubleGrp and bar_index >= 2 and c_bear1 and c_bull0 and c_body1 >= p_bigBody * 0.55 and c_body0 >= p_bigBody * 0.55 and open >= lag("open",1)
    pat_bearKicker   = s1_patternActive and patDoubleGrp and bar_index >= 2 and c_bull1 and c_bear0 and c_body1 >= p_bigBody * 0.55 and c_body0 >= p_bigBody * 0.55 and open <= lag("open",1)
    pat_bullMeetLine = s1_patternActive and patDoubleGrp and bar_index >= 2 and c_bear1 and c_bull0 and c_body1 >= p_bigBody * 0.55 and c_body0 >= p_bigBody * 0.55 and abs(close - lag("close",1)) <= nz(atrV) * 0.08
    pat_bearMeetLine = s1_patternActive and patDoubleGrp and bar_index >= 2 and c_bull1 and c_bear0 and c_body1 >= p_bigBody * 0.55 and c_body0 >= p_bigBody * 0.55 and abs(close - lag("close",1)) <= nz(atrV) * 0.08
    pat_morningStar     = s1_patternActive and patTripleGrp and bar_index >= 3 and c_bear2 and c_body1 <= c_body2 * 0.45 and c_bull0 and c_body2 >= p_bigBody * 0.55 and c_body0 >= p_bigBody * 0.40 and close >= (lag("close",2) + (lag("open",2) - lag("close",2)) * 0.50)
    pat_eveningStar     = s1_patternActive and patTripleGrp and bar_index >= 3 and c_bull2 and c_body1 <= c_body2 * 0.45 and c_bear0 and c_body2 >= p_bigBody * 0.55 and c_body0 >= p_bigBody * 0.40 and close <= (lag("open",2) + (lag("close",2) - lag("open",2)) * 0.50)
    pat_morningStarDoji = s1_patternActive and patTripleGrp and bar_index >= 3 and c_bear2 and c_doji1 and c_bull0 and c_body2 >= p_bigBody * 0.55 and c_body0 >= p_bigBody * 0.40 and close >= (lag("close",2) + (lag("open",2) - lag("close",2)) * 0.50)
    pat_eveningStarDoji = s1_patternActive and patTripleGrp and bar_index >= 3 and c_bull2 and c_doji1 and c_bear0 and c_body2 >= p_bigBody * 0.55 and c_body0 >= p_bigBody * 0.40 and close <= (lag("open",2) + (lag("close",2) - lag("open",2)) * 0.50)
    pat_3whiteSoldiers  = s1_patternActive and patTripleGrp and bar_index >= 3 and c_bull0 and c_bull1 and c_bull2 and close > lag("close",1) and lag("close",1) > lag("close",2) and c_body0 >= p_bigBody*0.40 and c_body1 >= p_bigBody*0.40 and c_body2 >= p_bigBody*0.40 and c_uWick0 <= c_body0*0.35 and c_uWick1 <= c_body1*0.35 and c_uWick2 <= c_body2*0.35 and open > lag("open",1) and lag("open",1) > lag("open",2)
    pat_3blackCrows     = s1_patternActive and patTripleGrp and bar_index >= 3 and c_bear0 and c_bear1 and c_bear2 and close < lag("close",1) and lag("close",1) < lag("close",2) and c_body0 >= p_bigBody*0.40 and c_body1 >= p_bigBody*0.40 and c_body2 >= p_bigBody*0.40 and c_lWick0 <= c_body0*0.35 and c_lWick1 <= c_body1*0.35 and c_lWick2 <= c_body2*0.35 and open < lag("open",1) and lag("open",1) < lag("open",2)
    pat_3insideUp       = s1_patternActive and patTripleGrp and bar_index >= 3 and c_bear2 and c_bull1 and c_bull0 and c_body2 >= p_bigBody*0.55 and lag("open",1) >= lag("close",2) and lag("close",1) <= lag("open",2) and close > lag("open",2)
    pat_3insideDown     = s1_patternActive and patTripleGrp and bar_index >= 3 and c_bull2 and c_bear1 and c_bear0 and c_body2 >= p_bigBody*0.55 and lag("open",1) <= lag("close",2) and lag("close",1) >= lag("open",2) and close < lag("open",2)
    pat_3outsideUp      = s1_patternActive and patTripleGrp and bar_index >= 3 and c_bear2 and c_bull1 and c_bull0 and c_body2 >= p_minBody and c_body1 >= c_body2 * 0.90 and lag("open",1) <= lag("close",2) and lag("close",1) >= lag("open",2) and close > lag("close",1)
    pat_3outsideDown    = s1_patternActive and patTripleGrp and bar_index >= 3 and c_bull2 and c_bear1 and c_bear0 and c_body2 >= p_minBody and c_body1 >= c_body2 * 0.90 and lag("open",1) >= lag("close",2) and lag("close",1) <= lag("open",2) and close < lag("close",1)
    pat_bullAbandBaby   = s1_patternActive and patTripleGrp and bar_index >= 3 and c_bear2 and c_doji1 and c_bull0 and c_body2 >= p_bigBody*0.50 and c_body0 >= p_bigBody*0.35 and lag("high",1) <= min(lag("open",2), lag("close",2)) and low >= max(lag("open",1), lag("close",1))
    pat_bearAbandBaby   = s1_patternActive and patTripleGrp and bar_index >= 3 and c_bull2 and c_doji1 and c_bear0 and c_body2 >= p_bigBody*0.50 and c_body0 >= p_bigBody*0.35 and lag("low",1)  >= max(lag("open",2), lag("close",2)) and high <= min(lag("open",1), lag("close",1))
    pat_rising3      = s1_patternActive and patContGrp and bar_index >= 5 and lag("close",4) > lag("open",4) and (lag("close",4)-lag("open",4)) >= p_bigBody*0.50 and lag("close",3) < lag("open",3) and lag("close",2) < lag("open",2) and lag("close",1) < lag("open",1) and lag("high",3) < lag("high",4) and lag("high",2) < lag("high",4) and lag("high",1) < lag("high",4) and lag("low",3) > lag("low",4) and lag("low",2) > lag("low",4) and lag("low",1) > lag("low",4) and c_bull0 and close > lag("close",4)
    pat_falling3     = s1_patternActive and patContGrp and bar_index >= 5 and lag("close",4) < lag("open",4) and (lag("open",4)-lag("close",4)) >= p_bigBody*0.50 and lag("close",3) > lag("open",3) and lag("close",2) > lag("open",2) and lag("close",1) > lag("open",1) and lag("low",3) > lag("low",4) and lag("low",2) > lag("low",4) and lag("low",1) > lag("low",4) and lag("high",3) < lag("high",4) and lag("high",2) < lag("high",4) and lag("high",1) < lag("high",4) and c_bear0 and close < lag("close",4)
    pat_upsideTasuki = s1_patternActive and patContGrp and bar_index >= 3 and c_bull2 and c_bull1 and c_bear0 and lag("open",1) > lag("close",2) and open < lag("close",1) and close > lag("close",2) and close < lag("open",1)
    pat_downsideTasuki= s1_patternActive and patContGrp and bar_index >= 3 and c_bear2 and c_bear1 and c_bull0 and lag("open",1) < lag("close",2) and open > lag("close",1) and close < lag("close",2) and close > lag("open",1)
    rawBullSingle = pat_dragonfly or (pat_hammerStruct and p_inDnTrnd) or (pat_invHammerStruct and p_inDnTrnd) or pat_bullBeltHold or pat_bullMarubozu
    rawBearSingle = pat_gravestone or (pat_hammerStruct and p_inUpTrnd) or (pat_invHammerStruct and p_inUpTrnd) or pat_bearBeltHold or pat_bearMarubozu
    rawBullDouble = pat_bullEngulf or pat_bullHarami or pat_bullHaramiX or pat_piercing or pat_tweezBot or pat_bullKicker or pat_bullMeetLine
    rawBearDouble = pat_bearEngulf or pat_bearHarami or pat_bearHaramiX or pat_darkCloud or pat_tweezTop or pat_bearKicker or pat_bearMeetLine
    rawBullTriple = pat_morningStar or pat_morningStarDoji or pat_3whiteSoldiers or pat_3insideUp or pat_3outsideUp or pat_bullAbandBaby
    rawBearTriple = pat_eveningStar or pat_eveningStarDoji or pat_3blackCrows or pat_3insideDown or pat_3outsideDown or pat_bearAbandBaby
    rawBullCont   = pat_rising3 or pat_upsideTasuki
    rawBearCont   = pat_falling3 or pat_downsideTasuki
    validBullRev  = (rawBullSingle or rawBullDouble or rawBullTriple) and p_patSzOK and p_volOK and p_bullRevC
    validBearRev  = (rawBearSingle or rawBearDouble or rawBearTriple) and p_patSzOK and p_volOK and p_bearRevC
    validBullCont = rawBullCont and p_patSzOK and p_volOK and bullTrend
    validBearCont = rawBearCont and p_patSzOK and p_volOK and bearTrend
    patValidBull = validBullRev or validBullCont
    patValidBear = validBearRev or validBearCont
    patRawBull  = rawBullSingle or rawBullDouble or rawBullTriple or rawBullCont
    patRawBear  = rawBearSingle or rawBearDouble or rawBearTriple or rawBearCont
    patFakeBull = patRawBull and not patValidBull
    patFakeBear = patRawBear and not patValidBear
    patStrongBull = (patValidBull and (pat_bullEngulf or pat_3whiteSoldiers or pat_morningStar or pat_morningStarDoji or pat_bullAbandBaby or pat_3insideUp or pat_3outsideUp or pat_bullKicker or pat_rising3))
    patStrongBear = (patValidBear and (pat_bearEngulf or pat_3blackCrows or pat_eveningStar or pat_eveningStarDoji or pat_bearAbandBaby or pat_3insideDown or pat_3outsideDown or pat_bearKicker or pat_falling3))
    # Pine names are used for setup labels; priorities follow source.
    bullish=[('Aband.Baby U',pat_bullAbandBaby),('3 White Sold',pat_3whiteSoldiers),('Morn.StarDji',pat_morningStarDoji),('Morning Star',pat_morningStar),('3 Inside Up',pat_3insideUp),('3 Outside Up',pat_3outsideUp),('Bull Kicker',pat_bullKicker),('Bull Engulf',pat_bullEngulf),('HaramiCross+',pat_bullHaramiX),('Bull Harami',pat_bullHarami),('Piercing',pat_piercing),('TweezBot',pat_tweezBot),('MeetLines+',pat_bullMeetLine),('Rising3Meth',pat_rising3),('Up Tasuki',pat_upsideTasuki),('Hammer',pat_hammerStruct and p_inDnTrnd),('Inv.Hammer',pat_invHammerStruct and p_inDnTrnd),('Dragonfly',pat_dragonfly),('Marubozu+',pat_bullMarubozu),('BeltHold+',pat_bullBeltHold)]
    bearish=[('Aband.Baby D',pat_bearAbandBaby),('3 Blk Crows',pat_3blackCrows),('Eve.StarDoji',pat_eveningStarDoji),('Evening Star',pat_eveningStar),('3 Inside Dn',pat_3insideDown),('3 Outside Dn',pat_3outsideDown),('Bear Kicker',pat_bearKicker),('Bear Engulf',pat_bearEngulf),('HaramiCross-',pat_bearHaramiX),('Bear Harami',pat_bearHarami),('DarkCloud',pat_darkCloud),('TweezTop',pat_tweezTop),('MeetLines-',pat_bearMeetLine),('Falling3Meth',pat_falling3),('Dn Tasuki',pat_downsideTasuki),('HangingMan',pat_hammerStruct and p_inUpTrnd),('Shoot.Star',pat_invHammerStruct and p_inUpTrnd),('Gravestone',pat_gravestone),('Marubozu-',pat_bearMarubozu),('BeltHold-',pat_bearBeltHold)]
    return dict(bull=patValidBull,bear=patValidBear,strong_bull=patStrongBull,strong_bear=patStrongBear,bull_name=next((n for n,v in bullish if v), '') if patValidBull else '',bear_name=next((n for n,v in bearish if v),'') if patValidBear else '')

def s2_pattern(ctx, lag, cfg):
    open, high, low, close = (ctx[k] for k in ('open','high','low','close'))
    atrV, ema9, ema21, bar_index = (ctx[k] for k in ('atrV','ema9','ema21','bar_index'))
    avg_body14, avg_range14 = ctx['avg_body14'],ctx['avg_range14']
    s2_showCandlePatterns=cfg.s2_patterns
    enableS2=cfg.enable_s2
    s2_body0  = abs(close - open)
    s2_body1  = abs(lag("close",1) - lag("open",1))
    s2_body2  = abs(lag("close",2) - lag("open",2))
    s2_body3  = abs(lag("close",3) - lag("open",3))
    s2_upW    = high - max(open, close)
    s2_dnW    = min(open, close) - low
    s2_upW1   = lag("high",1) - max(lag("open",1), lag("close",1))
    s2_rng0   = high - low
    s2_avgBod = avg_body14
    s2_avgRng = avg_range14
    s2_iB  = close > open
    s2_iR  = close < open
    s2_iB1 = lag("close",1) > lag("open",1)
    s2_iR1 = lag("close",1) < lag("open",1)
    s2_iB2 = lag("close",2) > lag("open",2)
    s2_iR2 = lag("close",2) < lag("open",2)
    s2_iB3 = lag("close",3) > lag("open",3)
    s2_iR3 = lag("close",3) < lag("open",3)
    s2_mid1  = (lag("open",1) + lag("close",1)) / 2.0
    s2_bTop1 = max(lag("open",1), lag("close",1))
    s2_bBot1 = min(lag("open",1), lag("close",1))
    s2_tiny0  = s2_body0 < s2_avgBod * 0.3
    s2_small0 = s2_body0 < s2_avgBod * 0.6
    s2_long0  = s2_body0 > s2_avgBod * 1.2
    s2_vlong0 = s2_body0 > s2_avgBod * 1.8
    s2_tiny1  = s2_body1 < s2_avgBod * 0.3
    s2_small1 = s2_body1 < s2_avgBod * 0.6
    s2_long1  = s2_body1 > s2_avgBod * 1.2
    s2_vlong1 = s2_body1 > s2_avgBod * 1.8
    s2_isDoji = s2_tiny0 and s2_rng0 > s2_avgRng * 0.5
    s2_inDnTrend = close < ema21 and ema9 < ema21 and close < lag("close",3)
    s2_inUpTrend = close > ema21 and ema9 > ema21 and close > lag("close",3)
    s2_pBullScore = 0.0
    s2_pBearScore = 0.0
    if s2_showCandlePatterns and enableS2 and True:
        if s2_tiny0 and s2_dnW > s2_rng0 * 0.65 and s2_upW < s2_rng0 * 0.1 and s2_inDnTrend:
            s2_pBullScore += 14.0
        if s2_small0 and s2_dnW >= s2_body0 * 2 and s2_upW < s2_body0 * 0.5 and s2_inDnTrend:
            s2_pBullScore += 15.0
        if s2_body0 < s2_avgBod * 0.4 and s2_dnW >= s2_body0 * 3.5 and s2_upW < s2_body0 * 0.3 and s2_inDnTrend:
            s2_pBullScore += 18.0
        if s2_small0 and s2_upW >= s2_body0 * 2 and s2_dnW < s2_body0 * 0.5 and s2_inDnTrend:
            s2_pBullScore += 12.0
        if s2_iB and s2_long0 and s2_upW < s2_body0 * 0.05 and s2_dnW < s2_body0 * 0.05:
            s2_pBullScore += 14.0
        if s2_iB and s2_long0 and s2_dnW < s2_body0 * 0.05 and s2_inDnTrend:
            s2_pBullScore += 12.0
        if s2_tiny0 and s2_upW > s2_rng0 * 0.65 and s2_dnW < s2_rng0 * 0.1 and s2_inUpTrend:
            s2_pBearScore += 14.0
        if s2_small0 and s2_upW >= s2_body0 * 2 and s2_dnW < s2_body0 * 0.5 and s2_inUpTrend:
            s2_pBearScore += 15.0
        if s2_small0 and s2_dnW >= s2_body0 * 2 and s2_upW < s2_body0 * 0.5 and s2_inUpTrend:
            s2_pBearScore += 13.0
        if s2_iR and s2_long0 and s2_upW < s2_body0 * 0.05 and s2_dnW < s2_body0 * 0.05:
            s2_pBearScore += 14.0
        if s2_iR and s2_long0 and s2_upW < s2_body0 * 0.05 and s2_inUpTrend:
            s2_pBearScore += 12.0
        if s2_iR1 and s2_iB and s2_body0 > s2_body1 and open <= lag("close",1) and close >= lag("open",1) and s2_inDnTrend:
            s2_pBullScore += 20.0
        if s2_iR1 and s2_long1 and s2_iB and open < lag("low",1) and close > s2_mid1 and close < lag("open",1) and s2_inDnTrend:
            s2_pBullScore += 15.0
        if s2_iR1 and s2_long1 and s2_iB and s2_small0 and open > lag("close",1) and close < lag("open",1) and s2_inDnTrend:
            s2_pBullScore += 12.0
        if s2_iR1 and s2_long1 and s2_isDoji and open > lag("close",1) and close < lag("open",1) and s2_inDnTrend:
            s2_pBullScore += 14.0
        if abs(low - lag("low",1)) < atrV * 0.05 and s2_iR1 and s2_iB and s2_inDnTrend:
            s2_pBullScore += 12.0
        if s2_iR1 and s2_vlong1 and s2_iB and s2_vlong0 and open > lag("open",1) and low > lag("high",1):
            s2_pBullScore += 25.0
        if s2_iR1 and s2_long1 and s2_isDoji and open < lag("close",1) and s2_inDnTrend:
            s2_pBullScore += 13.0
        if s2_iR1 and s2_long1 and s2_iB and s2_long0 and abs(close - lag("close",1)) < atrV * 0.1 and open < lag("low",1):
            s2_pBullScore += 11.0
        if s2_iR1 and s2_iR and abs(close - lag("close",1)) < atrV * 0.03 and s2_inDnTrend:
            s2_pBullScore += 10.0
        if s2_iR1 and s2_iR and s2_body0 < s2_body1 and high < lag("high",1) and low > lag("low",1) and s2_inDnTrend:
            s2_pBullScore += 10.0
        if s2_iB1 and s2_iR and s2_body0 > s2_body1 and open >= lag("close",1) and close <= lag("open",1) and s2_inUpTrend:
            s2_pBearScore += 20.0
        if s2_iB1 and s2_long1 and s2_iR and open > lag("high",1) and close < s2_mid1 and close > lag("open",1) and s2_inUpTrend:
            s2_pBearScore += 15.0
        if s2_iB1 and s2_long1 and s2_iR and s2_small0 and open < lag("close",1) and close > lag("open",1) and s2_inUpTrend:
            s2_pBearScore += 12.0
        if s2_iB1 and s2_long1 and s2_isDoji and open < lag("close",1) and close > lag("open",1) and s2_inUpTrend:
            s2_pBearScore += 14.0
        if abs(high - lag("high",1)) < atrV * 0.05 and s2_iB1 and s2_iR and s2_inUpTrend:
            s2_pBearScore += 12.0
        if s2_iB1 and s2_vlong1 and s2_iR and s2_vlong0 and open < lag("open",1) and high < lag("low",1):
            s2_pBearScore += 25.0
        if s2_iB1 and s2_long1 and s2_isDoji and open > lag("close",1) and s2_inUpTrend:
            s2_pBearScore += 13.0
        if s2_iB1 and s2_long1 and s2_iR and s2_long0 and abs(close - lag("close",1)) < atrV * 0.1 and open > lag("high",1):
            s2_pBearScore += 11.0
        if s2_iB1 and s2_iB and abs(close - lag("close",1)) < atrV * 0.03 and s2_inUpTrend:
            s2_pBearScore += 10.0
        if s2_iR2 and s2_body2 > s2_avgBod and s2_small1 and s2_iB and s2_long0 and close > s2_mid1 and lag("open",1) < lag("close",2):
            s2_pBullScore += 22.0
        if s2_iR2 and s2_body2 > s2_avgBod and s2_tiny1 and s2_iB and s2_long0 and close > (lag("close",2) + lag("open",2)) / 2.0:
            s2_pBullScore += 22.0
        if s2_iB2 and s2_iB1 and s2_iB and s2_body2 > s2_avgBod * 0.8 and s2_body1 > s2_avgBod * 0.8 and s2_body0 > s2_avgBod * 0.8 and close > lag("close",1) and lag("close",1) > lag("close",2) and open > lag("open",1) and lag("open",1) > lag("open",2):
            s2_pBullScore += 20.0
        if s2_iR2 and s2_body2 > s2_avgBod and s2_tiny1 and lag("high",1) < lag("low",2) and s2_iB and low > lag("high",1):
            s2_pBullScore += 25.0
        if s2_iR2 and s2_body2 > s2_avgBod and s2_iB1 and s2_body1 < s2_body2 and lag("open",1) > lag("close",2) and lag("close",1) < lag("open",2) and s2_iB and close > lag("open",2):
            s2_pBullScore += 16.0
        if s2_iR2 and s2_iB1 and s2_body1 > s2_body2 and lag("open",1) <= lag("close",2) and lag("close",1) >= lag("open",2) and s2_iB and close > lag("close",1):
            s2_pBullScore += 16.0
        if s2_isDoji and s2_tiny1 and abs(lag("close",2) - lag("open",2)) < s2_avgBod * 0.3 and lag("low",1) < lag("low",2) and lag("low",1) < low:
            s2_pBullScore += 15.0
        if s2_iB2 and s2_body2 > s2_avgBod and s2_small1 and s2_iR and s2_long0 and close < (lag("close",2) + lag("open",2)) / 2.0 and lag("open",1) > lag("close",2):
            s2_pBearScore += 22.0
        if s2_iB2 and s2_body2 > s2_avgBod and s2_tiny1 and s2_iR and s2_long0 and close < (lag("close",2) + lag("open",2)) / 2.0:
            s2_pBearScore += 22.0
        if s2_iR2 and s2_iR1 and s2_iR and s2_body2 > s2_avgBod * 0.8 and s2_body1 > s2_avgBod * 0.8 and s2_body0 > s2_avgBod * 0.8 and close < lag("close",1) and lag("close",1) < lag("close",2) and open < lag("open",1) and lag("open",1) < lag("open",2):
            s2_pBearScore += 20.0
        if s2_iB2 and s2_body2 > s2_avgBod and s2_tiny1 and lag("low",1) > lag("high",2) and s2_iR and high < lag("low",1):
            s2_pBearScore += 25.0
        if s2_iB2 and s2_body2 > s2_avgBod and s2_iR1 and s2_body1 < s2_body2 and lag("open",1) < lag("close",2) and lag("close",1) > lag("open",2) and s2_iR and close < lag("open",2):
            s2_pBearScore += 16.0
        if s2_iB2 and s2_iR1 and s2_body1 > s2_body2 and lag("open",1) >= lag("close",2) and lag("close",1) <= lag("open",2) and s2_iR and close < lag("close",1):
            s2_pBearScore += 16.0
        if s2_isDoji and s2_tiny1 and abs(lag("close",2) - lag("open",2)) < s2_avgBod * 0.3 and lag("high",1) > lag("high",2) and lag("high",1) > high:
            s2_pBearScore += 15.0
        if s2_iB2 and s2_iB1 and s2_iB and close > lag("close",1) and lag("close",1) > lag("close",2) and s2_body0 < s2_body1 and s2_body1 < s2_body2 and s2_upW > s2_body0 * 0.5 and s2_upW1 > s2_body1 * 0.5:
            s2_pBearScore += 12.0
        if s2_iB2 and s2_iB1 and s2_iB and s2_body2 > s2_avgBod and s2_body1 > s2_avgBod and s2_small0 and close > lag("close",1):
            s2_pBearScore += 10.0
        if s2_iB3 and s2_iB2 and s2_iB1 and lag("close",1) > lag("close",2) and lag("close",2) > lag("close",3) and s2_iR and s2_body0 > (s2_body1 + s2_body2 + s2_body3) * 0.7 and close < lag("open",3):
            s2_pBearScore += 22.0
        if s2_iR3 and s2_iR2 and s2_iR1 and lag("close",1) < lag("close",2) and lag("close",2) < lag("close",3) and s2_iB and s2_body0 > (s2_body1 + s2_body2 + s2_body3) * 0.7 and close > lag("open",3):
            s2_pBullScore += 18.0
        s2_insBar2 = lag("high",1) < lag("high",2) and lag("low",1) > lag("low",2)
        if s2_insBar2 and low < lag("low",1) and close > lag("high",1) and s2_iB:
            s2_pBullScore += 14.0
        if s2_insBar2 and high > lag("high",1) and close < lag("low",1) and s2_iR:
            s2_pBearScore += 14.0
        if bar_index > 4 and s2_iB3 and s2_body3 > s2_avgBod and lag("low",2) > lag("low",3) and lag("low",1) > lag("low",3) and lag("high",2) < lag("high",3) and lag("high",1) < lag("high",3) and s2_iB and close > lag("high",3) and s2_body0 > s2_avgBod * 0.8:
            s2_pBullScore += 14.0
        if bar_index > 4 and s2_iR3 and s2_body3 > s2_avgBod and lag("high",2) < lag("high",3) and lag("high",1) < lag("high",3) and lag("low",2) > lag("low",3) and lag("low",1) > lag("low",3) and s2_iR and close < lag("low",3) and s2_body0 > s2_avgBod * 0.8:
            s2_pBearScore += 14.0
        if s2_iB2 and s2_iB1 and lag("open",1) > lag("close",2) and s2_iR and open > s2_bBot1 and open < s2_bTop1 and close > lag("open",2) and close < lag("close",2):
            s2_pBullScore += 12.0
        if s2_iR2 and s2_iR1 and lag("open",1) < lag("close",2) and s2_iB and open < s2_bTop1 and open > s2_bBot1 and close < lag("open",2) and close > lag("close",2):
            s2_pBearScore += 12.0
        if s2_iR2 and s2_iB1 and s2_iR and abs(close - lag("close",2)) < atrV * 0.05 and s2_inDnTrend:
            s2_pBullScore += 11.0
    s2_pBullScore = min(s2_pBullScore, 35.0)
    s2_pBearScore = min(s2_pBearScore, 35.0)
    return s2_pBullScore,s2_pBearScore
