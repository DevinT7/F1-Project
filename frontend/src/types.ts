// Mirror of backend/app/models/schemas.py — keep in sync via PR (shared contract).

export interface Session {
  session_key: number;
  meeting_key: number;
  year: number;
  country_name?: string | null;
  circuit_short_name?: string | null;
  session_name?: string | null;
  session_type?: string | null;
  date_start?: string | null;
}

export interface Driver {
  driver_number: number;
  full_name?: string | null;
  name_acronym?: string | null;
  team_name?: string | null;
  team_colour?: string | null;
  headshot_url?: string | null;
}

export interface TelemetrySample {
  distance: number;
  speed?: number | null;
  throttle?: number | null;
  brake?: number | null;
  n_gear?: number | null;
  rpm?: number | null;
  drs?: number | null;
}

export interface TelemetryTrace {
  session_key: number;
  driver_number: number;
  lap_number?: number | null;
  lap_time?: number | null;
  // True when lap_time is a statistical outlier vs. this driver's other laps
  // this session (traffic / safety car / VSC / red flag). See LapComparison.warnings.
  is_anomalous_lap?: boolean;
  samples: TelemetrySample[];
}

export interface CornerInsight {
  corner_number: number;
  corner_name?: string | null;
  distance: number;
  entry_speed?: number | null;
  min_speed?: number | null;
  exit_speed?: number | null;
  braking_point_m?: number | null;
  braking_delta_m?: number | null;      // driver - reference; negative = driver braked earlier
  throttle_delay_s?: number | null;     // driver - reference; positive = driver later to full throttle
  min_speed_delta_kmh?: number | null;  // driver - reference; negative = driver carried less speed
  time_delta_s: number;
}

export interface SectorDelta { sector: number; delta_s: number; }
export interface DeltaPoint { distance: number; delta_s: number; }

export interface LapComparison {
  session_key: number;
  driver: Driver;
  reference: Driver;
  lap_time?: number | null;
  reference_lap_time?: number | null;
  total_delta_s: number;
  time_gained_s: number;
  time_lost_s: number;
  sector_deltas: SectorDelta[];
  delta_trace: DeltaPoint[];
  corners: CornerInsight[];
  driver_trace?: TelemetryTrace | null;
  reference_trace?: TelemetryTrace | null;
  // Human-readable caveats about this comparison (e.g. an anomalous lap). [] = none.
  warnings: string[];
}

export interface LapPacePrediction {
  laps_ahead: number;
  predicted_lap_time_s: number;
  lower_s: number;
  upper_s: number;
}

export interface TireAnalysis {
  session_key: number;
  driver_number: number;
  compound?: string | null;
  tyre_age_laps?: number | null;
  degradation_s_per_lap: number;
  remaining_competitive_laps?: number | null;
  recommended_pit_window?: [number, number] | null;
  model_confidence: number;
  predictions: LapPacePrediction[];
  model_name: string;
}

export interface AIInsight { label: string; delta_s?: number | null; }
export interface AIReport {
  summary: string;
  insights: AIInsight[];
  recommendations: string[];
  strategy_options: string[];
  source: string;
  grounded: boolean;
}
