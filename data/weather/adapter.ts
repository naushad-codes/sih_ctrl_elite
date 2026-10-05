export type WeatherSourceType = 'HISTORICAL_WEATHER' | 'DEMO_SYNTHETIC' | 'MANUAL_DEMO_ENTRY'
export type WeatherQuality = 'GOOD' | 'SUSPECT' | 'BAD' | 'UNKNOWN'
export type WeatherRecord = { timestamp: string; station: string; temperature_c: number | null; pressure_hpa: number | null; relative_humidity_pct: number | null; wind_speed: number | null; wind_direction_deg: number | null; source: string; source_url?: string; source_type: WeatherSourceType; quality: WeatherQuality }
export type WeatherManifest = { dataset_id: string; status: 'NOT_CONFIGURED' | 'CONFIGURED'; candidate_sources: Array<{ name: string; source_type: WeatherSourceType; reuse_status: string }> }

/** Validate and normalize a local weather row. This adapter never fetches external data. */
export function adaptWeatherRow(row: Record<string, unknown>): WeatherRecord {
  const numberOrNull = (value: unknown) => typeof value === 'number' && Number.isFinite(value) ? value : null
  const sourceType = ['DEMO_SYNTHETIC', 'MANUAL_DEMO_ENTRY'].includes(String(row.source_type)) ? row.source_type as WeatherSourceType : 'HISTORICAL_WEATHER'
  const quality = ['GOOD', 'SUSPECT', 'BAD', 'UNKNOWN'].includes(String(row.quality)) ? row.quality as WeatherQuality : 'UNKNOWN'
  if (typeof row.timestamp !== 'string' || typeof row.station !== 'string') throw new Error('Weather row requires timestamp and station')
  return { timestamp: row.timestamp, station: row.station, temperature_c: numberOrNull(row.temperature_c), pressure_hpa: numberOrNull(row.pressure_hpa), relative_humidity_pct: numberOrNull(row.relative_humidity_pct), wind_speed: numberOrNull(row.wind_speed), wind_direction_deg: numberOrNull(row.wind_direction_deg), source: typeof row.source === 'string' ? row.source : 'UNKNOWN', source_url: typeof row.source_url === 'string' ? row.source_url : undefined, source_type: sourceType, quality }
}
