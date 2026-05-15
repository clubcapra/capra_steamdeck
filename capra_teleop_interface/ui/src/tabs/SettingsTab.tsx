import { Callout, Card, Switch } from "@blueprintjs/core";
import { useCallback, useEffect, useState } from "react";

import {
  getIkCollision,
  getStuckHaptic,
  setIkCollision,
  setStuckHaptic,
} from "../api";
import type { TabProps } from "../App";

export function SettingsTab({ active, state }: TabProps) {
  // ---- IK collision toggle (proxied through to rove_ik_engine) -----------
  const [collision, setCollision] = useState<boolean | null>(null);
  const [collisionErr, setCollisionErr] = useState<string>("");
  const [collisionBusy, setCollisionBusy] = useState(false);

  useEffect(() => {
    if (!active) return;
    let cancelled = false;
    (async () => {
      try {
        const r = await getIkCollision();
        if (!cancelled) { setCollision(r.enabled); setCollisionErr(""); }
      } catch (e) {
        if (!cancelled) {
          setCollision(null);
          setCollisionErr(e instanceof Error ? e.message : String(e));
        }
      }
    })();
    return () => { cancelled = true; };
  }, [active]);

  const onToggleCollision = useCallback(async (enabled: boolean) => {
    setCollisionBusy(true);
    setCollisionErr("");
    try {
      const r = await setIkCollision(enabled);
      setCollision(r.enabled);
    } catch (e) {
      setCollisionErr(e instanceof Error ? e.message : String(e));
    } finally {
      setCollisionBusy(false);
    }
  }, []);

  // ---- Stuck-detection haptic --------------------------------------------
  // The /state snapshot already carries `stuck_haptic_enabled`, so we
  // initialise from there and update locally on toggle.
  const [stuckHaptic, setStuckHapticState] = useState<boolean | null>(null);
  const [hapticErr, setHapticErr] = useState<string>("");
  const [hapticBusy, setHapticBusy] = useState(false);

  useEffect(() => {
    if (state == null) return;
    // Trust the live snapshot; only fetch directly if it's missing.
    setStuckHapticState(state.stuck_haptic_enabled);
  }, [state]);

  useEffect(() => {
    if (!active || stuckHaptic !== null) return;
    let cancelled = false;
    (async () => {
      try {
        const r = await getStuckHaptic();
        if (!cancelled) { setStuckHapticState(r.enabled); setHapticErr(""); }
      } catch (e) {
        if (!cancelled) setHapticErr(e instanceof Error ? e.message : String(e));
      }
    })();
    return () => { cancelled = true; };
  }, [active, stuckHaptic]);

  const onToggleStuckHaptic = useCallback(async (enabled: boolean) => {
    setHapticBusy(true);
    setHapticErr("");
    try {
      const r = await setStuckHaptic(enabled);
      setStuckHapticState(r.enabled);
    } catch (e) {
      setHapticErr(e instanceof Error ? e.message : String(e));
    } finally {
      setHapticBusy(false);
    }
  }, []);

  return (
    <div style={{ display: "flex", flexDirection: "column", gap: "1em" }}>
      <Card>
        <h3 style={{ marginTop: 0 }}>IK Engine</h3>
        {collisionErr && (
          <Callout intent="danger" icon="error" style={{ marginBottom: "0.7em" }}>
            {collisionErr}
          </Callout>
        )}
        <Switch
          large
          checked={collision === true}
          disabled={collision === null || collisionBusy}
          onChange={(e) => void onToggleCollision(e.currentTarget.checked)}
          label="Collision-aware IK"
        />
        <div style={{ color: "#a7b6c2", marginTop: "0.2em" }}>
          Disable when the arm has locked itself into a self-collision and the
          engine is rejecting every IK step. Re-enable once the arm is freed.
        </div>
      </Card>

      <Card>
        <h3 style={{ marginTop: 0 }}>Haptics</h3>
        {hapticErr && (
          <Callout intent="danger" icon="error" style={{ marginBottom: "0.7em" }}>
            {hapticErr}
          </Callout>
        )}
        <Switch
          large
          checked={stuckHaptic === true}
          disabled={stuckHaptic === null || hapticBusy}
          onChange={(e) => void onToggleStuckHaptic(e.currentTarget.checked)}
          label="Stuck-detection rumble"
        />
        <div style={{ color: "#a7b6c2", marginTop: "0.2em" }}>
          Subscribes to the VectorNav IMU. While tracks are commanded but the
          IMU sees neither translation (vel_north/east) nor rotation (gyro_z),
          the controller rumbles after ~0.6 s of immobility. In-place pivots
          (left ≈ −right) register as rotation, so they don't trigger the
          alarm.
        </div>
      </Card>
    </div>
  );
}
