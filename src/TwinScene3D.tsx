import { useEffect, useRef } from 'react'
import * as THREE from 'three'
import { OrbitControls } from 'three/addons/controls/OrbitControls.js'

export type TwinNodeId = 'fuel' | 'chp' | 'power' | 'heat' | 'water_production' | 'water_storage' | 'critical_load' | 'deferrable_load'
type FlowState = { chp: 'AVAILABLE' | 'UNAVAILABLE' | 'UNKNOWN'; power: 'ACTIVE' | 'CONSTRAINED' | 'UNKNOWN'; heat: 'ACTIVE' | 'CONSTRAINED' | 'UNKNOWN'; water: 'ACTIVE' | 'CONSTRAINED' | 'UNKNOWN'; scenario: boolean }
type Props = { selected: TwinNodeId | null; state: FlowState; reducedMotion: boolean; onSelect: (id: TwinNodeId) => void; onFailure: () => void }
type ParticleTrack = { mesh: THREE.Mesh; start: THREE.Vector3; end: THREE.Vector3; phase: number; key: 'power' | 'heat' | 'water'; chpDependent: boolean }

const POS: Record<TwinNodeId, THREE.Vector3> = {
  fuel: new THREE.Vector3(-5.2, .95, 0), chp: new THREE.Vector3(-2.05, 1.05, 0),
  power: new THREE.Vector3(.9, .8, 1.85), heat: new THREE.Vector3(.9, .8, -1.85),
  water_production: new THREE.Vector3(4.15, .8, 0), water_storage: new THREE.Vector3(7.05, 1.05, 0),
  critical_load: new THREE.Vector3(4.1, .65, 3.35), deferrable_load: new THREE.Vector3(4.1, .65, -3.35),
}

const componentLabels: Array<{ id: TwinNodeId; label: string }> = [
  { id: 'fuel', label: 'FUEL' }, { id: 'chp', label: 'CHP' }, { id: 'power', label: 'POWER' },
  { id: 'heat', label: 'THERMAL STATE' }, { id: 'water_production', label: 'WATER PRODUCTION' },
  { id: 'water_storage', label: 'WATER STORAGE' }, { id: 'critical_load', label: 'CRITICAL LOAD' },
  { id: 'deferrable_load', label: 'DEFERRABLE LOAD' },
]

function mat(color: string, metalness = .28, roughness = .48) { return new THREE.MeshStandardMaterial({ color, metalness, roughness }) }
function box(w: number, h: number, d: number, material: THREE.Material, x = 0, y = 0, z = 0) {
  const mesh = new THREE.Mesh(new THREE.BoxGeometry(w, h, d), material); mesh.position.set(x, y, z); mesh.castShadow = true; mesh.receiveShadow = true; return mesh
}
function cylinder(radiusTop: number, radiusBottom: number, height: number, material: THREE.Material, x = 0, y = 0, z = 0, segments = 18) {
  const mesh = new THREE.Mesh(new THREE.CylinderGeometry(radiusTop, radiusBottom, height, segments), material); mesh.position.set(x, y, z); mesh.castShadow = true; mesh.receiveShadow = true; return mesh
}

