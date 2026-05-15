/* Centralised fetch wrappers. Every UI call goes through here so retry /
   error handling lives in one place. The browser talks to the local
   ui_server (same origin); ui_server proxies sensor_api + ik_engine. */

export interface TeleopState {
  sent: {
    tracks_left: number;
    tracks_right: number;
    flippers: { fl: number; fr: number; rl: number; rr: number };
    ovis: { x: number; y: number; z: number; yaw: number; pitch: number; roll: number };
    gripper_position: number;
    timestamp_us: number;
  } | null;
  sent_count: number;
  telemetry: {
    timestamp_us: number;
    machine_state: number;
    joints: { pos: number; amp: number; temp: number; state: number }[];
  } | null;
  rx_count: number;
  estopped: boolean;
  strategy: string;
  control_active: boolean;
  stuck_haptic_enabled: boolean;
}

export interface SensorSummary {
  id: string;
  display_name?: string;
  data_port?: number;
  command_port?: number;
  command_mode?: string;
  endpoints?: Record<string, unknown>;
}

export interface SensorInfo {
  id: string;
  data_schema?: { name: string; type_name: string; unit?: string; description?: string }[];
}

export interface SensorLive {
  id: string;
  display_name: string;
  packets: number;
  last_packet_age_s: number | null;
  rate_hz: number | null;
  last_error: string | null;
  latest: Record<string, unknown>;
}

async function jsonRequest<T>(url: string, init?: RequestInit): Promise<T> {
  const r = await fetch(url, init);
  if (!r.ok) {
    let detail = "";
    try { detail = (await r.json()).error ?? ""; } catch { /* ignore */ }
    throw new Error(`${r.status} ${detail || r.statusText}`);
  }
  return r.json() as Promise<T>;
}

// ---- core teleop state -----------------------------------------------------

export const getState = () => jsonRequest<TeleopState>("/state");

export const switchStrategy = (name: string) =>
  jsonRequest<{ strategy: string }>("/strategy", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ name }),
  });

export const triggerEstop = () =>
  jsonRequest<{ estopped: boolean; api_status: string }>("/estop", { method: "POST" });

export const triggerResume = () =>
  jsonRequest<{ estopped: boolean }>("/resume", { method: "POST" });

// ---- send-gate -------------------------------------------------------------

export const setControlActive = (active: boolean) =>
  jsonRequest<{ active: boolean }>("/api/control/active", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ active }),
  });

// ---- sensors (Data tab) ----------------------------------------------------

export const listSensors = () =>
  jsonRequest<{ sensors: SensorSummary[] }>("/api/sensors/discover");

export const getSensorInfo = (id: string) =>
  jsonRequest<SensorInfo>(`/api/sensors/${encodeURIComponent(id)}/info`);

export const setSensorSubscriptions = (ids: string[]) =>
  jsonRequest<{ subscribed: string[] }>("/api/sensors/subscribe", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ ids }),
  });

export const getSensorState = () =>
  jsonRequest<{ sensors: SensorLive[] }>("/api/sensors/state");

// ---- IK engine (Settings tab) ----------------------------------------------

export const getIkCollision = () =>
  jsonRequest<{ enabled: boolean }>("/api/ik/collision");

export const setIkCollision = (enabled: boolean) =>
  jsonRequest<{ ok: boolean; enabled: boolean }>("/api/ik/collision", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ enabled }),
  });

// ---- Haptics (Settings tab) ------------------------------------------------

export const getStuckHaptic = () =>
  jsonRequest<{ enabled: boolean }>("/api/haptics/stuck");

export const setStuckHaptic = (enabled: boolean) =>
  jsonRequest<{ enabled: boolean }>("/api/haptics/stuck", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ enabled }),
  });
