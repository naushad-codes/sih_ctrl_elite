import type { StateValue } from './types'

export type EmulatorInputs = { power_available_kw: number | null; critical_load_kw: number | null; deferrable_load_kw: number | null; fuel_l: number | null; indoor_temperature_c: number | null; outdoor_temperature_c: number | null; water_production_lph: number | null; water_storage_l: number | null; connectivity: 'CONNECTED' | 'OFFLINE' | 'UNKNOWN' }
export type EmulatorSnapshot = { [K in keyof EmulatorInputs]: StateValue<EmulatorInputs[K]> }

/** Deterministic synthetic fixture. It is independent of all forecast/decision model code. */
export function createSyntheticSnapshot(input: EmulatorInputs, timestamp: string, revision: number, quality: 'GOOD' | 'SUSPECT' | 'BAD' | 'UNKNOWN' = 'GOOD'): EmulatorSnapshot {
  return Object.fromEntries(Object.entries(input).map(([key, value]) => [key, { value, source_kind: 'DEMO_SYNTHETIC', timestamp, unit: key.endsWith('_kw') ? 'kW' : key.endsWith('_lph') ? 'L/h' : key.endsWith('_l') ? 'L' : key.endsWith('_c') ? '°C' : 'state', quality, revision }])) as EmulatorSnapshot
}
