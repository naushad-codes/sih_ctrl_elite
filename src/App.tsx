import { useEffect, useState } from 'react'
import { ArrowLeft, ArrowRight, Check, CircleHelp, Radio, ShieldCheck } from 'lucide-react'
import { RemoteOperations } from './RemoteOperations'
import { StationWorkspace } from './StationWorkspace'
import { TechnicalWorkspace } from './TechnicalWorkspace'

type Step = 1 | 2 | 3
type Station = 'bharati' | 'maitri'
type Role = 'hq' | 'station' | 'technical'
type Session = { token: string; username: string; role: Role; station: Station; created_at: string }

type StationInfo = { id: Station; name: string; location: string; code: string; image: string }
type RoleInfo = { id: Role; title: string; login: string; summary: string; icon: typeof Radio }

const API_URL = import.meta.env.VITE_API_URL ?? (import.meta.env.DEV ? 'http://127.0.0.1:8000' : '')

const stations: StationInfo[] = [
  { id: 'bharati', name: 'Bharati Station', location: 'Larsemann Hills, Antarctica', code: 'BHARATI', image: '/stations/bharati.webp' },
  { id: 'maitri', name: 'Maitri Station', location: 'Schirmacher Oasis, Antarctica', code: 'MAITRI', image: '/stations/maitri.webp' },
]

const roles: RoleInfo[] = [
  { id: 'hq', title: 'HQ Operations Analyst', login: 'admin_hq', summary: 'Station overview, incidents, planning and remote operation proposals.', icon: Radio },
  { id: 'station', title: 'Station Operations Officer', login: 'admin_station', summary: 'Incoming proposals, acknowledgement, local observations and task outcomes.', icon: ShieldCheck },
  { id: 'technical', title: 'Technical Engineer', login: 'admin_tech', summary: 'Technical readings, inspection tasks, asset observations and reports.', icon: CircleHelp },
]

function BackgroundVideo() {
  return (
    <div className="video-layer" aria-hidden="true">
      <video autoPlay muted loop playsInline preload="metadata" poster="/stations/maitri.webp">
        <source src="/hero.webm" type="video/webm" />
        <source src="/hero.mp4" type="video/mp4" />
      </video>
      <div className="video-wash" />
    </div>
  )
}

function Navigation() {
  return (
    <header className="navigation">
      <a className="brand" href="#access" aria-label="PRATIBIMB access">PRATIBIMB</a>
      <nav aria-label="Primary navigation">
        <a href="#about">About</a>
        <a href="#stations">Stations</a>
        <a href="#features">Features</a>
        <a href="#contact">Contact</a>
      </nav>
    </header>
  )
}

function ProgressIndicator({ step }: { step: Step }) {
  return (
    <div className="progress" aria-label={`Step ${step} of 3`}>
      {[['01', 'Select Station'], ['02', 'Select Role'], ['03', 'Sign In']].map(([number, label], index) => {
        const itemStep = (index + 1) as Step
        return (
          <div className={`progress-item ${itemStep === step ? 'active' : ''} ${itemStep < step ? 'complete' : ''}`} key={number}>
            <span className="progress-number">{itemStep < step ? <Check size={13} strokeWidth={2.5} /> : number}</span>
            <span>{label}</span>
          </div>
        )
      })}
    </div>
  )
}

function StationCard({ station, selected, onSelect }: { station: StationInfo; selected: boolean; onSelect: () => void }) {
  return (
    <button className={`station-card ${selected ? 'selected' : ''}`} onClick={onSelect} aria-pressed={selected}>
      <img className="station-photo" src={station.image} alt={`${station.name} station`} />
      <div className="card-copy">
        <span className="selection-mark">{selected && <Check size={13} />}</span>
        <span>
          <em>{station.code}</em>
          <strong>{station.name}</strong>
          <small>{station.location}</small>
        </span>
      </div>
    </button>
  )
}

