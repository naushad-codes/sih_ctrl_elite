import { lazy, Suspense, useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { ArrowDown, ArrowRight, Box, RefreshCw, RotateCcw, Wind } from 'lucide-react'
import { TwinSnapshot } from './TwinSnapshot'
import type { TwinNodeId } from './TwinScene3D'

const TwinScene3D=lazy(()=>import('./TwinScene3D').then(module=>({default:module.TwinScene3D})))

const API=import.meta.env.VITE_API_URL||'http://127.0.0.1:8000'
type Props={token:string;station:{id:string;code:string;name:string};onNavigate:(target:'SCENARIO_LAB',scenarioId?:string)=>void}
type StateValue={field:string;value:any;unit:string;source_type:string;observed_at?:string;revision?:number;model_version?:string;quality?:string}
const COMPONENTS:Array<{id:TwinNodeId;title:string;field:string;projection:string;unit:string;summary:string}>=[
 {id:'fuel',title:'Fuel storage',field:'fuel_reserve',projection:'fuel_l',unit:'L',summary:'Modeled fuel reserve. Storage capacity is not represented.'},
 {id:'chp',title:'Combined Heat & Power',field:'chp_available',projection:'',unit:'',summary:'CHP availability is a station state or a scenario assumption.'},
 {id:'power',title:'Power distribution',field:'power_available',projection:'power_available_kw',unit:'kW',summary:'Modeled electrical power available to the load categories.'},
 {id:'heat',title:'Thermal system',field:'heat_indoor',projection:'heat_indoor_c',unit:'°C',summary:'Indoor temperature state. CHP thermal output is not modeled.'},
 {id:'water_production',title:'Water production',field:'water_production',projection:'water_production_lph',unit:'L/h',summary:'Modeled water production rate.'},
 {id:'water_storage',title:'Water storage',field:'water_reserve',projection:'water_l',unit:'L',summary:'Modeled water reserve. Storage capacity is not represented.'},
 {id:'critical_load',title:'Critical load',field:'critical_load',projection:'critical_load_kw',unit:'kW',summary:'Critical load category in the operational model.'},
 {id:'deferrable_load',title:'Deferrable load',field:'deferrable_load',projection:'deferrable_load_kw',unit:'kW',summary:'Deferrable load category in the operational model.'},
]
const initialScenario=()=>new URLSearchParams(location.hash.split('?')[1]||'').get('scenario_id')||''
const fmt=(value:any)=>typeof value==='number'&&Number.isFinite(value)?new Intl.NumberFormat('en-IN',{maximumFractionDigits:2}).format(value):value===null||value===undefined||value===''?'Unavailable':String(value)
const date=(value?:string|null)=>value?new Date(value).toLocaleString():'Not recorded'
const human=(value:string)=>value.replaceAll('_',' ').toUpperCase()

export function DigitalTwinWorkspace({token,station,onNavigate}:Props){
 const [data,setData]=useState<any|null>(null),[scenarios,setScenarios]=useState<any[]>([]),[selectedScenario,setSelectedScenario]=useState<any|null>(null)
 const [selectedId,setSelectedId]=useState<TwinNodeId|null>(null),[loading,setLoading]=useState(true),[scenarioLoading,setScenarioLoading]=useState(false),[error,setError]=useState(''),[scenarioError,setScenarioError]=useState(''),[sceneError,setSceneError]=useState(false)
 const [weatherStatus,setWeatherStatus]=useState<string>('CHECKING'),[weatherBusy,setWeatherBusy]=useState(false),[weatherMessage,setWeatherMessage]=useState('')
 const weatherRefreshStarted=useRef<string|null>(null)
 const [source,setSource]=useState<any|null>(null)
 const stateByField=useMemo(()=>Object.fromEntries(((data?.state||[]) as StateValue[]).map(x=>[x.field,x])),[data])
 const scenario=selectedScenario
 const projected=scenario?.result?.projected||{}
 const selectedComponent=COMPONENTS.find(item=>item.id===selectedId)
 const reducedMotion=typeof window!=='undefined'&&window.matchMedia('(prefers-reduced-motion: reduce)').matches

 const load=useCallback(async()=>{setLoading(true);setError('');try{
   const headers={Authorization:`Bearer ${token}`}
   const [snapshotResponse,scenarioResponse]=await Promise.all([fetch(`${API}/api/hq/command-center`,{headers}),fetch(`${API}/hq/scenarios/${station.id}`,{headers})])
   const [snapshot,history]=await Promise.all([snapshotResponse.json(),scenarioResponse.json()])
   if(!snapshotResponse.ok)throw new Error(snapshot.detail||'Station state unavailable')
   if(!scenarioResponse.ok)throw new Error(history.detail||'Saved scenario list unavailable')
   setData(snapshot);setScenarios(history);if(snapshot.weather)setWeatherStatus(old=>old==='CHECKING'||old==='UNAVAILABLE'?'AVAILABLE':old)
   const wanted=initialScenario()
   if(wanted){setScenarioLoading(true);const resultResponse=await fetch(`${API}/hq/scenarios/${station.id}/${encodeURIComponent(wanted)}`,{headers});const result=await resultResponse.json();if(resultResponse.ok)setSelectedScenario(result);else setScenarioError(result.detail||'Linked scenario is not available for this station');setScenarioLoading(false)}
  }catch(e){setError(e instanceof Error?e.message:'Could not load Digital Twin data')}finally{setLoading(false)}},[token,station.id])
 const refreshWeather=useCallback(async(force=false)=>{setWeatherBusy(true);setWeatherMessage('');try{const response=await fetch(`${API}/api/weather/official/${station.id}/refresh${force?'?force=true':''}`,{method:'POST',headers:{Authorization:`Bearer ${token}`}});const result=await response.json();if(!response.ok)throw new Error(result.detail||'NCPOR refresh failed');setWeatherStatus(result.status||'UNAVAILABLE');setWeatherMessage(result.message||'');if(result.observation)setData((old:any)=>old?{...old,weather:result.observation}:old)}catch(e){setWeatherMessage(e instanceof Error?e.message:'NCPOR observation refresh failed');setWeatherStatus((old:string)=>old==='AVAILABLE'||old==='LAST_NCPOR_OBSERVATION'?old:'UNAVAILABLE')}finally{setWeatherBusy(false)}},[token,station.id])
 useEffect(()=>{void load();if(weatherRefreshStarted.current!==station.id){weatherRefreshStarted.current=station.id;void refreshWeather()}},[load,refreshWeather,station.id])

 const selectScenario=useCallback(async(id:string)=>{setScenarioError('');if(!id){setSelectedScenario(null);history.replaceState(null,'',`#/app/hq/digital-twin`);return}setScenarioLoading(true);try{const response=await fetch(`${API}/hq/scenarios/${station.id}/${encodeURIComponent(id)}`,{headers:{Authorization:`Bearer ${token}`}});const result=await response.json();if(!response.ok)throw new Error(result.detail||'Scenario is unavailable for this station');setSelectedScenario(result);history.replaceState(null,'',`#/app/hq/digital-twin?scenario_id=${encodeURIComponent(id)}`)}catch(e){setScenarioError(e instanceof Error?e.message:'Could not load scenario result')}finally{setScenarioLoading(false)}},[token,station.id])
 const current=(id:TwinNodeId)=>{const component=COMPONENTS.find(x=>x.id===id)!;return stateByField[component.field] as StateValue|undefined}
 const shownValue=(id:TwinNodeId)=>{const component=COMPONENTS.find(x=>x.id===id)!;if(id==='chp'){const value=scenario?scenario.inputs?.chp_available:current(id)?.value;if(typeof value==='boolean')return value?'AVAILABLE':'UNAVAILABLE';if(typeof value==='string')return value.toLowerCase()==='true'?'AVAILABLE':value.toLowerCase()==='false'?'UNAVAILABLE':value.toUpperCase();return 'NOT RECORDED'}const value=scenario?projected[component.projection]:current(id)?.value;return value===null||value===undefined?'Unavailable':`${fmt(value)}${component.unit?` ${component.unit}`:''}`}
 const provenance=(id:TwinNodeId)=>scenario?'SCENARIO PROJECTION':current(id)?.source_type||'SOURCE NOT RECORDED'
 const openSource=(item:any)=>setSource(item)

 const chpInput=scenario?.inputs?.chp_available
 const baselineChp=current('chp')?.value
 const chpStatus: 'AVAILABLE'|'UNAVAILABLE'|'UNKNOWN'=scenario&&typeof chpInput==='boolean'?(chpInput?'AVAILABLE':'UNAVAILABLE'):typeof baselineChp==='boolean'?(baselineChp?'AVAILABLE':'UNAVAILABLE'):'UNKNOWN'
 const flowState=useMemo(()=>{
   const flow=(projectedValue:any,baselineValue:any):'ACTIVE'|'CONSTRAINED'|'UNKNOWN'=>{const value=scenario?projectedValue:baselineValue;if(typeof value!=='number')return 'UNKNOWN';return value>0?'ACTIVE':'CONSTRAINED'}
   const constraints=scenario?.result?.constraints||[]
   const thermal=constraints.find((item:any)=>item.code==='THERMAL_DEFICIT')
   const heat: 'ACTIVE'|'CONSTRAINED'|'UNKNOWN'=scenario?(thermal?.status==='UNKNOWN'?'UNKNOWN':chpInput===false?'CONSTRAINED':'ACTIVE'):(current('heat')?.value===undefined?'UNKNOWN':'ACTIVE')
   return {chp:chpStatus,power:flow(projected.power_available_kw,current('power')?.value),heat,water:flow(projected.water_production_lph,current('water_production')?.value),scenario:!!scenario}
 },[scenario,projected,chpStatus,chpInput,stateByField])

 const onSelect=useCallback((id:TwinNodeId)=>setSelectedId(id),[])
 const onSceneFailure=useCallback(()=>setSceneError(true),[])
 const resetView=()=>window.dispatchEvent(new CustomEvent('pratibimb:twin-reset-view'))
 const constraintFor=(code:string)=>scenario?.result?.constraints?.find((item:any)=>item.code===code)
 const comparisons=[
   {label:'Power',currentField:'power_available',projectedField:'power_available_kw',unit:'kW'},
   {label:'Indoor temperature',currentField:'heat_indoor',projectedField:'heat_indoor_c',unit:'°C'},
   {label:'Fuel',currentField:'fuel_reserve',projectedField:'fuel_l',unit:'L'},
   {label:'Water reserve',currentField:'water_reserve',projectedField:'water_l',unit:'L'},
 ]

 return <main className="digital-twin-shell twin-experience">
  <header className="digital-twin-header twin-experience-header"><div><span className="header-kicker">HQ · READ-ONLY MODEL · NO EQUIPMENT CONTROL</span><h1>{station.name.replace(' Station','').toUpperCase()} · DIGITAL TWIN</h1><p>Revision #{data?.revision??'—'} · current station state remains separate from scenario projection.</p></div><button className="station-icon" onClick={()=>void load()} aria-label="Refresh station state and saved scenarios"><RefreshCw size={16}/></button></header>
  {error&&<div className="rom-alert" role="alert">{error}<button onClick={()=>void load()}>Retry</button></div>}
  <section className="twin-context-bar"><div className="twin-scenario-picker"><label htmlFor="twin-scenario">SCENARIO CONTEXT</label><select id="twin-scenario" value={scenario?.id||''} onChange={event=>void selectScenario(event.target.value)} disabled={scenarioLoading}><option value="">CURRENT STATION STATE</option>{scenarios.map(item=><option key={item.id} value={item.id}>{item.name} · {item.id}</option>)}</select><button onClick={()=>onNavigate('SCENARIO_LAB')}>OPEN SCENARIO LAB <ArrowRight size={14}/></button></div><div className="twin-source-context"><span><small>CURRENT STATE</small><b>{[...new Set((data?.state||[]).map((item:StateValue)=>item.source_type).filter(Boolean))].join(' · ')||'NO RECORDED STATE'}</b></span>{scenario&&<span className="projection-context"><small>PROJECTED RESULT · {scenario.model_version}</small><b>{scenario.name} · BASE REV #{scenario.base_revision}</b></span>}</div></section>
  {scenarioError&&<p className="twin-inline-error" role="alert">{scenarioError}</p>}
  <section className="twin-workspace-grid">
   <div className="twin-visual-column">
    <section className="twin-visual-panel"><header><div><span className="header-kicker">DIGITAL TWIN REPRESENTATION · NOT CONNECTED TO STATION EQUIPMENT</span><h2>ENERGY &amp; WATER DEPENDENCIES</h2></div><div className="twin-view-actions"><button onClick={resetView}><RotateCcw size={14}/> RESET VIEW</button><span><Box size={14}/> THREE-DIMENSIONAL MODEL</span></div></header>
     {loading?<div className="twin-loading">Loading station-scoped state and saved scenarios…</div>:sceneError?<div className="twin-fallback"><div><span className="twin-fallback-tag">3D VISUALIZATION UNAVAILABLE · 2D FALLBACK</span><h3>Operational dependency model</h3><p>The read-only model remains available below.</p></div><TwinSnapshot large state={data?.state||[]} revision={data?.revision} onSource={(field,value,metadata)=>openSource({field,value,metadata:{...metadata,station:station.name}})}/></div>:<Suspense fallback={<div className="twin-loading">Preparing interactive 3D model…</div>}><TwinScene3D selected={selectedId} state={flowState} reducedMotion={reducedMotion} onSelect={onSelect} onFailure={onSceneFailure}/></Suspense>}
     {!loading&&!sceneError&&<footer className="twin-scene-footer"><button onClick={()=>setSelectedId('fuel')}>FUEL</button><ArrowRight/><button onClick={()=>setSelectedId('chp')}>CHP</button><ArrowRight/><button onClick={()=>setSelectedId('power')}>POWER</button><span>+</span><button onClick={()=>setSelectedId('heat')}>THERMAL STATE</button><ArrowRight/><button onClick={()=>setSelectedId('water_production')}>WATER PRODUCTION</button><ArrowRight/><button onClick={()=>setSelectedId('water_storage')}>WATER STORAGE</button><ArrowDown/><button onClick={()=>setSelectedId('critical_load')}>CRITICAL LOAD</button><span>/</span><button onClick={()=>setSelectedId('deferrable_load')}>DEFERRABLE LOAD</button></footer>}
    </section>
    <section className="twin-flow-legend" aria-label="Visualization legend"><span><i className="flow-power"/>POWER FLOW · {flowState.power}</span><span><i className="flow-heat"/>THERMAL PATH · {flowState.heat}</span><span><i className="flow-water"/>WATER FLOW · {flowState.water}</span><span><i className="flow-current"/>CURRENT STATE</span>{scenario&&<span><i className="flow-projection"/>SCENARIO PROJECTION</span>}</section>
    {scenario&&<section className="twin-projection-panel"><header><div><span className="header-kicker">PERSISTED DETERMINISTIC MODEL RESULT · {scenario.model_version}</span><h2>SCENARIO PROJECTION</h2></div><span>SCENARIO ASSUMPTION · PROJECTED</span></header><div className="twin-scenario-sequence"><span>BASELINE<br/><b>CURRENT STATE</b></span><ArrowRight/><span>ASSUMPTION<br/><b>{scenario.name}</b></span><ArrowRight/><span>PROJECTED STATE<br/><b>{scenario.result?.projected?'RECORDED':'NOT AVAILABLE'}</b></span><ArrowRight/><span>CONSTRAINT<br/><b>{scenario.result?.constraints?.some((x:any)=>x.status==='CONDITIONAL')?'CONDITIONAL':scenario.result?.constraints?.some((x:any)=>x.status==='UNKNOWN')?'UNKNOWN':'SEE MODEL RESULT'}</b></span></div><div className="twin-constraint-list">{scenario.result?.constraints?.map((item:any,index:number)=><article key={`${item.code}-${index}`}><small>{human(item.code)}</small><b className={`constraint-${String(item.status).toLowerCase()}`}>{human(item.status)}</b><span>{item.explanation||item.message||'No explanation recorded.'}</span></article>)}</div></section>}
    {scenario&&<section className="twin-comparison-panel"><header><div><span className="header-kicker">OPTIONAL · COMPARABLE VALUES ONLY</span><h2>BASELINE VS PROJECTION</h2></div></header><div className="twin-comparison-grid">{comparisons.map(item=>{const before=stateByField[item.currentField]?.value,after=projected[item.projectedField],comparable=typeof before==='number'&&typeof after==='number';return <article key={item.label}><small>{item.label}</small><b>{comparable?`${fmt(before)} ${item.unit} → ${fmt(after)} ${item.unit}`:'NOT COMPARABLE'}</b></article>})}</div></section>}
   <section className="twin-environment-panel" data-weather-status={weatherStatus}><header><div><span className="header-kicker">SEPARATE OBSERVATION DOMAIN</span><h2>ENVIRONMENT · NCPOR</h2></div><button className="station-icon" type="button" onClick={()=>void refreshWeather(true)} disabled={weatherBusy} aria-label="Refresh NCPOR official observation" title="Fetch the latest public NCPOR weather observation"><Wind size={17}/></button></header>{data?.weather?<><div className="twin-weather-values">{[['Temperature',data.weather.temperature_c,'°C'],['Pressure',data.weather.pressure_hpa,'hPa'],['Humidity',data.weather.relative_humidity_pct,'%'],['Wind',data.weather.wind_speed,data.weather.wind_speed_unit||'m/s'],['Direction',data.weather.wind_direction_deg,'°']].map(([label,value,unit])=><span key={String(label)}><small>{label}</small><b>{fmt(value)} {unit}</b></span>)}</div><p>{weatherStatus==='LAST_NCPOR_OBSERVATION'?'LAST NCPOR OBSERVATION':'NCPOR OFFICIAL OBSERVATION'} · observed {date(data.weather.observation_time)} · fetched {date(data.weather.fetched_at)} · {data.weather.quality||'QUALITY UNKNOWN'}</p></>:<p className="twin-weather-unavailable">{weatherStatus}: no stored NCPOR official observation is available for this station.</p>}{weatherBusy&&<p role="status">Fetching latest NCPOR observation…</p>}{weatherMessage&&<p role="status">{weatherMessage}</p>}</section>
   </div>
   <aside className="twin-inspector"><header><span className="header-kicker">COMPONENT INSPECTOR · READ ONLY</span><h2>{selectedComponent?.title||'DIGITAL TWIN'}</h2><p>{selectedComponent?.summary||'Select a modeled component in the scene or component list to inspect its state and provenance.'}</p></header>
    {selectedComponent?<><div className="twin-inspector-primary"><small>{scenario?'PROJECTED VALUE':'CURRENT MODELED VALUE'}</small><b>{shownValue(selectedComponent.id)}</b><span>{provenance(selectedComponent.id)}</span></div>{selectedComponent.id==='chp'?<><div className="twin-inspector-section"><h3>STATUS</h3><b className={`twin-status status-${(scenario?(chpInput?'available':'unavailable'):chpStatus.toLowerCase())}`}>{scenario?`${chpInput?'AVAILABLE':'UNAVAILABLE'} · SCENARIO ASSUMPTION`:chpStatus==='UNKNOWN'?'NOT RECORDED':chpStatus}</b></div><div className="twin-inspector-section"><h3>INPUT</h3><p>Fuel · {shownValue('fuel')}</p><h3>MODELED CONNECTIONS</h3><p>Power distribution · {shownValue('power')}</p><p>Thermal system · indoor temperature {shownValue('heat')}</p><b className="not-modeled">CHP THERMAL OUTPUT · NOT MODELED</b></div></>:<div className="twin-inspector-section"><h3>{selectedComponent.id==='heat'?'THERMAL STATE':'MODEL DETAIL'}</h3><p>{selectedComponent.id==='heat'?'Indoor temperature is a station thermal state, not CHP heat output.':selectedComponent.summary}</p>{selectedComponent.id==='power'&&<><h3>PROJECTED CONSTRAINT</h3><p>{constraintFor('POWER_DEFICIT')?.status||'No scenario constraint selected'}</p></>}{selectedComponent.id==='fuel'&&scenario&&<><h3>FUEL RESERVE PROJECTION</h3><p>{constraintFor('FUEL_RESERVE')?.status||'Not reported'}</p></>}{selectedComponent.id==='water_storage'&&scenario&&<><h3>WATER SHORTAGE PROJECTION</h3><p>{constraintFor('WATER_SHORTAGE')?.status||'Not reported'}</p></>}</div>}
     <dl className="twin-provenance-details"><div><dt>SOURCE TYPE</dt><dd>{provenance(selectedComponent.id)}</dd></div><div><dt>STATION</dt><dd>{station.code}</dd></div><div><dt>REVISION</dt><dd>{scenario?`BASE REV #${scenario.base_revision}`:`REV #${current(selectedComponent.id)?.revision??data?.revision??'—'}`}</dd></div><div><dt>MODEL VERSION</dt><dd>{scenario?.model_version||current(selectedComponent.id)?.model_version||'Not recorded'}</dd></div><div><dt>OBSERVATION TIME</dt><dd>{scenario?'Projection · '+date(scenario.created_at):date(current(selectedComponent.id)?.observed_at)}</dd></div></dl>
    </>:<div className="twin-component-list"><span className="header-kicker">MODELED COMPONENTS</span>{COMPONENTS.map(item=><button key={item.id} onClick={()=>setSelectedId(item.id)}><span>{item.title}</span><b>{shownValue(item.id)}</b><ArrowRight size={14}/></button>)}</div>}
    <div className="twin-inspector-warning">DIGITAL TWIN REPRESENTATION · no real station equipment connection or control.</div>
   </aside>
  </section>
  <section className="twin-bottom-legend"><span>MODEL COMPONENT</span><span>POWER FLOW</span><span>THERMAL FLOW</span><span>WATER FLOW</span><span>CURRENT STATE</span>{scenario&&<span>SCENARIO PROJECTION</span>}<button onClick={()=>onNavigate('SCENARIO_LAB',scenario?.id)}>RUN OR REVIEW SCENARIO <ArrowRight size={14}/></button></section>
  {scenarioLoading&&<div className="cc-refreshing">Loading persisted scenario result…</div>}{loading&&<div className="cc-refreshing">Loading station snapshot…</div>}
  {source&&<div className="cc-drawer-backdrop" onMouseDown={event=>event.target===event.currentTarget&&setSource(null)}><aside className="cc-drawer"><header><div><span className="cc-kicker">VALUE PROVENANCE</span><h2>{source.field}</h2></div><button onClick={()=>setSource(null)} aria-label="Close provenance">×</button></header><h3>{source.value}</h3><dl>{Object.entries(source.metadata||{}).map(([key,value])=><div key={key}><dt>{key.toUpperCase().replaceAll('_',' ')}</dt><dd>{String(value??'Not available')}</dd></div>)}</dl></aside></div>}
 </main>
}
