use crate::models::Candle;
use fin_primitives::FinError;
use fin_primitives::signals::indicators::{Ema, Rsi};
use fin_primitives::signals::{BarInput, Signal, SignalValue};
use rust_decimal::Decimal;
use rust_decimal::prelude::ToPrimitive;

/// Indicator values for one candle. A field is `None` until its indicator has
/// seen enough candles to warm up (RSI(14): 15, EMA(n): n, MACD line: 26,
/// MACD signal and histogram: 34).
pub struct IndicatorResult {
    pub rsi: Option<f64>,
    pub macd: Option<f64>,
    pub macd_signal: Option<f64>,
    pub macd_histogram: Option<f64>,
    pub ema7: Option<f64>,
    pub ema25: Option<f64>,
}

pub struct TaState {
    rsi: Rsi,
    macd_fast: Ema,
    macd_slow: Ema,
    macd_signal: Ema,
    ema7: Ema,
    ema25: Ema,
}

fn value(v: Result<SignalValue, FinError>) -> Option<Decimal> {
    v.ok().and_then(|v| v.as_decimal())
}

fn to_f64(d: Option<Decimal>) -> Option<f64> {
    d.and_then(|d| d.to_f64())
}

impl TaState {
    pub fn new() -> Self {
        Self {
            // Wilder-smoothed RSI, the variant exchange charts plot.
            rsi: Rsi::new("rsi14", 14).unwrap(),
            macd_fast: Ema::new("macd_fast", 12).unwrap(),
            macd_slow: Ema::new("macd_slow", 26).unwrap(),
            macd_signal: Ema::new("macd_signal", 9).unwrap(),
            ema7: Ema::new("ema7", 7).unwrap(),
            ema25: Ema::new("ema25", 25).unwrap(),
        }
    }

    pub fn next(&mut self, candle: &Candle) -> IndicatorResult {
        // A non-finite close cannot be represented; skip it without touching state.
        let Ok(close) = Decimal::try_from(candle.close) else {
            return IndicatorResult {
                rsi: None,
                macd: None,
                macd_signal: None,
                macd_histogram: None,
                ema7: None,
                ema25: None,
            };
        };
        let bar = BarInput::from_close(close);

        let rsi = value(self.rsi.update(&bar));
        let fast = value(self.macd_fast.update(&bar));
        let slow = value(self.macd_slow.update(&bar));
        let ema7 = value(self.ema7.update(&bar));
        let ema25 = value(self.ema25.update(&bar));

        // The signal line is an EMA of the MACD line, so it only starts once the
        // slow EMA is warm.
        let macd = fast.zip(slow).map(|(f, s)| f - s);
        let macd_signal =
            macd.and_then(|m| value(self.macd_signal.update(&BarInput::from_close(m))));
        let macd_histogram = macd.zip(macd_signal).map(|(m, s)| m - s);

        IndicatorResult {
            rsi: to_f64(rsi),
            macd: to_f64(macd),
            macd_signal: to_f64(macd_signal),
            macd_histogram: to_f64(macd_histogram),
            ema7: to_f64(ema7),
            ema25: to_f64(ema25),
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn candle(close: f64) -> Candle {
        Candle {
            interval: "1m".to_string(),
            time: 0,
            open: close,
            high: close,
            low: close,
            close,
            volume: 0.0,
        }
    }

    #[test]
    fn values_stay_none_until_warm() {
        let mut ta = TaState::new();
        for i in 1..=40 {
            let r = ta.next(&candle(100.0 + (i % 5) as f64));
            assert_eq!(r.ema7.is_some(), i >= 7, "ema7 at candle {i}");
            assert_eq!(r.rsi.is_some(), i >= 15, "rsi at candle {i}");
            assert_eq!(r.ema25.is_some(), i >= 25, "ema25 at candle {i}");
            assert_eq!(r.macd.is_some(), i >= 26, "macd at candle {i}");
            assert_eq!(
                r.macd_signal.is_some(),
                i >= 34,
                "macd_signal at candle {i}"
            );
            assert_eq!(
                r.macd_histogram.is_some(),
                i >= 34,
                "macd_histogram at candle {i}"
            );
        }
    }

    #[test]
    fn rising_closes_give_rsi_100_and_positive_macd() {
        let mut ta = TaState::new();
        let mut last = None;
        for i in 0..40 {
            last = Some(ta.next(&candle(100.0 + i as f64)));
        }
        let r = last.unwrap();
        assert_eq!(r.rsi, Some(100.0));
        assert!(r.macd.unwrap() > 0.0);
        assert!(r.ema7.unwrap() > r.ema25.unwrap());
    }

    #[test]
    fn histogram_is_macd_minus_signal() {
        let mut ta = TaState::new();
        let mut last = None;
        for i in 0..60 {
            last = Some(ta.next(&candle(100.0 + ((i * 7) % 11) as f64)));
        }
        let r = last.unwrap();
        let diff = r.macd.unwrap() - r.macd_signal.unwrap() - r.macd_histogram.unwrap();
        assert!(diff.abs() < 1e-9);
    }

    #[test]
    fn non_finite_close_is_skipped() {
        let mut ta = TaState::new();
        for i in 0..7 {
            ta.next(&candle(100.0 + i as f64));
        }
        let r = ta.next(&candle(f64::NAN));
        assert!(r.ema7.is_none());
        assert!(ta.next(&candle(107.0)).ema7.is_some());
    }
}
