import { Button, ButtonGroup, Callout, Card, Intent, Tag } from "@blueprintjs/core";
import { useCallback } from "react";

import { switchStrategy, triggerEstop, triggerResume } from "../api";
import type { TabProps } from "../App";

const STRATEGIES = [
  { id: "arcade_drive", label: "Arcade Drive" },
  { id: "tank_drive",   label: "Tank Drive" },
  { id: "arm_control",  label: "Arm Control" },
  { id: "arcade_arm",   label: "Arcade Arm" },
];

const fmt = (v: number | null | undefined, d = 2) =>
  v == null || Number.isNaN(v) ? "—" : v.toFixed(d);

export function ControlTab({ state }: TabProps) {
  const strat = state?.strategy ?? "";
  const isDrive = strat === "arcade_drive" || strat === "tank_drive";

  const onSwitch = useCallback(async (name: string) => {
    try {
      await switchStrategy(name);
    } catch (e) {
      console.error("strategy switch failed:", e);
    }
  }, []);

  const onEstop = useCallback(async () => {
    try { await triggerEstop(); } catch (e) { console.error(e); }
  }, []);
  const onResume = useCallback(async () => {
    try { await triggerResume(); } catch (e) { console.error(e); }
  }, []);

  return (
    <div>
      {!state?.control_active && (
        <Callout intent={Intent.WARNING} icon="warning-sign" style={{ marginBottom: "1em" }}>
          Send gate is closed — commands won't reach the rover unless this tab is selected.
        </Callout>
      )}

      <ButtonGroup style={{ marginBottom: "1em" }}>
        {STRATEGIES.map((s) => (
          <Button
            key={s.id}
            active={strat === s.id}
            intent={strat === s.id ? Intent.PRIMARY : Intent.NONE}
            onClick={() => onSwitch(s.id)}
          >
            {s.label}
          </Button>
        ))}
      </ButtonGroup>

      <div className="grid-2">
        <Card>
          <h3 style={{ marginTop: 0 }}>Sent Commands</h3>

          <div className={`sec-label ${isDrive ? "" : "dim"}`}>Drive</div>
          <div className={isDrive ? "" : "dim"}>
            <div className="row">
              <span className="label">tracks L / R</span>
              <span className="value">
                {fmt(state?.sent?.tracks_left)} / {fmt(state?.sent?.tracks_right)}
              </span>
            </div>
            <div className="row">
              <span className="label">flippers fl/fr/rl/rr</span>
              <span className="value">
                {state?.sent?.flippers
                  ? `${state.sent.flippers.fl} / ${state.sent.flippers.fr} / ${state.sent.flippers.rl} / ${state.sent.flippers.rr}`
                  : "—"}
              </span>
            </div>
          </div>

          <div className={`sec-label ${!isDrive ? "" : "dim"}`}>Arm</div>
          <div className={!isDrive ? "" : "dim"}>
            <div className="row">
              <span className="label">pos x / y / z</span>
              <span className="value">
                {fmt(state?.sent?.ovis.x)} / {fmt(state?.sent?.ovis.y)} / {fmt(state?.sent?.ovis.z)}
              </span>
            </div>
            <div className="row">
              <span className="label">ori yaw/pitch/roll</span>
              <span className="value">
                {fmt(state?.sent?.ovis.yaw)} / {fmt(state?.sent?.ovis.pitch)} / {fmt(state?.sent?.ovis.roll)}
              </span>
            </div>
            <div className="row">
              <span className="label">gripper</span>
              <span className="value">{state?.sent?.gripper_position ?? "—"} / 255</span>
            </div>
          </div>

          <div className="row" style={{ marginTop: "0.5em" }}>
            <span className="label">sent #</span>
            <span className="value">{state?.sent_count ?? 0}</span>
          </div>
        </Card>

        <Card>
          <h3 style={{ marginTop: 0 }}>Telemetry</h3>
          <div className="row">
            <span className="label">timestamp</span>
            <span className="value">{state?.telemetry?.timestamp_us ?? "—"}</span>
          </div>
          <div className="row">
            <span className="label">machine state</span>
            <span className="value">
              <Tag minimal intent={state?.telemetry?.machine_state === 1 ? Intent.SUCCESS : Intent.NONE}>
                {machineStateName(state?.telemetry?.machine_state)}
              </Tag>
            </span>
          </div>
          <div className="joint-grid">
            {(state?.telemetry?.joints ?? []).map((j, i) => (
              <div className="joint-card" key={i}>
                <div className="name">J{i + 1}</div>
                <div className="v">{fmt(j.pos)}°</div>
                <div className="v small">{fmt(j.amp)} A</div>
                <div className="v small">{fmt(j.temp)} °C</div>
              </div>
            ))}
          </div>
          <div className="row" style={{ marginTop: "0.5em" }}>
            <span className="label">received #</span>
            <span className="value">{state?.rx_count ?? 0}</span>
          </div>
        </Card>
      </div>

      <div className="estop-bar">
        {state?.estopped ? (
          <Button large intent={Intent.SUCCESS} icon="play" onClick={onResume}>
            RESUME
          </Button>
        ) : (
          <Button large intent={Intent.DANGER} icon="stop" onClick={onEstop}>
            E-STOP
          </Button>
        )}
        <span style={{ alignSelf: "center", marginLeft: "1em", color: "#a7b6c2" }}>
          Space = E-STOP
        </span>
      </div>
    </div>
  );
}

function machineStateName(state: number | undefined): string {
  if (state === 0) return "idle";
  if (state === 1) return "running";
  if (state === 2) return "error";
  return state == null ? "—" : String(state);
}