function StationSelector({ selected, onSelect, onNext }: { selected: Station | null; onSelect: (station: Station) => void; onNext: () => void }) {
  return (
    <section className="step-content">
      <div className="step-heading"><h2>Select Station</h2><p>Choose the Antarctic research station you want to access.</p></div>
      <div className="station-grid">{stations.map((station) => <StationCard key={station.id} station={station} selected={selected === station.id} onSelect={() => onSelect(station.id)} />)}</div>
      <div className="action-row end"><button className="primary-button" disabled={!selected} onClick={onNext}>Next <ArrowRight size={16} /></button></div>
    </section>
  )
}

function RoleSelector({ selected, onSelect, onNext, onBack }: { selected: Role | null; onSelect: (role: Role) => void; onNext: () => void; onBack: () => void }) {
  return (
    <section className="step-content">
      <div className="step-heading"><h2>Select Role</h2><p>Choose the operational role for this session.</p></div>
      <div className="role-grid">{roles.map((role) => { const Icon = role.icon; return <button className={`role-card ${selected === role.id ? 'selected' : ''}`} key={role.id} onClick={() => onSelect(role.id)} aria-pressed={selected === role.id}><span className="role-icon"><Icon size={18} /></span><span className="role-check">{selected === role.id && <Check size={13} />}</span><strong>{role.title}</strong><p>{role.summary}</p><small>Login · {role.login}</small></button> })}</div>
      <div className="action-row"><button className="text-button" onClick={onBack}><ArrowLeft size={15} /> Back</button><button className="primary-button" disabled={!selected} onClick={onNext}>Continue <ArrowRight size={16} /></button></div>
    </section>
  )
}

function LoginForm({ station, role, onBack, onAuthenticated }: { station: Station; role: Role; onBack: () => void; onAuthenticated: (session: Session) => void }) {
  const [username, setUsername] = useState(roles.find((item) => item.id === role)?.login ?? '')
  const [password, setPassword] = useState('')
  const [error, setError] = useState('')
  const [isLoading, setIsLoading] = useState(false)
  const stationInfo = stations.find((item) => item.id === station)!
  const roleInfo = roles.find((item) => item.id === role)!

  async function handleSubmit(event: React.FormEvent<HTMLFormElement>) {
    event.preventDefault()
    setError('')
    setIsLoading(true)
    try {
      let response: Response
      try {
        response = await fetch(`${API_URL}/auth/login`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ username, password, station, role }) })
      } catch {
        throw new Error('AUTH_SERVICE_UNAVAILABLE')
      }
      if (!response.ok) throw new Error('INVALID_CREDENTIALS')
      onAuthenticated(await response.json() as Session)
    } catch (loginError) {
      if (loginError instanceof Error && loginError.message === 'AUTH_SERVICE_UNAVAILABLE') {
        setError('Authentication service unavailable. Start the local FastAPI server and try again.')
      } else {
        setError('Invalid credentials. Please check your username and password.')
      }
    } finally {
      setIsLoading(false)
    }
  }

  return (
    <section className="step-content login-step">
      <div className="context-strip"><span>{stationInfo.code} STATION</span><i /> <span>{roleInfo.title.toUpperCase()}</span></div>
      <div className="step-heading"><h2>Sign In</h2><p>Enter your prototype credentials to continue to your workspace.</p></div>
      <form className="login-form" onSubmit={handleSubmit}>
        <label>Username<input value={username} onChange={(event) => setUsername(event.target.value)} autoComplete="username" required /></label>
        <label>Password<input type="password" value={password} onChange={(event) => setPassword(event.target.value)} autoComplete="current-password" required /></label>
        {error && <p className="form-error" role="alert">{error}</p>}
        <div className="action-row"><button type="button" className="text-button" onClick={onBack}><ArrowLeft size={15} /> Back</button><button className="primary-button" disabled={isLoading}>{isLoading ? 'Checking...' : <>Sign In <ArrowRight size={16} /></>}</button></div>
      </form>
    </section>
  )
}

