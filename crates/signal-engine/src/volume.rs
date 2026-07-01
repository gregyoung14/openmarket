pub fn hourly_volume_rate(window_vol: f64, window_duration_secs: f64) -> f64 {
    let hours = (window_duration_secs / 3600.0).max(0.001);
    window_vol / hours
}

#[derive(Debug, Clone)]
pub struct VolumeMedianEstimator {
    observations: Vec<f64>,
    max_observations: usize,
}

impl VolumeMedianEstimator {
    pub fn with_capacity(max_observations: usize) -> Self {
        Self {
            observations: Vec::with_capacity(max_observations),
            max_observations,
        }
    }

    pub fn observe(&mut self, hourly_vol: f64) {
        if self.observations.len() >= self.max_observations {
            self.observations.remove(0);
        }
        self.observations.push(hourly_vol);
    }

    pub fn median(&self) -> f64 {
        if self.observations.is_empty() {
            return 0.0;
        }
        let mut sorted = self.observations.clone();
        sorted.sort_by(|a, b| a.partial_cmp(b).unwrap_or(std::cmp::Ordering::Equal));
        let mid = sorted.len() / 2;
        if sorted.len().is_multiple_of(2) {
            (sorted[mid - 1] + sorted[mid]) / 2.0
        } else {
            sorted[mid]
        }
    }

    pub fn len(&self) -> usize {
        self.observations.len()
    }
}
