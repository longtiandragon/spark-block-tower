export type Phase = 'lobby' | 'countdown' | 'turning_away' | 'green' | 'turning_back' | 'settling' | 'red' | 'paused' | 'finished' | 'estop'
export type Mode = 'demo' | 'webcam' | 'ros'
export type PlayerStatus = 'active' | 'pending' | 'eliminated' | 'winner'

export interface Player {
  id: number
  name: string
  status: PlayerStatus
  visible: boolean
  motion: number
  confidence: number
  ambiguous?: boolean
  reason?: string
}

export interface Evidence {
  id: string
  player_id: number
  round: number
  score: number
  status: 'pending' | 'confirmed' | 'dismissed'
  before_url: string | null
  after_url: string | null
  storage_error?: string
  saving?: boolean
  created_at: string
  duration_seconds?: number
}

export interface GameConfig {
  players: number
  green_min: number
  green_max: number
  red_seconds: number
  settle_seconds: number
  game_seconds: number
  motion_threshold: number
  hold_seconds: number
  auto_eliminate: boolean
  camera_index: number
  speech_target: 'off' | 'browser' | 'robot'
}

export interface Speech {
  id: string; session_id: string; clip: string; text: string
  target: 'browser' | 'robot'; status: 'pending' | 'claimed' | 'playing' | 'done' | 'failed'
  owner: string | null; required: boolean; error: string | null
}

export interface GameState {
  phase: Phase
  phase_label: string
  mode: Mode
  round: number
  remaining: number
  game_remaining: number
  session_id: string
  message: string
  camera: { ready: boolean; fps: number; width: number; height: number; error: string | null; model: string; last_frame_age: number | null; stable?: boolean; stability_reason?: string }
  robot: { connected: boolean; armed: boolean; yaw: number; status: string; moving?: boolean; clearance?: number | null; fault?: string | null; odom_age?: number; scan_age?: number; audio?: { ready: boolean; error?: string | null } }
  face_yaw: number
  speech: Speech | null
  players: Player[]
  events: { id: string; time: string; kind: string; text: string }[]
  evidence: Evidence[]
  config: GameConfig
  revision: number
  storage?: { error: string | null; pending?: boolean }
}