function PublicWorkflow() {
  const steps = ['STATION STATE', 'SCENARIO', 'PLAN / REQUEST', 'STATION RESPONSE', 'OUTCOME', 'RECONCILIATION']
  return <div className="workflow-wrap"><div className="workflow" aria-label="PRATIBIMB operational workflow">{steps.map((step) => <div className="workflow-step" key={step}><span>{step}</span></div>)}</div><p className="workflow-note">A model-supported coordination loop with a human at the station.</p></div>
}

function PublicStationCard({ station }: { station: StationInfo }) {
  return <article className="public-station-card"><img src={station.image} alt={`${station.name} station`} /><div><em>{station.code}</em><h3>{station.name.replace(' Station', ' Research Station')}</h3><p>{station.location}</p></div></article>
}

function PublicSections() {
  return <div className="public-sections">
    <section className="public-section about-section" id="about">
      <div className="section-inner split-layout"><div className="section-heading"><span className="section-kicker">THE FRAMEWORK</span><span className="section-index">01</span><h2>Built for remote operations in extreme environments.</h2></div><div><p className="section-body">PRATIBIMB connects station-specific digital-twin modeling with a human-in-the-loop remote operations workflow. It enables teams to examine scenarios, coordinate measurement and inspection requests, receive station-side responses, and reconcile reported outcomes.</p><PublicWorkflow /></div></div>
    </section>
    <section className="public-section stations-section" id="stations">
      <div className="section-inner"><div className="section-heading centered"><span className="section-kicker">STATION PROFILES</span><span className="section-index">02</span><h2>Two stations. One operational framework.</h2></div><div className="public-station-grid">{stations.map((station) => <PublicStationCard key={station.id} station={station} />)}</div></div>
    </section>
    <section className="public-section features-section" id="features">
      <div className="section-inner"><div className="section-heading centered"><span className="section-kicker">CAPABILITIES</span><span className="section-index">03</span><h2>One framework for remote operational coordination.</h2></div><div className="feature-grid"><article className="feature-card"><span>01</span><h3>DIGITAL TWIN</h3><p>Model station state. Explore scenarios.</p></article><article className="feature-card"><span>02</span><h3>REMOTE OPERATIONS</h3><p>Coordinate work across HQ and station.</p></article><article className="feature-card"><span>03</span><h3>OFFLINE RESILIENCE</h3><p>Keep working when the link is unavailable.</p></article><article className="feature-card"><span>04</span><h3>EVIDENCE &amp; RECONCILIATION</h3><p>Know what was observed and what was projected.</p></article></div></div>
    </section>
    <section className="public-section contact-section" id="contact">
      <div className="section-inner contact-inner"><div className="section-heading"><span className="section-kicker">THE BRIEF</span><span className="section-index">04</span><h2>Build the next generation of Antarctic operations.</h2></div><div className="contact-content"><p className="section-body">PRATIBIMB is a prototype framework developed around SIH Problem Statement 26060.</p><dl className="contact-details"><div><dt>PROBLEM STATEMENT</dt><dd>PS 26060</dd></div><div><dt>DOMAIN</dt><dd>Smart Automation</dd></div><div><dt>OPERATIONAL CONTEXT</dt><dd>Indian Antarctic Research Stations</dd></div></dl><a className="primary-button contact-cta" href="#access">Enter PRATIBIMB <ArrowRight size={16} /></a></div></div>
    </section>
  </div>
}

