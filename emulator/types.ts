export type ProvenanceKind = 'DOCUMENTED' | 'HISTORICAL_WEATHER' | 'DEMO_SYNTHETIC' | 'MANUAL_DEMO_ENTRY' | 'PROJECTED' | 'UNKNOWN'
export type Quality = 'GOOD' | 'SUSPECT' | 'BAD' | 'UNKNOWN'
export type StateValue<T = number | string | null> = { value: T; source_kind: ProvenanceKind; timestamp: string | null; unit: string; quality: Quality; revision: number; model_version?: string }
export type StationProfile = { id: 'bharati' | 'maitri'; name: string; manifest_version: string; parameters: Record<string, { value: number | null; unit: string; provenance: 'DOCUMENTED' | 'DEMO ASSUMPTION RANGE' | 'UNKNOWN' }> }
