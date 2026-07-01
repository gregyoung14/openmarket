use crate::models::Candle;
use ta::indicators::{RelativeStrengthIndex, MovingAverageConvergenceDivergence, ExponentialMovingAverage};
use ta::{Next, DataItem};

pub struct IndicatorResult {
    pub rsi: Option<f64>,
    pub macd: Option<f64>,
    pub macd_signal: Option<f64>,
    pub macd_histogram: Option<f64>,
    pub ema7: Option<f64>,
    pub ema25: Option<f64>,
}

pub struct TaState {
    rsi: RelativeStrengthIndex,
    macd: MovingAverageConvergenceDivergence,
    ema7: ExponentialMovingAverage,
    ema25: ExponentialMovingAverage,
}

impl TaState {
    pub fn new() -> Self {
        Self {
            rsi: RelativeStrengthIndex::new(14).unwrap(),
            macd: MovingAverageConvergenceDivergence::new(12, 26, 9).unwrap(),
            ema7: ExponentialMovingAverage::new(7).unwrap(),
            ema25: ExponentialMovingAverage::new(25).unwrap(),
        }
    }

    pub fn next(&mut self, candle: &Candle) -> IndicatorResult {
        let rsi_val = self.rsi.next(candle.close);
        let macd_val = self.macd.next(candle.close);
        let ema7_val = self.ema7.next(candle.close);
        let ema25_val = self.ema25.next(candle.close);

        IndicatorResult {
            rsi: Some(rsi_val),
            macd: Some(macd_val.macd),
            macd_signal: Some(macd_val.signal),
            macd_histogram: Some(macd_val.histogram),
            ema7: Some(ema7_val),
            ema25: Some(ema25_val),
        }
    }
}