function AuthenticatedHeader({session,onSignOut}:{session:Session;onSignOut:()=>void}){
  const [sync,setSync]=useState('CHECKING')
  useEffect(()=>{let active=true;const load=async()=>{try{if(!navigator.onLine){if(active)setSync('OFFLINE');return}const response=await fetch(`${API_URL}/api/sync/status`,{headers:{Authorization:`Bearer ${session.token}`}});if(!response.ok)throw new Error('Sync status unavailable');const data=await response.json();const counts=data.counts||{};const pending=Object.entries(counts).filter(([key])=>['PENDING','SYNCING','FAILED','CONFLICT','BLOCKED_PENDING_PREDECESSOR'].includes(key)).reduce((sum,[,value])=>sum+Number(value||0),0);if(active)setSync(pending?`${pending} PENDING`:data.last_successful_sync?'SYNCED':'NO SYNC RECEIPT')}catch{if(active)setSync('SYNC STATUS UNAVAILABLE')}};void load();const id=window.setInterval(load,15000);return()=>{active=false;window.clearInterval(id)}},[session.token])
  const roleLabel=session.role==='hq'?'HQ OPERATIONS ANALYST':session.role==='station'?'STATION OPERATIONS OFFICER':'TECHNICAL ENGINEER'
  const stationInfo=stations.find(item=>item.id===session.station)!
  return <header className="authenticated-topbar"><a className="authenticated-brand" href="#/app">PRATIBIMB</a><div className="authenticated-context"><span><small>STATION</small><b>{stationInfo.code}</b></span><span><small>ROLE</small><b>{roleLabel}</b></span><span><small>SERVER SYNC</small><b className={sync==='SYNCED'?'sync-ok':sync==='OFFLINE'?'sync-offline':'sync-neutral'}>{sync}</b></span></div><div className="authenticated-user"><span>{session.username}</span><button onClick={onSignOut}>SIGN OUT</button></div></header>
}

function Workspace({ session, onSignOut }: { session: Session; onSignOut: () => void }) {
  const station = stations.find((item) => item.id === session.station)!
  return <div className={`authenticated-shell role-${session.role}`}><AuthenticatedHeader session={session} onSignOut={onSignOut}/>{session.role === 'station' ? <StationWorkspace token={session.token} station={station} username={session.username} /> : session.role === 'hq' ? <div className="hq-shell"><RemoteOperations token={session.token} station={station} username={session.username} /></div> : <TechnicalWorkspace token={session.token} station={station} username={session.username} />}</div>
}

export function App() {
  const [step, setStep] = useState<Step>(1)
  const [station, setStation] = useState<Station | null>(null)
  const [role, setRole] = useState<Role | null>(null)
  const [session, setSession] = useState<Session | null>(() => { try { return JSON.parse(localStorage.getItem('pratibimb-session') || 'null') } catch { return null } })
  const authenticate = (value: Session) => { localStorage.setItem('pratibimb-session', JSON.stringify(value)); setSession(value) }
  const signOut=()=>{if(session)void fetch(`${API_URL}/auth/logout`,{method:'POST',headers:{Authorization:`Bearer ${session.token}`}}).catch(()=>undefined);localStorage.removeItem('pratibimb-session');setSession(null);setStep(1);setStation(null);setRole(null)}
  if (session) return <Workspace session={session} onSignOut={signOut} />
  return <div className="app-shell"><section className="hero-shell"><BackgroundVideo /><Navigation /><main className="hero"><div className="hero-intro"><span className="eyebrow">FOR INDIA'S ANTARCTIC RESEARCH OPERATIONS</span><h1>PRATIBIMB</h1><p className="hero-tagline">Remote Operations. Digital Twin Intelligence.<br />Human-Controlled Decisions.</p><p className="hero-description">A station-specific digital-twin framework connecting HQ planning with human-led operations across Bharati and Maitri.</p></div><div className="access-panel" id="access"><ProgressIndicator step={step} />{step === 1 && <StationSelector selected={station} onSelect={setStation} onNext={() => setStep(2)} />}{step === 2 && <RoleSelector selected={role} onSelect={setRole} onNext={() => setStep(3)} onBack={() => setStep(1)} />}{step === 3 && station && role && <LoginForm station={station} role={role} onBack={() => setStep(2)} onAuthenticated={authenticate} />}</div></main></section><PublicSections /></div>
}