export function TwinScene3D({ selected, state, reducedMotion, onSelect, onFailure }: Props) {
  const hostRef = useRef<HTMLDivElement>(null), labelRefs = useRef<Record<string, HTMLButtonElement | null>>({})
  const selectRef = useRef(onSelect), flowRef = useRef(state), selectedRef = useRef(selected)
  useEffect(() => { selectRef.current = onSelect }, [onSelect])
  useEffect(() => { flowRef.current = state }, [state])
  useEffect(() => { selectedRef.current = selected }, [selected])

  useEffect(() => {
    const host = hostRef.current
    if (!host) return
    let renderer: THREE.WebGLRenderer
    try {
      renderer = new THREE.WebGLRenderer({ antialias: true, alpha: false, powerPreference: 'low-power' })
    } catch { onFailure(); return }
    renderer.setPixelRatio(Math.min(window.devicePixelRatio || 1, 1.5))
    renderer.outputColorSpace = THREE.SRGBColorSpace
    renderer.toneMapping = THREE.ACESFilmicToneMapping
    renderer.toneMappingExposure = 1.08
    renderer.setClearColor('#eaf1f3', 1)
    renderer.domElement.className = 'twin-canvas'
    renderer.domElement.setAttribute('aria-label', 'Interactive three-dimensional model of station energy and water dependencies')
    host.appendChild(renderer.domElement)

    const scene = new THREE.Scene(); scene.background = new THREE.Color('#eaf1f3'); scene.fog = new THREE.Fog('#eaf1f3', 20, 36)
    const camera = new THREE.PerspectiveCamera(35, 1, .1, 80); camera.position.set(10, 10, 15)
    const controls = new OrbitControls(camera, renderer.domElement); controls.target.set(.5, .3, 0); controls.enableDamping = true; controls.dampingFactor = .08; controls.minDistance = 10; controls.maxDistance = 26; controls.maxPolarAngle = Math.PI * .48
    controls.saveState()
    const resetView = () => controls.reset()
    window.addEventListener('pratibimb:twin-reset-view', resetView)
    scene.add(new THREE.HemisphereLight('#ffffff', '#738792', 2.0))
    const key = new THREE.DirectionalLight('#fffdf8', 2.7); key.position.set(-5, 11, 8); scene.add(key)
    const fill = new THREE.DirectionalLight('#bfd5dc', 1.2); fill.position.set(8, 5, -7); scene.add(fill)
    const floor = new THREE.Mesh(new THREE.PlaneGeometry(19, 13), mat('#e2eaec', .06, .8)); floor.rotation.x = -Math.PI / 2; floor.position.y = -.08; floor.receiveShadow = true; scene.add(floor)
    const grid = new THREE.GridHelper(18, 18, '#aebfc5', '#ccd8dc'); grid.position.y = -.065; (grid.material as THREE.Material).transparent = true; (grid.material as THREE.Material).opacity = .28; scene.add(grid)

    const groups: Partial<Record<TwinNodeId, THREE.Group>> = {}, clickable: THREE.Object3D[] = [], pipes: Array<{ mesh: THREE.Mesh; key: 'power' | 'heat' | 'water'; chpDependent: boolean }> = [], particles: ParticleTrack[] = []
    const base = mat('#66808a'), steel = mat('#829aa3', .52), chpBody = mat('#829aa3', .52), dark = mat('#405c68', .55), fuelMat = mat('#8c795e', .5), pale = mat('#c5d6d9', .35)
    const addGroup = (id: TwinNodeId, x = POS[id].x, z = POS[id].z) => { const g = new THREE.Group(); g.position.set(x, 0, z); g.userData.componentId = id; scene.add(g); groups[id] = g; return g }
    const addHit = (g: THREE.Group, w: number, h: number, d: number) => { const hit = new THREE.Mesh(new THREE.BoxGeometry(w, h, d), new THREE.MeshBasicMaterial({ transparent: true, opacity: 0, depthWrite: false })); hit.userData.componentId = g.userData.componentId; g.add(hit); clickable.push(hit) }

    // Fuel storage: horizontal steel tank on two support saddles.
    { const g = addGroup('fuel'); const tank = cylinder(.66, .66, 1.75, fuelMat); tank.rotation.z = Math.PI / 2; tank.position.y = 1.05; g.add(tank); for (const x of [-.5, .5]) g.add(box(.13, .43, .9, dark, x, .34, 0)); g.add(cylinder(.49, .49, .08, steel, -.91, 1.05, 0, 16).applyQuaternion(new THREE.Quaternion().setFromUnitVectors(new THREE.Vector3(0, 1, 0), new THREE.Vector3(1, 0, 0)))); g.add(box(.34, .12, .25, dark, .1, 1.79, 0)); g.add(cylinder(.055, .055, .38, steel, .1, 1.99, 0)); g.add(box(1.98, .05, 1.25, pale, 0, .12, 0)); addHit(g, 2.2, 2.3, 1.65) }

    // CHP housing, engine block, generator coupling, heat exchanger and exhaust stack.
    { const g = addGroup('chp', -2.0, 0); g.add(box(2.35, .14, 1.7, dark, 0, .18, 0)); g.add(box(1.45, 1.13, 1.23, chpBody, -.2, .91, 0)); g.add(box(.85, .72, .97, base, .83, .71, 0)); g.add(cylinder(.26, .26, .18, dark, .83, .71, .58, 20)); g.add(cylinder(.17, .17, .22, pale, .83, .71, .69, 20));
      for (let i = 0; i < 5; i++) g.add(box(.055, .56, .09, dark, -.68 + i * .19, .93, .635))
      g.add(box(.62, .56, .28, mat('#9a8465'), -.52, .58, -.78)); for (let i = 0; i < 4; i++) g.add(box(.06, .45, .32, dark, -.74 + i * .14, .58, -.79))
      const rotor = new THREE.Group(); rotor.position.set(-.2, .95, .64); const hub = cylinder(.11, .11, .09, pale, 0, 0, 0, 14); hub.rotation.x = Math.PI / 2; rotor.add(hub); for (let i = 0; i < 3; i++) { const blade = box(.055, .34, .035, dark, 0, .18, 0); blade.rotation.z = i * Math.PI / 1.5; rotor.add(blade) } g.add(rotor); g.userData.engineRotor = rotor; g.userData.engineBody = chpBody
      g.add(cylinder(.14, .18, .78, dark, -.63, 1.75, -.39)); g.add(cylinder(.22, .22, .06, steel, -.63, 2.15, -.39)); g.add(box(2.52, .05, 1.8, pale, 0, .12, 0));
      const lamp = new THREE.Mesh(new THREE.SphereGeometry(.12, 14, 10), mat('#6a997d', .1, .25)); lamp.position.set(.87, 1.23, .65); g.add(lamp); g.userData.statusLamp = lamp; addHit(g, 2.8, 2.65, 2.1) }

    // Power bus and abstract load blocks deliberately avoid naming specific station equipment.
    { const g = addGroup('power'); g.add(box(1.48, .82, 1.22, base, 0, .78, 0)); g.add(box(1.58, .08, 1.31, pale, 0, .18, 0)); g.add(box(1.12, .18, .09, dark, 0, .83, .63)); for (let i = 0; i < 3; i++) g.add(cylinder(.055, .055, .44, mat('#78939b'), -.42 + i * .42, .81, -.62)); addHit(g, 1.9, 1.65, 1.65) }
    { const g = addGroup('heat'); g.add(box(1.25, .86, 1.05, mat('#8a8171'), 0, .76, 0)); for (let i = 0; i < 6; i++) g.add(box(.08, .67, .12, dark, -.42 + i * .17, .77, .55)); g.add(cylinder(.07, .07, .34, steel, 0, 1.28, -.47)); g.add(box(1.46, .06, 1.22, pale, 0, .15, 0)); addHit(g, 1.85, 1.8, 1.6) }
    { const g = addGroup('water_production'); g.add(box(1.9, .14, 1.56, dark, 0, .16, 0)); g.add(box(1.12, .92, .94, steel, -.28, .71, 0)); g.add(box(.58, .7, .7, base, .55, .59, .05)); g.add(cylinder(.2, .2, .08, pale, -.28, 1.2, .49)); g.add(cylinder(.08, .08, .55, steel, -.28, 1.48, .49)); for (let i = 0; i < 4; i++) g.add(box(.08, .54, .08, dark, -.59 + i * .2, .71, .52)); g.add(box(2.06, .05, 1.72, pale, 0, .1, 0)); addHit(g, 2.3, 2.1, 2) }
    { const g = addGroup('water_storage'); g.add(cylinder(.78, .78, 1.95, mat('#708c96', .4), 0, 1.18, 0, 24)); g.add(cylinder(.8, .8, .09, pale, 0, 2.18, 0, 24)); g.add(cylinder(.08, .08, .5, steel, -.35, 2.43, 0)); g.add(box(1.9, .06, 1.9, pale, 0, .16, 0)); for (const x of [-.48, .48]) g.add(box(.12, .36, .12, dark, x, .34, .48)); addHit(g, 2.15, 2.75, 2.15) }
    { const g = addGroup('critical_load'); g.add(box(1.56, .91, 1.32, mat('#547b87'), 0, .6, 0)); g.add(box(1.68, .08, 1.42, pale, 0, .12, 0)); g.add(box(.9, .08, .07, mat('#d4e1e2'), 0, .63, .68)); addHit(g, 1.9, 1.55, 1.65) }
    { const g = addGroup('deferrable_load'); g.add(box(1.56, .91, 1.32, mat('#87969a'), 0, .6, 0)); g.add(box(1.68, .08, 1.42, pale, 0, .12, 0)); g.add(box(.9, .08, .07, mat('#d4e1e2'), 0, .63, .68)); addHit(g, 1.9, 1.55, 1.65) }

    const pipe = (from: TwinNodeId, to: TwinNodeId, key: 'power' | 'heat' | 'water', color: string, chpDependent = false) => {
      const start = POS[from].clone(); start.y = .68; const end = POS[to].clone(); end.y = .68
      const mid = start.clone().lerp(end, .5); mid.y += .24
      const curve = new THREE.QuadraticBezierCurve3(start, mid, end), mesh = new THREE.Mesh(new THREE.TubeGeometry(curve, 24, .055, 7, false), mat(color, .3, .42)); mesh.userData.flowKey = key; mesh.userData.chpDependent = chpDependent; scene.add(mesh); pipes.push({ mesh, key, chpDependent })
      const bead = new THREE.Mesh(new THREE.SphereGeometry(.075, 10, 8), mat(color, .15, .3)); bead.userData.flowKey = key; scene.add(bead); particles.push({ mesh: bead, start, end, phase: Math.random(), key, chpDependent })
    }
    pipe('fuel', 'chp', 'power', '#9a835f', true)
    pipe('chp', 'power', 'power', '#52859a', true); pipe('chp', 'heat', 'heat', '#ad8b65', true)
    pipe('power', 'water_production', 'power', '#52859a', true); pipe('heat', 'water_production', 'heat', '#ad8b65', true)
    pipe('water_production', 'water_storage', 'water', '#6f9aa4', true)
    pipe('power', 'critical_load', 'power', '#52859a', true); pipe('power', 'deferrable_load', 'power', '#78919a', true)

    const selection = new THREE.BoxHelper(new THREE.Object3D(), '#2d7186'); selection.visible = false; scene.add(selection)
    const raycaster = new THREE.Raycaster(), pointer = new THREE.Vector2()
    const onPointer = (event: PointerEvent) => {
      const rect = renderer.domElement.getBoundingClientRect(); pointer.set(((event.clientX - rect.left) / rect.width) * 2 - 1, -((event.clientY - rect.top) / rect.height) * 2 + 1); raycaster.setFromCamera(pointer, camera)
      const hit = raycaster.intersectObjects(clickable, false)[0]?.object.userData.componentId as TwinNodeId | undefined
      if (hit) selectRef.current(hit)
    }
    renderer.domElement.addEventListener('pointerup', onPointer)

    const resize = () => { const width = host.clientWidth, height = host.clientHeight; if (!width || !height) return; camera.aspect = width / height; camera.updateProjectionMatrix(); renderer.setSize(width, height, false) }
    const observer = new ResizeObserver(resize); observer.observe(host); resize()
    const startTime = performance.now(); let frame = 0
    const render = () => {
      frame = requestAnimationFrame(render); const elapsed = (performance.now() - startTime) / 1000; controls.update()
      const current = flowRef.current
      for (const p of pipes) { const active = current[p.key] === 'ACTIVE' && (!p.chpDependent || current.chp === 'AVAILABLE'); const m = p.mesh.material as THREE.MeshStandardMaterial; m.opacity = active ? .92 : current[p.key] === 'UNKNOWN' ? .28 : .16; m.transparent = true }
      for (const p of particles) { const active = current[p.key] === 'ACTIVE' && (!p.chpDependent || current.chp === 'AVAILABLE'); p.mesh.visible = active && !reducedMotion; if (p.mesh.visible) { const t = (elapsed * .22 + p.phase) % 1; p.mesh.position.lerpVectors(p.start, p.end, t); p.mesh.position.y += Math.sin(t * Math.PI) * .22 } }
      const lamp = groups.chp?.userData.statusLamp as THREE.Mesh | undefined; if (lamp) (lamp.material as THREE.MeshStandardMaterial).color.set(current.chp === 'AVAILABLE' ? '#67967d' : current.chp === 'UNAVAILABLE' ? '#a06e5c' : '#b39a69')
      const engine = groups.chp?.userData.engineBody as THREE.MeshStandardMaterial | undefined; if (engine) engine.color.set(current.chp === 'AVAILABLE' ? '#829aa3' : current.chp === 'UNAVAILABLE' ? '#626f74' : '#777f82')
      const rotor = groups.chp?.userData.engineRotor as THREE.Group | undefined; if (rotor) { if (current.chp === 'AVAILABLE' && !reducedMotion) rotor.rotation.z += .018; else rotor.rotation.z = 0 }
      const selectedId = selectedRef.current, group = selectedId ? groups[selectedId] : undefined
      selection.visible = !!group; if (group) { selection.setFromObject(group); selection.update() }
      for (const item of componentLabels) { const label = labelRefs.current[item.id]; if (!label) continue; const point = POS[item.id].clone(); point.y += item.id === 'chp' ? 2.35 : item.id === 'water_storage' ? 2.7 : 1.8; point.project(camera); const visible = point.z > -1 && point.z < 1; label.style.display = visible ? 'block' : 'none'; if (visible) { const x = (point.x * .5 + .5) * host.clientWidth, y = (-point.y * .5 + .5) * host.clientHeight; label.style.left = `${Math.min(host.clientWidth - 50, Math.max(50, x))}px`; label.style.top = `${Math.min(host.clientHeight - 22, Math.max(22, y))}px`; label.classList.toggle('selected', selectedId === item.id) } }
      renderer.render(scene, camera)
    }
    render()
    return () => { cancelAnimationFrame(frame); observer.disconnect(); window.removeEventListener('pratibimb:twin-reset-view', resetView); controls.dispose(); renderer.domElement.removeEventListener('pointerup', onPointer); renderer.dispose(); scene.traverse(obj => { if (obj instanceof THREE.Mesh) { obj.geometry.dispose(); const materials = Array.isArray(obj.material) ? obj.material : [obj.material]; materials.forEach(m => m.dispose()) } }); host.removeChild(renderer.domElement) }
  }, [reducedMotion, onFailure])

  return <div className="twin-scene-host" ref={hostRef}>
    {componentLabels.map(item => <button key={item.id} ref={el => { labelRefs.current[item.id] = el }} className="twin-scene-label" onClick={() => onSelect(item.id)} aria-label={`Inspect ${item.label.toLowerCase()} component`}>{item.label}</button>)}
  </div>
}
